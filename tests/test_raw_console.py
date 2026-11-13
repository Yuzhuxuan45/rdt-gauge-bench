"""raw_console.py：解析输入、只读默认、写白名单、拆解回复。回复按顺序给，不碰设备。"""
import pytest

from rdt_gauge.contracts import LinkSettings, NotAllowed
from rdt_gauge.protocol import ModbusClient, add_crc, build_read, build_write
from rdt_gauge.raw_console import check_allowed, explain_reply, parse_command, repl, send
from frames import exception_reply, read_reply, write_echo
from scripted_port import ScriptedPort

H = bytes.fromhex


def _client(replies):
    return ModbusClient(ScriptedPort(replies), LinkSettings('COM9'))


@pytest.mark.parametrize('text, start, count', [('03 0050 0002', 80, 2), ('030050 0002', 80, 2), ('r 80 2', 80, 2),
                                                ('r 8', 8, 1), ('R 0x2C 2', 44, 2)])
def test_read_commands(text, start, count):
    request = parse_command(text)
    assert (request.function, request.start, request.count) == (0x03, start, count)
    assert request.reply_bytes == 5 + 2 * count


@pytest.mark.parametrize('text, start, values', [('10 0022 0001 02 0000', 34, (0,)), ('w 34 0', 34, (0,)),
                                                 ('w 5 0x5AA5', 5, (0x5AA5,)), ('w 34 0 0', 34, (0, 0))])
def test_write_commands(text, start, values):
    request = parse_command(text)
    assert (request.function, request.start, request.values, request.reply_bytes) == (0x10, start, values, 8)


@pytest.mark.parametrize('text', ['', 'zz', '06 0022 0000', '03 0050', '03 0050 0000', '03 0050 007E',
                                  '10 0022 0001 04 0000', 'r', 'w 34', 'w 34 70000', 'r x'])
def test_bad_input_is_refused_before_sending(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_writes_need_allow_write_and_the_whitelist():
    check_allowed(parse_command('r 7 1'), allow_write=False)  # 读什么都行，包括恢复出厂寄存器
    with pytest.raises(NotAllowed, match='--allow-write'):
        check_allowed(parse_command('w 34 0'), allow_write=False)
    check_allowed(parse_command('w 34 0'), allow_write=True)
    check_allowed(parse_command('w 5 0x5AA5'), allow_write=True)  # 解锁
    check_allowed(parse_command('w 5 0'), allow_write=True)  # 上锁
    # 白名单本身挡下来的（不是碰巧被后面的取值检查挡住）：恢复出厂、皮重、执行置零、标定、协议类型、协议没写的地址
    for text in ('w 7 0x55', 'w 84 0', 'w 94 1', 'w 36 0', 'w 3 1', 'w 500 0', 'w 35 0 0', 'w 5 0x1234'):
        with pytest.raises(NotAllowed, match='白名单'):
            check_allowed(parse_command(text), allow_write=True)
    for text in ('w 0 5', 'w 1 7', 'w 2 5'):  # 通信参数：在白名单里，但手动收发不让改（改了就断联）
        with pytest.raises(NotAllowed, match='set-link'):
            check_allowed(parse_command(text), allow_write=True)
    with pytest.raises(NotAllowed, match='不能写 99'):
        check_allowed(parse_command('w 34 99'), allow_write=True)


def test_send_adds_address_and_crc_and_explains_the_reply():
    client = _client([H('01 03 04 00 00 00 84 FA 50')])
    lines = send(client, parse_command('03 0050 0002'), allow_write=False)
    assert client.port.written == [build_read(1, 80, 2)]
    assert lines[0] == '发送：01 03 00 50 00 02 C4 1A'
    assert 'CRC：对' in lines and any('寄存器  81 = 0x0084' in line for line in lines)


def test_send_refuses_a_write_without_sending_anything():
    client = _client([])
    with pytest.raises(NotAllowed):
        send(client, parse_command('w 34 0'), allow_write=False)
    assert client.port.written == []


def test_send_shows_timeouts_and_refusals():
    assert any('超时' in line for line in send(_client([b'']), parse_command('r 80 2'), allow_write=False))
    lines = send(_client([exception_reply(function=0x10, code=0x04)]), parse_command('w 34 0'), allow_write=True)
    assert any('设备拒绝' in line for line in lines)
    lines = send(_client([write_echo(34, [0])]), parse_command('w 34 0'), allow_write=True)
    assert '写入回显：和请求一致' in lines and lines[0] == '发送：' + build_write(1, 34, [0]).hex(' ').upper()


def test_explain_reply_flags_bad_crc_wrong_address_and_length():
    request = parse_command('r 80 2')
    assert 'CRC：不对' in explain_reply(H('01 03 04 00 00 00 85 FA 50'), request, 1)
    assert any('地址' in line for line in explain_reply(read_reply([0, 1], address=2), request, 1))
    assert any('长度不对' in line for line in explain_reply(add_crc(H('01 03 02 00 01')), request, 1))


def test_repl_runs_commands_until_quit():
    client = _client([read_reply([0x0802])])
    printed = []
    commands = iter(['help', 'r 8', 'w 34 0', 'nonsense', 'quit'])
    repl(client, allow_write=False, input_fn=lambda prompt: next(commands), print_fn=printed.append)
    text = '\n'.join(printed)
    assert '模块状态' in text and '没发送：默认只读' in text and '没发送：不是十六进制' in text
    assert client.port.written == [build_read(1, 8, 1)]


def test_repl_ends_on_eof():
    def eof(prompt):
        raise EOFError
    repl(_client([]), allow_write=False, input_fn=eof, print_fn=lambda line: None)
