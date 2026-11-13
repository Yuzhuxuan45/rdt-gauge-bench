"""protocol.py：标准答案都来自锐力德说明书里的报文。"""
import pytest

from rdt_gauge.contracts import DeviceRejected, FrameError, LinkSettings, LinkTimeout
from rdt_gauge.protocol import (
    ModbusClient, add_crc, auto_timeout_s, build_read, build_write, crc16, decode_s32, decode_u32, encode_s32,
    frame_time_s, parse_read, parse_write, read_reply_length, validate_crc)
from scripted_port import ScriptedPort

H = bytes.fromhex


# ---------------- CRC：说明书的例子 ----------------

@pytest.mark.parametrize('body, crc', [
    ('01 03 00 50 00 02', 'C4 1A'),  # 读毛重（通讯举例 一）
    ('01 10 00 05 00 01 02 5A A5', '5C DE'),  # 解锁（指令解析 1.6）
    ('01 03 00 2C 00 02', '05 C2'),  # 读 AD 内码（指令解析 1.19）
    ('01 03 00 06 00 01', '64 0B'),  # 读固件版本（指令解析 1.7）
    ('01 03 00 08 00 01', '05 C8'),  # 读模块状态（指令解析 1.9）
    ('01 03 04 00 00 00 84', 'FA 50'),  # 读毛重的回复（通讯举例 一）
])
def test_crc_matches_the_manual(body, crc):
    assert add_crc(H(body)) == H(body) + H(crc)


def test_crc_of_a_full_frame_with_its_crc_is_zero():
    # Modbus CRC 的性质：整帧（含 CRC）再算一遍结果是 0。用来交叉检查算法本身。
    assert crc16(H('01 03 04 00 00 00 84 FA 50')) == 0


def test_validate_crc_rejects_a_flipped_byte():
    validate_crc(H('01 03 04 00 00 00 84 FA 50'))
    with pytest.raises(FrameError, match='CRC'):
        validate_crc(H('01 03 04 00 00 00 85 FA 50'))


# ---------------- 组帧 ----------------

def test_build_read_matches_the_manual():
    assert build_read(1, 80, 2) == H('01 03 00 50 00 02 C4 1A')
    assert build_read(1, 44, 2) == H('01 03 00 2C 00 02 05 C2')


def test_build_write_matches_the_manual():
    assert build_write(1, 5, [0x5AA5]) == H('01 10 00 05 00 01 02 5A A5 5C DE')  # 解锁
    assert build_write(1, 34, [8]) == H('01 10 00 22 00 01 02 00 08 A1 14')  # 滤波类型改 8（指令解析 1.13）
    assert build_write(1, 32, [2]) == H('01 10 00 20 00 01 02 00 02 20 F1')  # AD 速度（指令解析 1.11）


@pytest.mark.parametrize('call', [
    lambda: build_read(0, 80, 2), lambda: build_read(248, 80, 2), lambda: build_read(1, 80, 0),
    lambda: build_read(1, 80, 126), lambda: build_read(1, 0xFFFF, 2), lambda: build_write(1, 34, []),
    lambda: build_write(1, 34, [0x10000]), lambda: build_write(1, 34, [-1]), lambda: build_write(1, 34, [1.0]),
])
def test_bad_requests_are_refused_before_sending(call):
    with pytest.raises(ValueError):
        call()


# ---------------- 解析回复 ----------------

def test_parse_read_reply_from_the_manual():
    assert parse_read(H('01 03 04 00 00 00 84 FA 50'), 1, 2) == [0, 0x84]
    assert parse_read(H('01 03 02 08 02 3E 45'), 1, 1) == [0x0802]  # 模块状态
    assert parse_read(H('01 03 02 01 6A 39 FB'), 1, 1) == [0x016A]  # 固件版本 V3.62


@pytest.mark.parametrize('frame, why', [
    ('01 03 04 00 00 00 84 FA', '不完整'),  # 少一个字节
    ('01 03 04 00 00 00 85 FA 50', 'CRC'),  # 数据被改
    (add_crc(H('02 03 04 00 00 00 84')).hex(' '), '格式'),  # 地址不对
    (add_crc(H('01 04 04 00 00 00 84')).hex(' '), '格式'),  # 功能码不对
    (add_crc(H('01 03 02 00 00 00 84')).hex(' '), '格式'),  # 字节数不对
])
def test_parse_read_rejects_bad_replies(frame, why):
    with pytest.raises(FrameError, match=why):
        parse_read(H(frame), 1, 2)


def test_parse_read_recognises_a_device_exception():
    with pytest.raises(DeviceRejected) as info:
        parse_read(add_crc(H('01 83 02')), 1, 2)
    assert (info.value.function, info.value.code) == (0x03, 0x02)


def test_parse_write_needs_an_exact_echo():
    request = build_write(1, 5, [0x5AA5])
    parse_write(H('01 10 00 05 00 01 11 C8'), request)  # 说明书里解锁的回复
    with pytest.raises(FrameError, match='对不上'):
        parse_write(add_crc(H('01 10 00 06 00 01')), request)
    with pytest.raises(DeviceRejected):
        parse_write(add_crc(H('01 90 04')), request)


# ---------------- 解码 ----------------

@pytest.mark.parametrize('high, low, value', [
    (0x0000, 0x0084, 132),  # 通讯举例：00 00 00 84 → 132
    (0xFFFF, 0xF0C2, -3902),  # 指令解析 1.82 的报文；说明书算成 −3901 是算错了，见下一个测试
    (0xFFFF, 0xF0C3, -3901),
    (0xFFFF, 0xFFFF, -1), (0x0000, 0x0000, 0), (0x7FFF, 0xFFFF, 2**31 - 1), (0x8000, 0x0000, -2**31),
])
def test_decode_s32_high_word_first(high, low, value):
    assert decode_s32(high, low) == value
    assert encode_s32(value) == (high, low)


def test_the_manuals_negative_example_is_off_by_one():
    # 指令解析 1.82 把 FF FF F0 C2 算成 −3901，用的是"减 0xFFFFFFFF"，少减了 1。
    # 协议写的是"负数采用标准补码方式"，标准补码下 −1 是 FF FF FF FF，FF FF F0 C2 是 −3902。
    # 不要照说明书的例子把解码"改对"；实机到货后用一个已知的负读数（例如拉向加载）再核对一次。
    assert decode_s32(0xFFFF, 0xF0C2) == 0xFFFFF0C2 - 0xFFFFFFFF - 1 == -3902


def test_decode_u32_and_bad_words():
    assert decode_u32(0x0001, 0x86A0) == 100000  # 指令解析 1.21：量程 100000
    with pytest.raises(ValueError):
        decode_s32(0x10000, 0)
    with pytest.raises(ValueError):
        encode_s32(2**31)


# ---------------- 超时 ----------------

def test_timeout_follows_the_baudrate():
    slow, fast = LinkSettings('COM9', baudrate=9600), LinkSettings('COM9', baudrate=921600)
    request, reply = 8, read_reply_length(16)
    assert frame_time_s(11, slow) == pytest.approx(11 * 11 / 9600)  # 8N2：每字节 11 位
    assert auto_timeout_s(slow, request, reply) > 0.07  # 9600 下读 30–45，线上约 52 ms
    assert auto_timeout_s(fast, request, reply) < 0.03
    assert auto_timeout_s(LinkSettings('COM9', timeout_s=0.3), request, reply) == 0.3  # 手动给的优先


# ---------------- ModbusClient（用固定回复替身） ----------------

def test_client_reads_and_keeps_the_raw_frame():
    port = ScriptedPort([H('01 03 04 00 00 00 84 FA 50')])
    client = ModbusClient(port, LinkSettings('COM9'))
    assert client.read(80, 2) == [0, 0x84]
    assert port.written == [H('01 03 00 50 00 02 C4 1A')]
    assert client.last_frame == H('01 03 04 00 00 00 84 FA 50')
    assert port.timeout == pytest.approx(auto_timeout_s(client.link, 8, 9))


def test_client_retries_after_a_bad_frame_and_clears_the_buffer():
    port = ScriptedPort([b'', H('01 03 04 00 00 00 85 FA 50'), H('01 03 04 00 00 00 84 FA 50')])
    client = ModbusClient(port, LinkSettings('COM9'))
    assert client.read(80, 2, attempts=3) == [0, 0x84]
    assert len(port.written) == 3 and port.resets == 2


def test_client_gives_up_with_the_last_error():
    port = ScriptedPort([b'', b''])
    client = ModbusClient(port, LinkSettings('COM9'))
    with pytest.raises(LinkTimeout):
        client.read(80, 2, attempts=2)


def test_client_does_not_retry_when_the_device_refuses():
    port = ScriptedPort([add_crc(H('01 83 02'))])
    client = ModbusClient(port, LinkSettings('COM9'))
    with pytest.raises(DeviceRejected):
        client.read(80, 2, attempts=3)
    assert len(port.written) == 1


def test_client_writes_once_and_checks_the_echo():
    port = ScriptedPort([H('01 10 00 05 00 01 11 C8')])
    client = ModbusClient(port, LinkSettings('COM9'))
    client.write(5, [0x5AA5])
    assert port.written == [H('01 10 00 05 00 01 02 5A A5 5C DE')]
    with pytest.raises(LinkTimeout):
        ModbusClient(ScriptedPort([b'']), LinkSettings('COM9')).write(5, [0x5AA5])  # 写不重试


def test_optional_reply_write_tolerates_silence_and_garbage_but_not_a_refusal():
    # 改波特率时设备按新波特率回复，旧波特率下收到乱码或什么也收不到。
    assert ModbusClient(ScriptedPort([b'']), LinkSettings('COM9')).write(1, [7], reply_optional=True) is False
    assert ModbusClient(ScriptedPort([H('FF 00 FF')]), LinkSettings('COM9')).write(1, [7], reply_optional=True) is False
    echo = add_crc(build_write(1, 1, [7])[:6])
    assert ModbusClient(ScriptedPort([echo]), LinkSettings('COM9')).write(1, [7], reply_optional=True) is True
    with pytest.raises(DeviceRejected):
        ModbusClient(ScriptedPort([add_crc(H('01 90 04'))]), LinkSettings('COM9')).write(1, [7], reply_optional=True)


def test_client_uses_its_own_address():
    port = ScriptedPort([add_crc(H('06 03 02 00 07'))])
    client = ModbusClient(port, LinkSettings('COM9', address=6))
    assert client.read(1, 1) == [7]
    assert port.written[0][0] == 6
