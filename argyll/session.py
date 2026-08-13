#!/usr/bin/env python3
"""pty 会话内核 —— 驱动 ArgyllCMS 的交互式命令行工具。

## 为什么必须用 pty

ArgyllCMS 的工具(spotread / dispcal / chartread)是交互式 TTY 程序: 它们会调用
``tcgetattr`` / ``tcsetattr`` 把终端切到 raw 模式, 以便读取**不带回车的单个按键**
(空格触发测量、q 退出、h 切换高分辨率模式)。

如果用 ``subprocess.PIPE`` 接管它们的 stdio, 由于管道不是终端, 会直接报::

    next_con_char: tcgetattr failed with 'Operation not supported by device' on stdin

并且拒绝进入交互流程。所以在 Mac/Linux 下必须用 ``pty.openpty()``,
在 Windows 下用 ``pywinpty`` 分配真正的伪终端。
"""

from __future__ import annotations

import abc
import contextlib
import queue
import signal
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import config

if sys.platform == "win32":
    try:
        from pywinpty import PTY
    except ImportError:
        PTY = None
else:
    PTY = None
    import errno
    import fcntl
    import os
    import pty
    import select
    import struct
    import termios

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PTY_ROWS = 50
PTY_COLS = 160
SUBSCRIBER_QUEUE_SIZE = 2048

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
    IDLE = "idle"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"


@dataclass(slots=True)
class Event:
    kind: str
    payload: Any
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload, "seq": self.seq, "ts": self.ts}


class SessionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 会话基类
# --------------------------------------------------------------------------


class BasePtySession(abc.ABC):
    """会话基类，抽象出跨平台的终端管理逻辑。"""

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

        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._buffer: list[str] = []
        self._buffer_chars = 0
        self._line_carry = ""
        self._seq = 0
        self._reader: threading.Thread | None = None
        self._line_observers = list(line_observers)

    @staticmethod
    def _default_env() -> dict[str, str]:
        env = dict(os.environ if "os" in sys.modules else __import__("os").environ)
        env["TERM"] = "xterm-256color"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["COLUMNS"] = str(PTY_COLS)
        env["LINES"] = str(PTY_ROWS)
        return env

    def subscribe(self) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, kind: str, payload: Any) -> None:
        with self._lock:
            self._seq += 1
            event = Event(kind=kind, payload=payload, seq=self._seq)
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def start(self) -> None:
        with self._lock:
            if self.state is not SessionState.IDLE:
                raise SessionError(f"会话已处于 {self.state} 状态, 不能重复启动")
            try:
                self._spawn()
            except Exception as exc:
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

    @abc.abstractmethod
    def _spawn(self) -> None:
        pass

    @abc.abstractmethod
    def _read_loop(self) -> None:
        pass

    def _on_text(self, text: str) -> None:
        with self._lock:
            self._buffer.append(text)
            self._buffer_chars += len(text)
            while self._buffer_chars > config.MAX_BUFFER_CHARS and len(self._buffer) > 1:
                self._buffer_chars -= len(self._buffer.pop(0))

        self._emit("output", text)

        normalized = (self._line_carry + text).replace("\r\n", "\n").replace("\r", "\n")
        *lines, self._line_carry = normalized.split("\n")

        for line in lines:
            self._emit("line", line)
            self._run_observers(line)

    def _run_observers(self, line: str) -> None:
        for observer in self._line_observers:
            try:
                produced = observer(line)
            except Exception as exc:  # noqa: BLE001 - 解析器崩溃不应带走会话
                self._emit("error", {"where": "observer", "detail": str(exc)})
                continue
            for item in produced or ():
                self._emit("parsed", item)

    @abc.abstractmethod
    def _finalize(self) -> None:
        pass

    @abc.abstractmethod
    def write(self, data: str) -> None:
        pass

    def send_key(self, key: str) -> str:
        normalized = key.strip().lower()
        if normalized in KEY_MAP:
            seq = KEY_MAP[normalized]
        elif len(key) == 1 and key.isprintable():
            seq = key
        else:
            raise ValueError(f"未知按键: {key!r}")
        self.write(seq)
        return seq

    @abc.abstractmethod
    def terminate(self, *, force: bool = False) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
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
# POSIX 会话引擎 (Mac / Linux)
# --------------------------------------------------------------------------


class PosixSession(BasePtySession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pid: int | None = None
        self._fd: int | None = None

    def _spawn(self) -> None:
        master_fd, slave_fd = pty.openpty()
        packed = struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, packed)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()

        if pid == 0:
            try:
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(str(self.cwd))
                os.execve(self.argv[0], self.argv, self.env)  # noqa: S606 - argv[0] 来自白名单绝对路径
            except BaseException:  # noqa: BLE001 - 子进程必须吞掉一切并退出
                with contextlib.suppress(OSError):
                    os.write(2, b"\r\n[session] exec failed\r\n")
            os._exit(127)

        os.close(slave_fd)
        self._pid = pid
        self._fd = master_fd

    def _read_loop(self) -> None:
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
                    if not self._is_alive():
                        break
                    continue

                try:
                    chunk = os.read(fd, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
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

    def _is_alive(self) -> bool:
        if self._pid is None:
            return False
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return pid == 0

    def _finalize(self) -> None:
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

    def write(self, data: str) -> None:
        with self._lock:
            if self.state is not SessionState.RUNNING or self._fd is None:
                raise SessionError("会话未在运行, 无法写入")
            fd = self._fd
        try:
            os.write(fd, data.encode("utf-8"))
        except OSError as exc:
            raise SessionError(f"写入失败: {exc}") from exc

    def terminate(self, *, force: bool = False) -> None:
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
        try:
            pgid = os.getpgid(pid)
        except OSError:
            return

        own_pgid = os.getpgid(0)
        if pgid == pid and pgid != own_pgid:
            with contextlib.suppress(OSError):
                os.killpg(pgid, sig)
                return

        with contextlib.suppress(OSError):
            os.kill(pid, sig)


# --------------------------------------------------------------------------
# Windows 会话引擎
# --------------------------------------------------------------------------


class WinSession(BasePtySession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pty = None

    def _spawn(self) -> None:
        if PTY is None:
            raise OSError("Windows 平台缺少 pywinpty 库，请执行 uv sync 安装。")
        import subprocess

        cmdline = subprocess.list2cmdline(self.argv)
        self._pty = PTY(PTY_COLS, PTY_ROWS)
        self._pty.spawn(cmdline=cmdline, cwd=str(self.cwd), env=self.env)

    def _read_loop(self) -> None:
        pty_instance = self._pty
        if pty_instance is None:
            self._finalize()
            return

        try:
            while pty_instance.isalive():
                try:
                    text = pty_instance.read(8192, blocking=True)
                except EOFError:
                    break
                except Exception as exc:  # noqa: BLE001 - 兜底捕获异常避免子线程挂掉
                    self.error = f"读取失败: {exc}"
                    break

                if not text:
                    break

                self._on_text(text)

            with contextlib.suppress(Exception):
                while True:
                    text = pty_instance.read(8192, blocking=False)
                    if not text:
                        break
                    self._on_text(text)
        finally:
            self._finalize()

    def _finalize(self) -> None:
        with self._lock:
            if self.state is not SessionState.RUNNING:
                return

            if self._pty is not None:
                with contextlib.suppress(Exception):
                    self._pty.wait()
                    # pywinpty exit status is not always exposed via a simple method.
                    # If not, default to 0 for graceful exit.
                    if hasattr(self._pty, "get_exitstatus"):
                        self.exit_code = self._pty.get_exitstatus()
                    else:
                        self.exit_code = 0

                with contextlib.suppress(Exception):
                    self._pty.close()
                self._pty = None

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

    def write(self, data: str) -> None:
        with self._lock:
            if self.state is not SessionState.RUNNING or self._pty is None:
                raise SessionError("会话未在运行, 无法写入")
            pty_instance = self._pty
        try:
            pty_instance.write(data)
        except Exception as exc:
            raise SessionError(f"写入失败: {exc}") from exc

    def terminate(self, *, force: bool = False) -> None:
        with self._lock:
            if self.state is not SessionState.RUNNING or self._pty is None:
                return
            pty_instance = self._pty

        with contextlib.suppress(Exception):
            pty_instance.close()


ActiveSession = WinSession if sys.platform == "win32" else PosixSession
PtySession = ActiveSession


# --------------------------------------------------------------------------
# 单例管理器
# --------------------------------------------------------------------------


class SessionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: BasePtySession | None = None

    @property
    def current(self) -> BasePtySession | None:
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
    ) -> BasePtySession:
        with self._lock:
            if self.is_busy():
                running = self._current.label if self._current else "?"
                raise SessionError(f"已有任务 {running!r} 在运行, 请先停止")
            session = ActiveSession(argv, label=label, cwd=cwd, line_observers=line_observers)
            self._current = session

        session.start()
        return session

    def stop(self, *, force: bool = False) -> bool:
        with self._lock:
            session = self._current
        if session is None or session.state is not SessionState.RUNNING:
            return False
        session.terminate(force=force)
        return True

    def shutdown(self) -> None:
        self.stop(force=True)


manager = SessionManager()
