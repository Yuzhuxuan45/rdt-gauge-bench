"""live_plot.py：数据整理（PlotBuffer、status_line）不依赖 Qt，直接测；窗口本身只在图形库能用的电脑上测。"""
import os

import pytest

from rdt_gauge.contracts import SampleRow
from rdt_gauge.live_plot import GUI_MISSING, PlotBuffer, gui_problem, status_line


def _row(index, t_ms, ad=None, force=1.0, ok=True):
    t = int(t_ms * 1e6)
    if not ok:
        return SampleRow(index, t, t + 100_000, error='LinkTimeout: x')
    return SampleRow(index, t, t + 100_000, measured=int(force * 100), ad_code=ad, force_n=force)


def _preview(rows, gap=False):
    return dict(rows=rows, newest=rows[-1].sample_index if rows else 0, gap=gap, dropped=0)


def test_buffer_keeps_only_the_window_and_starts_time_at_zero():
    buffer = PlotBuffer(window_s=1.0)
    buffer.extend(_preview([_row(i + 1, i * 100, ad=i, force=i / 10) for i in range(31)]))  # 0–3 s，每 0.1 s 一个
    t, force = buffer.force_series()
    assert t[0] == pytest.approx(2.0) and t[-1] == pytest.approx(3.0) and len(t) == 11
    assert force[-1] == pytest.approx(3.0)
    assert buffer.after == 31


def test_buffer_tracks_new_value_intervals_and_skips_failed_rows():
    buffer = PlotBuffer(10)
    rows = [_row(1, 0, ad=5), _row(2, 1, ad=5), _row(3, 2, ok=False), _row(4, 3, ad=6), _row(5, 4, ad=7)]
    buffer.extend(_preview(rows))
    t, intervals = buffer.interval_series()
    assert intervals == pytest.approx([3.0, 1.0])  # 5→6 隔 3 ms，6→7 隔 1 ms
    assert len(buffer.force_series()[0]) == 4  # 失败行不画


def test_buffer_counts_preview_gaps_and_continues_after():
    buffer = PlotBuffer(10)
    buffer.extend(_preview([_row(1, 0, ad=1)]))
    buffer.extend(_preview([_row(5, 4, ad=2)], gap=True))
    assert buffer.gaps == 1 and buffer.after == 5


def test_buffer_without_newtons_plots_raw_counts():
    buffer = PlotBuffer(10)
    buffer.extend(_preview([SampleRow(1, 0, 10, measured=132, ad_code=1, force_n=None)]))
    assert buffer.force_series()[1] == [132.0]


def test_status_line_shows_rates_errors_and_stop_reason():
    class Session:
        def progress(self):
            return dict(reads=10, errors=1, running=False, stop_reason='duration')

    buffer = PlotBuffer(10)
    buffer.extend(_preview([_row(i + 1, i, ad=i, force=1 + (i % 2) / 100) for i in range(10)]))
    line = status_line(Session(), buffer, 'N')
    assert '读 ' in line and '新值 ' in line and 'σ ' in line and '共 10 次，错 1' in line and '已停：duration' in line


def test_window_must_be_positive():
    with pytest.raises(ValueError):
        PlotBuffer(0)


def test_gui_problem_explains_how_to_continue():
    problem = gui_problem()
    assert problem is None or ('--no-plot' in problem and 'pip install' in problem)
    assert '{error}' in GUI_MISSING


@pytest.mark.skipif(gui_problem() is not None, reason='这台电脑图形库用不了（没装，或被 Windows 智能应用控制拦截）')
def test_window_builds_and_refreshes_offscreen(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from rdt_gauge.live_plot import build_live_window

    class Session:
        running = True

        def read_preview(self, after):
            return _preview([_row(after + 1, after, ad=after)])

        def progress(self):
            return dict(reads=1, errors=0, running=True, stop_reason=None)

        def request_stop(self):
            self.running = False

    session = Session()
    parts = build_live_window(session, title='test')
    parts['refresh']()
    parts['refresh']()
    assert parts['buffer'].after == 2
    assert '共 1 次' in parts['header'].text
    parts['window'].close()
    assert not session.running  # 关窗口会请采集停下
    os.environ.pop('QT_QPA_PLATFORM', None)
