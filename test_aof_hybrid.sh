#!/usr/bin/env bash
# Hybrid AOF (RDB preamble + RESP delta) test. Run from project root after building.
set -u
PORT=1234; PASS=kek1234
CLI="redis-cli -p $PORT -a $PASS"
start(){ MYRED_AOF=1 setsid ./build/server > /tmp/myred_hybrid.log 2>&1 < /dev/null & sleep 0.6; }
stop(){ pkill -x server 2>/dev/null; sleep 0.4; }

stop; rm -f appendonly.aof* dump.rdb*

echo "### 1. populate all types, then BGREWRITEAOF (snapshot → RDB preamble)"
start
$CLI set s hello            >/dev/null 2>&1
$CLI expire s 9000          >/dev/null 2>&1
$CLI rpush mylist a b c d   >/dev/null 2>&1
$CLI hset h f1 v1 f2 v2     >/dev/null 2>&1
$CLI sadd myset x y z       >/dev/null 2>&1
$CLI zadd z 1 a 2 b 3 c     >/dev/null 2>&1
$CLI bgrewriteaof           >/dev/null 2>&1
sleep 1

echo -n "### marker (first 8 bytes, expect MYAOFRDB): "
head -c 8 appendonly.aof; echo

echo "### 2. write a DELTA after the rewrite (these append as RESP)"
$CLI set delta_key 123      >/dev/null 2>&1
$CLI lpush mylist FRONT     >/dev/null 2>&1
sleep 1

echo "### 3. restart — must load RDB preamble + replay RESP delta"
stop
start
grep -a "RDB preamble\|replayed" /tmp/myred_hybrid.log

echo "### verify state survived the restart:"
echo "  s          = $($CLI get s 2>/dev/null)            (expect hello)"
echo "  ttl s      = $($CLI ttl s 2>/dev/null)            (expect ~9000, positive)"
echo "  mylist     = $($CLI lrange mylist 0 -1 2>/dev/null | tr '\n' ' ')(expect FRONT a b c d)"
echo "  hget h f2  = $($CLI hget h f2 2>/dev/null)        (expect v2)"
echo "  scard myset= $($CLI scard myset 2>/dev/null)      (expect 3)"
echo "  zscore z b = $($CLI zscore z b 2>/dev/null)       (expect 2)"
echo "  delta_key  = $($CLI get delta_key 2>/dev/null)    (expect 123 — proves delta replayed)"
stop

echo
echo "### 4. backward-compat: a plain RESP AOF (no marker) still loads"
rm -f appendonly.aof* dump.rdb*
printf '*3\r\n$3\r\nset\r\n$3\r\nfoo\r\n$3\r\nbar\r\n' > appendonly.aof
start
echo "  foo = $($CLI get foo 2>/dev/null)                 (expect bar — RESP-only path)"
grep -a "replayed" /tmp/myred_hybrid.log
stop

echo
echo "### 5. crash recovery: corrupt RESP tail must truncate, NOT eat the preamble"
rm -f appendonly.aof* dump.rdb*
start
$CLI set keep me            >/dev/null 2>&1
$CLI bgrewriteaof           >/dev/null 2>&1
# wait until the rewrite actually lands (finalize is async) — poll for the marker
for i in $(seq 1 20); do
  [ "$(head -c 8 appendonly.aof 2>/dev/null)" = "MYAOFRDB" ] && break
  $CLI ping >/dev/null 2>&1     # poke the loop so it ticks + finalizes
  sleep 0.2
done
if [ "$(head -c 8 appendonly.aof 2>/dev/null)" != "MYAOFRDB" ]; then
  echo "  !! rewrite never produced a hybrid file — test inconclusive"
fi
stop
printf '*3\r\n$3\r\nset' >> appendonly.aof   # half a command at the end
start
echo "  keep = $($CLI get keep 2>/dev/null)               (expect me — preamble intact)"
grep -a "truncated\|RDB preamble" /tmp/myred_hybrid.log   # MUST show 'RDB preamble' now
stop
echo "### done"
