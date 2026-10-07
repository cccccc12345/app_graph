import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app_graph.webapp import ExplorerService, Handler


def test_web_ui_and_status_endpoints():
    service = ExplorerService()
    handler = type("TestHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/") as response:
            assert response.status == 200
            page = response.read().decode()
            assert "Android 页面图谱探索" in page
            assert "graphEdges" in page
            assert "loadDevices" in page
        with urllib.request.urlopen(base + "/api/status") as response:
            assert json.load(response)["running"] is False
        with urllib.request.urlopen(base + "/api/token") as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(base + "/api/stop", method="POST", data=b"{}")
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Stop endpoint should require a token")
        request = urllib.request.Request(
            base + "/api/stop",
            data=b"{}",
            method="POST",
            headers={"X-App-Graph-Token": token, "Origin": base},
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["ok"] is True
        request = urllib.request.Request(base + "/api/graph", headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Non-loopback Host should be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_service_validates_settings_before_worker_start(tmp_path):
    service = ExplorerService()
    ok, message = service.start({"package": "not valid", "output": str(tmp_path)}, token=None)
    assert not ok
    assert "format" in message
    assert not service.snapshot()["running"]
