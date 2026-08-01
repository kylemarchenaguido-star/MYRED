# MYRED — Design Decisions & Architecture

Rationale, conventions, and durable architecture facts. See `ROADMAP.md`
(current focus + completed milestones) and `BACKLOG.md` (future work + open bugs).

## How Agents Should Use These Files

The roadmap is split into three files (2026-07-21) so no single one grows
unwieldy:

1. **`ROADMAP.md`** — Current Snapshot, Current Focus, Completed Milestones,
   Testing Matrix.
2. **`BACKLOG.md`** — Open Bugs, Next Major Milestones, deferred items, feature
   gaps.
3. **`DECISIONS.md`** (this file) — conventions, Design Decisions, Architecture
   Notes.

Companion: **`CODE_REVIEW.md`** — audit worklist + Resolved Bugs Archive.

Read order for a cold start: ROADMAP Current Snapshot → ROADMAP Current Focus →
BACKLOG Open Bugs → this file's Design Decisions → ROADMAP Completed Milestones.

Update rules:

- Put new open bugs in `BACKLOG.md` → Open Bugs. When a bug is fixed, record it in
  `CODE_REVIEW.md` → Resolved Bugs Archive and leave a one-line note in BACKLOG.
- Put implementation tradeoffs only in this file (Design Decisions).
- Put completed work summaries only in `ROADMAP.md` → Completed Milestones.
- Keep active implementation instructions under the relevant active milestone in
  `ROADMAP.md`.
- Avoid inline session notes — convert them into durable tasks, bugs, decisions,
  or completed summaries.

Naming conventions:

- Version headings use `V<number> - Name`; active work uses `V<number>.<step> - Name`.
- Status labels: `[Next]`, `[In Progress]`, `[Done]`, `[Backlog]`, `[Deferred]`.
- Redis command names uppercase in prose: `GET`, `ACL SETUSER`, `BGREWRITEAOF`.
- Internal code names keep exact spelling: `CmdSpec`, `k_cmd_table`, `acl_check`.
- Config directive names lowercase: `requirepass`, `rename-command`, `auditlog`,
  `tls-port`.

## Design Decisions

### Dispatch Table

Current state:

- Command dispatch uses `CmdSpec` entries in `k_cmd_table`.
- `CmdSpec` owns handler, arity, write flag, AOF rewrite flag, ACL categories, and
  key spec.
- `acl_init_categories()` mutates the table at boot to derive categories from
  `is_write`.
- `rename-command` builds an owning `g_dispatch` (`{canonical, spec}`) over the
  `k_cmd_table` template; under `g_loading` (AOF replay) `do_request` falls back to
  `k_cmd_table` for canonical names that miss `g_dispatch`.

Tradeoff:

- Deriving `CAT_READ`/`CAT_WRITE` avoids duplicated truth in the initializer.
- Losing `const` on `k_cmd_table` is the cost.

Preferred upgrade:

- Replace `acl_init_categories()` with `build_cmd_table()`: build specs, derive
  categories/key specs/guard cats, return a frozen map. Also enables owned command
  names for `rename-command` without the mutate-at-boot step.

### AOF Write Path

- Normal successful writes use raw RESP bytes captured by `try_one_request`.
- TTL-sensitive writes use rewrite paths that emit deterministic absolute commands.
- Nondeterministic writes (SPOP) set `CmdSpec::aof_self` and feed a synthetic
  deterministic frame (`SREM` of popped members) via `aof_feed`, same mechanism
  eviction uses for its synthetic `DEL`.
- No-op writes are mutation-gated by `g_writes_since_save`.
- Renamed commands use canonicalized AOF frames, not raw aliases.

### ACL Model

Current deliberate simplification:

- Permissions compile to `allow_cats` plus `cmd_overrides`.
- Enforcement is O(1) and does not replay raw ACL token history per request.

Known gap:

- Redis's ordered last-match-wins rule composition is not fully preserved.

Upgrade path:

- Store raw ordered rule tokens alongside the compiled form; replay them at
  `ACL SETUSER` time into the compiled form; keep request-time enforcement O(1);
  use raw tokens for `ACL LIST`/`ACL GETUSER` output only.

### ACL Category Tagging

The `+@read` escalation (a read-only user reaching `CONFIG`/`KEYS`/`ACL`) was a
*mistagging*, not a missing gate: `acl_init_categories()` gave `CAT_READ` to every
non-write command. Fix: strip `CAT_READ`/`CAT_WRITE` from any command carrying
`CAT_ADMIN`/`CAT_DANGEROUS`, so a command is either data-plane or control-plane,
never both — `acl_check` stays a single O(1) `&`.

Rejected: a second `guard_cats` bitmask + AND-gate. It only expresses per-command
mandatory co-requirements ("grantable by @read but also always needs @admin"),
which no real command needs; revisit only if one appears.

Divergence from Redis (deliberate): `@read` no longer implies `KEYS`/`CONFIG`,
`@write` no longer implies `FLUSHALL`. Grant explicitly (`+keys`, `+flushall`) to
restore.

`@transaction` (`CAT_TRANSACTION`, bit 8) was added with V8.4 for
`MULTI`/`DISCARD` (and later `EXEC`/`WATCH`/`UNWATCH`). Unlike the admin/dangerous
rule above, it does **not** strip the `CAT_READ` base bit — a read-only user
should still be able to open a transaction — and the commands keep `CAT_FAST` so
`+@fast` users don't lose access, matching Redis's `@fast @transaction` tagging.

Adding a category means editing **four hand-maintained parallel lists**: the
constant in `state.h`, `acl_cat_bit()` (parse), `acl_format_user()`'s `cats[]`
(emit), and `ACL CAT`'s array (advertise). Parse and emit are a *matched pair*:
emit-without-parse writes a `+@category` into the config that the next boot
rejects — the same silent grant-dropping shape as the `~*&*` bug. V8.4 shipped
with exactly that split for one round-trip, caught by `ACL SETUSER` before any
`CONFIG REWRITE` ran. Collapsing the four lists into one table is a filed follow-up.

**Credential material never crosses the wire** (invariant, formalised V8.8).
`acl_format_user`'s `for_config` flag is the mechanism: `true` writes real hashes
into the config file on disk, `false` emits `#<hash>`, and `ACL LIST` passes
`false`. `CONFIG GET requirepass` follows the same rule and returns `<set>` rather
than the stored Argon2id hash — a hash is a *verifier*, so leaking it converts an
online attack (rate-limited by `k_max_auth_inflight`, audited, lockable) into an
offline one at the attacker's own speed. Redis returns the value because Redis
historically stored plaintext. Revisit notes in BACKLOG → Open Decisions.

`AUTH` is intentionally not a `k_cmd_table` command (its handler needs `conn`); it
is matched by literal name before dispatch and can never be renamed or disabled,
so no config can lock out every client by aliasing it away.

### Transactions (V8.4–V8.7)

Atomicity is free: the event loop is single-threaded, so nothing can interleave
between `EXEC`'s queued dispatches. The work was state machine and reply framing.

`raw == nullptr` in `do_request` is a **contract**, not an accident: it means the
caller has no verbatim client bytes and the AOF entry must be re-encoded from the
`cmd` vector. `EXEC` relies on it (the parser consumed the bytes at queue time);
`aof.cpp`'s replay loop was already passing it. Without this, transactional writes
succeed, reply `+OK`, and vanish on restart.

Queued commands are stored **as typed**, never canonicalized, because
`dispatch_build()` erases the old name from `g_dispatch` when `rename-command`
applies — storing the canonical name would let `EXEC` resurrect a command that was
deliberately renamed away.

We do **not** wrap the batch in `MULTI`/`EXEC` in the AOF the way Redis does; each
queued write is logged individually as it runs. This is load-bearing: `aof.cpp`'s
replay `Conn fake{}` has an `in_multi` field, so a `MULTI` appearing in the log
would make replay queue the rest of the file into `queue_cmds` and silently drop
it. Revisit only alongside replication.

`WATCH` uses eager dirty-marking (a write marks watchers immediately) rather than
lazy generation-diffing, so `EXEC` checks one bool. Marking is conservative —
`dirty_before` proves a command wrote *something*, not which key — and false
aborts are safe under optimistic locking while missed aborts are not. Natural
expiry deliberately does not invalidate a watch (modern Redis behaves the same;
TTL churn otherwise causes constant spurious aborts), but eviction does.

Divergence from Redis (deliberate): `UNWATCH` inside `MULTI` executes immediately
instead of queueing, consistent with the other four transaction commands, which
all dispatch above the queueing gate. `AUTH` likewise runs immediately inside
`MULTI` — it short-circuits before the gate, and our AUTH is async.

### Config Directive Table (V9.8)

One row per directive in `k_config_table` (state.cpp), owning name, arity,
`apply`, `get`, `boot_only`, `masked` and `emit`. `config_apply`,
`config_get_value`, `config_all_names` and `config_rewrite` are all walks over it,
so the three hand-maintained lists that caused four silent-drift incidents are
gone. **Table order is config-file order** — `config_rewrite` has no ordering
logic of its own, so moving a row moves the line.

Adding a directive is one row. The dispatcher validates arity before calling
`apply`, so `apply` may index `args` freely.

`emit == nullptr` is the load-bearing marker: it means *plain scalar* —
single-valued, unmasked, assign-not-append. That single property is what makes a
row safe both for the shared formatter and for the boot round-trip check, which is
why there is no separate `multi` flag. Anything conditional (`tls-*`, `auditlog`,
`requirepass`), multi-line (`bind`, `allow-ip`, `save`) or accumulating (`user`,
`rename-command`) supplies its own `emit` and is excluded from both.

Quoting is **on demand, not always**. `config_write_scalar` quotes only when the
value is empty or contains whitespace / `#` / `"` / `\` — the things
`config_tokenize` would otherwise mangle. Quoting everything was tried and
rejected: it gains nothing for `port 1234` and renormalizes every existing config
file. The empty case is not cosmetic — an unquoted empty value tokenizes to zero
args, which the arity check then rejects at the *next boot*, so
`notify-keyspace-events` off must be `""`.

`masked` rows must supply an `emit`, asserted at boot by `config_selfcheck()`.
`requirepass`'s getter answers `<set>`; its `emit` reaches past the getter to the
stored hash. Route it through the shared formatter and `CONFIG REWRITE` writes the
placeholder, after which the next boot hashes the literal string `<set>` as the
password — a rerun of `3e2d0e9`. The boot round-trip check skips masked rows for
exactly the same reason: re-applying that getter's output would hash `<set>` on
*every* boot.

**A round-trip check cannot catch a getter bound to the wrong field.** `format` →
`apply` → `format` is self-consistent even when `get` reads an unrelated variable,
which is how `appendonly` came to report `protected_mode` and survive a full suite
run. Only writing a *distinct* value and reading it back detects that class; the
`[REG]` probe block in `stress_test.py`'s CONFIG section is where it lives.

Quoting is **on demand, not always**. The tokenizer splits on whitespace, treats
`#` as a comment, and uses `"`/`\` as its own escapes, so a value is quoted only
when it is empty or contains one of those. Quoting everything was tried first and
rejected: it gains nothing for `port 1234`, normalizes the shape of every existing
config file, and broke a test that greps for a bare directive. The empty case is
not cosmetic — an unquoted empty value tokenizes to zero args, which `need1()`
then rejects at the *next boot*, so `notify-keyspace-events` off must be `""`.

`requirepass` is excluded twice over: `config_write_scalar` refuses it at the
choke point, and `metadata_selfcheck` fails the boot if it appears in
`config_rewrite_scalars()`. One guard is the invariant, the other is the alarm —
its masked `<set>` value reaching disk would make the next boot hash that literal
string as the password.

### Transport Seam (TLS)

All per-connection socket I/O routes through `transport.h/.cpp`
(`tr_read`/`tr_write`/`tr_close`), so OpenSSL is a private dependency of that one
translation unit — `server.cpp` includes no OpenSSL headers. TLS's key semantic
break from plaintext: a *read* can demand POLLOUT and a *write* can demand POLLIN.
That is why `Conn` carries transport-demand flags (`tr_want_read`/`tr_want_write`)
separate from application intent (`want_read`/`want_write`), and why the poll
dispatch is **intent-driven** (drive by application intent; readiness in either
direction retries the same operation). The handshake is loop-driven connection
state (`tls_handshaking` + a dedicated `hs_list` timer lane), never a synchronous
`SSL_accept`.

### Memory Eviction

- Best-of-`maxmemory_samples` sampling; no persistent 16-slot eviction pool yet.
- Acceptable for project scale; avoids stale-entry validation complexity.
- Revisit only if realistic cache workloads show poor hit rates.

### Compact Encodings

- Heavyweight structures for all collection sizes; `OBJECT ENCODING` reports
  honest MYRED names.
- Listpack/intset/quicklist-style encodings are future memory optimizations, not
  correctness requirements.

### Windows Port

- Not a simple socket port: persistence relies on `fork()` for `BGSAVE` and
  `BGREWRITEAOF`. A portable snapshot design must come before a serious Windows
  build.

### Portability: explicit includes

Newer GCC/libstdc++ (13+) dropped many transitive includes. Files must include the
header that actually provides each symbol — `<climits>` for `ULLONG_MAX`/`*_MAX`,
`<cstdint>` for `UINT64_MAX`/fixed-width types, `<limits>` for
`std::numeric_limits`, `<cstring>` for `memcpy`, `<algorithm>` for
`std::min`/`std::max`. Code that relied on transitive includes compiled on older
toolchains and breaks on newer ones — treat a missing-symbol build error as a
missing include, not a compiler bug.

## Architecture Notes

- Single-threaded `poll()` event loop; non-blocking sockets; `TCP_NODELAY` on
  accepted sockets.
- Thread pool for background work and large async deletes.
- `fork()`-based `BGSAVE` and `BGREWRITEAOF`.
- Top-level database is a dual-table HMap with progressive rehashing; `hm_scan`
  uses reverse-binary cursor iteration.
- Entry runtime types: `T_STR = 1`, `T_ZSET = 2`, `T_DLIST = 3`, `T_HASH = 4`,
  `T_SET = 5`.
- RDB tags are separate from runtime entry tags: string = 0, zset = 1, list = 2,
  hash = 3, set = 4.
- TTL is monotonic in memory and wall-clock on disk.
- Delta memory accounting: `Deque::elem_bytes` / `HMap::elem_bytes` maintained at
  mutation choke points; `entry_mem_usage` is O(1) for all types; a debug-only
  `cb_bytes_check` independently recounts in `mem_selfcheck` to catch drift.
- Python stress harness is useful for correctness and concurrency, not peak server
  throughput (client-bound at a few thousand ops/sec).
