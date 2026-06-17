"""policy.decide 纯函数单测：硬保护、补量排名、防抖、冷却、数据缺失等场景。"""

from datetime import UTC, datetime, timedelta

from scheduler.config import Config
from scheduler.models import AccountSnapshot, AccountState
from scheduler.policy import decide

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=84)  # 7d 窗口过半


def mk_cfg(**kw) -> Config:
    cfg = Config(base_url="http://x", admin_key="k")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def mk_snap(account_id: int, seven_day: float | None, **kw) -> AccountSnapshot:
    defaults = dict(
        name=f"acc-{account_id}",
        type="oauth",
        priority=1050,
        concurrency=1,
        load_factor=None,
        status="active",
        schedulable=True,
        rate_limited=False,
        overloaded=False,
        temp_unschedulable=False,
        five_hour_used=30.0,
        seven_day_used=seven_day,
        seven_day_reset_at=RESET if seven_day is not None else None,
        sampled_at=NOW if seven_day is not None else None,
        usage_source="passive" if seven_day is not None else "missing",
    )
    defaults.update(kw)
    return AccountSnapshot(id=account_id, **defaults)


def mk_state(account_id: int, seven_day: float, hours_ago: float = 1.0, **kw) -> AccountState:
    defaults = dict(
        last_priority=1050,
        last_7d_used=seven_day,
        last_5h_used=30.0,
        last_7d_reset_at=RESET,
        last_sampled_at=NOW - timedelta(hours=hours_ago),
        hourly_burn_ewma=0.3,
    )
    defaults.update(kw)
    return AccountState(account_id=account_id, **defaults)


def decide_one(snap, state=None, cfg=None, peers=()):
    cfg = cfg or mk_cfg()
    snaps = [snap, *peers]
    states = {} if state is None else {state.account_id: state}
    for p in peers:
        states.setdefault(p.id, mk_state(p.id, p.seven_day_used or 50.0))
    decisions, new_states = decide(snaps, states, cfg, NOW)
    return {d.account_id: d for d in decisions}[snap.id], new_states


def neutral_peer(account_id: int) -> AccountSnapshot:
    # 明显超前的旁观账号，会被 ahead protect，不参与 boost 排名
    return mk_snap(account_id, 90.0)


def terminal_snap(account_id: int, seven_day: float | None, hours_left: float = 6.0, **kw) -> AccountSnapshot:
    return mk_snap(account_id, seven_day, seven_day_reset_at=NOW + timedelta(hours=hours_left), **kw)


def terminal_state(account_id: int, seven_day: float, hours_left: float = 6.0, **kw) -> AccountState:
    return mk_state(account_id, seven_day, last_7d_reset_at=NOW + timedelta(hours=hours_left), **kw)


def test_hard_cap_7d_jumps_to_floor():
    d, _ = decide_one(mk_snap(1, 99.9, priority=1010), mk_state(1, 99.0))
    assert d.target_priority == 1099
    assert d.reason == "hard_cap_7d"


def test_hard_cap_5h_ignored_by_default():
    d, _ = decide_one(mk_snap(1, 50.0, five_hour_used=99.5), mk_state(1, 49.5))
    assert d.reason != "hard_cap_5h"
    assert d.target_priority != 1099


def test_hard_cap_5h_jumps_to_floor_when_guard_enabled():
    d, _ = decide_one(
        mk_snap(1, 50.0, five_hour_used=99.5),
        mk_state(1, 49.5),
        cfg=mk_cfg(enable_5h_guard=True),
    )
    assert d.reason == "hard_cap_5h"
    assert d.target_priority == 1099


def test_sonnet_window_counts_for_protection():
    d, _ = decide_one(mk_snap(1, 50.0, seven_day_sonnet_used=99.9), mk_state(1, 49.0))
    assert d.target_priority == 1099


def test_protect_7d_at_target():
    d, _ = decide_one(mk_snap(1, 97.5, priority=1010, load_factor=3), mk_state(1, 97.0), cfg=mk_cfg())
    assert d.target_priority == 1070
    assert d.target_load_factor == 1
    assert d.reason == "protect_7d"


def test_cooldown_holds_protective_band():
    state = mk_state(1, 50.0, cooldown_until=NOW + timedelta(minutes=30))
    d, _ = decide_one(mk_snap(1, 50.0, priority=1030), state)
    assert d.target_priority == 1070
    assert d.reason == "cooldown_hold"


def test_new_cooldown_when_burning_too_fast_near_curve():
    d, states = decide_one(mk_snap(1, 50.0), mk_state(1, 48.5))
    assert d.target_priority == 1070
    assert d.reason == "new_cooldown"
    assert states[1].cooldown_until == NOW + timedelta(minutes=60)


def test_fast_burn_does_not_cooldown_when_clearly_behind():
    d, _ = decide_one(mk_snap(1, 20.0), mk_state(1, 18.0), peers=[neutral_peer(2)])
    assert d.reason != "new_cooldown"
    assert d.target_priority == 1050
    assert d.target_load_factor == 3


def test_will_hit_goal_soon_sets_cooldown():
    d, states = decide_one(mk_snap(1, 96.0), mk_state(1, 95.5))
    assert d.target_priority == 1070
    assert d.reason == "new_cooldown_will_hit_goal"
    assert states[1].cooldown_until == NOW + timedelta(minutes=60)


def test_no_data_holds_current_priority():
    d, _ = decide_one(mk_snap(1, None, priority=1030))
    assert d.target_priority == 1030
    assert d.reason == "no_data_hold"


def test_stale_data_holds_current_priority():
    snap = mk_snap(1, 50.0, sampled_at=NOW - timedelta(hours=2))
    d, states = decide_one(snap, mk_state(1, 49.0))
    assert d.target_priority == 1050
    assert d.reason == "stale_hold"
    # 基线不能被旧数据污染
    assert states[1].last_7d_used == 49.0


def test_first_seen_takes_over_to_normal():
    d, _ = decide_one(mk_snap(1, 40.0, priority=50))
    assert d.target_priority == 1050
    assert d.reason == "takeover"


def test_first_seen_in_band_can_be_scheduled_with_valid_data():
    d, _ = decide_one(mk_snap(1, 40.0, priority=1030))
    assert d.target_priority == 1050
    assert d.reason == "boost"
    assert d.target_load_factor == 3


def test_invalid_reset_holds_current_priority():
    snap = mk_snap(1, 50.0, seven_day_reset_at=NOW - timedelta(minutes=1), priority=1030)
    d, states = decide_one(snap, mk_state(1, 49.0))
    assert d.target_priority == 1030
    assert d.reason == "invalid_reset_hold"
    assert states[1].last_7d_used == 49.0


def test_behind_account_boosts_with_load_factor_not_priority():
    # 严重落后（进度应 48.5，实际 10），补量只提高 load_factor，
    # priority 仍回到 normal，避免成为唯一低 priority 入口。
    behind = mk_snap(1, 10.0)
    d, _ = decide_one(behind, mk_state(1, 9.5), peers=[neutral_peer(2)])
    assert d.reason == "boost"
    assert d.target_priority == 1050
    assert d.target_load_factor == 3
    assert d.target_now is not None
    assert d.projected_end is not None
    assert d.required_rate is not None
    assert d.recent_rate is not None
    assert d.remaining_hours == 84.0


def test_two_behind_accounts_enter_boost():
    cfg = mk_cfg(max_boost_min=2)
    a, b = mk_snap(1, 10.0, priority=1030), mk_snap(2, 12.0, priority=1030)
    states = {1: mk_state(1, 9.5, last_priority=1030), 2: mk_state(2, 11.5, last_priority=1030)}
    decisions, new_states = decide([a, b, neutral_peer(3)], states, cfg, NOW)
    by_id = {d.account_id: d for d in decisions}
    assert by_id[1].target_priority == 1050
    assert by_id[2].target_priority == 1050
    assert by_id[1].reason == "boost"
    assert by_id[1].target_load_factor == 3
    assert by_id[2].target_load_factor == 3
    assert new_states[1].last_boost_at == NOW
    assert new_states[2].last_boost_at == NOW


def test_recent_5h_burn_reduces_catchup_score():
    slow = mk_snap(1, 50.0, recent_5h_burn=0.0)
    fast = mk_snap(1, 50.0, recent_5h_burn=1.0)
    slow_d, _ = decide_one(slow, mk_state(1, 50.0, hourly_burn_ewma=0.0), peers=[neutral_peer(2)])
    fast_d, _ = decide_one(fast, mk_state(1, 50.0, hourly_burn_ewma=0.0), peers=[neutral_peer(2)])
    assert fast_d.catchup_score < slow_d.catchup_score
    assert fast_d.recent_5h_burn == 1.0


def test_low_required_rate_excluded_from_strong():
    # 刚重置、跑道充裕的号：catchup 很高但 required_rate < 门槛 → 只给温和，不占强力名额。
    # warmup_hours 调小以隔离门槛逻辑（不让预热折减干扰 catchup）。
    fresh = mk_snap(1, 5.0, recent_5h_burn=0.0, seven_day_reset_at=NOW + timedelta(hours=160))
    state = mk_state(1, 5.0, hourly_burn_ewma=0.0)
    state.last_7d_reset_at = NOW + timedelta(hours=160)
    cfg = mk_cfg(warmup_hours=0.1, strong_min_required_rate=0.6)
    d, _ = decide_one(fresh, state, cfg=cfg, peers=[neutral_peer(2)])
    assert d.catchup_score >= cfg.strong_score_threshold  # 分数本身够强
    assert d.required_rate < cfg.strong_min_required_rate
    assert d.reason == "mild_boost"
    assert d.target_load_factor == 2


def test_warmup_discounts_fresh_reset_catchup():
    # 同一个刚重置的号，预热折减会显著压低其 catchup_score。
    fresh = mk_snap(1, 5.0, recent_5h_burn=0.0, seven_day_reset_at=NOW + timedelta(hours=160))

    def score(warmup_hours):
        state = mk_state(1, 5.0, hourly_burn_ewma=0.0)
        state.last_7d_reset_at = NOW + timedelta(hours=160)
        d, _ = decide_one(fresh, state, cfg=mk_cfg(warmup_hours=warmup_hours), peers=[neutral_peer(2)])
        return d.catchup_score

    assert score(48.0) < score(0.1)


def test_fresh_reset_does_not_steal_strong_from_behind():
    # pay2/pay4 复盘：刚重置的健康号（跑道充裕）不应抢走真正告急的号的唯一强力名额。
    behind = mk_snap(1, 35.0, recent_5h_burn=1.3, seven_day_reset_at=NOW + timedelta(hours=40))
    fresh = mk_snap(2, 5.0, recent_5h_burn=0.0, seven_day_reset_at=NOW + timedelta(hours=160))
    states = {
        1: mk_state(1, 34.0, last_7d_reset_at=NOW + timedelta(hours=40)),
        2: mk_state(2, 5.0, hourly_burn_ewma=0.0, last_7d_reset_at=NOW + timedelta(hours=160)),
    }
    decisions, _ = decide([behind, fresh], states, mk_cfg(), NOW)
    by_id = {d.account_id: d for d in decisions}
    assert by_id[1].reason == "boost"
    assert by_id[1].target_load_factor == 3
    assert by_id[2].reason != "boost"


def test_emergency_window_allows_band_jump():
    # 禁用 terminal 后保留旧 emergency 行为：priority 回 normal，补量通过 load_factor 加权。
    snap = mk_snap(1, 50.0, priority=1070, seven_day_reset_at=NOW + timedelta(hours=6))
    peer = mk_snap(2, 50.0, seven_day_reset_at=NOW + timedelta(hours=6))
    cfg = mk_cfg(max_boost_min=2, terminal_drain_enabled=False)
    states = {1: mk_state(1, 49.5, last_priority=1070), 2: mk_state(2, 49.5)}
    states[1].last_7d_reset_at = NOW + timedelta(hours=6)
    states[2].last_7d_reset_at = NOW + timedelta(hours=6)
    decisions, _ = decide([snap, peer], states, cfg, NOW)
    assert {d.target_priority for d in decisions} == {1050}
    assert {d.reason for d in decisions} == {"boost"}
    assert {d.target_load_factor for d in decisions} == {3}


def test_terminal_drain_strong_uses_boost_band_and_load_factor():
    snap = terminal_snap(1, 94.0, priority=1070, recent_5h_burn=0.1)
    d, states = decide_one(snap, terminal_state(1, 93.8, last_priority=1070, hourly_burn_ewma=0.1))
    assert d.mode == "terminal"
    assert d.reason == "terminal_drain_strong_jump"
    assert d.target_priority == 1010
    assert d.target_load_factor == 4
    assert d.drain_level == "strong"
    assert d.drain_gap == 5.4
    assert states[1].last_terminal_level == "strong"
    assert states[1].last_terminal_boost_at == NOW


def test_terminal_drain_mild_uses_mild_band():
    snap = terminal_snap(1, 99.0, hours_left=12.0, recent_5h_burn=1.0)
    d, _ = decide_one(snap, terminal_state(1, 98.8, hours_left=12.0, hourly_burn_ewma=1.0))
    assert d.reason == "terminal_drain_mild"
    assert d.target_priority == 1030
    assert d.target_load_factor == 2
    assert d.drain_level == "mild"


def test_terminal_done_protects_at_drain_target():
    snap = terminal_snap(1, 99.35, priority=1010, load_factor=4)
    d, _ = decide_one(snap, terminal_state(1, 99.2))
    assert d.mode == "terminal"
    assert d.reason == "terminal_done"
    assert d.target_priority == 1070
    assert d.target_load_factor == 1


def test_terminal_skips_pacing_target_and_cooldown():
    state = terminal_state(1, 96.5, cooldown_until=NOW + timedelta(minutes=30))
    snap = terminal_snap(1, 97.5, priority=1070, recent_5h_burn=0.0)
    d, _ = decide_one(snap, state)
    assert d.mode == "terminal"
    assert d.reason.startswith("terminal_drain")
    assert d.target_priority == 1010
    assert d.reason != "protect_7d"
    assert d.reason != "cooldown_hold"


def test_terminal_stale_returns_base_load_factor():
    snap = terminal_snap(1, 90.0, priority=1010, load_factor=4, sampled_at=NOW - timedelta(minutes=30))
    d, states = decide_one(snap, terminal_state(1, 89.0))
    assert d.reason == "terminal_stale_base"
    assert d.target_priority == 1010
    assert d.target_load_factor == 1
    assert states[1].last_7d_used == 89.0


def test_behind_account_can_boost_when_5h_high():
    snap = mk_snap(1, 10.0, five_hour_used=93.0)
    d, _ = decide_one(snap, mk_state(1, 9.5), peers=[neutral_peer(2)])
    assert d.reason == "boost"
    assert d.target_priority == 1050
    assert d.target_load_factor == 3


def test_ahead_account_steps_toward_protect():
    # 超前（进度应 48.5，实际 80）且 EWMA 速度足以达标
    snap = mk_snap(1, 80.0)
    d, _ = decide_one(snap, mk_state(1, 79.8, hourly_burn_ewma=0.5))
    assert d.reason == "ahead_protect"
    assert d.target_priority == 1070


def test_window_rollover_skips_burn():
    # reset 已翻转：7d 从 95 掉回 2，不应触发 hot_hour，EWMA 保留
    state = mk_state(1, 95.0, hourly_burn_ewma=0.4)
    state.last_7d_reset_at = NOW - timedelta(hours=1)
    snap = mk_snap(1, 2.0, seven_day_reset_at=NOW + timedelta(hours=167))
    d, states = decide_one(snap, state, peers=[neutral_peer(2)])
    assert d.reason != "hot_hour_cooldown"
    assert states[1].hourly_burn_ewma == 0.4
    assert states[1].last_7d_reset_at == NOW + timedelta(hours=167)


def test_ewma_updates_with_positive_burn():
    _, states = decide_one(mk_snap(1, 51.0), mk_state(1, 50.0, hourly_burn_ewma=0.0))
    assert abs(states[1].hourly_burn_ewma - 0.3) < 1e-9  # 0.7*0 + 0.3*1.0


def test_negative_delta_clamped_to_zero():
    _, states = decide_one(mk_snap(1, 49.0), mk_state(1, 50.0, hourly_burn_ewma=1.0))
    assert abs(states[1].hourly_burn_ewma - 0.7) < 1e-9  # burn=0


def test_unmanaged_band_priority_steps_from_normal():
    # 有历史但 priority 被外部改成 200：防抖基准视为 normal 档
    snap = mk_snap(1, 10.0, priority=200)
    d, _ = decide_one(snap, mk_state(1, 9.5, last_priority=200), peers=[neutral_peer(2)])
    assert d.target_priority == 1050
    assert d.target_load_factor == 3
