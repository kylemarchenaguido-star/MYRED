# MYRED Backlog

Everything not yet started: open bugs, future milestones, deferred optimizations,
and feature gaps. See `ROADMAP.md` for current/completed work and `DECISIONS.md`
for design rationale.

## Open Bugs / Correctness Follow-ups
- 🔴 **`SPOP`'s synthetic `SREM` frame carries an empty key** — every popped
  member resurrects on restart. `lookup_entry()` takes `std::string &keystr` and
  **swaps it out** (`key.key.swap(keystr)`, commands.cpp), so `do_spop` reading
  `cmd[1]` *after* its own lookup builds `SREM "" <member>`, which replays as a
  no-op. Live since V9.6.4 (2026-07-16), when `CmdSpec::aof_self` + the
  synthetic frame landed. Fix: `synth = { "srem", ent->key }` — the entry's own
  key, which is where `lookup_entry` swapped the string *to*; no copy on the
  non-feed path and it cannot drift from the entry being modified.
  - **Found 2026-08-03 by watching the V10.2a replication stream**, not by any
    suite: `test_restart_matrix.py` covers `GETEX`/`GETDEL`/`ZPOPMIN`/eviction
    `DEL`/renamed-command frames but never `SPOP`, and `stress_test.py` does not
    restart. Add a `SPOP`-then-restart case when next touching that suite.
  - Generalisable: a handler must not read `cmd[N]` after passing it to
    `lookup_entry`. A scan of every `do_*` found this as the only instance —
    worth re-running that check after any handler that builds its own AOF frame.

- 🟡 **`ACL GENPASS` generates passwords from a non-cryptographic PRNG.**
  **Scheduled: after V10 ships.** `rand_idx()` (`common.h`) is
  `std::mt19937_64` seeded once from `std::random_device`, and `do_acl`'s
  `genpass` branch builds its hex string with `hx[rand_idx(16)]`. Mersenne
  Twister is fully reconstructible from its output — 624 observed 32-bit words
  recover the internal state, and every past *and* future draw with it. So an
  attacker who obtains any one `ACL GENPASS` result (a password handed out in a
  chat log, a ticket, a shell history) can in principle derive every other
  password the same process ever generated, including ones already set on other
  users. This is a *generator* weakness, not a storage one: Argon2id still
  protects the stored hash, so the exposure is confined to secrets minted by
  this command.
  - Fix shape: read from a real CSPRNG for credential material specifically —
    `getrandom(2)` (glibc 2.25+, no fd to manage, `GRND_NONBLOCK` plus a
    `/dev/urandom` fallback), used *only* by `genpass`. Do **not** repoint
    `rand_idx()` itself: eviction sampling, `RANDOMKEY`, `SPOP` and
    `hm_random` call it on hot paths and want a fast PRNG, not syscall-backed
    entropy. Two generators with clearly separated jobs, one of them named for
    the job (e.g. `secure_random_bytes()`).
  - Deliberately **not** in scope: `g_data.repl_id` (V10.1) keeps using
    `rand_idx()`. A replication ID is a public identifier published in `INFO`,
    not a secret — predicting it grants nothing.
  - Note when fixing: this is the same "a verifier is not a display value"
    family as the `requirepass` masking decision in Open Decisions — worth
    re-reading that entry first, since the two share a threat model.

- ⚪ **Boot-time metadata cross-check for `k_cmd_table`** (follow-up, not a bug). V8.4's `discard`
  outage came from a duplicated `{"multi", …}` key in the `k_cmd_table`
  initializer list — `unordered_map` insert semantics drop the duplicate silently,
  no warning at `-Wall`. The `ks`/`extra` maps already carried `discard` entries
  with no command to attach to, so a reverse check at the end of
  `acl_init_categories()` — every key in `ks`/`extra`/`notify_cls`/`resolvers`
  must exist in `k_cmd_table` — would have caught it at boot. Same idea covers
  the four parallel ACL-category lists (DECISIONS → ACL Category Tagging).

**No open bugs in the data path.** Of the two items above, the `k_cmd_table`
cross-check is a hardening follow-up rather than a defect — V8.8 shipped the
equivalent check for config directives and it caught six real violations on its
first build, so the same idea applied to `k_cmd_table` is still worth doing. The
`ACL GENPASS` entry is a genuine weakness but a contained one, deferred by
decision (2026-08-03) until V10 ships rather than left unnoticed.

Every bug previously tracked here is FIXED; full root-cause
writeups live in `CODE_REVIEW.md` → Resolved Bugs Archive and in git history. New
bugs get filed here first, then folded into the CODE_REVIEW audit.

Recently resolved (terse; detail in CODE_REVIEW / git):
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
bugs above are now **V8.8**, the active milestone in ROADMAP → Current Focus.

### V10 - Replication and High Availability → moved to `ROADMAP.md`

**V10 is the active milestone and all of it now lives in `ROADMAP.md` → Current
Focus**: the preamble (why this codebase is already set up for replication, and
why the AOF stream *is* the replication stream), completed **V10.1**, active
**V10.2**, and the remaining steps V10.3–V10.6. Nothing V10 is left here.

### V11 - Testing Hardening: Differential, Fuzz, and Adversarial Security (post-1.0)

Gate: do not start until V10 (Replication and High Availability) ships. This is
explicitly a "first real version is done" milestone. Scheduling choice, not a
hard technical dependency — nothing below actually needs replication to exist;
auth, ACL, TLS, and transactions are the real attack surface this is aimed at.
Bundled into one milestone number the same way Pub/Sub and Transactions shared
V8 — for scheduling convenience, not because the pieces depend on each other.

Structured, not "point an agent at the live server and see what happens" — that
finds less than the combination below, because an LLM-driven agent is strong at
reasoning about logic bugs and weak at raw byte-level crash-finding compared to
a real fuzzer.

- **Differential harness**: drive the same randomized operation stream through
  redis-py against both a real `redis-server` and MYRED, diff replies, with a
  normalization table for deliberate divergences (e.g. the V9.5.1 ACL tagging
  rule). Catches semantics drift of the "SET should discard TTL" class that
  hand-written assertions miss.
- **libFuzzer/AFL harnesses** for `parse_resp_request` and `rdb_load_buffer` —
  both pure functions over byte buffers, so harnesses are ~20 lines each. Corpus
  seeds: real AOF/RDB files from the test scripts. Extend the same harness
  toward adversarial protocol fuzzing specifically (malformed bulk lengths,
  negative sizes, truncated frames) rather than building a second one.
- **ASan/UBSan CMake build type** (`-fsanitize=address,undefined`) and a CI lane
  that runs `stress_test.py --correctness-only` under it. The `container_of`
  pattern and manual `Buffer` management are exactly where sanitizers pay off —
  and it's the same build anything found during the adversarial pass below
  should be reproduced under, for a real stack trace instead of "the server died."
- **Static/code-level security review** — no live server needed. Auth, ACL
  logic, RESP parsing bounds, the TLS handshake state machine, and AOF/RDB
  loading from untrusted files: bounds issues, integer overflow on size fields,
  logic bypasses.
- **Targeted logic-level attacks** — the part an agent is actually good at:
  hypothesize specific abuse cases (case-aliasing around ACL deny, subscribe-mode
  gate bypass, key names containing RESP control bytes, TLS handshake state
  confusion, races around the `fork()`-based BGSAVE) and write concrete Python
  repro scripts against the real server for each one.
- **One running document** (e.g. `docs/SECURITY_TESTING.md`) logging every
  attempt, outcome, and repro steps — same evidence-preservation habit as the
  rest of the test suite.
- Run the adversarial/live-server pieces against a disposable local instance
  only, never anything that matters if it crashes or hangs.

## Deferred TLS Optimizations (V9.7.5 tail)

The body of V9.7.5 shipped (see ROADMAP → V9.7). These three are intentionally
NOT done — each is gated on a measured need, not implemented speculatively.
Escalate only when a metric demands it.

- **Handshake CPU under an accept storm** — escalate in this exact order, and
  re-measure accept-to-first-command latency under a connection burst after each
  step before moving to the next:
  1. Session resumption (done, V9.7.5) — already reduces how many *full* handshakes occur.
  2. Cap accepts per poll tick: change the unbounded
     `while (handle_accept(listeners[i].fd, listeners[i].is_tls) == 0) {}` to a
     bounded loop (e.g. `k_max_accepts_per_tick`) so one connection burst can't
     monopolize a tick and starve already-established connections' read/write
     readiness. Cheapest, and helps plaintext too.
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

- Full Redis ACL rule-order fidelity ("last match wins") — upgrade path recorded
  in DECISIONS → ACL Model.
- Pub/Sub channel-pattern enforcement (lands in V8.2 → ROADMAP Current Focus;
  needs a new `User::channel_patterns` field).
- `nopass`, selectors, `sanitize-payload`, `ACL LOAD`, `ACL SAVE`.
- `COMMAND`, `COMMAND DOCS`, `COMMAND COUNT` (`redis-cli` interactive mode probes these).
- Full `CONFIG GET/SET` coverage (also under Server Observability and Tooling).

## Command Coverage Gaps

Sorted sets: `ZINCRBY`, `ZCARD`, `ZCOUNT`, `ZMSCORE`, `ZPOPMAX`, `ZRANGEBYSCORE`,
`ZRANGEBYLEX`, `ZREVRANGE`, `ZREMRANGEBYRANK`, `ZREMRANGEBYSCORE`,
`ZREMRANGEBYLEX`, `ZUNIONSTORE`, `ZINTERSTORE`, `ZDIFFSTORE`, `ZRANDMEMBER`,
`ZSCAN`, `ZLEXCOUNT`, `ZRANGESTORE`, `ZMPOP`.

Strings and bitmaps: `SETBIT`, `GETBIT`, `BITCOUNT`, `BITPOS`, `BITOP`,
`BITFIELD`, `SUBSTR`, `LCS`.

Generic: `COPY`, `SORT`, `SORT_RO`, `DUMP`, `RESTORE`, `EXPIRETIME`,
`PEXPIRETIME`, `OBJECT HELP`, `SCAN ... TYPE`, `WAIT`.

Hashes: `HRANDFIELD`, `HINCRBYFLOAT`.

Lists: `LPOS`, `LMOVE`, `RPOPLPUSH`, `LMPOP`, `BLPOP`, `BRPOP`, `BLMOVE`.

Sets: `SINTERCARD`.

New data types: HyperLogLog (`PF*`), Streams (`X*`), Geo (`GEO*`), Bitmaps as a
first-class area.

## Server Observability and Tooling

- `CLIENT LIST`, `CLIENT KILL`, `CLIENT SETNAME`, `CLIENT GETNAME`, `CLIENT ID`.
- `HELLO` and RESP3 handshake.
- `RESET`, `SLOWLOG`, `LATENCY`, `MONITOR`, `DEBUG`, `SHUTDOWN`, `LASTSAVE`, `TIME`.
- Full `CONFIG GET/SET` surface.

## Platform Work

- Portable background snapshot design without `fork()`.
- Windows socket layer using `WSAPoll`; `WSAStartup`/`WSACleanup`.
- `FlushFileBuffers` replacement for `fdatasync`.
- Path handling and config path portability.

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
