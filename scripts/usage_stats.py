#!/usr/bin/env python3
"""Detailed, cached usage dashboard for tmux popups."""

import argparse
from datetime import datetime
import sys
import time

sys.dont_write_bytecode = True
from usage_data import LABELS, PROVIDERS, WINDOWS, cached_usage

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ACCENT = "\033[38;5;117m"
ORANGE = "\033[38;5;214m"
RED = "\033[38;5;160m"
BAR_WIDTH = 16


def colour_for(remaining: int) -> str:
    return RED if remaining <= 20 else ORANGE if remaining <= 50 else ACCENT


def bar(remaining: int) -> str:
    filled = round(max(0, min(100, remaining)) * BAR_WIDTH / 100)
    return (f"{colour_for(remaining)}{'█' * filled}"
            f"{DIM}{'░' * (BAR_WIDTH - filled)}{RESET}")


def fmt_delta(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


def provider_page(provider: str, data: dict) -> None:
    print(f"\n{BOLD}{LABELS[provider]}{RESET}", flush=True)
    if data.get("error"):
        print(f"  {RED}{data['error']}{RESET}")
        return
    selected_windows = data.get("details") or [
        data["windows"][window] for window in WINDOWS[provider] if window in data["windows"]
    ]
    for selected in selected_windows:
        pct = selected["remaining"]
        reset_at = selected["reset_at"]
        reset = "--"
        if reset_at is not None:
            date = datetime.fromtimestamp(reset_at)
            delta = fmt_delta(reset_at - int(time.time()))
            reset = f"{delta:<8} {date:%b %d %H:%M}"
        print(f"  {selected['label']:<32} {colour_for(pct)}{BOLD}{pct:3d}%{RESET}"
              f"  {bar(pct)}    {DIM}{reset}{RESET}")
    if data.get("credits_used") is not None:
        print(f"  {DIM}{data['credits_used']:,.0f} AI credits used this month{RESET}")
    elif not data["windows"]:
        print(f"  {data.get('status', 'No metered quota reported')}")
    if data.get("balance_usd") is not None:
        print(f"  {DIM}${data['balance_usd']:.2f} prepaid balance{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", nargs="?", choices=(*PROVIDERS, "all"), default="all")
    parser.add_argument("--nonce", default="0")
    args = parser.parse_args()
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    print(f"{BOLD}AGENT USAGE{RESET}  {DIM}Account quotas · remaining allowance{RESET}", flush=True)
    print(f"\n  {DIM}{'Window':<32} {'Left':>4}  {'':<{BAR_WIDTH}}    Resets in{RESET}")
    for provider in providers:
        provider_page(provider, cached_usage(provider, args.nonce))
    print(f"\n{DIM}Read-only usage · cached for 2 minutes · arrows scroll · q closes{RESET}")


if __name__ == "__main__":
    main()
