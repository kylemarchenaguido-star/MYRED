# MYRED stress test — 2026-06-18 23:22:24

```
(logging output to run.md)
═══════════════════════════════════════════════════════
  Redis Server RESP Stress Test
  Connecting to 127.0.0.1:1234
  Using authentication
═══════════════════════════════════════════════════════
✓ Server is reachable

── String Commands: GET / SET / DEL ──────────────────
  ✓ set k1 hello → OK
  ✓ get k1 → hello
  ✓ set k1 world → OK
  ✓ get k1 → world
  ✓ get missing → nil
  ✓ del k1 → 1
  ✓ get after del → nil
  ✓ del missing → 0
  ✓ get empty → ''
  ✓ get long value

── KEYS Command ──────────────────────────────────────
  ✓ keys returns list → ['kb', 'ka', 'kc']
  ✓ ka in keys
  ✓ kb in keys
  ✓ kc in keys

── TTL Commands: PEXPIRE / PTTL ──────────────────────
  ✓ pexpire ttlkey 5000 → 1
  ✓ pttl returns int → 5000
  ✓ pttl > 0
  ✓ pttl <= 5000
  ℹ  remaining TTL: 5000ms
  ✓ pttl no-ttl → -1
  ✓ pttl missing → -2
  ℹ  waiting 600ms for key to expire...
  ✓ expired key → nil
  ✓ pexpire -1 deletes key → 1
  ✓ pttl after delete → -2
  ✓ get after delete → nil

── Sorted Set: ZADD / ZSCORE / ZREM / ZRANK ──────────
  ✓ zadd n1 1.0 → 1
  ✓ zadd n2 2.0 → 1
  ✓ zadd n3 3.0 → 1
  ✓ zadd n4 0.5 → 1
  ✓ zadd n1 update → 0
  ✓ zscore n1 → 1.5 → 1.5
  ✓ zscore missing → nil
  ✓ zrank n4 → 0
  ✓ zrank n1 → 1
  ✓ zrank n2 → 2
  ✓ zrank n3 → 3
  ✓ zrank missing → nil
  ✓ zrem n1 → 1
  ✓ zscore after zrem → nil
  ✓ zrem missing → 0

── Sorted Set: ZQUERY / ZREVQUERY ────────────────────
  ✓ zquery returns list → ['a', '1', 'b', '2', 'c', '3', 'd', '4', 'e', '5']
  ✓ zquery all → 10 items
  ✓ zquery order correct
  ✓ zquery offset=1 → 8 items
  ✓ zquery limit=4 → 8 items (4 pairs)
  ✓ zquery from 3.0 → 6 items
  ✓ zquery no results → 0
  ✓ zrevquery returns list → ['e', '5', 'd', '4', 'c', '3', 'b', '2', 'a', '1']
  ✓ zrevquery all → 10 items
  ✓ zrevquery order correct
  ✓ zrevquery from 3.5 → 6 items

── Lists: LPUSH/RPUSH/LPOP/RPOP/LLEN/LINDEX/LRANGE ───
  ✗ rpush a b c → 3
    got:      94432173680248
    expected: 3
  ✗ llen → 3
    got:      94432173680248
    expected: 3
  ✓ lrange 0 -1 → [a,b,c]
  ✗ lpush x y → 5
    got:      94432173680248
    expected: 5
  ✓ lrange after lpush
  ✓ lindex 0 → y
  ✓ lindex -1 → c
  ✓ lindex 2 → a
  ✗ lindex 100 → nil
    got:      'c'
    expected: None
  ✓ lpop → y
  ✓ rpop → c
  ✓ lrange after pops

── Lists: LSET / LINSERT ─────────────────────────────
  ✓ lset 1 B → OK
  ✓ lindex 1 → B
  ✗ lset out of range → error
    got:      'OK'
    expected: a RESP error
  ✗ linsert before B → 4
    got:      94432173680248
    expected: 4
  ✓ lrange after insert before
  ✗ linsert after c → 5
    got:      94432173680248
    expected: 5
  ✓ lrange after insert after
  ✓ linsert pivot missing → -1
  ✓ linsert missing key → 0

── Lists: LREM / LTRIM ───────────────────────────────
  ✓ lrem 2 a (head) → 2
  ✓ lrange after lrem head
  ✓ lrem -1 a (tail) → 1
  ✓ lrange after lrem tail

Unexpected error: timed out

── Authentication ────────────────────────────────────

Unexpected error: timed out

═══════════════════════════════════════════════════════
Results: 68/75 passed
Failed tests:
  • rpush a b c → 3
  • llen → 3
  • lpush x y → 5
  • lindex 100 → nil
  • lset out of range → error
  • linsert before B → 4
  • linsert after c → 5
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✗ 10 errors during concurrent writes
    timed out
    timed out
    timed out

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000
  Worker 2 connect failed: timed out  Worker 0 connect failed: timed out
  Worker 6 connect failed: timed out  Worker 5 connect failed: timed out  Worker 4 connect failed: timed out  Worker 7 connect failed: timed out

  Worker 3 connect failed: timed out


  Worker 1 connect failed: timed out


  Elapsed:    5.01s
  Throughput: 0 ops/sec
  No operations recorded.

═══════════════════════════════════════════════════════
  SOME TESTS FAILED
═══════════════════════════════════════════════════════


```
