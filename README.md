# 锐力德测力计台架测试脚本（rdt_gauge_bench）

锐力德 RDT-TVK 变送器 + RDF-TC10B 拉压力传感器的台架测试脚本：通信、采集、实时波形、手动收发、查看和修改设置。

- 测力计放在台架上，接一台单独的电脑测，**不接 DH，不依赖 R11、R2**，运行时也不导入它们。
- 用途：对照测力计要求（A–G），看新测力计能不能代替现在的测力计。测试分类、到货后的调通顺序、A–G 的测法和合格标准见 [docs/TESTS.md](docs/TESTS.md)。
- 写法参考 `r11_upper_computer/wuji_r11/acquisition/` 和 `r11_upper_computer/tools/gauge_probe.py`、`cybsq_probe.py`、`cybsq_configure.py`；每个参考的来源文件、SHA-256、改了什么登记在 [docs/SOURCES.json](docs/SOURCES.json)。

## 当前状态

| 项 | 状态 |
|---|---|
| 全部模块（通信、寄存器表、写盘、串口、采集、设置、手动收发、波形、命令行） | 已写；离线测试 274 项通过、1 项跳过（图形窗口，见下）；关键安全规则做过变异检查 |
| 能测的要求（第一版） | A1、A2、C1、C2、D1、D3、D5（采集）、E1–E5（读和设置）、F1、F3、F4；B2 的设置部分 |
| 第二版要加的 | 阶跃和 12 Hz 峰值（B1、B3–B5，要继电器接线）、50 Hz 频谱（D2）、砝码分级（D4） |
| 实机 | **未验**（测力计还没到）。串口打开、采集时序、改波特率、图形窗口都要实机验证 |

## 目录结构

```
rdt_gauge_bench/
  pyproject.toml          依赖版本、pytest 配置、分层合同
  docs/
    SOURCES.json          从 R11 改写的函数：来源文件、SHA、改了什么
    TESTS.md              要求 A–G → 命令 → 合格标准
  rdt_gauge/
    contracts.py          最底层：数据类型、异常、常量，不做任何 I/O
    protocol.py           通信：CRC、组帧、读写寄存器、解码
    registers.py          寄存器表：名称、读写属性、白名单、取值范围、状态位
    storage.py            CSV 写盘（只有一个写入者）、JSON 原子写、运行目录
    ports.py              找串口（按 VID 和序列号）、扫描通信参数（只发 03）
    capture.py            读取线程 + 写盘线程 + 有界预览缓冲 + 实时统计
    settings_ops.py       查看、备份、修改（先显示差异、写完读回）、恢复、改波特率
    raw_console.py        手动收发报文
    live_plot.py          实时波形（pyqtgraph）
    cli.py                命令行入口
  tests/
    offline_guard/        离线测试时锁死真实串口
    test_*.py             按模块的离线测试
```

分层（从上到下）：`cli` → `live_plot`、`raw_console` → `capture`、`settings_ops` → `ports`、`storage` → `protocol`、`registers` → `contracts`。上层可以导入下层，下层不能导入上层。规则写在 `pyproject.toml` 的 `[tool.importlinter]`，`tests/test_layers.py` 离线复查。

## 安装和离线测试

Windows x64、CPython 3.13 x64，放在英文路径下。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"       # 要看波形再加 gui：".[gui,test]"
$env:PYTHONUTF8 = '1'
.venv\Scripts\python -m pytest -q
```

离线测试不碰任何设备：`tests/conftest.py` 先把 `serial.Serial` 和列串口的函数换成直接报错，再让测试导入代码（做法照 R2 `tests/offline_guard`）。子进程要用同样的防护时，设 `RDT_BENCH_OFFLINE_ONLY=1`，并把 `tests/offline_guard` 加进 `PYTHONPATH`。

### Windows "智能应用控制"

开着"智能应用控制"（Smart App Control）的电脑会拦截没有签名的程序文件，2026-11-13 在开发电脑上遇到两处：

- **PyQt6 加载不了**（`DLL load failed while importing sip`，WinError 4551）：波形窗口用不了。`live` 会在开始采集**之前**就报出来，不会采到一半；这时用 `live --no-plot` 只采集，波形在别的电脑上看，或者用 `stats` 看统计。
- **`rdt-gauge.exe` 运行不了**（pip 生成的启动程序）：一律用 `python -m rdt_gauge.cli`。

这是系统安全设置，关不关由你或 IT 决定，脚本不去改它。台架电脑先用 `python -c "from PyQt6 import QtCore"` 试一下。

## 命令

都在本目录运行，前缀 `.venv\Scripts\python -m rdt_gauge.cli`（下面省略）。`<命令> --help` 看全部参数。

| 命令 | 做什么 | 写设备吗 |
|---|---|---|
| `ports` | 列串口、USB 序列号，标出 FTDI 和它的延迟计时器 | 不打开串口 |
| `scan --port COM9` | 逐个试波特率、校验位、停止位、地址，找能通的那组 | 只发 03 读 |
| `info` | 读全部设置，翻译成能看懂的话，和测试设置不同的给提醒 | 只读 |
| `backup` | 全部设置存成带时间的 JSON | 只读 |
| `live --seconds 60 --label 空载` | 采集到新目录；默认开波形，`--no-plot` 只采集；`--mode gross` 只读毛重；`--period-ms 4` 按固定周期读 | 只读 |
| `stats <采集目录>` | 不连设备，从 `samples.csv` 重算统计并对照 C1、D1、D3 的合格线 | 不连设备 |
| `raw` | 手动收发报文（例 `r 80 2`、`03 0050 0002`） | 默认只读；`--allow-write` 后只能写白名单，通信参数不让写 |
| `set filter_type=0 filter_strength=0` | 改普通设置：先列出"原值 → 新值" | 加 `--apply` 才写；设备拒绝时再加 `--unlock` |
| `preset-test` | 改成测试设置（跟踪、清零全关，滤波关，应答延时 0，双向） | 加 `--apply` 才写 |
| `restore <备份或修改记录.json>` | 照记录写回原值（通信参数不在内） | 加 `--apply` 才写 |
| `set-link --to-baud 921600` | 改波特率（或 `--to-frame 5`、`--to-address 2`），一次一项，改完自动切换并读回确认 | 加 `--apply` 才改 |

连接参数：`--port COM9`（或 `--serial-number`，换 USB 口也找得到），`--baud`（默认 9600）、`--parity`（N）、`--stopbits`（2）、`--address`（1）。都不给端口时，本机正好一个 FTDI 串口才自动用它。

数据都写到 `%USERPROFILE%\RDT_TestData\`（`--data-root` 或环境变量 `RDT_BENCH_DATA` 可改）：
- `settings\`：`backup-*.json`（备份）、`change-*.json`（每次修改：原值、计划、每项读回、最后核对）、`link-*.json`（改通信参数的每一步）；
- `live\<时间>-<标签>\`：`metadata.json`（采集时的全部设置和提醒）、`samples.csv`（每次读一行，失败也有，带原始字节）、`summary.json`（停止原因、是否完整、统计）。

到货后按 [docs/TESTS.md](docs/TESTS.md) 第 ② 节的 11 步调通。

## 规矩

- **数据不进 git**：采集结果和备份默认写到仓库外（`%USERPROFILE%\RDT_TestData\`）；本目录的 `.gitignore` 兜底。
- **不写死 COM 号和设备序列号**：都从命令行传入；测试里的序列号是编出来的。
- **只用一个时钟**：所有时间戳都是 `time.perf_counter_ns()`。
- **一个串口只有一个读取者**：只有采集线程能碰串口；波形窗口只从缓冲里取数据，不读串口。
- **默认只读**：写寄存器只能写白名单里的；不加 `--apply` 只列计划；写之前原值先存盘，每项写完立刻读回。永远不写 7（恢复出厂）、36–43（标定）、46–49（免砝码标定）、60–66（多点修正）、84（皮重）、94（执行置零）、290（清除峰谷值）。
- **通信参数单独改**：地址、波特率、帧格式改了电脑这边就连不上，只能用 `set-link`；`set` 和 `raw` 都不让直接写。
- **在单独的电脑上测**，不要在正在做实验的 DH 电脑上跑。

## 已知的资料问题

- **负数例子算错了 1**：指令解析 1.82 把 `FF FF F0 C2` 换算成 −3901，用的是"减 0xFFFFFFFF"。协议写的是"负数采用标准补码方式"，标准补码下它是 **−3902**（−3901 是 `FF FF F0 C3`）。代码按标准补码解码，测试 `test_the_manuals_negative_example_is_off_by_one` 记下了这一点。到货后用一个已知的负读数（拉向加载）再核对一次。
- **出厂停止位**：协议表写数据帧格式默认 0x06（8N2），指令解析写默认 05（8N1）。`LinkSettings` 默认按协议表用 8N2；连不上时用扫描两种都试。
- **滤波地址缺失**：一级滤波次数、二级滤波强度、稳定滤波、AD 分辨率在说明书菜单 F1-14~17 里有，协议表没给地址；二级滤波设 0 是"自动调整"，不等于关。要问厂家，问清之前不放进白名单。
- **配置锁管什么**：协议 1.6 写锁只管地址、波特率、帧格式、协议类型、恢复出厂，所以改滤波、速度、跟踪默认不解锁。如果锁住时设备拒绝写这些（回 Modbus 异常），改的时候加 `--unlock`（先解锁，写完一定重新上锁）。

## 到货后要在实机上核对的

| 项 | 为什么 |
|---|---|
| 负数解码（拉向加载读一个已知负值） | 说明书例子和协议写法差 1，见上 |
| 出厂停止位是 1 还是 2 | 两份资料写的不一样；`scan` 两种都试 |
| 测量值（30）和毛重（80）是否一样 | `block` 读法读测量值（不受置零影响，一次同时拿到 AD 内码）；`gross` 读法读毛重（R11 原测力计读的是毛重）。跟踪、清零都关掉后两者应该一样，要实测 |
| AD 内码（44）是不是每次转换都变 | 判断"新值"靠它；协议写它是"滤波后的原始码" |
| 锁住时能不能写滤波 | 见"配置锁管什么" |
| 改波特率时设备回不回显、多久生效 | `change_link` 不依赖回显，按新参数读回确认 |
| AD 速度编码是高速、中速还是低速版 | 寄存器 32 的编码按版本不同，读不出版本；看合同或问厂家 |
| 寄存器 9、10 读不读得出 | R11 打开原测力计时读它们（通道数、字序），锐力德协议没写；`info` 会显示试读结果，交给维护者判断 R11 能不能直接接 |
| 图形窗口（台架电脑上） | 开发电脑被智能应用控制拦截，窗口代码没能在真窗口里跑过 |
