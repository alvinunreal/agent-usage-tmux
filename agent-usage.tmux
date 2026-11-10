#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

count_usage_width() {
    local string="$1"
    local total=0
    local token
    local tail

    for token in "#{agent_usage}" "#{agent_usage_all}" "#{claude_usage}" "#{codex_usage}" "#{copilot_usage}" "#{opencodego_usage}" "#{antigravity_usage}" "#{grok_usage}"; do
        tail="$string"
        while [[ "$tail" == *"$token"* ]]; do
            # one column more than the bar needs, for the 'w' of the weekly window
            case "$token" in
                "#{agent_usage}") total=$(( total + 41 )) ;;
                "#{agent_usage_all}") total=$(( total + 140 )) ;;
                *) total=$(( total + 21 )) ;;
            esac
            tail="${tail#*"$token"}"
        done
    done

    echo "$total"
}

# The window options travel in the job's command string on purpose: tmux keys
# its #() jobs by that string, so flipping a bar to weekly starts a new job
# immediately instead of leaving the old window up until the next tick.
do_interpolation() {
    local string="$1"
    local mode="${2:-full}"
    local claude_window="#{@agent_usage_window_claude}"
    local codex_window="#{@agent_usage_window_codex}"
    local copilot_window="#{@agent_usage_window_copilot}"
    local opencodego_window="#{@agent_usage_window_opencodego}"
    local antigravity_window="#{@agent_usage_window_antigravity}"
    local nonce="#{@agent_usage_nonce}"   # bumped by the menu's 'Refresh now'
    # individual bars
    string="${string//\#\{claude_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" claude $claude_window $nonce $mode)}"
    string="${string//\#\{codex_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" codex $codex_window $nonce $mode)}"
    string="${string//\#\{copilot_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" copilot $copilot_window $nonce $mode)}"
    string="${string//\#\{opencodego_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" opencodego $opencodego_window $nonce $mode)}"
    string="${string//\#\{antigravity_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" antigravity $antigravity_window $nonce $mode)}"
    string="${string//\#\{grok_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" grok credits $nonce $mode)}"
    # combined (backwards compat)
    string="${string//\#\{agent_usage\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" both $claude_window $codex_window $nonce)}"
    string="${string//\#\{agent_usage_all\}/#(\"$CURRENT_DIR/scripts/agent_usage.sh\" all $claude_window $codex_window $copilot_window $opencodego_window $nonce)}"
    echo "$string"
}

ensure_status_length() {
    local option="$1"
    local original_value="$2"
    local extra_width
    local length_option="${option}-length"
    local current_length
    local min_length

    extra_width="$(count_usage_width "$original_value")"
    (( extra_width > 0 )) || return

    current_length="$(tmux show-option -gqv "$length_option" 2>/dev/null)"
    [[ "$current_length" =~ ^[0-9]+$ ]] || current_length=0

    # Leave room for the existing clock/text to the right of the usage bars.
    min_length=$(( extra_width + 16 ))
    (( current_length >= min_length )) && return

    tmux set-option -gq "$length_option" "$min_length"
}

update_tmux_option() {
    local option="$1"
    local value
    value="$(tmux show-option -gqv "$option")"
    local new_value
    new_value="$(do_interpolation "$value")"
    ensure_status_length "$option" "$value"
    tmux set-option -gq "$option" "$new_value"
}

# Every placeholder above has to expand to something: an empty option would
# shift the remaining arguments of agent_usage.sh one to the left.
ensure_option() {
    local option="$1"
    local default="$2"
    [[ -n "$(tmux show-option -gqv "$option" 2>/dev/null)" ]] && return
    tmux set-option -gq "$option" "$default"
}

# --- mouse -----------------------------------------------------------------
#
# agent_usage.sh wraps each bar in '#[range=user|au-<agent>]'. A click inside
# such a range triggers the 'Status' mouse key and reports the range name in
# #{mouse_status_range}, so one binding can serve both bars and hand the agent
# on to usage_menu.sh. Clicks anywhere else must keep doing what they did
# before, hence the fall-back branch.

ON_BAR='#{m:au-*,#{mouse_status_range}}'

# What the key did before we touched it, remembered in an option so that
# re-sourcing the config does not nest our own binding inside itself.
saved_binding() {
    local key="$1"
    local option="$2"
    local fallback="$3"
    local saved current

    saved="$(tmux show-option -gqv "$option" 2>/dev/null)"
    if [[ -z "$saved" ]]; then
        current="$(tmux list-keys -T root "$key" 2>/dev/null | head -1 |
                   sed -E "s/^bind-key +-T +root +${key} +//")"
        [[ "$current" == *usage_menu.sh* ]] && current=""
        saved="${current:-$fallback}"
        tmux set-option -gq "$option" "$saved"
    fi
    printf '%s' "$saved"
}

bind_mouse() {
    local menu="$CURRENT_DIR/scripts/usage_menu.sh"
    local left right

    left="$(saved_binding MouseDown1Status @agent_usage_saved_mouse1 'select-window -t =')"
    right="$(saved_binding MouseDown3Status @agent_usage_saved_mouse3 'select-window -t =')"

    # -O keeps the menu up when the mouse button is released again; without it
    # the click that opens it would close it right away.
    tmux bind-key -n MouseDown1Status if-shell -F "$ON_BAR" \
        "run-shell -b '\"$menu\" toggle #{mouse_status_range}'" "$left"

    tmux bind-key -n MouseDown3Status if-shell -F "$ON_BAR" \
        "run-shell '\"$menu\" menu #{mouse_status_range}'" "$right"
}

main() {
    ensure_option @agent_usage_window_claude "5h"
    ensure_option @agent_usage_window_codex "5h"
    ensure_option @agent_usage_window_copilot "monthly"
    ensure_option @agent_usage_window_opencodego "5h"
    ensure_option @agent_usage_window_antigravity "5h"
    ensure_option @agent_usage_nonce "0"
    update_tmux_option "status-right"
    update_tmux_option "status-left"
    python3 "$CURRENT_DIR/scripts/usage_top.py" configure || return
    bind_mouse
}

main
