import argparse
import logging

from .config import load_config
from .runner import run
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="sub2api account 7D pacing controller")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--ui", action="store_true", help="run the read-only dashboard")
    parser.add_argument("--ui-host", default=None, help="dashboard bind host")
    parser.add_argument("--ui-port", type=int, default=None, help="dashboard bind port")
    parser.add_argument("--migrate-db", action="store_true", help="apply database migrations and exit")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(args.config)
    if args.migrate_db:
        Store(cfg.db_path).close()
        logging.getLogger(__name__).info("database migrated: %s", cfg.db_path)
        return
    ui_host = args.ui_host or cfg.ui_host
    ui_port = args.ui_port or cfg.ui_port
    if args.ui:
        from .ui import serve

        serve(ui_host, ui_port, cfg.db_path, cfg.heartbeat_file, cfg.platform, cfg.account_name_pattern)
        return
    if cfg.ui_enabled:
        from .ui import start_background

        start_background(ui_host, ui_port, cfg.db_path, cfg.heartbeat_file, cfg.platform, cfg.account_name_pattern)
        logging.getLogger(__name__).info("dashboard listening on %s:%s", ui_host, ui_port)
    run(cfg, once=args.once)


if __name__ == "__main__":
    main()
