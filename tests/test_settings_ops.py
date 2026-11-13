"""settings_ops.py：默认只读、只写白名单、写完读回、记录先落盘、改通信参数。回复按顺序给，不碰设备。"""
import pytest

from rdt_gauge.contracts import BenchError, LinkSettings, NotAllowed
from rdt_gauge.protocol import ModbusClient, build_read, build_write
from rdt_gauge.registers import BACKUP_BLOCKS, LOCK_VALUE, TEST_PRESET, UNLOCK_REGISTER, UNLOCK_VALUE
from rdt_gauge import settings_ops as ops
from rdt_gauge.storage import read_json, write_json
from frames import FACTORY, FACTORY_DIFFS, backup_replies, exception_reply, read_reply, write_echo
from scripted_port import ScriptedPort

PRESET = {**FACTORY, **{address: 0 for address, _ in FACTORY_DIFFS}}  # 改成测试设置以后的寄存器
WRITES = 0x10


def _client(replies, link=None):
    return ModbusClient(ScriptedPort(replies), link or LinkSettings('COM9'))


def _functions(client):
    return [frame[1] for frame in client.port.written]


def _snapshot(registers):
    return ops.read_all(_client(backup_replies(registers)))


# ---------------- 读 ----------------

def test_read_all_decodes_every_readable_register_and_records_probes():
    client = _client(backup_replies(FACTORY, probes=[exception_reply(), read_reply([0x0001])]))
    snapshot = ops.read_all(client)
    values = snapshot['values']
    assert values['gross'] == 132 and values['status'] == 2 and values['filter_type'] == 9 and values['unit_code'] == 4
    assert snapshot['errors'] == []
    assert snapshot['probes']['9']['ok'] is False and 'DeviceRejected' in snapshot['probes']['9']['error']
    assert snapshot['probes']['10'] == dict(ok=True, values=[1])
    expected = [build_read(1, start, count) for start, count in BACKUP_BLOCKS] + [build_read(1, 9, 1),
                                                                                  build_read(1, 10, 1)]
    assert client.port.written == expected  # 只发 03 读，顺序固定


def test_read_all_records_a_missing_block_and_carries_on():
    replies = backup_replies(FACTORY)
    replies[0:1] = [b'', b'']  # 第一块（0–4）两次都超时
    snapshot = ops.read_all(_client(replies))
    assert len(snapshot['errors']) == 1 and snapshot['errors'][0]['start'] == 0
    assert snapshot['values']['baud_code'] is None and snapshot['values']['gross'] == 132


def test_display_rows_translate_values():
    rows = {row['name']: row for row in ops.display_rows(_snapshot(FACTORY))}
    assert rows['baud_code']['text'] == '9600 bps' and rows['frame_code']['text'] == '8N2'
    assert rows['filter_type']['text'].startswith('滑动平均') and rows['firmware']['text'] == 'V3.62'
    assert rows['filter_type']['writable'] and not rows['gross']['writable']
    assert 'unlock' not in rows  # 只写寄存器不显示


def test_reading_scale_comes_from_status_and_unit():
    assert ops.reading_scale(_snapshot(FACTORY)) == (2, 'N')
    with pytest.raises(BenchError):
        ops.reading_scale(dict(values=dict(status=None, unit_code=4)))


def test_check_test_settings_lists_every_difference():
    warnings = ops.check_test_settings(_snapshot(FACTORY))
    assert len(warnings) == 7 and any('自动零位跟踪' in w and '中途改读数' in w for w in warnings)
    assert ops.check_test_settings(_snapshot(PRESET)) == []
    assert any('单位' in w for w in ops.check_test_settings(_snapshot({**PRESET, 104: 0})))


# ---------------- 计划 ----------------

@pytest.mark.parametrize('wanted, why', [
    (dict(nope=1), '没有'), (dict(baud_code=10), 'change_link'), (dict(tare=0), '白名单'),
    (dict(filter_strength=51), '允许的值'), (dict(filter_type=11), '允许的值'), (dict(factory_reset=0x55), '白名单'),
])
def test_plan_refuses_anything_not_allowed(wanted, why):
    with pytest.raises(NotAllowed, match=why):
        ops.plan_changes(_snapshot(FACTORY)['values'], wanted)


def test_plan_skips_unchanged_and_refuses_unknown_current_values():
    values = _snapshot(FACTORY)['values']
    assert ops.plan_changes(values, dict(filter_type=9)) == []
    planned = ops.plan_changes(values, dict(filter_type=0, filter_strength=5))
    assert [(p['name'], p['old'], p['new']) for p in planned] == [('filter_type', 9, 0)]
    with pytest.raises(NotAllowed, match='没读到'):
        ops.plan_changes(dict(values, filter_type=None), dict(filter_type=0))


# ---------------- 改普通寄存器 ----------------

def test_dry_run_only_reads(tmp_path):
    client = _client(backup_replies(FACTORY))
    result = ops.preset_test(client, record_dir=tmp_path)
    assert result['ok'] and not result['applied'] and len(result['planned']) == 7
    assert set(_functions(client)) == {0x03} and not list(tmp_path.iterdir())  # 没写设备，也没写记录


def test_preset_writes_reads_back_and_keeps_a_record(tmp_path):
    replies = backup_replies(FACTORY)
    for address, _ in FACTORY_DIFFS:
        replies += [write_echo(address, [0]), read_reply([0])]
    replies += backup_replies(PRESET)
    client = _client(replies)
    result = ops.preset_test(client, record_dir=tmp_path, apply=True)
    assert result['ok'], result['problem']
    assert [r['address'] for r in result['receipts']] == [address for address, _ in FACTORY_DIFFS]
    writes = [frame for frame in client.port.written if frame[1] == WRITES]
    assert writes == [build_write(1, address, [0]) for address, _ in FACTORY_DIFFS]
    record = read_json(result['record'])
    assert record['ok'] and record['before']['values']['filter_type'] == 9 and record['after']['values']['filter_type'] == 0
    assert not ops.check_test_settings(record['after'])


def test_the_record_with_the_original_values_is_on_disk_before_the_first_write(tmp_path):
    seen = []

    class WatchingPort(ScriptedPort):
        def write(self, data):
            if data[1] == WRITES and not seen:  # 第一条写命令发出去的那一刻
                seen.extend(read_json(path) for path in tmp_path.glob('change-*.json'))
            return super().write(data)

    replies = backup_replies(FACTORY) + [write_echo(34, [0]), read_reply([0])] + backup_replies({**FACTORY, 34: 0})
    result = ops.change_settings(ModbusClient(WatchingPort(replies), LinkSettings('COM9')), dict(filter_type=0),
                                 record_dir=tmp_path, apply=True)
    assert result['ok'] and len(seen) == 1
    assert seen[0]['before']['values']['filter_type'] == 9 and seen[0]['receipts'] == []  # 还没写，原值已在盘上


def test_a_wrong_read_back_stops_further_writes(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(34, [0]), read_reply([0]), write_echo(35, [0]), read_reply([5])]
    replies += backup_replies({**FACTORY, 34: 0})
    client = _client(replies)
    result = ops.preset_test(client, record_dir=tmp_path, apply=True)
    assert not result['ok'] and '读回 5' in result['problem']
    assert len([f for f in client.port.written if f[1] == WRITES]) == 2
    assert result['wrong']['filter_strength'] == 5 and read_json(result['record'])['ok'] is False


def test_a_refused_write_suggests_unlocking(tmp_path):
    replies = backup_replies(FACTORY) + [exception_reply(function=WRITES, code=0x04)] + backup_replies(FACTORY)
    result = ops.change_settings(_client(replies), dict(filter_type=0), record_dir=tmp_path, apply=True)
    assert not result['ok'] and '--unlock' in result['problem']


def test_unlock_wraps_the_writes_and_relocks_even_after_a_failure(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(UNLOCK_REGISTER, [UNLOCK_VALUE]), write_echo(34, [0]),
                                         read_reply([9]), write_echo(UNLOCK_REGISTER, [LOCK_VALUE])]
    replies += backup_replies(FACTORY)
    client = _client(replies)
    result = ops.change_settings(client, dict(filter_type=0), record_dir=tmp_path, apply=True, unlock=True)
    writes = [frame for frame in client.port.written if frame[1] == WRITES]
    assert writes[0] == build_write(1, UNLOCK_REGISTER, [UNLOCK_VALUE])
    assert writes[-1] == build_write(1, UNLOCK_REGISTER, [LOCK_VALUE])
    assert not result['ok'] and '读回 9' in result['problem']


def test_nothing_is_written_when_the_first_read_is_incomplete(tmp_path):
    replies = backup_replies(FACTORY)
    replies[0:1] = [b'', b'']
    client = _client(replies)
    result = ops.preset_test(client, record_dir=tmp_path, apply=True)
    assert not result['ok'] and '不写' in result['problem'] and set(_functions(client)) == {0x03}


def test_restore_takes_only_writable_ordinary_registers(tmp_path):
    record = tmp_path / 'backup.json'
    write_json(record, _snapshot(FACTORY))
    wanted = ops.restorable_values(read_json(record))
    assert wanted['filter_type'] == 9 and wanted['auto_zero_range'] == 100
    assert 'baud_code' not in wanted and 'gross' not in wanted and 'tare' not in wanted
    result = ops.restore(_client(backup_replies(PRESET)), record, record_dir=tmp_path / 'records')
    assert result['ok'] and len(result['planned']) == 7 and result['restored_from'] == str(record)


# ---------------- 改通信参数 ----------------

def test_change_baudrate_switches_both_sides_and_relocks(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(UNLOCK_REGISTER, [UNLOCK_VALUE]), b'', read_reply([0x0A]),
                                         write_echo(UNLOCK_REGISTER, [LOCK_VALUE])]
    replies += backup_replies({**FACTORY, 1: 0x0A})
    client = _client(replies)
    result = ops.change_link(client, baudrate=921600, record_dir=tmp_path, apply=True, settle_s=0)
    assert result['ok'], result['problem']
    assert client.link.baudrate == 921600 and client.port.baudrate == 921600
    assert result['steps'] == ['已解锁', '已写入（没收到回显，按新参数核对）', '新参数下读回一致', '已重新上锁']
    assert read_json(result['record'])['after']['values']['baud_code'] == 0x0A


def test_change_baudrate_that_did_not_take_returns_to_the_old_settings(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(UNLOCK_REGISTER, [UNLOCK_VALUE]), b'', b'', b'', b'',
                                         read_reply([0x03]), write_echo(UNLOCK_REGISTER, [LOCK_VALUE])]
    client = _client(replies)
    result = ops.change_link(client, baudrate=921600, record_dir=tmp_path, apply=True, settle_s=0)
    assert not result['ok'] and '没改成' in result['problem']
    assert client.link.baudrate == 9600 and client.port.baudrate == 9600
    assert client.port.written[-1] == build_write(1, UNLOCK_REGISTER, [LOCK_VALUE])


def test_change_baudrate_that_loses_the_device_gives_the_recovery_steps(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(UNLOCK_REGISTER, [UNLOCK_VALUE]), b''] + [b''] * 6
    client = _client(replies)
    result = ops.change_link(client, baudrate=921600, record_dir=tmp_path, apply=True, settle_s=0)
    assert not result['ok'] and result['problem'] == ops.RECOVERY_HINT
    assert client.link.baudrate == client.port.baudrate  # 电脑这边的记录和串口实际参数一致
    assert read_json(result['record'])['problem'] == ops.RECOVERY_HINT


def test_change_frame_format_switches_parity_and_stop_bits(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(UNLOCK_REGISTER, [UNLOCK_VALUE]), b'', read_reply([0x05]),
                                         write_echo(UNLOCK_REGISTER, [LOCK_VALUE])]
    replies += backup_replies({**FACTORY, 2: 0x05})
    client = _client(replies)
    result = ops.change_link(client, frame_code=0x05, record_dir=tmp_path, apply=True, settle_s=0)
    assert result['ok'] and (client.link.parity, client.link.stopbits) == ('N', 1)
    assert (client.port.parity, client.port.stopbits) == ('N', 1)


def test_change_link_dry_run_and_no_op(tmp_path):
    client = _client(backup_replies(FACTORY))
    assert ops.change_link(client, baudrate=921600, record_dir=tmp_path)['ok']
    assert set(_functions(client)) == {0x03}
    same = ops.change_link(_client(backup_replies(FACTORY)), baudrate=9600, record_dir=tmp_path, apply=True)
    assert same['ok'] and same['already'] and not list(tmp_path.iterdir())


@pytest.mark.parametrize('kwargs', [dict(baudrate=500000), dict(baudrate=9600, frame_code=5), dict(),
                                    dict(frame_code=7), dict(address=0), dict(address=248)])
def test_change_link_refuses_bad_requests(kwargs, tmp_path):
    with pytest.raises(NotAllowed):
        ops.change_link(_client([]), record_dir=tmp_path, **kwargs)


def test_test_preset_and_factory_fixture_agree():
    # 测试数据自洽：FACTORY 和 TEST_PRESET 不同的正好是 FACTORY_DIFFS 这 7 项
    from rdt_gauge.registers import spec
    differing = sorted(spec(name).address for name, value in TEST_PRESET.items() if FACTORY.get(spec(name).address, 0) != value)
    assert differing == [address for address, _ in FACTORY_DIFFS]
