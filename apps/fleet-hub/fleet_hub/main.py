import argparse
import os
from pathlib import Path

import uvicorn
import yaml

from packages.protocol.network import resolve_bind

from .app import create_app
from .notifications import load_env


def main():
    parser = argparse.ArgumentParser(description="Start the fleet control plane")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Private .env for notification and authentication settings",
    )
    parser.add_argument("--unsafe-bind-all", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8-sig"))
    if config.get("use_private_env", True):
        files = (
            [args.env_file]
            if args.env_file
            else [
                Path(__file__).resolve().parents[3] / ".env",
                args.config.resolve().parent / ".env",
            ]
        )
        load_env(config, files)
    host = resolve_bind(config, unsafe_bind_all=args.unsafe_bind_all)
    uvicorn.run(
        create_app(config),
        host=host,
        port=int(config.get("port", 8765)),
        proxy_headers=False,
        access_log=False,
        ssl_certfile=config.get("tls_cert"),
        ssl_keyfile=config.get("tls_key"),
    )


if __name__ == "__main__":
    main()
