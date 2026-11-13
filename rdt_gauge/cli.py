"""命令行入口：把各模块串起来。`python -m rdt_gauge.cli <命令> --help` 看每个命令的参数。

写法参考：r11_upper_computer/tools/gauge_probe.py、cybsq_probe.py、cybsq_configure.py —— 文件开头列用法、一个子命令
做一件事、输出放进带时间的目录；会写设备的命令**默认只读、只列计划，加 --apply 才写**（照 cybsq_configure.py）。

用法（--port 也可以换成 --serial-number；都不给时，本机正好一个 FTDI 串口才自动用它）：
    python -m rdt_gauge.cli ports                                   # 列串口和 FTDI 延迟，不打开任何串口
    python -m rdt_gauge.cli scan --port COM9                        # 找通信参数，只发 03 读
    python -m rdt_gauge.cli info --port COM9                        # 读全部设置并翻译
    python -m rdt_gauge.cli backup --port COM9                      # 备份全部设置
    python -m rdt_gauge.cli live --port COM9 --seconds 60 --label 空载   # 采集 + 实时波形（--no-plot 只采集）
    python -m rdt_gauge.cli stats <采集目录>                        # 不连设备：从 samples.csv 重算统计
    python -m rdt_gauge.cli raw --port COM9                         # 手动收发（--allow-write 才能写白名单）
    python -m rdt_gauge.cli set --port COM9 filter_type=0 filter_strength=0   # 只列计划；加 --apply 才写
    python -m rdt_gauge.cli preset-test --port COM9                 # 改成测试设置（同样要 --apply）
    python -m rdt_gauge.cli restore --port COM9 <备份或修改记录.json>   # 写回原值（同样要 --apply）
    python -m rdt_gauge.cli set-link --port COM9 --to-baud 921600   # 改波特率 / --to-frame / --to-address（要 --apply）
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import math
from pathlib import Path
import sys

from .capture import NEWTONS_PER_UNIT, CaptureConfig, CaptureSession, compute_stats, min_step
from .contracts import BAUDRATES, READ_BLOCK, READ_MODES, BenchError, LinkSettings
from .live_plot import gui_problem, run_live_window
from .ports import SCAN_BAUDRATES, ftdi_latency_ms, list_ports, open_serial, resolve_port, scan
from .protocol import ModbusClient
from . import raw_console
from .registers import FRAME_CODES, TEST_PRESET
from . import settings_ops as ops
from .storage import default_data_root, new_run_dir, read_json, read_samples

# 第七节 ③ 的合格线，只给 stats 当参考：对应的测试（空载静置、最快速度读）才有意义。
NEW_VALUE_RATE_MIN_HZ = 250  # C1 必须
NEW_VALUE_RATE_GOOD_HZ = 1000  # C2 最好
NOISE_STD_MAX_N = 0.01  # D1
STEP_MAX_N = 0.002  # D3


def _int(text: str) -> int:
    return int(text, 0)  # 十进制，或 0x 开头的十六进制


def _addresses(text: str) -> tuple[int, ...]:
    """'1' → (1,)；'1-7' → (1, …, 7)；'1,6' → (1, 6)。"""
    found: list[int] = []
    for part in text.split(','):
        low, _, high = part.partition('-')
        found.extend(range(_int(low), _int(high or low) + 1))
    if not found or any(not 1 <= address <= 247 for address in found):
        raise argparse.ArgumentTypeError('地址要在 1–247 之间，例：1、1-7、1,6')
    return tuple(found)


def _bauds(text: str) -> tuple[int, ...]:
    values = tuple(_int(part) for part in text.split(','))
    if any(value not in BAUDRATES for value in values):
        raise argparse.ArgumentTypeError(f'波特率要在 {BAUDRATES} 里')
    return values


def _assignment(text: str) -> tuple[str, int]:
    name, sep, value = text.partition('=')
    if not sep or not name:
        raise argparse.ArgumentTypeError(f'要写成 名字=值，例 filter_type=0，现在是 {text!r}')
    try:
        return name.strip(), _int(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f'{name} 的值要是整数：{value!r}') from None


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--data-root', type=Path, default=None,
                        help=f'数据和备份放哪（默认 {default_data_root()}，在仓库外）')
    link = argparse.ArgumentParser(add_help=False)
    group = link.add_argument_group('连接')
    group.add_argument('--port', help='COM 号，例 COM9')
    group.add_argument('--serial-number', help='按 USB 序列号找串口（换 USB 口也找得到）')
    group.add_argument('--baud', type=int, default=9600, choices=BAUDRATES, metavar='BAUD', help='波特率，默认出厂 9600')
    group.add_argument('--address', type=int, default=1, help='设备地址，默认 1（以拨码开关为准）')
    group.add_argument('--parity', default='N', choices=('N', 'E', 'O'), help='校验位，默认 N')
    group.add_argument('--stopbits', type=int, default=2, choices=(1, 2), help='停止位，默认 2（协议表写出厂 8N2）')
    group.add_argument('--timeout-ms', type=float, default=None, help='等回复的最长时间，默认按波特率算')
    write = argparse.ArgumentParser(add_help=False)
    write.add_argument('--apply', action='store_true', help='真的写进设备（不加只读、只列计划）')
    write.add_argument('--unlock', action='store_true', help='写之前先解锁、写完重新上锁（设备拒绝写入时再加）')

    parser = argparse.ArgumentParser(prog='python -m rdt_gauge.cli', description='锐力德 RDT-TVK 测力计台架测试脚本',
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    commands = parser.add_subparsers(dest='command', required=True, metavar='命令')
    commands.add_parser('ports', parents=[common], help='列串口和 FTDI 延迟（不打开串口）')
    p = commands.add_parser('scan', parents=[common], help='找通信参数（只发 03 读）')
    p.add_argument('--port')
    p.add_argument('--serial-number')
    p.add_argument('--bauds', type=_bauds, default=SCAN_BAUDRATES, help='要试的波特率，逗号隔开')
    p.add_argument('--addresses', type=_addresses, default=(1,), help='要试的地址，例 1 或 1-7')
    p.add_argument('--all', action='store_true', help='找到能通的也接着试完')
    commands.add_parser('info', parents=[common, link], help='读全部设置并翻译')
    commands.add_parser('backup', parents=[common, link], help='备份全部设置')
    p = commands.add_parser('live', parents=[common, link], help='采集，默认同时开实时波形')
    p.add_argument('--seconds', type=float, default=None, help='采多久；不给就一直采到关窗口或 Ctrl+C')
    p.add_argument('--label', default='', help='标签，进目录名，例 空载、砝码10N')
    p.add_argument('--mode', default=READ_BLOCK, choices=READ_MODES, help='block：测量值+AD内码（默认）；gross：只读毛重')
    p.add_argument('--period-ms', type=float, default=None, help='按固定周期读；不给就读完马上读下一个')
    p.add_argument('--no-plot', action='store_true', help='不开波形窗口，只采集并每秒打印')
    p.add_argument('--window-s', type=float, default=10.0, help='波形窗口显示最近多少秒')
    p = commands.add_parser('stats', parents=[common], help='不连设备：从采集目录的 samples.csv 重算统计')
    p.add_argument('run_dir', type=Path)
    p = commands.add_parser('raw', parents=[common, link], help='手动收发报文')
    p.add_argument('--allow-write', action='store_true', help='允许发写命令（只能写白名单）')
    p = commands.add_parser('set', parents=[common, link, write], help='改设置：名字=值（见 info 的英文名）')
    p.add_argument('assignments', nargs='+', type=_assignment, metavar='名字=值')
    commands.add_parser('preset-test', parents=[common, link, write],
                        help='改成测试设置：' + '、'.join(f'{k}={v}' for k, v in TEST_PRESET.items()))
    p = commands.add_parser('restore', parents=[common, link, write], help='按备份或修改记录写回原值')
    p.add_argument('record', type=Path)
    p = commands.add_parser('set-link', parents=[common, link], help='改波特率、帧格式或地址（一次一项）')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--to-baud', type=int, choices=BAUDRATES, metavar='BAUD')
    target.add_argument('--to-frame', type=_int, choices=tuple(FRAME_CODES), metavar='CODE',
                        help='3 = 8E1、4 = 8O1、5 = 8N1、6 = 8N2')
    target.add_argument('--to-address', type=int)
    p.add_argument('--apply', action='store_true', help='真的改（不加只读、只列计划）')
    return parser


def default_connect(args):
    """按命令行参数打开串口。返回 (client, close, evidence)。测试时由调用方换成固定回复替身。"""
    port, evidence = resolve_port(port=args.port, serial_number=args.serial_number)
    link = LinkSettings(port, baudrate=args.baud, address=args.address, parity=args.parity, stopbits=args.stopbits,
                        timeout_s=None if args.timeout_ms is None else args.timeout_ms / 1000)
    handle = open_serial(link)
    return ModbusClient(handle, link), handle.close, evidence


def _root(args) -> Path:
    return args.data_root if args.data_root is not None else default_data_root()


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return '-'
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return str(value)


# ---------------- 各命令 ----------------

def cmd_ports(args, out, **_) -> int:
    ports = list_ports()
    latency = ftdi_latency_ms()
    if not ports:
        out('没找到串口：插好 USB-RS485 转换器再试。')
        return 1
    for item in ports:
        tags = []
        if item.is_ftdi:
            ms = latency.get(item.device)
            tags.append('FTDI' + ('' if ms is None else f'，延迟 {ms} ms' + ('' if ms == 1 else '（要改成 1 ms）')))
        out(f'{item.device:7s} {item.description}  VID {item.as_dict()["vid_hex"] or "-"} PID '
            f'{item.as_dict()["pid_hex"] or "-"}  序列号 {item.serial_number or "-"}  {"；".join(tags)}')
    if any(item.is_ftdi for item in ports) and not latency:
        out('（没读到 FTDI 延迟：在设备管理器 → 端口 → 属性 → 端口设置 → 高级 里看，改成 1 ms）')
    return 0


def cmd_scan(args, out, **_) -> int:
    port, _ = resolve_port(port=args.port, serial_number=args.serial_number)
    out(f'在 {port} 上试：波特率 {list(args.bauds)}，地址 {list(args.addresses)}；只读固件版本，不写任何东西')
    found = []

    def progress(result):
        if result['ok']:
            found.append(result)
        tag = '通' if result['ok'] else '不通'
        firmware = result.get('firmware')
        extra = f'，固件 {firmware}' if firmware is not None else result.get('note', '')
        out(f'  {result["baudrate"]:>6} 8{result["parity"]}{result["stopbits"]} 地址 {result["address"]}: {tag}{extra}')

    results = scan(port, baudrates=args.bauds, addresses=args.addresses, stop_at_first=not args.all, progress=progress)
    if results and 'error' in results[-1] and '打不开' in results[-1]['error']:
        out(results[-1]['error'])
        return 1
    if not found:
        out('都不通：查 A、B 是否接反、供电、地址拨码；还不行按住变送器按键上电 3 秒恢复通信参数再扫。')
        return 1
    first = found[0]
    out(f'以后连接用：--port {port} --baud {first["baudrate"]} --parity {first["parity"]} '
        f'--stopbits {first["stopbits"]} --address {first["address"]}')
    return 0


def _print_snapshot(snapshot, out) -> None:
    for row in ops.display_rows(snapshot):
        mark = '可写' if row['writable'] else '只读'
        test = '' if row['test_value'] is None or row['test_value'] == row['value'] else f'  ← 测试设置是 {row["test_value"]}'
        out(f'  {row["address"]:>3} {row["label"]:<10} {_fmt(row["value"]):>10}  {row["text"]:<24} {mark}{test}')
    status = ops.status_of(snapshot)
    if status is not None:
        flags = [name for name in ('overflow', 'overload', 'unstable', 'power_on_zeroed', 'at_zero', 'negative')
                 if getattr(status, name)]
        out(f'  状态：小数位 {status.decimals}；置位：{"、".join(flags) or "无"}')
    for name, probe in snapshot['probes'].items():
        out(f'  寄存器 {name}（协议没写，R11 会读）：{probe["values"] if probe["ok"] else "读不到 " + probe["error"]}')
    for error in snapshot['errors']:
        out(f'  读不到 {error["start"]}–{error["start"] + error["count"] - 1}：{error["error"]}')


def cmd_info(args, out, client, **_) -> int:
    snapshot = ops.read_all(client)
    out(f'{client.link.port} {client.link.baudrate} 8{client.link.parity}{client.link.stopbits} 地址 {client.link.address}')
    _print_snapshot(snapshot, out)
    for warning in ops.check_test_settings(snapshot):
        out(f'  注意：{warning}')
    return 1 if snapshot['errors'] else 0


def cmd_backup(args, out, client, **_) -> int:
    path, snapshot = ops.backup(client, _root(args) / 'settings')
    out(f'已备份到 {path}' + (f'；有 {len(snapshot["errors"])} 块没读到' if snapshot['errors'] else ''))
    return 1 if snapshot['errors'] else 0


def _print_change(result, out, *, apply: bool) -> int:
    if result.get('problem') and not result.get('planned') and not result.get('receipts'):
        out(f'没写：{result["problem"]}')
        return 1
    planned = result.get('planned', [])
    if not planned:
        out('已经是想要的值，不用改。')
        return 0 if result['ok'] else 1
    out('要改的：')
    for change in planned:
        out(f'  {change["address"]:>3} {change["label"]}：{change["old_text"]} → {change["new_text"]}')
    if not apply:
        out('没写：确认无误后加 --apply 再运行一次。')
        return 0
    for receipt in result.get('receipts', []):
        out(f'  已写 {receipt["address"]}：读回 {receipt["read_back"]}{"" if receipt["ok"] else "（不一致）"}')
    if result.get('wrong'):
        out(f'  最后核对不一致：{result["wrong"]}')
    if result.get('problem'):
        out(f'  问题：{result["problem"]}')
    out(('完成' if result['ok'] else '没有全部完成') + f'。记录：{result.get("record")}')
    return 0 if result['ok'] else 1


def cmd_set(args, out, client, **_) -> int:
    wanted = dict(args.assignments)
    if len(wanted) != len(args.assignments):
        out('同一个名字给了两次。')
        return 2
    result = ops.change_settings(client, wanted, record_dir=_root(args) / 'settings', apply=args.apply,
                                 unlock=args.unlock)
    return _print_change(result, out, apply=args.apply)


def cmd_preset_test(args, out, client, **_) -> int:
    result = ops.preset_test(client, record_dir=_root(args) / 'settings', apply=args.apply, unlock=args.unlock)
    return _print_change(result, out, apply=args.apply)


def cmd_restore(args, out, client, **_) -> int:
    result = ops.restore(client, args.record, record_dir=_root(args) / 'settings', apply=args.apply,
                         unlock=args.unlock)
    return _print_change(result, out, apply=args.apply)


def cmd_set_link(args, out, client, **_) -> int:
    result = ops.change_link(client, baudrate=args.to_baud, frame_code=args.to_frame, address=args.to_address,
                             record_dir=_root(args) / 'settings', apply=args.apply)
    if result.get('already'):
        out('已经是这个值，不用改。')
        return 0
    if result.get('problem') and not result.get('steps'):
        out(f'没改：{result["problem"]}')
        return 1
    new = result['new_link']
    out(f'{result["register"]}：{result.get("old")} → {result["new"]}；改完连接要用 --baud {new["baudrate"]} '
        f'--parity {new["parity"]} --stopbits {new["stopbits"]} --address {new["address"]}')
    if not args.apply:
        out('没改：确认无误后加 --apply 再运行一次。改通信参数是最容易断联的一步，先 backup。')
        return 0
    for step in result['steps']:
        out(f'  {step}')
    if result['problem']:
        out(f'  问题：{result["problem"]}')
    out(('完成' if result['ok'] else '没有完成') + f'。记录：{result.get("record")}')
    return 0 if result['ok'] else 1


def cmd_raw(args, out, client, input_fn, **_) -> int:
    raw_console.repl(client, allow_write=args.allow_write, input_fn=input_fn, print_fn=out)
    return 0


def _summary_lines(stats: dict, step) -> list[str]:
    lines = [f'  时长 {_fmt(stats["duration_s"], 2)} s，读 {stats["reads"]} 次（成功 {stats["ok_reads"]}，'
             f'错误 {stats["errors"]}，最多连续错 {stats["max_consecutive_errors"]}）',
             f'  读取速率 {_fmt(stats["read_rate_hz"], 1)} 次/s']
    if stats['new_value_rate_hz'] is not None:
        rate = stats['new_value_rate_hz']
        grade = '最好' if rate >= NEW_VALUE_RATE_GOOD_HZ else '达标' if rate >= NEW_VALUE_RATE_MIN_HZ else '不达标'
        lines.append(f'  新值速率 {rate:.1f} 个/s（C1 要 ≥{NEW_VALUE_RATE_MIN_HZ}，C2 最好 ≥{NEW_VALUE_RATE_GOOD_HZ}：'
                     f'{grade}）')
        median, p99 = stats['new_interval_ms_median'], stats['new_interval_ms_p99']
        even = '' if median is None or p99 is None else ('，均匀' if p99 <= 2 * median else '，不均匀（p99 > 2×中位数）')
        lines.append(f'  新值间隔 中位 {_fmt(median, 2)} / p95 {_fmt(stats["new_interval_ms_p95"], 2)} / '
                     f'p99 {_fmt(p99, 2)} / 最大 {_fmt(stats["new_interval_ms_max"], 2)} ms{even}')
    else:
        lines.append('  新值速率：gross 读法没有 AD 内码，算不了')
    if stats['force_std_n'] is not None:
        std = stats['force_std_n']
        lines.append(f'  力 均值 {_fmt(stats["force_mean_n"])} N，σ {std:.5f} N（D1 静置要 ≤{NOISE_STD_MAX_N}：'
                     f'{"达标" if std <= NOISE_STD_MAX_N else "不达标"}），范围 {_fmt(stats["force_min_n"])}–'
                     f'{_fmt(stats["force_max_n"])} N')
    if step is not None:
        lines.append(f'  最小步长 {step:.6f} N（D3 要 ≤{STEP_MAX_N}：{"达标" if step <= STEP_MAX_N + 1e-12 else "不达标"}）')
    return lines


def cmd_stats(args, out, **_) -> int:
    rows = read_samples(args.run_dir / 'samples.csv')
    stats = asdict(compute_stats(rows))
    out(f'{args.run_dir}（从 samples.csv 重算；合格线只对空载静置、最快速度读的采集有意义）')
    for line in _summary_lines(stats, min_step(row.force_n for row in rows if row.ok)):
        out(line)
    metadata_path = args.run_dir / 'metadata.json'
    if metadata_path.exists():
        warnings = read_json(metadata_path).get('warnings') or []
        for warning in warnings:
            out(f'  采集时的设置提醒：{warning}')
    return 0


def cmd_live(args, out, client, evidence, **_) -> int:
    if not args.no_plot and (problem := gui_problem()):
        out(problem)
        return 1
    snapshot = ops.read_all(client)
    decimals, unit = ops.reading_scale(snapshot)
    warnings = ops.check_test_settings(snapshot)
    for warning in warnings:
        out(f'注意：{warning}')
    config = CaptureConfig(read_mode=args.mode, decimals=decimals, unit=unit, seconds=args.seconds,
                           period_s=None if args.period_ms is None else args.period_ms / 1000)
    run_dir = new_run_dir('live', args.label, _root(args))
    session = CaptureSession(client, config, run_dir, metadata=dict(
        label=args.label, settings=snapshot, warnings=warnings, port_evidence=evidence))
    unit_label = 'N' if unit in NEWTONS_PER_UNIT else '原始计数'
    out(f'采集到 {run_dir}（{config.read_mode} 读法，小数位 {decimals}，单位 {unit}）；'
        + ('关窗口停止' if not args.no_plot else 'Ctrl+C 停止'))
    session.start()
    try:
        if args.no_plot:
            while not session.wait(1.0):
                progress = session.progress()
                out(f'  读 {progress["reads"]}，错 {progress["errors"]}，新值 {_fmt(progress["new_values"])}')
        else:
            run_live_window(session, title=f'锐力德测力计 {client.link.port} {args.label}'.strip(),
                            unit_label=unit_label, window_s=args.window_s)
    except KeyboardInterrupt:
        out('收到 Ctrl+C，正在停止并收尾……')
    finally:
        summary = session.finish()
    out(f'停止原因：{summary["stop_reason"]}；数据{"完整" if summary["complete"] else "不完整"}')
    for line in _summary_lines(summary['stats'], None):
        out(line)
    out(f'详细数据：{run_dir}（最小步长等用 stats 命令重算）')
    return 0 if summary['complete'] and summary['stop_reason'] in ('duration', 'user_stop') else 1


COMMANDS = dict(ports=cmd_ports, scan=cmd_scan, info=cmd_info, backup=cmd_backup, live=cmd_live, stats=cmd_stats,
                raw=cmd_raw, set=cmd_set, **{'preset-test': cmd_preset_test, 'restore': cmd_restore,
                                             'set-link': cmd_set_link})
NEEDS_DEVICE = {'info', 'backup', 'live', 'raw', 'set', 'preset-test', 'restore', 'set-link'}


def main(argv=None, *, connect=default_connect, out=print, input_fn=input) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, 'seconds', None) is not None and not (math.isfinite(args.seconds) and args.seconds > 0):
        out('--seconds 要大于 0')
        return 2
    command = COMMANDS[args.command]
    try:
        if args.command not in NEEDS_DEVICE:
            return command(args, out)
        client, close, evidence = connect(args)
        try:
            return command(args, out, client=client, evidence=evidence, input_fn=input_fn)
        finally:
            close()
    except (BenchError, OSError, ValueError, RuntimeError) as exc:
        out(f'出错：{type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
