"""本地 mock sub2api：python tests/mock_server.py [port]，配合 --once 做端到端验证。

list 接口按 platform 查询参数返回 anthropic 或 openai 账号集。
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


def iso(offset_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


def anthropic_account(id_, name, priority, util_7d, util_5h):
    return {
        "id": id_, "name": name, "type": "oauth", "priority": priority,
        "concurrency": 1, "load_factor": None,
        "status": "active", "schedulable": True,
        "extra": {
            "session_window_utilization": util_5h,        # 0-1
            "passive_usage_7d_utilization": util_7d,      # 0-1
            "passive_usage_7d_reset": int(time.time()) + 84 * 3600,
            "passive_usage_sampled_at": iso(-60),
        },
    }


def openai_account(id_, name, priority, used_7d, used_5h):
    return {
        "id": id_, "name": name, "type": "oauth", "priority": priority,
        "concurrency": 1, "load_factor": None,
        "status": "active", "schedulable": True,
        "extra": {
            "codex_5h_used_percent": used_5h,             # 0-100
            "codex_7d_used_percent": used_7d,             # 0-100
            "codex_5h_reset_at": iso(2 * 3600),
            "codex_7d_reset_at": iso(84 * 3600),
            "codex_usage_updated_at": iso(-60),
        },
    }


ACCOUNTS = {
    "anthropic": [
        anthropic_account(1, "team-a-01", 50, 0.10, 0.30),   # 落后，受控
        anthropic_account(2, "team-a-02", 50, 0.48, 0.20),   # 正常，受控
        anthropic_account(3, "team-a-03", 50, 0.995, 0.10),  # 触 7d 硬保护
        anthropic_account(4, "manual-vip", 10, 0.50, 0.50),  # 名称不匹配，不受控
        {  # 受控但无被动数据 -> 触发 active 探测
            "id": 5, "name": "team-a-cold", "type": "oauth", "priority": 50,
            "concurrency": 1, "load_factor": None,
            "status": "active", "schedulable": True, "extra": {},
        },
    ],
    "openai": [
        openai_account(11, "codex-01", 50, 12.0, 30.0),      # 落后，受控
        openai_account(12, "codex-02", 50, 49.0, 20.0),      # 正常，受控
        openai_account(13, "codex-03", 50, 99.5, 10.0),      # 触 7d 硬保护
        {  # apikey 类型：无窗口数据，应被 type 过滤排除
            "id": 14, "name": "codex-apikey", "type": "apikey", "priority": 50,
            "concurrency": 1, "load_factor": None,
            "status": "active", "schedulable": True, "extra": {},
        },
        {  # 受控但无被动数据 -> 触发 active 探测
            "id": 15, "name": "codex-cold", "type": "oauth", "priority": 50,
            "concurrency": 1, "load_factor": None,
            "status": "active", "schedulable": True, "extra": {},
        },
    ],
}

PROBE_RESPONSES = {
    "5": {  # anthropic 冷账号：含 sonnet 窗口
        "source": "active",
        "five_hour": {"utilization": 12.0, "resets_at": None},
        "seven_day": {"utilization": 33.0, "resets_at": iso(84 * 3600)},
        "seven_day_sonnet": {"utilization": 41.0},
    },
    "15": {  # openai 冷账号：无 sonnet
        "source": "active",
        "five_hour": {"utilization": 8.0, "resets_at": iso(2 * 3600)},
        "seven_day": {"utilization": 21.0, "resets_at": iso(84 * 3600)},
    },
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, data):
        body = json.dumps({"code": 0, "message": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        # /api/v1/admin/accounts/:id/usage
        if len(parts) == 6 and parts[-1] == "usage":
            self._send(PROBE_RESPONSES.get(parts[-2], {"source": "active"}))
            return
        if url.path == "/api/v1/admin/accounts":
            platform = (parse_qs(url.query).get("platform") or ["anthropic"])[0]
            items = ACCOUNTS.get(platform, [])
            self._send({"items": items, "total": len(items),
                        "page": 1, "page_size": 1000, "pages": 1})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"BULK-UPDATE: {payload}", flush=True)
        self._send({"updated": len(payload.get("account_ids", []))})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18923
    print(f"mock sub2api on :{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
