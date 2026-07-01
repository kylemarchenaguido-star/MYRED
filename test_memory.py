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


def used_memory(sock) -> int:
    info = cmd(sock, "info")
    for ln in info.splitlines():
        if ln.startswith("used_memory:"):
            return int(ln.split(":", 1)[1])
    # fall back to the older field name if used_memory isn't present
    for ln in info.splitlines():
        if ln.startswith("used_memory_bytes:"):
            return int(ln.split(":", 1)[1])
    raise RuntimeError("no used_memory field in INFO")


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

    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
