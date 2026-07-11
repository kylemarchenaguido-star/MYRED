# MYRED — Full Codebase Review (v5.3)
redis-benchmark -p 1234 -a kek1234 -t set,get,incr,lpush,rpush,sadd,hset -n 200000 -c 50 -P 16 -q
Project-wide pass over every `.cpp`/`.h` file. Each item has a **location**, a
**what**, and a **why**. Grouped by severity, then by file.

Severity legend:
- 🔴 **CRITICAL** — crash, memory corruption, or silent data loss.
- 🟠 **BUG** — observably wrong behavior.
- 🟡 **ROBUSTNESS** — edge cases, hardening, latent hazards.
- 🔵 **OPTIMIZATION** — performance / memory.
- ⚪ **CLEAN CODE** — naming, dead code, consistency, style.

---

## Post-v9 Backlog Audit — 2026-07-07

This pass is a new audit addendum for work to do after the v9 security milestone.
It focuses on robustness, persistence correctness, memory accounting, optimization,
and code-shape issues that are worth tracking before the next large feature push.

### 0. Highest-priority correctness items

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🔴 | `aof.cpp:225-250`, `commands.cpp:3151` | AOF replay builds a fake authenticated `Conn`, but never assigns `fake.user`. `do_request()` now runs `acl_check()` after the auth gate, so replayed commands can be rejected with `NOPERM` and silently not rebuild the dataset. | During `g_loading`, either set `fake.user = &g_config.users["default"]` or explicitly bypass ACL checks for trusted local replay. Add an AOF restart test after ACL is enabled. |
| 🔴 | `aof.cpp:273-286` | `aof_check()` continues after `fopen()` failure and then calls `fseek()` on a null `FILE*`. The `fread()` condition is also inverted: a successful non-empty read is treated as "short read". | Return `false` immediately on open failure. Compare `fread(...) != (size_t)sz`, matching `aof_load()`. Add a `--check-aof` regression test with a valid non-empty file. |
| 🔴 | `commands.cpp:2485-2518` | `SMOVE` can call `mem_reaccount(dst_ent)` while `dst_ent == nullptr` when moving from a multi-member source set into a missing destination. It also reaccounts the wrong entry: the source shrank, not the missing destination. | Reaccount `src_ent` after removal when it survives; create `dst_ent` before destination reaccounting. Add tests for `SMOVE src missing-dst member` with source size 1 and >1. |
| 🔴 | `commands.cpp:966-1007`, `commands.cpp:3050` | `ZPOPMIN` mutates data but is registered without `is_write=true`. It will be categorized as read, skipped by AOF logging, skipped by write-side maxmemory/MISCONF gates, and its ACL category is wrong. | Mark the table row as write. Because it removes members, also make sure dirty counting and AOF replay cover it. |
| 🟠 | `commands.cpp:326-367`, `commands.cpp:3170-3184` | `GETEX PERSIST` and future-time `GETEX EX/PX/EXAT/PXAT` change TTL state but do not increment `g_writes_since_save`. Because AOF logging is mutation-gated by that counter, these TTL updates are not logged or saved. Only the delete-on-past path bumps the counter. | Bump the dirty counter when TTL state changes. Keep bare `GETEX key` read-only. Add AOF/RDB restart tests for `GETEX key PX ...` and `GETEX key PERSIST`. |
| 🟠 | `commands.cpp:837-850`, `commands.cpp:966-1007` | Sorted-set removals do not keep memory accounting stable. `ZREM` never calls `mem_reaccount()` and still leaves an empty zset key; `ZPOPMIN` reaccounts only on no-op or full key deletion, not when the zset survives with fewer members. | Reaccount surviving zsets after member removal and drop empty zsets consistently. This overlaps the roadmap's known `zrem` empty-key follow-up. |
| 🟠 | `state.cpp:471-479` | `parse_cidr()` validates `bits`, which is still the default `32`, instead of validating `parsed`. Values like `/33` can pass validation and then shift by a negative amount in `0xFFFFFFFFu << (32 - bits)`. | Validate `parsed < 0 || parsed > 32` before assigning `bits`. Add config-load tests for `/0`, `/32`, `/33`, and `/-1`. |

### 1. Persistence and durability hardening

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🟠 | `commands.cpp:1053-1064`, `server.cpp:655-664` | Configurable `dbfilename` is not honored everywhere. `SAVE` and shutdown still call `rdb_save("dump.rdb")`, while background save uses `g_config.dump_path`. `do_save()` also calls `rdb_on_save_complete()` after `rdb_save()` already did. | Route all save paths through `g_config.dump_path`. Keep save-completion bookkeeping in one place. |
| 🟡 | `aof.cpp:135`, `aof.cpp:150-164` | AOF rewrite uses hardcoded `appendonly.aof.tmp`, even when `g_config.aof_path` points elsewhere. The final `rename()` return value is ignored, and the temp file may not be in the same directory as the target, which weakens atomic replacement. | Build the temp path beside `g_config.aof_path`, check every write/fsync/rename result, and leave the old AOF fd untouched if finalization fails. |
| 🟡 | `aof.cpp:149-176` | If appending the rewrite delta fails before all bytes are written, the code still fsyncs, renames, repoints the fd, and reports success. | Track whether the full delta was appended. Treat short delta append, fsync failure, rename failure, or reopen failure as rewrite failure. |
| 🟡 | `rdb.cpp:350-351`, `rdb.cpp:886-900` | RDB temp path construction uses fixed 256-byte stack buffers and `snprintf()` without checking truncation. A long configured dump path can produce a truncated temp filename. | Use `std::string tmp = filename + ".tmp." + pid` before fork-time handoff, or validate `snprintf()` length before use. |
| 🟡 | `server.cpp:488-494` | Startup logs AOF load failure but continues serving with whatever partial state was loaded before failure. For a durability system, "failed to load appendonly.aof" should be a hard startup failure unless an explicit recovery flag is used. | Decide policy: fail fast on AOF load failure, or clearly document "best effort" recovery and add operator warnings. |

### 2. ACL and config robustness

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🟠 | `commands.cpp:2822-2825`, `commands.cpp:3211-3231` | ACL category checks use "any overlapping bit grants access". Because `acl_init_categories()` derives `CAT_READ` or `CAT_WRITE` for every command and ORs extra bits on top, admin commands like `ACL` and `CONFIG` can be allowed by `+@read` unless separately denied. The `acl` key-spec is now correctly `NONE`, but the category gate is still too permissive. | Add required-category semantics for admin/dangerous commands, or stop giving those commands the broad read/write base bit. Test a user with only `+@read ~*`. |
| 🟡 | `commands.cpp:1010-1023` | `do_auth()` copies the plaintext password into local `std::string pass` and wipes only the `cmd` copy. The local copy remains in memory until destruction and may keep capacity contents. | Hash directly from `cmd[...]` or call `secure_zero()` on `pass` before every return path after hashing. |
| 🟡 | `state.cpp:128-147`, `state.cpp:185-190`, `server.cpp:474-476` | Several config/env parsers use `atoi()` or loose boolean checks. Inputs like `123abc`, `yesplease`, or unknown `MYRED_AOF` values can be accepted silently. | Create shared strict parsers for int ranges and yes/no booleans. Use them for file config, env overrides, and `CONFIG SET`. |
| 🟡 | `commands.cpp:2557-2563` | `CONFIG SET` treats unknown directives as `+OK` because only `CfgResult::BADVALUE` is converted to an error. That preserves old tooling compatibility, but it makes real runtime config misleading once v9 depends on it. | Either return an error for `CfgResult::UNKNOWN` after v9, or explicitly keep a compatibility allowlist for known Redis probes. |
| 🟡 | `state.cpp:303-319` | `CONFIG REWRITE` serializes only a subset of live config. It drops `bind`, `protected-mode`, `allow-ip`, ACL users, AOF rewrite knobs, LFU knobs, and any future TLS/security settings. | Treat rewrite as incomplete until the config table is centralized. Post-v9, prefer a directive registry with get/set/rewrite callbacks. |
| ⚪ | `commands.cpp:2964-2994` | `ACL GETUSER` / `ACL LIST` report lossy placeholders like `+<cats>` and `#<hash>`, so they cannot round-trip the configured user state. | This matches the roadmap's design-decision note. Store raw ACL rule tokens alongside compiled permissions if faithful output becomes important. |

### 3. Parser and protocol robustness

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🟡 | `resp.cpp:71-75` | RESP bulk length accumulation can signed-overflow before the `str_len > k_max_msg` check runs. `n_args` has a pre-multiply guard; bulk length should match that pattern. | Guard before multiply/add, or parse into `uint64_t` with a max check at every digit. Fuzz this parser. |
| 🟡 | `commands.cpp:49-52` | `str2int()` ignores `errno`, so `strtoll()` overflow is accepted as `LLONG_MAX` or `LLONG_MIN`. TTL math and numeric commands then operate on clamped values as if the input was valid. | Clear/check `errno == ERANGE`; reject overflows. Consider one shared `parse_i64()` helper for config and command args. |
| 🟡 | `commands.cpp:352-355`, `commands.cpp:2699-2725` | TTL conversions multiply user-controlled seconds by `1000` without overflow checks in live handlers and AOF rewrite translation. | Add `mul_overflow` / range checks before converting seconds to milliseconds. Reject impossible timestamps consistently. |
| 🟡 | `resp.cpp:16-39` | Inline protocol support splits only on whitespace. This is enough for `PING_INLINE`, but it does not support Redis inline quoted strings or escapes. | Fine for benchmark compatibility, but document the subset or implement quoted inline parsing if interactive compatibility matters. |

### 4. Memory accounting and eviction follow-ups

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🟡 | `state.cpp:436-443` | `mem_reaccount()` relies on the invariant `used_memory >= ent->mem`. A missed reaccount can make the later subtract underflow in release builds. Debug self-check detects drift, but not underflow. | Add a release-safe guard or assertion strategy around subtraction. Make memory drift tests cover every mutating command family. |
| 🟡 | `commands.cpp:2202-2214` | `SADD` bumps dirty state and logs to AOF even when all members already exist. Similar no-op write inflation exists in a few handlers. | Track whether the command actually changed state before bumping `g_writes_since_save`. This keeps AOF cleaner and auto-save triggers meaningful. |
| 🟡 | `commands.cpp:2452-2482` | `SINTERSTORE` / `SUNIONSTORE` / `SDIFFSTORE` always leave a destination set entry, even when the result is empty. Redis deletes the destination for empty store results. | Decide whether to match Redis. If yes, delete the destination when result size is zero and return `0`. |
| 🔵 | `state.cpp:358-380` | `entry_mem_usage()` walks entire aggregate values. That is correct for exact accounting, but expensive when every write to a large collection calls `mem_reaccount()`. | Long-term: maintain per-container memory deltas at mutation sites, or store aggregate byte totals inside `EntryHash`/`EntrySet`/`ZSet`/`Deque`. |
| 🔵 | `commands.cpp:2759-2787` | Eviction runs up to 100 victims synchronously in the write path. A keyspace with many small keys over the limit can still stall one command. | Consider budgeting by elapsed time or bytes freed per command, then returning OOM if the budget is exhausted. |

### 5. Performance and code-shape improvements

| Severity | Location | Problem | Suggested direction |
|---|---|---|---|
| 🔵 | `commands.cpp:2163-2174` | `SUNION` deduplicates by collecting all members, sorting, then unique-ing. This is O(N log N) and copies every string. | Use a temporary HMap/string set for O(N) dedupe, or emit through a scratch `EntrySet`-like structure. |
| 🔵 | `commands.cpp:2286-2377` | `SPOP` and `SRANDMEMBER` collect every set member into a vector before sampling. That is expensive for large sets when only one or a few members are needed. | Add HMap random sampling or reservoir helpers for nested set HMaps. Reuse the keyspace random sampler idea. |
| 🔵 | `commands.cpp:2078-2129`, `commands.cpp:2452-2482` | Set store commands compute into `std::vector<std::string>` and then insert into a new set, creating duplicate copies. | Build the destination set directly from the operation result, or use move-aware temporary nodes. |
| 🔵 | `commands.cpp:888-963` | `ZQUERY` / `ZREVQUERY` copy results into a vector of `{string, double}` before emitting. RESP needs the count first, but the count can often be derived with a bounded walk before emission. | Two-pass count+emit can avoid storing all returned member names. Keep the vector path if the second tree walk costs more in practice. |
| 🔵 | `commands.cpp:1118-1188` | `INFO` does an O(N) keyspace scan every call for key counts and TTL counts. On a large DB this can become an observability-induced stall. | Maintain live key counters by type/TTL, or make expensive counts optional. |
| ⚪ | `hash.cpp:6-18` | `hash_set()` takes `const std::string value` and then `std::move(value)`, which cannot move from a const object. It copies on both insert and update paths. | Take `std::string value` by value, then move into the node, or take `std::string_view`/`const std::string&` deliberately. |
| ⚪ | `common.h:8-16` | The pointer-to-member `container_of()` avoids the old macro, but it computes offsets through a fake object in raw storage. This is still subtle enough to deserve either tests or a simpler standard-layout-only implementation. | If all intrusive nodes are standard-layout, use an `offsetof`-style helper with explicit constraints and static assertions. |

### Suggested post-v9 audit/test order

1. Persistence correctness: AOF replay with ACL enabled, `--check-aof`, `GETEX` TTL replay, `ZPOPMIN` replay.
2. Crashers/accounting: `SMOVE` missing destination, `ZREM`/`ZPOPMIN` memory drift, CIDR bad prefixes.
3. Config/security semantics: strict parsers, `CONFIG SET UNKNOWN`, ACL admin-category gating, `AUTH` plaintext wiping.
4. Path correctness: `dbfilename`, `appendfilename`, rewrite temp files, shutdown save path.
5. Performance passes: set sampling, set algebra dedupe, exact memory reaccount cost, `INFO` O(N) counters.

---

## Full-Codebase Audit — 2026-07-09

Scope: every `.cpp`/`.h` in the repo (data structures, server loop, persistence,
commands, ACL, crypto helper, client tool). Two parts: (A) status recheck of the
2026-07-07 table, (B) new findings. Line numbers are against the working tree of
2026-07-09.

### A. Status of the 2026-07-07 findings

Verified **fixed** since the last pass:

| Old item | Status |
|---|---|
| AOF replay never assigned `fake.user` | Fixed in shape (`aof.cpp:229-234` builds a synthetic replay user) — but **incompletely**; superseded by new 🔴 N1 below. |
| `SMOVE` null `dst_ent` reaccount / wrong-entry reaccount | Fixed. `commands.cpp:2510-2545` reaccounts `src_ent` after removal and creates `dst_ent` before its reaccount. |
| `do_keys` ignored its pattern | Fixed. `commands.cpp:657-670` streams `*`/bare fast path, globs otherwise. |
| Admin/dangerous commands reachable via `+@read` | Fixed by the V9.5.1 tagging rule (`commands.cpp:3327-3343` strips `CAT_READ/CAT_WRITE` from admin/dangerous specs). |
| `MSETNX` checked the same key repeatedly | Fixed (`commands.cpp:501-512` checks each `cmd[i]`). |

Verified **still open** (not restated in detail — see the 2026-07-07 table):

- 🔴 `aof_check()` — `aof.cpp:279-293`: still no `return` after `fopen` failure (`fseek` on
  a null `FILE*` segfaults), and the `fread` condition is still inverted
  (`if (fread(...))` treats a *successful* read as "short read"), so `--check-aof`
  currently fails on every valid non-empty AOF and crashes on a missing one.
- 🔴 `ZPOPMIN` still registered without `is_write` — `commands.cpp:3117`. It mutates and
  deletes keys but is ACL-read, skips the MISCONF/OOM write gates, and is never
  AOF-logged: a `ZADD`+`ZPOPMIN` sequence resurrects popped members after replay.
- 🟠 `GETEX` TTL changes still don't bump `g_writes_since_save` — `commands.cpp:341-345`
  (PERSIST) and `commands.cpp:366` (EX/PX/EXAT/PXAT future case) — so those TTL
  mutations are never AOF-logged (the `do_request` gate at `commands.cpp:3245` requires
  a dirty-counter change).
- 🟠 `ZREM` — `commands.cpp:863-877`: no `mem_reaccount`, no empty-key drop (also tracked
  in ROADMAP Known Bugs).
- 🟠 `parse_cidr()` — `state.cpp:505-509`: still validates `bits` (constant 32) instead of
  `parsed`; `/33` or `/-1` reaches `0xFFFFFFFFu << (32 - bits)` with a negative/oversized
  shift (UB).
- 🟠 `SAVE` and shutdown still hardcode `rdb_save("dump.rdb")` — `commands.cpp:1084`,
  `server.cpp:663` — ignoring `dbfilename`; `do_save` still double-calls
  `rdb_on_save_complete` (`commands.cpp:1085`).
- 🟡 AOF rewrite still hardcodes `appendonly.aof.tmp` and ignores `rename()`/short-delta
  failures — `aof.cpp:135`, `aof.cpp:150-176`. Additional note: if the delta-append
  `open()` at `aof.cpp:152` fails, the whole success block is skipped silently — no
  rename, no error, `g_aof_last_rewrite_ok` stays `true`, orphaned tmp file.
- 🟡 RDB tmp paths still use unchecked 256-byte `snprintf` — `rdb.cpp:350-351`, `rdb.cpp:879-880`.
- 🟡 `CONFIG SET <unknown>` still returns `+OK` — `commands.cpp:2584-2587`.
- 🟡 `do_auth` still leaves the plaintext copy in local `pass` unwiped — `commands.cpp:1037-1049`.
- 🟡 Loose `atoi`/env parsing — `state.cpp:158,215,254`, `server.cpp:474-475`.
- 🟡 `config_rewrite()` still drops `bind`, `protected-mode`, `allow-ip`, AOF-rewrite and
  LFU knobs — `state.cpp:331-355`.
- 🟡 `SADD`/`ZADD` still bump the dirty counter and log to AOF on pure no-ops —
  `commands.cpp:2236-2237`, `commands.cpp:857-858`.
- 🟡 `S*STORE` with an empty result still leaves an empty destination set —
  `commands.cpp:2477-2507` (`set_make_dest` always creates).
- 🔵 Eviction loop can still delete up to 100 victims synchronously — `commands.cpp:2791-2811`.
- 🔵 Perf items unchanged: `SUNION` sort-dedupe (`commands.cpp:2196-2198`), `SPOP`/`SRANDMEMBER`
  full-set collection (`commands.cpp:2318`, `commands.cpp:2365-2366`), `INFO` O(N) key scan
  (`commands.cpp:1135`), full-walk `entry_mem_usage` on every reaccount (`state.cpp:393-415`).
- ⚪ `hash_set(const std::string value)` still copies on both paths — `hash.cpp:6`.

Correction to the 2026-07-07 record: the flagged RESP bulk-length signed overflow
(`resp.cpp:71-75`) is **not reachable**. The per-digit check `str_len > k_max_msg`
bounds the value entering the next `*10 + d` at 2^25, so the maximum intermediate is
~3.4e8 < `INT32_MAX`. Tightening to match the `n_args` pre-multiply style is still
nice for symmetry, but it is not a bug.

### B. New findings

#### 🔴 CRITICAL

| # | Location | Problem | Failure scenario / fix |
|---|---|---|---|
| N1 | `aof.cpp:229-234` + `commands.cpp:2855-2879` | The AOF replay user sets `allow_cats = CAT_ALL` but never sets `all_keys = true` (default `false`, `state.h:95`). `acl_check()` passes the category test, then enters the key-pattern branch with an **empty** `key_patterns` list, so `acl_key_allowed()` returns false for every key. | Every keyed command in the RESP tail (`SET`, `DEL`, `HSET`, …) is rejected with `NOPERM` into the discarded sink during `aof_load()`. Plain AOF: the entire dataset silently fails to load on restart. Hybrid AOF: the RDB preamble loads but the whole delta is lost. Nothing is printed. Fix: `replay_user.all_keys = true;` — and make replay fail loudly (count `-ERR`/`-NOPERM` replies in the sink instead of draining blind), plus add the AOF-restart-with-ACL test the 2026-07-07 pass asked for. |
| N2 | `hashtable.cpp:50-75` (`hm_help_rehashing`, `hm_trigger_rehashing`; field at `hashtable.h:27`) | `migrate_pos` is never reset — not when a rehash completes (`older = HTab{}` at line 67 doesn't touch it) and not when a new rehash starts (`hm_trigger_rehashing`). After the first rehash finishes, `migrate_pos` is left at the last drained bucket index of the *old* table. | Second rehash: buckets `[0, stale_pos)` of the new `older` table are never visited; the loop hits `migrate_pos > older.mask` and breaks with `older.size > 0` **forever**. Consequences compound: entries stay stranded in `older` (extra probe on every lookup), and because `hm_insert` only triggers a rehash when `older.tab == NULL`, the `newer` table can never resize again — chains grow without bound and every HMap (the main db, every hash/set/zset) degrades toward O(n) lookups on long-running instances. Fix: `hmap->migrate_pos = 0;` inside `hm_trigger_rehashing()` (and defensively when `older` is freed). |
| N3 | `server.cpp:191` | `uint64_t soonest = (uint8_t)-1;` — the sentinel is 255, not `UINT64_MAX`. Once any pending write satisfies a save-condition change count (the default `save 3600 1` needs just one write), `soonest` stays 255, which is always `<= now_ms`, so `next_timer_ms()` returns 0. | `poll()` spins with timeout 0 at ~100% CPU from the first write until the save window actually elapses — up to a full hour with defaults. Fix: `(uint64_t)-1`. |
| N4 | `server.cpp:362-366` (`try_one_request`) + `server.cpp:401-411` (`handle_read`) | A RESP parse error (`consumed < 0`) only logs "bad RESP" and returns; it neither sets `conn->want_close` nor consumes the bad bytes. There is also no cap on `Conn::incoming` growth. | A client that sends one malformed frame wedges its connection permanently: the same garbage re-parses on every readable event, and everything it sends afterward accumulates in `incoming` forever (`buf_append` grows unbounded). A malicious client can also stream a syntactically valid frame header (`*65536` × 32 MB bulks) and force multi-GB buffering before any command executes. Fix: on parse error set `want_close = true` and reply `-ERR Protocol error` (Redis behavior); add a per-connection input cap tied to `k_max_msg`/arg budget. |

#### 🟠 BUG

| # | Location | Problem | Failure scenario / fix |
|---|---|---|---|
| N5 | `sha256.h:120-130` | The padding branch for inputs whose final partial block is ≥ 56 bytes is wrong: inside `while (i < 64)` it calls `transform()` on a **half-padded block** (stale message bytes still present) and does so once per remaining byte (up to 7 times), re-zeroing `data[0..55]` each pass. | `sha256_hex(msg)` is not SHA-256 whenever `msg.size() % 64 >= 56`. AUTH still works for plaintext passwords (both sides use the same broken function), but a pre-hashed `requirepass "#<hex>"` or ACL `#<hex>` generated with a real tool (`sha256sum`) will never match for 56–63-byte passwords, and any future interop (ACL file exchange, SCRIPT SHA-style uses) inherits wrong digests. Fix: after appending `0x80`, zero-fill to 64, `transform` once, zero the whole block, then write the length and do the final `transform`. Add known-answer tests (NIST vectors incl. a 56-byte and 63-byte input). |
| N6 | `commands.cpp:288-297` (`do_getset`) | The existing-key path swaps in the new value with no `mem_reaccount(ent)`. | `SET k <8MB>` then `GETSET k x`: `used_memory` stays ~8 MB too high permanently (drift accumulates per call; debug `mem_selfcheck` flags it, release never corrects). Under `maxmemory` this causes spurious evictions/OOM. Fix: reaccount after the swap. Same handler: Redis `GETSET` also discards the TTL — MYRED keeps it. |
| N7 | `commands.cpp:230-239` (`do_set`) | `SET` on an existing key keeps its TTL. Redis discards the TTL on plain `SET` (that's what `KEEPTTL` exists for). | `SET k v EX...`-less workflows: `SETEX k 10 v` then `SET k v2` — in Redis `k` is now persistent; in MYRED it still dies in 10 s. Divergence also propagates through AOF raw frames. Fix: `entry_set_ttl(ent, -1)` on overwrite in `do_set` (and decide `SETNX`-family semantics deliberately). |
| N8 | `rdb.cpp:359-372` (`rdb_save`) | `fsync(fileno(fp))` runs **before** the stdio buffer is flushed (no `fflush`). The tail of the serialized image can still sit in the `FILE*` userspace buffer at fsync time; `fclose` then writes it with no durability barrier. | Power loss just after `rdb_save` returns "done" can leave a truncated `dump.rdb` that fails CRC on load (recoverable only via `.bak`). Fix: `fflush(fp)` before `fsync`, or use raw `open/write/fsync` like `rdb_write_snapshot` does. |
| N9 | `rdb.cpp:905-911` (`rdb_save_background`) | When a save child is already running, the early-return path overwrites `g_dirty_at_save` with the *current* counter. The in-flight snapshot only contains writes up to its fork point, but `rdb_on_save_complete` (`rdb.cpp:334-339`) will subtract the inflated value. | Writes made *during* a background save lose their dirty status when it completes: save conditions don't re-trigger for them, widening the crash-loss window until unrelated future writes accumulate. Fix: don't touch `g_dirty_at_save` on the busy path (only the actual save start should snapshot it). |
| N10 | `rdb.cpp:777-787` (`rdb_load_buffer`) | Compressed path does `memcpy(&usize, payload, 4)` and `rdb_decompress(payload + 4, payload_size - 4, usize)` without checking `payload_size >= 4`; `usize` itself is also unchecked. | A truncated/corrupt file with the compressed flag and `payload_size < 4` under-reads out of bounds and passes a wrapped-around `size_t` to zlib → crash. A corrupt-but-CRC-valid file (CRC is over the corrupt bytes, so trivially "valid") can request a multi-GB `new uint8_t[usize]`. Fix: bounds-check the 4-byte header and clamp `usize` against a sane multiple of `payload_size`. |
| N11 | `commands.cpp:2392-2400` (`do_srandmember`) | Negative count is used unclamped: `n = (size_t)(-count)`, then `resp_arr((uint32_t)n)` truncates while the loop still runs `n` times. | `SRANDMEMBER k -3000000000`: the array header says one thing, the loop emits 3 billion bulk strings into `outgoing` — protocol desync plus effectively unbounded memory growth from a 30-byte command. Fix: cap (Redis proto limit style) or reject counts beyond a configured max reply size. |
| N12 | `state.cpp:20-32` (`acl_bootstrap_default`) + `server.cpp:480` | `acl_bootstrap_default()` runs *after* `config_load_file()` and unconditionally resets the default user: `allow_cats = CAT_ALL`, `all_keys = true`, clears `cmd_overrides`, `key_patterns`, `pw_hashes`. | Any `user default ...` hardening in `myred.conf` (e.g. `user default on #h1 -@dangerous ~app:*`) is silently discarded at boot — the operator believes default is restricted; it is fully privileged. Fix: bootstrap only when the config didn't define `default`, or merge (only sync `pw_hashes` from `requirepass`). |
| N13 | `state.cpp:252`, `state.cpp:263` | Directive names are `auto_aof_rewrite-percentage` / `auto_aof_rewrite-min-size` (underscore/hyphen mix). Redis spelling is `auto-aof-rewrite-percentage` / `auto-aof-rewrite-min-size`. | Copying a real redis.conf line yields `unknown directive` and (in `config_load_file`) a failed boot. Fix: accept the hyphenated names (keep the old ones as aliases if any config already uses them). |

#### 🟡 ROBUSTNESS

| # | Location | Problem | Failure scenario / fix |
|---|---|---|---|
| N14 | `server.cpp:208` | `return (int32_t)(next_ms - now_ms);` — a TTL more than `INT32_MAX` ms out (~24.8 days) overflows to a negative timeout, which `poll()` treats as infinite. | Idle server with only a far-future TTL and no other timers blocks forever; the expiry fires only when some connection event happens to wake the loop. Clamp to `INT32_MAX` (or a periodic-tick max like 10 s). |
| N15 | `server.cpp:616-618` vs `conn_set_timer` (`server.cpp:340-353`) | The poll loop moves every active conn into `idle_list` but leaves `timer_type` stale (`IO`), so `handle_read`'s `timer_type == IDLE` check never fires and the io/idle split never actually happens. | Harmless today only because `k_idle_timeout_ms == k_io_timeout_ms == 30s`; the moment the two constants diverge, mid-request connections get the wrong timeout class. Either route the poll-loop reinsertion through `conn_set_timer` or delete the two-list distinction. |
| N16 | `server.cpp:478` | Fallback `g_config.password = sha256_hex("kek1234")` gives every unconfigured deployment a publicly-known password (it's in the repo/README), and because a password exists, protected-mode never engages for remote peers. | Anyone who can reach the port can auth with `kek1234`. The safer default is *no* password + protected-mode's loopback-only refusal (`server.cpp:93-100` already implements it). Keep `kek1234` only in the test harness. |
| N17 | `rdb.cpp:696-699` | Set-entry load failure path `return false` without freeing the partially built `ent` (string/zset use `entry_del`, deque uses `entry_del_sync`); expired-skip paths (`rdb.cpp:624-633` etc.) ignore `cursor_read_u32` results and loop up to `n` (4B) times on garbage counts. | Corrupt-file load leaks entries and can stall seconds burning failed cursor reads. Free `ent` on all failure paths (pick `entry_del_sync` — pool-free during load is pointless) and bounds-check counts like the non-expired paths already do. |
| N18 | `buffer.cpp:32-37` | If a `Buffer` is ever created with capacity 0, growth does `new_cap = 0 * 2` and `while (new_cap < needed) new_cap *= 2` never terminates. | Latent: all current `buf_create` calls pass ≥ 4096, but `aof_load` computes `sz - resp_offset + 1` (the `+1` is the only guard). Start growth from `max(old_cap * 2, 64)`. |
| N19 | `commands.cpp:1373`, `commands.cpp:1543-1546`, `commands.cpp:2331-2334`, `commands.cpp:1695-1696` | Small reply-semantics divergences: `LSET` on a missing key returns `:0` (Redis: `-ERR no such key`); `LTRIM` missing key returns `:0` (Redis: `+OK`); `SPOP k <non-int>` returns an empty array (Redis: value-not-integer error); `SCAN`/`HSCAN`/`SSCAN` accept negative cursors via `str2int` then cast. | Client libraries that pattern-match on reply type (e.g. redis-py raising on `-ERR`) behave differently against MYRED; tighten before claiming tooling compat. |
| N20 | `client.cpp:76`, `client.cpp:91` | `std::stoi` on the wire data throws `std::invalid_argument` on a malformed/desynced reply and terminates the client. | Dev-tool only; wrap or use `strtol`. |

#### ⚪ CLEAN CODE

- `state.h:218` — stray `\` line-continuation after `EntryValue val;` (splices into the
  closing brace; harmless now, trap for the next edit).
- `state.h:283-284` — `dispatch_build()` / `command_is_known()` declared but defined
  nowhere (V9.5.3 scaffolding; fine if intentional, will confuse a linker the moment
  someone calls them).
- `state.h:22` — comment says "5s -> 5000ms" over a 30 s constant; `Conn::failed_attemps`
  typo; `expireat_generic` comment says `mult=10000` (`commands.cpp:2075`).
- `rdb.cpp:829-831` — comment claims "old format — no CRC, load normally" but the code
  (correctly) rejects it; make the comment match.
- `commands.cpp:3224-3226` — `mem_selfcheck(cmd[0])` runs *before* the handler, so a
  reported drift names the command *after* the one that caused it; move it after
  `spec.fn` or label it "checked before <cmd>".
- `common.h:21-27` — `str_hash` computes 32-bit FNV-1a into a `uint64_t`; upper 32 bits
  of every `hcode` are zero. Harmless at current scales, but switching to 64-bit FNV-1a
  is one line and removes a silent assumption.

### Suggested fix order (2026-07-09)

1. **N1** (AOF replay loses data) + regression test; then re-verify N-carryover `ZPOPMIN`/`GETEX` AOF items in the same test batch.
2. **N3** (busy-loop) and **N4** (protocol-error wedge) — both are two-line server.cpp fixes with big blast radius.
3. **N2** (rehash stall) — one line, then a soak test: insert 10M keys, confirm `older.tab` frees and lookups stay flat.
4. Persistence batch: N8, N9, N10, carry-over `aof_check` inversion, rewrite finalize errors.
5. Accounting/semantics batch: N6, N7, carry-over `ZREM` reaccount, `S*STORE` empty dest.
6. Security batch: N12, N16, N5 (+ carry-over `parse_cidr`, `AUTH` wipe, `CONFIG SET` unknown).
7. Protocol/compat polish: N11, N13, N19.
