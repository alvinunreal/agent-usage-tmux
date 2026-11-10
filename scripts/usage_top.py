#!/usr/bin/env python3
"""Maintain a one-line usage pane without moving tmux's normal status bar."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from usage_data import PROVIDERS, cache_directory

SCRIPT = Path(__file__).resolve()
ANSI = re.compile(r"\033\[[0-9;]*m")
HOOKS = (
    "after-new-session", "after-new-window", "after-split-window",
    "after-kill-pane", "pane-exited", "after-select-layout", "after-resize-pane", "client-resized",
)
NAMES = {
    "claude": "Claude", "codex": "Codex", "copilot": "Copilot",
    "opencodego": "OpenCode", "antigravity": "Gemini", "grok": "Grok",
}


def tmux(*arguments: str) -> str:
    return subprocess.run(
        ["tmux", *arguments], capture_output=True, text=True, check=True).stdout.strip()


def configure() -> None:
    enabled = tmux("show-option", "-gqv", "@agent_usage_top") == "on"
    command = shlex.join(["python3", str(SCRIPT), "ensure"])
    for hook in HOOKS:
        if enabled:
            tmux("set-hook", "-g", f"{hook}[1900]", f"run-shell -b {shlex.quote(command)}")
        else:
            tmux("set-hook", "-gu", f"{hook}[1900]")
    if enabled:
        tmux("set-hook", "-g", "after-select-pane[1900]",
             "if-shell -F '#{==:#{pane_id},#{@agent_usage_pane}}' 'select-pane -l'")
    else:
        tmux("set-hook", "-gu", "after-select-pane[1900]")
    ensure()


def ensure() -> None:
    with (cache_directory() / "top-layout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        enabled = tmux("show-option", "-gqv", "@agent_usage_top") == "on"
        windows = tmux("list-panes", "-a", "-F",
                       "#{window_id} #{pane_id} #{pane_top} #{pane_width} #{pane_height} "
                       "#{?@agent_usage_header,1,0} #{pane_active}").splitlines()
        grouped = {}
        for line in windows:
            window, pane, top, width, height, header, active = line.split()
            grouped.setdefault(window, {})[pane] = (
                int(top), int(width), int(height), header == "1", active == "1")
        for window, panes in grouped.items():
            headers = [pane for pane, values in panes.items() if values[3]]
            work = [pane for pane in panes if pane not in headers]
            if not enabled or not work:
                for pane in headers:
                    tmux("kill-pane", "-t", pane)
                if work:
                    tmux("set-option", "-wu", "-t", window, "@agent_usage_pane")
                continue
            width_and_zoom = tmux("display-message", "-p", "-t", window,
                                  "#{window_width} #{window_zoomed_flag}").split()
            window_width = int(width_and_zoom[0])
            if width_and_zoom[1] == "1":
                continue
            active = next((pane for pane in work if panes[pane][4]), work[0])
            if not headers:
                pane = tmux(
                    "split-window", "-bdfv", "-l", "1", "-t", active, "-P", "-F", "#{pane_id}",
                    shlex.join(["python3", str(SCRIPT), "render"]))
                tmux("set-option", "-p", "-t", pane, "@agent_usage_header", "on")
                tmux("set-option", "-w", "-t", window, "@agent_usage_pane", pane)
                tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
                tmux("select-pane", "-d", "-t", pane)
                tmux("select-pane", "-t", active)
            else:
                pane = headers[0]
                top, width, height = panes[pane][:3]
                if top != 0 or width < window_width:
                    tmux("move-pane", "-bdfv", "-l", "1", "-s", pane, "-t", active)
                elif height != 1:
                    tmux("resize-pane", "-t", pane, "-y", "1")


def ansi_usage(text: str) -> str:
    def replace(match):
        codes = []
        for field in match.group(1).split(","):
            if field == "default":
                codes.extend(("0", "40", "37"))
            elif field == "bold":
                codes.append("1")
            elif re.fullmatch(r"fg=colour\d+", field):
                codes.extend(("38", "5", field[9:]))
        return f"\033[{';'.join(codes)}m" if codes else ""
    return re.sub(r"#\[([^]]*)\]", replace, text)


def compact_line(sections: dict[str, str], width: int) -> str:
    values = [sections.get(provider, f"{NAMES[provider]} --") for provider in PROVIDERS]
    line = "  |  ".join(values)
    if len(ANSI.sub("", line)) > width:
        line = " | ".join(values)
    if len(ANSI.sub("", line)) > width:
        # Keep all providers visible in narrow terminals; resets stay in the popup.
        values = [re.sub(r"\s*\([^)]*\)", "", value) for value in values]
        line = " | ".join(values)
    if len(ANSI.sub("", line)) > width:
        line = "  ".join(values)
    return line


def render_provider(provider: str, nonce: str) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT.parent / "agent_usage.sh"), provider, "", nonce, "compact"],
        capture_output=True, text=True, timeout=45, check=True)
    return ansi_usage(result.stdout)


def render() -> None:
    sections = {}
    previous = None
    refreshed = 0

    def draw():
        nonlocal previous
        width = os.get_terminal_size().columns
        line = compact_line(sections, width)
        if line != previous:
            sys.stdout.write("\033[0m\033[40m\r\033[2K" + line + "\033[0m")
            sys.stdout.flush()
            previous = line

    def interrupted(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as executor:
        while True:
            active = tmux("display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{window_active}")
            if active == "1":
                draw()
                if time.monotonic() - refreshed >= 20:
                    nonce = tmux("show-option", "-gqv", "@agent_usage_nonce") or "0"
                    futures = {executor.submit(render_provider, provider, nonce): provider for provider in PROVIDERS}
                    for future in as_completed(futures):
                        provider = futures[future]
                        try:
                            sections[provider] = future.result()
                        except (OSError, subprocess.SubprocessError) as exc:
                            sections[provider] = f"{NAMES[provider]} \033[90m--\033[0m"
                            tmux("display-message", f"Usage renderer failed for {provider}: {type(exc).__name__}")
                        draw()
                    refreshed = time.monotonic()
            time.sleep(1)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "configure"
    if action == "configure":
        configure()
    elif action == "ensure":
        ensure()
    elif action == "render":
        render()
    else:
        raise SystemExit(f"unknown top-pane action: {action}")
