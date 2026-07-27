# MYRED Backlog

Everything not yet started: open bugs, future milestones, deferred optimizations,
and feature gaps. See `ROADMAP.md` for current/completed work and `DECISIONS.md`
for design rationale.

## Open Bugs / Correctness Follow-ups

- 🟠 **`ACL SETUSER` is not atomic** (found 2026-07-26 while adding `@transaction`).
  `do_acl` binds `User &u = g_config.users[cmd[2]]` and applies modifiers straight
  onto the live user, returning early on the first bad one — so a rejected rule
  leaves the user half-configured, and creates it if absent even when the command
  fails outright. Observed: `acl setuser tx on '>txpass' '-@all' '+@transaction' '~*'`
  failed on the unparsed `+@transaction` and left `user tx on #<hash> -@all` with
  `~*` never applied. That case failed *closed*, but ordering decides:
  `acl setuser u on '>p' '+@all' '~bad['` leaves `u` enabled with `+@all` and no key
  restriction. Fix: stage onto a `User` copy, run every modifier, and commit with
  `g_config.users[cmd[2]] = std::move(staged)` only on full success — assigning
  through `operator[]` on an existing key overwrites the node's value in place, so
  live `Conn::user` pointers stay valid (the `// stable address` invariant holds).
  Redis stages the same way.
- 🟡 **`CONFIG GET` covers 3 parameters, `CONFIG SET` covers all of them.** `do_config`'s
  get branch hand-rolls `maxmemory`, `maxmemory-policy`, `notify-keyspace-events`
  and returns an empty array for anything else, while set routes through
  `config_apply` and accepts the full directive set. So `CONFIG GET appendfilename`
  (or `appendonly`, `appendfsync`, `dir`, `port`, `save`, `tls-*`…) silently
  answers `[]`. Second time this has cost a verification cycle — `notify-keyspace-events`
  had to be added the same way during V8.3 (2026-07-25), and V8.5's AOF durability
  check failed on `appendfilename` (2026-07-26). Fix properly rather than adding a
  fourth special case: drive both directions off one directive table so get/set
  cannot drift.
- ⚪ **Boot-time metadata cross-check** (follow-up, not a bug). V8.4's `discard`
  outage came from a duplicated `{"multi", …}` key in the `k_cmd_table`
  initializer list — `unordered_map` insert semantics drop the duplicate silently,
  no warning at `-Wall`. The `ks`/`extra` maps already carried `discard` entries
  with no command to attach to, so a reverse check at the end of
  `acl_init_categories()` — every key in `ks`/`extra`/`notify_cls`/`resolvers`
  must exist in `k_cmd_table` — would have caught it at boot. Same idea covers
  the four parallel ACL-category lists (DECISIONS → ACL Category Tagging).

Other than the above, every bug previously tracked here is FIXED; full root-cause
writeups live in `CODE_REVIEW.md` → Resolved Bugs Archive and in git history. New
bugs get filed here first, then folded into the CODE_REVIEW audit.

Recently resolved (terse; detail in CODE_REVIEW / git):
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

## Next Major Milestones

### V8 - Transactions → moved to `ROADMAP.md`

Transactions are the **active milestone** — full scope lives in `ROADMAP.md` →
Current Focus. `MULTI`/`DISCARD` + queueing (V8.4) shipped 2026-07-26; `EXEC` is
V8.5, `WATCH` is V8.6–V8.7. Pub/Sub (V8.1–V8.3) shipped 2026-07-25 and is in
ROADMAP → Completed Milestones.

### V10 - Replication and High Availability

Planned: master-replica mode, `PSYNC`, replication backlog, partial resync,
replica propagation for writes/evictions/expirations, sentinel-style failover,
cluster mode or hash-slot sharding.

- Dependency: AOF canonicalization for renamed commands should land before
  replication, because replication must propagate canonical command intent, not
  client aliases.

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

Purely opportunistic/educational track — not on the critical path of any active
milestone.

- Candidate: `str_hash` (`common.h`, FNV-1a) — called on essentially every keyed
  command. Gate any work here on profiling first (`perf record`/`perf report`
  against a `redis-benchmark` run) — don't assume it's hot without measuring.
- FNV-1a's byte-at-a-time serial dependency chain means a line-for-line asm port
  of the same algorithm won't beat `-O2`'s output. A real win needs a different
  algorithm alongside the low-level rewrite — e.g. hardware CRC32 via
  `_mm_crc32_u64`, or xxHash — not "asm-ify FNV as written."
- Preference order if pursued: compiler intrinsics (`<immintrin.h>`, `__builtin_*`)
  first — compiler still owns register allocation/ABI; a standalone `.s`
  translation unit (own CMake `enable_language(ASM)` target, `extern "C"` linkage)
  only if intrinsics can't express what's needed; inline `asm volatile` inside a
  `.cpp` last, since it's hardest to keep clobber-list correct and ties the code to
  one compiler's dialect.

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
- **SIMD RESP parsing** — vectorized `\r\n` scanning in `parse_resp_request`
  (the simdjson trick: compare 16/32 bytes at once, build a bitmask of matches)
  instead of a byte-at-a-time scan. Profile first, same rule as `str_hash`.
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
  and libFuzzer items already listed under Differential and Fuzz Testing —
  this checks internal consistency, not Redis-compatibility.
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
