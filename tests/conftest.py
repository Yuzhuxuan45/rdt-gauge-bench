"""所有测试都离线：先锁死真实串口，再让测试导入 rdt_gauge。"""
import importlib.util
import pathlib

_GUARD = pathlib.Path(__file__).parent / 'offline_guard' / 'sitecustomize.py'
_spec = importlib.util.spec_from_file_location('rdt_offline_guard', _GUARD)
assert _spec is not None and _spec.loader is not None
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
if not _guard.block_serial():
    raise RuntimeError('没装 pyserial：先按 README 安装依赖（pip install -e ".[test]"）')
