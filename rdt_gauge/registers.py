"""锐力德 RDT-TVK 寄存器表：名称、位宽、读写属性、白名单、取值范围、测试设置值。

依据：锐力德 Modbus RTU 协议（地址取括号内的十进制）、锐力德 Modbus RTU 指令解析、RDT-TVK 变送器说明书 V2.5。
全部代码都按这张表查寄存器，不在别处写地址。

写法参考（登记在 docs/SOURCES.json）：
- 给寄存器起名字、列出"必须为 0"的寄存器：r11_upper_computer/wuji_r11/acquisition/protocols/cybsq_v5.py 的
  CONFIG_NAMES、MUST_BE_OFF；
- 白名单加取值范围、"永远不碰"清单：r11_upper_computer/tools/gauge_probe.py 的 WRITABLE、LIMITS 和文件开头说明；
- 单位编码、小数位取自寄存器 8：r11_upper_computer/wuji_r11/acquisition/protocols/force_sensor.py 的 UNIT_CODES。

只放进白名单的是"关跟踪、关滤波、调速度、调通信"要用的寄存器。下面这些协议里有、但这里不收：
- 一级滤波次数、二级滤波强度、稳定滤波、AD 分辨率：说明书菜单 F1-14~17 有，协议表里没有地址，要问厂家；
- 模拟量输出（130–139）、比较器、IO 口：台架测试用不到。
"""
from __future__ import annotations

from .contracts import RegisterSpec, decode_s32, decode_u32

UNLOCK_REGISTER = 5
UNLOCK_VALUE = 0x5AA5  # 写进寄存器 5 解锁；写别的值上锁；上电默认是锁住的
LOCK_VALUE = 0x0000

# 寄存器 104 的单位编码（协议 1.42 节）
UNIT_CODES = {0: 'none', 1: 'g', 2: 'kg', 3: 't', 4: 'N'}

# 寄存器 1 的波特率编码（高速版；中速版没有 0x09、0x0A，低速版最高到 0x07）
BAUD_CODES = {0x00: 1200, 0x01: 2400, 0x02: 4800, 0x03: 9600, 0x04: 19200, 0x05: 38400, 0x06: 57600,
              0x07: 115200, 0x08: 230400, 0x09: 460800, 0x0A: 921600}
BAUD_TO_CODE = {baud: code for code, baud in BAUD_CODES.items()}

# 寄存器 2 的数据帧格式编码：(校验位, 停止位)
FRAME_CODES = {0x03: ('E', 1), 0x04: ('O', 1), 0x05: ('N', 1), 0x06: ('N', 2)}

# 寄存器 32 的 AD 转换速度编码，单位：次/秒。版本不同编码不同，固件里读不出是哪个版本，所以三套都列出来。
AD_SPEED_CODES = {
    'high': {0x00: 10, 0x01: 80, 0x02: 120, 0x03: 200, 0x04: 400, 0x05: 600, 0x06: 800, 0x07: 1200,
             0x08: 1600, 0x09: 2400, 0x0A: 4800},
    'medium': {0x00: 7.5, 0x01: 15, 0x02: 30, 0x03: 60, 0x04: 120, 0x05: 240, 0x06: 480, 0x07: 960, 0x08: 1920},
    'low': {0x00: 10, 0x01: 40, 0x02: 640, 0x03: 1280},
}

# 寄存器 34 的滤波类型（协议 1.13 节）
FILTER_TYPES = {0x00: '不使用', 0x01: '平均值滤波', 0x02: '中位值滤波', 0x03: '一阶滤波', 0x04: '滑动平均滤波',
                0x05: '中位值平均滤波', 0x06: '滑动中位值平均滤波', 0x07: '平均值滤波 + 一阶滤波',
                0x08: '中位值滤波 + 一阶滤波', 0x09: '滑动平均滤波 + 一阶滤波', 0x0A: '中位值平均滤波 + 一阶滤波'}

_S = RegisterSpec
REGISTERS: tuple[RegisterSpec, ...] = (
    # ---- 系统 ----
    _S(0, 'module_address', '模块地址', writable=True, unlock=True, low=1, high=247,
       note='通信地址；改了以后要用新地址才能连上'),
    _S(1, 'baud_code', '波特率', writable=True, unlock=True, choices=tuple(BAUD_CODES),
       note='编码见 BAUD_CODES；立即生效，改完要马上换成新波特率（用 settings_ops.set_baud）'),
    _S(2, 'frame_code', '数据帧格式', writable=True, unlock=True, choices=tuple(FRAME_CODES),
       note='0x05 = 8N1，0x06 = 8N2（出厂）；立即生效'),
    _S(3, 'protocol_type', '协议类型', note='0 自由协议、1 Modbus RTU、2 ASCII；改了就不能用本脚本通信，不收进白名单'),
    _S(4, 'reply_delay_ms', '指令应答延时', writable=True, low=0, high=255, test_value=0,
       note='单位 ms；非 0 时设备晚这么久才回，测试时用 0'),
    _S(UNLOCK_REGISTER, 'unlock', '锁定/解锁系统配置', readable=False,
       note='只写：0x5AA5 解锁，其它值上锁；由 settings_ops 统一处理，不单独写'),
    _S(6, 'firmware', '固件版本', note='例：0x016A = 362 → V3.62'),
    _S(7, 'factory_reset', '恢复出厂设置', readable=False, note='永远不写：会删掉标定'),
    _S(8, 'status', '模块状态', note='各位见 contracts.StatusBits；bit2–0 是小数位'),
    # ---- 测量 ----
    _S(30, 'measured', '测量值', words=2, signed=True, note='AD 内码经标定后的值；不受去皮影响'),
    _S(32, 'ad_speed_code', 'AD 转换速度', writable=True, choices=tuple(range(0x0B)),
       note='编码按版本不同，见 AD_SPEED_CODES；越快噪声越大'),
    _S(33, 'polarity', '拉压双向', writable=True, choices=(0, 1), test_value=0,
       note='0 双向、1 单向；单向时负值会被截掉，测阶跃要用 0'),
    _S(34, 'filter_type', '滤波类型', writable=True, choices=tuple(FILTER_TYPES), test_value=0,
       note='0 不使用；类型见 FILTER_TYPES'),
    _S(35, 'filter_strength', '滤波强度', writable=True, low=0, high=50, test_value=0, note='0–50，越大越平滑'),
    # ---- 标定：只读，永远不写 ----
    _S(36, 'zero_ad', '零点内码值', words=2, signed=True, note='标定，永远不写'),
    _S(38, 'zero_weight', '零点测量值', words=2, signed=True, note='标定，永远不写'),
    _S(40, 'gain_ad', '增益内码值', words=2, signed=True, note='标定，永远不写'),
    _S(42, 'gain_weight', '增益测量值', words=2, signed=True, note='标定，永远不写'),
    _S(44, 'ad_code', 'AD 内码', words=2, signed=True,
       note='AD 转换经滤波后的原始码；用来判断是不是新值，不能当滤波前的参考'),
    _S(46, 'sensitivity', '传感器灵敏度', words=2, note='免砝码标定用，单位 0.0001 mV/V；永远不写'),
    _S(48, 'capacity', '传感器量程', words=2, note='免砝码标定用；永远不写'),
    _S(61, 'correction_points', '多点修正数量', note='只读'),
    # ---- 称重 ----
    _S(80, 'gross', '毛重', words=2, signed=True, note='实时力值'),
    _S(82, 'net', '净重', words=2, signed=True, note='毛重减皮重'),
    _S(84, 'tare', '皮重', words=2, signed=True, note='写 0x7FFFFFFF 会自动去皮；永远不写'),
    _S(86, 'max_capacity', '最大称量', words=2, note='称台功能用'),
    _S(88, 'division_code', '称台分度', note='0x03 = 0.001、0x04 = 0.002……；会不会让读数变粗要问厂家'),
    _S(93, 'manual_zero_range', '手动置零范围', writable=True, low=0, high=100, test_value=0,
       note='满量程的百分比；0 关闭手动清零（包括面板 ZERO 键和置零命令）'),
    _S(94, 'manual_zero', '执行手动置零', readable=False, note='只写；永远不写：会改零点'),
    _S(95, 'power_on_zero_range', '开机置零范围', writable=True, low=0, high=100, test_value=0,
       note='满量程的百分比；0 关闭开机清零'),
    _S(96, 'auto_zero_range', '自动零位跟踪范围', writable=True, low=0, high=10000, test_value=0,
       note='单位 0.1d；0 关闭零位跟踪'),
    _S(97, 'auto_zero_time', '自动零位跟踪时间', note='单位 0.1 s；跟踪关了就不起作用'),
    _S(98, 'stable_range', '判稳范围', writable=True, low=0, high=10000, test_value=0,
       note='单位 d；0 关闭判稳（V1.1 起）'),
    _S(99, 'stable_time', '判稳时间', note='单位 0.1 s'),
    _S(100, 'zero_range', '零点范围', words=2, note='V1.1 起'),
    _S(102, 'creep_range', '蠕变跟踪范围', writable=True, low=0, high=1000, test_value=0,
       note='单位 0.1d；0 关闭蠕变跟踪（V1.4 起）'),
    _S(103, 'creep_time', '蠕变跟踪时间', note='单位 0.1 s（V1.4 起）'),
    _S(104, 'unit_code', '重量单位', note='编码见 UNIT_CODES；4 = N'),
    # ---- 峰谷值（变送器自己算的，只读看看） ----
    _S(291, 'peak', '峰值', words=2, signed=True),
    _S(293, 'valley', '谷值', words=2, signed=True),
    _S(295, 'peak_detect_mode', '峰值检测使能方式'),
    _S(296, 'valley_detect_mode', '谷值检测使能方式'),
)

# 永远不写：恢复出厂、标定、免砝码标定、多点修正、皮重、执行置零、清除峰谷值。
# 协议里写了地址但表里没收的寄存器，同样不能写（is_writable 只认白名单）。
FORBIDDEN_WRITES = frozenset({7, *range(36, 44), *range(46, 50), *range(60, 67), 84, 85, 94, 290})

# 通信参数：改了电脑这边就连不上，只能走 settings_ops.change_link（cli 的 set-link），set 和 raw 都不让直接写。
LINK_REGISTERS = ('module_address', 'baud_code', 'frame_code')

BY_NAME = {spec.name: spec for spec in REGISTERS}
BY_ADDRESS = {spec.address: spec for spec in REGISTERS}

# 测试设置：会偷偷改读数的跟踪、清零全关，滤波关，应答延时 0，双向（照 R11 cybsq_v5.py 的 MUST_BE_OFF）。
TEST_PRESET = {spec.name: spec.test_value for spec in REGISTERS if spec.test_value is not None}

# 采集前要检查的"会改读数"的寄存器：不为 0 时警告（R11 是直接拒绝开始；台架上可能要故意打开做对照）。
MUST_BE_OFF = ('manual_zero_range', 'power_on_zero_range', 'auto_zero_range', 'stable_range', 'creep_range')

# 采集用的两种读法（见 contracts.READ_MODES）
BLOCK_START, BLOCK_COUNT = 30, 16  # 30–45：测量值在第 0、1 个，AD 内码在第 14、15 个
GROSS_START, GROSS_COUNT = 80, 2

# 备份时分块读。只读可读的寄存器：只写寄存器读出来没有意义，有的设备还会报错。
# 块之间跳过的地址（如 9–29、50–60）协议没写，读了可能报异常，所以单独列在 PROBE_BLOCKS 里试读。
# 80–93 一块：毛重、净重、皮重、最大称量、称台分度、两个标定砝码重量、手动置零范围（都有文档）；94 是只写，跳过。
BACKUP_BLOCKS = ((0, 5), (6, 1), (8, 1), (30, 20), (61, 1), (80, 14), (95, 10), (291, 6))
PROBE_BLOCKS = ((9, 1), (10, 1))  # R11 打开原测力计时读 9（通道数）和 10（字序）；锐力德协议没写，读不读得出都记下来


def spec(name_or_address) -> RegisterSpec:
    """按英文名或地址查寄存器；查不到就报错，不猜。"""
    table = BY_NAME if isinstance(name_or_address, str) else BY_ADDRESS
    try:
        return table[name_or_address]
    except KeyError:
        raise KeyError(f'寄存器表里没有 {name_or_address!r}') from None


def is_writable(address: int) -> bool:
    found = BY_ADDRESS.get(address)
    return found is not None and found.writable and address not in FORBIDDEN_WRITES


def span(item: RegisterSpec) -> range:
    """一个寄存器占的地址范围（32 位的占两个）。"""
    return range(item.address, item.address + item.words)


def value_from(registers: dict[int, int], item: RegisterSpec) -> int | None:
    """从"地址 → 16 位原始值"的字典里取出一个寄存器的值；缺了就返回 None。"""
    if any(address not in registers for address in span(item)):
        return None
    if item.words == 1:
        raw = registers[item.address]
        return raw - 0x10000 if item.signed and raw & 0x8000 else raw
    high, low = registers[item.address], registers[item.address + 1]
    return decode_s32(high, low) if item.signed else decode_u32(high, low)


def describe(item: RegisterSpec, value: int | None) -> str:
    """把寄存器的值翻译成人能看懂的话，例如 baud_code=3 → 9600。"""
    if value is None:
        return '（没读到）'
    if item.name == 'baud_code':
        return f'{BAUD_CODES.get(value, "未知编码")} bps'
    if item.name == 'frame_code':
        found = FRAME_CODES.get(value)
        return f'8{found[0]}{found[1]}' if found else '未知编码'
    if item.name == 'ad_speed_code':
        return ' / '.join(f'{version} {table.get(value, "-")}' for version, table in AD_SPEED_CODES.items()) + ' 次/秒'
    if item.name == 'filter_type':
        return FILTER_TYPES.get(value, '未知类型')
    if item.name == 'unit_code':
        return UNIT_CODES.get(value, '未知单位')
    if item.name == 'polarity':
        return {0: '双向', 1: '单向'}.get(value, '未知')
    if item.name == 'firmware':
        return f'V{value // 100}.{value % 100:02d}'
    return str(value)
