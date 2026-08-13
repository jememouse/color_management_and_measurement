#!/usr/bin/env python3
"""HTTP/SSE 服务测试。

重点在安全边界: Host 校验、CSRF 头、路径穿越。这三条是本地服务最容易被
忽视又最要命的地方 —— 浏览器里任何一个网页都能向 127.0.0.1 发请求。

会话相关的测试用 /bin/echo 替换真实的 ArgyllCMS 命令, 避免占用测量仪器。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

import config
import server as server_mod
from argyll import tools


@pytest.fixture
def live_server() -> Iterator[str]:
    """在随机端口起一个真实服务, 返回 base url。"""
    srv = server_mod.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server_mod.session_mod.manager.shutdown()
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    csrf: bool = True,
) -> tuple[int, Any]:
    """发一个请求, 返回 (状态码, 解析后的 JSON 或原始文本)。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - 固定 http 本地地址
    req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header(server_mod.CSRF_HEADER, "1")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


# --------------------------------------------------------------------------
# 安全边界
# --------------------------------------------------------------------------


def test_rejects_foreign_host_header(live_server):
    """DNS rebinding 防护: 攻击者把域名指向 127.0.0.1 也进不来。"""
    status, payload = request(f"{live_server}/api/status", headers={"Host": "evil.example.com"})
    assert status == 403
    assert "Host" in payload["error"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_accepts_local_hosts(live_server, host):
    port = live_server.rsplit(":", 1)[1]
    status, _ = request(f"{live_server}/api/status", headers={"Host": f"{host}:{port}"})
    assert status == 200


def test_post_without_csrf_header_is_rejected(live_server):
    """跨站的简单请求加不上自定义头 —— 这是 CSRF 防线。"""
    status, payload = request(f"{live_server}/api/session/stop", method="POST", body={}, csrf=False)
    assert status == 403
    assert server_mod.CSRF_HEADER in payload["error"]


def test_delete_without_csrf_header_is_rejected(live_server):
    status, _ = request(f"{live_server}/api/files/x.icc", method="DELETE", csrf=False)
    assert status == 403


def test_get_does_not_require_csrf(live_server):
    """读操作不需要 CSRF 头 —— 它们没有副作用。"""
    status, _ = request(f"{live_server}/api/status", csrf=False)
    assert status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/files/..%2F..%2Fserver.py",
        "/api/files/....//server.py",
        "/api/files/%2e%2e%2fconfig.py",
    ],
)
def test_file_download_blocks_traversal(live_server, path):
    status, _ = request(f"{live_server}{path}")
    assert status == 404


def test_file_download_blocks_disallowed_suffix(live_server):
    """即使文件确实在 work/ 里, 不在白名单的扩展名也不给下。"""
    sneaky = config.WORK_DIR / "notes.py"
    sneaky.write_text("secret = 1\n", encoding="utf-8")
    try:
        status, _ = request(f"{live_server}/api/files/notes.py")
        assert status == 404
    finally:
        sneaky.unlink(missing_ok=True)


@pytest.mark.parametrize("path", ["/../server.py", "/..%2fconfig.py", "/static/../../server.py"])
def test_static_blocks_traversal(live_server, path):
    status, payload = request(f"{live_server}{path}")
    assert status == 404
    assert "server.py" not in str(payload) or "找不到" in str(payload)


def test_oversized_body_is_rejected(live_server):
    """请求体上限, 防止内存被打爆。"""
    huge = {"action": "spotread", "params": {"pad": "x" * (server_mod.MAX_BODY_BYTES + 1000)}}
    status, _ = request(f"{live_server}/api/session/start", method="POST", body=huge)
    assert status == 400


def test_malformed_json_is_rejected(live_server):
    req = urllib.request.Request(  # noqa: S310
        f"{live_server}/api/session/start", data=b"{not json", method="POST"
    )
    req.add_header(server_mod.CSRF_HEADER, "1")
    try:
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            pytest.fail("非法 JSON 应被拒绝")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_json_array_body_is_rejected(live_server):
    req = urllib.request.Request(  # noqa: S310
        f"{live_server}/api/session/start", data=b"[1,2,3]", method="POST"
    )
    req.add_header(server_mod.CSRF_HEADER, "1")
    try:
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            pytest.fail("数组请求体应被拒绝")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


# --------------------------------------------------------------------------
# 只读接口
# --------------------------------------------------------------------------


def test_status_reports_environment(live_server):
    status, payload = request(f"{live_server}/api/status")
    assert status == 200
    assert "python" in payload
    assert "argyll_version" in payload
    assert "tools" in payload
    assert "session" in payload


def test_devices_endpoint(live_server):
    status, payload = request(f"{live_server}/api/devices")
    assert status == 200
    assert "instruments" in payload
    assert "displays" in payload
    assert isinstance(payload["has_instrument"], bool)


def test_options_endpoint_mirrors_backend(live_server):
    """前端的下拉选项来自后端, 避免两处各写一份逐渐对不上。"""
    status, payload = request(f"{live_server}/api/options")
    assert status == 200
    assert set(payload["filter_modes"]) == set(tools.FILTER_MODES)
    assert set(payload["measure_modes"]) == set(tools.MEASURE_MODES)


def test_workflow_endpoint(live_server):
    status, payload = request(f"{live_server}/api/workflow")
    assert status == 200
    steps = payload["display_profile"]
    assert [s["step"] for s in steps] == [
        "dispcal",
        "targen",
        "dispread",
        "colprof",
        "dispwin",
    ]


def test_files_listing(live_server):
    marker = config.WORK_DIR / "listing-probe.ti3"
    marker.write_text("probe\n", encoding="utf-8")
    try:
        status, payload = request(f"{live_server}/api/files")
        assert status == 200
        names = [f["name"] for f in payload["files"]]
        assert "listing-probe.ti3" in names
        entry = next(f for f in payload["files"] if f["name"] == "listing-probe.ti3")
        assert entry["downloadable"] is True
    finally:
        marker.unlink(missing_ok=True)


def test_unknown_api_returns_404(live_server):
    status, payload = request(f"{live_server}/api/no-such-thing")
    assert status == 404
    assert "未知接口" in payload["error"]


# --------------------------------------------------------------------------
# 文件下载与删除
# --------------------------------------------------------------------------


def test_download_and_delete_roundtrip(live_server):
    probe = config.WORK_DIR / "roundtrip.cal"
    probe.write_text("CAL DATA\n", encoding="utf-8")

    status, body = request(f"{live_server}/api/files/roundtrip.cal")
    assert status == 200
    assert "CAL DATA" in str(body)

    status, payload = request(f"{live_server}/api/files/roundtrip.cal", method="DELETE")
    assert status == 200
    assert payload["deleted"] == "roundtrip.cal"
    assert not probe.exists()


def test_delete_missing_file_returns_404(live_server):
    status, _ = request(f"{live_server}/api/files/never-existed.icc", method="DELETE")
    assert status == 404


# --------------------------------------------------------------------------
# 会话 (用 /bin/echo 替换真实 ArgyllCMS, 不占用仪器)
# --------------------------------------------------------------------------


@pytest.fixture
def fake_command(monkeypatch):
    """把命令构建换成一条无害的 /bin/echo。"""

    def build(action: str, params: dict[str, Any]) -> tools.Command:
        if action == "boom":
            raise tools.ToolError("故意失败")
        return tools.Command(
            tool="echo",
            argv=["/bin/echo", f"fake-{action}"],
            label=f"假任务 {action}",
        )

    monkeypatch.setattr(tools, "build", build)
    return build


def test_start_session(live_server, fake_command):
    status, payload = request(
        f"{live_server}/api/session/start",
        method="POST",
        body={"action": "spotread", "params": {}},
    )
    assert status == 200
    assert payload["started"] is True
    assert payload["command"]["tool"] == "echo"


def test_start_session_requires_action(live_server, fake_command):
    status, payload = request(f"{live_server}/api/session/start", method="POST", body={})
    assert status == 400
    assert "action" in payload["error"]


def test_start_session_rejects_non_object_params(live_server, fake_command):
    status, _ = request(
        f"{live_server}/api/session/start",
        method="POST",
        body={"action": "spotread", "params": "not-a-dict"},
    )
    assert status == 400


def test_tool_error_becomes_400(live_server, fake_command):
    """参数问题是用户的错, 应返回 400 而不是 500。"""
    status, payload = request(
        f"{live_server}/api/session/start", method="POST", body={"action": "boom"}
    )
    assert status == 400
    assert "故意失败" in payload["error"]


def test_concurrent_session_returns_409(live_server, monkeypatch):
    """仪器独占 —— 第二个任务必须被挡住, 且用 409 而非 500 表达。"""

    def build(action: str, params: dict[str, Any]) -> tools.Command:
        return tools.Command(tool="sh", argv=["/bin/sh", "-c", "sleep 20"], label="长任务")

    monkeypatch.setattr(tools, "build", build)

    first, _ = request(f"{live_server}/api/session/start", method="POST", body={"action": "a"})
    assert first == 200

    status, payload = request(
        f"{live_server}/api/session/start", method="POST", body={"action": "b"}
    )
    assert status == 409
    assert "已有任务" in payload["error"]

    request(f"{live_server}/api/session/stop", method="POST", body={"force": True})


def test_send_key_without_session_returns_409(live_server):
    status, _ = request(f"{live_server}/api/session/key", method="POST", body={"key": "space"})
    assert status == 409


def test_send_key_validates_key_name(live_server, monkeypatch):
    def build(action: str, params: dict[str, Any]) -> tools.Command:
        return tools.Command(tool="cat", argv=["/bin/cat"], label="cat")

    monkeypatch.setattr(tools, "build", build)
    request(f"{live_server}/api/session/start", method="POST", body={"action": "x"})

    ok, _ = request(f"{live_server}/api/session/key", method="POST", body={"key": "space"})
    assert ok == 200

    bad, payload = request(
        f"{live_server}/api/session/key", method="POST", body={"key": "super-nonsense-key"}
    )
    assert bad == 400
    assert "未知按键" in payload["error"]

    request(f"{live_server}/api/session/stop", method="POST", body={"force": True})


def test_stop_when_idle_reports_false(live_server):
    status, payload = request(f"{live_server}/api/session/stop", method="POST", body={})
    assert status == 200
    assert payload["stopped"] is False


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------


def test_sse_stream_sends_hello_and_snapshot(live_server):
    """SSE 连接建立后应立刻收到 hello 与 snapshot, 而不是干等到有事件才响应。"""
    req = urllib.request.Request(f"{live_server}/api/session/stream")  # noqa: S310
    lines: list[str] = []
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        # 必须逐行读: read(n) 会一直阻塞到攒够 n 字节, 而 SSE 是持续开着的流,
        # 开场的 hello + snapshot 通常远不足 n, 于是读到超时。
        for _ in range(12):
            line = resp.readline()
            if not line:
                break
            lines.append(line.decode("utf-8", errors="replace"))
            if any(line_text.startswith("event: snapshot") for line_text in lines):
                break

    chunk = "".join(lines)
    assert "event: hello" in chunk
    assert "event: snapshot" in chunk


def test_sse_picks_up_session_started_later(live_server, monkeypatch):
    """关键回归: 先连 SSE 再启动会话, 事件必须能送达。

    早期实现只在连接建立瞬间订阅一次, 之后启动的会话在这条连接上永远静默 ——
    用户点了"开始"却什么都看不到。
    """

    def build(action: str, params: dict[str, Any]) -> tools.Command:
        return tools.Command(tool="echo", argv=["/bin/echo", "LATE-SESSION-MARKER"], label="迟启动")

    monkeypatch.setattr(tools, "build", build)

    received: list[str] = []
    error: list[BaseException] = []

    def reader() -> None:
        try:
            req = urllib.request.Request(f"{live_server}/api/session/stream")  # noqa: S310
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                deadline = threading.Event()
                while not deadline.is_set() and len(received) < 40:
                    line = resp.readline()
                    if not line:
                        break
                    received.append(line.decode("utf-8", errors="replace"))
                    if "LATE-SESSION-MARKER" in received[-1]:
                        break
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    # 等 SSE 连接就绪后再启动会话
    threading.Event().wait(1.0)
    status, _ = request(f"{live_server}/api/session/start", method="POST", body={"action": "late"})
    assert status == 200

    thread.join(timeout=12)
    blob = "".join(received)

    assert not error, f"SSE 读取出错: {error}"
    assert "LATE-SESSION-MARKER" in blob, f"迟启动的会话事件没有送达。收到: {blob[:500]}"
