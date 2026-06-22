"""每轮 tick 编排：拉数据 -> 名称过滤 -> 主动探测 -> 决策 -> bulk-update -> 落库 -> 老化 -> 心跳。"""

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


def tick(cfg: Config, api: AdminAPI, store: Store) -> bool:
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

    probed = _probe_active_usage(managed, states, cfg, api, now)
    store.attach_recent_5h_burn(managed)
    terminal_active = any(_is_terminal_snapshot(s, cfg, now) for s in eligible)

    decisions, new_states = decide(eligible, states, cfg, now)

    for d in sorted(decisions, key=lambda x: x.account_id):
        log.info(
            "run=%s decision account=%d(%s) priority=%d->%d load_factor=%d->%d "
            "mode=%s reason=%s 7d=%s sonnet=%s 5h=%s catchup=%s burn=%s cap=%s "
            "drain_gap=%s drain_rate=%s drain_pressure=%s drain_level=%s deadline=%s src=%s",
            run_id, d.account_id, d.name, d.current_priority, d.target_priority,
            d.current_load_factor, d.target_load_factor, d.mode, d.reason,
            _fmt(d.seven_day_used), _fmt(d.seven_day_sonnet_used), _fmt(d.five_hour_used),
            _fmt(d.catchup_score), _fmt(d.recent_hour_burn), _fmt(d.safe_hour_cap),
            _fmt(d.drain_gap), _fmt(d.drain_required_rate), _fmt(d.drain_pressure), d.drain_level or "-",
            _fmt(d.deadline_hours), d.usage_source,
        )

    updated = 0
    snap_by_id = {s.id: s for s in managed}
    update_groups: dict[tuple[int, int], list[int]] = {}
    for d in decisions:
        if d.changed:
            update_groups.setdefault((d.target_priority, d.target_load_factor), []).append(d.account_id)

    for (priority, load_factor), ids in update_groups.items():
        if api.bulk_update_accounts(ids, {"priority": priority, "load_factor": load_factor}):
            updated += len(ids)
            for account_id in ids:
                if account_id in snap_by_id:
                    snap_by_id[account_id].priority = priority
                    snap_by_id[account_id].load_factor = load_factor
        else:
            # 更新失败不重试；回退 last_priority，下轮以 list 真实值重新决策
            for account_id in ids:
                if account_id in new_states:
                    new_states[account_id].last_priority = None

    _save_managed_states(managed, states, new_states, now)
    store.save_states(list(new_states.values()), now)
    deleted = store.delete_absent_accounts([s.id for s in managed])
    store.add_samples(managed, decisions)
    store.add_decisions(run_id, decisions, now)
    store.prune(cfg.sample_retention_days, cfg.decision_retention_days, cfg.state_retention_days)

    log.info(
        "run=%s done probes=%d updated=%d deleted_absent=%d terminal_active=%s",
        run_id, probed, updated, deleted, terminal_active,
    )
    return terminal_active


def _probe_active_usage(
    snaps: list[AccountSnapshot],
    states: dict[int, AccountState],
    cfg: Config,
    api: AdminAPI,
    now: datetime,
) -> int:
    """主动探测 managed 账号用量，决策只使用本轮 active 数据。

    探测响应里才有 sonnet 窗口数据；探测同时触发 sub2api 回写被动缓存。
    """
    probed = 0
    for snap in sorted(snaps, key=lambda s: _probe_score(s, states.get(s.id), cfg, now), reverse=True):
        state = states.setdefault(snap.id, AccountState(account_id=snap.id))
        state.last_probe_attempt_at = now
        _clear_passive_usage(snap)
        usage = api.probe_usage(snap.id)
        probed += 1
        if usage is not None:
            merge_probe(snap, usage, now)
            state.probe_failures = 0
        else:
            state.probe_failures += 1
    return probed


def _clear_passive_usage(snap: AccountSnapshot) -> None:
    snap.five_hour_used = None
    snap.seven_day_used = None
    snap.seven_day_sonnet_used = None
    snap.seven_day_reset_at = None
    snap.five_hour_reset_at = None
    snap.sampled_at = None
    snap.usage_source = "missing"


def _is_terminal_snapshot(snap: AccountSnapshot, cfg: Config, now: datetime) -> bool:
    if not cfg.terminal_drain_enabled or snap.seven_day_reset_at is None:
        return False
    remaining_h = (snap.seven_day_reset_at - now).total_seconds() / 3600.0
    return 0.0 < remaining_h <= cfg.terminal_window_hours


def _probe_score(snap: AccountSnapshot, state: AccountState | None, cfg: Config, now: datetime) -> float:
    score = 0.0
    no_data = snap.sampled_at is None or snap.seven_day_used is None
    if no_data:
        score += 1000.0

    terminal = _is_terminal_snapshot(snap, cfg, now)
    if snap.seven_day_reset_at is not None:
        remaining_h = max((snap.seven_day_reset_at - now).total_seconds() / 3600.0, 0.0)
        if terminal:
            score += 500.0
            score += max(0.0, cfg.terminal_window_hours - remaining_h) * 10.0

    if snap.seven_day_used is not None:
        gap = max(0.0, cfg.drain_target_7d_utilization - snap.seven_day_used)
        score += gap * 20.0
        if terminal and snap.priority in (cfg.band_protect, cfg.band_floor) and gap > cfg.terminal_done_band_pp:
            score += 100.0

    if snap.effective_load_factor > snap.base_load_factor:
        score += 80.0

    if snap.sampled_at is not None:
        stale_minutes = max(0.0, (now - snap.sampled_at).total_seconds() / 60.0)
        score += min(stale_minutes, 360.0) / 10.0

    if state is not None:
        score -= min(state.probe_failures, 5) * 50.0
    return score


def _save_managed_states(
    managed: list[AccountSnapshot],
    states: dict[int, AccountState],
    new_states: dict[int, AccountState],
    now: datetime,
) -> None:
    for snap in managed:
        state = new_states.get(snap.id)
        if state is None:
            state = states.get(snap.id) or AccountState(account_id=snap.id)
            state.last_priority = snap.priority
            new_states[snap.id] = state
        state.current_priority = snap.priority
        state.current_load_factor = snap.effective_load_factor
        if snap.seven_day_used is not None and snap.sampled_at is not None:
            state.last_7d_used = snap.seven_day_used
            state.last_5h_used = snap.five_hour_used
            state.last_7d_reset_at = snap.seven_day_reset_at
            state.last_sampled_at = snap.sampled_at


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
            terminal_active = False
            try:
                terminal_active = tick(cfg, api, store)
                _touch_heartbeat(cfg.heartbeat_file)
            except Exception:
                log.exception("tick failed")
            if once:
                return
            time.sleep(_sleep_minutes(cfg, terminal_active) * 60)
    finally:
        store.close()


def _sleep_minutes(cfg: Config, terminal_active: bool) -> int:
    if terminal_active and cfg.terminal_drain_enabled:
        return max(1, cfg.terminal_interval_minutes)
    return max(1, cfg.interval_minutes)


def _touch_heartbeat(path: str) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError as e:
        log.warning("heartbeat touch failed: %s", e)
