from datetime import UTC, datetime, timedelta

from scheduler.models import AccountProfile, AccountSnapshot, AccountState, Decision
from scheduler.store import SCHEMA_VERSION, Store

NOW = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=84)


def test_store_migrates_and_persists_new_state_fields(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        version = store.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == SCHEMA_VERSION

        state = AccountState(
            account_id=1,
            last_priority=1030,
            current_priority=1050,
            current_load_factor=3,
            last_boost_at=NOW,
            last_terminal_boost_at=NOW,
            last_terminal_level="strong",
            last_probe_attempt_at=NOW,
        )
        store.save_states([state], NOW)

        loaded = store.load_states()[1]
        assert loaded.current_priority == 1050
        assert loaded.current_load_factor == 3
        assert loaded.last_boost_at == NOW
        assert loaded.last_terminal_boost_at == NOW
        assert loaded.last_terminal_level == "strong"
        assert loaded.last_probe_attempt_at == NOW
    finally:
        store.close()


def test_store_persists_account_control(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        control = store.set_account_paused(7, True, NOW)
        assert control.paused is True

        loaded = store.load_account_controls([7])[7]
        assert loaded.paused is True
        assert loaded.updated_at == NOW

        store.set_account_paused(7, False, NOW + timedelta(minutes=1))
        assert store.load_account_controls([7])[7].paused is False
    finally:
        store.close()


def test_attach_recent_5h_burn_from_usage_samples(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        old = AccountSnapshot(
            id=1,
            name="pay1",
            priority=1050,
            status="active",
            schedulable=True,
            rate_limited=False,
            overloaded=False,
            temp_unschedulable=False,
            seven_day_used=40.0,
            seven_day_reset_at=RESET,
            sampled_at=NOW - timedelta(hours=5),
            usage_source="passive",
        )
        current = AccountSnapshot(
            id=1,
            name="pay1",
            priority=1050,
            status="active",
            schedulable=True,
            rate_limited=False,
            overloaded=False,
            temp_unschedulable=False,
            seven_day_used=45.0,
            seven_day_reset_at=RESET,
            sampled_at=NOW,
            usage_source="passive",
        )

        store.add_samples([old])
        store.attach_recent_5h_burn([current])

        assert current.recent_5h_burn == 1.0
    finally:
        store.close()


def test_decision_log_keeps_scheduler_metrics(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        decision = Decision(
            account_id=1,
            name="pay1",
            current_priority=1050,
            target_priority=1050,
            current_load_factor=1,
            target_load_factor=2,
            reason="mild_boost",
            catchup_score=2.5,
            seven_day_reset_at=RESET,
            recent_hour_burn=0.4,
            recent_5h_burn=0.2,
            target_now=48.5,
            projected_end=91.0,
            required_rate=0.7,
            recent_rate=0.3,
            remaining_hours=12.0,
            mode="terminal",
            drain_gap=3.1,
            drain_required_rate=0.7,
            drain_pressure=2.3,
            drain_level="strong",
            deadline_hours=11.75,
        )
        store.add_decisions("run-1", [decision], NOW)

        row = store.conn.execute("SELECT * FROM decision_log WHERE account_id = 1").fetchone()
        assert row["recent_5h_burn"] == 0.2
        assert row["seven_day_reset_at"] == "2026-06-15T22:00:00Z"
        assert row["current_load_factor"] == 1
        assert row["target_load_factor"] == 2
        assert row["target_now"] == 48.5
        assert row["projected_end"] == 91.0
        assert row["required_rate"] == 0.7
        assert row["recent_rate"] == 0.3
        assert row["remaining_hours"] == 12.0
        assert row["mode"] == "terminal"
        assert row["drain_gap"] == 3.1
        assert row["drain_required_rate"] == 0.7
        assert row["drain_pressure"] == 2.3
        assert row["drain_level"] == "strong"
        assert row["deadline_hours"] == 11.75
    finally:
        store.close()


def test_delete_absent_accounts_removes_state_control_profile_and_samples(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        store.save_states([
            AccountState(account_id=1, last_priority=1050),
            AccountState(account_id=2, last_priority=1050),
        ], NOW)
        store.set_account_paused(2, True, NOW)
        store.save_account_profiles([
            AccountProfile(account_id=2, email="deleted@example.com"),
        ], NOW)
        sample = AccountSnapshot(
            id=2,
            name="deleted",
            priority=1050,
            status="active",
            schedulable=True,
            rate_limited=False,
            overloaded=False,
            temp_unschedulable=False,
            seven_day_used=40.0,
            seven_day_reset_at=RESET,
            sampled_at=NOW,
            usage_source="active",
        )
        store.add_samples([sample])

        deleted = store.delete_absent_accounts([1])

        assert deleted == 4
        assert set(store.load_states()) == {1}
        assert store.load_account_controls() == {}
        assert store.load_account_profiles([2]) == {}
        assert store.conn.execute("SELECT COUNT(*) FROM usage_sample WHERE account_id = 2").fetchone()[0] == 0
    finally:
        store.close()


def test_prune_uses_iso_cutoffs(tmp_path):
    store = Store(str(tmp_path / "scheduler.db"))
    try:
        old = AccountSnapshot(
            id=1,
            name="pay1",
            priority=1050,
            status="active",
            schedulable=True,
            rate_limited=False,
            overloaded=False,
            temp_unschedulable=False,
            seven_day_used=40.0,
            seven_day_reset_at=RESET,
            sampled_at=NOW - timedelta(days=20),
            usage_source="passive",
        )
        store.add_samples([old])
        store.prune(sample_days=14, decision_days=30, state_days=30)

        count = store.conn.execute("SELECT COUNT(*) FROM usage_sample").fetchone()[0]
        assert count == 0
    finally:
        store.close()
