#!/usr/bin/env python3
"""
Stress test for custom Redis server.
Tests all commands: get, set, del, asyncdel, pexpire, pttl,
keys, zadd, zrem, zscore, zquery, zrevquery, zrank.

Usage:
    # Terminal 1 — start server
    ./server

    # Terminal 2 — run stress test
    python3 stress_test.py

    # Run only correctness tests (no stress)
    python3 stress_test.py --correctness-only

    # Run only stress tests
    python3 stress_test.py --stress-only

    # Custom host/port
    python3 stress_test.py --host 127.0.0.1 --port 1234
"""

import socket
import struct
import time
import random
import string
import threading
import argparse
import sys
from typing import Any, Optional

# ─── configuration ────────────────────────────────────────────────────────────
DEFAULT_HOST    = "127.0.0.1"
DEFAULT_PORT    = 1234
STRESS_THREADS  = 8
STRESS_OPS      = 500
ZSET_LARGE_SIZE = 1500      # entries to test asyncdel threshold (>1000)
TIMEOUT_SEC     = 5.0

# ─── colors ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ─── protocol — send ──────────────────────────────────────────────────────────

def send_request(sock: socket.socket, *args: str) -> None:
    """
    Request format:
      [total_len : 4 bytes]
      [n_strings : 4 bytes]
      [len1 : 4 bytes][str1 ...]
      ...
    """
    parts = [s.encode() for s in args]
    body  = struct.pack("<I", len(parts))
    for p in parts:
        body += struct.pack("<I", len(p)) + p
    sock.sendall(struct.pack("<I", len(body)) + body)


# ─── protocol — receive ───────────────────────────────────────────────────────

def read_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Server closed connection")
        buf += chunk
    return buf


def parse_value(data: bytes, offset: int):
    """
    Parse one value from the flat byte buffer starting at 'offset'.
    Returns (python_value, new_offset).

    Wire format inside a message:
      NIL : [0x00]
      ERR : [0x01][code:4][len:4][msg...]
      STR : [0x02][len:4][bytes...]
      INT : [0x03][int64:8]
      DBL : [0x04][double:8]
      ARR : [0x05][count:4][item0][item1]...   ← items are INLINE, no own header
    """
    tag = data[offset]
    offset += 1

    # TAG_NIL
    if tag == 0:
        return None, offset

    # TAG_ERR
    if tag == 1:
        code   = struct.unpack_from("<I", data, offset)[0]; offset += 4
        length = struct.unpack_from("<I", data, offset)[0]; offset += 4
        msg    = data[offset : offset + length].decode(errors="replace")
        offset += length
        raise RuntimeError(f"Server error (code={code}): {msg}")

    # TAG_STR
    if tag == 2:
        length = struct.unpack_from("<I", data, offset)[0]; offset += 4
        value  = data[offset : offset + length].decode(errors="replace")
        return value, offset + length

    # TAG_INT
    if tag == 3:
        value = struct.unpack_from("<q", data, offset)[0]
        return value, offset + 8

    # TAG_DBL
    if tag == 4:
        value = struct.unpack_from("<d", data, offset)[0]
        return value, offset + 8

    # TAG_ARR — items are embedded in the SAME buffer, not new socket reads
    if tag == 5:
        count  = struct.unpack_from("<I", data, offset)[0]; offset += 4
        items  = []
        for _ in range(count):
            item, offset = parse_value(data, offset)
            items.append(item)
        return items, offset

    raise RuntimeError(f"Unknown tag byte: {tag}")


def recv_response(sock: socket.socket) -> Any:
    """
    Read one complete response from the server.

    Response frame:
      [msg_len : 4 bytes]   ← how many bytes follow
      [tag : 1 byte]        ← value type
      [data ...]            ← tag-specific payload (may contain nested values)
    """
    raw_len  = read_exactly(sock, 4)
    msg_len  = struct.unpack("<I", raw_len)[0]
    if msg_len == 0:
        return None
    msg      = read_exactly(sock, msg_len)
    # parse the entire message from the buffer — no more socket reads
    value, _ = parse_value(msg, 0)
    return value


def cmd(sock: socket.socket, *args: str) -> Any:
    send_request(sock, *args)
    return recv_response(sock)


def make_conn(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_SEC)
    s.connect((host, port))
    return s


# ─── test runner ──────────────────────────────────────────────────────────────

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
        msg = (f"  {RED}✗{RESET} {name}\n"
               f"    got:      {repr(got)}\n"
               f"    expected: {repr(expected)}")
        print(msg)
        self.errors.append(name)
        self.failed += 1
        return False

    def check_type(self, name: str, got: Any, expected_type: type) -> bool:
        if isinstance(got, expected_type):
            print(f"  {GREEN}✓{RESET} {name} → {repr(got)}")
            self.passed += 1
            return True
        msg = (f"  {RED}✗{RESET} {name}\n"
               f"    got type: {type(got).__name__} ({repr(got)})\n"
               f"    expected: {expected_type.__name__}")
        print(msg)
        self.errors.append(name)
        self.failed += 1
        return False

    def check_none(self, name: str, got: Any) -> bool:
        return self.check(name, got, None)

    def check_approx(self, name: str, got: Any, expected: float,
                     tol: float = 1e-9) -> bool:
        if isinstance(got, float) and abs(got - expected) < tol:
            print(f"  {GREEN}✓{RESET} {name} → {got}")
            self.passed += 1
            return True
        msg = (f"  {RED}✗{RESET} {name}\n"
               f"    got:      {repr(got)}\n"
               f"    expected: ~{expected}")
        print(msg)
        self.errors.append(name)
        self.failed += 1
        return False

    def section(self, title: str):
        pad = 50 - len(title)
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


# ─── correctness tests ────────────────────────────────────────────────────────

def test_string_commands(r: TestRunner, sock: socket.socket):
    r.section("String Commands: GET / SET / DEL")

    # SET returns nil in this server implementation — just an acknowledgment
    result = cmd(sock, "set", "k1", "hello")
    r.check_none("set k1 hello → nil", result)

    r.check("get k1 → hello",   cmd(sock, "get", "k1"), "hello")

    cmd(sock, "set", "k1", "world")
    r.check("get k1 → world",   cmd(sock, "get", "k1"), "world")

    r.check_none("get missing → nil", cmd(sock, "get", "no_such_key"))

    r.check("del k1 → 1",      cmd(sock, "del", "k1"), 1)
    r.check_none("get after del → nil", cmd(sock, "get", "k1"))
    r.check("del missing → 0", cmd(sock, "del", "no_such_key"), 0)

    # empty string value
    cmd(sock, "set", "empty", "")
    r.check("get empty → ''",  cmd(sock, "get", "empty"), "")
    cmd(sock, "del", "empty")

    # long value
    long_val = "x" * 10000
    cmd(sock, "set", "big", long_val)
    r.check("get long value",  cmd(sock, "get", "big"), long_val)
    cmd(sock, "del", "big")


def test_keys_command(r: TestRunner, sock: socket.socket):
    r.section("KEYS Command")

    cmd(sock, "del", "ka")
    cmd(sock, "del", "kb")
    cmd(sock, "del", "kc")
    cmd(sock, "set", "ka", "1")
    cmd(sock, "set", "kb", "2")
    cmd(sock, "set", "kc", "3")

    result = cmd(sock, "keys")
    r.check_type("keys returns list", result, list)

    keys_set = set(result) if result else set()
    r.check("ka in keys", "ka" in keys_set, True)
    r.check("kb in keys", "kb" in keys_set, True)
    r.check("kc in keys", "kc" in keys_set, True)

    cmd(sock, "del", "ka")
    cmd(sock, "del", "kb")
    cmd(sock, "del", "kc")


def test_ttl_commands(r: TestRunner, sock: socket.socket):
    r.section("TTL Commands: PEXPIRE / PTTL")

    cmd(sock, "set", "ttlkey", "value")
    r.check("pexpire ttlkey 5000", cmd(sock, "pexpire", "ttlkey", "5000"), 1)

    ttl = cmd(sock, "pttl", "ttlkey")
    r.check_type("pttl returns int", ttl, int)
    r.check("pttl > 0",     ttl > 0,    True)
    r.check("pttl <= 5000", ttl <= 5000, True)
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
    print(f"  {YELLOW}ℹ{RESET}  waiting 400ms for key to expire...")
    time.sleep(0.4)
    r.check_none("expired key → nil", cmd(sock, "get", "shortlived"))

    # remove TTL with negative value
    cmd(sock, "set", "removettl", "val")
    cmd(sock, "pexpire", "removettl", "5000")
    cmd(sock, "pexpire", "removettl", "-1")
    r.check("pttl after remove → -1", cmd(sock, "pttl", "removettl"), -1)
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

    # rank — sorted: n4(0.5), n1(1.5), n2(2.0), n3(3.0)
    r.check("zrank n4 → 0", cmd(sock, "zrank", zset, "n4"), 0)
    r.check("zrank n1 → 1", cmd(sock, "zrank", zset, "n1"), 1)
    r.check("zrank n2 → 2", cmd(sock, "zrank", zset, "n2"), 2)
    r.check("zrank n3 → 3", cmd(sock, "zrank", zset, "n3"), 3)
    r.check_none("zrank missing → nil", cmd(sock, "zrank", zset, "ghost"))

    r.check("zrem n1 → 1",      cmd(sock, "zrem", zset, "n1"), 1)
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
    r.check_type("zquery returns list",    result, list)
    r.check("zquery all → 10 items",       len(result), 10)
    if result and len(result) == 10:
        r.check("zquery order correct",
                result[0::2], ["a", "b", "c", "d", "e"])

    # offset
    result2 = cmd(sock, "zquery", zset, "0", "", "1", "10")
    r.check("zquery offset=1 → 8 items", len(result2), 8)

    # limit
    result3 = cmd(sock, "zquery", zset, "0", "", "0", "4")
    r.check("zquery limit=4 → 4 items",  len(result3), 4)

    # from score 3.0 → c, d, e = 6 items
    result4 = cmd(sock, "zquery", zset, "3.0", "", "0", "10")
    r.check("zquery from 3.0 → 6 items", len(result4), 6)

    # no results
    result5 = cmd(sock, "zquery", zset, "999", "", "0", "10")
    r.check("zquery no results → 0",     len(result5), 0)

    # zrevquery — descending: e, d, c, b, a
    result6 = cmd(sock, "zrevquery", zset, "999", "", "0", "10")
    r.check_type("zrevquery returns list", result6, list)
    r.check("zrevquery all → 10 items",    len(result6), 10)
    if result6 and len(result6) == 10:
        r.check("zrevquery order correct",
                result6[0::2], ["e", "d", "c", "b", "a"])

    # from score 3.0 descending → c, b, a = 6 items
    result7 = cmd(sock, "zrevquery", zset, "3.5", "", "0", "10")
    r.check("zrevquery from 3.5 → 6 items", len(result7), 6)

    cmd(sock, "del", zset)


def test_asyncdel_command(r: TestRunner, sock: socket.socket):
    r.section("ASYNCDEL Command")

    # missing key → 0
    cmd(sock, "del", "asynctest")
    r.check("asyncdel missing → 0", cmd(sock, "asyncdel", "asynctest"), 0)

    # small string — synchronous path
    cmd(sock, "set", "asynctest", "hello")
    r.check("asyncdel string → 1",  cmd(sock, "asyncdel", "asynctest"), 1)
    r.check_none("string gone",     cmd(sock, "get", "asynctest"))

    # small zset — synchronous path
    small = "small_async_zset"
    cmd(sock, "del", small)
    for i in range(10):
        cmd(sock, "zadd", small, str(float(i)), f"m{i}")
    r.check("asyncdel small zset → 1", cmd(sock, "asyncdel", small), 1)
    r.check_none("small zset gone",    cmd(sock, "zscore", small, "m0"))

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

    r.check("asyncdel large → 1",         result, 1)
    r.check("asyncdel fast (<100ms)",      elapsed < 0.1, True)
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
    result = cmd(sock, "zquery", "samescores", "0", "", "0", "10")
    if result and len(result) == 6:
        r.check("same score sorted by name", result[0::2], ["a", "b", "c"])
    cmd(sock, "del", "samescores")

    # special characters in value
    special = "hello\tworld\nnewline"
    cmd(sock, "set", "special", special)
    r.check("special chars", cmd(sock, "get", "special"), special)
    cmd(sock, "del", "special")

    # 100 rapid set/get/del
    for i in range(100):
        cmd(sock, "set", f"rapid{i}", str(i))
    ok = all(cmd(sock, "get", f"rapid{i}") == str(i) for i in range(100))
    r.check("100 rapid get correct", ok, True)
    for i in range(100):
        cmd(sock, "del", f"rapid{i}")
    print(f"  {YELLOW}ℹ{RESET}  100 rapid set/get/del complete")


# ─── stress test ──────────────────────────────────────────────────────────────

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
    try:
        cmd(sock, "del", zset)
        for i in range(10):
            cmd(sock, "zadd", zset, str(float(i)), f"m{i}")
    except Exception:
        pass

    for _ in range(ops):
        op = random.randint(0, 9)
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
                cmd(sock, "zscore", zset, f"m{random.randint(0,9)}")
            elif op == 5:
                cmd(sock, "zrank",  zset, f"m{random.randint(0,9)}")
            elif op == 6:
                cmd(sock, "zquery", zset,
                    str(random.uniform(-50, 50)), "", "0", "5")
            elif op == 7:
                cmd(sock, "zrevquery", zset,
                    str(random.uniform(50, 150)),  "", "0", "5")
            elif op == 8:
                k = random_key()
                cmd(sock, "set",     k, "val")
                cmd(sock, "pexpire", k, "10000")
                cmd(sock, "pttl",    k)
            elif op == 9:
                cmd(sock, "keys")

            stats.record((time.perf_counter() - t0) * 1000)
        except Exception:
            stats.record_error()

    try:
        cmd(sock, "del", zset)
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


# ─── concurrent safety ────────────────────────────────────────────────────────

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
        for e in errors[:3]: print(f"    {e}")
        return False
    print(f"  {GREEN}✓ 10 threads × 50 ops, no errors{RESET}")
    return True


# ─── cleanup ─────────────────────────────────────────────────────────────────

def cleanup_stress_keys(host: str, port: int):
    try:
        sock = make_conn(host, port)
        keys = cmd(sock, "keys")
        if keys:
            sk = [k for k in keys if k.startswith("stress_")]
            for k in sk:
                cmd(sock, "del", k)
            if sk:
                print(f"  {YELLOW}ℹ{RESET}  cleaned {len(sk)} stress keys")
        sock.close()
    except Exception:
        pass


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host",             default=DEFAULT_HOST)
    ap.add_argument("--port",             default=DEFAULT_PORT, type=int)
    ap.add_argument("--correctness-only", action="store_true")
    ap.add_argument("--stress-only",      action="store_true")
    args = ap.parse_args()

    host, port = args.host, args.port

    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  Redis Server Stress Test{RESET}")
    print(f"  Connecting to {host}:{port}")
    print(f"{'═' * 55}")

    try:
        s = make_conn(host, port); s.close()
        print(f"{GREEN}✓ Server is reachable{RESET}")
    except Exception as e:
        print(f"{RED}✗ Cannot connect: {e}{RESET}")
        print("  Start the server first:  ./server")
        sys.exit(1)

    all_ok = True

    if not args.stress_only:
        r    = TestRunner(host, port)
        sock = make_conn(host, port)
        try:
            test_string_commands(r,  sock)
            test_keys_command(r,     sock)
            test_ttl_commands(r,     sock)
            test_zset_commands(r,    sock)
            test_zquery_commands(r,  sock)
            test_asyncdel_command(r, sock)
            test_edge_cases(r,       sock)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False
        finally:
            sock.close()
        all_ok = r.summary() and all_ok

    if not args.stress_only and not args.correctness_only:
        all_ok = test_concurrent_writes(host, port) and all_ok

    if not args.correctness_only:
        all_ok = run_stress_test(host, port) and all_ok
        cleanup_stress_keys(host, port)

    print(f"\n{BOLD}{'═' * 55}{RESET}")
    if all_ok:
        print(f"{BOLD}{GREEN}  ALL TESTS PASSED{RESET}")
    else:
        print(f"{BOLD}{RED}  SOME TESTS FAILED{RESET}")
    print(f"{'═' * 55}\n")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
