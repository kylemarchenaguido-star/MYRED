# MYRED Roadmap

MYRED is a Redis-compatible in-memory database written from scratch in C++.
It speaks RESP and is intended to work with `redis-cli`, Redis clients, and
`redis-benchmark` where the implemented command surface allows it.

This document is organized for agents. Use it as the project map before making
changes.

## How Agents Should Use This File

Read sections in this order:

1. Current Snapshot
2. Active Roadmap
3. Known Bugs and Correctness Follow-ups
4. Design Decisions
5. Completed Milestones
6. Backlog

Update rules:

- Put new open bugs only in `Known Bugs and Correctness Follow-ups`.
- When a bug is fixed, move it to `Resolved Bugs Archive`.
- Put implementation tradeoffs only in `Design Decisions`.
- Put completed work summaries only in `Completed Milestones`.
- Keep active implementation instructions under the relevant active milestone.
- Avoid adding session notes inline. Convert them into durable tasks, bugs, decisions,
  or completed summaries.

Naming conventions:

- Version headings use `V<number> - Name`.
- Active work uses `V<number>.<step> - Name`.
- Status labels are `[Next]`, `[In Progress]`, `[Done]`, `[Backlog]`, `[Deferred]`.
- Redis command names are uppercase in prose: `GET`, `ACL SETUSER`, `BGREWRITEAOF`.
- Internal code names keep exact spelling: `CmdSpec`, `k_cmd_table`, `acl_check`.
- Config directive names are lowercase: `requirepass`, `rename-command`, `auditlog`.

## Current Snapshot

Date: 2026-07-09.

Primary commands:

```bash
cmake -B build
cmake --build build
./build/server
python3 stress_test.py --password kek1234
python3 stress_test.py --password kek1234 --correctness-only
```

Default runtime assumptions:

- Default port: `1234`.
- Historical default password: `kek1234`.
- Config file: `myred.conf` can be loaded explicitly with `./build/server myred.conf`
  or via `MYRED_CONFIG`.
- Test harness: `stress_test.py` is the primary correctness and stress harness.
- Shell helpers live under `scripts/`.

Implemented command families:

| Area | Status |
|---|---|
| RESP2 parser and writers | Implemented |
| Strings | Implemented |
| Lists | Implemented |
| Hashes | Implemented |
| Sets | Implemented |
| Sorted sets | Implemented subset |
| Generic keyspace commands | Implemented subset |
| RDB persistence | Implemented |
| AOF persistence and rewrite | Implemented |
| Memory accounting and eviction | Implemented |
| Config file foundation | Implemented |
| Password hashing baseline | Implemented |
| ACL foundation | Implemented, needs hardening |
| TLS | Not implemented |
| Pub/Sub and transactions | Not implemented |
| Replication | Not implemented |

Do not rely on old test-count claims in this file. Run the harness for the current
count after any command or ACL change.

## Active Roadmap

### V9 - Security and Auth

Goal: move from one root password to a robust security model: config file, hashed
credentials, protected mode, named users, command and key ACLs, command hardening,
audit logging, stronger password hashing, and TLS.

Completed foundations:

- `[Done]` `V9.1 - Config File Foundation`
  - Shared `config_apply()` maps directives to `Config`.
  - `config_tokenize()` supports comments and quoted values.
  - Load precedence: defaults < config file < env < runtime `CONFIG SET`.
  - `CONFIG REWRITE` support exists for the implemented config surface.
- `[Done]` `V9.2 - Password Hashing and Constant-Time Compare`
  - `requirepass` is stored as SHA-256 digest or accepted as `#<hex>`.
  - `AUTH` hashes the supplied password and compares with `ct_equal`.
  - Plaintext command buffer is wiped with `secure_zero`.
- `[Done]` `V9.3 - Protected Mode, Bind, and IP Allowlist`
  - `bind` supports multiple listen addresses.
  - Protected mode rejects non-loopback peers when no password is set.
  - `allow-ip` CIDR entries are checked in `handle_accept`.
- `[Done]` `V9.4 - ACL Foundation`
  - `User` registry is stored under `Config::users`.
  - `Conn::user` is intended to be the single auth identity.
  - `CmdSpec` has ACL categories and key specs.
  - `AUTH <pass>` and `AUTH <user> <pass>` are supported.
  - `ACL SETUSER`, `GETUSER`, `DELUSER`, `LIST`, `USERS`, `WHOAMI`,
    `CAT`, and `GENPASS` are planned as the command surface.
  - `user` directives round-trip through config rewrite.

#### V9.5 - Command Hardening and Audit Log [Next]

Purpose: close the security holes that remain after ACL exists but before TLS.
This step makes dispatch, ACL categories, dangerous-command hiding, persistence,
and observability agree with each other.

Project-specific risks this step must address:

- `acl_init_categories()` derives `CAT_READ` for every non-write command, then ORs
  `CAT_ADMIN` and `CAT_DANGEROUS` on top. If `acl_check()` only checks category
  intersection, `+@read` can accidentally grant sensitive commands.
- `KeySpec` is intentionally small, but `OBJECT` and `MEMORY` key subcommands need
  key checks at index 2, and `SMOVE` should check only source and destination keys.
- `k_cmd_table` should not remain a process-lifetime mutable command definition just
  to support boot-time category stamping or command aliases.
- AOF currently has a raw-frame hot path. If a command is renamed and raw aliases are
  logged, the AOF becomes config-dependent and can leak secret aliases.

##### ✅ V9.5.1 - ACL Category Semantics Fix (tagging, not guard_cats) — DONE 2026-07-07

**Implemented this session:**
- Tagging fix in `acl_init_categories()`: after ORing `extra`, strip
  `CAT_READ`/`CAT_WRITE` from any command carrying `CAT_ADMIN`/`CAT_DANGEROUS`. `+@read`
  can no longer reach `CONFIG`/`ACL`/`KEYS`/`MEMORY`/`OBJECT`; `+@write` can no longer
  reach `FLUSHALL`. `acl_check` unchanged (single O(1) OR test).
- **Permission-before-arity reorder** in `do_request`: `acl_check` now runs *before* the
  arity check, so an unauthorized user always gets `NOPERM` instead of a "wrong number
  of arguments" that would leak a command's arity/shape. Authorized users still get
  normal arity errors. The `acl` dispatch stays after the arity check so `cmd[1]` is
  guaranteed to exist for `do_acl`.
- `keys` arity widened `1,1`→`1,2` to accept the pattern argument (see V9.5.1a).

**Design decision (2026-07-07):** the escalation is a *mistagging* bug, not a missing
gate. `acl_init_categories()` gave `CAT_READ` to every non-write command — including
control-plane commands (`CONFIG`/`ACL`/`KEYS`/`MEMORY`/`OBJECT`) — so `+@read`
intersected their category set and passed the O(1) check. Fix the tag, not the check.

Chosen approach (kept `acl_check` unchanged, no new `CmdSpec` field):

- In `acl_init_categories()`, after ORing the `extra` bits, strip the data-plane base
  from any control-plane command:
  ```cpp
  s.acl_cats = is_write ? CAT_WRITE : CAT_READ;
  if (extra has name){
    s.acl_cats |= extra;
    if (extra & (CAT_ADMIN | CAT_DANGEROUS)) s.acl_cats &= ~(CAT_READ | CAT_WRITE);
  }
  ```
- Result: `CONFIG = ADMIN|DANGEROUS`, `FLUSHALL = KEYSPACE|DANGEROUS`,
  `KEYS = KEYSPACE|DANGEROUS|SLOW`. A command is either data-plane (read/write) or
  control-plane (admin/dangerous/keyspace) — never both.
- `acl_check` keeps its single `(spec.acl_cats & user.allow_cats) != 0` test plus the
  existing `+cmd`/`-cmd` override and key-pattern checks. No 4th step, no hot-path cost.

Rejected: `guard_cats` (second bitmask + AND-gate in `acl_check`). It only buys
per-command *mandatory co-requirements* ("grantable by @read OR @keyspace but also
always requires @admin"), which no real command needs — so it was speculative
complexity over a mistag. Revisit only if such a command ever appears.

Deliberate divergence from Redis: `@read` no longer implies `KEYS`/`CONFIG`, and
`@write` no longer implies `FLUSHALL` (Redis's `+@write` does grant `FLUSHALL`). Safer
default; to restore Redis behavior a user writes an explicit `+flushall`/`+keys`.

Tests (unchanged — all satisfied by the tagging fix):

- `+@read ~*` cannot run `ACL`, `CONFIG`, `MEMORY`, `OBJECT`, or `KEYS`.
- `+@write ~*` cannot run `FLUSHALL`.
- `+@admin ~*` can run admin commands.
- Explicit `+acl` can run `ACL` without granting every admin command.

##### ✅ V9.5.1a - Real KEYS glob matching — DONE 2026-07-07

`do_keys` previously ignored its argument (`(void)cmd;`) and always returned every key,
so `keys user:*` over-returned the whole keyspace. Now it reuses the existing
`glob_match` (the same matcher used for ACL key patterns and `HSCAN`):
- bare `keys` or `keys *` → fast path: stream all keys, no glob, no temp buffer
  (unchanged cost for the common case);
- `keys <pattern>` → single-pass collect of matching keys, then emit.

##### `[Done]` V9.5.2 - Precise Key Resolution — 2026-07-09

Implemented: added `KeyResolver` (`void(*)(const cmd&, vector<string_view>& keys)`) as an
optional `CmdSpec::key_resolver`. When set it overrides the `KeySpec` enum in `acl_check`;
otherwise the enum fast path runs. `acl_key_allowed` was widened to take `std::string_view`
(so the enum and resolver paths share one checker with no overload). Resolvers `kr_smove`
(keys `cmd[1]`,`cmd[2]` — member `cmd[3]` excluded), `kr_object`/`kr_memory` (subcommand-aware
key at `cmd[2]`, case-insensitive `acl_sub_is`) are registered via a `resolvers` map in
`acl_init_categories`; `smove`/`object`/`memory` were removed from the `ks` enum map so the
resolver is the single source. All index access is bounds-checked (acl_check runs before the
arity gate).

Tests:

- A user with `~allowed:* +memory +object +smove` can access allowed keys only.
- `SMOVE` member names are not treated as keys.
- Blocked source or destination key returns `NOPERM`.

##### `[Done]` V9.5.3 - `rename-command` and Disabled Commands — 2026-07-09

Implemented (boot/config-file only, as scoped). `k_cmd_table` stays the canonical
ACL-stamped template; a boot-built owning map `g_dispatch`
(`unordered_map<string, DispatchEntry{canonical, spec}>`) is the live table.
`dispatch_build()` runs after `acl_init_categories()` (order:
config load → `acl_bootstrap_default` → `acl_init_categories` → `dispatch_build`),
copies each stamped spec, then applies `g_config.renames`: erase OLD, insert NEW (or
disable when NEW is `""`). `config_apply` parses `rename-command OLD NEW` with full
validation (unknown OLD, double-rename, NEW collision/control-chars, AUTH-lockout
guard) via `command_is_known()`; `config_rewrite` re-emits the lines. `do_request`
resolves via `g_dispatch`, uses **canonical** for ACL, the `acl` dispatch,
`cmd_can_grow_memory`, and `mem_selfcheck`. AOF: non-renamed writes keep the raw
`aof_append_raw` fast path; renamed writes snapshot + `aof_feed` with `snapshot[0]`
set to canonical (also drives TTL translation), so the alias never reaches the log.

Bug found + fixed this session: the auth dispatch was keyed on
`found && canonical=="auth"`, but `AUTH` is **not** a `k_cmd_table` command (its
handler takes `conn`), so it is never in `g_dispatch` → every command NOAUTH'd.
Fixed by matching `AUTH` by literal `cmd[0]` before the `g_dispatch` lookup. `AUTH`
is therefore not renameable (correct — no lock-out via aliasing). See Design Decisions.

Tests:

- `rename-command FLUSHALL ""` makes `FLUSHALL` unknown and produces no AOF entry.
- `rename-command CONFIG __secret_config__` makes `CONFIG` unknown and the alias work.
- A renamed write command survives restart even if the alias changes later.
- `-config` blocks the renamed config command because ACL uses canonical names.

##### V9.5.4 - Audit Log

Add a narrow audit logger, not a general logging framework.

Config:

```text
auditlog ""
auditlog stderr
auditlog /path/to/myred-audit.log
```

Optional later:

```text
auditlog-events auth,acl,admin,dangerous,deny
auditlog-required yes
```

Connection context:

- Store `peer_ip` and `peer_port` on `Conn` at accept time.
- Do not depend on `inet_ntoa()` later.
- Include `user=...` as `conn->user ? conn->user->name : "-"`.

Events:

- `auth_fail`: wrong password, disabled user, missing user.
- `auth_success`: optional and configurable.
- `acl_change`: `ACL SETUSER`, `ACL DELUSER`, future `ACL LOAD` or `ACL SAVE`.
- `acl_deny`: command or key-pattern denial.
- `admin_command`: canonical command has `CAT_ADMIN`.
- `dangerous_command`: canonical command has `CAT_DANGEROUS`.
- `accept_reject`: protected-mode or allowlist rejection.

Line format:

```text
ts=2026-07-09T18:23:10Z event=acl_deny peer=127.0.0.1:54321 user=alice cmd=flushall reason=category
ts=2026-07-09T18:23:22Z event=acl_change peer=127.0.0.1:54321 user=default cmd=acl sub=setuser target=bob result=ok
ts=2026-07-09T18:23:31Z event=auth_fail peer=10.0.0.9:52100 user=default result=wrongpass
```

Implementation rules:

- Use wall-clock UTC with `time()`, `gmtime_r()`, and `strftime()`.
- Use one `write()` per audit line to an `O_APPEND | O_CLOEXEC` fd.
- Avoid stdio buffering surprises across `fork()`.
- Never log passwords, password hashes, or raw argument vectors.
- `ACL SETUSER` logs target username and rule count only.
- `CONFIG SET requirepass` logs directive name only.
- Audit is best-effort by default; on write failure set sticky `audit_last_error` for
  future `INFO` exposure.

##### V9.5.5 - Protocol and Metadata Cleanup

- Fix `ACL CAT` to emit a RESP array header before category strings.
- Make unknown or unsupported `CONFIG SET` behavior explicit:
  - either document current compatibility behavior, or
  - return `ERR unsupported parameter`.
- Add a boot metadata self-check:
  - every command has nonzero `acl_cats`;
  - every admin/dangerous command has the data-plane base (`CAT_READ`/`CAT_WRITE`)
    stripped, so `@read`/`@write` cannot intersect it (V9.5.1 tagging rule);
  - every write command has a deliberate AOF mode: raw, rewrite, or canonicalize.
- Add stress-test coverage for hardening paths.
- Keep destructive or crash-probing tests behind an explicit flag.

Done criteria:

- Restricted `+@read` and `+@write` users cannot run admin/dangerous commands unless
  explicitly granted.
- Key-pattern ACLs are precise for `SMOVE`, `MEMORY`, and `OBJECT`.
- Disabled commands are unreachable and cannot dirty the DB or append to AOF.
- Renamed write commands are persisted canonically and survive restart.
- Audit log records auth failures, ACL changes, ACL denials, and admin/dangerous
  command attempts without leaking secrets.
- `stress_test.py --password kek1234 --correctness-only` covers ACL hardening,
  rename-command, and audit assertions.
- AOF shell tests cover at least one renamed write command.

#### V9.6 - Password Hashing Upgrade [Backlog]

SHA-256 is fast and unsalted. It is acceptable as the current baseline, but weak if a
config or ACL file leaks. Upgrade credentials at rest to a salted, memory-hard KDF.

Preferred path:

- Add Argon2id via `libargon2`.
- Store PHC-format strings (`$argon2id$...`) in `requirepass` and user password
  entries.
- Keep SHA-256 verification for existing `#<hex>` configs.
- Rehash to Argon2id on next `CONFIG SET requirepass` or `ACL SETUSER`.
- Keep `secure_zero()` and constant-time verification hygiene.

Fallback: bcrypt if Argon2 dependency cost is too high.

#### V9.7 - TLS [Backlog]

TLS is the heaviest security feature and can be its own milestone.

Plan:

- Link OpenSSL.
- Add `tls-port`, `tls-cert-file`, `tls-key-file`, and `tls-ca-cert-file`.
- Optional mutual TLS: require and verify client certificates.
- Wrap per-`Conn` I/O in an `SSL*`.
- Integrate non-blocking `SSL_read()` and `SSL_write()` with `poll()` using
  `SSL_ERROR_WANT_READ` and `SSL_ERROR_WANT_WRITE`.
- Add handshake timeouts.
- Use `SSL_shutdown()` on close.
- Support cert reload without restart only after basic TLS is stable.

### V8 - Pub/Sub and Transactions [Backlog]

Planned features:

- `SUBSCRIBE`
- `UNSUBSCRIBE`
- `PUBLISH`
- Pattern subscriptions
- `MULTI`
- `EXEC`
- `DISCARD`
- `WATCH`

Notes:

- Pub/Sub will make the existing ACL channel-pattern field useful.
- Transactions need command queueing and optimistic invalidation, not just parser work.
- Blocking or queued client state should be designed before adding blocking list
  commands.
- Keyspace notifications (`notify-keyspace-events`) should ride on Pub/Sub once
  `PUBLISH` exists. The hook points already exist and are few: lazy expiry
  (`expire_if_needed`), active expiry (`process_timers` TTL drain), eviction
  (`free_memory_if_needed`), and the write handlers themselves. One
  `notify_keyspace_event(class, event, key)` helper called from those sites covers
  Redis-compatible `K`/`E` channel semantics without touching the dispatch path.

### V10 - Replication and High Availability [Backlog]

Planned features:

- Master-replica mode.
- `PSYNC`.
- Replication backlog.
- Partial resync.
- Replica propagation for writes, evictions, and expirations.
- Sentinel-style failover.
- Cluster mode or hash-slot sharding.

Important dependency:

- AOF canonicalization for renamed commands should land before replication, because
  replication must propagate canonical command intent, not client aliases.

## Known Bugs and Correctness Follow-ups

Open items belong here until fixed.

### Security and ACL

- `ACL CAT` must emit a RESP array header. Current malformed output can desynchronize
  clients.
- ~~Admin/dangerous ACL categories can be granted too broadly by ORed membership~~
  → addressed in V9.5.1 by stripping the `CAT_READ`/`CAT_WRITE` base from control-plane
  commands (tagging fix), so `@read`/`@write` no longer intersect them. No `guard_cats`.
- `SMOVE` key checks are imprecise until a resolver checks only source and destination.
- `MEMORY` and `OBJECT` key subcommands need key-pattern ACL checks at `cmd[2]`.
- Full Redis ACL rule-order fidelity is not implemented. Current compiled form does
  not preserve "last match wins" rule history.
- Pub/Sub channel ACL patterns exist conceptually but remain no-op until Pub/Sub lands.
- `nopass` users, selectors, `sanitize-payload`, `ACL LOAD`, and `ACL SAVE` are not
  implemented.

### Config and Command Surface

- Unknown `CONFIG SET` behavior must be made explicit and tested.
- Full `CONFIG GET/SET` coverage is incomplete; current real parameters are focused on
  memory/security/config basics.
- `COMMAND`, `COMMAND DOCS`, and `COMMAND COUNT` are not implemented; `redis-cli`
  interactive mode may probe them.

### Data Correctness

- `ZREM` does not drop an emptied zset. Redis removes the key; `ZPOPMIN` already does.
- Add restart-level persistence tests for mutating commands that rewrite TTLs or remove
  keys, especially `GETEX`, `GETDEL`, `ZPOPMIN`, eviction `DEL`, and renamed commands.

### Persistence and AOF

- `BGREWRITEAOF` in flight at shutdown is silently discarded. `aof_write_snapshot()`
  (`aof.cpp`) never renames its own tmp file — by design, since only the parent holds
  the mid-rewrite write delta (`g_data.g_aof_rewrite_buf`) needed to finalize it — but
  the parent only finalizes (`aof_check_background_rewrite()`, appends delta + renames
  `appendonly.aof.tmp` -> `aof_path`) from inside the main poll loop
  (`server.cpp:644`). Shutdown blocks on `g_rdb_child_pid` before saving
  (`server.cpp:657-661`) but has no matching wait/finalize for `g_aof_child_pid`, so a
  rewrite child that is still running (or that finishes moments after the parent exits)
  never gets reaped, its finished `.tmp` file never gets renamed in, and the old AOF is
  kept as-is with no error printed. Reproduced: `BGREWRITEAOF` immediately followed by
  Ctrl-C left a complete, orphaned `appendonly.aof.tmp` on disk while `appendonly.aof`
  stayed unchanged. Fix: extract the finalize step (delta append + rename + reopen fd)
  out of `aof_check_background_rewrite()` into its own function; at shutdown, add a
  blocking `waitpid(g_aof_child_pid, &status, 0)` mirroring the existing RDB one, then
  call that finalize function directly instead of relying on the next poll tick.

### Testing Gaps

- Add explicit security tests for control-plane category gating (V9.5.1 tagging),
  renamed commands, disabled commands, audit logging, and precise key ACLs.
- Keep intentionally destructive or server-crashing edge cases behind an explicit test
  flag.
- Add AOF restart checks to verify canonicalized renamed writes.
- Add one test that `ACL CAT` reply framing is a valid RESP array.

## Resolved Bugs Archive

This section records fixed bugs without scattering them through milestone text.

### Persistence and AOF

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

- `RDB` save fork/malloc deadlock was avoided by serializing in the parent before fork.
- RDB loaders gained bounds checks.
- RDB save uses `.bak` rotation before atomic rename.
- `INFO` buffer was increased and `snprintf` length handling was clamped to avoid OOB
  reads.
- `accept()` loop, `EINTR`, `SO_REUSEADDR`, `TCP_NODELAY`, and thread-pool shutdown
  were hardened during the project-wide review.

## Design Decisions

### Dispatch Table

Current state:

- Command dispatch uses `CmdSpec` entries in `k_cmd_table`.
- `CmdSpec` owns handler, arity, write flag, AOF rewrite flag, ACL categories, and
  key spec.
- `acl_init_categories()` mutates the table at boot to derive categories from
  `is_write`.

Tradeoff:

- Deriving `CAT_READ` and `CAT_WRITE` avoids duplicated truth in the initializer.
- Losing `const` on `k_cmd_table` is the cost.

Preferred upgrade:

- Replace `acl_init_categories()` with `build_cmd_table()`.
- Build specs, derive categories/key specs/guard cats, then return a frozen map.
- This also enables owned command names for `rename-command`.

### AOF Write Path

- Normal successful writes use raw RESP bytes captured by `try_one_request`.
- TTL-sensitive writes use rewrite paths that emit deterministic absolute commands.
- No-op writes are mutation-gated by `g_writes_since_save`.
- Renamed commands must use canonicalized AOF frames, not raw aliases.

### ACL Model

Current deliberate simplification:

- Permissions compile to `allow_cats` plus `cmd_overrides`.
- Enforcement is O(1) and does not replay raw ACL token history per request.

Known gap:

- Redis's ordered last-match-wins rule composition is not fully preserved.

Upgrade path:

- Store raw ordered rule tokens alongside the compiled form.
- Replay tokens at `ACL SETUSER` time into the compiled form.
- Keep request-time enforcement O(1).
- Use raw tokens for `ACL LIST` and `ACL GETUSER` output only.

### Memory Eviction

- MYRED uses best-of-`maxmemory_samples` eviction sampling.
- It does not implement Redis's persistent 16-slot eviction pool yet.
- This is acceptable for the project scale and avoids stale-entry validation
  complexity.
- Revisit only if realistic cache workloads show poor hit rates.

### Compact Encodings

- MYRED currently uses heavyweight structures for all collection sizes.
- `OBJECT ENCODING` reports honest MYRED names.
- Listpack/intset/quicklist-style encodings are future memory optimizations, not
  correctness requirements.

### Windows Port

- Windows support is not a simple socket port because persistence relies on `fork()`
  for `BGSAVE` and `BGREWRITEAOF`.
- A portable snapshot design must come before a serious Windows build.

## Completed Milestones

### V5.2 - String Command Expansion [Done]

Implemented:

- Variadic `DEL` and `EXISTS`.
- `INCR`, `DECR`, `INCRBY`, `DECRBY`, `INCRBYFLOAT`.
- `SETNX`, `SETEX`, `PSETEX`, `GETSET`, `GETEX`, `GETDEL`.
- `MSET`, `MGET`, `MSETNX`.
- `APPEND`, `STRLEN`, `GETRANGE`, `SETRANGE`.

Important implementation notes:

- Integer operations guard signed overflow before addition or negation.
- `INCRBYFLOAT` rejects NaN and infinity and returns bulk strings.
- `GETRANGE` follows Redis negative-index and clamping behavior.
- `SETRANGE` zero-pads and rejects offsets past the 512 MB limit.

### V5.3 - Project-Wide Code Review [Done]

Major outcomes:

- `Entry::val` became a `std::variant`.
- Dispatch moved from long if/else chain to `CmdSpec`.
- Common error constants were introduced.
- RNG moved from `srand(time(NULL))` to `std::mt19937_64`.
- Lazy expiry was centralized.
- Direct emit paths reduced temporary vectors.
- `glob_match` became iterative.
- `container_of` became a portable C++ template.
- Server and RDB hardening were improved.
- Build warnings were strengthened.

### V6 - Persistence Hardening [Done]

Implemented:

- AOF write buffering.
- Mutation-gated write logging.
- TTL translation to absolute `PEXPIREAT`.
- `appendfsync` policy support: `always`, `everysec`, `no`.
- AOF replay through the same command path.
- `BGREWRITEAOF` compaction.
- RDB/AOF startup priority.
- AOF crash recovery and truncation.
- `--check-aof` and `--fix` tooling.
- Disk-full policy for AOF write failures.
- Config-driven save triggers.

### V6 Optimization Pass [Done]

Implemented:

- One templated `aof_encode`.
- Raw RESP write path for common AOF logging.
- Hybrid AOF format: RDB preamble plus RESP delta.
- `INFO persistence` observability.
- Buffer `reserve()` improvements for AOF.

Skipped:

- `writev()` scatter-gather flush. It was not worth adding without profiling.

### V6.1 - Redis Tooling Compatibility [Done]

Implemented:

- `PING`.
- Inline protocol parsing.
- Minimal `CONFIG` compatibility, later replaced by real config work.
- `ZPOPMIN`.
- Variadic `ZADD`.

Remaining optional tooling gap:

- `COMMAND`, `COMMAND DOCS`, and `COMMAND COUNT`.

### V7 - Memory Management [Done]

Implemented:

- Incremental memory accounting with `Entry::mem`.
- `used_memory` and `INFO memory`.
- `maxmemory`.
- Redis-style eviction policy names.
- LRU and LFU metadata.
- Sampling-based victim selection.
- Write-path eviction and OOM handling.
- AOF propagation of evictions.
- `MEMORY USAGE`, `MEMORY STATS`, `MEMORY DOCTOR`.
- `OBJECT ENCODING`, `OBJECT IDLETIME`, `OBJECT FREQ`, `OBJECT REFCOUNT`.

Deferred:

- Compact encodings.
- Shared object refcounting.
- Persistent 16-slot eviction pool.

## Backlog

### Memory and Encoding Optimizations

- `embstr` for small strings.
- Integer object storage instead of digit strings.
- `listpack` for small hashes, zsets, and lists.
- `intset` for all-integer sets.
- `quicklist`-style list storage.
- RDB and AOF support for multiple internal encodings.
- Accurate small-object memory accounting after compact encodings land.
- Optional jemalloc link plus allocator-stats-backed `INFO memory`. Today
  `mem_fragmentation_ratio` is RSS divided by `used_memory`, which conflates
  connection buffers, AOF buffers, and allocator overhead with keyspace data.
- Account per-connection buffer memory (`Conn::incoming`/`Conn::outgoing`)
  separately, Redis-style `INFO clients` / `MEMORY STATS` fields, so a slow
  reader draining `KEYS` output is visible as client memory, not fragmentation.
- Active defragmentation is explicitly deferred until after compact encodings;
  with the current one-allocation-per-node structures there is nothing useful to
  compact.

### Object Sharing

- Shared small-integer pool.
- Real object refcounts.
- Copy-on-mutate behavior.

### Command Coverage Gaps

Sorted sets:

- `ZINCRBY`
- `ZCARD`
- `ZCOUNT`
- `ZMSCORE`
- `ZPOPMAX`
- `ZRANGEBYSCORE`
- `ZRANGEBYLEX`
- `ZREVRANGE`
- `ZREMRANGEBYRANK`
- `ZREMRANGEBYSCORE`
- `ZREMRANGEBYLEX`
- `ZUNIONSTORE`
- `ZINTERSTORE`
- `ZDIFFSTORE`
- `ZRANDMEMBER`
- `ZSCAN`
- `ZLEXCOUNT`
- `ZRANGESTORE`
- `ZMPOP`

Strings and bitmaps:

- `SETBIT`
- `GETBIT`
- `BITCOUNT`
- `BITPOS`
- `BITOP`
- `BITFIELD`
- `SUBSTR`
- `LCS`

Generic:

- `COPY`
- `SORT`
- `SORT_RO`
- `DUMP`
- `RESTORE`
- `EXPIRETIME`
- `PEXPIRETIME`
- `OBJECT HELP`
- `SCAN ... TYPE`
- `WAIT`

Hashes:

- `HRANDFIELD`
- `HINCRBYFLOAT`

Lists:

- `LPOS`
- `LMOVE`
- `RPOPLPUSH`
- `LMPOP`
- `BLPOP`
- `BRPOP`
- `BLMOVE`

Sets:

- `SINTERCARD`

New data types:

- HyperLogLog (`PF*`)
- Streams (`X*`)
- Geo (`GEO*`)
- Bitmaps as a first-class area

### Server Observability and Tooling

- `CLIENT LIST`
- `CLIENT KILL`
- `CLIENT SETNAME`
- `CLIENT GETNAME`
- `CLIENT ID`
- `HELLO` and RESP3 handshake
- `RESET`
- `SLOWLOG`
- `LATENCY`
- `MONITOR`
- `DEBUG`
- `SHUTDOWN`
- `LASTSAVE`
- `TIME`
- Full `CONFIG GET/SET` surface

### Platform Work

- Portable background snapshot design without `fork()`.
- Windows socket layer using `WSAPoll`.
- `WSAStartup` and `WSACleanup`.
- `FlushFileBuffers` replacement for `fdatasync`.
- Path handling and config path portability.

### Event Loop and Connection Scaling

Current shape: one `poll()` loop that rebuilds `poll_args` from the whole
`fd2conn` vector every tick (`server.cpp`), a 64 KB stack staging buffer in
`handle_read` copied into `Conn::incoming`, and no ceilings on connection count
or buffer growth. Upgrades, in dependency order:

- Per-connection limits first (they are correctness/DoS issues, not just scale):
  a `maxclients` directive enforced in `handle_accept` with a
  `-ERR max number of clients reached` reply, an input cap on `Conn::incoming`
  (a frame that legally declares `k_max_args` bulks of `k_max_msg` bytes can
  demand terabytes today), and Redis-style `client-output-buffer-limit` classes
  on `Conn::outgoing` so a slow reader of `KEYS`/`HGETALL` output gets
  disconnected instead of ballooning the heap.
- Read directly into the connection buffer: give `Buffer` a
  `buf_reserve(n)`/writable-tail API and `read()` straight into `data_end`,
  removing the 64 KB memcpy per read in `handle_read`.
- `epoll` backend behind a tiny interface (`event_loop_add/mod/del/wait`),
  keeping `poll()` as the portable fallback. This kills the O(connections)
  rebuild per tick and is a prerequisite for any 10k-connection claim. Design
  the interface so the future Windows `WSAPoll` port is a third backend.
- Unix domain socket support (`unixsocket` directive) — trivially fits the
  existing `listen_fds` vector and skips protected-mode/allowlist concerns for
  local tooling.
- Only after the above: optional io-threads (Redis 6 model). Threads only do
  read+parse and serialize+write; command execution stays on the main thread, so
  `g_data` keeps its single-writer discipline. The thread pool from
  `thread_pool.cpp` is not reusable for this (it has no per-connection affinity);
  plan a dedicated design doc before starting.

### Multiple Logical Databases

`SELECT`, `SWAPDB`, `MOVE`, and `COPY ... DB` need real database indexes.
Concrete approach for the current code:

- `GlobalData::db` becomes `std::vector<HMap> dbs` (default 16, `databases`
  directive) plus a `Conn::db_index`.
- The TTL heap can stay global: `HeapItem::ref` already points back into the
  `Entry`, but expiry deletion needs the owning table, so `Entry` gains a small
  `uint8_t db` field (fits existing padding next to `type`).
- `SCAN`/`KEYS`/`DBSIZE`/`FLUSHDB` become per-index; `FLUSHALL` iterates all.
- RDB format: bump the version and emit per-entry db byte or `SELECTDB`-style
  records; AOF replay needs a synthetic `SELECT` frame when the writer's index
  changes (same canonicalization channel as rename-command).
- `INFO keyspace` reports `db0:keys=...,expires=...` lines per non-empty db.

### Scripting (EVAL)

Largest remaining Redis-compat feature after Pub/Sub and transactions. Sketch:

- Link Lua 5.4 (vendored single-directory build keeps the no-dependency spirit;
  system liblua as fallback).
- `EVAL`/`EVALSHA`/`SCRIPT LOAD|EXISTS|FLUSH` with a script cache keyed by SHA-1
  of the body (add SHA-1 next to the existing `sha256.h`, or key the cache by
  full body initially and defer SHA-1).
- `redis.call()` re-enters `do_request` with a synthetic reply buffer that gets
  translated RESP→Lua tables; errors become Lua errors.
- Persistence/replication rule: log *effects*, not scripts. Every write a script
  makes already flows through the normal handlers, so the existing
  `g_writes_since_save` gate plus `aof_feed`/`aof_append_raw` capture the write
  stream — but raw-frame capture must be disabled inside scripts (there is no
  client frame), so script-initiated writes always take the `aof_feed` re-encode
  path. This mirrors the rename-command canonicalization rule.
- Atomicity is free (single-threaded loop), but the OOM and MISCONF write gates
  in `do_request` must run per `redis.call`, and a script execution time limit
  needs a Lua debug hook.

### Structured Logging and Daemonization

Everything logs via bare `fprintf(stderr, ...)` today (server, rdb, aof, config
parsing). Before the audit log (V9.5.4) grows siblings:

- A leveled logger: `loglevel debug|verbose|notice|warning`, `logfile <path>`,
  timestamps, single `write()` per line.
- Fork-safety rule stays: children (`rdb_write_snapshot`, `aof_write_snapshot`)
  only use `write()` on an already-open fd — the logger API must expose that
  path.
- `daemonize yes` + `pidfile`; optional syslog. This is what makes protected
  mode, audit events, and `MISCONF` states operationally visible instead of lost
  on a detached stderr.

### Differential and Fuzz Testing

- Differential harness: drive the same randomized operation stream through
  redis-py against both a real `redis-server` and MYRED, diff replies, with a
  normalization table for deliberate divergences (e.g. the V9.5.1 ACL tagging
  rule). This mechanically catches semantics drift of the "SET should discard
  TTL" class that hand-written assertions miss.
- libFuzzer/AFL harnesses for `parse_resp_request` and `rdb_load_buffer` — both
  are pure functions over byte buffers, so harnesses are ~20 lines each. Corpus
  seeds: real AOF/RDB files from the test scripts.
- An ASan/UBSan CMake build type (`-fsanitize=address,undefined`) and a CI lane
  that runs `stress_test.py --correctness-only` under it. The intrusive
  `container_of` pattern and manual `Buffer` management are exactly the code
  shapes sanitizers pay off on.

## Testing Matrix

Primary harness:

```bash
python3 stress_test.py --password kek1234
python3 stress_test.py --password kek1234 --correctness-only
python3 stress_test.py --password kek1234 --stress-only --stress-threads 16 --stress-ops 2000
```

Persistence helpers:

```bash
scripts/test_aof.sh
scripts/test_aof_rewrite.sh
scripts/test_aof_hybrid.sh
scripts/diag_live.sh
scripts/diag_ttl.sh
```

Benchmarking:

```bash
redis-benchmark -p 1234 -a kek1234 -t set,get,incr,lpush,rpush,lpop,rpop,sadd,hset -n 200000 -c 50 -P 16 -q
```

Security test focus:

- `AUTH` success, failure, and disconnect after repeated failures.
- `AUTH <user> <pass>`.
- `ACL SETUSER` and config-file round trip.
- Command denial.
- Key-pattern denial.
- Protected-mode rejection.
- Allowlist rejection.
- Audit log redaction.
- Renamed command canonical AOF behavior.

## Architecture Notes

- Single-threaded `poll()` event loop.
- Non-blocking sockets.
- `TCP_NODELAY` on accepted sockets.
- Thread pool for background work and large async deletes.
- `fork()` based `BGSAVE` and `BGREWRITEAOF`.
- Top-level database is a dual-table HMap with progressive rehashing.
- `hm_scan` uses reverse-binary cursor iteration.
- Entry runtime types:
  - `T_STR = 1`
  - `T_ZSET = 2`
  - `T_DLIST = 3`
  - `T_HASH = 4`
  - `T_SET = 5`
- RDB tags are separate from runtime entry tags:
  - string = 0
  - zset = 1
  - list = 2
  - hash = 3
  - set = 4
- TTL is monotonic in memory and wall-clock on disk.
- Python stress harness is useful for correctness and concurrency, not peak server
  throughput.
