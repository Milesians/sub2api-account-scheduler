"""调度看板后端。

使用标准库 HTTPServer，提供快照 API、邀请管理代理和前端静态文件托管。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .api import AdminAPI
from .config import load_config
from .store import Store

STATIC_DIR = Path(__file__).with_name("frontend")


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>sub2api 调度看板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #151923;
      --muted: #667085;
      --good: #16835a;
      --warn: #a15c00;
      --bad: #b42318;
      --info: #1d5f96;
      --chip: #eef2f7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .wrap {
      max-width: 1280px;
      margin: 0 auto;
      padding: 18px 20px;
    }
    .topbar {
      display: flex;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
      letter-spacing: 0;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { background: #f3f5f8; }
    main.wrap { padding-top: 16px; }
    .status {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 12px;
      min-width: 0;
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .metric .value {
      margin-top: 4px;
      font-size: 22px;
      font-weight: 650;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .metric .sub {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    section {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      margin-bottom: 16px;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .hint {
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }
    .table-wrap { overflow-x: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 900px;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid #edf0f4;
      text-align: left;
      white-space: nowrap;
      vertical-align: middle;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      background: #fbfcfd;
    }
    tr:last-child td { border-bottom: 0; }
    .name { font-weight: 620; }
    .num { font-variant-numeric: tabular-nums; }
    .reason {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 8px;
      background: var(--chip);
      font-size: 12px;
      color: #344054;
    }
    .changed { color: var(--warn); font-weight: 650; }
    .boost { color: var(--good); }
    .protect, .floor, .hot { color: var(--bad); }
    .empty {
      padding: 26px 14px;
      color: var(--muted);
      text-align: center;
    }
    .error {
      margin-bottom: 16px;
      border: 1px solid #f3b7b1;
      background: #fff4f2;
      color: var(--bad);
      border-radius: 8px;
      padding: 12px 14px;
      display: none;
    }
    @media (max-width: 860px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      .status { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .wrap { padding-left: 14px; padding-right: 14px; }
    }
    @media (max-width: 520px) {
      .status { grid-template-columns: 1fr; }
      .metric .value { font-size: 19px; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div>
        <h1>sub2api 调度看板</h1>
        <div class="hint" id="subtitle">读取中...</div>
      </div>
      <button id="refresh" type="button">刷新</button>
    </div>
  </header>
  <main class="wrap">
    <div class="error" id="error"></div>
    <div class="status">
      <div class="metric">
        <div class="label">受控账号</div>
        <div class="value" id="accountCount">-</div>
        <div class="sub" id="updatedAt">-</div>
      </div>
      <div class="metric">
        <div class="label">最近一轮</div>
        <div class="value" id="lastRun">-</div>
        <div class="sub" id="lastRunAt">-</div>
      </div>
      <div class="metric">
        <div class="label">本轮调整</div>
        <div class="value" id="changedCount">-</div>
        <div class="sub" id="changedSub">-</div>
      </div>
      <div class="metric">
        <div class="label">心跳</div>
        <div class="value" id="heartbeat">-</div>
        <div class="sub" id="heartbeatSub">-</div>
      </div>
    </div>

    <section>
      <div class="section-head">
        <h2>账号状态</h2>
        <div class="hint">按最近更新时间排序</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>账号</th>
              <th>Priority</th>
              <th>7d</th>
              <th>7d 刷新</th>
              <th>5h</th>
              <th>EWMA/h</th>
              <th>Cooldown</th>
              <th>采样</th>
              <th>最近原因</th>
            </tr>
          </thead>
          <tbody id="accounts"></tbody>
        </table>
      </div>
      <div class="empty" id="accountsEmpty">暂无账号状态</div>
    </section>

    <section>
      <div class="section-head">
        <h2>最近决策</h2>
        <div class="hint">最多 80 条</div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>账号</th>
              <th>Priority / LF</th>
              <th>原因</th>
              <th>7d</th>
              <th>7d 刷新</th>
              <th>5h</th>
              <th>Catchup</th>
              <th>Burn/h</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody id="decisions"></tbody>
        </table>
      </div>
      <div class="empty" id="decisionsEmpty">暂无决策记录</div>
    </section>
  </main>
  <script>
    const apiPath = "__API_PATH__";
    const $ = (id) => document.getElementById(id);

    function fmtPct(v) {
      return v === null || v === undefined ? "-" : `${Number(v).toFixed(1)}%`;
    }

    function fmtNum(v, digits = 2) {
      return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
    }

    function fmtTime(v) {
      if (!v) return "-";
      const d = new Date(v);
      if (Number.isNaN(d.getTime())) return v;
      return d.toLocaleString();
    }

    function clsReason(reason) {
      if (!reason) return "";
      if (reason.includes("boost") || reason === "behind") return "boost";
      if (reason.includes("protect") || reason.includes("cap")) return "protect";
      if (reason.includes("cooldown") || reason.includes("hot")) return "hot";
      return "";
    }

    function cell(text, className = "") {
      const td = document.createElement("td");
      td.textContent = text;
      if (className) td.className = className;
      return td;
    }

    function render(data) {
      $("subtitle").textContent = `${data.config.platform} / ${data.config.account_name_pattern || "全部账号"} / ${data.config.db_path}`;
      $("accountCount").textContent = data.summary.account_count;
      $("updatedAt").textContent = `页面刷新 ${fmtTime(data.generated_at)}`;
      $("lastRun").textContent = data.summary.last_run_id || "-";
      $("lastRunAt").textContent = fmtTime(data.summary.last_decided_at);
      $("changedCount").textContent = data.summary.last_run_changed_count;
      $("changedSub").textContent = data.summary.last_run_decision_count ? `共 ${data.summary.last_run_decision_count} 条决策` : "-";
      $("heartbeat").textContent = data.heartbeat.exists ? "正常" : "缺失";
      $("heartbeatSub").textContent = data.heartbeat.modified_at ? fmtTime(data.heartbeat.modified_at) : data.config.heartbeat_file;

      const accountsBody = $("accounts");
      accountsBody.replaceChildren();
      for (const a of data.accounts) {
        const tr = document.createElement("tr");
        tr.appendChild(cell(`${a.name || "-"} #${a.account_id}`, "name"));
        tr.appendChild(cell(a.last_priority ?? "-", "num"));
        tr.appendChild(cell(fmtPct(a.last_7d_used), "num"));
        tr.appendChild(cell(fmtTime(a.last_7d_reset_at)));
        tr.appendChild(cell(fmtPct(a.last_5h_used), "num"));
        tr.appendChild(cell(fmtNum(a.hourly_burn_ewma), "num"));
        tr.appendChild(cell(fmtTime(a.cooldown_until)));
        tr.appendChild(cell(fmtTime(a.last_sampled_at)));
        const reason = cell(a.last_reason || "-");
        reason.className = clsReason(a.last_reason);
        tr.appendChild(reason);
        accountsBody.appendChild(tr);
      }
      $("accountsEmpty").style.display = data.accounts.length ? "none" : "block";

      const decisionsBody = $("decisions");
      decisionsBody.replaceChildren();
      for (const d of data.decisions) {
        const tr = document.createElement("tr");
        tr.appendChild(cell(fmtTime(d.decided_at)));
        tr.appendChild(cell(`${d.account_name || "-"} #${d.account_id}`, "name"));
        const lf = `${d.current_load_factor ?? "-"} -> ${d.target_load_factor ?? "-"}`;
        const pr = cell(`${d.current_priority ?? "-"} -> ${d.target_priority ?? "-"} / LF ${lf}`, d.changed ? "changed num" : "num");
        tr.appendChild(pr);
        const reason = document.createElement("td");
        const chip = document.createElement("span");
        chip.className = `reason ${clsReason(d.reason)}`;
        chip.textContent = d.reason || "-";
        reason.appendChild(chip);
        tr.appendChild(reason);
        tr.appendChild(cell(fmtPct(d.seven_day_used), "num"));
        tr.appendChild(cell(fmtTime(d.seven_day_reset_at)));
        tr.appendChild(cell(fmtPct(d.five_hour_used), "num"));
        tr.appendChild(cell(fmtNum(d.catchup_score), "num"));
        tr.appendChild(cell(fmtNum(d.recent_hour_burn), "num"));
        tr.appendChild(cell(d.usage_source || "-"));
        decisionsBody.appendChild(tr);
      }
      $("decisionsEmpty").style.display = data.decisions.length ? "none" : "block";
    }

    async function load() {
      $("error").style.display = "none";
      try {
        const resp = await fetch(apiPath, { cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        render(await resp.json());
      } catch (e) {
        $("error").textContent = `读取失败：${e.message}`;
        $("error").style.display = "block";
      }
    }

    $("refresh").addEventListener("click", load);
    load();
    setInterval(load, 60000);
  </script>
</body>
</html>
"""


def serve(
    host: str,
    port: int,
    db_path: str,
    heartbeat_file: str,
    platform: str = "",
    account_name_pattern: str = "",
    base_url: str = "",
    admin_key: str = "",
) -> None:
    Store(db_path).close()
    handler = _handler(db_path, heartbeat_file, platform, account_name_pattern, base_url, admin_key)
    ThreadingHTTPServer((host, port), handler).serve_forever()


def start_background(
    host: str,
    port: int,
    db_path: str,
    heartbeat_file: str,
    platform: str = "",
    account_name_pattern: str = "",
    base_url: str = "",
    admin_key: str = "",
) -> ThreadingHTTPServer:
    Store(db_path).close()
    server = ThreadingHTTPServer(
        (host, port),
        _handler(db_path, heartbeat_file, platform, account_name_pattern, base_url, admin_key),
    )
    thread = threading.Thread(target=server.serve_forever, name="scheduler-ui", daemon=True)
    thread.start()
    return server


def snapshot(
    db_path: str,
    heartbeat_file: str,
    decision_limit: int = 80,
    platform: str = "",
    account_name_pattern: str = "",
) -> dict[str, Any]:
    Store(db_path).close()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        decisions = _rows(
            conn,
            """
            SELECT * FROM decision_log
            ORDER BY decided_at DESC, id DESC
            LIMIT ?
            """,
            (decision_limit,),
        )
        latest_by_account = _latest_decisions_by_account(conn)
        accounts = _rows(
            conn,
            """
            SELECT
              s.account_id,
              COALESCE(d.account_name, '') AS name,
              s.last_priority,
              s.last_7d_used,
              s.last_7d_reset_at,
              s.last_5h_used,
              s.last_sampled_at,
              s.hourly_burn_ewma,
              s.cooldown_until,
              s.probe_failures,
              s.updated_at,
              d.reason AS last_reason,
              d.current_priority AS last_current_priority,
              d.target_priority AS last_target_priority,
              d.current_load_factor AS last_current_load_factor,
              d.target_load_factor AS last_target_load_factor,
              d.decided_at AS last_decided_at
            FROM account_state s
            LEFT JOIN (
              SELECT *
              FROM decision_log
              WHERE id IN (
                SELECT MAX(id)
                FROM decision_log
                GROUP BY account_id
              )
            ) d ON d.account_id = s.account_id
            ORDER BY s.updated_at DESC, s.account_id
            """,
        )
        return {
            "generated_at": _iso(datetime.now(UTC)),
            "config": {
                "platform": platform,
                "account_name_pattern": account_name_pattern,
                "db_path": db_path,
                "heartbeat_file": heartbeat_file,
            },
            "heartbeat": _heartbeat(heartbeat_file),
            "summary": _summary(decisions, accounts, latest_by_account),
            "accounts": accounts,
            "decisions": [_with_changed(d) for d in decisions],
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="sub2api scheduler dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    serve(
        args.host,
        args.port,
        cfg.db_path,
        cfg.heartbeat_file,
        cfg.platform,
        cfg.account_name_pattern,
        cfg.base_url,
        cfg.admin_key,
    )


def _handler(
    db_path: str,
    heartbeat_file: str,
    platform: str,
    account_name_pattern: str,
    base_url: str,
    admin_key: str,
) -> type[BaseHTTPRequestHandler]:
    api = AdminAPI(base_url, admin_key) if base_url and admin_key else None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/snapshot":
                limit = _limit(parse_qs(parsed.query).get("limit"))
                body = json.dumps(
                    snapshot(db_path, heartbeat_file, limit, platform, account_name_pattern),
                    ensure_ascii=False,
                )
                self._send_text(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            if self._handle_invite_reset_get(parsed.path, api):
                return
            if self._send_static(parsed.path):
                return
            self._send_text(HTTPStatus.NOT_FOUND, "not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if self._handle_invite_reset_post(parsed.path, api):
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle_invite_reset_get(self, path: str, api: AdminAPI | None) -> bool:
            account_id, action = _parse_invite_reset_path(path)
            if account_id is None or action != "status":
                return False
            if api is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "admin api is not configured"})
                return True
            try:
                self._send_json(HTTPStatus.OK, api.codex_invite_reset_status(account_id))
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            return True

        def _handle_invite_reset_post(self, path: str, api: AdminAPI | None) -> bool:
            account_id, action = _parse_invite_reset_path(path)
            if account_id is None or action not in {"invite", "consume"}:
                return False
            if api is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "admin api is not configured"})
                return True
            try:
                payload = self._read_json()
                if action == "invite":
                    self._send_json(HTTPStatus.OK, api.send_codex_invite_reset_invite(account_id, payload.get("emails") or []))
                    return True
                self._send_json(HTTPStatus.OK, api.consume_codex_invite_reset(account_id, str(payload.get("credit_id") or "")))
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            return True

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request body must be a JSON object")
            return data

        def _send_static(self, path: str) -> bool:
            if path == "/":
                if (STATIC_DIR / "index.html").is_file():
                    return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                self._send_text(HTTPStatus.OK, HTML.replace("__API_PATH__", "/api/snapshot"), "text/html; charset=utf-8")
                return True
            static_path = _static_path(path)
            if static_path is None or not static_path.is_file():
                if "." not in Path(path).name and (STATIC_DIR / "index.html").is_file():
                    return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return False
            content_type = mimetypes.guess_type(static_path.name)[0] or "application/octet-stream"
            if static_path.suffix in {".js", ".css", ".svg"}:
                content_type += "; charset=utf-8"
            return self._send_file(static_path, content_type)

        def _send_file(self, path: Path, content_type: str) -> bool:
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return True

        def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            self._send_text(status, json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")

        def _send_text(self, status: HTTPStatus, body: str, content_type: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _parse_invite_reset_path(path: str) -> tuple[int | None, str]:
    parts = path.strip("/").split("/")
    if len(parts) != 6 or parts[:2] != ["api", "accounts"] or parts[3:5] != ["codex", "invite-reset"]:
        return None, ""
    try:
        return int(parts[2]), parts[5]
    except ValueError:
        return None, ""


def _static_path(path: str) -> Path | None:
    relative = path.lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return None
    full = (STATIC_DIR / relative).resolve()
    try:
        full.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    return full


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _latest_decisions_by_account(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = _rows(
        conn,
        """
        SELECT *
        FROM decision_log
        WHERE id IN (
          SELECT MAX(id)
          FROM decision_log
          GROUP BY account_id
        )
        """,
    )
    return {int(r["account_id"]): r for r in rows}


def _summary(
    decisions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    latest_by_account: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    last = decisions[0] if decisions else None
    last_run_id = last["run_id"] if last else None
    last_run = [d for d in decisions if d["run_id"] == last_run_id] if last_run_id else []
    return {
        "account_count": len(accounts),
        "last_run_id": last_run_id,
        "last_decided_at": last["decided_at"] if last else None,
        "last_run_decision_count": len(last_run),
        "last_run_changed_count": sum(1 for d in last_run if _is_changed(d)),
        "changed_account_count": sum(
            1 for d in latest_by_account.values() if _is_changed(d)
        ),
    }


def _heartbeat(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"exists": False, "path": path, "modified_at": None, "age_seconds": None}
    modified = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
    age = (datetime.now(UTC) - modified).total_seconds()
    return {"exists": True, "path": path, "modified_at": _iso(modified), "age_seconds": round(age)}


def _with_changed(row: dict[str, Any]) -> dict[str, Any]:
    row["changed"] = _is_changed(row)
    return row


def _is_changed(row: dict[str, Any]) -> bool:
    return (
        row.get("current_priority") != row.get("target_priority")
        or row.get("current_load_factor") != row.get("target_load_factor")
    )


def _limit(values: list[str] | None) -> int:
    if not values:
        return 80
    try:
        return max(1, min(500, int(values[0])))
    except ValueError:
        return 80


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    main()
