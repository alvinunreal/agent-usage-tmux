#!/usr/bin/env bash
# Usage: agent_usage.sh <provider> [window] [nonce] [full|compact]
#        agent_usage.sh both [claude-window] [codex-window]
# Defaults to showing both bars when no argument is given.
#
# The second argument picks the rate-limit window; without it the tmux option
# @agent_usage_window_<agent> decides (see usage_menu.sh, which flips it on a
# click). Each bar is wrapped in a '#[range=user|au-<agent>]' so tmux can tell
# the key bindings which bar was clicked.
#
# Trailing arguments beyond the documented ones are ignored on purpose: the
# status line appends a nonce there, and a changed command string is what makes
# tmux start a fresh #() job instead of reusing the cached output.

AGENT="${1:-}"
WINDOW="${2:-}"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

icon_for() {
    case "$1" in
        claude) printf '✻' ;;
        codex) printf '>_' ;;
        copilot) printf 'GH' ;;
        opencodego) printf 'Go' ;;
        antigravity) printf 'AG' ;;
        grok) printf 'Grok' ;;
    esac
}

label_for() {
    case "$1" in
        claude) printf Claude ;;
        codex) printf Codex ;;
        copilot) printf Copilot ;;
        opencodego) printf OpenCode ;;
        antigravity) printf Gemini ;;
        grok) printf Grok ;;
    esac
}

# 5h unless the argument or the tmux option says otherwise.
get_window() {
    local agent="$1"
    local window="$2"

    if [[ -z "$window" ]]; then
        window="$(tmux show-option -gqv "@agent_usage_window_${agent}" 2>/dev/null)"
    fi

    [[ "$agent" == "copilot" ]] && { echo "monthly"; return; }
    [[ "$agent" == "grok" ]] && { echo "credits"; return; }
    case "$window" in
        weekly|week|7d|secondary) echo "weekly" ;;
        monthly) [[ "$agent" == "opencodego" ]] && echo "monthly" || echo "5h" ;;
        *)                        echo "5h" ;;
    esac
}

show_icons() {
    local value
    value="$(tmux show-option -gqv @agent_usage_show_icons 2>/dev/null)"
    if [[ -z "$value" ]]; then
        value="$(tmux show-option -gqv @agent_usage_disable_icons 2>/dev/null)"
        case "$value" in
            1|yes|true|on) return 1 ;;
        esac
        return 0
    fi

    case "$value" in
        0|no|false|off) return 1 ;;
    esac
    return 0
}

custom_command() {
    local agent="$1"
    local kind="$2"
    local cmd
    cmd="$(tmux show-option -gqv "@agent_usage_${kind}_${agent}" 2>/dev/null)"
    if [[ -z "$cmd" ]]; then
        [[ "$agent" == "claude" ]] && cmd="$(tmux show-option -gqv "@agent_usage_${kind}" 2>/dev/null)"
    fi
    printf '%s' "$cmd"
}

# HH:MM as before; the weekly window resets days away, where '3d04h' beats
# a three-digit hour count.
format_reset() {
    local reset_in="$1"
    local days hours minutes

    [[ "$reset_in" =~ ^[0-9]+$ ]] || { printf -- '--:--'; return; }
    days=$(( reset_in / 86400 ))
    hours=$(( (reset_in % 86400) / 3600 ))
    minutes=$(( (reset_in % 3600) / 60 ))
    if (( days > 0 )); then
        printf "%dd%02dh" "$days" "$hours"
    else
        printf "%02d:%02d" "$hours" "$minutes"
    fi
}

format_compact_reset() {
    local seconds="$1"
    [[ "$seconds" =~ ^[0-9]+$ ]] || { printf -- '--'; return; }
    local days=$(( seconds / 86400 ))
    local hours=$(( seconds % 86400 / 3600 ))
    local minutes=$(( seconds % 3600 / 60 ))
    if (( seconds == 0 )); then printf now
    elif (( days > 0 )); then printf '%dd%dh' "$days" "$hours"
    elif (( hours > 0 && minutes > 0 )); then printf '%dh%dm' "$hours" "$minutes"
    elif (( hours > 0 )); then printf '%dh' "$hours"
    elif (( minutes > 0 )); then printf '%dm' "$minutes"
    else printf '<1m'
    fi
}

render_bar() {
    local agent="$1"
    local pct="$2"
    local reset_label="$3"
    local window="${4:-5h}"
    local mode="${5:-full}"
    local width=6
    local empty_bg="colour236"
    local sub_chars=('▏' '▎' '▍' '▌' '▋' '▊' '▉')

    (( pct < 0 )) && pct=0
    (( pct > 100 )) && pct=100

    if [[ "$mode" == compact ]]; then
        local quota_color=colour117
        (( pct <= 50 )) && quota_color=colour214
        (( pct <= 20 )) && quota_color=colour203
        printf '#[fg=colour252]%s #[fg=%s,bold]%d%%#[default]' "$(label_for "$agent")" "$quota_color" "$pct"
        [[ "$reset_label" == "--" ]] || printf ' #[fg=colour244](%s)#[default]' "$reset_label"
        return
    fi

    read -r full partial_idx <<< "$(awk -v p="$pct" -v w="$width" 'BEGIN {
        filled = p * w / 100
        full   = int(filled)
        idx    = int((filled - full) * 8)
        print full, idx
    }')"

    local color
    case "$agent" in
        claude) color="colour214" ;;  # orange
        codex)  color="colour250" ;;  # soft light gray
        copilot) color="colour117" ;;
        opencodego) color="colour114" ;;
        antigravity) color="colour183" ;;
        grok) color="colour255" ;;
        *)
            if   (( pct >= 80 )); then color="colour160"
            elif (( pct >= 50 )); then color="colour214"
            else                       color="colour71"
            fi ;;
    esac

    local bar_on=""
    for (( i=0; i<full; i++ )); do bar_on+="█"; done

    local partial="" empty
    if (( partial_idx > 0 )); then
        partial="${sub_chars[$(( partial_idx - 1 ))]}"
        empty=$(( width - full - 1 ))
    else
        empty=$(( width - full ))
    fi

    local bar_off=""
    for (( i=0; i<empty; i++ )); do bar_off+=" "; done

    local icon_prefix=""
    if show_icons; then
        icon_prefix="$(icon_for "$agent") "
    fi

    # 'w' in front of the percentage: the bar is showing the weekly window.
    local window_tag=""
    [[ "$window" == "weekly" ]] && window_tag="w"
    [[ "$window" == "monthly" ]] && window_tag="m"

    printf "#[fg=%s,bold]%s%s%d%%#[default] #[fg=%s,bg=%s]%s%s#[fg=%s,bg=%s]%s#[default] #[fg=%s,bold]%s#[default]" \
        "$color" "$icon_prefix" "$window_tag" "$pct" "$color" "$empty_bg" "$bar_on" "$partial" "$empty_bg" "$empty_bg" "$bar_off" "$color" "$reset_label"
}

render_agent() {
    local agent="$1"
    local window
    local pct
    local reset_in
    local reset_label
    local data pct_cmd reset_cmd actual_window failed=0
    local nonce="${3:-0}"
    local mode="${4:-full}"
    window=$(get_window "$agent" "${2:-}")
    pct_cmd="$(custom_command "$agent" cmd)"
    reset_cmd="$(custom_command "$agent" reset_cmd)"
    # One cached snapshot provides percentage and reset, for every built-in window.
    if [[ -z "$pct_cmd" || -z "$reset_cmd" ]]; then
        if data="$(python3 "$SCRIPT_DIR/usage_data.py" "$agent" --window "$window" --nonce "$nonce")"; then
            IFS=$'\t' read -r pct reset_in actual_window <<< "$data"
            [[ -n "$actual_window" ]] && window="$actual_window"
        else
            failed=1
        fi
    fi
    if [[ -n "$pct_cmd" ]]; then
        if pct="$(bash -c "$pct_cmd")"; then
            [[ -n "$reset_cmd" ]] && failed=0
        else
            failed=1
        fi
    fi
    if [[ -n "$reset_cmd" ]]; then
        if ! reset_in="$(bash -c "$reset_cmd")"; then failed=1; fi
    fi

    # Mouse range around the whole bar: a click in here reaches the bindings
    # in agent-usage.tmux with #{mouse_status_range} = 'au-<agent>'. Only
    # '#[norange]' ends it - the '#[default]' inside just resets the colours.
    # (No colon in the name: tmux's #{s/…/…/:…} splits its arguments on it.)
    printf "#[range=user|au-%s]" "$agent"
    if (( failed )); then
        if [[ "$mode" == compact ]]; then
            printf '#[fg=colour252]%s #[fg=colour244]--#[default]' "$(label_for "$agent")"
        else
            printf '#[fg=colour160]%s N/A#[default]' "$(icon_for "$agent")"
        fi
    elif [[ "$pct" == "status" ]]; then
        if [[ "$mode" == compact ]]; then
            printf '#[fg=colour252]%s #[fg=colour244]--#[default]' "$(label_for "$agent")"
        else
            printf '#[fg=colour117]%s %s#[default]' "$(icon_for "$agent")" "$reset_in"
        fi
    elif [[ "$pct" =~ ^[0-9]+$ && ${#pct} -le 3 && "$pct" -le 100 ]] &&
         [[ "$reset_in" == "-" || "$reset_in" =~ ^[0-9]+$ ]]; then
        if [[ "$mode" == compact ]]; then
            reset_label=$(format_compact_reset "$reset_in")
        else
            reset_label=$(format_reset "$reset_in")
        fi
        render_bar "$agent" "$((10#$pct))" "$reset_label" "$window" "$mode"
    else
        printf 'invalid usage output for %s\n' "$agent" >&2
        printf '#[fg=colour160]%s N/A#[default]' "$(icon_for "$agent")"
    fi
    printf "#[norange]"
}

case "$AGENT" in
    claude|codex|copilot|opencodego|antigravity|grok) render_agent "$AGENT" "$WINDOW" "${3:-0}" "${4:-full}" ;;
    all)
        nonce="${6:-0}"
        render_agent claude "$WINDOW" "$nonce"
        printf " | "
        render_agent codex "${3:-}" "$nonce"
        printf " | "
        render_agent copilot monthly "$nonce"
        printf " | "
        render_agent opencodego "${5:-}" "$nonce"
        printf " | "
        render_agent antigravity "" "$nonce"
        printf " | "
        render_agent grok credits "$nonce"
        ;;
    ""|both)
        # Both bars: second argument is claude's window, third codex's.
        render_agent "claude" "$WINDOW" "${4:-0}"
        printf " "
        render_agent "codex" "${3:-}" "${4:-0}"
        ;;
    *) printf 'unknown provider: %s\n' "$AGENT" >&2; exit 1 ;;
esac
