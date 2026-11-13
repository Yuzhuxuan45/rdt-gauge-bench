"""采集：一个读取线程独占串口，每读一次记一行；写盘、预览、统计都不让读取线程等。

不决定用什么设置采（那是 cli 读设置后传进来的），也不改任何寄存器。写法参考（登记在 docs/SOURCES.json）：
- SampleReader.rows：r11_upper_computer/wuji_r11/acquisition/force.py 的 ForceSource.records ——
  每行记序号、发请求时刻、收到回复时刻（perf_counter_ns）；一次读失败照常记一行、接着读，连续 3 次失败才停；
  两种节拍：一读完马上读下一个，或者按固定时间格子读（读晚了就等下一格，不连着补；_next_deadline、_wait_until）。
- to_newtons：同一文件的 newtons_from —— 按单位统一换算成 N。
- CaptureSession：r11_upper_computer/wuji_r11/acquisition/capture.py 的 CaptureSession ——
  只有一个读取线程拥有串口；写盘交给 CsvWriter；预览缓冲有上限，满了丢最旧的并计数，取锁失败就跳过这一条预览，
  **绝不让读取线程等**；read_preview(after) 让画图的一方只拿新数据并知道中间有没有丢；
  开始写 metadata.json，结束写 summary.json。
- block 读法（30–45，测量值和 AD 内码一起）、按 AD 内码判断新值：r11_upper_computer/tools/gauge_probe.py 的 capture。
- 统计的项（往返、间隔、新值间隔、均值、标准差）：r11_upper_computer/tools/cybsq_probe.py 的 summarize。

和 R11 不一样的地方：
- 读失败后清空接收缓冲：迟到的回复字节会和下一次回复错位，接连 CRC 错，凑满 3 次就误停（R11 force.py 没有这一步）。
- 帧间隔：Modbus RTU 规定帧之间至少 3.5 个字符时间，19200 以上固定 1.75 ms；R11 只在定时读里留 1.75 ms，
  这里两种节拍都留，按波特率算（出厂 9600 下是 4 ms）。
"""
from __future__ import annotations

from array import array
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import math
from pathlib import Path
import threading
import time

from .contracts import READ_BLOCK, READ_MODES, SAMPLE_FIELDS, BenchError, CaptureStats, SampleRow, decode_s32
from .protocol import ModbusClient, frame_time_s
from .registers import BLOCK_COUNT, BLOCK_START, GROSS_COUNT, GROSS_START
from .storage import CsvWriter, write_json

NEWTONS_PER_UNIT = {'N': 1.0, 'g': 0.00980665, 'kg': 9.80665, 't': 9806.65}  # 寄存器 104 的单位 → N
MAX_CONSECUTIVE_ERRORS = 3
MIN_FRAME_GAP_S = 0.00175


def to_newtons(measured: int, decimals: int, unit: str) -> float | None:
    """读出来的整数 → 牛顿。单位是 none（寄存器 104 = 0，没设单位）时换算不了，返回 None。"""
    factor = NEWTONS_PER_UNIT.get(unit)
    if factor is None:
        return None
    return measured / 10 ** decimals * factor


def frame_gap_s(link) -> float:
    """两帧之间至少要隔的时间：3.5 个字符时间，最少 1.75 ms。"""
    return max(MIN_FRAME_GAP_S, 3.5 * frame_time_s(1, link))


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    read_mode: str = READ_BLOCK
    decimals: int = 0  # 寄存器 8 的 bit2–0
    unit: str = 'N'  # 寄存器 104 翻译出来的单位
    period_s: float | None = None  # None：一读完马上读下一个；给了值：按这个周期读
    seconds: float | None = None  # None：一直读到 request_stop
    max_consecutive_errors: int = MAX_CONSECUTIVE_ERRORS
    preview_capacity: int = 20_000

    def __post_init__(self):
        if self.read_mode not in READ_MODES:
            raise ValueError(f'读法要是 {READ_MODES} 之一')
        if type(self.decimals) is not int or not 0 <= self.decimals <= 7:
            raise ValueError('小数位要是 0–7')
        if self.period_s is not None and not (math.isfinite(self.period_s) and 0 < self.period_s <= 10):
            raise ValueError('读取周期要在 0–10 s 之间，或者不填（读完马上读下一个）')
        if self.seconds is not None and not (math.isfinite(self.seconds) and self.seconds > 0):
            raise ValueError('采集时长要大于 0，或者不填（一直读到手动停）')
        if type(self.max_consecutive_errors) is not int or self.max_consecutive_errors < 1:
            raise ValueError('连续失败次数上限要是正整数')
        if type(self.preview_capacity) is not int or self.preview_capacity < 1:
            raise ValueError('预览缓冲容量要是正整数')


def _next_deadline(deadline: float, now: float, period: float) -> float:
    """时间格子上的下一格；读晚了就从现在起再等一个周期，不连着补读（照 R11 force._next_poll_deadline）。"""
    candidate = deadline + period
    return candidate if candidate > now else now + period


def _wait_until(deadline: float, stop: threading.Event) -> bool:
    """等到 deadline（perf_counter 秒）。先短睡，最后 0.2 ms 空转让出 CPU（照 R11 force._wait_until）。停了返回 True。"""
    while (remaining := deadline - time.perf_counter()) > 0:
        if stop.is_set():
            return True
        time.sleep(min(.005, remaining - .0001) if remaining > .0002 else 0)
    return stop.is_set()


class SampleReader:
    """一直读，每次产出一行 SampleRow。停下的原因记在 stop_reason：duration / user_stop / consecutive_errors。"""

    def __init__(self, client: ModbusClient, config: CaptureConfig, stop: threading.Event):
        self.client = client
        self.config = config
        self.stop = stop
        self.stop_reason: str | None = None

    def rows(self):
        config, client = self.config, self.client
        block = config.read_mode == READ_BLOCK
        start, count = (BLOCK_START, BLOCK_COUNT) if block else (GROSS_START, GROSS_COUNT)
        gap = frame_gap_s(client.link)
        index = failures = 0
        deadline = started = last_end = None
        while not self.stop.is_set():
            now = time.perf_counter()
            if started is not None and config.seconds is not None and now - started >= config.seconds:
                self.stop_reason = 'duration'
                return
            if config.period_s is None:
                deadline = now
            else:
                deadline = now if deadline is None else _next_deadline(deadline, now, config.period_s)
            if last_end is not None:
                deadline = max(deadline, last_end + gap)
            if _wait_until(deadline, self.stop):
                break
            if started is None:
                started = time.perf_counter()
            index += 1
            request_ns = time.perf_counter_ns()
            try:
                values = client.read(start, count, attempts=1)
                response_ns = time.perf_counter_ns()
                measured = decode_s32(values[0], values[1])
                ad_code = decode_s32(values[14], values[15]) if block else None
                row = SampleRow(index, request_ns, response_ns, measured, ad_code,
                                to_newtons(measured, config.decimals, config.unit))
                failures = 0
            except (BenchError, OSError) as exc:  # OSError 包括 pyserial 的 SerialException（拔线等）
                row = SampleRow(index, request_ns, time.perf_counter_ns(), raw_hex=client.last_frame.hex(),
                                error=f'{type(exc).__name__}: {exc}')
                failures += 1
                try:
                    client.port.reset_input_buffer()
                except OSError:
                    pass
            last_end = time.perf_counter()
            yield row
            if failures >= config.max_consecutive_errors:
                self.stop_reason = 'consecutive_errors'
                return
        self.stop_reason = self.stop_reason or 'user_stop'


def _percentile(sorted_values, q: float) -> float | None:
    """线性插值的分位数，q 在 0–1。"""
    if not sorted_values:
        return None
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


class StatsAccumulator:
    """边读边累计统计，内存只随"新值间隔"个数线性增长（array('d')，100 万个约 8 MB）。

    新值：AD 内码和上一个成功读数不一样（第一个成功读数也算）。样本时刻取请求和回复的中点。
    力的均值和标准差用 Welford 算法，不用存全部读数；标准差是总体标准差（同 R11 的 pstdev）。
    """

    def __init__(self):
        self.reads = self.ok_reads = self.errors = self.consecutive = self.max_consecutive = 0
        self.first_ns: int | None = None
        self.last_ns: int | None = None
        self.has_ad = False
        self.new_values = 0
        self._last_ad: int | None = None
        self._last_new_ns: float | None = None
        self.new_intervals_ms = array('d')
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._min: float | None = None
        self._max: float | None = None

    def add(self, row: SampleRow) -> None:
        self.reads += 1
        self.first_ns = row.request_ns if self.first_ns is None else self.first_ns
        self.last_ns = row.response_ns
        if not row.ok:
            self.errors += 1
            self.consecutive += 1
            self.max_consecutive = max(self.max_consecutive, self.consecutive)
            return
        self.consecutive = 0
        self.ok_reads += 1
        if row.force_n is not None:
            self._n += 1
            delta = row.force_n - self._mean
            self._mean += delta / self._n
            self._m2 += delta * (row.force_n - self._mean)
            self._min = row.force_n if self._min is None else min(self._min, row.force_n)
            self._max = row.force_n if self._max is None else max(self._max, row.force_n)
        if row.ad_code is not None:
            self.has_ad = True
            if self._last_ad is None or row.ad_code != self._last_ad:
                self.new_values += 1
                stamp = (row.request_ns + row.response_ns) / 2
                if self._last_new_ns is not None:
                    self.new_intervals_ms.append((stamp - self._last_new_ns) / 1e6)
                self._last_new_ns = stamp
            self._last_ad = row.ad_code

    def result(self) -> CaptureStats:
        duration = 0.0 if self.first_ns is None else (self.last_ns - self.first_ns) / 1e9
        rate = (lambda count: count / duration if duration > 0 else None)
        intervals = sorted(self.new_intervals_ms)
        return CaptureStats(
            duration_s=duration, reads=self.reads, ok_reads=self.ok_reads, errors=self.errors,
            max_consecutive_errors=self.max_consecutive,
            new_values=self.new_values if self.has_ad else None,
            read_rate_hz=rate(self.reads), new_value_rate_hz=rate(self.new_values) if self.has_ad else None,
            new_interval_ms_median=_percentile(intervals, .5), new_interval_ms_p95=_percentile(intervals, .95),
            new_interval_ms_p99=_percentile(intervals, .99), new_interval_ms_max=intervals[-1] if intervals else None,
            force_mean_n=self._mean if self._n else None,
            force_std_n=math.sqrt(self._m2 / self._n) if self._n else None,
            force_min_n=self._min, force_max_n=self._max)


def min_step(values) -> float | None:
    """读数最小一次跳多少：所有不同读数排好序，相邻差的最小值（要求 D3）。少于两个不同读数时返回 None。"""
    distinct = sorted({value for value in values if value is not None and math.isfinite(value)})
    steps = [b - a for a, b in zip(distinct, distinct[1:])]
    return min(steps) if steps else None


def compute_stats(rows) -> CaptureStats:
    """对一段读数（例如预览窗口里最近 1 s）算统计，和整段采集用同一套算法。"""
    accumulator = StatsAccumulator()
    for row in rows:
        accumulator.add(row)
    return accumulator.result()


class CaptureSession:
    """一次采集：start 开读取线程；request_stop 请它停；finish 收尾并写 summary.json。

    metadata：cli 在开始前读出来的全部设置、警告等，原样写进 metadata.json（照 R11：每次采集记下当时的设置，
    拿到数据就知道它是在什么设置下测的）。
    """

    def __init__(self, client: ModbusClient, config: CaptureConfig, run_dir, *, metadata: dict | None = None):
        self.client = client
        self.config = config
        self.run_dir = Path(run_dir)
        self.metadata = dict(metadata or {})
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.writer: CsvWriter | None = None
        self.stats = StatsAccumulator()
        self.stop_reason: str | None = None
        self.errors: list[str] = []
        self.summary: dict | None = None
        self._preview: deque = deque(maxlen=config.preview_capacity)
        self._preview_lock = threading.Lock()
        self._newest = 0
        self.preview_dropped = 0  # 预览缓冲满了挤掉的、或者取锁失败跳过的（CSV 里照样有）

    def start(self) -> CaptureSession:
        if self.thread is not None:
            raise RuntimeError('一次采集只能开始一次')
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.run_dir / 'metadata.json', dict(
            started_at=datetime.now().isoformat(timespec='seconds'), link=asdict(self.client.link),
            config=asdict(self.config), fields=list(SAMPLE_FIELDS), time_basis='time.perf_counter_ns',
            **self.metadata))
        self.writer = CsvWriter(self.run_dir / 'samples.csv', SAMPLE_FIELDS)
        self.thread = threading.Thread(target=self._run, name='force-reader', daemon=False)
        self.thread.start()
        return self

    def _run(self) -> None:
        assert self.writer is not None
        reader = SampleReader(self.client, self.config, self.stop)
        try:
            for row in reader.rows():
                try:
                    self.writer.writerow(row.as_row())
                except RuntimeError as exc:
                    self.errors.append(str(exc))
                    self.stop_reason = 'writer_error'  # 原始数据不完整，不能接着采
                    break
                self.stats.add(row)
                self._newest = row.sample_index
                if self._preview_lock.acquire(blocking=False):
                    try:
                        if len(self._preview) == self._preview.maxlen:
                            self.preview_dropped += 1
                        self._preview.append(row)
                    finally:
                        self._preview_lock.release()
                else:
                    self.preview_dropped += 1
            self.stop_reason = self.stop_reason or reader.stop_reason
        except Exception as exc:
            self.errors.append(f'{type(exc).__name__}: {exc}')
            self.stop_reason = 'exception'

    def request_stop(self) -> None:
        self.stop.set()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def progress(self) -> dict:
        """给界面每秒刷新用的计数，不加锁（读整数在 CPython 里是原子的）。"""
        return dict(reads=self.stats.reads, ok_reads=self.stats.ok_reads, errors=self.stats.errors,
                    new_values=self.stats.new_values if self.stats.has_ad else None, running=self.running,
                    stop_reason=self.stop_reason)

    def read_preview(self, after: int = 0) -> dict:
        """拷贝预览缓冲里序号大于 after 的行；gap 为 True 表示中间有行没进预览（CSV 里照样有）。"""
        if type(after) is not int or after < 0:
            raise ValueError('after 要是不小于 0 的整数')
        with self._preview_lock:
            rows = [row for row in self._preview if row.sample_index > after]
            newest = self._newest
        expected = after + 1
        gap = False
        for row in rows:
            if row.sample_index != expected:
                gap = True
            expected = row.sample_index + 1
        if rows and rows[-1].sample_index < newest or not rows and newest > after:
            gap = True
        return dict(rows=rows, newest=newest, gap=gap, dropped=self.preview_dropped)

    def wait(self, timeout: float | None = None) -> bool:
        """等读取线程自己停（到时长、连续失败）；返回是否已停。"""
        if self.thread is None:
            return True
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def finish(self, timeout: float = 10.0) -> dict:
        """请读取线程停下，等它和写盘收尾，写 summary.json。返回 summary。"""
        self.request_stop()
        reader_closed = self.wait(timeout)
        writer_result = self.writer.close(timeout) if self.writer is not None else None
        stats = self.stats.result()
        self.summary = dict(
            finished_at=datetime.now().isoformat(timespec='seconds'), stop_reason=self.stop_reason,
            errors=self.errors, reader_closed=reader_closed, writer=writer_result,
            complete=bool(reader_closed and writer_result and writer_result['drained'] and not self.errors),
            preview_dropped=self.preview_dropped, stats=asdict(stats))
        write_json(self.run_dir / 'summary.json', self.summary)
        return self.summary
