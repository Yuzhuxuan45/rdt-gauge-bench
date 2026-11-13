"""registers.py：表本身要自洽，危险的寄存器写不进去。"""
import pytest

from rdt_gauge import registers as reg
from rdt_gauge.contracts import StatusBits


def test_addresses_do_not_overlap_and_names_are_unique():
    used = {}
    for item in reg.REGISTERS:
        for address in reg.span(item):
            assert address not in used, f'地址 {address} 同时属于 {used.get(address)} 和 {item.name}'
            used[address] = item.name
    assert len(reg.BY_NAME) == len(reg.REGISTERS)


@pytest.mark.parametrize('address', sorted(reg.FORBIDDEN_WRITES))
def test_forbidden_registers_are_never_writable(address):
    assert not reg.is_writable(address)
    found = reg.BY_ADDRESS.get(address)
    assert found is None or not found.writable


@pytest.mark.parametrize('address', [3, 5, 6, 8, 30, 44, 80, 104, 9, 10, 500])
def test_read_only_and_unknown_registers_are_not_writable(address):
    assert not reg.is_writable(address)


def test_only_these_registers_are_writable():
    writable = sorted(item.address for item in reg.REGISTERS if item.writable)
    assert writable == [0, 1, 2, 4, 32, 33, 34, 35, 93, 95, 96, 98, 102]


def test_system_registers_need_unlock():
    assert {item.address for item in reg.REGISTERS if item.unlock} == {0, 1, 2}


def test_test_preset_turns_every_tracking_and_filter_off():
    assert reg.TEST_PRESET == dict(reply_delay_ms=0, polarity=0, filter_type=0, filter_strength=0,
                                   manual_zero_range=0, power_on_zero_range=0, auto_zero_range=0,
                                   stable_range=0, creep_range=0)
    assert set(reg.MUST_BE_OFF) <= set(reg.TEST_PRESET)
    for name, value in reg.TEST_PRESET.items():
        assert reg.spec(name).writable and reg.spec(name).accepts(value)


def test_value_from_handles_16_and_32_bit_registers():
    raw = {80: 0x0000, 81: 0x0084, 82: 0xFFFF, 83: 0xF0C2, 35: 5, 33: 1}
    assert reg.value_from(raw, reg.spec('gross')) == 132
    assert reg.value_from(raw, reg.spec('net')) == -3902  # 标准补码（说明书例子算成 −3901 是算错了）
    assert reg.value_from(raw, reg.spec('filter_strength')) == 5
    assert reg.value_from(raw, reg.spec('measured')) is None  # 30–31 没读到


def test_block_read_positions():
    # 采集一次读 30–45：测量值在开头，AD 内码在第 14、15 个。
    assert reg.spec('measured').address == reg.BLOCK_START
    assert reg.spec('ad_code').address - reg.BLOCK_START == 14
    assert reg.BLOCK_START + reg.BLOCK_COUNT - 1 == reg.spec('ad_code').address + 1
    assert (reg.GROSS_START, reg.GROSS_COUNT) == (reg.spec('gross').address, 2)


def test_backup_blocks_cover_every_readable_register_and_nothing_write_only():
    covered = {address for start, count in reg.BACKUP_BLOCKS for address in range(start, start + count)}
    for item in reg.REGISTERS:
        if item.readable:
            assert set(reg.span(item)) <= covered, f'备份没覆盖 {item.name}'
        else:
            assert item.address not in covered, f'备份不应读只写寄存器 {item.name}'
    assert all(count <= 125 for _, count in reg.BACKUP_BLOCKS)
    addresses = [address for start, count in reg.BACKUP_BLOCKS for address in range(start, start + count)]
    assert len(addresses) == len(set(addresses)), '备份分块有重复读的地址'


def test_codes_from_the_manual():
    assert reg.BAUD_CODES[0x03] == 9600 and reg.BAUD_CODES[0x0A] == 921600 and reg.BAUD_TO_CODE[115200] == 0x07
    assert reg.FRAME_CODES[0x06] == ('N', 2)
    assert reg.AD_SPEED_CODES['high'][0x0A] == 4800 and reg.AD_SPEED_CODES['low'][0x02] == 640
    assert reg.UNIT_CODES[4] == 'N' and reg.FILTER_TYPES[0] == '不使用'


def test_describe_makes_values_readable():
    assert reg.describe(reg.spec('baud_code'), 3) == '9600 bps'
    assert reg.describe(reg.spec('frame_code'), 6) == '8N2'
    assert reg.describe(reg.spec('firmware'), 0x016A) == 'V3.62'
    assert reg.describe(reg.spec('unit_code'), 4) == 'N'
    assert 'high 1200' in reg.describe(reg.spec('ad_speed_code'), 7)
    assert reg.describe(reg.spec('gross'), None) == '（没读到）'


def test_spec_lookup_does_not_guess():
    assert reg.spec('gross') is reg.spec(80)
    with pytest.raises(KeyError):
        reg.spec('grosss')
    with pytest.raises(KeyError):
        reg.spec(81)  # 81 是毛重的低 16 位，不是单独的寄存器


def test_status_register_decimals_feed_the_reading():
    # 寄存器 8 = 0x0802：2 位小数，所以毛重 132 表示 1.32。
    assert 132 / 10 ** StatusBits.from_register(0x0802).decimals == pytest.approx(1.32)
