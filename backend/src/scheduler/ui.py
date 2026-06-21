"""调度看板后端。

使用标准库 HTTPServer，提供快照 API、Codex 邀请管理和前端静态文件托管。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .api import AdminAPI
from .config import Config, load_config
from .codex_invite import CodexInviteReset, InviteResetError
from .runner import _touch_heartbeat, tick
from .store import Store

STATIC_DIR = Path(__file__).with_name("frontend")
DEFAULT_SENSITIVE_ACTION_PASSWORD = "123456"
EMBEDDED_SESSION_COOKIE = "scheduler_embedded_admin"
EMBEDDED_SESSION_TTL_SECONDS = 12 * 60 * 60
EMBEDDED_AUTH_TIMEOUT_SECONDS = 5.0


class _AuthResult:
    def __init__(
        self,
        ok: bool,
        status: HTTPStatus = HTTPStatus.OK,
        error: str = "",
    ) -> None:
        self.ok = ok
        self.status = status
        self.error = error


class _AuthError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


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
    codex_invite_base_url: str = "",
    frame_ancestors: tuple[str, ...] = (),
    cfg: Config | None = None,
) -> None:
    runtime_cfg = _runtime_config(cfg, db_path, heartbeat_file, platform, account_name_pattern, base_url, admin_key)
    Store(runtime_cfg.db_path).close()
    handler = _handler(
        runtime_cfg.db_path,
        runtime_cfg.heartbeat_file,
        runtime_cfg.platform,
        runtime_cfg.account_name_pattern,
        runtime_cfg.base_url,
        runtime_cfg.admin_key,
        codex_invite_base_url,
        frame_ancestors,
        runtime_cfg,
    )
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
    codex_invite_base_url: str = "",
    frame_ancestors: tuple[str, ...] = (),
    cfg: Config | None = None,
) -> ThreadingHTTPServer:
    runtime_cfg = _runtime_config(cfg, db_path, heartbeat_file, platform, account_name_pattern, base_url, admin_key)
    Store(runtime_cfg.db_path).close()
    server = ThreadingHTTPServer(
        (host, port),
        _handler(
            runtime_cfg.db_path,
            runtime_cfg.heartbeat_file,
            runtime_cfg.platform,
            runtime_cfg.account_name_pattern,
            runtime_cfg.base_url,
            runtime_cfg.admin_key,
            codex_invite_base_url,
            frame_ancestors,
            runtime_cfg,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, name="scheduler-ui", daemon=True)
    thread.start()
    return server


def _runtime_config(
    cfg: Config | None,
    db_path: str,
    heartbeat_file: str,
    platform: str,
    account_name_pattern: str,
    base_url: str,
    admin_key: str,
) -> Config:
    if cfg is not None:
        return cfg
    return Config(
        base_url=base_url.rstrip("/"),
        admin_key=admin_key,
        platform=platform or "anthropic",
        account_name_pattern=account_name_pattern,
        db_path=db_path,
        heartbeat_file=heartbeat_file,
    )


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
              s.current_priority,
              s.current_load_factor,
              s.last_7d_used,
              s.last_7d_reset_at,
              s.last_5h_used,
              s.last_sampled_at,
              s.hourly_burn_ewma,
              s.cooldown_until,
              s.probe_failures,
              s.updated_at,
              COALESCE(c.paused, 0) AS scheduler_paused,
              c.updated_at AS scheduler_control_updated_at,
              p.email,
              p.subscription_plan,
              p.subscription_status,
              p.subscription_expires_at,
              p.updated_at AS profile_updated_at,
              p.subscription_error,
              d.reason AS last_reason,
              d.current_priority AS last_current_priority,
              d.target_priority AS last_target_priority,
              d.current_load_factor AS last_current_load_factor,
              d.target_load_factor AS last_target_load_factor,
              d.target_now AS expected_7d_used,
              CASE
                WHEN d.target_now IS NOT NULL AND s.last_7d_used IS NOT NULL
                THEN d.target_now - s.last_7d_used
                ELSE NULL
              END AS expected_7d_gap,
              d.projected_end AS projected_7d_end,
              d.required_rate,
              d.recent_rate,
              d.remaining_hours,
              d.mode,
              d.drain_gap,
              d.drain_required_rate,
              d.drain_pressure,
              d.drain_level,
              d.deadline_hours,
              d.decided_at AS last_decided_at
            FROM account_state s
            LEFT JOIN account_control c ON c.account_id = s.account_id
            LEFT JOIN account_profile_cache p ON p.account_id = s.account_id
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
        frame_ancestors=cfg.ui_frame_ancestor_hosts,
        cfg=cfg,
    )


def _handler(
    db_path: str,
    heartbeat_file: str,
    platform: str,
    account_name_pattern: str,
    base_url: str,
    admin_key: str,
    codex_invite_base_url: str = "",
    frame_ancestors: tuple[str, ...] = (),
    cfg: Config | None = None,
) -> type[BaseHTTPRequestHandler]:
    runtime_cfg = _runtime_config(cfg, db_path, heartbeat_file, platform, account_name_pattern, base_url, admin_key)
    api = AdminAPI(runtime_cfg.base_url, runtime_cfg.admin_key) if runtime_cfg.base_url and runtime_cfg.admin_key else None
    invite = CodexInviteReset(api, codex_invite_base_url or os.environ.get("CODEX_INVITE_RESET_BASE_URL", "")) if api else None
    frame_ancestors_header = _frame_ancestors_header(frame_ancestors)
    expected_src_origin = _origin(runtime_cfg.base_url)
    session_secret = _session_secret(runtime_cfg.admin_key)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if _requires_embedded_auth(parsed.path):
                auth = self._embedded_auth(parsed)
                if not auth.ok:
                    self._send_json(auth.status, {"error": auth.error})
                    return
            if parsed.path == "/api/snapshot":
                limit = _limit(parse_qs(parsed.query).get("limit"))
                body = json.dumps(
                    snapshot(
                        runtime_cfg.db_path,
                        runtime_cfg.heartbeat_file,
                        limit,
                        runtime_cfg.platform,
                        runtime_cfg.account_name_pattern,
                    ),
                    ensure_ascii=False,
                )
                self._send_text(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            if self._handle_invite_reset_get(parsed.path, invite):
                return
            if self._send_static(parsed.path):
                return
            self._send_text(HTTPStatus.NOT_FOUND, "not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            auth = self._embedded_auth(parsed)
            if not auth.ok:
                self._send_json(auth.status, {"error": auth.error})
                return
            if self._handle_scheduler_control_post(parsed.path):
                return
            if self._handle_scheduler_refresh_post(parsed.path, api):
                return
            if self._handle_invite_reset_post(parsed.path, invite):
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _embedded_auth(self, parsed: Any) -> "_AuthResult":
            if not expected_src_origin:
                return _AuthResult(False, HTTPStatus.SERVICE_UNAVAILABLE, "sub2api base url is not configured")
            if self._valid_embedded_session():
                return _AuthResult(True)
            token, src_host, ui_mode, user_id = self._embedded_auth_values(parsed)
            if ui_mode != "embedded":
                return _AuthResult(False, HTTPStatus.UNAUTHORIZED, "embedded ui_mode is required")
            if _origin(src_host) != expected_src_origin:
                return _AuthResult(False, HTTPStatus.FORBIDDEN, "invalid embedded source host")
            if not token:
                return _AuthResult(False, HTTPStatus.UNAUTHORIZED, "embedded token is required")
            try:
                profile = _validate_sub2api_admin(runtime_cfg.base_url, token, user_id)
            except _AuthError as e:
                return _AuthResult(False, e.status, str(e))
            self._set_embedded_session(profile["user_id"])
            return _AuthResult(True)

        def _embedded_auth_values(self, parsed: Any) -> tuple[str, str, str, str]:
            query = parse_qs(parsed.query)
            token = _single_query_value(query, "token")
            src_host = _single_query_value(query, "src_host")
            ui_mode = _single_query_value(query, "ui_mode")
            user_id = _single_query_value(query, "user_id")
            if token:
                return token, src_host, ui_mode, user_id
            auth_header = self.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
            return (
                token,
                self.headers.get("X-Embedded-Src-Host", "").strip(),
                self.headers.get("X-Embedded-Ui-Mode", "").strip(),
                self.headers.get("X-Embedded-User-Id", "").strip(),
            )

        def _valid_embedded_session(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            session = _cookie_value(cookie, EMBEDDED_SESSION_COOKIE)
            return _verify_session(session, session_secret) is not None

        def _set_embedded_session(self, user_id: str) -> None:
            self._pending_embedded_session = _make_session(user_id, session_secret)

        def _handle_invite_reset_get(self, path: str, invite: CodexInviteReset | None) -> bool:
            account_id, action = _parse_invite_reset_path(path)
            if account_id is None or action != "status":
                return False
            if invite is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "admin api is not configured"})
                return True
            try:
                self._send_json(HTTPStatus.OK, invite.status(account_id))
            except InviteResetError as e:
                self._send_json(HTTPStatus(e.status), {"error": str(e)})
            except Exception as e:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(e)})
            return True

        def _handle_scheduler_control_post(self, path: str) -> bool:
            account_id = _parse_scheduler_control_path(path)
            if account_id is None:
                return False
            try:
                payload = self._read_json()
                _verify_sensitive_password(payload)
                if "paused" not in payload:
                    raise ValueError("paused is required")
                paused = payload["paused"]
                if not isinstance(paused, bool):
                    raise ValueError("paused must be a boolean")
                store = Store(runtime_cfg.db_path)
                try:
                    control = store.set_account_paused(account_id, paused, datetime.now(UTC))
                finally:
                    store.close()
                self._send_json(HTTPStatus.OK, {
                    "account_id": control.account_id,
                    "scheduler_paused": control.paused,
                    "scheduler_control_updated_at": _iso(control.updated_at),
                })
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return True

        def _handle_scheduler_refresh_post(self, path: str, api: AdminAPI | None) -> bool:
            if path != "/api/scheduler/refresh":
                return False
            if api is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "admin api is not configured"})
                return True
            try:
                payload = self._read_json()
                _verify_sensitive_password(payload)
                store = Store(runtime_cfg.db_path)
                try:
                    terminal_active = tick(runtime_cfg, api, store)
                finally:
                    store.close()
                _touch_heartbeat(runtime_cfg.heartbeat_file)
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "terminal_active": terminal_active,
                    "triggered_at": _iso(datetime.now(UTC)),
                })
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return True

        def _handle_invite_reset_post(self, path: str, invite: CodexInviteReset | None) -> bool:
            account_id, action = _parse_invite_reset_path(path)
            if account_id is None or action not in {"invite", "consume"}:
                return False
            if invite is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "admin api is not configured"})
                return True
            try:
                payload = self._read_json()
                _verify_sensitive_password(payload)
                if action == "invite":
                    self._send_json(HTTPStatus.OK, invite.send_invite(account_id, payload.get("emails") or []))
                    return True
                self._send_json(HTTPStatus.OK, invite.consume(account_id, str(payload.get("credit_id") or "")))
            except ValueError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except InviteResetError as e:
                self._send_json(HTTPStatus(e.status), {"error": str(e)})
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
            self._send_frame_ancestors_header()
            self._send_embedded_session_header()
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
            self._send_frame_ancestors_header()
            self._send_embedded_session_header()
            self.end_headers()
            self.wfile.write(data)

        def _send_frame_ancestors_header(self) -> None:
            if frame_ancestors_header:
                self.send_header("Content-Security-Policy", frame_ancestors_header)

        def _send_embedded_session_header(self) -> None:
            session = getattr(self, "_pending_embedded_session", "")
            if session:
                self.send_header(
                    "Set-Cookie",
                    (
                        f"{EMBEDDED_SESSION_COOKIE}={session}; "
                        f"Max-Age={EMBEDDED_SESSION_TTL_SECONDS}; Path=/; HttpOnly; "
                        "Secure; SameSite=None"
                    ),
                )
                self._pending_embedded_session = ""

    return Handler


def _parse_invite_reset_path(path: str) -> tuple[int | None, str]:
    parts = path.strip("/").split("/")
    if len(parts) != 6 or parts[:2] != ["api", "accounts"] or parts[3:5] != ["codex", "invite-reset"]:
        return None, ""
    try:
        return int(parts[2]), parts[5]
    except ValueError:
        return None, ""


def _parse_scheduler_control_path(path: str) -> int | None:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "accounts"] or parts[3] != "scheduler-control":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _verify_sensitive_password(payload: dict[str, Any]) -> None:
    expected = os.environ.get("SENSITIVE_ACTION_PASSWORD") or DEFAULT_SENSITIVE_ACTION_PASSWORD
    provided = payload.get("sensitive_password")
    if not isinstance(provided, str) or not secrets.compare_digest(provided, expected):
        raise ValueError("敏感操作密码错误")


def _frame_ancestors_header(frame_ancestors: tuple[str, ...]) -> str:
    sources = [source.strip() for source in frame_ancestors if source.strip()]
    if not sources:
        return ""
    return "frame-ancestors " + " ".join(sources)


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


def _requires_embedded_auth(path: str) -> bool:
    if path == "/" or path.startswith("/api/"):
        return True
    static_path = _static_path(path)
    if static_path is not None and static_path.is_file():
        return False
    return "." not in Path(path).name


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


def _single_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0].strip() if values else ""


def _origin(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def _validate_sub2api_admin(base_url: str, token: str, user_id: str) -> dict[str, str]:
    url = f"{base_url.rstrip('/')}/api/v1/user/profile"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=EMBEDDED_AUTH_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise _AuthError(HTTPStatus.BAD_GATEWAY, "failed to validate embedded token") from e
    if resp.status_code == 401:
        raise _AuthError(HTTPStatus.UNAUTHORIZED, "invalid embedded token")
    if resp.status_code >= 400:
        raise _AuthError(HTTPStatus.BAD_GATEWAY, "sub2api auth validation failed")
    try:
        body = resp.json()
    except ValueError as e:
        raise _AuthError(HTTPStatus.BAD_GATEWAY, "invalid sub2api auth response") from e
    profile = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(profile, dict):
        raise _AuthError(HTTPStatus.BAD_GATEWAY, "invalid sub2api auth response")
    profile_user_id = str(profile.get("id") or "").strip()
    if not profile_user_id or profile_user_id != user_id.strip():
        raise _AuthError(HTTPStatus.FORBIDDEN, "embedded user_id does not match token")
    if str(profile.get("role") or "").strip().lower() != "admin":
        raise _AuthError(HTTPStatus.FORBIDDEN, "admin access required")
    if str(profile.get("status") or "").strip().lower() != "active":
        raise _AuthError(HTTPStatus.FORBIDDEN, "user account is not active")
    return {"user_id": profile_user_id}


def _session_secret(admin_key: str) -> bytes:
    secret = os.environ.get("UI_SESSION_SECRET") or admin_key
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _make_session(user_id: str, secret: bytes) -> str:
    expires_at = str(int(time.time()) + EMBEDDED_SESSION_TTL_SECONDS)
    payload = f"{user_id}:{expires_at}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    raw = f"{payload}:{_b64(sig)}"
    return _b64(raw.encode("utf-8"))


def _verify_session(session: str, secret: bytes) -> str | None:
    if not session:
        return None
    try:
        raw = _b64decode(session).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    user_id, expires_at, sig = parts
    try:
        if int(expires_at) < int(time.time()):
            return None
    except ValueError:
        return None
    payload = f"{user_id}:{expires_at}"
    expected = _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


def _cookie_value(raw_cookie: str, name: str) -> str:
    if not raw_cookie:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


if __name__ == "__main__":
    main()
