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

Two complementary durability mechanisms: RDB (point-in-time snapshots) and AOF (command log). Redis runs both together — RDB for fast restarts, AOF for durability. AOF code lives in `aof.cpp` / `aof.h`.

### ✅ Step 1 — AOF write path (DONE)

- Every mutating command is serialized to RESP and appended to an in-memory `g_aof_buf`, flushed to `appendonly.aof` once per event-loop tick (batched — pipelined commands share one `write()`).
- `CmdSpec` gained `bool is_write` (default `false`); only the ~46 write commands are tagged `true`.
- Logging is **mutation-gated**: `do_request` diffs `g_writes_since_save` across `spec.fn()` and only logs if the command actually changed state — no-op writes (`SETNX` miss, `DEL` of missing key) never reach the AOF.
- Relative-TTL commands are translated to absolute `PEXPIREAT key <wall_ms>` in `aof_feed` so TTLs survive a restart.
- **Bug fixed during impl:** handlers `swap()` out `cmd`'s strings, so `do_request` snapshots `cmd` *before* calling `spec.fn()`. Also fixed missing verbatim fallback in `aof_feed`, and missing counter bumps in `setex_generic` (+ `strlen` mis-tagged as write).

### ✅ Step 2 — fsync policies (DONE)

`Config::aof_fsync` enum (`MYRED_AOF_FSYNC=always|everysec|no`), parsed to an enum once at boot (int compare in hot path):

| Mode | Durability | How |
|---|---|---|
| `always` | 0 loss | `fdatasync` synchronously in the loop after each flush (only when bytes were written) |
| `everysec` | ≤1s loss | `fdatasync` posted to the **thread pool** every 1s; never blocks the loop |
| `no` | ~30s loss | OS decides |

`everysec` uses a `std::atomic<bool> g_aof_fsync_pending` CAS guard so a slow disk can't queue up a backlog of sync jobs. `fdatasync` (not `fsync`) skips non-essential metadata.

### ✅ Step 3 — AOF loading / replay (DONE)

`aof_load(path)` slurps the file and feeds it through the **same** `parse_resp_request` → `do_request` path the network uses (zero duplicate command logic). Key points:
- `g_data.g_loading = true` during replay → the write gate suppresses re-logging.
- Dummy `Conn` with `authenticaded = true` bypasses auth; replies discarded into a drained sink buffer.
- `g_writes_since_save` reset to 0 after replay so it doesn't immediately trip the save trigger.

### ✅ Step 4 — BGREWRITEAOF compaction (DONE)

Two-phase fork (mirrors BGSAVE): parent serializes the whole dataset to minimal RESP commands (`cb_aof_rewrite`), forks, child writes the snapshot + `fdatasync` + `_exit`, parent appends the delta (`g_aof_rewrite_buf`, dual-written during the rewrite) and atomically `rename`s into place, then repoints the live fd. Per-type emit is batched at `k_aof_batch = 64` elements (`SET`/`ZADD`/`RPUSH`/`HSET`/`SADD`), `PEXPIREAT` emitted after the data command. Manual `BGREWRITEAOF` + auto-trigger (size in memory, gated 1/s, fires past `aof_rewrite_min_size` and `perc` growth). **Bug fixed:** `appebdonly.aof.tmp` filename typo made finalize a silent no-op.

### ✅ Step 5 — RDB + AOF priority load (DONE)

Startup priority: AOF wins when enabled and present (RDB ignored, warns); else RDB; else empty. **Bug fixed:** `aof_enable` must be parsed from env *before* the load decision and the AOF `open()` must come *after* the load — original ordering loaded RDB by mistake.

### ✅ Step 6 — Crash recovery & AOF truncation (DONE)

- **Truncation recovery:** `aof_load` stops on `parse_resp_request` returning `0` (partial tail) or `-1` (corrupt), keeps everything up to the last good offset, and `truncate()`s the file to that offset. Partial trailing writes from a crash are non-fatal.
- **`--check-aof [--fix] [path]` CLI flag:** `aof_check` parses the AOF without executing, reports the last-good offset and command count, and (with `--fix`) truncates a bad tail. Runs before any server init, then exits.
- **Disk-full policy:** `aof_flush` sets `g_aof_write_err` on a real write error; `do_request` then rejects write commands with `MISCONF ...` (reads still served) until a later flush drains the buffer and clears the flag — no silent data loss, no unbounded buffer growth.
- **Signal hardening:** `SIGXFSZ` and `SIGPIPE` are `SIG_IGN`'d so a file-size limit or a client disconnect mid-write returns a handleable `errno` (`EFBIG`/`EPIPE`) instead of killing the process.

### ❌ Step 7 — Config-driven save triggers (TODO)

Redis `save N M` directive (save RDB if M writes in N seconds). Add `std::vector<SaveCondition>{seconds, changes}` to `Config`; in `process_timers()`, fire `rdb_save_background()` when any condition's `g_writes_since_save >= changes && now - g_last_save_ms >= seconds*1000`. Counters already exist; needs config parsing + the multi-condition loop. (Currently a single hardcoded `k_save_interval_ms` / `k_save_after_writes`.)

---

## v6 — Optimizations & future work

Forward-looking improvements to the persistence layer. None are required for correctness — the core AOF (Steps 1-5) is functional. Ordered roughly by value.

### A. `aof_encode` → one template

There are currently two `aof_encode` overloads (`initializer_list<string_view>` and `vector<string>`). Collapse into a single template over any iterable of `string_view`-convertible elements:
```cpp
template <typename Range>
void aof_encode(std::string &dst, const Range &args);   // works for both call sites
```
Removes the duplicate body and the maintenance hazard of fixing one and forgetting the other.

### B. Alternative write path — log raw RESP bytes

Step 1 snapshots `cmd` (a `std::vector<std::string>` copy) and re-encodes the RESP frame. Both costs vanish if we log the **raw consumed bytes**: in `try_one_request` the original frame sits in `conn->incoming` at `[0, consumed)` before `buf_consume()` and before any handler mutates `cmd`.

```cpp
int32_t consumed = parse_resp_request(&conn->incoming, cmd);
const char *raw = (const char *)buf_data(&conn->incoming);   // capture BEFORE consume/dispatch
size_t raw_len = (size_t)consumed;
do_request(cmd, &conn->outgoing, conn, raw, raw_len);
buf_consume(&conn->incoming, raw_len);
```
`do_request` then `memcpy`s `raw` into `g_aof_buf` (no vector copy, no re-encode) when logging.

**Wrinkle:** TTL-relative commands (`EXPIRE`, `SETEX`, `GETEX … EX`) still need the translate-to-`PEXPIREAT` path — they can't be logged verbatim. So: command in the small "needs TTL rewrite" set → re-encode rewritten form; else → `memcpy` raw. The expensive path is the rare one. **Trade-off:** stores the command as the client sent it (original casing) rather than normalized — fine for a RESP-only server. Removes a per-write heap allocation on the hot path.

### C. Hybrid AOF format (embedded RDB header)

A rewritten AOF that begins with an RDB binary block (fast to load) followed by RESP commands for changes since the rewrite — much faster to load than replaying millions of RESP commands for a large dataset. Header magic `MYRED` (same as RDB): `aof_load` detects it, runs the RDB loader for the header, then switches to RESP replay for the tail. Reuses the existing `rdb_serialize` in `cb_aof_rewrite`'s parent phase. Do this only once datasets are large enough that RESP replay is the bottleneck.

### D. `INFO persistence` section / observability

Expose the counters we already track: `aof_enabled`, `aof_current_size`, `aof_base_size`, `aof_pending_rewrite` (`g_aof_child_pid != -1`), `aof_last_bgrewrite_status`, `rdb_last_save_time`, `rdb_changes_since_save`. Cheap (data already in `g_data`) and makes the AOF debuggable in production without log-diving.

### E. Smaller wins

- **`g_aof_rewrite_buf` as a `Buffer`** (or `reserve()` it) — during a long rewrite under heavy write load the delta `std::string` reallocates repeatedly.
- **Precise `GETEX`/`GETDEL` translation** — `GETEX` is currently logged verbatim; `GETEX key EX 100` should emit `PEXPIREAT`, `GETEX key PERSIST` → `PERSIST`, `GETDEL` → `DEL`. (Correctness gap, not just speed.)
- **`writev()` for flush** — write the AOF buffer without first concatenating frames, scatter-gather from per-frame chunks. Marginal; only if profiling shows the `+=` copies matter.
- **Reserve `g_aof_buf` capacity** at startup to avoid early reallocations under burst load.

### Optimization summary (implemented)

| Concern | Approach (done) |
|---|---|
| AOF write latency | Buffer in memory; one `write()` per tick; `fdatasync` not `fsync` |
| fsync never blocks loop | `everysec` offloads `fdatasync` to thread pool with CAS in-flight guard |
| BGREWRITEAOF memory | Serialize in parent before fork (no CoW explosion); child does pure I/O |
| Replay correctness | `g_loading` suppresses re-logging; counter reset after load |
| Rewrite correctness | Dual-buffer: `g_aof_buf` live + `g_aof_rewrite_buf` delta during child run |
| No-op writes | Mutation-gated via `g_writes_since_save` delta |
| Auto-rewrite cost | Size tracked in memory (no per-tick `stat`); check gated to 1/s |

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
