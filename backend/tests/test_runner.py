from datetime import UTC, datetime

import scheduler.runner as runner
from scheduler.config import Config
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
