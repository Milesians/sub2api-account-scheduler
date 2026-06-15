"""每轮 tick 编排：拉数据 -> 名称过滤 -> 按需探测 -> 决策 -> bulk-update -> 落库 -> 老化 -> 心跳。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .api import AdminAPI, merge_probe, parse_account
from .config import Config
from .models import AccountSnapshot, AccountState
from .openai_subscription import (
    OpenAISubscriptionClient,
    apply_profile,
    has_profile_value,
    merge_profile,
    profile_from_account_raw,
)
from .policy import decide
from .store import Store

log = logging.getLogger(__name__)

# 仅这些账号类型有 5h/7d 窗口数据；其余类型（apikey / upstream 等）无窗口概念，
# 纳入只会浪费探测名额，直接排除在受控范围外
MANAGED_ACCOUNT_TYPES = {
    "anthropic": ("oauth", "setup-token"),
    "openai": ("oauth",),
}


def tick(cfg: Config, api: AdminAPI, store: Store) -> None:
    now = datetime.now(UTC)
    run_id = uuid.uuid4().hex[:12]
    states = store.load_states()

    raw_accounts = api.list_accounts(cfg.platform)
    snaps = [parse_account(a, now, cfg.platform) for a in raw_accounts]

    managed_types = MANAGED_ACCOUNT_TYPES[cfg.platform]
    managed = [
        s for s in snaps
        if s.type in managed_types and _name_matches(cfg.account_name_pattern, s.name)
    ]
    controls = store.load_account_controls([s.id for s in managed])
    paused_ids = {account_id for account_id, control in controls.items() if control.paused}
    eligible = [s for s in managed if s.eligible and s.id not in paused_ids]
    _sync_account_profiles(managed, raw_accounts, store, cfg, api, now)
    log.info(
        "run=%s accounts total=%d managed=%d eligible=%d paused=%d",
        run_id, len(snaps), len(managed), len(eligible), len(paused_ids),
    )

    probed = _probe_stale(eligible, states, cfg, api, now)
    store.attach_recent_5h_burn(eligible)

    decisions, new_states = decide(eligible, states, cfg, now)

    for d in sorted(decisions, key=lambda x: x.account_id):
        log.info(
            "run=%s decision account=%d(%s) priority=%d->%d load_factor=%d->%d "
            "reason=%s 7d=%s sonnet=%s 5h=%s catchup=%s burn=%s cap=%s src=%s",
            run_id, d.account_id, d.name, d.current_priority, d.target_priority,
            d.current_load_factor, d.target_load_factor, d.reason,
            _fmt(d.seven_day_used), _fmt(d.seven_day_sonnet_used), _fmt(d.five_hour_used),
            _fmt(d.catchup_score), _fmt(d.recent_hour_burn), _fmt(d.safe_hour_cap), d.usage_source,
        )

    updated = 0
    update_groups: dict[tuple[int, int], list[int]] = {}
    for d in decisions:
        if d.changed:
            update_groups.setdefault((d.target_priority, d.target_load_factor), []).append(d.account_id)

    for (priority, load_factor), ids in update_groups.items():
        if api.bulk_update_accounts(ids, {"priority": priority, "load_factor": load_factor}):
            updated += len(ids)
        else:
            # 更新失败不重试；回退 last_priority，下轮以 list 真实值重新决策
            for account_id in ids:
                if account_id in new_states:
                    new_states[account_id].last_priority = None

    _save_managed_states(managed, states, new_states, now)
    store.save_states(list(new_states.values()), now)
    store.add_samples(eligible, decisions)
    store.add_decisions(run_id, decisions, now)
    store.prune(cfg.sample_retention_days, cfg.decision_retention_days, cfg.state_retention_days)

    log.info("run=%s done probes=%d updated=%d", run_id, probed, updated)


def _probe_stale(
    eligible: list[AccountSnapshot],
    states: dict[int, AccountState],
    cfg: Config,
    api: AdminAPI,
    now: datetime,
) -> int:
    """对被动数据缺失/过期的账号做限量 active 探测，结果直接合并进快照。

    探测响应里才有 sonnet 窗口数据；探测同时触发 sub2api 回写被动缓存。
    按 sampled_at 最旧优先，超出名额的下轮自然轮转。
    """
    threshold = timedelta(minutes=cfg.usage_stale_threshold_minutes)
    stale = [
        s for s in eligible
        if s.sampled_at is None or s.seven_day_used is None or (now - s.sampled_at) > threshold
    ]
    stale.sort(key=lambda s: s.sampled_at or datetime.min.replace(tzinfo=UTC))

    probed = 0
    for snap in stale[: cfg.max_active_probes_per_round]:
        state = states.setdefault(snap.id, AccountState(account_id=snap.id))
        usage = api.probe_usage(snap.id)
        probed += 1
        if usage is not None:
            merge_probe(snap, usage, now)
            state.probe_failures = 0
        else:
            state.probe_failures += 1
    return probed


def _save_managed_states(
    managed: list[AccountSnapshot],
    states: dict[int, AccountState],
    new_states: dict[int, AccountState],
    now: datetime,
) -> None:
    for snap in managed:
        if snap.id in new_states:
            continue
        state = states.get(snap.id) or AccountState(account_id=snap.id)
        state.last_priority = snap.priority
        state.last_7d_used = snap.seven_day_used
        state.last_5h_used = snap.five_hour_used
        state.last_7d_reset_at = snap.seven_day_reset_at
        state.last_sampled_at = snap.sampled_at or now
        new_states[snap.id] = state


def _sync_account_profiles(
    snaps: list[AccountSnapshot],
    raw_accounts: list[dict],
    store: Store,
    cfg: Config,
    api: AdminAPI,
    now: datetime,
) -> None:
    if cfg.platform != "openai" or not snaps:
        return

    by_id = {int(a["id"]): a for a in raw_accounts if a.get("id") is not None}
    cached = store.load_account_profiles([s.id for s in snaps])
    raw_profiles = {
        s.id: profile_from_account_raw(s.id, by_id.get(s.id, {}))
        for s in snaps
    }

    seed_profiles = []
    for snap in snaps:
        merged = merge_profile(cached.get(snap.id), raw_profiles[snap.id])
        if has_profile_value(merged):
            apply_profile(snap, merged)
        if has_profile_value(raw_profiles[snap.id]) and snap.id not in cached:
            seed_profiles.append(raw_profiles[snap.id])
    store.save_account_profiles(seed_profiles, now)

    if not cfg.account_profile_refresh_enabled:
        return

    ttl = timedelta(minutes=max(1, cfg.account_profile_ttl_minutes))
    stale = [
        s for s in snaps
        if s.id not in cached
        or cached[s.id].profile_updated_at is None
        or now - cached[s.id].profile_updated_at > ttl
        or (
            cached[s.id].subscription_expires_at is None
            and cached[s.id].subscription_plan.lower() != "free"
        )
    ]
    if not stale:
        return

    client = OpenAISubscriptionClient(api, cfg.openai_subscription_base_url)
    refreshed = []
    for snap in stale:
        try:
            profile = client.fetch(snap.id, now)
        except Exception as e:
            log.warning("OpenAI subscription fetch failed account_id=%s: %s", snap.id, e)
            continue
        merged = merge_profile(cached.get(snap.id), profile)
        apply_profile(snap, merged)
        refreshed.append(merged)
    store.save_account_profiles(refreshed, now)


def _name_matches(pattern: str, name: str) -> bool:
    return not pattern or re.search(pattern, name) is not None


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def run(cfg: Config, once: bool = False) -> None:
    api = AdminAPI(cfg.base_url, cfg.admin_key)
    store = Store(cfg.db_path)
    try:
        while True:
            try:
                tick(cfg, api, store)
                _touch_heartbeat(cfg.heartbeat_file)
            except Exception:
                log.exception("tick failed")
            if once:
                return
            time.sleep(cfg.interval_minutes * 60)
    finally:
        store.close()


def _touch_heartbeat(path: str) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError as e:
        log.warning("heartbeat touch failed: %s", e)
