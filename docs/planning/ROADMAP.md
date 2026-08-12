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

Date: 2026-08-09.

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
| Replication | In progress (V10.1–V10.5 done; only V10.6 failover/cluster left) |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V10 - Replication and High Availability [In Progress]

Master-replica mode, split into gated sub-steps because "replication" is four
separable systems (bookkeeping, handshake/full-resync, write-mode enforcement,
partial resync) that fail independently and are worth testing independently.
Sequenced after V9.8 because replication adds config surface, and adding it to
three hand-maintained lists was exactly the risk the config table removed.

**V10.1-V10.5 are closed — see Completed Milestones.** Working master-replica
replication: full and partial resync, read-only replicas that survive restarts,
automatic reconnect, ack tracking and `WAIT`. Only V10.6 is left, and it is
deliberately unscoped.

#### V10.6 - Sentinel-style failover, cluster/hash-slot sharding [Backlog, unscoped]

Deliberately left as a stub, not fleshed into steps yet. Both are separate,
much larger problems (leader election and split-brain avoidance for failover;
key-space partitioning and cross-node command routing for cluster) that
deserve their own design pass. **V10.1-V10.5 are now done and tested, so the
precondition for scoping this is met** — it was deliberately left unscoped until
there was a real working system to design against instead of guesses.

**Carry-overs: all clear.** V9.7 TLS closed 2026-07-25 (603/603 both transports),
V8 Pub/Sub, V8 Transactions, V8.8 and V9.8 all closed. One 🟡 filed in BACKLOG
(`ACL GENPASS` uses a non-cryptographic PRNG), deliberately scheduled after V10.

## Completed Milestones

### V10.1–V10.5 - Replication [Done]

Closed 2026-08-09. Working master-replica replication end to end: identity and
backlog bookkeeping, the full-resync handshake on both sides, read-only replicas
Closed 2026-08-09. Working master-replica replication end to end: identity and
backlog bookkeeping, the full-resync handshake on both sides, read-only replicas
that survive restarts, partial resync off the backlog, automatic reconnect, ack
tracking and `WAIT`. Only V10.6 (failover / cluster) is left.

Regression coverage is `scripts/test_replication.py` — its own master, replica and
a **killable in-process TCP proxy**, because partial resync needs the link to break
while the master keeps running (killing the master destroys its backlog and mints a
new `repl_id`, forcing a full resync and making the test silently vacuous). It is
gitignored like every suite but `stress_test.py`; see BACKLOG → V11 Step 0.

**The recurring lesson of this milestone, in three separate incidents: a deadline
nothing wakes up for is not a deadline.** `next_timer_ms()` had to learn about the
reconnect retry (V10.4c), the `REPLCONF ACK` cadence (V10.5) and the `WAIT` timeout
(V10.5) — `poll()` sleeps until the next known timer and returns `-1` when there is
none, so any periodic replication work that is not represented there simply stops
the moment traffic does.

#### V10.1 - Replication identity, offset, and backlog [Done]

Closed 2026-08-03. Pure bookkeeping, no networking yet: `GlobalData` gained
`repl_id` (40 hex, regenerated at boot via the same `rand_idx()` source as
`ACL GENPASS`), `master_repl_offset`, and a ring-buffer `repl_backlog`
(`repl_backlog_pos`/`histlen`) sized by the new `repl-backlog-size` directive
(`boot_only`, 1 MB default, added through `k_config_table`). `repl_backlog_off`
is derived (`master_repl_offset - histlen + 1`), never stored.

The propagation choke point sits at the tail of `aof_feed`/`aof_append_raw`
(renamed `propagate_cmd`/`propagate`), not a sibling block in `do_request` —
a sibling would have diverged the replica three ways: `SPOP`'s synthetic
`SREM`, eviction's synthetic `DEL`, and `EXPIRE`'s relative-time re-encode all
happen inside those two functions, not in `do_request`. The three separate
`g_config.aof_enable` gates collapsed into one `propagate_enabled()`
predicate. `INFO` gained `master_replid`/`master_repl_offset`/
`repl_backlog_*` fields; `connected_slaves` deliberately stayed hardcoded `0`
until V10.2 made it real.

#### V10.2 - REPLCONF / PSYNC handshake + full resync [Done]

Closed 2026-08-07 (**V10.2a**, master side, 2026-08-03; **V10.2b**, replica
side, 2026-08-07). New commands `REPLCONF listening-port/capa/ACK` (ack-only
no-ops), `PSYNC <replid> <offset>`, `REPLICAOF host port` / `REPLICAOF NO ONE`
(`SLAVEOF` as a second wire-compat row, not an alias) — all conn-aware special
cases in `do_request` except `REPLICAOF`, which is an ordinary `k_cmd_table`
row since it mutates server-wide state rather than per-connection state.

**Master side**: `PSYNC ? -1` marks the `Conn` `is_replica`, replies
`+FULLRESYNC <repl_id> <offset>\r\n`, sends an RDB image via the new
`rdb_build_image()` (a synchronous, no-fork split of the existing
`rdb_build_aof_preamble()`), then streams every subsequent write into that
`Conn`'s `outgoing` buffer exactly like a Pub/Sub subscriber.
`GlobalData::replicas` holds raw `Conn*`; `conn_destroy` unlinks it — the
third instance of the same use-after-free guard pattern as
`pubsub_remove_conn`/`watch_clear_conn`.

**Replica side**: `REPLICAOF host port` opens a normal outbound `poll()` fd, a
state machine `HANDSHAKE → RDB_LEN → RDB_BODY → STREAMING` driven from
`handle_read` (`repl_master_data` replaces `try_one_request` for that one
`Conn`). The RDB image drains into `repl_rdb_buf` (not `incoming`, which never
shrinks) and loads via the existing `rdb_load_buffer`. From `STREAMING` on,
every frame runs through the normal `do_request` dispatch via a privileged
pseudo-`Conn` that bypasses `NOAUTH`/ACL the same way AOF replay bypasses
them under `g_loading`. The replica's dataset is wiped via the existing
`flushall` handler before the image loads.

Both link ends had to be exempted from the idle/IO timer sweeps (an idle
master link is healthy; a replica may hear nothing for minutes) — the
exemption must detach *and* `dlist_init` the conn's timer node, since
`dlist_detach` doesn't self-link and a frozen `last_active_ms` still gets
reaped.

Three silent transcription slips caught only by watching the wire, not the
compiler: `SPOP`'s synthetic `SREM` carried an empty key (pre-existing since
V9.6.4, corrupting the AOF too — see BACKLOG); the master-link read branch was
nested inside the `k_max_incoming` check instead of replacing it, so the
replica's entire read path was dead code above 64 MB; and `+FULLRESYNC`'s
length-12 compare against a literal missing its trailing space, so no
handshake line ever matched.

**Known limitations, deliberate** (all from `repl_apply` running under
`g_loading`): replicated writes don't reach the replica's own AOF, don't fire
keyspace notifications, and don't invalidate `WATCH`ers; chained replication
doesn't work; a dropped link doesn't auto-reconnect. Each is a V10.3+
concern, not a data-correctness bug.

#### V10.2.1 - Optional-dependency build [Done]

Closed 2026-08-07 (detour, not replication work — build-system only, taken
mid-V10.2b). OpenSSL is now optional like Argon2 already was:
`option(MYRED_TLS ON)` + non-`REQUIRED` `find_package`, gated on
`MYRED_HAVE_TLS`. Confined to `transport.cpp`/`state.h`'s forward-declared
`ssl_st*`, so making it optional touched one `.cpp`, no headers. `tr_read`/
`tr_write` had to be restructured (TLS branch first with an early return,
plaintext as the unconditional tail) to avoid a no-return path under
`-Wreturn-type`. A no-TLS build fails loud (`fatal_exit`) rather than
silently serving cleartext on a configured `tls-port`; zlib stays a hard
requirement (on-disk RDB format compatibility). Caught one silent bug: a
misspelled `#ifdef MYRED_HAEV_TLS` compiles clean and takes the `#else`
branch forever — grep guard spellings against the CMake definition after any
`#ifdef` edit.

#### V10.2.2 - `INFO [section ...]` [Done]

Closed 2026-08-07 (prerequisite surfaced by V10.2b — `INFO replication`
previously errored on arity). `{do_info, 1, -1}` + `k_info_sections`, table
order = output order, unknown sections emit nothing (matches Redis). The real
win: `info_add`'s `__attribute__((format(printf, ...)))` makes `-Wformat`
check every field against its own argument, replacing a ~25-conversion
positional `snprintf` where a field could silently read the wrong variable —
the same hand-maintained-parallel-list shape V9.8 removed from config. Note
the blind spot this does *not* close: a field wired to the wrong variable
still type-checks (the `appendonly`/`protected_mode` class from V9.8).

#### V10.3a - Read-only replica gate [Done]

Closed 2026-08-07. `g_data.replica_mode` (named to avoid colliding with
`Conn::is_replica`, which means the opposite subject) gates writes in
`do_request`, placed after ACL and the arity check so a rejected write inside
`MULTI` behaves like an ACL denial (`EXECABORT`) via the existing
`resp_err_txn` poison path. Role and link-phase are separate flags — gating
on `repl_state != NONE` would silently promote a replica to writable on every
dropped socket, since `repl_link_lost` resets `repl_state` but not the role.
The replication stream itself bypasses the gate via `g_data.g_loading` (same
flag `may_log`/`may_notify`/`may_watch` use) — a different bypass mechanism
than `NOAUTH`'s, which uses a superuser identity. Verified live: `SET`/
`FLUSHALL` on the replica answer `READONLY`, `GET` still serves, `REPLICAOF
NO ONE` promotes with a new `master_replid`.

#### V10.3b - `replicaof` config directive [Done]

Closed 2026-08-07. The V10.3a gate protects a *running* replica only: a
restarted one came back a writable master pointed at nothing, accepting writes
that vanish the instant someone re-issues `REPLICAOF` (full resync wipes first).
Found by testing V10.3a — both servers had been restarted for a rebuild and the
"replica" correctly answered `OK`, because it no longer was one.

One `boot_only` `k_config_table` row, so `REPLICAOF` stays the single runtime
path. **`apply` may only record the target, never connect**: it runs before
`repl_init()` mints a `repl_id`, before the local RDB/AOF load, and before the
poll loop exists — a resync image landing mid-load would be overwritten by the
local dataset. The connect is the last statement before the event loop, and a
failure there is fatal (failing open would leave a writable instance where a
replica was asked for). **`apply` and `emit` deliberately read different state**
— `apply` stages into `g_config`, `emit` reports the live role from `g_data`, so
`CONFIG REWRITE` records what the server *is*. Same exception `requirepass`
carries; invisible to the boot round-trip check, which skips rows with their own
`emit`. `get == nullptr` (two tokens, no single-value form, like
`user`/`rename-command`).

**The test that mattered was `REPLICAOF NO ONE` + `CONFIG REWRITE` dropping the
line.** Until promotion the staged and live values are identical, so an `emit`
bound to the wrong one is byte-identical and passes everything else — the same
reasoning as V9.8's distinct-value `CONFIG` probes.

#### V10.4 - Partial resync via the backlog [Done]

Closed 2026-08-09 (**V10.4a** master, **V10.4b** replica; V10.4c reconnect
deferred into V10.5). On `PSYNC <replid> <offset>` matching our `repl_id` with
the gap still in the ring, the master replies `+CONTINUE <replid>` and streams
only the missing tail from `repl_backlog_copy()` — no RDB.

- **Validation is framed as "how many bytes is this replica missing"**:
  `need = (master_repl_offset + 1) - psync_offset`, serviceable when
  `psync_offset <= master_repl_offset + 1` and `need <= repl_backlog_histlen`.
  That never touches `repl_backlog_start_offset()`, whose `histlen == 0`
  sentinel is where an off-by-one would hide, and `need == 0` (fully caught up)
  falls out as a valid empty `+CONTINUE` rather than a special case.
- Every rejection falls through to full resync. An unnecessary RDB transfer
  costs bandwidth; a wrongly accepted `+CONTINUE` is silent data divergence.
- **The replica needed almost nothing new** — V10.3a already preserved
  `replica_mode`/`master_host`/`master_port` across a link loss, and `repl_id`/
  `master_repl_offset` were never cleared. `repl_start` captures that history
  *before* its own `repl_stop()` call and claims it only for the same
  `host:port`, which is what makes `REPLICAOF NO ONE` → re-point correctly force
  a full resync (promotion minted a fresh `repl_id`). `+CONTINUE` must be matched
  **before** the "skip anything that isn't `+FULLRESYNC`" ack rule, or it is
  discarded as a stray `+OK`.
- **`sync_full`/`sync_partial_ok`/`sync_partial_err` in `INFO stats` are part of
  the deliverable, not instrumentation.** Both resync paths leave the replica
  with correct data, so no data assertion can tell them apart; the counters are
  what a test asserts on. `scripts/test_replication.py` caught a missing
  `sync_full++` on its first run — the full-resync path worked, only the counter
  didn't move.

#### V10.4c - Automatic reconnect [Done]

Closed 2026-08-09. `repl_cron(now_ms)` on the existing `process_timers` sweep
re-dials whenever `replica_mode && !master_link`, with a backoff doubling
`k_repl_retry_min_ms` (1 s) → `k_repl_retry_max_ms` (8 s) and resetting once a
link reaches `STREAMING`. Partial resync is now automatic instead of something a
- **`next_timer_ms()` had to learn about the retry deadline.** `poll()` sleeps
  until the next timer and returns `-1` (forever) when there is none, so on a
  replica with an idle keyspace `process_timers` would never run again and the
  link would stay dead permanently. A reconnect is only as alive as the thing
  that wakes the loop.
- **The backoff has exactly one owner.** It grows in `repl_cron` *before* dialing
  (so an attempt that fails synchronously cannot spin) and resets there too when
  the link is healthy — not at the two points that enter `STREAMING`. `repl_stop`
  is deliberately left alone: it is called *from* `repl_start`, so resetting the
  delay there would flatten every retry to a fixed 1 s.
- **Fixed a latent bug the retry loop would have weaponized.** `repl_start` called
  `repl_stop()` — which clears `replica_mode` — *before* three failure returns,
  and restored the role only on success. A failed dial therefore demoted the
  instance to a **writable master**, and since `repl_cron` gates on
  `replica_mode`, it would then never retry again. One transient `socket()`
  failure was enough. `repl_stop()` now runs only after the socket is open and the
  address validated, so every early return leaves the instance as it was.
- The reconnect copies `g_data.master_host` before calling `repl_start`, which
  takes `const std::string &` and clears that very member through `repl_stop()`.

#### V10.5 - Replica ACK tracking + `WAIT` [Done]

Closed 2026-08-09. The replication link is now bidirectional: the replica reports
its applied offset once a second (`k_repl_ack_period_ms`) from `repl_cron`, the
master records it per replica `Conn`, and `WAIT numreplicas timeout` answers how
many replicas have caught up.

- **`WAIT` needed no new blocking machinery.** The reply is deferred on the conn
  and resumed through `conn_resume` — the exact path async `AUTH` already used —
  and `osonn::in_exec`, which had been sitting in `state.h` labelled "blocking cmds
  must not block" since V8.5, got its first user. `try_one_request` gates on
  `auth_pending || wait_pending`, one notion of "this conn's reply is deferred".
- **It answers immediately under `in_exec` or `g_loading`.** Inside `EXEC` the
  reply is one element of an array `resp_arr()` has already sized, so deferring
  would leave the batch permanently short; under `g_loading` the `Conn` is a stack
  object in `repl_apply`/`aof_load`, so registering it in `waiters` would dangle —
  the same reason `do_psync` refuses there.
- `g_data.waiters` is the **fourth** raw-`Conn*` registry, so `conn_destroy`
  unlinks it beside pubsub/watch/replicas. `wait_try_resume` collects before
  settling: `wait_finish` erases from the set it is walking, and `conn_resume` can
  destroy the conn outright.
- An unsatisfiable `WAIT` returns a **short count, never an error** — the count is
  the timeout signal. No `REPLCONF GETACK`: `WAIT` resolves off the periodic ack,
  so it can take up to a second where Redis takes milliseconds. Deliberate — the
  alternative is sending commands down the replication stream for the replica to
  answer out-of-band from `repl_apply`.

**Two bugs, and both were about waking the event loop.**

- **`REPLCONF ACK` is the first command in MYRED that answers nothing**, and
  `handle_read`'s tail only cleared `try_one_request`'s optimistic
  `want_write = true` when there was actually output. So the conn sat in write
  intent with an empty buffer, `poll()` reported `POLLOUT`, and `handle_write`
  tripped its `assert`. `conn_resume` had carried the corrective `else` branch all
  along while calling itself "the mirror of handle_read's tail" — it was the more
  complete half. **In a Release build the assert compiles out and this degrades
  along while calling itself "the mirror of handle_read's tail" — it was the more
  complete half. **In a Release build the assert compiles out and this degrades
  into a silent per-tick zero-length write instead of a crash**, so it would have
  surfaced only as unexplained CPU on a master with replicas.
- **`next_timer_ms()` knew about `repl_retry_at_ms` but not `repl_ack_at_ms`**, so
  a replica sent one ack and then went silent the moment traffic stopped — `poll()`
  slept straight through the cadence. Same class as V10.4c's, three lines away from
  the fix that taught it. The `STREAMING` guard on the new branch matters: outside
  it `repl_ack_at_ms` is still 0, and `min(next_ms, 0)` spins the loop at 100% CPU
  for the whole handshake.
- Diagnosing it produced a **false positive worth remembering**: a probe that read
  `INFO` from the *replica* woke its loop and made an ack fly, so `WAIT` answered
  correctly and the bug vanished under observation. Only measuring from the master
  alone showed `lag` climbing 1,2,3,4,5,6 with no acks at all.
- Also fixed: `REPLCONF listening-port` validated `p > 65536` instead of
  `p < 65536`, so `replica_port` was never assigned and every `slave0:` line
  reported `port=0` — the one field whose entire purpose is naming an address
  worth dialing.

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
being folded into `stress_test.py`, tracked in BACKLOG → V11. Ports 12401–12406
are reserved for their private instances so they never collide with a live
server. Anything a local suite proves that `stress_test.py` does not is coverage
at risk of being lost.

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
