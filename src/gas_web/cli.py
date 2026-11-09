"""Run the local, single-origin conversation UI."""

import argparse

import uvicorn
from dotenv import load_dotenv

from .app import create_app, default_db_path


def parser():
    result = argparse.ArgumentParser(
        description="Local IACPaaS chat; anonymous cookie identity, not account login."
    )
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8080)
    result.add_argument("--db", default=str(default_db_path()), help="SQLite database path")
    result.add_argument("--env-file", help="Private configuration file, e.g. a Docker secret")
    result.add_argument(
        "--trusted-host", action="append", help="Exact permitted Host; repeat for more"
    )
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if not 1 <= args.port <= 65535:
        arguments.error("--port must be between 1 and 65535")
    loaded = load_dotenv(args.env_file, interpolate=False)
    if args.env_file and not loaded:
        arguments.error("Не удалось загрузить указанный файл настроек.")
    uvicorn.run(
        create_app(db_path=args.db, bind_host=args.host, trusted_hosts=args.trusted_host),
        host=args.host,
        port=args.port,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
