from scheduler.config import load_config


def test_ui_config_from_env(monkeypatch):
    monkeypatch.setenv("SUB2API_BASE_URL", "http://x")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "k")
    monkeypatch.setenv("UI_ENABLED", "true")
    monkeypatch.setenv("UI_HOST", "127.0.0.1")
    monkeypatch.setenv("UI_PORT", "19090")

    cfg = load_config()

    assert cfg.ui_enabled is True
    assert cfg.ui_host == "127.0.0.1"
    assert cfg.ui_port == 19090
    assert cfg.enable_5h_guard is False
    assert cfg.pacing_target_7d_utilization == 97.0
    assert cfg.drain_target_7d_utilization == 99.4
    assert cfg.hard_cap_7d_utilization == 99.8


def test_legacy_target_backfills_pacing_target(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("target_7d_utilization: 96.5\nhard_cap_7d_utilization: 99.8\n", encoding="utf-8")
    monkeypatch.setenv("SUB2API_BASE_URL", "http://x")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "k")

    cfg = load_config(str(config))

    assert cfg.pacing_target_7d_utilization == 96.5


def test_invalid_drain_target_rejected(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "drain_target_7d_utilization: 99.9\nhard_cap_7d_utilization: 99.8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB2API_BASE_URL", "http://x")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "k")

    try:
        load_config(str(config))
    except ValueError as e:
        assert "drain_target_7d_utilization" in str(e)
    else:
        raise AssertionError("expected invalid drain target")


def test_ui_frame_ancestor_hosts_from_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
ui_frame_ancestor_hosts:
  - https://example.com
  - https://admin.example.com:8443
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB2API_BASE_URL", "http://x")
    monkeypatch.setenv("SUB2API_ADMIN_KEY", "k")

    cfg = load_config(str(config))

    assert cfg.ui_frame_ancestor_hosts == (
        "https://example.com",
        "https://admin.example.com:8443",
    )
