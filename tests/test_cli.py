"""cli.py：命令解析和各命令的串联。连接换成固定回复替身，数据写到临时目录，不碰设备。"""
import pytest

from rdt_gauge import cli
from rdt_gauge.contracts import SAMPLE_FIELDS, LinkSettings, SampleRow
from rdt_gauge.ports import FTDI_VID, PortInfo
from rdt_gauge.protocol import ModbusClient, build_write
from rdt_gauge.storage import CsvWriter, read_json, write_json
from frames import FACTORY, FACTORY_DIFFS, backup_replies, block_reply, read_reply, write_echo
from scripted_port import ScriptedPort

PRESET = {**FACTORY, **{address: 0 for address, _ in FACTORY_DIFFS}}


def _run(argv, replies=(), *, exhausted=None, inputs=()):
    """跑一条命令，返回 (退出码, 打印的文字, 替身串口)。"""
    port = ScriptedPort(list(replies), exhausted=exhausted)
    closed = []
    printed = []
    answers = iter(inputs)

    def connect(args):
        link = LinkSettings(args.port or 'COM9', baudrate=args.baud, address=args.address, parity=args.parity,
                            stopbits=args.stopbits)
        return ModbusClient(port, link), lambda: closed.append(True), dict(resolution='test')

    code = cli.main(argv, connect=connect, out=printed.append, input_fn=lambda prompt: next(answers))
    if any(command in argv for command in cli.NEEDS_DEVICE):
        assert closed == [True], '用完串口要关'
    return code, '\n'.join(printed), port


def test_every_command_has_a_parser_and_a_handler():
    parser = cli.build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(choices) == set(cli.COMMANDS)
    assert cli.NEEDS_DEVICE < set(cli.COMMANDS)


@pytest.mark.parametrize('argv', [['set', '--port', 'COM9', 'filter_type'], ['set', '--port', 'COM9', 'x=abc'],
                                  ['scan', '--addresses', '0'], ['scan', '--bauds', '500000'],
                                  ['set-link', '--port', 'COM9'], ['live', '--baud', '500000'], []])
def test_bad_arguments_exit_with_usage(argv, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(argv, connect=None, out=lambda line: None)
    assert info.value.code == 2


def test_ports_lists_ftdi_and_latency(monkeypatch):
    monkeypatch.setattr(cli, 'list_ports', lambda: [PortInfo('COM9', 'USB Serial Port', FTDI_VID, 0x6001, 'TESTSN01')])
    monkeypatch.setattr(cli, 'ftdi_latency_ms', lambda: {'COM9': 16})
    code, text, _ = _run(['ports'])
    assert code == 0 and 'TESTSN01' in text and '延迟 16 ms（要改成 1 ms）' in text


def test_info_prints_translated_settings_and_warnings():
    code, text, port = _run(['info', '--port', 'COM9'], backup_replies(FACTORY))
    assert code == 0 and '9600 bps' in text and '8N2' in text and 'V3.62' in text and '注意：' in text
    assert all(frame[1] == 0x03 for frame in port.written)


def test_backup_writes_a_file_under_the_data_root(tmp_path):
    code, text, _ = _run(['backup', '--port', 'COM9', '--data-root', str(tmp_path)], backup_replies(FACTORY))
    files = list((tmp_path / 'settings').glob('backup-*.json'))
    assert code == 0 and len(files) == 1 and read_json(files[0])['values']['filter_type'] == 9


def test_set_without_apply_writes_nothing(tmp_path):
    code, text, port = _run(['set', '--port', 'COM9', '--data-root', str(tmp_path), 'filter_type=0'],
                            backup_replies(FACTORY))
    assert code == 0 and '滑动平均滤波 + 一阶滤波 → 不使用' in text and '加 --apply' in text
    assert all(frame[1] == 0x03 for frame in port.written) and not (tmp_path / 'settings').exists()


def test_set_with_apply_writes_and_reports(tmp_path):
    replies = backup_replies(FACTORY) + [write_echo(34, [0]), read_reply([0])] + backup_replies({**FACTORY, 34: 0})
    code, text, port = _run(['set', '--port', 'COM9', '--data-root', str(tmp_path), 'filter_type=0', '--apply'], replies)
    assert code == 0 and '已写 34：读回 0' in text and '完成' in text
    assert [frame for frame in port.written if frame[1] == 0x10] == [build_write(1, 34, [0])]


def test_set_refuses_a_forbidden_register(tmp_path):
    code, text, port = _run(['set', '--port', 'COM9', '--data-root', str(tmp_path), 'tare=0', '--apply'],
                            backup_replies(FACTORY))
    assert code == 1 and '白名单' in text and all(frame[1] == 0x03 for frame in port.written)


def test_preset_test_dry_run_lists_seven_changes(tmp_path):
    code, text, _ = _run(['preset-test', '--port', 'COM9', '--data-root', str(tmp_path)], backup_replies(FACTORY))
    assert code == 0 and text.count(' → ') == 7


def test_restore_reads_a_backup_file(tmp_path):
    record = tmp_path / 'backup.json'
    write_json(record, dict(values=dict(filter_type=9, filter_strength=5)))
    code, text, _ = _run(['restore', '--port', 'COM9', '--data-root', str(tmp_path), str(record)],
                         backup_replies(PRESET))
    assert code == 0 and '不使用 → 滑动平均滤波 + 一阶滤波' in text


def test_set_link_dry_run_tells_the_next_connection(tmp_path):
    code, text, port = _run(['set-link', '--port', 'COM9', '--data-root', str(tmp_path), '--to-baud', '921600'],
                            backup_replies(FACTORY))
    assert code == 0 and '--baud 921600' in text and '加 --apply' in text
    assert all(frame[1] == 0x03 for frame in port.written)


def test_raw_console_reads_and_quits():
    code, text, port = _run(['raw', '--port', 'COM9'], [read_reply([0x0802])], inputs=['r 8', 'quit'])
    assert code == 0 and '模块状态' in text


def test_live_without_plot_captures_to_a_run_dir(tmp_path):
    replies = backup_replies(PRESET) + [block_reply(100 + i, i) for i in range(20)]
    code, text, _ = _run(['live', '--port', 'COM9', '--data-root', str(tmp_path), '--no-plot', '--label', '空载'],
                         replies, exhausted=b'')
    runs = list((tmp_path / 'live').iterdir())
    assert len(runs) == 1 and runs[0].name.endswith('-空载')
    summary = read_json(runs[0] / 'summary.json')
    assert summary['stats']['new_values'] == 20 and summary['stop_reason'] == 'consecutive_errors'
    assert code == 1  # 停下的原因是连续失败，不算正常结束
    metadata = read_json(runs[0] / 'metadata.json')
    assert metadata['settings']['values']['filter_type'] == 0 and metadata['warnings'] == []
    assert metadata['config']['decimals'] == 2 and metadata['config']['unit'] == 'N'


def test_live_refuses_to_start_when_the_gui_cannot_open(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'gui_problem', lambda: '图形库用不了：测试')
    code, text, port = _run(['live', '--port', 'COM9', '--data-root', str(tmp_path)])
    assert code == 1 and '图形库用不了' in text and port.written == [] and not (tmp_path / 'live').exists()


def test_stats_recomputes_from_the_csv(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    writer = CsvWriter(run / 'samples.csv', SAMPLE_FIELDS)
    for index in range(1, 401):  # 每 1 ms 一个新值，噪声 ±0.001 N
        t = index * 1_000_000
        writer.writerow(SampleRow(index, t, t + 100_000, measured=100, ad_code=index,
                                  force_n=1.0 + (0.001 if index % 2 else -0.001)).as_row())
    assert writer.close()['drained']
    write_json(run / 'metadata.json', dict(warnings=['自动零位跟踪没关']))
    code, text, _ = _run(['stats', str(run)])
    assert code == 0 and '新值速率' in text and '最好' in text and 'D1' in text and '达标' in text
    assert '最小步长 0.002000 N' in text and '自动零位跟踪没关' in text


def test_device_errors_are_reported_not_raised(tmp_path):
    code, text, _ = _run(['info', '--port', 'COM9'], [b''] * 40)
    assert code == 1 and '读不到' in text
