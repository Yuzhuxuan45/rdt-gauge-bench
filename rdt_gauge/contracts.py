"""各层共用的数据类型、异常和常量。

只描述事实，不做任何 I/O，谁都可以导入（照 R11 wuji_r11/contracts.py：跨模块共享的事实类型不带 I/O）。
数据类都用 frozen=True：建好以后不能改，传来传去不会被谁偷偷改掉。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

# 锐力德高速版支持的波特率（协议寄存器 1；中速、低速版没有 460800 和 921600）。
BAUDRATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
PARITIES = ('N', 'E', 'O')

# 采集时一次读哪些寄存器。
READ_BLOCK = 'block'  # 30–45：测量值（30–31）和 AD 内码（44–45）一起读，用 AD 内码判断新值（照 R11 tools/gauge_probe.py）
READ_GROSS = 'gross'  # 80–81：只读毛重，回复最短，读得最快
READ_MODES = (READ_BLOCK, READ_GROSS)


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def decode_s32(high: int, low: int) -> int:
    """两个 16 位寄存器拼成 32 位有符号数，高 16 位在前（锐力德协议："080 高 16 位"）。

    例：00 00 00 84 → 132，FF FF FF FF → −1，FF FF F0 C2 → −3902。
    指令解析 1.82 把 FF FF F0 C2 算成 −3901（少减了 1），以协议写的"标准补码"为准，见 tests/test_protocol.py。
    纯算术，放在最底层，protocol 和 registers 都用。
    """
    for word in (high, low):
        if type(word) is not int or not 0 <= word <= 0xFFFF:
            raise ValueError(f'寄存器值要是 0–65535 的整数，现在是 {word!r}')
    unsigned = (high << 16) | low
    return unsigned - 0x1_0000_0000 if unsigned & 0x8000_0000 else unsigned


def decode_u32(high: int, low: int) -> int:
    for word in (high, low):
        if type(word) is not int or not 0 <= word <= 0xFFFF:
            raise ValueError(f'寄存器值要是 0–65535 的整数，现在是 {word!r}')
    return (high << 16) | low


class BenchError(Exception):
    """本脚本自己报的错误都从这里派生，方便上层统一处理。"""


class FrameError(BenchError):
    """回复不完整或格式不对：长度、CRC、地址、功能码、字节数有一项对不上。"""


class DeviceRejected(BenchError):
    """设备回了 Modbus 异常帧（功能码 + 0x80）：报文格式对，但设备拒绝执行。"""

    def __init__(self, function: int, code: int):
        super().__init__(f'设备拒绝功能码 0x{function:02X}：Modbus 异常码 0x{code:02X}')
        self.function = function
        self.code = code


class LinkTimeout(BenchError):
    """规定时间内没收到完整回复。"""


class NotAllowed(BenchError):
    """要写的寄存器不在白名单里，或者写入的值不在允许范围内。"""


@dataclass(frozen=True, slots=True)
class LinkSettings:
    """一条串口连接的通信参数。COM 号由命令行传入，不写死在代码里。"""
    port: str
    baudrate: int = 9600
    address: int = 1
    parity: str = 'N'
    stopbits: int = 2  # 协议表写出厂 8N2，指令解析写 8N1；不确定时用 scan 两种都试
    timeout_s: float | None = None  # None：按波特率自动算（见 protocol.py）
    settle_s: float = 1.0  # 打开串口后先等这么久再问（照 R11 force_sensor.py 打开后的 time.sleep(1.0)）

    def __post_init__(self):
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError('串口不能为空，例如 COM9')
        if self.baudrate not in BAUDRATES:
            raise ValueError(f'波特率 {self.baudrate} 不在锐力德支持的列表里：{BAUDRATES}')
        if type(self.address) is not int or not 1 <= self.address <= 247:
            raise ValueError('设备地址要在 1–247 之间')
        if self.parity not in PARITIES:
            raise ValueError('校验位要是 N、E 或 O')
        if self.stopbits not in (1, 2):
            raise ValueError('停止位要是 1 或 2')
        if self.timeout_s is not None and not (_finite(self.timeout_s) and 0 < self.timeout_s <= 2):
            raise ValueError('超时要在 0–2 s 之间，或者不填（按波特率自动算）')
        if not (_finite(self.settle_s) and 0 <= self.settle_s <= 5):
            raise ValueError('打开串口后的等待时间要在 0–5 s 之间')


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    """寄存器表里的一项（照 R11 cybsq_v5.py 的 CONFIG_NAMES、tools/gauge_probe.py 的 WRITABLE 和 LIMITS）。"""
    address: int  # 协议里括号内的十进制地址，例如毛重是 80
    name: str  # 英文名：备份文件和命令行里用
    label: str  # 中文名：显示用
    words: int = 1  # 1：16 位；2：32 位（锐力德是高 16 位在前）
    signed: bool = False
    readable: bool = True  # 只写寄存器（例如 5 解锁、94 执行置零）读不出有意义的值
    writable: bool = False  # 本脚本是否允许写（白名单）
    unlock: bool = False  # 写之前要先往寄存器 5 写 0x5AA5
    choices: tuple[int, ...] = ()  # 允许写入的值（枚举）
    low: int | None = None  # 或者：允许写入的范围
    high: int | None = None
    test_value: int | None = None  # 测试设置下应为这个值（照 R11 cybsq_v5.py 的 MUST_BE_OFF）
    note: str = ''

    def __post_init__(self):
        if self.words not in (1, 2):
            raise ValueError(f'寄存器 {self.address}：words 只能是 1 或 2')
        if type(self.address) is not int or not 0 <= self.address <= 0xFFFF - (self.words - 1):
            raise ValueError(f'寄存器地址不对：{self.address!r}')
        if not self.name.isidentifier():
            raise ValueError(f'寄存器 {self.address}：英文名要能当标识符用，现在是 {self.name!r}')
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f'寄存器 {self.address}：范围下限大于上限')
        if self.writable and not self.choices and (self.low is None or self.high is None):
            raise ValueError(f'可写寄存器 {self.address} 要给出允许的值或范围')
        if self.writable and self.test_value is not None and not self.accepts(self.test_value):
            raise ValueError(f'寄存器 {self.address}：测试设置值 {self.test_value} 不在允许范围内')

    def accepts(self, value: int) -> bool:
        """value 是不是允许写进这个寄存器的值（不管 writable，那个由调用方另外检查）。"""
        if type(value) is not int:
            return False
        if self.choices:
            return value in self.choices
        return self.low is not None and self.high is not None and self.low <= value <= self.high


@dataclass(frozen=True, slots=True)
class StatusBits:
    """模块状态寄存器 8 解出来的各位（锐力德 Modbus RTU 协议 1.9 节）。"""
    raw: int
    decimals: int  # bit2–0：小数点位置
    negative: bool  # bit3：负号
    power_on_zeroed: bool  # bit4：开机已清零
    unstable: bool  # bit5：不稳
    overflow: bool  # bit6：溢出
    at_zero: bool  # bit7：零点
    smart_sensor: bool  # bit8：智能传感器
    overload: bool  # bit9：超载（固件 V1.3 起）
    valley_detected: bool  # bit10：谷值已检测
    peak_detected: bool  # bit11：峰值已检测

    @classmethod
    def from_register(cls, value: int) -> StatusBits:
        if type(value) is not int or not 0 <= value <= 0xFFFF:
            raise ValueError(f'状态寄存器的值要是 0–65535 的整数，现在是 {value!r}')
        bit = lambda n: bool(value >> n & 1)  # noqa: E731
        return cls(value, value & 0x07, bit(3), bit(4), bit(5), bit(6), bit(7), bit(8), bit(9), bit(10), bit(11))


# 采集 CSV 的列顺序（照 R11 acquisition/force.py 的 ForceSource.fields）。
SAMPLE_FIELDS = ('sample_index', 'request_ns', 'response_ns', 'measured', 'ad_code', 'force_n', 'raw_hex', 'error')


@dataclass(frozen=True, slots=True)
class SampleRow:
    """采集的一行。失败的读取也记一行，error 写原因（照 R11：失败照常记录，再接着读）。"""
    sample_index: int  # 从 1 开始，每发一次请求加 1（失败也加）
    request_ns: int  # 发请求前的 perf_counter_ns
    response_ns: int  # 收到回复（或判定失败）时的 perf_counter_ns
    measured: int | None = None  # 读出来的整数（测量值或毛重），还没除小数位
    ad_code: int | None = None  # AD 内码（只有 block 读法有），用来判断是不是新值
    force_n: float | None = None  # 换算成牛顿
    raw_hex: str = ''  # 原始回复字节，出错时排查用
    error: str = ''  # 空表示这次读成功

    @property
    def ok(self) -> bool:
        return not self.error

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    """一次写寄存器的事实记录：写之前是多少、要写多少、读回来是多少。"""
    address: int
    name: str
    before: int | None
    requested: int
    read_back: int | None
    detail: str = ''

    @property
    def ok(self) -> bool:
        return self.read_back == self.requested


@dataclass(frozen=True, slots=True)
class CaptureStats:
    """一段采集的统计：采集时每秒刷新，结束时写进 summary.json。算不出来的项为 None。"""
    duration_s: float
    reads: int
    ok_reads: int
    errors: int
    max_consecutive_errors: int
    new_values: int | None  # AD 内码变化的次数；gross 读法没有 AD 内码，为 None
    read_rate_hz: float | None
    new_value_rate_hz: float | None
    new_interval_ms_median: float | None
    new_interval_ms_p95: float | None
    new_interval_ms_p99: float | None
    new_interval_ms_max: float | None
    force_mean_n: float | None
    force_std_n: float | None
    force_min_n: float | None
    force_max_n: float | None
