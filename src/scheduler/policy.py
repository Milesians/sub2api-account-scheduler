"""档位决策纯函数：输入账号快照 + 历史状态 + 配置 + now，输出决策与新状态。

无任何 IO，便于离线单测。priority 档位为严格排队信号（数值小优先），
控制器只做账号分层，档内分散与请求级硬保护交给 sub2api。
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Mapping

from .config import Config
from .models import AccountSnapshot, AccountState, Decision

EWMA_ALPHA = 0.3          # 新样本权重
MIN_BURN_INTERVAL_H = 0.25  # 两次采样间隔过短不更新 burn，避免噪声放大


def decide(
    snapshots: list[AccountSnapshot],
    states: Mapping[int, AccountState],
    cfg: Config,
    now: datetime,
) -> tuple[list[Decision], dict[int, AccountState]]:
    decisions: dict[int, Decision] = {}
    new_states: dict[int, AccountState] = {}
    ranking: list[_Ranked] = []

    for snap in snapshots:
        old = states.get(snap.id) or AccountState(account_id=snap.id)
        state = replace(old)
        new_states[snap.id] = state

        d = Decision(
            account_id=snap.id,
            name=snap.name,
            current_priority=snap.priority,
            target_priority=snap.priority,
            current_load_factor=snap.effective_load_factor,
            target_load_factor=snap.effective_load_factor,
            reason="",
            seven_day_used=snap.seven_day_used,
            seven_day_sonnet_used=snap.seven_day_sonnet_used,
            five_hour_used=snap.five_hour_used,
            usage_source=snap.usage_source,
        )
        decisions[snap.id] = d

        first_takeover_invalid = state.last_priority is None and snap.priority not in cfg.priority_bands

        # 数据缺失 / 过期：保持当前档位，不降档（冷账号死锁防护），不更新基线。
        # 首次接管非法档位例外：先归一到 normal，避免外部旧 priority 持续吸流。
        if snap.seven_day_used is None or snap.seven_day_reset_at is None or snap.sampled_at is None:
            if first_takeover_invalid:
                _finalize(d, state, snap, cfg.band_normal, "takeover")
            else:
                d.reason = "no_data_hold"
            continue
        if (now - snap.sampled_at) > timedelta(minutes=cfg.usage_stale_threshold_minutes):
            if first_takeover_invalid:
                _finalize(d, state, snap, cfg.band_normal, "takeover")
            else:
                d.reason = "stale_hold"
            continue

        seven_day = max(snap.seven_day_used, snap.seven_day_sonnet_used or 0.0)
        remaining_h_raw = (snap.seven_day_reset_at - now).total_seconds() / 3600.0
        if remaining_h_raw <= 0.0 or remaining_h_raw > cfg.window_hours:
            if first_takeover_invalid:
                _finalize(d, state, snap, cfg.band_normal, "takeover")
            else:
                d.reason = "invalid_reset_hold"
            continue

        previous_burn_ewma = state.hourly_burn_ewma
        recent_burn = _update_burn(state, snap)
        _update_baseline(state, snap)

        remaining_h = max(remaining_h_raw, 0.1)
        rate_5h = snap.recent_5h_burn if snap.recent_5h_burn is not None else previous_burn_ewma
        metrics = _metrics(cfg, seven_day, remaining_h, recent_burn, rate_5h)
        cap = max(cfg.cooldown_abs_rate_pph, cfg.cooldown_required_rate_multiplier * metrics.required_rate)
        d.safe_hour_cap = cap
        _attach_metrics(d, metrics, remaining_h, recent_burn, snap.recent_5h_burn)

        # 硬规则：允许跳档直接到位
        if seven_day >= cfg.hard_cap_7d_utilization:
            _finalize(d, state, snap, cfg.band_floor, "hard_cap_7d")
            continue
        if snap.five_hour_used is not None and snap.five_hour_used >= cfg.hard_cap_5h_utilization:
            _finalize(d, state, snap, cfg.band_floor, "hard_cap_5h")
            continue
        if seven_day >= cfg.target_7d_utilization:
            _finalize(d, state, snap, cfg.band_protect, "protect_7d")
            continue
        if state.cooldown_until is not None and state.cooldown_until > now:
            target = _most_protective(snap.priority, cfg.band_protect, cfg)
            _finalize(d, state, snap, target, "cooldown_hold")
            continue
        if _needs_cooldown(cfg, seven_day, remaining_h, recent_burn, metrics, cap):
            state.cooldown_until = now + timedelta(minutes=cfg.cooldown_minutes)
            reason = (
                "new_cooldown_will_hit_goal"
                if _will_hit_goal_soon(cfg, seven_day, remaining_h, recent_burn)
                else "new_cooldown"
            )
            _finalize(d, state, snap, cfg.band_protect, reason)
            continue
        if metrics.ahead >= cfg.ahead_band_pp and metrics.projected_end >= cfg.target_7d_utilization:
            _finalize(d, state, snap, cfg.band_protect, "ahead_protect")
            continue
        if first_takeover_invalid:
            _finalize(d, state, snap, cfg.band_normal, "takeover")
            continue

        ranking.append(
            _Ranked(
                snap=snap,
                decision=d,
                state=state,
                seven_day=seven_day,
                remaining_h=remaining_h,
                metrics=metrics,
            )
        )

    _rank_and_assign(ranking, len(snapshots), cfg, now)
    return list(decisions.values()), new_states


class _Metrics:
    def __init__(
        self,
        target_now: float,
        final_gap: float,
        required_rate: float,
        recent_rate: float,
        projected_end: float,
        ahead: float,
    ):
        self.target_now = target_now
        self.final_gap = final_gap
        self.required_rate = required_rate
        self.recent_rate = recent_rate
        self.projected_end = projected_end
        self.ahead = ahead


class _Ranked:
    def __init__(self, snap, decision, state, seven_day, remaining_h, metrics):
        self.snap: AccountSnapshot = snap
        self.decision: Decision = decision
        self.state: AccountState = state
        self.seven_day = seven_day
        self.remaining_h = remaining_h
        self.metrics: _Metrics = metrics
        self.pace_error = 0.0
        self.projected_gap = 0.0
        self.speed_gap = 0.0
        self.catchup = 0.0
        self.clearly_need_boost = False


def _rank_and_assign(ranking: list[_Ranked], eligible_total: int, cfg: Config, now: datetime) -> None:
    if not ranking:
        return
    for r in ranking:
        r.pace_error = r.metrics.target_now - r.seven_day
        r.projected_gap = cfg.target_7d_utilization - r.metrics.projected_end
        r.speed_gap = r.metrics.required_rate - r.metrics.recent_rate
        urgency = 1.0 + _clamp((cfg.emergency_window_hours - r.remaining_h) / cfg.emergency_window_hours, 0.0, 1.0)
        r.catchup = urgency * (
            max(0.0, r.pace_error)
            + 0.7 * max(0.0, r.projected_gap)
            + 6.0 * max(0.0, r.speed_gap)
        )
        r.clearly_need_boost = (
            r.metrics.projected_end < cfg.emergency_projected_end_threshold
            or r.metrics.final_gap >= cfg.emergency_final_gap_pp
            or r.metrics.required_rate >= r.metrics.recent_rate + cfg.emergency_rate_gap_pph
        )
        r.decision.catchup_score = round(r.catchup, 4)

    eligible_count = len(ranking)
    strong_cap = max(cfg.max_boost_min, math.ceil(eligible_count * cfg.max_boost_ratio))
    mild_cap = math.ceil(eligible_count * cfg.mild_boost_ratio)
    very_old = datetime.min.replace(tzinfo=now.tzinfo)
    ranked = sorted(
        ranking,
        key=lambda r: (-r.catchup, r.seven_day, r.remaining_h, r.state.last_boost_at or very_old, r.snap.id),
    )

    strong_candidates = [r for r in ranked if r.catchup >= cfg.strong_score_threshold]
    strong = strong_candidates[:strong_cap]

    strong_ids = {r.snap.id for r in strong}
    mild: list[_Ranked] = []
    for r in ranked:
        if r.snap.id in strong_ids:
            continue
        if r.catchup >= cfg.mild_score_threshold and len(mild) < mild_cap:
            mild.append(r)
    mild_ids = {r.snap.id for r in mild}

    for r in ranking:
        if r.snap.id in strong_ids:
            _assign(r, r.decision, cfg.band_normal, "boost", cfg)
        elif r.snap.id in mild_ids:
            _assign(r, r.decision, cfg.band_normal, "mild_boost", cfg)
        else:
            _assign(r, r.decision, cfg.band_normal, "normal", cfg)


def _assign(r: _Ranked, d: Decision, target: int, reason: str, cfg: Config) -> None:
    bands = cfg.priority_bands
    current = d.current_priority if d.current_priority in bands else cfg.band_normal
    ci, ti = bands.index(current), bands.index(target)
    # 防抖：向高流量方向默认最多一档；保护方向直接生效。
    if ti < ci:
        emergency = r.remaining_h < cfg.emergency_window_hours and r.clearly_need_boost
        if not emergency:
            target = bands[ci - 1]
        elif ci - ti > 1:
            reason = f"{reason}_emergency_jump"
    d.target_priority = target
    d.reason = reason
    d.target_load_factor = _load_factor_for_reason(r, target, reason, cfg)
    r.state.last_priority = target
    if d.target_load_factor > r.snap.base_load_factor:
        r.state.last_boost_at = r.snap.sampled_at


def _finalize(d: Decision, state: AccountState, snap: AccountSnapshot, target: int, reason: str) -> None:
    d.target_priority = target
    d.target_load_factor = snap.base_load_factor
    d.reason = reason
    state.last_priority = target


def _load_factor_for_reason(r: _Ranked, target: int, reason: str, cfg: Config) -> int:
    base = r.snap.base_load_factor
    multiplier = 1.0
    if target != cfg.band_normal:
        return base
    if reason.startswith("boost"):
        multiplier = cfg.boost_load_factor_multiplier
    elif reason == "mild_boost":
        multiplier = cfg.mild_load_factor_multiplier
    return max(1, min(cfg.max_load_factor, math.ceil(base * multiplier)))


def _update_burn(state: AccountState, snap: AccountSnapshot) -> float | None:
    """计算本次小时消耗并更新 EWMA。返回未平滑的 recent burn（无法计算时 None）。"""
    if state.last_sampled_at is None or state.last_7d_used is None:
        return None
    if snap.sampled_at is None or snap.seven_day_used is None:
        return None
    # 7d 窗口翻转：跳过本次 burn，保留 EWMA（消耗速度跨窗口大体连续）
    if state.last_7d_reset_at != snap.seven_day_reset_at:
        return None
    delta_h = (snap.sampled_at - state.last_sampled_at).total_seconds() / 3600.0
    if delta_h < MIN_BURN_INTERVAL_H:
        return None
    burn = max(0.0, snap.seven_day_used - state.last_7d_used) / delta_h
    state.hourly_burn_ewma = (1 - EWMA_ALPHA) * state.hourly_burn_ewma + EWMA_ALPHA * burn
    return burn


def _update_baseline(state: AccountState, snap: AccountSnapshot) -> None:
    state.last_7d_used = snap.seven_day_used
    state.last_5h_used = snap.five_hour_used
    state.last_7d_reset_at = snap.seven_day_reset_at
    state.last_sampled_at = snap.sampled_at


def _metrics(
    cfg: Config,
    seven_day: float,
    remaining_h: float,
    recent_burn: float | None,
    burn_ewma: float | None,
) -> _Metrics:
    elapsed_h = _clamp(cfg.window_hours - remaining_h, 0.0, cfg.window_hours)
    target_window_h = max(cfg.window_hours - cfg.safe_tail_hours, 1.0)
    runway_h = max(remaining_h - cfg.safe_tail_hours, 1.0)
    target_now = cfg.target_7d_utilization * min(elapsed_h / target_window_h, 1.0)
    final_gap = cfg.target_7d_utilization - seven_day
    required_rate = max(0.0, final_gap) / runway_h
    recent_rate = _weighted_recent_rate(recent_burn, burn_ewma)
    projected_end = seven_day + recent_rate * runway_h
    return _Metrics(
        target_now=target_now,
        final_gap=final_gap,
        required_rate=required_rate,
        recent_rate=recent_rate,
        projected_end=projected_end,
        ahead=seven_day - target_now,
    )


def _attach_metrics(
    d: Decision,
    metrics: _Metrics,
    remaining_h: float,
    recent_burn: float | None,
    recent_5h_burn: float | None,
) -> None:
    d.recent_hour_burn = recent_burn
    d.recent_5h_burn = recent_5h_burn
    d.target_now = round(metrics.target_now, 4)
    d.projected_end = round(metrics.projected_end, 4)
    d.required_rate = round(metrics.required_rate, 4)
    d.recent_rate = round(metrics.recent_rate, 4)
    d.remaining_hours = round(remaining_h, 4)


def _weighted_recent_rate(rate_1h: float | None, rate_5h: float | None) -> float:
    if rate_1h is not None and rate_5h is not None:
        return 0.65 * rate_1h + 0.35 * rate_5h
    if rate_1h is not None:
        return rate_1h
    if rate_5h is not None:
        return rate_5h
    return 0.0


def _needs_cooldown(
    cfg: Config,
    seven_day: float,
    remaining_h: float,
    recent_burn: float | None,
    metrics: _Metrics,
    cap: float,
) -> bool:
    if recent_burn is None:
        return False
    too_fast = recent_burn >= cap and seven_day >= metrics.target_now - cfg.cooldown_near_target_band_pp
    return too_fast or _will_hit_goal_soon(cfg, seven_day, remaining_h, recent_burn)


def _will_hit_goal_soon(
    cfg: Config,
    seven_day: float,
    remaining_h: float,
    recent_burn: float | None,
) -> bool:
    return (
        recent_burn is not None
        and recent_burn > 0.0
        and seven_day + recent_burn * min(cfg.will_hit_goal_soon_hours, remaining_h) >= cfg.target_7d_utilization
    )


def _most_protective(current: int, floor: int, cfg: Config) -> int:
    bands = cfg.priority_bands
    if current not in bands:
        return floor
    return max(current, floor, key=bands.index)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
