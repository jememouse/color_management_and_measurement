#!/usr/bin/env python3
"""HTTP + SSE 服务 —— 把 ArgyllCMS 会话暴露给浏览器。

启动::

    uv run python server.py

## 安全模型

服务默认只绑定 127.0.0.1, 但"只监听本地"并不等于安全: 用户浏览器里打开的
**任意网页**都能向 http://127.0.0.1:8721 发请求。因此这里做了两道防护:

1. **Host 校验** —— 挡住 DNS rebinding。攻击者把自己的域名解析到 127.0.0.1,
   浏览器就会带着攻击者的 Origin 访问本服务; 校验 Host 头只接受 localhost
   形式可以断掉这条路。
2. **自定义请求头** —— 所有写操作必须带 ``X-Workbench: 1``。跨站的简单请求
   无法附加自定义头, 加了就会触发 CORS 预检, 而本服务不返回任何 CORS 头,
   预检必然失败。这是无状态 CSRF 防护里最省事且可靠的一种。

再加上 ``tools.py`` 的工具白名单与路径校验, 构成完整边界。
"""

from __future__ import annotations

import json
import queue
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import config
from argyll import session as session_mod
from argyll import tools
from argyll.parser import ArgyllParser
from argyll.session import SessionError, SessionState

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 请求体上限。最大的合法请求也就是一份参数字典, 64KB 绰绰有余。
MAX_BODY_BYTES = 64 * 1024

#: 写操作必须携带的请求头 —— CSRF 防护, 见模块文档。
CSRF_HEADER = "X-Workbench"

#: 允许的 Host 值(不含端口)。用于挡 DNS rebinding。
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

#: 可下载/删除的产物类型。限制扩展名, 避免把服务自身的源码也暴露出去。
DOWNLOADABLE_SUFFIXES = frozenset(
    {".icc", ".icm", ".ti1", ".ti2", ".ti3", ".cal", ".sp", ".ccss", ".ccmx", ".log", ".txt"}
)

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# --------------------------------------------------------------------------
# 会话编排
# --------------------------------------------------------------------------


class Workbench:
    """把会话管理器、解析器与命令构建串起来。

    每次启动会话都新建一个 :class:`ArgyllParser` 并挂成 line observer,
    这样解析状态(比如"正在收集光谱")不会跨会话残留。
    """

    def __init__(self) -> None:
        self.manager = session_mod.manager
        self.parser: ArgyllParser | None = None
        self.last_command: tools.Command | None = None
        self._lock = threading.Lock()

    def start(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """构建命令并启动会话。

        Raises:
            tools.ToolError: 参数非法。
            SessionError: 已有会话在运行。
        """
        command = tools.build(action, params)

        with self._lock:
            parser = ArgyllParser(illuminant=str(params.get("illuminant", "D50")).upper())
            session = self.manager.start(
                command.argv,
                label=command.label,
                line_observers=[parser.feed],
            )
            self.parser = parser
            self.last_command = command

        return {
            "started": True,
            "command": command.to_dict(),
            "session": session.snapshot(),
        }

    def status(self) -> dict[str, Any]:
        session = self.manager.current
        return {
            "busy": self.manager.is_busy(),
            "session": session.snapshot() if session else None,
            "command": self.last_command.to_dict() if self.last_command else None,
        }


workbench = Workbench()


# --------------------------------------------------------------------------
# 请求处理
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ColorWorkbench/0.1"
    protocol_version = "HTTP/1.1"

    # ---------------- 日志 ----------------

    def log_message(self, format: str, *args: Any) -> None:  # 参数名沿用父类签名
        """默认实现往 stderr 打每条请求, SSE 心跳会把终端刷爆。"""
        if self.path.startswith("/api/session/stream"):
            return
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {format % args}\n")

    # ---------------- 响应辅助 ----------------

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_default)
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_error_json(self, status: HTTPStatus, message: str, **extra: Any) -> None:
        self._send_json({"error": message, **extra}, status=status)

    def _read_json_body(self) -> dict[str, Any]:
        """读取并解析 JSON 请求体。

        Raises:
            ValueError: 体积超限或不是合法的 JSON 对象。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError(f"请求体过大 ({length} 字节)")
        if length <= 0:
            return {}

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    # ---------------- 安全校验 ----------------

    def _check_host(self) -> bool:
        """校验 Host 头, 挡住 DNS rebinding 攻击。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip()
        if host in ALLOWED_HOSTS:
            return True
        self._send_error_json(HTTPStatus.FORBIDDEN, f"拒绝的 Host: {host!r}")
        return False

    def _check_csrf(self) -> bool:
        """写操作必须携带自定义头 —— 跨站请求加不上它。"""
        if self.headers.get(CSRF_HEADER):
            return True
        self._send_error_json(
            HTTPStatus.FORBIDDEN,
            f"缺少 {CSRF_HEADER} 请求头 —— 写操作只接受本应用发起的请求",
        )
        return False

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # BaseHTTPRequestHandler 约定的方法名
        if not self._check_host():
            return

        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        try:
            if path == "/api/status":
                self._send_json(self._status_payload())
            elif path == "/api/devices":
                self._send_json(self._devices_payload())
            elif path == "/api/session":
                self._send_json(workbench.status())
            elif path == "/api/session/stream":
                self._stream_events()
            elif path == "/api/files":
                self._send_json({"files": self._list_files()})
            elif path.startswith("/api/files/"):
                self._download_file(path[len("/api/files/") :])
            elif path == "/api/workflow":
                self._send_json({"display_profile": list(tools.DISPLAY_PROFILE_WORKFLOW)})
            elif path == "/api/options":
                self._send_json(self._options_payload())
            elif path.startswith("/api/"):
                self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口: {path}")
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass  # 客户端提前断开, 正常现象
        except Exception as exc:  # noqa: BLE001 - 兜底, 避免单个请求打死线程
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器内部错误: {exc}")

    def do_POST(self) -> None:  # BaseHTTPRequestHandler 约定的方法名
        if not self._check_host() or not self._check_csrf():
            return

        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            if path == "/api/session/start":
                self._start_session(body)
            elif path == "/api/session/key":
                self._send_key(body)
            elif path == "/api/session/text":
                self._send_text(body)
            elif path == "/api/session/stop":
                self._stop_session(body)
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口: {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务器内部错误: {exc}")

    def do_DELETE(self) -> None:  # BaseHTTPRequestHandler 约定的方法名
        if not self._check_host() or not self._check_csrf():
            return

        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/files/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口: {path}")
            return

        try:
            self._delete_file(path[len("/api/files/") :])
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"删除失败: {exc}")

    # ---------------- 接口实现 ----------------

    def _status_payload(self) -> dict[str, Any]:
        report = config.environment_report()
        report["session"] = workbench.status()
        return report

    def _devices_payload(self) -> dict[str, Any]:
        instruments = tools.list_instruments()
        displays = tools.list_displays()
        return {
            "instruments": [i.to_dict() for i in instruments],
            "displays": [d.to_dict() for d in displays],
            # 便于前端直接判断"能不能开始测量"
            "has_instrument": any(i.is_measuring_device for i in instruments),
        }

    def _options_payload(self) -> dict[str, Any]:
        """把后端的选项枚举暴露给前端, 避免两处各写一份而逐渐对不上。"""
        return {
            "measure_modes": sorted(tools.MEASURE_MODES),
            "filter_modes": sorted(tools.FILTER_MODES),
            "display_types": sorted(tools.DISPLAY_TYPES),
            "quality_levels": sorted(tools.QUALITY_LEVELS),
            "profile_algorithms": sorted(tools.PROFILE_ALGORITHMS),
            "observers": sorted(tools.OBSERVERS),
            "illuminants": sorted(tools.ILLUMINANTS),
        }

    def _start_session(self, body: dict[str, Any]) -> None:
        action = body.get("action")
        if not isinstance(action, str):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少 action 字段")
            return

        params = body.get("params") or {}
        if not isinstance(params, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "params 必须是对象")
            return

        try:
            result = workbench.start(action, params)
        except tools.ToolError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except SessionError as exc:
            self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except (RuntimeError, ValueError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json(result)

    def _send_key(self, body: dict[str, Any]) -> None:
        key = body.get("key")
        if not isinstance(key, str):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少 key 字段")
            return

        current = workbench.manager.current
        if current is None or current.state is not SessionState.RUNNING:
            self._send_error_json(HTTPStatus.CONFLICT, "没有正在运行的会话")
            return

        try:
            sent = current.send_key(key)
        except (ValueError, SessionError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json({"sent": True, "bytes": len(sent)})

    def _send_text(self, body: dict[str, Any]) -> None:
        """发送一整行文本(用于 ArgyllCMS 偶尔要求输入文件名/数值的场景)。"""
        text = body.get("text")
        if not isinstance(text, str) or len(text) > 1024:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "text 字段缺失或过长")
            return

        current = workbench.manager.current
        if current is None or current.state is not SessionState.RUNNING:
            self._send_error_json(HTTPStatus.CONFLICT, "没有正在运行的会话")
            return

        try:
            current.write(text if text.endswith("\r") else text + "\r")
        except SessionError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._send_json({"sent": True})

    def _stop_session(self, body: dict[str, Any]) -> None:
        force = bool(body.get("force", False))
        stopped = workbench.manager.stop(force=force)
        self._send_json({"stopped": stopped})

    # ---------------- SSE ----------------

    def _stream_events(self) -> None:
        """Server-Sent Events: 把会话事件实时推给浏览器。

        **必须跟踪会话切换**: 前端通常一进页面就建立 SSE 连接, 那时还没有任何
        会话。若只在连接建立的瞬间订阅一次, 之后启动的会话在这条连接上将永远
        静默 —— 用户点了"开始测量"却什么都看不到。因此循环里持续比对
        ``manager.current``, 一旦换了会话就重新订阅并补发 snapshot。

        每次订阅新会话都先发 snapshot, 使刷新页面或中途接入都能拿到完整上下文。
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        # 不给 Content-Length: 响应体以连接关闭为边界, EventSource 支持这种流式读取
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        tracked: session_mod.PtySession | None = None
        subscription: queue.Queue[session_mod.Event] | None = None
        last_beat = time.monotonic()

        try:
            self._write_sse("hello", {"heartbeat": config.SSE_HEARTBEAT})

            while True:
                current = workbench.manager.current

                # 会话发生切换(首次出现 / 换了新任务)
                if current is not tracked:
                    if tracked is not None and subscription is not None:
                        tracked.unsubscribe(subscription)
                    tracked = current
                    if current is not None:
                        # 先订阅再发 snapshot, 否则两者之间产生的事件会丢失
                        subscription = current.subscribe()
                        self._write_sse("snapshot", current.snapshot())
                    else:
                        subscription = None
                        self._write_sse("snapshot", {"state": "idle", "output": ""})

                event: session_mod.Event | None = None
                if subscription is not None:
                    try:
                        event = subscription.get(timeout=0.5)
                    except queue.Empty:
                        event = None
                else:
                    # 空闲时轻度轮询, 等待会话出现
                    time.sleep(0.25)

                if event is not None:
                    self._write_sse(event.kind, event.to_dict())
                    continue

                # 心跳: 让代理与浏览器知道连接还活着
                if time.monotonic() - last_beat >= config.SSE_HEARTBEAT:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 浏览器关闭标签页 / 刷新
        finally:
            if tracked is not None and subscription is not None:
                tracked.unsubscribe(subscription)

    def _write_sse(self, event_name: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default)
        chunk = f"event: {event_name}\ndata: {data}\n\n".encode()
        self.wfile.write(chunk)
        self.wfile.flush()

    # ---------------- 文件 ----------------

    def _list_files(self) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(config.WORK_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "suffix": path.suffix.lower(),
                    "downloadable": path.suffix.lower() in DOWNLOADABLE_SUFFIXES,
                }
            )
        return entries

    def _resolve_download(self, filename: str) -> Path | None:
        """把请求的文件名解析为 work/ 内的真实路径, 非法则返回 None。"""
        name = filename.strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return None

        path = (config.WORK_DIR / name).resolve()
        if not path.is_relative_to(config.WORK_DIR.resolve()) or not path.is_file():
            return None
        if path.suffix.lower() not in DOWNLOADABLE_SUFFIXES:
            return None
        return path

    def _download_file(self, filename: str) -> None:
        path = self._resolve_download(filename)
        if path is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件不存在或类型不允许下载")
            return

        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(raw)

    def _delete_file(self, filename: str) -> None:
        path = self._resolve_download(filename)
        if path is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件不存在或类型不允许删除")
            return
        path.unlink()
        self._send_json({"deleted": path.name})

    # ---------------- 静态资源 ----------------

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")

        # 路径穿越防护: 解析后必须仍在 static/ 内
        target = (config.STATIC_DIR / rel).resolve()
        if not target.is_relative_to(config.STATIC_DIR.resolve()) or not target.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, f"找不到 {rel}")
            return

        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            STATIC_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(raw)))
        # 本地开发工具, 不缓存以免改完前端还要强刷
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def _json_default(obj: Any) -> Any:
    """JSON 编码兜底: Path 转字符串, 其余转 repr 而不是抛异常。"""
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def make_server(host: str = config.HOST, port: int = config.PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    # 关闭时不等待 SSE 长连接线程 —— 否则 Ctrl-C 后要挂很久
    server.daemon_threads = True
    return server


def main() -> int:
    if config.ARGYLL_BIN is None:
        print("错误: 未找到 ArgyllCMS", file=sys.stderr)
        for note in config.ARGYLL_NOTES:
            print(f"  - {note}", file=sys.stderr)
        print("\n请执行 `brew install argyll-cms`, 或设置环境变量 ARGYLL_BIN", file=sys.stderr)
        return 1

    server = make_server()
    host, port = server.server_address[:2]

    stopping = threading.Event()

    def shutdown(signum: int, frame: Any) -> None:
        # 幂等: 一次 Ctrl-C 可能触发多次。终端把 SIGINT 发给整个前台进程组,
        # 而包装进程(uv run / launchd)通常还会再转发一份, 于是同一个 handler
        # 被连着调用两次 —— 表现为重复打印"正在停止", 并多起一个 shutdown 线程。
        # Event.set() 是原子的, 且信号 handler 只在主线程执行, 这里不存在竞态。
        if stopping.is_set():
            return
        stopping.set()
        # 必须先停会话: 否则 dispcal 拉起的全屏测试窗口会留在屏幕上关不掉
        print("\n正在停止...", file=sys.stderr)
        session_mod.manager.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    version = config.argyll_version() or "未知"
    print(f"Color Workbench  —  ArgyllCMS {version}")
    print(f"工作目录: {config.WORK_DIR}")
    print(f"服务地址: http://{host}:{port}")
    print("按 Ctrl-C 停止\n")

    try:
        server.serve_forever()
    finally:
        session_mod.manager.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
