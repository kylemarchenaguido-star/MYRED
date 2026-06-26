# MYRED Roadmap

Redis-compatible in-memory database built from scratch in C++.
Speaks RESP on port 1234 — real `redis-cli` works against it.

**Build:** `cmake -B build && cmake --build build` → `build/server`  
**Run:** `./build/server` (opens `dump.rdb` from CWD)  
**Test:** `python3 stress_test.py --password kek1234`  
**Password:** `kek1234` (hardcoded in `server.cpp main()`)

---

## Current state — 2026-06-25

**328/328 tests passing. 0 stress errors.**

All 5 Redis data types implemented and full project-wide code review (v5.3) complete:

| Type | Commands | Status |
|---|---|---|
| String | get, set, del, exists, incr, decr, incrby, decrby, incrbyfloat, setnx, setex, psetex, getset, getex, getdel, mset, mget, msetnx, append, strlen, getrange, setrange | ✅ |
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
- Missing key → `entry_str(ent)` is empty → treat as 0. Check: `!entry_str(ent).empty() && !str2int(entry_str(ent), cur)`.
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

### ✅ Step 4 — Multi-key: MSET / MGET / MSETNX (DONE 2026-06-22)

```
MSET key val [key val...]    → always OK (dispatch: cmd.size() >= 3 && size odd)
MGET key [key...]            → array of bulk strings / nil per key (type mismatch → nil, NOT WRONGTYPE)
MSETNX key val [key val...]  → set ALL or NONE. Scan all keys first; if any exist → return 0.
```

### ✅ Step 5 — Bulk/range: APPEND / STRLEN / GETRANGE / SETRANGE (DONE 2026-06-22)

```
APPEND key val             → entry_str(ent) += val, return new length. Create if missing.
STRLEN key                 → entry_str(ent).size(). Missing → 0. WRONGTYPE on wrong type.
GETRANGE key start end     → substring with Redis slice semantics (negative indices, clamp, never error).
SETRANGE key offset val    → overwrite bytes at offset; zero-pad if offset > len. Return new length.
```

`GETRANGE` negative index: `if (idx < 0) idx = max(0, (int64_t)len + idx)`.  
`SETRANGE` zero-pad: `if (offset > entry_str(ent).size()) entry_str(ent).resize(offset, '\0')`.  
`SETRANGE` max offset: 512 MB limit — reject `offset >= 512 * 1024 * 1024`.

---

## ✅ v5.3 — Project-wide code review (DONE 2026-06-25)

Full pass over the codebase before adding AOF or replication. 328/328 tests passing.

- **Entry type:** `Entry::val` replaced with `std::variant<monostate, string, ZSet, Deque, EntryHash, EntrySet>` — eliminates a whole class of type-confusion bugs and makes the union explicit.
- **Dispatch table:** `do_request` replaced 110-branch if/else with `std::unordered_map<string_view, CmdSpec>` — O(1) dispatch, arity checked from table, ~20 lines of routing code.
- **Error constants:** `MSG_WRONGTYPE`, `MSG_NOT_INT`, `MSG_NOT_FLOAT`, `MSG_SYNTAX`, `MSG_OUT_OF_RANGE` — kills typo class.
- **RNG:** `srand(time(NULL))` → `std::mt19937_64` seeded from `std::random_device` — fixes modulo bias; `SPOP`/`SRANDMEMBER`/`RANDOMKEY` no longer deterministic per boot.
- **Uniform lazy expiry:** `expire_if_needed` added to `expire_generic` / `expireat_generic` — every keyspace read now expires first.
- **Direct-emit:** `do_keys`, `do_smembers`, `h_collect_reply` no longer collect into a `vector` then emit; use `hm_size` for upfront count and write directly into `Buffer*`.
- **ScanCtx unification:** `HScanCtx` / `SScanCtx` merged into single `ScanCtx`.
- **`glob_match`:** recursive `*` case replaced with O(n·m) two-pointer iterative algorithm.
- **`container_of`:** GCC statement-expression macro replaced with portable C++ template; 56 call sites updated; type errors now caught at compile time.
- **Server hardening:** `SO_REUSEADDR`, `accept()` while-loop, `EINTR` handling in read/write, env vars `MYRED_PORT` / `MYRED_PASSWORD`, `thread_pool_destroy` on shutdown.
- **RDB hardening:** fork+malloc deadlock fixed (serialize in parent before fork, child uses POSIX only), `.bak` rotation before atomic rename, bounds checks in all loaders, `stat()` for post-save size.
- **Thread pool:** graceful shutdown (`stop` flag, `thread_pool_destroy`), exception guard in worker, explicit `abort()` on pthread errors.
- **Build:** `-Wall -Wextra -Wshadow` added; Release build uses `-O3 -DNDEBUG` by default.

---

## v6 — Persistence hardening

Two complementary durability mechanisms: RDB (point-in-time snapshots, already done) and AOF (command log). Redis runs both together — RDB for fast restarts, AOF for durability.

---

### Step 1 — AOF write path

Every write command is serialized in RESP format and appended to `appendonly.aof` after it executes successfully.

**What to append:**
- Append the command as received, normalized to uppercase name + original args.
- Expiry-setting commands (`SETEX`, `PSETEX`, `EXPIRE`, `PEXPIRE`) must be rewritten as `PEXPIREAT key <absolute_ms>` when written to AOF so the TTL is correct on replay after a restart. Relative TTLs would be wrong.
- `GETEX` with EX/PX option → emit `PEXPIREAT` separately.
- `GETEX PERSIST` → emit `PERSIST key`.
- `FLUSHALL` → write it; it must replay.
- Read-only commands (`GET`, `HGET`, `LRANGE`, …) → never written.
- `DEL` of a missing key → still write it (simpler; the replay `DEL` is a no-op).

**Write path in code (`commands.cpp` / `server.cpp`):**
```
after do_request() returns:
  if (cmd is a write command)
      aof_append(cmd)   // serialize to RESP, write() into aof_buf
```

Track which commands are writes via a flag in `CmdSpec` (add `bool is_write` field, or a separate `k_write_cmds` set).

**AOF buffer:** Don't call `write()` per command — buffer in a `std::string aof_buf` in `g_data`. Flush the buffer:
- On every write if `appendfsync = always`
- Every 1 second if `appendfsync = everysec` (flush + `fdatasync` posted to thread pool)
- Never if `appendfsync = no` (OS decides)

**`fdatasync` vs `fsync`:** Prefer `fdatasync` — skips flushing the inode metadata (mtime, size), which is a significant speedup on ext4/xfs.

---

### Step 2 — fsync policies

Three modes, configurable (add `std::string aof_fsync` to `Config`):

| Mode | Durability | Throughput | Notes |
|---|---|---|---|
| `always` | 0 data loss | ~1–3k ops/sec | `fdatasync` after every `write()` in event loop |
| `everysec` | ≤1 sec loss | ~50k ops/sec | post `fdatasync` to thread pool every second |
| `no` | ~30 sec loss | no overhead | OS controls flush; fastest |

**`everysec` implementation:** add a field `uint64_t g_aof_last_fsync_ms` to `g_data`. In `process_timers()` (already called every event loop tick), if `now - g_aof_last_fsync_ms >= 1000`: flush `aof_buf` to disk and post `fdatasync(aof_fd)` to thread pool. Update `g_aof_last_fsync_ms`.

---

### Step 3 — AOF loading / replay

On startup, if `appendonly.aof` exists: replay it instead of (or after) loading RDB.

```cpp
void aof_load(const char *path) {
    // open file, read into a Buffer
    // loop: parse_resp_request() → do_request() (skip auth, skip AOF write during replay)
    // on parse error: warn + truncate to last good position (see Step 6)
}
```

**Optimization during replay:**
- Set a `g_data.loading = true` flag; during replay skip `aof_append()` (don't re-log commands being loaded).
- Disable the save trigger (`g_writes_since_save` doesn't count during load).
- Don't call `expire_if_needed` during load — keys will expire naturally once the server starts.
- Open the AOF fd for append at the end of the file after load completes.

---

### Step 4 — BGREWRITEAOF (compaction)

The AOF grows without bound. Compaction rewrites it to the minimum set of commands that reproduce current state. Triggered manually (`BGREWRITEAOF` command) or automatically when `aof_size > aof_rewrite_min_size` and `aof_size > last_rewrite_size * aof_rewrite_growth_factor`.

**Two-phase approach (same fork pattern as BGSAVE):**

**Phase 1 — serialize in parent (before fork), child writes only:**
- Serialize entire dataset into a buffer (same traversal as RDB but emit RESP commands instead of binary).
- Fork. Child receives the serialized buffer, writes it to `temp.aof`, calls `fdatasync`, `_exit(0)`.
- Parent continues serving; new write commands go to both `aof_buf` AND a new `aof_rewrite_buf`.

**Phase 2 — parent finalizes after child exits:**
- `waitpid` detects child done (via `rdb_check_background_save` pattern already in place).
- Parent appends `aof_rewrite_buf` (commands that arrived during rewrite) to `temp.aof`.
- `rename("temp.aof", "appendonly.aof")` — atomic swap.
- Clear `aof_rewrite_buf`.

**RESP commands to emit per type:**
```
String:  SET key value  [+ PEXPIREAT key ms if has TTL]
ZSet:    ZADD key score1 m1 score2 m2 ...  (batch all members in one command)
List:    RPUSH key e1 e2 e3 ...  (single command, preserves order)
Hash:    HSET key f1 v1 f2 v2 ...  (batch)
Set:     SADD key m1 m2 m3 ...  (batch)
```
Batching is critical — one command per element would inflate the AOF and slow replay.

**Cap on batch size:** Redis caps at 64 elements per command to keep individual RESP frames reasonable. Use `k_aof_batch_size = 64`.

---

### Step 5 — RDB + AOF hybrid loading

**Priority on startup:**
1. If `appendonly = yes` and `appendonly.aof` exists → load AOF only (most complete).
2. If only `dump.rdb` exists → load RDB.
3. If both exist and `appendonly = yes` → AOF wins; log a warning that RDB is being ignored.
4. If neither exists → start empty.

**Hybrid AOF format (optional, Redis 4.0+):** The rewritten AOF begins with an embedded RDB binary block (faster to load than replaying millions of RESP commands), followed by RESP commands for changes since the last rewrite. Header magic: `REDIS` (same as RDB). AOF loader detects the magic and switches to RDB loader for the header, then switches back to RESP replay for the tail. Implement this AFTER basic AOF is working.

---

### Step 6 — Crash recovery & AOF truncation

AOF files can be truncated mid-command if the server crashes during a `write()`. The loader must handle this gracefully.

**Detection:** `parse_resp_request` returns `-1` or `0` (unexpected EOF) partway through a command.

**Recovery:** on a truncation error, log a warning with the file offset, truncate the file to the last successfully parsed position (`ftruncate(fd, good_offset)`), and continue. Don't treat partial writes as fatal — this is expected after a crash.

**`redis-check-aof` equivalent:** add a `--check-aof` CLI flag that opens the AOF, parses it without executing, reports the last good offset, and optionally truncates.

**Test scenarios to run:**
```bash
# 1. Kill mid-save (RDB)
./build/server &; redis-cli -p 1234 -a kek1234 bgsave; kill -9 $!; ./build/server
# dump.rdb must be intact (old copy); new temp file is orphaned

# 2. Kill mid-AOF-write
# After recovery, server must load up to last complete command

# 3. Disk full during AOF write
# Server must log error and switch to read-only or warn loudly — don't silently drop writes
```

---

### Step 7 — Config-driven save triggers

Redis `save` directive: save RDB if N keys changed in the last M seconds.

```
save 3600 1      # 1 write in 1 hour
save 300 100     # 100 writes in 5 minutes
save 60 10000    # 10000 writes in 1 minute
```

Add `std::vector<SaveCondition> save_conditions` to `Config`, where `SaveCondition = {uint32_t seconds, uint32_t changes}`.

Check in `process_timers()`: for each condition, if `g_writes_since_save >= changes && now - g_last_save_time >= seconds * 1000` → trigger `BGSAVE` automatically.

Already have `g_writes_since_save` and `g_last_save_time`. Just need the config parsing and the multi-condition loop.

---


// Remember to the overload on aof_encode, make it an template at the end

### Optimization summary

| Concern | Approach |
|---|---|
| AOF write latency | Buffer writes; use `fdatasync` not `fsync` |
| BGREWRITEAOF memory | Serialize in parent before fork (no CoW explosion); child does pure I/O |
| Replay speed | Disable `aof_append` + `expire_if_needed` during load |
| Rewrite correctness | Dual-buffer: `aof_buf` for live AOF, `aof_rewrite_buf` for delta during child run |
| Disk full | Check `write()` return value; log + stop accepting writes rather than silently losing data |
| AOF size monitoring | Track `aof_current_size` (bytes written since last rewrite); expose in `INFO persistence` |

---

### Alternative write path — log raw RESP bytes (optimization, do after Step 1 works)

Step 1 serializes the AOF entry from the parsed `cmd` vector in `do_request`. That has two costs that bit us during testing:

1. **A defensive copy.** The command handlers `swap()`/consume `cmd`'s strings for speed (`do_set` → `entry_str(ent).swap(cmd[2])`), so by the time `aof_feed` runs after `spec.fn()`, the args are empty. Step 1 works around this by snapshotting `cmd` *before* the handler runs — one `std::vector<std::string>` copy per logged write.
2. **Re-encoding.** We rebuild the RESP frame (`*N`, `$len`, …) that the client already sent us verbatim.

Both vanish if we log the **raw consumed bytes** instead. In `try_one_request`, the original RESP frame is sitting in `conn->incoming` at `[0, consumed)` *before* `buf_consume()` runs and *before* any handler touches `cmd`. So:

```cpp
int32_t consumed = parse_resp_request(&conn->incoming, cmd);
...
// capture the raw frame BEFORE consuming / dispatching
const char *raw = (const char *)buf_data(&conn->incoming);
size_t raw_len = (size_t)consumed;

do_request(cmd, &conn->outgoing, conn, raw, raw_len);   // pass it through
buf_consume(&conn->incoming, raw_len);
```

`do_request` then appends `raw[0..raw_len)` to `g_aof_buf` with a single `memcpy` (no vector copy, no re-encode) when the write is logged.

**The one wrinkle:** TTL-relative commands (`EXPIRE`, `SETEX`, `GETEX … EX`) still can't be logged verbatim — a relative TTL replays wrong after a restart. So those keep the translate-to-`PEXPIREAT` path from Step 1. The dispatch is: if the command is in the small "needs TTL rewrite" set → re-encode the rewritten form; otherwise → `memcpy` the raw bytes. The expensive path is the rare one.

**Trade-off:** the raw-bytes path means the AOF stores the command *as the client sent it* (e.g. original casing, inline vs multibulk if you ever accept inline) rather than a normalized form. For a RESP-only server that's fine. Net: removes a per-write heap allocation on the hot path while keeping TTL correctness.

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
- **Entry types:** `T_STR=1`, `T_ZSET=2`, `T_DLIST=3`, `T_HASH=4`, `T_SET=5`. Value stored as `std::variant` in `Entry::val`.
- **RDB tags** (separate from Entry::type): string=0, zset=1, list=2, hash=3, set=4.
- **TTL:** monotonic clock in memory, wall clock on disk (survives reboots).
- **Benchmarking:** Python harness is client-bound (~1800 ops/sec). Server min latency ~0.02 ms → ~50k ops/sec single-thread. For real numbers: `redis-benchmark -p 1234 -a kek1234 -t set,get,lpush -n 200000 -c 50 -P 16`
