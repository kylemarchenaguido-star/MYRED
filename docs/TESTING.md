# MYRED — Testing Runbook

## Is the server running?

```bash
redis-cli -p 1234 ping                    # plaintext        -> PONG
redis-cli -p 1234 -a <PASS> ping          # if requirepass is set
redis-cli --tls --insecure -p 1235 -a <PASS> ping   # TLS    -> PONG
```

| Answer | Meaning |
|---|---|
| `PONG` | up and reachable |
| `Could not connect` | not running (or wrong port) |
| `NOAUTH` / `WRONGPASS` | up, but your password is wrong or missing |
| `wrong version number` | you pointed a TLS client at the plaintext port |

Start one:

```bash
./build/server myred.conf     # ports 1234 plaintext + 1235 TLS
./build/server bench.conf     # ports 1336 plaintext + 1337 TLS, no auth
```

## Which test am I running?

Every run prints its identity in the banner and again at the end, and writes to a
file named after itself — a TLS run never overwrites a plaintext one, and a run
from one machine never overwrites a run from another.

```
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + managed-instance phases + stress over plaintext (passwordless) → 127.0.0.1:12590
═══════════════════════════════════════════════════════

-- Platform (read from the kernel) ---------------------
  Environment:  WSL (WSL2)
  Kernel:       6.6.87.2-microsoft-standard-WSL2
  CPU:          AMD Ryzen 5 3600X 6-Core Processor
  Threads:      12 (usable by this process: 12)
  Crypto ISA:   aes pclmulqdq sha_ni avx2  (no vaes — one AES block per instruction)
  Memory:       12209364 kB  swap 3145728 kB
  Governor:     n/a
  Load average: 0.15 0.07 0.05 1/441 27995
  somaxconn:    4096   nofile=1048576   tcp_ulp=tls
  Build:        release  [build-rel/]
  Log:          docs/logs/WSL/full_plain.md
```

### Where the results land

```
docs/logs/<WSL|Native>/<kind>_<plain|tls>.md      readable transcript
docs/logs/<WSL|Native>/<kind>_<plain|tls>.json    machine-comparable summary
```

The environment directory is decided by reading the kernel, not by a flag:
`/proc/sys/kernel/osrelease` and `/proc/version` (plus `WSL_DISTRO_NAME`,
`/proc/sys/fs/binfmt_misc/WSLInterop` and `/run/WSL` as independent fallbacks,
because a custom-built WSL2 kernel drops `microsoft` from its release string).
Anything that is not WSL files under `Native`.

**That split is the point.** A VM's syscall and network costs are not the host's,
so a throughput number from WSL and one from bare metal are two different
measurements. Filing them under one name makes the difference vanish the moment
the second run finishes.

| Flags | `<kind>` |
|---|---|
| `--server <binary>` | `full` |
| *(none)* | `stress_results` |
| `--bench` | `bench` |
| `--stress-only` | `stress` |
| `--correctness-only` | `correctness` |

Override the path with `--log path.md`, move the root with `--log-dir`, disable
with `--log ''`. The JSON always sits beside the markdown.

The JSON carries the platform block, the build type, per-phase pass/fail/skip
counts, and the parsed `redis-benchmark` throughput — everything a comparison
needs and nothing that requires re-reading a transcript.

**`Crypto ISA` is there because it decides TLS throughput and the CPU model name
does not tell you.** A part with `vaes` + `vpclmulqdq` runs several AES blocks
per instruction where plain `aes` runs one, which is large enough to make a TLS
comparison between two machines meaningless if you don't know which is which.

---

## The configs

| | `myred.conf` | `bench.conf` | `replica/replica.conf` |
|---|---|---|---|
| plaintext / TLS port | 1234 / 1235 | 1336 / 1337 | 1338 / — |
| auth | `requirepass` + `user alice` | **none** | none |
| AOF | on | off | off (see below) |
| maxmemory | 64 MB `allkeys-lru` | unlimited | unlimited |
| role | master | master | replica of `:1336` |

**Benchmark against `bench.conf` only.** AOF fsync, the eviction ceiling, and the
Argon2 AUTH gate all distort throughput. `redis-benchmark`'s 50-client AUTH storm
also hits `k_max_auth_inflight=4` and returns `BUSY` — the KDF working as
designed, not a result.

### Replication pair

`bench.conf` is the master, `replica/replica.conf` the replica. **Run the replica
from its own directory** — the server opens `dump.rdb`/`appendonly.aof` by
relative path, so a replica started from the project root would fight the master
over the same files and load the master's snapshot as its own.

```bash
./build/server bench.conf                          # master  :1336, terminal 1
cd replica && ../build/server replica.conf         # replica :1338, terminal 2
```

```bash
redis-cli -p 1338 info replication   # role:slave + master_link_status:up
redis-cli -p 1336 set k v && redis-cli -p 1338 get k     # v
redis-cli -p 1338 set k v            # READONLY ... (V10.3a)
```

This pair is for looking at a live link by hand. Nothing needs to be set up to
*test* replication — `--phases replication` runs a master, a replica, a second
replica and two failover instances behind a killable proxy, all on private ports:

```bash
python3 scripts/stress_test.py --server build-rel/server --phases replication
```

The role is what `INFO replication` reports — check `master_link_status:up`
*first*, since every other assertion is meaningless without it.

Two reads that localise a broken link fast: `master_replid` on the replica must be
byte-identical to the master's (if it still shows its own boot id, the
`+FULLRESYNC` line never parsed), and `master_repl_offset` counting up from zero
with `repl_backlog_first_byte_offset:1` means the instance is propagating its
*own* writes — i.e. it is not a replica at all.

The replica has **no AOF on purpose**: replicated writes are applied under
`g_data.g_loading`, which keeps them out of its own AOF, so one there would record
only pre-replication state. Same reason it emits no keyspace notifications and
does not invalidate its own `WATCH`ers — see ROADMAP → V10.2b known limitations.

## Build

```bash
cmake -B build && cmake --build build -j                                # DEBUG
cmake -B build-rel -DCMAKE_BUILD_TYPE=Release && cmake --build build-rel -j   # RELEASE
```

**`build/` is the Debug build and `build-rel/` is the Release one.** Debug runs
`mem_selfcheck()` after every command and it walks the whole keyspace, so it is
O(keyspace) per operation. Use it to catch accounting drift; never to measure
speed. The suite prints the build type it found and says so.

---

## The suite

`scripts/stress_test.py` is **the** harness. It has two halves.

The **live-server half** talks to a server you already started, and covers the
command surface, transactions, pub/sub, ACLs, memory accounting and the
concurrent-write stress phase:

```bash
./build/server myred.conf     # in another terminal

python3 scripts/stress_test.py --password <PASS>
python3 scripts/stress_test.py --password <PASS> --correctness-only
python3 scripts/stress_test.py --password <PASS> --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/stress_test.py --password <PASS> --bench
```

The **managed-instance half** needs process control — a restart, a crash, a
second instance, a link that can be cut — so it spawns and reaps its own servers
on private high ports in temp directories. Give it the binary:

```bash
python3 scripts/stress_test.py --server build-rel/server
```

That one command is the whole suite: it starts an instance for the live half too,
so nothing has to be running first, and nothing it touches is yours. Add
`--destructive` for the SIGKILL crash-recovery and protocol-abuse checks.

```bash
python3 scripts/stress_test.py --server build-rel/server --destructive        # everything
python3 scripts/stress_test.py --server build-rel/server --bench              # ... plus speed
python3 scripts/stress_test.py --server build-rel/server --tls                # ... over TLS
python3 scripts/stress_test.py --server build-rel/server --phases replication # just one phase
python3 scripts/stress_test.py --list-phases
```

| Phase | Covers |
|---|---|
| `unit` | HMap incremental rehash — compiled against the repo's `hashtable.cpp` |
| `memory` | per-type accounting drains to zero, `maxmemory` under both policies, incremental eviction |
| `config` | `CONFIG REWRITE` → restart → every directive still holds its value |
| `auth` | async AUTH: pipeline gating, lockout, concurrent completions, loop latency |
| `security` | ACL category and key gating, renamed/disabled commands, audit redaction, protocol abuse |
| `persistence` | AOF write gating, rewrite, hybrid preamble + delta, torn tails, RDB round-trip, restart matrix |
| `tls` | handshake on both listeners, live certificate rotation, rollback on a refused swap |
| `replication` | full/partial resync, `WAIT`, durability floor, wedged links, failover, promotion history |

Run against your own instance instead by naming it — `--host`/`--port` keep the
live half pointed where you say while the managed phases still use their own
private ports:

```bash
python3 scripts/stress_test.py --server build-rel/server --port 1234 --password <PASS>
```

Ports come from `--base-port` (default 12500) and are bind-tested before use, so
two runs can share a machine: `--base-port 12700` for the second.

When something fails, the temp workdir is kept and every instance's stderr tail
is printed with it — a failure without the server's own words is unusable.
`--keep` keeps the workdir even on a clean run.

---

## TLS

```bash
./build/server myred.conf

redis-cli --tls --insecure -p 1235 -a <PASS> ping
openssl s_client -connect 127.0.0.1:1235 -tls1_3 </dev/null 2>&1 | head -20
```

Against your own instance, over TLS:

```bash
./build/server myred.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <PASS>

./build/server bench.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1337
```

Or with no setup at all — this generates a throwaway self-signed pair, boots an
instance with `tls-port`, and runs the whole suite over it:

```bash
python3 scripts/stress_test.py --server build-rel/server --tls
```

Don't rely on a remembered check count; run it and read the number.

Session resumption — `s_client -reconnect` is a **false negative** on TLS 1.3
(the ticket is post-handshake and our protocol is client-speaks-first), so use
session files and exchange real data:

```bash
{ printf 'AUTH <PASS>\r\n'; sleep 0.5; } | openssl s_client -connect 127.0.0.1:1235 -sess_out /tmp/s.pem 2>&1 | grep -i "new\|reused"
{ printf 'AUTH <PASS>\r\n'; sleep 0.5; } | openssl s_client -connect 127.0.0.1:1235 -sess_in  /tmp/s.pem 2>&1 | grep -i reused
```

Second run must say **`Reused, TLSv1.3`**.

Real certs instead of `--tls-insecure`: `--tls-ca ca.pem`, plus
`--tls-cert client.pem --tls-key client.key` if `tls-auth-clients` is on.

---

## Benchmarks

Release build, no password, cool machine, one at a time. **A `GET` slower than
`SET` means the machine is throttling and the numbers are junk.**

```bash
python3 scripts/stress_test.py --server build-rel/server --bench
python3 scripts/stress_test.py --server build-rel/server --tls --bench
```

The instance it spawns is already the right shape for this — passwordless, AOF
off, `save ""` — so the KDF, an fsync and a stray BGSAVE fork all stay out of the
numbers. Against `bench.conf` by hand instead:

```bash
./build/server bench.conf
python3 scripts/stress_test.py --port 1336 --bench
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench
```

Tuning: `--bench-requests`, `--bench-clients`, `--bench-pipeline`.
Baselines live in `planning/ROADMAP.md` → Testing Matrix.

### Comparing two machines

Each `--bench` run writes its throughput into the summary JSON next to its log.
Diff two of them:

```bash
python3 scripts/stress_test.py --compare docs/logs/WSL/full_plain.json \
                                         docs/logs/Native/full_plain.json
```

It prints per-test ops/sec for both sides and the ratio, and **refuses outright**
when the two runs used different `-n`/`-c`/`-P` — throughput scales with all
three, so a mismatch manufactures whatever result you want. Transport is *not*
part of that check: plaintext against TLS on one box is a comparison you
legitimately want, so it is labelled rather than refused.

```bash
python3 scripts/stress_test.py --compare docs/logs/Native/full_plain.json \
                                         docs/logs/Native/full_tls.json
```

**Only the redis-benchmark table measures the server.** The Python stress
phase's ops/sec is client-bound — eight GIL-contending threads parsing RESP in
the interpreter — and on a five-run-per-side measurement it reports TLS ~13%
*faster* than plaintext, well outside a 3–6% spread. That is a real property of
the client (the `ssl` module releases the GIL across longer C sections than a
bare socket), not of MYRED. Compare it across runs of the *same* transport, and
never between transports.

There is no verdict column and there will not be one: a single run per side has
no noise floor to judge a delta against.

**The noise floor is measured, and it is large.** Two runs of the same binary on
the same box with a fixed governor:

| | median deviation | worst |
|---|---|---|
| plaintext, small ops | **13.7%** | 32.7% (`rpop`) |
| plaintext, bulk LRANGE | ~1% | 2.4% |
| **TLS, all ops** | **1.3%** | 28% |

So a single small-op plaintext delta supports no claim finer than about 30%, and
two findings have already been retracted for ignoring that. **TLS and bulk
LRANGE are the reproducible numbers** — bounded by deterministic crypto and
memory work rather than by whatever the host scheduler did — so prefer them for
regression tracking, and run three times and take a median before believing any
small-op plaintext difference.

---

## Full pre-commit gate

```bash
cmake --build build -j && cmake --build build-rel -j

# everything, on the Release build: correctness, concurrency, every managed
# phase, crash recovery, protocol abuse, stress, speed
python3 scripts/stress_test.py --server build-rel/server --destructive --bench
python3 scripts/stress_test.py --server build-rel/server --destructive --tls

# drift check — Debug audits the whole keyspace after every command, which is
# what catches accounting bugs Release silently tolerates
python3 scripts/stress_test.py --server build/server --phases memory,persistence
#   any "[mem] drift" line in the kept stderr is a real bug

# and against the config you actually ship
./build/server myred.conf &
python3 scripts/stress_test.py --password <PASS>
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <PASS>
kill %1
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `WRONGPASS` after a `CONFIG REWRITE` | the `requirepass` bug — see `planning/BACKLOG.md` |
| `BUSY too many pending AUTH attempts` | AUTH storm vs `k_max_auth_inflight=4`; use `bench.conf` |
| `wrong version number` | TLS client pointed at the plaintext port |
| `Server closed the connection` in `redis-cli subscribe` | usually redis-cli exiting on stdin EOF; confirm with `printf ... \| nc` |
| benchmark `GET` < `SET` | thermal throttling — cool down and re-run |
| `ULLONG_MAX` undefined | GCC 13+ dropped transitive includes; add the explicit header |
| need failure evidence | the workdir is kept on any failure; read its `stderr-*.log`. `--keep` keeps it on a clean run too |
| `warning: this is a Debug build` | you pointed `--server` at `build/`; benchmark `build-rel/` |
| a phase reports `skip` | the binary predates that directive or command — skips are counted separately and never pass as green |
| `server never opened port N` | a stale instance from an interrupted run holds it; pick another `--base-port` |
| results overwrote each other | two runs of the same kind, transport and environment share a path — `--log` to separate them |
