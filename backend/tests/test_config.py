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
