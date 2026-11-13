"""写盘：CSV 只有一个写入者、JSON 原子写、每次运行一个新目录。

不碰设备，也不管数据是什么意思。写法参考（登记在 docs/SOURCES.json）：
- CsvWriter：r11_upper_computer/wuji_r11/storage/raw_writer.py 的 CsvOutput —— 一个文件只有一个写入线程；
  调用方只往有界队列里放一行，**从不等待磁盘**；队列满了就记成错误（少一行就是丢数据，不能悄悄丢）；
  每 0.5 s 刷一次盘；"x" 模式打开，不覆盖已有文件；utf-8-sig，Excel 直接打开不乱码。
- write_json：r11_upper_computer/wuji_r11/storage/atomic.py —— 先写同目录的临时文件并 fsync，再整体替换，
  断电也不会留下写了一半的文件。
- new_run_dir：r11_upper_computer/tools/cybsq_probe.py 的 out_dir —— 带时间和标签的目录，不覆盖以前的结果。

和 R11 不一样的地方：
- 默认写到仓库外（%USERPROFILE%\\RDT_TestData），数据不会被误提交到 git。
- 没有 R11 的"学习检查点"（那是给在线学习用的），这里只要原始数据完整。
"""
from __future__ import annotations

from datetime import datetime
import csv
import json
import os
from pathlib import Path
import queue
import re
import threading
import time

from .contracts import SAMPLE_FIELDS, SampleRow

DATA_ROOT_ENV = 'RDT_BENCH_DATA'  # 设了这个环境变量就写到它指定的目录


def default_data_root() -> Path:
    """数据根目录：环境变量 RDT_BENCH_DATA，否则 %USERPROFILE%\\RDT_TestData（在仓库外）。"""
    configured = os.environ.get(DATA_ROOT_ENV)
    return Path(configured) if configured else Path.home() / 'RDT_TestData'


def write_json(path, value) -> Path:
    """原子写 JSON：先写临时文件并 fsync，再替换。不允许 NaN、无穷大（写进去别的程序读不回来）。"""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def read_json(path):
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)


def read_samples(path) -> list[SampleRow]:
    """把采集写的 samples.csv 读回成 SampleRow（离线重算统计用）。表头不对就报错，不猜列。"""
    def optional(text: str, kind):
        return kind(text) if text != '' else None

    rows = []
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SAMPLE_FIELDS:
            raise ValueError(f'{path} 的表头不是采集格式：{reader.fieldnames}')
        for record in reader:
            rows.append(SampleRow(int(record['sample_index']), int(record['request_ns']), int(record['response_ns']),
                                  optional(record['measured'], int), optional(record['ad_code'], int),
                                  optional(record['force_n'], float), record['raw_hex'], record['error']))
    return rows


_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def safe_label(label: str) -> str:
    """把标签变成能当目录名的字符串：去掉 Windows 不允许的字符和空白，中文保留。"""
    cleaned = _UNSAFE.sub('-', str(label)).strip('-.')
    return cleaned[:40] or 'run'


def new_run_dir(kind: str, label: str = '', root=None, *, now=None) -> Path:
    """建一个新的运行目录：<根>/<kind>/<年月日-时分秒>-<标签>。重名就加 -2、-3，绝不覆盖。"""
    stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
    base = Path(root) if root is not None else default_data_root()
    parent = base / safe_label(kind)
    parent.mkdir(parents=True, exist_ok=True)
    name = f'{stamp}-{safe_label(label)}' if label else stamp
    for index in range(1, 1000):
        candidate = parent / (name if index == 1 else f'{name}-{index}')
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f'{parent} 下同名目录太多，换个标签')


class CsvWriter:
    """一个 CSV 文件只有一个写入线程。调用方用 writerow 放进队列，立刻返回，不等磁盘。

    出错（队列满、磁盘写不进去）以后 error 记下第一个原因，之后的 writerow 都会抛错：
    丢了一行就是数据不完整，不能当作没事接着写。
    """

    def __init__(self, path, fields, *, capacity: int = 100_000, flush_interval_s: float = 0.5):
        if capacity < 1:
            raise ValueError('队列容量要大于 0')
        self.path = Path(path)
        self.fields = tuple(fields)
        self.flush_interval_s = max(0.02, float(flush_interval_s))
        self._queue: queue.Queue = queue.Queue(maxsize=capacity)
        self._closing = threading.Event()
        self.ready = threading.Event()  # 文件已打开、表头已写
        self.error: str | None = None
        self.written = 0
        self.thread = threading.Thread(target=self._run, name='csv-writer', daemon=False)
        self.thread.start()

    def writerow(self, row: dict) -> None:
        if self.error is not None:
            raise RuntimeError('CSV 写入已失败：' + self.error)
        if self._closing.is_set():
            raise RuntimeError('CSV 已经在关闭，不再接收新行')
        try:
            self._queue.put_nowait(dict(row))
        except queue.Full:
            self.error = self.error or 'CSV 队列已满：磁盘跟不上，少写了一行，数据不完整'
            raise RuntimeError(self.error) from None

    def close(self, timeout: float = 10.0) -> dict:
        """不再收新行，等已经放进队列的行全部写完。返回写了多少行、有没有出错。"""
        self._closing.set()
        self.thread.join(timeout)
        return dict(path=str(self.path), written=self.written, closed=not self.thread.is_alive(),
                    drained=not self.thread.is_alive() and self._queue.empty() and self.error is None,
                    error=self.error)

    def _run(self) -> None:
        try:
            with self.path.open('x', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fields, extrasaction='raise')
                writer.writeheader()
                handle.flush()
                self.ready.set()
                flushed = time.monotonic()
                while not self._closing.is_set() or not self._queue.empty():
                    try:
                        row = self._queue.get(timeout=0.02)
                    except queue.Empty:
                        row = None
                    if row is not None:
                        writer.writerow(row)
                        self.written += 1
                    if time.monotonic() - flushed >= self.flush_interval_s:
                        handle.flush()
                        flushed = time.monotonic()
                handle.flush()
                os.fsync(handle.fileno())  # 正常关闭时落盘一次
        except Exception as exc:
            self.error = self.error or f'{type(exc).__name__}: {exc}'
        finally:
            self.ready.set()
