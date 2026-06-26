#!/usr/bin/env bash
# Quick AOF smoke test for MYRED. Run from the project root after building.
set -u
PORT=1234
PASS=kek1234
CLI="redis-cli -p $PORT -a $PASS"

pkill -x server 2>/dev/null; sleep 0.5
rm -f appendonly.aof dump.rdb

MYRED_AOF=1 MYRED_AOF_FSYNC=everysec setsid ./build/server > /tmp/myred_aof.log 2>&1 < /dev/null &
sleep 0.6

echo "### sending commands"
$CLI set foo bar        >/dev/null 2>&1   # write  -> should log
$CLI incr counter       >/dev/null 2>&1   # write  -> should log
$CLI get foo            >/dev/null 2>&1   # read   -> must NOT log
$CLI setex sess 100 hi  >/dev/null 2>&1   # write  -> SET + PEXPIREAT
$CLI setnx foo NOPE     >/dev/null 2>&1   # no-op  -> must NOT log (gate)
$CLI del missingkey     >/dev/null 2>&1   # no-op  -> must NOT log (gate)

sleep 1.5   # let the everysec fsync tick run

echo "### AOF size: $(wc -c < appendonly.aof) bytes"
echo "### AOF content (^M = \\r, \$ = \\n):"
cat -A appendonly.aof

echo
echo "### replay check (clean db, pipe the AOF back):"
pkill -x server 2>/dev/null; sleep 0.5
rm -f dump.rdb
MYRED_AOF=1 setsid ./build/server > /tmp/myred_aof2.log 2>&1 < /dev/null &
sleep 0.6
$CLI --pipe < appendonly.aof >/dev/null 2>&1
echo "foo = $($CLI get foo 2>/dev/null)        (expect: bar)"
echo "counter = $($CLI get counter 2>/dev/null) (expect: 1)"
echo "ttl sess = $($CLI ttl sess 2>/dev/null)   (expect: positive, < 100)"

pkill -x server 2>/dev/null
