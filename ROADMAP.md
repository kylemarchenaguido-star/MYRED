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
| Sorted Set | zadd (variadic), zrem, zscore, zrank, zquery, zrevquery, zpopmin | ✅ |
| List | lpush, rpush, lpop, rpop, llen, lindex, lrange, lset, linsert, lrem, ltrim | ✅ |
| Hash | hset, hget, hdel, hexists, hlen, hgetall, hkeys, hvals, hmget, hsetnx, hincrby, hstrlen, hscan | ✅ |
| Set | sadd, srem, sismember, smismember, scard, smembers, spop, srandmember, sscan, sinter, sunion, sdiff, sinterstore, sunionstore, sdiffstore, smove | ✅ |

Generic: exists, type, keys, scan, dbsize, randomkey, rename, renamenx, touch, unlink, flushall, expire, pexpire, expireat, pexpireat, ttl, pttl, persist  
Admin: auth, info, save, bgsave, bgrewriteaof, ping, config (stub)

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

### ✅ Step 7 — Config-driven save triggers (DONE)

Redis-style `save N M` (save RDB if ≥M writes happened within N seconds), OR-combined across conditions.
- **Config:** `std::vector<SaveCondition>{seconds, changes}` with defaults `{3600,1} {300,100} {60,10000}`; overridable via `MYRED_SAVE="3600 1 300 100 60 10000"` (empty string disables auto-save).
- **Trigger:** `process_timers` loops the conditions; first match (`g_writes_since_save >= changes && elapsed >= seconds*1000`) fires `rdb_save_background()`, guarded by `g_rdb_child_pid == -1`.
- **Dirty-counter fix:** `rdb_on_save_complete` previously did `g_writes_since_save++` (never reset!). Now each save path snapshots `g_dirty_at_save` at start, and completion **subtracts** it — so writes during a background save survive while saved changes clear. This is what makes the "changes since last save" windows meaningful.
- **Idle wake tightened:** `next_timer_ms` derives the next-save wake from the soonest *armed* condition instead of the old `k_save_interval_ms`, so an idle-but-dirty server saves on time. `k_save_interval_ms` / `k_save_after_writes` retired.

---

## ✅ v6 — Optimizations (DONE 2026-06-28)

All five optimizations applied. Several latent bugs surfaced and were fixed along the way.

### ✅ A. `aof_encode` → one template
Collapsed the two overloads into `template <typename Range> aof_encode(std::string&, const Range&)` plus a thin `initializer_list<string_view>` forwarder (braced `{...}` calls can't deduce a generic `Range`, so the forwarder keeps those call sites working). Body lives in one place. Moved to `aof.h` (templates are implicitly inline) to dedupe across TUs.

### ✅ B. Alternative write path — raw RESP bytes
`try_one_request` captures the raw frame from `conn->incoming` (`buf_consume` moved *after* dispatch) and threads `(raw, raw_len)` into `do_request`. Common writes are logged with one `memcpy` (`aof_append_raw`) — no snapshot copy, no re-encode. Only the five TTL-rewrite commands (flagged `aof_rewrite` in `CmdSpec`) still snapshot + translate. Dropped a per-write heap allocation on the hot path.

### ✅ C. Hybrid AOF format (embedded RDB preamble)
Rewrite now emits `["MYAOFRDB"][rdb_len:u64][RDB image][RESP delta]`. `rdb_build_aof_preamble` wraps a standalone `rdb_serialize` image; `rdb_load_buffer` loads an RDB from memory. `aof_load` detects the marker → fast binary load of the snapshot → RESP-replay only the tail; markerless files still load as plain RESP (backward-compatible). Load for large datasets drops from "replay N commands" to "one RDB pass + small delta". Verified by `test_aof_hybrid.sh`.

### ✅ D. `INFO persistence` observability
Added `aof_enabled`, `aof_current_size`, `aof_base_size`, `aof_pending_rewrite`, `aof_last_write_status` (from `g_aof_write_err`) to the `# Persistence` section — all read straight from `g_data`/`g_config`. Also bumped the `INFO` buffer to 4096 + clamped `len` (it trusted `snprintf`'s return as a length → latent OOB read).

### ✅ E. Smaller wins
- **Precise `GETEX` translation** (correctness): `aof_feed` now emits `PEXPIREAT`/`PERSIST`/`DEL` for every mutating `GETEX` form instead of logging it verbatim (relative `EX`/`PX` would replay wrong). `GETDEL` stays verbatim (deterministic, correct). `getex` tagged `aof_rewrite`.
- **`reserve()`** `g_aof_buf` (startup) and `g_aof_rewrite_buf` (per rewrite) to 64 KB — avoids reallocation churn.
- `writev()` scatter-gather flush: **skipped** (marginal, not worth it without profiling).

### Bugs fixed during the optimization pass
- **`g_last_save_ms` uninitialized** (Step 7): defaulted to 0, so `now - 0` satisfied every `save N M` window → spurious `BGSAVE` on the *first write*, which then blocked `BGREWRITEAOF` via the fork guard. Now seeded to boot time (with `g_aof_last_fsync_ms` / `g_aof_check_ms`).
- **`aof_feed` `return` bug**: leftover `return`s in the `EXPIRE`/`SETEX` branches skipped `g_aof_buf += frame` → relative-TTL commands were never logged live (masked by the RDB preamble on rewrite). Restructured so all branches fall through to the append.
- **`aof_feed` `g_aof_child_pid != 1`** (should be `!= -1`): mirrored every write into `g_aof_rewrite_buf` outside a rewrite → unbounded growth. Fixed.

### Optimization summary (implemented)

| Concern | Approach (done) |
|---|---|
| AOF write latency | Buffer in memory; one `write()` per tick; `fdatasync` not `fsync` |
| Per-write allocation | Raw-bytes `memcpy` path — no snapshot copy / re-encode except TTL commands |
| fsync never blocks loop | `everysec` offloads `fdatasync` to thread pool with CAS in-flight guard |
| BGREWRITEAOF memory | Serialize in parent before fork (no CoW explosion); child does pure I/O |
| Large-dataset load | Hybrid AOF: binary RDB preamble + RESP delta, not full RESP replay |
| Replay correctness | `g_loading` suppresses re-logging; counter reset after load |
| Rewrite correctness | Dual-buffer: `g_aof_buf` live + `g_aof_rewrite_buf` delta during child run |
| TTL correctness | All relative-TTL cmds (incl. `GETEX`) logged as absolute `PEXPIREAT` |
| No-op writes | Mutation-gated via `g_writes_since_save` delta |
| Auto-rewrite cost | Size tracked in memory (no per-tick `stat`); check gated to 1/s |
| Observability | `INFO persistence` exposes AOF size/state/write-status |

---

## v6.1 — redis-benchmark / tooling compatibility

Goal: make standard Redis tooling (`redis-benchmark`, `redis-cli`) run against MYRED so throughput can be measured with the real C client (the Python harness is client-bound at ~1800 ops/sec and measures the client, not the server).

### ✅ `PING`
`PING` → `+PONG`; `PING msg` → bulk-string echo of `msg`. RESP multibulk form, handled by the existing parser. Required because the default `redis-benchmark` suite leads with ping tests.

### ✅ `CONFIG` (stub)
`CONFIG GET <param>` → empty array, `CONFIG SET` / `CONFIG RESETSTAT` / `CONFIG REWRITE` → `+OK`. Just enough to satisfy tooling startup probes without implementing a real config system (real config is v9).

### Usage
```bash
redis-benchmark -p 1234 -a kek1234 -t set,get,incr,lpush,rpush,lpop,rpop,sadd,hset -n 200000 -c 50 -P 16 -q
```

### ✅ Inline protocol
`parse_resp_request` now handles requests not starting with `*` as inline commands: read a line (tolerates `\n` and `\r\n`), split on whitespace into args, return bytes consumed. Runaway lines past `k_max_msg` with no terminator are rejected. This makes `PING_INLINE` (bare `PING\r\n`) work alongside `PING_MBULK`.

### ✅ `ZPOPMIN`
`ZPOPMIN key [count]` — pops the lowest-score member(s) (leftmost AVL node), returns `[member, score, …]`, drops the key when the zset empties. `is_write`, deterministic → logged verbatim to AOF.

### ✅ Variadic `ZADD`
`ZADD key score member [score member ...]` now accepts multiple pairs (was single-pair, arity `4,4`). Table entry is `4, -1`; the handler enforces an even arg count, validates **all** scores before inserting any (atomicity — a bad score adds nothing), and returns the count of newly-added members.

### Remaining gap (optional)
- **`COMMAND` / `COMMAND DOCS`** — `redis-cli` interactive probes these; harmless if absent, add a stub if the interactive CLI complains. Not needed for `redis-benchmark`.

With inline + `ZPOPMIN`, the **default** `redis-benchmark` suite (no `-t`) now runs clean.

## v7 — Memory management

Goal: bound the server's footprint with a `maxmemory` limit and evict keys under
pressure, plus the introspection commands (`MEMORY USAGE`, `OBJECT ENCODING`) that
tooling and humans use to reason about it. Everything here builds on the existing
single-threaded loop, the `std::variant` `Entry`, the TTL min-heap, and the thread
pool for async free. Do the steps in order — accounting first, because nothing else
can be enforced or reported without it.

**Status (2026-07-03): Steps 1–6 DONE — v7 feature-complete.** Accounting, `maxmemory`
config, all 8 eviction policies, LRU/LFU metadata + sampling, the eviction/OOM trigger,
and introspection (`MEMORY`/`OBJECT`) are implemented and verified by `test_memory.py`.
Deferred to the **General upgrades** backlog (below v10): compact encodings
(listpack/intset/quicklist), object sharing / real `OBJECT REFCOUNT`, and the 16-slot
eviction pool.

### ✅ Step 1 — Memory accounting (foundation) (DONE 2026-07-01)

Implemented via a drift-free incremental counter: `Entry::mem` holds the bytes last
charged to each entry, the invariant `g_data.used_memory == Σ Entry::mem` is kept by
`mem_reaccount(ent)` (add-new-before-subtract-old), and `entry_del` discharges on the
main thread (safe with the async-delete pool). `entry_mem_usage(Entry*)` estimates
per-type cost; write handlers reaccount after mutating (once per distinct entry — so
MSET reaccounts inside its loop, HSET/SADD once after theirs); RDB loaders reaccount
each entry on the success path. `INFO memory` now reports `used_memory` +
`used_memory_rss` + `mem_fragmentation_ratio`. Verified by `test_memory.py` (per-type
create→drain→baseline, overwrite-stability, `FLUSHALL`→0) and a `mem_selfcheck`
counter-vs-sweep pass under the full `stress_test.py` suite. Bugs caught along the
way: `lpop`/`rpop` use-after-free (reaccount after `entry_del`), `mset`/`msetnx`
single-reaccount-outside-loop. Follow-ups noted: `setrange` missing `is_write`,
`zrem` doesn't drop an emptied zset.

Before any limit can be enforced we need a live `used_memory` number. Two layers:

- **Global counter** `g_data.used_memory` (`size_t`, atomic not required — single loop
  thread). Bump it in the allocation choke points, not by walking the keyspace:
  - `entry_new` / `entry_del_sync` — add/subtract the entry's accounted size.
  - Every value mutation that grows/shrinks a value (`APPEND`, `SETRANGE`, `RPUSH`,
    `HSET`, `SADD`, `ZADD`, `LREM`, …) must adjust the delta. Centralize this: a small
    `mem_account(Entry*, ssize_t delta)` helper so handlers don't each reinvent it.
- **Per-entry size estimator** `entry_mem_usage(const Entry*)` — walks the value by
  type and returns an approximate byte cost (used by both the counter's initial
  charge and by `MEMORY USAGE`):

  | Type | Cost model |
  |---|---|
  | key | `sizeof(Entry)` + key string len + HMap slot overhead |
  | string | `capacity()` of the `std::string` |
  | list (deque) | ring-buffer `cap * sizeof(slot)` + sum of element lens |
  | hash | nested HMap buckets + Σ(field+value) |
  | set | nested HMap buckets + Σ(member) |
  | zset | AVL nodes (`sizeof(AVLNode)` each) + HMap + Σ(member) |

  Keep it *approximate and cheap* — Redis's own `MEMORY USAGE` samples large
  aggregates rather than walking every element (see `SAMPLES` option, Step 6).
- **`INFO memory`** section: `used_memory`, `used_memory_human`, `maxmemory`,
  `maxmemory_policy`, `mem_fragmentation_ratio` (report RSS via `getrusage`/`statm`
  ÷ `used_memory`), `evicted_keys`, `expired_keys`. Read straight from `g_data`.

**Robustness:** the counter must never underflow — clamp at 0 and assert in debug.
A drift between the counter and a full re-walk is the classic accounting bug; add a
debug-only `MEMORY DOCTOR`-style self-check that compares the counter to a full
`entry_mem_usage` sweep and warns on divergence.

### ✅ Step 2 — `maxmemory` config (DONE 2026-07-03)

`Config::maxmemory` (bytes) + `MaxmemoryPolicy` enum (all 8 values, default
`noeviction`); `MYRED_MAXMEMORY` / `MYRED_MAXMEMORY_POLICY` env knobs; one shared
`parse_memory_size` (`k/m/g` = ×1000, `kb/mb/gb` = ×1024) reused by env + `CONFIG SET`.
`CONFIG GET/SET` upgraded from stub to real for `maxmemory` + `maxmemory-policy`
(validates policy names; unknown params still return empty array / `+OK`, so tooling
probes don't break). `INFO memory` reports `maxmemory` + `maxmemory_policy`. Note:
config is runtime-only (not persisted) — a restart resets `maxmemory` to `0`.

- `Config::maxmemory` (bytes, `0` = unlimited). Env knob `MYRED_MAXMEMORY`, parsed
  with a human-size reader (`100mb`, `1gb`, `512kb` → bytes) reused by `CONFIG SET`.
- `Config::maxmemory_policy` enum (see Step 3), default `noeviction`.
- Wire both into the `CONFIG GET/SET` stub so they're runtime-tunable (this turns the
  v6.1 stub into its first real parameters).

### ✅ Step 3 — Eviction policies (DONE 2026-07-03)

Encoded in `evict_pick_victim()`: random policies do one pick, `volatile-ttl` reads the
TTL heap root (nearest expiry, nearly free), LRU/LFU use best-of-N sampling. A `nullptr`
return means `noeviction` **or** a `volatile-*` policy with no TTL keys to take — both
resolve to `-OOM` in Step 5 (the volatile-fallback rule, in one place).

Eight policies, matching Redis. Store as an enum parsed once at boot / on `CONFIG SET`:

| Policy | Candidate set | Victim chosen by |
|---|---|---|
| `noeviction` | — | none — reject writes with `-OOM` |
| `allkeys-random` | all keys | random |
| `volatile-random` | keys with a TTL | random |
| `allkeys-lru` | all keys | oldest idle time |
| `volatile-lru` | keys with a TTL | oldest idle time |
| `allkeys-lfu` | all keys | lowest access frequency |
| `volatile-lfu` | keys with a TTL | lowest access frequency |
| `volatile-ttl` | keys with a TTL | nearest expiry |

**Volatile fallback:** when a `volatile-*` policy has no keys with a TTL to evict and
we're still over the limit, behave like `noeviction` (return `-OOM`) — do *not* touch
non-volatile keys.

### ✅ Step 4 — LRU / LFU metadata + approximated eviction (DONE 2026-07-03)

`Entry::lru` (24-bit: coarse clock for LRU / `[16b minute | 8b counter]` for LFU),
stamped at the single `lookup_entry` choke point (`entry_touch_access` on hit,
`entry_init_access` on create). `g_lru_clock` bumped once per event-loop tick from the
cached `now_ms` — no per-access syscall. LFU uses logarithmic probabilistic increment
(`p = 1/(base·factor+1)`) + minute-based decay. Samplers: `db_random_entry` (random
bucket across both rehash tables) and `volatile_random_entry` (off the TTL heap);
`evict_pick_victim` keeps best-of-`maxmemory_samples` (default 5). Configs
`maxmemory_samples` / `lfu_log_factor` / `lfu_decay_time`. **Deferred:** the persistent
16-slot eviction pool (best-of-N is correct; pool is a sampling-quality optimization).

Redis does **not** keep a true global LRU list (too much memory + pointer churn). It
stores a small per-object field and evicts by *sampling*. Mirror that:

- **Entry field:** add a `uint32_t lru` to `Entry` (24 bits used). Under an LRU policy
  it holds a coarse access timestamp; under LFU it packs a 16-bit last-decay-time +
  8-bit logarithmic counter. Costs 4 bytes/key.
- **LRU clock:** a global `g_lru_clock` updated once per event-loop tick from the
  cached monotonic time (no per-access syscall). On every keyspace *read/write* stamp
  `ent->lru = g_lru_clock`. Idle time = `g_lru_clock - ent->lru` (handle wraparound).
- **LFU counter:** probabilistic log increment (`p = 1/(counter*factor+1)`) on access,
  with time-based decay so cold-but-once-hot keys age out. `lfu_log_factor` /
  `lfu_decay_time` configs.
- **Eviction pool:** keep a fixed 16-slot pool of best candidates across calls. Each
  eviction round samples `maxmemory_samples` keys (default 5) via a cheap random
  sampler over the HMap, merges them into the pool sorted by idle-time / inverse-freq
  / nearest-TTL, and evicts from the good end. Amortizes sampling cost.
- **Random sampler:** needs an O(1)-ish "give me a random live entry" over the dual
  HMap. Generalize the `RANDOMKEY` reservoir path into `hm_random_entry()` that both
  tables can serve; for `volatile-*` iterate the TTL min-heap instead (it already
  holds exactly the keys with a TTL, and for `volatile-ttl` it's *sorted by expiry* —
  the victim is near the heap root, nearly free).

### ✅ Step 5 — Eviction trigger + write path integration (DONE 2026-07-03)

`free_memory_if_needed()` runs at the top of the write path (gated on `spec.is_write`),
evicts via `evict_pick_victim` → `entry_del` (async-frees big values + discharges
`used_memory` on the main thread), bumps `evicted_keys`, and **propagates a synthetic
`DEL` to the AOF via `aof_feed`** (critical: otherwise replay resurrects evicted keys).
Bounded attempts; `nullptr` victim → `-OOM command not allowed…`. `INFO` exposes
`evicted_keys`. **Bug fixed:** the OOM gate must exempt memory-freeing commands
(`cmd_can_grow_memory` — `DEL`/`UNLINK`/`FLUSHALL`/`EXPIRE`/pop/rem/…), otherwise you
deadlock over the cap with no way to recover. Verified end-to-end by `test_memory.py`'s
maxmemory section: `noeviction` OOMs + stays bounded + evicts nothing; `allkeys-lru`
never OOMs + holds near cap + `evicted_keys` climbs; freeing commands still work over
the cap.

- **Where:** a `free_memory_if_needed()` called at the top of `do_request` for any
  command flagged `is_write` in `k_cmd_table` (reads never trigger eviction) — before
  the handler runs, while over `maxmemory`.
- **Loop:** while `used_memory > maxmemory`: pick a victim per policy, delete it,
  subtract its size, `evicted_keys++`. Give up after a bounded number of attempts to
  avoid stalling the loop on a pathological keyspace; if still over and policy is
  `noeviction` (or volatile-with-no-volatiles), the triggering write returns
  `-OOM command not allowed when used memory > 'maxmemory'`.
- **Async free:** route large-value evictions through the existing `asyncdel`/thread
  pool path so freeing a huge hash/zset doesn't stall the loop (same mechanism as
  `UNLINK`).
- **AOF / persistence correctness (critical):** an eviction is a data change. It must
  be **propagated as an explicit `DEL key` to the AOF** (and later to replicas in v10)
  so the log doesn't replay the evicted key back into existence. Feed it through the
  same `aof_feed`/`aof_append_raw` path used by real `DEL`, gated by `g_loading` so
  replay itself never evicts-and-logs.
- **Don't evict when:** `g_loading` is true (startup replay), inside the BGSAVE/AOF
  fork child, or `maxmemory == 0`.

### ✅ Step 6 — Introspection commands (DONE 2026-07-03)

`MEMORY USAGE` (→ `entry_mem_usage`; accepts `[SAMPLES n]` but computes exact — the
estimator is already a cheap single pass), `MEMORY DOCTOR` (release-safe counter-vs-sweep
drift check), `MEMORY STATS` (flat array). `OBJECT ENCODING` (honest MYRED names
`raw`/`int`/`deque`/`hashtable`/`skiplist`), `IDLETIME` (non-LFU only), `FREQ` (LFU only),
`REFCOUNT` (stub `1`). `lookup_any` = non-touching, type-agnostic lookup so `IDLETIME`
doesn't reset the value it reports. Missing key: `USAGE`→nil, `OBJECT`→error. Compact
encodings + real object sharing deferred — see **General upgrades**.

- `MEMORY USAGE key [SAMPLES count]` → `entry_mem_usage(ent)`; for big aggregates,
  sample `count` elements (default 5, `0` = exact) and extrapolate, matching Redis.
- `MEMORY STATS` → array of internal figures (used, overhead, keys count, …).
- `MEMORY DOCTOR` → human string; reuse the Step 1 counter-vs-sweep self-check.
- `OBJECT ENCODING key` → report our encoding names. We don't have Redis's listpack/
  intset/skiplist duality, so map honestly: string→`raw`/`int` (if `str2int` fits),
  list→`deque`, hash→`hashtable`, set→`hashtable` (or `intset` if all-integer, if we
  ever add that), zset→`skiplist`. Document that these are MYRED encodings.
- `OBJECT IDLETIME key` → `(g_lru_clock - ent->lru)` in seconds (LRU policies).
- `OBJECT FREQ key` → the LFU counter (LFU policies only; error otherwise, like Redis).
- `OBJECT REFCOUNT key` → we don't share objects, so always `1` (stub for compat).

### Optimization / robustness summary

| Concern | Approach |
|---|---|
| Accounting cost | Incremental global counter at alloc choke points — never walk the keyspace to answer `maxmemory` |
| `MEMORY USAGE` cost | Sample large aggregates (`SAMPLES`), don't sum every element |
| LRU memory overhead | 4 bytes/key, sampled eviction — no global linked list |
| LRU clock cost | One cached clock per loop tick, not a syscall per access |
| Eviction sampling cost | 16-slot eviction pool amortized across rounds; TTL heap serves `volatile-ttl` near-free |
| Large-value eviction stall | Async free via the existing thread pool (`asyncdel`) |
| AOF divergence | Evictions propagate an explicit `DEL` to the AOF; suppressed during `g_loading` |
| Counter drift | Debug self-check (`MEMORY DOCTOR`) compares counter to a full sweep |
| Fork safety | No eviction inside the BGSAVE/rewrite child |
| Volatile starvation | `volatile-*` with no TTL keys falls back to `-OOM`, never evicts persistent keys |
| Loop starvation | Bounded eviction attempts per write; give up → `-OOM` rather than spin |

### Suggested testing

- `test_maxmemory.sh`: set a low `MYRED_MAXMEMORY`, flood keys, assert `used_memory`
  stays bounded and `evicted_keys` climbs; assert `noeviction` returns `-OOM`.
- Per-policy victim-selection unit checks (fill with known idle/freq/TTL spreads).
- AOF-eviction regression: evict under load, restart, assert evicted keys stay gone
  (the `DEL` propagation actually took).
- Accounting regression: build a mixed dataset, compare the counter to a full
  `entry_mem_usage` sweep (drift == bug).

## v8 — Pub/Sub & Transactions

- `SUBSCRIBE` / `UNSUBSCRIBE` / `PUBLISH` + pattern subscriptions
- `MULTI` / `EXEC` / `DISCARD` / `WATCH` — transactions

## v9 — Security & auth

> **Note: doing v9 before v8.** Hardening the server (real config, hashed auth, ACLs,
> network exposure control, TLS) is higher-value than Pub/Sub right now.

Goal: take MYRED from "one plaintext password, binds to `0.0.0.0`, everyone who
authenticates is root" to a real security model — a config file, hashed credentials,
multi-user ACLs with per-command/per-key permissions, network exposure control, and
TLS. Today's baseline: `do_auth` compares `cmd[1] == g_config.password` (plaintext,
non-constant-time), `conn->authenticaded` is a single bool, the listener is hardcoded
to `INADDR_ANY`, and there's no notion of users or per-command permissions. Do the
steps in order — the config file is the foundation everything else is declared in.

### ✅ Step 1 — `redis.conf`-style config file (foundation) (DONE 2026-07-04)

Shared `config_apply()` (state.cpp) is the single directive→`Config` mapper, reused by
both `config_load_file` and `CONFIG SET` so they can't drift; `config_tokenize` handles
`#` comments + quoted values; `config_load_file` fails with `file:line` on bad/unknown
directives (file `save` lines replace defaults); `config_rewrite` serializes back for
`CONFIG REWRITE`. `main()` loads the file first (argv first-non-flag or `MYRED_CONFIG`),
then env overrides, then the `CONFIG SET` runtime layer → precedence
`defaults < file < env < CONFIG SET`. Added `Config::port` + `Config::config_path`.
Bugs found in transcription: leftover hardcoded `password = kek1234` line (clobbered the
file), and a `line[++i]` pre-increment in the tokenizer that dropped each token's first
char. `CONFIG GET` full-table extension left as a mechanical follow-up.

Turn the scattered `MYRED_*` env knobs + the v6.1/v7 `CONFIG` stub into a real config layer.

- **Parser:** `--config <path>` (or `MYRED_CONFIG`); line-based `directive arg [arg…]`,
  `#` comments, quoted values. Map to `Config` fields: `requirepass`, `port`, `bind`,
  `maxmemory`/`maxmemory-policy`, `save`, `appendonly`/`appendfsync`, `dir`, `logfile`,
  `loglevel`, plus the v9 ones below.
- **Precedence:** defaults → config file → env → runtime `CONFIG SET`. Load the file in
  `main()` *before* opening sockets; keep env as overrides for backward compat.
- **`CONFIG REWRITE`** (currently a stub `+OK`): serialize live `Config` back to the file,
  preserving comments where practical. Extend `CONFIG GET/SET` from the v7 two-parameter
  set to the full table.
- **Robustness:** validate on load (port range, enum values, sizes via `parse_memory_size`);
  reject unknown directives with `file:line` context rather than silently ignoring.

### ✅ Step 2 — Password hashing + constant-time compare (DONE 2026-07-04)

Header-only `sha256.h` (`sha256_hex`, `ct_equal`, `secure_zero`). `g_config.password`
now holds the **SHA-256 digest** (or empty for no-auth), never plaintext: `config_apply`
hashes `requirepass` (also accepts an empty value and a pre-hashed `#<hex>` form),
`config_rewrite` emits the digest as a quoted `"#<hex>"`, and the `MYRED_PASSWORD` env +
`kek1234` default are hashed too. `do_auth` hashes the supplied password and
**constant-time compares** (`ct_equal`, no early return), then `secure_zero`s the
plaintext out of the request buffer. The argon2/bcrypt upgrade is tracked as its own
hardening step below (before TLS).

- Replace the plaintext `==` with a **hash compare**. Store `requirepass` as a **SHA-256**
  digest (hand-rolled or small vendored impl, matching the from-scratch ethos); config
  accepts a plaintext password (hashed at load) or a pre-hashed value.
- **Constant-time comparison** (XOR-accumulate over the full digest, no early return) —
  the current `==` leaks match length via timing. This is the single most important
  robustness fix in v9.
- **Upgrade path:** note `argon2`/`bcrypt` (salted, memory-hard) as the real answer for
  credentials at rest — needs a library; SHA-256 is the baseline.
- **Hygiene:** `explicit_bzero` the plaintext buffer after hashing; never log passwords;
  keep the existing max-3-attempts disconnect and add a small fixed backoff on failure.

### ✅ Step 3 — Protected mode + `bind` + IP allowlist (DONE 2026-07-04)

All enforced in `handle_accept` **before** a `Conn` is allocated. `ip_allowed`
(CIDRs pre-parsed to `(net,mask)` at config load; loopback always allowed to prevent
lockout) + protected mode (no password + non-loopback peer → `-DENIED` + close).
Rejections `return 0` so the accept loop keeps draining other pending peers. `bind`
now honors `Config::binds` (multiple listen fds — see below); `protected-mode` and the
MYRED `allow-ip <cidr>` directives added to `config_apply`.

All checked at `handle_accept` (before auth), so unauthorized peers never reach the loop.

- **Protected mode** (Redis default): if no `requirepass` *and* bound to a non-loopback
  address, refuse non-loopback peers with an explanatory error. Check the peer `sockaddr`.
- **`bind`:** listen on specific interfaces instead of the hardcoded `INADDR_ANY`; support
  multiple bind addresses (a listen fd per address, all in the `poll` set).
- **IP allowlist:** optional CIDR list matched against the peer IP at accept; reject + log
  others. Cheap — a handful of prefix compares.

### Step 4 — ACL system (multi-user, command + key permissions)

The big one. A `User` = name, enabled flag, password hash(es), allowed **command
categories/commands**, allowed **key glob patterns**, allowed pub/sub channels.
Builds directly on Step 2 (`sha256_hex`/`ct_equal`/`secure_zero` in `shah256.h`) and
Step 1's config parser (`config_apply`/`config_tokenize` in `state.cpp`). Do the
sub-steps in this order — data model first, since enforcement and parsing both need it.

#### 4a — `User` struct + registry

- New `struct User { std::string name; bool enabled = true; std::vector<std::string> pw_hashes; uint64_t allow_cats = 0; std::unordered_map<std::string,bool> cmd_overrides; std::vector<std::string> key_patterns; bool all_keys = false; }`
  (multiple `pw_hashes` so a password can be rotated without a moment of lockout —
  `AUTH` matches against any of them via `ct_equal`).
- **Registry gotcha:** store users in `std::unordered_map<std::string, User>`, not
  `std::vector<User>`. `Conn` will hold a raw `User *` (below); a `vector` reallocates
  and invalidates every live `Conn::user` pointer the moment `ACL SETUSER` adds a user
  while other clients are connected — a dangling-pointer bug that won't show up until
  a second client connects mid-session. A `Config`-owned `unordered_map` gives stable
  element addresses across inserts (erase is the only thing to still be careful about —
  `ACL DELUSER` on a currently-connected user needs to null out or kick that `Conn`).
- Pre-populate a `default` user at boot: `allow_cats = CAT_ALL`, `all_keys = true`,
  `pw_hashes = {g_config.password}` (today's `requirepass`, already a SHA-256 digest
  after Step 2) — this is what makes the feature backward compatible; a config with no
  `user` directives behaves exactly like today.

#### 4b — Command categories on `CmdSpec`

- `CmdSpec` (`commands.cpp:2768`) currently has `fn, min_args, max_args, is_write,
  aof_rewrite`. Add `uint64_t acl_cats = 0`. Define category bits as an enum/constexpr
  set: `CAT_READ, CAT_WRITE, CAT_ADMIN, CAT_KEYSPACE, CAT_DANGEROUS, CAT_FAST, CAT_SLOW, …`.
- **Don't hand-retype `@read`/`@write` for all 82 entries in `k_cmd_table`**
  (`commands.cpp:2776-2880`) — derive it: `spec.is_write ? CAT_WRITE : CAT_READ` is
  already known per-command via the existing `is_write` flag, computed once at table-
  build time (or a small init pass over the table). Only the extra categories
  (`@admin`, `@dangerous`, `@keyspace`) need manual per-entry tagging, and
  `cmd_can_grow_memory`'s `no_grow` set (`commands.cpp:2883-2894`) is a ready-made
  starting list for what counts as `@dangerous`-adjacent — same shape of "special
  commands" set, don't invent a second taxonomy from scratch.
- **Rule compilation semantics:** Redis ACL rules apply left-to-right, last match wins
  (`+@read -@write +get -flushall` → category grants, then a specific command name can
  override its own category). For this project's scope, a reasonable simplification
  (not full Redis fidelity, worth stating as a deliberate choice): keep `allow_cats` as
  one bitset plus a small `unordered_map<string, bool> cmd_overrides` for explicit
  `+cmd`/`-cmd` rules, checked *before* falling back to the category bitset. `ACL
  SETUSER` parses the rule tokens once and populates both.

#### 4c — Enforcement in `do_request`

Insert the check in the existing flow (`commands.cpp:2896-2925`) right after the arity
check and before `spec.fn(...)` — auth itself stays exempt exactly like the current
`if (cmd[0] == "auth")` bypass at line 2907:
- (a) **Command permitted?** `cmd_overrides` lookup first, else `spec.acl_cats & conn->user->allow_cats`. Reject `NOPERM` otherwise.
- (b) **Key patterns?** Every key argument must match one of `conn->user->key_patterns`
  via the existing `glob_match` (`commands.cpp:1550`) unless `all_keys`. This needs a
  **key-position descriptor** per command, since key args aren't uniformly at `cmd[1]`
  (`GET`/`SET`: just `cmd[1]`; `DEL`/`EXISTS`/`MGET`: every arg from 1; `MSET`: every
  *other* arg from 1). Add a small `KeySpec` enum to `CmdSpec` (`NONE, FIRST,
  ALL_FROM_1, STRIDE2_FROM_1`) alongside `acl_cats` so this is an O(1) table lookup
  per command, not per-command special-casing in `do_request`.
- (c) Pub/sub channel patterns — no-op stub until v8 exists; keep the field on `User`
  now so the config/parsing format doesn't need to change again later.

#### 4d — `AUTH` 2-arg form + `ACL` commands

- `do_auth` (`commands.cpp` ~1010) currently only handles `AUTH <pass>` against
  `g_config.password`. Extend to `AUTH <user> <pass>` (2-arg) vs `AUTH <pass>` (1-arg =
  implicit `default` user) — look up the `User` by name in the registry, `ct_equal`
  against each of its `pw_hashes`, reusing the exact same hash/compare/wipe path
  already written for Step 2.
- New commands: `ACL SETUSER/GETUSER/DELUSER/LIST/USERS/WHOAMI/CAT/GENPASS` — dispatch
  style matches the existing `CONFIG GET/SET` sub-dispatch (`do_config`), i.e. one
  `do_acl(cmd, out, conn)` entry in `k_cmd_table` that switches on `cmd[1]`. `WHOAMI` →
  `conn->user->name`; `GENPASS` → random hex via the project's existing `mt19937_64` RNG
  (from the v5.3 review), not `rand()`.

#### 4e — Per-`Conn` auth state

- Replace `Conn::authenticaded` (`state.h:83`) with `User *user = nullptr;` — `nullptr`
  means unauthenticated. `do_request`'s `if (!conn->authenticaded)` (line 2912) becomes
  `if (!conn->user)`. The per-command permission check is then an O(1) bitset test, no
  re-hash/re-parse per request.

#### 4f — Config file persistence

- `user` directives in `myred.conf`, parsed via a new helper alongside `config_apply`
  (`state.cpp:81`) — this needs its own mini-parser, not a reuse of the generic
  `name/args` shape every other directive uses, because the rule grammar is rich:
  `user alice on >5f4dcc... ~cache:* +@read +@write -flushall`. Tokenize with the
  existing `config_tokenize` (handles quoting already), then walk tokens applying
  `on`/`off`, `>hash`/`<hash` (add/remove password), `~pattern` (key pattern, `~*` =
  `all_keys`), `+@cat`/`-@cat`/`+cmd`/`-cmd` in order. Same multi-token-per-line shape
  you just fixed for `bind` (`state.cpp:119-123`) — look at that fix before writing
  this parser, since the "collect all args, don't assume exactly one" pattern applies
  here too, just with a richer per-token grammar than a flat address list.

### Step 5 — Command hardening + audit log

- **`rename-command`** (config): rename or disable dangerous commands (`rename-command
  FLUSHALL ""` disables; renaming `CONFIG` to a secret name). Requires building
  `k_cmd_table` at boot (it's `const` today) so keys can be mutated/removed.
- **Audit log:** timestamp + peer IP for AUTH failures, ACL changes, and
  `@admin`/`@dangerous` command use → a file (ties into the Continuous "logging
  framework" item). A disabled command must also drop out of the AOF/replication path.

### Step 6 — Password hashing upgrade: argon2/bcrypt (do before TLS)

Optimization/hardening of Step 2. SHA-256 is fast and unsalted → weak against offline
brute-force if the config/ACL file leaks. Upgrade credential-at-rest to a **salted,
memory-hard** KDF.

- **argon2id** (preferred) or **bcrypt** — needs a library (`libargon2` / `libbcrypt`),
  the first external dependency in v9 (hence *before* TLS/OpenSSL, so the build gains one
  dep at a time).
- Store `$argon2id$…` / `$2b$…` PHC-format strings (they embed salt + params) in place of
  the bare SHA-256 hex; `secure_zero`/`ct_equal` and the `do_auth` flow are unchanged —
  only the hash/verify calls swap. Per-user params (cost/memory) live in the ACL entry.
- Keep SHA-256 verification for backward compat so existing `#<hex>` configs still load;
  re-hash to argon2 on next `CONFIG SET requirepass` / `ACL SETUSER`.

### Step 7 — TLS (heaviest; could be its own milestone)

- Link **OpenSSL**. `tls-port`, `tls-cert-file`/`tls-key-file`/`tls-ca-cert-file`;
  optional **mutual TLS** (require + verify client certs).
- **Event-loop integration (the hard part):** wrap per-`Conn` I/O in an `SSL*`; the
  non-blocking handshake and `SSL_read`/`SSL_write` return `SSL_ERROR_WANT_READ/WRITE`,
  which must drive the `poll` interest flags (`want_read`/`want_write`). Abstract `Conn`
  read/write behind a plain-vs-TLS function pointer so the loop stays protocol-agnostic.
- **Robustness:** handshake timeouts, `SSL_shutdown` on close, cert reload without restart.

### Optimization / robustness summary

| Concern | Approach |
|---|---|
| Timing side-channel on password | Constant-time XOR-accumulate compare — never `==` / early return |
| Password at rest | SHA-256 baseline (→ argon2/bcrypt), salted; `explicit_bzero` plaintext; never logged |
| Per-command auth cost | User resolved once at AUTH; command check is an O(1) bitset test; key patterns via existing `glob_match` |
| Command category lookup | Precomputed `acl_cats` bitflags in `CmdSpec`, not recomputed per request |
| Unauthorized peers | Rejected at `accept()` (protected mode / bind / allowlist) before the command loop |
| Config precedence | defaults < file < env < `CONFIG SET`; `CONFIG REWRITE` persists live state |
| TLS without blocking | `SSL_ERROR_WANT_*` drives poll interest; `Conn` I/O behind a plain/TLS abstraction |
| Dangerous commands | `rename-command` disables/renames at boot; audit log for `@admin`/`@dangerous` |
| Fail-safe defaults | Protected mode when no password; ability to disable the `default` user |

### Suggested testing

- **Auth:** `AUTH` right/wrong/2-arg-user; wrong password still disconnects after 3 tries.
- **ACL:** `SETUSER`/`GETUSER` round-trip; `NOPERM` on a denied command and on a key
  outside the user's pattern; `default` user preserves legacy behavior.
- **Config:** bad directive → `file:line` error; `CONFIG REWRITE` → restart → values survive.
- **Protected mode:** no-password + remote connect refused; loopback allowed; allowlist hit/miss.
- **Password hashing:** stored value is a digest, not plaintext; (optional) statistical
  timing test that compare time is independent of how many leading chars match.
- **TLS:** `openssl s_client` / `redis-cli --tls` handshake; mutual TLS with and without a
  client cert.

## v10 — Replication & HA

- Master-replica: `PSYNC`, replication backlog, partial resync
- Sentinel-style failover; cluster mode / hash-slot sharding

---

## General upgrades & unsupported features (backlog)

Cross-cutting improvements and known gaps surfaced while building v1–v7. Not tied to a
single version — pull each into a milestone when it becomes worthwhile. Nothing here is
required for correctness; it's the difference between "works" and "Redis-grade".

### Compact encodings (memory optimization)

The single biggest memory win we don't have. Redis stores *small* collections in flat,
cache-friendly layouts and only converts to the heavyweight structure past a size/element
threshold. MYRED always uses the heavyweight structure — `OBJECT ENCODING` reflects this
honestly today (`raw`, `deque`, `hashtable`, `skiplist`).

- **`embstr` / `int` for strings** — small strings allocated inline with the object;
  integer values stored as `int64`, not text. (We *detect* `int` in `OBJECT ENCODING`
  but still store the digits as a `std::string`.)
- **`listpack`** — small hashes / zsets / lists as one contiguous byte blob instead of
  HMap (+AVL) / deque. Thresholds: `hash-max-listpack-entries/value`,
  `zset-max-listpack-entries/value`, `list-max-listpack-size`.
- **`intset`** — all-integer sets as a sorted packed integer array (no HMap).
- **`quicklist`** — lists as a linked list of listpacks (we use one ring-buffer deque).
- Requires: an encoding tag per `Entry`, conversion on threshold crossing, and
  encode/decode paths in RDB **and** AOF. Also unlocks accurate small-object accounting.

### Object sharing / real refcount

- Shared-integer pool (Redis shares small ints 0–9999) so `OBJECT REFCOUNT` returns real
  counts — we stub `1`. Saves memory on integer-heavy datasets. Needs a refcount field, a
  shared-object table, and copy-on-mutate.

### Command coverage gaps (all unimplemented; * = commonly requested)

- **Sorted set (biggest gap — we only have zadd/zrem/zscore/zrank/zquery/zpopmin):**
  `ZINCRBY`*, `ZCARD`*, `ZCOUNT`, `ZMSCORE`, `ZPOPMAX`, `ZRANGEBYSCORE`*/`ZRANGEBYLEX`,
  `ZREVRANGE`*, `ZREMRANGEBYRANK/SCORE/LEX`, `ZUNIONSTORE`/`ZINTERSTORE`/`ZDIFFSTORE`,
  `ZRANDMEMBER`, `ZSCAN`, `ZLEXCOUNT`, `ZRANGESTORE`, `ZMPOP`.
- **String / bitmap:** `SETBIT`/`GETBIT`/`BITCOUNT`/`BITPOS`/`BITOP`/`BITFIELD`, `SUBSTR`, `LCS`.
- **Generic:** `COPY`, `SORT`/`SORT_RO`, `DUMP`/`RESTORE`, `EXPIRETIME`/`PEXPIRETIME`,
  `OBJECT HELP`, `SCAN ... TYPE`, `WAIT`.
- **Hash:** `HRANDFIELD`, `HINCRBYFLOAT`.
- **List:** `LPOS`, `LMOVE`/`RPOPLPUSH`, `LMPOP`, and blocking `BLPOP`/`BRPOP`/`BLMOVE`
  (needs blocking-client machinery — a substantial addition to the event loop).
- **Set:** `SINTERCARD`.
- **New data types (each a large effort):** HyperLogLog (`PF*`), Streams (`X*`),
  Geo (`GEO*`), Bitmaps (as above).

### Server / observability & tooling

- `COMMAND` / `COMMAND DOCS` / `COMMAND COUNT` (redis-cli interactive probes these).
- `CLIENT LIST`/`KILL`/`SETNAME`/`GETNAME`/`ID`, `HELLO` (RESP3 handshake), `RESET`.
- `SLOWLOG`, `LATENCY`, `MONITOR`, `DEBUG`, graceful `SHUTDOWN`, `LASTSAVE`, `TIME`, `LOLWUT`.
- Full `CONFIG GET/SET` coverage (today only `maxmemory` + `maxmemory-policy` are real) +
  `CONFIG REWRITE` to a `redis.conf`.
- **RESP3** (`HELLO 3`) — maps, push frames, attributes; our parser/writers are RESP2 only.

### Correctness follow-ups (small, known)

- `zrem` doesn't drop an emptied zset (Redis removes the key; `zpopmin` already does).

### Design decisions (deliberate, not gaps)

- **Eviction: best-of-N, not the 16-slot pool.** We keep `evict_pick_victim`'s
  best-of-`maxmemory_samples` sampling (tunable; default raised to 10) rather than
  Redis's persistent 16-slot eviction pool. Best-of-N is correct and was Redis's own
  approach pre-3.0; the pool only sharpens victim quality at cache scale, and a
  persistent pool would force key-based storage + stale-entry validation (a pooled
  candidate can be async-freed between rounds) — real complexity for marginal gain at
  this project's scale. Revisit only if a real cache workload shows poor hit rates.

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
