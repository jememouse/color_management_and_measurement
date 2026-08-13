#!/usr/bin/env python3
"""pty 会话内核测试。

用 /bin/echo、/bin/cat 等系统工具代替 ArgyllCMS, 使测试不依赖硬件。

最关键的一条是 ``test_child_sees_a_real_tty`` —— 它验证了本项目的技术前提:
子进程必须认为自己连着终端, 否则 ArgyllCMS 会拒绝进入交互流程。
"""

from __future__ import annotations

import queue
import sys
import time

import pytest

from argyll.session import (
    Event,
    PtySession,
    SessionError,
    SessionManager,
    SessionState,
)


def drain(session: PtySession, timeout: float = 10.0) -> str:
    """等待会话结束并返回全部输出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.state in (SessionState.EXITED, SessionState.FAILED):
            # 读线程可能还在冲刷最后一块, 给它一个调度间隙
            time.sleep(0.05)
            return session.snapshot()["output"]
        time.sleep(0.02)
    session.terminate(force=True)
    pytest.fail(f"会话在 {timeout}s 内未结束")


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------
# 基础生命周期
# --------------------------------------------------------------------------


def test_echo_roundtrip():
    """最简单的一条: 能启动、能拿到输出、退出码为 0。"""
    session = PtySession(["/bin/echo", "hello-workbench"], label="echo")
    session.start()
    output = drain(session)

    assert "hello-workbench" in output
    assert session.state is SessionState.EXITED
    assert session.exit_code == 0


def test_nonzero_exit_code_is_captured():
    session = PtySession(["/bin/sh", "-c", "exit 7"], label="fail")
    session.start()
    drain(session)
    assert session.exit_code == 7


def test_exec_failure_yields_127():
    """可执行文件不存在时, 子进程应以 127 退出而不是把异常抛回父进程。"""
    session = PtySession(["/nonexistent/binary/xyz"], label="missing")
    session.start()
    drain(session)
    assert session.exit_code == 127


def test_cannot_start_twice():
    session = PtySession(["/bin/echo", "x"], label="once")
    session.start()
    drain(session)
    with pytest.raises(SessionError, match="不能重复启动"):
        session.start()


# --------------------------------------------------------------------------
# 核心: 伪终端语义
# --------------------------------------------------------------------------


def test_child_sees_a_real_tty():
    """本项目的技术前提。

    若用 subprocess.PIPE, 三个 isatty 都会是 False, ArgyllCMS 随即报
    tcgetattr failed 并拒绝交互。这里必须全部为 True。
    """
    code = "import sys; print(sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty())"
    session = PtySession([sys.executable, "-c", code], label="isatty")
    session.start()
    output = drain(session)
    assert "True True True" in output


def test_tcgetattr_works_in_child():
    """直接验证 termios 调用可用 —— 这正是之前 spotread 报错的那个系统调用。"""
    code = "import sys, termios; termios.tcgetattr(sys.stdin.fileno()); print('TCGETATTR-OK')"
    session = PtySession([sys.executable, "-c", code], label="termios")
    session.start()
    output = drain(session)
    assert "TCGETATTR-OK" in output


def test_window_size_is_applied():
    """窗口尺寸必须在 exec 前设好, 否则子进程按 80x24 折行。"""
    code = "import os; ts = os.get_terminal_size(); print(f'{ts.columns}x{ts.lines}')"
    session = PtySession([sys.executable, "-c", code], label="winsize")
    session.start()
    output = drain(session)
    assert "160x50" in output


def test_locale_is_forced_to_c():
    """locale 必须为 C —— 某些 locale 用逗号作小数点, 会破坏数值解析。"""
    code = "import os; print('LC=' + os.environ.get('LC_ALL', ''))"
    session = PtySession([sys.executable, "-c", code], label="locale")
    session.start()
    output = drain(session)
    assert "LC=C" in output


# --------------------------------------------------------------------------
# 交互输入
# --------------------------------------------------------------------------


def test_send_key_reaches_child():
    """单键输入: 不带回车也要送达 —— ArgyllCMS 就是这么读按键的。

    子进程必须先把终端切到 raw 模式再读, 否则 tty 处于 canonical 模式,
    read(1) 会一直等到用户敲回车才返回。ArgyllCMS 正是这样做的
    (tcgetattr → tty.setraw → 读单键 → tcsetattr 还原), 测试也照此模拟,
    这样验证的才是真实链路。
    """
    code = (
        "import sys, tty, termios; "
        "fd = sys.stdin.fileno(); "
        "old = termios.tcgetattr(fd); "
        "tty.setraw(fd); "
        "ch = sys.stdin.read(1); "
        "termios.tcsetattr(fd, termios.TCSADRAIN, old); "
        "print(f'GOT[{ch}]', flush=True)"
    )
    session = PtySession([sys.executable, "-c", code], label="key")
    session.start()

    assert wait_for(lambda: session.state is SessionState.RUNNING)
    time.sleep(0.4)  # 等子进程进入 read
    session.send_key("q")

    output = drain(session)
    assert "GOT[q]" in output


def test_send_key_maps_semantic_names():
    session = PtySession(["/bin/cat"], label="map")
    session.start()
    assert session.send_key("space") == " "
    assert session.send_key("enter") == "\r"
    assert session.send_key("esc") == "\x1b"
    session.terminate(force=True)


def test_send_key_rejects_unknown():
    session = PtySession(["/bin/cat"], label="reject")
    session.start()
    with pytest.raises(ValueError, match="未知按键"):
        session.send_key("f13-super")
    session.terminate(force=True)


def test_write_after_exit_raises():
    session = PtySession(["/bin/echo", "done"], label="closed")
    session.start()
    drain(session)
    with pytest.raises(SessionError, match="未在运行"):
        session.write("x")


# --------------------------------------------------------------------------
# 终止
# --------------------------------------------------------------------------


def test_terminate_stops_long_running_process():
    session = PtySession(["/bin/sh", "-c", "sleep 60"], label="sleeper")
    session.start()
    assert wait_for(lambda: session.state is SessionState.RUNNING)

    session.terminate()
    assert wait_for(lambda: session.state is SessionState.EXITED, timeout=8.0), "终止后状态未收敛"


def test_terminate_kills_whole_process_group():
    """dispcal 会拉起 dispwin 子进程; 只杀父进程会留下孤儿测试窗口。"""
    script = "sleep 60 & echo CHILD=$!; wait"
    session = PtySession(["/bin/sh", "-c", script], label="group")
    session.start()

    assert wait_for(lambda: "CHILD=" in session.snapshot()["output"], timeout=5.0)
    child_pid = int(session.snapshot()["output"].split("CHILD=")[1].split()[0])

    session.terminate(force=True)
    assert wait_for(lambda: session.state is SessionState.EXITED, timeout=8.0)

    # 孙子进程应随进程组一起消失
    def gone() -> bool:
        import os

        try:
            os.kill(child_pid, 0)
        except OSError:
            return True
        return False

    assert wait_for(gone, timeout=5.0), f"孙子进程 {child_pid} 未被回收"


# --------------------------------------------------------------------------
# 广播与解析观察者
# --------------------------------------------------------------------------


def test_subscriber_receives_events():
    session = PtySession(["/bin/echo", "broadcast-test"], label="sub")
    q: queue.Queue[Event] = session.subscribe()
    session.start()
    drain(session)

    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait().kind)

    assert "state" in kinds
    assert "output" in kinds
    assert "exit" in kinds


def test_carriage_return_splits_lines():
    """ArgyllCMS 用裸 \\r 做进度条原地刷新, 必须当作行边界。"""
    lines: list[str] = []
    code = r"import sys; sys.stdout.write('A\rB\rC\n'); sys.stdout.flush()"
    session = PtySession(
        [sys.executable, "-c", code],
        label="cr",
        line_observers=[lambda ln: lines.append(ln) or ()],
    )
    session.start()
    drain(session)

    assert "A" in lines and "B" in lines and "C" in lines


def test_observer_output_is_broadcast_as_parsed():
    def observer(line: str):
        if "MARK" in line:
            yield {"type": "hit", "line": line}

    session = PtySession(["/bin/echo", "MARK-42"], label="parse", line_observers=[observer])
    q = session.subscribe()
    session.start()
    drain(session)

    parsed = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.kind == "parsed":
            parsed.append(ev.payload)

    assert parsed == [{"type": "hit", "line": "MARK-42"}]


def test_observer_exception_does_not_kill_session():
    """解析器崩溃不应带走整个会话 —— 测量数据比解析更重要。"""

    def broken(line: str):
        raise ValueError("解析器炸了")

    session = PtySession(["/bin/echo", "still-alive"], label="robust", line_observers=[broken])
    session.start()
    output = drain(session)

    assert "still-alive" in output
    assert session.exit_code == 0


def test_seq_is_monotonic():
    session = PtySession(["/bin/echo", "seq"], label="seq")
    q = session.subscribe()
    session.start()
    drain(session)

    seqs = []
    while not q.empty():
        seqs.append(q.get_nowait().seq)

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# --------------------------------------------------------------------------
# 单例管理器
# --------------------------------------------------------------------------


def test_manager_rejects_concurrent_sessions():
    """仪器是独占设备 —— 第二个任务必须被挡住。"""
    mgr = SessionManager()
    mgr.start(["/bin/sh", "-c", "sleep 30"], label="first")
    assert wait_for(mgr.is_busy)

    with pytest.raises(SessionError, match="已有任务"):
        mgr.start(["/bin/echo", "second"], label="second")

    mgr.shutdown()
    assert wait_for(lambda: not mgr.is_busy(), timeout=8.0)


def test_manager_allows_sequential_sessions():
    mgr = SessionManager()
    first = mgr.start(["/bin/echo", "one"], label="one")
    drain(first)

    second = mgr.start(["/bin/echo", "two"], label="two")
    output = drain(second)
    assert "two" in output


def test_manager_stop_returns_false_when_idle():
    mgr = SessionManager()
    assert mgr.stop() is False


def test_signal_group_never_kills_own_process_group():
    """回归测试: 终止逻辑绝不能杀掉自己所在的进程组。

    子进程在 fork 之后、setsid 之前仍属于*父进程的*进程组。早期实现直接
    ``os.killpg(os.getpgid(pid), sig)``, 在这个竞态窗口里会把本进程一起杀掉 ——
    开发时表现为 pytest 静默消失(无任何输出、退出码 1), 线上则是服务被自己的
    停止按钮杀死。

    这里刻意构造那个窗口: fork 一个**不** setsid 的子进程, 它与我们同组。
    若 _signal_group 退化正确, 只有子进程会死; 若回归, 整个测试进程组陪葬。
    """
    import os
    import signal as sig_mod

    pid = os.fork()
    if pid == 0:
        # 子进程: 不调用 setsid, 刻意与父进程同组
        try:
            time.sleep(30)
        finally:
            os._exit(0)

    try:
        # 前置条件: 确实处于危险的同组状态
        assert os.getpgid(pid) == os.getpgid(0), "测试前提不成立: 子进程未与父进程同组"

        PtySession._signal_group(pid, sig_mod.SIGKILL)

        # 走到这里就说明我们没被自己杀掉
        reaped, status = os.waitpid(pid, 0)
        assert reaped == pid
        assert os.WIFSIGNALED(status), "子进程应被信号终止"
    finally:
        with __import__("contextlib").suppress(ChildProcessError, OSError):
            os.kill(pid, sig_mod.SIGKILL)
            os.waitpid(pid, os.WNOHANG)
