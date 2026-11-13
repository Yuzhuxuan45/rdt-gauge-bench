"""实时波形（pyqtgraph）：上图最近一段的力，下图新值间隔；标题行实时显示读取速率、新值速率、错误、标准差。

界面线程只用 QTimer 每 0.1 s 从 CaptureSession.read_preview 拿一份拷贝来画，**从不碰串口**；串口只归采集线程。
图上只显示最近 window_s 秒，完整数据以 samples.csv 为准。写法参考：
- r11_upper_computer/wuji_r11/acquisition/capture.py 的 read_preview(after)：只拿新数据，知道中间有没有丢；
- r11_upper_computer/wuji_r11/gui/live_plot.py：pyqtgraph 画实时曲线，只留最近一段；
- R11 的 Qt 规则（PR 模板）：界面线程不做设备 I/O。

PyQt6 和 pyqtgraph 只在 run_live_window 里导入：没装图形库也能 import 这个模块、跑离线测试和其他命令。
数据整理（PlotBuffer）不依赖 Qt，单独测试。
"""
from __future__ import annotations

from collections import deque

from .capture import compute_stats


class PlotBuffer:
    """攒最近 window_s 秒的点，给图用。时间以第一个样本为 0，单位 s；样本时刻取请求和回复的中点。"""

    def __init__(self, window_s: float = 10.0):
        if not window_s > 0:
            raise ValueError('显示窗口要大于 0 s')
        self.window_s = window_s
        self.origin_ns: float | None = None
        self.rows: deque = deque()
        self.after = 0  # 已经拿过的最大序号
        self.gaps = 0  # 预览报告"中间有丢"的次数（只影响画图，CSV 完整）
        self._last_ad = None
        self._last_new_t: float | None = None
        self.new_points: deque = deque()  # (t, 距上一个新值的间隔 ms)

    @staticmethod
    def stamp_ns(row) -> float:
        return (row.request_ns + row.response_ns) / 2

    def extend(self, preview: dict) -> None:
        if preview['gap']:
            self.gaps += 1
        for row in preview['rows']:
            self.after = max(self.after, row.sample_index)
            if self.origin_ns is None:
                self.origin_ns = self.stamp_ns(row)
            self.rows.append(row)
            if row.ok and row.ad_code is not None and row.ad_code != self._last_ad:
                t = self.t(row)
                if self._last_new_t is not None:
                    self.new_points.append((t, (t - self._last_new_t) * 1000))
                self._last_new_t = t
                self._last_ad = row.ad_code
        self._trim()

    def t(self, row) -> float:
        assert self.origin_ns is not None
        return (self.stamp_ns(row) - self.origin_ns) / 1e9

    def _trim(self) -> None:
        if not self.rows:
            return
        newest = self.t(self.rows[-1])
        while self.rows and self.t(self.rows[0]) < newest - self.window_s:
            self.rows.popleft()
        while self.new_points and self.new_points[0][0] < newest - self.window_s:
            self.new_points.popleft()

    def force_series(self) -> tuple[list[float], list[float]]:
        points = [(self.t(row), row.force_n if row.force_n is not None else float(row.measured))
                  for row in self.rows if row.ok and (row.force_n is not None or row.measured is not None)]
        return [p[0] for p in points], [p[1] for p in points]

    def interval_series(self) -> tuple[list[float], list[float]]:
        return [p[0] for p in self.new_points], [p[1] for p in self.new_points]

    def recent_stats(self, seconds: float = 1.0):
        """最近 seconds 秒的统计（和整段采集同一套算法）。"""
        if not self.rows:
            return compute_stats([])
        newest = self.t(self.rows[-1])
        return compute_stats([row for row in self.rows if self.t(row) >= newest - seconds])


def status_line(session, buffer: PlotBuffer, unit_label: str) -> str:
    """标题上那一行：最近 1 s 的读取速率、新值速率、标准差，总读数、错误、预览丢点、最新读数。"""
    recent = buffer.recent_stats(1.0)
    progress = session.progress()
    parts = [f'读 {recent.read_rate_hz:.0f}/s' if recent.read_rate_hz else '读 -/s']
    if recent.new_value_rate_hz is not None:
        parts.append(f'新值 {recent.new_value_rate_hz:.0f}/s')
    if recent.force_std_n is not None:
        parts.append(f'σ {recent.force_std_n:.4f} {unit_label}')
    latest = buffer.rows[-1] if buffer.rows else None
    if latest is not None and latest.ok:
        value = latest.force_n if latest.force_n is not None else latest.measured
        parts.append(f'当前 {value:.4f} {unit_label}' if latest.force_n is not None else f'当前 {value}')
    parts.append(f'共 {progress["reads"]} 次，错 {progress["errors"]}')
    if buffer.gaps:
        parts.append(f'预览丢点 {buffer.gaps} 次（CSV 完整）')
    if not progress['running']:
        parts.append(f'已停：{progress["stop_reason"]}')
    return '　'.join(parts)


GUI_MISSING = ('图形库用不了：{error}。没装就 pip install -e ".[gui]"；如果是 Windows "智能应用控制"拦截了 PyQt6'
               '（WinError 4551），那是系统安全设置，要你或 IT 决定。现在可以用 live --no-plot 只采集不画图。')


def gui_problem() -> str | None:
    """图形库能不能用：能用返回 None，不能用返回给人看的原因（cli 在开始采集**之前**先查，免得采到一半才报错）。"""
    try:
        import pyqtgraph  # noqa: F401
        from PyQt6 import QtCore  # noqa: F401
    except ImportError as exc:
        return GUI_MISSING.format(error=exc)
    return None


def build_live_window(session, *, title: str, unit_label: str = 'N', window_s: float = 10.0,
                      refresh_ms: int = 100) -> dict:
    """建窗口和定时器，不进事件循环（测试和 run_live_window 都用它）。返回 app、window、refresh、timer、buffer。"""
    try:
        import pyqtgraph as pg
        from PyQt6 import QtCore, QtWidgets
    except ImportError as exc:
        raise RuntimeError(GUI_MISSING.format(error=exc)) from exc

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pg.setConfigOptions(antialias=False, background='w', foreground='k')
    window = pg.GraphicsLayoutWidget(title=title)
    window.resize(1100, 700)
    header = window.addLabel('', row=0, col=0)
    force_plot = window.addPlot(row=1, col=0)
    force_plot.setLabel('left', f'力（{unit_label}）')
    force_plot.showGrid(x=True, y=True, alpha=0.3)
    force_curve = force_plot.plot(pen=pg.mkPen('#1F5FA8', width=1))
    interval_plot = window.addPlot(row=2, col=0)
    interval_plot.setLabel('left', '新值间隔（ms）')
    interval_plot.setLabel('bottom', '时间（s）')
    interval_plot.showGrid(x=True, y=True, alpha=0.3)
    interval_plot.setXLink(force_plot)
    interval_curve = interval_plot.plot(pen=None, symbol='o', symbolSize=3, symbolBrush='#2E7D4F', symbolPen=None)
    window.ci.layout.setRowStretchFactor(1, 3)
    window.ci.layout.setRowStretchFactor(2, 1)
    buffer = PlotBuffer(window_s)

    def refresh() -> None:
        buffer.extend(session.read_preview(buffer.after))
        force_curve.setData(*buffer.force_series())
        interval_curve.setData(*buffer.interval_series())
        header.setText(status_line(session, buffer, unit_label))
        if not session.running:
            timer.stop()

    timer = QtCore.QTimer()
    timer.timeout.connect(refresh)
    timer.setInterval(refresh_ms)

    def on_close(event) -> None:
        session.request_stop()
        timer.stop()
        event.accept()

    window.closeEvent = on_close
    return dict(app=app, window=window, refresh=refresh, timer=timer, buffer=buffer, header=header,
                force_curve=force_curve, interval_curve=interval_curve)


def run_live_window(session, **kwargs) -> None:
    """开窗口画图，直到窗口关掉（采集自己停下后窗口保留，方便看最后的画面）。关窗口会请采集停下。"""
    parts = build_live_window(session, **kwargs)
    parts['timer'].start()
    parts['window'].show()
    parts['app'].exec()
    session.request_stop()
