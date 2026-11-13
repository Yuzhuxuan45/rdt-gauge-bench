"""ports.py：用编出来的串口列表和固定回复替身，不碰本机串口。序列号都是编的。"""
from types import SimpleNamespace

import pytest

from rdt_gauge.contracts import BenchError
from rdt_gauge.ports import (FTDI_VID, PortInfo, apply_link, ftdi_latency_ms, list_ports, resolve_port, scan,
                             scan_plan)
from rdt_gauge.protocol import add_crc, build_read
from scripted_port import ScriptedPort

H = bytes.fromhex
FTDI_A = PortInfo('COM9', 'USB Serial Port', FTDI_VID, 0x6001, 'TESTSN01', 'FTDI')
FTDI_B = PortInfo('COM12', 'USB Serial Port', FTDI_VID, 0x6001, 'TESTSN02', 'FTDI')
CH340 = PortInfo('COM3', 'USB-SERIAL CH340', 0x1A86, 0x7523, None, 'wch.cn')
BUILTIN = PortInfo('COM1', 'Communications Port')


def test_list_ports_reads_pyserial_fields_without_opening_anything():
    fake = [SimpleNamespace(device='COM9', description='USB Serial Port', vid=0x0403, pid=0x6001,
                            serial_number='TESTSN01', manufacturer='FTDI'),
            SimpleNamespace(device='COM1', description=None, vid=None, pid=None, serial_number='', manufacturer=None)]
    ports = list_ports(comports=lambda: fake)
    assert [port.device for port in ports] == ['COM1', 'COM9']
    assert ports[1].is_ftdi and ports[1].serial_number == 'TESTSN01' and not ports[0].is_ftdi
    assert ports[0].serial_number is None
    assert ports[1].as_dict()['vid_hex'] == '0x0403'


def test_real_port_listing_is_blocked_offline():
    with pytest.raises(RuntimeError, match='离线测试'):
        list_ports()


def test_explicit_port_is_used_as_given():
    assert resolve_port(port='COM7', candidates=[]) == ('COM7', dict(resolution='explicit', port='COM7'))


def test_serial_number_finds_the_port_case_insensitively():
    port, evidence = resolve_port(serial_number='testsn02', candidates=[FTDI_A, FTDI_B, CH340])
    assert port == 'COM12' and evidence['resolution'] == 'serial_number'


def test_serial_number_and_port_must_agree():
    assert resolve_port(port='com12', serial_number='TESTSN02', candidates=[FTDI_A, FTDI_B])[0] == 'COM12'
    with pytest.raises(BenchError, match='不是 COM9'):
        resolve_port(port='COM9', serial_number='TESTSN02', candidates=[FTDI_A, FTDI_B])


def test_unknown_serial_number_is_refused():
    with pytest.raises(BenchError, match='匹配到 0 个'):
        resolve_port(serial_number='NOPE', candidates=[FTDI_A])


def test_the_only_ftdi_port_is_used_when_nothing_is_given():
    port, evidence = resolve_port(candidates=[CH340, FTDI_A, BUILTIN])
    assert port == 'COM9' and evidence['resolution'] == 'only_ftdi'


@pytest.mark.parametrize('candidates', [[CH340, BUILTIN], [FTDI_A, FTDI_B]])
def test_zero_or_several_ftdi_ports_are_not_guessed(candidates):
    with pytest.raises(BenchError, match='分不出'):
        resolve_port(candidates=candidates)


def test_scan_plan_tries_factory_settings_first_and_only_supported_rates():
    plan = scan_plan()
    assert plan[0] == (9600, 'N', 2, 1) and plan[1] == (9600, 'N', 1, 1)
    assert len(plan) == 11 * 4
    with pytest.raises(ValueError):
        scan_plan(baudrates=(500000,))


class _Opener:
    """按扫描顺序给每个组合一个固定回复替身，记下打开过哪些组合。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.links = []
        self.ports = []

    def __call__(self, link):
        self.links.append(link)
        port = ScriptedPort(self.replies.pop(0))
        port.close = lambda: None
        self.ports.append(port)
        return port


def test_scan_stops_at_the_first_combination_that_answers():
    firmware = add_crc(H('01 03 02 01 6A'))
    opener = _Opener([[b'', b''], [b'', b''], [firmware]])
    results = scan('COM9', frames=(('N', 2), ('N', 1), ('E', 1)), baudrates=(9600,), open_port=opener)
    assert [r['ok'] for r in results] == [False, False, True]
    assert results[-1]['firmware'] == 0x016A and (results[-1]['parity'], results[-1]['stopbits']) == ('E', 1)


def test_scan_only_ever_sends_read_requests():
    opener = _Opener([[b'', b''] for _ in range(8)])
    scan('COM9', baudrates=(9600, 115200), open_port=opener)
    sent = [frame for port in opener.ports for frame in port.written]
    assert sent and all(frame == build_read(1, 6, 1) for frame in sent)


def test_scan_counts_a_device_exception_as_reachable():
    opener = _Opener([[add_crc(H('01 83 02'))]])
    results = scan('COM9', baudrates=(9600,), frames=(('N', 2),), open_port=opener)
    assert results[0]['ok'] and results[0]['firmware'] is None


def test_scan_stops_when_the_port_cannot_be_opened():
    def refuse(link):
        raise OSError('拒绝访问')
    results = scan('COM9', open_port=refuse)
    assert len(results) == 1 and not results[0]['ok'] and '打不开' in results[0]['error']


def test_open_serial_passes_the_link_settings_to_pyserial(monkeypatch):
    import serial
    from rdt_gauge.contracts import LinkSettings
    from rdt_gauge.ports import open_serial
    opened = {}

    class Recorder:
        def __init__(self, **kwargs):
            opened.update(kwargs)
            self.resets = 0

        def reset_input_buffer(self):
            self.resets += 1

    monkeypatch.setattr(serial, 'Serial', Recorder)  # 只在这个测试里换掉（离线防护本来就把它换成报错）
    handle = open_serial(LinkSettings('COM9', baudrate=921600, parity='E', stopbits=1, settle_s=0))
    assert opened['port'] == 'COM9' and opened['baudrate'] == 921600
    assert opened['parity'] == serial.PARITY_EVEN and opened['stopbits'] == serial.STOPBITS_ONE
    assert opened['bytesize'] == serial.EIGHTBITS and handle.resets == 1


def test_apply_link_switches_the_pc_side():
    import serial
    from rdt_gauge.contracts import LinkSettings
    port = ScriptedPort([])
    apply_link(port, LinkSettings('COM9', baudrate=921600, parity='E', stopbits=1))
    assert (port.baudrate, port.parity, port.stopbits, port.resets) == (921600, 'E', 1, 1)
    # 直接赋值能成立的前提：pyserial 的常量就是这些值
    assert (serial.PARITY_NONE, serial.PARITY_EVEN, serial.PARITY_ODD) == ('N', 'E', 'O')
    assert (serial.STOPBITS_ONE, serial.STOPBITS_TWO) == (1, 2)


def test_ftdi_latency_reader_only_reads():
    # 只读注册表；这台电脑插没插 FTDI 都行，返回的是字典。
    found = ftdi_latency_ms()
    assert isinstance(found, dict)
    assert all(value is None or isinstance(value, int) for value in found.values())
