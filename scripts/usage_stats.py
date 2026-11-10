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
GREEN = "\033[38;5;71m"
ORANGE = "\033[38;5;214m"
RED = "\033[38;5;160m"


def colour_for(remaining: int) -> str:
    return RED if remaining <= 20 else ORANGE if remaining <= 50 else GREEN


def bar(remaining: int, width: int = 20) -> str:
    filled = round(remaining * width / 100)
    return f"{colour_for(remaining)}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def fmt_delta(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours:02d}h" if days else f"{hours:02d}h {minutes:02d}m"


def provider_page(provider: str, data: dict) -> None:
    print(f"\n{BOLD}{LABELS[provider]}{RESET}", flush=True)
    if data.get("error"):
        print(f"  {RED}{data['error']}{RESET}")
        return
    for window in WINDOWS[provider]:
        selected = data["windows"].get(window)
        if selected is None:
            continue
        pct = selected["remaining"]
        reset_at = selected["reset_at"]
        reset = "reset not reported"
        if reset_at is not None:
            date = datetime.fromtimestamp(reset_at)
            delta = fmt_delta(reset_at - int(time.time()))
            reset = f"resets in {delta}  ({date:%b %d %H:%M})"
        print(f"  {selected['label']:<8} {colour_for(pct)}{BOLD}{pct:3d}% left{RESET} "
              f"{bar(pct)}  {DIM}{reset}{RESET}")
    if data.get("credits_used") is not None:
        print(f"  {DIM}{data['credits_used']:g} AI credits used this month{RESET}")
    elif not data["windows"]:
        print(f"  {data.get('status', 'No metered quota reported')}")
    updated = datetime.fromtimestamp(data["fetched_at"])
    print(f"  {DIM}Updated {updated:%H:%M:%S}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", nargs="?", choices=(*PROVIDERS, "all"), default="all")
    parser.add_argument("--nonce", default="0")
    args = parser.parse_args()
    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    print(f"{BOLD}AGENT USAGE{RESET}  {DIM}Account quotas · remaining allowance{RESET}", flush=True)
    for provider in providers:
        provider_page(provider, cached_usage(provider, args.nonce))
    print(f"\n{DIM}Read-only usage · cached for 2 minutes · arrows scroll · q closes{RESET}")


if __name__ == "__main__":
    main()
