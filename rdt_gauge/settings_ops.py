"""查看、备份、修改、恢复设置；改通信参数（波特率、帧格式、地址）。

默认只读：不加 apply=True 只列出要改什么，一个寄存器也不写。写法参考（登记在 docs/SOURCES.json）：
- read_all：r11_upper_computer/tools/gauge_probe.py 的 read_all、cybsq_probe.py 的 read_all —— 分块读全部寄存器，
  读不到的块记下来，不中断。
- change_settings：r11_upper_computer/tools/cybsq_configure.py 的 configure —— 先只读，有读不到的就不写；只改有差异的；
  不加 apply 只列计划；（需要时）解锁 → 逐个写、立刻读回 → finally 里一定重新上锁 → 全部再读一遍核对。
  加上 gauge_probe.py 的 hold：**动手之前**先把原值写到磁盘上的记录文件里，改坏了也能照着恢复。
- restore：gauge_probe.py 的 restore —— 照记录写回原值，写完读回核对。
- change_link：cybsq_configure.py 的 set_baud 和 finish —— 设备按新参数回复，回显允许收不到；换成新参数读回确认，
  再上锁、全部读一遍。

和 R11 不一样的地方（按锐力德协议改）：
- 锐力德的锁（寄存器 5）只管地址、波特率、帧格式、协议类型、恢复出厂（协议 1.6）；滤波、速度、跟踪这些不用解锁。
  change_settings 默认不解锁；如果设备锁住时拒绝写滤波（报 Modbus 异常），提示加 unlock=True 再试。
- 改通信参数后新旧参数都连不上时，提示锐力德的恢复办法（说明书第 4 节：按住按键上电 3 秒恢复通信参数）。
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import time

from .contracts import BenchError, DeviceRejected, LinkSettings, NotAllowed, StatusBits, WriteReceipt
from .ports import apply_link
from .protocol import ModbusClient
from .registers import (BACKUP_BLOCKS, BAUD_TO_CODE, BY_NAME, FRAME_CODES, LINK_REGISTERS, LOCK_VALUE, MUST_BE_OFF,
                        PROBE_BLOCKS, REGISTERS, TEST_PRESET, UNIT_CODES, UNLOCK_REGISTER, UNLOCK_VALUE, describe,
                        spec, value_from)
from .storage import read_json, write_json

RECOVERY_HINT = ('新旧通信参数都连不上：断电，按住变送器上的轻触按键再上电，按住 3 秒，通信参数恢复出厂'
                 '（9600、地址 1，帧格式见说明书）；然后用 scan 重新找。')
LOCKED_HINT = '设备拒绝写入：可能是配置锁着，加 --unlock 再试（会先解锁、写完重新上锁）。'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _error_text(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {exc}'


# ---------------- 读 ----------------

def read_all(client: ModbusClient, *, attempts: int = 2) -> dict:
    """读寄存器表里全部可读的寄存器。读不到的块记在 errors，不中断；9、10 试读结果记在 probes。"""
    raw: dict[int, int] = {}
    errors = []
    for start, count in BACKUP_BLOCKS:
        try:
            values = client.read(start, count, attempts=attempts)
        except (BenchError, OSError) as exc:
            errors.append(dict(start=start, count=count, error=_error_text(exc), raw_hex=client.last_frame.hex(' ')))
            continue
        raw.update({start + offset: value for offset, value in enumerate(values)})
    probes = {}
    for start, count in PROBE_BLOCKS:
        try:
            probes[str(start)] = dict(ok=True, values=client.read(start, count, attempts=1))
        except (BenchError, OSError) as exc:
            probes[str(start)] = dict(ok=False, error=_error_text(exc))
    values = {item.name: value_from(raw, item) for item in REGISTERS if item.readable}
    return dict(read_at=_now(), link=asdict(client.link), registers={str(a): v for a, v in sorted(raw.items())},
                values=values, errors=errors, probes=probes)


def display_rows(snapshot: dict) -> list[dict]:
    """给界面和命令行显示用：每个可读寄存器一行，带中文名、翻译后的值、能不能写。"""
    rows = []
    for item in REGISTERS:
        if not item.readable:
            continue
        value = snapshot['values'].get(item.name)
        rows.append(dict(address=item.address, name=item.name, label=item.label, value=value,
                         text=describe(item, value), writable=item.writable, test_value=item.test_value,
                         note=item.note))
    return rows


def status_of(snapshot: dict) -> StatusBits | None:
    value = snapshot['values'].get('status')
    return None if value is None else StatusBits.from_register(value)


def reading_scale(snapshot: dict) -> tuple[int, str]:
    """采集要用的小数位（寄存器 8）和单位（寄存器 104）。读不到就报错，不猜。"""
    status = status_of(snapshot)
    unit_code = snapshot['values'].get('unit_code')
    if status is None or unit_code is None:
        raise BenchError('没读到状态寄存器 8 或单位寄存器 104，不知道读数怎么换算')
    return status.decimals, UNIT_CODES.get(unit_code, f'unknown_{unit_code}')


def check_test_settings(snapshot: dict) -> list[str]:
    """和测试设置（registers.TEST_PRESET）不一样的地方，每条一句话。空列表表示全部符合。

    R11 遇到追零类没关是直接拒绝开始（cybsq_v5.MUST_BE_OFF）；台架上可能要故意打开做对照，所以这里只警告。
    """
    values = snapshot['values']
    warnings = []
    for name, wanted in TEST_PRESET.items():
        actual = values.get(name)
        item = spec(name)
        if actual is None:
            warnings.append(f'{item.label}（寄存器 {item.address}）没读到')
        elif actual != wanted:
            why = '会在测试中途改读数' if name in MUST_BE_OFF else '和测试设置不同'
            warnings.append(f'{item.label}（寄存器 {item.address}）是 {describe(item, actual)}，测试设置是 '
                            f'{describe(item, wanted)}：{why}')
    if values.get('unit_code') == 0:
        warnings.append('单位（寄存器 104）是 0（无）：读数换算不成牛顿，只记原始整数')
    return warnings


def backup(client: ModbusClient, backup_dir) -> tuple[Path, dict]:
    """读全部寄存器，存成带时间的 JSON。返回文件路径和内容。"""
    snapshot = read_all(client)
    return write_json(_record_path(backup_dir, 'backup'), snapshot), snapshot


def _record_path(directory, kind: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for index in range(1, 1000):
        path = directory / (f'{kind}-{stamp}.json' if index == 1 else f'{kind}-{stamp}-{index}.json')
        if not path.exists():
            return path
    raise RuntimeError(f'{directory} 下同名记录太多')


# ---------------- 改普通寄存器 ----------------

def plan_changes(values: dict, wanted: dict) -> list[dict]:
    """当前值 → 想要的值，算出要写哪些寄存器。一样的跳过。任何一项不允许就整体拒绝（NotAllowed），一个都不写。"""
    planned = []
    for name, new in wanted.items():
        item = BY_NAME.get(name)
        if item is None:
            raise NotAllowed(f'寄存器表里没有 {name!r}')
        if name in LINK_REGISTERS:
            raise NotAllowed(f'{item.label} 改了就连不上，要用 change_link（命令 set-link）单独改')
        if not item.writable:
            raise NotAllowed(f'{item.label}（寄存器 {item.address}）不在白名单里，不允许写')
        if not item.accepts(new):
            allowed = list(item.choices) if item.choices else f'{item.low}–{item.high}'
            raise NotAllowed(f'{item.label}（寄存器 {item.address}）不能写 {new!r}，允许的值：{allowed}')
        old = values.get(name)
        if old is None:
            raise NotAllowed(f'{item.label}（寄存器 {item.address}）没读到当前值，不写')
        if old != new:
            planned.append(dict(name=name, address=item.address, label=item.label, old=old, new=new,
                                old_text=describe(item, old), new_text=describe(item, new)))
    return planned


def change_settings(client: ModbusClient, wanted: dict, *, record_dir, apply: bool = False,
                    unlock: bool = False) -> dict:
    """把 wanted 里的寄存器改成给定值。apply=False 只读、列计划；apply=True 才写。

    结果里 ok 为 True 的条件：没出错、每一项最后读回都等于想要的值、改完的全部读取没有读不到的块。
    """
    before = read_all(client)
    result = dict(kind='change', at=_now(), wanted=dict(wanted), before=before, planned=[], receipts=[],
                  applied=apply, unlock=unlock, ok=False, problem=None)
    if before['errors']:
        result['problem'] = '改之前的全部读取有读不到的寄存器，不写'
        return result
    result['planned'] = plan_changes(before['values'], wanted)
    if not apply or not result['planned']:
        result.update(ok=True, already=not result['planned'])
        return result
    record = _record_path(record_dir, 'change')
    result['record'] = str(record)
    write_json(record, result)  # 原值先落盘，再动设备
    problem = None
    try:
        if unlock:
            client.write(UNLOCK_REGISTER, [UNLOCK_VALUE])
        for change in result['planned']:
            client.write(change['address'], [change['new']])
            read_back = client.read(change['address'], 1, attempts=3)[0]
            receipt = WriteReceipt(change['address'], change['name'], change['old'], change['new'], read_back)
            result['receipts'].append(dict(asdict(receipt), ok=receipt.ok))
            if not receipt.ok:
                raise BenchError(f'{change["label"]} 写 {change["new"]}，读回 {read_back}')
    except DeviceRejected as exc:
        problem = f'{_error_text(exc)}。{"" if unlock else LOCKED_HINT}'
    except (BenchError, OSError) as exc:
        problem = _error_text(exc)
    finally:
        if unlock:
            try:
                client.write(UNLOCK_REGISTER, [LOCK_VALUE])
            except (BenchError, OSError) as exc:
                problem = (problem + '；' if problem else '') + f'没能重新上锁：{_error_text(exc)}'
    after = read_all(client)
    wrong = {name: after['values'].get(name) for name, value in wanted.items() if after['values'].get(name) != value}
    result.update(after=after, wrong=wrong, problem=problem,
                  ok=problem is None and not wrong and not after['errors'])
    write_json(record, result)
    return result


def preset_test(client: ModbusClient, **kwargs) -> dict:
    """一键改成测试设置：跟踪、清零全关，滤波关，应答延时 0，双向。"""
    return change_settings(client, dict(TEST_PRESET), **kwargs)


def restorable_values(record: dict) -> dict:
    """从备份文件（backup 的输出）或修改记录（change 的输出）里取出能写回的原值：只要白名单里的普通寄存器。"""
    source = record['before']['values'] if 'before' in record else record['values']
    return {name: value for name, value in source.items()
            if value is not None and name in BY_NAME and BY_NAME[name].writable and name not in LINK_REGISTERS}


def restore(client: ModbusClient, record_path, **kwargs) -> dict:
    """照备份或修改记录写回原值（通信参数不在内，那个用 change_link）。"""
    result = change_settings(client, restorable_values(read_json(record_path)), **kwargs)
    result['restored_from'] = str(record_path)
    return result


# ---------------- 改通信参数 ----------------

def change_link(client: ModbusClient, *, baudrate: int | None = None, frame_code: int | None = None,
                address: int | None = None, record_dir, apply: bool = False, settle_s: float = 0.3) -> dict:
    """改波特率、帧格式或地址，一次只改一项。成功后 client.link 换成新参数。

    步骤：全部读一遍 → 记录原值 → 解锁 → 写（回显允许收不到）→ 电脑换成新参数读回确认 → 上锁 → 新参数下全部读一遍。
    新参数下读不回来就换回旧参数试：旧参数能通说明没改成（照样上锁）；两边都不通就给出恢复办法。
    """
    chosen = [value for value in (baudrate, frame_code, address) if value is not None]
    if len(chosen) != 1:
        raise NotAllowed('波特率、帧格式、地址一次只改一项')
    old_link = client.link
    if baudrate is not None:
        if baudrate not in BAUD_TO_CODE:
            raise NotAllowed(f'锐力德不支持波特率 {baudrate}，可选：{sorted(BAUD_TO_CODE)}')
        name, code, new_link = 'baud_code', BAUD_TO_CODE[baudrate], replace(old_link, baudrate=baudrate)
    elif frame_code is not None:
        if frame_code not in FRAME_CODES:
            raise NotAllowed(f'帧格式编码要是 {sorted(FRAME_CODES)} 之一')
        parity, stopbits = FRAME_CODES[frame_code]
        name, code, new_link = 'frame_code', frame_code, replace(old_link, parity=parity, stopbits=stopbits)
    else:
        if not spec('module_address').accepts(address):
            raise NotAllowed('地址要在 1–247 之间')
        name, code, new_link = 'module_address', address, replace(old_link, address=address)
    item = spec(name)
    before = read_all(client)
    result = dict(kind='link', at=_now(), register=name, new=code, old_link=asdict(old_link),
                  new_link=asdict(new_link), before=before, applied=apply, steps=[], ok=False, problem=None)
    if before['errors']:
        result['problem'] = '改之前的全部读取有读不到的寄存器，不改'
        return result
    result['old'] = before['values'].get(name)
    if result['old'] == code:
        result.update(ok=True, already=True)  # 已经是这个值：现在能通，说明电脑这边本来就是它
        return result
    if not apply:
        result.update(ok=True, already=False)
        return result
    record = _record_path(record_dir, 'link')
    result['record'] = str(record)
    write_json(record, result)

    def switch(link: LinkSettings) -> None:
        apply_link(client.port, link)
        client.link = link
        if settle_s:
            time.sleep(settle_s)
        client.port.reset_input_buffer()

    def read_register(link: LinkSettings) -> int | None:
        switch(link)
        try:
            return client.read(item.address, 1, attempts=3)[0]
        except (BenchError, OSError):
            return None

    try:
        client.write(UNLOCK_REGISTER, [UNLOCK_VALUE])
        result['steps'].append('已解锁')
        echoed = client.write(item.address, [code], reply_optional=True)
        result['steps'].append(f'已写入（{"收到回显" if echoed else "没收到回显，按新参数核对"}）')
    except (BenchError, OSError) as exc:
        result['problem'] = f'写入失败，设备还是旧参数：{_error_text(exc)}'
        _lock_quietly(client, result)
        write_json(record, result)
        return result
    if read_register(new_link) == code:
        result['steps'].append('新参数下读回一致')
        _lock_quietly(client, result)
        after = read_all(client)
        result.update(after=after, ok=not after['errors'] and result['problem'] is None)
    elif read_register(old_link) is not None:
        result['problem'] = '设备还在旧参数上，没改成'
        _lock_quietly(client, result)
    else:
        result['problem'] = RECOVERY_HINT  # 设备状态不明；电脑这边停在旧参数上（最后试的那个），两者一致
    result['link'] = asdict(client.link)
    write_json(record, result)
    return result


def _lock_quietly(client: ModbusClient, result: dict) -> None:
    try:
        client.write(UNLOCK_REGISTER, [LOCK_VALUE])
        result['steps'].append('已重新上锁')
    except (BenchError, OSError) as exc:
        result['problem'] = (result['problem'] + '；' if result['problem'] else '') + f'没能重新上锁：{_error_text(exc)}'
