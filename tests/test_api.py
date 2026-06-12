"""parse_account / merge_probe 的平台差异单测：字段名与单位换算。"""

from datetime import UTC, datetime, timedelta

from scheduler.api import merge_probe, parse_account

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)


def base_raw(**extra):
    return {
        "id": 7,
        "name": "acc",
        "type": "oauth",
        "priority": 1050,
        "status": "active",
        "schedulable": True,
        "extra": extra,
    }


def test_parse_anthropic_ratio_fields():
    raw = base_raw(
        session_window_utilization=0.3,
        passive_usage_7d_utilization=0.42,
        passive_usage_7d_reset=int(NOW.timestamp()) + 84 * 3600,
        passive_usage_sampled_at="2026-06-12T09:59:00Z",
    )
    raw["session_window_end"] = "2026-06-12T12:00:00Z"
    snap = parse_account(raw, NOW, "anthropic")
    assert snap.five_hour_used == 30.0      # 0-1 -> 0-100
    assert snap.seven_day_used == 42.0
    assert snap.seven_day_reset_at == NOW + timedelta(hours=84)
    assert snap.five_hour_reset_at == datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    assert snap.usage_source == "passive"


def test_parse_openai_codex_percent_fields():
    raw = base_raw(
        codex_5h_used_percent=30.0,         # 已是 0-100
        codex_7d_used_percent=42.0,
        codex_7d_reset_at="2026-06-15T22:00:00Z",
        codex_5h_reset_at="2026-06-12T12:00:00Z",
        codex_usage_updated_at="2026-06-12T09:59:00Z",
    )
    snap = parse_account(raw, NOW, "openai")
    assert snap.five_hour_used == 30.0
    assert snap.seven_day_used == 42.0
    assert snap.seven_day_reset_at == datetime(2026, 6, 15, 22, 0, tzinfo=UTC)
    assert snap.five_hour_reset_at == datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    assert snap.sampled_at == datetime(2026, 6, 12, 9, 59, tzinfo=UTC)
    assert snap.usage_source == "passive"


def test_parse_openai_without_codex_fields_is_missing():
    snap = parse_account(base_raw(), NOW, "openai")
    assert snap.seven_day_used is None
    assert snap.usage_source == "missing"


def test_merge_probe_shared_structure():
    snap = parse_account(base_raw(), NOW, "openai")
    merge_probe(snap, {
        "five_hour": {"utilization": 55.0, "resets_at": "2026-06-12T13:00:00Z"},
        "seven_day": {"utilization": 66.0, "resets_at": "2026-06-16T10:00:00Z"},
    }, NOW)
    assert snap.five_hour_used == 55.0
    assert snap.seven_day_used == 66.0
    assert snap.seven_day_sonnet_used is None   # openai 无 sonnet 窗口
    assert snap.five_hour_reset_at == datetime(2026, 6, 12, 13, 0, tzinfo=UTC)
    assert snap.usage_source == "active"
