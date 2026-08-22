# MYRED — Code Review and Bug Audit

Single working document for bug tracking. The older full-codebase audits (v5.3
review, Post-v9 Backlog Audit 2026-07-07, Full-Codebase Audit 2026-07-09) were
re-verified item-by-item and folded into the consolidated audit below; their
superseded text was removed on 2026-07-13. Fixed bugs move to the Resolved Bugs
Archive at the bottom (moved here from the ROADMAP the same day).

Quick benchmark:
`redis-benchmark -p 1234 -a kek1234 -t set,get,incr,lpush,rpush,sadd,hset -n 200000 -c 50 -P 16 -q`

Severity legend:
- 🔴 **CRITICAL** — crash, memory corruption, or silent data loss.
- 🟠 **BUG** — observably wrong behavior.
- 🟡 **ROBUSTNESS** — edge cases, hardening, latent hazards.
- 🔵 **OPTIMIZATION** — performance / memory.
- ⚪ **CLEAN CODE** — naming, dead code, consistency, style.

---

## V11 Static Security Review — 2026-08-19

**All four reproducing findings FIXED and re-verified 2026-08-19.** Each repro
was re-run against the patched `build-rel/server` and no longer reproduces: the
accept loop went from 96% of one core and 9.6 MB of log to **0% and nothing**
(with `startup: maxclients 10000 lowered to 32 by RLIMIT_NOFILE 64` announcing
the clamp), the forged audit record now lands escaped on a single line, the
32.5 MB inline line moved RSS by **0.0 MB instead of +494.9 MB**, and the
dribbling TLS peer is reaped on schedule.

**Nine regression checks landed in `stress_test.py`** — seven in
`phase_security` → "Security: V11 review regressions", two in `phase_tls`
(which starts its own 1s-`tls-handshake-timeout` instance, since the directive
is boot-only). Suite: **1401/1401**. Each check asserts against the *attack*
rather than the fix — that the forged record does not appear, that the
amplifying parse does not happen, that the refusal is explicit rather than a
silent drop — so they survive a reimplementation. Validated in both directions:
against a pre-fix binary all seven security-phase checks fail with the forged
line printed in the diagnostic, and the phase still runs to completion because
the `maxclients` group gates on `has_directive` instead of raising.

**Nothing from this review is left open.** The original scope, findings and
measurements are kept below as the record of what was looked at and what was
ruled out — the verified-correct list at the end is the part that saves the next
pass from re-deriving it.

Scope: the five areas the ROADMAP named for this pass — auth/ACL (`cred.cpp`,
the `ACL SETUSER` staged apply), RESP parsing bounds (`resp.cpp`), the TLS
handshake state machine (`transport.cpp` + the poll loop), RDB/AOF loading from
untrusted files (`rdb.cpp`), and the audit-log path. Read at the code level, no
live server needed to *find* any of these.

**Every finding below was then reproduced against a disposable server**, because
"by inspection" has been wrong here before. The measured numbers are in each
item. Repro scripts are throwaway; the permanent home for them is the
`security` phase of `stress_test.py`.

The four that reproduced, in fix order:

- 🔴 **The accept loop spins at 100% CPU once the process runs out of fds, and
  there is no `maxclients` to stop it getting there.** `grep -n "maxclients"`
  across the tree returns nothing: the only bound on concurrent connections is
  `RLIMIT_NOFILE`. When `accept()` fails with `EMFILE`, `handle_accept`
  (`server.cpp:557-560`) logs and returns -1, the drain loop at `server.cpp:1288`
  exits, and the listening socket is *still* readable because the pending
  connection is still in the kernel's accept queue — so `poll()` returns
  immediately, forever. **Measured** with `RLIMIT_NOFILE=64`: ~55 established
  connections were enough to take the server from 0 CPU ticks/2s idle to **96% of
  one core**, emitting **416,509 `[errno:accept() error]` lines / 9.6 MB of
  stderr in ~5 seconds** (≈2 MB/s, i.e. it fills a disk as well as a core). With
  a stock `ulimit -n 1024` this costs an attacker ~1020 idle TCP connections and
  no bandwidth at all. Remote, unauthenticated, and it degrades service for every
  existing client. Fix has two halves: a `maxclients` config that accepts-and-
  immediately-closes past the limit with `-ERR max number of clients reached`
  (so the backlog still drains and poll goes back to sleep), and rate-limiting
  the accept-error log so a failure can never be the loudest thing in the file.
- 🟠 **Unauthenticated audit-log forgery through the `AUTH` username.**
  `audit_write` (`commands.cpp:1314-1337`) builds `ts=… event=… peer=… user=…`
  by concatenation with no escaping and appends one `\n`. `do_auth` takes
  `uname = cmd[1]` verbatim (`commands.cpp:1447`) — a RESP bulk string, which is
  length-prefixed and may therefore legally contain `\n` — and `auth_complete`
  interpolates it into `" target=" + job->uname` (`commands.cpp:1409`). No
  username validation exists anywhere in `cred.cpp`/`commands.cpp`. **Measured**:
  one failed `AUTH` produced two log lines, the second being
  `ts=2099-01-01T00:00:00Z event=auth_success peer=10.0.0.9:1 user=default …`,
  byte-identical in shape to a genuine success record. This needs no credentials
  — a *failed* auth writes it, which is the point: the one thing an attacker can
  always reach is the failure path. Same shape at `commands.cpp:1399`, `4637`,
  `4663` (`target=` built from `cmd[2]`). Fix: escape `\n \r \t \\` and space plus
  anything outside printable ASCII in every attacker-reachable field, with the
  `peer`/`user` escaping inside `audit_write` itself so a new call site cannot
  forget it.
- 🟠 **The inline-command path has no argument cap, giving 16x memory
  amplification.** The `*`-prefixed path caps arguments at `k_max_args` (65536,
  `resp.cpp:53-55`); the inline path (`resp.cpp:31-39`) splits on whitespace with
  no limit of any kind, and its only bail is a `size > k_max_msg` check that fires
  *only when no newline is present at all* (`resp.cpp:23`). So one inline line up
  to `k_max_incoming` (64 MiB) becomes ~32M `std::string`s. **Measured**: a single
  32.5 MB write of `"a "` pairs took the server from 25.4 MB to **520.3 MB peak
  RSS — 16.0x — and it stayed alive afterwards**, so it is repeatable and
  multiplies by connection count (which finding 1 leaves unbounded). Fix: cap the
  inline line the way Redis does with `PROTO_INLINE_MAX_SIZE`, at 64 KiB; one
  check on `eol` replaces the existing `k_max_msg` bail and covers both the
  runaway-line and the too-many-args cases.
- 🟠 **`tls-handshake-timeout` is an inactivity timeout, not a handshake
  deadline, so it never fires against a slow attacker.** The poll loop refreshes
  every ready connection's timer at `server.cpp:1308`
  (`conn_set_timer(conn, conn->timer_type)`) *before* the `tls_handshaking`
  branch at `1310`, and `conn_set_timer` rewrites `last_active_ms` and moves the
  conn to the back of `hs_list` (`server.cpp:907-920`). Any byte therefore resets
  the deadline the config exists to enforce. **Measured** with
  `tls-handshake-timeout 2`: a connection sending a TLS record header and then
  **one byte per second was still open and un-reaped after 8.7 seconds**, 4x the
  configured timeout. Each held slot is a `Conn` plus 128 KB of buffers and,
  with no `maxclients`, a step toward finding 1. Fix: make the handshake deadline
  absolute from accept — skip the refresh at `1308` when
  `conn->timer_type == ConnTimer::HANDSHAKE`. Insertion order into `hs_list` is
  already accept order, so the reaper's "break on the first unexpired" scan stays
  correct with no other change.

Hardening found in the same pass, none of them currently exploitable — recorded
because each is safe today only by an unstated relationship, which is the class
that breaks quietly later. **All five ALSO FIXED 2026-08-19**; suite is
1401/1401 on Release and identical under ASan+UBSan+LSan.

Two things the hardening pass itself taught, both worth more than the items:

- **The AOF teardown fix broke the RDB fuzz harness at link time.** `aof.cpp` is
  linked into that harness for its RDB-preamble reader, so the four new
  `*_remove_conn(&fake)` calls became undefined references to `commands.cpp`,
  which the harness deliberately excludes. Fixed by stubbing them in
  `FUZZ_RDB_SRC` with the same `abort()` convention as the other six stubs. The
  general shape: **a fuzz harness that excludes a translation unit is coupled to
  every call that TU makes**, so any new cross-TU call is a harness break — and
  the harness is what tells you, which is the argument for keeping it in the
  suite rather than running it by hand.
- **A discarded `wait_until` result made an unrelated check the accused.**
  `phase_persistence` disarms the AOF auto-rewrite and waits for the temp file to
  disappear, but threw away the bool. Under a sanitizer build (several times
  slower) the 10 s budget expired, the server was restarted mid-rewrite, and the
  *TTL* regression check failed — reporting a TTL bug that did not exist, roughly
  2 runs in 6. Now asserted with a 30 s budget; 6/6 clean after. Honest caveat:
  the new assertion never fired in those 6 runs, so the timeout was never caught
  in the act — 0/6 against 2/6 is consistent with the diagnosis, not proof of it.
  The change stands on its own regardless, because discarding a wait result is
  wrong whatever it was masking.

- 🟡 `resp.cpp:75-76` — the `str_len` accumulator checks `> k_max_msg` *after*
  the multiply, where the `n_args` loop checks *before* it (`resp.cpp:53`). It
  cannot overflow today only because 32 MiB × 10 + 9 still fits in `int32_t`;
  raise `k_max_msg` past ~214 MB and it is signed overflow (UB). The ROADMAP
  flagged this by inspection before the fuzzer existed; 1M libFuzzer iterations
  under UBSan agree it does not fire today. Make it match the `n_args` shape. **FIXED 2026-08-19.**
- 🟡 `rdb.cpp:421,446` — `if (c->pos + len > c->end)` forms a pointer past
  one-past-the-end before comparing, which is UB even when it happens to work.
  With `len` a `uint32_t` it cannot wrap a 64-bit pointer, so it is correct in
  practice on this target. `if (len > (size_t)(c->end - c->pos))` is the same
  check without the UB. **FIXED 2026-08-19.**
- 🟡 `rdb.cpp:533-542` — the *live* zset loader reads `n_members` and loops with
  no sanity cap, while the expired-zset skip path six lines above it (`516`) and
  the list, hash and set loaders (`599`, `653`, `700`) all have one. Damage is
  bounded because each iteration is a bounds-checked `cursor_read` that fails on
  the first short read, so it is an inconsistency rather than a hole — but it is
  the only type missing the guard. **FIXED 2026-08-19.**
- 🟡 `aof.cpp:262` — replay dispatches through a stack-allocated `Conn fake{}`
  that never passes through `conn_destroy`, so any command that registers a
  `Conn*` in a global (`pubsub_remove_conn`, `watch_clear_conn`,
  `repl_remove_conn` are the three teardowns it would need) would leave a
  dangling pointer to a dead stack frame once `aof_load` returns. Not reachable
  today because `aof_feed` logs only write commands and SUBSCRIBE/WATCH are not
  writes — the hazard is that the safety is a property of what the *writer*
  chooses to log, enforced nowhere on the *reader*. Cheapest guard is to run the
  four teardowns on `fake` after the replay loop. **FIXED 2026-08-19.**
  (A claim in the first draft of this item — that `fake.fd` is 0 and so points at
  stdin — was wrong: `Conn::fd` carries a default member initializer of `-1`
  (`state.h:156`), so `Conn fake{}` already gets -1 and no fd guard was needed.)
- 🟡 `transport.cpp:180` — `SSL_write(c->ssl, buf, (int)len)` truncates a
  `size_t`. A >2 GiB `outgoing` buffer would pass a negative length; OpenSSL 3
  rejects it with `SSL_R_BAD_LENGTH` so the failure mode is a spurious connection
  close, not corruption. Clamp to a chunk size instead. **FIXED 2026-08-19.**
- ⚪ Build-config note, no action needed — recorded only so it is not
  re-discovered as a finding: with `MYRED_ARGON2=OFF` or libargon2 missing,
  `cred_hash_new` stores unsalted SHA-256 and `cred_needs_rehash` returns
  `false`, so those credentials never upgrade. That path is already loud at both
  ends — `CMakeLists.txt:49` warns at configure time and `server.cpp:1128-1130`
  warns at every boot — and `build-rel/server` has Argon2id linked in (verified
  in the binary), so it is not current behaviour.

Checked in the named areas and found **correct** — recorded so the next pass does
not re-derive them:

- `ACL SETUSER`'s staged apply (`commands.cpp:4618-4640`) does hold: every
  modifier is applied to a `User` copy and committed only after all of them
  succeed, and `g_config.users` is a `std::unordered_map` (`state.h:332`), so the
  commit cannot invalidate the `Conn::user` pointers other connections hold.
- The unguarded `conn->user->name` in `ACL WHOAMI` (`commands.cpp:4572`) is
  unreachable with a null user: `do_request` gates on `if (!conn->user)` at
  `commands.cpp:5353` before dispatch, and `EXEC` re-enters through `do_request`
  rather than calling handlers directly, so the queued path is gated too. The
  `ACL DELUSER` teardown that sets `c->user = nullptr` (`4658`) is therefore safe.
- The poll loop re-resolves `Conn *conn = g_data.fd2conn[poll_args[i].fd]` at
  `server.cpp:1304` and `conn_destroy` nulls that slot, so a connection destroyed
  earlier in the same iteration is skipped rather than used after free. `accept()`
  runs at `1288`, before the dispatch loop, so no fd can be closed and reused
  within one iteration either.
- `rdb_load_buffer` is the strongest part of this review: CRC over the whole
  image *before* any parsing (`rdb.cpp:777-779`), element-count caps on list,
  hash and set, and a zlib decompression-bomb guard at `797`. Consistent with
  1M fuzz iterations finding nothing.
- `tr_close` nulls `c->ssl` after `SSL_free` (`transport.cpp:248`), and every
  handshake exit — success, `SSL_ERROR_SSL`, timeout, and a plaintext frame
  arriving at a `tls-port` — funnels through exactly one `conn_destroy`.
- `ct_equal` (`sha256.h:152`) is constant-time past the length check, and
  `cred_verify` delegates argon2id comparison to the library.

---

## 🔴 `conn_destroy` never deletes the Conn — 2026-08-22, FIXED

432 bytes leaked per connection, measured linear (1/10/40 connect-disconnect
cycles → 2/11/41 leaked allocations). `conn_destroy` (`server.cpp:103-116`)
released everything the connection owned and never `delete`d the `Conn` itself.
Pre-existing — HEAD built with the same sanitizer flags reproduced it and the
function was byte-identical there — and the tail of the earlier V11 sanitizer
fix, which added the two `buf_destroy` calls and dropped the `delete`. 128 KB
became 432 bytes: 300x smaller, still unbounded.

**Fixed, and the proof is arithmetic rather than a green light:** the
replication master's leak went from 266,896 bytes in 15 allocations to 263,008
in 6 — a drop of exactly 3,888 = 9 × 432 — and the three instances that leaked
only `Conn` structs went completely clean.

A second, pre-existing leak sat underneath it: **connections still open at
shutdown were never freed**, because `main()` returned without walking
`fd2conn`. It surfaced only in the replication phase, whose links the server
itself holds open; every other phase's clients disconnect first. Measured on the
master: 2 Conns + 4 buffers = 2 × (432 + 2 × 65,536) = 263,008 bytes exactly,
with `fo-replica` leaking precisely half. Also fixed — a loop over `fd2conn` at
the end of `main()`.

**Both closed 2026-08-22: the sanitizer build now runs to completion with no
ASan, LSan or UBSan output at all.**

**Why they survived a sanitizer build for a week:** the suite had run under
ASan+UBSan+LSan since V11 Step 0 and **nothing ever read the sanitizer's
output**. Every phase asserted on protocol behaviour and left the server's
stderr in a file on disk. `PhaseCtx.check_sanitizer_output()` now scans each
instance's stderr after the phase stops it and fails one check per phase on any
report — verified failing on the ASan build and passing on Release. **An
instrument nobody reads is not instrumentation**, and that is the more useful
half of this finding.

---

## V11 Targeted Logic-Level Attacks — 2026-08-21

**FIX STATUS 2026-08-22 — six of the seven applied and verified; one is still
open because the fix I specified was wrong.**

Verified working against a rebuilt `build-rel/server`, each by the regression
check written for it: the wedged-pid 🔴 (`aof_pending_rewrite` clears on both
failure paths, the failure is logged, the next `BGREWRITEAOF` really forks, and
the shadow copy is gone — 33 MB against a control's 33 MB where it used to be
66 MB), the `close()`-on-a-byte-count 🔴 (fd count flat across three rewrites,
zero bytes of superseded AOF left pinned), the delta pointer 🟠, all four
config-injection payloads 🟠, the ACL rule validation 🟡 and the `ACL GETUSER`
rendering 🟡. **Suite 1457/1458 on Release.**

**Still open: 🟡 the fork children inherit every socket.** `SOCK_CLOEXEC` and
`accept4` landed exactly as specified and the check still fails, because
**`SOCK_CLOEXEC` fires on `exec()`, not on `fork()`** — and these children never
exec; they write a snapshot and `_exit()`. The flag has nothing to fire on. That
was my error in the write-up above, not a mis-application: the snippet could not
have worked. The child has to close the fds itself, first thing after `fork()`
returns 0 (`close_range(3, ~0U, 0)`, or a `getrlimit`-bounded loop), from one
shared helper called at both fork sites. The `SOCK_CLOEXEC` change is harmless
and worth keeping on its own terms; it simply does not address this finding.

Worth the note because the failure mode was silent in the most ordinary way: the
change was real, the grep confirmed it, the build was clean, and only the
regression check knew it did nothing. A test written against the attack rather
than the fix is what caught a *fix* that was wrong.

⚪ **Surfaced by the ACL validation fix:** `auth` is not in `k_cmd_table` at all
— `do_request` intercepts `AUTH` before dispatch and before `acl_check` — so
`+auth`/`-auth` was always a rule that could not be enforced, and the new
`command_is_known` check now rejects it. Consistent, and worth knowing because
real Redis accepts `+auth`, so a pasted Redis ACL line will now error. `quit` and
`reset` are in the same position: named in `cmd_ok_in_subscribe`, absent from
`k_cmd_table`. No action taken.


The last V11 item: hypothesize specific abuse cases, write a concrete repro
against a disposable server for each, and record what happened either way. Five
cases were named in the ROADMAP. **Three of them checked out clean. Two found
bugs — and the second of those led, by following the machinery rather than the
hypothesis, to the two most serious findings of the whole milestone**, neither
of which was on the list.

That ratio is the useful part of this pass. The two roadmap bets that were
reasoned about most confidently (ACL enforcement through a rename, control bytes
re-scanned downstream) both came back clean *in the place they were predicted*,
and the real bug in the second one was one layer over. A hypothesis is worth
writing down because it aims the search, not because it is likely right.

### 🔴 The AOF rewrite child's pid is never cleared unless it exits 0

`aof_rewrite_reap` (`aof.cpp:148-201`) only reaches `g_aof_child_pid = -1`
inside its success branch — nested two levels deep, under
`WIFEXITED(status) && WEXITSTATUS(status) == 0` and then under a successful
`open()` of the temp file. A child that dies **by signal** (the OOM killer's
usual target is exactly this fork: it is the newest large RSS on the box) or
**exits non-zero** (a full or read-only disk, which is what its `_exit(1)`
paths mean) falls out of the function with the pid still set — after `waitpid()`
has already reaped the process. From then on `waitpid()` returns `-1`/`ECHILD`
every loop, `r != g_aof_child_pid`, and nothing ever clears it.

`rdb_check_background_save` (`rdb.cpp:966-996`) is the same function for the
other child and gets it right: it clears the pid on a clean exit, on a failure,
*and* on a `waitpid` error, logging each. **The two copies of one routine, one
correct.** That is the third time this project has been bitten by a
repeated-shape divergence.

Measured, all four against `build-rel/server` with the child frozen on a FIFO
and then killed:

| symptom | measured |
|---|---|
| anything logged when the child dies | **nothing at all** — the last line is still `aof_rewrite: started (pid=N)` |
| `INFO aof_pending_rewrite` | stuck at **1** forever (control: back to 0) |
| a later `BGREWRITEAOF` | replies **`Background append only file rewriting started`** and does nothing; `aof_rewrite_background` bails at its "already running" guard |
| RSS after 32 MB of writes | **+65.8 MB, 2.05x** — `propagate()` (`commands.cpp:3244-3251`) keeps mirroring every write into `g_aof_rewrite_buf` while a child is believed to live, and nothing will ever drain it |

So one OOM kill of a rewrite child leaves a server that never rewrites its AOF
again, never reports why, and stores every subsequent write twice until it is
OOM-killed itself — this time the parent.

**Fix:** one exit path. Clear the pid, clear (and `shrink_to_fit`) the shadow
buffer, and log the outcome, whatever the outcome was.

### 🔴 `close()` on a byte count — one fd leaked per successful rewrite

`aof.cpp:179`, in the success path of the same function:

```cpp
if (g_data.g_aof_fd >= 0){ close(g_data.g_aof_current_size); }
g_data.g_aof_fd = open(g_config.aof_path.c_str(), ...);
```

`g_aof_current_size` is a `size_t` byte count, not an fd. Two consequences, both
measured:

- **The live AOF fd is never closed.** 12 successful rewrites took the process
  from 7 open fds to 19 — exactly one leaked per rewrite. With `maxclients` now
  clamped to `RLIMIT_NOFILE` at boot, that budget is fixed, so a server doing
  routine auto-rewrites walks itself into refusing clients.
- **The rewrite frees no disk space.** Each leaked fd pins the superseded AOF
  inode. Measured: an 8,557,676-byte AOF compacted to **135 bytes**, and
  `stat` on the leaked fd showed all **8,557,676 bytes still held**. `ls` shows
  135; the filesystem still owes 8.5 MB. Compaction is the entire point of the
  command, and it was returning nothing.
- **`close()` is aimed at an arbitrary fd number.** Demonstrated: with a fresh
  AOF (`aof_current_size == 0`) the first successful rewrite runs `close(0)`.
  The server was spawned with fd 0 pointing at a marker file; after the rewrite
  fd 0 was `appendonly.aof` — stdin closed, and the new AOF handed the freed
  slot. A tracked size that lands on 4 or 7 instead would aim at the listener or
  a client socket.

**Fix:** `close(g_data.g_aof_fd)`.

### 🟠 The delta write never advances its pointer

`aof.cpp:159-168`, the loop that appends the rewrite delta:

```cpp
ssize_t n = write(fd, d.data(), d.size() - off);
...
off += (size_t)n;
```

`off` advances, the pointer does not, so a short write re-sends the prefix and
the delta lands with duplicated commands. `aof_write_snapshot` (`aof.cpp:108`)
does `data += n` correctly, fifty lines away.

**Not reproduced live** — forcing a short write on a regular file needs a
filesystem that fills mid-write, and no attempt was made to build one. It is
recorded as an inspection finding on its own merits: the loop is wrong however
rarely it is entered, replayed duplicate `INCR`/`LPUSH`/`SADD` frames change
data rather than being idempotent, and the fix is one term.

### 🟠 CONFIG REWRITE writes operator strings into a file it then parses by line

The ROADMAP asked whether anything downstream re-scans a length-prefixed value
as if it were delimiter-bounded. The keyspace answer is **no** (see the clean
list below). The answer for the config file is **yes**, four ways, every one of
them confirmed end-to-end (set → `CONFIG REWRITE` → restart → observe):

| payload | outcome after the restart |
|---|---|
| `CONFIG SET auditlog 'audit.log"\nmaxclients 20077  # '` | the server boots **running `maxclients 20077`** |
| `CONFIG SET dbfilename 'dump.rdb\nmaxclients 20077  # '` | same, and `dbfilename` silently truncated to `dump.rdb` |
| `CONFIG SET requirepass '$argon2id$x\n…'` | the server boots and **nobody can authenticate** — `WRONGPASS` for the operator's own password |
| `ACL SETUSER "bob on ~* +@read" … ~x:*` | the user is renamed to `bob` and its key scope **widens from `~x:*` to `~*`** — confirmed reading a key it was denied before the restart |

Root cause, one sentence: `config_write_scalar` (`state.cpp:842-865`) quotes a
value containing whitespace or a quote and escapes `"` and `\` — which is
correct for spaces and quotes and **cannot work for a newline**, because the
newline ends the line before the reader ever reaches the closing quote. Three
emit lambdas skip even that much and `fprintf` the value raw inside quotes:
`auditlog` (`state.cpp:714`), `requirepass` on its `$argon2id$` passthrough
(`state.cpp:305`), and `user` via `acl_format_user` (`commands.cpp:4526`), which
writes the username completely bare. `masterauth`, six lines from `requirepass`,
*does* escape — the inconsistency is the evidence that the intent was there.

The last row is the one with teeth: `~*` sets `all_keys = true`, and the later
real `~x:*` pushes a pattern **without clearing it** (`commands.cpp:4479-4482`),
so the injected grant wins. `acl_check` short-circuits on `all_keys`, and the
user quietly holds the whole keyspace from the next boot onward.

Reachability is admin-only (`CONFIG SET` and `ACL SETUSER` are `@admin`), and
that is stated plainly rather than dressed up. The realistic vector is not an
attacker typing these by hand — it is a provisioning script syncing usernames
from somewhere else, or an operator picking a password with a quote in it. The
outcome ranges from "a directive the operator never wrote" to "the server comes
back up and locks everyone out", and neither is announced.

**Fix:** validate at the setter, not the writer. `rename-command` already
rejects control characters in its NEW name (`state.cpp:682`) — **the correct
check exists in this codebase exactly once**. The same rule belongs on
`auditlog`, `dbfilename`, `masterauth`, the `requirepass` passthrough, and ACL
usernames and patterns (which additionally must reject whitespace, since
`acl_format_user` writes them unquoted).

### 🟡 An ACL rule the server accepts is not necessarily a rule it enforces

`acl_apply_rule` validates `+@cat`/`-@cat` against the category table and
rejects an unknown one (`commands.cpp:4485-4499`), then two branches later takes
**any string at all** after a bare `+`/`-` and files it under that name
(`4503-4518`). `acl_check` only ever looks up the canonical command name, so:

- `ACL SETUSER u … -nosuchcommand` → **`OK`**. The rule is stored, echoed back
  by `ACL LIST`, and never consulted. A typo (`-flushal`) is a deny that does
  nothing, and all three surfaces agree it worked.
- With `rename-command get zzget` in place, `-zzget` → **`OK`**, and the user it
  was written to deny still runs the command. The name the operator actually
  types is the one that cannot work.

Enforcement itself is fine — that half is in the clean list.

### 🟡 ACL GETUSER does not describe the user that exists

`ACL GETUSER` (`commands.cpp:4693-4721`) renders `commands` as one of three
fixed strings — `+@all`, `+<cats>`, `-@all` — and never reads `cmd_overrides` or
the channel patterns at all. Confirmed in both directions:

- `-@all +flushall` reports **`-@all`** while `FLUSHALL` succeeds. An audit of a
  single user cannot see a dangerous command granted on top of the base rule.
- `+@all -flushall` reports **`+@all`** while `FLUSHALL` is denied.
- `&news:*` does not appear anywhere in the reply.

`ACL LIST` renders all of it correctly, so the information exists and only the
per-user introspection command — the natural one to reach for — is wrong.

### 🟡 The fork children inherit every socket

Confirmed from `/proc/<child>/fd` with a child frozen on a FIFO: the rewrite
child holds the **listener and every live client socket**. Nothing in
`server.cpp` uses `SOCK_CLOEXEC` or `accept4` (only the eventfd and the audit
log set it). Consequences, both measured:

- A client that hangs up mid-rewrite **cannot finish closing**. The parent drops
  it (`connected_clients` decrements) but the child's duplicate reference keeps
  the socket in `CLOSE-WAIT`/`FIN-WAIT-2`; killing the child moved it to
  `TIME-WAIT` immediately, which is the attribution.
- The listening port stays bound after the parent exits — `bind()` without
  `SO_REUSEADDR` fails with `EADDRINUSE` while the child lives.

Impact is bounded by how long the child runs, and that is genuinely short here:
both `rdb_save_background` and `aof_rewrite_background` serialize **fully in the
parent** before forking, so the child's whole life is one `write()` plus
`fsync()` of a pre-built buffer. **A restart during that window still works**,
because the server sets `SO_REUSEADDR` — the hypothesis that it would fail was
tested and is wrong. Worth noting how nearly this was mis-recorded: a bare
`connect()` to the port succeeds even with nobody accepting, because the
child's inherited listener still completes handshakes out of its backlog. The
first version of that check "passed" on exactly that basis; it took demanding a
real RESP round trip to make the result mean anything.

**Fix:** `SOCK_CLOEXEC` on the two `socket()` calls (`server.cpp:203`, `654`)
and `accept4(..., SOCK_CLOEXEC)` at `557`.

### Verified correct — the three cases that did not pay off

Kept in as much detail as the findings. This is the part that stops the next
pass re-deriving it, and two of these were the ROADMAP's most confident bets.

- **ACL enforcement through a renamed command.** Eight probes, all correct.
  `do_request` lowercases `cmd[0]`, resolves it through `g_dispatch` to the
  canonical name, and every gate downstream uses that canonical: category grants
  (`+@read` allows the aliased GET, `-@write` denies the aliased SET), explicit
  per-command overrides written canonically (`-get` denies `zzget`), admin
  commands (a non-admin cannot reach `CONFIG` through its alias), and key
  patterns (`~allowed:*` permits `allowed:1` and denies `denied:1` through the
  alias). The bet was placed because this same canonicalization path had already
  produced two real bugs — the AOF-restart brick and the V9.5.1 admin-category
  mistagging — and it did not produce a third.
- **The subscribe-mode gate through an alias.** `cmd_ok_in_subscribe` is checked
  against the canonical name: an aliased `SET` is refused with the subscribe-mode
  error while an aliased `PING` is allowed.
- **Keys holding control bytes, through AOF re-encode and RDB round trip.**
  Eight key shapes (CR+LF, bare LF, NUL, embedded quote and backslash, a literal
  `*3\r\n$3\r\nSET\r\n`, `#`, spaces, high bytes) across strings, lists and
  hashes: 24 keys stored, 24 after a `BGREWRITEAOF` and restart, 24 after a
  `SAVE` and restart, every value byte-identical. Length-prefixed in, length-
  prefixed out.
- **TLS handshake state confusion.** Seven shapes at a `tls-port` with a 1s
  `tls-handshake-timeout` — a plaintext RESP frame, an HTTP request, a truncated
  ClientHello then silence, a record header claiming 16 KB that never arrives, an
  RST mid-handshake, a corrupt record inside a completed session, and a TLS
  ClientHello at the *plaintext* port. Every one ends at the same clean close:
  immediate for the ones OpenSSL rejects outright, at 2.01s against a 2s timeout
  for the ones that go silent, never hung past it. A bystander TLS session was
  untouched throughout and the server took new connections afterwards. Re-run
  under **ASan+UBSan+LSan**: no double free of the `SSL*`, no leak, no report —
  which was the specific question the ROADMAP asked.

### What landed in the suite

**46 checks across five sections**, all in existing phases:

- `phase_aof_rewrite` → "Persistence: rewrite-child failure and fd hygiene" (10):
  fd count across three successful rewrites, bytes pinned by leaked fds, socket
  inheritance, the signal path (logged, pid cleared, next rewrite really runs,
  no shadow copy), and the non-zero-exit path.
- `phase_config_roundtrip` → "Config: operator strings must not rewrite the
  config file" (15): five payloads, each on its own instance, asserting the
  server still boots, the operator's password still works, no directive was
  injected, and the key scope is unchanged.
- `phase_security` → "Security: an ACL rule must mean what it says" (7): rule
  validation, the alias rule, GETUSER fidelity in both directions, channels, and
  the two positive alias-enforcement checks that lock in the clean result.
- `phase_persistence` → "Persistence: keys holding control bytes survive a
  re-encode" (5) and `phase_tls` → the handshake-confusion shapes (9).

Suite: **1401 → 1447 checks, currently 1428/1447 on Release**, and the 19
failures are exactly the `[REG]` checks for the findings above — nothing else
moved. **All 19 were watched failing against today's binary before any of this
was written up**, which is the only thing that makes them regression tests
rather than assertions of what the code already does. Every one asserts on the
attack — "no directive appeared that the operator did not set", "the user's key
scope is what was granted", "RSS did not grow by a second copy of the write
stream" — never on the shape of a fix.

Two test-design notes worth keeping, both paid for in this pass:

- **The shadow-copy measurement was wrong twice, in opposite directions.**
  First it seeded 16 MB before breaking the child, and the 16 MB buffer
  `rdb_build_aof_preamble` had just freed was arena enough to absorb the whole
  duplicate: RSS grew 256 KB for a 16 MB write in *both* the broken server and
  the control. That run measured the allocator and called a broken server
  clean. Seeding almost nothing fixed it — on Release. Then the replacement, an
  absolute "RSS grew less than 1.5x the payload", turned out to be unportable:
  the identical workload on a **healthy** server costs **1.04x on Release and
  5.78x under ASan**, whose size classes and redzones dwarf the thing being
  measured, so the check would have failed on the sanitizer build no matter what
  the code did. The version that works compares against a **control instance** —
  same binary, same seed, same rewrite, but one that completes — and asserts the
  difference is under half a payload. It separates cleanly on both builds. The
  general rule: an RSS number is only meaningful next to another RSS number
  taken the same way.
- **A check that neither passes nor fails is worse than one that fails.** The
  first version of the rewrite-child section shared one instance across all
  three failure cases; the wedged parent from the signal case then made the two
  cases after it silently unable to fork a child at all. Their checks simply did
  not run, and the section reported 1/8 with no indication that two of the eight
  had evaporated. Each case now gets its own instance.

---

## Consolidated Bug Audit — 2026-07-13 (V9.6.4 worklist)

Scope: every open item from ROADMAP "Known Bugs and Correctness Follow-ups" (now moved
here) merged with the 2026-07-07 / 2026-07-09 audit findings (folded in; their
superseded text was removed 2026-07-13). **Each item was re-verified against the
working tree of 2026-07-13**; line numbers are against that tree unless marked
otherwise. This section is the single worklist for V9.6.4; the ROADMAP no longer
carries bug bodies.

**AUDIT COMPLETE 2026-07-17.** Every 🔴/🟠/🟡 item closed 2026-07-13; every 🔵/⚪
perf/polish item closed 2026-07-16/17 (plus bonus fixes found en route: SPOP AOF
replay determinism, 🔴 RDB non-TTL set loader data loss, ECHO + empty-inline
redis-cli compat, incremental eviction with `scripts/test_evict_tick.sh`). The
remaining Testing debt items moved to ROADMAP → V9.6.5 (general and speed test).

### A. Verified fixed since 2026-07-09

| Item | Fixed by / evidence |
|---|---|
| 🟠 N5 — `sha256_hex` padding (`len%64 >= 56`) | V9.6 prerequisite, 2026-07-12. KAT-verified against NIST vectors + hashlib for lengths 0–200. |
| 🟡 `do_auth` plaintext copy left unwiped | V9.6.1/V9.6.2 — `do_auth` wipes the `cmd` copy on every path; the worker owns the only other copy and `secure_zero`s it after verify (`commands.cpp:1132`). |
| 🟡 `CONFIG SET <unknown>` returned `+OK` | V9.5.5 — `commands.cpp:2724` returns `ERR Unknown config parameter`. |
| 🟠 `ACL CAT` missing RESP array header (ROADMAP) | V9.5.5 — `commands.cpp:3129` emits `resp_arr` before the elements. |
| 🟠 `SMOVE` imprecise key checks (ROADMAP) | V9.5.2 — `kr_smove` resolver (`commands.cpp:3488`) checks source + destination only. |
| 🟠 `OBJECT`/`MEMORY` key at `cmd[2]` unchecked (ROADMAP) | V9.5.2 — `kr_object`/`kr_memory` resolvers (`commands.cpp:3493-3513`). |
| ⚪ `dispatch_build`/`command_is_known` declared but undefined | V9.5.3 — both defined; `g_dispatch` built at boot. |
| ⚪ `state.h:218` stray `\` line-continuation | Gone — no trailing backslash remains in `state.h`. |
| 🟠 Audit-log peer separator (`peer=ip.port`) | Fixed 2026-07-13 — `server.cpp:115` formats `%u.%u.%u.%u:%u`. |

(Already recorded fixed on 2026-07-09: replay `fake.user` assigned-in-shape, `SMOVE`
reaccount, `do_keys` glob, V9.5.1 admin-category tagging, `MSETNX`.)

### B. The V9.6.4 worklist — CLOSED 2026-07-17

Every unmarked line below was re-verified **open** on 2026-07-13; all bug items
have since been ticked with FIXED notes. Only Testing debt remains, now tracked
under ROADMAP V9.6.5.

#### 🔴 Critical

- [x] **N1 — AOF replay silently loses the whole RESP tail.** FIXED 2026-07-13.
  Cause: the replay user had `allow_cats = CAT_ALL` but never `all_keys = true`, so
  the `acl_check` key gate ran with empty `key_patterns` and NOPERM'd every keyed
  tail command into the discarded sink. Fix: `replay_user.all_keys = true`
  (`aof.cpp:233`), plus the bug class is now loud instead of silent — the replay loop
  counts error replies in the sink, a WARNING with the first offending command prints
  at the end, and `aof_load` returns false on any replay error (`aof.cpp:240-296`).
  Regression: `scripts/test_aof_restart.py` (3 server lifetimes; plain-AOF replay + hybrid
  preamble-and-delta replay) — all data checks green 2026-07-13. Archived in the
  Resolved Bugs Archive below.
- [x] **N2 — `migrate_pos` never reset.** FIXED 2026-07-13: `hmap->migrate_pos = 0;`
  added in `hm_trigger_rehashing()` (`hashtable.cpp:74`). Cause: the cursor survived
  from the previous drain, so from the second rehash on the low buckets of `older`
  were stranded, `older.tab` was never freed, and `hm_insert`'s `!older.tab` gate
  blocked every future resize — silent O(n) degradation, no data loss (lookups fell
  back to `older`). Regression: `scripts/test_hashtable.cpp` (standalone, ~5 rehash
  cycles; asserts the draining table empties and is released) — red on the old code,
  4/4 green with the fix. Archived in ROADMAP → Resolved Bugs Archive.
- [x] **N3 — timer sentinel `(uint8_t)-1`.** FIXED 2026-07-13: `(uint64_t)-1`
  (`server.cpp:231`). Cause: 255 is a quarter-second-after-boot *timestamp*, so with
  any write pending `next_timer_ms()` returned 0 both when a save condition matched
  (255 beat any real deadline) and when none did (255 passed the `!= (uint64_t)-1`
  "found one" guard) — `poll()` busy-spun at ~100% CPU until the save fired. Verified
  in-tree + by `top` before/after a `SET`: ~100% → ~0%. **Was a TLS prerequisite.**
- [x] **N4 — protocol-error wedge / no input cap.** FIXED 2026-07-13: malformed frame
  → `-ERR Protocol error` + `want_close` (`server.cpp:413-417`); per-conn cap
  `k_max_incoming` = 2×`k_max_msg` in `handle_read`. **Was a TLS prerequisite.**
- [x] **`aof_check()` crash + inverted read.** FIXED 2026-07-13: `return false` after
  `fopen` failure; `fread(...) != (size_t)sz` (`aof.cpp:316,326`).
- [x] **`ZPOPMIN` not `is_write`.** FIXED 2026-07-13: row flagged write
  (`commands.cpp:3268`); frame verified in the AOF (handler already dirty-counted and
  was OOM-gate-exempt).
- [x] **BGREWRITEAOF discarded at shutdown.** FIXED 2026-07-13: finalize extracted to
  `aof_rewrite_reap()`; `aof_rewrite_wait_shutdown()` (`aof.cpp:190`) blocks and
  finalizes at shutdown (`server.cpp:751`), AOF fd close reordered after it.

#### 🟠 Bugs

- [x] **N6 — `GETSET` no reaccount + kept TTL.** FIXED 2026-07-13: overwrite path
  now discards the TTL and reaccounts (verified in tree).
- [x] **N7 — `SET` keeps TTL on overwrite.** FIXED 2026-07-13: `entry_set_ttl(ent, -1)`
  on the `do_set` overwrite path; SETNX/SETEX verified already correct.
- [x] **`ZREM`/`ZPOPMIN` reaccount + empty-key drop.** FIXED 2026-07-13: both drop
  the emptied key and reaccount survivors.
- [x] **`GETEX` TTL changes not dirty-counted + EXAT overflow.** FIXED 2026-07-13:
  `entry_has_ttl`-guarded PERSIST bump, dirty bump on the future path, `INT64_MAX/1000`
  guards on EX/EXAT, negative EXAT short-circuits to the delete path.
- [x] **N8 — `rdb_save` fsync before stdio flush.** FIXED 2026-07-13: `fflush(fp)`
  checked before `fsync(fileno(fp))`.
- [x] **N9 — `g_dirty_at_save` misplaced.** FIXED 2026-07-13: snapshot captured at
  serialize time in `rdb_save_background()`; busy-branch clobber removed. (Found
  worse than filed: the success path never captured it at all.)
- [x] **N10 — compressed-RDB bounds.** FIXED 2026-07-13: `payload_size >= 4` check +
  `usize` capped by zlib's 1032:1 max expansion ratio.
- [x] **N11 — `SRANDMEMBER` negative count unclamped.** FIXED 2026-07-13:
  `INT64_MIN` + `-count > k_max_args` rejected with `ERR count is out of range`.
- [x] **N12 — `acl_bootstrap_default` clobbers a configured default user.** FIXED
  2026-07-13: bootstrap respects a config-defined `default` (only fills `pw_hashes`
  from `requirepass` when the operator gave no password rule). Note: `config_rewrite`
  still skips emitting `default` — folded into the rewrite-coverage item below.
- [x] **N13 — directive spelling.** FIXED 2026-07-13: hyphenated
  `auto-aof-rewrite-*` accepted (`state.cpp:286,297`), old spellings kept as aliases.
- [x] **`parse_cidr` validates the wrong variable.** FIXED 2026-07-13:
  `parsed < 0 || parsed > 32` verified in tree; `/33` and `/-1` both rejected.
- [x] **`SAVE`/shutdown hardcode `dump.rdb` + double-complete.** FIXED 2026-07-13:
  both route through `g_config.dump_path`; `do_save`'s duplicate
  `rdb_on_save_complete` call removed (`rdb_save` owns it).
- [x] **AOF rewrite tmp/finalize fragility.** FIXED 2026-07-13: tmp derived from
  `aof_path` (built pre-fork); `aof_rewrite_reap` checks delta write/fsync/rename,
  only repoints the fd after a successful swap, single `ok` exit sets
  `g_aof_last_rewrite_ok` and cleans the tmp on any failure.
- [x] **N21 — background-child reaping waits for a poll wakeup.** FIXED 2026-07-13:
  `next_timer_ms()` caps the poll timeout at `now_ms + 100` whenever
  `g_rdb_child_pid`/`g_aof_child_pid` is live (`server.cpp:242-244`), so a finished
  child is reaped within ~100 ms even on a fully idle server. Cause (found by
  `scripts/test_aof_restart.py`): the reap/finalize checks only run per loop
  iteration, and a clean idle server had no periodic tick, so a finished BGREWRITEAOF
  sat unreaped with a stale `.tmp` until the next client event (>10 s observed).
  Regression guard: the 10 s "AOF became hybrid" check in
  `scripts/test_aof_restart.py`.

#### 🟡 Robustness

- [x] **N14 — `next_timer_ms` return overflow.** FIXED 2026-07-13: the return is
  clamped — `wait > INT32_MAX` returns `INT32_MAX` (`server.cpp:252-254`). Previously
  a timer >24.8 days out (far-future TTL) cast negative and `poll()` blocked forever.
- [x] **N15 — idle/io timer split is dead code.** FIXED 2026-07-13: poll loop routes
  through `conn_set_timer(conn, conn->timer_type)`; `handle_write` sets IDLE when the
  reply drains. Classes are now real: accept→IO, reply→IDLE, read→IO.
- [x] **N16 — `kek1234` fallback password.** FIXED 2026-07-13: fallback deleted; no
  password = nopass default user + protected-mode loopback-only (Redis posture).
- [x] **N17 — RDB loader leaks + blind expired-skips.** FIXED 2026-07-13: set loader
  frees the partial entry; all five loaders' skip paths check every cursor read.
- [x] **N18 — `Buffer` zero-capacity growth loop.** FIXED 2026-07-13:
  `new_cap = old_cap ? old_cap * 2 : 64`.
- [x] **Loose `atoi` config/env parsing.** FIXED 2026-07-13: `parse_int_strict` /
  `parse_bool_strict` shared by config directives (port, samples, aof knobs,
  appendonly, protected-mode) and env overrides.
- [x] **`config_rewrite` drops directives + non-atomic.** FIXED 2026-07-13: emits
  bind/protected-mode/allow-ip/auto-aof knobs; atomic tmp+fsync+rename. LFU knobs
  deferred (no load directives exist yet). `default` user still skipped (see N12
  note). Tmp-suffix space typo fixed same day.
- [x] **`SADD`/`ZADD` no-op writes dirty the AOF.** FIXED 2026-07-13: SADD gates on
  `added > 0`; ZADD tracks `changed` incl. score updates (`zset_update` exported).
- [x] **`S*STORE` empty result leaves an empty destination.** FIXED 2026-07-13:
  shared `set_store_result()` deletes the destination on an empty result (dirty-bumps
  only when a pre-existing dest was removed); three handlers deduped through it.
- [x] **N19 — reply-semantics divergences.** FIXED 2026-07-13 (all four re-verified
  live first): `LSET` missing → `ERR no such key`; `LTRIM` missing → `OK`;
  `SPOP` non-int/negative count → error; `SCAN`/`HSCAN`/`SSCAN` reject negative
  cursors.
- [x] **N20 — client.cpp `std::stoi` throws.** FIXED 2026-07-13: `parse_reply_int`
  (strtol, whole-string, range-checked) at both `$`/`*` header sites.
- [x] **AOF-load-failure policy.** DECIDED + FIXED 2026-07-13: fail-fast —
  `fatal_exit` on `aof_load` failure (unreadable file or replay divergence via the N1
  error counter); operator repair path is `--check-aof --fix`.
- [x] **`die()` vs `fatal_exit()` split.** FIXED 2026-07-13: `fatal_exit` (print +
  `exit(1)`, with `strerror`) at the five operational sites; `panic` (print +
  `abort()`) kept only for the mid-run `poll()` failure. Verified live: bad config
  now exits `fatal: invalid config file`, no core dump. Original finding: `die()`
  (`server.cpp:57`) prints then `abort()`s — a core-dump-producing, crash-looking exit
  — for routine startup mistakes: bad config path (`server.cpp:536`), listener setup
  (`:636`), fcntl (`:67,74`), eventfd (`:644`). Reproduced: a typo'd config filename
  prints the error then `Aborted (core dumped)`. Fix: `panic(msg)` (print + `abort()`)
  reserved for internal-invariant violations — the runtime `poll()` failure (`:672`) is
  the one plausible keeper — and `fatal_exit(msg)` (print + `exit(1)`) for the
  operational sites.

#### 🔵 Performance / ⚪ polish (fix only after everything above is green)

##### Easy — single line, single file, no design tradeoff

- [x] **`hash_set` copies on both paths.** FIXED 2026-07-16: by-value param de-consted
  (a `const` by-value param makes `std::move` silently copy), both branches move.
- [x] **`str_hash` upper 32 bits always zero.** FIXED 2026-07-16: real 64-bit FNV-1a
  (64-bit offset basis + prime).
- [x] **`mem_selfcheck` blames the wrong command.** FIXED 2026-07-16: call moved to
  the end of `do_request`, after the handler + AOF feed.

##### Harder — multi-file, or a real design task

- [x] **`SPOP`/`SRANDMEMBER` full materialization.** FIXED 2026-07-16: new `hm_random`
  hashtable primitive (weighted two-table bucket walk, deduped with
  `db_random_entry`); SPOP pops by node — removal makes distinctness free, O(k);
  SRANDMEMBER positive count uses pop-and-reinsert for distinct sampling, negative
  count k independent draws. Follow-up: SPOP AOF replay nondeterminism filed in
  ROADMAP Known Bugs 2026-07-16.
- [x] **SPOP AOF replay nondeterminism** (follow-up to the item above). FIXED
  2026-07-16: new `CmdSpec::aof_self` flag (dispatcher skips verbatim logging);
  `do_spop` feeds a synthetic deterministic `SREM key member...` of the
  actually-popped members via `aof_feed` — same mechanism as eviction's synthetic
  DEL. Replay-equivalent because `do_srem` drops emptied keys.
- [x] **Set-algebra store commands double-copy.** FIXED 2026-07-16: `set_add` takes
  `std::string` by value + `std::move` (same idiom as `hash_set`); all four call
  sites move (`do_sadd`, `set_store_result`, `do_smove`, RDB set loader). Bonus: the
  call-site audit surfaced the 🔴 `rdb_load_set_entry` non-TTL data-loss bug (see
  ROADMAP Known Bugs, also fixed 2026-07-16).
- [x] **Eviction deletes up to 100 victims synchronously.** FIXED 2026-07-17:
  Redis EVICT_RUNNING semantics — when the 100-victim batch runs out with victims
  remaining, the write is admitted and `g_data.g_evict_pending` arms `evict_tick()`
  (event loop) + a zero poll timeout until under the limit; OOM now means "policy
  can't free", not "ran out of patience". Verified live by
  `scripts/test_evict_tick.sh` (50k→5275 keys drained idle in <1s; probe writes
  admitted during overshoot). Bonus fixes en route: ECHO command added +
  empty inline commands silently ignored (redis-cli --pipe compat).
- [x] **`INFO` O(N) keyspace scan.** FIXED 2026-07-17: no scattered counters needed —
  the TTL heap already IS the `with_ttl` counter (`heap_idx != NO_TTL` ⇔ heap
  membership, maintained by every TTL/expire/delete path); `total` is
  `hm_size(&g_data.db)`. Two O(1) reads, `cb_count_keys` deleted.

#### Testing debt — MOVED to ROADMAP V9.6.5 (general and speed test) 2026-07-17

- [x] AOF-restart-with-ACL test; restart tests for `GETEX`, `GETDEL`, `ZPOPMIN`,
  eviction `DEL`, renamed-command frames. DONE 2026-07-18:
  `scripts/test_aof_restart.py` + `scripts/test_restart_matrix.py` (green).
- [x] Security tests. DONE 2026-07-18: `scripts/test_security.py` (green) —
  gating, rename/disable, audit redaction, key ACLs + SMOVE resolver, `ACL CAT`
  framing, CONFIG REWRITE round-trip.
- [x] Destructive/server-crashing edge cases. DONE 2026-07-18: `--destructive`
  flags in both suites (SIGKILL crash recovery; protocol abuse + liveness).

### C. Not bugs — feature gaps returned to ROADMAP Backlog

Full Redis ACL rule-order fidelity ("last match wins"); Pub/Sub channel-pattern
enforcement (blocked on V8); `nopass`, selectors, `sanitize-payload`, `ACL LOAD`,
`ACL SAVE`; full `CONFIG GET/SET` coverage; `COMMAND` / `COMMAND DOCS` /
`COMMAND COUNT`.

### Suggested fix order (V9.6.4)

1. **N1** + the AOF-restart-with-ACL regression test (the data loss is live today);
   fold `ZPOPMIN is_write` and the `GETEX` dirty-count fix into the same test batch.
2. **N3** and **N4** — two-line loop fixes with big blast radius; both are TLS
   prerequisites.
3. **N2** — one line, then a soak check (insert 10M keys; `older.tab` frees, lookups
   stay flat).
4. Persistence batch: `aof_check`, shutdown AOF finalize, N8, N9, N10, rewrite
   tmp/finalize, `SAVE` dbfilename.
5. Accounting/semantics batch: N6, N7, `ZREM`, `SADD`/`ZADD` no-ops, `S*STORE` empty
   dest, N11.
6. Config/security batch: N12, N13, N16, `parse_cidr`, strict parsers,
   `config_rewrite` coverage + atomic write, `die()`/`fatal_exit` split.
7. Robustness polish: N14, N15, N17, N18, N19, N20, AOF-load policy.
8. 🔵/⚪ items last.

## Resolved Bugs Archive

This section records fixed bugs without scattering them through milestone text.

### V9.6.4 Bug Sweep — 2026-07-13

The entire consolidated worklist above (every 🔴, 🟠, and 🟡 item) was fixed and
verified in a single sweep on 2026-07-13; each `[x]` entry in section B carries its
own cause/fix/evidence, so they are not duplicated here. Highlights with dedicated
archive entries below: N1 (AOF replay data loss), N2 (`migrate_pos`), the
`next_timer_ms` triple (N3/N14/N21). Still open after the sweep: the 🔵/⚪
performance-and-polish list and the Testing-debt items.

### Post-V10 Carry-overs — 2026-08-11

The two items left open when V10 closed, fixed together once replication shipped.

- 🔴 **`SPOP`'s synthetic `SREM` frame carried an empty key.** `lookup_entry()`
  takes `std::string &keystr` and its first act is `key.key.swap(keystr)` into a
  default-constructed `LookupKey` — so the caller's string is left empty, and only
  the *create* path ever moves it somewhere durable (`ent->key.swap(key.key)`).
  `do_spop` passes `create=false` and then read `cmd[1]` to build its AOF/replica
  frame, logging `SREM "" <member>`, which replays as a no-op against a
  nonexistent key. Every popped member came back on AOF reload and on a replica.
  Live since V9.6.4 (2026-07-16), when `CmdSpec::aof_self` and the synthetic frame
  landed. Fixed by reading `ent->key`: the found entry's own copy, which cannot
  drift from the entry being mutated and costs nothing on the non-feed path.
  - **Found by watching the V10.2a replication stream, not by any suite** — see
    BACKLOG → V11 Step 0 for the coverage gap it exposed.
  - Generalisable, and the second instance of this family in the archive (see
    "Handlers that `swap()` command strings…" under Persistence and AOF): **a
    handler must not read `cmd[N]` after passing it to `lookup_entry`.** A scan of
    every `do_*` found this as the only remaining case; re-run it after adding any
    handler that builds its own propagation frame.

- 🟡 **`ACL GENPASS` minted passwords from a non-cryptographic PRNG.** The branch
  built its hex string with `hx[rand_idx(16)]`, and `rand_idx` is a single
  process-wide `std::mt19937_64`. Mersenne Twister is fully reconstructible from
  its output — 624 observed 32-bit words recover the state and with it every past
  and future draw — so an attacker holding any one `GENPASS` result could derive
  every other password the same process ever generated. A *generator* weakness,
  not a storage one: Argon2id still protected the stored hash.
  - Fixed by promoting what already existed rather than writing a third
    generator. `cred.cpp` had a `getrandom(2)`-backed `fill_random()` (used for
    Argon2id salts, already commented "NOT g_rng"), and `cred_dummy()` already
    carried the bytes→hex loop. Both now sit behind one exported
    `cred_random_hex(nhex)` in `cred.h`; `cred_dummy` was rewritten onto it, so
    the hex encoder exists once.
  - **The new path fails closed**: `cred_random_hex` returns `""` on entropy
    failure and `genpass` turns that into an error reply. A predictable password
    that looks random is worse than a visible failure. `cred_dummy` keeps its old
    tolerance — its only job is to burn constant time, so a deterministic fallback
    there is harmless and deliberate.
  - Scope held deliberately narrow: `rand_idx()` itself is unchanged, because
    eviction sampling, `RANDOMKEY`, `SPOP` and `hm_random` call it on hot paths and
    want a fast PRNG, not a syscall. `g_data.repl_id` also still uses it — a
    replication ID is a public identifier published in `INFO`, not a secret.
  - Rejected from the filed plan: the `GRND_NONBLOCK` + `/dev/urandom` fallback.
    `getrandom` with `flags=0` blocks only until the kernel pool is initialised,
    which has already happened on a running server, so the fallback would be a
    second code path that can essentially never execute.

### V10.6c/d Apply Slips — 2026-08-13

Five defects introduced while hand-applying the V10.6c/d snippets, all found and
fixed the same day. None shipped; they are archived because the *shape* recurs
and four of the five were invisible to `-Wall -Wextra`.

- 🔴 **The `READONLY` gate was deleted.** The `min-replicas-to-write` check was
  meant to be inserted *after* the read-only check and instead **replaced** it, so
  every replica in the tree accepted writes and V10.3a was silently undone. The
  comment above it (`// Read-only replica`) survived, which is what made the diff
  look right. **Rule: after applying an "insert after X" snippet, grep that X is
  still there** — verifying only that the new lines landed cannot detect this.
- 🔴 **`FAILOVER TO` rejected every usable port**: `port < 1 || port < 65535`
  where the second test means `> 65535`. Identical to V10.5's `REPLCONF
  listening-port` bug (`p > 65536` for `p < 65536`) — same subsystem, same
  inverted comparison, four days apart. Both were caught by a test that used a
  realistic port; neither is visible by reading the line in isolation, because
  each half is individually plausible.
- 🔴 **`FORCE` was parsed, validated, and discarded.** `do_failover` never wrote
  `g_data.failover_force = force;`, so the flag was only ever assigned `false` and
  `failover_cron` always took the "timed out, aborting (no FORCE)" branch. The
  code reads correctly at every point; only a runtime test that actually forces a
  handover past a lagging target can see it. Found by
  `scripts/test_replication.py`'s V10.6d phase on its first run.
- 🟠 **`failover_state` was emitted inside `INFO`'s `if (replica)` block**, so the
  one state it exists to report — `WAIT_FOR_SYNC`, which only ever occurs on a
  *master* — was unobservable. A half-applied repair then left `master_replid`
  emitted twice for a replica.
- 🟡 **`min-replicas-max-lag` defaulted to 0** instead of 10, which means "do not
  judge on lag": every connected replica counts, including one still loading its
  image, quietly turning off the half of V10.6c that makes the floor mean
  anything.

### Persistence and AOF

- AOF replay ran under an incomplete synthetic superuser (`all_keys` unset), so the
  ACL key gate silently NOPERM'd the whole RESP tail on every restart — plain AOF
  loaded empty, hybrid AOF lost the delta (N1, fixed 2026-07-13; replay errors are
  now counted and reported; regression: `scripts/test_aof_restart.py`).
- Handlers that `swap()` command strings required `do_request` to snapshot `cmd`
  before calling `spec.fn()`.
- AOF write path needed a verbatim fallback in `aof_feed`.
- `SETEX` and related TTL commands were missing counter bumps.
- `STRLEN` was mistagged as a write command.
- `BGREWRITEAOF` used typo `appebdonly.aof.tmp`, making finalize a silent no-op.
- AOF load priority originally parsed `aof_enable` too late, so startup loaded RDB by
  mistake.
- AOF file open had to happen after load, not before load.
- `g_last_save_ms` was uninitialized, causing an immediate spurious `BGSAVE` on first
  write.
- `aof_feed` branches returned before appending relative TTL frames.
- `g_aof_child_pid != 1` should have been `!= -1`; the bug mirrored writes outside
  rewrites and could grow `g_aof_rewrite_buf` unbounded.
- `GETEX` AOF translation now emits deterministic `PEXPIREAT`, `PERSIST`, or `DEL`.
- AOF truncation handles partial or corrupt tails and keeps the last good offset.
- Disk-full AOF errors now reject future writes with `MISCONF` while reads continue.
- `SIGXFSZ` and `SIGPIPE` are ignored so write failures return errno instead of
  killing the server.

### Memory Management

- `LPOP` and `RPOP` reaccounting after `entry_del()` caused use-after-free patterns.
- `MSET` and `MSETNX` needed per-entry reaccounting, not one outside-loop reaccount.
- OOM gate had to exempt memory-freeing commands such as `DEL`, `UNLINK`, `FLUSHALL`,
  `EXPIRE`, pop/rem commands, and related shrinking commands.
- Evictions must propagate explicit `DEL` to AOF so replay does not resurrect evicted
  keys.

### Config and Auth

- **`appendonly`'s getter read `g_config.protected_mode`** (V9.8.2, fixed
  2026-07-30). Introduced while migrating the directive into `k_config_table`, and
  a persistence bug rather than a display one: V9.8.1 had already routed
  `CONFIG REWRITE` through the getter, so a rewrite wrote
  `appendonly <protected-mode's value>` to disk. A server with `protected-mode yes`
  + `appendonly no` silently gained AOF; the reverse silently lost durability on
  the next restart. Every suite passed because `myred.conf` sets both to `yes`.
  **The generalisable lesson:** a `format → apply → format` round-trip cannot
  detect a getter bound to the wrong field — it is self-consistent. Only a probe
  that writes a *distinct* value and reads it back can, which is now the `[REG]`
  block in `stress_test.py`'s CONFIG section.
- **`tls-auth-clients` rejected `no`** (pre-existing, fixed 2026-07-30 during the
  same migration). The branch read `else if (v == "nos")`, so the documented value
  `no` fell through to the error path — and since an invalid directive is fatal at
  boot, writing it explicitly made the server refuse to start. It survived because
  the default is already `NO` and `config_rewrite` only emits the directive when it
  differs, so the value never round-tripped. Message typo `mus be` fixed alongside.
- A leftover hardcoded `password = kek1234` clobbered config-loaded passwords.
- `config_tokenize()` had a pre-increment bug that dropped each token's first char.
- `#<hash>` ACL/config tokens must be quoted in config rewrite because `#` starts a
  comment.
- Pre-hashed ACL token validation checked the wrong length for `#` plus 64 hex chars.

### ACL

- `acl_init_categories()` was not called at boot, so command ACL categories stayed `0`.
- `acl_init_categories()` needed a prototype in `state.h`.
- `ACL` category bits were accidentally placed in the key-spec map instead of the
  extra-category map.
- `ACL` needed `KeySpec::NONE`.
- `AUTH <user> <pass>` had an extra plaintext password copy that needed wiping.
- ACL deny parser branches checked the wrong token prefix for `-@cat` and `-cmd`.
- `acl_apply_rule()` missed a final `return false` for unrecognized tokens.
- `ACL GENPASS` had unreachable or wrongly nested code and could send no reply.
- `resetkeys` did not clear `all_keys`.
- `ACL LIST` originally hid partial `~pattern` and `+@cat` rules; it should use the
  same formatter as config rewrite.

### General Hardening

- `next_timer_ms()` had three defects fixed together 2026-07-13: the save-condition
  sentinel was `(uint8_t)-1` = 255, making `poll()` busy-spin at ~100% CPU whenever a
  write was pending (N3); the `int32_t` return overflowed negative for timers >24.8
  days out, turning `poll()` into an infinite block (N14); and nothing bounded the
  timeout while a background save/rewrite child was live, so an idle server never
  reaped finished children (N21 — the 100 ms child tick fixes it; regression:
  `scripts/test_aof_restart.py`).
- `HMap::migrate_pos` was never reset when a new rehash started, so from the second
  rehash on entries were stranded in `older` and all future resizes were blocked —
  silent O(n) degradation on long-running instances, no data loss (N2, fixed
  2026-07-13; regression: `scripts/test_hashtable.cpp`, standalone unit test).
- `RDB` save fork/malloc deadlock was avoided by serializing in the parent before fork.
- RDB loaders gained bounds checks.
- RDB save uses `.bak` rotation before atomic rename.
- `INFO` buffer was increased and `snprintf` length handling was clamped to avoid OOB
  reads.
- `accept()` loop, `EINTR`, `SO_REUSEADDR`, `TCP_NODELAY`, and thread-pool shutdown
  were hardened during the project-wide review.



