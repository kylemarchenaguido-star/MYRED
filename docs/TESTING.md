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

Every `stress_test.py` run prints its identity in the banner and again at the end,
and writes to a file named after itself — a TLS run never overwrites a plaintext one.

```
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + stress over TLS (authenticated) → 127.0.0.1:1235
═══════════════════════════════════════════════════════
  Target:    127.0.0.1:1235
  Transport: TLS (insecure — cert not verified)
  Auth:      password
  Log:       docs/stress_results_tls.md
```

| Flags | Log file (plaintext / TLS) |
|---|---|
| *(none)* | `docs/stress_results_plain.md` / `stress_results_tls.md` |
| `--bench` | `docs/bench_plain.md` / `bench_tls.md` |
| `--stress-only` | `docs/stress_plain.md` / `stress_tls.md` |
| `--correctness-only` | `docs/correctness_plain.md` / `correctness_tls.md` |

Override with `--log path.md`, disable with `--log ''`.

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

`replicaof` in that config file needs **V10.3b**; before it lands, drop the line
and use `redis-cli -p 1338 replicaof 127.0.0.1 1336` after boot. Either way the
role is what `INFO replication` reports — check `master_link_status:up` *first*,
since every other assertion below is meaningless without it, and a restarted
replica silently comes back a writable master until V10.3b.

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
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j   # normal + benchmarks
cmake -B build-dbg -DCMAKE_BUILD_TYPE=Debug && cmake --build build-dbg -j
```

`build-dbg/` runs a whole-keyspace memory check per command — use it to catch
accounting drift, never to measure speed.

---

## Suites that need no running server

Private ports, temp dirs — safe to run while a real instance is up.

```bash
python3 scripts/test_pubsub.py [--evict]              # V8 pub/sub + notifications
python3 scripts/test_security.py --destructive        # ACL, audit, protocol abuse
python3 scripts/test_restart_matrix.py --destructive  # RDB/AOF restart + crash recovery
python3 scripts/test_aof_restart.py
```

Add `--keep` to preserve the temp workdir (server stderr, configs) on failure.

## Suites that need a running server

```bash
./build/server myred.conf     # in another terminal

python3 scripts/stress_test.py --password <PASS>
python3 scripts/stress_test.py --password <PASS> --correctness-only
python3 scripts/stress_test.py --password <PASS> --stress-only --stress-threads 16 --stress-ops 2000
python3 scripts/test_async_auth.py --password <PASS>
python3 scripts/test_memory.py --password <PASS>

scripts/test_evict_tick.sh
scripts/test_aof.sh   scripts/test_aof_rewrite.sh   scripts/test_aof_hybrid.sh
scripts/diag_live.sh  scripts/diag_ttl.sh
```

---

## TLS

```bash
./build/server myred.conf

redis-cli --tls --insecure -p 1235 -a <PASS> ping
openssl s_client -connect 127.0.0.1:1235 -tls1_3 </dev/null 2>&1 | head -20
```

**The V9.7 close-out gate** (V9.7 itself is closed — 603/603 green over both
transports, 2026-07-25 — this pair of runs is the regression check, not an
open item):

```bash
./build/server myred.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <PASS>   # ~555 checks

./build/server bench.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1337                     # ~551 checks
```

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

Release build, `bench.conf`, cool machine, one at a time. **A `GET` slower than
`SET` means the machine is throttling and the numbers are junk.**

```bash
./build/server bench.conf
python3 scripts/stress_test.py --port 1336 --bench                    # -> docs/bench_plain.md
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench  # -> docs/bench_tls.md
```

Tuning: `--bench-requests`, `--bench-clients`, `--bench-pipeline`.
Baselines live in `planning/ROADMAP.md` → Testing Matrix.

---

## Full pre-commit gate

```bash
cmake --build build -j && cmake --build build-dbg -j

python3 scripts/test_pubsub.py --evict
python3 scripts/test_security.py --destructive
python3 scripts/test_restart_matrix.py --destructive

./build/server myred.conf &
python3 scripts/stress_test.py --password <PASS>
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <PASS>
kill %1

./build/server bench.conf &
python3 scripts/stress_test.py --port 1336 --bench
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench
kill %1

# drift check — catches accounting bugs Release silently tolerates
./build-dbg/server myred.conf &
python3 scripts/stress_test.py --password <PASS> --correctness-only
kill %1        # any "[mem] drift" line in stderr is a real bug
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
| need failure evidence | re-run with `--keep`, read the temp workdir's `stderr-*.log` |
