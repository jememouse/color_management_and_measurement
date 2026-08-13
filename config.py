#!/usr/bin/env python3
"""全局配置与 ArgyllCMS 环境探测。

设计原则:
    - 运行时零第三方依赖, 仅使用 Python 标准库
    - 所有路径在此集中定义, 避免散落在各模块中
    - ArgyllCMS 的安装位置在运行时探测, 不硬编码 Homebrew 路径
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WORK_DIR = BASE_DIR / "work"
DOCS_DIR = BASE_DIR / "docs"

WORK_DIR.mkdir(exist_ok=True)

# --------------------------------------------------------------------------
# 服务器
# --------------------------------------------------------------------------

HOST = os.environ.get("I1_HOST", "127.0.0.1")
PORT = int(os.environ.get("I1_PORT", "8721"))

# SSE 心跳间隔(秒)。需低于浏览器/代理的空闲超时, 防止连接被静默切断。
SSE_HEARTBEAT = 15.0

# 单次会话的输出缓冲上限(字符)。超出后丢弃最旧内容, 防止长任务耗尽内存。
MAX_BUFFER_CHARS = 2 * 1024 * 1024

# 等待子进程退出的宽限期(秒), 超时后升级为 SIGKILL。
TERMINATE_GRACE = 3.0

# --------------------------------------------------------------------------
# ArgyllCMS 工具链
# --------------------------------------------------------------------------

# 白名单: 只有列在这里的可执行文件才允许被 spawn。
#
# 这是本项目最重要的安全边界。服务虽然默认只监听 127.0.0.1, 但浏览器里的
# 任意页面都可能向 localhost 发起跨站请求(CSRF), 若不做白名单, 一个构造出来
# 的 POST 就能在用户机器上执行任意程序。参数同样不经过 shell(见 session.py
# 的 execv), 因此不存在注入元字符的风险。
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "spotread",  # 点测量(反射/发光/环境光/透射)
        "dispcal",  # 显示器校准, 产出 .cal
        "dispread",  # 读取显示器色块, 产出 .ti3
        "dispwin",  # 测试窗口 / ICC 安装卸载
        "targen",  # 生成测试色块表 .ti1
        "printtarg",  # 排版打印色卡
        "chartread",  # 扫描读取实体色卡, 产出 .ti3
        "colprof",  # 由 .ti3 生成 ICC profile
        "profcheck",  # 校验 profile 精度
        "ccxxmake",  # 生成 CCSS/CCMX 校正矩阵
        "iccdump",  # 转储 ICC 内容
        "average",  # 多次测量取平均
    }
)


def find_argyll_bin() -> tuple[Path | None, list[str]]:
    """定位 ArgyllCMS 的 bin 目录。

    优先级:
        1. 环境变量 ARGYLL_BIN (显式覆盖)
        2. PATH 中的 spotread 所在目录
        3. 常见安装位置兜底

    Returns:
        (bin_dir, 诊断信息列表)。bin_dir 为 None 表示未找到。
    """
    notes: list[str] = []

    if env_bin := os.environ.get("ARGYLL_BIN"):
        candidate = Path(env_bin)
        if (candidate / "spotread").is_file():
            return candidate, [f"使用环境变量 ARGYLL_BIN: {candidate}"]
        notes.append(f"环境变量 ARGYLL_BIN={env_bin} 无效(未找到 spotread), 已忽略")

    if which := shutil.which("spotread"):
        # Homebrew 的 bin 是 symlink, 解析到真实路径更稳妥
        real = Path(which).resolve()
        return real.parent, [*notes, f"从 PATH 定位: {real}"]

    for cand in (
        Path("/opt/homebrew/bin"),  # Apple Silicon Homebrew
        Path("/usr/local/bin"),  # Intel Homebrew
        Path("/Applications/ArgyllCMS/bin"),  # 官方 tarball 手动安装
        Path.home() / "Argyll" / "bin",
    ):
        if (cand / "spotread").is_file():
            return cand, [*notes, f"在常见位置找到: {cand}"]

    notes.append("未找到 ArgyllCMS。请执行 `brew install argyll-cms` 或设置 ARGYLL_BIN")
    return None, notes


ARGYLL_BIN, ARGYLL_NOTES = find_argyll_bin()


def tool_path(name: str) -> str:
    """把工具名解析为绝对路径, 同时执行白名单校验。

    返回绝对路径而非依赖 PATH, 是为了避免 PATH 劫持 —— 即便进程的 PATH
    被污染, 也只会执行我们探测到的那个 ArgyllCMS。

    Raises:
        ValueError: 工具不在白名单中。
        RuntimeError: ArgyllCMS 未安装, 或该工具在安装目录中缺失。
    """
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"工具 {name!r} 不在白名单中")
    if ARGYLL_BIN is None:
        raise RuntimeError("未找到 ArgyllCMS 安装位置")
    path = ARGYLL_BIN / name
    if not path.is_file():
        raise RuntimeError(f"工具 {name} 不存在于 {ARGYLL_BIN}")
    return str(path)


def available_tools() -> dict[str, bool]:
    """返回白名单中各工具的实际可用性, 用于前端置灰不可用功能。"""
    if ARGYLL_BIN is None:
        return dict.fromkeys(sorted(ALLOWED_TOOLS), False)
    return {name: (ARGYLL_BIN / name).is_file() for name in sorted(ALLOWED_TOOLS)}


def argyll_version() -> str | None:
    """从 spotread 的 usage 输出中提取版本号。

    ArgyllCMS 没有 --version 选项, 版本号印在 usage 首行,
    形如 "Measure spot values, Version 3.5.0"。传 -? 会打印 usage
    并以非零码退出 —— 这是预期行为, 不视作错误。
    """
    if ARGYLL_BIN is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - 路径来自白名单校验
            [tool_path("spotread"), "-?"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"Version\s+(\d+\.\d+(?:\.\d+)?)", blob)
    return m.group(1) if m else None


def environment_report() -> dict[str, object]:
    """汇总环境信息, 供 /api/status 与命令行自检共用。"""
    return {
        "python": sys.version.split()[0],
        "argyll_bin": str(ARGYLL_BIN) if ARGYLL_BIN else None,
        "argyll_version": argyll_version(),
        "argyll_notes": ARGYLL_NOTES,
        "tools": available_tools(),
        "work_dir": str(WORK_DIR),
    }


if __name__ == "__main__":
    # 直接运行本文件即可做一次环境自检: uv run python config.py
    report = environment_report()
    print(f"BASE_DIR   : {BASE_DIR}")
    print(f"WORK_DIR   : {report['work_dir']}")
    print(f"ARGYLL_BIN : {report['argyll_bin']}")
    for note in ARGYLL_NOTES:
        print(f"  - {note}")
    print(f"Argyll 版本 : {report['argyll_version'] or '未知'}")
    print(f"Python     : {report['python']}")
    print("\n工具可用性:")
    for name, ok in available_tools().items():
        print(f"  {name:<12} {'OK' if ok else '缺失'}")
