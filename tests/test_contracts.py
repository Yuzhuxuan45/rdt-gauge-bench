"""contracts.py：参数检查、状态位解码、数据行格式。"""
import math

import pytest

from rdt_gauge.contracts import (
    SAMPLE_FIELDS, DeviceRejected, LinkSettings, RegisterSpec, SampleRow, StatusBits, WriteReceipt)


def test_link_defaults_are_the_factory_settings():
    link = LinkSettings('COM9')
    assert (link.baudrate, link.address, link.parity, link.stopbits, link.timeout_s) == (9600, 1, 'N', 2, None)


@pytest.mark.parametrize('kwargs', [
    dict(port=''), dict(port='COM9', baudrate=500000), dict(port='COM9', address=0), dict(port='COM9', address=248),
    dict(port='COM9', stopbits=3), dict(port='COM9', parity='X'), dict(port='COM9', timeout_s=0),
    dict(port='COM9', timeout_s=math.nan), dict(port='COM9', settle_s=-1),
])
def test_link_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        LinkSettings(**kwargs)


def test_status_word_example_from_the_manual():
    # 锐力德 Modbus RTU 指令解析 1.9：返回 08 02 → 峰值已检测、2 位小数，其余各位为 0。
    status = StatusBits.from_register(0x0802)
    assert status.decimals == 2 and status.peak_detected
    assert not any((status.negative, status.power_on_zeroed, status.unstable, status.overflow, status.at_zero,
                    status.smart_sensor, status.overload, status.valley_detected))


def test_status_word_overflow_and_overload_bits():
    status = StatusBits.from_register(1 << 6 | 1 << 9)
    assert status.overflow and status.overload and status.decimals == 0


@pytest.mark.parametrize('value', [-1, 0x10000, 1.5, True])
def test_status_word_rejects_non_register_values(value):
    with pytest.raises(ValueError):
        StatusBits.from_register(value)


def test_writable_register_needs_allowed_values():
    with pytest.raises(ValueError):
        RegisterSpec(34, 'filter_type', '滤波类型', writable=True)


def test_register_accepts_only_allowed_integers():
    spec = RegisterSpec(35, 'filter_strength', '滤波强度', writable=True, low=0, high=50, test_value=0)
    assert spec.accepts(0) and spec.accepts(50)
    assert not spec.accepts(51) and not spec.accepts(-1) and not spec.accepts(True) and not spec.accepts(5.0)
    enum = RegisterSpec(34, 'filter_type', '滤波类型', writable=True, choices=(0, 9))
    assert enum.accepts(9) and not enum.accepts(5)


def test_test_value_must_itself_be_writable():
    with pytest.raises(ValueError):
        RegisterSpec(34, 'filter_type', '滤波类型', writable=True, choices=(1, 2), test_value=0)


@pytest.mark.parametrize('kwargs', [dict(words=3), dict(address=-1), dict(address=0xFFFF, words=2), dict(name='a-b'),
                                    dict(low=5, high=1)])
def test_register_spec_rejects_bad_definitions(kwargs):
    with pytest.raises(ValueError):
        RegisterSpec(**dict(dict(address=1, name='x', label='x'), **kwargs))


def test_sample_row_columns_follow_the_csv_header():
    row = SampleRow(1, 10, 20, error='LinkTimeout: 超时')
    assert tuple(row.as_row()) == SAMPLE_FIELDS
    assert not row.ok and SampleRow(2, 30, 40, measured=132).ok


def test_write_receipt_is_ok_only_when_read_back_matches():
    assert WriteReceipt(35, 'filter_strength', 5, 0, 0).ok
    assert not WriteReceipt(35, 'filter_strength', 5, 0, 5).ok
    assert not WriteReceipt(35, 'filter_strength', 5, 0, None).ok


def test_device_rejected_keeps_function_and_code():
    exc = DeviceRejected(0x10, 0x02)
    assert (exc.function, exc.code) == (0x10, 0x02) and '0x02' in str(exc)
