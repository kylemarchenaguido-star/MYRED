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

Date: 2026-08-26. **V4 through V11 are done.** V11 (testing hardening) closed
2026-08-22 with the suite at **1458/1458 on Release and under
ASan+UBSan+LSan**. The active milestone is **V13 — Production-Pointable**,
promoted ahead of V12 (cluster): the question is now whether an unmodified real
application can be pointed at this server and just work.

Primary commands:
```bash
cmake -B build && cmake --build build          # build/ is a DEBUG build
cmake -B build-rel -DCMAKE_BUILD_TYPE=Release && cmake --build build-rel -j
./build/server myred.conf

# the whole suite, no server needed first — it starts its own (V11 Step 0)
python3 scripts/stress_test.py --server build-rel/server --destructive --bench
cmake -B build-asan -DCMAKE_BUILD_TYPE=Debug -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1"
python3 scripts/stress_test.py --server build-asan/server   # same suite, sanitizer output asserted
python3 scripts/stress_test.py --server build-rel/server --tls
python3 scripts/stress_test.py --list-phases

# against a server you started yourself
python3 scripts/stress_test.py --password <pass>
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <pass>   # over TLS
python3 scripts/test_tls.py --server build-rel/server          # TLS metrics (measurement, not pass/fail)
```

Results land in `docs/logs/<WSL|Native>/` — the environment is read from the
kernel, and the split exists so a VM number is never mistaken for a bare-metal
one. `--compare A.json B.json` diffs two machines.

**⚠ `scripts/stress_test.py` is tracked on the `test` branch only.** On `main`
it is gitignored, so it exists as an untracked local file and a `main` checkout
does not create it — checking out `main` from a branch that has it will *delete
it from the working tree*, which is exactly what happened on 2026-08-26. It is
not lost; restore the working copy with:

```bash
git show test:scripts/stress_test.py > scripts/stress_test.py
```

A fresh clone of `main` has no suite at all until that is run.

**Branch policy (2026-08-26): `main` is code only; `test` carries everything
local.** The direction matters — `.gitignore` does not apply to already-tracked
files, so **merging `test` into `main` would pull every test artifact onto main
and start tracking it**. Only `main` → `test` merges; move code the other way
with `git checkout test -- <paths>`.

**`build/` is Debug and `build-rel/` is Release** (corrected 2026-08-14 — this
file previously claimed the opposite). Never benchmark `build/`: `commands.cpp`
runs `mem_selfcheck()` after every command when `NDEBUG` is unset and it walks
the keyspace, so a Debug binary is O(keyspace) per command. `test_tls.py`
refuses to measure one.

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
| **TLS** | **Implemented (V9.7)** — live cert rotation without restart (V10.6.1c) |
| **Pub/Sub (+ patterns, channel ACL, keyspace notifications)** | **Implemented (V8)** |
| **Transactions** | **Implemented (V8.4–V8.7)** |
| **Replication + coordinated failover** | **Implemented (V10.1–V10.6d)** — V10.6e (automatic election) unscoped, cluster → V12 |

Do not rely on old test-count claims; run the harness for the current count.

## Current Focus

### V13 - Production-Pointable: closing the app-compatibility gap [ACTIVE 2026-08-22]

**Promoted from `BACKLOG.md` on 2026-08-22**, when V11 closed. It runs **ahead
of V12 (cluster)**, a deliberate reordering: cluster multiplies the state space,
and pointing one real application at a single node is both cheaper and a better
test of whether any of this is usable.

A different question than V11. V11 proved the server that exists today does not
lie, crash, or leak under adversarial input. **V13 asks whether an unmodified
real application — one already written against real Redis, using a real client
library — can be pointed at MYRED and just work for its actual command mix.**

Scope boundary: this is **not** "reach full Redis parity." It is the practical
minimum for a real application to run against MYRED without modification.

**Target shape, chosen 2026-08-26: a heavy web application** — cache + sessions
+ pub/sub, with a ranked structure (leaderboard or sliding-window rate limiter)
and ideally a replica behind the primary. That mix was picked because it reaches
the parts of MYRED that only its own test suite has ever exercised, rather than
the six commands a pure cache would touch.

---

### Measured command surface (2026-08-26) — supersedes the guesses below

Read out of `k_cmd_table` in `commands.cpp`, not taken from a list. **113
commands implemented.** The backlog's gap list was wrong in one way that
matters, and right about the rest.

**🔴 The zset is implemented but unreachable from a real client.** MYRED has
`zadd`, `zrem`, `zscore`, `zrank`, `zpopmin` — and then **`zquery` /
`zrevquery`, which are not Redis commands at all**. Every range query over the
AVL zset exists, works, and is invisible to `redis-py`, `redis-cli`, or any
other client. This is not "some zset commands are missing"; it is a working data
structure behind a private door.

**The primitives underneath are already there**, which makes the fix wiring
rather than design:

| Needed command | Primitive it sits on |
|---|---|
| `ZCARD` | `avl_cnt(root)` — `avl.h` carries `cnt`, the subtree size |
| `ZRANGE` / `ZREVRANGE` (by index) | `avl_offset(node, i)` — O(log n) rank navigation, already used by `ZRANK` |
| `ZRANGEBYSCORE` / `ZREVRANGEBYSCORE` | `zset_seekge` / `zset_seekle` + the walk `zquery` already does |
| `ZCOUNT` | two seeks, rank difference |
| `ZINCRBY` | `zscore` + `zadd` |
| `ZREMRANGEBYSCORE` / `BYRANK` | seek + repeated delete |

**Decision: replace `zquery`/`zrevquery` with the real Redis names, do not alias
them.** Standing rule — no back-compat spellings carried forward. Cost is 25
references in `stress_test.py`, which is a local file and cheap to update.

**Missing and relevant to a client connecting at all:** `SELECT`, `CLIENT`,
`HELLO`, `COMMAND`. Note `AUTH` is *not* in `k_cmd_table` either but is
intercepted in `do_request` before dispatch, so it works — and `+auth` is
correctly rejected by ACL rule validation as a result. `QUIT` and `RESET` are in
the same position minus the interception, so they currently answer
`-ERR unknown command`; `redis-py` closes the socket rather than sending `QUIT`,
so this is probably harmless. Verify, do not assume.

**Missing and gating nothing in the target shape:** `EVAL`/`EVALSHA`/`SCRIPT`,
blocking list ops (`BLPOP`/`BRPOP`/`BLMOVE`), streams (`XADD` family),
geospatial (`GEO*`), HyperLogLog (`PF*`), bit operations, `SORT`, `COPY`,
`DUMP`/`RESTORE`, `LPOS`, `SLOWLOG`, `DEBUG`, `SHUTDOWN`, `LASTSAVE`, `SWAPDB`.

---

### Order of work

Judged against "a server that works in various cases" first, "deployed" second —
those two want different orders, and this follows the former.

1. **Step 1, the session/protocol stubs.** Smallest change with the broadest
   effect: without `HELLO`/`CLIENT`/`SELECT` answering *something*, a real
   client may not finish connecting at all, which gates every use case equally.
2. **Step 2, the zset surface only.** Highest value-to-effort item on the whole
   list, and the part of MYRED that is most distinctive. Gates leaderboards,
   sliding-window rate limiting, feeds and schedulers — roughly half the
   candidate applications.
3. **Step 4, deployment ergonomics.** Turns "runs in a terminal" into "runs."
   Use-case independent, so it can land any time after the above without
   changing what works.

**Deferred, with reasons: Step 3 (`EVAL`)** gates nothing in the target shape
and is the largest item on the page; **Step 5 (operability)** is a nicety once
something is actually running. Both stay scoped in `BACKLOG.md`.

Step 0 below is not a phase so much as the **instrument** steps 1-3 are verified
with.

---

### Step 0 - The empirical check, partly answered

Two of its three questions are now settled structurally, by reading the code
rather than a library's internals:

- **`CLIENT SETINFO` on connect** — MYRED returns `-ERR unknown command`
  *without closing the connection* (`do_request`'s `!found` branch). Whether a
  current `redis-py` treats that as fatal is still open and needs the real
  client.
- **`SELECT n` on a `redis://host/1` URL** — `SELECT` does not exist at all, so
  this fails today. Confirmed, not hypothesised.

**Blocked on one command**: this box has no `pip` and no `ensurepip`, so
`redis-py` cannot be installed.

```bash
sudo apt install python3-pip      # then an isolated venv, not the system python
```

The apt package `python3-redis` is 4.3.4, which **predates `CLIENT SETINFO`** —
installing it would sidestep the exact question being asked rather than answer
it. Use pip and a current 5.x/6.x.

---

### 1. Protocol/session surface — **NEEDED, and cheap**

All four are missing; all four are stubbable without touching the data path.

- **`SELECT`** — the one real decision here. Full multiple databases
  (`std::vector<HMap> dbs`, `Conn::db_index`, an RDB/AOF format bump) is scoped
  in `BACKLOG.md` under **Multiple Logical Databases** and is days of work.
  **Accepting `SELECT 0` and erroring on anything non-zero is ~10 lines** and
  unblocks every app that leaves the database at its default, which is most of
  them. Do the cheap version first and let a real failure justify the expensive
  one.
- **`HELLO`** — do not build RESP3 speculatively. Answer `HELLO` / `HELLO 2`
  with the standard reply map and error *cleanly* (not fatally) on `HELLO 3`.
- **`CLIENT`** — `SETINFO`, `SETNAME`, `GETNAME`, `ID` as stubs that reply
  instead of erroring. `CLIENT LIST`/`KILL` belong to step 5, not here.
- **`COMMAND`** — `redis-cli` calls it on connect; an empty array is usually
  enough to get past it.

### 2. Command-surface gaps — **zsets NEEDED, the rest DEFERRED**

Full lists stay in `BACKLOG.md` → **Command Coverage Gaps** and **ACL and
Command-Surface Feature Gaps**. Deliberately not duplicated here: this project
has been bitten more than once by one command list being edited while a parallel
copy was forgotten (the `k_cmd_table` duplicate-key outage, the `CONFIG`
get/set/rewrite drift). What V13 adds is priority.

- **The zset family** — see the measured-surface block above. This is the item.
- `SCAN ... TYPE`, `SORT`, `COPY`, `DUMP`/`RESTORE` — small and self-contained,
  but they gate nothing in the target shape. Do them when something asks.
- **Blocking list ops** — gate job queues only, which are explicitly out of the
  target shape (they also need `EVAL`). Not V13 work.

### 3. Scripting — **DEFERRED**

`EVAL`/`EVALSHA` is fully designed in `BACKLOG.md` → **Scripting (EVAL)**
(custom bytecode VM, not embedded Lua). It gates nothing in the target shape and
is the largest item on this page. Pull it in only if a chosen application turns
out to depend on Lua-based atomicity — Redlock-style locks, Sidekiq/BullMQ
internals, a hand-rolled `EVAL`.

### 4. Deployment ergonomics — **NEEDED; this is literally "deployment"**

Nothing here is deployable without it, and it is use-case independent — the same
fixed cost whichever application wins.

- **Daemonization + leveled logging** — scoped in `BACKLOG.md` under
  **Structured Logging and Daemonization** (`daemonize yes`, `pidfile`,
  `loglevel`/`logfile`). Today every log line is a bare `fprintf(stderr, ...)`
  and the process only runs in the foreground: fine for `stress_test.py`,
  unworkable for anything meant to stay up unattended.
- **A systemd unit file** — tracked nowhere else. `Type=notify` or
  `Type=simple` with `Restart=on-failure`, once daemonization lands. Small and
  mechanical, and it is the difference between "runs in my terminal" and "runs."
- **A Dockerfile** — currently only an Upgrade-Catalog curiosity. "Point an
  application at it" very often means "point a container at it."
- `SHUTDOWN` is worth folding in here despite living in step 5's family: it is
  trivial and operators expect it.

### 5. Minimum operability — **DEFERRED**

`CLIENT LIST` / `CLIENT KILL`, `SLOWLOG` — already in `BACKLOG.md` under
**Server Observability and Tooling**. Not a connection-time blocker, but the
first thing anyone reaches for when a real app's traffic does something
unexpected.

### Safety gate — satisfied

V13 assumed V11's differential/fuzz/static-review pass had at least a first pass
done before anything real was pointed at this server. **V11 closed 2026-08-22**,
so this is met rather than pending. The standing rule still applies: point apps
at a disposable local instance, never at anything that matters if it crashes or
hangs.

### Done when

Not full Redis parity. Concretely: **a real client library completes its normal
on-connect handshake without a fatal error, every command the application
actually issues in its real workflow gets a correct reply, and the server can be
started, stopped and left running as a background service without a human
watching a terminal.** Steps 1-5 only need as much investment as the target
application demands — this milestone is scoped to "make one real thing work,"
not "close every gap in the README's Known Gaps list."

## Completed Milestones

### V11 - Testing Hardening: Differential, Fuzz, and Adversarial Security [Done 2026-08-22]

Opened 2026-08-16, closed 2026-08-22. **Suite 1023 → 1458 checks, green on
Release and under ASan+UBSan+LSan with no sanitizer output of any kind.** Every
finding it produced is fixed and carries a regression check that was watched
failing against the pre-fix binary first.

**Step 0 made `scripts/stress_test.py` the whole suite.** One command, no setup:
`--server <binary>` turns on the phases that need process control *and* starts
the instance the rest talks to. The machinery that made it possible is
`Instance` (spawn in a temp dir, SIGTERM/SIGKILL, stderr kept in a file) and
`PhaseCtx` (bind-tested private ports, per-phase workdirs, evidence dump on
failure, a `skip` counted separately from a pass) — which is what lets a
restart, a crash and a two-node link live in the same file as the command tests.
Ten phases now: `unit`, `memory`, `config`, `auth`, `security`, `persistence`,
`tls`, `replication`, `differential` (against a real `redis-server`), `fuzz`
(libFuzzer on `parse_resp_request` and `rdb_load_buffer`). **New coverage goes
in a phase, never a new file.** The replication suite ported whole — 153 checks
— including the three shapes that existed nowhere else: a freezable proxy (a
link that goes silent *without* closing, which `poll()` cannot see), assertions
read from a server's stderr file rather than over the wire, and a phase that
spawns its own pair because a handover swaps both roles. The shell scripts
became assertions; `test_tls.py` deliberately stayed out, being a measurement
harness with nothing to assert.

Results file by environment, decided by **reading the kernel** rather than a
flag (`/proc/sys/kernel/osrelease`, `/proc/version`, with `WSL_DISTRO_NAME`,
`binfmt_misc/WSLInterop` and `/run/WSL` as fallbacks — a custom WSL2 kernel
drops "microsoft"): `docs/logs/<WSL|Native>/<kind>_<plain|tls>.{md,json}`. The
split exists because the WSL numbers sit ~2.4x below native and one filename for
both erases that. `--compare` refuses mismatched `-n/-c/-P` but *not* a
differing transport, and prints no verdict column, because one run per side has
no noise floor.

**Step 0c characterised the harness itself, and that paid twice over.** Its own
client read *one byte per `recv()`*: a 100k-element `KEYS` cost over a million
syscalls — **1422 ms client-side against 19 ms of real server CPU**, with the
distortion scaling by the host's syscall cost (26x between two machines the C
client puts 2.3x apart). Buffered, that became 253 ms, and the stress phase went
145 → 3450 ops/sec on WSL against 3742 → 22957 on native. **The asymmetry is the
proof**: 1.41x off total WSL runtime and 1.01x off native, exactly proportional
to syscall cost. What *survived* the fix matters more — TLS still measures ~13%
faster than plaintext there, and it is the client (`ssl` releases the GIL across
longer C sections), so "never compare transports with the Python stress number"
is right for a completely different reason than syscalls. That caveat now prints
next to the number, because keeping it only in a doc failed twice.

**The findings.** The sanitizer build found a 🔴 128 KB-per-connection leak
(`Buffer` is a POD; `delete conn` orphaned both 64 KB arrays). The **static
security review** (2026-08-19) found four reproducing issues: a 🔴 accept-loop
spin — no `maxclients` existed anywhere in the tree, and at `RLIMIT_NOFILE` the
loop burned **96% of a core and 2 MB/s of log** (416,509 identical lines in five
seconds) — plus 🟠 audit-log forgery through an unescaped `AUTH` username (a
*failed* auth wrote a well-formed fake success record), 🟠 a missing inline-command
cap worth **16x measured RSS amplification**, and 🟠 a `tls-handshake-timeout`
that any byte reset, so a peer dribbling 1 B/s survived 4x past it. The
**targeted logic-level attacks** (2026-08-21) added two 🔴s in the fork
machinery: `aof_rewrite_reap` cleared `g_aof_child_pid` only on total success,
so an OOM-killed or `_exit(1)` child wedged the server silently — nothing
logged, `BGREWRITEAOF` replying success while doing nothing, every write
duplicated into a buffer nothing drains (66 MB against a control's 33 MB); and
`close(g_aof_current_size)` closed a **byte count** rather than `g_aof_fd`,
leaking one fd per rewrite, pinning the superseded inode (8.5 MB compacted to
135 bytes freed nothing) and aiming `close()` at an arbitrary fd number —
demonstrated closing stdin. Then 🟠 `CONFIG REWRITE` writing operator strings
into a line-oriented file four ways (an ACL username containing spaces widened a
user from `~x:*` to `~*` across a restart), 🟡 ACL rules accepting any string and
never being consulted, 🟡 `ACL GETUSER` ignoring overrides and channels, 🟡 fork
children inheriting every socket, and 🟡 conns open at shutdown never freed.
Full write-ups and measurements in `CODE_REVIEW.md`; the log of every attempt,
including the ones that found nothing, is `docs/SECURITY_TESTING.md`.

**Four lessons, each paid for:**

- **The hypotheses were mostly wrong, and that was fine.** Three of the five
  targeted attack cases came back clean — including the two reasoned about most
  confidently (ACL enforcement through a rename; control bytes re-scanned
  downstream). Both 🔴s came from following the fork machinery next door to
  where the search was aimed. A hypothesis earns its place by aiming the search,
  not by being right.
- **An instrument nobody reads is not instrumentation.** The suite ran under
  ASan+UBSan+LSan from Step 0 onward and *nothing ever read the output*, so a
  `Conn` leaked on every single connection sat in a stderr file for a week while
  the suite reported green. `PhaseCtx.check_sanitizer_output()` now scans every
  instance after its phase stops it, one check per phase.
- **A fix can grep clean, build clean, and do nothing.** `SOCK_CLOEXEC` was
  applied exactly as specified against the fd-inheritance finding and could
  never have worked — CLOEXEC fires on `exec()` and the save children never
  exec. Only the check written against the *attack* could tell a real fix from a
  plausible one. Verifying that a snippet landed is not verifying that it works.
- **A measurement needs a control taken the same way.** The shadow-buffer check
  was wrong twice: first a 16 MB seed let a freed allocator arena absorb the
  entire duplicate (+256 KB on the broken server *and* the control), then an
  absolute "under 1.5x the payload" threshold proved unportable — the same
  workload on a healthy server costs 1.04x on Release and **5.78x under ASan**.
  What works is a control instance built the same way.

**The milestone's recurring bug, in three forms: a routine that exists twice
where only one copy is right** — `aof_rewrite_reap` against
`rdb_check_background_save`, the delta write loop against `aof_write_snapshot`,
and (from V9.8) the config row whose second copy carried the first's identifier.
When a routine exists twice, diff the two.

### V10.6.1 - Deferred TLS Optimizations (V9.7.5 tail) [Done 2026-08-16]

Opened 2026-08-14 as a bounded detour, closed 2026-08-16. Three deferred items,
each gated on "escalate only when a metric demands it" — and none of those
metrics existed, so the milestone opened by building the instrument. Outcome:
**one reverted, one declined, one shipped.** That spread is the point; a
measurement pass where everything ships was not measuring.

**Step 0 — `scripts/test_tls.py`, the measurement harness.** Spawns its own
passwordless instance on private ports, emits four blocks (handshake, accept
storm, redis-benchmark throughput, cert rotation) into a machine-comparable
`docs/tls_metrics_<tag>.json`, and `--compare` diffs two runs. Baselines
committed: `baseline` (workers 8), `baseline-w300`, `cert-reload`. Release build,
WSL2 — absolute numbers sit ~2.4x below the native figures in the Testing Matrix
and compare only to runs on the same box.

Constraints it encodes, each one silent when violated: **it refuses a Debug
build** (`mem_selfcheck()` runs after every command when `NDEBUG` is unset and
walks the keyspace — and `build/` *is* Debug, `build-rel/` is the Release one);
passwordless, because `k_max_auth_inflight` is 4 against Argon2id's 76MiB bound
and an authed benchmark measures the KDF; `save ""`, so no BGSAVE fork lands
mid-measurement; throughput only ever from `redis-benchmark`, never from the
harness's own client-bound Python; every metric repeated with the spread across
repeats reported as **the noise floor**, and `--compare` refuses to render a
verdict when either side ran `--repeat 1`, because a floor of 0% turns jitter
into a result. The established-connection pinger runs in its own **process** —
from a thread, GIL contention would be reported as server stall.

Baseline, for anything later: TLS handshake **1.10 ms of server CPU per
connection** (±3.0%) against 0.37 ms for the plaintext accept path, giving a
~900 new-TLS-connections/s ceiling on one core — confirmed twice over, since
`cpu_per_conn × conns_per_s` = 1.00 means the storm saturates exactly one core.
Session resumption works (rate 1.000, saves 40.8%). TLS/plaintext throughput
0.59 at `-P 16`, 0.76 at `-P 1`.

**V10.6.1a — bound handshake work per tick: APPLIED, MEASURED, REVERTED.**
`k_max_accepts_per_tick = 4` moved nothing: established-connection p99 43.84 →
36.19 ms against a predicted ~4.4 ms, every row inside a ±135-225% noise floor.
Reverted by this milestone's own rule — kept only if the number moves. Three
explanations were tested and rejected, which is what makes the leftover finding
worth trusting: it is **not** client-side CPU contention (pinning the server,
victim and burst workers to separate cores changed nothing), **not** the
per-accept `fprintf(stderr)` (<0.033 ms/conn), and **not** the 128 KB/conn
buffer allocation (3-6 minor faults per connection, versus the 32 pages a
touched allocation would show).

Two durable facts survive it. First, **the accept loop is not where the cost
is** — `handle_accept` does accept + `Conn` alloc + `tr_tls_attach`, while the
RSA signature happens in `handle_tls_handshake` (`server.cpp:1309`) on a later
tick, driven by poll readiness. Second, **the stall scales with total burst size
while CPU per connection stays flat** (44/34/51 ms at burst 300 vs 68/81/95 ms
at burst 1500, CPU/conn 1.13-1.27 ms throughout), which no accept-rate cap can
explain, and flat CPU/conn across a 5x burst also rules out an O(N²) event loop.
Leading hypothesis for whoever picks this up: OpenSSL flushes its server session
cache automatically every 256 handshakes (`ssl_update_cache` →
`SSL_CTX_flush_sessions`, an O(cache size) walk), V9.7.5 turned that cache on
(`SSL_SESS_CACHE_SERVER`), and the storm mints a fresh session per connection —
which fits both the burst scaling and the stall growing run-over-run (68 → 81 →
95). Test it by capping the cache before writing any code, and **do not go to
the thread pool until the cause is identified.**

**V10.6.1b — kTLS: DECLINED, measured.** It removes the userspace copy and moves
record crypto into the kernel; it removes no syscalls. MYRED's records are ~50
bytes, so the copy is on the order of ten nanoseconds against a measured TLS
overhead of **4.50 µs/op** at `-P 1` and 0.68 µs/op at `-P 16` — microseconds are
not recovered by eliminating nanoseconds. kTLS earns its keep on bulk transfer
via `sendfile()`, the opposite end of the workload axis. Two supporting facts:
the `perf` decomposition this was gated on **cannot run on this machine**
(`perf` absent, `perf_event_paranoid` 2), and kTLS engages silently or not at
all, so a naive before/after can measure nothing and report success. Falsifiable
cheaply — ~5 lines plus a `BIO_get_ktls_send()` assertion, against a bench metric
that resolves ±3.5% — but the expected effect is ~0.3%. **Reading trap recorded:
the TLS/plaintext *ratio* inverts this conclusion** (0.59 pipelined vs 0.76 not),
because pipelining packs 16 messages into one record and amortizes exactly the
per-record cost. Compare absolute per-op deltas, never the ratio.

**V10.6.1c — cert reload without restart: SHIPPED.** `tls-cert-file` and
`tls-key-file` are no longer `boot_only`; their `apply` stages the value,
rebuilds, and rolls back on failure. **1.09 ms with every established connection
surviving, against 62.07 ms and all of them dropped** — 57x, and non-disruptive,
which matters because replica links are ordinary TLS connections since V10.

- **`SSL_CTX_free(old)` on the swap is REQUIRED, and an earlier draft of this
  plan said the opposite.** OpenSSL refcounts the context, but `SSL_CTX_new`
  hands you refcount 1 and *that reference is yours*; each `SSL_new()` takes its
  own and each `SSL_free()` returns it. Dropping the pointer without freeing
  floors the count at 1 and leaks one whole context, session cache included, per
  rotation. Freeing drops only our reference; live connections keep theirs.
- **`tls_ctx_build()` never touches the global.** It returns a complete context
  or frees the half-built one on every error path, and both `tr_tls_init` and
  `tr_tls_reload` go through it. That is the entire reason a failed reload is
  harmless: the live context is never the thing being mutated.
- `tr_tls_reload` returns true as a **no-op when no context exists yet** —
  `config_apply` walks the config file before `tr_tls_init` runs, and the
  cert/key pair is not fully read at that point.
- **Rotation is in-place, by design.** `CONFIG SET` takes a single pair and a
  cert must swap together with its key, so changing both *paths* at once is
  impossible; overwrite the files and use `CONFIG SET tls-cert-file <same path>`
  as the trigger, which re-reads both atomically. A path change fails cleanly and
  mutates nothing. Multi-pair `CONFIG SET` is in BACKLOG.
- A reload starts with an **empty session cache**, so clients full-handshake once.

**The harness found two boot-bricking slips before it measured anything**, both
clean under `-Wall -Wextra`: `tr_tls_init`'s "TLS is off" early-out returned
`false` instead of `true`, so every *plaintext-only* config died at boot through
an unguarded `fatal_exit` with an empty message; and the `tls-key-file` row's
`apply` was wired to `g_config.tls_cert_file`, so `tls_key_file` was never
assigned and every *TLS* config died on the emptiness check. The second is the
V9.8 wrong-field bug one level down — a setter, not a getter — and the boot
round-trip check cannot see it, because it skips any row that owns an `emit`.
`test_tls.py` pins it with a `[REG]` check that sets `tls-key-file` and reads the
field back.

### V10 - Replication and High Availability [Done]

Opened 2026-08-03, closed 2026-08-13. Master-replica replication end to end plus
a coordinated, zero-data-loss handover: identity/offset/backlog bookkeeping, the
full-resync handshake on both sides, read-only replicas that survive restarts,
partial resync off the backlog, automatic reconnect, ack tracking and `WAIT`,
detection of a link that goes silent without closing, a durability floor, and
`FAILOVER`. **V10.6e** (automatic, Sentinel-style election) is the only piece
left; it moved to `BACKLOG.md` on 2026-08-14, deliberately behind V11's testing
work. Cluster/hash-slot sharding was split out to **V12** on 2026-08-12 — it
shares no code and no design with failover, which is precisely why bundling them
under one number left this unscoped for three weeks.

Regression coverage is `scripts/test_replication.py`: its own master, two
replicas, a separate pair for `FAILOVER`, and a **killable, freezable in-process
TCP proxy**. Killing the master instead would destroy its backlog and mint a new
`repl_id`, forcing a full resync and making every partial-resync assertion
silently vacuous. `freeze()` is the other failure mode and the one V10.6b/d need:
stop moving bytes while leaving every socket **open**, because a link that closes
is already handled — `poll()` reports it immediately. All of it now lives in
`stress_test.py` as the `replication` phase (V11 Step 0, 2026-08-15).

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

#### V10.6 - Failover [Done 2026-08-13]

Four steps in dependency order — each is a prerequisite for the *correctness* of
the next, not just its convenience — all deterministic and testable on one box
with no second process. Design rationale in `DECISIONS.md` → Replication and
Failover; **V10.6e** (automatic election) is in `BACKLOG.md`, behind V11.

**a. Promotion that works in the case you actually promote in.** Four defects,
found by reading the promotion path against the failover scenario, then running it:

- `REPLICAOF NO ONE` was gated on `repl_state` (the *link phase*) instead of
  `replica_mode` (the role), making it a silent `+OK` no-op on exactly the
  replica whose master had died — the only case anyone types it in.
- `repl_new_id()` discarded the history, full-resyncing every sibling at the
  moment the deployment is already a node down. Now `repl_shift_id()`: retire the
  old identity into `repl_id2`/`second_repl_offset` and let `do_psync` honour it.
- **A replica never fed its own backlog** — `repl_backlog_feed`'s only call site
  is inside `propagate()`, gated off under `g_loading` — so the ring was empty at
  the instant of promotion and cannot be backfilled after. It now feeds the
  master's bytes **verbatim** (a re-encode could differ in length and drift every
  later offset), *replacing* the manual `master_repl_offset +=` rather than
  joining it (the feed advances the offset itself; both = every byte counted
  twice, corrupting `REPLCONF ACK` and `WAIT`), and *before* the `buf_consume`.
- Site 10: the replica never adopted the replid `+CONTINUE` carries, so the
  partial resync worked exactly once — lesson 3 above.

A function declared, defined and never called draws no warning at `-Wall
-Wextra`; that is how `repl_shift_id()` sat unused after "eight sites applied".

**b. Detect a master that stops talking without closing.** `repl-timeout`
(runtime-settable, seconds on the wire, ms in `Config`), `master_last_io_ms`
stamped on **any** inbound byte, `master_last_io_seconds_ago` in `INFO`, and the
mirror image on the master side — a replica that stops acking is dropped.

- The replica check is **phase-independent**: the master link sits in no idle
  list, so nothing had ever reaped it, and a master that accepts the socket then
  never sends `+FULLRESYNC` had stranded replicas in `HANDSHAKE` since V10.2b.
- Only replicas that acked **at least once** are reaped; `ack_time_ms == 0` means
  "still doing its initial resync", and reaping those kills exactly the
  big-dataset replicas the timeout was never meant to touch.
- **The keepalive is the other half of the timeout.** Nothing travels
  master→replica on an idle link, so a timeout on inbound bytes expires on a
  *healthy* master (measured: two drops and three resyncs in 13s at
  `repl-timeout 5`). The `PING` goes through `repl_backlog_feed` +
  `repl_feed_replicas`, **not `propagate()`** (which would grow an idle server's
  AOF without bound), and it *does* advance the offset. `ping-period <<
  repl-timeout` is the contract between the two directives.
- `repl_ping_at_ms` is written only when a ping fires, so lowering the directive
  at runtime didn't apply for one full *old* interval; the apply lambda now
  re-arms. `config_selfcheck` cannot catch a seconds/ms mix-up — a getter missing
  its `/1000` round-trips against itself.

**c. `min-replicas-to-write` / `min-replicas-max-lag`.** `good_replicas()` counts
replicas that acked within `max-lag` seconds, riding the existing `spec.is_write`
gate beside the read-only check; `-NOREPLICAS`, reads untouched.

- **Master side only, never under `g_loading`**: a replica that drops a write it
  was *sent* has silently forked from its master.
- **No "is it alive" stamp at attach** (where the first draft went) —
  `ack_time_ms == 0` already means "attached, never heard from", and stamping
  would start a `repl-timeout` clock against replicas legitimately mid-image.
- `max-lag 0` counts replicas still loading their image: the documented meaning
  of 0, and weaker than the default 10.

**d. `FAILOVER [TO host port [FORCE]] [ABORT] [TIMEOUT ms]`.** Zero-data-loss
planned handover with **no second process, no quorum, no election**: pause writes,
wait for the target to ack `master_repl_offset`, demote, hand over. `do_failover`
sets state; `failover_cron` does the work, since the demotion destroys connections.

- **The offsets line up for free, and that is the whole trick.** The target is
  promoted by `PSYNC ... FAILOVER` *before* the resync logic runs, so `repl_id2`
  is the history it shared with the sender and `second_repl_offset` is where the
  sender stopped — it asks for that offset and is owed **zero bytes**. Only the
  pause makes it true. Verified: a clean handover moves **no RDB at all**.
- **Demoting drops every replica first** (collect before `conn_destroy`, which
  erases from the set): this server does not relay a stream it is receiving, and
  the target is among them, which would loop its own writes back at it.
- `repl_start`'s `have_history` had to learn `IN_PROGRESS`, or the handover sends
  `PSYNC ? -1` and pulls a full image — losing the point of the command.
- **`FORCE` pays for its lost bytes with a full resync, and must**: the demoted
  master is *ahead* of the offset the target promoted at, so a `+CONTINUE` would
  keep writes the new master never saw.
- Two deliberate Redis divergences: `ABORT` refused once `IN_PROGRESS`, and paused
  writes **refused** (`-FAILOVER`) rather than blocked (deferred version in BACKLOG).

**Five slips landed while applying c/d by hand, four invisible to the compiler**
(writeups in `CODE_REVIEW.md` → V10.6c/d Apply Slips): the `NOREPLICAS` gate
**replaced** the `READONLY` gate instead of following it; `port < 1 || port <
65535` rejected every usable port — **the same inverted comparison as V10.5's
`p > 65536`, four days apart**; `failover_force` was never assigned, so `FORCE`
did nothing; `failover_state` was emitted inside `if (replica)`, invisible in the
one state that only exists on a master; `min-replicas-max-lag` defaulted to 0.
The rule: **grep the anchor a snippet was supposed to leave alone**, and remember
that an assignment nobody typed reads perfectly — only a runtime test finds it.

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

Primary harness — `scripts/stress_test.py` is the suite, and `--server` runs all
of it (V11 Step 0, 2026-08-15):
```bash
python3 scripts/stress_test.py --server build-rel/server --destructive --bench
python3 scripts/stress_test.py --server build-rel/server --tls
python3 scripts/stress_test.py --server build-rel/server --phases replication
python3 scripts/stress_test.py --list-phases

python3 scripts/stress_test.py --correctness-only                        # live server
python3 scripts/stress_test.py --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <pass>
```

Naming the binary turns on the phases that need process control and starts the
instance the rest of the suite talks to, so no server has to be running first.
Private ports come from `--base-port` (default 12500) and are bind-tested, so two
runs can share a machine. **1022 checks on a clean Release build, ~90 s.**

| Phase | Covers |
|---|---|
| `unit` | HMap incremental rehash, compiled against the repo's `hashtable.cpp` |
| `memory` | per-type accounting drains to zero, both `maxmemory` policies, incremental eviction |
| `config` | `CONFIG REWRITE` → restart → every directive holds its value, boot-only ones included |
| `auth` | async AUTH: pipeline gating, lockout, concurrent completions, loop latency under storm |
| `security` | ACL category + key gating, renamed/disabled commands, audit redaction, protocol abuse |
| `persistence` | AOF write gating, rewrite, hybrid preamble + delta, torn tail, RDB round-trip, restart matrix |
| `tls` | handshake on both listeners, live cert rotation, rollback on a refused swap |
| `replication` | full/partial resync, `WAIT`, durability floor, wedged links, `FAILOVER`, promotion history |

Replication is 153 of those checks, capability-gated so it stays runnable while a
milestone is half-applied: full and partial resync, the read-only gate, link loss,
backlog overflow, automatic reconnect, `WAIT`, `repl-timeout` on a wedged link,
the V10.6c durability floor, three-node promotion with a sibling partial-resync on
the **second** reconnect, and the whole of `FAILOVER` on its own pair — pause,
`ABORT`, `TIMEOUT` on an idle server, `FORCE` and its full resync, and the clean
handover that moves no RDB. Timer-deadline phases assert on the server's **stderr
file**, never by polling `INFO`, because polling wakes the loop the test is trying
to catch asleep.

Results are filed under `docs/logs/<WSL|Native>/`, split by an environment read
from the kernel, with a `.json` summary beside each transcript carrying the
platform block, per-phase counts and parsed throughput. `--compare A.json B.json`
diffs two machines and refuses mismatched benchmark parameters.

**Benchmark only on a Release build** (`cmake -B build-rel -DCMAKE_BUILD_TYPE=Release`;
Debug runs `mem_selfcheck`'s whole-keyspace walk per command and poisons numbers).
Same-machine comparisons only. Benchmark TLS against a **passwordless** instance —
`redis-benchmark`'s 50-client AUTH storm hits `k_max_auth_inflight=4`.

Recorded baselines (`-n 100000 -c 50 -P 16`, ops/sec).

**Re-baselined 2026-08-16 from four runs** (2 machines x 2 transports, every one
1023/1023 green) produced by `stress_test.py --server build-rel/server --bench`.
Unlike every earlier entry in this file these carry their own provenance — the
`.json` beside each log records CPU, kernel, governor, crypto ISA and build type,
so a number can be checked against the machine that produced it instead of being
trusted.

**Native** — Arch 6.18.35-lts, i7-1165G7, `governor=performance`,
crypto ISA `aes vaes pclmulqdq vpclmulqdq sha_ni avx2 avx512f avx512vl`:

| | plaintext | TLS | TLS % |
|---|---|---|---|
| PING_MBULK | 3.03M | 1.56M | 52% |
| SET | 2.38M | 1.39M | 58% |
| GET | 2.33M | 1.43M | 61% |
| INCR | 2.13M | 1.28M | 60% |
| LPUSH | 2.00M | 1.25M | 63% |
| SADD | 2.04M | 1.01M | 50% |
| HSET | 1.96M | 1.25M | 64% |
| MSET(10) | 840k | 588k | 70% |
| LRANGE 100/300/500/600 | 215k/76.5k/46.6k/38.6k | 188k/55.6k/27.7k/22.3k | 88/73/59/58% |

**WSL2** — Ryzen 5 3600X, governor unreadable (the Windows host owns the
P-states), crypto ISA `aes pclmulqdq sha_ni avx2` — **no `vaes`**:
PING_MBULK 901k / 565k · SET 909k / 543k · GET 901k / 549k · MSET(10) 265k / 199k.

**TLS costs ~40%, and it is the SAME on both machines**: plain->TLS is 0.59x
native and 0.61x on WSL. One of those CPUs has VAES and the other does not, and
it makes no measurable difference — so MYRED's TLS cost is dominated by
per-record framing and syscalls, not by the AES math. That is the V10.6.1 kTLS
conclusion reached a second time, from measurement rather than arithmetic.

Cross-machine is 2.38x (plaintext) and 2.37x (TLS). It conflates virtualization
with microarchitecture — different CPUs, different core counts, laptop vs
desktop — so treat it as an upper bound on the cost of WSL2, never a measurement
of it. A real answer needs the same box booted both ways.

**The noise floor, measured, and it is large.** Two runs of the same binary on
the same machine with the same governor:

| | median deviation | worst |
|---|---|---|
| plaintext, small ops | **13.7%** | 32.7% (`rpop`) |
| plaintext, bulk LRANGE | ~1% | 2.4% |
| **TLS, all ops** | **1.3%** | 28% |

Small-op plaintext throughput cannot support any claim finer than about 30%, and
this file has twice recorded a confident finding inside that band. **TLS numbers
are the reproducible instrument** — 13 of 19 within 3%, several identical to the
digit — because they are bounded by deterministic crypto work rather than by
whatever the host scheduler did that afternoon. Prefer TLS and bulk numbers for
regression tracking; treat a single small-op plaintext delta as nothing at all.

Benchmark only on a Release build (`build-rel/`; `build/` is Debug and runs
`mem_selfcheck`'s whole-keyspace walk per command). Passwordless instances only —
`redis-benchmark`'s 50-client AUTH storm hits `k_max_auth_inflight=4`. The suite's
spawned primary is already the right shape (no auth, `appendonly no`, `save ""`).

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
