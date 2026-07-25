# MYRED Roadmap — Progress

MYRED is a Redis-compatible in-memory database written from scratch in C++. It
speaks RESP and works with `redis-cli`, Redis clients, and `redis-benchmark`
where the implemented command surface allows.

**This file is one of three** (split 2026-07-21 to keep each readable):
- `ROADMAP.md` (this file) — current focus + completed milestones + testing matrix.
- `BACKLOG.md` — everything not started: future milestones, deferred items, feature gaps, open bugs.
- `DECISIONS.md` — design decisions, architecture notes, and conventions.

Companion: `CODE_REVIEW.md` — audit worklist + Resolved Bugs Archive.

## Current Snapshot

Date: 2026-07-21.

Primary commands:
```bash
cmake -B build
cmake --build build
./build/server myred.conf
python3 scripts/stress_test.py --password <pass>
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <pass>   # over TLS
```

Runtime assumptions:
- Default plaintext port `1234`; TLS via `tls-port` (see `myred.conf`).
- Config loaded with `./build/server myred.conf` or `MYRED_CONFIG`.
- `scripts/stress_test.py` is the primary correctness/stress harness (now `--tls`-aware).

Implemented command families:

| Area | Status |
|---|---|
| RESP2 parser and writers | Implemented |
| Strings / Lists / Hashes / Sets | Implemented |
| Sorted sets | Implemented subset |
| Generic keyspace commands | Implemented subset |
| RDB + AOF persistence (+ rewrite) | Implemented |
| Memory accounting and eviction | Implemented |
| Config file + password hashing (Argon2id) | Implemented |
| ACL foundation and hardening | Implemented (V9.4–V9.5) |
| **TLS** | **Implemented (V9.7)** |
| Pub/Sub | V8.1 core done; V8.2 patterns in progress |
| Transactions | Not implemented (→ BACKLOG V8.4) |
| Replication | Not implemented (→ BACKLOG V10) |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V8 - Pub/Sub [In Progress]

Redis Pub/Sub: a live broadcast mechanism with **no storage and no persistence** —
a message only reaches clients subscribed *at the moment* it is published, then
it is gone. Built in three ordered steps; each is independently testable before
the next starts. (Transactions — `MULTI`/`EXEC`/`WATCH`, the other half of the
old combined "V8" milestone — stay in `BACKLOG.md` as V8.4/V8.5; they share the
number for scheduling only and are an unrelated feature.)

#### V8.1 - Pub/Sub core: `SUBSCRIBE` / `UNSUBSCRIBE` / `PUBLISH` [Done]

Done 2026-07-24. Exact-channel-name matching only. Verified live: `SUBSCRIBE`
confirmation + cross-connection `PUBLISH` delivering a `message` push, the
subscribe-mode gate rejecting `GET`, and clean teardown (`PUBLISH` after the
subscriber disconnects returns `0`, no use-after-free).

- Registry `GlobalData::channels`
  (`unordered_map<string, unordered_set<Conn*>>`) — direct lookup, no scanning.
- **Design change from the original plan:** the per-conn state is
  `Conn::sub_channels` (an `unordered_set<string>` of joined channels), *not* the
  planned `size_t sub_count`. The set is simultaneously the subscribe-mode flag,
  the running count, and — critically — the teardown index, making
  `conn_destroy`'s unlink O(channels joined) instead of a scan of the whole
  registry. Since `PUBLISH` dereferences `Conn*` out of the registry, that unlink
  is a use-after-free guard, not an optimization.
- `PUBLISH` RESP-encodes straight into each subscriber's `outgoing` and sets
  `want_write`. **Zero event-loop changes, as predicted** — an idle subscriber
  sits at `want_read=true`, so poll dispatches it to `handle_read`, whose tail
  (`server.cpp:566-570`) flushes any non-empty `outgoing`; a mid-write conn takes
  the `handle_write` branch. Both steady states flush.
- `SUBSCRIBE`/`UNSUBSCRIBE`/`PUBLISH` are dispatched conn-aware in `do_request`
  (like `AUTH`/`ACL`) via a `do_pubsub_stub` table entry that only carries arity
  + ACL metadata; they return before the AOF/maxmemory/audit path, since Pub/Sub
  touches no keyspace.
- Channels are `KeySpec::NONE` + `CAT_FAST` — a channel name is not a key, so
  key-pattern ACL must not apply to it.

#### V8.2a - Pattern subscriptions [Done]

Done 2026-07-24. `PSUBSCRIBE`/`PUNSUBSCRIBE` with a second registry
`GlobalData::patterns` (same `map<string, set<Conn*>>` shape as `channels`, so
unlink/teardown code stays symmetric) + `Conn::sub_patterns`. `PUBLISH` gained a
second step: after the O(1) exact lookup it scans the *distinct patterns* and
`glob_match`es each against the channel, emitting a 4-element `pmessage` (which
carries the matched pattern so clients can demux). Reuses the same `glob_match`
as key-pattern ACL. Verified: one `PUBLISH news.sports` delivered to both a
`PSUBSCRIBE news.*` and a `SUBSCRIBE news.sports` conn, receiver count `2`.

Two V8.1 semantics this step corrected: the confirmation count is now the conn's
**total** subscriptions (channels + patterns, Redis's `clientSubscriptionsCount`)
via `pubsub_count()`, and the subscribe-mode gate checks **both** sets — a conn
holding only pattern subs is still in subscribe mode.

Five transcription slips were caught by grep-verify, none by the compiler and
none by the first passing test: `PUBLISH` min_args left at 2 while `do_publish`
reads `cmd[2]` (out-of-bounds, remotely triggerable), an iterator pair spanning
two different containers (`sub_patterns.begin()`/`sub_channels.end()` — UB that
happened to work on libstdc++), an explicit-arg loop starting at `i=0` (treating
the command name as a channel), `UNSUBSCRIBE` min_args 2 making the no-args
branch dead code, and one missed `pubsub_count` call site.

#### V8.2b - Channel ACL [Done]

Real channel-scoped ACL (Redis's `&pattern` rule). There is no existing field to
reuse — `User` only has `key_patterns` for key-scoped ACL — so this adds
`channel_patterns` + `all_channels` to `User`, mirroring `key_patterns`/`all_keys`.

- Rules: `&<pattern>`, `allchannels`/`&*`, `resetchannels`; round-tripped through
  `acl_format_user` so `CONFIG REWRITE` preserves them.
- Enforcement follows Redis exactly: `SUBSCRIBE`/`PUBLISH` **glob-match** the
  channel against granted patterns; `PSUBSCRIBE` requires the requested pattern to
  be **string-equal** to a granted one (you cannot widen your grant by subscribing
  to `*`). Checked inside the conn-aware handlers, since a channel is not a key and
  the `KeySpec` path does not apply. All-or-nothing per command.
- `UNSUBSCRIBE`/`PUNSUBSCRIBE` are never ACL-checked — leaving is always allowed.
- Default is restrictive (no channels) to match current Redis; the bootstrap
  `default` user gets `allchannels` so the common no-ACL setup is unaffected.
- Done when: a user granted `&news.*` can `SUBSCRIBE news.sports` but not `other`,
  `PSUBSCRIBE news.*` works while `PSUBSCRIBE *` is denied, and the grant survives
  a `CONFIG REWRITE` round-trip.

Done 2026-07-24, all of the above verified. One `acl_format_user` slip found by
grep-verify: the `all_channels` branch emitted `"&*"` without a leading space,
fusing it onto the previous token (`~*&*`) so a reload parsed it as key-pattern
`*&*` and dropped both grants. Untested by the live run because the `else` branch
(explicit `&pat`) was correct and `default` is skipped by `config_rewrite`.

#### V8.3 - Keyspace notifications [In Progress]

Rides entirely on top of V8.1/V8.2 — this step is wiring, not new mechanism.

- One `notify_keyspace_event(class, event, key)` helper that internally calls the
  now-existing `PUBLISH` path. Hook points are few and already identified:
  lazy expiry (`expire_if_needed`), active expiry (`process_timers`' TTL drain),
  eviction (`free_memory_if_needed`), and the write handlers themselves.
  Covers Redis-compatible `K`/`E` channel semantics (`notify-keyspace-events`
  config) without touching the dispatch path.
- Done when: `CONFIG SET notify-keyspace-events KEA`, a `PSUBSCRIBE
  __keyevent@0__:*`, and a plain `SET`/`EXPIRE`/eviction each produce the
  expected event on that channel.

**TLS carry-over (still open):** a full TLS suite re-run after the V9.7.5 flags
(session resumption + `SSL_MODE_RELEASE_BUFFERS`) landed — the 555/551 gate runs
predate those two config lines. Once green, V9.7 is fully closed. A commit
checkpoint of the V9.7.2→.5 body plus the docs reorg is also outstanding.

## Completed Milestones

### V9 - Security and Auth [Done]

Config file → hashed credentials → protected mode → ACLs → command hardening →
Argon2id → TLS.

- **V9.1 Config File Foundation** — shared `config_apply()`; `config_tokenize()`
  (comments + quotes); precedence defaults < file < env < `CONFIG SET`;
  `CONFIG REWRITE`.
- **V9.2 Password Hashing + Constant-Time Compare** — `requirepass` as SHA-256 /
  `#<hex>`; `AUTH` compares with `ct_equal`; plaintext wiped with `secure_zero`.
- **V9.3 Protected Mode / Bind / Allowlist** — multi-address `bind`; protected
  mode rejects non-loopback with no password; `allow-ip` CIDR in `handle_accept`
  (loopback always exempt).
- **V9.4 ACL Foundation** — `User` registry under `Config::users`; `Conn::user`
  identity; `CmdSpec` categories + key specs; `AUTH <user> <pass>`; `ACL
  SETUSER/GETUSER/DELUSER/LIST/USERS/WHOAMI/CAT/GENPASS`; users round-trip rewrite.
- **V9.5 Command Hardening + Audit Log** (2026-07-11) — category semantics
  (control-plane commands drop `@read`/`@write`, `acl_check` stays O(1), NOPERM
  before arity); real `KEYS` glob; precise key resolvers (`SMOVE`,
  `OBJECT`/`MEMORY`); `rename-command`/disable with canonical `g_dispatch`; audit
  log (`auth_*`, `acl_deny`, `acl_change`, `admin/dangerous_command`,
  `accept_reject`; never secrets); `metadata_selfcheck()` boot guard.
- **V9.6 Password Hashing Upgrade → Argon2id** (closed 2026-07-18) — self-
  describing credentials (`cred.h/cpp`; legacy SHA-256 forever-verifiable vs
  `$argon2id$` PHC); async verify on the thread pool via eventfd completion
  channel (`k_max_auth_inflight=4`, ~76 MiB cap; `Conn::id` liveness;
  `auth_pending` gating + `conn_resume`; uniform-timing dummy for unknown users);
  rehash-on-AUTH migration with value-matched CAS + `cred_rehash` audit.
  - **V9.6.4 audit bug sweep** (2026-07-17) — closed every open 🔴/🟠/🟡 + 🔵/⚪
    from the CODE_REVIEW consolidated audit (move semantics, 64-bit FNV-1a,
    `mem_selfcheck` placement, `hm_random` O(k) SPOP/SRANDMEMBER, incremental
    eviction `evict_tick`, O(1) INFO stats). Bonus: SPOP AOF determinism, RDB
    non-TTL-set loader data loss, ECHO + empty-inline (`--pipe` compat).
  - **V9.6.5 general + speed test** (2026-07-18) — new suites
    `test_restart_matrix.py` + `test_security.py` (+ `myred_testlib.py`), both
    `--destructive` green (caught 4 real bugs, all fixed); delta accounting
    (`Deque::elem_bytes` + `HMap::elem_bytes`, O(1) `entry_mem_usage`, drift-
    verified 0). Baselines recorded (see Testing Matrix).

### V9.7 - TLS [Done]

Real `redis-cli --tls` / `redis-benchmark --tls` round-trips; plaintext and TLS
listeners serve simultaneously. Built in five ordered steps so the event-loop
breakage was absorbed before any crypto existed. OpenSSL lives entirely behind a
transport seam — `server.cpp` includes no OpenSSL headers.

- **V9.7.1 Transport seam** — `transport.h/.cpp`: `IoResult` +
  `tr_read/tr_write/tr_close`. All per-conn socket I/O routes through it.
  `Conn::tr_want_read/tr_want_write` (transport demand) OR'd into poll flags
  beside application intent; poll asserts relaxed to guards. Zero behavior change.
- **V9.7.2 Context / config / listeners** — five boot-only `tls-*` directives
  through `config_apply`/`config_rewrite` (+ `do_config` reject); one global
  `SSL_CTX` (`TLS1.2` min, `SSL_OP_NO_RENEGOTIATION`, default ECDHE,
  `PARTIAL_WRITE | ACCEPT_MOVING_WRITE_BUFFER` — the moving-buffer flag is
  mandatory because `Buffer` slides between write retries); `Listener {fd,
  is_tls}`; `tr_tls_attach` on accept, no synchronous `SSL_accept`.
- **V9.7.3 Handshake as connection state** — `Conn::tls_handshaking` + a
  dedicated `hs_list` with a tighter `tls-handshake-timeout` (10 s default);
  `tr_handshake` maps `WANT_READ/WRITE` to the transport-demand flags; failed
  handshakes `audit_event("tls_handshake_fail")` + destroy.
- **V9.7.4 Data path** — `tr_read/tr_write` branch on `c->ssl`; classify with
  `SSL_get_error`, never errno (`ZERO_RETURN`→PEER_CLOSED, syscall/SSL→ERR);
  `handle_read` loops on `tr_has_pending` (uses `SSL_has_pending`, stronger than
  the roadmap's `SSL_pending` — catches unprocessed pipelined records); poll
  dispatch became **intent-driven** (drive by want_read/want_write, either-
  direction readiness retries) so a TLS read can block on POLLOUT and a write on
  POLLIN; `tr_close` one-shot best-effort `SSL_shutdown`.
- **V9.7.5 Optimizations (body)** — session resumption (`SSL_SESS_CACHE_SERVER`,
  verified `Reused,TLSv1.3` via `s_client -sess_out/-sess_in`), record-sized-flush
  invariant verified (no code), `SSL_MODE_RELEASE_BUFFERS`. Remaining opts
  (accept-storm cap, kTLS, cert reload) parked in `BACKLOG.md`.

Gate (2026-07-21, native Linux): authed **555/555** on :1235, passwordless
**551/551** on :1337, full `--bench` + stress suite green over TLS. TLS
throughput retains a healthy fraction of plaintext; the LRANGE curve (bigger
replies → bigger TLS hit) is the expected shape.

### Earlier milestones [Done]

- **V5.2 String Command Expansion** — variadic `DEL`/`EXISTS`;
  `INCR*`/`DECR*`/`INCRBYFLOAT`; `SETNX/SETEX/PSETEX/GETSET/GETEX/GETDEL`;
  `MSET/MGET/MSETNX`; `APPEND/STRLEN/GETRANGE/SETRANGE` (overflow guards, NaN/inf
  rejection, Redis negative-index semantics, 512 MB cap).
- **V5.3 Project-Wide Code Review** — `Entry::val` → `std::variant`; dispatch →
  `CmdSpec`; `std::mt19937_64`; centralized lazy expiry; iterative `glob_match`;
  portable `container_of`; server/RDB hardening; stronger build warnings.
- **V6 Persistence Hardening** — AOF buffering, mutation-gated logging, TTL →
  absolute `PEXPIREAT`, `appendfsync` policies, replay through the command path,
  `BGREWRITEAOF`, RDB/AOF startup priority, crash recovery/truncation,
  `--check-aof`/`--fix`, disk-full policy, config save triggers.
- **V6 Optimization Pass** — templated `aof_encode`; raw RESP AOF path; hybrid
  RDB-preamble + RESP-delta; `INFO persistence`. (Skipped `writev()` — unproven.)
- **V6.1 Redis Tooling Compatibility** — `PING`, inline protocol, `ZPOPMIN`,
  variadic `ZADD`. (Gap: `COMMAND`/`COMMAND DOCS`/`COMMAND COUNT` → BACKLOG.)
- **V7 Memory Management** — `Entry::mem` accounting, `used_memory`/`INFO
  memory`, `maxmemory`, Redis eviction policy names, LRU/LFU metadata, sampling
  victim selection, write-path eviction + OOM, AOF eviction propagation,
  `MEMORY USAGE/STATS/DOCTOR`, `OBJECT ENCODING/IDLETIME/FREQ/REFCOUNT`.
  (Deferred: compact encodings, shared-object refcounting, 16-slot eviction pool
  → BACKLOG.)

## Testing Matrix

Primary harness (`--tls`-aware since 2026-07-21):
```bash
python3 scripts/stress_test.py
python3 scripts/stress_test.py --correctness-only
python3 scripts/stress_test.py --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/stress_test.py --bench                                   # + redis-benchmark
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <pass>
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench  # passwordless TLS bench
```

Restart / security / eviction suites:
```bash
python3 scripts/test_restart_matrix.py [--destructive]   # private instance, port 12401
python3 scripts/test_security.py       [--destructive]   # private instance, port 12402
scripts/test_evict_tick.sh                                # EVICT_RUNNING regression
scripts/test_aof.sh  scripts/test_aof_rewrite.sh  scripts/test_aof_hybrid.sh
```

**Benchmark only on a Release build** (`cmake -B build-rel -DCMAKE_BUILD_TYPE=Release`;
Debug runs `mem_selfcheck`'s whole-keyspace walk per command and poisons numbers).
Same-machine comparisons only. Benchmark TLS against a **passwordless** instance —
`redis-benchmark`'s 50-client AUTH storm hits `k_max_auth_inflight=4`.

Recorded baselines (`-n 100000 -c 50 -P 16`, ops/sec):
- **WSL2 Release** (2026-07-18, plaintext): PING 1.01M · SET 1.06M · GET 1.02M ·
  INCR 1.00M · LPUSH 870k · SADD 917k · HSET 926k · SPOP 1.02M · ZADD 917k ·
  ZPOPMIN 1.06M · MSET(10) 327k · LRANGE 100/300/500/600 103k/41.5k/25.5k/16.9k.
- **Native Linux** (2026-07-18, plaintext, ~2.2–2.5× WSL): PING 2.78M · SET
  2.22M · GET 2.50M · SPOP 2.63M · ZADD 1.92M · MSET 833k. Cool-machine reference;
  a warm back-to-back run throttles ~½ (watch for a GET<SET anomaly = throttled).
- **Native Linux TLS** (2026-07-21, passwordless :1337): PING_MBULK 1.02M · SET
  901k · GET 971k · SADD 826k · SPOP 926k · HSET 833k · MSET(10) 412k · LRANGE
  100/300/500/600 133k/34k/11.4k/9.0k. TLS overhead scales with bytes/op — small
  ops stay high, bulk-range takes the biggest hit. (Same-session plaintext ran
  warm/throttled — re-run isolated for a clean ratio.)

Full logs: `docs/stress_results.md`, `docs/bench_plain.md`, `docs/bench_tls.md`,
`docs/stress_tls.md` (test-result files stay in `docs/`, separate from these
planning docs).

Security test focus: `AUTH` success/failure/lockout, `AUTH <user> <pass>`, `ACL
SETUSER` + config round-trip, command/key-pattern denial, protected-mode +
allowlist rejection, audit redaction, renamed-command canonical AOF.
