import argparse
from pathlib import Path

import uvicorn
import yaml

from packages.protocol.network import resolve_bind

from .app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--unsafe-bind-all", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
    config["_config_file"] = str(args.config.resolve())
    uvicorn.run(
        create_app(config),
        host=resolve_bind(config, args.unsafe_bind_all),
        port=config.get("port", 8766),
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
