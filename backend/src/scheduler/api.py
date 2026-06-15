"""sub2api Admin API 客户端。

响应包装统一为 {"code": 0, "message": "...", "data": ...}，
认证使用 x-api-key header（Admin API Key）。

平台数据源差异（源码核实）：
- anthropic: extra.session_window_utilization / passive_usage_7d_utilization 为 0-1，
  passive_usage_7d_reset 为 Unix 秒，5h reset 在账号字段 session_window_end
- openai(Codex OAuth): extra.codex_5h/7d_used_percent 为 0-100，
  codex_5h/7d_reset_at 与 codex_usage_updated_at 为 RFC3339
- active 探测响应两平台结构一致（five_hour/seven_day，utilization 0-100），
  sonnet 窗口仅 anthropic 存在
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import requests

from .models import AccountSnapshot

log = logging.getLogger(__name__)

PAGE_SIZE = 1000  # sub2api 分页上限


class AdminAPI:
    def __init__(self, base_url: str, admin_key: str, timeout: float = 30.0):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["x-api-key"] = admin_key

    def _get_data(self, resp: requests.Response) -> Any:
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"admin api error: code={body.get('code')} message={body.get('message')}")
        return body.get("data")

    def list_accounts(self, platform: str) -> list[dict]:
        """拉取全量账号（被动用量随 extra 返回），失败抛异常由调用方放弃本轮。"""
        items: list[dict] = []
        page = 1
        while True:
            resp = self.session.get(
                f"{self.base_url}/api/v1/admin/accounts",
                params={"platform": platform, "page": page, "page_size": PAGE_SIZE},
                timeout=self.timeout,
            )
            data = self._get_data(resp)
            batch = data.get("items") or []
            items.extend(batch)
            if page >= int(data.get("pages") or 1) or not batch:
                return items
            page += 1

    def probe_usage(self, account_id: int) -> dict | None:
        """active 探测：真实调用上游，成功后 sub2api 会回写被动缓存。失败返回 None。"""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/admin/accounts/{account_id}/usage",
                params={"source": "active", "force": "true"},
                timeout=self.timeout,
            )
            return self._get_data(resp)
        except Exception as e:
            log.warning("active probe failed account_id=%s: %s", account_id, e)
            return None

    def bulk_update_accounts(self, account_ids: list[int], fields: dict[str, Any]) -> bool:
        """批量局部更新账号字段。失败返回 False，不重试。"""
        payload = {"account_ids": account_ids, **fields}
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v1/admin/accounts/bulk-update",
                json=payload,
                timeout=self.timeout,
            )
            self._get_data(resp)
            return True
        except Exception as e:
            log.error("bulk-update failed fields=%s ids=%s: %s", fields, account_ids, e)
            return False

    def export_account_data(self, account_id: int) -> dict:
        """导出单账号原始数据，用于读取未脱敏 OAuth credentials。"""
        resp = self.session.get(
            f"{self.base_url}/api/v1/admin/accounts/data",
            params={"ids": str(account_id), "include_proxies": "true"},
            timeout=self.timeout,
        )
        return self._get_data(resp)

    def refresh_openai_account(self, account_id: int) -> dict:
        """刷新 OpenAI OAuth 账号。仅由邀请管理在 token 缺失或过期时触发。"""
        resp = self.session.post(
            f"{self.base_url}/api/v1/admin/openai/accounts/{account_id}/refresh",
            timeout=self.timeout,
        )
        return self._get_data(resp)


def _parse_time(value: Any) -> datetime | None:
    """解析 RFC3339 字符串或 Unix 秒为 UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _pct(value: Any, scale: float = 1.0) -> float | None:
    if value is None:
        return None
    try:
        return float(value) * scale
    except (TypeError, ValueError):
        return None


def parse_account(raw: dict, now: datetime, platform: str) -> AccountSnapshot:
    extra = raw.get("extra") or {}
    if platform == "openai":
        five_hour = _pct(extra.get("codex_5h_used_percent"))
        seven_day = _pct(extra.get("codex_7d_used_percent"))
        seven_day_reset = _parse_time(extra.get("codex_7d_reset_at"))
        five_hour_reset = _parse_time(extra.get("codex_5h_reset_at"))
        sampled_at = _parse_time(extra.get("codex_usage_updated_at"))
    else:  # anthropic：utilization 为 0-1
        five_hour = _pct(extra.get("session_window_utilization"), 100.0)
        seven_day = _pct(extra.get("passive_usage_7d_utilization"), 100.0)
        seven_day_reset = _parse_time(extra.get("passive_usage_7d_reset"))
        five_hour_reset = _parse_time(raw.get("session_window_end"))
        sampled_at = _parse_time(extra.get("passive_usage_sampled_at"))

    return AccountSnapshot(
        id=int(raw["id"]),
        name=raw.get("name") or "",
        type=raw.get("type") or "",
        priority=int(raw.get("priority") or 0),
        concurrency=int(raw.get("concurrency") or 0),
        load_factor=_int_or_none(raw.get("load_factor")),
        status=raw.get("status") or "",
        schedulable=bool(raw.get("schedulable")),
        rate_limited=_in_future(_parse_time(raw.get("rate_limit_reset_at")), now),
        overloaded=_in_future(_parse_time(raw.get("overload_until")), now),
        temp_unschedulable=_in_future(_parse_time(raw.get("temp_unschedulable_until")), now),
        five_hour_used=five_hour,
        seven_day_used=seven_day,
        seven_day_sonnet_used=None,  # 被动数据不含 sonnet（且仅 anthropic 有此窗口）
        seven_day_reset_at=seven_day_reset,
        five_hour_reset_at=five_hour_reset,
        sampled_at=sampled_at,
        usage_source="passive" if seven_day is not None and sampled_at is not None else "missing",
    )


def merge_probe(snap: AccountSnapshot, usage: dict, now: datetime) -> None:
    """将 active 探测结果（utilization 为 0-100，两平台同构）合并进快照。"""

    def window(name: str) -> dict:
        return usage.get(name) or {}

    five = window("five_hour")
    seven = window("seven_day")
    sonnet = window("seven_day_sonnet")
    if five.get("utilization") is not None:
        snap.five_hour_used = float(five["utilization"])
        snap.five_hour_reset_at = _parse_time(five.get("resets_at")) or snap.five_hour_reset_at
    if seven.get("utilization") is not None:
        snap.seven_day_used = float(seven["utilization"])
        snap.seven_day_reset_at = _parse_time(seven.get("resets_at")) or snap.seven_day_reset_at
    if sonnet.get("utilization") is not None:
        snap.seven_day_sonnet_used = float(sonnet["utilization"])
    snap.sampled_at = now
    snap.usage_source = "active"


def _in_future(dt: datetime | None, now: datetime) -> bool:
    return dt is not None and dt > now


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
