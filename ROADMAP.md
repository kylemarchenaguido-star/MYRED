# MYRED Roadmap

Redis-compatible in-memory database built from scratch in C++.
Speaks RESP on port 1234 — real `redis-cli` works against it.

**Build:** `cmake -B build && cmake --build build` → `build/server`  
**Run:** `./build/server` (opens `dump.rdb` from CWD)  
**Test:** `python3 stress_test.py --password kek1234`  
**Password:** `kek1234` (hardcoded in `server.cpp main()`)

---

## Current state — 2026-06-21

**294/294 tests passing. 0 stress errors. 1823 ops/sec.**

All 5 Redis data types implemented:

| Type | Commands | Status |
|---|---|---|
| String | get, set, del, exists | ✅ (numeric/bulk variants in progress) |
| Sorted Set | zadd, zrem, zscore, zrank, zquery, zrevquery | ✅ |
| List | lpush, rpush, lpop, rpop, llen, lindex, lrange, lset, linsert, lrem, ltrim | ✅ |
| Hash | hset, hget, hdel, hexists, hlen, hgetall, hkeys, hvals, hmget, hsetnx, hincrby, hstrlen, hscan | ✅ |
| Set | sadd, srem, sismember, smismember, scard, smembers, spop, srandmember, sscan, sinter, sunion, sdiff, sinterstore, sunionstore, sdiffstore, smove | ✅ |

Generic: exists, type, keys, scan, dbsize, randomkey, rename, renamenx, touch, unlink, flushall, expire, pexpire, expireat, pexpireat, ttl, pttl, persist  
Admin: auth, info, save, bgsave

---

## v5.2 — String commands (CURRENT WORK)

Complete the String type to near-Redis parity. All changes in `commands.cpp` only — no new files or data structures.

### ✅ Step 1 — Variadic DEL / EXISTS (DONE 2026-06-21)

- `DEL key [key...]` — loop `cmd[1..N]`, swap each key into `LookupKey`, `hm_delete` + `entry_del`, count hits, batch `g_writes_since_save += deleted`. Dispatch changed from `== 2` to `>= 2`.
- `EXISTS key [key...]` — same loop but copy (not swap) into `LookupKey` so duplicate keys are each counted. `expire_if_needed` check before counting. Dispatch changed from `== 2` to `>= 2`.

### ✅ Step 2 — Numeric: INCR / DECR / INCRBY / DECRBY / INCRBYFLOAT (DONE 2026-06-21)

One shared helper `incr_generic(cmd, out, delta)` used by all four integer commands.

```
INCR key          → cur + 1   (dispatch: cmd.size() == 2)
DECR key          → cur - 1   (dispatch: cmd.size() == 2)
INCRBY key N      → cur + N   (dispatch: cmd.size() == 3, parse N with str2int)
DECRBY key N      → cur - N   (dispatch: cmd.size() == 3, parse N with str2int)
INCRBYFLOAT key N → cur + N   (dispatch: cmd.size() == 3, parse N with str2dbl, return bulk string)
```

Key implementation details:
- Missing key → `ent->str` is empty → treat as 0. Check: `!ent->str.empty() && !str2int(ent->str, cur)`.
- Overflow guard BEFORE addition (C++ signed overflow is UB):
  `(delta > 0 && cur > INT64_MAX - delta) || (delta < 0 && cur < INT64_MIN - delta)`
- `DECRBY`: guard `if (by == INT64_MIN)` before negating — `-INT64_MIN` overflows `int64_t`.
- `INCRBYFLOAT`: check `isinf(result) || isnan(result)` after addition. Format: `snprintf(buf, 64, "%.17g", result)`. Returns bulk string (`resp_str`), not integer.
- `str2int` and `str2dbl` already exist in `commands.cpp` (~line 156–165).

### ✅ Step 3 — Set variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL (DONE 2026-06-21)

```
SETNX key val              → 1 if set, 0 if key existed
SETEX key seconds val      → set + expire (seconds). Error if ttl <= 0.
PSETEX key ms val          → set + expire (milliseconds). Error if ttl <= 0.
GETSET key val             → return old value (nil if missing), then set new
GETEX key [EX|PX|EXAT|PXAT|PERSIST]  → get + optionally update/clear expiry
GETDEL key                 → get + delete atomically. nil if missing.
```

`SETEX`/`PSETEX` reuse `entry_set_ttl` from `state.h` (same as EXPIRE/PEXPIRE path).

### ⬜ Step 4 — Multi-key: MSET / MGET / MSETNX

```
MSET key val [key val...]    → always OK (dispatch: cmd.size() >= 3 && size odd)
MGET key [key...]            → array of bulk strings / nil per key (type mismatch → nil, NOT WRONGTYPE)
MSETNX key val [key val...]  → set ALL or NONE. Scan all keys first; if any exist → return 0.
```

### ⬜ Step 5 — Bulk/range: APPEND / STRLEN / GETRANGE / SETRANGE

```
APPEND key val             → ent->str += val, return new length. Create if missing.
STRLEN key                 → ent->str.size(). Missing → 0. WRONGTYPE on wrong type.
GETRANGE key start end     → substring with Redis slice semantics (negative indices, clamp, never error).
SETRANGE key offset val    → overwrite bytes at offset; zero-pad if offset > len. Return new length.
```

`GETRANGE` negative index: `if (idx < 0) idx = max(0, (int64_t)len + idx)`.  
`SETRANGE` zero-pad: `if (offset > ent->str.size()) ent->str.resize(offset, '\0')`.  
`SETRANGE` max offset: 512 MB limit — reject `offset >= 512 * 1024 * 1024`.

---

## v5.3 — Project-wide code review (AFTER all string commands)

Full pass over the codebase before adding AOF or replication. Focus areas:

- **Clean code:** remove duplicate `unlink` dispatch (~line 1468 and ~1558 in `commands.cpp` — second is dead code). Magic numbers → named constants. Comment quality.
- **Efficiency:** unnecessary `std::string` copies (pass by const ref where possible). `hm_foreach` vs `hm_scan` misuse. `Buffer` realloc patterns.
- **Optimizations:** `srand(time(NULL))` in `server.cpp main()` — `RANDOMKEY`/`SPOP`/`SRANDMEMBER` are currently deterministic per boot. Consider `SO_REUSEPORT`.
- **Robustness:** lazy expiry gaps — `expire_if_needed` not called on zset/list reads. `fork()` + thread-pool malloc-lock deadlock (rare, noted). `SETRANGE` 512 MB bound.
- **Tests:** add `stress_test.py` coverage for all v5.2 commands. Target ~350+ passing tests.

---

## v6 — Persistence hardening

- AOF append-only log + fsync policies (always / everysec / no)
- BGREWRITEAOF — compaction (AOF grows unbounded without it)
- Crash-recovery testing (kill mid-save, reload); RDB+AOF hybrid loading

## v7 — Memory management

- `maxmemory` limit + eviction policies: noeviction, allkeys-lru, allkeys-lfu, volatile-lru, volatile-ttl, random
- `MEMORY USAGE` / `OBJECT ENCODING` introspection

## v8 — Pub/Sub & Transactions

- `SUBSCRIBE` / `UNSUBSCRIBE` / `PUBLISH` + pattern subscriptions
- `MULTI` / `EXEC` / `DISCARD` / `WATCH` — transactions

## v9 — Security & auth

- Config file for password → full `redis.conf`-style config
- ACL system, password hashing (bcrypt/argon2), IP allowlist, TLS

## v10 — Replication & HA

- Master-replica: `PSYNC`, replication backlog, partial resync
- Sentinel-style failover; cluster mode / hash-slot sharding

---

## Architecture notes

- **Single-threaded `poll()` event loop**, non-blocking I/O. `TCP_NODELAY` on all accepted sockets.
- **Thread pool (8 threads)** for background work: large ZSet/Set async deletes.
- **fork()-based BGSAVE**: child writes snapshot, parent keeps serving. `g_rdb_child_pid` tracked.
- **Dual HMap** with progressive rehashing. `hm_scan` uses reverse-binary cursor (safe during rehash).
- **Entry types:** `T_STR=1`, `T_ZSET=2`, `T_DLIST=3`, `T_HASH=4`, `T_SET=5`.
- **RDB tags** (separate from Entry::type): string=0, zset=1, list=2, hash=3, set=4.
- **TTL:** monotonic clock in memory, wall clock on disk (survives reboots).
- **Benchmarking:** Python harness is client-bound (~1800 ops/sec). Server min latency ~0.02 ms → ~50k ops/sec single-thread. For real numbers: `redis-benchmark -p 1234 -a kek1234 -t set,get,lpush -n 200000 -c 50 -P 16`
