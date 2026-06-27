#!/usr/bin/env bash
set -u
CLI="redis-cli -p 1234 -a kek1234"
pkill -x server 2>/dev/null; sleep 0.5
rm -f appendonly.aof dump.rdb

MYRED_AOF=1 MYRED_AOF_REWRITE_MIN=4096 setsid ./build/server >/tmp/diag.log 2>&1 </dev/null &
sleep 0.6

$CLI set foo bar      >/dev/null 2>&1
$CLI expire foo 5000  >/dev/null 2>&1
echo "live ttl foo (expect ~5000): $($CLI ttl foo 2>/dev/null)"

$CLI bgrewriteaof >/dev/null 2>&1
sleep 1

echo "=== is PEXPIREAT in the rewritten AOF? ==="
grep -a -c PEXPIREAT appendonly.aof
echo "=== full compacted AOF ==="
cat -A appendonly.aof
pkill -x server 2>/dev/null
