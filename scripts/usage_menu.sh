#!/usr/bin/env bash
# Click actions for the usage bars in the tmux status line (see agent-usage.tmux).
#
#     usage_menu.sh toggle <agent>            5h window <-> weekly window
#     usage_menu.sh window <agent> <5h|weekly>
#     usage_menu.sh stats  <agent>            detailed page, meant for a popup
#     usage_menu.sh refresh                   drop the cached bar and refetch
#
# <agent> may be given as the raw range name tmux reports in
# #{mouse_status_range} ('au-claude', 'au-codex') - the prefix is stripped here
# so the key bindings can simply forward it.

set -u

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

ACTION="${1:-stats}"
AGENT="${2:-claude}"
AGENT="${AGENT##*[-:]}"

case "$AGENT" in
    claude|codex|copilot|opencodego|antigravity|grok) ;;
    *) printf 'unknown provider: %s\n' "$AGENT" >&2; exit 1 ;;
esac

current_window() {
    [[ "$AGENT" == "copilot" ]] && { echo monthly; return; }
    [[ "$AGENT" == "grok" ]] && { echo credits; return; }
    case "$(tmux show-option -gqv "@agent_usage_window_${AGENT}" 2>/dev/null)" in
        weekly|week|7d|secondary) echo "weekly" ;;
        monthly) echo "monthly" ;;
        *) echo "5h" ;;
    esac
}

# The bar runs as a #() job and tmux caches such a job by its command string.
# agent-usage.tmux puts the window option into that string, so setting the
# option here gives the status line a *new* job that runs at once - otherwise
# the old window would stay up until the next status-interval tick.
set_window() {
    tmux set-option -g "@agent_usage_window_${AGENT}" "$1"
    tmux refresh-client -S
}

wait_for_key() {
    printf '\n\033[2m[press any key]\033[0m'
    { read -rsn1 </dev/tty; } 2>/dev/null
    printf '\n'
}

case "$ACTION" in
    toggle)
        case "$AGENT:$(current_window)" in
            copilot:*) set_window monthly ;;
            grok:*) set_window credits ;;
            opencodego:weekly) set_window monthly ;;
            *:5h) set_window weekly ;;
            *) set_window 5h ;;
        esac
        ;;
    window)
        case "$AGENT:${3:-5h}" in
            copilot:monthly|opencodego:monthly|claude:5h|claude:weekly|codex:5h|codex:weekly|opencodego:5h|opencodego:weekly|antigravity:5h|antigravity:weekly|grok:credits)
                set_window "${3:-5h}" ;;
            *) echo "unsupported provider window" >&2; exit 1 ;;
        esac
        ;;
    menu)
        entries=("Detailed stats" s "display-popup -w 90% -h 80% -s 'bg=black,fg=colour252' -S 'bg=black,fg=colour39' -E 'bash \"$SCRIPT_DIR/usage_popup.sh\" $AGENT'" "")
        if [[ "$AGENT" != copilot && "$AGENT" != grok ]]; then
            entries+=("5-hour window" 5 "run-shell -b '\"$SCRIPT_DIR/usage_menu.sh\" window $AGENT 5h'")
            entries+=("Weekly window" w "run-shell -b '\"$SCRIPT_DIR/usage_menu.sh\" window $AGENT weekly'")
        fi
        if [[ "$AGENT" == copilot || "$AGENT" == opencodego ]]; then
            entries+=("Monthly window" m "run-shell -b '\"$SCRIPT_DIR/usage_menu.sh\" window $AGENT monthly'")
        fi
        entries+=("Refresh now" r "run-shell -b '\"$SCRIPT_DIR/usage_menu.sh\" refresh'")
        tmux display-menu -O -T "$AGENT usage" -x M -y S "${entries[@]}"
        ;;
    refresh)
        # A new command string restarts jobs and invalidates their shared snapshots.
        tmux set-option -g @agent_usage_nonce "$(date +%s)"
        tmux refresh-client -S
        ;;
    stats)
        python3 "$SCRIPT_DIR/usage_stats.py" "$AGENT" --nonce "$(tmux show-option -gqv @agent_usage_nonce)"
        wait_for_key
        ;;
    *)
        echo "unknown action: $ACTION" >&2
        echo "expected: toggle, window, menu, stats, refresh" >&2
        exit 1
        ;;
esac
