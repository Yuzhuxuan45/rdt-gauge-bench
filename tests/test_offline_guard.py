"""离线防护本身要可靠：本进程和子进程里都打不开串口、列不出串口。"""
import os
import pathlib
import subprocess
import sys

import pytest
import serial
from serial.tools import list_ports

GUARD = pathlib.Path(__file__).parent / 'offline_guard'


def test_opening_a_port_is_blocked_in_this_process():
    with pytest.raises(RuntimeError, match='离线测试'):
        serial.Serial('COM1', 9600)


def test_listing_ports_is_blocked_in_this_process():
    with pytest.raises(RuntimeError, match='离线测试'):
        list_ports.comports()


def test_opening_a_port_is_blocked_in_child_processes():
    paths = [str(GUARD)] + ([os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])
    env = dict(os.environ, RDT_BENCH_OFFLINE_ONLY='1', PYTHONPATH=os.pathsep.join(paths), PYTHONUTF8='1')
    result = subprocess.run([sys.executable, '-c', "import serial; serial.Serial('COM1', 9600)"],
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode != 0
    assert '离线测试' in result.stderr
