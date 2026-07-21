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
- When a bug is fixed, record it in `docs/CODE_REVIEW.md` → `Resolved Bugs Archive`
  (the archive moved out of this file on 2026-07-13).
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

Date: 2026-07-11.

Primary commands:

```bash
cmake -B build
cmake --build build
./build/server
python3 scripts/stress_test.py --password kek1234
python3 scripts/stress_test.py --password kek1234 --correctness-only
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
| ACL foundation and hardening | Implemented (V9.4–V9.5) |
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

#### `[Done]` V9.5 - Command Hardening and Audit Log — 2026-07-11

Closed the post-ACL security gaps before TLS. All substeps done, in order:

- `[Done]` **V9.5.1 - ACL category semantics.** `acl_init_categories()` strips
  `CAT_READ`/`CAT_WRITE` from any admin/dangerous command, so `+@read`/`+@write` can no
  longer reach `CONFIG`/`ACL`/`KEYS`/`MEMORY`/`OBJECT`/`FLUSHALL`; `acl_check` stays a
  single O(1) `&` test. `acl_check` also reordered before the arity check so an
  unauthorized user gets `NOPERM`, never an arity/shape leak. (Design Decisions: ACL
  Category Tagging.)
- `[Done]` **V9.5.1a - Real `KEYS` glob.** `do_keys` filters through `glob_match`; bare
  `keys`/`keys *` keep the no-copy streaming fast path.
- `[Done]` **V9.5.2 - Precise key resolution.** Optional subcommand-aware
  `CmdSpec::key_resolver` overrides the `KeySpec` enum for `SMOVE` (source/dest only) and
  `OBJECT`/`MEMORY` (key at `cmd[2]`); `acl_key_allowed` widened to `string_view`.
- `[Done]` **V9.5.3 - `rename-command` / disable.** Boot-built owning `g_dispatch`
  (`{canonical, spec}`) over the `k_cmd_table` template; ACL and AOF use the canonical
  name so aliases never leak into the log. `AUTH` is matched by literal name (not a table
  command) and cannot be renamed. Config-file only; validated and persisted.
- `[Done]` **V9.5.4 - Audit log.** `auditlog ""|stderr|<path>`; one `write()` per line to
  an `O_APPEND|O_CLOEXEC` fd; events `auth_success`/`auth_fail`, `acl_deny`, `acl_change`,
  `admin_command`/`dangerous_command`, `accept_reject`; never logs secrets.
- `[Done]` **V9.5.5 - Protocol/metadata cleanup.** `ACL CAT` emits its RESP array header;
  `CONFIG SET` rejects unknown params and boot-only `rename-command`;
  `metadata_selfcheck()` fails loud at boot if any command has `acl_cats==0` or a
  control-plane command still carries `@read`/`@write`.

Deferred to Backlog: `stress_test.py` ACL/rename/audit coverage; `INFO audit_last_error`
exposure; optional `auditlog-events` / `auditlog-required` filters.

#### V9.6 - Password Hashing Upgrade [Done]

Credentials at rest upgraded from unsalted SHA-256 to Argon2id (OWASP baseline
m=19456 KiB, t=2, p=1) with async verification, while every pre-existing config stays
loadable. All substeps V9.6.1–V9.6.5 shipped; milestone closed 2026-07-18.

- `[Done]` **N5 prerequisite** — 2026-07-12. `sha256_hex` padding bug (`len%64 >= 56`)
  fixed; KAT-verified against NIST vectors + hashlib (lengths 0–200).
- `[Done]` **V9.6.1 - Credential abstraction** — 2026-07-12. `cred.h/cred.cpp`:
  `cred_hash_new` / `cred_verify` / `cred_needs_rehash` + `cred_dummy()`. Stored
  credentials are self-describing strings: 64-hex legacy SHA-256 (verified forever)
  vs `$argon2id$...` PHC. All six write/verify sites switched; PHC tokens round-trip
  `acl_apply_rule`, `requirepass`, `acl_format_user`, and config rewrite. CMake
  `MYRED_ARGON2` (default ON) with SHA-256 fallback + boot warning.
- `[Done]` **V9.6.2 - Async verification** — 2026-07-12. eventfd completion channel
  (`loop_post`/`loop_drain`); `AuthJob` runs `cred_verify` on the thread pool
  (deep-copied hash snapshot, worker-wiped plaintext) and `auth_complete` applies the
  result on the loop. `Conn::id` liveness (fd-reuse safe), `auth_pending` pipeline
  gating + `conn_resume`, `k_max_auth_inflight=4` → `-BUSY` (~76 MiB Argon2 cap),
  unmatchable random dummy for unknown/disabled users (uniform timing class, can
  never authenticate). Proof on an argon2 build: PING p50=3.84 ms / p99=6.55 ms on
  another conn during a 4-thread AUTH storm (a sync verify would sit at 20–60 ms+).
- `[Done]` **V9.6.3 - Migration and rotation** — 2026-07-13. Rehash-on-AUTH: the
  worker computes `cred_hash_new` in the only window where plaintext exists; the
  completion applies it by value-matched compare-and-swap (safe against mid-verify
  rotation), syncs `g_config.password` for the default user so `CONFIG REWRITE`
  persists the upgrade, and audits `cred_rehash` (username only). Config is never
  auto-rewritten; a restart before rewrite just re-migrates on first AUTH.
  `CONFIG SET requirepass` / `ACL SETUSER >plain` hash to Argon2id directly (V9.6.1).
  `test_async_auth.py` green incl. the migration + audit-redaction test.

##### `[Done]` V9.6.4 - Audit bug sweep (pre-TLS cleanup) — 2026-07-17

Fix every open bug the audits have found before starting TLS. V9.7 explicitly depends
on N3 (timer busy-loop) and N4 (protocol-error wedge), and TLS buffering must not land
on top of open loop/persistence bugs.

- Worklist: `docs/CODE_REVIEW.md` → **"Consolidated Bug Audit — 2026-07-13"**. Every
  ROADMAP known bug and every 2026-07-07/07-09 finding was re-verified against the
  2026-07-13 tree: fixed items are recorded with evidence, open items are ranked
  (🔴 5, 🟠 14, 🟡 13, plus 🔵/⚪ polish — N1 + N2 fixed 2026-07-13, N21 filed) with a
  suggested fix order.
- Headline finding **N1 closed 2026-07-13**: the AOF replay user never set
  `all_keys`, so `acl_check`'s key gate NOPERM'd every keyed RESP-tail command on
  every restart. Fixed (`replay_user.all_keys = true` + replay-error counting),
  regression `scripts/test_aof_restart.py`. That test also surfaced **N21** (idle server
  defers rewrite finalize — filed 🟠, fix with N3/N14). Next up: fix-order step 2,
  N3 + N4 (TLS prerequisites).
- Done criteria: every 🔴/🟠 item closed (or explicitly re-filed to Backlog with a
  reason); each fix lands with a regression test where feasible; statuses ticked in
  CODE_REVIEW.md and fixed items recorded in its Resolved Bugs Archive.
- **Closed 2026-07-17**: all 🔴/🟠/🟡 done 2026-07-13; all 🔵/⚪ perf/polish done
  2026-07-16/17 (`hash_set`/`set_add` move semantics, 64-bit FNV-1a, `mem_selfcheck`
  placement, `hm_random` + O(k) SPOP/SRANDMEMBER, incremental eviction with
  `evict_tick`, O(1) INFO keyspace stats). Bonus fixes found en route: SPOP AOF
  replay determinism (`aof_self` + synthetic SREM), 🔴 RDB non-TTL set loader data
  loss, ECHO command + empty-inline ignore (redis-cli --pipe compat). New regression
  script: `scripts/test_evict_tick.sh`. Testing debt rolled into V9.6.5.

##### `[Done]` V9.6.5 - General and speed test — 2026-07-18

Full-surface validation pass before TLS: general correctness testing plus a speed
baseline, absorbing the Testing debt items from the V9.6.4 audit (moved here
2026-07-17 from CODE_REVIEW):

- General tests: AOF-restart-with-ACL (pairs with N1); restart tests for `GETEX`,
  `GETDEL`, `ZPOPMIN`, eviction `DEL`, renamed-command canonicalized frames;
  security tests (control-plane category gating, renamed/disabled commands,
  audit-log redaction, precise key ACLs, `ACL CAT` RESP framing); destructive/
  server-crashing edge cases behind an explicit test flag.
  - **Suites GREEN 2026-07-18** (both with `--destructive`; en route they caught
    and got fixed: 🔴 rename-command AOF-restart brick, 🟠 nopass round-trip):
    `scripts/test_restart_matrix.py`
    (GETEX ttl / aliased-GETDEL canonical frames / ZPOPMIN / eviction-DEL exact
    keyspace replay; `--destructive` adds SIGKILL crash recovery) and
    `scripts/test_security.py` (category gating, rename/disable, audit
    redaction, key ACLs incl. SMOVE resolver, ACL CAT framing, CONFIG REWRITE
    round-trip across restart; `--destructive` adds protocol abuse). Shared
    helpers in `scripts/myred_testlib.py`. Both spawn private instances on
    ports 12401/12402 — safe next to a live server. Watchpoints these may
    expose: `acl_format_user` emits `nopass` but `acl_apply_rule` has no
    `nopass` token (round-trip may fail to load), and AOF replay of canonical
    names while `rename-command` is active.
- Speed test: benchmark baseline (`redis-benchmark` where the command surface
  allows, plus timed `stress_test.py` runs). Record the numbers — V9.7.1's
  transport-seam refactor must prove zero perf regression against this baseline.
  - **BASELINE RECORDED 2026-07-18 — the V9.7.1 zero-regression reference.**
    Release build, WSL2 loopback, `-n 100000 -c 50 -P 16` (`--bench`), post
    delta-accounting, ops/sec:
    PING 1.01M · SET 1.06M · GET 1.02M · INCR 1.00M · LPUSH 870k · RPUSH 935k ·
    LPOP 990k · RPOP 885k · SADD 917k · HSET 926k · SPOP 1.02M · ZADD 917k ·
    ZPOPMIN 1.06M · MSET(10 keys) 327k · LRANGE 100/300/500/600 el:
    103k/41.5k/25.5k/16.9k (output-bound, scales ~linearly with range).
    Growth check (`-r 100000 -n 200000`): SADD 656k · HSET 629k · ZADD 299k —
    flat as containers grow. Full log in `docs/stress_results.md`.
  - **Native-Linux reference (same commit, laptop, no WSL), 2026-07-18** —
    uniformly ~2.2-2.5x the WSL numbers, confirming the WSL baseline was
    environment-limited, not server-limited: PING 2.78M · SET 2.22M · GET
    2.50M · INCR 2.22M · LPUSH 2.00M · RPUSH 2.13M · LPOP 2.33M · RPOP 2.27M ·
    SADD 2.08M · HSET 2.08M · SPOP 2.63M · ZADD 1.92M · ZPOPMIN 2.27M ·
    MSET(10) 833k · LRANGE 100/300/500/600: 211k/74k/44k/36k. Concurrent
    stress phase: p50 0.21ms, max 17ms, `KEYS` contention cost 13ms (vs 333ms
    on WSL — the WSL tail was loopback queueing, not server work). V9.7.1
    regression comparisons must be same-machine, same-environment.
- Harness updated 2026-07-17 (hold lifted): moved to `scripts/stress_test.py` (docs
  already pointed there), new coverage for ECHO + inline protocol + empty-inline
  ignore, FLUSHDB, SPOP/SRANDMEMBER edge semantics + randomness distribution,
  O(1) INFO keyspace stats, incremental eviction (EVICT_RUNNING); new `--bench`
  flag shells out to redis-benchmark for the speed baseline. Full suite green
  (552 checks) against an isolated instance.
- **First baseline findings (2026-07-17):** (a) benchmarks are only meaningful on a
  Release build — the default `build/` is `CMAKE_BUILD_TYPE=Debug` (no `-O`, no
  `-DNDEBUG`), so `mem_selfcheck` walks the whole keyspace after every command;
  use `cmake -B build-rel -DCMAKE_BUILD_TYPE=Release` for numbers. (b) a real
  release-mode perf bug fell out: `mem_reaccount` is O(container size) per
  mutation — filed in Known Bugs.

#### V9.7 - TLS [In progress]

TLS is the heaviest security feature. The event loop, not OpenSSL, is where the risk
lives: today plaintext I/O touches exactly four places — accept
(`handle_accept`, `server.cpp:71`), read (`handle_read`, `server.cpp:410`), write
(`handle_write`, `server.cpp:391`), close (`conn_destroy`) — and readiness is derived
purely from application intent (`want_read`/`want_write`). TLS breaks that assumption,
so the milestone is ordered to absorb the breakage before any crypto exists.

Prerequisites: CODE_REVIEW 2026-07-09 N3 (timer busy-loop) and N4 (protocol-error
wedge / missing input caps) — TLS multiplies buffering complexity and must not land on
top of a loop that spins or buffers unboundedly.

##### V9.7.1 - Transport seam (zero-behavior-change refactor, no OpenSSL yet) [Done 2026-07-19]

- Introduce a per-conn transport layer and route ALL socket I/O through it:

  ```cpp
  enum class IoResult { OK, WANT_READ, WANT_WRITE, PEER_CLOSED, ERR };
  IoResult tr_read (Conn *c, uint8_t *buf, size_t cap, size_t *n);
  IoResult tr_write(Conn *c, const uint8_t *buf, size_t len, size_t *n);
  void     tr_close(Conn *c);   // plaintext: close(fd)
  ```

- The key semantic change the loop must learn now: with TLS, a *read* attempt can
  demand POLLOUT and a *write* attempt can demand POLLIN (handshake and record
  processing). So poll flags become `application intent + transport demand`: add
  `Conn::tr_want_read`/`tr_want_write` set from `IoResult`, and OR them into `pfd.events`
  next to the existing flags. The `assert(conn->want_read)` /
  `assert(conn->want_write)` pairs in the poll loop must be relaxed accordingly.
- Plaintext maps trivially (`EAGAIN` on read → WANT_READ, on write → WANT_WRITE), so
  the whole stress suite verifies this refactor with zero crypto in the build. Do not
  start V9.7.2 until it is green.

##### V9.7.2 - Context, config, listeners [Done 2026-07-19]

- Config: `tls-port` (0 = disabled; may coexist with plaintext `port`),
  `tls-cert-file`, `tls-key-file`, `tls-ca-cert-file`,
  `tls-auth-clients yes|no|optional`. All boot-only at first; wire through
  `config_apply` + `config_rewrite` like the V9.1 directives.
- One global `SSL_CTX` at boot: `TLS1.2` minimum (`SSL_CTX_set_min_proto_version`),
  `SSL_OP_NO_RENEGOTIATION`, default ECDHE ciphers, and
  `SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER` — the second
  flag is mandatory because `Buffer` slides/reallocs between write retries
  (`buf_consume`/`buf_append`), which vanilla OpenSSL rejects on a retried
  `SSL_write`.
- Listener vector becomes `{fd, bool is_tls}`; `handle_accept` on a TLS listener
  creates the `SSL`, `SSL_set_fd`, `SSL_set_accept_state` — and returns. No
  synchronous `SSL_accept`; the handshake is loop-driven state.
- Accept-time policy (allowlist, protected mode, `audit_reject`) stays where it is:
  those checks are IP-based and need no handshake.

##### V9.7.3 - Handshake as connection state [Done 2026-07-19]

- `Conn::tls_handshaking = true` until `SSL_do_handshake` returns 1. While set, the
  poll loop calls `SSL_do_handshake` on readiness and maps
  `SSL_ERROR_WANT_READ/WANT_WRITE` to the transport-demand flags; on success it clears
  the flag and enters the normal read-intent state.
- Handshake timeout rides the existing io_list timer machinery — the conn is already
  on `io_list` with `k_io_timeout_ms`; add a tighter `tls-handshake-timeout`
  (default 10 s) checked in `process_timers` for handshaking conns. A TCP connect that
  never speaks TLS must not hold a slot for 30 s.
- Failed handshakes: `audit_event("tls_handshake_fail", ...)`, destroy. AUTH remains
  required after the handshake (mTLS-derived identity is explicitly out of scope until
  a later step).

##### V9.7.4 - Data path rules

`handle_read`/`handle_write` swap `read()`/`write()` for `tr_read`/`tr_write`. Three
OpenSSL behaviors must be encoded as rules, or they become heisenbugs:

- **Classify with `SSL_get_error`, never errno.** `SSL_ERROR_ZERO_RETURN` = clean
  close-notify → `want_close`. `SSL_ERROR_SYSCALL` with 0 return = dirty EOF.
- **Drain `SSL_pending()` after every successful `SSL_read`.** Decrypted bytes can sit
  inside the SSL object with nothing left on the socket; returning to `poll()` without
  draining stalls the reply until the peer happens to send another byte. The
  `while (try_one_request(conn))` loop already handles multi-command buffers — the
  transport must guarantee it received *all* currently decryptable bytes.
- **`SSL_shutdown` once, best-effort, then `SSL_free`** in `tr_close`. Do not wait for
  the peer's close-notify; a synchronous bidirectional shutdown is a hang primitive.

##### V9.7.5 - Optimizations (strictly after correctness)

- **Session resumption first** — biggest win, near-zero code:
  `SSL_CTX_set_session_cache_mode(SSL_SESS_CACHE_SERVER)` plus default TLS 1.3
  tickets. Reconnect-heavy tooling (`redis-benchmark` without `-k`) drops from full
  handshakes to resumed ones.
- **Record-sized flushes**: TLS records cap at 16 KB; the single `outgoing` Buffer
  already batches pipelined replies into large `tr_write` calls — keep that property,
  never introduce per-reply `SSL_write` calls.
- **`SSL_MODE_RELEASE_BUFFERS`**: reclaims ~34 KB per idle connection.
- **Handshake CPU**: if accept storms show up, mitigate in this order — resumption,
  accept-rate cap per tick, and only as a last resort offload `SSL_do_handshake` to
  the thread pool using the V9.6.2 completion channel (same conn-id liveness rule).
- **kTLS** (`SSL_OP_ENABLE_KTLS`): measure before adopting; not planned.
- **Cert reload without restart** (explicitly last): build a fresh `SSL_CTX`, swap the
  global pointer; existing conns keep the old ctx alive via OpenSSL refcounting.

Tests / done criteria:

- `redis-cli --tls --cacert ...` runs the correctness suite against `tls-port`;
  plaintext and TLS listeners serve simultaneously.
- Python harness gains `--tls` (`ssl.wrap_socket` over the existing client).
- Handshake-timeout test: TCP connect, send nothing, conn reaped at the TLS deadline.
- Mid-handshake disconnect and mid-write disconnect leak nothing (run under ASan).
- Large pipeline (>16 KB replies) over TLS byte-identical to plaintext.
- V9.7.1 refactor alone passes the full plaintext stress suite unchanged.

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

Consolidated 2026-07-13: every open bug that lived here was moved to
`docs/CODE_REVIEW.md` → **"Consolidated Bug Audit — 2026-07-13 (V9.6.4 worklist)"**,
where each item was re-verified against the current tree, ranked by severity, and
given a fix order. That section is the working list while V9.6.4 runs. New bugs still
get filed here first, then folded into that audit.

Feature gaps that were listed here (missing features, not defects) moved to Backlog →
"ACL and Command-Surface Feature Gaps".

- **`rename-command` bricks the server on AOF restart** 🔴 — FIXED 2026-07-17
  (replay-only `k_cmd_table` fallback in `do_request` when `g_loading`;
  `scripts/test_restart_matrix.py --destructive` green). Original filing follows.
  (filed 2026-07-17, found by `scripts/test_restart_matrix.py`): the AOF
  logs canonical names by design (V9.5.3), but `dispatch_build` *erases* the
  canonical entry when a command is renamed (`commands.cpp:3384`). On the next
  boot, replaying any canonical frame of a renamed command returns "unknown
  command" → `aof_load` returns false → `fatal_exit("AOF load failed...")`
  (`server.cpp:597-600`). Any write through a renamed command = server refuses
  to start until the rename is removed. Fix: during replay (`g_data.g_loading`),
  fall back to `k_cmd_table` for canonical names that miss `g_dispatch`.

- **`nopass` breaks the ACL config round-trip** 🟠 — FIXED 2026-07-17 (`nopass`
  token accepted in `acl_apply_rule`, clears `pw_hashes`;
  `scripts/test_security.py --destructive` green). Original filing follows.
  (filed 2026-07-17, found by `scripts/test_security.py`): `acl_format_user`
  writes ` nopass` for a user with no credentials (`commands.cpp:3123`), but
  `acl_apply_rule` has no `nopass` token, so the config written by
  `CONFIG REWRITE` fails to load ("Invalid ACL rule 'nopass'") and the server
  won't boot. Fix: accept `nopass` in `acl_apply_rule` (clear `pw_hashes`,
  same as `resetpass`).

- **`mem_reaccount` is O(container size) per mutation** 🔵 — FIXED 2026-07-18.
  Phase 1 (lists: `Deque::elem_bytes` maintained counter, O(1)
  `entry_mem_usage`, debug drift detector, LTRIM `.clear()` retention fix) and
  Phase 2 (hash/set/zset via `HMap::elem_bytes`; `entry_mem_usage_sampled`
  deleted, `MEMORY USAGE` now exact) both applied. Evidence: growth benchmark
  flat instead of degrading — SADD 655.7k / HSET 628.9k / ZADD 299.4k ops/s at
  `-r 100000 -n 200000 -c 50 -P 16` (Release, WSL2). Drift-verified 2026-07-18:
  Debug-build `cb_bytes_check` audit over the full correctness suite = zero
  drift lines (after two fixes it caught: the zset_delete `#ifdef NDEBUG`
  inversion and the LINSERT SSO-residue over-subtract). (filed 2026-07-17, found by the first `--bench` baseline): `mem_reaccount` calls the full
  `entry_mem_usage` (`state.cpp:580`), which walks every element of a
  T_DLIST/T_HASH/T_SET/T_ZSET (`state.cpp:505-517`). Every LPUSH onto a 20k-element
  list walks all 20k strings — measured degrading 15.6k→1.7k ops/s as the list
  grew (Debug build amplifies but the walk exists in Release too).
  `entry_mem_usage_sampled` exists but only `MEMORY USAGE` uses it. Real fix is
  delta accounting (each mutating handler folds in the exact bytes it
  added/removed) — broad like the old INFO item, touches every mutating command;
  alternative is sampled reaccount at some accuracy cost. Needs a design decision.

- **`rdb_load_set_entry` destroys every non-TTL set on load** 🔴 — FIXED 2026-07-16
  (inner garbled skip block deleted, `entry_del` on member-read failure; `-Wshadow`
  warnings gone, build warning-free again). Original filing follows. (filed
  2026-07-16, found while auditing `set_add` call sites): the
  correct expired-set skip exists at `rdb.cpp:679-686`, but a garbled duplicate of it
  sits *inside* the member loop (`rdb.cpp:704-712`). When `has_ttl == 0`, `expire_at`
  stays 0 so `expire_at <= get_wall_msec()` is always true: after the first member it
  misreads the next member as a key, misreads raw bytes as a count, desyncs the
  cursor, returns early, and leaks `ent` — the set is lost and every entry after it
  in the RDB file is corrupted. The `-Wshadow` warnings at `rdb.cpp:701-708` point at
  exactly this block. Fix: delete the inner block; also `entry_del(ent)` before the
  `return false` on member-read failure (matches the hash loader).

- **SPOP is nondeterministic but AOF-logged verbatim** — FIXED 2026-07-16:
  `CmdSpec::aof_self` flag + `do_spop` feeds synthetic `SREM` of popped members via
  `aof_feed`. Original filing follows. (filed 2026-07-16, found while
  reworking SPOP sampling): `k_cmd_table` logs SPOP raw, so AOF replay re-runs the
  random pick and removes *different* members than the original run — silent state
  divergence that the replay error counter cannot detect (the replayed command
  succeeds). Redis propagates SPOP as SREM of the actually-popped members. Fix needs a
  handler-fed AOF path: a `CmdSpec` flag (e.g. `aof_self`) telling `do_request` not to
  log, plus `do_spop` feeding a synthetic `SREM key member...` via `aof_feed` (same
  mechanism eviction already uses for its synthetic DEL).

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

### ACL Category Tagging

The `+@read` escalation (a read-only user reaching `CONFIG`/`KEYS`/`ACL`) was a
*mistagging*, not a missing gate: `acl_init_categories()` gave `CAT_READ` to every
non-write command. Fix: strip `CAT_READ`/`CAT_WRITE` from any command carrying
`CAT_ADMIN`/`CAT_DANGEROUS`, so a command is either data-plane or control-plane, never
both — `acl_check` stays a single O(1) `&`.

Rejected: a second `guard_cats` bitmask + AND-gate. It only expresses per-command
mandatory co-requirements ("grantable by @read but also always needs @admin"), which no
real command needs; revisit only if one appears.

Divergence from Redis (deliberate): `@read` no longer implies `KEYS`/`CONFIG`, `@write`
no longer implies `FLUSHALL`. Grant explicitly (`+keys`, `+flushall`) to restore.

`AUTH` is intentionally not a `k_cmd_table` command (its handler needs `conn`); it is
matched by literal name before dispatch and can never be renamed or disabled, so no
config can lock out every client by aliasing it away.

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

### Hand-Tuned Hot Paths (Assembly / Intrinsics)

Purely opportunistic/educational track — not on the critical path of any active
milestone.

- Candidate: `str_hash` (`common.h:21-27`, FNV-1a) — called on essentially every
  keyed command (`SET`/`GET`/`HSET`/... across `commands.cpp`/`rdb.cpp`/`zset.cpp`).
  Gate any work here on profiling first (`perf record`/`perf report` against a
  `redis-benchmark` run) — don't assume it's hot without measuring.
- FNV-1a's byte-at-a-time serial dependency chain means a line-for-line asm port
  of the same algorithm won't beat `-O2`'s output. A real win needs a different
  algorithm alongside the low-level rewrite — e.g. hardware CRC32 via
  `_mm_crc32_u64`, or xxHash — not "asm-ify FNV as written."
- Preference order if pursued: compiler intrinsics (`<immintrin.h>`,
  `__builtin_*`) first — compiler still owns register allocation/ABI; a
  standalone `.s` translation unit (own CMake `enable_language(ASM)` target,
  `extern "C"` linkage) only if intrinsics can't express what's needed; inline
  `asm volatile` inside a `.cpp` last, since it's the hardest to keep clobber-list
  correct and ties the code to one compiler's dialect.

### ACL and Command-Surface Feature Gaps

Moved from "Known Bugs" 2026-07-13 — missing features, not defects:

- Full Redis ACL rule-order fidelity ("last match wins") — upgrade path recorded in
  Design Decisions → ACL Model.
- Pub/Sub channel-pattern enforcement (no-op until V8 lands).
- `nopass`, selectors, `sanitize-payload`, `ACL LOAD`, `ACL SAVE`.
- `COMMAND`, `COMMAND DOCS`, `COMMAND COUNT` (`redis-cli` interactive mode probes
  these).
- Full `CONFIG GET/SET` coverage (also under Server Observability and Tooling).

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

Largest remaining Redis-compat feature after Pub/Sub and transactions.

Decision (2026-07-14): **custom language + bytecode VM, not embedded Lua.**
Deliberately scoped as a small Redis-scripting DSL, not a general-purpose
language — no closures, coroutines, metatables, or modules. This is an
educational track (writing an interpreter from scratch) that happens to fit
the problem well: EVAL only ever needs values, branching, loops, calls, and one
privileged builtin, and a script has no state that outlives one invocation, so
the usual hardest part of a from-scratch language — GC — is not needed at all
(see memory model below). Sits in Backlog with no deadline pressure, which is
exactly the condition under which this bet is reasonable.

Pipeline:

- Lexer → recursive-descent parser → AST → single-pass compiler to flat
  bytecode (stack-based VM, not tree-walking — a tree-walker re-traverses the
  AST every run, which is both slower and not the design any real language
  ships).
- Values: nil, boolean, number (int/double, kept distinct for RESP fidelity),
  string, table/array (needed both to receive multi-bulk RESP replies and to
  build multi-bulk `redis.call` arguments).
- Memory: bump/arena allocator scoped to one `EVAL` invocation, freed wholesale
  on return. No cross-invocation state exists in Redis's EVAL model, so no GC
  is required — this is the one deliberate simplification that makes "write it
  from scratch, make it good" tractable alongside everything else in flight.
- `redis.call`/`redis.pcall` is a VM opcode that re-enters `do_request` with a
  synthetic reply buffer, translating RESP→VM values and back; `redis.call`
  errors abort script execution, `redis.pcall` catches them as a VM-level
  error value.
- Safety: the VM's dispatch loop checks an instruction counter every iteration
  against a configured max-instructions-per-script limit — the from-scratch
  equivalent of hooking Lua's `debug` API for a time limit, and simpler since
  it's just an integer compare already inside the loop.
- `EVAL`/`EVALSHA`/`SCRIPT LOAD|EXISTS|FLUSH`: cache compiled bytecode objects
  keyed by SHA-1 of the script source (add SHA-1 next to the existing
  `sha256.h`); `EVALSHA` looks up that cache directly, no recompilation.
- Persistence/replication rule (language-independent, carries over unchanged):
  log *effects*, not scripts. Every write a script makes already flows through
  the normal handlers, so the existing `g_writes_since_save` gate plus
  `aof_feed`/`aof_append_raw` capture the write stream — but raw-frame capture
  must be disabled inside scripts (there is no client frame), so
  script-initiated writes always take the `aof_feed` re-encode path. This
  mirrors the rename-command canonicalization rule.
- Atomicity is free (single-threaded loop), but the OOM and MISCONF write gates
  in `do_request` must still run per `redis.call` — the VM doesn't bypass them.

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

### Eviction Batch-Exhaustion False OOM

Low priority — park until the performance/polish pass. `free_memory_if_needed`
(`commands.cpp:2952-2967`) caps itself at 100 eviction attempts per call ("bounded, we
don't stall the loop"), which is correct as a stall guard, but the final return —
`return g_data.used_memory <= g_config.maxmemory;` — can't distinguish "ran out of
batch budget while genuinely still evicting real keys" from "policy can't free
anything" (the latter already returns `false` earlier, at the `!victim` check). Result:
after a `CONFIG SET maxmemory` shrink under a large dataset, every write command gets
a spurious OOM until enough separate calls have each chipped off 100 keys, even though
eviction is working the whole time.

Decision (2026-07-17): mirror Redis's approach — treat "batch exhausted but still
making progress" as success, not failure, so the write goes through and the next
write's call to `free_memory_if_needed` picks up where sampling naturally continues.
Concretely: since the only way to reach the final `return` is either (a) genuinely
under budget now, or (b) attempts exhausted while `victim` was never null, `return
true;` unconditionally at that line is the fix — the `!victim` early-return is
untouched, so real OOM (`noeviction`, or `volatile-*` with nothing evictable) still
rejects correctly. Reverting to `return g_data.used_memory <= g_config.maxmemory;` is
the strict-backpressure alternative if that's ever preferred instead.

Two tradeoffs to accept consciously before applying, not just default into:

- ~~MYRED has no cron/timer-driven eviction sweep~~ — resolved 2026-07-17:
  `evict_tick()` runs a bounded batch per event-loop tick while `g_evict_pending`
  is armed, and `next_timer_ms()` returns 0 while pending so an idle server keeps
  draining (verified by `scripts/test_evict_tick.sh`: 50k→5.3k keys in <1s idle).
- `used_memory` can transiently overshoot `maxmemory` by more than it does today,
  since a write is now allowed to land on top of an already-over-budget state instead
  of being rejected outright. That's the intended availability-over-strict-ceiling
  tradeoff, not a bug, but worth confirming is acceptable before shipping.

## Testing Matrix

Primary harness (updated 2026-07-17: ECHO/inline, FLUSHDB, SPOP/SRANDMEMBER edge
semantics, O(1) INFO keyspace, incremental eviction, `--bench`):

```bash
python3 scripts/stress_test.py
python3 scripts/stress_test.py --correctness-only
python3 scripts/stress_test.py --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/stress_test.py --bench   # + redis-benchmark speed baseline
```

Persistence / eviction helpers:

```bash
scripts/test_aof.sh
scripts/test_aof_rewrite.sh
scripts/test_aof_hybrid.sh
scripts/test_evict_tick.sh          # incremental eviction (EVICT_RUNNING) regression
scripts/diag_live.sh
scripts/diag_ttl.sh
```

Benchmarking — **Release build only** (`cmake -B build-rel -DCMAKE_BUILD_TYPE=Release`;
the default Debug build runs `mem_selfcheck`'s whole-keyspace walk after every
command and poisons all numbers). Newly benchmarkable since 2026-07-17: SPOP
(O(k) pop-by-node), ZADD/ZPOPMIN, LRANGE, MSET, PING (ECHO makes `redis-cli
--pipe` work too):

```bash
redis-benchmark -p 1234 -a kek1234 -t ping,set,get,incr,lpush,rpush,lpop,rpop,sadd,hset,spop,zadd,zpopmin,lrange,mset -n 200000 -c 50 -P 16 -q
```

Caveat while the `mem_reaccount` O(n) bug is open: list/large-container benchmarks
(LPUSH/RPUSH/LPOP/RPOP/LRANGE on one growing key) measure the accounting walk, not
the data structure — see Known Bugs.

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
