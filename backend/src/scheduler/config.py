"""配置加载：环境变量 > config.yaml > 内置默认值。敏感项只允许环境变量。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class Config:
    base_url: str = ""
    admin_key: str = ""

    platform: str = "anthropic"
    account_name_pattern: str = ""

    interval_minutes: int = 60
    target_7d_utilization: float = 97.0
    enable_5h_guard: bool = False
    pacing_target_7d_utilization: float = 97.0
    drain_target_7d_utilization: float = 99.4
    hard_cap_7d_utilization: float = 99.8
    hard_cap_5h_utilization: float = 98.0
    protect_7d_utilization: float = 97.0
    max_boost_ratio: float = 0.15
    mild_boost_ratio: float = 0.35
    max_boost_min: int = 1
    boost_load_factor_multiplier: float = 3.0
    mild_load_factor_multiplier: float = 2.0
    max_load_factor: int = 100
    max_active_probes_per_round: int = 10
    usage_stale_threshold_minutes: int = 90
    cooldown_minutes: int = 60
    safe_tail_hours: float = 2.0
    warmup_hours: float = 24.0
    strong_score_threshold: float = 3.0
    strong_min_required_rate: float = 0.6
    mild_score_threshold: float = 1.0
    ahead_band_pp: float = 3.0
    cooldown_abs_rate_pph: float = 1.2
    cooldown_required_rate_multiplier: float = 2.5
    cooldown_near_target_band_pp: float = 2.0
    will_hit_goal_soon_hours: float = 5.0
    emergency_window_hours: float = 12.0
    emergency_projected_end_threshold: float = 94.0
    emergency_final_gap_pp: float = 5.0
    emergency_rate_gap_pph: float = 0.8
    terminal_drain_enabled: bool = True
    terminal_window_hours: float = 36.0
    terminal_final_margin_hours: float = 0.25
    terminal_min_runway_hours: float = 0.25
    terminal_strong_gap_pp: float = 1.5
    terminal_mild_gap_pp: float = 0.4
    terminal_strong_required_rate_pph: float = 0.35
    terminal_mild_required_rate_pph: float = 0.10
    terminal_strong_pressure: float = 1.8
    terminal_mild_pressure: float = 0.9
    terminal_done_band_pp: float = 0.10
    terminal_min_recent_rate_pph: float = 0.05
    terminal_dynamic_load_factor_enabled: bool = True
    terminal_strong_load_factor_multiplier: float = 4.0
    terminal_mild_load_factor_multiplier: float = 2.5
    terminal_normal_load_factor_multiplier: float = 1.5
    terminal_max_load_factor: int = 100
    terminal_usage_stale_threshold_minutes: int = 20
    terminal_max_active_probes_per_round: int = 50
    terminal_active_probe_ratio: float = 0.50
    terminal_min_active_probes_per_round: int = 20
    terminal_interval_minutes: int = 15
    priority_bands: tuple[int, ...] = (1010, 1030, 1050, 1070, 1099)

    db_path: str = "/data/scheduler.db"
    heartbeat_file: str = "/data/last_tick"
    ui_enabled: bool = False
    ui_host: str = "0.0.0.0"
    ui_port: int = 18080
    ui_frame_ancestor_hosts: tuple[str, ...] = ()
    sample_retention_days: int = 14
    decision_retention_days: int = 30
    state_retention_days: int = 30

    window_hours: float = 168.0
    openai_subscription_base_url: str = "https://chatgpt.com/backend-api"
    account_profile_ttl_minutes: int = 720
    account_profile_refresh_enabled: bool = True

    @property
    def band_boost(self) -> int:
        return self.priority_bands[0]

    @property
    def band_mild(self) -> int:
        return self.priority_bands[1]

    @property
    def band_normal(self) -> int:
        return self.priority_bands[2]

    @property
    def band_protect(self) -> int:
        return self.priority_bands[3]

    @property
    def band_floor(self) -> int:
        return self.priority_bands[4]


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    config_path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    configured_keys: set[str] = set()

    if Path(config_path).is_file():
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        configured_keys = set(data)
        valid = {f.name for f in fields(Config)}
        for key, value in data.items():
            if key not in valid:
                log.warning("ignoring unknown config key: %s", key)
                continue
            if key == "priority_bands":
                value = tuple(int(v) for v in value)
            if key in {
                "account_profile_refresh_enabled",
                "enable_5h_guard",
                "terminal_drain_enabled",
                "terminal_dynamic_load_factor_enabled",
                "ui_enabled",
            }:
                value = _bool(value)
            if key == "ui_frame_ancestor_hosts":
                value = _string_tuple(value)
            setattr(cfg, key, value)

    if (
        "pacing_target_7d_utilization" not in configured_keys
        and "target_7d_utilization" in configured_keys
    ):
        cfg.pacing_target_7d_utilization = cfg.target_7d_utilization
    if "drain_target_7d_utilization" not in configured_keys:
        cfg.drain_target_7d_utilization = round(
            max(
                cfg.pacing_target_7d_utilization,
                cfg.hard_cap_7d_utilization - 0.4,
            ),
            4,
        )

    cfg.base_url = os.environ.get("SUB2API_BASE_URL", cfg.base_url).rstrip("/")
    cfg.admin_key = os.environ.get("SUB2API_ADMIN_KEY", cfg.admin_key)
    cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
    cfg.heartbeat_file = os.environ.get("HEARTBEAT_FILE", cfg.heartbeat_file)
    cfg.ui_enabled = _bool(os.environ.get("UI_ENABLED", cfg.ui_enabled))
    cfg.ui_host = os.environ.get("UI_HOST", cfg.ui_host)
    cfg.ui_port = int(os.environ.get("UI_PORT", cfg.ui_port))
    cfg.openai_subscription_base_url = os.environ.get(
        "OPENAI_SUBSCRIPTION_BASE_URL",
        cfg.openai_subscription_base_url,
    )
    cfg.account_profile_ttl_minutes = int(os.environ.get(
        "ACCOUNT_PROFILE_TTL_MINUTES",
        cfg.account_profile_ttl_minutes,
    ))
    cfg.account_profile_refresh_enabled = _bool(os.environ.get(
        "ACCOUNT_PROFILE_REFRESH_ENABLED",
        cfg.account_profile_refresh_enabled,
    ))

    if not cfg.base_url or not cfg.admin_key:
        raise ValueError("SUB2API_BASE_URL and SUB2API_ADMIN_KEY are required (env)")
    if cfg.platform not in ("anthropic", "openai"):
        raise ValueError(f"unsupported platform: {cfg.platform} (anthropic / openai)")
    if len(cfg.priority_bands) != 5 or list(cfg.priority_bands) != sorted(cfg.priority_bands):
        raise ValueError("priority_bands must be 5 ascending values")
    if cfg.drain_target_7d_utilization >= cfg.hard_cap_7d_utilization:
        raise ValueError("drain_target_7d_utilization must be lower than hard_cap_7d_utilization")
    if cfg.pacing_target_7d_utilization > cfg.drain_target_7d_utilization:
        raise ValueError("pacing_target_7d_utilization must be <= drain_target_7d_utilization")
    if cfg.terminal_window_hours <= 0:
        raise ValueError("terminal_window_hours must be positive")
    if cfg.terminal_final_margin_hours < 0:
        raise ValueError("terminal_final_margin_hours must be >= 0")
    return cfg


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in value if str(v).strip())
