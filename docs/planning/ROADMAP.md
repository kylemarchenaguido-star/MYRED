# MYRED Roadmap — Progress

MYRED is a Redis-compaosible in-memory database written from scratch in C++. It
speaks RESP and works with `redis-cli`, Redis clients, and `redis-benchmark`
where the implemented command surface allows.

**This file is one of three** (split 2026-07-21 to keep each readable):
- `ROADMAP.md` (this file) — current focus + completed milestones + testing matrix.
- `BACKLOG.md` — everything not started: future milestones, deferred items, feature gaps, open bugs.
- `DECISIONS.md` — design decisions, architecture notes, and conventions.

Companion: `CODE_REVIEW.md` — audit worklist + Resolved Bugs Archive.

## Current Snapshot

Date: 2026-08-13.

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
| **Transactions** | **Implemented (V8.4–V8.7)** |
| **Replication + coordinated failover** | **Implemented (V10.1–V10.6d)** — V10.6e (automatic election) unscoped, cluster → V12 |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V10.6e - Automatic failover, Sentinel-compatible [Next, unscoped]

**V10.1 through V10.6d are closed — see Completed Milestones.** What is left of
V10 is the one part of it that is not deterministic: deciding, *without a human*,
that the master is gone, and agreeing on who takes over.

Everything closed so far runs on one box and is driven by a command somebody
typed. This is distributed consensus — quorum, config epochs, leader election,
the `__sentinel__:hello` bus, `SENTINEL is-master-down-by-addr`. It was worth
refusing to start it until a-d were solid, because a correct election that then
performs an *incorrect* handover buys exactly nothing. The handover it would
drive is `FAILOVER`, which now exists, is coordinated, loses no writes, and has
35 checks standing on it.

**Gate, unchanged: V11 Step 0** — fold the local suites back into one runnable
regression surface (BACKLOG). An automatic failover you cannot regression-test is
a liability, not a feature: it is the one subsystem that acts on its own, at
night, unattended. So the honest running order from here is V10.6.1 below
(bounded, unrelated, already scheduled as a detour), then V11 Step 0, then scope
V10.6e against a suite that can prove it.

Two residuals inherited from V10.6, both filed in BACKLOG → Open Bugs, neither
blocking: the master's own keepalive deadline is still nested inside the replica
branch of `next_timer_ms()`, and `INFO` renders a never-acked replica as
`state=online,lag=0`.

### V10.6.1 - Deferred TLS Optimizations (V9.7.5 tail) [Detour, after V10.6]

Moved here from `BACKLOG.md` on 2026-08-12 and scheduled as a short detour
**after V10.6 closes**, not before — it is bounded, unrelated work, and the
failover steps above have a dependency chain that is worth finishing without an
interruption in the middle of it.

The body of V9.7.5 shipped (see V9.7). These three are intentionally NOT done —
each is gated on a measured need, not implemented speculatively. Escalate only
when a metric demands it. **That gate survives the move**: arriving here does not
mean "now implement all three", it means "now go take the measurement that says
whether any of them is warranted."

- **Handshake CPU under an accept storm** — escalate in this exact order, and
  re-measure accept-to-first-command latency under a connection burst after each
  step before moving to the next:
  1. Session resumption (done, V9.7.5) — already reduces how many *full* handshakes occur.
  2. Cap accepts per poll tick: change the unbounded
     `while (handle_accept(listeners[i].fd, listeners[i].is_tls) == 0) {}`
     (`server.cpp:1148`) to a bounded loop (e.g. `k_max_accepts_per_tick`) so one
     connection burst can't monopolize a tick and starve already-established
     connections' read/write readiness. Cheapest, and helps plaintext too.
  3. Last resort only, if 1-2 don't hold up: move the `SSL_do_handshake` call
     (`tr_handshake`) onto `g_data.thread_pool`, posting the result back through
     the same completion-channel pattern the Argon2 auth path uses (V9.6.2) —
     including its conn-id liveness check, since the conn can be destroyed (client
     gave up, `tls-handshake-timeout` fired) while the handshake CPU work is in
     flight on a worker thread.
- **kTLS** (`SSL_OP_ENABLE_KTLS`): do not implement speculatively — requires a
  measured before/after on MYRED's actual small-message workload showing it
  matters first. Not planned until that measurement exists.
- **Cert reload without restart** (operability, not perf; explicitly last, only
  once everything above is done and stable):
  1. Add a trigger — a dedicated command or `CONFIG SET` support for
     `tls-cert-file`/`tls-key-file` specifically (reversing V9.7.2's boot-only
     decision for just those two directives).
  2. Build a **new** `SSL_CTX` by re-running `tr_tls_init`'s sequence — do not
     mutate `g_tls_ctx` in place, so a bad cert/key is rejected without disturbing
     the live context.
  3. On success only, atomically repoint `g_tls_ctx = new_ctx;` — do **not**
     `SSL_CTX_free` the old one. OpenSSL refcounts it (every live conn's `SSL*`
     holds a reference via `tr_tls_attach`), so it frees itself once the last
     connection using it closes. On validation failure, keep serving on the old
     ctx and report the error — never leave the server without a working `SSL_CTX`.

**Carry-overs: all clear.** V9.7 TLS closed 2026-07-25 (603/603 both transports),
V8 Pub/Sub, V8 Transactions, V8.8 and V9.8 all closed. The two bugs that were
carried past V10 — the 🔴 `SPOP` empty-key `SREM` frame and the 🟡 `ACL GENPASS`
PRNG — were both **fixed 2026-08-11**; root causes in `CODE_REVIEW.md` →
Post-V10 Carry-overs. No open bugs in the data path.

## Completed Milestones

### V10 - Replication and High Availability [Done]

Opened 2026-08-03, closed 2026-08-13. Master-replica replication end to end plus
a coordinated, zero-data-loss handover: identity/offset/backlog bookkeeping, the
full-resync handshake on both sides, read-only replicas that survive restarts,
partial resync off the backlog, automatic reconnect, ack tracking and `WAIT`,
detection of a link that goes silent without closing, a durability floor, and
`FAILOVER`. **V10.6e** (automatic, Sentinel-style election) is the only piece
left and sits in Current Focus. Cluster/hash-slot sharding was split out to
**V12** on 2026-08-12 — it shares no code and no design with failover, which is
precisely why bundling them under one number left this unscoped for three weeks.

Regression coverage is `scripts/test_replication.py`: its own master, two
replicas, a separate pair for `FAILOVER`, and a **killable, freezable in-process
TCP proxy**. Killing the master instead would destroy its backlog and mint a new
`repl_id`, forcing a full resync and making every partial-resync assertion
silently vacuous. `freeze()` is the other failure mode and the one V10.6b/d need:
stop moving bytes while leaving every socket **open**, because a link that closes
is already handled — `poll()` reports it immediately. Still gitignored like every
suite but `stress_test.py`; see BACKLOG → V11 Step 0.

**Three lessons, each learned more than once.**

1. **A deadline nothing wakes up for is not a deadline — six forms.** `poll()`
   sleeps until the next known timer and returns `-1` when there is none, so any
   periodic replication work missing from `next_timer_ms()` stops the moment
   traffic does. It had to be taught, one incident at a time: the reconnect retry
   (V10.4c), the `REPLCONF ACK` cadence (V10.5), the `WAIT` timeout (V10.5), the
   replica's link timeout and the master's reap deadline (V10.6b), the `FAILOVER`
   timeout (V10.6d). Every one showed up only on an *idle* server, which is why
   those tests watch the server's **stderr file** instead of polling `INFO` — an
   `INFO` poll wakes the loop and hides the exact bug being tested.
2. **Correct data proves nothing about which resync path ran.** Full and partial
   resync both leave a correct replica, so `sync_full`/`sync_partial_ok`/
   `sync_partial_err` in `INFO stats` are part of the deliverable, not
   instrumentation: they are the only thing a test can assert on. The suite's
   first run caught a missing `sync_full++` — the path worked, the counter didn't.
3. **A test that stops at the first success measures the happy path of the fix.**
   V10.6a's sites 1-9 all grep-verified, the build was clean, and the headline
   assertion (`sync_partial_ok:1`) was **true** — while the replica quietly kept
   the *dead* master's replid and would full-resync on every reconnect after the
   first. The defect lived in a field nobody had thought to assert on (site 10),
   and only the **second** reconnect exposes it.

#### V10.1 - Identity, offset, and backlog [Done 2026-08-03]

Pure bookkeeping, no networking: `repl_id` (40 hex, regenerated at boot),
`master_repl_offset`, and a ring-buffer `repl_backlog` sized by `repl-backlog-size`
(`boot_only`, 1 MB default). `repl_backlog_off` is *derived*
(`master_repl_offset - histlen + 1`), never stored.

The propagation choke point is the tail of `aof_feed`/`aof_append_raw` (renamed
`propagate_cmd`/`propagate`), **not** a sibling block in `do_request`: a sibling
would have diverged the replica three ways, since `SPOP`'s synthetic `SREM`,
eviction's synthetic `DEL` and `EXPIRE`'s relative-time re-encode all happen
inside those two functions. Three `g_config.aof_enable` gates collapsed into one
`propagate_enabled()`.

#### V10.2 - REPLCONF / PSYNC handshake + full resync [Done 2026-08-07]

`REPLCONF`, `PSYNC`, `REPLICAOF`/`SLAVEOF` (a second wire-compat row, not an
alias). **Master side**: `PSYNC ? -1` marks the `Conn` `is_replica`, answers
`+FULLRESYNC`, ships an image via `rdb_build_image()` (synchronous, no fork),
then streams writes into that conn's buffer exactly like a Pub/Sub subscriber.
`GlobalData::replicas` holds raw `Conn*` and `conn_destroy` unlinks it — the
third instance of the `pubsub_remove_conn`/`watch_clear_conn` guard pattern.
**Replica side**: `HANDSHAKE → RDB_LEN → RDB_BODY → STREAMING` driven from
`handle_read`, the image draining into `repl_rdb_buf` (not `incoming`, which
never shrinks) and loading through `rdb_load_buffer` after a `flushall`. From
`STREAMING` on, frames run through normal `do_request` dispatch via a privileged
pseudo-`Conn`, the way AOF replay does under `g_loading`. Both link ends are
exempt from the idle sweeps — an idle master link is healthy — and the exemption
must `dlist_init` as well as detach, since a frozen `last_active_ms` still reaps.

Three transcription slips caught only by watching the wire: `SPOP`'s synthetic
`SREM` carried an empty key (live since V9.6.4, corrupting the AOF too), the
master-link read branch was nested inside the `k_max_incoming` check so the whole
read path was dead code, and `+FULLRESYNC`'s length-12 compare missed its
trailing space so no handshake line ever matched.

Deliberate limitations, all from `repl_apply` running under `g_loading`:
replicated writes reach neither the replica's AOF, nor keyspace notifications,
nor `WATCH` invalidation, and chained replication does not work.

**V10.2.1** made OpenSSL optional (`MYRED_TLS`/`MYRED_HAVE_TLS`), confined to
`transport.cpp`; a no-TLS build fails loud rather than serving cleartext on a
configured `tls-port`. Lesson: a misspelled `#ifdef MYRED_HAEV_TLS` compiles
clean and takes the `#else` branch forever. **V10.2.2** added `INFO [section]`
(`k_info_sections`, table order = output order) and `info_add`'s
`__attribute__((format(printf,...)))`, which replaced a ~25-conversion positional
`snprintf` — though a field wired to the *wrong variable* still type-checks.

#### V10.3 - Read-only replicas [Done 2026-08-07]

**a)** `g_data.replica_mode` (named against `Conn::is_replica`, which means the
opposite subject) gates writes after ACL and arity, so a rejected write inside
`MULTI` behaves like an ACL denial (`EXECABORT`) through `resp_err_txn`. Role and
link-phase stay separate flags: gating on `repl_state != NONE` would silently
promote a replica to writable on every dropped socket. The stream itself bypasses
the gate via `g_loading`.

**b)** The gate protects a *running* replica only — a restarted one came back a
writable master pointed at nothing. One `boot_only` `replicaof` row, whose
`apply` may only **record** the target, never connect: it runs before
`repl_init()`, before the local RDB/AOF load, and before the poll loop exists.
`apply` and `emit` deliberately read different state (`g_config` vs the live
`g_data` role) so `CONFIG REWRITE` records what the server *is*. The test that
mattered was `REPLICAOF NO ONE` + `CONFIG REWRITE` dropping the line: until
promotion, staged and live values are identical and a wrongly-bound `emit` is
byte-identical.

#### V10.4 - Partial resync, and reconnecting on its own [Done 2026-08-09]

Validation is framed as **"how many bytes is this replica missing"** —
`need = (master_repl_offset + 1) - psync_offset`, serviceable when
`need <= repl_backlog_histlen` — which never touches `repl_backlog_start_offset()`
(whose `histlen == 0` sentinel is where an off-by-one would hide) and makes
`need == 0` a valid empty `+CONTINUE` instead of a special case. **Every
rejection falls through to a full resync**: an unnecessary RDB costs bandwidth, a
wrongly accepted `+CONTINUE` is silent divergence. `+CONTINUE` must be matched
*before* the "skip anything that isn't `+FULLRESYNC`" ack rule, or it is
discarded as a stray `+OK`.

**V10.4c** re-dials from `repl_cron` with a 1s→8s backoff that has exactly one
owner (it grows before dialing, resets when the link is healthy, and `repl_stop`
is deliberately left alone since `repl_start` calls it). It also fixed a latent
bug the retry loop would have weaponized: `repl_start` called `repl_stop()` —
which clears `replica_mode` — before three failure returns, so one transient
`socket()` failure demoted the instance to a **writable master** that then never
retried, because `repl_cron` gates on `replica_mode`.

#### V10.5 - Replica ACK tracking + `WAIT` [Done 2026-08-09]

The link became bidirectional: the replica reports its applied offset once a
second, the master records it per `Conn`, and `WAIT` answers how many caught up.
It needed no new blocking machinery — the reply defers on the conn and resumes
through `conn_resume`, the path async `AUTH` already used, and `Conn::in_exec`
(sitting in `state.h` unused since V8.5) got its first user. It answers
immediately under `in_exec` (the reply is one element of an already-sized array)
or `g_loading` (the `Conn` is a stack object and would dangle in `waiters` — the
same reason `do_psync` refuses there). `g_data.waiters` is the **fourth** raw-`Conn*`
registry `conn_destroy` unlinks. An unsatisfiable `WAIT` returns a **short count,
never an error**; there is no `REPLCONF GETACK`, so it resolves off the periodic
ack and can take up to a second where Redis takes milliseconds.

Two bugs, both about waking the loop. `REPLCONF ACK` is the first command that
answers *nothing*, and `handle_read`'s tail only cleared the optimistic
`want_write` when there was output — so the conn sat in write intent with an
empty buffer and tripped `handle_write`'s assert; **in a Release build that
assert compiles out and degrades into a silent per-tick zero-length write**,
surfacing only as unexplained CPU. And `next_timer_ms()` knew `repl_retry_at_ms`
but not `repl_ack_at_ms`. Diagnosing the second produced a false positive worth
remembering: reading `INFO` from the *replica* woke its loop and made an ack fly,
so the bug vanished under observation. Also fixed: `REPLCONF listening-port`
validated `p > 65536` instead of `p < 65536`, so `replica_port` was never
assigned and every `slave0:` line reported `port=0`.

#### V10.6a - Promotion that works in the case you actually promote in [Done 2026-08-13]

Four defects, all found by reading (and then running) the promotion path against
the failover scenario rather than the happy path.

- **`REPLICAOF NO ONE` was a silent no-op on a replica whose master is down** —
  gated on `repl_state != NONE`, the *link phase*, but `repl_link_lost` sets that
  to `NONE` while deliberately keeping `replica_mode = true`. So in the only
  situation anyone types the command, it returned `+OK` and did nothing. The gate
  must be the role flag.
- **Promotion threw the history away.** `repl_new_id()` overwrites `repl_id`
  outright, so every sibling repointed at the new master fails `id_ok` and takes a
  full resync — one synchronous `rdb_build_image()` each, at the moment the
  deployment is already a node down. Replaced with `repl_shift_id()`: keep the old
  identity as `repl_id2` with `second_repl_offset`, and let `do_psync` honour a
  replica naming it.
- **…except a replica never fed its own backlog**, because `repl_backlog_feed`'s
  only call site is inside `propagate()`, which `propagate_enabled()` gates off
  while `g_loading` is set. The ring has to be warm *before* the promotion; it
  cannot be backfilled after. The `STREAMING` branch now feeds the master's bytes
  **verbatim** — a re-encode could differ in length and drift every later offset.
  Two traps, both of which bit on first application: `repl_backlog_feed` advances
  `master_repl_offset` **itself**, so it replaces the manual `+=` rather than
  joining it (keeping both double-counts every byte and corrupts `REPLCONF ACK`
  and `WAIT`), and the feed must precede the `buf_consume`.
- **Site 10: the replica never adopted the replid `+CONTINUE` carries**, so the
  partial resync worked exactly once — see lesson 3 above.

A function that is declared, defined and never called draws no warning at
`-Wall -Wextra`; that is how `repl_shift_id()` sat unused after "eight sites
applied".

#### V10.6b - Detect a master that stops talking without closing [Done 2026-08-13]

A wedged or black-holed master left the replica in `STREAMING` indefinitely,
serving stale reads and reporting `master_link_status:up`, because nothing was
watching the clock. Added `repl-timeout` (runtime-settable, seconds on the wire
and ms in `Config`), `master_last_io_ms` stamped on **any** byte from the master,
`master_last_io_seconds_ago` in `INFO`, and the mirror image on the master side —
a replica that stops acking is dropped, since a dead one otherwise counts toward
`WAIT` and toward V10.6c's quorum.

- **The replica check is phase-independent, not `STREAMING`-only.** The master
  link sits in no idle/io list on purpose, which also means nothing had ever
  reaped it: a master that accepts the socket and never sends `+FULLRESYNC` had
  stranded replicas in `HANDSHAKE` since V10.2b.
- **Only replicas that have acked at least once are reaped.** `ack_time_ms == 0`
  means "still doing its initial resync"; reaping those would kill exactly the
  big-dataset replicas the timeout was never meant to touch. They hold
  `ack_offset` 0, so they can satisfy no `WAIT` and inflate no quorum.
- **A keepalive is not optional here, it is the other half of the timeout.**
  Nothing travels master→replica on an idle link (`REPLCONF ACK` is
  replica→master only and is never answered), so a timeout measured on inbound
  bytes expires on a perfectly healthy master: at `repl-timeout 5` the replica
  dropped a healthy master twice in 13 seconds and forced three resyncs. The
  master now feeds a `PING` every `repl-ping-replica-period` (10s) through
  `repl_backlog_feed` + `repl_feed_replicas` — **deliberately not `propagate()`**,
  which would grow an idle server's AOF without bound — and it *does* advance
  `master_repl_offset`, because the replica counts every byte it consumes.
  `ping-period << repl-timeout` is the real contract between the two directives.
- **A cached deadline outlives the setting it came from.** `repl_ping_at_ms` is
  written only when a ping fires, so lowering the directive at runtime didn't
  take effect for one full *old* interval — precisely when someone is lowering
  it. The apply lambda now re-arms from `get_monotonic_msec()`. `repl-timeout`
  stores no deadline and applies instantly.
- `config_selfcheck`'s get→apply→get probe **cannot** catch a seconds/ms
  mis-wiring: a getter missing its `/1000` still round-trips against itself. Same
  shape as the V9.8 `appendonly` bug. Only an external `CONFIG SET 5` → `GET`
  catches it.

#### V10.6c - `min-replicas-to-write` / `min-replicas-max-lag` [Done 2026-08-13]

The write-safety knob, and the one that decides whether a failover loses data: an
isolated old master that keeps accepting writes is *how* split-brain costs you
writes, and this makes it stop on its own without needing to know it has been
superseded. `good_replicas()` is a loop over `g_data.replicas` counting those
that acked within `max-lag` seconds, riding the existing `spec.is_write` gate
beside the read-only check. Refuses with `-NOREPLICAS`; reads are untouched.

- **Master side only, and never under `g_loading`.** On a replica the read-only
  gate has already turned the client away, and the master's own stream must never
  be refused — a replica that drops a write it was *sent* has silently forked.
- **No new "is it alive" stamp at attach**, which is where the first draft went.
  `ack_time_ms == 0` already means "attached, never heard from", and requiring it
  non-zero is what keeps a still-loading replica out of the quorum; stamping at
  attach would have started a `repl-timeout` clock against replicas that are
  legitimately mid-image, reversing V10.6b's third decision without noticing it
  was a decision.
- `min-replicas-max-lag 0` means "do not judge on lag", so it counts replicas
  still loading their image. That is the documented meaning of 0 and a weaker
  guarantee than the default 10.

#### V10.6d - `FAILOVER [TO host port [FORCE]] [ABORT] [TIMEOUT ms]` [Done 2026-08-13]

Coordinated, zero-data-loss, planned handover — **no second process, no quorum,
no election.** The master already knows every replica's acked offset, so it
pauses writes, waits for the chosen replica to reach `master_repl_offset`, then
hands over. `do_failover` only sets state; `failover_cron` does the work, because
the demotion has to destroy connections and that is `server.cpp`'s job.

- **The offsets line up for free, and that is the whole trick.** The target is
  promoted by `PSYNC ... FAILOVER` *before* the resync logic runs, so `repl_id2`
  is exactly the history it shared with the sender — which the sender named in
  that very PSYNC — and `second_repl_offset` is where the sender stopped writing.
  It asks for `second_repl_offset` and is owed **zero bytes**. This only works
  because step 1 paused writes; without the pause the finish line moves every
  time a client writes and the target never arrives. Verified by the suite: a
  clean handover moves **no RDB at all** (`sync_full` unchanged,
  `sync_partial_ok` +1).
- **Demoting must drop every replica first.** This server does not relay a stream
  it is itself receiving, so after the handover its replicas would sit on a link
  that never feeds them again — and the target is *among* them, which would loop
  its own writes back at it. Collect before destroying: `conn_destroy` erases
  from the set being iterated.
- **`repl_start` had to learn about it.** `have_history` derives from
  `replica_mode`, still false mid-demotion, so the handover would have sent
  `PSYNC ? -1` and pulled a full image — losing the entire point of the command.
- **`ABORT` is refused once `IN_PROGRESS`.** We have already demoted and asked the
  target to take over; it may have promoted a millisecond ago. Redis allows it;
  this does not, and the narrower rule is the defensible one.
- **The write pause refuses rather than blocks** (`-FAILOVER`). Same correctness
  property — no write survives past the offset snapshot — but a client sees an
  error instead of a delay. Doing it properly means routing writes through the
  deferred-reply path `WAIT` uses; filed in BACKLOG.
- **`FORCE` pays for its lost bytes with a full resync, and must.** The demoted
  master is *ahead* of the offset the target promoted at, so serving it a
  `+CONTINUE` would keep writes the new master never saw: two instances, one
  replid, different data. The suite pins the full resync as the correct outcome.

**Five slips landed while applying V10.6c/d by hand, and four were invisible to
the compiler.** Worth keeping as the shape of this failure mode:

- The `NOREPLICAS` gate **replaced** the `READONLY` gate instead of following it,
  so every replica in the tree was writable and V10.3a was silently undone. A
  snippet that says "insert after" can land as "replace" — grep that the *anchor*
  still exists, not just that the new line does.
- `port < 1 || port < 65535` rejected every usable port in `FAILOVER TO`. **The
  same inverted comparison as V10.5's `p > 65536`, in the same subsystem, four
  days apart.**
- `g_data.failover_force = force;` was never written, so `FORCE` parsed, passed
  validation and did nothing — `failover_cron` always took the abort branch. Only
  a *runtime* test could see this one; it reads fine.
- `failover_state` was emitted inside `if (replica)`, i.e. invisible in
  `WAIT_FOR_SYNC`, the one state that only ever exists on a master (and a
  duplicated `master_replid` line came with it).
- `min-replicas-max-lag` defaulted to 0 instead of 10, quietly turning the lag
  half of V10.6c off.

### V9.8 - Config refactor: one directive table [Done]

Closed 2026-07-30. `config_apply` (parse/validate/assign), `config_get_value`
(format/return) and `config_rewrite` (format/write) each hand-enumerated the same
~23 directives. One truth, three lists — and **forgetting one was silent**: no
compiler error, no warning, no failing test unless that exact path ran. Four
incidents came from it: the `requirepass` emission pasted over in `config_rewrite`
(`3e2d0e9`, undetected 6 days, server came back passwordless),
`notify-keyspace-events` missing from get (V8.3), `appendfilename` missing from
get (V8.5), and four directives plus two string-literal typos in the new getter
(V8.8). All three functions are now walks over `k_config_table`; `config_apply`
went from ~290 lines to 15 and `config_get_value` from ~60 to 5.

**Shipped in three stages** (V9.8.1 has its own entry below; V9.8.2 was split into
a hybrid migration where the table was consulted first and the old if-chain stayed
as fallback, so directives moved in batches and each batch was verifiable — chosen
over a flag-day rewrite because of this project's transcription-slip rate).

- **One row owns everything about a directive**: name, arity, `apply`, `get`,
  `boot_only`, `masked`, `emit`. Table order *is* config-file order, so
  `config_rewrite` is a walk with no ordering logic of its own.
- **`emit == nullptr` is the load-bearing marker.** It means "plain scalar":
  single-valued, unmasked, assign-not-append. That one property is what makes both
  the shared formatter and the boot round-trip check safe on a row, so it does
  double duty and no separate `multi` flag was needed. Anything conditional
  (`tls-*`, `auditlog`, `requirepass`), multi-line (`bind`, `allow-ip`, `save`) or
  accumulating (`user`, `rename-command`) supplies its own `emit`.
- **`masked` rows must supply an `emit`**, asserted at boot. `requirepass`'s getter
  answers `<set>`; its `emit` reaches past the getter to the stored hash. Without
  that split the shared formatter would write the placeholder to disk and the next
  boot would hash the literal string `<set>` as the password.
- **The round-trip check skips masked rows for the same reason** — re-applying
  `requirepass`'s own getter output would hash `<set>` at *every* boot. The trap
  reappears inside the check designed to prevent it.
- **`boot_only` replaced a string-prefix hack.** `do_config` matched
  `p.rfind("tls-", 0) == 0` to decide what `CONFIG SET` may not touch; it now asks
  the table.
- `user` and `rename-command` carry `get == nullptr` — write-only config-file
  constructs with no single-value form, which is why they were never in
  `config_all_names`.

**Two pre-existing bugs surfaced by the migration**, both invisible until a
directive's parse and format sat next to each other — see `CODE_REVIEW.md`:
`tls-auth-clients` rejected the value `no`, and the `appendonly` getter read
`protected_mode`. The second was live for one stage and is the sharpest lesson
here: a `format → apply → format` round-trip **cannot** catch a getter wired to
the wrong field, because reading the wrong field is perfectly self-consistent.
Only writing a distinct value and reading it back catches it, which is what the
`[REG]` probe block in `stress_test.py`'s CONFIG section now does.

### V9.8.1 - Shared formatter for unconditional scalars [Done]

Closed 2026-07-30. `config_rewrite` now emits 12 directives via
`config_write_scalar()`, which reads the value from `config_get_value()` — so
`CONFIG GET` and `CONFIG REWRITE` are structurally incapable of disagreeing about
`port`, `protected-mode`, `dbfilename`, `appendonly`, `appendfilename`,
`appendfsync`, `maxmemory`, `maxmemory-policy`, `maxmemory-samples`,
`notify-keyspace-events` and the two `auto-aof-rewrite-*`. Names live in three
arrays (`k_scalars_net/data/aof`) rather than one, because the hand-written
multi-line directives sit between them in file order; `port`/`protected-mode`
moved above `bind` to make the first run contiguous, which is safe since all four
are plain assignments in `config_apply`.

- **`requirepass` is blocked twice, deliberately.** `config_write_scalar` returns
  false for it at the choke point (aborting the whole rewrite, leaving the live
  file untouched via the existing `unlink(tmp)` path), *and* `metadata_selfcheck`
  fails the boot if it ever appears in `config_rewrite_scalars()`. The masked
  `<set>` value reaching disk would make the next boot hash that literal string as
  the password — a rerun of `3e2d0e9`.
- **Quote on demand, not always.** The first cut quoted every value and broke
  `test_security.py`'s `port {port}` grep. Only an empty value or one containing
  whitespace / `#` / `"` / `\` needs quoting; everything else stays bare. Keeps
  11 of 12 byte-identical to the old output, and fixes the latent bug where an
  unquoted `dbfilename /path/with space` made the *next boot* fail `need1()`.
- Verified: two consecutive `CONFIG REWRITE`s diff clean (idempotent), the
  password round-tripped as a real `$argon2id$` line, `dbfilename "my dump.rdb"`
  emitted quoted beside a bare `appendfilename appendonly.aof`, and a restart on
  the rewritten file came back up and answered `my dump.rdb`. Nothing in `scripts/`
  covers rewrite idempotence — that check is manual, on a throwaway conf.
- Also fixed here: `test_security.py`'s `ACL CAT` assertion still expected 8
  categories after `@transaction` made it 9 in V8.4, and gained a `[REG]` check
  that feeds every advertised category back through `ACL SETUSER` so the
  emit/parse pair cannot silently split again.

### V8.8 - Follow-up fixes [Done]

Closed 2026-07-26. Two defects filed during V8.4–V8.7, both in security-adjacent
code, cleared to restore the "no open bugs" invariant.

- **🟠 `ACL SETUSER` was not atomic.** It bound `User &u = g_config.users[cmd[2]]`
  and applied modifiers onto the live user, returning early on the first bad one —
  leaving a half-configured user, and creating one even when the command failed
  outright. Ordering decided whether that failed closed or open:
  `acl setuser u on '>p' '+@all' '~bad['` left `u` enabled with `+@all` and no key
  restriction. Now stages onto a `User` copy and commits with `std::move` only on
  full success. The `// stable address` invariant holds — assigning through
  `operator[]` on an existing key overwrites the node's value in place, so live
  `Conn::user` pointers stay valid.
- **🟡 `CONFIG GET` answered for 3 directives while `CONFIG SET` accepted all of
  them.** Replaced the hand-rolled list with `config_get_value()` +
  `config_all_names()` in `state.cpp` (deliberately beside `config_rewrite`, since
  the two format the same values), covering all 23 gettable directives. `do_config`
  now glob-matches like Redis, so `CONFIG GET maxmemory*` and `CONFIG GET *` both
  work. `user` and `rename-command` stay out — structural multi-line directives
  that Redis does not expose through CONFIG either (`ACL LIST` covers users).
- **`requirepass` is masked** (`<set>` / empty), not returned. What is stored is an
  Argon2id hash, and a hash is a *verifier*: whoever holds it can test candidates
  offline at their own speed — no round trip, no `k_max_auth_inflight` throttle, no
  audit entry, no lockout. This is not a new policy but the existing one applied to
  the one path about to violate it: `acl_format_user`'s `for_config` flag already
  makes `ACL LIST` emit `#<hash>`, so real credential material goes only into the
  config file on disk, never over the wire. See BACKLOG → Open Decisions for the
  alternative.
- A boot selfcheck in `metadata_selfcheck()` asserts every advertised directive is
  actually gettable. It earned its place immediately — the first build died on six
  violations (four missing directives, plus `"Mmaxmemory-samples"` and
  `"notify_keyspace-events"` typos). Without it the server would have started fine
  and `CONFIG GET appendfilename` would have silently returned `[]` again: the
  exact bug V8.8 existed to fix, reintroduced while fixing it. Note the limit — it
  catches a name listed but not gettable, **not** a directive added to
  `config_apply` and forgotten in both lists. That direction needs V9.8.

### V8 - Transactions [Done]

Closed 2026-07-26. `MULTI`/`DISCARD`/`EXEC`/`WATCH`/`UNWATCH` — queue a batch of
ordinary commands and commit it as one uninterruptible unit. **Atomicity was free:**
the event loop is single-threaded, so no other connection can interleave between
queued dispatches. The work was the per-conn state machine and the reply framing.

- **V8.4 transaction mode + queueing** — `Conn` gains `in_multi`, `multi_dirty`,
  `queue_cmds`. All five transaction commands dispatch conn-aware **above** the
  queueing gate (the `AUTH`/`ACL`/pub-sub shape), which is what makes them
  unqueueable and `EXEC`'s recursion safe without a depth guard. The gate sits
  *after* the found/ACL/arity checks on purpose, so all three rejections poison the
  batch through `resp_err_txn` — Redis's `flagTransaction`/`EXECABORT` semantics
  for free. Queued commands are stored **as typed, never canonicalized**:
  `dispatch_build()` erases the old name from `g_dispatch` under `rename-command`,
  so canonical storage would let `EXEC` resurrect a command deliberately renamed
  away. Pub/Sub mode switches are rejected at queue time, never deferred. New ACL
  category `@transaction` (bit 8) — four parallel lists, of which parse and emit
  are a matched pair.
- **V8.5 `EXEC`** — swap the queue into a local batch, leave `in_multi`, emit
  `resp_arr(n)` and dispatch straight into `out`; RESP array elements are just
  concatenated encoded values, so no per-command scratch buffer is needed.
  **Caught a 🔴 the plan had missed**: a queued command has no verbatim client
  bytes, so `do_request`'s AOF branch appended nothing — transactional writes
  replied `+OK` and vanished on restart. Fixed by making **`raw == nullptr` a
  contract** meaning "re-encode the log entry from the vector".
- **V8.6 `WATCH` registry** — eager dirty-marking over `GlobalData::watchers`
  (key → conns), so `EXEC` checks one bool instead of re-diffing at commit time.
  It could *not* ride V8.3's notify hook: that hook is gated on notifications being
  enabled and captures only `cmd[1]`, which is a tolerable miss for an event but a
  correctness bug for `WATCH`. `cmd_collect_keys()` was extracted from `acl_check`
  as the single source of truth for a command's keys. Marking is conservative
  (false aborts are safe under optimistic locking, missed ones are not); natural
  expiry deliberately does not invalidate, eviction does; `watch_clear_conn` in
  `conn_destroy` is a use-after-free guard.
- **V8.7 integration + `UNWATCH`** — the invalidation reply is a null **array**
  (`*-1`, new `resp_nil_arr`), not the null bulk `resp_nil` writes; both render as
  `(nil)`, so it is pinned at wire level. Watches clear at all three `EXEC` exits
  and at `DISCARD`, and **before** the dispatch loop — otherwise the batch's own
  writes re-dirty its conn and break every *later* transaction, a delayed failure
  that would be miserable to trace.

**Deliberate divergences from Redis:** each queued write is logged to the AOF
individually rather than wrapped in `MULTI`/`EXEC` (replay's `Conn fake{}` now has
an `in_multi` field, so a `MULTI` in the log would queue the rest of the file into
`queue_cmds` and silently drop it); `UNWATCH` and `AUTH` execute immediately inside
`MULTI` instead of queueing.

**Process note:** four more silent transcription slips — a duplicated `{"multi",…}`
key that dropped `discard` entirely (`unordered_map` initializer lists keep the
first duplicate, no `-Wall` warning), `"ERR"` missing its trailing space,
`acl_cat_bit` left unedited so `ACL CAT` advertised a category `SETUSER` rejected,
and a fourth loop-from-zero (`do_watch` registering a phantom watcher on a key
literally named `watch`). None caught by the compiler, all by grep-verify.

Regression coverage: `test_transactions` + `test_transaction_watch` in
`scripts/stress_test.py`, with `[REG]` checks for the wire-level `*-1`, multi-key
invalidation, and the `watch` key-name slip.

### V8 - Pub/Sub [Done]

Closed 2026-07-25. A live broadcast mechanism with **no storage and no
persistence** — a message only reaches clients subscribed at the moment it is
published. (Transactions share the V8 number for scheduling only — unrelated
feature, no broadcast mechanism involved. They took the V8.4–V8.7 steps.)

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

**`stress_test.py` is the only tracked suite.** The per-milestone suites
(restart matrix, security, pub/sub, replication, eviction, the AOF shell
scripts) are gitignored and local-only as of 2026-08-09 — their coverage is
being folded into `stress_test.py`, tracked in BACKLOG → V11. Ports 12401–12410
are reserved for their private instances so they never collide with a live
server. Anything a local suite proves that `stress_test.py` does not is coverage
at risk of being lost.

Replication coverage lives in `scripts/test_replication.py` (~145 checks,
capability-gated so it stays runnable while a milestone is half-applied): full
and partial resync, the read-only gate, link loss, backlog overflow, automatic
reconnect, `WAIT`, `repl-timeout` on a wedged link, the V10.6c durability floor,
three-node promotion with a sibling partial-resync on the **second** reconnect,
and the whole of `FAILOVER` on its own pair (12408–12410) — pause, `ABORT`,
`TIMEOUT` on an idle server, `FORCE` and its full resync, and the clean handover
that moves no RDB. Timer-deadline phases assert on the server's **stderr file**,
never by polling `INFO`, because polling wakes the loop the test is trying to
catch asleep.

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
