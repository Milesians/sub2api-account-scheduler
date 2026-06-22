from datetime import UTC, datetime, timedelta

import scheduler.runner as runner
from scheduler.config import Config
from scheduler.models import AccountSnapshot, AccountState
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
        return {
            "five_hour": {"utilization": 11.0, "resets_at": "2026-06-12T12:00:00Z"},
            "seven_day": {"utilization": 31.0, "resets_at": "2026-06-15T22:00:00Z"},
        }

    def bulk_update_accounts(self, account_ids, fields):
        self.updated.append((account_ids, fields))
        return True


def test_tick_probes_paused_accounts_without_scheduling(tmp_path):
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
        samples = store.conn.execute("SELECT source, seven_day_used FROM usage_sample").fetchall()
    finally:
        store.close()

    assert 7 in states
    assert states[7].last_7d_used == 31.0
    assert states[7].current_priority == 1050
    assert states[7].current_load_factor == 1
    assert states[7].last_probe_attempt_at is not None
    assert decisions == 0
    assert [dict(row) for row in samples] == [{"source": "active", "seven_day_used": 31.0}]
    assert api.probed == [7]
    assert api.updated == []


def test_tick_does_not_schedule_from_passive_usage_when_probe_fails(tmp_path):
    class FailingProbeAPI(FakeAPI):
        def probe_usage(self, account_id):
            self.probed.append(account_id)
            return None

    db = tmp_path / "scheduler.db"
    store = Store(str(db))
    api = FailingProbeAPI()
    cfg = Config(
        base_url="http://admin",
        admin_key="secret",
        platform="openai",
        account_profile_refresh_enabled=False,
    )

    try:
        runner.tick(cfg, api, store)
        row = store.conn.execute("SELECT * FROM decision_log WHERE account_id = 7").fetchone()
        sample_count = store.conn.execute("SELECT COUNT(*) FROM usage_sample").fetchone()[0]
    finally:
        store.close()

    assert api.probed == [7]
    assert api.updated == []
    assert row["reason"] == "no_data_hold"
    assert row["seven_day_used"] is None
    assert row["usage_source"] == "missing"
    assert sample_count == 0


def test_tick_removes_accounts_missing_from_admin_list(tmp_path):
    db = tmp_path / "scheduler.db"
    store = Store(str(db))
    store.save_states([AccountState(account_id=99, last_priority=1050)], NOW)
    store.set_account_paused(99, True, NOW)
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
        controls = store.load_account_controls()
    finally:
        store.close()

    assert set(states) == {7}
    assert 99 not in controls


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


def test_terminal_probe_score_prioritizes_gap_and_protective_band():
    cfg = Config(base_url="http://admin", admin_key="secret", platform="openai")
    urgent = stale_snap(1, sampled_at=NOW - timedelta(minutes=25), seven_day=80.0, reset_hours=3, priority=1070)
    less_urgent = stale_snap(2, sampled_at=NOW - timedelta(minutes=25), seven_day=99.3, reset_hours=20)

    assert runner._probe_score(urgent, None, cfg, NOW) > runner._probe_score(less_urgent, None, cfg, NOW)


def test_tick_returns_terminal_active_when_eligible_account_is_in_drain_window(tmp_path):
    class TerminalAPI(FakeAPI):
        def list_accounts(self, platform):
            now = datetime.now(UTC)
            account = super().list_accounts(platform)[0]
            account["extra"] = {
                **account["extra"],
                "codex_7d_used_percent": 90.0,
                "codex_7d_reset_at": (now + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
                "codex_usage_updated_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            }
            return [account]

        def probe_usage(self, account_id):
            self.probed.append(account_id)
            now = datetime.now(UTC)
            return {
                "five_hour": {"utilization": 10.0, "resets_at": (now + timedelta(hours=2)).isoformat()},
                "seven_day": {"utilization": 90.0, "resets_at": (now + timedelta(hours=6)).isoformat()},
            }

    db = tmp_path / "scheduler.db"
    store = Store(str(db))
    api = TerminalAPI()
    cfg = Config(
        base_url="http://admin",
        admin_key="secret",
        platform="openai",
        account_profile_refresh_enabled=False,
    )

    try:
        terminal_active = runner.tick(cfg, api, store)
    finally:
        store.close()

    assert terminal_active is True


def test_sleep_minutes_switches_to_terminal_interval_only_when_terminal_active():
    cfg = Config(
        base_url="http://admin",
        admin_key="secret",
        platform="openai",
        interval_minutes=60,
        terminal_interval_minutes=15,
    )

    assert runner._sleep_minutes(cfg, terminal_active=True) == 15
    assert runner._sleep_minutes(cfg, terminal_active=False) == 60

    cfg.terminal_drain_enabled = False
    assert runner._sleep_minutes(cfg, terminal_active=True) == 60


def test_sleep_minutes_has_minimum_one_minute():
    cfg = Config(
        base_url="http://admin",
        admin_key="secret",
        platform="openai",
        interval_minutes=0,
        terminal_interval_minutes=0,
    )

    assert runner._sleep_minutes(cfg, terminal_active=False) == 1
    assert runner._sleep_minutes(cfg, terminal_active=True) == 1
