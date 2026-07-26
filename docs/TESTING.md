# MYRED — Testing Runbook

Every command needed to validate a build, in the order you'd actually run them.
For what the suites *cover*, see `planning/ROADMAP.md` → Testing Matrix.

Two config files, deliberately different:

| | `myred.conf` | `bench.conf` |
|---|---|---|
| plaintext port | 1234 | 1336 |
| TLS port | 1235 | 1337 |
| auth | `requirepass` + `user alice` | **none** |
| AOF | `appendonly yes` | `appendonly no` |
| maxmemory | 64 MB, `allkeys-lru` | unlimited |

**Benchmark against `bench.conf`, never `myred.conf`.** The AOF fsync path, the
64 MB eviction ceiling, and the Argon2 AUTH gate all distort throughput numbers.

---

## 0. Prerequisites

### Restore `requirepass` (one-time)

`myred.conf` currently has **no `requirepass` line** — `CONFIG REWRITE` stripped it
(see `planning/BACKLOG.md` → Open Bugs). Until the `config_rewrite` fix is applied
*and* this line is restored, the server runs passwordless and every auth test is
meaningless. Add it back near the top:

```
requirepass "#1ec1c26b50d5d3c58d9583181af8076655fe00756bf7285940ba3670f99fcba0"
```

Verify the fix holds before trusting anything else:

```bash
./build/server myred.conf &
redis-cli -p 1234 -a <PASS> config rewrite
grep '^requirepass' myred.conf          # MUST still be there
# restart, then:
redis-cli -p 1234 -a <PASS> ping        # PONG, not WRONGPASS
```

### Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
cmake -B build-dbg -DCMAKE_BUILD_TYPE=Debug && cmake --build build-dbg -j
```

`build/` (Release) is for benchmarks and normal runs. `build-dbg/` runs
`mem_selfcheck`'s whole-keyspace walk per command — use it to catch accounting
drift and UB, **never** to measure speed.

### TLS certificates

`tls/cert.pem` + `tls/key.pem` must exist (already referenced by both configs).
To regenerate a self-signed pair:

```bash
mkdir -p tls && openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout tls/key.pem -out tls/cert.pem -days 365 -subj "/CN=localhost"
chmod 600 tls/key.pem
```

---

## 1. Self-contained suites (spawn their own server)

These need **no running server** and use private ports, so they're safe to run
while a real instance is up on 1234.

```bash
python3 scripts/test_pubsub.py                    # V8: pub/sub, patterns, channel ACL, notifications
python3 scripts/test_pubsub.py --evict            #   + the eviction-notification hook
python3 scripts/test_security.py --destructive    # ACL, rename-command, audit redaction, protocol abuse
python3 scripts/test_restart_matrix.py --destructive   # RDB/AOF restart matrix, crash recovery
python3 scripts/test_aof_restart.py               # AOF replay across restarts
```

Add `--keep` to any of them to preserve the temp workdir (server stderr, config,
dump files) when debugging a failure.

---

## 2. Live-server suites (plaintext)

Start the server first:

```bash
./build/server myred.conf
```

Then, in another terminal:

```bash
# full run: correctness + concurrency + stress
python3 scripts/stress_test.py --password <PASS>

# faster slices
python3 scripts/stress_test.py --password <PASS> --correctness-only
python3 scripts/stress_test.py --password <PASS> --stress-only \
        --stress-threads 16 --stress-ops 2000

# targeted
python3 scripts/test_async_auth.py --password <PASS>      # async AUTH / ACL, needs --password
python3 scripts/test_memory.py --password <PASS>          # accounting + eviction
```

Shell suites (start their own instances where needed):

```bash
scripts/test_evict_tick.sh        # EVICT_RUNNING incremental-eviction regression
scripts/test_aof.sh
scripts/test_aof_rewrite.sh
scripts/test_aof_hybrid.sh
```

Diagnostics against a live server:

```bash
scripts/diag_live.sh
scripts/diag_ttl.sh
```

---

## 3. TLS

### Handshake sanity

```bash
./build/server myred.conf     # opens 1234 plaintext AND 1235 TLS

openssl s_client -connect 127.0.0.1:1235 -tls1_3 </dev/null 2>&1 | head -20
redis-cli --tls --insecure -p 1235 -a <PASS> ping        # PONG
redis-cli --tls --insecure -p 1235 -a <PASS> set k v
```

Pointing a TLS client at **1234** correctly fails with `wrong version number` —
that's the plaintext port, not a bug.

### Session resumption (V9.7.5)

`s_client -reconnect` gives a **false negative** on TLS 1.3: the ticket is a
post-handshake message and MYRED's protocol is client-speaks-first, so
`</dev/null` closes before the ticket lands. Use explicit session files and
exchange real data:

```bash
{ printf 'AUTH <PASS>\r\n'; sleep 0.5; } | \
  openssl s_client -connect 127.0.0.1:1235 -sess_out /tmp/sess.pem 2>&1 | grep -i "session-id\|new\|reused"

{ printf 'AUTH <PASS>\r\n'; sleep 0.5; } | \
  openssl s_client -connect 127.0.0.1:1235 -sess_in /tmp/sess.pem 2>&1 | grep -i "reused"
```

Second run must report **`Reused, TLSv1.3`**.

### TLS suites — the V9.7 close-out

This is the outstanding carry-over: the recorded 555/551 gate predates the
V9.7.5 flags (`SSL_SESS_CACHE_SERVER` + `SSL_MODE_RELEASE_BUFFERS`), so V9.7
isn't formally closed until both re-run green.

```bash
# authed, over TLS  (expect ~555 checks)
./build/server myred.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password <PASS>

# passwordless, over TLS  (expect ~551 checks)
./build/server bench.conf
python3 scripts/stress_test.py --tls --tls-insecure --port 1337
```

`--tls-insecure` skips certificate verification (self-signed). With a real CA:

```bash
python3 scripts/stress_test.py --tls --tls-ca /path/ca.pem --port 1235 --password <PASS>
# client certs, if tls-auth-clients is yes/optional:
python3 scripts/stress_test.py --tls --tls-ca ca.pem --tls-cert client.pem --tls-key client.key --port 1235
```

---

## 4. Benchmarks

**Release build, `bench.conf`, cool machine, one run at a time.** Back-to-back
runs thermally throttle — a `GET` slower than `SET` is the tell that the numbers
are junk.

```bash
./build/server bench.conf

# plaintext baseline  (port 1336)
python3 scripts/stress_test.py --port 1336 --bench

# TLS baseline  (port 1337)
python3 scripts/stress_test.py --tls --tls-insecure --port 1337 --bench
```

Raw `redis-benchmark` for a single number:

```bash
redis-benchmark -p 1336 -n 100000 -c 50 -P 16 -t set,get
redis-benchmark --tls --insecure -p 1337 -n 100000 -c 50 -P 16 -t set,get
```

Tuning flags: `--bench-requests`, `--bench-clients`, `--bench-pipeline`.

> **Never benchmark an authed instance.** `redis-benchmark`'s 50-client AUTH storm
> hits `k_max_auth_inflight = 4` (an Argon2id ~76 MiB memory bound) and returns
> `BUSY`. That is the KDF working as designed, not a throughput result — which is
> exactly why `bench.conf` is passwordless.

Current recorded baselines live in `planning/ROADMAP.md` → Testing Matrix. The
plaintext figures there were taken warm and are known-low; re-run isolated before
trusting any TLS-vs-plaintext ratio.

---

## 5. Full pre-commit gate

Everything, in dependency order:

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
```

Then a drift check on the Debug build — this is the one that catches memory
accounting bugs the Release build silently tolerates:

```bash
./build-dbg/server myred.conf &
python3 scripts/stress_test.py --password <PASS> --correctness-only
kill %1        # any "[mem] drift" line in stderr is a real bug
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `WRONGPASS` after a `CONFIG REWRITE` | the `requirepass` bug — see §0 |
| `BUSY too many pending AUTH attempts` | AUTH storm vs `k_max_auth_inflight=4`; use a passwordless instance |
| `wrong version number` from a TLS client | pointed at the plaintext port |
| `Server closed the connection` in `redis-cli subscribe` | usually redis-cli exiting on stdin EOF, not a server fault — confirm with `printf ... \| nc` |
| benchmark GET < SET | thermal throttling; let the machine cool and re-run |
| `U_LONGMAX`/`ULLONG_MAX` undefined | GCC 13+ dropped transitive includes; add the explicit header |
| suite fails, need the evidence | re-run with `--keep` and read the temp workdir's `stderr-*.log` |
