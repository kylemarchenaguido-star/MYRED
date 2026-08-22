# MYRED — Security Testing Log

A running record of every adversarial attempt made against MYRED: what was
tried, what actually happened, and how to run it again. Entries are kept whether
they found something or not — **the "nothing here" entries are the point**, since
they are what stops the next pass re-deriving a conclusion someone already
reached.

Two ground rules, both learned the hard way:

- **Every claim in this file was reproduced against a running server.** "By
  inspection" has been wrong here more than once, in both directions: a
  hardening item was written up as missing when the warning already existed at
  every boot, and a comment about a stale fd was written up as a bug when the
  field already carried the right default. Inspection aims the search. It does
  not close it.
- **The server under attack is always disposable** — a private high port, a
  temp directory, its own config. Never the instance on 1234.

## How to run the permanent checks

Everything that survived as a regression check lives in `scripts/stress_test.py`
and runs with the rest of the suite:

```bash
python3 scripts/stress_test.py --server build-rel/server          # everything
python3 scripts/stress_test.py --server build-rel/server \
        --phases security,config,persistence,tls --log ''         # the security-relevant phases
```

Add `--destructive` for the protocol-abuse section (malformed frames, oversized
arguments); it is off by default because it deliberately tries to wedge the
server it is pointed at.

The throwaway repro scripts written while hunting are **not** kept in the repo.
Each finding below names the mechanism precisely enough to rebuild one in a few
minutes, and the permanent version of it is the suite check.

## Techniques that turned out to be worth reusing

- **Freeze a forked child with a FIFO instead of racing it.** Both
  `aof_write_snapshot` and `rdb_write_snapshot` open their temp file with
  `O_WRONLY | O_CREAT | O_TRUNC`, and opening a FIFO for write blocks until a
  reader arrives. `aof.cpp`'s temp path is fixed (`<aof_path>.tmp`), so
  `os.mkfifo` on it before `BGREWRITEAOF` holds the child at a known point for
  as long as the test needs — exactly what a slow disk looks like from the
  parent's side, with no sleeps and no flakiness. (`rdb.cpp`'s temp path
  embeds the child pid, so the same trick does not reach it.)
- **Make the child fail a chosen way.** A FIFO plus `SIGKILL` is the OOM killer;
  a *directory* where the temp file goes makes `open()` fail so the child
  `_exit(1)`s, which is the full-disk path.
- **Read `/proc/<pid>/fd` for both counts and identity.** `os.readlink` gives
  the target — including `(deleted)` for an unlinked inode still held open — and
  `os.stat` on the *link* gives that inode's size, which is how "the rewrite
  freed no disk space" was measured directly rather than argued.
- **A bare `connect()` is not proof a server is up.** A listening socket
  inherited by a forked child still completes handshakes out of its backlog with
  nobody accepting. Demand a real RESP round trip.
- **Assert on the attack, never on the fix.** "No directive appeared that the
  operator did not set" survives a reimplementation; "CONFIG SET returns an
  error" does not.
- **An RSS number means nothing on its own.** Measuring the duplicated write
  stream took three attempts: a 16 MB seed let a freed allocator arena absorb
  the entire duplicate (+256 KB on the broken server *and* the control), and an
  absolute "under 1.5x the payload" threshold turned out to be unportable —
  the same workload on a healthy server costs 1.04x on Release and **5.78x under
  ASan**. What works is a control instance: same binary, same seed, same
  rewrite but one that completes, and assert the difference is under half a
  payload. Release 33 MB vs 66 MB, ASan 186 MB vs 263 MB — both decisive.

---

# Findings

Ordered newest first. Severity uses `docs/planning/CODE_REVIEW.md`'s legend.
Full write-ups, with the code references, live there; this file is the log of
*attempts*.

## 2026-08-22 — All fixes applied; two more leaks found on the way

**Everything below is fixed. Suite 1458/1458 on Release and under
ASan+UBSan+LSan, with no sanitizer output of any kind.**

Two leaks surfaced while verifying, neither of them caused by the fixes and both
pre-existing: 🔴 `conn_destroy` never `delete`d the `Conn` (432 bytes per
connection, measured linear), and 🟡 connections still open at shutdown were
never freed (`main()` returned without walking `fd2conn` — 2 × (432 + 2 × 64 KB)
on a master holding two replica links). Both fixed. The first was verified by
arithmetic rather than a green light: the master's leak fell from 266,896 bytes
in 15 allocations to 263,008 in 6, exactly 9 × 432.

**They had been invisible because the suite ran under ASan+UBSan+LSan and
nothing ever read the sanitizer's output.** Every phase asserted on protocol
behaviour and left the verdict in a stderr file. `check_sanitizer_output()` now
scans every instance after its phase stops it. A sanitizer build is not coverage
until something asserts on what it printed.

The seventh fix is a correction worth its own entry. Finding 7 (fork children
inherit every socket) was specified as `SOCK_CLOEXEC` + `accept4`. That landed
exactly as written — and the check still fails, because **`SOCK_CLOEXEC` fires
on `exec()`, not on `fork()`**, and these children never exec: they write a
snapshot and `_exit()`. The flag has nothing to fire on. The child has to close
the fds itself right after `fork()` returns 0.

**The lesson is about the test, not the bug.** The change was real, a grep
confirmed the exact flag on the exact call, and the build was clean. Every
verification short of running the attack said the fix was in. Only a check
written against the attack — "does the child still hold sockets?" — could
distinguish a real fix from a plausible one. That is the whole argument for the
"assert on the attack, never on the fix" rule, demonstrated against a *fix*
rather than against a reimplementation.

## 2026-08-21 — Targeted logic-level attacks (V11)

Five hypothesized abuse cases from the ROADMAP, each with a repro written
against a disposable instance. **Three came back clean. Two found bugs, and
following the machinery behind the second one produced the two most serious
findings of the milestone — neither of which was on the list.**

### Found

| # | Severity | What | How it was shown |
|---|---|---|---|
| 1 | 🔴 | A rewrite child that dies by signal or exits non-zero leaves `g_aof_child_pid` set forever: no log line, `aof_pending_rewrite` stuck at 1, later `BGREWRITEAOF` replies success and does nothing, and every subsequent write is duplicated into a buffer nothing drains | FIFO-freeze the child, `SIGKILL` it, then write 32 MB. Against a control instance whose rewrite completed: **33 MB of RSS there, 66 MB here** — one whole extra copy, and the control clears `aof_pending_rewrite` while this one never does |
| 2 | 🔴 | `close(g_data.g_aof_current_size)` closes a *byte count*, so every successful rewrite leaks the live AOF fd, pins the superseded inode, and aims a `close()` at an arbitrary fd number | 12 rewrites took the process from **7 fds to 19**; an 8,557,676-byte AOF compacted to 135 bytes with **all 8.5 MB still pinned** by the leaked fd; with a fresh AOF (`size == 0`) the first rewrite ran `close(0)` and stdin's slot came back holding `appendonly.aof` |
| 3 | 🟠 | The rewrite delta's write loop advances `off` but never the pointer, so a short write re-sends the prefix | **Inspection only** — no attempt was made to build a filesystem that fills mid-write. Recorded because the twin loop ten lines away does it correctly |
| 4 | 🟠 | `CONFIG REWRITE` writes operator-supplied strings into a file the next boot parses line by line. A newline cannot be quoted away, and three emitters do not even quote | Four payloads, each set → rewrite → restart → observed: `auditlog` and `dbfilename` both left the server **running an injected `maxclients 20077`**; the `requirepass` `$argon2id$` passthrough left it booting with **nobody able to authenticate**; an ACL username containing spaces **widened the user's key scope from `~x:*` to `~*`**, confirmed by reading a key it was denied before the restart |
| 5 | 🟡 | `+cmd`/`-cmd` accept any string at all — unknown commands and renamed aliases included — and store a rule `acl_check` never looks up. Unknown *categories* are rejected in the same parser | `ACL SETUSER u … -nosuchcommand` → `OK`; with `rename-command get zzget`, `-zzget` → `OK` and the denied user still ran the command |
| 6 | 🟡 | `ACL GETUSER` renders `commands` as one of three fixed strings and never reads per-command overrides or channel patterns | `-@all +flushall` reports `-@all` while FLUSHALL succeeds; `+@all -flushall` reports `+@all` while it is denied; `&news:*` does not appear at all. `ACL LIST` gets all three right |
| 7 | 🟡 | Fork children inherit every socket — no `SOCK_CLOEXEC`, no `accept4` | `/proc/<child>/fd` on a frozen child shows the listener and every client socket. A client that hung up mid-rewrite stayed in `CLOSE-WAIT`/`FIN-WAIT-2` and only reached `TIME-WAIT` when the child was killed — which is the attribution |

### Clean

- **ACL enforcement through a renamed command** (the ROADMAP's most confident
  bet, placed because this same canonicalization path had produced two earlier
  bugs). Eight probes: category grants, canonical per-command overrides, admin
  commands, and key patterns all enforce correctly through an alias, because
  `do_request` resolves to the canonical name before any gate runs.
- **The subscribe-mode gate through an alias.** An aliased `SET` gets the
  subscribe-mode refusal; an aliased `PING` is allowed.
- **Keys holding control bytes through AOF re-encode and RDB round trip.** Eight
  key shapes (CR+LF, bare LF, NUL, quote and backslash, a literal RESP frame,
  `#`, spaces, high bytes) across strings, lists and hashes: 24 keys and every
  value byte-identical after a `BGREWRITEAOF` + restart and again after a `SAVE`
  + restart. The parser is length-prefixed and so is everything downstream of
  it — the place that *does* re-scan text is the config file, finding 4 above.
- **TLS handshake state confusion.** Seven shapes against a 1s
  `tls-handshake-timeout`: a plaintext RESP frame, an HTTP request, a truncated
  ClientHello then silence, a record header claiming 16 KB that never arrives,
  an RST mid-handshake, a corrupt record in a live session, and a ClientHello at
  the *plaintext* port. All end at the same clean close — immediately for the
  ones OpenSSL rejects, at 2.01s against a 2s timeout for the ones that go
  silent — with a bystander session untouched. Re-run under **ASan+UBSan+LSan**:
  no double free of the `SSL*`, no leak, no report.
- **A restart during a `BGSAVE`/`BGREWRITEAOF` window.** Predicted to fail on the
  inherited listener; it does not, because the server sets `SO_REUSEADDR`. Both
  save paths also serialize fully in the parent before forking, so the child's
  whole life is one `write()` plus `fsync()` of a pre-built buffer.

## 2026-08-19 — Static code-level security review (V11)

Read at the code level across the five areas the ROADMAP named — auth/ACL, RESP
parsing bounds, the TLS handshake state machine, RDB/AOF loading from untrusted
files, and the audit log — then every candidate reproduced against a live
instance before being written up.

### Found

| # | Severity | What | How it was shown |
|---|---|---|---|
| 1 | 🔴 | The accept loop spins at 100% CPU once the process runs out of fds. An unaccepted peer stays in the kernel backlog, so the listener stays readable and `poll()` never sleeps. There was no `maxclients` at all | With `RLIMIT_NOFILE=64`: **96% of one core and 2 MB/s of log** (416,509 identical `accept() error` lines). After the fix: **0% and nothing** |
| 2 | 🟠 | Audit-log forgery through an unescaped username. `AUTH <user> <pass>` takes a RESP bulk string, which may legally contain `\n`, and `audit_write` concatenated it in raw | One failed `AUTH` wrote **two** lines, the second a well-formed `event=auth_success user=default` record indistinguishable from a real one |
| 3 | 🟠 | No bound on an inline command's length | A 32.5 MB inline line moved RSS by **+494.9 MB — 16x amplification**. After the fix: **0.0 MB**, connection refused |
| 4 | 🟠 | `tls-handshake-timeout` was refreshed by any byte, so it was an inactivity timer rather than a deadline | A peer dribbling 1 B/s was **still un-reaped at 4x the configured timeout**, holding a `Conn` and 128 KB of buffers |

Five 🟡 hardening items were fixed in the same pass (pointer-overflow idioms in
`rdb.cpp`, a zset member-count cap, `SSL_write` length clamping, AOF-replay
teardowns, and the audit escaping applied at all six sites).

### Clean

Six results verified correct and listed in `CODE_REVIEW.md` — notably that
`rdb_load_buffer` CRCs the whole image before parsing anything, that the poll
loop cannot use a destroyed `Conn`, that `tr_close` nulls `c->ssl` after
`SSL_free` so no handshake exit can double-free, and that `ct_equal` is
constant-time past the length check.

One item was written up as a finding and then **retracted after checking**: the
argon2 SHA-256 fallback was called insufficiently loud, but `server.cpp` already
warns about it at every boot.

---

# Standing weak spots

Not bugs — the places worth aiming the next pass at, and why.

- **Anything reached only by an admin.** `CONFIG SET` and `ACL SETUSER` are
  `@admin`, which is why finding 4 above is 🟠 rather than 🔴. That is a real
  mitigation and not a reason to stop looking: the realistic vector is a
  provisioning script feeding externally-sourced names through a trusted admin
  connection, and the outcome (a config file the server refuses, or an ACL that
  quietly differs from what was typed) does not care how the string arrived.
- **Repeated-shape code where only one copy is right.** Three separate incidents
  now: the config-table row whose second copy carried the first's identifier,
  `aof_rewrite_reap` versus `rdb_check_background_save`, and the delta write loop
  versus `aof_write_snapshot`. When a routine exists twice, diff the two.
- **Validation that exists exactly once.** `rename-command` rejects control
  characters in its NEW name. Nothing else does, and finding 4 is the whole cost
  of that. Grep for a check before assuming it is applied uniformly.
- **The fork children.** Serializing in the parent keeps the window short, but
  everything in it — inherited fds, the reap path, the shadow delta buffer — has
  now produced findings. Two of the three most serious V11 bugs came from that
  ~200 lines.
