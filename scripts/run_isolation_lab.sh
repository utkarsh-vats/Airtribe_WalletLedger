#!/usr/bin/env bash
# Runs scripts/isolation_lab.sql in two real, interactive psql sessions (A and B) inside a
# detached tmux session, and records both sessions to notes/lab-A.log and notes/lab-B.log.
#
#   scripts/run_isolation_lab.sh
#
# Each step of the lab (marked `\echo '== 1b (A) =='` in the .sql) is typed into the pane it
# names. After each step the script sends a sentinel `\echo '== end 1b (A) =='`; psql prints it
# only once everything before it has finished, so "the sentinel appeared" means "the prompt came
# back". Step 3b is the one expected lock wait: B must still be blocked a moment later, and must
# return once A's step 3c commits. Any other step that doesn't return within STEP_TIMEOUT seconds
# is a failure: both panes are dumped and the run stops.
#
# At the end, and on failure, it quits both psql sessions (\q) and waits for them to exit before
# removing the session, so no psql is left connected inside the container.
#
# Only ever creates and kills the tmux session named `isolab` (exact match); other tmux sessions
# are never touched. Needs the compose stack running (`docker compose -p walletledger up -d`).
set -euo pipefail

SESSION="isolab"
TARGET="=$SESSION"            # '=' = exact name match, never a prefix of another session
STEP_TIMEOUT="${STEP_TIMEOUT:-10}"
QUIT_TIMEOUT="${QUIT_TIMEOUT:-5}"   # seconds for both psql sessions to exit at the end
LOCK_WAIT_STEP="3b"           # sent to B, expected to block until A's next step commits
LOCK_RELEASE_STEP="3c"

cd "$(dirname "$0")/.."
LAB_SQL="scripts/isolation_lab.sql"
LOG_A="notes/lab-A.log"
LOG_B="notes/lab-B.log"
PSQL=(docker compose -p walletledger exec postgres psql -U wallet_owner -d walletledger)

PANE_A=""
PANE_B=""
WORK="$(mktemp -d)"
CAPTURED=0

log() { printf '[isolab] %s\n' "$*" >&2; }

capture_scrollback() {
  # Append the clean, joined scrollback of both panes (the pipe-pane logs above it are the raw
  # terminal stream, including readline's control sequences).
  [[ "$CAPTURED" == 1 ]] && return 0
  local pane logfile
  for pair in "A:$LOG_A" "B:$LOG_B"; do
    logfile="${pair#*:}"
    if [[ "${pair%%:*}" == A ]]; then pane="$PANE_A"; else pane="$PANE_B"; fi
    [[ -n "$pane" ]] || continue
    tmux pipe-pane -t "$pane" 2>/dev/null || true   # stop the raw stream first
    {
      printf '\n\n===== full scrollback (tmux capture-pane -J) =====\n'
      tmux capture-pane -p -J -S - -E - -t "$pane" 2>/dev/null || true
    } >>"$logfile"
  done
  CAPTURED=1
}

pane_alive() {  # pane id: still part of the isolab session?
  tmux list-panes -s -t "$TARGET" -F '#{pane_id}' 2>/dev/null | grep -Fxq -- "$1"
}

quit_psql() {
  # Killing the tmux session would only kill the `docker compose exec` client: the psql inside
  # the container would stay connected to Postgres. So quit psql first. C-c cancels a running
  # query or a half-typed line (a quit mid-transaction rolls it back), then \q exits; the
  # pane closes once docker compose exec has returned, i.e. once psql has really exited.
  local pane name deadline
  for pane in "$PANE_A" "$PANE_B"; do
    [[ -n "$pane" ]] && pane_alive "$pane" || continue
    tmux send-keys -t "$pane" C-c
    tmux send-keys -t "$pane" -l -- '\q'
    tmux send-keys -t "$pane" Enter
  done
  deadline=$((SECONDS + QUIT_TIMEOUT))
  for name in A B; do
    pane="$(pane_of "$name")"
    [[ -n "$pane" ]] || continue
    while pane_alive "$pane"; do
      if (( SECONDS >= deadline )); then
        log "WARNING: psql in pane $name did not exit within ${QUIT_TIMEOUT}s; killing the" \
          "session anyway, so a psql may be left connected (check pg_stat_activity)"
        return 0
      fi
      sleep 0.2
    done
  done
}

cleanup() {
  local status=$?
  if tmux has-session -t "$TARGET" 2>/dev/null; then
    capture_scrollback
    quit_psql
    # Normally both panes have exited by now and the session is already gone.
    if tmux has-session -t "$TARGET" 2>/dev/null; then
      tmux kill-session -t "$TARGET"
    fi
  fi
  rm -rf "$WORK"
  exit "$status"
}
trap cleanup EXIT INT TERM

dump_and_fail() {
  log "FAILED: $*"
  for name in A B; do
    local pane="$PANE_A"; [[ "$name" == B ]] && pane="$PANE_B"
    printf '\n----- pane %s -----\n' "$name" >&2
    tmux capture-pane -p -J -S - -E - -t "$pane" >&2 || true
  done
  exit 1
}

pane_of() { if [[ "$1" == A ]]; then echo "$PANE_A"; else echo "$PANE_B"; fi; }

pane_has_line() {  # pane, exact line
  tmux capture-pane -p -J -S - -E - -t "$1" | grep -Fxq -- "$2"
}

wait_for_line() {  # pane, exact line, timeout seconds
  local deadline=$((SECONDS + $3))
  while (( SECONDS < deadline )); do
    pane_has_line "$1" "$2" && return 0
    sleep 0.2
  done
  return 1
}

type_lines() {  # pane, file: type each line as if at the keyboard, then Enter
  # tmux's command parser treats a ';' at the end of an argument as a command separator and
  # drops it, so a trailing ';' is sent separately as its raw key code (0x3b).
  local pane="$1" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == *";" ]]; then
      tmux send-keys -t "$pane" -l -- "${line%;}"
      tmux send-keys -t "$pane" -H 3b
    else
      tmux send-keys -t "$pane" -l -- "$line"
    fi
    tmux send-keys -t "$pane" Enter
  done <"$2"
}

# --- preflight -------------------------------------------------------------------------------
command -v tmux >/dev/null || { log "tmux is not installed"; exit 1; }
if tmux has-session -t "$TARGET" 2>/dev/null; then
  log "a tmux session named '$SESSION' already exists; not touching it. Remove it first:"
  log "  tmux kill-session -t '=$SESSION'"
  trap - EXIT; rm -rf "$WORK"; exit 1
fi
docker compose -p walletledger exec -T postgres pg_isready -q -h 127.0.0.1 \
  || { log "postgres is not ready; start the stack first"; exit 1; }

# --- split the lab into steps ------------------------------------------------------------------
# One file per step, from its `== name (S) ==` marker up to the next marker. Comment-only and
# blank lines are dropped; everything before the SETUP marker is replaced by the per-pane setup.
python3 - "$LAB_SQL" "$WORK" <<'PY'
import pathlib, re, sys
src, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
marker = re.compile(r"^\\echo '== (\S+) \((A|B)\) =='$")
steps, current = [], None
for line in src.read_text().splitlines():
    m = marker.match(line)
    if m:
        current = [m.group(1), m.group(2), [line]]
        steps.append(current)
    elif current is not None and line.strip() and not line.lstrip().startswith("--"):
        current[2].append(line)
for i, (name, session, lines) in enumerate(steps):
    (work / f"{i:03d}_{name}_{session}.sql").write_text("\n".join(lines) + "\n")
print(f"{len(steps)} steps", file=sys.stderr)
PY

# --- start both sessions -------------------------------------------------------------------------
: >"$LOG_A"
: >"$LOG_B"
PANE_A="$(tmux new-session -d -s "$SESSION" -c "$PWD" -x 200 -y 50 -P -F '#{pane_id}' "${PSQL[*]}")"
PANE_B="$(tmux split-window -t "$PANE_A" -c "$PWD" -h -P -F '#{pane_id}' "${PSQL[*]}")"
tmux pipe-pane -t "$PANE_A" -o "cat >> '$LOG_A'"
tmux pipe-pane -t "$PANE_B" -o "cat >> '$LOG_B'"
log "tmux session '$SESSION' started (A=$PANE_A, B=$PANE_B)"

for name in A B; do
  pane="$(pane_of "$name")"
  # psql's own startup prompt: 'walletledger=>' (wallet_owner is not a superuser).
  deadline=$((SECONDS + 20))
  until tmux capture-pane -p -t "$pane" | grep -Eq 'walletledger=[>#]'; do
    (( SECONDS < deadline )) || dump_and_fail "psql in pane $name did not start"
    sleep 0.2
  done
  printf '%s\n' \
    "\\set PROMPT1 '($name) %R%# '" \
    "\\set PROMPT2 '($name) %R%# '" \
    "\\pset pager off" \
    "\\echo '== ready ($name) =='" >"$WORK/init_$name.sql"
  type_lines "$pane" "$WORK/init_$name.sql"
  wait_for_line "$pane" "== ready ($name) ==" "$STEP_TIMEOUT" \
    || dump_and_fail "session $name did not accept its prompt settings"
done

# --- run the steps -------------------------------------------------------------------------------
blocked=""   # sentinel of the step that is waiting on a lock, if any
for file in "$WORK"/[0-9][0-9][0-9]_*.sql; do
  base="$(basename "$file" .sql)"
  rest="${base#*_}"
  name="${rest%_*}"
  session="${rest##*_}"
  pane="$(pane_of "$session")"
  sentinel="== end $name ($session) =="

  log "step $name -> $session"
  printf "\\\\echo '%s'\n" "$sentinel" >>"$file"
  type_lines "$pane" "$file"

  if [[ "$name" == "$LOCK_WAIT_STEP" ]]; then
    sleep 2
    pane_has_line "$pane" "$sentinel" \
      && dump_and_fail "step $name returned at once; expected it to wait for A's lock"
    log "step $name is blocked on the row lock, as expected; continuing with A"
    blocked="$sentinel"
    continue
  fi

  wait_for_line "$pane" "$sentinel" "$STEP_TIMEOUT" \
    || dump_and_fail "step $name ($session) did not return within ${STEP_TIMEOUT}s"

  if [[ "$name" == "$LOCK_RELEASE_STEP" && -n "$blocked" ]]; then
    wait_for_line "$PANE_B" "$blocked" "$STEP_TIMEOUT" \
      || dump_and_fail "B did not return after A's commit in step $name"
    log "B returned after A's commit"
    blocked=""
  fi
done

[[ -z "$blocked" ]] || dump_and_fail "a blocked step never returned: $blocked"

capture_scrollback
log "done; logs: $LOG_A, $LOG_B"
