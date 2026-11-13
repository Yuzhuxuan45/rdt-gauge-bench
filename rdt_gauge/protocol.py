"""Modbus RTU 通信：CRC、组帧、读写寄存器、解码。

这一层只管"一问一答"的报文：不管寄存器是什么意思（见 registers.py），也不管读到的数怎么用（见 capture.py）。
从 R11 改写的函数登记在 docs/SOURCES.json：
- crc16 / add_crc / validate_crc / read_registers：r11_upper_computer/wuji_r11/acquisition/protocols/force_sensor.py
- write_register：r11_upper_computer/tools/gauge_probe.py

和 R11 不一样的地方（按锐力德 Modbus RTU 协议改）：
- 32 位数固定高 16 位在前（协议："080 高 16 位"）。原测力计是读寄存器 10 决定字序，锐力德的协议里没有寄存器 10。
  负数按标准补码（协议原文）；指令解析 1.82 的 −3901 例子算错了 1，见 contracts.decode_s32。
- 超时按波特率算：出厂 9600 下一问一答要 20–50 ms，不能照搬 R11 固定的 0.05 s。
"""
from __future__ import annotations

import struct
import time

from .contracts import DeviceRejected, FrameError, LinkSettings, LinkTimeout, decode_s32, decode_u32

__all__ = ['crc16', 'add_crc', 'validate_crc', 'build_read', 'build_write', 'parse_read', 'parse_write',
           'read_reply_length', 'decode_s32', 'decode_u32', 'encode_s32', 'frame_time_s', 'auto_timeout_s',
           'ModbusClient']

READ_HOLDING = 0x03
WRITE_MULTIPLE = 0x10
MAX_READ_COUNT = 125  # Modbus 一次最多读 125 个寄存器


def crc16(data: bytes) -> int:
    """Modbus CRC16（多项式 0xA001，初值 0xFFFF）。"""
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
    return value


def add_crc(data: bytes) -> bytes:
    """在报文末尾加上 CRC，低字节在前。"""
    return data + struct.pack('<H', crc16(data))


def validate_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise FrameError(f'回复太短，只有 {len(frame)} 字节')
    expected = struct.unpack('<H', frame[-2:])[0]
    actual = crc16(frame[:-2])
    if actual != expected:
        raise FrameError(f'CRC 不对：回复里是 0x{expected:04X}，算出来是 0x{actual:04X}')


def _check_address(address: int) -> None:
    if type(address) is not int or not 1 <= address <= 247:
        raise ValueError('设备地址要在 1–247 之间（0 是广播，不用）')


def _check_span(start: int, count: int) -> None:
    if type(start) is not int or type(count) is not int or count < 1 or start < 0 or start + count > 0x10000:
        raise ValueError(f'寄存器范围不对：从 {start!r} 开始读 {count!r} 个')


def build_read(address: int, start: int, count: int) -> bytes:
    """功能码 03 读保持寄存器。例：build_read(1, 80, 2) == 01 03 00 50 00 02 C4 1A。"""
    _check_address(address)
    _check_span(start, count)
    if count > MAX_READ_COUNT:
        raise ValueError(f'一次最多读 {MAX_READ_COUNT} 个寄存器')
    return add_crc(struct.pack('>BBHH', address, READ_HOLDING, start, count))


def build_write(address: int, start: int, values: list[int] | tuple[int, ...]) -> bytes:
    """功能码 10 写多个寄存器（锐力德说明书的写操作都用 10）。例：解锁是 01 10 00 05 00 01 02 5A A5 5C DE。"""
    _check_address(address)
    _check_span(start, len(values))
    if len(values) > 123:
        raise ValueError('一次最多写 123 个寄存器')
    if any(type(value) is not int or not 0 <= value <= 0xFFFF for value in values):
        raise ValueError('写入的每个寄存器值都要是 0–65535 的整数')
    count = len(values)
    return add_crc(struct.pack(f'>BBHHB{count}H', address, WRITE_MULTIPLE, start, count, 2 * count, *values))


def read_reply_length(count: int) -> int:
    """读 count 个寄存器时，正常回复的长度：地址 + 功能码 + 字节数 + 数据 + CRC。"""
    return 5 + 2 * count


def _check_exception(frame: bytes, function: int) -> None:
    """设备拒绝时回 5 字节：地址、功能码 + 0x80、异常码、CRC。"""
    if len(frame) == 5 and frame[1] == function | 0x80:
        validate_crc(frame)
        raise DeviceRejected(function, frame[2])


def parse_read(frame: bytes, address: int, count: int) -> list[int]:
    """检查读回复的每一项，返回寄存器值（16 位无符号）。"""
    _check_exception(frame, READ_HOLDING)
    expected = read_reply_length(count)
    if len(frame) != expected:
        raise FrameError(f'回复不完整：收到 {len(frame)} 字节，应为 {expected} 字节')
    validate_crc(frame)
    if frame[0] != address or frame[1] != READ_HOLDING or frame[2] != 2 * count:
        raise FrameError(f'回复格式不对：{frame.hex(" ")}')
    return list(struct.unpack(f'>{count}H', frame[3:-2]))


def parse_write(frame: bytes, request: bytes) -> None:
    """写回复是 8 字节，前 6 字节要和请求完全一样（地址、功能码、起始地址、数量）。"""
    _check_exception(frame, WRITE_MULTIPLE)
    if len(frame) != 8:
        raise FrameError(f'写回复不完整：收到 {len(frame)} 字节，应为 8 字节')
    validate_crc(frame)
    if frame[:6] != request[:6]:
        raise FrameError(f'写回复和请求对不上：请求 {request[:6].hex(" ")}，回复 {frame[:6].hex(" ")}')


def encode_s32(value: int) -> tuple[int, int]:
    """32 位有符号数拆成两个 16 位寄存器（高 16 位在前），decode_s32 的反过程。"""
    if type(value) is not int or not -0x8000_0000 <= value <= 0x7FFF_FFFF:
        raise ValueError(f'不是 32 位有符号整数：{value!r}')
    unsigned = value & 0xFFFF_FFFF
    return unsigned >> 16, unsigned & 0xFFFF


def frame_time_s(nbytes: int, link: LinkSettings) -> float:
    """在线上传 nbytes 字节要多久：每个字节 = 1 起始位 + 8 数据位 + 校验位 + 停止位。"""
    bits = 1 + 8 + (0 if link.parity == 'N' else 1) + link.stopbits
    return nbytes * bits / link.baudrate


def auto_timeout_s(link: LinkSettings, request_bytes: int, reply_bytes: int) -> float:
    """等回复的最长时间：请求和回复在线上的时间，加 USB 延迟、设备处理时间和余量。

    9600 下读 30–45（请求 8 字节、回复 37 字节）约 0.1 s；921600 下约 0.025 s。
    协议寄存器 4 的"应答延时"也要算进去，默认是 0，改了就用 LinkSettings.timeout_s 手动给。
    """
    if link.timeout_s is not None:
        return link.timeout_s
    on_wire = frame_time_s(request_bytes + reply_bytes, link)
    return 0.02 + 1.5 * on_wire + 0.005


class ModbusClient:
    """一个串口上的 Modbus 主站。同一时间只能有一个线程用它（照 R11：一个数据源只有一个读者）。

    port 是已经打开的 pyserial 串口对象，或任何有 write / read / reset_input_buffer 三个方法、
    并且有 timeout 属性的对象（离线测试用的固定回复替身就是这样）。
    """

    def __init__(self, port, link: LinkSettings):
        self.port = port
        self.link = link
        self.last_request = b''
        self.last_frame = b''  # 最近一次收到的原始回复，出错时记进数据里排查（照 R11 force_sensor.py）

    def _exchange(self, request: bytes, reply_bytes: int) -> bytes:
        self.port.timeout = auto_timeout_s(self.link, len(request), reply_bytes)
        self.last_request = request
        self.last_frame = b''
        written = self.port.write(request)
        if written is not None and written != len(request):
            raise FrameError(f'请求没发完：{written}/{len(request)} 字节')
        # 不调用 flush()：Windows 上它内部是每次睡 50 ms 的循环，会拖慢读取（照 R11 force_sensor.py 的注释）。
        frame = bytes(self.port.read(reply_bytes))
        self.last_frame = frame
        if not frame:
            raise LinkTimeout(f'{self.port.timeout * 1000:.0f} ms 内没有回复')
        # 异常帧只有 5 字节，read 会等满超时才返回，这是正常的；其余不足长度的回复交给 parse_* 判断。
        return frame

    def read(self, start: int, count: int, *, attempts: int = 1) -> list[int]:
        """读 count 个寄存器。失败时清空接收缓冲再试，试满 attempts 次还不行就把最后一次的错误抛出去。"""
        request = build_read(self.link.address, start, count)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self.port.reset_input_buffer()  # 丢掉上一次残留的字节，免得错位（照 R11）
                time.sleep(0.05)
            try:
                frame = self._exchange(request, read_reply_length(count))
                return parse_read(frame, self.link.address, count)
            except DeviceRejected:
                raise  # 设备明确拒绝，重试也没用
            except (FrameError, LinkTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def write(self, start: int, values: list[int] | tuple[int, ...], *, reply_optional: bool = False) -> bool:
        """写寄存器，只发一次、不重试：写命令重发可能被执行两次。写完读回确认由 settings_ops.py 负责。

        reply_optional：改波特率、帧格式、地址时，设备是按**新参数**回复的（说明书 1.2、1.3），旧参数下收到的是乱码
        或什么也收不到，这时不报错、返回 False，由调用方换成新参数后读回确认（照 R11 tools/cybsq_configure.py 的
        reply_optional）。设备明确拒绝（异常帧）照样报错。正常收到正确回显返回 True。
        """
        request = build_write(self.link.address, start, values)
        try:
            frame = self._exchange(request, 8)
            parse_write(frame, request)
        except (FrameError, LinkTimeout):
            if reply_optional:
                return False
            raise
        return True

    def raw(self, request_without_crc: bytes, reply_bytes: int) -> bytes:
        """发一条自己拼的报文（自动补 CRC），原样返回回复。给 raw_console.py 用，不做任何检查。"""
        request = add_crc(bytes(request_without_crc))
        return self._exchange(request, reply_bytes)
