import base64
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from scheduler.openai_subscription import OpenAISubscriptionClient, profile_from_account_raw

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)


class FakeAdminAPI:
    def __init__(self, data):
        self.data = data
        self.exports = []

    def export_account_data(self, account_id):
        self.exports.append(account_id)
        return self.data

    def refresh_openai_account(self, account_id):
        raise AssertionError("subscription fetch must not refresh accounts")


def test_fetch_openai_subscription_uses_official_subscription_endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("chatgpt-account-id")))
            body = {
                "plan_type": "plus",
                "active_until": "2026-07-01T00:00:00Z",
                "will_renew": True,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        admin = FakeAdminAPI({
            "accounts": [{
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "access_token": "fake-access-token",
                    "chatgpt_account_id": "acc_123",
                    "email": "codex@example.com",
                },
            }],
            "proxies": [],
        })
        client = OpenAISubscriptionClient(
            admin,
            f"http://127.0.0.1:{server.server_address[1]}/backend-api",
        )

        profile = client.fetch(7, NOW)

        assert admin.exports == [7]
        assert profile.email == "codex@example.com"
        assert profile.subscription_plan == "plus"
        assert profile.subscription_status == "active"
        assert profile.subscription_expires_at == datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        assert seen == [(
            "/backend-api/subscriptions?account_id=acc_123",
            "Bearer fake-access-token",
            "acc_123",
        )]
    finally:
        server.shutdown()
        server.server_close()


def test_profile_from_account_raw_reads_id_token_claims():
    payload = {
        "email": "claim@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc_claim",
            "chatgpt_plan_type": "team",
        },
    }
    token = f"x.{_b64(payload)}.y"

    profile = profile_from_account_raw(9, {
        "credentials": {"id_token": token},
    })

    assert profile.account_id == 9
    assert profile.email == "claim@example.com"
    assert profile.subscription_plan == "team"


def _b64(payload):
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
