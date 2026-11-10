#!/usr/bin/env python3
"""Read-only account quotas, shared by the status bar and detailed popup."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.dont_write_bytecode = True
import fetch_claude_usage as claude
import fetch_codex_usage as codex

PROVIDERS = ("claude", "codex", "copilot", "opencodego", "antigravity", "grok")
WINDOWS = {
    "claude": ("5h", "weekly"),
    "codex": ("5h", "weekly"),
    "copilot": ("monthly",),
    "opencodego": ("5h", "weekly", "monthly"),
    "antigravity": ("5h", "weekly"),
    "grok": ("credits",),
}
LABELS = {
    "claude": "Claude Code", "codex": "Codex",
    "copilot": "GitHub Copilot", "opencodego": "OpenCode Go",
    "antigravity": "Antigravity", "grok": "Grok",
}


class UsageError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UsageError("usage endpoint redirected; refusing to forward credentials")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise UsageError(f"expected a JSON object in {path}")
    return value


def number(value) -> float:
    if isinstance(value, bool):
        raise UsageError("invalid numeric quota")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise UsageError("missing or invalid numeric quota") from None
    if not math.isfinite(result):
        raise UsageError("non-finite numeric quota")
    return result


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(number(value))
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise UsageError("invalid quota reset date") from None
    return int(date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp())


def meter(remaining, reset_at, label: str) -> dict:
    return {"remaining": max(0, min(100, round(number(remaining)))),
            "reset_at": timestamp(reset_at), "label": label}


def request_json(url: str, token: str, headers=None, scheme="Bearer") -> dict:
    request = Request(url, headers={
        "Authorization": f"{scheme} {token}",
        "Accept": "application/json",
        "User-Agent": "agent-usage-tmux/1.0",
        **(headers or {}),
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            value = json.load(response)
    except HTTPError as exc:
        # Never put raw API bodies (which can include account data) in the cache.
        raise UsageError(f"HTTP {exc.code}; check the provider login or subscription") from None
    except URLError:
        raise UsageError("usage request failed; check network connectivity") from None
    if not isinstance(value, dict):
        raise UsageError("usage endpoint returned a non-object response")
    return value


def copilot_token() -> str:
    for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=5, check=False)
    except FileNotFoundError:
        raise UsageError("set COPILOT_GITHUB_TOKEN or install gh and run gh auth login") from None
    if result.returncode or not result.stdout.strip():
        raise UsageError("run gh auth login or set COPILOT_GITHUB_TOKEN")
    return result.stdout.strip()


def credential_token(value: dict) -> str:
    if not isinstance(value, dict):
        raise UsageError("invalid OpenCode credential; run opencode auth login")
    kind = value.get("type")
    if kind in ("key", "api"):
        token = value.get("key")
    elif kind == "oauth":
        if number(value.get("expires")) / 1000 <= time.time():
            raise UsageError("OpenCode login expired; run opencode auth login")
        token = value.get("access")
    else:
        raise UsageError("unsupported OpenCode credential type")
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise UsageError("invalid OpenCode credential; run opencode auth login")
    return token


def opencode_token() -> str:
    if os.environ.get("OPENCODE_GO_API_KEY"):
        return os.environ["OPENCODE_GO_API_KEY"]
    data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data_home.is_absolute():
        data_home = Path.home() / ".local/share"
    directory = data_home / "opencode"
    filename = os.environ.get("OPENCODE_DB", "opencode.db")
    if filename == ":memory:":
        raise UsageError("OPENCODE_DB=:memory: has no readable credentials")
    database = directory / filename
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential'"
            ).fetchone():
                rows = connection.execute(
                    "SELECT integration_id,value FROM credential WHERE active=1 "
                    "AND integration_id IN ('opencode-go','opencode') "
                    "ORDER BY CASE integration_id WHEN 'opencode-go' THEN 0 ELSE 1 END"
                ).fetchall()
                if not rows:
                    raise UsageError("no active OpenCode login; run opencode auth login")
                if len(rows) > 1 and rows[0][0] == rows[1][0]:
                    raise UsageError("ambiguous OpenCode logins; select one active account")
                return credential_token(json.loads(rows[0][1]))
    auth_file = directory / "auth.json"
    if auth_file.exists():
        entry = read_json(auth_file).get("opencode-go")
        if isinstance(entry, dict):
            return credential_token(entry)
    raise UsageError("run opencode auth login for OpenCode Go or set OPENCODE_GO_API_KEY")


def parse_copilot(payload: dict) -> dict:
    if payload.get("access_type_sku") == "subscription_ended":
        raise UsageError("Copilot subscription ended")
    quotas = payload.get("quota_snapshots")
    quota = quotas.get("premium_interactions") if isinstance(quotas, dict) else None
    if not isinstance(quota, dict):
        raise UsageError("Copilot premium quota is unavailable")
    reset = payload.get("quota_reset_date")
    credits = quota.get("credits_used")
    windows = {}
    placeholder = quota.get("entitlement") == 0 and quota.get("remaining") == 0
    if not quota.get("unlimited") and not placeholder:
        remaining = quota.get("percent_remaining")
        if remaining is None and quota.get("entitlement") is not None and number(quota["entitlement"]) > 0:
            remaining = 100 * number(quota.get("remaining")) / number(quota["entitlement"])
        if remaining is not None:
            windows["monthly"] = meter(remaining, reset, "Monthly")
    status = "unlimited" if quota.get("unlimited") else "quota unavailable"
    if credits is not None:
        status = f"{number(credits):g} credits used"
    if not windows and credits is None and not quota.get("unlimited"):
        raise UsageError("Copilot returned no metered quota or credit counter")
    return {"windows": windows, "status": status, "credits_used": credits}


def first_value(data: dict, keys):
    return next((data[key] for key in keys if data.get(key) is not None), None)


def parse_opencode(payload: dict, now: float) -> dict:
    usage = payload.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("rolling"), dict):
        raise UsageError("OpenCode Go response is missing rolling usage")
    windows = {}
    for source, window, label in (
        ("rolling", "5h", "5 hours"), ("weekly", "weekly", "Weekly"),
        ("monthly", "monthly", "Monthly"),
    ):
        data = usage.get(source)
        if data is None:
            continue
        if not isinstance(data, dict):
            raise UsageError(f"invalid OpenCode Go {source} meter")
        used = first_value(data, (
            "usagePercent", "usedPercent", "percentUsed", "percent", "usage_percent",
            "used_percent", "utilization", "utilizationPercent", "utilization_percent", "usage",
        ))
        if used is None:
            consumed = first_value(data, ("used", "usage", "consumed", "count", "usedTokens", "usedMicroCents"))
            limit = number(first_value(data, ("limit", "total", "quota", "max", "cap", "tokenLimit", "limitMicroCents")))
            if limit <= 0:
                raise UsageError(f"invalid OpenCode Go {source} quota limit")
            used = 100 * number(consumed) / limit
        reset_in = first_value(data, (
            "resetInSec", "resetInSeconds", "resetSeconds", "reset_sec", "reset_in_sec",
            "resetsInSec", "resetsInSeconds", "resetIn", "resetSec",
        ))
        reset = first_value(data, ("resetAt", "resetsAt", "reset_at", "resets_at",
                                  "nextReset", "next_reset", "renewAt", "renew_at"))
        if reset_in is not None:
            reset = int(now + max(0, number(reset_in)))
        # The API reports 0...100, not fractions: 0.5 means 0.5% used.
        windows[window] = meter(100 - number(used), reset, label)
    return {"windows": windows}


def grok_token() -> str:
    if os.environ.get("GROK_OAUTH_TOKEN"):
        token = os.environ["GROK_OAUTH_TOKEN"]
    else:
        path = Path(os.environ.get("GROK_HOME") or Path.home() / ".grok").expanduser() / "auth.json"
        if not path.exists():
            raise UsageError("Grok is not signed in; run grok login")
        entries = read_json(path)
        candidates = [
            (scope, entry) for scope, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"]
            and (scope.startswith("https://auth.x.ai::") or "/sign-in" in scope)
        ]
        candidates.sort(key=lambda pair: not pair[0].startswith("https://auth.x.ai::"))
        if not candidates:
            raise UsageError("no Grok OAuth login; run grok login")
        entry = candidates[0][1]
        if entry.get("expires_at") and timestamp(entry["expires_at"]) <= time.time():
            raise UsageError("Grok login expired; run grok login")
        if str(entry.get("principal_type", "")).lower() == "team":
            raise UsageError("Grok does not expose supported team usage")
        token = entry["key"]
    if token.startswith("xai-"):
        raise UsageError("Grok usage needs a subscriber OAuth token, not an xAI API key")
    return token


def parse_grok(payload: dict) -> dict:
    config = payload.get("config")
    if not isinstance(config, dict):
        raise UsageError("Grok billing response is missing credit configuration")
    period = config.get("currentPeriod") or {}
    reset = period.get("end") or config.get("billingPeriodEnd")
    used = config.get("creditUsagePercent")
    if used is None:
        cap = (config.get("onDemandCap") or {}).get("val")
        consumed = (config.get("onDemandUsed") or {}).get("val")
        if cap is not None and consumed is not None and number(cap) > 0:
            used = number(consumed) / number(cap) * 100
    data = {"windows": {}}
    if used is not None:
        data["windows"]["credits"] = meter(100 - number(used), reset, "Credits")
    wallet = config.get("prepaidBalance")
    if isinstance(wallet, dict):
        cents = number(wallet.get("val", 0))
        if cents >= 0:
            data["balance_usd"] = cents / 100
    if not data["windows"]:
        # A current billing period without a percentage is unknown, not zero usage.
        data["status"] = "credit usage unknown"
        if reset:
            data["period_reset_at"] = timestamp(reset)
        elif "balance_usd" not in data:
            raise UsageError("Grok returned no credit meter, billing period, or wallet")
    return data


def parse_antigravity(payload: dict) -> dict:
    command = payload.get("command")
    if payload.get("status") != "SUCCESS" or not isinstance(command, dict) or command.get("name") != "usage":
        raise UsageError("Antigravity usage report failed; run agy and sign in")
    report = command.get("data")
    if not isinstance(report, dict) or not isinstance(report.get("groups"), list):
        raise UsageError("Antigravity report is missing quota groups")
    windows = {}
    details = []
    for group in report["groups"]:
        if not isinstance(group, dict):
            raise UsageError("invalid Antigravity quota group")
        name = group.get("name") or group.get("display_name") or group.get("displayName") or "Models"
        if not isinstance(name, str) or "gemini" not in name.casefold():
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                raise UsageError("invalid Antigravity quota bucket")
            if bucket.get("disabled"):
                continue
            fraction = first_value(bucket, ("remaining_fraction", "remainingFraction"))
            nested = bucket.get("remaining")
            if fraction is None and isinstance(nested, dict):
                fraction = first_value(nested, ("remaining_fraction", "remainingFraction"))
                if fraction is None and nested.get("case") == "remainingFraction":
                    fraction = nested.get("value")
            if fraction is None:
                continue
            window = bucket.get("window")
            label = "5 hours" if window == "5h" else "Weekly" if window == "weekly" else (
                bucket.get("name") or bucket.get("display_name") or "Quota")
            selected = meter(
                number(fraction) * 100,
                first_value(bucket, ("reset_time", "resetTime")), f"{name} / {label}")
            details.append(selected)
            if window in WINDOWS["antigravity"]:
                # Show the bottleneck, not an average of independent model allowances.
                if window not in windows or selected["remaining"] < windows[window]["remaining"]:
                    windows[window] = selected
    if not details:
        raise UsageError("Antigravity reported no measured, enabled Gemini quota")
    if not windows:
        raise UsageError("Antigravity reported unrecognized quota windows")
    return {"windows": windows, "details": details}


def run_agy(arguments: list[str], environment: dict, directory: str, timeout: float) -> bytes:
    """Bound output and lifetime, including subprocesses in the owned process group."""
    process = subprocess.Popen(
        arguments, env=environment, cwd=directory, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("Antigravity usage timed out; run agy and check the login")
            if not select.select([process.stdout], [], [], min(remaining, 0.1))[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 1048576:
                raise UsageError("Antigravity usage report exceeds 1 MiB")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise UsageError("Antigravity usage command failed; run agy and sign in")
        return bytes(output)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def fetch_antigravity() -> dict:
    binary = os.environ.get("ANTIGRAVITY_CLI_PATH") if "ANTIGRAVITY_CLI_PATH" in os.environ else shutil.which("agy")
    if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise UsageError("install agy or set ANTIGRAVITY_CLI_PATH, then sign in with agy")
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix="agent-usage-agy-") as directory:
        environment = dict(os.environ, AGY_CLI_DISABLE_AUTO_UPDATE="true")
        for name in ("open", "xdg-open"):
            launcher = Path(directory) / name
            launcher.write_text("#!/bin/sh\nexit 1\n")
            launcher.chmod(0o700)
        environment["PATH"] = directory + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
        environment["BROWSER"] = str(Path(directory) / "open")
        version = run_agy([binary, "--version"], environment, directory, 3).decode().strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or tuple(map(int, version.split("."))) < (1, 1, 11):
            raise UsageError("read-only Antigravity usage reports require agy 1.1.11 or later")
        report = run_agy(
            [binary, "-p", "/usage", "--output-format", "json", "--print-timeout", "30s"],
            environment, directory, 35)
        return parse_antigravity(json.loads(report))


def fetch_provider(provider: str) -> dict:
    if provider == "antigravity":
        return fetch_antigravity()
    if provider == "grok":
        return parse_grok(request_json(
            "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
            grok_token(), {"x-xai-token-auth": "xai-grok-cli"}))
    if provider == "copilot":
        return parse_copilot(request_json(
            "https://api.github.com/copilot_internal/user", copilot_token(), {
                "Editor-Version": "vscode/1.96.2",
                "Editor-Plugin-Version": "copilot-chat/0.26.7",
                "User-Agent": "GitHubCopilotChat/0.26.7",
                "X-Github-Api-Version": "2025-04-01",
            }, scheme="token"))
    if provider == "opencodego":
        return parse_opencode(request_json(
            "https://opencode.ai/zen/go/v1/usage", opencode_token()), time.time())
    if provider == "claude":
        headers = claude.fetch_rate_limit_headers(
            claude.DEFAULT_MESSAGES_URL, claude.load_credentials(claude.DEFAULT_CREDENTIALS_PATH), 15)
        windows = {}
        for key, source in (("5h", "5h"), ("weekly", "7d")):
            if f"anthropic-ratelimit-unified-{source}-utilization" in headers:
                pct, reset, _ = claude.parse_utilisation(headers, source)
                windows[key] = meter(pct, reset, "5 hours" if key == "5h" else "Weekly")
        if not windows:
            raise UsageError("Claude returned no usage windows")
        return {"windows": windows}
    if provider == "codex":
        token, account = codex.load_auth(codex.DEFAULT_AUTH_PATH)
        payload = codex.fetch_usage(codex.DEFAULT_URL, token, account, 15, "")
        windows = {}
        for fallback, source in (("5h", "primary"), ("weekly", "secondary")):
            window_data = (payload.get("rate_limit") or {}).get(f"{source}_window")
            if window_data:
                duration = window_data.get("limit_window_seconds")
                key = {18000: "5h", 604800: "weekly"}.get(duration, fallback)
                pct, reset, _ = codex.parse_utilisation(payload, source)
                label = "5 hours" if key == "5h" else "Weekly"
                if duration and duration not in (18000, 604800):
                    label = f"{number(duration) / 3600:g} hours"
                windows[key] = meter(pct, reset, label)
        if not windows:
            raise UsageError("Codex returned no usage windows")
        return {"windows": windows}
    raise UsageError(f"unknown provider: {provider}")


def cache_directory() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "agent-usage-tmux"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def cached_usage(provider: str, nonce: str = "0") -> dict:
    directory = cache_directory()
    path = directory / f"{provider}.json"
    # Serialize overlapping status jobs; switching windows reuses the same response.
    with (directory / f"{provider}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            try:
                cached = read_json(path)
            except json.JSONDecodeError:
                cached = {}
            ttl = 30 if cached.get("error") else 120
            age = time.time() - cached.get("fetched_at", 0)
            if cached.get("nonce") == nonce and 0 <= age < ttl:
                return cached
        try:
            data = fetch_provider(provider)
        except SystemExit as exc:
            # Legacy fetchers report errors via SystemExit; don't persist raw API bodies.
            message = str(exc).split(": ", 1)[0]
            data = {"windows": {}, "error": f"{message}; check provider login"}
        except (UsageError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            message = str(exc) if isinstance(exc, UsageError) else f"cannot read {LABELS[provider]} usage ({type(exc).__name__})"
            data = {"windows": {}, "error": message}
        data.update(provider=provider, fetched_at=time.time(), nonce=nonce)
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(data, handle)
                handle.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--window", choices=("5h", "weekly", "monthly", "credits"))
    parser.add_argument("--nonce", default="0")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = cached_usage(args.provider, args.nonce)
    if args.json:
        print(json.dumps(data))
    elif data.get("error"):
        print(data["error"], file=sys.stderr)
        raise SystemExit(1)
    else:
        window = args.window or WINDOWS[args.provider][0]
        if args.provider in ("codex", "antigravity") and len(data["windows"]) == 1 and window == "5h":
            window = next(iter(data["windows"]))
        selected = data["windows"].get(window)
        if selected:
            reset = selected["reset_at"]
            seconds = "-" if reset is None else str(max(0, reset - int(time.time())))
            print(f"{selected['remaining']}\t{seconds}\t{window}")
        elif not data["windows"] and data.get("status"):
            print(f"status\t{data['status']}")
        else:
            print(f"{window} quota unavailable for {LABELS[args.provider]}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
