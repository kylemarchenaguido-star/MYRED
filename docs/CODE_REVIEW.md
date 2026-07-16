# MYRED — Code Review and Bug Audit

Single working document for bug tracking. The older full-codebase audits (v5.3
review, Post-v9 Backlog Audit 2026-07-07, Full-Codebase Audit 2026-07-09) were
re-verified item-by-item and folded into the consolidated audit below; their
superseded text was removed on 2026-07-13. Fixed bugs move to the Resolved Bugs
Archive at the bottom (moved here from the ROADMAP the same day).

Quick benchmark:
`redis-benchmark -p 1234 -a kek1234 -t set,get,incr,lpush,rpush,sadd,hset -n 200000 -c 50 -P 16 -q`

Severity legend:
- 🔴 **CRITICAL** — crash, memory corruption, or silent data loss.
- 🟠 **BUG** — observably wrong behavior.
- 🟡 **ROBUSTNESS** — edge cases, hardening, latent hazards.
- 🔵 **OPTIMIZATION** — performance / memory.
- ⚪ **CLEAN CODE** — naming, dead code, consistency, style.

---

## Consolidated Bug Audit — 2026-07-13 (V9.6.4 worklist)

Scope: every open item from ROADMAP "Known Bugs and Correctness Follow-ups" (now moved
here) merged with the 2026-07-07 / 2026-07-09 audit findings (folded in; their
superseded text was removed 2026-07-13). **Each item was re-verified against the
working tree of 2026-07-13**; line numbers are against that tree unless marked
otherwise. This section is the single worklist for V9.6.4; the ROADMAP no longer
carries bug bodies.

### A. Verified fixed since 2026-07-09

| Item | Fixed by / evidence |
|---|---|
| 🟠 N5 — `sha256_hex` padding (`len%64 >= 56`) | V9.6 prerequisite, 2026-07-12. KAT-verified against NIST vectors + hashlib for lengths 0–200. |
| 🟡 `do_auth` plaintext copy left unwiped | V9.6.1/V9.6.2 — `do_auth` wipes the `cmd` copy on every path; the worker owns the only other copy and `secure_zero`s it after verify (`commands.cpp:1132`). |
| 🟡 `CONFIG SET <unknown>` returned `+OK` | V9.5.5 — `commands.cpp:2724` returns `ERR Unknown config parameter`. |
| 🟠 `ACL CAT` missing RESP array header (ROADMAP) | V9.5.5 — `commands.cpp:3129` emits `resp_arr` before the elements. |
| 🟠 `SMOVE` imprecise key checks (ROADMAP) | V9.5.2 — `kr_smove` resolver (`commands.cpp:3488`) checks source + destination only. |
| 🟠 `OBJECT`/`MEMORY` key at `cmd[2]` unchecked (ROADMAP) | V9.5.2 — `kr_object`/`kr_memory` resolvers (`commands.cpp:3493-3513`). |
| ⚪ `dispatch_build`/`command_is_known` declared but undefined | V9.5.3 — both defined; `g_dispatch` built at boot. |
| ⚪ `state.h:218` stray `\` line-continuation | Gone — no trailing backslash remains in `state.h`. |
| 🟠 Audit-log peer separator (`peer=ip.port`) | Fixed 2026-07-13 — `server.cpp:115` formats `%u.%u.%u.%u:%u`. |

(Already recorded fixed on 2026-07-09: replay `fake.user` assigned-in-shape, `SMOVE`
reaccount, `do_keys` glob, V9.5.1 admin-category tagging, `MSETNX`.)

### B. Open — the V9.6.4 worklist

Every unmarked line below was re-verified **open** on 2026-07-13.

#### 🔴 Critical

- [x] **N1 — AOF replay silently loses the whole RESP tail.** FIXED 2026-07-13.
  Cause: the replay user had `allow_cats = CAT_ALL` but never `all_keys = true`, so
  the `acl_check` key gate ran with empty `key_patterns` and NOPERM'd every keyed
  tail command into the discarded sink. Fix: `replay_user.all_keys = true`
  (`aof.cpp:233`), plus the bug class is now loud instead of silent — the replay loop
  counts error replies in the sink, a WARNING with the first offending command prints
  at the end, and `aof_load` returns false on any replay error (`aof.cpp:240-296`).
  Regression: `scripts/test_aof_restart.py` (3 server lifetimes; plain-AOF replay + hybrid
  preamble-and-delta replay) — all data checks green 2026-07-13. Archived in the
  Resolved Bugs Archive below.
- [x] **N2 — `migrate_pos` never reset.** FIXED 2026-07-13: `hmap->migrate_pos = 0;`
  added in `hm_trigger_rehashing()` (`hashtable.cpp:74`). Cause: the cursor survived
  from the previous drain, so from the second rehash on the low buckets of `older`
  were stranded, `older.tab` was never freed, and `hm_insert`'s `!older.tab` gate
  blocked every future resize — silent O(n) degradation, no data loss (lookups fell
  back to `older`). Regression: `scripts/test_hashtable.cpp` (standalone, ~5 rehash
  cycles; asserts the draining table empties and is released) — red on the old code,
  4/4 green with the fix. Archived in ROADMAP → Resolved Bugs Archive.
- [x] **N3 — timer sentinel `(uint8_t)-1`.** FIXED 2026-07-13: `(uint64_t)-1`
  (`server.cpp:231`). Cause: 255 is a quarter-second-after-boot *timestamp*, so with
  any write pending `next_timer_ms()` returned 0 both when a save condition matched
  (255 beat any real deadline) and when none did (255 passed the `!= (uint64_t)-1`
  "found one" guard) — `poll()` busy-spun at ~100% CPU until the save fired. Verified
  in-tree + by `top` before/after a `SET`: ~100% → ~0%. **Was a TLS prerequisite.**
- [x] **N4 — protocol-error wedge / no input cap.** FIXED 2026-07-13: malformed frame
  → `-ERR Protocol error` + `want_close` (`server.cpp:413-417`); per-conn cap
  `k_max_incoming` = 2×`k_max_msg` in `handle_read`. **Was a TLS prerequisite.**
- [x] **`aof_check()` crash + inverted read.** FIXED 2026-07-13: `return false` after
  `fopen` failure; `fread(...) != (size_t)sz` (`aof.cpp:316,326`).
- [x] **`ZPOPMIN` not `is_write`.** FIXED 2026-07-13: row flagged write
  (`commands.cpp:3268`); frame verified in the AOF (handler already dirty-counted and
  was OOM-gate-exempt).
- [x] **BGREWRITEAOF discarded at shutdown.** FIXED 2026-07-13: finalize extracted to
  `aof_rewrite_reap()`; `aof_rewrite_wait_shutdown()` (`aof.cpp:190`) blocks and
  finalizes at shutdown (`server.cpp:751`), AOF fd close reordered after it.

#### 🟠 Bugs

- [ ] **N6 — `GETSET` no reaccount** — TTL-discard half FIXED 2026-07-13; **still
  missing `mem_reaccount(ent);` after the swap in the overwrite path**
  (`do_getset`) → `used_memory` drift remains until that one line lands.
- [x] **N7 — `SET` keeps TTL on overwrite.** FIXED 2026-07-13: `entry_set_ttl(ent, -1)`
  on the `do_set` overwrite path; SETNX/SETEX verified already correct.
- [ ] **`ZREM`/`ZPOPMIN` reaccount + empty-key drop** — `do_zrem` FIXED 2026-07-13
  (reaccount + empty drop); **`do_zpopmin` still misses the survivors case**: move
  `mem_reaccount(ent)` from the no-op `else` into an `else` of the emptied-check
  inside `if (!popped.empty())`.
- [x] **`GETEX` TTL changes not dirty-counted + EXAT overflow.** FIXED 2026-07-13:
  `entry_has_ttl`-guarded PERSIST bump, dirty bump on the future path, `INT64_MAX/1000`
  guards on EX/EXAT, negative EXAT short-circuits to the delete path.
- [x] **N8 — `rdb_save` fsync before stdio flush.** FIXED 2026-07-13: `fflush(fp)`
  checked before `fsync(fileno(fp))`.
- [x] **N9 — `g_dirty_at_save` misplaced.** FIXED 2026-07-13: snapshot captured at
  serialize time in `rdb_save_background()`; busy-branch clobber removed. (Found
  worse than filed: the success path never captured it at all.)
- [x] **N10 — compressed-RDB bounds.** FIXED 2026-07-13: `payload_size >= 4` check +
  `usize` capped by zlib's 1032:1 max expansion ratio.
- [x] **N11 — `SRANDMEMBER` negative count unclamped.** FIXED 2026-07-13:
  `INT64_MIN` + `-count > k_max_args` rejected with `ERR count is out of range`.
- [x] **N12 — `acl_bootstrap_default` clobbers a configured default user.** FIXED
  2026-07-13: bootstrap respects a config-defined `default` (only fills `pw_hashes`
  from `requirepass` when the operator gave no password rule). Note: `config_rewrite`
  still skips emitting `default` — folded into the rewrite-coverage item below.
- [x] **N13 — directive spelling.** FIXED 2026-07-13: hyphenated
  `auto-aof-rewrite-*` accepted (`state.cpp:286,297`), old spellings kept as aliases.
- [x] **`parse_cidr` validates the wrong variable** — HALF-APPLIED 2026-07-13: the
  check is now `if (bits < 0 || parsed > 32)` — `/33` is rejected (verified live) but
  the left side still tests the constant `bits`, so **`/-1` still reaches the shift
  (UB)**. Change `bits < 0` → `parsed < 0`.
- [x] **`SAVE`/shutdown hardcode `dump.rdb` + double-complete.** FIXED 2026-07-13:
  both route through `g_config.dump_path`; `do_save`'s duplicate
  `rdb_on_save_complete` call removed (`rdb_save` owns it).
- [x] **AOF rewrite tmp/finalize fragility.** FIXED 2026-07-13: tmp derived from
  `aof_path` (built pre-fork); `aof_rewrite_reap` checks delta write/fsync/rename,
  only repoints the fd after a successful swap, single `ok` exit sets
  `g_aof_last_rewrite_ok` and cleans the tmp on any failure.
- [x] **N21 — background-child reaping waits for a poll wakeup.** FIXED 2026-07-13:
  `next_timer_ms()` caps the poll timeout at `now_ms + 100` whenever
  `g_rdb_child_pid`/`g_aof_child_pid` is live (`server.cpp:242-244`), so a finished
  child is reaped within ~100 ms even on a fully idle server. Cause (found by
  `scripts/test_aof_restart.py`): the reap/finalize checks only run per loop
  iteration, and a clean idle server had no periodic tick, so a finished BGREWRITEAOF
  sat unreaped with a stale `.tmp` until the next client event (>10 s observed).
  Regression guard: the 10 s "AOF became hybrid" check in
  `scripts/test_aof_restart.py`.

#### 🟡 Robustness

- [x] **N14 — `next_timer_ms` return overflow.** FIXED 2026-07-13: the return is
  clamped — `wait > INT32_MAX` returns `INT32_MAX` (`server.cpp:252-254`). Previously
  a timer >24.8 days out (far-future TTL) cast negative and `poll()` blocked forever.
- [ ] **N15 — idle/io timer split is dead code** (poll loop vs `conn_set_timer`):
  `timer_type` stays stale (`IO`); wrong timeout class the moment the two constants
  diverge.
- [ ] **N16 — `kek1234` fallback password** (`server.cpp:544`): a publicly-known
  default credential that also defeats protected mode (password exists → loopback-only
  refusal never engages). Safer default: no password + protected mode.
- [ ] **N17 — RDB set-load failure leaks the partial entry**; expired-skip paths ignore
  cursor-read failures (`rdb.cpp:696-699`, per 2026-07-09 — not re-verified).
- [ ] **N18 — `Buffer` zero-capacity growth loop** (`buffer.cpp:35-37`): `new_cap = 0*2`
  never terminates; latent. Floor growth at `max(old_cap*2, 64)`.
- [ ] **Loose `atoi` config/env parsing** (`state.cpp:187,244,283`, env in server.cpp):
  `123abc` accepted silently. Shared strict int/bool parsers for file config, env, and
  `CONFIG SET`.
- [ ] **`config_rewrite` drops directives** (`state.cpp:368+`): still missing `bind`,
  `protected-mode`, `allow-ip`, auto-AOF-rewrite and LFU knobs (users, rename-command,
  auditlog now covered). Related hardening (V9.6 review): rewrite is full-regeneration
  — write atomically (tmp+rename) and add a config round-trip regression test.
- [ ] **`SADD`/`ZADD` no-op writes dirty the AOF** (`commands.cpp:2362-2363` for SADD;
  ZADD same shape): `g_writes_since_save` bumps even when nothing changed.
- [ ] **`S*STORE` empty result leaves an empty destination** (`set_make_dest`,
  `commands.cpp:2266`, always creates): Redis deletes the destination.
- [ ] **N19 — reply-semantics divergences** (`LSET` missing key → `:0` vs `-ERR no such
  key`; `LTRIM` → `:0` vs `+OK`; `SPOP k <non-int>` → empty array vs error; negative
  `SCAN` cursors accepted) — per 2026-07-09, not re-verified.
- [ ] **N20 — client.cpp `std::stoi` throws on malformed replies** (dev tool only).
- [ ] **AOF-load-failure policy** (server.cpp startup): logs the failure and serves
  whatever partial state loaded; decide fail-fast vs documented best-effort.
- [x] **`die()` vs `fatal_exit()` split.** FIXED 2026-07-13: `fatal_exit` (print +
  `exit(1)`, with `strerror`) at the five operational sites; `panic` (print +
  `abort()`) kept only for the mid-run `poll()` failure. Verified live: bad config
  now exits `fatal: invalid config file`, no core dump. Original finding: `die()`
  (`server.cpp:57`) prints then `abort()`s — a core-dump-producing, crash-looking exit
  — for routine startup mistakes: bad config path (`server.cpp:536`), listener setup
  (`:636`), fcntl (`:67,74`), eventfd (`:644`). Reproduced: a typo'd config filename
  prints the error then `Aborted (core dumped)`. Fix: `panic(msg)` (print + `abort()`)
  reserved for internal-invariant violations — the runtime `poll()` failure (`:672`) is
  the one plausible keeper — and `fatal_exit(msg)` (print + `exit(1)`) for the
  operational sites.

#### 🔵 Performance / ⚪ polish (fix only after everything above is green)

- [ ] Eviction deletes up to 100 victims synchronously in the write path.
- [ ] `SUNION` sort-dedupe; `SPOP`/`SRANDMEMBER` collect the full set; set-algebra
  store commands double-copy.
- [ ] `INFO` does an O(N) keyspace scan per call.
- [ ] `entry_mem_usage` full walk on every `mem_reaccount`.
- [ ] `hash_set(const std::string value)` copies on both paths.
- [ ] `str_hash` computes 32-bit FNV into a `uint64_t` (upper half always zero).
- [ ] `mem_selfcheck` runs before the handler, blaming the wrong command.
- [ ] `rdb.cpp:829-831` comment contradicts the (correct) old-format rejection;
  `state.h` comment/typo nits (`failed_attemps`, "5s -> 5000ms" over 30 s).

#### Testing debt (from ROADMAP Testing Gaps — closes with the batches above)

- [ ] AOF-restart-with-ACL test (pairs with N1); restart tests for `GETEX`, `GETDEL`,
  `ZPOPMIN`, eviction `DEL`, and renamed-command canonicalized frames.
- [ ] Security tests: control-plane category gating (V9.5.1), renamed/disabled
  commands, audit-log redaction, precise key ACLs; one test that `ACL CAT` framing is
  a valid RESP array.
- [ ] Destructive/server-crashing edge cases behind an explicit test flag.

### C. Not bugs — feature gaps returned to ROADMAP Backlog

Full Redis ACL rule-order fidelity ("last match wins"); Pub/Sub channel-pattern
enforcement (blocked on V8); `nopass`, selectors, `sanitize-payload`, `ACL LOAD`,
`ACL SAVE`; full `CONFIG GET/SET` coverage; `COMMAND` / `COMMAND DOCS` /
`COMMAND COUNT`.

### Suggested fix order (V9.6.4)

1. **N1** + the AOF-restart-with-ACL regression test (the data loss is live today);
   fold `ZPOPMIN is_write` and the `GETEX` dirty-count fix into the same test batch.
2. **N3** and **N4** — two-line loop fixes with big blast radius; both are TLS
   prerequisites.
3. **N2** — one line, then a soak check (insert 10M keys; `older.tab` frees, lookups
   stay flat).
4. Persistence batch: `aof_check`, shutdown AOF finalize, N8, N9, N10, rewrite
   tmp/finalize, `SAVE` dbfilename.
5. Accounting/semantics batch: N6, N7, `ZREM`, `SADD`/`ZADD` no-ops, `S*STORE` empty
   dest, N11.
6. Config/security batch: N12, N13, N16, `parse_cidr`, strict parsers,
   `config_rewrite` coverage + atomic write, `die()`/`fatal_exit` split.
7. Robustness polish: N14, N15, N17, N18, N19, N20, AOF-load policy.
8. 🔵/⚪ items last.

## Resolved Bugs Archive

This section records fixed bugs without scattering them through milestone text.

### Persistence and AOF

- AOF replay ran under an incomplete synthetic superuser (`all_keys` unset), so the
  ACL key gate silently NOPERM'd the whole RESP tail on every restart — plain AOF
  loaded empty, hybrid AOF lost the delta (N1, fixed 2026-07-13; replay errors are
  now counted and reported; regression: `scripts/test_aof_restart.py`).
- Handlers that `swap()` command strings required `do_request` to snapshot `cmd`
  before calling `spec.fn()`.
- AOF write path needed a verbatim fallback in `aof_feed`.
- `SETEX` and related TTL commands were missing counter bumps.
- `STRLEN` was mistagged as a write command.
- `BGREWRITEAOF` used typo `appebdonly.aof.tmp`, making finalize a silent no-op.
- AOF load priority originally parsed `aof_enable` too late, so startup loaded RDB by
  mistake.
- AOF file open had to happen after load, not before load.
- `g_last_save_ms` was uninitialized, causing an immediate spurious `BGSAVE` on first
  write.
- `aof_feed` branches returned before appending relative TTL frames.
- `g_aof_child_pid != 1` should have been `!= -1`; the bug mirrored writes outside
  rewrites and could grow `g_aof_rewrite_buf` unbounded.
- `GETEX` AOF translation now emits deterministic `PEXPIREAT`, `PERSIST`, or `DEL`.
- AOF truncation handles partial or corrupt tails and keeps the last good offset.
- Disk-full AOF errors now reject future writes with `MISCONF` while reads continue.
- `SIGXFSZ` and `SIGPIPE` are ignored so write failures return errno instead of
  killing the server.

### Memory Management

- `LPOP` and `RPOP` reaccounting after `entry_del()` caused use-after-free patterns.
- `MSET` and `MSETNX` needed per-entry reaccounting, not one outside-loop reaccount.
- OOM gate had to exempt memory-freeing commands such as `DEL`, `UNLINK`, `FLUSHALL`,
  `EXPIRE`, pop/rem commands, and related shrinking commands.
- Evictions must propagate explicit `DEL` to AOF so replay does not resurrect evicted
  keys.

### Config and Auth

- A leftover hardcoded `password = kek1234` clobbered config-loaded passwords.
- `config_tokenize()` had a pre-increment bug that dropped each token's first char.
- `#<hash>` ACL/config tokens must be quoted in config rewrite because `#` starts a
  comment.
- Pre-hashed ACL token validation checked the wrong length for `#` plus 64 hex chars.

### ACL

- `acl_init_categories()` was not called at boot, so command ACL categories stayed `0`.
- `acl_init_categories()` needed a prototype in `state.h`.
- `ACL` category bits were accidentally placed in the key-spec map instead of the
  extra-category map.
- `ACL` needed `KeySpec::NONE`.
- `AUTH <user> <pass>` had an extra plaintext password copy that needed wiping.
- ACL deny parser branches checked the wrong token prefix for `-@cat` and `-cmd`.
- `acl_apply_rule()` missed a final `return false` for unrecognized tokens.
- `ACL GENPASS` had unreachable or wrongly nested code and could send no reply.
- `resetkeys` did not clear `all_keys`.
- `ACL LIST` originally hid partial `~pattern` and `+@cat` rules; it should use the
  same formatter as config rewrite.

### General Hardening

- `next_timer_ms()` had three defects fixed together 2026-07-13: the save-condition
  sentinel was `(uint8_t)-1` = 255, making `poll()` busy-spin at ~100% CPU whenever a
  write was pending (N3); the `int32_t` return overflowed negative for timers >24.8
  days out, turning `poll()` into an infinite block (N14); and nothing bounded the
  timeout while a background save/rewrite child was live, so an idle server never
  reaped finished children (N21 — the 100 ms child tick fixes it; regression:
  `scripts/test_aof_restart.py`).
- `HMap::migrate_pos` was never reset when a new rehash started, so from the second
  rehash on entries were stranded in `older` and all future resizes were blocked —
  silent O(n) degradation on long-running instances, no data loss (N2, fixed
  2026-07-13; regression: `scripts/test_hashtable.cpp`, standalone unit test).
- `RDB` save fork/malloc deadlock was avoided by serializing in the parent before fork.
- RDB loaders gained bounds checks.
- RDB save uses `.bak` rotation before atomic rename.
- `INFO` buffer was increased and `snprintf` length handling was clamped to avoid OOB
  reads.
- `accept()` loop, `EINTR`, `SO_REUSEADDR`, `TCP_NODELAY`, and thread-pool shutdown
  were hardened during the project-wide review.

