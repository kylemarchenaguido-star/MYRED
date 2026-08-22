#!/usr/bin/env bash
# test_evict_tick.sh — verify incremental (tick-based) eviction.
#
# Proves two things about the EVICT_RUNNING change:
#   1. A write right after a huge maxmemory overshoot succeeds (no spurious OOM).
#   2. dbsize drains while the server is completely idle (evict_tick doing work).
#
# Server must be running WITHOUT a password (no .conf / no requirepass).
# WARNING: FLUSHALLs the target server (start and end). Dev server only.
#
# Usage: ./scripts/test_evict_tick.sh [host] [port]

set -u
HOST="${1:-127.0.0.1}"
PORT="${2:-1234}"
RCLI="redis-cli -h $HOST -p $PORT"
NKEYS=50000
LIMIT=1048576   # 1MB in bytes; avoids depending on "1mb" unit parsing

ORIG_MAX=""
ORIG_POL=""

restore(){
  if [ -n "$ORIG_MAX" ]; then $RCLI config set maxmemory "$ORIG_MAX" >/dev/null; fi
  if [ -n "$ORIG_POL" ]; then $RCLI config set maxmemory-policy "$ORIG_POL" >/dev/null; fi
}

fail(){
  echo "FAIL: $*"
  restore
  exit 1
}

expect_ok(){
  # expect_ok <description> <cmd...>
  local desc="$1"; shift
  local reply
  reply=$($RCLI "$@")
  [ "$reply" = "OK" ] || fail "$desc: got '$reply', expected OK"
}

# --- reachability ---
[ "$($RCLI ping 2>/dev/null)" = "PONG" ] || { echo "FAIL: no server at $HOST:$PORT"; exit 1; }

ORIG_MAX=$($RCLI config get maxmemory | sed -n 2p)
ORIG_POL=$($RCLI config get maxmemory-policy | sed -n 2p)
echo "server ok (maxmemory=$ORIG_MAX policy=$ORIG_POL) — flushing and filling $NKEYS keys"

$RCLI flushall >/dev/null

# --- fill via one pipelined connection ---
pipe_out=$(seq 1 $NKEYS | awk '{print "set evk:"$1" v-"$1"-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}' | $RCLI --pipe 2>&1)
echo "$pipe_out"
echo "$pipe_out" | grep -q "errors: 0" || fail "pipelined fill reported errors"

n0=$($RCLI dbsize)
[ "$n0" -ge "$NKEYS" ] || fail "expected >= $NKEYS keys after fill, dbsize=$n0"

# --- force a huge overshoot ---
expect_ok "set policy"   config set maxmemory-policy allkeys-random
expect_ok "set limit"    config set maxmemory $LIMIT

# 1) the write that triggers eviction must be admitted, not OOM'd
expect_ok "probe write right after limit drop (would OOM on pre-fix build)" set probe 1
echo "PASS(1): write admitted immediately after overshoot"

# 2) idle drain: no writes from here on; dbsize must fall on its own
prev=$($RCLI dbsize)
stable=0
for i in $(seq 1 60); do
  sleep 1
  cur=$($RCLI dbsize)
  echo "  t=${i}s dbsize=$cur"
  if [ "$cur" -lt "$prev" ]; then stable=0; else stable=$((stable + 1)); fi
  prev=$cur
  [ "$stable" -ge 3 ] && break
done

[ "$prev" -lt $((n0 / 2)) ] || fail "dbsize only fell from $n0 to $prev while idle — evict_tick not draining"
echo "PASS(2): idle drain worked ($n0 -> $prev keys)"

# a write after stabilization must also succeed (we should be at/near the limit now)
expect_ok "post-drain write" set probe2 1

restore
$RCLI flushall >/dev/null
echo "PASS: incremental eviction OK (probe writes admitted, idle drain $n0 -> $prev)"
