"""配置加载：环境变量 > config.yaml > 内置默认值。敏感项只允许环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass
class Config:
    base_url: str = ""
    admin_key: str = ""

    platform: str = "anthropic"
    account_name_pattern: str = ""

    interval_minutes: int = 60
    target_7d_utilization: float = 97.0
    hard_cap_7d_utilization: float = 99.2
    hard_cap_5h_utilization: float = 98.0
    protect_7d_utilization: float = 97.0
    max_boost_ratio: float = 0.15
    mild_boost_ratio: float = 0.35
    max_boost_min: int = 1
    max_active_probes_per_round: int = 10
    usage_stale_threshold_minutes: int = 90
    cooldown_minutes: int = 60
    safe_tail_hours: float = 2.0
    strong_score_threshold: float = 3.0
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
    priority_bands: tuple[int, ...] = (1010, 1030, 1050, 1070, 1099)

    db_path: str = "/data/scheduler.db"
    heartbeat_file: str = "/data/last_tick"
    ui_enabled: bool = False
    ui_host: str = "0.0.0.0"
    ui_port: int = 18080
    sample_retention_days: int = 14
    decision_retention_days: int = 30
    state_retention_days: int = 30

    window_hours: float = 168.0

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

    if Path(config_path).is_file():
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        valid = {f.name for f in fields(Config)}
        for key, value in data.items():
            if key not in valid:
                raise ValueError(f"unknown config key: {key}")
            if key == "priority_bands":
                value = tuple(int(v) for v in value)
            if key == "ui_enabled":
                value = _bool(value)
            setattr(cfg, key, value)

    cfg.base_url = os.environ.get("SUB2API_BASE_URL", cfg.base_url).rstrip("/")
    cfg.admin_key = os.environ.get("SUB2API_ADMIN_KEY", cfg.admin_key)
    cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
    cfg.heartbeat_file = os.environ.get("HEARTBEAT_FILE", cfg.heartbeat_file)
    cfg.ui_enabled = _bool(os.environ.get("UI_ENABLED", cfg.ui_enabled))
    cfg.ui_host = os.environ.get("UI_HOST", cfg.ui_host)
    cfg.ui_port = int(os.environ.get("UI_PORT", cfg.ui_port))

    if not cfg.base_url or not cfg.admin_key:
        raise ValueError("SUB2API_BASE_URL and SUB2API_ADMIN_KEY are required (env)")
    if cfg.platform not in ("anthropic", "openai"):
        raise ValueError(f"unsupported platform: {cfg.platform} (anthropic / openai)")
    if len(cfg.priority_bands) != 5 or list(cfg.priority_bands) != sorted(cfg.priority_bands):
        raise ValueError("priority_bands must be 5 ascending values")
    return cfg


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
