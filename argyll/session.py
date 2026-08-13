#!/usr/bin/env python3
"""pty 会话内核 —— 驱动 ArgyllCMS 的交互式命令行工具。

## 为什么必须用 pty

ArgyllCMS 的工具(spotread / dispcal / chartread)是交互式 TTY 程序: 它们会调用
``tcgetattr`` / ``tcsetattr`` 把终端切到 raw 模式, 以便读取**不带回车的单个按键**
(空格触发测量、q 退出、h 切换高分辨率模式)。

如果用 ``subprocess.PIPE`` 接管它们的 stdio, 由于管道不是终端, 会直接报::

    next_con_char: tcgetattr failed with 'Operation not supported by device' on stdin

并且拒绝进入交互流程。所以必须用 ``pty.openpty()`` 分配一对真正的伪终端,
让子进程认为自己连着终端。

## 并发模型

同一时刻只允许一个会话运行 —— 这不是实现上的偷懒, 而是硬件约束:
分光光度计是独占设备, 两个 ArgyllCMS 进程同时打开 USB 句柄必然失败。
``SessionManager`` 因此是进程级单例。

输出通过"广播 + 订阅"分发给任意多个 SSE 客户端, 每个订阅者持有独立队列,
慢客户端不会阻塞快客户端(队列满时丢弃最旧事件并打标记)。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pty
import queue
import select
import signal
import struct
import termios
import threading
import time
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import config

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 伪终端的窗口尺寸。ArgyllCMS 会按终端宽度折行并渲染进度条,
#: 给足宽度可避免它把一行拆成难以解析的多行。
PTY_ROWS = 50
PTY_COLS = 160

#: 单个订阅者队列的容量。超出说明该客户端消费不过来(通常是标签页在后台被节流),
#: 此时丢弃最旧事件而非阻塞广播线程。
SUBSCRIBER_QUEUE_SIZE = 2048

#: 语义按键名 -> 实际写入 pty 的字节序列。
#:
#: 前端只传语义名, 不传原始控制字符 —— 这样既避免了在 JSON 里转义控制码的麻烦,
#: 也让"能发什么键"成为一份可审计的白名单。
KEY_MAP: dict[str, str] = {
    "space": " ",
    "enter": "\r",
    "return": "\r",
    "esc": "\x1b",
    "escape": "\x1b",
    "tab": "\t",
    "backspace": "\x7f",
    "delete": "\x7f",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-z": "\x1a",
}


class SessionState(StrEnum):
    """会话生命周期。"""

    IDLE = "idle"  # 从未启动, 或上一次已被清理
    RUNNING = "running"  # 子进程存活
    EXITED = "exited"  # 子进程正常结束(可能退出码非零)
    FAILED = "failed"  # 启动阶段就失败(找不到工具、fork 失败等)


@dataclass(slots=True)
class Event:
    """广播给订阅者的一条事件。

    Attributes:
        kind: output(原始输出块) | line(完整行) | state(状态变更)
              | exit(进程退出) | parsed(解析出的结构化数据) | error(内部错误)
        payload: 随 kind 而定的负载
        seq: 单调递增序号, 供前端断线重连后去重
        ts: Unix 时间戳
    """

    kind: str
    payload: Any
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload, "seq": self.seq, "ts": self.ts}


class SessionError(RuntimeError):
    """会话操作失败(重复启动、向已结束的会话写入等)。"""


# --------------------------------------------------------------------------
# 底层 pty 辅助
# --------------------------------------------------------------------------


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """设置伪终端窗口尺寸。

    必须在 exec **之前**对 slave 端设置: 子进程一启动就会读取尺寸,
    晚设置会有竞态, 导致首屏输出按 80x24 折行。
    """
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def _spawn_in_pty(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, int]:
    """在伪终端中启动进程。

    这里手动 ``openpty`` + ``fork`` 而不用 ``pty.fork()``, 是为了能在 exec 前
    设置窗口尺寸 —— ``pty.fork()`` 不暴露 slave fd, 只能事后对 master 设置,
    存在竞态。

    Returns:
        (pid, master_fd)
    """
    master_fd, slave_fd = pty.openpty()
    _set_winsize(slave_fd, PTY_ROWS, PTY_COLS)

    # Python 3.12+ 会对多线程进程中的 fork() 发 DeprecationWarning, 因为
    # fork 只复制调用线程, 其他线程持有的锁会在子进程中永远处于锁定态。
    # 这里安全的原因是: fork 之后子进程只调用 async-signal-safe 的 os.* 系统
    # 调用, 随即 execve 换掉整个地址空间, 从不触碰任何需要加锁的解释器结构。
    # 这是 fork+exec 的标准安全模式, 也是唯一能同时完成 setsid + TIOCSCTTY 的
    # 途径(os.posix_spawn 无法设置控制终端)。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        # ---------------- 子进程 ----------------
        # 此处禁止抛出异常或使用 logging: fork 之后父进程的锁状态是不确定的,
        # 任何可能获取锁的操作都可能死锁。只用 os.* 系统调用, 且必须以
        # os._exit() 结束, 绝不能让异常传播回 Python 解释器 —— 否则会出现
        # 两个进程同时跑同一份解释器状态。
        try:
            os.close(master_fd)
            os.setsid()  # 脱离原会话, 成为新会话首进程

            # 把 slave 设为控制终端。macOS 要求第三个参数为 0。
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)

            os.chdir(str(cwd))
            os.execve(argv[0], argv, env)  # noqa: S606 - argv[0] 来自白名单绝对路径
        except BaseException:  # noqa: BLE001 - 子进程必须吞掉一切并退出
            # 这里刻意不用 contextlib.suppress: fork 之后只使用 os.* 系统调用,
            # 避免任何可能触碰解释器内部锁的操作。
            try:  # noqa: SIM105
                os.write(2, b"\r\n[session] exec failed\r\n")
            except OSError:
                pass
        os._exit(127)

    # ---------------- 父进程 ----------------
    os.close(slave_fd)
    return pid, master_fd


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


class PtySession:
    """一次 ArgyllCMS 工具的运行。

    线程安全: 所有公开方法都可从任意线程调用。内部用一把可重入锁保护状态,
    读线程是唯一写 ``_buffer`` 的线程。
    """

    def __init__(
        self,
        argv: list[str],
        *,
        label: str = "",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        line_observers: Iterable[Callable[[str], Iterable[dict[str, Any]] | None]] = (),
    ) -> None:
        self.argv = argv
        self.label = label or Path(argv[0]).name
        self.cwd = cwd or config.WORK_DIR
        self.env = env or self._default_env()
        self.state: SessionState = SessionState.IDLE
        self.exit_code: int | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None

        self._pid: int | None = None
        self._fd: int | None = None
        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._line_carry = ""  # 跨读取块残留的半行
        self._seq = 0
        self._reader: threading.Thread | None = None
        self._line_observers = list(line_observers)

    # ---------------- 环境 ----------------

    @staticmethod
    def _default_env() -> dict[str, str]:
        """构造子进程环境。

        显式设置 TERM 与 LC_ALL: ArgyllCMS 会按 TERM 决定是否输出控制序列,
        而 locale 影响小数点符号(某些 locale 用逗号), 会破坏数值解析。
        """
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["COLUMNS"] = str(PTY_COLS)
        env["LINES"] = str(PTY_ROWS)
        return env

    # ---------------- 订阅 ----------------

    def subscribe(self) -> queue.Queue[Event]:
        """注册一个订阅者, 返回其专属队列。"""
        q: queue.Queue[Event] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, kind: str, payload: Any) -> None:
        """向所有订阅者广播一条事件。"""
        with self._lock:
            self._seq += 1
            event = Event(kind=kind, payload=payload, seq=self._seq)
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 慢客户端: 丢弃最旧的一条腾出空间, 保证实时性优先于完整性。
                # 前端可凭 seq 跳变察觉丢失, 必要时重新拉取 snapshot。
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    # ---------------- 启动 ----------------

    def start(self) -> None:
        """启动子进程并拉起读线程。

        Raises:
            SessionError: 会话已被启动过(一个实例只能跑一次)。
        """
        with self._lock:
            if self.state is not SessionState.IDLE:
                raise SessionError(f"会话已处于 {self.state} 状态, 不能重复启动")
            try:
                self._pid, self._fd = _spawn_in_pty(self.argv, self.cwd, self.env)
            except OSError as exc:
                self.state = SessionState.FAILED
                self.error = f"启动失败: {exc}"
                self._emit("state", {"state": self.state, "error": self.error})
                raise SessionError(self.error) from exc

            self.state = SessionState.RUNNING
            self.started_at = time.time()

        self._emit(
            "state",
            {"state": self.state, "label": self.label, "argv": self.argv, "cwd": str(self.cwd)},
        )

        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-{self.label}", daemon=True
        )
        self._reader.start()

    # ---------------- 读循环 ----------------

    def _read_loop(self) -> None:
        """读取 pty 输出直到 EOF, 然后回收子进程。

        在 macOS/Linux 上, 当子进程结束、slave 端全部关闭后, 对 master 的 read
        会抛 ``OSError(EIO)`` 而不是返回空字节 —— 这是 pty 的正常 EOF 语义,
        必须当作正常结束处理, 否则会被误报成错误。
        """
        # 不用 assert 做类型收窄: python -O 会移除断言, 那时 fd 为 None
        # 会在 select 里炸成难以定位的 TypeError。
        fd = self._fd
        if fd is None:
            self._finalize()
            return

        try:
            while True:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    break

                if not ready:
                    # 无数据: 顺带检查子进程是否已消失(极少数情况下 EIO 不会到来)
                    if not self._is_alive():
                        break
                    continue

                try:
                    chunk = os.read(fd, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break  # 正常 EOF
                    if exc.errno == errno.EINTR:
                        continue
                    self.error = f"读取失败: {exc}"
                    break

                if not chunk:
                    break

                text = chunk.decode("utf-8", errors="replace")
                self._on_text(text)
        finally:
            self._finalize()

    def _on_text(self, text: str) -> None:
        """处理一块新输出: 落入缓冲、广播、并按行喂给观察者。"""
        with self._lock:
            self._buffer.append(text)
            self._buffer_chars += len(text)
            # 超出上限时从头丢弃整块, 保持 O(1) 摊还成本
            while self._buffer_chars > config.MAX_BUFFER_CHARS and len(self._buffer) > 1:
                self._buffer_chars -= len(self._buffer.pop(0))

        self._emit("output", text)

        # ArgyllCMS 用裸 \r 做进度条原地刷新, 用 \n 结束真正的行。
        # 两者都当作行边界, 否则进度信息会一直卡在 carry 里出不来。
        normalized = (self._line_carry + text).replace("\r\n", "\n").replace("\r", "\n")
        *lines, self._line_carry = normalized.split("\n")

        for line in lines:
            self._emit("line", line)
            self._run_observers(line)

    def _run_observers(self, line: str) -> None:
        """把整行喂给解析观察者, 广播它们产出的结构化事件。"""
        for observer in self._line_observers:
            try:
                produced = observer(line)
            except Exception as exc:  # noqa: BLE001 - 解析器崩溃不应带走会话
                self._emit("error", {"where": "observer", "detail": str(exc)})
                continue
            for item in produced or ():
                self._emit("parsed", item)

    # ---------------- 结束 ----------------

    def _is_alive(self) -> bool:
        if self._pid is None:
            return False
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return pid == 0

    def _finalize(self) -> None:
        """回收子进程与 fd, 广播退出事件。可被重复调用(幂等)。"""
        with self._lock:
            if self.state is not SessionState.RUNNING:
                return

            if self._pid is not None:
                try:
                    _, status = os.waitpid(self._pid, 0)
                    if os.WIFEXITED(status):
                        self.exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        self.exit_code = -os.WTERMSIG(status)
                except ChildProcessError:
                    self.exit_code = self.exit_code if self.exit_code is not None else -1

            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None

            # 冲掉最后残留的半行
            if self._line_carry:
                tail, self._line_carry = self._line_carry, ""
            else:
                tail = ""

            self.state = SessionState.EXITED
            self.finished_at = time.time()

        if tail:
            self._emit("line", tail)
            self._run_observers(tail)

        self._emit(
            "exit",
            {
                "exit_code": self.exit_code,
                "duration": (self.finished_at or 0) - (self.started_at or 0),
                "error": self.error,
            },
        )
        self._emit("state", {"state": self.state, "exit_code": self.exit_code})

    # ---------------- 写入 ----------------

    def write(self, data: str) -> None:
        """向子进程写入原始文本。

        Raises:
            SessionError: 会话未在运行。
        """
        with self._lock:
            if self.state is not SessionState.RUNNING or self._fd is None:
                raise SessionError("会话未在运行, 无法写入")
            fd = self._fd
        try:
            os.write(fd, data.encode("utf-8"))
        except OSError as exc:
            raise SessionError(f"写入失败: {exc}") from exc

    def send_key(self, key: str) -> str:
        """发送一个语义按键或单个可打印字符。

        Args:
            key: KEY_MAP 中的语义名(如 "space"), 或单个可打印字符(如 "q")。

        Returns:
            实际写入的序列, 便于调用方回显。

        Raises:
            ValueError: 未知的按键名, 或传入了多字符字符串。
        """
        normalized = key.strip().lower()
        if normalized in KEY_MAP:
            seq = KEY_MAP[normalized]
        elif len(key) == 1 and key.isprintable():
            seq = key
        else:
            raise ValueError(f"未知按键: {key!r}")
        self.write(seq)
        return seq

    def terminate(self, *, force: bool = False) -> None:
        """结束子进程。

        先发 SIGTERM 给整个进程组(ArgyllCMS 的 dispcal 会拉起 dispwin 子进程,
        只杀父进程会留下孤儿测试窗口), 宽限期后升级为 SIGKILL。

        Args:
            force: 直接 SIGKILL, 跳过宽限期。
        """
        with self._lock:
            if self.state is not SessionState.RUNNING or self._pid is None:
                return
            pid = self._pid

        first = signal.SIGKILL if force else signal.SIGTERM
        self._signal_group(pid, first)

        if force:
            return

        deadline = time.monotonic() + config.TERMINATE_GRACE
        while time.monotonic() < deadline:
            if not self._is_alive():
                return
            time.sleep(0.05)

        self._signal_group(pid, signal.SIGKILL)

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        """向子进程所在进程组发信号, 无法确认时退化为只发给该进程。

        **这里有一个能杀死本服务的陷阱**: 子进程在 fork 之后、``setsid()``
        之前的那一小段时间里, 仍然属于*父进程的*进程组。此刻若直接
        ``os.killpg(os.getpgid(pid), sig)``, 杀掉的是我们自己 —— 开发时这会
        让 pytest 静默消失, 线上则是整个服务被自己的停止按钮杀死。

        判据: 子进程 ``setsid()`` 成功后会成为新会话兼新进程组的组长, 此时
        ``pgid == pid``。只有满足该条件、且与本进程组不同, 才允许 killpg。
        """
        try:
            pgid = os.getpgid(pid)
        except OSError:
            return  # 进程已消失

        own_pgid = os.getpgid(0)
        if pgid == pid and pgid != own_pgid:
            # setsid 已完成, 组内是子进程及其派生的 dispwin 等孙子进程
            with contextlib.suppress(OSError):
                os.killpg(pgid, sig)
                return

        # setsid 尚未完成(或失败): 只能精确投递给子进程本身
        with contextlib.suppress(OSError):
            os.kill(pid, sig)

    # ---------------- 查询 ----------------

    def snapshot(self) -> dict[str, Any]:
        """返回会话当前状态与全部已缓冲输出, 供新连入的客户端补齐历史。"""
        with self._lock:
            return {
                "label": self.label,
                "argv": self.argv,
                "cwd": str(self.cwd),
                "state": str(self.state),
                "exit_code": self.exit_code,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "seq": self._seq,
                "output": "".join(self._buffer),
            }


# --------------------------------------------------------------------------
# 单例管理器
# --------------------------------------------------------------------------


class SessionManager:
    """进程级单例, 保证同一时刻只有一个 ArgyllCMS 会话占用测量仪器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: PtySession | None = None

    @property
    def current(self) -> PtySession | None:
        with self._lock:
            return self._current

    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.state is SessionState.RUNNING

    def start(
        self,
        argv: list[str],
        *,
        label: str = "",
        cwd: Path | None = None,
        line_observers: Iterable[Callable[[str], Iterable[dict[str, Any]] | None]] = (),
    ) -> PtySession:
        """启动新会话。

        Raises:
            SessionError: 已有会话在运行。仪器是独占设备, 必须先停掉旧的。
        """
        with self._lock:
            if self.is_busy():
                running = self._current.label if self._current else "?"
                raise SessionError(f"已有任务 {running!r} 在运行, 请先停止")
            session = PtySession(argv, label=label, cwd=cwd, line_observers=line_observers)
            self._current = session

        session.start()
        return session

    def stop(self, *, force: bool = False) -> bool:
        """停止当前会话。返回是否确实停了一个正在运行的会话。"""
        with self._lock:
            session = self._current
        if session is None or session.state is not SessionState.RUNNING:
            return False
        session.terminate(force=force)
        return True

    def shutdown(self) -> None:
        """服务退出时调用, 确保不留下孤儿进程和亮着的测试窗口。"""
        self.stop(force=True)


#: 全局单例。server.py 直接 import 使用。
manager = SessionManager()
