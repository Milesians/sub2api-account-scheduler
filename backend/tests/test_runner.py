from datetime import UTC, datetime, timedelta

import scheduler.runner as runner
from scheduler.config import Config
from scheduler.models import AccountSnapshot
from scheduler.store import Store

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)


class FakeAPI:
    def __init__(self):
        self.probed = []
        self.updated = []

    def list_accounts(self, platform):
        assert platform == "openai"
        return [{
            "id": 7,
            "name": "pay1",
            "type": "oauth",
            "priority": 1050,
            "concurrency": 1,
            "load_factor": 1,
            "status": "active",
            "schedulable": True,
            "extra": {
                "codex_7d_used_percent": 30.0,
                "codex_5h_used_percent": 10.0,
                "codex_7d_reset_at": "2026-06-15T22:00:00Z",
                "codex_5h_reset_at": "2026-06-12T12:00:00Z",
                "codex_usage_updated_at": "2026-06-12T09:59:00Z",
            },
        }]

    def probe_usage(self, account_id):
        self.probed.append(account_id)
        return None

    def bulk_update_accounts(self, account_ids, fields):
        self.updated.append((account_ids, fields))
        return True


def test_tick_skips_paused_accounts(tmp_path):
    db = tmp_path / "scheduler.db"
    store = Store(str(db))
    store.set_account_paused(7, True, NOW)
    api = FakeAPI()
    cfg = Config(
        base_url="http://admin",
        admin_key="secret",
        platform="openai",
        account_profile_refresh_enabled=False,
    )

    try:
        runner.tick(cfg, api, store)
        states = store.load_states()
        decisions = store.conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    finally:
        store.close()

    assert 7 in states
    assert states[7].last_7d_used == 30.0
    assert decisions == 0
    assert api.probed == []
    assert api.updated == []


def stale_snap(account_id, *, sampled_at, seven_day=80.0, reset_hours=84.0, priority=1050):
    return AccountSnapshot(
        id=account_id,
        name=f"pay{account_id}",
        type="oauth",
        priority=priority,
        concurrency=1,
        load_factor=None,
        status="active",
        schedulable=True,
        rate_limited=False,
        overloaded=False,
        temp_unschedulable=False,
        seven_day_used=seven_day,
        seven_day_reset_at=NOW + timedelta(hours=reset_hours),
        sampled_at=sampled_at,
        usage_source="passive" if sampled_at else "missing",
    )


def test_terminal_probe_uses_shorter_stale_threshold_and_larger_budget():
    cfg = Config(base_url="http://admin", admin_key="secret", platform="openai")
    terminal = [
        stale_snap(i, sampled_at=NOW - timedelta(minutes=25), seven_day=80.0 + i, reset_hours=6)
        for i in range(1, 31)
    ]
    normal = stale_snap(100, sampled_at=NOW - timedelta(minutes=25), seven_day=20.0, reset_hours=84)
    stale = [s for s in [*terminal, normal] if runner._snapshot_stale(s, cfg, NOW)]

    assert len(stale) == 30
    assert runner._probe_budget([*terminal, normal], stale, cfg, NOW) == 20


def test_terminal_probe_score_prioritizes_gap_and_protective_band():
    cfg = Config(base_url="http://admin", admin_key="secret", platform="openai")
    urgent = stale_snap(1, sampled_at=NOW - timedelta(minutes=25), seven_day=80.0, reset_hours=3, priority=1070)
    less_urgent = stale_snap(2, sampled_at=NOW - timedelta(minutes=25), seven_day=99.3, reset_hours=20)

    assert runner._probe_score(urgent, None, cfg, NOW) > runner._probe_score(less_urgent, None, cfg, NOW)
