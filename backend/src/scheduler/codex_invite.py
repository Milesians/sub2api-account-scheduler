from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import requests

from .api import AdminAPI

REFERRAL_KEY = "codex_referral_persistent_invite"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_USER_AGENT = "Codex Desktop/0.0.0 (Linux; x86_64)"
DEFAULT_TIMEOUT = 8.0
MAX_EMAILS = 5
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InviteResetError(Exception):
    status = 400


class InviteResetUpstreamError(InviteResetError):
    status = 424


class CodexInviteReset:
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

    def status(self, account_id: int) -> dict[str, Any]:
        account = self._load_account(account_id)
        eligibility = self._get(account, "/referrals/invite/eligibility", {"referral_key": REFERRAL_KEY})
        rules = self._get(account, "/wham/referrals/eligibility_rules", {"referral_key": REFERRAL_KEY})
        credits_raw = self._get(account, "/wham/rate-limit-reset-credits")
        credits = _normalize_credits(credits_raw)
        available_count = _int(credits_raw.get("available_count"))
        if available_count == 0:
            available_count = sum(1 for credit in credits if (credit.get("status") or "").lower() == "available")
        return {
            "referral_key": REFERRAL_KEY,
            "invite_eligibility": eligibility,
            "eligibility_rules": _normalize_rules(rules),
            "requires_consent": _bool(eligibility.get("requires_explicit_confirmation"), True),
            "available_count": available_count,
            "credits": credits,
            "raw_eligibility_rules": rules,
            "raw_credits": credits_raw,
        }

    def send_invite(self, account_id: int, emails: list[str]) -> dict[str, Any]:
        account = self._load_account(account_id)
        normalized = _normalize_emails(emails)
        raw = self._post(account, "/wham/referrals/invite", {"referral_key": REFERRAL_KEY, "emails": normalized})
        return {
            "invites": _map_list(raw.get("invites")),
            "failed_emails": _string_list(raw.get("failed_emails")),
            "message": _string(raw.get("message")),
            "raw": raw,
        }

    def consume(self, account_id: int, credit_id: str) -> dict[str, Any]:
        account = self._load_account(account_id)
        credit_id = credit_id.strip()
        if not credit_id:
            raise InviteResetError("credit_id is required")
        redeem_request_id = str(uuid.uuid4())
        raw = self._post(
            account,
            "/wham/rate-limit-reset-credits/consume",
            {"credit_id": credit_id, "redeem_request_id": redeem_request_id},
        )
        result: dict[str, Any] = {
            "code": _string(raw.get("code")),
            "credit_id": credit_id,
            "redeem_request_id": redeem_request_id,
            "remaining_credits": _map_list(raw.get("credits")),
            "raw": raw,
        }
        if "available_count" in raw:
            result["available_count"] = _int(raw.get("available_count"))
        return result

    def _load_account(self, account_id: int) -> dict[str, Any]:
        data = self.admin_api.export_account_data(account_id)
        accounts = data.get("accounts") or []
        if not accounts:
            raise InviteResetError("account not found in exported data")
        account = accounts[0]
        if account.get("platform") != "openai" or account.get("type") != "oauth":
            raise InviteResetError("only OpenAI OAuth accounts support Codex invite reset")
        credentials = account.get("credentials") or {}
        if not isinstance(credentials, dict):
            raise InviteResetError("account credentials are invalid")
        token = _string(credentials.get("access_token"))
        if not _token_usable(credentials):
            self.admin_api.refresh_openai_account(account_id)
            data = self.admin_api.export_account_data(account_id)
            accounts = data.get("accounts") or []
            if accounts:
                account = accounts[0]
                credentials = account.get("credentials") or {}
                token = _string(credentials.get("access_token")) if isinstance(credentials, dict) else ""
        if not token:
            raise InviteResetError("missing OpenAI OAuth access token")
        account["_access_token"] = token
        account["_chatgpt_account_id"] = _string(credentials.get("chatgpt_account_id"))
        account["_proxy_url"] = _proxy_url(data, account.get("proxy_key"))
        return account

    def _get(self, account: dict[str, Any], path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("GET", account, path, query=query)

    def _post(self, account: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", account, path, body=body)

    def _request(
        self,
        method: str,
        account: dict[str, Any],
        path: str,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path, query)
        headers = {
            "Authorization": f"Bearer {account['_access_token']}",
            "Accept": "application/json",
            "Host": "chatgpt.com",
            "OAI-Language": "zh-CN",
            "originator": "Codex Desktop",
            "X-OpenAI-Attach-Auth": "1",
            "X-OpenAI-Attach-Integrity-State": "1",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if account.get("_chatgpt_account_id"):
            headers["chatgpt-account-id"] = account["_chatgpt_account_id"]
        proxy_url = account.get("_proxy_url")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = self.session.request(method, url, headers=headers, json=body, proxies=proxies, timeout=self.timeout)
        except requests.RequestException as e:
            raise InviteResetUpstreamError(f"codex invite reset request failed: {e}") from e
        if resp.status_code < 200 or resp.status_code >= 300:
            raise InviteResetUpstreamError(
                f"codex invite reset upstream returned {resp.status_code}: {resp.text[:500]}"
            )
        if not resp.content.strip():
            return {}
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise InviteResetUpstreamError(f"decode codex invite reset response failed: {e}") from e
        if not isinstance(data, dict):
            raise InviteResetUpstreamError("codex invite reset response must be a JSON object")
        return data

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query)}"
        return url


def _token_usable(credentials: dict[str, Any]) -> bool:
    token = _string(credentials.get("access_token"))
    if not token:
        return False
    expires_at = _parse_time(credentials.get("expires_at"))
    return expires_at is None or expires_at > datetime.now(UTC) + timedelta(minutes=5)


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


def _normalize_emails(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for email in re.split(r"[,\s;]+", str(value)):
            email = email.strip()
            if not email:
                continue
            key = email.lower()
            if key in seen:
                continue
            if not EMAIL_PATTERN.match(email):
                raise InviteResetError(f"invalid email: {email}")
            seen.add(key)
            result.append(email)
            if len(result) > MAX_EMAILS:
                raise InviteResetError(f"最多一次邀请 {MAX_EMAILS} 个邮箱")
    if not result:
        raise InviteResetError("emails are required")
    return result


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


def _normalize_credits(raw: dict[str, Any]) -> list[dict[str, Any]]:
    credits: list[dict[str, Any]] = []
    for item in _map_list(raw.get("credits")):
        credit_id = _string(item.get("id"))
        if not credit_id:
            continue
        credits.append({
            "id": credit_id,
            "status": _string(item.get("status")),
            "title": _string(item.get("title")),
            "description": _string(item.get("description")),
            "expires_at": _credit_expires_at(item),
            "profile_user_id": _string(item.get("profile_user_id")),
            "profile_image_url": _string(item.get("profile_image_url")),
            "raw": item,
        })
    return credits


def _credit_expires_at(raw: dict[str, Any]) -> str:
    for key in ("expires_at", "expired_at", "expiration_time", "expiresAt", "expiredAt", "expirationTime"):
        value = raw.get(key)
        parsed = _parse_time(value)
        if parsed is not None:
            return parsed.isoformat().replace("+00:00", "Z")
        text = _string(value)
        if text:
            return text
    return ""


def _normalize_rules(raw: dict[str, Any]) -> list[str]:
    rules: list[str] = []
    values = raw.get("rules")
    if not isinstance(values, list):
        return rules
    for item in values:
        if isinstance(item, str) and item.strip():
            rules.append(item.strip())
        elif isinstance(item, dict):
            for key in ("text", "description", "message", "title"):
                text = _string(item.get(key))
                if text:
                    rules.append(text)
                    break
    return rules


def _map_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_string(item) for item in value) if text]


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback
