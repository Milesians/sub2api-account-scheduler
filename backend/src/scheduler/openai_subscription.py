from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import requests

from .api import AdminAPI
from .models import AccountProfile, AccountSnapshot

DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_TIMEOUT = 8.0
DEFAULT_USER_AGENT = "Codex Desktop/0.0.0 (Linux; x86_64)"

class OpenAISubscriptionError(Exception):
    pass


class OpenAISubscriptionClient:
    def __init__(
        self,
        admin_api: AdminAPI,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ):
        self.admin_api = admin_api
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, account_id: int, now: datetime) -> AccountProfile:
        """读取官方 OpenAI subscription。只读，不刷新 token。"""
        data = self.admin_api.export_account_data(account_id)
        accounts = data.get("accounts") or []
        if not accounts or not isinstance(accounts[0], dict):
            raise OpenAISubscriptionError("account not found in exported data")

        account = accounts[0]
        profile = profile_from_account_raw(account_id, account)
        profile.profile_updated_at = now

        if account.get("platform") != "openai" or account.get("type") != "oauth":
            return profile

        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        access_token = _string(credentials.get("access_token"))
        chatgpt_account_id = _chatgpt_account_id(account, credentials)
        if not access_token or not chatgpt_account_id:
            profile.subscription_status = profile.subscription_status or "unknown"
            profile.subscription_error = "missing OpenAI OAuth access token or account id"
            return profile

        raw = self._request_subscription(data, account, access_token, chatgpt_account_id)
        active_until = _parse_time(raw.get("active_until") or raw.get("expires_at"))
        profile.subscription_plan = _string(raw.get("plan_type")) or profile.subscription_plan
        profile.subscription_expires_at = active_until or profile.subscription_expires_at
        profile.subscription_status = _derive_status(raw, profile.subscription_plan, profile.subscription_expires_at, now)
        profile.subscription_error = ""
        return profile

    def _request_subscription(
        self,
        data: dict[str, Any],
        account: dict[str, Any],
        access_token: str,
        chatgpt_account_id: str,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Host": "chatgpt.com",
            "OAI-Language": "zh-CN",
            "originator": "Codex Desktop",
            "X-OpenAI-Attach-Auth": "1",
            "X-OpenAI-Attach-Integrity-State": "1",
            "User-Agent": DEFAULT_USER_AGENT,
            "chatgpt-account-id": chatgpt_account_id,
        }
        proxy_url = _proxy_url(data, account.get("proxy_key"))
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = self.session.get(
                f"{self.base_url}/subscriptions",
                params={"account_id": chatgpt_account_id},
                headers=headers,
                proxies=proxies,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OpenAISubscriptionError(f"OpenAI subscription request failed: {e}") from e
        if resp.status_code < 200 or resp.status_code >= 300:
            raise OpenAISubscriptionError(f"OpenAI subscription returned {resp.status_code}: {resp.text[:500]}")
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise OpenAISubscriptionError(f"decode OpenAI subscription response failed: {e}") from e
        if not isinstance(body, dict):
            raise OpenAISubscriptionError("OpenAI subscription response must be a JSON object")
        return body


def profile_from_account_raw(account_id: int, raw: dict[str, Any]) -> AccountProfile:
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    payload = _jwt_payload(_string(credentials.get("id_token")))
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
    if not isinstance(auth, dict):
        auth = {}

    email = _first_string(
        raw.get("email"),
        extra.get("email"),
        credentials.get("email"),
        payload.get("email") if isinstance(payload, dict) else None,
    )
    plan = _first_string(
        raw.get("subscription_plan"),
        raw.get("plan_type"),
        extra.get("subscription_plan"),
        extra.get("plan_type"),
        extra.get("chatgpt_plan_type"),
        credentials.get("subscription_plan"),
        credentials.get("plan_type"),
        credentials.get("chatgpt_plan_type"),
        auth.get("chatgpt_plan_type"),
    )
    status = _first_string(
        raw.get("subscription_status"),
        extra.get("subscription_status"),
        credentials.get("subscription_status"),
    )
    expires_at = _first_time(
        raw.get("subscription_expires_at"),
        raw.get("active_until"),
        extra.get("subscription_expires_at"),
        extra.get("active_until"),
        credentials.get("subscription_expires_at"),
        credentials.get("active_until"),
    )
    return AccountProfile(
        account_id=account_id,
        email=email,
        subscription_plan=plan,
        subscription_status=status,
        subscription_expires_at=expires_at,
    )


def apply_profile(snapshot: AccountSnapshot, profile: AccountProfile) -> None:
    snapshot.email = profile.email
    snapshot.subscription_plan = profile.subscription_plan
    snapshot.subscription_status = profile.subscription_status
    snapshot.subscription_expires_at = profile.subscription_expires_at
    snapshot.profile_updated_at = profile.profile_updated_at
    snapshot.subscription_error = profile.subscription_error


def merge_profile(base: AccountProfile | None, update: AccountProfile) -> AccountProfile:
    if base is None:
        return update
    refreshed = update.profile_updated_at is not None
    successful_refresh = refreshed and not update.subscription_error
    error = update.subscription_error if refreshed else (
        update.subscription_error or base.subscription_error
    )
    return AccountProfile(
        account_id=update.account_id,
        email=update.email or base.email,
        subscription_plan=update.subscription_plan or base.subscription_plan,
        subscription_status=update.subscription_status if successful_refresh else (
            update.subscription_status or base.subscription_status
        ),
        subscription_expires_at=update.subscription_expires_at if successful_refresh else (
            update.subscription_expires_at or base.subscription_expires_at
        ),
        profile_updated_at=update.profile_updated_at or base.profile_updated_at,
        subscription_error=error,
    )


def has_profile_value(profile: AccountProfile) -> bool:
    return any((
        profile.email,
        profile.subscription_plan,
        profile.subscription_status,
        profile.subscription_expires_at is not None,
        profile.subscription_error,
    ))


def _chatgpt_account_id(account: dict[str, Any], credentials: dict[str, Any]) -> str:
    payload = _jwt_payload(_string(credentials.get("id_token")))
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else {}
    if not isinstance(auth, dict):
        auth = {}
    return _first_string(
        credentials.get("chatgpt_account_id"),
        account.get("chatgpt_account_id"),
        auth.get("chatgpt_account_id"),
    )


def _derive_status(raw: dict[str, Any], plan: str, expires_at: datetime | None, now: datetime) -> str:
    status = _string(raw.get("status") or raw.get("subscription_status"))
    if status:
        return status
    if expires_at is not None:
        return "active" if expires_at > now else "expired"
    if plan.lower() == "free":
        return "free"
    if plan:
        return "unknown"
    return ""


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        body = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _first_time(*values: Any) -> datetime | None:
    for value in values:
        dt = _parse_time(value)
        if dt is not None:
            return dt
    return None


def _first_string(*values: Any) -> str:
    for value in values:
        text = _string(value)
        if text:
            return text
    return ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _proxy_url(data: dict[str, Any], proxy_key: Any) -> str:
    key = _string(proxy_key)
    if not key:
        return ""
    proxies = data.get("proxies")
    if not isinstance(proxies, list):
        return ""
    for proxy in proxies:
        if not isinstance(proxy, dict) or _string(proxy.get("proxy_key")) != key:
            continue
        protocol = _string(proxy.get("protocol")) or "http"
        host = _string(proxy.get("host"))
        port = _int(proxy.get("port"))
        if not host or port <= 0:
            return ""
        username = quote(_string(proxy.get("username")), safe="")
        password = quote(_string(proxy.get("password")), safe="")
        auth = f"{username}:{password}@" if username or password else ""
        return f"{protocol}://{auth}{host}:{port}"
    return ""
