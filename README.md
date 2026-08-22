# MYRED — a Redis-like in-memory database in C++

MYRED is a from-scratch, single-threaded, RESP-speaking in-memory key–value
database written in C++. It implements all five core Redis data types — strings,
lists, hashes, sorted sets, and sets — plus key expiry, RDB/AOF persistence,
ACL-backed authentication, TLS, master-replica replication with coordinated
failover, transactions, pub/sub, and runtime config. Because it speaks the real
**RESP protocol**, you can talk to it with the official `redis-cli` and other
Redis clients.

> **Foundation:** this project is built on the excellent guide at
> **https://build-your-own.org/redis/** — the book provides the core event-loop,
> hashtable, AVL/zset and protocol foundations that MYRED extends.

It's a learning project: every core data structure (hashtable, AVL tree,
min-heap, ring-buffer deque, intrusive lists) is implemented by hand rather
than using the STL containers.

---

## Features

- **RESP protocol** — works with `redis-cli` and standard Redis client libraries
- **All 5 data types:** strings, lists, hashes, sorted sets, sets
- **Key expiry (TTL):** `EXPIRE`/`PEXPIRE`/`EXPIREAT`/`PEXPIREAT`/`TTL`/`PTTL`/`PERSIST`, active + lazy expiration
- **Persistence:** custom RDB snapshots plus append-only-file (AOF) replay, hybrid AOF rewrite, and CRC32-protected RDB payloads
- **`fork()`-based background work:** `BGSAVE` and `BGREWRITEAOF` keep the parent serving clients
- **Authentication and ACLs:** `AUTH`, named users, command/category rules, and
  key-pattern checks; passwords stored as **Argon2id** (legacy SHA-256 still
  verifies), with verification offloaded to a thread pool so the event loop never
  blocks on a hash
- **Security hardening:** protected mode, multi-address `bind` + IP allowlist,
  a `maxclients` cap enforced at accept time (clamped to fit `RLIMIT_NOFILE` at
  boot so it can never be exceeded by accident), an escaped audit log (every
  field is delimiter-safe, so attacker-controlled input like an `AUTH` username
  can't forge a fake log line), `rename-command`/disable, and control-plane
  category gating
- **TLS** — optional `tls-port` alongside the plaintext port, with OpenSSL kept a
  private dependency of a single transport translation unit (`transport.cpp`)
  and the handshake driven as connection state (bounded by
  `tls-handshake-timeout`) rather than a blocking `SSL_accept`; certificates can
  be rotated live via `CONFIG SET` with no restart and no dropped connections
- **Pub/Sub:** `SUBSCRIBE`/`PUBLISH`, pattern subscriptions (`PSUBSCRIBE` →
  `pmessage`), channel-scoped ACL (`&pattern`), and Redis-compatible keyspace
  notifications (`notify-keyspace-events`)
- **Transactions:** `MULTI`/`EXEC`/`DISCARD` with error poisoning (`EXECABORT`),
  plus `WATCH`/`UNWATCH` optimistic locking backed by an eager dirty-marking
  watcher registry
- **Replication and failover:** master-replica with `PSYNC` full **and partial**
  resync (RDB image + live write streaming, reusing the AOF byte stream),
  automatic reconnect after a silent/dropped link, a read-only gate on the
  replica, `WAIT` as a durability barrier, a `min-replicas-*` write floor, and
  coordinated `FAILOVER` (pauses writes, hands over cleanly, loses nothing).
  What's *not* here yet: failover is operator-triggered, not automatic — there
  is no unattended, Sentinel-style election if a master silently dies
- **Runtime configuration:** config file, selected environment overrides,
  `CONFIG GET`/`CONFIG SET`, and `CONFIG REWRITE` — every directive is one row in
  a single table owning its arity, parser, getter and on-disk form, checked for
  self-consistency at boot
- **Memory management:** approximate memory accounting, `maxmemory` policies with
  **incremental eviction** (bounded batches continued across event-loop ticks —
  Redis `EVICT_RUNNING` semantics, so an overshoot never spuriously OOMs writes),
  eviction stats, `MEMORY`, and `OBJECT`
- **Cursor iteration** — non-blocking `SCAN`/`HSCAN`/`SSCAN` with `MATCH`/`COUNT` (glob patterns)
- **Generic keyspace commands** — `DBSIZE`, `RANDOMKEY`, `RENAME`/`RENAMENX`, `TOUCH`, `UNLINK`, `FLUSHALL`
- **Single-threaded event loop** (`poll`, non-blocking I/O) with `TCP_NODELAY`
- **Thread pool** for offloading large async deletions (`UNLINK`)
- **Regression suite:** one command spins up its own server and runs 1000+
  checks across unit, memory, config, auth, security, persistence, TLS, and
  replication phases in under two minutes — see Testing below. A differential
  pass against real `redis-server`, fuzzing, and a sanitizer build are active
  work, not yet landed.

### Known gaps

Worth knowing before you point a real application at this: there is **no
multiple-database support** (`SELECT`/`SWAPDB` — everything lives in db0), **no
`HELLO`/RESP3 handshake** or `CLIENT` command family, **no scripting**
(`EVAL`/Lua — a custom bytecode VM is designed but not built), and **no
cluster/sharding**. It also only builds and runs on Linux today (WSL2 and
native both tested) — there is no Windows port. None of these are secret; they
are scoped and tracked in `docs/planning/BACKLOG.md`.

## Architecture

- **Event loop:** a single-threaded `poll()` loop with non-blocking sockets handles
  all clients; one command is processed at a time, so the core needs no locks.
- **Storage:** the database is one big hashtable mapping `key → Entry`. Each `Entry`
  holds one value whose type tag is `T_STR=1`, `T_ZSET=2`, `T_DLIST=3`, `T_HASH=4`, or `T_SET=5`.
- **Expiry:** TTLs are tracked in a min-heap ordered by expiry time (active reaping),
  plus lazy expiration on read. On disk, TTLs are stored as wall-clock time so they
  survive restarts.
- **Persistence:** `SAVE` writes synchronously; `BGSAVE` and the periodic auto-save
  `fork()` a child that serializes a copy-on-write snapshot while the parent keeps
  serving requests. When AOF is enabled, writes are appended as RESP frames; rewrite
  compacts the log into an RDB preamble plus a RESP tail.
- **Networking:** plaintext and TLS connections share one non-blocking transport
  interface (`transport.cpp`); a TLS handshake is driven forward on the same
  `poll()` ticks as ordinary reads instead of blocking the event loop.

### Data structures (all hand-written)

| Structure | File | Used for |
|---|---|---|
| Hash table (progressive rehashing) | `hashtable.*` | the keyspace, hash fields, set members, zset member index |
| AVL tree | `avl.*` | sorted-set ordering / ranking |
| Sorted set (AVL + hashtable) | `zset.*` | the `ZSET` type |
| Ring-buffer deque | `deque.*` | the `LIST` type |
| Hash node map | `hash.*` | the `HASH` type (field → value) |
| Set node map | `set.*` | the `SET` type (members only, value-less HMap) |
| Min-heap | `heap.*` | TTL expiry |
| Intrusive doubly-linked list | `list.h` | connection idle/IO timeout queues |

## Building

Requires a C++20 compiler, **CMake ≥ 3.16**, **zlib**, and **pthreads**.

```bash
# required deps (Debian/Ubuntu/WSL)
sudo apt install build-essential cmake zlib1g-dev

# optional deps - see the table below
sudo apt install libssl-dev libargon2-dev

# a debug build for day-to-day dev/test work
cmake -B build
cmake --build build

# a release build for anything you'll benchmark or run for real
cmake -B build-rel -DCMAKE_BUILD_TYPE=Release
cmake --build build-rel -j
```

This produces two binaries per build directory: `server` and `client`.
**Only benchmark the Release build.** A Debug build runs a whole-keyspace
memory self-check after every single command, so its latency/throughput
numbers do not reflect the server's real performance.

### Optional dependencies

Both are detected at configure time and **compile out cleanly when absent** — the
build succeeds either way, and CMake prints a summary line
(`MYRED build: TLS=... Argon2id=...`) plus a warning naming the missing package.

| Dependency | Package | Present | Absent |
|---|---|---|---|
| OpenSSL | `libssl-dev` | `tls-port` works | plaintext only; **setting `tls-port` refuses to boot** |
| libargon2 | `libargon2-dev` | new passwords hashed with Argon2id | new passwords fall back to SHA-256; existing `$argon2id$` credentials **cannot be verified** |

Force a feature off even when the library is installed with
`-DMYRED_TLS=OFF` / `-DMYRED_ARGON2=OFF`.

zlib is **not** optional: compression is part of the on-disk RDB format, so a
build without it could not read snapshots written by a build with it.

> Building without OpenSSL means `myred.conf` and `bench.conf` will not boot as
> shipped — both set `tls-port`. Comment out their `tls-port` / `tls-cert-file` /
> `tls-key-file` lines, or run `./build/server` with no config file.

## Running

```bash
# start the server (listens on port 1234; run from the project root so it finds dump.rdb)
./build-rel/server

# or load a config file explicitly
./build-rel/server myred.conf
```

Without a config file the server runs open on loopback (protected mode rejects
non-loopback peers when no password is set). Set `requirepass` in the config to
require `AUTH`; the historical dev password is `kek1234`.

Common settings can also be overridden at startup via environment variable:
`MYRED_CONFIG`, `MYRED_PASSWORD`, `MYRED_PORT`, `MYRED_AOF`, `MYRED_SAVE`,
`MYRED_AOF_FSYNC`, `MYRED_AOF_REWRITE_MIN`, `MYRED_AOF_REWRITE_PERC`,
`MYRED_MAXMEMORY`, `MYRED_MAXMEMORY_POLICY`.

### With `redis-cli`

Because MYRED speaks RESP, the official Redis CLI works directly:

```bash
redis-cli -p 1234 -a kek1234 set foo bar
redis-cli -p 1234 -a kek1234 get foo
redis-cli -p 1234 -a kek1234 sadd myset a b c
redis-cli -p 1234 -a kek1234 smembers myset
redis-cli -p 1234 -a kek1234 hset user:1 name alice age 30
redis-cli -p 1234 -a kek1234 hgetall user:1
redis-cli -p 1234 -a kek1234 scan 0 match 'user:*'
```

Or interactively:

```bash
redis-cli -p 1234 -a kek1234
127.0.0.1:1234> rpush mylist a b c
127.0.0.1:1234> lrange mylist 0 -1
127.0.0.1:1234> sadd tags redis cpp database
127.0.0.1:1234> sinter tags othertags
```

> A general-purpose Redis client library (redis-py, ioredis, Jedis, go-redis,
> ...) has not been validated against MYRED yet — only `redis-cli`,
> `redis-benchmark`, and this project's own raw-socket test harness have. Plain
> `GET`/`SET`/hash/list/set/sorted-set traffic over RESP2 should work; anything
> that leans on `HELLO`/RESP3, multiple databases, or `EVAL` will not, per
> Known gaps above.

### With the bundled client

```bash
REDIS_PASSWORD=kek1234 ./build-rel/client set foo bar      # single command
REDIS_PASSWORD=kek1234 ./build-rel/client                  # interactive REPL
```

## Supported commands

### Strings
`GET`, `SET`, `DEL key [key...]`, `EXISTS key [key...]`
`INCR`, `DECR`, `INCRBY`, `DECRBY`, `INCRBYFLOAT`
`SETNX`, `SETEX`, `PSETEX`, `GETSET`, `GETEX`, `GETDEL`
`MSET`, `MGET`, `MSETNX`
`APPEND`, `STRLEN`, `GETRANGE`, `SETRANGE`

### Generic / keyspace
`TYPE`, `EXPIRE`, `PEXPIRE`, `EXPIREAT`, `PEXPIREAT`, `TTL`, `PTTL`, `PERSIST`,
`KEYS`, `SCAN`, `DBSIZE`, `RANDOMKEY`, `RENAME`, `RENAMENX`, `TOUCH`,
`UNLINK`, `FLUSHALL`, `FLUSHDB`

### Hashes
`HSET`, `HGET`, `HDEL`, `HEXISTS`, `HLEN`, `HGETALL`, `HKEYS`, `HVALS`,
`HMGET`, `HSETNX`, `HINCRBY`, `HSTRLEN`, `HSCAN`

### Lists
`LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LLEN`, `LINDEX`, `LRANGE`,
`LSET`, `LINSERT`, `LREM`, `LTRIM`

### Sets
`SADD`, `SREM`, `SISMEMBER`, `SMISMEMBER`, `SCARD`, `SMEMBERS`,
`SPOP`, `SRANDMEMBER`, `SSCAN`,
`SINTER`, `SUNION`, `SDIFF`,
`SINTERSTORE`, `SUNIONSTORE`, `SDIFFSTORE`, `SMOVE`

### Sorted sets
`ZADD`, `ZREM`, `ZSCORE`, `ZRANK`, `ZQUERY`, `ZREVQUERY`, `ZPOPMIN`

This is a functional subset, not the full Redis zset surface — `ZCARD`,
`ZINCRBY`, `ZRANGEBYSCORE`, `ZUNIONSTORE`, `ZSCAN` and friends are tracked as a
gap in `docs/planning/BACKLOG.md`, not silently missing.

### Pub/Sub
`SUBSCRIBE`, `UNSUBSCRIBE`, `PSUBSCRIBE`, `PUNSUBSCRIBE`, `PUBLISH`

### Transactions
`MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`

### Replication
`REPLICAOF` (`SLAVEOF`), `REPLCONF`, `PSYNC`, `WAIT`, `FAILOVER`

### Admin / connection
`AUTH`, `ACL`, `PING`, `ECHO`, `INFO`, `CONFIG`, `MEMORY`, `OBJECT`,
`SAVE`, `BGSAVE`, `BGREWRITEAOF`

Command names are case-insensitive. Besides RESP framing, plain-text **inline
commands** are accepted (newline-terminated), and empty inline lines are ignored
— so `redis-cli --pipe` bulk loading works end to end.

## Testing

**`scripts/stress_test.py` is the whole regression suite**, and one command
runs it — no server needs to be started first, it manages its own:

```bash
# the full suite against a Release build: 1000+ checks in well under two minutes
python3 scripts/stress_test.py --server build-rel/server --destructive --bench

# the TLS-aware run
python3 scripts/stress_test.py --server build-rel/server --tls

# see what a run covers before running it
python3 scripts/stress_test.py --list-phases
```

`--server` is what turns on process-management: it spawns a private instance in
a temp directory, drives it through eight phases — `unit`, `memory`, `config`,
`auth`, `security`, `persistence`, `tls`, `replication` — and tears it down
after, so nothing it touches belongs to anyone and nothing has to be running
beforehand. `--phases` selects a subset.

```bash
# against a server you already started yourself
python3 scripts/stress_test.py --password kek1234

# TLS, against your own instance
python3 scripts/stress_test.py --tls --tls-insecure --port 1235 --password kek1234

# TLS metrics (measurement, not pass/fail) — handshake cost, accept-storm
# behavior, redis-benchmark throughput, cert-rotation latency
python3 scripts/test_tls.py --server build-rel/server
```

Results are filed under `docs/logs/<WSL|Native>/` — the environment is detected
from the kernel rather than passed as a flag, because WSL2 and native Linux
throughput numbers are not comparable and mixing them into one file was a real
mistake this project made once. `--compare A.json B.json` diffs two runs.

> Only benchmark a Release build. `build/`'s Debug binary runs a whole-keyspace
> memory audit after every command, which `test_tls.py` refuses to measure.

Additional helpers in `scripts/`:

```bash
scripts/test_evict_tick.sh      # incremental eviction regression (EVICT_RUNNING)
scripts/test_aof_restart.py     # AOF replay across restarts (incl. ACL identity)
scripts/test_async_auth.py      # async Argon2id AUTH / ACL suite
scripts/test_memory.py          # focused memory/eviction checks
scripts/test_replication.py     # replication + failover (ported into stress_test.py's `replication` phase)
scripts/test_security.py        # ACL, auth, protected-mode, audit-log checks
scripts/test_aof.sh
scripts/test_aof_rewrite.sh
scripts/test_aof_hybrid.sh
scripts/diag_live.sh
scripts/diag_ttl.sh
```

The old per-topic scripts are still on disk and still runnable, but nothing in
`stress_test.py` depends on them any more — it is one tracked file with no
local-only dependency.

**Not built yet, tracked as active work in `docs/planning/ROADMAP.md` → V11:** a
differential harness comparing replies against a real `redis-server`,
libFuzzer/AFL fuzzing of the RESP parser and RDB loader, an ASan/UBSan build,
and a static security review. All scoped, none landed.

## Project structure

```
server.cpp         event loop + main()
state.*            Entry, the global DB, constants, entry lifecycle, clocks
resp.*             RESP request parser + response writers
commands.*         command handlers + dispatch
transport.*        plaintext/TLS socket I/O behind one non-blocking interface
buffer.*           per-connection growable byte buffer
hashtable.*        the core hash table (dual-table progressive rehashing)
zset.* / avl.*     sorted set + AVL tree
deque.*            ring-buffer deque (lists)
hash.*             hash fields (HashNode: field + value)
set.*              set members (SetNode: member only, no value)
heap.*             TTL min-heap
thread_pool.*      background worker pool
list.h             intrusive list (connection timers)
common.h           container_of, FNV hash
client.cpp         a small RESP client (single-shot + REPL)
cred.* / sha256.*  Argon2id/SHA-256 credential hashing + verification
aof.* / rdb.*      append-only-file and RDB snapshot persistence
myred.conf         example server configuration
scripts/           all tests: stress_test.py (primary harness), persistence,
                   auth/ACL, replication, TLS, and diagnostic helpers
docs/planning/     ROADMAP (progress), BACKLOG (future work + open bugs),
                   DECISIONS (design + architecture), CODE_REVIEW (bug audit)
docs/TESTING.md    testing runbook: every command, TLS, benchmarks, commit gate
docs/logs/         current per-environment (WSL|Native) test-run logs/baselines
docs/*.md          older test-run logs, superseded in part by docs/logs/
```

## Status and what's next

See `docs/planning/ROADMAP.md` for the authoritative, actively-maintained
detail — this is a summary.

**Done:**

- **V9 — Security and auth:** config file, Argon2id credentials with async
  verification, protected mode + CIDR allowlist, ACLs with key patterns,
  command hardening, and an escaped audit log.
- **V9.7 — TLS:** a transport seam keeps OpenSSL a private dependency of one
  translation unit, with the handshake driven as connection state; live
  certificate rotation shipped later in V10.6.1c.
- **V8 — Pub/Sub and Transactions:** channel/pattern pub/sub with keyspace
  notifications; `MULTI`/`EXEC`/`WATCH` with atomicity free from the
  single-threaded event loop.
- **V9.8 — Config directive table:** `CONFIG GET`/`SET`/`REWRITE` unified onto
  one self-checking table, closing a class of bug that had already shipped a
  passwordless-server regression once.
- **V10 — Replication and coordinated failover:** full and partial `PSYNC`
  resync, automatic reconnect, a read-only replica gate, `WAIT`,
  `min-replicas-*`, and coordinated `FAILOVER`. Automatic (unattended,
  Sentinel-style) failover is deliberately **not** in this milestone — it's
  parked until there's a regression suite that can prove it, which V11 builds.
- **V10.6.1 — TLS optimization pass:** measured three deferred ideas; shipped
  live cert reload, reverted a bounded-accept-queue change that didn't clear
  the noise floor, declined kTLS on the arithmetic.

**Active — V11, Testing Hardening:** a unified regression harness
(`scripts/stress_test.py --server ...`, 1000+ checks, 8 phases) is done. What's
left: a differential harness against real `redis-server`, fuzzing the RESP
parser and RDB loader, an ASan/UBSan build, and a static security review —
all scoped in `docs/planning/ROADMAP.md`, none executed yet.

**Scoped but not started:** automatic/Sentinel-style failover (V10.6e),
cluster/hash-slot sharding (V12), a Windows port (POSIX→Win32 translation is
fully scoped in `docs/planning/BACKLOG.md` with concrete difficulty rankings),
and `EVAL` scripting (custom bytecode VM, designed but not built).

**No open bugs in the data path.** What's tracked in
`docs/planning/BACKLOG.md` → Open Bugs is a hardening follow-up, a latent
scheduling gap, an observability wart, and a deliberate protocol divergence —
none of them can corrupt or lose data.

## Acknowledgements

Built following **[build-your-own.org/redis](https://build-your-own.org/redis/)**,
then extended with all five data types, a full generic keyspace command suite,
`fork()`-based persistence, cursor-based `SCAN`/`HSCAN`/`SSCAN`, TLS, ACLs,
pub/sub, transactions, replication with coordinated failover, and a CMake build.
