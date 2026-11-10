# agent-usage-tmux

Tmux status bar plugin for Claude, Codex, GitHub Copilot, OpenCode Go, Antigravity, and Grok account quotas.

## This fork

- The fork lives at `alvinunreal/agent-usage-tmux`, with `artischocki/agent-usage-tmux` as upstream.
- `references/CodexBar` is a pinned, read-only reference submodule for provider APIs and authentication. It is not a runtime dependency.
- The plugin needs tmux 3.4+, Python 3.10+, Bash 3.2+, and `less` for the dashboard. No Python packages are needed.
- The built-in fetchers only read usage. Claude polling does not send prompts or consume model tokens.
- All widgets show **remaining** allowance. The minimal strip uses `--` for unavailable/unknown usage; traditional progress bars use `N/A` for errors. The dashboard shows the reason.

## Local installation

```bash
git clone --recurse-submodules https://github.com/alvinunreal/agent-usage-tmux.git ~/repos/tmux-agent-usage
```

Add these lines to `~/.tmux.conf`:

```tmux
set -g status-right ""
set -g status-position bottom
set -g @agent_usage_top on
set -g @agent_usage_window_claude weekly
set -g @agent_usage_window_codex weekly
set -g @agent_usage_window_opencodego weekly
set -g @agent_usage_window_antigravity weekly
run-shell ~/repos/tmux-agent-usage/agent-usage.tmux
bind u display-popup -w 90% -h 80% -T " Agent usage " -s "bg=black,fg=colour252" -S "bg=black,fg=colour39" -E 'bash "$HOME/repos/tmux-agent-usage/scripts/usage_popup.sh"'
```

- Reload with `tmux source-file ~/.tmux.conf` from a running tmux server.
- Press your prefix, then **u**, to open the dashboard. Scroll with arrows or Page Up/Down, and press **q** to close.
- `@agent_usage_top on` adds a one-line, non-interactive usage pane at the top of each window. Your normal status bar stays at the bottom, unchanged. The pane takes one content line plus its separator.
- The strip uses consistent names: Claude, Codex, Copilot, OpenCode, Gemini, and Grok. Each provider shows its reported reset countdown in parentheses. Wider terminals show all details; narrow terminals omit countdowns to keep provider names visible.
- The configuration above selects weekly remaining quotas for Claude, Codex, OpenCode, and Gemini. Copilot retains its monthly allowance; Grok uses its reported credit period. The popup still shows every supported window.
- The black popup uses aligned window, remaining-percentage, progress-bar, and reset columns. It includes all supported windows, credit details, and actionable errors.
- New windows get the usage pane automatically. Reloads do not duplicate it; layout changes and resizes keep it at the top. Zooming a work pane temporarily hides it.
- Set `@agent_usage_top off` and reload the plugin to remove usage panes and their lifecycle hooks. Normal work panes and status options are left untouched.
- Individual placeholders remain available for traditional status-left/right layouts: `#{claude_usage}`, `#{codex_usage}`, `#{copilot_usage}`, `#{opencodego_usage}`, `#{antigravity_usage}`, and `#{grok_usage}`.
- `#{agent_usage}` retains the original Claude/Codex pair. `#{agent_usage_all}` displays all six.
- The dashboard and bars share one private cache per provider in `${XDG_CACHE_HOME:-~/.cache}/agent-usage-tmux`. Successful snapshots last 120 seconds; failures last 30 seconds. Credentials are never cached.
- Traditional status widgets support right-click **Refresh now** to bypass cached snapshots. The top strip is non-interactive; changing `@agent_usage_nonce` forces a fresh snapshot on its next refresh.
- The plugin preserves your `status-interval`, existing bindings outside usage bars, and mouse preference. Top panes refresh only in active windows and do not receive keyboard input.

## Authentication and windows

| Provider | Credentials | Windows |
|---|---|---|
| Claude | Existing `~/.claude/.credentials.json` OAuth login. | Five-hour and weekly. |
| Codex | Existing `~/.codex/auth.json` access token and account ID. | Windows reported by the account; a single weekly window is labeled weekly, not five-hour. |
| Copilot | `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token --hostname github.com`. | Monthly premium interactions / AI credits. |
| OpenCode Go | `OPENCODE_GO_API_KEY`, then the active OpenCode CLI credential. | Five-hour, weekly, and monthly when reported. |
| Antigravity | Existing `agy` CLI login, with `agy` 1.1.11+. `ANTIGRAVITY_CLI_PATH` can select an executable. | Measured five-hour and weekly model-group quotas. |
| Grok | `GROK_OAUTH_TOKEN`, then `${GROK_HOME:-~/.grok}/auth.json` from `grok login`. | Reported credit allowance and prepaid wallet. |

- Copilot uses GitHub's internal usage API. A token must have Copilot access; a generic PAT may be rejected. Run `gh auth login` with the intended GitHub account or supply a suitable OAuth token.
- Copilot shows credit usage without inventing a percentage when the API supplies no metered allowance. Unlimited plans are labeled explicitly.
- OpenCode Go reads the CLI's v2 `credential` SQLite table in read-only mode. Active `opencode-go` credentials take precedence over `opencode`; ambiguous or expired logins report an error.
- Legacy OpenCode Go API credentials in `auth.json` are used only when there is no v2 credential table. A v2 logout never resurrects stale legacy credentials.
- `XDG_DATA_HOME` and `OPENCODE_DB` follow the OpenCode CLI path conventions. Tokens are reread on each uncached fetch; this plugin does not renew or modify them.
- Use `@agent_usage_window_copilot monthly` and `@agent_usage_window_opencodego 5h|weekly|monthly` to select the new provider windows. A monthly bar has an `m` prefix; a weekly bar has `w`.
- Clicking Copilot keeps its monthly view. Clicking OpenCode Go cycles five-hour, weekly, and monthly; the context menu offers each supported window.
- Antigravity runs the structured `agy -p /usage --output-format json` report, never a model prompt. Versions older than 1.1.11 are rejected before invoking `/usage`.
- The Antigravity probe disables auto-updates and browser launchers, uses a private working directory, caps output at 1 MiB, and bounds the report to 35 seconds. Its owned process group is stopped on completion or failure.
- Antigravity shows **Gemini models only**, in both the status row and popup. The status shows the lowest remaining allowance across measured Gemini groups for the selected window, not an average. Starter accounts with only weekly quotas do not gain a fabricated five-hour quota.
- Grok reads the subscriber credit API with the existing CLI OAuth token. It never uses an xAI management/API key, opens a login browser, or refreshes tokens. Missing/expired authentication reports `run grok login`.
- A Grok billing period without a percentage is labeled unknown, not 100% remaining. The popup shows a reported prepaid wallet separately. Team-principal usage is unsupported.
- Custom percentage/reset overrides also support the `copilot` and `opencodego` suffixes. They retain the original shell-command interface and must print numeric output.

![Plugin preview](docs/preview.png)

## Install

Add the plugin with TPM:

```tmux
set -g @plugin 'alvinunreal/agent-usage-tmux'
set -g status-right "#{claude_usage} | #{codex_usage} "
```

Then reload tmux or install plugins with TPM.

## Original Claude/Codex display

- Remaining usage percentage
- Time until reset
- Separate bars for Claude and Codex

Current format:

```text
✻ 42% ████▍      03:02
>_ 88% █████▎    00:44
```

## Clicking a bar

Every bar is a mouse range, so tmux can tell which one was hit. Needs
`set -g mouse on`.

- **Left click** flips that bar between the 5-hour window and the weekly one.
  While the weekly window is up, a `w` sits in front of the percentage and the
  reset is written as days:

  ```text
  ✻ 42% ████▍      03:02      5-hour window
  ✻ w80% ████▊     2d06h      weekly window
  ```

- **Right click** opens a context menu for that bar:

  | Entry | Key | Does |
  |-------|-----|------|
  | Detailed stats | `s` | scrollable popup with account windows, remaining quota, reset times, credits, and errors |
  | 5-hour window  | `5` | show that window in the status line |
  | Weekly window  | `w` | show that window in the status line |
  | Refresh now    | `r` | refetch instead of waiting for the next `status-interval` |

Clicks next to a bar keep doing whatever they did before: the bindings remember
the previous `MouseDown1Status` / `MouseDown3Status` command and fall back to
it, so the window list still works and other status-line plugins that bind the
same keys survive.

Which window a bar shows lives in `@agent_usage_window_claude` /
`@agent_usage_window_codex` (`5h` or `weekly`) and can be preset:

```tmux
set -g @agent_usage_window_claude weekly
```

Custom `@agent_usage_cmd_*` commands are called exactly as written - the window
flag only goes to the built-in fetchers.

## Configuration

By default, the plugin fetches usage from:

- `scripts/fetch_claude_usage.py`
- `scripts/fetch_codex_usage.py`

Optional tmux overrides:

```tmux
set -g @agent_usage_cmd_claude "python3 /path/to/custom_claude_percent.py"
set -g @agent_usage_cmd_codex "python3 /path/to/custom_codex_percent.py"

set -g @agent_usage_reset_cmd_claude "python3 /path/to/custom_claude_reset.py"
set -g @agent_usage_reset_cmd_codex "python3 /path/to/custom_codex_reset.py"
```

Hide the leading icons:

```tmux
set -g @agent_usage_show_icons off
```

That changes the display to:

```text
42% ████▍      03:02
88% █████▎    00:44
```

## Development

Useful local commands:

```bash
python3 -m unittest discover -s tests -v
shellcheck agent-usage.tmux scripts/agent_usage.sh scripts/usage_menu.sh scripts/usage_popup.sh
python3 scripts/usage_data.py copilot --json
python3 scripts/usage_data.py opencodego --json
python3 scripts/usage_data.py antigravity --json
python3 scripts/usage_data.py grok --json
python3 scripts/usage_stats.py all

python3 scripts/fetch_claude_usage.py
python3 scripts/fetch_claude_usage.py --field reset_in
python3 scripts/fetch_codex_usage.py
python3 scripts/fetch_codex_usage.py --field reset_in

python3 scripts/usage_stats.py claude    # the page behind "Detailed stats"
scripts/agent_usage.sh claude weekly     # one bar, weekly window
scripts/usage_menu.sh toggle claude      # what a left click does
```
