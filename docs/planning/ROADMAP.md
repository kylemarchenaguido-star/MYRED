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

Date: 2026-07-31.

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
| Replication | In progress (V10.1 — bookkeeping only, no networking) |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V10 - Replication and High Availability [In Progress]

Master-replica mode, `PSYNC`, a replication backlog, partial resync, replica
propagation for writes, and read-only replica enforcement. Split into gated
sub-steps, same shape as V9.7/V8's sub-milestones, because "replication" is
really four separable systems (bookkeeping, handshake/full-resync, write-mode
enforcement, partial resync) that fail independently and should be tested
independently. **V10.1 is active below; V10.2–V10.6 stay in `BACKLOG.md`.**

Sequenced deliberately after V9.8: replication adds config surface, and adding
it to three hand-maintained lists was exactly the risk the config table removed.

**The dependency this milestone used to list — AOF canonicalization for renamed
commands — is already satisfied**, not still pending. `do_request`
(commands.cpp) already computes `bool renamed = (cmd[0] != canonical)` and, on a
write, feeds a snapshot with `snapshot[0]` rewritten to the canonical name;
replay already resolves through `k_cmd_table` under `g_data.g_loading`
(`rename-command bricks the server on AOF restart`, fixed 2026-07-17). The
stream AOF already produces — canonical command names,
absolute-`PEXPIREAT`-reencoded TTLs, verbatim raw bytes for everything else —
**is** a valid replication stream. V10 does not need to invent a wire format; it
needs to fan those exact bytes out to replica connections instead of (or in
addition to) the AOF file.

**Why this codebase is unusually well set up for this**, worth internalizing
before writing any of it:
- The single-threaded `poll()` loop means "propagate to N replicas" has the
  same free-atomicity property already used for Transactions/EVAL — no
  interleaving to reason about, no locking.
- V8.1 Pub/Sub already proved the exact mechanism a replica connection needs:
  `do_publish` (commands.cpp) writes a RESP-encoded payload directly into
  *another* `Conn`'s `outgoing` buffer and sets `want_write = true`; the poll
  loop picks it up on the next tick with zero event-loop changes. A replica
  connection is a subscriber that never unsubscribes.
- `rdb_load_buffer(const uint8_t *data, size_t size)` (rdb.h/rdb.cpp) already
  exists and is already proven: `aof_load` (aof.cpp) calls it today to parse the
  RDB preamble embedded in the hybrid AOF format. A replica receiving a
  full-resync RDB payload over its socket calls this exact function on the
  buffered bytes — no new parser.
- `rdb_save_background` (rdb.cpp) already does fork-based, non-blocking snapshot
  generation. Full resync's RDB payload can be produced by the same path rather
  than a new one (see V10.2's note on the pragmatic first cut).

#### V10.1 - Replication identity, offset, and backlog (bookkeeping only, no networking) [Done]

Closed 2026-08-03, grep-verified in the tree and building clean. Landed across
`804de57` / `4b2c2ea` plus working-tree changes to `commands.cpp`/`server.cpp`.

Pure data structures and accounting. No replica can connect yet in this step —
it exists so the wire protocol in V10.2 has something real to report, and so the
byte-accounting is provably correct before anything depends on it over a socket.

- `GlobalData` gains `repl_id` (40 hex, regenerated every boot — reuse the
  `rand_idx()` source `ACL GENPASS` already uses, don't add a second one),
  `master_repl_offset`, and a ring buffer `repl_backlog` + `repl_backlog_pos` +
  `repl_backlog_histlen`, sized by a new `repl-backlog-size` directive (default
  1 MB, matching Redis). **`repl_backlog_off` is derived, never stored**
  (`master_repl_offset - histlen + 1`): a second counter is a second thing to
  drift.
- One new `k_config_table` row for `repl-backlog-size` (`masterauth` /
  `repl-timeout` come with V10.2) — through the V9.8 table, not an ad-hoc path.
  `boot_only` for the first cut: resizing a live backlog without losing
  in-flight partial-resync eligibility is a real design question, deferred
  rather than guessed at. `emit == nullptr`, so it is a plain scalar and gets
  the shared formatter plus the boot round-trip check for free.

**Placement decision — the propagation choke point is *not* a sibling block in
`do_request`.** This step's original plan called for one, reasoning that AOF and
replication are independent gates on the same byte stream. The *reasoning* is
right; the *location* is wrong, and would have shipped a diverging replica three
ways:

  1. **`SPOP` would replicate nondeterministically.** It sets `CmdSpec::aof_self`
     precisely so `do_request` does *not* log it, and feeds a synthetic
     deterministic `SREM` of the actually-popped members from inside the handler.
     A `do_request` sibling either misses that frame or ships the raw `SPOP`, and
     master and replica pop different members.
  2. **Evictions would resurrect on the replica.** `free_memory_if_needed()`
     feeds its synthetic `DEL` directly; it never passes through `do_request`'s
     logging block at all. The existing comment there already says "so AOF replay
     / replicas don't resurrect the key" — it was written for this.
  3. **TTLs would replicate as relative times.** The `EXPIRE` → `PEXPIREAT`
     re-encode lives *inside* `aof_feed`, so a sibling block encoding `snapshot`
     ships `EXPIRE key 100`, which the replica applies 100 ms later against its
     own clock. This is the same bug AOF already fixed once.

  The correct choke point is where the AOF bytes are actually produced — the tail
  of `aof_feed`/`aof_append_raw` — so the two sinks are fed from one call by
  construction and *cannot* describe different histories. Same argument the V9.8
  config table made: don't create a second list that has to be kept in sync by
  hand. What moves is the **gate**, not the hook: the `g_config.aof_enable`
  checks at the three call sites become one `propagate_enabled()` predicate
  (`!g_loading && (aof_enable || backlog armed)`), and each sink carries its own
  gate inside `propagate()`. Callers then decide only *whether a command produced
  stream bytes at all* — which is the independence the original note was after.

- Renames that follow, since the functions no longer serve only the AOF (no
  aliases kept): `aof_feed` → `propagate_cmd`, `aof_append_raw` → `propagate`.
- `INFO`'s `# Replication` stub (`do_info`) gains `master_replid`,
  `master_repl_offset`, `repl_backlog_active`, `repl_backlog_size`,
  `repl_backlog_first_byte_offset` and `repl_backlog_histlen` — the last four are
  what make the ring's wrap observable from `redis-cli`, so this step needs no
  new test script. `connected_slaves` is deliberately **not** added yet: a
  hardcoded `0` is a field nobody remembers to make real in V10.2, which is this
  project's most-repeated bug shape.
- Done when: `master_repl_offset` advances by exactly the byte length of the
  frames AOF would have logged (`SET foo bar` = 31 bytes over RESP), `SPOP` and
  an eviction both advance it, `repl_backlog_histlen` saturates at
  `repl-backlog-size` and `repl_backlog_first_byte_offset` starts advancing once
  it does, and `CONFIG SET repl-backlog-size` is refused as boot-only.

#### V10.2 - REPLCONF / PSYNC handshake + full resync [In Progress]

Gated on V10.1. First real networking; still only the full-resync path —
partial resync is V10.4, deliberately later.

**Split into two halves, applied and verified separately** (same reasoning as
V9.8's hybrid migration: this project's transcription-slip rate makes one large
application worse than two verifiable ones, and the two halves fail in
completely different ways):

- **V10.2a - master side. [Done]** Closed 2026-08-03, verified with a raw
  socket standing in for a replica: `+FULLRESYNC <replid> <offset>` matching
  `INFO`, a correctly framed `$<len>` image with the `MYRED` magic, and live
  `SET`/`SADD` frames arriving with no event-loop change. Two slips caught by
  that probe rather than by the compiler — a missing `\r` in the `+FULLRESYNC`
  terminator, and (pre-existing, see BACKLOG) `SPOP`'s synthetic `SREM` carrying
  an empty key because `lookup_entry` had already swapped `cmd[1]` out. **The
  second one had been corrupting the AOF since V9.6.4 and no suite noticed** —
  the first thing replication bought this project was a way to *watch* the write
  stream instead of trusting it.
- **V10.2b - replica side.** `REPLICAOF host port`, the outbound connection as
  a state machine in the same `poll()` loop, consuming `+FULLRESYNC` + RDB via
  `rdb_load_buffer`, then applying the stream through `do_request`.

- New commands: `REPLCONF listening-port <port>` / `REPLCONF capa <...>` (both
  effectively no-ops that just ack `+OK`, kept for wire compatibility),
  `REPLCONF ACK <offset>` (V10.5 needs it; safe to accept-and-ignore here),
  `PSYNC <replid> <offset>`, and `REPLICAOF host port` / `REPLICAOF NO ONE`
  (`SLAVEOF` as an alias — same `rename-command`-style aliasing already used
  elsewhere, not a special case). These need `Conn`, so they're conn-aware
  special-cases in `do_request` exactly like `AUTH`/`ACL`/pub-sub/transactions
  — do not route them through `k_cmd_table`'s two-argument `CmdFn`.
- **Master side**, on `PSYNC ? -1` (replica has no history — the only case this
  step handles): mark the `Conn` with a new `bool is_replica` flag, reply
  `+FULLRESYNC <repl_id> <master_repl_offset>\r\n`, then send an RDB payload,
  then transition this `Conn` into the same "just keep pushing bytes into
  `outgoing`" mode V8.1 built for Pub/Sub — every subsequent write's propagated
  bytes (from V10.1) also get written into every `Conn` with
  `is_replica == true`.
- **Generating that RDB payload: `rdb_build_image()`, synchronously, not a
  fork.** This step's original plan called for reusing `rdb_save_background`'s
  fork path to write a snapshot to a distinct temp file and streaming that file
  once the child exits. That is strictly more machinery than the codebase needs:
  `rdb_build_aof_preamble()` already serializes the whole keyspace into a
  `Buffer` in the parent, synchronously, and has done so since V6 — the
  BGREWRITEAOF hybrid format depends on it. Splitting its bare-image half out as
  `rdb_build_image()` gives full resync its payload with no fork, no temp file,
  no child-reaping hook, and no completion callback. The cost is an honest one:
  the loop stalls for the serialization, exactly as `SAVE` already does. Doing
  it off-loop is a real optimization (backpressure on a slow replica while
  forking is legitimately hard) and belongs after correctness — the same
  ordering TLS used, V9.7.1–.4 before V9.7.5.
- **The replica registry is a use-after-free hazard, not a convenience.**
  `GlobalData::replicas` holds raw `Conn*` that `propagate()` dereferences on
  every write, so `conn_destroy` must unlink — identical in kind to
  `pubsub_remove_conn` and `watch_clear_conn`, and the third instance of this
  exact pattern in the project.
- **Replica side**: `REPLICAOF host port` opens a normal outbound connection
  (just another fd in the same `poll()` loop — no new threading model needed,
  this is the payoff of the single-threaded design) to the master, sends the
  handshake, receives `+FULLRESYNC`, buffers the RDB bytes, and calls the
  already-existing `rdb_load_buffer` on them directly — zero new parsing code.
  From that point, every RESP frame arriving on the master connection is fed
  through the normal `do_request` dispatch (so every command gets the exact
  same handler a real client would trigger — no second implementation of
  `SET`/`ZADD`/etc.), but originating from a privileged pseudo-`Conn` that
  bypasses `NOAUTH`/ACL checks, mirroring exactly how AOF replay already
  bypasses them via `g_data.g_loading`.
- Done when: two MYRED instances, `REPLICAOF <master-host> <master-port>` on
  the second, the replica's dataset matches the master's at the moment of
  resync, and a `SET` issued on the master appears on the replica shortly
  after with no further manual action.

#### V10.2.1 - Optional-dependency build [Done] *(detour, not replication work)*

Closed 2026-08-07. A **detour taken mid-V10.2b**, filed here to keep the V10.2
block contiguous — it is build-system work, not replication work, and changes no
runtime behaviour on a fully-provisioned machine.

Trigger: on a dev machine with neither `libssl-dev` nor `libargon2-dev`,
`find_package(OpenSSL REQUIRED)` failed at **configure** time, so the tree could
not be built at all — not a degraded build, no build. Argon2 was already optional
(CMake + `cred.cpp` guards, since V9.6); OpenSSL was the only hard blocker.

- **OpenSSL is now optional**, mirroring the `MYRED_ARGON2` pattern already in the
  file: `option(MYRED_TLS ... ON)` + a non-`REQUIRED` `find_package`, setting
  `MYRED_HAVE_TLS` only on success. `OpenSSL::SSL`/`OpenSSL::Crypto` moved out of
  the unconditional `target_link_libraries` into that branch.
- **The V9.7.1 transport seam paid for itself a second time.** Because OpenSSL was
  already confined to `transport.cpp`, and `Conn::ssl` is a forward-declared
  `struct ssl_st *` in `state.h`, making TLS optional touched exactly one `.cpp`
  and no header. The seam's first payoff was keeping `server.cpp` OpenSSL-free;
  this is the second.
- **`tr_read`/`tr_write` had to be *inverted*, not just `#ifdef`-wrapped.** Both
  read `if (!c->ssl){ ...plaintext, all paths return... }` then fell through to a
  TLS tail. Wrapping only that tail leaves the no-TLS build a path with no return
  — `-Wreturn-type` under the project's `-Wall -Wextra -Wshadow`. The TLS block now
  comes first, inside the guard, with an early return; plaintext is the
  unconditional tail. The two `rv` declarations are not `-Wshadow` hits: the inner
  scope closes before the outer one is declared.
- **Fail loud, never downgrade.** A no-TLS `tr_tls_init` returns `false` when
  `tls_port != 0` rather than ignoring the directive, so `fatal_exit` names the
  missing package. Silently serving cleartext on a port an operator configured for
  TLS is the worst available outcome. Consequence to know: **`myred.conf` and
  `bench.conf` do not boot on a no-TLS build as shipped** — both set `tls-port`.
- **The Argon2 fallback has a sharp edge now that it is reachable.** A build
  without libargon2 cannot *verify* existing `$argon2id$` credentials — `cred.cpp`
  returns `false` for the PHC branch — so a correct password answers `WRONGPASS`.
  Not new code, but previously unreachable in practice; documented in README.
- **zlib stays required, deliberately.** Compression is part of the on-disk RDB
  format, so a build without it could not read snapshots written by a build with
  it. Only its error message improved (names `zlib1g-dev`).
- CMake prints a `MYRED build: TLS=... Argon2id=...` summary at configure time, so
  a silently-absent optional dep is visible then rather than at runtime.

Verified: both configurations build warning-free (the four `repl_*` unused-function
warnings are V10.2b's half-written replica side, pre-existing); `--correctness-only`
**652/652** green on the TLS-enabled build; the no-TLS build refuses a `tls-port`
config with the actionable message.

**Process note — a misspelled `#ifdef` is silent.** Hand-application slipped
`MYRED_HAEV_TLS` for `MYRED_HAVE_TLS`. That is not a compile error in either
direction: it takes the `#else` branch *forever*, so the build would have compiled
and linked and run with TLS quietly stripped out **even on a machine with OpenSSL
installed** — discoverable only by noticing `tls-port` had stopped working. Same
family as the duplicated `{"multi", …}` key (V8.4) and the four parallel ACL lists:
a name that is never checked against anything. Grep the guard spellings against
the CMake definition after any edit to a `#ifdef` region.

#### V10.3 - Read-only replica mode + command gating [Backlog]

Gated on V10.2. Small, but load-bearing — without it V10.2's replica accepts
writes from ordinary clients too, silently diverging from the master.

- A **server-wide** mode flag (`g_data.is_replica` — not per-`Conn`; this is
  unlike the `sub_channels`/`in_multi` per-connection modes, since the whole
  server is read-only, not just one connection). Gate placement in
  `do_request`: alongside the existing `NOAUTH` check, before ACL — a write
  command from an ordinary client on a replica replies `-READONLY You can't
  write against a read only replica.` and never reaches dispatch.
- The exception that must not go through this gate at all: the replication
  stream itself, applied via the privileged pseudo-`Conn` from V10.2. Route it
  around the `is_replica` check the same way it already bypasses `NOAUTH` —
  one bypass flag, not two.
- `REPLICAOF NO ONE` clears `is_replica`, closes the master connection, and
  **generates a fresh `repl_id`** — from this point the replica's write
  history has diverged from its old master's, so it must not claim continuity
  with the old `repl_id` (this is what makes V10.4's partial-resync validation
  safe later: a `repl_id` mismatch always means "don't trust this offset,
  fall back to full resync").
- Done when: a plain client connected directly to a replica gets `READONLY` on
  `SET` but correct data on `GET`; the replication stream itself still applies
  fine; `REPLICAOF NO ONE` makes the instance writable again with a new
  `repl_id`.

#### V10.4 - Partial resync via the backlog [Backlog]

Gated on V10.1 (backlog must exist) and V10.2 (basic full resync must be
solid) — this step is purely an optimization on top of both, never a
correctness requirement, so do not start it until full resync is trustworthy
enough to be the safe fallback.

- On `PSYNC <replid> <offset>` where `replid` is non-`?`: if it equals the
  master's own `repl_id` **and** `offset` still falls inside the live
  `repl_backlog` window, reply `+CONTINUE <repl_id>\r\n` and stream only the
  missing tail from the backlog — no RDB transfer.
- Any mismatch (different `repl_id`, or `offset` older than what the backlog
  still retains) falls back to V10.2's full-resync path unconditionally. This
  is the one piece of this whole milestone where "when in doubt, do the
  expensive-but-correct thing" is the right default — an incorrect partial
  resync is silent data divergence, which is much worse than an unnecessary
  full RDB transfer.
- Done when: a replica's connection is dropped and reconnected (or the network
  is briefly interrupted) while writes continue on the master; reconnecting
  with a gap small enough to still be in the backlog produces a `+CONTINUE`
  (verify via a log line or `INFO replication`, not just "it still has the
  right data" — a full resync would also leave it with the right data, so the
  test must distinguish the two paths); a gap larger than
  `repl-backlog-size` correctly falls back to full resync instead of failing.

#### V10.5 - Replica ACK tracking + `WAIT` [Backlog]

Gated on V10.2. Independent of V10.4 — can be built in either order once
basic streaming replication works.

- Replica sends `REPLCONF ACK <offset>` back to the master on a timer (every
  ~1s, matching Redis's cadence) reporting how far it has applied. This needs
  a new timer type alongside the existing idle/IO/TLS-handshake ones
  (`ConnTimer` in state.h) or a simple periodic check in the same tick loop
  that already drives `process_timers()` — reuse that dispatch point rather
  than adding a second timer sweep.
- Master tracks a per-replica-`Conn` "last acked offset," surfaced in `INFO
  replication` per connected replica (address, acked offset, lag).
- `WAIT <numreplicas> <timeout>`: block the *requesting client's* reply (not
  the event loop) until at least `numreplicas` have acked an offset ≥ the
  master's offset at the moment `WAIT` was issued, or `timeout` elapses. This
  is the same "can't literally block a single-threaded loop" family as the
  still-not-implemented `BLPOP`/blocking-list commands already flagged in
  ROADMAP's V8.4/V8.5 history — needs a per-conn "pending, resumed by a
  matching event or a timer" state, not a blocking wait. Do not invent a new
  pattern for this; if `BLPOP` lands first, copy its resume mechanism, don't
  design a second one.
- Done when: `WAIT 1 1000` against one healthy, caught-up replica returns
  quickly; against a replica that's stopped acking, it returns after
  `timeout` with a count less than requested, and the requesting connection
  stays otherwise responsive the whole time it was "waiting."

#### V10.6 - Sentinel-style failover, cluster/hash-slot sharding [Backlog, unscoped]

Deliberately left as a stub, not fleshed into steps yet. Both are separate,
much larger problems (leader election and split-brain avoidance for failover;
key-space partitioning and cross-node command routing for cluster) that
deserve their own design pass once V10.1-V10.5 are running and have survived
real testing — scoping them now, before basic replication even exists, would
be designing against guesses instead of an actual working system's real
failure modes. Revisit this section specifically once V10.5 is done.

**Carry-overs: all clear.** V9.7 TLS closed 2026-07-25 (603/603 both transports),
V8 Pub/Sub, V8 Transactions, V8.8 and V9.8 all closed. One 🟡 filed in BACKLOG
(`ACL GENPASS` uses a non-cryptographic PRNG), deliberately scheduled after V10.

## Completed Milestones

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
