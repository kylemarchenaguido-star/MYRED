#!/usr/bin/env python3
"""
Memory-accounting test for MYRED (v7 Step 1).

Verifies the used_memory counter maintained by mem_reaccount()/entry_del:
  - grows when data is added
  - is STABLE when a key is overwritten (no leak on in-place growth/shrink)
  - returns to the empty-DB baseline (0) once every key is gone

The strongest invariant is "drain everything -> used_memory == 0", because
entry_del subtracts the exact bytes last charged to the entry. A non-zero
residual means a discharge path is missing or something is double-counted;
a residual that only appears for one type localizes the broken handler.

Usage:
    ./build/server &                       # in another terminal
    python3 test_memory.py --password kek1234
"""

import socket, argparse, sys
from typing import Any, Optional

HOST, PORT = "127.0.0.1", 1234
G_PASSWORD: Optional[str] = None
GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"


# ─── minimal RESP client (mirrors stress_test.py) ──────────────────────────────
def send(sock, *args):
    out = bytearray(f"*{len(args)}\r\n".encode())
    for a in args:
        b = str(a).encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    sock.sendall(bytes(out))


def _line(sock):
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            raise ConnectionError("server closed")
        if c == b"\r":
            sock.recv(1)
            return bytes(buf)
        buf += c


def _exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed")
        buf += chunk
    return bytes(buf)


def recv(sock) -> Any:
    line = _line(sock)
    p, body = line[0:1], line[1:]
    if p == b"+": return body.decode(errors="replace")
    if p == b"-": raise RuntimeError(body.decode(errors="replace"))
    if p == b":": return int(body)
    if p == b"$":
        n = int(body)
        if n < 0: return None
        d = _exact(sock, n); _exact(sock, 2)
        return d.decode(errors="replace")
    if p == b"*":
        n = int(body)
        return None if n < 0 else [recv(sock) for _ in range(n)]
    raise RuntimeError(f"bad prefix {line!r}")


def cmd(sock, *args):
    send(sock, *args)
    return recv(sock)


def connect():
    s = socket.socket(); s.settimeout(5.0); s.connect((HOST, PORT))
    if G_PASSWORD:
        send(s, "auth", G_PASSWORD); recv(s)
    return s


def info_field(sock, name: str) -> Optional[str]:
    for ln in cmd(sock, "info").splitlines():
        if ln.startswith(name + ":"):
            return ln.split(":", 1)[1]
    return None


def used_memory(sock) -> int:
    v = info_field(sock, "used_memory") or info_field(sock, "used_memory_bytes")
    if v is None:
        raise RuntimeError("no used_memory field in INFO")
    return int(v)


def evicted_keys(sock) -> int:
    v = info_field(sock, "evicted_keys")
    return int(v) if v is not None else 0


def set_result(sock, key, val) -> str:
    """SET a key; return 'OK' or 'OOM'. Re-raise any other error."""
    try:
        cmd(sock, "set", key, val)
        return "OK"
    except RuntimeError as e:
        if "OOM" in str(e):
            return "OOM"
        raise


# ─── test harness ──────────────────────────────────────────────────────────────
PASS = FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  {GREEN}✓{RESET} {name}")
    else:
        FAIL += 1; print(f"  {RED}✗{RESET} {name}   {detail}")


def drain_to_zero(s, name, build, drain):
    """build(s) then drain(s); assert used_memory returns to the pre-build value."""
    base = used_memory(s)
    build(s)
    grew = used_memory(s)
    drain(s)
    back = used_memory(s)
    check(f"{name}: grows on build", grew > base, f"base={base} grew={grew}")
    check(f"{name}: back to baseline after drain", back == base,
          f"base={base} after={back} (leak={back - base})")


def main():
    global G_PASSWORD
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--password")
    a = ap.parse_args()
    globals()["HOST"], globals()["PORT"] = a.host, a.port
    G_PASSWORD = a.password

    s = connect()
    cmd(s, "flushall")
    check("empty DB baseline is 0", used_memory(s) == 0, f"got {used_memory(s)}")

    # per-type create -> delete round-trips (localizes a broken discharge to one type)
    drain_to_zero(s, "string (set/del)",
                  lambda s: cmd(s, "set", "k", "x" * 5000),
                  lambda s: cmd(s, "del", "k"))
    drain_to_zero(s, "list (rpush/lpop-all)",
                  lambda s: [cmd(s, "rpush", "L", f"item-{i}") for i in range(500)],
                  lambda s: [cmd(s, "lpop", "L") for _ in range(500)])
    drain_to_zero(s, "hash (hset/hdel-all)",
                  lambda s: cmd(s, "hset", "H", *sum(([f"f{i}", f"v{i}"] for i in range(300)), [])),
                  lambda s: [cmd(s, "hdel", "H", f"f{i}") for i in range(300)])
    drain_to_zero(s, "set (sadd/srem-all)",
                  lambda s: cmd(s, "sadd", "S", *[f"m{i}" for i in range(300)]),
                  lambda s: [cmd(s, "srem", "S", f"m{i}") for i in range(300)])
    drain_to_zero(s, "zset (zadd/zpopmin-all)",
                  lambda s: cmd(s, "zadd", "Z", *sum(([str(i), f"m{i}"] for i in range(300)), [])),
                  lambda s: cmd(s, "zpopmin", "Z", "300"))

    # may-delete branches that reach empty via a value command (not DEL)
    drain_to_zero(s, "list emptied via lrem",
                  lambda s: [cmd(s, "rpush", "LR", "dup") for _ in range(200)],
                  lambda s: cmd(s, "lrem", "LR", "0", "dup"))
    drain_to_zero(s, "list emptied via ltrim",
                  lambda s: [cmd(s, "rpush", "LT", f"i{i}") for i in range(200)],
                  lambda s: cmd(s, "ltrim", "LT", "5", "1"))   # start>stop -> clears key
    drain_to_zero(s, "set emptied via spop",
                  lambda s: cmd(s, "sadd", "SP", *[f"m{i}" for i in range(200)]),
                  lambda s: cmd(s, "spop", "SP", "200"))

    # overwrite stability: repeatedly replacing a value must NOT leak
    cmd(s, "flushall")
    cmd(s, "set", "ov", "start")
    before = used_memory(s)
    for i in range(1000):
        cmd(s, "set", "ov", ("v%d" % i) * (i % 50 + 1))   # varying sizes
    cmd(s, "set", "ov", "start")                          # back to original value
    after = used_memory(s)
    check("string overwrite is stable (no leak)", abs(after - before) <= 64,
          f"before={before} after={after}")

    # append growth then delete
    drain_to_zero(s, "append growth",
                  lambda s: [cmd(s, "append", "AP", "z" * 100) for _ in range(200)],
                  lambda s: cmd(s, "del", "AP"))

    # store ops + rename (fresh-dest and re-key accounting)
    drain_to_zero(s, "sinterstore dest",
                  lambda s: (cmd(s, "sadd", "A", *[str(i) for i in range(200)]),
                             cmd(s, "sadd", "B", *[str(i) for i in range(100, 300)]),
                             cmd(s, "sinterstore", "DST", "A", "B")),
                  lambda s: cmd(s, "del", "A", "B", "DST"))
    drain_to_zero(s, "rename re-key",
                  lambda s: (cmd(s, "set", "old", "y" * 1000), cmd(s, "rename", "old", "new")),
                  lambda s: cmd(s, "del", "new"))

    # grand finale: a big mixed load, then FLUSHALL must return to exactly 0
    cmd(s, "flushall")
    for i in range(200):
        cmd(s, "set", f"str:{i}", "v" * (i + 1))
        cmd(s, "rpush", f"list:{i}", *[f"e{j}" for j in range(i % 20 + 1)])
        cmd(s, "hset", f"hash:{i}", "a", "1", "b", "2", "c", str(i))
        cmd(s, "sadd", f"set:{i}", *[f"m{j}" for j in range(i % 15 + 1)])
        cmd(s, "zadd", f"zset:{i}", "1", "x", "2", "y", str(i), "z")
    loaded = used_memory(s)
    cmd(s, "flushall")
    residual = used_memory(s)
    check("mixed load grows used_memory", loaded > 0, f"loaded={loaded}")
    check("FLUSHALL returns used_memory to 0", residual == 0,
          f"residual={residual} (leak/double-count)")

    # ── maxmemory: OOM (noeviction) vs eviction (allkeys-lru) ─────────────────
    # Both floods push ~6 MB of data into a 1 MB cap, so anything that stays
    # bounded near the cap proves the limit is enforced; anything that grows to
    # multiples of the cap means the -OOM return value is being ignored.
    CAP = 1024 * 1024                       # 1 MB
    VAL = "y" * 1000                        # ~1 KB values
    BOUND = CAP + CAP // 2                   # tolerate ~1 write of overshoot; reject unbounded growth
    cmd(s, "config", "set", "maxmemory", str(CAP))

    # noeviction: writes succeed until full, then every write returns OOM
    cmd(s, "flushall")
    cmd(s, "config", "set", "maxmemory-policy", "noeviction")
    ev_before = evicted_keys(s)
    oks = ooms = 0
    for i in range(6000):
        r = set_result(s, f"ne:{i}", VAL)
        oks += (r == "OK"); ooms += (r == "OOM")
        if ooms >= 50:                       # seen enough rejections
            break
    check("noeviction: writes succeed then start OOMing", oks > 0 and ooms > 0,
          f"ok={oks} oom={ooms}")
    check("noeviction: used_memory stays bounded near cap", used_memory(s) <= BOUND,
          f"used={used_memory(s)} bound={BOUND}")
    check("noeviction: nothing was evicted", evicted_keys(s) == ev_before,
          f"evicted_delta={evicted_keys(s) - ev_before}")
    # over the cap now -> a fresh write must be refused
    check("noeviction: write over cap returns OOM", set_result(s, "over", VAL) == "OOM")

    # allkeys-lru: writes never fail, memory holds near cap, evictions climb
    cmd(s, "flushall")
    cmd(s, "config", "set", "maxmemory-policy", "allkeys-lru")
    ev0 = evicted_keys(s)
    oks = ooms = 0
    for i in range(6000):
        r = set_result(s, f"lru:{i}", VAL)
        oks += (r == "OK"); ooms += (r == "OOM")
    check("allkeys-lru: no write ever OOMs", ooms == 0, f"oom={ooms}")
    check("allkeys-lru: used_memory held near cap", used_memory(s) <= BOUND,
          f"used={used_memory(s)} bound={BOUND}")
    check("allkeys-lru: evicted_keys climbed", evicted_keys(s) - ev0 > 0,
          f"evicted={evicted_keys(s) - ev0}")

    # restore the server to unlimited so we leave it as we found it
    cmd(s, "config", "set", "maxmemory", "0")
    cmd(s, "config", "set", "maxmemory-policy", "noeviction")
    cmd(s, "flushall")

    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
