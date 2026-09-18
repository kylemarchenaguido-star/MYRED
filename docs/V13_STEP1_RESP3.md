# V13 Step 1 — RESP3

Apply order matters: **1 → 2 → 3 → 4** must land together or the build breaks
(4 calls what 2/3 declare). **5 onward are independent** and can land one at a
time, each verifiable on its own.

Every reply shape below was **measured against a real `redis-server` 7.0.15**,
RESP2 vs RESP3 on raw sockets, not taken from the spec. The measurement matters
because it removed work as often as it added it.

## What actually changes (measured, 2026-09-18)

**Changes:**

| reply | RESP2 | RESP3 |
|---|---|---|
| `HGETALL`, `CONFIG GET` | `*2n` flat | `%n` map |
| `SMEMBERS`, `SDIFF`, `SINTER`, `SUNION`, `SPOP key N` | `*n` | `~n` set |
| `ZSCORE`, `ZADD ... INCR` | `$` bulk | `,` double |
| null (`GET` miss, `HGET` miss, `LPOP` miss) | `$-1` | `_` |
| aborted `EXEC` | `*-1` | `_` |
| `INFO` | `$` bulk | `=` verbatim, `txt:` prefixed |
| pub/sub message **and** subscribe/unsubscribe confirmations | `*n` | `>n` push |
| empty hash / empty set | `*0` | `%0` / `~0` |

**Does NOT change — do not touch these:**

- `SISMEMBER`, `EXPIRE`, `PERSIST`, `SETNX`, `EXISTS`, `TTL`, `COMMAND COUNT`
  stay `:` **integers**. RESP3 has a boolean type; Redis does not use it here.
  **No `resp_bool` is needed anywhere.**
- `INCRBYFLOAT`, `HINCRBYFLOAT` stay `$` **bulk strings**, not doubles.
- `KEYS`, `SCAN`, `LRANGE`, `HMGET`, `SRANDMEMBER`, `HRANDFIELD WITHVALUES`
  stay `*` flat arrays. An **empty array stays `*0`** — only empty *maps* and
  *sets* change.
- Errors stay `-`. RESP3's blob error `!` is not used by Redis for these.

**The nesting trap** — `WITHSCORES` is not a flat array in RESP3:

```
ZRANGE z 0 -1 WITHSCORES
  RESP2: *4  $2 m1  $3 1.5   $2 m2  $3 2.5
  RESP3: *2  *2 $2 m1 ,1.5   *2 $2 m2 ,2.5      <- array of [member, double] PAIRS
```

Same for `ZRANGEBYSCORE WITHSCORES`, `ZRANDMEMBER WITHSCORES`, and
`ZPOPMIN key N`. But **`ZPOPMIN key` with no count stays flat**: `*2 $2 m1 ,1.5`.
This binds Step 1 to Step 2 — the new zset commands must emit the nested form.

---

## 1. `state.h` — per-connection protocol

`Conn` ends at **line 197**. Add before the closing `};`:

```cpp
  // RESP protocol for this connection: 2 (default) or 3, set by HELLO and
  // never changed otherwise. Reply *shape* follows it - maps, sets, doubles,
  // nulls and pub/sub pushes all differ. A client that never sends HELLO stays
  // on 2, which is what every pre-7.0 client speaks.
  int resp_proto = 2;
  std::string client_name; // CLIENT SETNAME / GETNAME; empty = unnamed
```

## 2. `resp.h` — declarations

Append to the end of the file:

```cpp
// ── RESP3 ──────────────────────────────────────────────────────────────────
// Reply shape depends on the protocol of the connection being replied to.
// Command handlers take only (cmd, out) and never see the Conn, so do_request
// publishes the current connection's version here before dispatch and restores
// it after. Safe because command dispatch is single-threaded: the thread pool
// only runs auth verification, AOF fsync and entry frees, never handlers.
//
// Cross-connection writes must NOT read this - they are emitted while some
// *other* connection is the one being dispatched. Pub/sub delivery is the only
// such path and it takes the target's version explicitly via resp_push_n().
extern int g_reply_proto;

void resp_map(Buffer *out, uint32_t n);  // n PAIRS -> %n (RESP3) / *2n (RESP2)
void resp_set(Buffer *out, uint32_t n);  // ~n (RESP3) / *n (RESP2)
void resp_push_n(Buffer *out, uint32_t n, int proto); // >n / *n, explicit proto
void resp_verbatim(Buffer *out, const char *s, size_t len); // =txt: / $
```

## 3. `resp.cpp` — the writers

Add `#include <cmath>` to the include block (line 1-7); `resp_dbl` needs
`isinf`/`isnan` for RESP3.

**Replace `resp_nil` (line 93) and `resp_nil_arr` (line 98) in place:**

```cpp
// NIL response. RESP3 has one null for every context; RESP2 needs the bulk
// form here and the array form below, which is why both exist.
void resp_nil(Buffer *out){
  if (g_reply_proto >= 3){ return buf_append(out, "_\r\n", 3); }
  buf_append(out, "$-1\r\n", sizeof("$-1\r\n") - 1);
}

// null distinct from the null bulk string (used by EXEC): RESP2 null array
void resp_nil_arr(Buffer *out){
  if (g_reply_proto >= 3){ return buf_append(out, "_\r\n", 3); }
  buf_append(out, "*-1\r\n", sizeof("*-1\r\n") - 1);
}
```

**Replace `resp_dbl` (line 139) in place:**

```cpp
// DBL response. RESP2 sends a bulk string; RESP3 has a real double type with
// its own spellings for the non-finite values.
void resp_dbl(Buffer *out, double val){
  char tmp[64];
  if (g_reply_proto >= 3){
    int len;
    if (std::isnan(val)){
      len = snprintf(tmp, sizeof(tmp), ",nan\r\n");
    } else if (std::isinf(val)){
      len = snprintf(tmp, sizeof(tmp), val < 0 ? ",-inf\r\n" : ",inf\r\n");
    } else {
      len = snprintf(tmp, sizeof(tmp), ",%.17g\r\n", val);
    }
    return buf_append(out, tmp, (size_t)len);
  }
  int len = snprintf(tmp, sizeof(tmp), "%.17g", val);
  resp_str(out, tmp, (size_t)len);
}
```

**Append the new writers at the end of the file:**

```cpp
// dispatch-scoped; see resp.h. Not thread-local: dispatch is single-threaded.
int g_reply_proto = 2;

// MAP header. n is the number of PAIRS, not the number of elements - RESP2
// doubles it, RESP3 does not. Passing an element count here is the easy bug.
void resp_map(Buffer *out, uint32_t n){
  char tmp[32];
  int len = (g_reply_proto >= 3) ? snprintf(tmp, sizeof(tmp), "%%%u\r\n", n)
                                 : snprintf(tmp, sizeof(tmp), "*%u\r\n", n * 2);
  buf_append(out, tmp, (size_t)len);
}

// SET header. Same element count in both; only the type byte differs.
void resp_set(Buffer *out, uint32_t n){
  char tmp[32];
  int len = (g_reply_proto >= 3) ? snprintf(tmp, sizeof(tmp), "~%u\r\n", n)
                                 : snprintf(tmp, sizeof(tmp), "*%u\r\n", n);
  buf_append(out, tmp, (size_t)len);
}

// PUSH header. Takes proto explicitly: pub/sub writes into a *subscriber's*
// buffer while a different connection is being dispatched, so the global is
// the wrong one. Two subscribers on one PUBLISH can be on different protocols.
void resp_push_n(Buffer *out, uint32_t n, int proto){
  char tmp[32];
  int len = (proto >= 3) ? snprintf(tmp, sizeof(tmp), ">%u\r\n", n)
                         : snprintf(tmp, sizeof(tmp), "*%u\r\n", n);
  buf_append(out, tmp, (size_t)len);
}

// VERBATIM string: RESP3 tags the format so a client can render it. The length
// covers the "txt:" prefix - real Redis sends 624 where RESP2 sent 620.
void resp_verbatim(Buffer *out, const char *s, size_t len){
  if (g_reply_proto < 3){ return resp_str(out, s, len); }
  char tmp[32];
  int hlen = snprintf(tmp, sizeof(tmp), "=%zu\r\n", len + 4);
  buf_append(out, tmp, (size_t)hlen);
  buf_append(out, "txt:", 4);
  buf_append(out, s, len);
  buf_append(out, "\r\n", sizeof("\r\n") - 1);
}
```

## 4. `commands.cpp` — publish the protocol at dispatch

Add near the top of the file, above `do_request`:

```cpp
// Publishes a connection's RESP version for the reply writers and restores the
// previous one on every exit path. do_request has ~20 early returns, nests for
// EXEC, is driven for other connections by fo_resume_paused_writes(), and is
// replayed with a fake Conn by AOF load and the replication link - a plain
// assignment leaks the wrong version into all four. RAII, not discipline.
struct ReplyProtoScope {
  int prev;
  explicit ReplyProtoScope(int proto) : prev(g_reply_proto) {
    g_reply_proto = proto;
  }
  ~ReplyProtoScope() { g_reply_proto = prev; }
};
```

In **`do_request` (line 5398)**, make this the first statement — before the
`cmd.empty()` check, so every return path is covered:

```cpp
void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn,
                const char *raw, size_t raw_len) {
  ReplyProtoScope proto_scope(conn->resp_proto);

  if (cmd.empty()) {
```

## 5. `HELLO`

`HELLO` runs **before authentication** (it can carry `AUTH`), so it is
intercepted like `auth` rather than living in `k_cmd_table`. In `do_request`,
immediately after the existing `if (cmd[0] == "auth")` block:

```cpp
  if (cmd[0] == "hello") {
    return do_hello(cmd, out, conn);
  }
```

### 5a. Why `HELLO ... AUTH` has to defer

**`do_auth` is fully asynchronous.** It queues an `AuthJob` to the thread pool
and writes *no reply*; `auth_complete()` writes `+OK` into `conn->outgoing`
later, from `loop_post`. So `HELLO 3 AUTH user pass` cannot be answered inline.

And it cannot simply be rejected either. `redis-py` sends the credentials
*inside* `HELLO` whenever a password is set — its RESP3-with-auth path is
`send_command("HELLO", 3, "AUTH", user, pass)` as **one** command. Refusing the
`AUTH` option would mean no authenticated RESP3 connection is possible at all,
which matters because MYRED is meant to run with `requirepass`.

So the handshake map has to be emitted from the auth completion path when
`HELLO` carried credentials. One extra `Conn` field carries the intent across.

Add to `Conn` in `state.h`, next to `resp_proto` from step 1:

```cpp
  // >0 while an async AUTH issued by HELLO is in flight: the protocol to
  // switch to, and the signal that auth_complete() must answer with the HELLO
  // map instead of +OK. Cleared on both outcomes.
  int hello_pending_proto = 0;
```

### 5b. The handler

```cpp
// The handshake map. Shared by the inline path and the deferred auth path, so
// the two can never drift. Caller has already set g_reply_proto to `proto`.
static void hello_emit_map(Buffer *out, Conn *conn, int proto) {
  resp_map(out, 7);
  resp_str(out, "server", 6);   resp_str(out, "myred", 5);
  resp_str(out, "version", 7);  resp_str(out, "1.0.0", 5);
  resp_str(out, "proto", 5);    resp_int(out, proto);
  resp_str(out, "id", 2);       resp_int(out, (int64_t)conn->id);
  resp_str(out, "mode", 4);     resp_str(out, "standalone", 10);
  resp_str(out, "role", 4);
  if (g_data.replica_mode) { resp_str(out, "replica", 7); }
  else                     { resp_str(out, "master", 6); }
  resp_str(out, "modules", 7);  resp_arr(out, 0);
}

// HELLO [protover [AUTH user pass] [SETNAME name]]
// Redis allows re-HELLO at any time, including downgrading 3 -> 2, so this is
// not one-shot. Error texts below are what a real redis-server returns.
static void do_hello(std::vector<std::string> &cmd, Buffer *out, Conn *conn) {
  int want = conn->resp_proto; // no argument: report, don't change

  if (cmd.size() >= 2) {
    int64_t v = 0;
    if (!str2int(cmd[1], v)) {
      return resp_err(out,
                      "ERR Protocol version is not an integer or out of range");
    }
    if (v != 2 && v != 3) {
      return resp_err(out, "NOPROTO unsupported protocol version");
    }
    want = (int)v;
  }

  // Scan the options first: nothing may take effect until the whole command is
  // known good, or a bad SETNAME after a good AUTH leaves half a handshake.
  size_t auth_at = 0, name_at = 0;
  for (size_t i = 2; i < cmd.size();) {
    std::string opt = cmd[i];
    for (char &ch : opt) { ch = (char)tolower((unsigned char)ch); }
    if (opt == "auth" && i + 2 < cmd.size())      { auth_at = i; i += 3; }
    else if (opt == "setname" && i + 1 < cmd.size()) { name_at = i; i += 2; }
    else {
      return resp_err(out, "ERR Syntax error in HELLO option");
    }
  }

  if (name_at) { conn->client_name = cmd[name_at + 1]; }

  if (auth_at) {
    // Hand off to the async verifier and answer from auth_complete().
    conn->hello_pending_proto = want;
    std::vector<std::string> a = {"auth", cmd[auth_at + 1], cmd[auth_at + 2]};
    do_auth(a, out, conn);   // queues the job, writes nothing
    return;                  // no reply now - auth_complete() emits the map
  }

  // No credentials offered: HELLO still requires an already-authenticated
  // connection when a password is configured.
  if (!conn->user) {
    return resp_err(out, "NOAUTH HELLO must be called with the client already "
                         "authenticated, otherwise the HELLO <proto> AUTH "
                         "<user> <pass> option can be used to authenticate the "
                         "client and select the RESP protocol version.");
  }

  // switch first: the reply itself is encoded in the negotiated protocol
  conn->resp_proto = want;
  g_reply_proto = want;
  hello_emit_map(out, conn, want);
}
```

### 5c. `auth_complete` — answer the HELLO instead of `+OK`

`auth_complete` runs from `loop_post`, **outside `do_request` and therefore
outside `ReplyProtoScope`**, so it must set `g_reply_proto` itself. The plain
`+OK`/`-WRONGPASS` replies are shape-identical in both protocols, so only the
HELLO path needs it.

In the success branch, replace `resp_ok(&c->outgoing);` with:

```cpp
    if (c->hello_pending_proto) {
      // this AUTH came from HELLO: negotiate and answer with the handshake map
      c->resp_proto = c->hello_pending_proto;
      c->hello_pending_proto = 0;
      g_reply_proto = c->resp_proto;
      hello_emit_map(&c->outgoing, c, c->resp_proto);
      g_reply_proto = 2;   // nothing else on this callback path replies
    } else {
      resp_ok(&c->outgoing);
    }
```

And in the failure branch, before the existing `resp_err(...)`:

```cpp
    c->hello_pending_proto = 0;   // failed handshake: no protocol switch
```

> The `g_reply_proto = 2` reset matters. `auth_complete` is not inside the RAII
> scope, so whatever it leaves behind is what the *next* non-dispatch writer
> sees. Leaving it at 3 would make an unrelated connection's reply RESP3.

Also check the **early-return path** at the top of `auth_complete` (the one that
`delete job; return;`s when the conn is gone) — if it can fire while
`hello_pending_proto` is set, the field dies with the conn, which is fine.

### 5d. Two things to confirm when pasting

- **`do_auth` clears `cmd`'s password in place** (`secure_zero` on the last
  element). The snippet passes a *copy* `a`, so the original `cmd[auth_at + 2]`
  in the HELLO command is **not** wiped. Add the same `secure_zero` over
  `cmd[auth_at + 2]` after the `do_auth` call, or the plaintext password
  survives in the request buffer — the existing AUTH path is careful about
  exactly this.
- `resp_err` takes `const char *`; every error string above is a literal, so
  none of them need `.c_str()`.

## 6. `CLIENT`

```cpp
// CLIENT SETNAME|GETNAME|ID|SETINFO - the connection-time subset. LIST and
// KILL are step 5. SETINFO is accepted and discarded: nothing reads lib-name
// until CLIENT LIST exists, and redis-py swallows an error here anyway.
static void do_client(std::vector<std::string> &cmd, Buffer *out, Conn *conn) {
  std::string sub = cmd[1];
  for (char &ch : sub) { ch = (char)tolower((unsigned char)ch); }

  if (sub == "id" && cmd.size() == 2) {
    return resp_int(out, (int64_t)conn->id);
  }
  if (sub == "getname" && cmd.size() == 2) {
    if (conn->client_name.empty()) { return resp_nil(out); }
    return resp_str(out, conn->client_name.data(), conn->client_name.size());
  }
  if (sub == "setname" && cmd.size() == 3) {
    // a name with a space or newline would corrupt CLIENT LIST output later
    for (unsigned char ch : cmd[2]) {
      if (ch < '!' || ch > '~') {
        return resp_err(out, "ERR Client names cannot contain spaces, "
                             "newlines or special characters.");
      }
    }
    conn->client_name = cmd[2];
    return resp_ok(out);
  }
  if (sub == "setinfo" && cmd.size() == 4) {
    return resp_ok(out); // accepted and dropped, deliberately
  }
  return resp_err(out, "ERR Unknown CLIENT subcommand or wrong number of "
                       "arguments");
}
```

`CLIENT` needs the `Conn`, so it dispatches like the other conn-aware commands.
In `do_request`, next to the `if (canonical == "acl")` block:

```cpp
  if (canonical == "client") {
    return do_client(cmd, out, conn);
  }
```

### 6a. `CLIENT` needs THREE table rows, not one

This is the hazard the roadmap keeps flagging — one command list edited while a
parallel copy is forgotten. `acl_init_categories()` has an `orphan_check` that
dies at boot if a side table names a command `k_cmd_table` lacks, but **nothing
catches the reverse**: a command missing from `ks` silently inherits
`KeySpec::FIRST`.

**1. `k_cmd_table` (line 5166).** Conn-aware commands use a *stub*, never
`nullptr` — the interception above returns before `spec.fn` is reached, exactly
like `acl` and `subscribe`. Reuse the existing stub:

```cpp
    {"client", {do_pubsub_stub, 2, -1}},
```

**2. The `ks` table in `acl_init_categories()` (line ~5690).** Not optional:
without it `spec.keys` defaults to `KeySpec::FIRST`, so ACL would treat
`SETNAME` — the *subcommand* — as a key name, and any user with a restricted
key pattern would be denied `CLIENT SETNAME`.

```cpp
      {"client", KeySpec::NONE},
```

**3. The `extra` category table (line ~5729).** Optional but correct; without
it `client` gets only the READ/WRITE base.

```cpp
      {"client", CAT_FAST},
```

## 7. Call sites — the measured list

**`h_collect_reply` (line 2526)** — `mode == 0` is `HGETALL`:

```cpp
  case Lookup::MISSING:
    return (mode == 0) ? resp_map(out, 0) : resp_arr(out, 0);
  case Lookup::OK:
    break;
  }
  size_t n = hm_size(&entry_hash(ent));
  if (mode == 0) { resp_map(out, (uint32_t)n); }        // n PAIRS
  else           { resp_arr(out, (uint32_t)n); }
```

**`do_smembers` (line 3174)** — both the empty and populated paths:

```cpp
  case Lookup::MISSING:
    return resp_set(out, 0);
  ...
  resp_set(out, (uint32_t)hm_size(&entry_set(ent)));
```

**`do_sinter` (3768), `do_sunion` (3780), `do_sdiff` (3792)** — one line each:

```cpp
  resp_set(out, (uint32_t)result.size());   // was resp_arr
```

**`do_spop` (3574)** — only the *counted* form is a set; bare `SPOP` returns a
single member and stays a bulk string:

```cpp
  case Lookup::MISSING:
    return (cmd.size() >= 3) ? resp_set(out, 0) : resp_nil(out);
```

...and the same swap in the `hm_size(set) == 0` guard, plus the `resp_arr`
that heads the counted result further down.

**`do_config` (3919), the `get` branch** — `kv.size()` is already a pair count,
so the `* 2` goes away:

```cpp
    resp_map(out, (uint32_t)kv.size());   // was resp_arr(out, kv.size() * 2)
```

**`do_info` (1739)**, last line:

```cpp
  resp_verbatim(out, body.data(), body.size());   // was resp_str
```

**Zset scores need nothing** — `resp_dbl` is proto-aware now and its only four
callers (`do_zscore` 1122, the two `zquery` walks 1195/1234, `zpopmin` 1276)
are all scores. `INCRBYFLOAT` does not use it, which is what makes this safe.

## 8. Pub/sub — pushes, per subscriber

**`pubsub_publish` (line 4976).** Each subscriber has its own protocol, so the
header comes from `sub`, not the global:

```cpp
    for (Conn *sub : it->second) {
      resp_push_n(&sub->outgoing, 3, sub->resp_proto);   // was resp_arr(.., 3)
      resp_str(&sub->outgoing, "message", 7);
```

and the pattern loop:

```cpp
    for (Conn *sub : pe.second) {
      resp_push_n(&sub->outgoing, 4, sub->resp_proto);   // was resp_arr(.., 4)
      resp_str(&sub->outgoing, "pmessage", 8);
```

**`pubsub_confirm` (line 4821).** Subscribe/unsubscribe *confirmations* are
pushes in RESP3 too — measured, and easy to miss. It writes to the dispatching
connection, so the global is correct here:

```cpp
static void pubsub_confirm(Buffer *out, const char *kind,
                           const std::string *chan, int64_t count) {
  resp_push_n(out, 3, g_reply_proto);   // was resp_arr(out, 3)
```

## 9. Subscribe-mode gate — RESP3 lifts it

In RESP3 push and reply travel on separate channels, so a subscribed client may
run ordinary commands. Measured: real Redis answers `GET k` with `_` while
subscribed on RESP3, and errors on RESP2. In `do_request`, the existing gate
becomes:

```cpp
  // RESP3 separates push from reply, so a subscriber can still issue ordinary
  // commands; RESP2 cannot distinguish them and keeps the restriction.
  if (conn->resp_proto < 3 &&
      !(conn->sub_channels.empty() && conn->sub_patterns.empty()) &&
      !cmd_ok_in_subscribe(canonical)) {
```

## 10. Verify — against the attack, not the fix

Run each of these *before* applying, confirm it fails, then after. The suite is
the instrument, but these are the shapes to add to it.

```bash
# the whole point: a stock client with no options
~/.local/share/myred-testenv/bin/python - <<'PY'
import redis
r = redis.Redis(host="127.0.0.1", port=29500, decode_responses=True)  # RESP3 default
print("ping    :", r.ping())
r.delete("h"); r.hset("h", mapping={"uid":"42","csrf":"tok"})
print("hgetall :", r.hgetall("h"))        # MUST be a dict, not a list
print("get miss:", r.get("nope"))         # MUST be None
r.delete("s"); r.sadd("s","x","y")
print("smembers:", r.smembers("s"))       # MUST be a set
r.delete("z"); r.zadd("z", {"m":1.5})
print("zscore  :", r.zscore("z","m"))     # MUST be 1.5 (float)
print("config  :", r.config_get("maxmemory"))  # MUST be a dict
PY
```

The dict-vs-list check on `HGETALL` is the one that matters most: it is the
exact failure that makes a half-RESP3 implementation worse than none, and it
fails **silently** — no exception, just the wrong Python type.

```bash
# both protocols still work, on one server
~/.local/share/myred-testenv/bin/python - <<'PY'
import redis
for proto in (2, 3):
    r = redis.Redis(host="127.0.0.1", port=29500, protocol=proto,
                    decode_responses=True)
    r.delete("h"); r.hset("h", mapping={"a":"1"})
    print(proto, r.ping(), r.hgetall("h"), r.get("nope"))
PY

# mixed-protocol pub/sub: one RESP2 and one RESP3 subscriber, one PUBLISH.
# This is what resp_push_n's explicit proto argument exists for - a global
# would send both subscribers whatever the *publisher* negotiated.
```

Raw-socket shape checks, which the Python client would normalise away:

```bash
printf 'HELLO 3\r\nHGETALL h\r\n' | timeout 2 nc 127.0.0.1 29500 | xxd | head
# expect %2 ... not *4
```

## Deliberately not in this step

- `CLIENT LIST` / `CLIENT KILL` (step 5), `CLIENT INFO` (needs `CLIENT LIST`'s
  field assembly; also `=` verbatim in RESP3).
- RESP3 attributes (`|`) and client-side-caching invalidation pushes. Nothing
  in the target shape asks for them.
- `SELECT`, `COMMAND`, `QUIT`, `RESET` — Step 0 measured all four as blocking
  nothing. They are cheap, but they are not what gates a connection.
- `PING` in subscribe mode: RESP2 Redis answers `*2 pong ""`, MYRED answers
  `+PONG`. That is a pre-existing RESP2 divergence, and `+PONG` happens to be
  exactly right for RESP3. Changing it is a RESP2 behaviour change, so it is
  not part of this step.
