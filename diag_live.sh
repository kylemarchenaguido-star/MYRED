#!/usr/bin/env bash
set -u
CLI="redis-cli -p 1234 -a kek1234"
pkill -x server 2>/dev/null; sleep 0.5
rm -f appendonly.aof* appebdonly.aof* dump.rdb

MYRED_AOF=1 setsid ./build/server >/tmp/diaglive.log 2>&1 </dev/null &
sleep 0.6

$CLI set foo bar     >/dev/null 2>&1
$CLI expire foo 5000 >/dev/null 2>&1
sleep 1

echo "=== LIVE appendonly.aof (NO rewrite yet) — expect SET foo bar THEN PEXPIREAT foo ==="
cat -A appendonly.aof
echo "=== orphan temp files left by the typo? ==="
ls -la appebdonly.aof.tmp appendonly.aof.tmp 2>/dev/null || echo "(none)"
pkill -x server 2>/dev/null
