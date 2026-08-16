# MYRED Backlog

Everything not yet started: open bugs, future milestones, deferred optimizations,
and feature gaps. See `ROADMAP.md` for current/completed work and `DECISIONS.md`
for design rationale.

## Open Bugs / Correctness Follow-ups

- ⚪ **Boot-time metadata cross-check for `k_cmd_table`** (follow-up, not a bug). V8.4's `discard`
  outage came from a duplicated `{"multi", …}` key in the `k_cmd_table`
  initializer list — `unordered_map` insert semantics drop the duplicate silently,
  no warning at `-Wall`. The `ks`/`extra` maps already carried `discard` entries
  with no command to attach to, so a reverse check at the end of
  `acl_init_categories()` — every key in `ks`/`extra`/`notify_cls`/`resolvers`
  must exist in `k_cmd_table` — would have caught it at boot. Same idea covers
  the four parallel ACL-category lists (DECISIONS → ACL Category Tagging).

- 🟡 **A master never schedules a wakeup for its own keepalive** (V10.6 residual,
  latent). In `next_timer_ms()` (`server.cpp:704`) the `repl_ping_at_ms` deadline
  is nested inside `if (g_data.replica_mode)`, so a plain master has no timer for
  the `PING` that V10.6b made the other half of `repl-timeout`. It is latent
  rather than live: a `STREAMING` replica's `REPLCONF ACK` arrives every second
  and wakes `poll()` on inbound data, well inside the 10s ping period, so the
  ping always goes out on time. But the schedule is being met by network traffic
  rather than by the timer meant to guarantee it — the same shape as every other
  bug in that milestone, and it stops being latent the moment a replica goes
  quiet, which is exactly when the keepalive matters. Fix is to hoist the branch
  to the top level of `next_timer_ms()`.
- ⚪ **`INFO` renders a never-acked replica as `state=online,lag=0`**
  (`commands.cpp:1583`) — "never heard from" displayed as "perfectly caught up",
  on the line an operator reads during an incident. `state=` is hardcoded
  `online`; the honest fix is a real `online` vs `sync` distinction, which
  `ack_time_ms == 0` already carries.
- ⚪ **The `FAILOVER` write pause refuses instead of blocking.** Redis pauses
  clients so the write lands *after* the handover; MYRED answers `-FAILOVER`. The
  correctness property is identical — no write survives past the offset snapshot
  — but a client sees an error where Redis shows a delay. Doing it properly means
  routing paused writes through the deferred-reply path `WAIT` already uses
  (`conn_resume`, `g_data.waiters`). Deliberate for V10.6d; filed rather than
  forgotten.

**No open bugs in the data path.** The items above are a hardening follow-up, a
latent scheduling gap, an observability wart and a deliberate protocol
divergence — none of them can corrupt or lose data. V8.8 shipped the equivalent
boot-time check for config directives and it caught six real violations on its
first build, so the same idea applied to `k_cmd_table` is still worth doing.

Every bug previously tracked here is FIXED; full root-cause
writeups live in `CODE_REVIEW.md` → Resolved Bugs Archive and in git history. New
bugs get filed here first, then folded into the CODE_REVIEW audit.

Recently resolved (terse; detail in CODE_REVIEW / git):
- `SPOP`'s synthetic `SREM` frame carried an empty key 🔴 — `lookup_entry()` swaps
  the caller's `std::string &keystr` into its probe and only hands it back on the
  *create* path, so `do_spop` reading `cmd[1]` afterwards logged `SREM "" <member>`
  and every popped member came back on AOF reload and on a replica. Live since
  V9.6.4. Fixed 2026-08-11 by reading `ent->key` (2026-08-11).
- `ACL GENPASS` minted passwords from `rand_idx()` 🟡 — Mersenne Twister is
  reconstructible from its output, so one leaked password exposed every other one
  the process ever generated. Fixed 2026-08-11 by routing it through the new
  `cred_random_hex()`, which is `getrandom(2)`-backed and fails closed
  (2026-08-11).
- `appendonly`'s getter read `protected_mode` 🔴 — introduced and fixed inside
  V9.8.2. Because V9.8.1 emits through the getter, `CONFIG REWRITE` wrote
  `appendonly <protected-mode's value>`, silently flipping AOF on or off across a
  restart. Invisible to a `format`→`apply`→`format` round-trip; caught by reading
  the table, and now pinned by a set-then-read-back probe (2026-07-30).
- `tls-auth-clients` rejected the value `no` 🟠 — branch read `v == "nos"`, and an
  invalid directive is fatal at boot, so writing it explicitly made the server
  refuse to start. Latent because the default is already `NO` and the rewrite only
  emits the directive when it differs (2026-07-30).
- `ACL SETUSER` was not atomic 🟠 — applied modifiers onto the live user and
  returned early on the first bad one, leaving a half-configured (or newly created)
  user; ordering decided whether that failed closed or open. Now stages onto a
  `User` copy and commits only on full success (V8.8, 2026-07-26).
- `CONFIG GET` answered for 3 of 23 directives 🟡 — replaced with
  `config_get_value()` + `config_all_names()` beside `config_rewrite`, plus glob
  matching and a boot selfcheck. Had cost a verification cycle twice
  (`notify-keyspace-events` V8.3, `appendfilename` V8.5). The deeper get/set/rewrite
  drift is now **V9.8** in ROADMAP, not a bug (V8.8, 2026-07-26).
- `CONFIG REWRITE` silently strips `requirepass` 🔴 — regression from `3e2d0e9`
  (v7.2.1 TLS plumbing) which pasted the `tls-*` block *over* the requirepass
  emission in `config_rewrite`, so a rewrite+restart came back passwordless with a
  nopass `+@all ~*` default user (and the old password answering `WRONGPASS`).
  Emission block restored before the `// TLS` section, both branches intact —
  `$argon2id$` verbatim, legacy SHA-256 re-prefixed with `#`, because an unprefixed
  hash would be re-hashed as plaintext on reload. Found by `test_pubsub.py`; it and
  `test_security.py` (red since 2026-07-19) both pin it now (2026-07-25).
- `rename-command` bricks the server on AOF restart 🔴 — replay-only `k_cmd_table`
  fallback in `do_request` under `g_loading`; `test_restart_matrix.py` green (2026-07-17).
- `nopass` breaks the ACL config round-trip 🟠 — accept `nopass` in `acl_apply_rule`
  (clears `pw_hashes`); `test_security.py` green (2026-07-17).
- `mem_reaccount` O(container size) per mutation 🔵 — delta accounting
  (`Deque::elem_bytes` + `HMap::elem_bytes`, O(1) `entry_mem_usage`), drift-verified 0 (2026-07-18).
- `rdb_load_set_entry` destroys every non-TTL set on load 🔴 — deleted garbled inner
  skip block, `entry_del` on member-read failure (2026-07-16).
- SPOP nondeterministic but AOF-logged verbatim — `CmdSpec::aof_self` + `do_spop`
  feeds synthetic `SREM` of popped members via `aof_feed` (2026-07-16).

## Multi-pair `CONFIG SET` (Redis 7 semantics)

Filed 2026-08-16, out of V10.6.1c. `do_config`'s `set` branch reads exactly
`cmd[2]`/`cmd[3]` and ignores anything after, so only one directive can be set
per call. That is a Redis 7 compatibility gap on its own, but the concrete cost
is in TLS: a certificate and its private key must swap **together**
(`SSL_CTX_check_private_key` rejects a new cert against the old key, in either
order), so rotating to genuinely *new paths* is impossible today. In-place
rotation — new bytes at the same paths, then `CONFIG SET tls-cert-file <same
path>` as the trigger — covers how certbot, cert-manager and mounted secrets
actually work, which is why V10.6.1c shipped without this.

Doing it properly means `CONFIG SET p1 v1 p2 v2 ...` applying as a transaction:
validate every pair first, then commit, so a bad second pair cannot leave the
first applied. Note that shape is already solved once in this codebase — V8.8's
`ACL SETUSER` stages onto a `User` copy and commits with `std::move` only on
full success. The same reasoning applies, with the extra wrinkle that a TLS
directive's apply() has a side effect (rebuilding the `SSL_CTX`), so the commit
step has to be "stage all values, then rebuild once", not "apply each in turn".

## Open Decisions

Not bugs and not scheduled work — deliberate choices worth revisiting if the
reasoning changes.

- **Should `CONFIG GET requirepass` return the real value?** V8.8 masks it
  (`<set>` when set, empty when not). Rationale: what we store is an Argon2id hash,
  and a hash is a *verifier* — whoever holds it can test candidates offline at
  their own speed, with no round trip, no `k_max_auth_inflight` throttle, no audit
  entry and no lockout. Argon2id's 76MiB memory bound makes each attempt expensive,
  but that buys time proportional to password strength, not immunity; the legacy
  `#<64 hex>` SHA-256 form is far weaker (unsalted, GPU-fast). The exposure is not
  merely admin-reads-own-secret: an ACL user holding `+config|get` or `+@admin` but
  not `default`'s password could walk away with `default`'s credential material,
  and hashes leak onward through dashboards, support bundles and screenshots.
  Masking also matches the existing invariant — `acl_format_user`'s `for_config`
  flag already makes `ACL LIST` emit `#<hash>`, so real material goes only into the
  config file on disk, never over the wire. Redis returns the value because Redis
  historically stored plaintext.
  - Alternatives if revisited: (a) return the real value to match Redis exactly —
    then drop the `ACL LIST` redaction too, so the policy is consistent in one
    direction; (b) keep masking but align the marker with the neighbouring
    convention (`#<hash>` instead of `<set>`).

## Next Major Milestones

### V8 - Transactions → moved to `ROADMAP.md`

Both halves of V8 are **done** and live in `ROADMAP.md` → Completed Milestones:
Pub/Sub (V8.1–V8.3) 2026-07-25, Transactions (V8.4–V8.7) 2026-07-26. The two open
bugs that were listed here became **V8.8**, closed 2026-07-26 — ROADMAP →
Completed Milestones.

### V10 - Replication and High Availability → moved to `ROADMAP.md`

**V10.1–V10.6d are DONE** (closed 2026-08-13) and compacted into `ROADMAP.md` →
Completed Milestones: bookkeeping, handshake and full resync, read-only replicas,
partial resync, automatic reconnect, `WAIT`, silent-link detection, the
`min-replicas-*` durability floor, and coordinated `FAILOVER`. The only V10 work
left is **V10.6e** (automatic, Sentinel-style election) — moved *here* on
2026-08-14 and filed below, after V11, because it does not get built until there
is a suite that can prove it against a running server. Cluster/hash-slot sharding
split out to V12 on 2026-08-12.

### V11 - Testing Hardening → moved to `ROADMAP.md`

**Promoted to `ROADMAP.md` → Current Focus on 2026-08-16**, when V10.6.1 closed
and it became the active milestone. **Step 0 closed 2026-08-15**: one command
(`stress_test.py --server <binary>`) now runs 1022 checks across eight
managed-instance phases with no setup, so the gate V10.6e was waiting on is met.
The differential, fuzz and adversarial work is what remains.

### V10.6e - Automatic failover, Sentinel-compatible [Unscoped, entry conditions met]

**Moved here from `ROADMAP.md` → Current Focus on 2026-08-14**, and the placement
*after* V11 is the decision, not an accident of ordering: this is the one
subsystem that acts on its own, at night, unattended. It gets built once there is
a suite that can be run against a server and prove it, not before. An automatic
failover you cannot regression-test is a liability, not a feature.

The last piece of V10, and the only part of it that is not deterministic:
deciding, **without a human**, that the master is gone, and agreeing on who takes
over. Everything V10.1–V10.6d does runs on one box and is driven by a command
somebody typed. This is distributed consensus — quorum, config epochs, leader
election, the `__sentinel__:hello` bus, `SENTINEL is-master-down-by-addr`.

What it can now build on, which is why refusing to start it earlier was right:
the handover itself is **done**. `FAILOVER` (V10.6d) is coordinated, pauses
writes, waits for the target's ack, loses nothing, moves no RDB, and has 35
checks standing on it. An election is only worth writing on top of a handover
that is already correct — a correct election driving an incorrect handover buys
exactly nothing.

Entry conditions, in order:

1. ~~**V11 Step 0** — one runnable regression surface.~~ **Done 2026-08-15.**
   The three shapes an election also needs are all in `stress_test.py`'s
   `replication` phase now: a freezable link, stderr-based assertions, and a
   phase that spawns its own instances from `PhaseCtx`. A fourth arrives with the
   election itself — killing a master and asserting on *who* won — and the
   promotion-history phase already stops a master and interrogates the survivors,
   so it is the place to build it.
2. Decide the scope: Sentinel-compatible (a separate process speaking the real
   Sentinel protocol, so `redis-cli --sentinel` and existing clients work) versus
   an in-process gossip between MYRED instances. The first is more work and far
   more useful; the second is tempting and strands you on a private protocol.
3. Only then scope the steps. Nothing below V10.6d needs to change for it —
   `repl_id2`/`second_repl_offset`, per-replica `ack_offset`, `min-replicas-*`
   and `FAILOVER` are the full mechanical surface an election needs to drive.

## Deferred TLS Optimizations (V9.7.5 tail) → CLOSED as V10.6.1, 2026-08-16

All three resolved, writeup in `ROADMAP.md` → Completed Milestones → **V10.6.1**.
The gate they carried here — measure first, implement only what a metric demands
— is what produced the spread: the bounded accept loop was **applied, measured
and reverted** (no effect outside the noise floor), kTLS was **declined on
arithmetic** (it removes a ~10ns copy against a 4.50 µs/op overhead), and cert
reload **shipped** (1.09 ms with connections surviving, against a 62 ms restart
that dropped them all). Nothing TLS is left in this file.

One lead was left open rather than closed, in case the accept-storm stall is
ever worth chasing again: the established-connection stall scales with total
burst size while CPU per connection stays flat, and the leading hypothesis is
OpenSSL's automatic session-cache flush every 256 handshakes. Detail under
V10.6.1a in ROADMAP.

## V12 - Cluster / hash-slot sharding [Unscoped]

Split out of V10.6 on 2026-08-12. It was bundled with failover under one number
and that is why V10.6 stayed unscoped for three weeks — the two share no code and
no design. Failover is a role transition on top of replication that already
exists; this is key-space partitioning, and it touches every command's key
extraction, adds `MOVED`/`ASK` to the error surface, needs a gossip protocol on a
second port, and needs resharding to be interruptible. It gets its own design
pass, after V11.

Note the hard ordering against V11: cluster multiplies the state space that the
differential and fuzz work has to cover, so building it *before* there is one
runnable regression suite means testing it by hand forever.

## Memory and Encoding Optimizations

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
  separately, Redis-style `INFO clients` / `MEMORY STATS` fields, so a slow reader
  draining `KEYS` output is visible as client memory, not fragmentation.
- Active defragmentation is explicitly deferred until after compact encodings;
  with the current one-allocation-per-node structures there is nothing useful to
  compact.

## Object Sharing

- Shared small-integer pool.
- Real object refcounts.
- Copy-on-mutate behavior.

## Hand-Tuned Hot Paths (Assembly / Intrinsics)

Educational track with a real payoff. **Not scheduled** — promote to a milestone
when picked. Gate: nothing here blocks V10.

### The target is `crc32_compute`, not `str_hash`

Measured 2026-08-07 (i7-1165G7, `-O2`, 64 MiB random buffer):

| | throughput | note |
|---|---|---|
| `crc32_compute` (rdb.cpp, byte-at-a-time table) | **0.22 GB/s** | current |
| zlib `crc32()` — same polynomial, hardware-accelerated | **2.10 GB/s** | **9.8x**, bit-identical output |
| `str_hash` (FNV-1a) | 7.0 ns @ 8B key · 16.6 ns @ 16B · 34.9 ns @ 32B | |

`str_hash` is **demoted to a footnote** (kept below). At the recorded 2.2M SET/s
baseline with realistic ~16-byte keys, hashing costs ~37 ms per core-second —
low single-digit percent of total work, and a *fraction* of that is the most any
rewrite could recover. The previous entry's instinct to profile first was right,
and profiling says no. `crc32_compute` is
where the headroom actually is, and it is a better teaching target on every axis:

- **9.8x of measured, already-proven headroom** — not a guess. zlib's number is
  the same algorithm on the same machine, so the gap is purely implementation.
- **A perfect correctness oracle.** The existing scalar function must be matched
  *bit-exactly*, and zlib is an independent third check. Compare that to
  `str_hash`, where any output is "correct" and there is nothing to verify against.
- **It runs over MB-scale buffers**, which is the only regime where SIMD wins.
  `str_hash` runs over 8–32 byte keys, where vector setup costs more than it saves.
- **It sits on a stall that matters right now.** `crc32_compute` →
  `rdb_serialize` → `rdb_build_image` → `do_psync` (commands.cpp:2549), which
  V10.2a runs **synchronously in the event loop** by deliberate design. Also
  `rdb_build_aof_preamble` (BGREWRITEAOF), `SAVE`, and both load paths
  (rdb.cpp:777, 861) at boot. At 0.22 GB/s every 100 MB of RDB is ~450 ms of pure
  checksum time — on full resync, that is 450 ms the loop serves nobody.
- **It is a pure function over a byte buffer**, so it needs no server, no event
  loop, and no socket to test — the same property that makes V11's
  `rdb_load_buffer` fuzz harness cheap.

### 🔴 Trap: `_mm_crc32_u64` is the WRONG instruction here

The previous version of this entry suggested it. It would corrupt the on-disk
format. SSE4.2's `CRC32` instruction computes **CRC-32C** (Castagnoli, poly
`0x1EDC6F41`). `crc32_compute` uses `0xEDB88320` — reflected ISO-HDLC, the
zlib/Ethernet polynomial. Different polynomial, different checksum: **every
existing RDB and hybrid-AOF file would fail its CRC check and refuse to load**,
and files written by the new build would be unreadable by the old one.

The correct hardware path for this polynomial is **`PCLMULQDQ`** (carry-less
multiply) doing polynomial folding — Intel's *"Fast CRC Computation for Generic
Polynomials Using PCLMULQDQ"* whitepaper is the reference implementation. The CPU
reports `pclmulqdq`, so this is available.

`_mm_crc32_u64` remains fine for a *new* checksum with no on-disk history. It is
only wrong for this one, and only because the format already shipped.

### How assembly enters this codebase

The infrastructure question, answered once so every later kernel reuses it.

**1. A seam, not a rewrite.** Same shape as the V9.7.1 transport seam and the
V10.2.1 TLS guard — the project has done this twice now and it works. One header
declares the operation; one TU per implementation:

- `crc32.h` — declares `crc32_compute` (unchanged signature; every existing caller
  keeps compiling).
- `crc32_scalar.cpp` — today's table version, moved out of rdb.cpp. **Always
  compiled, never deleted.** It is the portable fallback *and* the oracle.
- `crc32_pclmul.cpp` — the accelerated one, compiled only when the toolchain can
  target it.

**2. Runtime dispatch — the part that is easy to get dangerously wrong.** Do
**not** add `-mpclmul -msse4.2` to the global flags. That licenses the compiler to
emit those instructions *anywhere* it auto-vectorizes, so the binary dies with
`SIGILL` on an older CPU at some unrelated line, nowhere near this code. Two safe
options:

  - **Per-TU flags** — `set_source_files_properties(crc32_pclmul.cpp PROPERTIES
    COMPILE_OPTIONS "-mpclmul;-msse4.2")`. Only that file may emit them.
  - **Function multiversioning** — `__attribute__((target("pclmul,sse4.2")))` on
    the function itself, in a normally-compiled TU. No CMake change at all.

  Then pick once at boot with `__builtin_cpu_supports("pclmul")` and store a
  function pointer. Resolve it beside `metadata_selfcheck()`, where boot-time
  invariant setup already lives. One indirect call **per buffer**, not per byte —
  unmeasurable against a multi-MB checksum.

**3. Escalation ladder — stop at whichever rung stops paying.**

  - **Stage 0 — harness first, before any optimization.** A differential test
    (candidate vs scalar over random buffers at **every** length 0–4096, plus
    MB-scale) and a micro-benchmark. Plus a fixed-vector regression pinning the
    current constant, so any future change that alters the polynomial fails at
    build time instead of at a customer's RDB. Writing this first is what makes
    every later stage safe to attempt.
  - **Stage 1 — slicing-by-8, pure C, no intrinsics.** Eight lookup tables (8 KB),
    8 bytes per iteration. Typically 3–4x over byte-at-a-time, fully portable,
    zero dispatch machinery. **Do this before touching intrinsics**, because it
    teaches the real lesson — most of the 9.8x is *algorithmic*, not
    instruction-level — and it sets the honest baseline. Beating byte-at-a-time
    proves nothing; beating slicing-by-8 is a genuine result.
  - **Stage 2 — PCLMULQDQ intrinsics.** `_mm_clmulepi64_si128`, fold 128-bit
    lanes, Barrett reduction at the tail. Compiler still owns registers and ABI.
    This is where the remaining win lives.
  - **Stage 3 — standalone `.s`, only if Stage 2 measurably leaves something.**
    `enable_language(ASM)`, `extern "C"`, System V AMD64 ABI. Real educational
    payoff; usually a small delta over intrinsics. Gate on a measurement.
  - **Never inline `asm volatile` in a `.cpp` for this.** A subtly wrong clobber
    list produces corruption that appears only under optimization — inside a
    checksum, which is the worst possible place for a heisenbug.

**4. The honesty check — the rule that keeps this from shipping a toy.** zlib is
*already linked* and its `crc32()` is bit-identical and hardware-accelerated. So
zlib's number is the bar, not the byte-at-a-time version. If the hand-written
kernel cannot match it, the shippable decision is to call `zlib`'s `crc32()` and
keep the hand-written one behind a build flag as a learning artifact. The
educational track does not get to ship a slower checksum because it was fun to
write. (Noted deliberately: "just call zlib" is available *today* as a ~3-line
change if the 9.8x is ever wanted without the learning detour.)

**5. Verification, beyond the unit tests.**
  - Every length 0–4096 against scalar. Tail handling is the **#1 SIMD bug class**
    — a correct vectorized main loop with a wrong <16-byte remainder.
  - Cross-check against zlib on the same buffers.
  - Round-trip both directions: write an RDB with the accelerated build and load
    it with a scalar-only build, and vice versa. This is the check that would
    actually catch the `_mm_crc32_u64` polynomial trap.
  - This becomes the project's **first real C++ test file** — see the "C++ test
    layer" entry in the Upgrade Catalog, whose first named candidate this now is —
    and the natural precursor to V11's `rdb_load_buffer` fuzz harness, which
    exercises the same function over the same kind of buffer.

### Footnote: `str_hash`, kept but demoted

Not worth doing on the measurements above; recorded so it is not re-litigated.
FNV-1a's byte-at-a-time serial dependency chain also means a line-for-line asm
port cannot beat `-O2` — a win needs a *different algorithm* (CRC-32C via
`_mm_crc32_u64`, which **is** appropriate here, or xxHash), not "asm-ify FNV as
written." Worth knowing if it is ever revisited: swapping the hash function is
**safe with respect to persistence** — `hcode` is recomputed on every load
(rdb.cpp:490, 539, 605, 659, 706) and never written to disk — so this is a pure
in-memory change with no format consequences, unlike the RDB CRC.

## ACL and Command-Surface Feature Gaps

Missing features, not defects:

- `COMMAND`, `COMMAND DOCS`, `COMMAND COUNT` (`redis-cli` interactive mode probes
  these today, so this is a live usability gap, not just a compat checkbox).
- Full Redis ACL rule-order fidelity ("last match wins") — upgrade path recorded
  in DECISIONS → ACL Model.
- Selectors, `sanitize-payload`, `ACL LOAD`, `ACL SAVE`.
- Full `CONFIG GET/SET` coverage (also under Server Observability and Tooling;
  largely closed by V9.8/V8.8 — what's left is niche: LFU knobs with no load
  directives yet, and the deliberately-excluded multi-line `user`/
  `rename-command` rows).

## Command Coverage Gaps

Ordered by rough cost/value — trivial single-type gaps first, the two
self-contained new-type efforts last.

Hashes: `HRANDFIELD`, `HINCRBYFLOAT`.

Sets: `SINTERCARD`.

Generic: `COPY`, `SORT`, `SORT_RO`, `DUMP`, `RESTORE`, `EXPIRETIME`,
`PEXPIRETIME`, `OBJECT HELP`, `SCAN ... TYPE`, `WAIT` (concrete design for this
one lives in ROADMAP → V10.5).

Sorted sets: `ZINCRBY`, `ZCARD`, `ZCOUNT`, `ZMSCORE`, `ZPOPMAX`, `ZRANGEBYSCORE`,
`ZRANGEBYLEX`, `ZREVRANGE`, `ZREMRANGEBYRANK`, `ZREMRANGEBYSCORE`,
`ZREMRANGEBYLEX`, `ZUNIONSTORE`, `ZINTERSTORE`, `ZDIFFSTORE`, `ZRANDMEMBER`,
`ZSCAN`, `ZLEXCOUNT`, `ZRANGESTORE`, `ZMPOP`. Biggest single-type gap and a
heavily-used real-world type (leaderboards etc.) — highest payoff per command
implemented.

Strings and bitmaps: `SETBIT`, `GETBIT`, `BITCOUNT`, `BITPOS`, `BITOP`,
`BITFIELD`, `SUBSTR`, `LCS`.

Lists: `LPOS`, `LMOVE`, `RPOPLPUSH`, `LMPOP`, `BLPOP`, `BRPOP`, `BLMOVE`. The
three blocking ops need the same "pending, resumed by a matching event or a
timer" per-conn state as `WAIT` (ROADMAP → V10.5) — do not design that
mechanism twice; copy whichever lands first.

New data types (biggest lift, least urgent): HyperLogLog (`PF*`), Streams
(`X*`), Geo (`GEO*`), Bitmaps as a first-class area.

## Server Observability and Tooling

- `HELLO` and RESP3 handshake — foundational for newer client libraries that
  negotiate protocol version on connect.
- `CLIENT LIST`, `CLIENT KILL`, `CLIENT SETNAME`, `CLIENT GETNAME`, `CLIENT ID`.
- `RESET`, `SLOWLOG`, `LATENCY`, `MONITOR`, `DEBUG`, `SHUTDOWN`, `LASTSAVE`, `TIME`.
- Full `CONFIG GET/SET` surface (see BACKLOG → ACL and Command-Surface Feature
  Gaps for what's actually left — mostly closed already by V9.8/V8.8).

## Platform Work

Everything below is grounded in what the code actually calls today (checked by
grep across `server.cpp`, `transport.cpp`, `thread_pool.cpp`/`.h`, `rdb.cpp`,
`aof.cpp`, `client.cpp`, `state.cpp`, `cred.cpp`, `commands.cpp`, and
`CMakeLists.txt`), not a generic POSIX-porting checklist. CMake is already the
build system, so the build graph itself is not the hard part — the runtime API
surface and (per Difficulty 15) provisioning its Windows dependencies are.

### Possibilities

- **Native Windows binary, no WSL2.** Every perf baseline on record so far
  (`docs/tls_metrics.md`, and the WSL2 numbers noted in the roadmap) is
  measured under WSL2's syscall-translation layer; a native build is the only
  way to know MYRED's real Windows-kernel numbers instead of WSL2's.
- **Two viable routes, and they don't dodge the same wall:**
  - *MinGW-w64*, keeping `pthread_*`, `poll()`, and most `<sys/*.h>` headers
    via its POSIX-compatible layer. Smallest source diff, but it does **not**
    make `fork()` real — MinGW has no kernel-level `fork()` either, so the
    background-save redesign (Difficulty 1 below) is required under this
    route too.
  - *Native MSVC + Winsock2*, replacing the socket/thread layer outright.
    Bigger diff, but it's the version that can eventually plug into IOCP
    instead of `WSAPoll`, which matters if the Event Loop and Connection
    Scaling work (above) ever wants a real Windows-native backend rather
    than the portable-fallback `poll()`/`WSAPoll()` path.
- **`std::thread`/`std::mutex`/`std::condition_variable` instead of
  `pthread_*`.** `thread_pool.cpp`/`thread_pool.h` and the `g_loop_mu` mutex
  in `server.cpp` are the only `pthread_*` call sites in the codebase
  (create, join, mutex lock/unlock, cond wait/signal/broadcast — nothing
  exotic like `pthread_rwlock` or thread-specific keys). Since the project
  is already C++17, swapping these for the standard-library equivalents
  removes the Windows-thread problem *entirely* instead of translating it —
  one thread API for both platforms, no `#ifdef` needed for this part.

### Difficulties (ranked by how far they are from a 1:1 API swap)

1. **`fork()` for background persistence has no structural Windows
   equivalent — this is a redesign, not a translation.** `rdb.cpp:939`
   (bgsave) and `aof.cpp:133` (AOF rewrite) both rely on `fork()` giving the
   child an implicit, atomic, copy-on-write snapshot of the *entire* heap
   for free. `CreateProcess` on Windows starts a fresh address space with
   nothing shared — there is no API that reproduces "child sees a frozen
   copy of parent memory at the instant of the call." This is what the
   existing BACKLOG bullet "portable background snapshot design without
   `fork()`" is actually asking for, and it has to be solved once, for both
   platforms, not per-OS. Note for whoever designs it: MYRED forks from a
   process that already has thread-pool workers running (`aof_fsync_job` is
   queued from `server.cpp:870`) — `fork()` only duplicates the calling
   thread, so the child never has to worry about the other workers touching
   memory mid-copy. A non-`fork()` design (worker thread + explicit copy, or
   a stop-the-world serialize) has to preserve that same "exactly one
   execution context observes the data during the snapshot" invariant
   itself; the fork model got it for free, a thread-based one won't.
2. **`waitpid()`/`WIFEXITED`/`WEXITSTATUS` fall with `fork()`, not
   separately.** `rdb.cpp:965,974` and `aof.cpp:151,206,216` all reap the
   bgsave/AOF child this way. Once (1) is redesigned as a thread instead of
   a process, these calls disappear rather than needing a Windows
   equivalent — there's no child process left to wait on.
3. **Sockets are used as plain fds, not through socket-specific calls.**
   `client.cpp:19,49,51,93` (`write(fd, ...)`/`read(fd, ...)`) and every
   `close(fd)`/`close(connfd)` on a socket in `server.cpp`
   (207,214,572,584,645,649,655 — accept/connect/bind paths) call the raw
   POSIX I/O functions directly. A Winsock `SOCKET` is *not* a CRT file
   descriptor — `read()`/`write()`/`close()` do not work on it at all.
   Every one of these call sites becomes `recv()`/`send()`/`closesocket()`,
   and every adjacent `errno` check becomes `WSAGetLastError()`, a
   different, non-overlapping error-code space (see the table below). This
   is the most *invasive* change even though each individual swap is
   simple, because the call sites are scattered rather than centralized
   behind one wrapper — worth centralizing into `transport.cpp` as part of
   the port rather than patching in place. **This does not apply to every
   `read`/`write`/`close` in the codebase, only the ones holding a `SOCKET`**
   — `rdb.cpp:902-918`'s forked-snapshot writer and `commands.cpp:1213`'s
   audit-log fd (`open`+`write`+`close`, no socket involved) work unmodified
   through the MSVC/MinGW CRT's plain-file I/O and must be left alone; a
   mechanical find-replace across every `read`/`write`/`close` call site
   would wrongly rewrite these too.
4. **`eventfd()` is Linux-only — not even POSIX — and has no Winsock
   analogue.** `server.cpp` uses `eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC)`
   (line 1243) to let `aof_fsync_job` (run on the thread pool) wake the main
   `poll()` loop via `notify_loop()`/`loop_drain()` (lines 117-129) by
   writing to `g_loop_efd`, which sits in the same `poll_args` vector as
   the listen sockets (line 1266). `WSAPoll` can wait on sockets only —
   there is no fd-like kernel object it can poll that a background thread
   can signal the way `eventfd` does. The Windows replacement is a
   loopback socket pair (Winsock has no `socketpair()` either; the usual
   trick is a connected pair of local TCP sockets, or switching this one
   wakeup to `WSAPoll` + a dedicated Win32 event via `WSAEventSelect`).
   This needs its own small design, not a header swap.
5. **Non-blocking mode and "would block" checks are set/read through
   different mechanisms entirely.**
   - `server.cpp:95,98` sets `O_NONBLOCK` via `fcntl(fd, F_SETFL, ...)`.
     Winsock sockets have no `fcntl`; the equivalent is
     `ioctlsocket(fd, FIONBIO, &mode)` — a different function, not a flag
     rename.
   - `transport.cpp:138,168`, `server.cpp:556,760,1279` check
     `errno == EAGAIN`/`EINTR`. Winsock reports "would block" as
     `WSAEWOULDBLOCK` from `WSAGetLastError()`, a separate call from
     reading `errno`, and Windows has no signal-driven syscall
     interruption at all, so the `EINTR` half of every one of these checks
     simply has nothing to correspond to. Expect a `#ifdef _WIN32` at each
     of these sites, not a shared constant.
6. **`fsync`/`fdatasync` assume a POSIX fd; `FlushFileBuffers` wants a
   Win32 `HANDLE`.** Call sites: `state.cpp:848`, `rdb.cpp:387,918`,
   `aof.cpp:110,169`, `server.cpp:785,1328,1342` (the last three are the
   `appendfsync` `always`/`everysec` paths off the `Aoffsync` enum,
   `state.h:91`). Since the code mixes `FILE*` (`fileno(fp)`) and raw `fd`
   at these sites, the Windows path needs `_get_osfhandle(fd)` to bridge to
   a `HANDLE` before calling `FlushFileBuffers` — an extra conversion step
   at every call site, not a drop-in rename. `fdatasync` (data only, no
   metadata) has no metadata/data distinction on Windows either;
   `FlushFileBuffers` always flushes both, so the `always` vs `everysec`
   distinction in `Aoffsync` keeps its meaning but the syscall itself gets
   slightly more expensive per call on Windows than on Linux.
7. **`clock_gettime` doesn't exist on MSVC at all, and its two POSIX clock
   IDs map to two unrelated Windows APIs.** `get_monotonic_msec()`/
   `get_wall_msec()` (`state.cpp:1028-1039`) are the server's *only* time
   source — 44 call sites across `server.cpp`, `commands.cpp`, `rdb.cpp`
   and `aof.cpp`, covering every TTL check, the expire heap,
   `next_timer_ms()`'s scheduling, and — per the function's own comment —
   wall-clock values that get **persisted to disk** as TTL deadlines.
   MSVC's `<time.h>` defines neither `CLOCK_MONOTONIC`/`CLOCK_REALTIME` nor
   `clock_gettime` itself, so a naive port is a hard compile error, not a
   warning (MinGW-w64 does ship a shim through its pthreads layer, but it
   just wraps the same two Win32 primitives below, so it doesn't remove the
   need to know them).
   - `CLOCK_MONOTONIC` → `QueryPerformanceCounter()`/
     `QueryPerformanceFrequency()` for full precision, or the cheaper
     `GetTickCount64()` (millisecond resolution, ~15.6ms default timer
     granularity) — `get_monotonic_msec()` already truncates to
     milliseconds, so the simpler call loses nothing this codebase reads.
   - `CLOCK_REALTIME` → `GetSystemTimePreciseAsFileTime()` (Windows 8+;
     `GetSystemTimeAsFileTime()` on older targets), which counts 100ns
     ticks since 1601-01-01, not 1970-01-01 — every value needs the fixed
     116444736000000000-tick epoch offset subtracted before dividing by
     10000 for Unix milliseconds. Get this conversion reviewed, not just
     compiled: because `get_wall_msec()` writes straight into on-disk TTL
     fields, an off-by-epoch bug here doesn't crash, it silently mis-dates
     every expiry loaded on Windows.
8. **`/proc/self/status` doesn't exist on Windows at all — `INFO memory`'s
   RSS reading needs a different API, not a different path.**
   `get_memory_usage()` (`commands.cpp:762-782`) opens
   `/proc/self/status` and greps the `VmRSS:` line for resident set size;
   on open failure it already returns `0` rather than erroring
   (`commands.cpp:765-767`), which is the dangerous part — a straight port
   compiles, boots, and answers every `INFO memory` query with
   `used_memory:0` on Windows instead of failing loudly. `/proc` is a
   Linux-only pseudo-filesystem (not even POSIX), so there is no path
   variant to swap in. The Windows replacement is `GetProcessMemoryInfo()`
   (`<psapi.h>`, `PROCESS_MEMORY_COUNTERS`, dynamically available as
   `K32GetProcessMemoryInfo` with no extra linking since Vista) reading
   `WorkingSetSize`, the direct RSS analogue.
9. **`WSAStartup`/`WSACleanup` are pure additions with nothing to
   translate from.** POSIX has no per-process socket-subsystem
   init/teardown step at all, so this isn't a translation of an existing
   call — it's new code that has to run once before the first `socket()`
   call and once at shutdown, near the same place `server.cpp:1074-1077`
   installs the `SIGINT`/`SIGTERM`/`SIGXFSZ`/`SIGPIPE` handlers today.
10. **Signal handling only partially maps.** `server.cpp:1077` ignores
    `SIGPIPE` so a write to a closed socket returns an error instead of
    killing the process, and `server.cpp:1076` ignores `SIGXFSZ` (file-size
    limit exceeded) — on Windows both lines can simply be deleted, since
    neither concept exists for Winsock sockets or Windows file I/O; the
    error paths that already check the return value of `send()`/`write()`
    cover the same cases. `SIGINT`/`SIGTERM` (`server.cpp:1074-1075`) do
    exist in the Windows CRT, but nothing external delivers `SIGTERM` the
    way POSIX `kill(pid, SIGTERM)` does — a Windows service or another
    process asking MYRED to shut down gracefully needs a different trigger
    (`SetConsoleCtrlHandler`, a named event, or a service-control callback),
    which is new plumbing, not a signal-name rename.
11. **Near-drop-in, low-risk swaps** (worth calling out separately so they
    don't get lumped in with the hard problems above):
    - `poll()`/`struct pollfd`/`POLLIN`/`POLLOUT`/`POLLERR`
      (`server.cpp:1247-1321`) → `WSAPoll()` uses the *same* `struct pollfd`
      shape and the same flag names. This is the one place the existing
      BACKLOG note ("future Windows `WSAPoll` port is a third backend" in
      Event Loop and Connection Scaling) is accurate as a near-literal swap.
    - `inet_pton`/`sockaddr_in` (`client.cpp:145,148`; `state.cpp:994`;
      `server.cpp:203,206,551,641,644`) → identical signatures via
      `<ws2tcpip.h>`, once `<winsock2.h>` is included *before* `<windows.h>`
      (order matters — `<windows.h>` alone pulls in the older WinSock 1
      headers and conflicts) and the binary links `ws2_32.lib`.
    - `setsockopt(..., IPPROTO_TCP, TCP_NODELAY, ...)` and
      `setsockopt(..., SOL_SOCKET, SO_REUSEADDR, ...)`
      (`server.cpp:210,638,639`) → same constants exist on Windows, but flag
      this for testing, not just compiling: Windows' `SO_REUSEADDR` is
      looser than Linux's (it can let a second process silently rebind a
      port still in use by the first), so the port needs a behavioral check
      here, not just a green build.
    - `getpid()` (`rdb.cpp:357,900`, used only to uniquify bgsave/rewrite
      temp filenames) → `_getpid()` from `<process.h>`, or
      `GetCurrentProcessId()`. Cosmetic; no behavior to verify.
    - `stat`/`access`/`unlink` (`rdb.cpp:332-333,876,884,912,918`;
      `aof.cpp:107,110,195`; `server.cpp:1140,1141,1167`; `state.cpp:850`) →
      `_stat`/`_access`/`_unlink` from `<sys/stat.h>`/`<io.h>` under strict
      MSVC, or the identically-named functions unmodified under MinGW (which
      provides them directly). `F_OK` isn't defined by MSVC's headers — pass
      the literal `0` or `#define F_OK 0` alongside `_access`. **`rename` is
      the one call in this family that is NOT a drop-in — see Difficulty 13.**
    - `O_CLOEXEC` on the audit-log fd (`commands.cpp:1213`) has nothing to
      protect once Difficulty 1's redesign removes `fork()`: the flag exists
      to keep an fd from leaking into a child across `exec()`, and Windows'
      handle-inheritance model (`bInheritHandles` at process-creation time,
      no per-fd flag) doesn't have the same shape at all. Drop it rather than
      hunt for an equivalent.
    - The `0644`/`0640` mode argument on every `open(..., O_CREAT, mode)`
      call (`aof.cpp:101,180`; `rdb.cpp:902`; `server.cpp:1162`;
      `commands.cpp:1214`) compiles under the MSVC/MinGW CRT but silently
      stops meaning what it says: `_open`'s `pmode` only distinguishes
      `_S_IREAD`/`_S_IWRITE`, with no owner/group/other split at all, so
      `commands.cpp:1214`'s `0640` on the **audit log** — deliberately
      unreadable by "other" on Linux — becomes whatever ACL the parent
      directory hands out by inheritance on Windows, typically far more
      permissive. Same "compiles clean, quietly does less" shape as
      `SO_REUSEADDR` above, but security-relevant rather than cosmetic; a
      real Windows port needs an explicit `SECURITY_ATTRIBUTES`/ACL on this
      file, not just a mode-bits rename.
12. **Path handling is lighter than it looks.** There's no `realpath`,
    `getcwd`, `opendir`/`readdir`, or `PATH_MAX` use anywhere in the code —
    config/cert paths are plain strings joined with `'/'` (`state.cpp`
    config parsing). Windows accepts `/` as a path separator in its own
    APIs, so this is likely a non-issue in practice; the real risk is a
    future contributor hand-rolling a `\\`-based join instead of reusing
    the existing string path, which would silently break the Linux build.
    Worth a one-line convention note when this work actually starts, not a
    design task on its own.
13. **`rename()` for atomic replace is not atomic replace on Windows — a
    silent correctness gap, not a build error, and structurally as serious
    as `fork()` above.** MYRED's entire durability model is "write `.tmp`,
    `fsync`, `rename(tmp, target)`" so a crash mid-write can never corrupt
    the live file. That exact pattern is duplicated independently in three
    subsystems: `SAVE`/RDB (`rdb.cpp:399` rotates the previous dump to
    `.bak`, `rdb.cpp:401` commits the new one; the forked `BGSAVE` child
    repeats it at `rdb.cpp:920`), `BGREWRITEAOF` (`aof.cpp:175`), and
    `CONFIG REWRITE` (`state.cpp:854`). POSIX specifies that `rename()`
    atomically replaces an existing destination; the Windows CRT `rename()`
    — identical under MinGW and MSVC — instead **fails with `EEXIST`
    whenever the destination already exists**, which on this codebase is
    every call after the very first one, since replacing an existing file
    is the entire point. A naive header-swap port compiles clean, passes a
    first-boot smoke test (no `dump.rdb` yet), and then silently fails
    every `SAVE`/rewrite after that — the dangerous kind of gap, because
    nothing catches it until a real run with real data. The correct
    primitive is Win32 `MoveFileExW(tmp, target,
    MOVEFILEEX_REPLACE_EXISTING | MOVEFILEEX_WRITE_THROUGH)`, not the CRT
    `rename()` wrapper — same "new code, not a translation" shape as
    `WSAStartup` above, and it has to replace all four live call sites
    (the fifth, inside the forked `BGSAVE` child, disappears along with
    `fork()` itself per Difficulty 1, but its replacement path needs the
    same fix).
14. **The one CSPRNG call site has no Windows syscall equivalent — it needs
    the Windows crypto API, not a libc header swap.** `cred.cpp:26`'s
    `fill_random()` wraps `getrandom(buf, n, 0)` (`<sys/random.h>`) and is
    the sole source of security-grade randomness in the codebase — Argon2id
    password-hash salts and `cred_random_hex()` (`ACL GENPASS`) both go
    through it; `rand_idx()`/mt19937 deliberately stays non-crypto for hot
    paths and is out of scope here. Nothing in MinGW's POSIX-compatibility
    headers shims this one. The Windows-native replacement is
    `BCryptGenRandom()` from CNG (`<bcrypt.h>`, links `bcrypt.lib`), called
    with the `BCRYPT_USE_SYSTEM_PREFERRED_RNG` flag so no algorithm-provider
    handle needs opening/closing around it — a different API family
    entirely, not a renamed function. Get this one reviewed, not just
    compiled: a fallback to a non-CSPRNG source here would silently
    downgrade every password hash and generated credential on Windows only,
    while the Linux build stayed correct.
15. **Dependency provisioning has no Windows equivalent to
    `apt install libargon2-dev`, and blocks the build before any port code
    even runs.** `CMakeLists.txt:18` (`find_package(ZLIB)`), `:23`
    (`find_package(Threads REQUIRED)`) and `:29` (`find_package(OpenSSL)`)
    all assume dev packages a Linux package manager already provides. ZLIB
    and OpenSSL both have maintained vcpkg ports and real CMake config
    packages once installed through vcpkg's toolchain file
    (`-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake`) —
    the closest thing to a 1:1 swap anywhere in this section. libargon2 does
    not: `CMakeLists.txt:43-44`'s `find_path(ARGON2_INCLUDE_DIR argon2.h)` /
    `find_library(ARGON2_LIBRARY argon2)` pair exists in the first place
    because upstream libargon2 ships no CMake config even on Linux, and it
    has no vcpkg port either. A Windows build either hand-builds libargon2
    (its upstream repo does build under MSVC) and points
    `-DARGON2_INCLUDE_DIR=`/`-DARGON2_LIBRARY=` at the result, or ships the
    first port with `-DMYRED_ARGON2=OFF` (already a supported flag,
    documented SHA-256 fallback) and promotes Argon2id in a follow-up.
    `find_package(Threads REQUIRED)` becomes unnecessary the moment the
    `std::thread` swap from Possibilities above lands — nothing
    pthread-shaped is left to find, so the Windows CMake path can drop the
    requirement rather than satisfy it.

### POSIX → Windows translation table

| Used for | POSIX call (file:line) | Windows equivalent | 1:1 swap? |
|---|---|---|---|
| bgsave / AOF rewrite child | `fork()` (`rdb.cpp:939`, `aof.cpp:133`) | *(none — needs redesign, see Difficulty 1)* | No |
| reap bgsave/AOF child | `waitpid`/`WIFEXITED`/`WEXITSTATUS` (`rdb.cpp:965,974`; `aof.cpp:151,206,216`) | *(removed along with `fork()`)* | No |
| thread pool | `pthread_create/join/mutex_*/cond_*` (`thread_pool.cpp`/`.h`; `server.cpp` `g_loop_mu`) | `std::thread`/`std::mutex`/`std::condition_variable` (recommended — see Possibilities) | Yes, via stdlib |
| socket read/write/close | `read`/`write`/`close` on a socket fd (`client.cpp:19,49,51,93`; `server.cpp:207,214,572,584,645,649,655`) | `recv`/`send`/`closesocket` | Rename + scattered call sites |
| cross-thread loop wakeup | `eventfd()` (`server.cpp:21,57,117-129,1243`) | loopback socket pair, or `WSAEventSelect` + `WSAPoll` | No — needs its own design |
| non-blocking mode | `fcntl(fd, F_SETFL, O_NONBLOCK)` (`server.cpp:95,98`) | `ioctlsocket(fd, FIONBIO, &mode)` | Different function |
| "would block" / interrupted | `errno == EAGAIN || EINTR` (`transport.cpp:138,168`; `server.cpp:556,760,1279`) | `WSAGetLastError() == WSAEWOULDBLOCK`; no `EINTR` equivalent | No — different error space |
| durability sync | `fsync`/`fdatasync` (`state.cpp:848`; `rdb.cpp:387,918`; `aof.cpp:110,169`; `server.cpp:785,1328,1342`) | `FlushFileBuffers(HANDLE)` via `_get_osfhandle(fd)` | Needs a `HANDLE` conversion step |
| monotonic + wall clock | `clock_gettime(CLOCK_MONOTONIC / CLOCK_REALTIME)` (`state.cpp:1028-1039`, 44 call sites) | `QueryPerformanceCounter`/`GetTickCount64` (monotonic); `GetSystemTimePreciseAsFileTime` + epoch offset (wall) | No — different API per clock, see Difficulty 7 |
| process memory (RSS) for `INFO memory` | `fopen("/proc/self/status")` + parse `VmRSS:` (`commands.cpp:762-782`) | `GetProcessMemoryInfo()` (`<psapi.h>`, `WorkingSetSize`) | No — no Windows filesystem equivalent, see Difficulty 8 |
| socket subsystem lifecycle | *(none — nothing to translate)* | `WSAStartup`/`WSACleanup` | New code, not a translation |
| ignore SIGPIPE / SIGXFSZ | `signal(SIGPIPE, SIG_IGN)`, `signal(SIGXFSZ, SIG_IGN)` (`server.cpp:1076,1077`) | *(delete — neither condition exists on Windows)* | N/A |
| graceful shutdown signal | `signal(SIGTERM, ...)` (`server.cpp:1075`) | CRT `SIGTERM` exists but nothing external delivers it; needs `SetConsoleCtrlHandler` or a service callback | No — different delivery mechanism |
| event loop | `poll()`/`struct pollfd`/`POLLIN`/`POLLOUT`/`POLLERR` (`server.cpp:1247-1321`) | `WSAPoll()` — same struct, same flags | Yes |
| address parsing | `inet_pton`/`sockaddr_in` (`client.cpp:145,148`; `state.cpp:994`; `server.cpp:203,206,551,641,644`) | Identical via `<ws2tcpip.h>` (include order matters) | Yes |
| socket tuning | `setsockopt(IPPROTO_TCP, TCP_NODELAY)` / `(SOL_SOCKET, SO_REUSEADDR)` (`server.cpp:210,638,639`) | Same constants; `SO_REUSEADDR` semantics differ | Compiles, behavior needs testing |
| file-creation permissions | `open(..., O_CREAT, 0644/0640)` (`aof.cpp:101,180`; `rdb.cpp:902`; `server.cpp:1162`; `commands.cpp:1214`) | CRT `_open` `pmode` (`_S_IREAD`/`_S_IWRITE` only) | Compiles, permission semantics silently narrower |
| temp-file uniquifier | `getpid()` (`rdb.cpp:357,900`) | `_getpid()` (`<process.h>`) or `GetCurrentProcessId()` | Yes |
| existence/removal checks | `stat`/`access`/`unlink` (`rdb.cpp:332,876,884,912,918`; `aof.cpp:107,110,195`; `server.cpp:1140,1141,1167`; `state.cpp:850`) | `_stat`/`_access`/`_unlink` (`<sys/stat.h>`/`<io.h>`), or unmodified under MinGW | Yes |
| atomic file replace | `rename(tmp, target)` (`rdb.cpp:399,401,920`; `aof.cpp:175`; `state.cpp:854`) | `MoveFileExW(tmp, target, MOVEFILEEX_REPLACE_EXISTING \| MOVEFILEEX_WRITE_THROUGH)` | **No — CRT `rename()` fails on an existing destination, see Difficulty 13** |
| CSPRNG for credential material | `getrandom()` (`cred.cpp:26`, via `fill_random`/`cred_random_hex`) | `BCryptGenRandom()` (`<bcrypt.h>`, `bcrypt.lib`, `BCRYPT_USE_SYSTEM_PREFERRED_RNG`) | No — different API family, see Difficulty 14 |

## Event Loop and Connection Scaling

Current shape: one `poll()` loop that rebuilds `poll_args` from the whole
`fd2conn` vector every tick, a 64 KB stack staging buffer in `handle_read` copied
into `Conn::incoming`, and no ceilings on connection count or buffer growth.
Upgrades, in dependency order:

- Per-connection limits first (correctness/DoS issues, not just scale): a
  `maxclients` directive enforced in `handle_accept` with a `-ERR max number of
  clients reached` reply, an input cap on `Conn::incoming` (a frame that legally
  declares `k_max_args` bulks of `k_max_msg` bytes can demand terabytes today),
  and Redis-style `client-output-buffer-limit` classes on `Conn::outgoing` so a
  slow reader of `KEYS`/`HGETALL` output gets disconnected instead of ballooning
  the heap.
- Read directly into the connection buffer: give `Buffer` a
  `buf_reserve(n)`/writable-tail API and `read()` straight into `data_end`,
  removing the 64 KB memcpy per read in `handle_read`.
- `epoll` backend behind a tiny interface (`event_loop_add/mod/del/wait`), keeping
  `poll()` as the portable fallback. This kills the O(connections) rebuild per tick
  and is a prerequisite for any 10k-connection claim. Design the interface so the
  future Windows `WSAPoll` port is a third backend.
- Unix domain socket support (`unixsocket` directive) — trivially fits the existing
  `listen_fds` vector and skips protected-mode/allowlist concerns for local tooling.
- Only after the above: optional io-threads (Redis 6 model). Threads only do
  read+parse and serialize+write; command execution stays on the main thread, so
  `g_data` keeps its single-writer discipline. The `thread_pool.cpp` pool is not
  reusable for this (no per-connection affinity); plan a dedicated design doc first.

## Multiple Logical Databases

`SELECT`, `SWAPDB`, `MOVE`, and `COPY ... DB` need real database indexes. Concrete
approach for the current code:

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

## Scripting (EVAL)

Largest remaining Redis-compat feature after Pub/Sub and transactions.

Decision (2026-07-14): **custom language + bytecode VM, not embedded Lua.**
Deliberately scoped as a small Redis-scripting DSL, not a general-purpose
language — no closures, coroutines, metatables, or modules. Educational track
(writing an interpreter from scratch) that fits the problem: EVAL only ever needs
values, branching, loops, calls, and one privileged builtin, and a script has no
state that outlives one invocation, so GC is not needed at all. Sits in Backlog
with no deadline pressure — exactly when this bet is reasonable.

Pipeline:

- Lexer → recursive-descent parser → AST → single-pass compiler to flat bytecode
  (stack-based VM, not tree-walking).
- Values: nil, boolean, number (int/double kept distinct for RESP fidelity),
  string, table/array (to receive multi-bulk RESP replies and build multi-bulk
  `redis.call` arguments).
- Memory: bump/arena allocator scoped to one `EVAL` invocation, freed wholesale on
  return. No cross-invocation state in Redis's EVAL model, so no GC required.
- `redis.call`/`redis.pcall` is a VM opcode that re-enters `do_request` with a
  synthetic reply buffer, translating RESP↔VM values; `redis.call` errors abort,
  `redis.pcall` catches them as a VM-level error value.
- Safety: dispatch loop checks an instruction counter each iteration against a
  configured max-instructions-per-script limit.
- `EVAL`/`EVALSHA`/`SCRIPT LOAD|EXISTS|FLUSH`: cache compiled bytecode keyed by
  SHA-1 of source (add SHA-1 next to `sha256.h`); `EVALSHA` looks up directly.
- Persistence/replication: log *effects*, not scripts. Script writes flow through
  the normal handlers (the `g_writes_since_save` gate + `aof_feed`/`aof_append_raw`
  capture the stream), but raw-frame capture must be disabled inside scripts (no
  client frame) so script-initiated writes always take the `aof_feed` re-encode
  path — mirrors the rename-command canonicalization rule.
- Atomicity is free (single-threaded loop), but the OOM and MISCONF write gates in
  `do_request` must still run per `redis.call` — the VM doesn't bypass them.

## Structured Logging and Daemonization

Everything logs via bare `fprintf(stderr, ...)` today. Before the audit log
(V9.5.4) grows siblings:

- A leveled logger: `loglevel debug|verbose|notice|warning`, `logfile <path>`,
  timestamps, single `write()` per line.
- Fork-safety rule stays: children (`rdb_write_snapshot`, `aof_write_snapshot`)
  only use `write()` on an already-open fd — the logger API must expose that path.
- `daemonize yes` + `pidfile`; optional syslog. Makes protected mode, audit
  events, and `MISCONF` states operationally visible instead of lost on a detached
  stderr.

## Eviction Batch-Exhaustion False OOM

Low priority — park until the performance/polish pass. `free_memory_if_needed`
caps itself at 100 eviction attempts per call (a correct stall guard), but the
final `return g_data.used_memory <= g_config.maxmemory;` can't distinguish "ran
out of batch budget while genuinely still evicting" from "policy can't free
anything" (the latter already returns `false` at the `!victim` check). Result:
after a `CONFIG SET maxmemory` shrink under a large dataset, every write gets a
spurious OOM until enough separate calls have each chipped off 100 keys.

Decision (2026-07-17): mirror Redis — treat "batch exhausted but still making
progress" as success. Since the only way to reach the final `return` is either (a)
genuinely under budget, or (b) attempts exhausted while `victim` was never null,
`return true;` unconditionally there is the fix; the `!victim` early-return is
untouched, so real OOM still rejects. Reverting to the strict comparison is the
backpressure alternative.

Tradeoffs to accept consciously:
- ~~No cron/timer-driven eviction sweep~~ — resolved 2026-07-17: `evict_tick()`
  runs a bounded batch per tick while `g_evict_pending` is armed, and
  `next_timer_ms()` returns 0 while pending so an idle server keeps draining
  (verified by `scripts/test_evict_tick.sh`: 50k→5.3k keys in <1s idle).
- `used_memory` can transiently overshoot `maxmemory` more than today, since a
  write may land on an already-over-budget state instead of being rejected. The
  intended availability-over-strict-ceiling tradeoff, not a bug.

## Upgrade Catalog (pick-your-adventure)

Everything above is scoped, dependency-ordered backlog. This section is different
on purpose: a browsable menu, not a queue — pick by mood/curiosity, not priority.
Nothing here is scheduled; picking one just means promoting it into a real
milestone above with its own design pass. Grouped by what kind of itch it
scratches.

### Production-Grade (matters most if this ever serves real traffic)

- **Backup verification sidecar** — a small tool that periodically loads the
  latest RDB/AOF into a throwaway process (`--check-aof`, plus an equivalent RDB
  dry-run load) and alarms on failure. Cheap insurance: a backup nobody has ever
  successfully restored isn't a backup.
- **Crash-only / fsync-ordering audit** — formally walk every write path
  (`aof_feed` → buffer → `write()` → `fdatasync`) and state the exact durability
  window on an unclean shutdown at each step. Turns "we think this is safe" into
  a checked invariant, cross-referenced against `appendfsync` policy.
- **Key namespacing / multi-tenancy** — prefix-scoped views so one server can
  safely host multiple logical tenants without full `SELECT ... DB` isolation
  overhead. Real production feature; also a good ACL-model stress test.
- Replication (V10) and RESP3/`HELLO` (Observability section above) already
  cover the two biggest production gaps — start there if this category is the pick.

### Low-Level Systems Programming (the "learn something hard" track)

- **io_uring event loop** — a step past the already-planned `epoll` backend
  (Event Loop section): submission/completion queues, batched syscalls, optional
  zero-copy send/recv. The real prize is learning async I/O that isn't
  readiness-based like `poll`/`epoll` — a genuinely different mental model.
- **Custom slab/arena allocator** — replace per-`Entry`/`HNode` `new`/`malloc`
  with a size-class slab allocator (jemalloc's core idea, hand-rolled). Distinct
  from the jemalloc-*linking* item under Memory and Encoding Optimizations —
  this is building the allocator, not adopting one.
- **Lock-free completion queue** — `g_loop_jobs`/`g_loop_mu` (the cross-thread
  channel worker threads use to post results back to the main loop) is a small,
  contained, single-producer-friendly spot to try a lock-free MPSC ring buffer
  without threatening the single-writer discipline everywhere else in `g_data`.
- ~~**SIMD RESP parsing**~~ — **checked and dropped 2026-08-07.** The premise was
  wrong: `parse_resp_request`'s byte-at-a-time scans (resp.cpp:46, 68) only walk
  the *digits of a length prefix* — 1–7 bytes — and the bulk payload is never
  scanned at all, since `str_len` indexes straight past it. There is no MB-scale
  scan to vectorize. The only unbounded scan is the inline-command path
  (resp.cpp:19), which is a `redis-cli --pipe` compatibility path, not a hot one.
  The simdjson trick needs a parser that scans its whole input; RESP is
  length-prefixed precisely so it doesn't have to. See Hand-Tuned Hot Paths for
  the target that *did* survive measurement.
- **Write your own RDB compressor** — a small LZ77/LZ4-style codec replacing the
  `zlib` dependency. Real compression-algorithm learning with a natural
  correctness check (round-trip against every existing RDB test fixture).
- **Build HyperLogLog from scratch** — the "New data types" gap already lists
  `PF*` as missing; doing it yourself (dense/sparse representation, the
  bias-corrected cardinality estimator) is the low-level-learning angle on that
  same gap rather than a new item.
- Cross-reference: Hand-Tuned Hot Paths and the EVAL bytecode VM (above) are
  already-scoped entries in this same spirit.

### Totally Different Domain (not low-level at all)

- **Web admin dashboard** — a small HTTP server (new, separate from RESP) plus a
  browser UI: live `INFO` stats, a keyspace browser, slow-command view. Frontend
  + HTTP design, nothing to do with the event loop internals.
- **REST/HTTP gateway in front of RESP** — `GET /keys/:key` translating to a real
  `GET`, etc. A protocol-translation exercise, not a performance one.
- **Prometheus exporter + Grafana dashboard** — scrape `INFO`-equivalent metrics
  over HTTP. Standard ops tooling, good pairing with the Structured Logging item.
- **Docker image + Kubernetes manifests/Helm chart** — packaging and deployment
  ergonomics; zero C++ required, entirely different skill.
- **Terminal dashboard (`htop`-style, ncurses/notcurses)** — live-updating view of
  connected clients, ops/sec, memory. A fun middle ground: some low-level
  terminal-handling, but the actual work is UI/UX.
- **WASM build via Emscripten** — compile the core to run client-side in a
  browser playground. Mostly a build-portability exercise (no `fork()`, no raw
  sockets), surprisingly different from anything else on this list.
- **Client library in Python or JS** — hand-write a minimal RESP client from the
  wire protocol up. Good way to see the protocol from the *other* side.

### Interesting Middle Ground (novel tooling, not pure feature work)

- **AOF time-travel debugger** — step an AOF file command-by-command in a CLI,
  showing a diff of affected keys at each step. Built entirely on `aof_load`'s
  existing replay path, just observed instead of applied silently.
- **Model-based testing** — drive random operations through MYRED and a trivial
  Python reference model (a `dict` + a sorted TTL list) in lockstep, diffing
  state after every op. Different technique from the differential-against-real-Redis
  and libFuzzer items already listed under V11 — this checks internal
  consistency, not Redis-compatibility.
- **A C++ test layer — new tests, not a rewrite of the existing suite.** The
  654+ Python assertions in `scripts/` stay exactly as they are: they exercise
  MYRED the way a real client does, over a real socket, speaking real RESP —
  that's a feature of black-box testing, not a limitation to fix, and rewriting
  working, battle-tested assertions into C++ for no functional gain would be
  pure churn. What C++ actually buys, that Python structurally can't:
  - **Unit tests for internal pure functions**, which today have zero direct
    coverage — every one of them is only ever exercised indirectly through a
    full command dispatch over a socket. `glob_match` (commands.cpp),
    `parse_notify_flags`/`notify_flags_string` (state.cpp), and
    `rdb_load_buffer` (rdb.cpp) are natural first candidates: small, pure,
    already isolated enough to link against directly from a `.cpp` test file
    with no server/event-loop bootstrapping required.
    - **`crc32_compute` is now the concrete first one** — Hand-Tuned Hot Paths
      needs exactly this file as its Stage 0, with a real oracle to diff
      against, so that entry and this one are the same first step.
  - **This is also just V11's fuzzing work, one step earlier.** V11 already
    plans libFuzzer/AFL harnesses for `parse_resp_request` and
    `rdb_load_buffer` — those *are* C++ test code, and are the natural first
    real example of this rather than a separate effort. Building a plain unit
    test for a function is most of the work of building a fuzz harness for the
    same function; doing the unit test first de-risks the fuzz target.
  - **A real load-generator client**, separate purpose from the unit tests
    above: the benchmark-methodology lesson already recorded in ROADMAP's
    Testing Matrix (Python stress throughput is client-bound and unusable for
    transport comparisons — proven via the pub/sub-inversion argument, not
    asserted) is a Python *interpreter-speed* ceiling, not something fixable
    in Python at all. A small C++ client using the same raw-socket RESP
    encoding `redis-benchmark` uses would let concurrency/throughput testing
    actually saturate the server instead of measuring the test harness.
  - Keep the two purposes separate: correctness/unit coverage of internal
    functions is not the same project as a throughput-capable load generator,
    even though both happen to require C++. Don't let "convert tests to C++"
    become one undifferentiated pile.
- **Chaos harness** — inject random `SIGKILL` mid-fsync, simulated disk-full
  (`ENOSPC` via a small `LD_PRELOAD` shim), or latency/partition on the loopback
  interface (`tc netem`), then assert the AOF/RDB recovery guarantees actually
  hold. Reliability-engineering, not systems-programming.
- **"Explain mode"** — an opt-in verbose trace per command (hash computed,
  bucket probed, rehash triggered, TTL heap touched) for teaching/debugging.
  Pure observability feature, no perf ambition.
- **Module/plugin system** — `dlopen`-based third-party command registration,
  Redis-Modules-API-flavored. Sits between low-level (C ABI design, symbol
  versioning) and ecosystem-building (it's what lets other people extend MYRED
  without forking it).
