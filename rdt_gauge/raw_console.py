"""手动收发报文（调试用，相当于自己做的串口调试助手）。

输入一条不带地址和 CRC 的报文，例如 `03 0050 0002`，自动补上地址和 CRC 发出去，把回复按字段拆开显示。
默认只允许 03 读；要发 10 写，必须 allow_write=True，并且写的每个寄存器都在白名单里、值在允许范围内。
写法参考：r11_upper_computer/wuji_r11/acquisition/protocols/force_sensor.py 的帧检查和 last_frame（原始字节和拆解
结果都给出来）、r11_upper_computer/tools/gauge_probe.py 的白名单（只能写 WRITABLE、值在 LIMITS 内）。

这里不做"写完读回"：手动收发就是要看设备原样回什么。要可靠地改设置用 settings_ops（cli 的 set 命令）。
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

from .contracts import BenchError, DeviceRejected, NotAllowed
from .protocol import MAX_READ_COUNT, READ_HOLDING, WRITE_MULTIPLE, crc16, read_reply_length
from .registers import BY_ADDRESS, LINK_REGISTERS, LOCK_VALUE, UNLOCK_REGISTER, UNLOCK_VALUE, describe, is_writable

HELP = """输入格式（十六进制，空格随意；地址和 CRC 自动补）：
  03 起始地址 数量          读寄存器，例：03 0050 0002（读毛重）、03 0008 0001（读状态）
  10 起始地址 数量 字节数 数据…   写寄存器（要 --allow-write），例：10 0022 0001 02 0000（滤波类型改 0）
也可以写十进制：r 80 2（读 80 起 2 个）、w 34 0（寄存器 34 写 0）。输入 help 看这段，quit 退出。"""


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    body: bytes  # 不含地址和 CRC：功能码 + 数据
    function: int
    start: int
    count: int
    values: tuple[int, ...] = ()

    @property
    def reply_bytes(self) -> int:
        return read_reply_length(self.count) if self.function == READ_HOLDING else 8


def parse_command(text: str) -> ParsedRequest:
    """把一行输入变成请求。格式不对抛 ValueError（不发任何东西）。"""
    words = text.strip().split()
    if not words:
        raise ValueError('空输入')
    if words[0].lower() in ('r', 'w'):
        return _parse_decimal(words)
    try:
        body = bytes.fromhex(''.join(words))
    except ValueError:
        raise ValueError(f'不是十六进制：{text!r}') from None
    if not body:
        raise ValueError('空输入')
    function = body[0]
    if function == READ_HOLDING:
        if len(body) != 5:
            raise ValueError('03 读要 5 个字节：03 起始地址(2) 数量(2)')
        start, count = struct.unpack('>HH', body[1:5])
        if not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f'一次读 1–{MAX_READ_COUNT} 个寄存器')
        return ParsedRequest(body, function, start, count)
    if function == WRITE_MULTIPLE:
        if len(body) < 6:
            raise ValueError('10 写至少要：10 起始地址(2) 数量(2) 字节数(1) 数据…')
        start, count = struct.unpack('>HH', body[1:5])
        nbytes = body[5]
        if count < 1 or nbytes != 2 * count or len(body) != 6 + nbytes:
            raise ValueError('10 写的数量、字节数和数据长度对不上')
        values = struct.unpack(f'>{count}H', body[6:])
        return ParsedRequest(body, function, start, count, values)
    raise ValueError(f'只支持功能码 03（读）和 10（写），不支持 0x{function:02X}')


def _parse_decimal(words: list[str]) -> ParsedRequest:
    try:
        numbers = [int(word, 0) for word in words[1:]]
    except ValueError:
        raise ValueError('r/w 后面要跟十进制（或 0x 开头的十六进制）数') from None
    if words[0].lower() == 'r':
        if len(numbers) not in (1, 2):
            raise ValueError('读：r 起始地址 [数量]，例 r 80 2')
        start, count = numbers[0], numbers[1] if len(numbers) == 2 else 1
        return parse_command(struct.pack('>BHH', READ_HOLDING, start, count).hex())
    if len(numbers) < 2:
        raise ValueError('写：w 起始地址 值 [值…]，例 w 34 0')
    start, values = numbers[0], numbers[1:]
    if any(not 0 <= value <= 0xFFFF for value in values):
        raise ValueError('每个值要在 0–65535 之间')
    body = struct.pack(f'>BHHB{len(values)}H', WRITE_MULTIPLE, start, len(values), 2 * len(values), *values)
    return parse_command(body.hex())


def check_allowed(request: ParsedRequest, *, allow_write: bool) -> None:
    """读随便读；写要 allow_write，而且只能写白名单里的寄存器、值在允许范围内（解锁和上锁寄存器 5 除外）。"""
    if request.function == READ_HOLDING:
        return
    if not allow_write:
        raise NotAllowed('默认只读：要发写命令，启动时加 --allow-write')
    for offset, value in enumerate(request.values):
        address = request.start + offset
        if address == UNLOCK_REGISTER:
            if value not in (UNLOCK_VALUE, LOCK_VALUE):
                raise NotAllowed(f'寄存器 5 只能写 0x{UNLOCK_VALUE:04X}（解锁）或 0x{LOCK_VALUE:04X}（上锁），不在白名单里的值')
            continue
        if not is_writable(address):
            raise NotAllowed(f'寄存器 {address} 不在白名单里，不允许写')
        item = BY_ADDRESS[address]
        if item.name in LINK_REGISTERS:
            raise NotAllowed(f'{item.label} 改了就连不上，用 set-link 改（会自动切换并读回确认）')
        if item.words != 1:
            raise NotAllowed(f'寄存器 {address} 是 32 位的，手动收发不写')
        if not item.accepts(value):
            raise NotAllowed(f'{item.label}（寄存器 {address}）不能写 {value}')


def explain_reply(frame: bytes, request: ParsedRequest, address: int) -> list[str]:
    """把回复拆开成几行说明。不抛错：手动收发就是要看清楚设备回了什么，包括坏帧。"""
    lines = [f'收到 {len(frame)} 字节：{frame.hex(" ").upper() or "（没有）"}']
    if not frame:
        return lines + ['超时：什么也没收到。查接线（A、B 是否接反）、波特率、停止位、地址。']
    crc_ok = len(frame) >= 4 and crc16(frame) == 0  # 整帧连 CRC 一起算是 0 就对
    lines.append(f'CRC：{"对" if crc_ok else "不对"}')
    if len(frame) >= 2 and frame[0] != address:
        lines.append(f'地址：回复里是 {frame[0]}，问的是 {address}')
    if len(frame) == 5 and frame[1] == request.function | 0x80:
        exc = DeviceRejected(request.function, frame[2])
        return lines + [f'设备拒绝：{exc}（02 地址不对、03 数据不对、04 设备执行失败，常见原因是配置锁着）']
    if request.function == READ_HOLDING and len(frame) == request.reply_bytes and crc_ok:
        words = struct.unpack(f'>{request.count}H', frame[3:-2])
        for offset, word in enumerate(words):
            register = request.start + offset
            item = BY_ADDRESS.get(register)
            name = f'{item.label}（{item.name}）' if item else '（协议里没写）'
            text = describe(item, word) if item is not None and item.words == 1 else ''
            lines.append(f'  寄存器 {register:>3} = 0x{word:04X} = {word:>5}  {name} {text}'.rstrip())
    elif request.function == WRITE_MULTIPLE and len(frame) == 8 and crc_ok:
        lines.append('写入回显：' + ('和请求一致' if frame[1:6] == request.body[:5] else '和请求对不上'))
    elif crc_ok:
        lines.append(f'长度不对：应为 {request.reply_bytes} 字节')
    return lines


def send(client, request: ParsedRequest, *, allow_write: bool) -> list[str]:
    """检查、发送、拆解回复。返回给人看的几行。"""
    check_allowed(request, allow_write=allow_write)
    frame_without_crc = bytes([client.link.address]) + request.body
    try:
        frame = client.raw(frame_without_crc, request.reply_bytes)
    except BenchError:
        frame = client.last_frame  # 空就是超时：explain_reply 会给出排查办法
    sent = client.last_request.hex(' ').upper()
    return [f'发送：{sent}'] + explain_reply(frame, request, client.link.address)


def repl(client, *, allow_write: bool, input_fn=input, print_fn=print) -> None:
    """交互循环：一行一条命令，quit 或 Ctrl+Z 回车退出。"""
    print_fn(HELP)
    print_fn(f'当前：{client.link.port} {client.link.baudrate} 8{client.link.parity}{client.link.stopbits} '
             f'地址 {client.link.address}；{"允许写（只能写白名单）" if allow_write else "只读"}')
    while True:
        try:
            text = input_fn('> ').strip()
        except EOFError:
            return
        if text.lower() in ('quit', 'exit', 'q'):
            return
        if text.lower() in ('help', '?', ''):
            print_fn(HELP)
            continue
        try:
            lines = send(client, parse_command(text), allow_write=allow_write)
        except (ValueError, NotAllowed) as exc:
            lines = [f'没发送：{exc}']
        for line in lines:
            print_fn(line)
