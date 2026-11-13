"""分层检查：下层模块不能导入上层。

规则只写在一个地方：pyproject.toml 的 [tool.importlinter]（写法同 R11）。这里读同一张表离线复查，
没装 import-linter 也能检查；新加的模块没登记分层也会报出来。
"""
import ast
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'rdt_gauge'


def _layers():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    contract = next(c for c in config['tool']['importlinter']['contracts'] if c['type'] == 'layers')
    rank, independent = {}, {}
    for index, layer in enumerate(contract['layers']):
        separator = '|' if '|' in layer else ':'
        names = [part.strip() for part in layer.split(separator)]
        for name in names:
            rank[name] = index
            independent[name] = separator == '|' and len(names) > 1
    return rank, independent


def _imports(path, module):
    """path 里导入的 rdt_gauge 子模块（相对导入和绝对导入都算）。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                found.update({f'rdt_gauge.{node.module}'} if node.module else
                             {f'rdt_gauge.{alias.name}' for alias in node.names})
            elif node.module and node.module.split('.')[0] == 'rdt_gauge':
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.split('.')[0] == 'rdt_gauge')
    return {'.'.join(name.split('.')[:2]) for name in found} - {module, 'rdt_gauge'}


def _modules():
    return {f'rdt_gauge.{path.stem}': path for path in PACKAGE.glob('*.py') if path.stem != '__init__'}


def test_every_module_has_a_layer():
    rank, _ = _layers()
    missing = set(_modules()) - set(rank)
    assert not missing, f'这些模块没在 pyproject.toml 里登记分层：{sorted(missing)}'


def test_lower_layers_never_import_higher_ones():
    rank, independent = _layers()
    problems = []
    for module, path in sorted(_modules().items()):
        for target in sorted(_imports(path, module)):
            if target not in rank:
                problems.append(f'{module} 导入了没登记分层的 {target}')
            elif rank[target] < rank[module]:
                problems.append(f'{module} 在下层，不能导入上层的 {target}')
            elif rank[target] == rank[module] and independent[module]:
                problems.append(f'{module} 和 {target} 在同一层、互相独立，不能互相导入')
    assert not problems, '\n'.join(problems)


def test_the_checker_sees_relative_and_absolute_imports(tmp_path):
    source = tmp_path / 'probe.py'
    source.write_text('from .cli import main\nfrom . import capture\nimport rdt_gauge.storage\n'
                      'from rdt_gauge.protocol import crc16\nimport os\n', encoding='utf-8')
    assert _imports(source, 'rdt_gauge.probe') == {
        'rdt_gauge.cli', 'rdt_gauge.capture', 'rdt_gauge.storage', 'rdt_gauge.protocol'}


def test_package_init_imports_nothing():
    # __init__.py 一导入子模块，下层模块被导入时就会把上层也拉进来，分层就形同虚设。
    assert not _imports(PACKAGE / '__init__.py', 'rdt_gauge')
