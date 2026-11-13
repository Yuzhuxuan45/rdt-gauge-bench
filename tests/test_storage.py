"""storage.py：只写临时目录，不碰任何设备。"""
from datetime import datetime
import csv
import json
import math

import pytest

from rdt_gauge.contracts import SAMPLE_FIELDS, SampleRow
from rdt_gauge.storage import (CsvWriter, default_data_root, new_run_dir, read_json, read_samples, safe_label,
                               write_json)


def test_write_json_replaces_atomically_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / 'backup.json'
    write_json(path, {'filter_type': 9})
    write_json(path, {'filter_type': 0, '说明': '中文'})
    assert read_json(path) == {'filter_type': 0, '说明': '中文'}
    assert '中文' in path.read_text(encoding='utf-8')  # ensure_ascii=False：备份文件里中文直接可读
    assert not (tmp_path / 'backup.json.tmp').exists()


def test_write_json_refuses_nan_and_keeps_the_old_file(tmp_path):
    path = tmp_path / 'summary.json'
    write_json(path, {'std': 0.003})
    with pytest.raises(ValueError):
        write_json(path, {'std': math.nan})
    assert read_json(path) == {'std': 0.003}


def test_run_dirs_never_overwrite(tmp_path):
    now = datetime(2026, 11, 13, 9, 30, 0)
    first = new_run_dir('live', '空载 台1', tmp_path, now=now)
    second = new_run_dir('live', '空载 台1', tmp_path, now=now)
    assert first.name == '20261113-093000-空载-台1'
    assert second.name == '20261113-093000-空载-台1-2'
    assert first.parent == second.parent == tmp_path / 'live'


@pytest.mark.parametrize('label, expected', [('a/b:c', 'a-b-c'), ('  ', 'run'), ('砝码 10N', '砝码-10N'),
                                             ('..', 'run'), ('x' * 60, 'x' * 40)])
def test_safe_label(label, expected):
    assert safe_label(label) == expected


def test_default_data_root_is_outside_the_repo(monkeypatch, tmp_path):
    monkeypatch.delenv('RDT_BENCH_DATA', raising=False)
    assert default_data_root().name == 'RDT_TestData'
    monkeypatch.setenv('RDT_BENCH_DATA', str(tmp_path))
    assert default_data_root() == tmp_path


def test_csv_writer_keeps_every_row_in_order(tmp_path):
    path = tmp_path / 'samples.csv'
    writer = CsvWriter(path, SAMPLE_FIELDS)
    for index in range(1, 2001):
        writer.writerow(SampleRow(index, index * 10, index * 10 + 5, measured=index, force_n=index / 100).as_row())
    result = writer.close()
    assert result['drained'] and result['written'] == 2000 and result['error'] is None
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == SAMPLE_FIELDS
    assert [int(row['sample_index']) for row in rows] == list(range(1, 2001))
    assert path.read_bytes().startswith(b'\xef\xbb\xbf')  # utf-8-sig：Excel 打开中文不乱码


def test_csv_writer_never_overwrites_an_existing_file(tmp_path):
    path = tmp_path / 'samples.csv'
    path.write_text('old data\n', encoding='utf-8')
    writer = CsvWriter(path, SAMPLE_FIELDS)
    writer.ready.wait(5)
    result = writer.close()
    assert result['error'] and 'FileExistsError' in result['error']
    assert path.read_text(encoding='utf-8') == 'old data\n'
    with pytest.raises(RuntimeError, match='已失败'):
        writer.writerow(SampleRow(1, 1, 2).as_row())


def test_csv_writer_reports_a_full_queue_instead_of_dropping_silently(tmp_path, monkeypatch):
    import rdt_gauge.storage as storage
    gate = storage.threading.Event()
    real_open = storage.Path.open

    def slow_open(self, *args, **kwargs):  # 写线程卡在打开文件之前，模拟磁盘跟不上
        gate.wait(5)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(storage.Path, 'open', slow_open)
    writer = CsvWriter(tmp_path / 'samples.csv', ('a',), capacity=2)
    try:
        writer.writerow({'a': 1})
        writer.writerow({'a': 2})
        with pytest.raises(RuntimeError, match='队列已满'):
            writer.writerow({'a': 3})  # 第三行放不进去：报错，不悄悄丢
        with pytest.raises(RuntimeError, match='已失败'):
            writer.writerow({'a': 4})  # 丢过一行以后一直报错
    finally:
        gate.set()  # 不管上面成不成功都放行写线程，测试失败时也不会卡住
    result = writer.close(timeout=5)
    assert result['closed'], '写线程 5 s 内没收尾'
    assert result['written'] == 2 and not result['drained'] and '队列已满' in result['error']


def test_csv_writer_reports_a_file_it_cannot_open(tmp_path):
    writer = CsvWriter(tmp_path / 'missing-dir' / 'samples.csv', ('a',))
    writer.ready.wait(5)
    assert writer.error and 'FileNotFoundError' in writer.error
    with pytest.raises(RuntimeError):
        writer.writerow({'a': 1})
    assert not writer.close()['drained']


def test_csv_writer_refuses_rows_after_close(tmp_path):
    writer = CsvWriter(tmp_path / 'samples.csv', ('a',))
    writer.writerow({'a': 1})
    assert writer.close()['written'] == 1
    with pytest.raises(RuntimeError, match='关闭'):
        writer.writerow({'a': 2})


def test_csv_writer_rejects_unknown_columns(tmp_path):
    writer = CsvWriter(tmp_path / 'samples.csv', ('a',))
    writer.writerow({'a': 1, 'b': 2})
    result = writer.close()
    assert result['error'] and 'ValueError' in result['error']


def test_read_samples_round_trips_what_the_writer_wrote(tmp_path):
    rows = [SampleRow(1, 10, 20, measured=132, ad_code=-5, force_n=1.32),
            SampleRow(2, 30, 40, raw_hex='0103', error='FrameError: CRC 不对'),
            SampleRow(3, 50, 60, measured=-1, ad_code=None, force_n=None)]
    writer = CsvWriter(tmp_path / 'samples.csv', SAMPLE_FIELDS)
    for row in rows:
        writer.writerow(row.as_row())
    assert writer.close()['drained']
    assert read_samples(tmp_path / 'samples.csv') == rows


def test_read_samples_refuses_another_csv(tmp_path):
    (tmp_path / 'other.csv').write_text('a,b\n1,2\n', encoding='utf-8')
    with pytest.raises(ValueError, match='表头'):
        read_samples(tmp_path / 'other.csv')


def test_json_files_are_plain_utf8(tmp_path):
    path = write_json(tmp_path / 'x.json', {'v': [1, 2]})
    assert json.loads(path.read_bytes().decode('utf-8')) == {'v': [1, 2]}
