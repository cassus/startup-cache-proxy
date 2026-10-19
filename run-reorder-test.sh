#!/bin/zsh
# Interactive Haiku sessions through the proxy, other mods off.
# Usage: [MOD=1] run-reorder-test.sh <name> <max wait seconds before typing> <first message> [port, default 8081 = dev-proxy]
# MOD=1 loads mod/stable-mcp-listing from this repo. Sessions start in $TEST_DIR
# (default: the current directory), so its CLAUDE.md and settings apply.
# Waits for the prompt and for each answer instead of fixed sleeps.
mod=${MOD:+--plugin-dir ${0:A:h}/mod/stable-mcp-listing}
cd ${TEST_DIR:-$PWD}
tmux kill-session -t $1 2>/dev/null
tmux new-session -d -s $1 -x 200 -y 50 "claude --model haiku $mod --settings '{\"env\":{\"CLAUDE_CODE_PLUGIN_DIRS\":\"\",\"ENABLE_TOOL_SEARCH\":\"true\",\"ANTHROPIC_BASE_URL\":\"http://localhost:${4:-8081}\"}}'"

# Wait until <pattern> has appeared <count> times in the pane, at most <seconds>.
wait_for() {
  local i
  for i in {1..$(( $3 * 5 ))}; do
    (( $(tmux capture-pane -p -t $1 | grep -c -- "$2") >= ${4:-1} )) && return 0
    sleep 0.2
  done
  echo "timeout waiting for '$2' x${4:-1}" >&2
}
ask() {
  tmux send-keys -t $1 -l "$2"
  sleep 0.3
  tmux send-keys -t $1 Enter
}

wait_for $1 '^❯' $2
sleep ${DELAY:-0}  # DELAY=<s>: let the MCP servers connect before typing
ask $1 "$3"
wait_for $1 '^⏺' 30 1
# TURNS=<n> typed follow-ups (default 1), each doubling the last answer.
for n in {2..$(( ${TURNS:-1} + 1 ))}; do
  sleep ${GAP:-0}  # GAP=<s>: a fresh cache entry takes a few seconds to become readable
  ask $1 "And times two? One word."
  wait_for $1 '^⏺' 30 $n
done
sleep 0.5
tmux capture-pane -p -t $1 | grep -v '^\s*$' | grep -E '^[❯⏺]' | tail -$(( 2 * ${TURNS:-1} + 4 ))
tmux kill-session -t $1
