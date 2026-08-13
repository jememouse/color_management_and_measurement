# Color Workbench

基于 [ArgyllCMS](https://www.argyllcms.com/) 的色彩管理与测量工作台，为 X-Rite i1 Pro 2
等分光光度计提供浏览器交互界面。

把 ArgyllCMS 那套强大但交互笨拙的命令行工具（`spotread` / `dispcal` / `colprof` …）
包装成可视化流程：实时光谱曲线、一键校准、显示器 ICC profile 全链路生成。

---

## 为什么是「本地服务 + 浏览器」

浏览器无法直接访问分光光度计。WebUSB 在 macOS 上无法接管已被系统驱动
（X-Rite 的 `xrdd`）注册的设备，且 i1Pro2 的通信协议复杂（EEPROM 解析、暗电流校准、
自适应积分时间），重新实现一遍不现实。

所以架构必然是：

```
浏览器 (前端)
   │  HTTP + SSE
   ▼
本地 Python 服务 (server.py)
   │  pty (伪终端)
   ▼
ArgyllCMS CLI (spotread / dispcal / …)
   │  USB (IOKit)
   ▼
X-Rite i1 Pro 2
```

**关键难点在 pty 这一层**：ArgyllCMS 的工具是交互式 TTY 程序，会把终端切到 raw 模式
读取单个按键（不带回车），普通的 `subprocess.PIPE` 会让它报
`tcgetattr failed with 'Operation not supported by device'` 并拒绝工作。必须用
`pty.fork()` 分配真正的伪终端。

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| macOS | 12+ | Linux 理论可用，未测试 |
| Python | 3.13+ | 由 uv 管理，见 `.python-version` |
| ArgyllCMS | 3.x | `brew install argyll-cms` |
| uv | 0.9+ | 包与环境管理 |

**运行时零第三方依赖** —— 全部基于 Python 标准库（`pty` / `http.server` / `json`）。
这是刻意的设计决定：色彩测量工具常需部署在无网络的产线机器上，少一个依赖就少一个装不上的理由。
`uv` 只用于锁定 Python 版本和管理开发工具（ruff / pytest）。

## 快速开始

```bash
# 1. 安装 ArgyllCMS
brew install argyll-cms

# 2. 启动（自动完成环境同步、自检、开浏览器）
./start.sh
```

`start.sh` 在拉起服务前会依次检查 uv、端口占用、USB 上的 X-Rite 设备、
以及会抢占仪器的 i1Profiler 进程，任何一项不对都直接给出可执行的修复命令。
服务就绪后自动打开 <http://127.0.0.1:8721>。

常用选项：

```bash
./start.sh --check            # 只做环境自检, 打印完整报告后退出(装机后先跑这个)
./start.sh --port 9000        # 换端口
./start.sh --restart          # 端口上有旧实例时, 先优雅停掉再启动
./start.sh --no-browser       # 不自动开浏览器
./start.sh --no-sync          # 跳过 uv sync(离线机器)
./start.sh --argyll-bin /opt/Argyll/bin   # 指定 ArgyllCMS 安装位置
```

也可以绕过脚本直接跑，环境变量与上述选项等价：

```bash
uv sync --group dev
uv run python config.py                    # 环境自检
uv run python server.py                    # 启动
I1_PORT=9000 uv run python server.py       # 换端口
ARGYLL_BIN=/opt/Argyll/bin uv run python server.py   # 指定 ArgyllCMS 位置
```

## 与 X-Rite 官方软件的共存

ArgyllCMS 与 X-Rite 的 `xrdd` 守护进程**可以共存**（`xrdd` 不独占 USB 句柄），
但 **i1Profiler 主程序运行时会独占设备**，此时本工作台无法连接，反之亦然。

如果 `i1ProfilerTray` 后台进程占用 CPU 异常，可永久禁用其开机自启（可逆）：

```bash
sudo launchctl disable gui/$(id -u)/com.xrite.i1Profiler.tray
sudo launchctl bootout gui/$(id -u)/com.xrite.i1Profiler.tray
```

恢复：把 `disable` 换成 `enable`，`bootout` 换成
`bootstrap gui/$(id -u) /Library/LaunchAgents/com.xrite.i1Profiler.tray.plist`。

## 已知硬件约束

**M0/M1/M2 测量条件需要仪器配备 UV 滤镜硬件。** 并非所有 i1 Pro 2 都带这个配件 ——
启动测量时留意仪器自检信息里的这一行：

```
U.V. filter ?:     No        <- 没有滤镜硬件
```

若为 `No`，选择 M1/M2 会让 `spotread` 直接以
`Setting requested filter not supported by instrument` 退出。界面会读取这一栏并自动
禁用不可用的选项，测量条件请保持「不指定」。

> 这一条是真机测试时撞出来的，不是从文档里读到的 —— ArgyllCMS 的用法说明里
> `-F` 选项一直存在，它不会告诉你手上这台设备装没装滤镜。

**灯管寿命**：仪器档案里的 `Total lamp usage` 是卤钨灯累计点亮时长，**单位是秒，不是小时**。

这一点极易看错。示例设备读出 `7243.09`，乍看像"7243 小时"，但结合累计测量次数一除就露馅：
8599 次测量对应 7243 秒，即每次点亮 0.84 秒 —— 正是 i1Pro 一次反射测量的积分时间量级；
若当成小时，就成了每次测量点灯 50 分钟，显然不成立。实际累计仅约 2 小时。

界面会直接给出换算后的时长与健康结论，不显示原始秒数。需要注意的是
**X-Rite 未公开 i1Pro 灯管的额定寿命**，分档只是按卤钨灯的一般经验给出的参考；真正
权威的判断来自仪器自报的 `Lamp is weak` / `Lamp has failed`，那两条会单独以告警呈现。

## 项目结构

```
.
├── config.py           # 全局配置、ArgyllCMS 探测、工具白名单
├── server.py           # HTTP + SSE 服务，路由层
├── argyll/
│   ├── session.py      # pty 会话内核 —— 驱动交互式 CLI
│   ├── parser.py       # 输出解析：XYZ / Lab / 光谱 / 进度
│   └── tools.py        # 各工具的命令构建与参数校验
├── static/             # 前端（原生 HTML/CSS/JS，无构建步骤）
├── work/               # 运行时产物：.ti1 / .ti3 / .cal / .icc
├── docs/               # 测量原理与流程说明
└── tests/              # pytest
```

## 安全边界

服务默认只监听 `127.0.0.1`。即便如此，浏览器里的任意页面都可能向 localhost 发起
跨站请求，因此：

- **工具白名单**：只有 `config.ALLOWED_TOOLS` 中的可执行文件允许被 spawn
- **不经过 shell**：使用 `os.execv` 直接执行，参数不做 shell 解析，无注入面
- **绝对路径**：工具路径来自启动时探测的结果，避免 PATH 劫持
- **工作目录限制**：文件读写限制在 `work/` 内，路径经 `resolve()` 后校验前缀

## 开发

```bash
uv run ruff check .          # lint（含 bandit 安全规则）
uv run ruff format .         # 格式化
uv run pytest                # 测试
```

## 许可

MIT
