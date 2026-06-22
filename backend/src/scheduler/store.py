"""SQLite 持久化：账号状态、采样历史、决策日志，以及数据老化。

时间一律存 UTC ISO8601 文本（2026-06-12T10:00:00Z），便于 datetime() 直接比较。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import AccountControl, AccountProfile, AccountSnapshot, AccountState, Decision

SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_state (
    account_id        INTEGER PRIMARY KEY,
    last_priority     INTEGER,
    current_priority  INTEGER,
    current_load_factor INTEGER,
    last_7d_used      REAL,
    last_5h_used      REAL,
    last_7d_reset_at  TEXT,
    last_sampled_at   TEXT,
    hourly_burn_ewma  REAL NOT NULL DEFAULT 0,
    cooldown_until    TEXT,
    last_boost_at     TEXT,
    last_terminal_boost_at TEXT,
    last_terminal_level    TEXT,
    last_probe_attempt_at  TEXT,
    probe_failures    INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_profile_cache (
    account_id               INTEGER PRIMARY KEY,
    email                    TEXT,
    subscription_plan        TEXT,
    subscription_status      TEXT,
    subscription_expires_at  TEXT,
    subscription_error       TEXT,
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_control (
    account_id  INTEGER PRIMARY KEY,
    paused      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_sample (
    account_id            INTEGER NOT NULL,
    sampled_at            TEXT    NOT NULL,
    seven_day_used        REAL,
    seven_day_sonnet_used REAL,
    five_hour_used        REAL,
    recent_hour_burn      REAL,
    recent_5h_burn        REAL,
    seven_day_reset_at    TEXT,
    source                TEXT NOT NULL,
    PRIMARY KEY (account_id, sampled_at)
);
CREATE INDEX IF NOT EXISTS idx_usage_sample_account_time
    ON usage_sample(account_id, sampled_at);

CREATE TABLE IF NOT EXISTS decision_log (
    id               INTEGER PRIMARY KEY,
    run_id           TEXT    NOT NULL,
    account_id       INTEGER NOT NULL,
    account_name     TEXT,
    decided_at       TEXT    NOT NULL,
    current_priority INTEGER,
    target_priority  INTEGER,
    current_load_factor INTEGER,
    target_load_factor  INTEGER,
    catchup_score    REAL,
    reason           TEXT,
    seven_day_used        REAL,
    seven_day_sonnet_used REAL,
    seven_day_reset_at    TEXT,
    five_hour_used        REAL,
    recent_hour_burn      REAL,
    recent_5h_burn        REAL,
    safe_hour_cap         REAL,
    target_now            REAL,
    projected_end         REAL,
    required_rate         REAL,
    recent_rate           REAL,
    remaining_hours       REAL,
    mode                  TEXT,
    drain_gap             REAL,
    drain_required_rate   REAL,
    drain_pressure        REAL,
    drain_level           TEXT,
    deadline_hours        REAL,
    usage_source          TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_account
    ON decision_log(account_id, decided_at);
"""

DECISION_LOG_COLUMNS = {
    "account_name": "TEXT",
    "current_load_factor": "INTEGER",
    "target_load_factor": "INTEGER",
    "seven_day_used": "REAL",
    "seven_day_sonnet_used": "REAL",
    "seven_day_reset_at": "TEXT",
    "five_hour_used": "REAL",
    "recent_hour_burn": "REAL",
    "recent_5h_burn": "REAL",
    "safe_hour_cap": "REAL",
    "target_now": "REAL",
    "projected_end": "REAL",
    "required_rate": "REAL",
    "recent_rate": "REAL",
    "remaining_hours": "REAL",
    "mode": "TEXT",
    "drain_gap": "REAL",
    "drain_required_rate": "REAL",
    "drain_pressure": "REAL",
    "drain_level": "TEXT",
    "deadline_hours": "REAL",
    "usage_source": "TEXT",
}

ACCOUNT_STATE_COLUMNS = {
    "current_priority": "INTEGER",
    "current_load_factor": "INTEGER",
    "last_boost_at": "TEXT",
    "last_terminal_boost_at": "TEXT",
    "last_terminal_level": "TEXT",
    "last_probe_attempt_at": "TEXT",
}

USAGE_SAMPLE_COLUMNS = {
    "recent_hour_burn": "REAL",
    "recent_5h_burn": "REAL",
}

MIGRATIONS = {
    1: """
       CREATE INDEX IF NOT EXISTS idx_usage_sample_account_time
           ON usage_sample(account_id, sampled_at);
       """,
    2: """
       ALTER TABLE account_state ADD COLUMN last_boost_at TEXT;
       ALTER TABLE usage_sample ADD COLUMN recent_hour_burn REAL;
       ALTER TABLE usage_sample ADD COLUMN recent_5h_burn REAL;
       ALTER TABLE decision_log ADD COLUMN recent_5h_burn REAL;
       ALTER TABLE decision_log ADD COLUMN target_now REAL;
       ALTER TABLE decision_log ADD COLUMN projected_end REAL;
       ALTER TABLE decision_log ADD COLUMN required_rate REAL;
       ALTER TABLE decision_log ADD COLUMN recent_rate REAL;
       ALTER TABLE decision_log ADD COLUMN remaining_hours REAL;
       """,
    3: """
       ALTER TABLE decision_log ADD COLUMN current_load_factor INTEGER;
       ALTER TABLE decision_log ADD COLUMN target_load_factor INTEGER;
       """,
    4: """
       ALTER TABLE decision_log ADD COLUMN seven_day_reset_at TEXT;
       """,
    5: """
       CREATE TABLE IF NOT EXISTS account_profile_cache (
           account_id               INTEGER PRIMARY KEY,
           email                    TEXT,
           subscription_plan        TEXT,
           subscription_status      TEXT,
           subscription_expires_at  TEXT,
           subscription_error       TEXT,
           updated_at               TEXT NOT NULL
       );
       """,
    6: """
       CREATE TABLE IF NOT EXISTS account_control (
           account_id  INTEGER PRIMARY KEY,
           paused      INTEGER NOT NULL DEFAULT 0,
           updated_at  TEXT NOT NULL
       );
       """,
    7: """
       ALTER TABLE account_state ADD COLUMN last_terminal_boost_at TEXT;
       ALTER TABLE account_state ADD COLUMN last_terminal_level TEXT;
       ALTER TABLE account_state ADD COLUMN last_probe_attempt_at TEXT;
       ALTER TABLE decision_log ADD COLUMN mode TEXT;
       ALTER TABLE decision_log ADD COLUMN drain_gap REAL;
       ALTER TABLE decision_log ADD COLUMN drain_required_rate REAL;
       ALTER TABLE decision_log ADD COLUMN drain_pressure REAL;
       ALTER TABLE decision_log ADD COLUMN drain_level TEXT;
       ALTER TABLE decision_log ADD COLUMN deadline_hours REAL;
       """,
    8: """
       ALTER TABLE account_state ADD COLUMN current_priority INTEGER;
       ALTER TABLE account_state ADD COLUMN current_load_factor INTEGER;
       """,
}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        current = self._schema_version()
        for version in range(current + 1, SCHEMA_VERSION + 1):
            self._apply_migration(version)
        self._ensure_columns("account_state", ACCOUNT_STATE_COLUMNS)
        self._ensure_columns("usage_sample", USAGE_SAMPLE_COLUMNS)
        self._ensure_columns("decision_log", DECISION_LOG_COLUMNS)
        self.conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (SCHEMA_VERSION, _iso(datetime.now(UTC))),
        )

    def _schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        version = row["version"] if row is not None else None
        return int(version or 0)

    def _apply_migration(self, version: int) -> None:
        script = MIGRATIONS.get(version)
        if script:
            self._exec_migration_script(script)
        self.conn.execute(
            """INSERT OR REPLACE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (version, _iso(datetime.now(UTC))),
        )

    def _exec_migration_script(self, script: str) -> None:
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def load_states(self) -> dict[int, AccountState]:
        rows = self.conn.execute("SELECT * FROM account_state").fetchall()
        states = {}
        for r in rows:
            states[r["account_id"]] = AccountState(
                account_id=r["account_id"],
                last_priority=r["last_priority"],
                current_priority=r["current_priority"],
                current_load_factor=r["current_load_factor"],
                last_7d_used=r["last_7d_used"],
                last_5h_used=r["last_5h_used"],
                last_7d_reset_at=_from_iso(r["last_7d_reset_at"]),
                last_sampled_at=_from_iso(r["last_sampled_at"]),
                hourly_burn_ewma=r["hourly_burn_ewma"],
                cooldown_until=_from_iso(r["cooldown_until"]),
                last_boost_at=_from_iso(r["last_boost_at"]),
                last_terminal_boost_at=_from_iso(r["last_terminal_boost_at"]),
                last_terminal_level=r["last_terminal_level"],
                last_probe_attempt_at=_from_iso(r["last_probe_attempt_at"]),
                probe_failures=r["probe_failures"],
            )
        return states

    def save_states(self, states: list[AccountState], now: datetime) -> None:
        self.conn.executemany(
            """INSERT INTO account_state
               (account_id, last_priority, current_priority, current_load_factor,
                last_7d_used, last_5h_used, last_7d_reset_at,
                last_sampled_at, hourly_burn_ewma, cooldown_until, last_boost_at,
                last_terminal_boost_at, last_terminal_level, last_probe_attempt_at,
                probe_failures, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 last_priority=excluded.last_priority,
                 current_priority=excluded.current_priority,
                 current_load_factor=excluded.current_load_factor,
                 last_7d_used=excluded.last_7d_used,
                 last_5h_used=excluded.last_5h_used,
                 last_7d_reset_at=excluded.last_7d_reset_at,
                 last_sampled_at=excluded.last_sampled_at,
                 hourly_burn_ewma=excluded.hourly_burn_ewma,
                 cooldown_until=excluded.cooldown_until,
                 last_boost_at=excluded.last_boost_at,
                 last_terminal_boost_at=excluded.last_terminal_boost_at,
                 last_terminal_level=excluded.last_terminal_level,
                 last_probe_attempt_at=excluded.last_probe_attempt_at,
                 probe_failures=excluded.probe_failures,
                 updated_at=excluded.updated_at""",
            [
                (
                    s.account_id,
                    s.last_priority,
                    s.current_priority,
                    s.current_load_factor,
                    s.last_7d_used,
                    s.last_5h_used,
                    _iso(s.last_7d_reset_at),
                    _iso(s.last_sampled_at),
                    s.hourly_burn_ewma,
                    _iso(s.cooldown_until),
                    _iso(s.last_boost_at),
                    _iso(s.last_terminal_boost_at),
                    s.last_terminal_level,
                    _iso(s.last_probe_attempt_at),
                    s.probe_failures,
                    _iso(now),
                )
                for s in states
            ],
        )
        self.conn.commit()

    def load_account_profiles(self, account_ids: list[int]) -> dict[int, AccountProfile]:
        if not account_ids:
            return {}
        placeholders = ",".join("?" for _ in account_ids)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM account_profile_cache
            WHERE account_id IN ({placeholders})
            """,
            tuple(account_ids),
        ).fetchall()
        return {
            int(r["account_id"]): AccountProfile(
                account_id=int(r["account_id"]),
                email=r["email"] or "",
                subscription_plan=r["subscription_plan"] or "",
                subscription_status=r["subscription_status"] or "",
                subscription_expires_at=_from_iso(r["subscription_expires_at"]),
                profile_updated_at=_from_iso(r["updated_at"]),
                subscription_error=r["subscription_error"] or "",
            )
            for r in rows
        }

    def save_account_profiles(self, profiles: list[AccountProfile], now: datetime) -> None:
        if not profiles:
            return
        self.conn.executemany(
            """INSERT INTO account_profile_cache
               (account_id, email, subscription_plan, subscription_status,
                subscription_expires_at, subscription_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 email=excluded.email,
                 subscription_plan=excluded.subscription_plan,
                 subscription_status=excluded.subscription_status,
                 subscription_expires_at=excluded.subscription_expires_at,
                 subscription_error=excluded.subscription_error,
                 updated_at=excluded.updated_at""",
            [
                (
                    p.account_id,
                    p.email,
                    p.subscription_plan,
                    p.subscription_status,
                    _iso(p.subscription_expires_at),
                    p.subscription_error,
                    _iso(now),
                )
                for p in profiles
            ],
        )
        self.conn.commit()

    def load_account_controls(self, account_ids: list[int] | None = None) -> dict[int, AccountControl]:
        if account_ids is None:
            rows = self.conn.execute("SELECT * FROM account_control").fetchall()
        elif not account_ids:
            return {}
        else:
            placeholders = ",".join("?" for _ in account_ids)
            rows = self.conn.execute(
                f"SELECT * FROM account_control WHERE account_id IN ({placeholders})",
                tuple(account_ids),
            ).fetchall()
        return {
            int(r["account_id"]): AccountControl(
                account_id=int(r["account_id"]),
                paused=bool(r["paused"]),
                updated_at=_from_iso(r["updated_at"]),
            )
            for r in rows
        }

    def set_account_paused(self, account_id: int, paused: bool, now: datetime) -> AccountControl:
        self.conn.execute(
            """INSERT INTO account_control (account_id, paused, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 paused=excluded.paused,
                 updated_at=excluded.updated_at""",
            (account_id, 1 if paused else 0, _iso(now)),
        )
        self.conn.commit()
        return AccountControl(account_id=account_id, paused=paused, updated_at=now)

    def add_samples(self, snaps: list[AccountSnapshot], decisions: list[Decision] | None = None) -> None:
        """记录有用量数据的快照；(account_id, sampled_at) 主键天然去重 stale 重复。"""
        by_account = {d.account_id: d for d in decisions or []}
        self.conn.executemany(
            """INSERT OR IGNORE INTO usage_sample
               (account_id, sampled_at, seven_day_used, seven_day_sonnet_used,
                five_hour_used, recent_hour_burn, recent_5h_burn, seven_day_reset_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    s.id,
                    _iso(s.sampled_at),
                    s.seven_day_used,
                    s.seven_day_sonnet_used,
                    s.five_hour_used,
                    by_account.get(s.id).recent_hour_burn if s.id in by_account else None,
                    s.recent_5h_burn,
                    _iso(s.seven_day_reset_at),
                    s.usage_source,
                )
                for s in snaps
                if s.sampled_at is not None and s.seven_day_used is not None
            ],
        )
        self.conn.commit()

    def attach_recent_5h_burn(self, snaps: list[AccountSnapshot]) -> None:
        """用 usage_sample 计算近 5 小时 7d 用量增量，写回快照供本轮策略使用。"""
        for snap in snaps:
            if snap.sampled_at is None or snap.seven_day_used is None:
                continue
            since = snap.sampled_at - timedelta(hours=5)
            row = self.conn.execute(
                """
                SELECT sampled_at, seven_day_used, seven_day_reset_at
                FROM usage_sample
                WHERE account_id = ?
                  AND sampled_at <= ?
                  AND sampled_at >= ?
                  AND seven_day_used IS NOT NULL
                ORDER BY sampled_at ASC
                LIMIT 1
                """,
                (snap.id, _iso(snap.sampled_at), _iso(since)),
            ).fetchone()
            if row is None:
                continue
            sampled_at = _from_iso(row["sampled_at"])
            if sampled_at is None or row["seven_day_used"] is None:
                continue
            if _from_iso(row["seven_day_reset_at"]) != snap.seven_day_reset_at:
                continue
            delta_h = (snap.sampled_at - sampled_at).total_seconds() / 3600.0
            if delta_h < 1.0:
                continue
            snap.recent_5h_burn = max(0.0, snap.seven_day_used - float(row["seven_day_used"])) / delta_h

    def add_decisions(self, run_id: str, decisions: list[Decision], now: datetime) -> None:
        self.conn.executemany(
            """INSERT INTO decision_log
               (run_id, account_id, account_name, decided_at, current_priority, target_priority,
                current_load_factor, target_load_factor, catchup_score, reason,
                seven_day_used, seven_day_sonnet_used, seven_day_reset_at, five_hour_used,
                recent_hour_burn, recent_5h_burn, safe_hour_cap, target_now, projected_end,
                required_rate, recent_rate, remaining_hours, mode, drain_gap, drain_required_rate,
                drain_pressure, drain_level, deadline_hours, usage_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    d.account_id,
                    d.name,
                    _iso(now),
                    d.current_priority,
                    d.target_priority,
                    d.current_load_factor,
                    d.target_load_factor,
                    d.catchup_score,
                    d.reason,
                    d.seven_day_used,
                    d.seven_day_sonnet_used,
                    _iso(d.seven_day_reset_at),
                    d.five_hour_used,
                    d.recent_hour_burn,
                    d.recent_5h_burn,
                    d.safe_hour_cap,
                    d.target_now,
                    d.projected_end,
                    d.required_rate,
                    d.recent_rate,
                    d.remaining_hours,
                    d.mode,
                    d.drain_gap,
                    d.drain_required_rate,
                    d.drain_pressure,
                    d.drain_level,
                    d.deadline_hours,
                    d.usage_source,
                )
                for d in decisions
            ],
        )
        self.conn.commit()

    def delete_absent_accounts(self, account_ids: list[int]) -> int:
        """删除本轮已不在 managed 集合里的账号状态和缓存。"""
        tables = ("account_state", "account_control", "account_profile_cache", "usage_sample")
        total = 0
        if account_ids:
            placeholders = ",".join("?" for _ in account_ids)
            params = tuple(account_ids)
            for table in tables:
                cur = self.conn.execute(
                    f"DELETE FROM {table} WHERE account_id NOT IN ({placeholders})",
                    params,
                )
                total += max(cur.rowcount, 0)
        else:
            for table in tables:
                cur = self.conn.execute(f"DELETE FROM {table}")
                total += max(cur.rowcount, 0)
        self.conn.commit()
        return total

    def prune(self, sample_days: int, decision_days: int, state_days: int) -> None:
        now = datetime.now(UTC)
        self.conn.execute(
            "DELETE FROM usage_sample WHERE sampled_at < ?", (_iso(now - timedelta(days=sample_days)),)
        )
        self.conn.execute(
            "DELETE FROM decision_log WHERE decided_at < ?", (_iso(now - timedelta(days=decision_days)),)
        )
        self.conn.execute(
            "DELETE FROM account_state WHERE updated_at < ?", (_iso(now - timedelta(days=state_days)),)
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
