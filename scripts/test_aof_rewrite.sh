#!/usr/bin/env bash
# BGREWRITEAOF test: manual trigger + auto-trigger. Run from anywhere after building.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR" || exit 1

PORT=1234; PASS=kek1234
CLI="redis-cli -p $PORT -a $PASS"

pkill -x server 2>/dev/null; sleep 0.5
rm -f appendonly.aof appendonly.aof.tmp dump.rdb

# low floor (4 KB) so the auto-trigger fires without needing 64 MB
MYRED_AOF=1 MYRED_AOF_FSYNC=everysec \
MYRED_AOF_REWRITE_MIN=4096 MYRED_AOF_REWRITE_PERC=100 \
  setsid ./build/server > /tmp/myred_rewrite.log 2>&1 < /dev/null &
sleep 0.6

echo "### bloat: write the same key 2000 times + one list"
for i in $(seq 1 2000); do $CLI set k $i >/dev/null 2>&1; done
$CLI rpush mylist a b c d e >/dev/null 2>&1
$CLI hset h f1 v1 f2 v2     >/dev/null 2>&1
$CLI sadd s m1 m2 m3        >/dev/null 2>&1
$CLI expire k 10000         >/dev/null 2>&1
sleep 1.5
echo "### size before rewrite: $(wc -c < appendonly.aof) bytes"

echo "### --- MANUAL trigger ---"
$CLI bgrewriteaof
sleep 1
echo "### size after manual rewrite: $(wc -c < appendonly.aof) bytes"
echo "### compacted content:"
cat -A appendonly.aof

echo
echo "### --- AUTO trigger --- (bloat again, wait for the 1s check)"
for i in $(seq 1 2000); do $CLI set k $i >/dev/null 2>&1; done
sleep 2.5
echo "### size after auto rewrite: $(wc -c < appendonly.aof) bytes"
grep -a "auto-trigger\|completed" /tmp/myred_rewrite.log

echo
echo "### --- replay check (compacted file reconstructs state) ---"
pkill -x server 2>/dev/null; sleep 0.5
rm -f dump.rdb
MYRED_AOF=1 setsid ./build/server > /tmp/myred_rewrite2.log 2>&1 < /dev/null &
sleep 0.6
$CLI --pipe < appendonly.aof >/dev/null 2>&1
echo "k       = $($CLI get k 2>/dev/null)        (expect: 2000)"
echo "ttl k   = $($CLI ttl k 2>/dev/null)        (expect: positive)"
echo "mylist  = $($CLI lrange mylist 0 -1 2>/dev/null | tr '\n' ' ')(expect: a b c d e)"
echo "h.f1    = $($CLI hget h f1 2>/dev/null)    (expect: v1)"
echo "scard s = $($CLI scard s 2>/dev/null)      (expect: 3)"

pkill -x server 2>/dev/null
echo "### done"
