"""离线测试时锁死真实串口（照 R2 r2_five_station/tests/offline_guard/sitecustomize.py）。

- 本进程里的 pytest：tests/conftest.py 直接调用 block_serial()。
- 子进程：把本目录加进 PYTHONPATH，并设 RDT_BENCH_OFFLINE_ONLY=1，解释器启动时自动生效。

锁死以后，打开串口和列出本机串口都会直接报错，测试不可能误连设备，也不会依赖这台电脑插了什么。
"""
import importlib
import os


def _blocked(*args, **kwargs):
    raise RuntimeError('离线测试：不允许打开真实串口，也不允许列出本机串口')


def block_serial() -> bool:
    """换掉 pyserial 的入口；没装 pyserial 返回 False（由调用方决定怎么处理）。"""
    try:
        import serial
    except ImportError:
        return False
    serial.Serial = _blocked
    if hasattr(serial, 'serial_for_url'):
        serial.serial_for_url = _blocked
    for name in ('serial.serialwin32', 'serial.serialposix'):
        try:
            importlib.import_module(name).Serial = _blocked
        except ImportError:
            pass
    for name in ('serial.tools.list_ports', 'serial.tools.list_ports_windows', 'serial.tools.list_ports_posix'):
        try:
            importlib.import_module(name).comports = _blocked
        except ImportError:
            pass
    return True


if os.environ.get('RDT_BENCH_OFFLINE_ONLY') == '1':
    block_serial()
