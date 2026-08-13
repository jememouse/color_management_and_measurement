#!/usr/bin/env python3
"""校准光强诊断 —— 定位 'Light level is too low' 的具体原因。

用法::

    uv run python diagnose_light.py

脚本会以高 debug 级别启动 spotread, 自动触发一次白板校准, 并从原始传感器
读数判断问题出在哪一环:

    完全无光    -> 光路被彻底挡住(环境光扩散帽未取下 / 仪器不在底座上)
    有光但很弱  -> 白板脏污、仪器未压紧、或位置偏移
    光强正常    -> 问题不在光路, 需看其他错误
"""

from __future__ import annotations

import re
import sys
import time

import config
from argyll.session import PtySession, SessionState

PREPARE_SECONDS = 15


def main() -> int:
    print("=" * 66)
    print("  i1 Pro 2 校准光强诊断")
    print("=" * 66)
    print()
    print("请先完成以下准备:")
    print("  1. 取下测量口上的环境光扩散帽(若有) —— 反射测量必须裸露测量口")
    print("  2. 把仪器扣进白色校准底座, 推到卡住")
    print("  3. 确认白板洁净无指纹")
    print()
    for remaining in range(PREPARE_SECONDS, 0, -1):
        sys.stdout.write(f"\r  {remaining} 秒后自动开始…  ")
        sys.stdout.flush()
        time.sleep(1)
    print("\r  开始诊断              \n")

    argv = [config.tool_path("spotread"), "-v", "-c", "1", "-s", "-D5"]
    session = PtySession(argv, label="diagnose")
    session.start()

    triggered = False
    deadline = time.time() + 60
    while time.time() < deadline and session.state is SessionState.RUNNING:
        time.sleep(0.4)
        out = session.snapshot()["output"]
        if not triggered and ("hit any key" in out.lower() or "to continue" in out.lower()):
            time.sleep(0.6)
            session.send_key("space")
            triggered = True
        if "Calibration complete" in out or "Calibration failed" in out:
            time.sleep(1.5)
            break

    if session.state is SessionState.RUNNING:
        session.send_key("q")
        time.sleep(0.8)
        session.terminate(force=True)

    out = session.snapshot()["output"]
    return analyse(out)


def analyse(out: str) -> int:
    print("=" * 66)
    print("  诊断结果")
    print("=" * 66)

    # 传感器原始读数: 高 debug 下会打印各类 raw/sens 数值
    numbers: list[float] = []
    for pattern in (
        r"Dark threshold\s*=\s*([\d.]+)",
        r"absraw\[\d+\]\s*=\s*([\d.]+)",
        r"sens\[\d+\]\s*=\s*([\d.]+)",
        r"maxval\s*=\s*([\d.]+)",
        r"White reference.*?([\d.]+)",
    ):
        numbers.extend(float(m) for m in re.findall(pattern, out))

    saw_low = "Light level is too low" in out
    saw_ok = "Calibration complete" in out
    lamp_warn = any(k in out for k in ("Lamp is weak", "Lamp marginal", "Lamp has failed"))

    print(f"  校准成功        : {'是' if saw_ok else '否'}")
    print(f"  报告光强不足    : {'是' if saw_low else '否'}")
    print(f"  灯管告警        : {'是' if lamp_warn else '否'}")
    print(f"  捕获到的传感器值: {len(numbers)} 个")

    if numbers:
        peak = max(numbers)
        print(f"  峰值读数        : {peak:.2f}")
        print()
        if peak < 1.0:
            print("  → 传感器几乎没有收到光。")
            print("    最可能: 环境光扩散帽未取下, 或仪器根本不在校准底座上。")
        elif peak < 100:
            print("  → 有光但极弱。")
            print("    最可能: 测量口被部分遮挡, 或仪器未压到位、存在漏光缝隙。")
        else:
            print("  → 光强量级看起来正常, 问题可能不在光路。")
            print("    建议把下面的完整输出发出来进一步分析。")
    else:
        print()
        print("  → 未捕获到传感器数值(debug 输出格式可能与预期不同)。")
        print("    请把下面的完整输出发出来。")

    if lamp_warn:
        print()
        print("  ⚠ 仪器自报灯管状态异常 —— 这是厂商固件的判断, 优先级高于时长推算。")

    log = config.WORK_DIR / "diagnose_light.log"
    log.write_text(out, encoding="utf-8")
    print()
    print(f"  完整输出已保存: {log}")
    print()
    print("-" * 66)
    print("输出尾部:")
    print("-" * 66)
    print(out[-1500:])
    return 0 if saw_ok else 1


if __name__ == "__main__":
    sys.exit(main())
