"""Command line interface for the Meridian Energy API client.

Examples::

    uvx meridian login
    uvx meridian accounts
    uvx meridian usage --days 10
    uvx meridian usage --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

from meridian_energy.api import MeridianEnergyApi
from meridian_energy.auth import MeridianEnergyAuth, TokenSet
from meridian_energy.errors import MeridianAuthError, MeridianEnergyError

console = Console(stderr=False)
err_console = Console(stderr=True)

DEFAULT_CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "meridian-energy"
    / "tokens.json"
)


def _default_cache() -> Path:
    override = os.environ.get("MERIDIAN_TOKEN_CACHE")
    return Path(override) if override else DEFAULT_CACHE


def _load_tokens(cache: Path) -> TokenSet | None:
    if not cache.is_file():
        return None
    data = json.loads(cache.read_text())
    return TokenSet.from_dict(data)


def _save_tokens_factory(cache: Path) -> Callable[[TokenSet], Awaitable[None]]:
    async def save_tokens(tokens: TokenSet) -> None:
        def _write() -> None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, json.dumps(tokens.to_dict()).encode())
            finally:
                os.close(fd)

        await asyncio.to_thread(_write)

    return save_tokens


def _email_from_env_or_prompt() -> str:
    email = os.environ.get("MERIDIAN_EMAIL")
    if email:
        return email.strip().lower()
    return console.input("Meridian account email: ").strip().lower()


async def cmd_login(args: argparse.Namespace) -> int:
    cache: Path = args.token_cache
    email = args.email or _email_from_env_or_prompt()
    auth = MeridianEnergyAuth(on_token_update=_save_tokens_factory(cache))
    async with httpx.AsyncClient(timeout=30.0) as client:
        err_console.print(f"Requesting login code for [bold]{email}[/]…")
        journey_id = await auth.request_otp(client, email)
        otp = (
            args.otp
            or console.input("Enter the 6-digit code from your email: ").strip()
        )
        await auth.verify_otp(client, email, otp, journey_id)
    payload = json.loads(cache.read_text())
    payload["email"] = email
    cache.write_text(json.dumps(payload))
    os.chmod(cache, 0o600)
    err_console.print(f"[green]Signed in.[/] Tokens cached at {cache}")
    return 0


async def cmd_logout(args: argparse.Namespace) -> int:
    cache: Path = args.token_cache
    if cache.is_file():
        cache.unlink()
        err_console.print(f"Removed {cache}")
    else:
        err_console.print("Not signed in.")
    return 0


async def _api_from_cache(args: argparse.Namespace) -> MeridianEnergyApi:
    cache: Path = args.token_cache
    tokens = _load_tokens(cache)
    if tokens is None:
        raise MeridianAuthError(f"Not signed in. Run `meridian login` (cache: {cache})")
    auth = MeridianEnergyAuth(
        tokens=tokens, on_token_update=_save_tokens_factory(cache)
    )
    return MeridianEnergyApi(auth)


async def cmd_accounts(args: argparse.Namespace) -> int:
    api = await _api_from_cache(args)
    try:
        accounts = await api.get_accounts()
    finally:
        await api.aclose()

    if args.json:
        console.print_json(
            data=[a.model_dump(mode="json", by_alias=True) for a in accounts]
        )
        return 0

    if not accounts:
        err_console.print("[yellow]No accounts.[/]")
        return 1

    table = Table(title="Meridian accounts", show_lines=False)
    table.add_column("Account")
    table.add_column("Status")
    table.add_column("ICP")
    table.add_column("Name")
    table.add_column("Address")
    for account in accounts:
        addr = ""
        if account.properties:
            addr = (account.properties[0].address or "").replace("\n", ", ")
        table.add_row(
            account.number,
            account.status or "",
            account.primary_icp or "—",
            account.billing_name or "",
            addr,
        )
    console.print(table)
    return 0


async def cmd_usage(args: argparse.Namespace) -> int:
    api = await _api_from_cache(args)
    try:
        accounts = await api.get_accounts()
        if not accounts:
            err_console.print("[yellow]No accounts.[/]")
            return 1
        account_number = args.account or accounts[0].number
        summary = await api.get_usage(
            account_number,
            days=args.days,
            include_generation=not args.no_generation,
            skip_estimated=args.skip_estimated,
        )
    finally:
        await api.aclose()

    if args.json:
        payload: dict[str, Any] = {
            "account": account_number,
            "import_kwh": summary.import_kwh,
            "export_kwh": summary.export_kwh,
            "cost_nzd": summary.cost_nzd,
            "cost_currency": summary.cost_currency,
            "readings": len(summary.measurements),
            "measurements": [m.model_dump(mode="json") for m in summary.measurements],
        }
        console.print_json(data=payload)
        return 0

    table = Table(title=f"Usage · {account_number} · last {args.days} days")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Import", f"{summary.import_kwh:.3f} kWh")
    table.add_row("Export", f"{summary.export_kwh:.3f} kWh")
    currency = summary.cost_currency or "NZD"
    table.add_row("Cost (ex standing)", f"{summary.cost_nzd:.2f} {currency}")
    table.add_row("Intervals", str(len(summary.measurements)))
    console.print(table)
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    cache: Path = args.token_cache
    tokens = _load_tokens(cache)
    if tokens is None:
        console.print("Not signed in.")
        return 1
    email = ""
    try:
        email = json.loads(cache.read_text()).get("email") or ""
    except (OSError, json.JSONDecodeError):
        pass
    exp = tokens.expires_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    state = "[red]expired[/]" if tokens.is_expired else "[green]valid[/]"
    console.print(f"Signed in{f' as [bold]{email}[/]' if email else ''}")
    console.print(f"ID token {state}, expires {exp}")
    console.print(f"Cache: {cache}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="CLI for the Meridian Energy (NZ) customer API.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"meridian-energy {version('meridian-energy')}",
    )
    cache_flags = argparse.ArgumentParser(add_help=False)
    cache_flags.add_argument(
        "--token-cache",
        type=Path,
        default=_default_cache(),
        help=f"Token cache path (default: {_default_cache()})",
    )

    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", parents=[cache_flags], help="Email OTP sign-in")
    p_login.add_argument("--email", help="Account email (or MERIDIAN_EMAIL)")
    p_login.add_argument("--otp", help="One-time code (prompted if omitted)")
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser(
        "logout", parents=[cache_flags], help="Clear cached tokens"
    )
    p_logout.set_defaults(func=cmd_logout)

    p_status = sub.add_parser(
        "status", parents=[cache_flags], help="Show session status"
    )
    p_status.set_defaults(func=cmd_status)

    p_accounts = sub.add_parser("accounts", parents=[cache_flags], help="List accounts")
    p_accounts.add_argument("--json", action="store_true")
    p_accounts.set_defaults(func=cmd_accounts)

    p_usage = sub.add_parser("usage", parents=[cache_flags], help="Show recent usage")
    p_usage.add_argument("--account", help="Account number (default: first)")
    p_usage.add_argument("--days", type=int, default=10)
    p_usage.add_argument("--json", action="store_true")
    p_usage.add_argument("--no-generation", action="store_true")
    p_usage.add_argument("--skip-estimated", action="store_true")
    p_usage.set_defaults(func=cmd_usage)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        raise SystemExit(2)
    try:
        raise SystemExit(asyncio.run(args.func(args)))
    except MeridianEnergyError as err:
        err_console.print(f"[red]error:[/] {err}")
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
