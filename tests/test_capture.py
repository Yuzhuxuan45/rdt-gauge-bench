"""capture.py：读法、失败计数、节拍、统计、采集会话。回复用 ScriptedPort 按顺序给，不碰设备。"""
import csv
import statistics
import threading
import time

import pytest

from rdt_gauge.capture import (CaptureConfig, CaptureSession, SampleReader, _percentile, compute_stats, frame_gap_s,
                               to_newtons)
from rdt_gauge.contracts import READ_GROSS, SAMPLE_FIELDS, LinkSettings, SampleRow
from rdt_gauge.protocol import ModbusClient, build_read
from rdt_gauge.storage import read_json
from frames import block_reply, read_reply
from scripted_port import ScriptedPort

FAST = LinkSettings('COM9', baudrate=921600)  # 帧间隔 1.75 ms，测试跑得快


def _client(replies, *, exhausted=None, link=FAST):
    return ModbusClient(ScriptedPort(replies, exhausted=exhausted), link)


def _rows(client, config, stop=None):
    reader = SampleReader(client, config, stop or threading.Event())
    return list(reader.rows()), reader


# ---------------- 换算和参数 ----------------

def test_to_newtons_follows_decimals_and_unit():
    assert to_newtons(132, 2, 'N') == pytest.approx(1.32)
    assert to_newtons(132, 2, 'g') == pytest.approx(1.32 * 0.00980665)
    assert to_newtons(-5, 0, 'kg') == pytest.approx(-5 * 9.80665)
    assert to_newtons(132, 2, 'none') is None


def test_frame_gap_follows_the_baudrate():
    assert frame_gap_s(LinkSettings('COM9')) == pytest.approx(3.5 * 11 / 9600)  # 出厂 9600 8N2：约 4 ms
    assert frame_gap_s(FAST) == pytest.approx(0.00175)


@pytest.mark.parametrize('kwargs', [dict(read_mode='x'), dict(decimals=8), dict(period_s=0), dict(seconds=0),
                                    dict(max_consecutive_errors=0), dict(preview_capacity=0)])
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        CaptureConfig(**kwargs)


# ---------------- SampleReader ----------------

def test_block_reads_measured_and_ad_code_and_converts_to_newtons():
    client = _client([block_reply(132, 1000), block_reply(-20, 1001)], exhausted=b'')
    rows, reader = _rows(client, CaptureConfig(decimals=2, unit='N'))
    good = [row for row in rows if row.ok]
    assert [(row.measured, row.ad_code) for row in good] == [(132, 1000), (-20, 1001)]
    assert good[0].force_n == pytest.approx(1.32) and good[1].force_n == pytest.approx(-0.2)
    assert all(frame == build_read(1, 30, 16) for frame in client.port.written)
    assert reader.stop_reason == 'consecutive_errors'


def test_gross_mode_reads_80_and_has_no_ad_code():
    client = _client([read_reply([0, 84])], exhausted=b'')
    rows, _ = _rows(client, CaptureConfig(read_mode=READ_GROSS, decimals=1))
    assert rows[0].measured == 84 and rows[0].ad_code is None and rows[0].force_n == pytest.approx(8.4)
    assert client.port.written[0] == build_read(1, 80, 2)


def test_stops_after_three_consecutive_failures_and_clears_the_buffer_each_time():
    client = _client([block_reply(1, 1)] * 5, exhausted=b'')
    rows, reader = _rows(client, CaptureConfig())
    assert len(rows) == 8 and [row.ok for row in rows] == [True] * 5 + [False] * 3
    assert reader.stop_reason == 'consecutive_errors' and client.port.resets == 3
    assert 'LinkTimeout' in rows[-1].error


def test_isolated_failures_do_not_stop_the_capture():
    good = block_reply(1, 1)
    bad = good[:-1] + bytes([good[-1] ^ 0xFF])  # CRC 坏了
    client = _client([good, b'', good, bad, b'', good, b'', b'', b''])
    rows, reader = _rows(client, CaptureConfig())
    assert [row.ok for row in rows] == [True, False, True, False, False, True, False, False, False]
    assert 'FrameError' in rows[3].error and rows[3].raw_hex == bad.hex()  # 失败行留着原始字节
    assert reader.stop_reason == 'consecutive_errors'


def test_a_preset_stop_reads_nothing():
    stop = threading.Event()
    stop.set()
    rows, reader = _rows(_client([]), CaptureConfig(), stop)
    assert rows == [] and reader.stop_reason == 'user_stop'


def test_duration_stops_the_reader():
    client = _client([], exhausted=block_reply(1, 1))
    started = time.perf_counter()
    rows, reader = _rows(client, CaptureConfig(seconds=0.1))
    assert reader.stop_reason == 'duration' and rows
    assert time.perf_counter() - started < 1.0


def test_fixed_period_paces_the_requests():
    client = _client([], exhausted=block_reply(1, 1))
    rows, _ = _rows(client, CaptureConfig(period_s=0.01, seconds=0.3))
    spacing = [(b.request_ns - a.request_ns) / 1e6 for a, b in zip(rows, rows[1:])]
    assert 8 <= statistics.median(spacing) <= 30, spacing


def test_back_to_back_reads_keep_the_frame_gap():
    client = _client([], exhausted=block_reply(1, 1), link=LinkSettings('COM9', baudrate=9600))
    rows, _ = _rows(client, CaptureConfig(seconds=0.1))
    gaps = [(b.request_ns - a.response_ns) / 1e6 for a, b in zip(rows, rows[1:])]
    assert min(gaps) >= 3.9  # 9600 8N2 下 3.5 个字符约 4.0 ms


# ---------------- 统计 ----------------

def _row(index, ad, force=1.0, ok=True):
    t = index * 1_000_000  # 每 1 ms 一次，往返 0.2 ms
    return SampleRow(index, t, t + 200_000, measured=1, ad_code=ad, force_n=force) if ok else \
        SampleRow(index, t, t + 200_000, error='LinkTimeout: x')


def test_new_values_follow_ad_code_changes():
    rows = [_row(1, 10), _row(2, 10), _row(3, 11), _row(4, 12), _row(5, 12), _row(6, 13)]
    stats = compute_stats(rows)
    assert stats.new_values == 4  # 10、11、12、13：时刻（请求和回复的中点）1.1、3.1、4.1、6.1 ms
    assert stats.new_interval_ms_median == pytest.approx(2.0)  # 间隔 2、1、2 ms
    assert stats.new_interval_ms_max == pytest.approx(2.0) and stats.new_interval_ms_p95 == pytest.approx(2.0)
    assert stats.read_rate_hz == pytest.approx(6 / 0.0052)


def test_force_statistics_and_error_counts():
    forces = [1.0, 1.2, 0.8, 1.1, 0.9]
    rows = [_row(i + 1, i, f) for i, f in enumerate(forces)] + [_row(6, 0, ok=False), _row(7, 0, ok=False),
                                                                  _row(8, 9, 1.0)]
    stats = compute_stats(rows)
    assert stats.force_mean_n == pytest.approx(statistics.fmean(forces + [1.0]))
    assert stats.force_std_n == pytest.approx(statistics.pstdev(forces + [1.0]))
    assert (stats.force_min_n, stats.force_max_n) == (0.8, 1.2)
    assert (stats.reads, stats.ok_reads, stats.errors, stats.max_consecutive_errors) == (8, 6, 2, 2)


def test_gross_rows_have_no_new_value_statistics():
    rows = [SampleRow(i, i * 10, i * 10 + 5, measured=1, force_n=1.0) for i in range(1, 5)]
    stats = compute_stats(rows)
    assert stats.new_values is None and stats.new_value_rate_hz is None and stats.new_interval_ms_median is None


def test_empty_statistics_are_none_not_nan():
    stats = compute_stats([])
    assert stats.reads == 0 and stats.read_rate_hz is None and stats.force_std_n is None


def test_min_step_finds_the_smallest_jump_between_distinct_readings():
    from rdt_gauge.capture import min_step
    assert min_step([1.0, 1.002, 1.002, 1.01, None]) == pytest.approx(0.002)
    assert min_step([1.0, 1.0]) is None and min_step([]) is None


def test_percentile_interpolates():
    assert _percentile([1, 2, 3, 4], .5) == 2.5 and _percentile([5], .99) == 5 and _percentile([], .5) is None


# ---------------- CaptureSession ----------------

@pytest.fixture
def started():
    """开采集会话用它：测试不管成不成功，结束时都把读取线程停掉（否则断言失败会让 pytest 一直等这个线程）。"""
    sessions = []

    def start(session):
        sessions.append(session)
        return session.start()

    yield start
    for session in sessions:
        if session.running:
            session.finish(timeout=5)


def test_session_writes_every_row_metadata_and_summary(tmp_path, started):
    client = _client([block_reply(100 + i, i) for i in range(50)], exhausted=b'')
    session = CaptureSession(client, CaptureConfig(decimals=2), tmp_path / 'run', metadata=dict(label='空载', warnings=[]))
    started(session)
    assert session.wait(5), '采集 5 s 内没自己停下'
    summary = session.finish()
    assert summary['stop_reason'] == 'consecutive_errors' and summary['complete']
    assert summary['stats']['reads'] == 53 and summary['stats']['errors'] == 3 and summary['stats']['new_values'] == 50
    with (tmp_path / 'run' / 'samples.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == SAMPLE_FIELDS and len(rows) == 53
    assert rows[0]['measured'] == '100' and rows[-1]['error'].startswith('LinkTimeout')
    metadata = read_json(tmp_path / 'run' / 'metadata.json')
    assert metadata['label'] == '空载' and metadata['link']['baudrate'] == 921600
    assert metadata['time_basis'] == 'time.perf_counter_ns'
    assert read_json(tmp_path / 'run' / 'summary.json') == summary


def test_session_stops_on_request(tmp_path, started):
    session = started(CaptureSession(_client([], exhausted=block_reply(1, 1)), CaptureConfig(), tmp_path / 'run'))
    time.sleep(0.2)
    summary = session.finish()
    assert summary['stop_reason'] == 'user_stop' and summary['reader_closed'] and summary['stats']['reads'] > 0


def test_session_stops_when_rows_can_no_longer_be_saved(tmp_path, started):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'samples.csv').write_text('old', encoding='utf-8')  # 已有文件：写线程打不开（不覆盖）
    session = started(CaptureSession(_client([], exhausted=block_reply(1, 1)), CaptureConfig(), run))
    assert session.wait(5)
    summary = session.finish()
    assert summary['stop_reason'] == 'writer_error' and not summary['complete']
    assert (run / 'samples.csv').read_text(encoding='utf-8') == 'old'


def test_preview_is_bounded_and_reports_gaps(tmp_path, started):
    client = _client([block_reply(i, i) for i in range(30)], exhausted=b'')
    session = started(CaptureSession(client, CaptureConfig(preview_capacity=10), tmp_path / 'run'))
    assert session.wait(5)
    preview = session.read_preview(0)
    assert [row.sample_index for row in preview['rows']] == list(range(24, 34))
    assert preview['gap'] and preview['dropped'] == 23 and preview['newest'] == 33
    assert session.read_preview(33) == dict(rows=[], newest=33, gap=False, dropped=23)
    tail = session.read_preview(30)
    assert [row.sample_index for row in tail['rows']] == [31, 32, 33] and not tail['gap']
    session.finish()


def test_session_starts_only_once(tmp_path, started):
    session = started(CaptureSession(_client([], exhausted=b''), CaptureConfig(), tmp_path / 'run'))
    with pytest.raises(RuntimeError):
        session.start()
    session.finish()
