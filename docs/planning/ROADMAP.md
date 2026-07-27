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
| **Pub/Sub (+ patterns, channel ACL, keyspace notifications)** | **Implemented (V8)** |
| Transactions | **In progress — V8.4 `MULTI`/`DISCARD` done, V8.5 `EXEC` next** |
| Replication | Not implemented (→ BACKLOG V10) |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V8 - Transactions [Next]

`MULTI`/`EXEC`/`DISCARD` + `WATCH`: queueing a batch of ordinary commands and
committing them atomically. Shares the "V8" number with Pub/Sub for scheduling
only — unrelated feature, no broadcast mechanism involved. Step numbers continue
from the Pub/Sub steps so they never collide.

**Atomicity is free here.** The event loop is single-threaded, so no other
connection's command can interleave between queued commands — the same reasoning
already recorded for EVAL's `redis.call`. The work is the conn state machine and
the reply framing, not concurrency control.

#### V8.4 - Transaction mode: `MULTI`, queueing, `DISCARD` [Done] 2026-07-26

Just the state machine and the queueing gate — no execution yet. `EXEC` itself
is V8.5, so this step is done as soon as a client can open a transaction, see
`+QUEUED` for each command, and close it with `DISCARD` without anything ever
actually running.

- `Conn` gains `bool in_multi`, `bool multi_dirty` (a queue-time error — unknown
  command, bad arity — that makes `EXEC` abort without running anything, Redis's
  `EXECABORT`), and `std::vector<std::vector<std::string>> queued_cmds`.
- `MULTI`: error ("MULTI calls can not be nested") if already `in_multi`; else
  set it, reply `+OK`.
- While `in_multi`, `do_request` intercepts after command/arity validation but
  before dispatch: a command that doesn't exist or has the wrong arity sets
  `multi_dirty` and replies the error immediately (but queuing continues for
  anything after it); otherwise the raw `cmd` vector is pushed onto
  `queued_cmds` and the reply is `+QUEUED` instead of actually executing.
  `MULTI`/`EXEC`/`DISCARD`/`WATCH`/`RESET`/`QUIT` are the commands that never queue.
- **Resolving the mode-composition question this section used to leave open**:
  `SUBSCRIBE`/`PSUBSCRIBE`/`UNSUBSCRIBE`/`PUNSUBSCRIBE` must *not* be queueable —
  real Redis explicitly rejects them inside `MULTI` ("... is not allowed in
  transactions") as a queue-time error, the same way a bad-arity command sets
  `multi_dirty`, rather than deferring the subscribe until `EXEC`. The reverse
  direction already takes care of itself: `cmd_ok_in_subscribe`'s whitelist
  (commands.cpp:3540ish) doesn't include `MULTI`, so a connection with any
  `sub_channels`/`sub_patterns` already can't open a transaction in the first
  place — the two modes are mutually exclusive by construction, not by new code.
- `DISCARD`: clears `in_multi`/`queued_cmds`/`multi_dirty`, replies `+OK`; errors
  if not currently in `MULTI`.
- **Precedent from V8.1**: Pub/Sub added its own per-conn mode with a gate in
  `do_request` (`sub_channels`/`sub_patterns` non-empty ⇒ only a whitelist runs).
  `in_multi` is a second such mode, and (per above) the two don't need to compose
  at all — reuse the conn-aware dispatch shape (`AUTH`/`ACL`/pubsub) for
  `MULTI`/`EXEC`/`DISCARD`/`WATCH` rather than routing them through
  `k_cmd_table`'s `CmdFn`, since they need `Conn`.
- Done when: `MULTI` replies `+OK`, a nested `MULTI` errors without disturbing
  the open transaction, ordinary commands reply `+QUEUED` and land in
  `queued_cmds` in order, an unknown/bad-arity command sets `multi_dirty` but
  queuing continues afterward, and `DISCARD` clears all three fields and
  replies `+OK`.

**Shipped 2026-07-26** — all done-criteria verified live. Notes that differ from
the plan above, or that V8.5 depends on:

- The conn field is named **`queue_cmds`** (not `queued_cmds`).
- **Queued commands are stored AS TYPED, never canonicalized.** `dispatch_build()`
  *erases* the old name from `g_dispatch` when `rename-command` applies, so
  storing the canonical name would let `EXEC` resurrect a command that was
  deliberately renamed away. The stored vector re-resolves through the identical
  lookup path at exec time — same rename, same ACL, same `renamed` flag for the
  AOF re-encode. `cmd[0]` is already lowercased in place before the gate.
- **Poisoning is centralised in `resp_err_txn(out, conn, msg)`**, which sets
  `multi_dirty` when `in_multi` and then forwards to `resp_err`. Wired into the
  three pre-dispatch rejections: unknown command, ACL deny, wrong arity. Putting
  the queue gate *after* the ACL check is what makes a NOPERM poison the
  transaction, matching Redis's `flagTransaction`.
- `cmd_no_queue()` rejects the four pub/sub mode switches at queue time. V8.5's
  `exec`/`watch`/`unwatch` must dispatch **above** the gate, never through this list.
- **New ACL category `@transaction`** (`CAT_TRANSACTION = 1ull << 8`), added
  alongside — see DECISIONS → ACL Category Tagging. `multi`/`discard` are
  `CAT_FAST | CAT_TRANSACTION`, keeping the `CAT_READ` base bit so a read-only
  user can still open a transaction. `CAT_ALL` is `~0ull`, so existing `+@all`
  users needed no migration.
- **Deliberate deviation**: `AUTH` executes immediately inside `MULTI` instead of
  queueing — it short-circuits at the top of `do_request` before the gate, and
  our AUTH is async (`auth_pending` gates parsing), so queueing it would be a mess.
- **Known limitation, not fixed**: `queue_cmds` is unbounded, so an authenticated
  client can queue until the process dies. Real Redis is the same. A cap belongs
  with V8.5 hardening.
- No `conn_destroy` hook is needed — nothing outside the `Conn` points at this
  state, so the member destructors cover it. `WATCH` (V8.6) *will* need one.
- Three transcription slips again reached a running server, none caught by the
  compiler: a duplicated `{"multi", …}` table key that silently dropped `discard`
  (`unordered_map` init-lists use insert semantics — first wins, no warning), a
  missing space producing `-ERRsubscribe is not allowed…`, and `acl_cat_bit`
  left unedited so `ACL CAT` advertised `transaction` while `SETUSER` rejected
  `+@transaction`. See BACKLOG → the metadata-selfcheck follow-up.

#### V8.5 - `EXEC`: atomic dispatch + reply assembly [Done] 2026-07-26

Do not start until V8.4 is solid — this step only has something real to run
once queueing works.

- If `multi_dirty`, discard the queue and reply `-EXECABORT`, running nothing.
- Otherwise: `resp_arr(out, queued_cmds.size())` up front, then dispatch each
  queued command in order straight into `out` — RESP array elements are just
  concatenated encoded values, so no per-command scratch buffer is needed, the
  replies naturally land back-to-back inside the one array.
- **The trap to design around from the start**: dispatching a queued command
  must not re-enter the V8.4 queueing gate, or `EXEC` would push its own queued
  commands right back onto `queued_cmds` instead of running them. Clear
  `in_multi` (or dispatch through a separate "now executing" path) before
  running the queue, and restore/clear it once `EXEC` finishes.
- Design for this interaction now even though blocking commands ship later:
  real Redis's blocking commands (`BLPOP` etc.) never actually block inside
  `MULTI`/`EXEC` — they run non-blocking and return nil immediately if not
  ready. Whatever state `EXEC`'s dispatch loop checks for "am I inside a
  transaction commit" needs to be visible to those commands later, or the
  blocking list commands (BACKLOG → Command Coverage Gaps → Lists) will need
  this redesigned.
- Done when: a queued sequence of writes replies `+QUEUED` per command, `EXEC`
  returns one array with each individual result in order, and a bad command
  mid-queue produces `-EXECABORT` on `EXEC` without running anything.

**Shipped 2026-07-26** — all done-criteria verified live, plus one 🔴 the plan
above did not anticipate:

- **`EXEC` would have silently lost every transactional write from the AOF.** A
  queued command has no verbatim client bytes (the parser consumed them at queue
  time), so `do_request`'s `aof_append_raw(raw, raw_len)` branch appended nothing:
  `SET` succeeded, client saw `+OK`, key gone after restart. Fixed by making
  **`raw == nullptr` a documented contract meaning "re-encode the log entry from
  the vector"** — `bool reencode = spec.aof_rewrite || renamed || !raw;` drives
  both the snapshot copy and the `aof_feed` branch. `aof_feed` ends in a verbatim
  `aof_encode(frame, cmd)` fallback, so it is always correct, just slower than the
  memcpy. Costs one bool test on the hot path. Note `aof.cpp`'s replay loop was
  *already* calling `do_request(..., nullptr, 0)`; it was safe only because
  `may_log` is false under `g_loading`. The contract is now explicit for both.
  Proof it works: an *inline* `nc` command came back out of the AOF as a proper
  RESP array, which only the re-encode path can produce.
- `do_exec` swaps the queue into a local `batch` and clears `in_multi` **before**
  dispatching, so queued commands can't re-enter the queueing gate. `EXEC`/`MULTI`/
  `DISCARD` dispatch above that gate and are therefore never queueable, which is
  what makes the recursion safe without a depth guard.
- `Conn::in_exec` is set around the dispatch loop. Write-only today — it exists so
  blocking commands (`BLPOP`) can see "inside a transaction commit" when they ship.
- **We deliberately do NOT wrap the batch in `MULTI`/`EXEC` in the AOF** the way
  Redis does; each queued write is logged individually as it runs. This is
  load-bearing: `aof.cpp`'s replay `Conn fake{}` now has an `in_multi` field, so a
  `MULTI` appearing in the log would make replay queue the rest of the file into
  `queue_cmds` and silently drop it. Revisit only alongside replay support.
- `CONFIG GET` exposes only 3 parameters while `CONFIG SET` accepts the full
  directive set — filed in BACKLOG; it wasted a verification cycle here.

#### V8.6 - `WATCH`: registry + eager dirty-marking [Backlog]

Do not start until V8.5 is solid — `WATCH` only matters relative to a working
`EXEC`. This step is just the tracking mechanism; `EXEC` actually honoring it
is V8.7.

- `WATCH key [key...]`: only valid *before* `MULTI` starts (Redis rejects `WATCH`
  inside an open transaction). Repeated calls accumulate more watched keys
  rather than replacing the set.
- Recommended mechanism — eager dirty-marking, not lazy generation-diffing: a
  global `std::unordered_map<std::string, std::unordered_set<Conn*>> watchers`
  (key name → watching conns). Any write to that key immediately sets every
  watching `Conn::watch_dirty = true`; `EXEC` then checks one flag instead of
  re-diffing per-key state at commit time. Mirrors Redis's `touchWatchedKey()`,
  and avoids a per-key generation counter that would have to survive a key being
  deleted and recreated under the same name.
- **The write hooks already exist.** V8.3's keyspace notifications instrumented
  exactly these points (the central `do_request` hook keyed off the
  `g_writes_since_save` dirty counter, plus lazy expiry, active expiry, and
  eviction). `WATCH` should ride the same instrumentation rather than adding a
  second parallel set.
- Teardown: `conn_destroy` must remove the conn from every `watchers` set —
  identical to `pubsub_remove_conn`, and load-bearing for the same reason (a
  freed `Conn*` left in the registry is a use-after-free).
- Done when: `WATCH` registers the conn in `watchers` for each key argument
  (verify directly — grep the registry state or log it), and a write to a
  watched key flips `watch_dirty` on every watching conn. `EXEC` doesn't need
  to honor it yet; that's V8.7.

#### V8.7 - `EXEC`/`DISCARD` integration + `UNWATCH` [Backlog]

Do not start until V8.6 is solid.

- `EXEC` gains a pre-check: if `conn->watch_dirty`, abort with a **nil array**
  reply instead of running the queue — distinct from `EXECABORT`, which is a
  queue-time error; this is a commit-time invalidation.
- **Filling a gap in the original design note**: watches must be cleared, not
  just checked. Real Redis clears a connection's entire watched-key set (and
  removes it from every `watchers` entry) after `EXEC` runs — whether it
  committed or aborted on `watch_dirty` — and after `DISCARD`, since a `WATCH`
  is only meant to guard the *next* transaction attempt, not persist forever.
  One shared "clear my watches" helper, called from all three sites, mirrors
  the existing `pubsub_remove_conn` teardown shape.
- `UNWATCH`: a standalone command (no `MULTI` required) that calls the same
  clear-my-watches helper and replies `+OK` — this command was missing from
  the original `WATCH` note entirely but is required for a client to abandon
  watches without committing a transaction.
- Done when: two connections `WATCH` the same key, one modifies it, and the
  other's subsequent `EXEC` returns a nil array instead of running its queued
  commands; a fresh `EXEC` right after that (watches now cleared) runs
  normally; and a standalone `UNWATCH` clears watches without needing `EXEC`
  at all.

**Carry-overs: all clear.** V9.7 TLS closed 2026-07-25 (603/603 both transports),
the trustworthy plaintext↔TLS baseline is recorded in the Testing Matrix, the
`requirepass` regression is fixed, and the tree is committed through `c7912e0`.

## Completed Milestones

### V8 - Pub/Sub [Done]

Closed 2026-07-25. A live broadcast mechanism with **no storage and no
persistence** — a message only reaches clients subscribed at the moment it is
published. (Transactions — `MULTI`/`EXEC`/`WATCH` — were never part of this work;
they keep the V8.4/V8.5 numbers and are now the active milestone in Current Focus.)

- **V8.1 core** — `SUBSCRIBE`/`UNSUBSCRIBE`/`PUBLISH` over a
  `GlobalData::channels` registry (`map<string, set<Conn*>>`), exact-name lookup.
  Per-conn state is `Conn::sub_channels` (a *set*, not the originally planned
  `size_t sub_count`): it doubles as the subscribe-mode flag, the running count,
  and the teardown index, so `conn_destroy` unlinks in O(channels joined) rather
  than scanning the registry. Because `PUBLISH` dereferences `Conn*` out of that
  registry, the unlink is a **use-after-free guard**, not an optimization.
- **V8.2a patterns** — `PSUBSCRIBE`/`PUNSUBSCRIBE` over a second registry of the
  same shape, so unlink/teardown stays symmetric. `PUBLISH` scans *distinct
  patterns* (not conns) after its O(1) exact lookup and emits a 4-element
  `pmessage` carrying the matched pattern. Confirmation counts are the conn
  **total** (channels + patterns), and the subscribe-mode gate checks both sets.
- **V8.2b channel ACL** — `&pattern` / `allchannels` / `resetchannels` on a new
  `User::channel_patterns` + `all_channels`. Enforcement mirrors Redis's
  asymmetry: `SUBSCRIBE`/`PUBLISH` **glob-match** the channel, `PSUBSCRIBE`
  requires **string equality** so a narrow grant can't be widened by subscribing
  to `*`. Checked inside the conn-aware handlers (a channel is not a key, so the
  `KeySpec` path never applies), all-or-nothing per command; leaving is never
  ACL-checked. Restrictive by default, with `allchannels` on the bootstrap
  `default` user so no-ACL setups are unaffected.
- **V8.3 keyspace notifications** — `notify-keyspace-events` flag mask (Redis's
  `K`/`E` + class chars) driving `notify_keyspace_event()` over the extracted
  `pubsub_publish()` core. **One central hook in `do_request`** rather than ~40
  handler edits: the existing `g_writes_since_save` dirty-counter already proves
  a command mutated something, and the Redis event name is the canonical command
  name, so a `CmdSpec::notify_class` stamped at boot covers the whole write
  surface. The key is captured *before* `spec.fn` because handlers consume `cmd`.
  Three lifecycle hooks (lazy expiry, active expiry, eviction) supply the events
  no command can. Zero cost when disabled — the mask gate short-circuits before
  any allocation.

**Architectural claim that held:** Pub/Sub needed **zero event-loop changes**.
`poll_args` is rebuilt from every conn's `want_read`/`want_write` each tick, so
`PUBLISH` writing into another connection's buffer and flipping its flag is
picked up on the next `poll()` — no eventfd, no cross-thread signalling.

**Process note worth keeping:** V8 shipped seven transcription slips that neither
the compiler nor the first passing smoke test caught — an out-of-bounds `cmd[2]`
read from a wrong `min_args`, an iterator pair spanning two containers (UB that
worked by luck on libstdc++), a loop starting at `i=0` that treated the command
name as a channel, a dead no-args branch, a missed count call site, and a missing
space that fused `~*&*` into one ACL token. Every one was found by grep-verifying
the tree against the snippet, which is why that step is non-negotiable here.

Regression coverage: `scripts/test_pubsub.py` (dedicated suite, own server) plus
five sections in `scripts/stress_test.py`, including a concurrent fan-out test.

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

**Full command runbook: `docs/TESTING.md`** — prerequisites, every suite, TLS,
benchmarks, and the pre-commit gate. This section records *what is covered and
what the baselines are*; the runbook records *how to run it*.

Primary harness (`--tls`-aware since 2026-07-21):
```bash
python3 scripts/stress_test.py
python3 scripts/stress_test.py --correctness-only
python3 scripts/stress_test.py --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/stress_test.py --bench                                   # + redis-benchmark
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <pass>
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench  # passwordless TLS bench
```

Restart / security / pubsub / eviction suites:
```bash
python3 scripts/test_restart_matrix.py [--destructive]   # private instance, port 12401
python3 scripts/test_security.py       [--destructive]   # private instance, port 12402
python3 scripts/test_pubsub.py         [--evict]         # private instance, port 12403
scripts/test_evict_tick.sh                                # EVICT_RUNNING regression
scripts/test_aof.sh  scripts/test_aof_rewrite.sh  scripts/test_aof_hybrid.sh
```

`test_pubsub.py` covers all of V8 (core, patterns, channel ACL, keyspace
notifications) against its own server, and every check tagged `[REG]` pins a bug
that actually shipped during development. `stress_test.py` carries the same
ground as live-server sections plus a concurrent fan-out test.

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
  100/300/500/600 133k/34k/11.4k/9.0k.

**Current reference pair — 2026-07-25, native Linux, `bench.conf`, back-to-back,
both 603/603 green.** The plaintext half matches the cool-machine baseline above
(PING/SET/SPOP at or above it), so this is the first trustworthy plaintext↔TLS
ratio recorded:

| | plaintext :1336 | TLS :1337 | TLS % |
|---|---|---|---|
| PING_MBULK | 2.86M | 1.52M | 53% |
| SET | 2.22M | 1.41M | 63% |
| GET | 2.04M | 1.43M | 70% |
| INCR | 2.17M | 1.14M | 52% |
| LPUSH | 2.00M | 1.19M | 60% |
| SADD | 2.08M | 1.16M | 56% |
| HSET | 1.96M | 1.16M | 59% |
| SPOP | 2.70M | 1.28M | 47% |
| ZADD | 1.92M | 1.14M | 59% |
| MSET(10) | 870k | 637k | 73% |
| LRANGE 100/300/500/600 | 213k/73k/45k/38k | 185k/56k/28k/22k | 87/77/62/59% |

**TLS costs ~30–50% of throughput**, worst on the smallest ops (per-record
overhead dominates) and recovering on bulk ranges where bytes amortize it.

Two reading traps in these logs, both measurement artifacts rather than server
behaviour:

- **The Python stress throughput is client-bound and must be ignored** for
  transport comparisons (3.6k plain vs 5.7k TLS ops/sec — the server is 99.8%
  idle at that rate). Proof it is noise: the pub/sub fan-out test in the *same*
  runs inverts the ranking (15.1k plain vs 11.3k TLS deliveries/s). Two
  client-bound numbers disagreeing on direction measure Python, not MYRED.
- **`p50` latency cannot be compared across runs at different throughputs** when
  `-P 16` pipelining is on. At LRANGE_300 plaintext shows *higher* throughput and
  *higher* p50 (10.5 ms vs TLS 4.4 ms) — Little's Law with a fixed pipeline depth.
  Compare throughput; the inversion only appears once replies are big enough for
  queueing to dominate (LRANGE_100 does not invert).

Full logs are named `docs/<kind>_<plain|tls>.md` automatically (`stress_results_*`,
`bench_*`, `stress_*`, `correctness_*`), so a TLS run never overwrites its
plaintext counterpart: `docs/bench_plain.md`, `docs/bench_tls.md`,
`docs/stress_tls.md` (test-result files stay in `docs/`, separate from these
planning docs).

Security test focus: `AUTH` success/failure/lockout, `AUTH <user> <pass>`, `ACL
SETUSER` + config round-trip, command/key-pattern denial, protected-mode +
allowlist rejection, audit redaction, renamed-command canonical AOF.
