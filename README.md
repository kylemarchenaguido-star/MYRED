# MYRED — a Redis-like in-memory database in C++

> **Project status: paused (2026-10-05).** This is the last version that is being
> worked on for now. The server is on hold and is not expected to be developed
> again for a long time. What is described below is exactly what `main` does today;
> nothing here is a promise of future work. See [Status](#status-and-whats-next)
> for what is finished, what is known to be missing, and why.

MYRED is a from-scratch, single-threaded, RESP-speaking in-memory key–value
database written in C++. It implements all five core Redis data types — strings,
lists, hashes, sorted sets, and sets — plus key expiry, RDB/AOF persistence,
ACL-backed authentication, TLS, master-replica replication with coordinated
failover, transactions, pub/sub, RESP3, and runtime config. Because it speaks the
real **RESP protocol**, you can talk to it with the official `redis-cli` and with
Redis client libraries (stock `redis-py` is verified, see below).

> **Foundation:** this project is built on the excellent guide at
> **https://build-your-own.org/redis/** — the book provides the core event-loop,
> hashtable, AVL/zset and protocol foundations that MYRED extends.

It's a learning project: every core data structure (hashtable, AVL tree,
min-heap, ring-buffer deque, intrusive lists) is implemented by hand rather
than using the STL containers.

---

## What `main` contains

`main` is **code only**: the server and client sources under `src/`, the CMake
build, example configurations under `conf/`, and the documentation under `docs/`.
The regression suite, the stress and benchmark tooling and their logs live on the
separate **`test`** branch and are not part of `main` (see
[Branches](#branches)). Everything in this README is something you can do with
`main` alone: build it, run it, and talk to it with `redis-cli` or a client library.

## Features

- **RESP2 and RESP3** — `HELLO 2|3` negotiates the protocol per connection
  (including `HELLO 3 AUTH <user> <pass> SETNAME <name>`). Under RESP3 replies use
  the real types: maps for `HGETALL`/`CONFIG GET`/`HELLO`, doubles for sorted-set
  scores, a proper null, `>` push messages for pub/sub, and a verbatim string for
  `INFO`. A minimal `CLIENT` subset (`ID`, `GETNAME`, `SETNAME`, `SETINFO`) is
  there because client libraries send it on connect.
- **Works with stock client libraries** — `redis-py` 8.1.0 was run against it over
  both RESP2 and RESP3 and gave the same results as a real Redis 7.0 for `SET`
  with options, the sorted-set range commands, pipelines and pub/sub (see
  [Using it from a client library](#using-it-from-a-client-library))
- **All 5 data types:** strings, lists, hashes, sorted sets, sets
- **Key expiry (TTL):** `EXPIRE`/`PEXPIRE`/`EXPIREAT`/`PEXPIREAT`/`TTL`/`PTTL`/`PERSIST`, active + lazy expiration
- **`SET` with its options:** `NX`, `XX`, `GET`, `EX`, `PX`, `EXAT`, `PXAT`, `KEEPTTL`
  — the cache and session idiom (`SET key value EX 3600`). Logged to the AOF and to
  replicas as a single frame carrying an absolute deadline, so a restart does not
  restart the TTL
- **A real sorted-set read surface:** `ZRANGE` (with `REV`, `BYSCORE`, `LIMIT`,
  `WITHSCORES`), `ZREVRANGE`, `ZRANGEBYSCORE`, `ZREVRANGEBYSCORE`, `ZCOUNT`,
  `ZCARD`, `ZINCRBY`, `ZRANK`/`ZREVRANK`, `ZREMRANGEBYSCORE`, `ZREMRANGEBYRANK`.
  Range queries are O(log n): they reduce to rank arithmetic on the size-augmented
  AVL tree, so leaderboards, sliding-window rate limiters and schedulers work
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
  private dependency of a single transport translation unit (`src/server/transport.cpp`)
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
  Failover is operator-triggered, not automatic — there is no unattended,
  Sentinel-style election if a master silently dies
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

In total the server answers **124 command names** (listed under
[Supported commands](#supported-commands)).

### Known gaps

Worth knowing before you point a real application at this. Each is a decision,
not an oversight; the larger ones are scoped in `docs/planning/BACKLOG.md`.

- **One database only.** `SELECT` and `SWAPDB` do not exist: everything lives in
  db0. (Clients that never select a non-zero database are unaffected.)
- **No scripting.** No `EVAL`/`EVALSHA`/`SCRIPT`.
- **No cluster or sharding**, and **no automatic failover**.
- **Not implemented as commands:** `QUIT`, `RESET`, `COMMAND`, `DEBUG`,
  `SLOWLOG`, `SHUTDOWN`, `LASTSAVE`, blocking list operations (`BLPOP`/`BRPOP`/
  `BLMOVE`), streams (`XADD` family), geospatial (`GEO*`), HyperLogLog (`PF*`),
  bit operations, `SORT`, `COPY`, `DUMP`/`RESTORE`, `LPOS`. Unknown commands
  answer `-ERR unknown command`. Clients close the socket instead of sending `QUIT`.
- **A subset of the sorted-set commands.** Missing: `ZADD` options
  (`NX`/`XX`/`GT`/`LT`/`CH`/`INCR`), `ZPOPMAX`, `ZMSCORE`, `ZRANDMEMBER`,
  `ZRANGESTORE`, `ZUNIONSTORE`/`ZINTERSTORE`/`ZDIFF*`, `ZSCAN`, `ZLEXCOUNT`, and
  `ZRANGE ... BYLEX` (refused with an error rather than misread).
- **`CLIENT` is a subset:** `ID`, `GETNAME`, `SETNAME`, `SETINFO` only (no
  `CLIENT LIST`/`KILL`).
- **Small, known divergences from Redis 7.0:** the arity error is
  `ERR wrong number of arguments` without the `for '<cmd>' command` suffix;
  `SET ... EX` emits the `set` keyspace event but not Redis's extra `expire`
  event; `SET key v EXAT <past>` removes the key immediately, where Redis 7.0
  leaves a logically-dead key that `DBSIZE` still counts until it is touched.
- **Linux only.** It builds and runs on Linux (WSL2 and native both tested);
  there is no Windows port (the POSIX→Win32 translation is scoped in
  `docs/planning/BACKLOG.md`).

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
  compacts the log into an RDB preamble plus a RESP tail. Commands whose request
  cannot be replayed verbatim (`SETEX`, `EXPIRE`, `SET` with options, ...) are
  logged as their effect, with absolute deadlines.
- **Sorted sets:** an AVL tree ordered by `(score, member)` whose nodes carry
  subtree sizes, plus a hashtable from member to node. Rank,
  `ZCOUNT` and every range command are O(log n) descents; nothing is re-sorted.
- **Networking:** plaintext and TLS connections share one non-blocking transport
  interface (`src/server/transport.cpp`); a TLS handshake is driven forward on the same
  `poll()` ticks as ordinary reads instead of blocking the event loop.

### Data structures (all hand-written)

| Structure | File | Used for |
|---|---|---|
| Hash table (progressive rehashing) | `src/ds/hashtable.*` | the keyspace, hash fields, set members, zset member index |
| AVL tree | `src/ds/avl.*` | sorted-set ordering / ranking |
| Sorted set (AVL + hashtable) | `src/types/zset.*` | the `ZSET` type |
| Ring-buffer deque | `src/ds/deque.*` | the `LIST` type |
| Hash node map | `src/types/hash.*` | the `HASH` type (field → value) |
| Set node map | `src/types/set.*` | the `SET` type (members only, value-less HMap) |
| Min-heap | `src/ds/heap.*` | TTL expiry |
| Intrusive doubly-linked list | `src/ds/list.h` | connection idle/IO timeout queues |

## Building

Requires a C++20 compiler, **CMake ≥ 3.16**, **zlib**, and **pthreads**.

```bash
# required deps (Debian/Ubuntu/WSL)
sudo apt install build-essential cmake zlib1g-dev

# optional deps - see the table below
sudo apt install libssl-dev libargon2-dev

# a debug build for day-to-day dev work
cmake -B build
cmake --build build

# a release build for anything you'll run for real
cmake -B build-rel -DCMAKE_BUILD_TYPE=Release
cmake --build build-rel -j
```

This produces two binaries per build directory: `server` and `client`.
**Only use the Release build day to day.** A Debug build runs a whole-keyspace
memory self-check after every single command, so its latency does not reflect
the server's real performance.

If you use clangd, ask CMake for a compile database so it knows the include
root (`src/`): `cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`.

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

> Building without OpenSSL means `conf/myred.conf` and `conf/bench.conf` will not boot as
> shipped — both set `tls-port`. Comment out their `tls-port` / `tls-cert-file` /
> `tls-key-file` lines, or run `./build/server` with no config file.

## Running

```bash
# start the server (listens on port 1234; run from the project root so it finds dump.rdb)
./build-rel/server

# or load a config file explicitly
./build-rel/server conf/myred.conf
```

Without a config file the server runs open on loopback (protected mode rejects
non-loopback peers when no password is set). Set `requirepass` in the config to
require `AUTH`; the historical dev password is `kek1234`.

Common settings can also be overridden at startup via environment variable:
`MYRED_CONFIG`, `MYRED_PASSWORD`, `MYRED_PORT`, `MYRED_AOF`, `MYRED_SAVE`,
`MYRED_AOF_FSYNC`, `MYRED_AOF_REWRITE_MIN`, `MYRED_AOF_REWRITE_PERC`,
`MYRED_MAXMEMORY`, `MYRED_MAXMEMORY_POLICY`.

### Configuration directives

All of these can be set in a config file; most can also be changed at runtime
with `CONFIG SET` and persisted with `CONFIG REWRITE`. Examples are in `conf/`.

| Area | Directives |
|---|---|
| Network | `port`, `bind`, `protected-mode`, `allow-ip`, `maxclients` |
| TLS | `tls-port`, `tls-cert-file`, `tls-key-file`, `tls-ca-cert-file`, `tls-auth-clients`, `tls-handshake-timeout` |
| Auth and ACL | `requirepass`, `user`, `rename-command`, `auditlog` |
| Persistence | `dbfilename`, `save`, `appendonly`, `appendfilename`, `appendfsync`, `auto-aof-rewrite-percentage`, `auto-aof-rewrite-min-size` |
| Memory | `maxmemory`, `maxmemory-policy`, `maxmemory-samples` |
| Events | `notify-keyspace-events` |
| Replication | `replicaof`, `masterauth`, `repl-backlog-size`, `repl-timeout`, `repl-ping-replica-period`, `min-replicas-to-write`, `min-replicas-max-lag` |

### A simple test with `redis-cli`

Because MYRED speaks RESP, the official Redis CLI works directly:

```bash
redis-cli -p 1234 -a kek1234 set foo bar
redis-cli -p 1234 -a kek1234 set session:42 data ex 3600
redis-cli -p 1234 -a kek1234 get foo
redis-cli -p 1234 -a kek1234 sadd myset a b c
redis-cli -p 1234 -a kek1234 smembers myset
redis-cli -p 1234 -a kek1234 hset user:1 name alice age 30
redis-cli -p 1234 -a kek1234 hgetall user:1
redis-cli -p 1234 -a kek1234 zadd board 10 alice 20 bob 15 carol
redis-cli -p 1234 -a kek1234 zrange board 0 -1 withscores
redis-cli -p 1234 -a kek1234 scan 0 match 'user:*'
```

Or interactively:

```bash
redis-cli -p 1234 -a kek1234
127.0.0.1:1234> rpush mylist a b c
127.0.0.1:1234> lrange mylist 0 -1
127.0.0.1:1234> sadd tags redis cpp database
127.0.0.1:1234> sinter tags othertags
127.0.0.1:1234> zrangebyscore board (10 +inf limit 0 2 withscores
```

### Using it from a client library

A stock `redis-py` 8.1.0 connects with its default handshake (`HELLO 3`) and was
checked against a real Redis 7.0 on the same calls, over both RESP2 and RESP3,
with identical results for: `SET` with `ex`/`px`/`exat`/`pxat`/`nx`/`xx`/`get`/
`keepttl`, `ZADD`, `ZRANGE` (including `desc`, `byscore`, `offset`/`num`,
`withscores`), `ZREVRANGE`, `ZRANGEBYSCORE`, `ZINCRBY`, `ZPOPMIN`, both
`ZREMRANGE*`, transaction pipelines, pub/sub and `WRONGTYPE` errors.

```python
import redis

r = redis.Redis(port=1234, password="kek1234", decode_responses=True)  # RESP3 by default

r.set("session:42", "data", ex=3600)          # cache / session idiom
r.zadd("board", {"alice": 10, "bob": 20, "carol": 15})
r.zincrby("board", 5, "alice")
r.zrange("board", 0, 2, desc=True, withscores=True)   # top three
r.zrevrank("board", "alice")                           # position from the top

p = r.pubsub(); p.subscribe("news")
r.publish("news", "hello")
```

What still will not work with a real application is listed under Known gaps:
multiple databases, scripting, blocking list commands, `ZADD` options. Other
libraries (ioredis, Jedis, go-redis, ...) have not been tried.

### With the bundled client

```bash
REDIS_PASSWORD=kek1234 ./build-rel/client set foo bar      # single command
REDIS_PASSWORD=kek1234 ./build-rel/client                  # interactive REPL
```

## Supported commands

### Strings
`GET`, `SET key value [NX|XX] [GET] [EX s|PX ms|EXAT ts|PXAT ms-ts|KEEPTTL]`,
`DEL key [key...]`, `EXISTS key [key...]`
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
`ZADD`, `ZREM`, `ZSCORE`, `ZINCRBY`, `ZCARD`, `ZCOUNT`, `ZRANK`, `ZREVRANK`,
`ZRANGE key start stop [BYSCORE] [REV] [LIMIT offset count] [WITHSCORES]`,
`ZREVRANGE`, `ZRANGEBYSCORE`, `ZREVRANGEBYSCORE`,
`ZREMRANGEBYSCORE`, `ZREMRANGEBYRANK`, `ZPOPMIN`

Score bounds accept `(` for exclusive and `-inf`/`+inf`. This is a functional
subset of the Redis sorted-set surface; the missing commands are listed under
Known gaps.

### Pub/Sub
`SUBSCRIBE`, `UNSUBSCRIBE`, `PSUBSCRIBE`, `PUNSUBSCRIBE`, `PUBLISH`

### Transactions
`MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`

### Replication
`REPLICAOF` (`SLAVEOF`), `REPLCONF`, `PSYNC`, `WAIT`, `FAILOVER`

### Admin / connection
`AUTH`, `HELLO`, `CLIENT` (`ID`/`GETNAME`/`SETNAME`/`SETINFO`), `ACL`, `PING`,
`ECHO`, `INFO`, `CONFIG`, `MEMORY`, `OBJECT`, `SAVE`, `BGSAVE`, `BGREWRITEAOF`

Command names are case-insensitive. Besides RESP framing, plain-text **inline
commands** are accepted (newline-terminated), and empty inline lines are ignored
— so `redis-cli --pipe` bulk loading works end to end.

## Project structure

All code lives under `src/`, one directory per module. Local includes are rooted
at `src/` (`#include "ds/avl.h"`, `#include "core/state.h"`); CMake adds `src` as
the include directory.

```
src/
  server/        server.cpp        event loop + main()
                 transport.*       plaintext/TLS socket I/O behind one non-blocking interface
                 thread_pool.*     background worker pool
  protocol/      resp.*            RESP request parser + response writers (RESP2 and RESP3)
                 buffer.*          per-connection growable byte buffer
  commands/      commands.*        command handlers + dispatch
  core/          state.*           Entry, the global DB, constants, entry lifecycle, clocks
                 common.h          container_of, FNV hash
  ds/            hashtable.*       the core hash table (dual-table progressive rehashing)
                 avl.*             AVL tree
                 deque.*           ring-buffer deque (lists)
                 heap.*            TTL min-heap
                 list.h            intrusive list (connection timers)
                 str_node.h        string node
  types/         zset.*            sorted set (AVL + hashtable)
                 hash.*            hash fields (HashNode: field + value)
                 set.*             set members (SetNode: member only, no value)
  persistence/   aof.* / rdb.*     append-only-file and RDB snapshot persistence
  auth/          cred.*            Argon2id/SHA-256 credential hashing + verification
                 sha256.h
  client/        client.cpp        a small RESP client (single-shot + REPL)
conf/            myred.conf, bench.conf, replica.conf    example configurations
                 (paths inside them, like tls/cert.pem, are relative to where you
                 start the server: run from the project root)
docs/planning/   ROADMAP (progress), BACKLOG (future work + open bugs),
                 DECISIONS (design + architecture), CODE_REVIEW (bug audit)
```

## Branches

| Branch | Contains | Use it for |
|---|---|---|
| `main` | the code only: `src/`, `conf/`, `CMakeLists.txt`, the README and the planning docs | building and running the server — **this is the finished, paused version** |
| `test` | everything in `main`, plus the local-only regression suite and its logs | verifying a change |

The suite on `test` is a single script, `scripts/stress_test.py`: correctness for
every command, a concurrent stress run, and managed phases that start their own
servers for memory accounting, config rewrite, auth, security/ACL, RESP3,
persistence (AOF/RDB restart matrix), TLS, replication and failover, a
**differential** phase that replays the same operations against a real
`redis-server` and compares the replies, and libFuzzer harnesses over the RESP and
RDB parsers. It is 2083 checks, passing on a Release build and under
AddressSanitizer + UBSan + LSan. A couple of timing checks (a `WAIT` timeout, a TTL
across a restart) measure with the wall clock and can fail on a machine whose clock
is being stepped, which was observed under WSL2; they are not server bugs. Changes
flow `main` → `test` only; the suite is never merged into `main`.

## Status and what's next

**The project is paused.** `main` is the last version being worked on, and the
server is not expected to be developed again for a long time. See
`docs/planning/ROADMAP.md` for the detailed history — this is a summary.

**Done:**

- **V8 — Pub/Sub and Transactions:** channel/pattern pub/sub with keyspace
  notifications; `MULTI`/`EXEC`/`WATCH` with atomicity free from the
  single-threaded event loop.
- **V9 — Security and auth:** config file, Argon2id credentials with async
  verification, protected mode + CIDR allowlist, ACLs with key patterns,
  command hardening, and an escaped audit log.
- **V9.7 — TLS:** a transport seam keeps OpenSSL a private dependency of one
  translation unit, with the handshake driven as connection state; live
  certificate rotation shipped later in V10.6.1c.
- **V9.8 — Config directive table:** `CONFIG GET`/`SET`/`REWRITE` unified onto
  one self-checking table, closing a class of bug that had already shipped a
  passwordless-server regression once.
- **V10 — Replication and coordinated failover:** full and partial `PSYNC`
  resync, automatic reconnect, a read-only replica gate, `WAIT`,
  `min-replicas-*`, and coordinated `FAILOVER`. Automatic (unattended,
  Sentinel-style) failover is deliberately **not** in this milestone.
- **V10.6.1 — TLS optimization pass:** measured three deferred ideas; shipped
  live cert reload, reverted a bounded-accept-queue change that didn't clear
  the noise floor, declined kTLS on the arithmetic.
- **V11 — Testing hardening:** the single regression suite described under
  Branches, with a differential oracle and fuzzing, run clean under sanitizers.
- **V13 — Production-pointable (the part that was finished):** an unmodified
  `redis-py` can now connect and work. Step 1: `HELLO`/RESP3 and the `CLIENT`
  subset. Step 2: `SET` with options and the sorted-set read surface (the
  private `ZQUERY`/`ZREVQUERY` commands were replaced by the real Redis names,
  not aliased).

**Not done, and not planned while the project is paused:** the `ZADD` options and
the rest of the sorted-set family, multiple databases (`SELECT`), `EVAL`
scripting (a custom bytecode VM is designed but not built), automatic
Sentinel-style failover, cluster/hash-slot sharding, deployment ergonomics
(packaging/service files), and a Windows port. All are scoped in
`docs/planning/BACKLOG.md`.

The planning docs record the last audit of known bugs under
`docs/planning/BACKLOG.md` → Open Bugs; at that point none could corrupt or lose
data in the data path.

## Acknowledgements

Built following **[build-your-own.org/redis](https://build-your-own.org/redis/)**,
then extended with all five data types, a full generic keyspace command suite,
`fork()`-based persistence, cursor-based `SCAN`/`HSCAN`/`SSCAN`, TLS, ACLs,
pub/sub, transactions, replication with coordinated failover, RESP3, `SET`
options, the sorted-set range commands, and a CMake build.
