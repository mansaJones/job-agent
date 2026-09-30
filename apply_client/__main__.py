"""python -m apply_client --watch | --queue-id N | --dry-run --url URL"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console
from rich.logging import RichHandler

from apply_client.config import ConfigError, load_config

console = Console()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apply_client",
        description="Fill job applications from the Jetson's apply queue. Never submits — you do.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="Poll the queue and process requests")
    mode.add_argument("--queue-id", type=int, metavar="N", help="Process one queue request, then exit")
    mode.add_argument("--dry-run", action="store_true",
                      help="Open --url, show what would be filled, fill nothing")
    parser.add_argument("--url", help="Page to inspect with --dry-run")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)
    if args.dry_run and not args.url:
        parser.error("--dry-run needs --url")
    return args


async def _main(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    from apply_client.browser import launch_persistent_context
    from apply_client.jetson_api import AuthError, JetsonAPI, JetsonAPIError, NotConfiguredError
    from apply_client.runner import ApplyRunner, dry_run

    try:
        cfg = load_config(require_jetson=not args.dry_run)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    async with async_playwright() as pw:
        if args.dry_run:
            await dry_run(pw, cfg, args.url, console)
            return 0

        async with JetsonAPI(cfg.jetson_url, cfg.apply_client_token) as api:
            try:
                health = await api.health()
            except (AuthError, NotConfiguredError) as e:
                console.print(f"[red]{e}[/red]\nAPPLY_CLIENT_TOKEN in apply_client/.env must match "
                              "APPLY_CLIENT_TOKEN in the Jetson's config/secrets.env.")
                return 2
            except JetsonAPIError as e:
                console.print(f"[red]{e}[/red]")
                return 2
            console.print(f"Connected to {cfg.jetson_url} — {health.get('pending', 0)} pending")

            context = await launch_persistent_context(pw, cfg)
            try:
                runner = ApplyRunner(cfg, api, context, console)
                if args.watch:
                    await runner.run_watch()
                else:
                    pending = {r.queue_id for r in await api.get_pending()}
                    if args.queue_id not in pending:
                        console.print(f"[red]#{args.queue_id} isn't pending — check the Apply "
                                      "Queue page on the dashboard[/red]")
                        return 1
                    await runner.run_one(args.queue_id)
            finally:
                await context.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", handlers=[RichHandler(console=console, show_path=False)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        console.print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
