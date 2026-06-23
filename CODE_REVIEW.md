# MYRED — Full Codebase Review (v5.3)

Project-wide pass over every `.cpp`/`.h` file. Each item has a **location**, a
**what**, and a **why**. Grouped by severity, then by file.

Severity legend:
- 🔴 **CRITICAL** — crash, memory corruption, or silent data loss.
- 🟠 **BUG** — observably wrong behavior.
- 🟡 **ROBUSTNESS** — edge cases, hardening, latent hazards.
- 🔵 **OPTIMIZATION** — performance / memory.
- ⚪ **CLEAN CODE** — naming, dead code, consistency, style.

---

## 0. Critical bugs (fix first)

| # | Location | Problem |
|---|----------|---------|
| C1 | `heap.cpp:43` | `heap_down` right-child check reads `a[l].val` instead of `a[r].val`, and never updates `min_val` when it picks the right child. The min-heap invariant breaks → TTL keys expire in the wrong order (some early, some never until a later op reshuffles the heap). |
| C2 | `commands.cpp:2060` | `do_srandmember` positive-count path: `resp_str(out, members[i].data(), members.size())` passes the **vector length** as the string length instead of `members[i].size()`. Out-of-bounds read → sends garbage / can crash. |
| C3 | `commands.cpp:2007` | `do_spop` with `count == 0`: `resp_err(out, 0)` passes `NULL` as the message → `strlen(NULL)` inside `resp_err` → crash. Redis returns an empty array here. Should be `resp_arr(out, 0)`. |
| C4 | `state.cpp:77` | `entry_del_sync` for `T_SET` calls `set_clear(&ent->hash)` — wrong field. Must be `set_clear(&ent->set)`. It clears the (empty) hash HMap and **leaks every set member**, while the real set table is freed only by `hm_clear` inside `set_clear` that never runs on it. Memory corruption/leak on every set deletion. |

These four should be the first commit of the refactor; they are outright defects, not style.

---

## 1. `state.cpp` / `state.h`

🟠 **`entry_del` never offloads to the thread pool** — `state.cpp:59-65`
```cpp
size_t set_size = 0;
if ((ent->type == T_ZSET)){ hm_size(&ent->zset.hmap);}   // return value discarded
else if ((ent->type == T_SET)){ hm_size(&ent->set); }    // return value discarded
...
if (set_size > k_large_container_size){ /* never true */ }
```
`set_size` is computed but the result is thrown away, so it stays `0` and the
large-container branch is dead. Every delete is synchronous, including
million-element sets — that stalls the single-threaded event loop. Fix: assign
the result (`set_size = hm_size(...)`). Also handle `T_HASH` and `T_DLIST`
(`entry_deque(ent).count`), which are not measured at all. *Why:* the whole point of
`entry_del` vs `entry_del_sync` is non-blocking deletion of big containers; right
now that split does nothing.

🔵 **`Entry` carries every type's storage at once** — `state.h:88-101`
```cpp
struct Entry {
  ...
  std::string str;   // T_STR
  ZSet zset;         // T_ZSET
  Deque deque;       // T_DLIST
  HMap hash;         // T_HASH
  HMap set;          // T_SET
};
```
Every key — even a tiny string — allocates and default-constructs a `ZSet`, a
`Deque`, and **two** `HMap`s. That is a large per-key memory and
construction-time overhead across the whole keyspace. *Why / how:* make the value
a tagged `union` (or `std::variant`) constructed per type, or hold a `void*`/
`std::unique_ptr` to a type-specific payload. This is the single biggest memory
win available and also removes the "which field is valid for this type" footguns
that produced C4.

🟡 **`expire_if_needed` reads the heap before checking emptiness is fine, but the magic `-1` is repeated everywhere** — `state.cpp:121-129`. The sentinel
`(size_t)-1` for "no TTL" appears in ~20 call sites as a raw literal. Introduce
`static constexpr size_t NO_TTL = (size_t)-1;` and a helper
`bool entry_has_ttl(const Entry*)`. *Why:* one definition, no chance of a
`-1` vs `(size_t)-1` signedness slip.

⚪ **`heap_idx` is `size_t` initialized to `-1`** — `state.h:92`. `size_t heap_idx = -1;`
relies on implicit wrap to `SIZE_MAX`. Works, but name the constant (above) and
assign it explicitly.

⚪ **Leading-space indentation / mixed style** — `state.cpp:1-4` lines begin with a
stray space. Cosmetic but it signals the file was hand-patched; run
clang-format across the project.

---

## 2. `heap.cpp` / `heap.h`

🔴 See **C1**. The corrected `heap_down` inner block:
```cpp
if (r < len && a[r].val < min_val){   // a[r], not a[l]
    min_pos = r;
    min_val = a[r].val;               // must update so a later swap is correct
}
```

🟡 **No bounds/representation invariants are asserted** — a malformed
`pos >= len` passed to `heap_update` indexes out of range. Add
`assert(pos < len)` at the top of `heap_update`. *Why:* cheap guard for a data
structure that backs expiry correctness.

⚪ **`heap.h` exposes only `heap_update`** but `heap_delete`/`heap_upsert` live as
`static` in `state.cpp`. That's fine, but a one-line comment in `heap.h` saying
"insert/delete are implemented in state.cpp against `g_data.heap`" avoids the
"where's the rest of the API" hunt.

---

## 3. `hashtable.cpp` / `hashtable.h`

🔵 **`h_foreach` rescans from bucket 0 every call; fine — but `hm_foreach` ignores the callback's `false` return across tables** — `hashtable.cpp:134-136`
```cpp
void hm_foreach(HMap *hmap, bool(*f)(HNode*,void*), void *arg){
    h_foreach(&hmap->newer, f, arg) && h_foreach(&hmap->older, f, arg);
}
```
The short-circuit works, but the function returns `void`, so callers that want
early termination (none today) can't observe it. Either return `bool` or
document that early-stop only happens within a single table. *Why:* every
`cb_collect`-style callback returns `true` unconditionally today, so the
early-exit capability is unused dead semantics — make it intentional.

🟡 **`hm_help_rehashing` runs on every lookup/insert/delete** — `hashtable.cpp:78,87,110`.
Correct, but each `hm_lookup` does up to `k_rehashing_work = 128` node moves.
Under a read-heavy load during a resize this adds latency to reads. Consider
amortizing rehash work only on writes, or lowering the per-call quota. *Why:*
reads shouldn't pay write-side maintenance cost.

⚪ **Comment noise** — `hashtable.cpp:122` `// what is this ?`, `:177` `// <---- magic`.
The reverse-bit scan cursor is subtle and deserves a real one-paragraph
explanation, not a "magic" tag. Keep the explanation, drop the self-deprecation.

⚪ **`k_rehashing_work` / `k_max_load_factor` are file-scope non-`static` `const`** —
`hashtable.cpp:48,97`. Give them internal linkage (`static constexpr`) or move to
a named-constants header. *Why:* avoids ODR surprises if another TU ever defines
the same name.

⚪ **`str_hash` is named "FNV" but only mixes 32 bits** — `common.h:13-19`. `h` is
`uint32_t`, returned widened to `uint64_t`. Buckets only need low bits so it's
acceptable, but either compute a true 64-bit FNV-1a or rename to make the 32-bit
nature explicit. *Why:* a reader expecting 64-bit dispersion (e.g. for future
larger tables) would be misled.

---

## 4. `resp.cpp` / `resp.h`

🟠 **Partial bulk-string headers are rejected as malformed** — `resp.cpp:42`
```cpp
while (pos < size && data[pos] != '\r') { pos++; }
if (pos + 1 >= size) { return -1; }   // <-- should be 'return 0' (need more)
```
When a `$<len>\r\n` header is split across two TCP segments, the parser returns
`-1` ("bad RESP, close connection") instead of `0` ("need more data"). The
array-header path (`:22`) and the body path (`:53`) correctly return `0`. *Why:*
under real network fragmentation or pipelining, a client can be disconnected
mid-command. This is a correctness bug masked by tests that send whole commands
in one `write`.

🟡 **`n_args` accumulation has no overflow guard** — `resp.cpp:25-31`. The
per-bulk length loop checks `str_len > k_max_msg` *inside* the loop (`:49`), but
the `n_args` loop has no equivalent bound, so `*999999999999\r\n` overflows
`int32_t` (UB) before the `n_args > k_max_msg` check at `:31`. Bound it inside
the digit loop like `str_len` is. *Why:* a hostile or buggy client can trigger
signed-overflow UB in the parser — the #1 place you don't want UB.

🟡 **No cap on total argument count vs. memory** — even with `k_max_msg` (32 MB)
per element, an attacker can request `n_args` near 32M small strings and force a
huge `cmd` vector. Consider a separate, smaller `k_max_args`. *Why:* request
amplification / memory exhaustion.

⚪ **Magic `sizeof("literal") - 1`** repeated — `resp.cpp:67,72,79,...`. Fine, but a
tiny `buf_append_lit(out, "+OK\r\n")` macro/inline removes the `-1` boilerplate
and the chance of an off-by-one.

---

## 5. `buffer.cpp` / `buffer.h`

🟡 **`buf_size` / `buf_data` take non-`const Buffer*`** — `buffer.h:20-21`. They only
read. Make them `const`. *Why:* lets const-correct callers (and future read-only
paths) use them, and documents intent.

🟡 **`buf_consume` only reclaims space when the buffer fully drains** — `buffer.cpp:82-89`.
For a long-lived connection that always has a little residual data, `data_begin`
creeps forward and the next `buf_append` does a `memmove` to slide it back. That's
the intended design, but on a pipelined stream it can mean repeated large
`memmove`s. Consider compacting when `data_begin` passes the halfway mark. *Why:*
avoids O(n) slides on steady pipelined traffic.

⚪ **Dead code** — `buffer.cpp:66-68` commented-out `buf_append_u32`. Remove it.

⚪ **`buf_append(Buffer*, uint8_t)` single-byte overload** allocates the same
growth path for 1 byte. Fine, but the RDB writer calls it in tight loops
(`buf_append(ctx->buf, 0)` etc.); a small-write fast path would help the
serializer. 🔵

---

## 6. `deque.cpp` / `deque.h`

🟡 **`deque_get` bounds check is `==`, not `>=`** — `deque.cpp:67-69`
```cpp
const std::string *deque_get(const Deque *d, size_t idx){
    if (idx == d->count){ return nullptr; }   // idx > count reads OOB
    return &d->buf[deque_phys(d, idx)];
}
```
Only the exact `idx == count` case is rejected; any `idx > count` indexes the
ring buffer out of logical range. Today every caller pre-clamps, so it's latent,
but it's a trap for the next caller. Use `if (idx >= d->count) return nullptr;`.
*Why:* defense in depth for a primitive that returns a raw pointer.

🔵 **`deque_grow` doubles but never shrinks** — after a list grows to 1M then
pops down to 2 elements, the 1M-slot buffer is retained until the key is deleted.
Consider halving when `count < cap/4`. *Why:* unbounded memory retention on
churning lists.

⚪ `deque.h:29` `deque_phys` is `inline` in the header (good) while
`deque_grow`/push/pop are out-of-line; consistent, fine.

---

## 7. `avl.cpp` / `avl.h` / `zset.cpp` / `zset.h`

🟡 **`avl_offset` can walk off the tree on a bad offset** — `avl.cpp:141-168`. The
loop trusts that `offset` is reachable; `znode_offset` callers pass arbitrary
user offsets (ZQUERY). It does return `NULL` via the parentless branch, so it
terminates — but the logic is dense and untested at the boundaries. Add a unit
test for offset past both ends. *Why:* AVL rank math is the easiest place to get
an off-by-one that only shows on specific tree shapes.

🔵 **`do_zquery` materializes results into a `std::vector<ZQueryResult>` then
emits** — `commands.cpp:731-749`. Necessary because RESP needs the count first.
Fine, but for large `limit` you copy every name into a `std::string` twice (into
the vector, then into the buffer). You can count first (`avl_cnt`/walk) then emit
directly. *Why:* halves allocations on big range queries.

⚪ **`min` redefined locally** — `zset.cpp:19-21` defines a `static size_t min(...)`
shadowing `std::min`. Harmless but use `std::min`.

⚪ **`zset_clear` order** — `zset.cpp:190-194` calls `hm_clear` then `tree_dispose`.
Correct (nodes are freed via the tree, the HMap only holds intrusive nodes), but
add a comment saying the HMap must be cleared first precisely because it does
**not** own the nodes. *Why:* prevents a future "double free" edit.

---

## 8. `hash.cpp` / `hash.h` / `set.cpp` / `set.h`

These two pairs are near-duplicates (`HashNode{field,value}` vs `SetNode{member}`,
plus `HKey`/`SKey`, `hnode_field_eq`/`snode_eq`, `cb_collect`/`cb_set_collect`,
`hash_clear`/`set_clear`).

🔵 / ⚪ **Deduplicate the intrusive-HMap-of-strings pattern.** A small templated
helper (or a shared `kv_node`/`member_node` base with a comparator) would remove
~60 lines of copy-paste and guarantee both stay in sync. *Why:* C4 (`set_clear`
vs `hash` field mix-up) is exactly the class of bug that duplicated, almost-
identical code invites.

🟡 **`hash_set` / `set_add` recompute the hash twice on the create path** — they
`str_hash` into the `HKey`, look up, miss, then build a node reusing
`key.node.hcode` (good) — actually fine. But `hash_set`'s update path copies the
value via `=`; for large values consider taking the value by value and moving.
🔵 *Why:* avoids a copy on `HSET bigfield bigvalue`.

---

## 9. `commands.cpp`

### Correctness / bugs
🔴 C2 (`:2060`), C3 (`:2007`) — see top table.

🟠 **Misspelled error code breaks the RESP error contract** — `commands.cpp:1903`
`resp_err(out, "WRONGTPE wrong type")`. Clients (and `redis-cli`) treat the first
token as the machine-readable error code. `WRONGTPE` ≠ `WRONGTYPE`, so any client
branching on the code mishandles it. *Why:* this is a protocol bug, not a typo —
it changes observable behavior.

🟡 **`do_lpop` / `do_rpop` delete with the wrong comparator** — `commands.cpp:973,993`
```cpp
hm_delete(&g_data.db, &ent->node, &entry_eq);   // everywhere else uses &hnode_same
```
`entry_eq` does `container_of(key, LookupKey, node)` on what is actually an
`Entry*`. It only works because `Entry` and `LookupKey` share the same
`{HNode node; std::string key;}` prefix layout, so the aliased `->key` read lands
on the right bytes. This is fragile UB-adjacent aliasing. Use `&hnode_same`
(pointer identity), as `do_srem`/`do_hdel`/`do_spop`/expiry all do. *Why:* a
future field reorder in either struct silently corrupts deletes.

🟡 **`expire_generic` / `expireat_generic` don't lazily expire first** —
`commands.cpp:514-538`, `1758-1782`. They `hm_lookup` and then set a TTL without
calling `expire_if_needed`. Setting a new TTL on a key that is already past its
old TTL (but not yet reaped) effectively resurrects it. *Why:* `EXPIRE` on a
logically-dead key should report `0`, not revive it.

🟡 **`do_asyncdel` (UNLINK) only measures `T_ZSET`/`T_SET` for offload** —
`commands.cpp:847-851`. Large `T_HASH` and `T_DLIST` always delete synchronously.
Same root cause as the `entry_del` bug; unify the "is this container big?"
decision into one helper used by both. *Why:* UNLINK on a huge hash/list blocks
the loop, defeating its purpose.

🟡 **`glob_match` has catastrophic backtracking** — `commands.cpp:1294-1353`. Each
`*` recurses over every suffix split; a pattern like `a*a*a*a*a*b` against a long
non-matching key is exponential. `KEYS`/`SCAN MATCH` with attacker-chosen
patterns can hang the server. *Why:* a single `SCAN 0 MATCH '*a*a*a*...'` is a
DoS. Use the classic two-pointer linear glob (track last-`*` backtrack point)
instead of recursion.

🟡 **`rand() % n` modulo bias + shared global RNG** — `do_randomkey:1682`,
`do_spop:1998,2011`, `do_srandmember:2041,2055,2067`. `rand()` is low-quality and
`% n` skews toward small values; also `rand()` is not thread-safe (the pool
threads don't call it, so OK for now). *Why:* SPOP/SRANDMEMBER distribution is
visibly nonuniform for large sets. Use `<random>` (`std::mt19937_64` +
`uniform_int_distribution`) seeded once.

### Correctness — smaller
🟡 `do_spop` count-path: after `count == 0` is fixed, note `str2int` failure
returns `resp_arr(out, 0)` (`:2005`) where Redis returns an error for a
non-integer count. Minor parity gap.

⚪ **`do_set` supports no options** — `commands.cpp:62-70`. Real `SET` has
`EX/PX/EXAT/PXAT/NX/XX/KEEPTTL/GET`. Now that `SETEX`/`SETNX`/`GETSET` exist
separately, folding them into `SET` options would match Redis and remove
duplicate handlers. Feature/clean-code.

⚪ **`do_zadd` is single-member, no flags** — `commands.cpp:642-656`. Redis ZADD
takes `[NX|XX] [GT|LT] [CH] [INCR] score member [score member ...]`. Feature gap;
note for a future ZSET parity pass.

### Optimization
🔵 **Dispatch is a ~110-branch `if/else if` string-compare chain** —
`commands.cpp:2237-2415`. Every command does up to ~100 `std::string ==`
comparisons plus a size check. Replace with a `static const
std::unordered_map<std::string_view, Handler>` (or a perfect-hash/`switch` on a
small command-id) keyed on `cmd[0]`, with arity validated inside each handler or
via a table of `{min_args, max_args, fn}`. *Why:* O(1) dispatch, and it removes
the single largest and most error-prone function in the file (arity bugs like the
`mset` odd-size check are easy to get wrong when buried in the chain).

🔵 **`do_keys` uses `hm_foreach` (full O(N) scan, materializes all keys)** —
`commands.cpp:460-470`. On a large keyspace this blocks the event loop and
buffers every key. Document it as debug-only and steer clients to `SCAN`
(already implemented). *Why:* one `KEYS` on a big DB freezes all clients.

🔵 **Collect-then-emit allocates a `std::vector<std::string>` of copies** in
`do_smembers`, `h_collect_reply`, `do_sscan`, `sunion_impl`, etc. The members are
copied out of the nodes purely to count them, then copied again into the buffer.
Where the count is known up front (`hm_size`), emit straight from the nodes via a
callback that writes to the buffer. *Why:* halves allocations and copies on every
bulk read.

🔵 **`sunion_impl` dedups with sort+unique (O(N log N) + full copy)** —
`commands.cpp:1860-1872`. A temporary `HMap`/`unordered_set` membership check is
O(N) and avoids sorting. Minor unless unions are large.

### Clean code
⚪ **Inconsistent / typo'd error messages** throughout: `"WRONGTYPE wrong type"`
vs `"WRONGTYPE Operation against a key holding the wrong kind of value"` vs
`"WRONGTPE..."`; `"Opreation"`, `"excessds"`, `"maximun"`, `"succesfully"`. Define
the standard messages once as named constants
(`MSG_WRONGTYPE`, `MSG_NOT_INT`, …) and reuse. *Why:* consistency for clients and
no more code-token typos (see the `WRONGTPE` bug).

⚪ **Profanity / placeholder comments** — e.g. `:1491` `// bull ↑ and shit ↓`,
`:1673` `(crazy this trash (garbage))`, `:1806` `// i am dumb asfck`, `:1320`
`// what is this bomboclat ???`. Replace with real explanations or delete. *Why:*
this is going public (README + ROADMAP committed); the comments undercut an
otherwise serious project and obscure genuinely subtle code (the scan cursor, the
glob char-class parser) that *does* need explaining.

⚪ **`str2int` / `str2dbl` are defined at `:499`/`505` but forward-declared at
`:72`** to be used by the new string handlers above them. Move the definitions up
near the top (just after `lookup_entry`) so the forward decls can go away. *Why:*
removes a declaration that has to be kept in sync.

⚪ **`lookup_entry` destructively `swap`s the key out of `cmd[i]`** —
`commands.cpp:23-47`. Several handlers had to learn this the hard way (SETNX,
GETSET, MSET use a non-destructive `hm_lookup` copy specifically to avoid it).
Document this contract in a comment on the function, or split into
`lookup_entry` (non-destructive, takes `const std::string&`) and
`lookup_or_create` (consumes the key). *Why:* the swap-then-empty behavior caused
real bugs during the string-command work; make it impossible to misuse.

⚪ **`KeyStats`/`KeysCtx`/`ScanCtx`/`HCollect`/`RandKeyCtx`/… callback-context
structs** are declared inline next to each command. They're fine, but several are
identical shapes (`{vector<string>*, const string* pattern}`); a single
`CollectCtx` would serve SCAN/HSCAN/SSCAN.

---

## 10. `server.cpp`

🟡 **`next_timer_ms` clobbers the idle deadline with the IO deadline** —
`server.cpp:128-131`
```cpp
if (!dlist_empty(&g_data.idle_list)){ next_ms = conn->last_active_ms + k_idle_timeout_ms; }
if (!dlist_empty(&g_data.io_list )){ next_ms = conn->last_active_ms + k_io_timeout_ms; }  // overwrite
```
The second assignment overwrites rather than `std::min`-ing, so the idle timer is
ignored whenever the IO list is non-empty. The heap/save checks below *do* use
`std::min`. *Why:* poll can sleep too long and idle connections time out late.
(Both timeouts are 30 s today so the symptom is hidden; it breaks the moment they
differ.)

🟠 **Connection counters are swapped** — `server.cpp:103,114` and `state.h:77-78`.
`handle_accept` does `g_total_connections++`; `conn_destroy` does
`g_total_connections--`. That makes `g_total_connections` a *current* gauge, not a
lifetime total, and `INFO` reports it as `total_connections` (`commands.cpp:908`).
Meanwhile `connected_clients` (the field meant for the live count) is **never
updated** and `INFO` always prints 0 for it (`:907`). Fix: `total_connections`
only ever increments; `connected_clients` is the one that ++/-- on
accept/destroy. *Why:* both INFO numbers are currently wrong.

🟡 **No `SO_REUSEADDR` on the listen socket** — `server.cpp:308-326`. Only
`TCP_NODELAY` is set. After a restart the port sits in `TIME_WAIT` and `bind()`
fails with "Address already in use". *Why:* every quick restart during
development/ops fails to bind. Add `setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, …)`
before `bind`.

🟡 **`handle_accept` accepts exactly one connection per wakeup** —
`server.cpp:357-359`. With many simultaneous connects, the backlog drains one per
poll cycle. Loop `accept()` until `EAGAIN`. *Why:* connection-storm latency.

🟡 **No `EINTR` retry on `read`/`write`** — `server.cpp:244,262`. Only `EAGAIN` is
handled; an `EINTR` (signal) is treated as a fatal error and closes the
connection. Retry on `EINTR`. *Why:* a `SIGCHLD` from the BGSAVE child (or
`SIGINT`) landing mid-syscall can drop a client.

🟡 **`fork()` + thread pool deadlock hazard** — `server.cpp:302` starts 8 pool
threads; `rdb.cpp:829` forks for BGSAVE. If a pool thread holds the libc
`malloc`/`stdio` lock at the instant of `fork()`, the child (which then calls
`rdb_save` → `malloc`/`fprintf`) can deadlock, since only the forking thread
survives in the child. *Why:* rare but real and very hard to debug. Mitigations:
prepare the entire serialized snapshot buffer in the parent **before** forking and
have the child only `write()` it (no malloc/stdio in the child), or quiesce the
pool around the fork. This also makes BGSAVE point-in-time-consistent without COW
surprises.

⚪ **`addr.sin_port = ntohs(1234)`** — `server.cpp:317`. Should be `htons`. Works
only because both are byte-swaps on this host. Same for
`s_addr = ntohl(0)` (0 either way). *Why:* misleading; breaks if copied to a
context with a non-zero address.

⚪ **Port and password are hardcoded** — `server.cpp:288,317`. Move to a config
file / CLI args / env (`redis.conf`-style is already on the roadmap). At minimum
read `MYRED_PASSWORD`/`MYRED_PORT` env vars. *Why:* can't run two instances or
change the secret without recompiling.

⚪ **`g_data.fd2conn` grows to the largest fd and never shrinks**, and a linear
scan over it builds `poll_args` each loop — `server.cpp:338-349`. Fine at small
scale; for many connections, maintain a compact list. 🔵

⚪ `msg_errno` format string `"[errno:%s\n]"` has the `\n` inside the brackets —
cosmetic log oddity (`server.cpp:33`).

---

## 11. `rdb.cpp` / `rdb.h`

🟡 **The `.bak` recovery path is dead — backups are never written** —
`rdb.cpp:357-361` (`rdb_save` just `rename(tmp, filename)`), but `rdb_load`
(`:806-815`) tries `filename + ".bak"` on failure. Since nothing ever creates the
`.bak`, the "recover from backup" logic can never fire. *Why:* the code advertises
a safety net that doesn't exist. Either rename the old file to `.bak` before the
final rename, or delete the dead recovery branch.

🟡 **BGSAVE child always writes `"dump.rdb"`** — `rdb.cpp:838`. The hardcoded name
ignores any future configurable path and diverges from `do_save`/`rdb_save`'s
`filename` parameter. *Why:* a configurable dump path (roadmap) will silently not
apply to BGSAVE.

🟡 **`g_writes_since_save` reset only on success paths, set in two places** —
`do_save` (`commands.cpp:812`) resets it directly; the periodic/BGSAVE path resets
it in `rdb_check_background_save` (`rdb.cpp:867`). If a synchronous `SAVE` races a
periodic trigger the bookkeeping can double-count. Centralize "a save just
completed" into one function. *Why:* INFO `rdb_changes_since_save` accuracy and
correct auto-save triggering.

🟡 **Loader trusts `n_entries` and member counts from the file** — e.g.
`rdb_load_zset_entry:495` loops `n_members` reading score+name. A corrupted (but
CRC-valid, e.g. truncated-then-rewritten) count drives large allocations / long
loops. The CRC check mitigates accidental corruption but not a crafted file.
Bound counts against remaining bytes. *Why:* hardening for untrusted dump files.

🔵 **Whole file is read into memory, then fully decompressed into another
buffer, then parsed** — `rdb.cpp:716-792`. For large dumps this is 2–3× the file
size resident at once. Streaming decompression would cap memory. Low priority.

⚪ **Duplicate include** — `rdb.cpp:3` and `:5` both `#include "buffer.h"`.

⚪ **`mono_expired_to_wall` name typo** ("expired" → "expiry") and the
`int64_t remaining = (int64_t)(mono_expire - mono_now)` underflow-then-clamp is
correct but worth a comment (`rdb.cpp:17-24`).

---

## 12. `thread_pool.cpp` / `thread_pool.h`

🟡 **No shutdown / join / destroy** — threads spin forever, `mu`/`not_empty` are
never `pthread_*_destroy`'d, and there's no way to drain on exit. On `SIGINT` the
process just exits with work possibly queued. *Why:* a queued large-container
delete can be lost (acceptable on shutdown) but the missing shutdown also means
you can't cleanly quiesce the pool around `fork()` (see the BGSAVE deadlock
item). Add a `stop` flag + `pthread_cond_broadcast` + `join`.

🟡 **`worker` has no exception guard** — if a `Work` function throws (some handlers
allocate), the exception propagates out of `worker` → `std::terminate`. *Why:*
one bad task kills a pool thread silently. Wrap `w.f(w.arg)` in try/catch or
guarantee tasks are `noexcept`.

⚪ **`assert(rv == 0)` for pthread init** disappears under `NDEBUG` (Release), so
init failures are unchecked in the build you actually ship — `thread_pool.cpp:26-36`.
Handle the error or `abort()` explicitly. *Why:* Release silently ignores a failed
mutex init.

---

## 13. `client.cpp`

(Reviewed; it's a test/dev client, lower stakes.)

⚪ Mirrors the server's RESP writing/parsing by hand — if `resp.*` were factored
into a tiny shared library, the client could reuse the parser instead of a second
implementation that can drift. 🔵/⚪

🟡 Same partial-read consideration as the server parser applies if the client ever
reads large multi-bulk replies in pieces — verify it loops on short reads.

---

## 14. `common.h` / `list.h`

⚪ **`container_of` uses the GCC statement-expression extension** —
`common.h:8-10`. Works on gcc/clang (the project targets gnu++17), but it's
non-standard. A `reinterpret_cast`-based `container_of` template is portable and
type-checks the member. *Why:* portability + the template form catches
wrong-type mismatches at compile time.

⚪ `list.h` comments `// ? i gotta be dumb` / `// ?` — replace with what
`dlist_init` (self-linking sentinel) actually does. The DList is correct; just
document the circular-sentinel invariant.

---

## 15. Cross-cutting / architecture

1. 🔵 **Tagged-union value in `Entry`** (see §1) — biggest memory + safety win.
2. 🔵 **Hash-map command dispatch** (see §9) — biggest per-command CPU win and
   removes the most bug-prone function.
3. 🟡 **One "is container large?" helper** shared by `entry_del`, `do_asyncdel`
   (and any future eviction) — removes the duplicated, currently-broken size
   logic.
4. ⚪ **Named message/constant headers** — error strings, `NO_TTL` sentinel,
   load-factor/rehash constants. Kills a whole class of typo/мagic-number bugs.
5. 🟡 **Lazy expiry is not uniform** — `lookup_entry` applies it, but several
   type-agnostic generic commands (`EXPIRE`, `EXPIREAT`) skip it. Route every
   keyspace read through a single lookup that always expires first.
6. 🟡 **RNG** — replace global `rand()` with a seeded `<random>` engine; fixes
   modulo bias and is a prerequisite for ever moving randomized ops off-thread.
7. ⚪ **Tooling** — the volume of typos, mixed indentation, and dead code says
   there's no formatter/linter in the loop. Add `clang-format` + `clang-tidy` and
   build with `-Wall -Wextra -Wshadow` (a shadowed `min`, discarded `hm_size`
   results, and the `a[l]`/`a[r]` heap bug would likely have been flagged by
   `-Wunused-result` / static analysis).
8. 🟡 **Fuzz the RESP parser** — it's hand-written and is the single most exposed
   attack surface; the two parser bugs above (partial-read, `n_args` overflow)
   are exactly what a fuzzer finds in minutes.

---

## Suggested order of work

1. **C1–C4** (heap, srandmember OOB, spop NULL-deref, set_clear field) — pure defects.
2. `entry_del` size bug + unify the large-container helper; fix `lpop/rpop`
   comparator; fix `next_timer_ms`; fix connection counters; add `SO_REUSEADDR`.
3. RESP parser hardening (partial reads, `n_args` overflow) + fuzz harness.
4. `glob_match` linear rewrite (DoS) + `<random>` RNG.
5. Architecture: tagged-union `Entry`, hash-map dispatch, named constants/messages.
6. Cleanup pass: clang-format, comment cleanup, dead code, `.bak` path decision.
