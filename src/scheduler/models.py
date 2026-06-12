"""共享数据模型。所有百分比字段用 0-100 表示，时间一律 UTC aware datetime。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class AccountSnapshot:
    """一个受控账号在本轮 tick 的快照（来自 list / active 探测合并）。"""

    id: int
    name: str
    priority: int
    status: str
    schedulable: bool
    rate_limited: bool
    overloaded: bool
    temp_unschedulable: bool
    type: str = ""
    five_hour_used: float | None = None
    seven_day_used: float | None = None
    seven_day_sonnet_used: float | None = None
    seven_day_reset_at: datetime | None = None
    five_hour_reset_at: datetime | None = None
    sampled_at: datetime | None = None
    recent_5h_burn: float | None = None
    usage_source: str = "missing"  # passive / active / missing

    @property
    def eligible(self) -> bool:
        return (
            self.schedulable
            and self.status == "active"
            and not self.rate_limited
            and not self.overloaded
            and not self.temp_unschedulable
        )


@dataclass
class AccountState:
    """每账号控制状态，持久化在 account_state 表。"""

    account_id: int
    last_priority: int | None = None
    last_7d_used: float | None = None
    last_5h_used: float | None = None
    last_7d_reset_at: datetime | None = None
    last_sampled_at: datetime | None = None
    hourly_burn_ewma: float = 0.0
    cooldown_until: datetime | None = None
    last_boost_at: datetime | None = None
    probe_failures: int = 0


@dataclass
class Decision:
    """单账号一轮决策结果，落 decision_log 表并打日志。"""

    account_id: int
    name: str
    current_priority: int
    target_priority: int
    reason: str
    catchup_score: float | None = None
    seven_day_used: float | None = None
    seven_day_sonnet_used: float | None = None
    five_hour_used: float | None = None
    recent_hour_burn: float | None = None
    recent_5h_burn: float | None = None
    safe_hour_cap: float | None = None
    target_now: float | None = None
    projected_end: float | None = None
    required_rate: float | None = None
    recent_rate: float | None = None
    remaining_hours: float | None = None
    usage_source: str = "missing"

    @property
    def changed(self) -> bool:
        return self.target_priority != self.current_priority
