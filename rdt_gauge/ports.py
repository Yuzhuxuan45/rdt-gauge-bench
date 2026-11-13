"""找串口、打开串口、扫描通信参数。

写法参考（登记在 docs/SOURCES.json）：
- PortInfo / list_ports / resolve_port：r11_upper_computer/wuji_r11/acquisition/protocols/transport.py 的
  enumerate_ports、resolve_cdc_port —— 按 USB 身份（VID、PID、序列号）认设备，不认 COM 号（插拔、换 USB 口
  COM 号会变，序列号不会）；匹配到多个时报错，不猜（选错口读到的是别的设备的数据，看起来还完全正常）。
- FTDI_VID：r11_upper_computer/wuji_r11/configuration.py —— 只有一个 FTDI 串口时可以直接用它。
- scan：2026-11-04 CYBSQ-V5 第一次上电，出厂并不是说明书写的 9600，靠"只发 03"的扫描才找到 256000、地址 1
  （r11_upper_computer/docs/changes/2026-11-04.md）。扫描**绝不发写命令**。
- ftdi_latency：r2_five_station/tools/check_environment.ps1 的 Get-FtdiPorts —— 只读注册表看延迟计时器是不是
  1 ms，不替用户改。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time

from .contracts import BAUDRATES, BenchError, DeviceRejected, LinkSettings
from .protocol import ModbusClient

FTDI_VID = 0x0403


@dataclass(frozen=True, slots=True)
class PortInfo:
    device: str
    description: str = ''
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None

    @property
    def is_ftdi(self) -> bool:
        return self.vid == FTDI_VID

    def as_dict(self) -> dict:
        value = asdict(self)
        value['vid_hex'] = None if self.vid is None else f'0x{self.vid:04X}'
        value['pid_hex'] = None if self.pid is None else f'0x{self.pid:04X}'
        return value


def list_ports(*, comports=None) -> list[PortInfo]:
    """列出本机串口，不打开任何一个。comports 只给测试用（传一个返回固定列表的函数）。"""
    if comports is None:
        from serial.tools.list_ports import comports
    found = []
    for item in comports():
        found.append(PortInfo(device=str(item.device), description=str(item.description or ''),
                              vid=None if item.vid is None else int(item.vid),
                              pid=None if item.pid is None else int(item.pid),
                              serial_number=str(item.serial_number) if item.serial_number else None,
                              manufacturer=str(item.manufacturer) if item.manufacturer else None))
    return sorted(found, key=lambda port: port.device)


def resolve_port(*, port: str | None = None, serial_number: str | None = None,
                 candidates: list[PortInfo] | None = None) -> tuple[str, dict]:
    """决定用哪个 COM 口，并留下"为什么选它"的证据。

    - 只给 port：直接用。
    - 给了 serial_number：按序列号找（不分大小写）；同时给了 port 时还要核对两者是同一个口。
    - 都没给：本机正好一个 FTDI 串口就用它；零个或多个都报错，不猜。
    """
    if port and not serial_number:
        return port, dict(resolution='explicit', port=port)
    ports = list_ports() if candidates is None else candidates
    evidence = dict(all_ports=[item.as_dict() for item in ports])
    if serial_number:
        want = serial_number.strip().upper()
        hits = [item for item in ports if (item.serial_number or '').upper() == want]
        if len(hits) != 1:
            raise BenchError(f'序列号 {serial_number} 匹配到 {len(hits)} 个串口，要正好 1 个。本机串口：'
                             f'{[item.device + " " + (item.serial_number or "-") for item in ports] or "一个都没有"}')
        if port and hits[0].device.casefold() != port.casefold():
            raise BenchError(f'序列号 {serial_number} 现在在 {hits[0].device}，不是 {port}')
        return hits[0].device, dict(evidence, resolution='serial_number', port=hits[0].device)
    ftdi = [item for item in ports if item.is_ftdi]
    if len(ftdi) != 1:
        raise BenchError(f'没指定串口，本机有 {len(ftdi)} 个 FTDI 串口，分不出哪个是测力计：用 --port 或 --serial-number '
                         f'指定。本机串口：{[item.device + " " + item.description for item in ports] or "一个都没有"}')
    return ftdi[0].device, dict(evidence, resolution='only_ftdi', port=ftdi[0].device)


def open_serial(link: LinkSettings):
    """按通信参数打开串口，打开后等 link.settle_s 秒让转换器稳定（照 R11 force_sensor.py）。"""
    import serial
    parity = {'N': serial.PARITY_NONE, 'E': serial.PARITY_EVEN, 'O': serial.PARITY_ODD}[link.parity]
    stopbits = serial.STOPBITS_ONE if link.stopbits == 1 else serial.STOPBITS_TWO
    port = serial.Serial(port=link.port, baudrate=link.baudrate, bytesize=serial.EIGHTBITS, parity=parity,
                         stopbits=stopbits, timeout=0.1, write_timeout=0.5)
    if link.settle_s:
        time.sleep(link.settle_s)
    port.reset_input_buffer()
    return port


def apply_link(port, link: LinkSettings) -> None:
    """电脑这边换成 link 的波特率、校验位、停止位（设备那边由 settings_ops.change_link 先改）。

    pyserial 的 PARITY_NONE/EVEN/ODD 就是 'N'/'E'/'O'，STOPBITS_ONE/TWO 就是 1/2，可以直接赋值；改完清空接收缓冲，
    丢掉换参数之前收到的乱码。
    """
    port.baudrate = link.baudrate
    port.parity = link.parity
    port.stopbits = link.stopbits
    port.reset_input_buffer()


# 扫描顺序：出厂 9600 优先，然后按常用程度。停止位两种都试（协议表写出厂 8N2，指令解析写 8N1）。
SCAN_BAUDRATES = (9600, 115200, 921600, 460800, 230400, 57600, 38400, 19200, 4800, 2400, 1200)
SCAN_FRAMES = (('N', 2), ('N', 1), ('E', 1), ('O', 1))


def scan_plan(*, baudrates=SCAN_BAUDRATES, frames=SCAN_FRAMES, addresses=(1,)) -> list[tuple[int, str, int, int]]:
    """要试的组合（波特率, 校验位, 停止位, 地址），按顺序。只读这个列表就知道 scan 会发什么。"""
    for baud in baudrates:
        if baud not in BAUDRATES:
            raise ValueError(f'锐力德不支持波特率 {baud}')
    return [(baud, parity, stop, address) for baud in baudrates for parity, stop in frames for address in addresses]


def scan(port_name: str, *, baudrates=SCAN_BAUDRATES, frames=SCAN_FRAMES, addresses=(1,), stop_at_first=True,
         open_port=open_serial, progress=None) -> list[dict]:
    """逐个组合试读寄存器 6（固件版本），只发 03 读命令，不写任何寄存器。

    返回每个组合的结果；stop_at_first 为 True 时找到一个能通的就停。
    设备回异常帧也算"通了"：说明波特率、帧格式、地址都对，只是这个寄存器不让读。
    """
    results = []
    for baud, parity, stop, address in scan_plan(baudrates=baudrates, frames=frames, addresses=addresses):
        link = LinkSettings(port_name, baudrate=baud, address=address, parity=parity, stopbits=stop, settle_s=0.2)
        outcome = dict(baudrate=baud, parity=parity, stopbits=stop, address=address)
        try:
            handle = open_port(link)
        except Exception as exc:
            outcome.update(ok=False, error=f'打不开串口：{type(exc).__name__}: {exc}')
            results.append(outcome)
            break  # 串口本身打不开，换参数也没用
        try:
            client = ModbusClient(handle, link)
            try:
                firmware = client.read(6, 1, attempts=2)[0]
                outcome.update(ok=True, firmware=firmware)
            except DeviceRejected as exc:
                outcome.update(ok=True, firmware=None, note=str(exc))
            except BenchError as exc:
                outcome.update(ok=False, error=f'{type(exc).__name__}: {exc}', raw_hex=client.last_frame.hex(' '))
        finally:
            handle.close()
        results.append(outcome)
        if progress is not None:
            progress(outcome)
        if outcome['ok'] and stop_at_first:
            break
    return results


def ftdi_latency_ms() -> dict[str, int | None]:
    """只读注册表：每个 FTDI 串口的延迟计时器（ms）。不是 Windows 或读不到就返回空字典。不改任何设置。"""
    try:
        import winreg
    except ImportError:
        return {}
    found: dict[str, int | None] = {}
    base = r'SYSTEM\CurrentControlSet\Enum\FTDIBUS'
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return {}
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            device = winreg.EnumKey(root, index)
            try:
                device_key = winreg.OpenKey(root, device)
            except OSError:
                continue
            with device_key:
                for sub in range(winreg.QueryInfoKey(device_key)[0]):
                    instance = winreg.EnumKey(device_key, sub)
                    try:
                        params = winreg.OpenKey(device_key, instance + r'\Device Parameters')
                    except OSError:
                        continue
                    with params:
                        try:
                            name = winreg.QueryValueEx(params, 'PortName')[0]
                        except OSError:
                            continue
                        try:
                            latency = int(winreg.QueryValueEx(params, 'LatencyTimer')[0])
                        except OSError:
                            latency = None
                        found[str(name)] = latency
    return found
