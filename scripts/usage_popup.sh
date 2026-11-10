#!/usr/bin/env bash

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
nonce="$(tmux show-option -gqv @agent_usage_nonce)"
exec python3 "$SCRIPT_DIR/usage_pager.py" "$SCRIPT_DIR/usage_stats.py" \
    "${1:-all}" --nonce "${nonce:-0}"
