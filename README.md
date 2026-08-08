# MYRED — a Redis-like in-memory database in C++

MYRED is a from-scratch, single-threaded, RESP-speaking in-memory key–value
database written in C++. It implements all five core Redis data types — strings,
lists, hashes, sorted sets, and sets — plus key expiry, RDB/AOF persistence,
ACL-backed authentication, runtime config, and memory-limit policies. Because it
speaks the real **RESP protocol**, you can talk to it
with the official `redis-cli` and other Redis clients.

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
  audit log, `rename-command`/disable, control-plane category gating
- **TLS** — optional `tls-port` alongside the plaintext port, with OpenSSL kept a
  private dependency of a single transport translation unit and the handshake
  driven as connection state rather than a blocking `SSL_accept`
- **Pub/Sub:** `SUBSCRIBE`/`PUBLISH`, pattern subscriptions (`PSUBSCRIBE` →
  `pmessage`), channel-scoped ACL (`&pattern`), and Redis-compatible keyspace
  notifications (`notify-keyspace-events`)
- **Transactions:** `MULTI`/`EXEC`/`DISCARD` with error poisoning (`EXECABORT`),
  plus `WATCH`/`UNWATCH` optimistic locking backed by an eager dirty-marking
  watcher registry
- **Replication:** master-replica with `PSYNC` full resync (RDB image + live
  write streaming, reusing the AOF byte stream) and a read-only gate on the
  replica; `REPLICAOF host port` / `REPLICAOF NO ONE`
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

# configure + build
cmake -B build
cmake --build build
```

This produces two binaries in `build/`: `server` and `client`.

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
./build/server

# or load a config file explicitly
./build/server myred.conf
```

Without a config file the server runs open on loopback (protected mode rejects
non-loopback peers when no password is set). Set `requirepass` in the config to
require `AUTH`; the historical dev password is `kek1234`. Set `MYRED_PASSWORD`,
`MYRED_PORT`, `MYRED_AOF`, `MYRED_CONFIG`, `MYRED_MAXMEMORY`, or
`MYRED_MAXMEMORY_POLICY` to override common settings at startup.

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

### With the bundled client

```bash
REDIS_PASSWORD=kek1234 ./build/client set foo bar      # single command
REDIS_PASSWORD=kek1234 ./build/client                  # interactive REPL
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

### Pub/Sub
`SUBSCRIBE`, `UNSUBSCRIBE`, `PSUBSCRIBE`, `PUNSUBSCRIBE`, `PUBLISH`

### Transactions
`MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`

### Replication
`REPLICAOF` (`SLAVEOF`), `REPLCONF`, `PSYNC`

### Admin / connection
`AUTH`, `ACL`, `PING`, `ECHO`, `INFO`, `CONFIG`, `MEMORY`, `OBJECT`,
`SAVE`, `BGSAVE`, `BGREWRITEAOF`

Command names are case-insensitive. Besides RESP framing, plain-text **inline
commands** are accepted (newline-terminated), and empty inline lines are ignored
— so `redis-cli --pipe` bulk loading works end to end.

## Testing

A Python test harness (`scripts/stress_test.py`) speaks RESP directly over a
socket and covers command correctness (including protocol edge cases like inline
commands), auth/ACL behavior, persistence checks, memory accounting, maxmemory +
incremental eviction, concurrent writes, and a randomized stress run. It prints
per-section timings, command latency percentiles, command mix, and slowest
operation tables. Drop `--password` if the server runs without one.

```bash
# server must be running in another terminal
python3 scripts/stress_test.py --password kek1234

# correctness only
python3 scripts/stress_test.py --password kek1234 --correctness-only

# writes a shareable log, named per transport+mode (docs/stress_results_plain.md,
# docs/bench_tls.md, ...) so a TLS run never overwrites the plaintext one
python3 scripts/stress_test.py --password kek1234 --log run.md

# stress only, with a larger worker/operation count
python3 scripts/stress_test.py --password kek1234 --stress-only --stress-threads 16 --stress-ops 2000

# + redis-benchmark speed baseline (per-test invocations and timeouts)
python3 scripts/stress_test.py --password kek1234 --bench
```

> Note: the Python harness measures correctness/concurrency, not raw throughput
> (it's bound by synchronous round-trips — under the concurrent stress phase,
> per-command latencies include queueing behind O(N) commands like `KEYS`, not
> just server time). For real numbers use `--bench` or `redis-benchmark`
> directly, and **benchmark a Release build only**
> (`cmake -B build -DCMAKE_BUILD_TYPE=Release`): a Debug build runs a
> whole-keyspace memory audit after every command.

Additional helpers in `scripts/`:

```bash
scripts/test_evict_tick.sh      # incremental eviction regression (EVICT_RUNNING)
scripts/test_aof_restart.py     # AOF replay across restarts (incl. ACL identity)
scripts/test_async_auth.py      # async Argon2id AUTH / ACL suite
scripts/test_memory.py          # focused memory/eviction checks
scripts/test_aof.sh
scripts/test_aof_rewrite.sh
scripts/test_aof_hybrid.sh
scripts/diag_live.sh
scripts/diag_ttl.sh
```

## Project structure

```
server.cpp         event loop + main()
state.*            Entry, the global DB, constants, entry lifecycle, clocks
resp.*             RESP request parser + response writers
commands.*         command handlers + dispatch
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
                   auth/ACL, eviction, and diagnostic helpers
docs/planning/     ROADMAP (progress), BACKLOG (future work + open bugs),
                   DECISIONS (design + architecture), CODE_REVIEW (bug audit)
docs/TESTING.md    testing runbook: every command, TLS, benchmarks, commit gate
docs/*.md          test-run logs (bench_plain, bench_tls, stress_tls, ...)
```

## Status and what's next

Recently completed (see `docs/planning/ROADMAP.md` for detail):

- **V9 — Security and auth** *(done)*: config file, Argon2id credentials with
  async verification, protected mode + CIDR allowlist, ACLs with key patterns,
  command hardening + audit log, and **TLS** — a transport seam keeps OpenSSL a
  private dependency of one translation unit, with the handshake driven as
  connection state rather than a blocking `SSL_accept`.
- **V8 — Pub/Sub** *(done)*: `SUBSCRIBE`/`PUBLISH`, pattern subscriptions
  (`PSUBSCRIBE` → `pmessage`), channel-scoped ACL (`&pattern`), and
  Redis-compatible keyspace notifications (`notify-keyspace-events`). Needed zero
  event-loop changes — the poll loop already rebuilds its flags every tick.

- **V8 — Transactions** *(done)*: `MULTI`/`EXEC`/`DISCARD` plus `WATCH`/`UNWATCH`.
  Atomicity came free from the single-threaded event loop, so the work was the
  per-connection state machine and reply framing. All five commands dispatch
  *above* the queueing gate, which is what makes them unqueueable and `EXEC`'s
  recursion safe without a depth guard.
- **V9.8 — Config directive table** *(done)*: `config_apply`, `config_get_value`
  and `config_rewrite` used to hand-enumerate the same ~23 directives, and four
  separate incidents came from editing one list and forgetting another — including
  a `CONFIG REWRITE` that dropped `requirepass` and brought the server back
  passwordless. All three are now walks over one table, with a boot self-check on
  its shape.

- **V10 — Replication** *(in progress)*: `PSYNC` full resync on both master and
  replica sides (RDB image + live write streaming, reusing the AOF byte stream
  as the replication stream), plus a read-only gate on the replica. What's left
  is closing a restart-safety gap (a restarted replica must come back
  read-only, not as a writable master) and, later, partial resync and `WAIT`.

Next up:

- Finishing V10 Replication, then the pick-your-adventure upgrade catalog —
  both scoped in `docs/planning/BACKLOG.md` / `docs/planning/ROADMAP.md`.
- **No open bugs in the data path** (two low-severity, deliberately-deferred
  items tracked in `docs/planning/BACKLOG.md` → Open Bugs).

## Acknowledgements

Built following **[build-your-own.org/redis](https://build-your-own.org/redis/)**,
then extended with all five data types, a full generic keyspace command suite,
`fork()`-based persistence, cursor-based `SCAN`/`HSCAN`/`SSCAN`, and a CMake build.
