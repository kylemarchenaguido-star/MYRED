# MYRED — a Redis-like in-memory database in C++

MYRED is a from-scratch, single-threaded, RESP-speaking in-memory key–value
database written in C++. It implements a meaningful subset of Redis — strings,
lists, hashes, sorted sets, key expiry, and RDB persistence — and because it
speaks the real **RESP protocol**, you can talk to it with the official
`redis-cli` and other Redis clients.

> **Foundation:** this project is built on the excellent guide at
> **https://build-your-own.org/redis/** — the book provides the core event-loop,
> hashtable, AVL/zset and protocol foundations that MYRED extends.

It's a learning project: every core data structure (hashtable, AVL tree,
min-heap, ring-buffer deque, intrusive lists) is implemented by hand rather
than using the STL containers.

---

## Features

- **RESP protocol** — works with `redis-cli` and standard Redis client libraries
- **Data types:** strings, lists, hashes, sorted sets
- **Key expiry (TTL):** `EXPIRE`/`PEXPIRE`/`TTL`/`PTTL`/`PERSIST`, active + lazy expiration
- **Persistence (RDB):** custom binary format with zlib compression and CRC32 integrity check
- **`fork()`-based background save** (`BGSAVE`) — the parent keeps serving while a child snapshots
- **Authentication** — password `AUTH`
- **Cursor iteration** — non-blocking `SCAN` with `MATCH`/`COUNT` (glob patterns)
- **Single-threaded event loop** (`poll`, non-blocking I/O) with `TCP_NODELAY`
- **Thread pool** for offloading large deletions

## Architecture

- **Event loop:** a single-threaded `poll()` loop with non-blocking sockets handles
  all clients; one command is processed at a time, so the core needs no locks.
- **Storage:** the database is one big hashtable mapping `key → Entry`. Each `Entry`
  holds one value whose type is `string`, `list`, `hash`, or `sorted set`.
- **Expiry:** TTLs are tracked in a min-heap ordered by expiry time (active reaping),
  plus lazy expiration on read. On disk, TTLs are stored as wall-clock time so they
  survive restarts.
- **Persistence:** `SAVE` writes synchronously; `BGSAVE` and the periodic auto-save
  `fork()` a child that serializes a copy-on-write snapshot while the parent keeps
  serving requests.

### Data structures (all hand-written)

| Structure | File | Used for |
|---|---|---|
| Hash table (progressive rehashing) | `hashtable.*` | the keyspace, and the backing store for hashes & zset member index |
| AVL tree | `avl.*` | sorted-set ordering / ranking |
| Sorted set (AVL + hashtable) | `zset.*` | the `ZSET` type |
| Ring-buffer deque | `deque.*` | the `LIST` type |
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
```

The server requires authentication (default password `kek1234`, set in `main()`).

### With `redis-cli`

Because MYRED speaks RESP, the official Redis CLI works directly:

```bash
redis-cli -p 1234 -a kek1234 set foo bar
redis-cli -p 1234 -a kek1234 get foo
redis-cli -p 1234 -a kek1234 hset user:1 name alice age 30
redis-cli -p 1234 -a kek1234 hgetall user:1
redis-cli -p 1234 -a kek1234 scan 0 match 'user:*'
```

Or interactively:

```bash
redis-cli -p 1234 -a kek1234
127.0.0.1:1234> rpush mylist a b c
127.0.0.1:1234> lrange mylist 0 -1
```

### With the bundled client

```bash
REDIS_PASSWORD=kek1234 ./build/client set foo bar      # single command
REDIS_PASSWORD=kek1234 ./build/client                  # interactive REPL
```

## Supported commands

- **Strings:** `GET`, `SET`, `DEL`, `ASYNCDEL`
- **Generic / keyspace:** `EXISTS`, `TYPE`, `EXPIRE`, `PEXPIRE`, `TTL`, `PTTL`, `PERSIST`, `KEYS`, `SCAN`
- **Hashes:** `HSET`, `HGET`, `HDEL`, `HEXISTS`, `HLEN`, `HGETALL`, `HKEYS`, `HVALS`, `HMGET`
- **Lists:** `LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LLEN`, `LINDEX`, `LRANGE`, `LSET`, `LINSERT`, `LREM`, `LTRIM`
- **Sorted sets:** `ZADD`, `ZREM`, `ZSCORE`, `ZRANK`, `ZQUERY`, `ZREVQUERY`
- **Admin:** `AUTH`, `INFO`, `SAVE`, `BGSAVE`

Command names are case-insensitive.

## Testing

A Python test harness (`stress_test.py`) speaks RESP directly over a socket and
covers correctness for every command plus a concurrency/throughput stress run:

```bash
# server must be running in another terminal
python3 stress_test.py --password kek1234

# correctness only
python3 stress_test.py --password kek1234 --correctness-only

# writes a shareable log (stress_results.md by default)
python3 stress_test.py --password kek1234 --log run.md
```

> Note: the Python harness measures correctness/concurrency, not raw throughput
> (it's bound by synchronous round-trips). For real numbers use
> `redis-benchmark -p 1234 -a kek1234 -t set,get,lpush,rpush,lpop,rpop -P 16`.

## Project structure

```
server.cpp         event loop + main()
state.*            Entry, the global DB, constants, entry lifecycle, clocks
resp.*             RESP request parser + response writers
commands.*         command handlers + dispatch
buffer.*           per-connection growable byte buffer
hashtable.*        the core hash table
zset.* / avl.*     sorted set + AVL tree
deque.*            ring-buffer deque (lists)
hash.*             hash fields
heap.*             TTL min-heap
thread_pool.*      background worker pool
list.h             intrusive list (connection timers)
common.h           container_of, FNV hash
client.cpp         a small RESP client (single-shot + REPL)
stress_test.py     RESP test + benchmark harness
```

## Acknowledgements

Built following **[build-your-own.org/redis](https://build-your-own.org/redis/)**,
then extended with additional commands, data types, `fork()`-based persistence,
`SCAN`, and a CMake build.
