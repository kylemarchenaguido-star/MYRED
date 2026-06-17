#!/usr/bin/env python3
"""
Stress test for custom Redis server — RESP protocol edition.
redis-benchmark -h 127.0.0.1 -p 1234 -a kek1234 \
  -t set,get,lpush,rpush,lpop,rpop -n 200000 -c 50 -P 16
Tests all commands:
  Strings: get, set, del, asyncdel
  TTL:     pexpire, pttl
  Keys:    keys
  ZSet:    zadd, zrem, zscore, zquery, zrevquery, zrank
  Lists:   lpush, rpush, lpop, rpop, llen, lindex, lrange, lset, linsert, lrem, ltrim
  Admin:   auth, info, save, bgsave

Because the server now speaks RESP, this test also works against the
real redis-cli for cross-validation.

Usage:
    # Terminal 1 — start server
    ./server

    # Terminal 2 — run tests
    python3 stress_test.py

    # only correctness, no stress
    python3 stress_test.py --correctness-only

    # only stress
    python3 stress_test.py --stress-only

    # if your server requires a password
    python3 stress_test.py --password your_password_here

    # custom host/port
    python3 stress_test.py --host 127.0.0.1 --port 1234
"""

import socket
import time
import random
import string
import threading
import argparse
import sys
import os
import re
import atexit
from typing import Any, Optional

# ─── configuration ────────────────────────────────────────────────────────────
DEFAULT_HOST    = "127.0.0.1"
DEFAULT_PORT    = 1234
STRESS_THREADS  = 8
STRESS_OPS      = 500
ZSET_LARGE_SIZE = 1500       # entries to test asyncdel threshold (>1000)
TIMEOUT_SEC     = 5.0

# password is set from argparse in main(), shared by all connections
G_PASSWORD: Optional[str] = None

# ─── colors ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


# ─── output logging (tee console → file, ANSI stripped) ─────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

class _Tee:
    """Mirror stdout to a file with color codes stripped, for a shareable log."""
    def __init__(self, stream, fh):
        self._stream = stream   # original stdout (keeps colors in the terminal)
        self._fh     = fh       # log file (plain text)
    def write(self, data):
        self._stream.write(data)
        self._fh.write(_ANSI_RE.sub("", data))
    def flush(self):
        self._stream.flush()
        self._fh.flush()


def start_logging(path: str):
    """Redirect stdout through a tee into `path` (markdown, fenced code block)."""
    fh = open(path, "w", encoding="utf-8")
    fh.write(f"# MYRED stress test — {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n```\n")
    sys.stdout = _Tee(sys.stdout, fh)

    def _finish():
        sys.stdout.flush()
        fh.write("\n```\n")
        fh.close()
    atexit.register(_finish)


# ═══════════════════════════════════════════════════════════════════════════════
#  RESP PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════════

class RespError(RuntimeError):
    """Raised when the server returns a RESP error reply (-ERR ...)."""
    pass


def send_request(sock: socket.socket, *args: str) -> None:
    """
    Encode a command as a RESP array of bulk strings and send it.

      *<n_args>\r\n
      $<len>\r\n<arg>\r\n     (repeated for each arg)
    """
    out = bytearray()
    out += f"*{len(args)}\r\n".encode()
    for a in args:
        encoded = a.encode()
        out += f"${len(encoded)}\r\n".encode()
        out += encoded
        out += b"\r\n"
    sock.sendall(bytes(out))


def _recv_line(sock: socket.socket) -> bytes:
    """Read one CRLF-terminated line, returning the content without CRLF."""
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            raise ConnectionError("Server closed connection")
        if c == b"\r":
            nl = sock.recv(1)            # consume the \n
            if nl != b"\n":
                raise RespError("malformed line ending")
            return bytes(buf)
        buf += c


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Server closed connection")
        buf += chunk
    return bytes(buf)


def recv_response(sock: socket.socket) -> Any:
    """
    Read and parse one RESP reply.

      +<str>\r\n              simple string  -> str
      -<err>\r\n              error          -> raises RespError
      :<int>\r\n              integer        -> int
      $<len>\r\n<bytes>\r\n   bulk string    -> str  (or None if len == -1)
      *<count>\r\n<items...>  array          -> list (or None if count == -1)
    """
    line = _recv_line(sock)
    if not line:
        raise RespError("empty reply")

    prefix = line[0:1]
    body   = line[1:]

    if prefix == b"+":
        return body.decode(errors="replace")

    if prefix == b"-":
        raise RespError(body.decode(errors="replace"))

    if prefix == b":":
        return int(body)

    if prefix == b"$":
        length = int(body)
        if length < 0:
            return None
        data = _recv_exact(sock, length)
        _recv_exact(sock, 2)             # consume trailing \r\n
        return data.decode(errors="replace")

    if prefix == b"*":
        count = int(body)
        if count < 0:
            return None
        return [recv_response(sock) for _ in range(count)]

    raise RespError(f"unknown RESP prefix: {line!r}")


def cmd(sock: socket.socket, *args: str) -> Any:
    """Send a command and return the parsed reply."""
    send_request(sock, *args)
    return recv_response(sock)


def make_conn(host: str, port: int) -> socket.socket:
    """Open a connection, authenticating first if a password is configured."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_SEC)
    s.connect((host, port))
    if G_PASSWORD:
        # authenticate immediately on every new connection
        send_request(s, "auth", G_PASSWORD)
        recv_response(s)                 # expect +OK, raises on failure
    return s


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunner:
    def __init__(self, host: str, port: int):
        self.host   = host
        self.port   = port
        self.passed = 0
        self.failed = 0
        self.errors = []

    def check(self, name: str, got: Any, expected: Any) -> bool:
        if got == expected:
            print(f"  {GREEN}✓{RESET} {name}")
            self.passed += 1
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: {expected!r}")
        self.errors.append(name)
        self.failed += 1
        return False

    def check_type(self, name: str, got: Any, expected_type: type) -> bool:
        if isinstance(got, expected_type):
            print(f"  {GREEN}✓{RESET} {name} → {got!r}")
            self.passed += 1
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got type: {type(got).__name__} ({got!r})\n"
              f"    expected: {expected_type.__name__}")
        self.errors.append(name)
        self.failed += 1
        return False

    def check_none(self, name: str, got: Any) -> bool:
        return self.check(name, got, None)

    def check_approx(self, name: str, got: Any, expected: float,
                     tol: float = 1e-6) -> bool:
        # RESP returns doubles as strings via %g — accept str or float
        val = None
        if isinstance(got, (int, float)):
            val = float(got)
        elif isinstance(got, str):
            try:
                val = float(got)
            except ValueError:
                val = None
        if val is not None and abs(val - expected) < tol:
            print(f"  {GREEN}✓{RESET} {name} → {got}")
            self.passed += 1
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: ~{expected}")
        self.errors.append(name)
        self.failed += 1
        return False

    def check_true(self, name: str, condition: bool) -> bool:
        return self.check(name, condition, True)

    def expect_error(self, name: str, sock: "socket.socket", *args: str) -> bool:
        """Pass iff the command raises a RESP error (e.g. WRONGTYPE)."""
        try:
            got = cmd(sock, *args)
        except RespError:
            print(f"  {GREEN}✓{RESET} {name}")
            self.passed += 1
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: a RESP error")
        self.errors.append(name)
        self.failed += 1
        return False

    def section(self, title: str):
        pad = max(0, 50 - len(title))
        print(f"\n{BOLD}{BLUE}── {title} {'─' * pad}{RESET}")

    def summary(self) -> bool:
        total = self.passed + self.failed
        print(f"\n{BOLD}{'═' * 55}{RESET}")
        print(f"{BOLD}Results: {self.passed}/{total} passed{RESET}")
        if self.failed:
            print(f"{RED}Failed tests:{RESET}")
            for e in self.errors:
                print(f"  • {e}")
        else:
            print(f"{GREEN}All tests passed!{RESET}")
        print(f"{'═' * 55}")
        return self.failed == 0


# ─── helper: convert RESP double-string back to float ──────────────────────────

def as_float(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  CORRECTNESS TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_string_commands(r: TestRunner, sock: socket.socket):
    r.section("String Commands: GET / SET / DEL")

    # SET now returns +OK in RESP
    r.check("set k1 hello → OK", cmd(sock, "set", "k1", "hello"), "OK")
    r.check("get k1 → hello",    cmd(sock, "get", "k1"),          "hello")

    r.check("set k1 world → OK", cmd(sock, "set", "k1", "world"), "OK")
    r.check("get k1 → world",    cmd(sock, "get", "k1"),          "world")

    r.check_none("get missing → nil", cmd(sock, "get", "no_such_key"))

    r.check("del k1 → 1",        cmd(sock, "del", "k1"),          1)
    r.check_none("get after del → nil", cmd(sock, "get", "k1"))
    r.check("del missing → 0",   cmd(sock, "del", "no_such_key"), 0)

    # empty string value
    cmd(sock, "set", "empty", "")
    r.check("get empty → ''",    cmd(sock, "get", "empty"),       "")
    cmd(sock, "del", "empty")

    # long value
    long_val = "x" * 10000
    cmd(sock, "set", "big", long_val)
    r.check("get long value",    cmd(sock, "get", "big"),         long_val)
    cmd(sock, "del", "big")


def test_keys_command(r: TestRunner, sock: socket.socket):
    r.section("KEYS Command")

    for k in ("ka", "kb", "kc"):
        cmd(sock, "del", k)
    cmd(sock, "set", "ka", "1")
    cmd(sock, "set", "kb", "2")
    cmd(sock, "set", "kc", "3")

    result = cmd(sock, "keys")
    r.check_type("keys returns list", result, list)

    ks = set(result) if result else set()
    r.check("ka in keys", "ka" in ks, True)
    r.check("kb in keys", "kb" in ks, True)
    r.check("kc in keys", "kc" in ks, True)

    for k in ("ka", "kb", "kc"):
        cmd(sock, "del", k)


def test_ttl_commands(r: TestRunner, sock: socket.socket):
    r.section("TTL Commands: PEXPIRE / PTTL")

    cmd(sock, "set", "ttlkey", "value")
    r.check("pexpire ttlkey 5000 → 1",
            cmd(sock, "pexpire", "ttlkey", "5000"), 1)

    ttl = cmd(sock, "pttl", "ttlkey")
    r.check_type("pttl returns int", ttl, int)
    r.check_true("pttl > 0",     ttl > 0)
    r.check_true("pttl <= 5000", ttl <= 5000)
    print(f"  {YELLOW}ℹ{RESET}  remaining TTL: {ttl}ms")

    # no TTL → -1
    cmd(sock, "set", "nottlkey", "val")
    r.check("pttl no-ttl → -1", cmd(sock, "pttl", "nottlkey"), -1)
    cmd(sock, "del", "nottlkey")

    # missing key → -2
    cmd(sock, "del", "missingttl")
    r.check("pttl missing → -2", cmd(sock, "pttl", "missingttl"), -2)

    # actual expiration
    cmd(sock, "set", "shortlived", "bye")
    cmd(sock, "pexpire", "shortlived", "200")
    print(f"  {YELLOW}ℹ{RESET}  waiting 600ms for key to expire...")
    time.sleep(0.6)
    r.check_none("expired key → nil", cmd(sock, "get", "shortlived"))

    # PEXPIRE with a non-positive TTL deletes the key (Redis semantics)
    cmd(sock, "set", "removettl", "val")
    cmd(sock, "pexpire", "removettl", "5000")
    r.check("pexpire -1 deletes key → 1", cmd(sock, "pexpire", "removettl", "-1"), 1)
    r.check("pttl after delete → -2",      cmd(sock, "pttl", "removettl"), -2)
    r.check_none("get after delete → nil", cmd(sock, "get", "removettl"))
    cmd(sock, "del", "removettl")
    cmd(sock, "del", "ttlkey")


def test_zset_commands(r: TestRunner, sock: socket.socket):
    r.section("Sorted Set: ZADD / ZSCORE / ZREM / ZRANK")

    zset = "testzset"
    cmd(sock, "del", zset)

    r.check("zadd n1 1.0 → 1", cmd(sock, "zadd", zset, "1.0", "n1"), 1)
    r.check("zadd n2 2.0 → 1", cmd(sock, "zadd", zset, "2.0", "n2"), 1)
    r.check("zadd n3 3.0 → 1", cmd(sock, "zadd", zset, "3.0", "n3"), 1)
    r.check("zadd n4 0.5 → 1", cmd(sock, "zadd", zset, "0.5", "n4"), 1)

    # update existing → 0
    r.check("zadd n1 update → 0", cmd(sock, "zadd", zset, "1.5", "n1"), 0)
    r.check_approx("zscore n1 → 1.5", cmd(sock, "zscore", zset, "n1"), 1.5)

    r.check_none("zscore missing → nil", cmd(sock, "zscore", zset, "ghost"))

    # sorted order: n4(0.5), n1(1.5), n2(2.0), n3(3.0)
    r.check("zrank n4 → 0", cmd(sock, "zrank", zset, "n4"), 0)
    r.check("zrank n1 → 1", cmd(sock, "zrank", zset, "n1"), 1)
    r.check("zrank n2 → 2", cmd(sock, "zrank", zset, "n2"), 2)
    r.check("zrank n3 → 3", cmd(sock, "zrank", zset, "n3"), 3)
    r.check_none("zrank missing → nil", cmd(sock, "zrank", zset, "ghost"))

    r.check("zrem n1 → 1", cmd(sock, "zrem", zset, "n1"), 1)
    r.check_none("zscore after zrem → nil", cmd(sock, "zscore", zset, "n1"))
    r.check("zrem missing → 0", cmd(sock, "zrem", zset, "ghost"), 0)

    cmd(sock, "del", zset)


def test_zquery_commands(r: TestRunner, sock: socket.socket):
    r.section("Sorted Set: ZQUERY / ZREVQUERY")

    zset = "qzset"
    cmd(sock, "del", zset)

    for name, score in [("a", 1), ("b", 2), ("c", 3), ("d", 4), ("e", 5)]:
        cmd(sock, "zadd", zset, str(float(score)), name)

    # all entries — 5 pairs = 10 items
    result = cmd(sock, "zquery", zset, "0", "", "0", "10")
    r.check_type("zquery returns list", result, list)
    r.check("zquery all → 10 items", len(result), 10)
    if result and len(result) == 10:
        r.check("zquery order correct",
                result[0::2], ["a", "b", "c", "d", "e"])

    # offset=1 skips 'a' → b,c,d,e = 8 items
    res2 = cmd(sock, "zquery", zset, "0", "", "1", "10")
    r.check("zquery offset=1 → 8 items", len(res2), 8)

    # limit=4 → server returns 4 PAIRS = 8 items (name+score each)
    res3 = cmd(sock, "zquery", zset, "0", "", "0", "4")
    r.check("zquery limit=4 → 8 items (4 pairs)", len(res3), 8)

    # from 3.0 → c,d,e = 6 items
    res4 = cmd(sock, "zquery", zset, "3.0", "", "0", "10")
    r.check("zquery from 3.0 → 6 items", len(res4), 6)

    # no results
    res5 = cmd(sock, "zquery", zset, "999", "", "0", "10")
    r.check("zquery no results → 0", len(res5), 0)

    # reverse — descending: e,d,c,b,a
    res6 = cmd(sock, "zrevquery", zset, "999", "", "0", "10")
    r.check_type("zrevquery returns list", res6, list)
    r.check("zrevquery all → 10 items", len(res6), 10)
    if res6 and len(res6) == 10:
        r.check("zrevquery order correct",
                res6[0::2], ["e", "d", "c", "b", "a"])

    # from 3.5 lands on c descending → c,b,a = 6 items
    res7 = cmd(sock, "zrevquery", zset, "3.5", "", "0", "10")
    r.check("zrevquery from 3.5 → 6 items", len(res7), 6)

    cmd(sock, "del", zset)


def test_list_commands(r: TestRunner, sock: socket.socket):
    r.section("Lists: LPUSH/RPUSH/LPOP/RPOP/LLEN/LINDEX/LRANGE")

    key = "stress_list"
    cmd(sock, "del", key)

    # RPUSH appends, LPUSH prepends (each arg pushed in turn)
    r.check("rpush a b c → 3", cmd(sock, "rpush", key, "a", "b", "c"), 3)
    r.check("llen → 3", cmd(sock, "llen", key), 3)
    r.check("lrange 0 -1 → [a,b,c]",
            cmd(sock, "lrange", key, "0", "-1"), ["a", "b", "c"])

    # lpush x then y → [y, x, a, b, c]
    r.check("lpush x y → 5", cmd(sock, "lpush", key, "x", "y"), 5)
    r.check("lrange after lpush",
            cmd(sock, "lrange", key, "0", "-1"), ["y", "x", "a", "b", "c"])

    # LINDEX incl. negative + out of range
    r.check("lindex 0 → y",  cmd(sock, "lindex", key, "0"),  "y")
    r.check("lindex -1 → c", cmd(sock, "lindex", key, "-1"), "c")
    r.check("lindex 2 → a",  cmd(sock, "lindex", key, "2"),  "a")
    r.check_none("lindex 100 → nil", cmd(sock, "lindex", key, "100"))

    # LPOP / RPOP
    r.check("lpop → y", cmd(sock, "lpop", key), "y")
    r.check("rpop → c", cmd(sock, "rpop", key), "c")
    r.check("lrange after pops",
            cmd(sock, "lrange", key, "0", "-1"), ["x", "a", "b"])

    cmd(sock, "del", key)

    r.section("Lists: LSET / LINSERT")
    cmd(sock, "del", key)
    cmd(sock, "rpush", key, "a", "b", "c")               # [a, b, c]

    r.check("lset 1 B → OK", cmd(sock, "lset", key, "1", "B"), "OK")
    r.check("lindex 1 → B",  cmd(sock, "lindex", key, "1"), "B")
    r.expect_error("lset out of range → error", sock, "lset", key, "100", "z")

    # LINSERT: new length on success, -1 pivot-not-found, 0 missing key
    r.check("linsert before B → 4",
            cmd(sock, "linsert", key, "before", "B", "X"), 4)
    r.check("lrange after insert before",
            cmd(sock, "lrange", key, "0", "-1"), ["a", "X", "B", "c"])
    r.check("linsert after c → 5",
            cmd(sock, "linsert", key, "after", "c", "Y"), 5)
    r.check("lrange after insert after",
            cmd(sock, "lrange", key, "0", "-1"), ["a", "X", "B", "c", "Y"])
    r.check("linsert pivot missing → -1",
            cmd(sock, "linsert", key, "before", "ghost", "Z"), -1)
    r.check("linsert missing key → 0",
            cmd(sock, "linsert", "stress_nolist", "before", "a", "z"), 0)

    cmd(sock, "del", key)

    r.section("Lists: LREM / LTRIM")
    cmd(sock, "del", key)
    cmd(sock, "rpush", key, "a", "b", "a", "c", "a")     # [a, b, a, c, a]
    r.check("lrem 2 a (head) → 2", cmd(sock, "lrem", key, "2", "a"), 2)
    r.check("lrange after lrem head",
            cmd(sock, "lrange", key, "0", "-1"), ["b", "c", "a"])
    r.check("lrem -1 a (tail) → 1", cmd(sock, "lrem", key, "-1", "a"), 1)
    r.check("lrange after lrem tail",
            cmd(sock, "lrange", key, "0", "-1"), ["b", "c"])

    cmd(sock, "del", key)
    cmd(sock, "rpush", key, "a", "b", "c", "d", "e")     # [a..e]
    r.check("ltrim 1 3 → OK", cmd(sock, "ltrim", key, "1", "3"), "OK")
    r.check("lrange after ltrim",
            cmd(sock, "lrange", key, "0", "-1"), ["b", "c", "d"])
    r.check("ltrim 0 -1 keeps all", cmd(sock, "ltrim", key, "0", "-1"), "OK")
    r.check("lrange unchanged",
            cmd(sock, "lrange", key, "0", "-1"), ["b", "c", "d"])
    # trimming to an empty range deletes the key
    cmd(sock, "ltrim", key, "5", "10")
    r.check("llen after empty ltrim → 0", cmd(sock, "llen", key), 0)

    cmd(sock, "del", key)

    r.section("Lists: wrong-type + missing-key behavior")
    cmd(sock, "del", "stress_liststr")
    cmd(sock, "set", "stress_liststr", "hello")
    r.expect_error("lpush on string → WRONGTYPE",  sock, "lpush",  "stress_liststr", "x")
    r.expect_error("lrange on string → WRONGTYPE", sock, "lrange", "stress_liststr", "0", "-1")
    cmd(sock, "del", "stress_liststr")

    cmd(sock, "del", "stress_nolist")
    r.check("llen missing → 0",      cmd(sock, "llen",   "stress_nolist"), 0)
    r.check("lrange missing → []",   cmd(sock, "lrange", "stress_nolist", "0", "-1"), [])
    r.check_none("lpop missing → nil", cmd(sock, "lpop", "stress_nolist"))
    r.check("lrem missing → 0",      cmd(sock, "lrem",   "stress_nolist", "0", "x"), 0)


def test_asyncdel_command(r: TestRunner, sock: socket.socket):
    r.section("ASYNCDEL Command")

    cmd(sock, "del", "asynctest")
    r.check("asyncdel missing → 0", cmd(sock, "asyncdel", "asynctest"), 0)

    cmd(sock, "set", "asynctest", "hello")
    r.check("asyncdel string → 1", cmd(sock, "asyncdel", "asynctest"), 1)
    r.check_none("string gone", cmd(sock, "get", "asynctest"))

    # small zset — synchronous path
    small = "small_async_zset"
    cmd(sock, "del", small)
    for i in range(10):
        cmd(sock, "zadd", small, str(float(i)), f"m{i}")
    r.check("asyncdel small zset → 1", cmd(sock, "asyncdel", small), 1)
    r.check_none("small zset gone", cmd(sock, "zscore", small, "m0"))

    # large zset — thread pool path (>1000 entries)
    large = "large_async_zset"
    cmd(sock, "del", large)
    print(f"  {YELLOW}ℹ{RESET}  inserting {ZSET_LARGE_SIZE} entries...")
    for i in range(ZSET_LARGE_SIZE):
        cmd(sock, "zadd", large, str(float(i)), f"member{i}")

    r.check_approx("large zset created",
                   cmd(sock, "zscore", large, "member0"), 0.0)

    print(f"  {YELLOW}ℹ{RESET}  sending asyncdel (thread pool path)...")
    t0      = time.time()
    result  = cmd(sock, "asyncdel", large)
    elapsed = time.time() - t0
    r.check("asyncdel large → 1", result, 1)
    r.check_true("asyncdel fast (<100ms)", elapsed < 0.1)
    r.check_none("large zset immediately gone",
                 cmd(sock, "zscore", large, "member0"))
    print(f"  {YELLOW}ℹ{RESET}  returned in {elapsed*1000:.1f}ms")
    time.sleep(0.5)


def test_edge_cases(r: TestRunner, sock: socket.socket):
    r.section("Edge Cases")

    # negative score
    cmd(sock, "del", "negzset")
    cmd(sock, "zadd", "negzset", "-5.0", "neg")
    r.check_approx("zscore negative", cmd(sock, "zscore", "negzset", "neg"), -5.0)
    cmd(sock, "del", "negzset")

    # zero score
    cmd(sock, "del", "zerozset")
    cmd(sock, "zadd", "zerozset", "0.0", "zero")
    r.check_approx("zscore zero", cmd(sock, "zscore", "zerozset", "zero"), 0.0)
    cmd(sock, "del", "zerozset")

    # same score → sorted by name
    cmd(sock, "del", "samescores")
    for name in ["b", "a", "c"]:
        cmd(sock, "zadd", "samescores", "1.0", name)
    res = cmd(sock, "zquery", "samescores", "0", "", "0", "10")
    if res and len(res) == 6:
        r.check("same score sorted by name", res[0::2], ["a", "b", "c"])
    cmd(sock, "del", "samescores")

    # special characters in value
    special = "hello\tworld with spaces"
    cmd(sock, "set", "special", special)
    r.check("special chars in value", cmd(sock, "get", "special"), special)
    cmd(sock, "del", "special")

    # wrong type — get on a zset
    cmd(sock, "del", "typezset")
    cmd(sock, "zadd", "typezset", "1.0", "x")
    try:
        cmd(sock, "get", "typezset")
        r.check("get on zset → error", False, True)
    except RespError:
        r.check("get on zset → WRONGTYPE error", True, True)
    cmd(sock, "del", "typezset")

    # 100 rapid set/get/del
    for i in range(100):
        cmd(sock, "set", f"rapid{i}", str(i))
    ok = all(cmd(sock, "get", f"rapid{i}") == str(i) for i in range(100))
    r.check("100 rapid get correct", ok, True)
    for i in range(100):
        cmd(sock, "del", f"rapid{i}")
    print(f"  {YELLOW}ℹ{RESET}  100 rapid set/get/del complete")


def test_info_command(r: TestRunner, sock: socket.socket):
    r.section("INFO Command")

    result = cmd(sock, "info")
    r.check_type("info returns string", result, str)
    if not isinstance(result, str):
        return

    sections = ["# Server", "# Clients", "# Memory",
                "# Stats", "# Keyspace", "# Persistence"]
    for s in sections:
        r.check(f"has {s} section", s in result, True)

    fields = ["version:", "uptime_seconds:", "connected_clients:",
              "total_commands:", "keys_total:", "keys_with_ttl:"]
    for f in fields:
        r.check(f"has {f.rstrip(':')} field", f in result, True)

    print(f"\n  {YELLOW}INFO output:{RESET}")
    for line in result.split("\r\n"):
        if line:
            print(f"    {line}")


def test_save_command(r: TestRunner, sock: socket.socket):
    r.section("SAVE / RDB Persistence")

    cmd(sock, "set", "rdb_key1", "value1")
    cmd(sock, "set", "rdb_key2", "value2")
    cmd(sock, "pexpire", "rdb_key2", "60000")

    r.check("save → OK", cmd(sock, "save"), "OK")

    # give the thread pool time to flush the file
    time.sleep(0.5)

    r.check("dump.rdb exists", os.path.exists("dump.rdb"), True)
    if os.path.exists("dump.rdb"):
        size = os.path.getsize("dump.rdb")
        r.check_true("dump.rdb not empty", size > 18)
        print(f"  {YELLOW}ℹ{RESET}  dump.rdb size: {size} bytes")

        with open("dump.rdb", "rb") as f:
            header = f.read(5)
        r.check("magic number correct", header, b"MYRED")

    cmd(sock, "del", "rdb_key1")
    cmd(sock, "del", "rdb_key2")


def test_bgsave_command(r: TestRunner, sock: socket.socket):
    r.section("BGSAVE (fork-based background save)")

    # populate some data
    for i in range(20):
        cmd(sock, "set", f"bg_key{i}", f"value{i}")

    # trigger background save — should return immediately
    t0     = time.time()
    result = cmd(sock, "bgsave")
    elapsed = time.time() - t0

    # bgsave returns a status string, not OK
    r.check_type("bgsave returns string", result, str)
    r.check_true("bgsave returns fast (<50ms)", elapsed < 0.05)
    print(f"  {YELLOW}ℹ{RESET}  bgsave returned in {elapsed*1000:.1f}ms: {result!r}")

    # server must stay responsive WHILE the fork child is saving
    # fire a burst of commands immediately after bgsave
    responsive = True
    burst_start = time.time()
    for i in range(50):
        try:
            cmd(sock, "set", f"during_save{i}", "x")
            cmd(sock, "get", f"during_save{i}")
        except Exception:
            responsive = False
            break
    burst_elapsed = time.time() - burst_start

    r.check_true("server responsive during save", responsive)
    print(f"  {YELLOW}ℹ{RESET}  100 ops during save took "
          f"{burst_elapsed*1000:.1f}ms")

    # the burst should NOT have been blocked — if it took seconds,
    # the save was blocking the event loop
    r.check_true("save did not block event loop (burst <500ms)",
                 burst_elapsed < 0.5)

    # give the child time to finish writing
    time.sleep(0.5)

    # verify file exists and is valid
    r.check("dump.rdb exists after bgsave",
            os.path.exists("dump.rdb"), True)
    if os.path.exists("dump.rdb"):
        with open("dump.rdb", "rb") as f:
            magic = f.read(5)
        r.check("bgsave file has magic", magic, b"MYRED")

    # second bgsave while first might still run — should be rejected
    # or accepted depending on timing; just verify it doesn't crash
    try:
        cmd(sock, "bgsave")
        r.check_true("second bgsave handled gracefully", True)
    except RespError:
        # "background save already in progress" is also valid
        r.check_true("second bgsave handled gracefully", True)

    # cleanup
    for i in range(20):
        cmd(sock, "del", f"bg_key{i}")
    for i in range(50):
        cmd(sock, "del", f"during_save{i}")


def test_auth_command(r: TestRunner, host: str, port: int):
    """Only meaningful when a password is set. Skipped otherwise."""
    r.section("Authentication")

    if not G_PASSWORD:
        print(f"  {YELLOW}ℹ{RESET}  no password configured — skipping auth tests")
        print(f"  {YELLOW}ℹ{RESET}  run with --password to test auth")
        return

    # wrong password should fail
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_SEC)
    s.connect((host, port))
    try:
        send_request(s, "auth", "definitely_wrong_password")
        recv_response(s)
        r.check("wrong password → error", False, True)
    except RespError:
        r.check("wrong password → error", True, True)
    finally:
        s.close()

    # unauthenticated command should fail
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_SEC)
    s.connect((host, port))
    try:
        send_request(s, "get", "anything")
        recv_response(s)
        r.check("unauthenticated → error", False, True)
    except RespError:
        r.check("unauthenticated → NOAUTH error", True, True)
    finally:
        s.close()

    # correct password then a real command
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_SEC)
    s.connect((host, port))
    try:
        send_request(s, "auth", G_PASSWORD)
        r.check("correct password → OK", recv_response(s), "OK")
        cmd(s, "set", "authtest", "value")
        r.check("authenticated set works", cmd(s, "get", "authtest"), "value")
        cmd(s, "del", "authtest")
    except Exception as e:
        r.check(f"auth flow failed: {e}", False, True)
    finally:
        s.close()


def test_persistence_roundtrip(r: TestRunner, host: str, port: int):
    """
    Verify data survives a save by saving, then re-reading.
    Does not restart the server (the test harness can't), but confirms
    the save+in-memory state is consistent.
    """
    r.section("Persistence Round-trip (in-memory)")

    sock = make_conn(host, port)
    try:
        # set known data
        cmd(sock, "set", "persist_str", "hello_persist")
        cmd(sock, "zadd", "persist_zset", "10.0", "alice")
        cmd(sock, "zadd", "persist_zset", "20.0", "bob")
        cmd(sock, "pexpire", "persist_str", "120000")

        # save
        r.check("save → OK", cmd(sock, "save"), "OK")
        time.sleep(0.5)

        # verify still readable in memory
        r.check("string still readable",
                cmd(sock, "get", "persist_str"), "hello_persist")
        r.check_approx("zset alice still readable",
                       cmd(sock, "zscore", "persist_zset", "alice"), 10.0)
        r.check_approx("zset bob still readable",
                       cmd(sock, "zscore", "persist_zset", "bob"), 20.0)
        r.check("zrank alice → 0",
                cmd(sock, "zrank", "persist_zset", "alice"), 0)

        ttl = cmd(sock, "pttl", "persist_str")
        r.check_true("ttl preserved after save", isinstance(ttl, int) and ttl > 0)

        # cleanup
        cmd(sock, "del", "persist_str")
        cmd(sock, "del", "persist_zset")
    finally:
        sock.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  STRESS TEST
# ═══════════════════════════════════════════════════════════════════════════════

class StressStats:
    def __init__(self):
        self.ops       = 0
        self.errors    = 0
        self.latencies = []
        self.lock      = threading.Lock()

    def record(self, ms: float):
        with self.lock:
            self.ops += 1
            self.latencies.append(ms)

    def record_error(self):
        with self.lock:
            self.errors += 1

    def report(self):
        with self.lock:
            if not self.latencies:
                print("  No operations recorded.")
                return
            srt = sorted(self.latencies)
            avg = sum(srt) / len(srt)
            print(f"  Total ops:   {self.ops}")
            print(f"  Errors:      {self.errors}")
            print(f"  Latency avg: {avg:.2f}ms")
            print(f"  Latency min: {srt[0]:.2f}ms")
            print(f"  Latency max: {srt[-1]:.2f}ms")
            print(f"  Latency p95: {srt[int(len(srt)*0.95)]:.2f}ms")
            print(f"  Latency p99: {srt[int(len(srt)*0.99)]:.2f}ms")
            if self.errors == 0:
                print(f"  {GREEN}No errors!{RESET}")
            else:
                print(f"  {RED}{self.errors} errors!{RESET}")


def random_key(prefix: str = "stress") -> str:
    return f"{prefix}_{random.randint(0, 200)}"


def random_string(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def stress_worker(host: str, port: int, ops: int,
                  stats: StressStats, wid: int):
    try:
        sock = make_conn(host, port)
    except Exception as e:
        print(f"  {RED}Worker {wid} connect failed: {e}{RESET}")
        stats.record_error()
        return

    zset = f"stress_zset_{wid}"
    lst  = f"stress_list_{wid}"
    try:
        cmd(sock, "del", zset)
        cmd(sock, "del", lst)
        for i in range(10):
            cmd(sock, "zadd", zset, str(float(i)), f"m{i}")
        cmd(sock, "rpush", lst, "a", "b", "c", "d", "e")
    except Exception:
        pass

    for _ in range(ops):
        op = random.randint(0, 14)
        try:
            t0 = time.perf_counter()
            if op == 0:
                cmd(sock, "set", random_key(), random_string())
            elif op == 1:
                cmd(sock, "get", random_key())
            elif op == 2:
                cmd(sock, "del", random_key())
            elif op == 3:
                cmd(sock, "zadd", zset,
                    str(random.uniform(-100, 100)), random_string(6))
            elif op == 4:
                cmd(sock, "zscore", zset, f"m{random.randint(0, 9)}")
            elif op == 5:
                cmd(sock, "zrank", zset, f"m{random.randint(0, 9)}")
            elif op == 6:
                cmd(sock, "zquery", zset,
                    str(random.uniform(-50, 50)), "", "0", "5")
            elif op == 7:
                cmd(sock, "zrevquery", zset,
                    str(random.uniform(50, 150)), "", "0", "5")
            elif op == 8:
                k = random_key()
                cmd(sock, "set", k, "val")
                cmd(sock, "pexpire", k, "10000")
                cmd(sock, "pttl", k)
            elif op == 9:
                cmd(sock, "keys")
            elif op == 10:
                cmd(sock, "info")
            elif op == 11:
                cmd(sock, "rpush", lst, random_string(6))
            elif op == 12:
                cmd(sock, "lpush", lst, random_string(6))
            elif op == 13:
                cmd(sock, "lrange", lst, "0", "5")
            elif op == 14:
                # keep the list from growing unbounded: pop both ends, check len
                cmd(sock, "lpop", lst)
                cmd(sock, "rpop", lst)
                cmd(sock, "llen", lst)

            stats.record((time.perf_counter() - t0) * 1000)
        except Exception:
            stats.record_error()

    try:
        cmd(sock, "del", zset)
        cmd(sock, "del", lst)
        sock.close()
    except Exception:
        pass


def run_stress_test(host: str, port: int) -> bool:
    print(f"\n{BOLD}{BLUE}── Stress Test {'─' * 40}{RESET}")
    print(f"  Threads:    {STRESS_THREADS}")
    print(f"  Ops/thread: {STRESS_OPS}")
    print(f"  Total ops:  {STRESS_THREADS * STRESS_OPS}")

    stats   = StressStats()
    threads = [
        threading.Thread(target=stress_worker,
                         args=(host, port, STRESS_OPS, stats, i))
        for i in range(STRESS_THREADS)
    ]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed    = time.time() - t0
    throughput = stats.ops / elapsed if elapsed > 0 else 0

    print(f"\n  Elapsed:    {elapsed:.2f}s")
    print(f"  Throughput: {throughput:.0f} ops/sec")
    stats.report()
    return stats.errors == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  CONCURRENT SAFETY
# ═══════════════════════════════════════════════════════════════════════════════

def test_concurrent_writes(host: str, port: int) -> bool:
    print(f"\n{BOLD}{BLUE}── Concurrent Write Safety {'─' * 29}{RESET}")
    errors = []
    lock   = threading.Lock()

    def writer(tid: int):
        try:
            sock = make_conn(host, port)
            for i in range(50):
                cmd(sock, "set", "concurrent_key", f"t{tid}_v{i}")
                cmd(sock, "get", "concurrent_key")
            sock.close()
        except Exception as e:
            with lock:
                errors.append(str(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    try:
        s = make_conn(host, port)
        cmd(s, "del", "concurrent_key")
        s.close()
    except Exception:
        pass

    if errors:
        print(f"  {RED}✗ {len(errors)} errors during concurrent writes{RESET}")
        for e in errors[:3]:
            print(f"    {e}")
        return False
    print(f"  {GREEN}✓ 10 threads × 50 ops, no errors{RESET}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_stress_keys(host: str, port: int):
    try:
        sock = make_conn(host, port)
        keys = cmd(sock, "keys")
        if keys:
            junk = [k for k in keys
                    if k.startswith("stress_") or k.startswith("rapid")]
            for k in junk:
                cmd(sock, "del", k)
            if junk:
                print(f"  {YELLOW}ℹ{RESET}  cleaned {len(junk)} leftover keys")
        sock.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global G_PASSWORD

    ap = argparse.ArgumentParser(description="Redis server RESP stress test")
    ap.add_argument("--host",             default=DEFAULT_HOST)
    ap.add_argument("--port",             default=DEFAULT_PORT, type=int)
    ap.add_argument("--password",         default=None,
                    help="server password (if auth is enabled)")
    ap.add_argument("--correctness-only", action="store_true")
    ap.add_argument("--stress-only",      action="store_true")
    ap.add_argument("--log",              default="stress_results.md",
                    help="write a copy of all output here (ANSI stripped); "
                         "pass --log '' to disable")
    args = ap.parse_args()

    host, port  = args.host, args.port
    G_PASSWORD  = args.password

    # mirror everything to a shareable markdown log
    if args.log:
        start_logging(args.log)
        print(f"(logging output to {args.log})")

    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  Redis Server RESP Stress Test{RESET}")
    print(f"  Connecting to {host}:{port}")
    if G_PASSWORD:
        print(f"  Using authentication")
    print(f"{'═' * 55}")

    # reachability check
    try:
        s = make_conn(host, port)
        s.close()
        print(f"{GREEN}✓ Server is reachable{RESET}")
    except RespError as e:
        print(f"{RED}✗ Auth failed: {e}{RESET}")
        print("  Check your --password value")
        sys.exit(1)
    except Exception as e:
        print(f"{RED}✗ Cannot connect: {e}{RESET}")
        print("  Start the server first:  ./server")
        sys.exit(1)

    all_ok = True

    # ── correctness ────────────────────────────────────────────────────────────
    if not args.stress_only:
        r    = TestRunner(host, port)
        sock = make_conn(host, port)
        try:
            test_string_commands(r,       sock)
            test_keys_command(r,          sock)
            test_ttl_commands(r,          sock)
            test_zset_commands(r,         sock)
            test_zquery_commands(r,       sock)
            test_list_commands(r,         sock)
            test_asyncdel_command(r,      sock)
            test_edge_cases(r,            sock)
            test_info_command(r,          sock)
            test_save_command(r,          sock)
            test_bgsave_command(r,        sock)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False
        finally:
            sock.close()

        # tests that manage their own connections
        try:
            test_auth_command(r,          host, port)
            test_persistence_roundtrip(r, host, port)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False

        all_ok = r.summary() and all_ok

    # ── concurrent safety ──────────────────────────────────────────────────────
    if not args.stress_only and not args.correctness_only:
        all_ok = test_concurrent_writes(host, port) and all_ok

    # ── stress ─────────────────────────────────────────────────────────────────
    if not args.correctness_only:
        all_ok = run_stress_test(host, port) and all_ok
        cleanup_stress_keys(host, port)

    # ── final verdict ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    if all_ok:
        print(f"{BOLD}{GREEN}  ALL TESTS PASSED{RESET}")
    else:
        print(f"{BOLD}{RED}  SOME TESTS FAILED{RESET}")
    print(f"{'═' * 55}\n")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()