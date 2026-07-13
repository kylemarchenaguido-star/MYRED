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

Date: 2026-07-11.

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

#### V9.6 - Password Hashing Upgrade [In Progress]

SHA-256 is fast and unsalted: fine against wire sniffing, weak if `myred.conf` or an
ACL line leaks (GPU cracking of unsalted fast hashes). Upgrade credentials at rest to
Argon2id while keeping every existing config loadable.

Prerequisite: fix the `sha256_hex` padding bug (CODE_REVIEW 2026-07-09, N5) first.
Migration code will verify legacy digests; it must verify *correct* ones.
`[Done]` 2026-07-12 — KAT-verified against hashlib for lengths 0-200 + NIST vectors;
old code failed at exactly `len%64 >= 56`. No stored digest changed (all short passwords).

Current write/verify sites (all of them, so nothing is missed):

- Verify: `do_auth` (`commands.cpp:1083`) — hashes once, `ct_equal` against each entry
  of `User::pw_hashes`, dummy-compare for unknown users, `failed_attemps` counter.
- Store: `config_apply("requirepass")` (`state.cpp:119-137`), `acl_apply_rule`
  `>pass` / `<pass` / `#<hex>` (`commands.cpp:2943-2963`), env `MYRED_PASSWORD`
  (`server.cpp:482`), historical default (`server.cpp:487`).
- Render: `acl_format_user` (config rewrite + `ACL LIST`), which already quotes
  hash tokens.

##### `[Done]` V9.6.1 - Credential abstraction — 2026-07-12

Implemented as planned below: `cred.h/cred.cpp` (three-function API), all six
write/verify sites switched, `$argon2id$` tokens accepted in `acl_apply_rule` and
`requirepass`, PHC emitted bare/quoted by `acl_format_user` + `config_rewrite`,
CMake `MYRED_ARGON2` (default ON, SHA-256 fallback with boot warning when
`libargon2-dev` is absent). Fallback behavior tested end-to-end; argon2 branch
compile-verified. `do_auth` now also wipes its local plaintext copy.

<details><summary>original plan</summary>

Do not scatter `if (argon2)` branches across those sites. Add a small module
(`cred.h/cred.cpp`, alongside `sha256.h`) that owns the stored-credential format:

- A stored credential stays a `std::string` inside `User::pw_hashes` (no struct churn,
  config round-trip untouched). Two self-describing forms:
  - legacy: 64 lowercase hex chars = SHA-256 digest (verified forever);
  - PHC: `$argon2id$v=19$m=...,t=...,p=...$<salt_b64>$<tag_b64>`.
- API — exactly three functions, and every site above switches to them:
  - `std::string cred_hash_new(const std::string &plain)` — Argon2id, fresh random
    salt (16 B via `getrandom()`; do NOT use `g_rng`, it is not seeded for security).
  - `bool cred_verify(const std::string &plain, const std::string &stored)` —
    dispatches on the `$argon2id$` prefix vs 64-hex; tag comparison stays
    constant-time (`ct_equal` on the decoded tag); unknown format returns false.
  - `bool cred_needs_rehash(const std::string &stored)` — true for legacy digests and
    for PHC strings whose `m/t/p` are below current policy.
- `acl_apply_rule` gains one branch: a token starting with `$argon2id$` is accepted as
  a pre-hashed credential (same idea as `#<hex>`). Note `<pass` removal can no longer
  hash-and-compare — salts differ — so it must `cred_verify` against each stored entry
  and erase matches. That is O(n) KDF runs, but it is an admin-time operation on a
  tiny vector; acceptable.
- Build: `find_package`/pkg-config for `libargon2` behind a CMake option
  (`MYRED_ARGON2`, default ON). When OFF or missing, `cred_hash_new` falls back to
  SHA-256 with a startup warning — the zero-dependency build keeps working. Skip
  bcrypt entirely; one optional dependency is enough.

Parameters: start with OWASP baseline `m=19456 KiB, t=2, p=1` as constants. Only add
`argon2-*` config directives if tuning is ever actually needed — each directive is
rewrite/round-trip surface.

</details>

##### `[Done]` V9.6.2 - Async verification — 2026-07-12

Implemented as planned: eventfd completion channel (`loop_post`/`loop_drain` in
`server.cpp`, mutex-guarded job vector drained on the poll loop), `do_auth` queues an
`AuthJob` (deep-copied hash snapshot, worker-owned plaintext, wiped by the worker) to
the thread pool, `auth_complete` applies the result on the main thread. Both
correctness traps closed: `Conn::id` liveness stamping (completions for a dead/reused
fd are dropped) and `auth_pending` pipeline gating + `conn_resume` drain. DoS bound:
`k_max_auth_inflight = 4` → `-BUSY`, capping Argon2 memory at ~76 MiB.
Unknown/disabled/nopass users verify against an unmatchable random dummy
(`cred_dummy`, `user_known=false`), so the timing class matches and the dummy can
never authenticate. Verified by `test_async_auth.py` (6/6 on an argon2-linked build):
pipelined AUTH+PING+SET replies in order, repeated failures close the conn, 8 parallel
AUTHs all complete, and PING on another conn during a 4-thread AUTH storm stayed at
p50=3.84 ms / p99=6.55 ms — a synchronous Argon2id verify would sit at 20–60 ms+.

<details><summary>original plan</summary>

Argon2id costs tens of milliseconds and ~19 MiB *by design*. Running it inside
`do_auth` on the event loop stalls every connected client per AUTH attempt and hands
an attacker a DoS lever (spam AUTH → server frozen). This is the core engineering work
of V9.6:

- Build a generic worker→loop completion channel first (it is also what V9.7's
  handshake offload and future async jobs need — build once):
  - an `eventfd` (or self-pipe) registered in the `poll()` loop;
  - a mutex-guarded completion queue drained by the main thread when the eventfd fires;
  - `loop_post(fn, arg)` callable from `thread_pool` workers.
- `do_auth` flow becomes: parse args → copy plaintext into a worker-owned buffer →
  set `conn->auth_pending = true` → `thread_pool_queue` the verify → return without
  replying. Worker runs `cred_verify` (against each stored hash), `secure_zero`s its
  plaintext copy, posts `{conn_id, user_name, ok}`.
- Main thread on completion: resolve the conn, set `conn->user`, emit
  `+OK`/`-WRONGPASS` into `conn->outgoing`, flip `want_write`, run the existing
  `failed_attemps`/`audit_event` logic.
- Two correctness traps, both mandatory:
  - **Conn liveness**: the client can disconnect mid-verify and the fd can be reused.
    Do not capture `Conn*` in the completion. Add a monotonically increasing
    `uint64_t Conn::id` stamped at accept; completions carry the id and are dropped if
    `fd2conn` no longer maps to a conn with that id.
  - **Pipeline gating**: while `auth_pending`, `try_one_request` must stop parsing
    (return false, leave bytes buffered) so pipelined commands cannot run with the
    pre-AUTH identity.
- DoS bound: cap concurrent verifications (counter, e.g. 4): beyond the cap, either
  queue the AUTH or reply `-BUSY`. Bounds Argon2 memory to `cap × m` and keeps the
  worker pool from starving `entry_del`/fsync jobs.
- Timing hygiene: unknown users must take the same path — verify against a baked-in
  dummy PHC string instead of today's `ct_equal(h, k_dummy)`, so "user exists" is not
  distinguishable by response time class.
- AOF replay is unaffected: the replay identity is synthetic and never AUTHs.

</details>

##### `[In Progress]` V9.6.3 - Migration and rotation

Note: the second and third bullets below were already satisfied by V9.6.1
(`CONFIG SET requirepass` / `ACL SETUSER >plain` call `cred_hash_new` directly;
`g_config.password` is only `.empty()`-tested outside `cred_*`). The remaining work
is rehash-on-AUTH.

- On successful AUTH where `cred_needs_rehash(stored)` is true: the plaintext is still
  in hand — compute `cred_hash_new`, replace that `pw_hashes` entry in place (worker
  computes, completion applies it on the main thread), log an `audit_event`
  (`cred_rehash`, username only). Never auto-run `CONFIG REWRITE`; the operator
  persists when ready.
- `CONFIG SET requirepass <plain>` and `ACL SETUSER >plain` produce Argon2id directly.
  These are admin-rate operations: synchronous hashing is acceptable at first;
  offload later only if it shows up.
- `g_config.password` currently doubles as "the default user's digest"
  (`acl_bootstrap_default`, protected-mode check `password.empty()`). Keep it a
  stored-credential string; only `.empty()` is ever tested outside `cred_*`.

Tests / done criteria:

- PHC round-trip through config rewrite and restart (quoting preserved).
- Legacy `#<hex>` configs still authenticate; first AUTH flips the entry to PHC in
  memory; `ACL LIST` shows a redacted marker, never the hash.
- AUTH storm (50 clients × wrong passwords) leaves PING p99 on other connections flat
  — this is the assertion that proves the async design.
- Unknown-user vs wrong-password timing in the same class.
- `--correctness-only` green with `MYRED_ARGON2=OFF` (fallback path).

#### V9.7 - TLS [Backlog]

TLS is the heaviest security feature. The event loop, not OpenSSL, is where the risk
lives: today plaintext I/O touches exactly four places — accept
(`handle_accept`, `server.cpp:71`), read (`handle_read`, `server.cpp:410`), write
(`handle_write`, `server.cpp:391`), close (`conn_destroy`) — and readiness is derived
purely from application intent (`want_read`/`want_write`). TLS breaks that assumption,
so the milestone is ordered to absorb the breakage before any crypto exists.

Prerequisites: CODE_REVIEW 2026-07-09 N3 (timer busy-loop) and N4 (protocol-error
wedge / missing input caps) — TLS multiplies buffering complexity and must not land on
top of a loop that spins or buffers unboundedly.

##### V9.7.1 - Transport seam (zero-behavior-change refactor, no OpenSSL yet)

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

##### V9.7.2 - Context, config, listeners

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

##### V9.7.3 - Handshake as connection state

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

### Process Lifecycle and Error Handling

- `die()` (`server.cpp:42-46`) prints `[errno] msg` and then calls `abort()` for every
  call site, with no distinction between an ordinary, expected operational failure and
  an actual internal-invariant violation. `abort()`/`SIGABRT` conventionally means "the
  program reached a structurally impossible state" and exists to leave a debuggable
  core dump — but several `die()` call sites are just routine startup mistakes: a
  missing/misspelled config path (`server.cpp:480`, `if (cfg_path &&
  !config_load_file(cfg_path)){ die("invalid config file"); }`), a bind failure such as
  `EADDRINUSE`/`EADDRNOTAVAIL` (`server.cpp:580`, `"listener setup"`), and `fcntl`
  failures setting non-blocking mode (`server.cpp:52`, `server.cpp:59`). Reproduced:
  `./build/server myred.cond` (typo'd filename) prints `fatal: cannot open config
  myred.cond: No such file or directory` then `Aborted (core dumped)` — an alarming,
  crash-looking exit for what is just a bad CLI argument, and it leaves a core-dump
  artifact (subject to whatever `core_pattern`/`ulimit -c` the host has configured) on
  every single bad invocation. Fix: split `die()` into two helpers — `panic(msg)`
  (unchanged: print + `abort()`) reserved for genuine internal-invariant violations, and
  a new `fatal_exit(msg)` (print + `exit(1)`, no core dump) for ordinary
  startup/operational failures. Route `"invalid config file"`, `"listener setup"`, and
  the two `"fcntl error"` sites to `fatal_exit()`; the runtime `poll()` failure
  (`server.cpp:609`, `die("poll")`) is the one call site that plausibly stays a `panic()`
  — a `poll()` failure mid-operation (as opposed to at startup) more likely indicates an
  actual fd-management bug worth a debuggable core dump.

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
