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
- **Authentication and ACLs:** `AUTH`, named users, command/category rules, and key-pattern checks
- **Runtime configuration:** config file, selected environment overrides, and `CONFIG GET`/`CONFIG SET`
- **Memory management:** approximate memory accounting, `maxmemory` policies, eviction stats, `MEMORY`, and `OBJECT`
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

Requires a C++17 compiler, **CMake ≥ 3.16**, **zlib**, and **pthreads**.

```bash
# install deps (Debian/Ubuntu/WSL)
sudo apt install build-essential cmake zlib1g-dev

# configure + build
cmake -B build
cmake --build build
```

This produces two binaries in `build/`: `server` and `client`.

## Running

```bash
# start the server (listens on port 1234; run from the project root so it finds dump.rdb)
./build/server

# or load a config file explicitly
./build/server myred.conf
```

The server requires authentication. The historical default password is `kek1234`.
Set `MYRED_PASSWORD`, `MYRED_PORT`, `MYRED_AOF`, `MYRED_CONFIG`,
`MYRED_MAXMEMORY`, or `MYRED_MAXMEMORY_POLICY` to override common settings at
startup.

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

### Admin
`AUTH`, `ACL`, `PING`, `INFO`, `CONFIG`, `MEMORY`, `OBJECT`,
`SAVE`, `BGSAVE`, `BGREWRITEAOF`

Command names are case-insensitive.

## Testing

A Python test harness (`stress_test.py`) speaks RESP directly over a socket and
covers command correctness, auth/ACL behavior, persistence checks, memory
accounting, maxmemory behavior, concurrent writes, and a randomized throughput
stress run. It prints per-section timings, command latency percentiles, command
mix, and slowest operation tables.

```bash
# server must be running in another terminal
python3 stress_test.py --password kek1234

# correctness only
python3 stress_test.py --password kek1234 --correctness-only

# writes a shareable log (stress_results.md by default)
python3 stress_test.py --password kek1234 --log run.md

# stress only, with a larger worker/operation count
python3 stress_test.py --password kek1234 --stress-only --stress-threads 16 --stress-ops 2000
```

> Note: the Python harness measures correctness/concurrency, not raw throughput
> (it's bound by synchronous round-trips). For real numbers use
> `redis-benchmark -p 1234 -a kek1234 -t set,get,lpush,rpush,lpop,rpop -P 16`.

Additional shell helpers live in `scripts/`:

```bash
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
stress_test.py     RESP test + benchmark harness
test_memory.py     focused memory/eviction test helper
myred.conf         example server configuration
scripts/           AOF and diagnostic shell helpers
docs/ROADMAP.md    milestone roadmap and next steps
docs/CODE_REVIEW.md audit notes and future hardening ideas
```

## Acknowledgements

Built following **[build-your-own.org/redis](https://build-your-own.org/redis/)**,
then extended with all five data types, a full generic keyspace command suite,
`fork()`-based persistence, cursor-based `SCAN`/`HSCAN`/`SSCAN`, and a CMake build.
