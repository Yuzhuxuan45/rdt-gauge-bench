"""测试用的回复字节：按说明书的帧格式**事先**拼好，按顺序交给 ScriptedPort。

只拼字节，不看代码发了什么请求，所以不是假测力计；请求对不对由各测试自己检查 port.written。
"""
import struct

from rdt_gauge.protocol import add_crc, build_write, encode_s32
from rdt_gauge.registers import BACKUP_BLOCKS, PROBE_BLOCKS


def read_reply(values, address=1):
    return add_crc(bytes([address, 0x03, 2 * len(values)]) + struct.pack(f'>{len(values)}H', *values))


def exception_reply(function=0x03, code=0x02, address=1):
    return add_crc(bytes([address, function | 0x80, code]))


def write_echo(start, values, address=1):
    return add_crc(build_write(address, start, values)[:6])


def block_reply(measured, ad_code, address=1):
    """30–45 一块：测量值在第 0、1 个，AD 内码在第 14、15 个，其余填 0。"""
    words = [0] * 16
    words[0:2] = encode_s32(measured)
    words[14:16] = encode_s32(ad_code)
    return read_reply(words, address)


def backup_replies(registers, *, probes=None, address=1):
    """settings_ops.read_all 依次读 BACKUP_BLOCKS、PROBE_BLOCKS；按这个顺序给回复。默认 9、10 都回异常（协议没写）。"""
    replies = [read_reply([registers.get(start + offset, 0) for offset in range(count)], address)
               for start, count in BACKUP_BLOCKS]
    replies += probes if probes is not None else [exception_reply(address=address) for _ in PROBE_BLOCKS]
    return replies


# 一份像出厂的寄存器（取值按协议的默认值，故意让几个跟踪、滤波不是 0，好测"改成测试设置"）。不是任何真实设备。
FACTORY = {
    0: 1, 1: 0x03, 2: 0x06, 3: 1, 4: 0, 6: 0x016A, 8: 0x0002,
    30: 0, 31: 132, 32: 0x07, 33: 0, 34: 9, 35: 5,
    44: 0x0019, 45: 0x3B67,
    80: 0, 81: 132, 93: 50, 95: 10, 96: 100, 97: 10, 98: 100, 99: 10, 101: 100, 102: 10, 103: 100, 104: 4,
}
# FACTORY 和测试设置不同的 7 项：(地址, 原值)
FACTORY_DIFFS = ((34, 9), (35, 5), (93, 50), (95, 10), (96, 100), (98, 100), (102, 10))
