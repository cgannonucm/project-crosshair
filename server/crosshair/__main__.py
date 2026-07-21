"""Entry points.

    python -m crosshair                 # MCP over stdio (what an agent harness runs)
    python -m crosshair serve           # the detached display daemon
    python -m crosshair status          # is a daemon running?
    python -m crosshair stop            # stop the daemon
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .daemon import DEFAULT_PORT


def _serve(args) -> None:
    from .daemon import serve

    try:
        asyncio.run(serve(host=args.host, port=args.port))
    except KeyboardInterrupt:
        pass


def _mcp(args) -> None:
    from .tools import mcp

    if args.data_dir:
        os.environ["CROSSHAIR_DATA_DIR"] = args.data_dir
    try:
        asyncio.run(mcp.run_stdio_async())
    except KeyboardInterrupt:
        pass


def _status(_args) -> None:
    from . import client

    async def run():
        url = await client.discover()
        if url is None:
            print("No Crosshair daemon running.")
            return 1
        health = await client._health(url) or {}
        print(f"Crosshair daemon on {url} (pid {health.get('pid')}) — "
              f"{health.get('views', 0)} view(s), {health.get('panels', 0)} panel(s), "
              f"browser {'connected' if health.get('browser_connected') else 'not connected'}")
        return 0

    sys.exit(asyncio.run(run()))


def _stop(_args) -> None:
    import httpx

    from . import client

    async def run():
        url = await client.discover()
        if url is None:
            print("No Crosshair daemon running.")
            return 0
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                await http.post(f"{url}/control/shutdown")
        except Exception:
            pass
        print(f"Stopped the daemon at {url}.")
        return 0

    sys.exit(asyncio.run(run()))


def main() -> None:
    parser = argparse.ArgumentParser(prog="crosshair", description=__doc__)
    parser.add_argument("--data-dir", default=None,
                        help="base directory for relative $ref paths (default: cwd)")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the detached display daemon")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int,
                         default=int(os.environ.get("CROSSHAIR_PORT", DEFAULT_PORT)))
    p_serve.set_defaults(func=_serve)

    sub.add_parser("status", help="report whether a daemon is running").set_defaults(func=_status)
    sub.add_parser("stop", help="stop the running daemon").set_defaults(func=_stop)

    args = parser.parse_args()
    (getattr(args, "func", None) or _mcp)(args)


if __name__ == "__main__":
    main()
