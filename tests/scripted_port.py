"""只在测试里用的固定回复替身（不是假测力计）：按顺序吐出测试里写好的字节，记下收到的每条请求。

它不懂 Modbus，不会按请求算回复，也不模拟时序；只用来检查 protocol.ModbusClient 怎么处理给定的回复。
"""


class ScriptedPort:
    def __init__(self, replies, *, exhausted=None):
        self.replies = list(replies)  # 每一项：bytes 原样返回；b'' 表示超时（什么也没收到）
        self.exhausted = exhausted  # 回复用完以后：None 报错（默认，抓多发的请求）；b'' 一直超时（测采集自己停下）
        self.written = []
        self.resets = 0
        self.timeout = None
        self.baudrate = 9600
        self.parity = 'N'
        self.stopbits = 2

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def read(self, size):
        if not self.replies:
            if self.exhausted is None:
                raise AssertionError('测试写的回复用完了：代码比预期多发了请求')
            return self.exhausted
        reply = self.replies.pop(0)
        return reply[:size]

    def close(self):
        pass

    def reset_input_buffer(self):
        self.resets += 1
