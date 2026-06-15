from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from urllib.request import Request, urlopen

from scheduler.models import AccountState, Decision
from scheduler.store import Store
from scheduler.ui import snapshot, start_background

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=84)


def test_snapshot_returns_dashboard_data(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    heartbeat.touch()

    store = Store(str(db))
    store.save_states([
        AccountState(
            account_id=7,
            last_priority=1050,
            last_7d_used=42.0,
            last_7d_reset_at=RESET,
            last_5h_used=12.0,
            last_sampled_at=NOW,
            hourly_burn_ewma=0.2,
        )
    ], NOW)
    store.add_decisions("run-1", [
        Decision(
            account_id=7,
            name="pay1",
            current_priority=100,
            target_priority=1050,
            current_load_factor=1,
            target_load_factor=1,
            reason="takeover",
            seven_day_used=42.0,
            seven_day_reset_at=RESET,
            five_hour_used=12.0,
            usage_source="passive",
        )
    ], NOW)
    store.close()

    data = snapshot(str(db), str(heartbeat))

    assert data["summary"]["account_count"] == 1
    assert data["summary"]["last_run_changed_count"] == 1
    assert data["heartbeat"]["exists"] is True
    assert data["accounts"][0]["name"] == "pay1"
    assert data["accounts"][0]["last_7d_reset_at"] == "2026-06-15T22:00:00Z"
    assert data["decisions"][0]["seven_day_reset_at"] == "2026-06-15T22:00:00Z"
    assert data["decisions"][0]["changed"] is True


def test_start_background_serves_snapshot(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    Store(str(db)).close()

    server = start_background(
        "127.0.0.1",
        0,
        str(db),
        str(heartbeat),
        platform="openai",
        account_name_pattern=r"pay\d+",
    )
    try:
        host, port = server.server_address
        import json
        from urllib.request import urlopen

        with urlopen(f"http://{host}:{port}/api/snapshot", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["config"]["platform"] == "openai"
        assert data["config"]["account_name_pattern"] == r"pay\d+"
    finally:
        server.shutdown()
        server.server_close()


def test_invite_reset_proxy_forwards_to_admin_api(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    Store(str(db)).close()
    seen = {}

    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["key"] = self.headers.get("x-api-key")
            body = {
                "code": 0,
                "message": "success",
                "data": {
                    "available_count": 1,
                    "credits": [{"id": "credit-1", "status": "available"}],
                    "eligibility_rules": ["rule"],
                    "requires_consent": True,
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    admin_server = HTTPServer(("127.0.0.1", 0), AdminHandler)
    admin_thread = threading.Thread(target=admin_server.serve_forever, daemon=True)
    admin_thread.start()
    ui_server = start_background(
        "127.0.0.1",
        0,
        str(db),
        str(heartbeat),
        platform="openai",
        base_url=f"http://127.0.0.1:{admin_server.server_address[1]}",
        admin_key="secret",
    )
    try:
        host, port = ui_server.server_address
        req = Request(f"http://{host}:{port}/api/accounts/7/codex/invite-reset/status")
        with urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["available_count"] == 1
        assert seen["path"] == "/api/v1/admin/accounts/7/codex/invite-reset/status"
        assert seen["key"] == "secret"
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        admin_server.shutdown()
        admin_server.server_close()
