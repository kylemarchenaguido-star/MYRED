# MYRED stress test — 2026-06-19 00:42:31

```
(logging output to stress_results.md)
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
  ✓ rpush a b c → 3
  ✓ llen → 3
  ✓ lrange 0 -1 → [a,b,c]
  ✓ lpush x y → 5
  ✓ lrange after lpush
  ✓ lindex 0 → y
  ✓ lindex -1 → c
  ✓ lindex 2 → a
  ✓ lindex 100 → nil
  ✓ lpop → y
  ✓ rpop → c
  ✓ lrange after pops

── Lists: LSET / LINSERT ─────────────────────────────
  ✓ lset 1 B → OK
  ✓ lindex 1 → B
  ✓ lset out of range → error
  ✓ linsert before B → 4
  ✓ lrange after insert before
  ✓ linsert after c → 5
  ✓ lrange after insert after
  ✓ linsert pivot missing → -1
  ✓ linsert missing key → 0

── Lists: LREM / LTRIM ───────────────────────────────
  ✓ lrem 2 a (head) → 2
  ✓ lrange after lrem head
  ✓ lrem -1 a (tail) → 1
  ✓ lrange after lrem tail
  ✓ ltrim 1 3 → OK
  ✓ lrange after ltrim
  ✓ ltrim 0 -1 keeps all
  ✓ lrange unchanged
  ✓ llen after empty ltrim → 0

── Lists: wrong-type + missing-key behavior ──────────
  ✓ lpush on string → WRONGTYPE
  ✓ lrange on string → WRONGTYPE
  ✓ llen missing → 0
  ✓ lrange missing → []
  ✓ lpop missing → nil
  ✓ lrem missing → 0

── Hashes: HSET / HGET / HDEL / HEXISTS / HLEN / HGETALL / HKEYS / HVALS / HMGET 
  ✓ hset a b (2 new) → 2
  ✓ hset a update → 0
  ✓ hget a → 9
  ✓ hget missing → nil
  ✓ hlen → 2
  ✓ hexists a → 1
  ✓ hexists zzz → 0
  ✓ hmget a b zzz
  ✓ hkeys = {a,b}
  ✓ hvals = {9,2}
  ✓ hgetall = {a:9,b:2}
  ✓ hdel a → 1
  ✓ hdel a again → 0
  ✓ hdel b → 1
  ✓ type after empty → none
  ✓ hlen missing → 0
  ✓ hget missing → nil
  ✓ hexists missing → 0
  ✓ hgetall missing → []
  ✓ hmget missing → [nil,nil]
  ✓ hget on string → WRONGTYPE
  ✓ hset on string → WRONGTYPE

── Generic: EXISTS / TYPE / EXPIRE / TTL / PERSIST ───
  ✓ exists missing → 0
  ✓ exists present → 1
  ✓ type string
  ✓ type zset
  ✓ type list
  ✓ type hash
  ✓ type missing → none
  ✓ expire gk 100 → 1
  ✓ ttl in (0,100]
  ✓ ttl no-such-key → -2
  ✓ persist gk → 1
  ✓ ttl after persist → -1
  ✓ persist again → 0
  ✓ expire gk -1 deletes → 1
  ✓ exists after expire -1 → 0

── SCAN (cursor iteration + MATCH) ───────────────────
  ✓ scan sees user:1
  ✓ scan sees user:2
  ✓ scan sees user:3
  ✓ scan sees order:1
  ✓ scan sees order:2
  ✓ scan match user:* → only users
  ✓ scan match excludes orders
  ✓ scan match order:? → both orders

── ASYNCDEL Command ──────────────────────────────────
  ✓ asyncdel missing → 0
  ✓ asyncdel string → 1
  ✓ string gone
  ✓ asyncdel small zset → 1
  ✓ small zset gone
  ℹ  inserting 1500 entries...
  ✓ large zset created → 0
  ℹ  sending asyncdel (thread pool path)...
  ✓ asyncdel large → 1
  ✓ asyncdel fast (<100ms)
  ✓ large zset immediately gone
  ℹ  returned in 0.1ms

── Edge Cases ────────────────────────────────────────
  ✓ zscore negative → -5
  ✓ zscore zero → 0
  ✓ same score sorted by name
  ✓ special chars in value
  ✓ get on zset → WRONGTYPE error
  ✓ 100 rapid get correct
  ℹ  100 rapid set/get/del complete

── INFO Command ──────────────────────────────────────
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:12\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:0\r\ntotal_connections:1\r\n\r\n# Memory\r\nused_memory_bytes:4603904\r\nused_memory_mb:4.39\r\n\r\n# Stats\r\ntotal_commands:2042\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:5167\r\nrdb_changes_since_save:1435\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\n\r\n# Replication\r\nrole:master\r\n'
  ✓ has # Server section
  ✓ has # Clients section
  ✓ has # Memory section
  ✓ has # Stats section
  ✓ has # Keyspace section
  ✓ has # Persistence section
  ✓ has version field
  ✓ has uptime_seconds field
  ✓ has connected_clients field
  ✓ has total_commands field
  ✓ has keys_total field
  ✓ has keys_with_ttl field

  INFO output:
    # Server
    version:1.0.0
    uptime_seconds:12
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:0
    total_connections:1
    # Memory
    used_memory_bytes:4603904
    used_memory_mb:4.39
    # Stats
    total_commands:2042
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:5167
    rdb_changes_since_save:1435
    rdb_last_save_ok:1
    rdb_last_save_size_bytes:0
    # Replication
    role:master

── SAVE / RDB Persistence ────────────────────────────
  ✓ save → OK
  ✓ dump.rdb exists
  ✓ dump.rdb not empty
  ℹ  dump.rdb size: 75 bytes
  ✓ magic number correct

── BGSAVE (fork-based background save) ───────────────
  ✓ bgsave returns string → 'Background saving started'
  ✓ bgsave returns fast (<50ms)
  ℹ  bgsave returned in 0.7ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 9.2ms
  ✓ save did not block event loop (burst <500ms)
  ✓ dump.rdb exists after bgsave
  ✓ bgsave file has magic
  ✓ second bgsave handled gracefully

── Authentication ────────────────────────────────────
  ✓ wrong password → error
  ✓ unauthenticated → NOAUTH error
  ✓ correct password → OK
  ✓ authenticated set works

── Persistence Round-trip (in-memory) ────────────────
  ✓ save → OK
  ✓ string still readable
  ✓ zset alice still readable → 10
  ✓ zset bob still readable → 20
  ✓ zrank alice → 0
  ✓ ttl preserved after save

═══════════════════════════════════════════════════════
Results: 180/180 passed
All tests passed!
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    7.07s
  Throughput: 566 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 13.78ms
  Latency min: 0.03ms
  Latency max: 161.39ms
  Latency p95: 111.98ms
  Latency p99: 139.33ms
  No errors!
  ℹ  cleaned 152 leftover keys

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
═══════════════════════════════════════════════════════


```
