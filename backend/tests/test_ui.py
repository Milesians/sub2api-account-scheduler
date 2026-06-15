from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from urllib.error import HTTPError
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
            target_load_factor=3,
            reason="takeover",
            seven_day_used=42.0,
            seven_day_reset_at=RESET,
            five_hour_used=12.0,
            target_now=55.0,
            projected_end=92.5,
            required_rate=0.7,
            recent_rate=0.4,
            remaining_hours=84.0,
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
    assert data["accounts"][0]["expected_7d_used"] == 55.0
    assert data["accounts"][0]["expected_7d_gap"] == 13.0
    assert data["accounts"][0]["last_current_load_factor"] == 1
    assert data["accounts"][0]["last_target_load_factor"] == 3
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


def test_invite_reset_routes_call_codex_backend_with_exported_token(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    Store(str(db)).close()
    admin_seen = []
    codex_seen = []

    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            admin_seen.append((self.path, self.headers.get("x-api-key")))
            body = {
                "code": 0,
                "message": "success",
                "data": {
                    "accounts": [{
                        "name": "codex-01",
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "access_token": "fake-access-token",
                            "expires_at": "2099-01-01T00:00:00Z",
                            "chatgpt_account_id": "chatgpt-account",
                        },
                    }],
                    "proxies": [],
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            raise AssertionError("valid token must not trigger refresh")

        def log_message(self, format, *args):
            pass

    class CodexHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            codex_seen.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("chatgpt-account-id")))
            if self.path.startswith("/backend-api/referrals/invite/eligibility"):
                body = {"requires_explicit_confirmation": True}
            elif self.path.startswith("/backend-api/wham/referrals/eligibility_rules"):
                body = {"rules": ["rule"]}
            elif self.path == "/backend-api/wham/rate-limit-reset-credits":
                body = {
                    "available_count": 1,
                    "credits": [{
                        "id": "credit-1",
                        "status": "available",
                        "title": "Reset",
                        "expires_at": "2026-06-20T12:30:00Z",
                    }],
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            codex_seen.append((self.command, self.path, self.headers.get("Authorization"), payload))
            if self.path == "/backend-api/wham/referrals/invite":
                body = {"invites": [{"email": email} for email in payload["emails"]], "message": "ok"}
            elif self.path == "/backend-api/wham/rate-limit-reset-credits/consume":
                body = {"code": "reset", "available_count": 0}
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    admin_server = HTTPServer(("127.0.0.1", 0), AdminHandler)
    admin_thread = threading.Thread(target=admin_server.serve_forever, daemon=True)
    admin_thread.start()
    codex_server = HTTPServer(("127.0.0.1", 0), CodexHandler)
    codex_thread = threading.Thread(target=codex_server.serve_forever, daemon=True)
    codex_thread.start()
    ui_server = start_background(
        "127.0.0.1",
        0,
        str(db),
        str(heartbeat),
        platform="openai",
        base_url=f"http://127.0.0.1:{admin_server.server_address[1]}",
        admin_key="secret",
        codex_invite_base_url=f"http://127.0.0.1:{codex_server.server_address[1]}/backend-api",
    )
    try:
        host, port = ui_server.server_address
        req = Request(f"http://{host}:{port}/api/accounts/7/codex/invite-reset/status")
        with urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["available_count"] == 1
        assert data["credits"][0]["id"] == "credit-1"
        assert data["credits"][0]["expires_at"] == "2026-06-20T12:30:00Z"

        req = Request(
            f"http://{host}:{port}/api/accounts/7/codex/invite-reset/invite",
            data=json.dumps({"emails": ["a@example.com b@example.com", "A@example.com"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=2) as resp:
            invite_data = json.loads(resp.read().decode("utf-8"))
        assert invite_data["message"] == "ok"

        req = Request(
            f"http://{host}:{port}/api/accounts/7/codex/invite-reset/consume",
            data=json.dumps({"credit_id": "credit-1"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=2) as resp:
            consume_data = json.loads(resp.read().decode("utf-8"))
        assert consume_data["code"] == "reset"
        assert consume_data["redeem_request_id"]

        assert all(item[1] == "secret" for item in admin_seen)
        assert admin_seen[0][0].startswith("/api/v1/admin/accounts/data?")
        assert ("GET", "/backend-api/wham/rate-limit-reset-credits", "Bearer fake-access-token", "chatgpt-account") in codex_seen
        invite_payload = next(item[3] for item in codex_seen if item[1] == "/backend-api/wham/referrals/invite")
        assert invite_payload["emails"] == ["a@example.com", "b@example.com"]
        consume_payload = next(item[3] for item in codex_seen if item[1] == "/backend-api/wham/rate-limit-reset-credits/consume")
        assert consume_payload["credit_id"] == "credit-1"
        assert consume_payload["redeem_request_id"] == consume_data["redeem_request_id"]
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        admin_server.shutdown()
        admin_server.server_close()
        codex_server.shutdown()
        codex_server.server_close()


def test_invite_reset_refreshes_expired_token_against_mock_admin(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    Store(str(db)).close()
    export_count = 0
    refreshed = []
    codex_auth = []

    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal export_count
            export_count += 1
            token = "expired-token" if export_count == 1 else "fresh-token"
            expires_at = "2020-01-01T00:00:00Z" if export_count == 1 else "2099-01-01T00:00:00Z"
            body = {
                "code": 0,
                "message": "success",
                "data": {
                    "accounts": [{
                        "name": "codex-01",
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {"access_token": token, "expires_at": expires_at},
                    }],
                    "proxies": [],
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            refreshed.append(self.path)
            body = {"code": 0, "message": "success", "data": {}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    class CodexHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            codex_auth.append(self.headers.get("Authorization"))
            if self.path.startswith("/backend-api/referrals/invite/eligibility"):
                body = {}
            elif self.path.startswith("/backend-api/wham/referrals/eligibility_rules"):
                body = {}
            else:
                body = {"credits": []}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    admin_server = HTTPServer(("127.0.0.1", 0), AdminHandler)
    threading.Thread(target=admin_server.serve_forever, daemon=True).start()
    codex_server = HTTPServer(("127.0.0.1", 0), CodexHandler)
    threading.Thread(target=codex_server.serve_forever, daemon=True).start()
    ui_server = start_background(
        "127.0.0.1",
        0,
        str(db),
        str(heartbeat),
        platform="openai",
        base_url=f"http://127.0.0.1:{admin_server.server_address[1]}",
        admin_key="secret",
        codex_invite_base_url=f"http://127.0.0.1:{codex_server.server_address[1]}/backend-api",
    )
    try:
        host, port = ui_server.server_address
        with urlopen(f"http://{host}:{port}/api/accounts/7/codex/invite-reset/status", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["available_count"] == 0
        assert refreshed == ["/api/v1/admin/openai/accounts/7/refresh"]
        assert codex_auth == ["Bearer fresh-token", "Bearer fresh-token", "Bearer fresh-token"]
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        admin_server.shutdown()
        admin_server.server_close()
        codex_server.shutdown()
        codex_server.server_close()


def test_invite_reset_upstream_failure_returns_json_424(tmp_path):
    db = tmp_path / "scheduler.db"
    heartbeat = tmp_path / "last_tick"
    Store(str(db)).close()

    class AdminHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {
                "code": 0,
                "message": "success",
                "data": {
                    "accounts": [{
                        "name": "codex-01",
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "access_token": "fake-access-token",
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    }],
                    "proxies": [],
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    admin_server = HTTPServer(("127.0.0.1", 0), AdminHandler)
    threading.Thread(target=admin_server.serve_forever, daemon=True).start()
    ui_server = start_background(
        "127.0.0.1",
        0,
        str(db),
        str(heartbeat),
        platform="openai",
        base_url=f"http://127.0.0.1:{admin_server.server_address[1]}",
        admin_key="secret",
        codex_invite_base_url="http://127.0.0.1:9/backend-api",
    )
    try:
        host, port = ui_server.server_address
        req = Request(f"http://{host}:{port}/api/accounts/7/codex/invite-reset/status")
        try:
            urlopen(req, timeout=2)
        except HTTPError as e:
            data = json.loads(e.read().decode("utf-8"))
            assert e.code == 424
            assert "codex invite reset request failed" in data["error"]
        else:
            raise AssertionError("expected HTTPError")
    finally:
        ui_server.shutdown()
        ui_server.server_close()
        admin_server.shutdown()
        admin_server.server_close()
