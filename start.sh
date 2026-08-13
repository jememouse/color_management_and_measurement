#!/usr/bin/env bash
#
# Color Workbench 启动脚本
#
# 职责边界(刻意保持很窄):
#   1. 前置检查 —— uv / ArgyllCMS / 端口 / 设备占用, 在启动前把问题说清楚
#   2. 同步环境 —— uv sync, 离线时自动降级为使用已有 .venv
#   3. 用 exec 交棒给 server.py, 让 Ctrl-C 直接落到 Python 的信号处理器上
#
# 最后一点是关键: server.py 的 SIGINT handler 负责关闭 dispcal 拉起的全屏
# 测试窗口。如果这里用普通调用而非 exec, 中间多一层 bash, Ctrl-C 的传递和
# 退出码都会变得不可靠, 窗口可能留在屏幕上关不掉。
#
# 用法: ./start.sh --help

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'
    C_DIM=$'\033[2m';  C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
    C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF=''
fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s  !%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%s错误:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
step() { printf '%s%s%s\n' "$C_BLD" "$*" "$C_OFF"; }
hint() { printf '%s      %s%s\n' "$C_DIM" "$*" "$C_OFF" >&2; }

# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

PORT="${I1_PORT:-8721}"
HOST="${I1_HOST:-127.0.0.1}"
OPEN_BROWSER=1
DO_SYNC=1
CHECK_ONLY=0
RESTART=0

usage() {
    cat <<'USAGE'
Color Workbench —— 基于 ArgyllCMS 的色彩管理与测量工作台

用法:
  ./start.sh [选项]

选项:
  -p, --port PORT      监听端口 (默认 8721, 等价于 I1_PORT)
  -H, --host HOST      监听地址 (默认 127.0.0.1, 等价于 I1_HOST)
      --argyll-bin DIR ArgyllCMS 的 bin 目录 (等价于 ARGYLL_BIN)
  -c, --check          只做环境自检, 打印完整报告后退出, 不启动服务
  -r, --restart        若端口已被本项目占用, 先停掉旧进程再启动
  -n, --no-browser     启动后不自动打开浏览器
      --no-sync        跳过 uv sync (离线机器 / 确认环境已就绪时用)
  -h, --help           显示本帮助

示例:
  ./start.sh                              # 常规启动
  ./start.sh -c                           # 装机后自检: 确认 ArgyllCMS 与全部工具就位
  ./start.sh -p 9000 -n                   # 换端口, 不开浏览器
  ./start.sh -H 0.0.0.0                   # 允许局域网访问(注意: 无鉴权, 仅限可信网络)
  ./start.sh --argyll-bin /opt/Argyll/bin # 指定非标准安装位置
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)       PORT="${2:?--port 需要一个值}"; shift 2 ;;
        -H|--host)       HOST="${2:?--host 需要一个值}"; shift 2 ;;
        --argyll-bin)    export ARGYLL_BIN="${2:?--argyll-bin 需要一个值}"; shift 2 ;;
        -c|--check)      CHECK_ONLY=1; shift ;;
        -r|--restart)    RESTART=1; shift ;;
        -n|--no-browser) OPEN_BROWSER=0; shift ;;
        --no-sync)       DO_SYNC=0; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "未知参数: $1 (用 --help 查看用法)" ;;
    esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 0 && PORT < 65536 )) || die "端口无效: $PORT"

export I1_PORT="$PORT"
export I1_HOST="$HOST"

# 输出重定向到日志文件时, Python 的 stdout 会转为块缓冲, 启动横幅要等
# 缓冲区攒满才出现。服务的输出量很小, 关掉缓冲不会有性能代价。
export PYTHONUNBUFFERED=1

# 浏览器要访问的地址: 监听 0.0.0.0 时不能直接拿它当 URL
URL_HOST="$HOST"
[[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]] && URL_HOST="127.0.0.1"
URL="http://${URL_HOST}:${PORT}"

# --------------------------------------------------------------------------
# 后台探测 USB 设备
#
# system_profiler 通常要跑 1~3 秒, 而后面的 uv sync 也要花时间。
# 把它甩到后台与 sync 并行, 结束时再收割, 省掉这段串行等待。
# --------------------------------------------------------------------------

USB_RESULT="$(mktemp -t cw-usb)"
trap 'rm -f "$USB_RESULT"' EXIT

if [[ "$(uname -s)" == "Darwin" ]]; then
    (
        if system_profiler SPUSBDataType 2>/dev/null \
            | grep -qiE 'i1 ?Pro|i1Display|ColorMunki|X-Rite'; then
            echo found > "$USB_RESULT"
        else
            echo absent > "$USB_RESULT"
        fi
    ) &
    USB_PID=$!
else
    echo skip > "$USB_RESULT"
    USB_PID=""
fi

# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------

step "环境检查"

command -v uv >/dev/null 2>&1 || {
    printf '%s错误:%s 未找到 uv\n' "$C_RED" "$C_OFF" >&2
    hint "brew install uv"
    hint "或 curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
}
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ArgyllCMS 的探测逻辑是 config.py 的职责(优先级: ARGYLL_BIN > PATH > 常见位置),
# 这里不重复实现, 只在肉眼可见地缺失时提前给出更友好的提示。
if [[ -z "${ARGYLL_BIN:-}" ]] && ! command -v spotread >/dev/null 2>&1; then
    warn "PATH 中没有 spotread, 稍后将由 config.py 在常见安装位置继续查找"
fi

# 端口占用
port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1; }

if PID="$(port_pid "$PORT")" && [[ -n "$PID" ]]; then
    CMD="$(ps -o command= -p "$PID" 2>/dev/null || true)"
    if [[ "$CMD" == *server.py* ]]; then
        if (( RESTART )); then
            info "  端口 $PORT 上有旧的 Color Workbench (PID $PID), 正在停止..."
            # SIGTERM 而非 SIGKILL: server.py 的 handler 需要机会去关闭
            # dispcal 的全屏测试窗口, 硬杀会把窗口留在屏幕上
            kill -TERM "$PID" 2>/dev/null || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$PID" 2>/dev/null || break
                sleep 0.3
            done
            kill -0 "$PID" 2>/dev/null && die "旧进程 $PID 未能退出, 请手动处理"
            ok "旧进程已停止"
        else
            printf '%s错误:%s 端口 %s 已被另一个 Color Workbench 占用 (PID %s)\n' \
                "$C_RED" "$C_OFF" "$PORT" "$PID" >&2
            hint "直接访问 $URL, 或用 ./start.sh --restart 重启它"
            exit 1
        fi
    else
        printf '%s错误:%s 端口 %s 被其他程序占用 (PID %s: %s)\n' \
            "$C_RED" "$C_OFF" "$PORT" "$PID" "${CMD:0:60}" >&2
        hint "换个端口: ./start.sh --port 9000"
        exit 1
    fi
else
    ok "端口 $PORT 可用"
fi

# i1Profiler 抢占设备 —— 非致命, 但连不上设备时这是头号原因
if pgrep -x i1Profiler >/dev/null 2>&1; then
    warn "检测到 i1Profiler 正在运行, 它会独占分光光度计"
    hint "测量前请先退出 i1Profiler"
fi

# --------------------------------------------------------------------------
# 同步环境
# --------------------------------------------------------------------------

if (( DO_SYNC )); then
    step "同步 Python 环境"
    if uv sync --group dev --quiet; then
        ok "依赖就绪"
    elif [[ -x .venv/bin/python ]]; then
        # 产线机器常常没有外网。已有 .venv 时不该因为同步失败就拒绝启动。
        warn "uv sync 失败(离线?), 沿用已有的 .venv 继续"
    else
        die "uv sync 失败, 且没有可用的 .venv"
    fi
fi

# --------------------------------------------------------------------------
# 收割 USB 探测结果
# --------------------------------------------------------------------------

[[ -n "$USB_PID" ]] && wait "$USB_PID" 2>/dev/null || true
case "$(cat "$USB_RESULT" 2>/dev/null)" in
    found)  ok "USB 上检测到 X-Rite 设备" ;;
    absent) warn "USB 上没有检测到 X-Rite 设备, 服务仍会启动但无法测量" ;;
esac

# --------------------------------------------------------------------------
# ArgyllCMS 自检 —— 交给 config.py, 它是唯一的权威
# --------------------------------------------------------------------------

step "ArgyllCMS 自检"

if (( CHECK_ONLY )); then
    uv run --no-sync python config.py
    exit 0
fi

if ! uv run --no-sync python -c 'import config, sys; sys.exit(0 if config.ARGYLL_BIN else 1)'; then
    printf '%s错误:%s 未找到 ArgyllCMS\n' "$C_RED" "$C_OFF" >&2
    uv run --no-sync python config.py >&2 || true
    hint "brew install argyll-cms"
    hint "或 ./start.sh --argyll-bin /path/to/Argyll/bin"
    exit 1
fi
ok "ArgyllCMS $(uv run --no-sync python -c 'import config; print(config.argyll_version() or "版本未知")')"

# --------------------------------------------------------------------------
# 打开浏览器
#
# 必须等服务真正 listen 之后再开, 否则浏览器会先撞上一个连接失败页,
# 用户看到的是 ERR_CONNECTION_REFUSED 而不是工作台。
# --------------------------------------------------------------------------

if (( OPEN_BROWSER )) && command -v open >/dev/null 2>&1; then
    (
        for _ in $(seq 1 100); do   # 最多等 20 秒
            if curl -fsS -o /dev/null --max-time 1 "$URL/api/status" 2>/dev/null; then
                open "$URL"
                exit 0
            fi
            sleep 0.2
        done
    ) &
    disown 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# 启动
#
# exec: 用 server.py 顶替掉当前 shell。
# 这样 Ctrl-C 的 SIGINT 直接送达 Python, 由它去停会话、关测试窗口、清理 pty;
# 退出码也原样透传, 便于外层(launchd / CI / .command 包装)判断。
# --------------------------------------------------------------------------

printf '\n'
rm -f "$USB_RESULT"
trap - EXIT

exec uv run --no-sync python server.py
