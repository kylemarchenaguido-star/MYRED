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
