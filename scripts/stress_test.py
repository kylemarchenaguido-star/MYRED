#!/usr/bin/env python3
"""
Stress test for custom Redis server — RESP protocol edition.
redis-benchmark -h 127.0.0.1 -p 1234 -a kek1234 \
  -t set,get,lpush,rpush,lpop,rpop -n 200000 -c 50 -P 16
Tests all commands:
  Strings: get, set, del
  Generic: exists, type, expire, pexpire, ttl, pttl, persist, keys, scan,
           dbsize, randomkey, rename, renamenx, touch, unlink,
           expireat, pexpireat, flushall
  ZSet:    zadd, zrem, zscore, zquery, zrevquery, zrank, zpopmin
  Lists:   lpush, rpush, lpop, rpop, llen, lindex, lrange, lset, linsert, lrem, ltrim
  Hashes:  hset, hget, hdel, hexists, hlen, hgetall, hkeys, hvals, hmget,
           hsetnx, hincrby, hstrlen, hscan
  Admin:   auth, acl, config, info, save, bgsave, bgrewriteaof,
           memory, object, ping

Because the server now speaks RESP, this test also works against the
real redis-cli for cross-validation.

Usage (run from the repo root):
    # Terminal 1 — start server
    ./build/server

    # Terminal 2 — run tests
    python3 scripts/stress_test.py

    # only correctness, no stress
    python3 scripts/stress_test.py --correctness-only

    # only stress
    python3 scripts/stress_test.py --stress-only

    # tune stress size and metrics tables
    python3 scripts/stress_test.py --stress-threads 16 --stress-ops 2000 --metrics-top 20

    # redis-benchmark speed baseline (Release build only — a Debug build runs
    # mem_selfcheck's whole-keyspace walk per command and the numbers are garbage)
    python3 scripts/stress_test.py --bench

    # if your server requires a password
    python3 scripts/stress_test.py --password your_password_here

    # custom host/port
    python3 scripts/stress_test.py --host 127.0.0.1 --port 1234
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
import shutil
import subprocess
import atexit
import ssl
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

# TLS config, set from argparse in main(). G_TLS gates client-socket wrapping
# and the redis-benchmark --tls flags; all connections share one SSLContext.
G_TLS: bool = False
G_TLS_INSECURE: bool = False
G_TLS_CA: Optional[str] = None
G_TLS_CERT: Optional[str] = None
G_TLS_KEY: Optional[str] = None
_G_TLS_CTX: Optional["ssl.SSLContext"] = None

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
        try:
            self._fh.flush()
        except ValueError:
            pass    # interpreter's final flush runs after atexit closed the log


def run_kind(args) -> str:
    """Which phase mix this invocation runs. Precedence: bench > stress > correctness."""
    if args.bench:
        return "bench"
    if args.stress_only:
        return "stress"
    if args.correctness_only:
        return "correctness"
    return "stress_results"


def default_log_path(args) -> str:
    """Per-run log name so a TLS run never overwrites the plaintext one.

    docs/{bench,stress,correctness,stress_results}_{plain,tls}.md
    """
    return os.path.join("docs",
                        f"{run_kind(args)}_{'tls' if args.tls else 'plain'}.md")


def run_label(args, host: str, port: int) -> str:
    """One-line human description of what this run actually covers."""
    phases = {
        "bench":          "correctness + concurrency + stress + redis-benchmark",
        "stress":         "stress only",
        "correctness":    "correctness only",
        "stress_results": "correctness + concurrency + stress",
    }[run_kind(args)]
    return (f"{phases} over {'TLS' if args.tls else 'plaintext'} "
            f"({'authenticated' if args.password else 'passwordless'}) "
            f"→ {host}:{port}")


def start_logging(path: str, label: str = ""):
    """Redirect stdout through a tee into `path` (markdown, fenced code block)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fh = open(path, "w", encoding="utf-8")
    fh.write(f"# MYRED stress test — {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    if label:
        fh.write(f"**Run:** {label}\n\n")
    fh.write("```\n")
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


def _percentile(sorted_values, pct: float) -> float:
    """Return a nearest-rank percentile from an already sorted non-empty list."""
    if not sorted_values:
        return 0.0
    idx = int(round((len(sorted_values) - 1) * pct))
    idx = max(0, min(idx, len(sorted_values) - 1))
    return sorted_values[idx]


class CommandMetrics:
    """Lightweight command-level latency and error accounting."""
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.resp_errors = 0
        self.transport_errors = 0
        self.latencies = []
        self.by_cmd = {}

    def record(self, name: str, ms: float, *, resp_error: bool = False,
               transport_error: bool = False):
        name = (name or "<empty>").lower()
        with self.lock:
            self.total += 1
            self.latencies.append(ms)
            if resp_error:
                self.resp_errors += 1
            if transport_error:
                self.transport_errors += 1

            row = self.by_cmd.setdefault(name, {
                "count": 0,
                "resp_errors": 0,
                "transport_errors": 0,
                "latencies": [],
            })
            row["count"] += 1
            row["latencies"].append(ms)
            if resp_error:
                row["resp_errors"] += 1
            if transport_error:
                row["transport_errors"] += 1

    def report(self, top_n: int = 12):
        with self.lock:
            total = self.total
            resp_errors = self.resp_errors
            transport_errors = self.transport_errors
            latencies = list(self.latencies)
            by_cmd = {
                k: {
                    "count": v["count"],
                    "resp_errors": v["resp_errors"],
                    "transport_errors": v["transport_errors"],
                    "latencies": list(v["latencies"]),
                }
                for k, v in self.by_cmd.items()
            }

        print(f"\n{BOLD}{BLUE}-- Command Metrics {'-' * 37}{RESET}")
        if not latencies:
            print("  No commands recorded.")
            return

        srt = sorted(latencies)
        avg = sum(srt) / len(srt)
        print(f"  Commands observed: {total}")
        print(f"  RESP errors:       {resp_errors} (expected negative tests included)")
        print(f"  Transport errors:  {transport_errors}")
        print(f"  Latency avg:       {avg:.2f}ms")
        print(f"  Latency p50/p95/p99: "
              f"{_percentile(srt, 0.50):.2f}/"
              f"{_percentile(srt, 0.95):.2f}/"
              f"{_percentile(srt, 0.99):.2f}ms")
        print(f"  Latency max:       {srt[-1]:.2f}ms")

        common = sorted(by_cmd.items(),
                        key=lambda item: item[1]["count"],
                        reverse=True)[:top_n]
        print("  Most used commands:")
        for name, row in common:
            print(f"    {name:<14} {row['count']:>6} calls")

        slow = sorted(by_cmd.items(),
                      key=lambda item: (
                          sum(item[1]["latencies"]) / len(item[1]["latencies"])
                      ),
                      reverse=True)[:top_n]
        print("  Slowest commands by average latency:")
        for name, row in slow:
            vals = row["latencies"]
            avg_ms = sum(vals) / len(vals)
            print(f"    {name:<14} {avg_ms:>8.2f}ms avg over {len(vals)} calls")


COMMAND_METRICS = CommandMetrics()


def cmd(sock: socket.socket, *args: str) -> Any:
    """Send a command and return the parsed reply."""
    name = str(args[0]) if args else "<empty>"
    t0 = time.perf_counter()
    try:
        send_request(sock, *args)
        reply = recv_response(sock)
        COMMAND_METRICS.record(name, (time.perf_counter() - t0) * 1000)
        return reply
    except RespError:
        COMMAND_METRICS.record(name, (time.perf_counter() - t0) * 1000,
                               resp_error=True)
        raise
    except Exception:
        COMMAND_METRICS.record(name, (time.perf_counter() - t0) * 1000,
                               transport_error=True)
        raise



def _tls_context() -> "ssl.SSLContext":
    """Build (once) the shared client SSLContext from the --tls-* args."""
    global _G_TLS_CTX
    if _G_TLS_CTX is not None:
        return _G_TLS_CTX
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if G_TLS_CA:
        ctx.load_verify_locations(G_TLS_CA)
    if G_TLS_CERT:                        # optional client cert (mTLS)
        ctx.load_cert_chain(G_TLS_CERT, G_TLS_KEY)
    if G_TLS_INSECURE:                    # self-signed test certs: skip verification
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    _G_TLS_CTX = ctx
    return ctx


def open_socket(host: str, port: int) -> socket.socket:
    """TCP connect, wrapped in TLS when --tls is set. Does NOT authenticate."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(TIMEOUT_SEC)
    raw.connect((host, port))
    if not G_TLS:
        return raw
    sni = None if G_TLS_INSECURE else host
    return _tls_context().wrap_socket(raw, server_hostname=sni)


def _authenticate(sock: socket.socket, *auth_args: str) -> None:
    """AUTH, retrying past the server's BUSY throttle (k_max_auth_inflight=4).
    Concurrent workers all AUTH at connect, so the 5th+ can bounce with BUSY;
    a bounded retry lets the stress/concurrent phases run over an authed port."""
    deadline = time.time() + TIMEOUT_SEC
    delay = 0.01
    while True:
        send_request(sock, "auth", *auth_args)
        try:
            recv_response(sock)           # +OK, or raises RespError
            return
        except RespError as e:
            if "BUSY" in str(e) and time.time() < deadline:
                time.sleep(delay)
                delay = min(delay * 2, 0.2)
                continue
            raise


def make_conn(host: str, port: int) -> socket.socket:
    """Open a connection, authenticating first if a password is configured."""
    s = open_socket(host, port)
    if G_PASSWORD:
        _authenticate(s, G_PASSWORD)      # retries past the AUTH throttle
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
        self.started_at = time.perf_counter()
        self.current_section = None
        self.section_started_at = None
        self.section_passed = 0
        self.section_failed = 0
        self.section_stats = []

    def _record_result(self, ok: bool):
        if ok:
            self.section_passed += 1
        else:
            self.section_failed += 1

    def _finish_section(self):
        if self.current_section is None or self.section_started_at is None:
            return
        self.section_stats.append({
            "title": self.current_section,
            "passed": self.section_passed,
            "failed": self.section_failed,
            "duration": time.perf_counter() - self.section_started_at,
        })
        self.current_section = None
        self.section_started_at = None
        self.section_passed = 0
        self.section_failed = 0

    def check(self, name: str, got: Any, expected: Any) -> bool:
        if got == expected:
            print(f"  {GREEN}✓{RESET} {name}")
            self.passed += 1
            self._record_result(True)
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: {expected!r}")
        self.errors.append(name)
        self.failed += 1
        self._record_result(False)
        return False

    def check_type(self, name: str, got: Any, expected_type: type) -> bool:
        if isinstance(got, expected_type):
            print(f"  {GREEN}✓{RESET} {name} → {got!r}")
            self.passed += 1
            self._record_result(True)
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got type: {type(got).__name__} ({got!r})\n"
              f"    expected: {expected_type.__name__}")
        self.errors.append(name)
        self.failed += 1
        self._record_result(False)
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
            self._record_result(True)
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: ~{expected}")
        self.errors.append(name)
        self.failed += 1
        self._record_result(False)
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
            self._record_result(True)
            return True
        print(f"  {RED}✗{RESET} {name}\n"
              f"    got:      {got!r}\n"
              f"    expected: a RESP error")
        self.errors.append(name)
        self.failed += 1
        self._record_result(False)
        return False

    def section(self, title: str):
        self._finish_section()
        self.current_section = title
        self.section_started_at = time.perf_counter()
        pad = max(0, 50 - len(title))
        print(f"\n{BOLD}{BLUE}── {title} {'─' * pad}{RESET}")

    def summary(self) -> bool:
        self._finish_section()
        total = self.passed + self.failed
        elapsed = time.perf_counter() - self.started_at
        print(f"\n{BOLD}{'═' * 55}{RESET}")
        print(f"{BOLD}Results: {self.passed}/{total} passed{RESET}")
        if elapsed > 0:
            print(f"Runtime: {elapsed:.2f}s ({total / elapsed:.1f} assertions/sec)")
        if self.failed:
            print(f"{RED}Failed tests:{RESET}")
            for e in self.errors:
                print(f"  • {e}")
        else:
            print(f"{GREEN}All tests passed!{RESET}")
        if self.section_stats:
            print("Slowest sections:")
            slow = sorted(self.section_stats,
                          key=lambda row: row["duration"],
                          reverse=True)[:8]
            for row in slow:
                total_checks = row["passed"] + row["failed"]
                print(f"  {row['duration']:.2f}s  "
                      f"{row['passed']}/{total_checks}  {row['title']}")
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


def test_numeric_commands(r: TestRunner, sock: socket.socket):
    r.section("String Numerics: INCR / DECR / INCRBY / DECRBY / INCRBYFLOAT")

    for k in ("nc", "nc2", "nc3", "nf", "nf2"):
        cmd(sock, "del", k)

    # INCR on missing key → starts at 0, returns 1
    r.check("incr missing → 1",      cmd(sock, "incr", "nc"), 1)
    r.check("incr again → 2",        cmd(sock, "incr", "nc"), 2)
    r.check("incr again → 3",        cmd(sock, "incr", "nc"), 3)

    # DECR
    r.check("decr → 2",              cmd(sock, "decr", "nc"), 2)
    r.check("decr → 1",              cmd(sock, "decr", "nc"), 1)

    # INCRBY
    r.check("incrby 10 → 11",        cmd(sock, "incrby", "nc", "10"), 11)
    r.check("incrby -5 → 6",         cmd(sock, "incrby", "nc", "-5"), 6)

    # DECRBY
    r.check("decrby 3 → 3",          cmd(sock, "decrby", "nc", "3"), 3)
    r.check("decrby -2 → 5",         cmd(sock, "decrby", "nc", "-2"), 5)

    # operations on a key with an explicit string value
    cmd(sock, "set", "nc", "100")
    r.check("incr from '100' → 101", cmd(sock, "incr", "nc"), 101)
    cmd(sock, "set", "nc", "0")
    r.check("decr from '0' → -1",    cmd(sock, "decr", "nc"), -1)

    # non-integer value → error
    cmd(sock, "set", "nc2", "notanumber")
    r.expect_error("incr on non-int value → error",    sock, "incr",   "nc2")
    r.expect_error("incrby on non-int value → error",  sock, "incrby", "nc2", "5")
    r.expect_error("decrby on non-int value → error",  sock, "decrby", "nc2", "1")

    # wrong type → WRONGTYPE
    cmd(sock, "del", "nc3")
    cmd(sock, "sadd", "nc3", "x")
    r.expect_error("incr on set → WRONGTYPE",   sock, "incr",   "nc3")
    r.expect_error("incrby on set → WRONGTYPE", sock, "incrby", "nc3", "1")

    # INCRBYFLOAT
    cmd(sock, "set", "nf", "10.5")
    r.check_approx("incrbyfloat 0.1 → ~10.6",  cmd(sock, "incrbyfloat", "nf", "0.1"),  10.6)
    r.check_approx("incrbyfloat -3.5 → ~7.1",  cmd(sock, "incrbyfloat", "nf", "-3.5"), 7.1)
    r.check_approx("incrbyfloat 0 → ~7.1",     cmd(sock, "incrbyfloat", "nf", "0"),    7.1)

    # incrbyfloat on missing key → delta as value
    r.check_approx("incrbyfloat missing → 1.5", cmd(sock, "incrbyfloat", "nf2", "1.5"), 1.5)

    # incrbyfloat produces a string result, not an integer
    r.check_type("incrbyfloat returns str", cmd(sock, "incrbyfloat", "nf", "0"), str)

    # NaN / infinity → error
    r.expect_error("incrbyfloat inf → error",  sock, "incrbyfloat", "nf", "inf")
    r.expect_error("incrbyfloat -inf → error", sock, "incrbyfloat", "nf", "-inf")

    # overflow / underflow → error
    cmd(sock, "set", "nc", str(2**63 - 1))
    r.expect_error("incr at INT64_MAX → overflow", sock, "incr", "nc")

    cmd(sock, "set", "nc", str(-2**63))
    r.expect_error("decr at INT64_MIN → overflow", sock, "decr", "nc")

    for k in ("nc", "nc2", "nc3", "nf", "nf2"):
        cmd(sock, "del", k)


def test_setvariant_commands(r: TestRunner, sock: socket.socket):
    r.section("String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL")

    for k in ("sv1", "sv2", "sv3", "sv4", "sv5", "sv_wt"):
        cmd(sock, "del", k)

    # ── SETNX ─────────────────────────────────────────────────────────────────
    r.check("setnx missing → 1",       cmd(sock, "setnx", "sv1", "hello"), 1)
    r.check("get after setnx → hello", cmd(sock, "get",   "sv1"),          "hello")
    r.check("setnx existing → 0",      cmd(sock, "setnx", "sv1", "world"), 0)
    r.check("value unchanged → hello", cmd(sock, "get",   "sv1"),          "hello")

    # any existing type counts as "exists" → 0
    cmd(sock, "sadd", "sv_wt", "x")
    r.check("setnx on set → 0 (key exists)", cmd(sock, "setnx", "sv_wt", "v"), 0)
    cmd(sock, "del", "sv_wt")

    # ── SETEX ─────────────────────────────────────────────────────────────────
    r.check("setex 10s → OK",         cmd(sock, "setex", "sv2", "10", "exval"), "OK")
    r.check("get sv2 → exval",        cmd(sock, "get", "sv2"),                  "exval")
    ttl = cmd(sock, "pttl", "sv2")
    r.check_true("setex ttl > 0",     ttl > 0)
    r.check_true("setex ttl ≤ 10000", ttl <= 10000)
    r.expect_error("setex ttl=0 → error",  sock, "setex", "sv2", "0",   "v")
    r.expect_error("setex ttl=-1 → error", sock, "setex", "sv2", "-1",  "v")
    r.expect_error("setex non-int → error",sock, "setex", "sv2", "abc", "v")

    # ── PSETEX ────────────────────────────────────────────────────────────────
    r.check("psetex 5000ms → OK",      cmd(sock, "psetex", "sv3", "5000", "msval"), "OK")
    r.check("get sv3 → msval",         cmd(sock, "get", "sv3"),                     "msval")
    ttl = cmd(sock, "pttl", "sv3")
    r.check_true("psetex ttl > 0",     ttl > 0)
    r.check_true("psetex ttl ≤ 5000",  ttl <= 5000)
    r.expect_error("psetex ttl=0 → error", sock, "psetex", "sv3", "0", "v")

    # actual expiry: set a very short TTL and wait
    r.check("psetex 200ms → OK",       cmd(sock, "psetex", "sv3", "200", "gone"), "OK")
    print(f"  \033[33mℹ\033[0m  waiting 400ms for psetex key to expire...")
    time.sleep(0.4)
    r.check_none("sv3 expired → nil",  cmd(sock, "get", "sv3"))

    # ── GETSET ────────────────────────────────────────────────────────────────
    cmd(sock, "set", "sv4", "old")
    r.check("getset returns old value", cmd(sock, "getset", "sv4", "new"),  "old")
    r.check("get after getset → new",   cmd(sock, "get",    "sv4"),         "new")

    # getset on missing key → nil, then creates the key
    cmd(sock, "del", "sv5")
    r.check_none("getset missing → nil",     cmd(sock, "getset", "sv5", "first"))
    r.check("key created by getset → first", cmd(sock, "get", "sv5"),             "first")

    # getset on wrong type → WRONGTYPE
    cmd(sock, "sadd", "sv_wt", "x")
    r.expect_error("getset on set → WRONGTYPE", sock, "getset", "sv_wt", "v")
    cmd(sock, "del", "sv_wt")

    # ── GETEX ─────────────────────────────────────────────────────────────────
    cmd(sock, "set", "sv4", "gxval")
    cmd(sock, "persist", "sv4")

    # bare: just get, no TTL change
    r.check("getex bare → value",      cmd(sock, "getex", "sv4"),        "gxval")
    r.check("pttl unchanged → -1",     cmd(sock, "pttl", "sv4"),         -1)

    # EX
    r.check("getex EX 5 → value",      cmd(sock, "getex", "sv4", "EX", "5"), "gxval")
    ttl = cmd(sock, "pttl", "sv4")
    r.check_true("getex EX set ttl > 0",     ttl > 0)
    r.check_true("getex EX set ttl ≤ 5000",  ttl <= 5000)

    # PERSIST clears the TTL
    r.check("getex PERSIST → value",   cmd(sock, "getex", "sv4", "PERSIST"), "gxval")
    r.check("pttl after PERSIST → -1", cmd(sock, "pttl", "sv4"),             -1)

    # PX
    r.check("getex PX 3000 → value",   cmd(sock, "getex", "sv4", "PX", "3000"), "gxval")
    ttl = cmd(sock, "pttl", "sv4")
    r.check_true("getex PX set ttl > 0",     ttl > 0)
    r.check_true("getex PX set ttl ≤ 3000",  ttl <= 3000)
    cmd(sock, "persist", "sv4")

    # missing key → nil (no option)
    cmd(sock, "del", "sv5")
    r.check_none("getex missing → nil", cmd(sock, "getex", "sv5"))

    # invalid option → error
    r.expect_error("getex bad opt → error",  sock, "getex", "sv4", "BADOPT", "5")
    r.expect_error("getex EX 0 → error",     sock, "getex", "sv4", "EX", "0")
    r.expect_error("getex PX -1 → error",    sock, "getex", "sv4", "PX", "-1")

    # ── GETDEL ────────────────────────────────────────────────────────────────
    cmd(sock, "set", "sv5", "delval")
    r.check("getdel → value",               cmd(sock, "getdel", "sv5"), "delval")
    r.check_none("key gone after getdel",   cmd(sock, "get", "sv5"))

    cmd(sock, "del", "sv5")
    r.check_none("getdel missing → nil",    cmd(sock, "getdel", "sv5"))

    cmd(sock, "sadd", "sv_wt", "x")
    r.expect_error("getdel on set → WRONGTYPE", sock, "getdel", "sv_wt")

    for k in ("sv1", "sv2", "sv3", "sv4", "sv5", "sv_wt"):
        cmd(sock, "del", k)


def test_multikey_commands(r: TestRunner, sock: socket.socket):
    r.section("String Multi-key: MSET / MGET / MSETNX")

    for k in ("mk1", "mk2", "mk3", "mk4", "mk_wt"):
        cmd(sock, "del", k)

    # ── MSET ──────────────────────────────────────────────────────────────────
    r.check("mset 3 pairs → OK",  cmd(sock, "mset", "mk1", "a", "mk2", "b", "mk3", "c"), "OK")
    r.check("get mk1 → a",        cmd(sock, "get", "mk1"), "a")
    r.check("get mk2 → b",        cmd(sock, "get", "mk2"), "b")
    r.check("get mk3 → c",        cmd(sock, "get", "mk3"), "c")

    # mset replaces existing values
    r.check("mset overwrites → OK", cmd(sock, "mset", "mk1", "x", "mk2", "y"), "OK")
    r.check("mk1 now x",            cmd(sock, "get", "mk1"), "x")
    r.check("mk2 now y",            cmd(sock, "get", "mk2"), "y")

    # mset with duplicate keys: last value wins
    r.check("mset dup key → OK", cmd(sock, "mset", "mk4", "first", "mk4", "second"), "OK")
    r.check("mk4 → second (last wins)", cmd(sock, "get", "mk4"), "second")

    # ── MGET ──────────────────────────────────────────────────────────────────
    r.check("mget 3 keys → list",  cmd(sock, "mget", "mk1", "mk2", "mk3"), ["x", "y", "c"])

    # mget: missing key → nil in that slot
    cmd(sock, "del", "mk4")
    result = cmd(sock, "mget", "mk1", "mk4", "mk3")
    r.check_type("mget returns list",      result, list)
    r.check("mget[0] → x",                result[0], "x")
    r.check_none("mget[1] → nil (missing)", result[1])
    r.check("mget[2] → c",                result[2], "c")

    # mget: wrong type → nil (NOT a WRONGTYPE error)
    cmd(sock, "sadd", "mk_wt", "x")
    result = cmd(sock, "mget", "mk1", "mk_wt", "mk3")
    r.check("mget list has 3 elements", len(result), 3)
    r.check("mget[0] → x",              result[0], "x")
    r.check_none("mget wrong-type → nil", result[1])
    r.check("mget[2] → c",              result[2], "c")
    cmd(sock, "del", "mk_wt")

    # mget single key
    r.check("mget 1 key → [x]", cmd(sock, "mget", "mk1"), ["x"])

    # ── MSETNX ────────────────────────────────────────────────────────────────
    for k in ("mn1", "mn2", "mn3"):
        cmd(sock, "del", k)

    # all missing → sets all, returns 1
    r.check("msetnx all missing → 1", cmd(sock, "msetnx", "mn1", "v1", "mn2", "v2"), 1)
    r.check("mn1 → v1",              cmd(sock, "get", "mn1"), "v1")
    r.check("mn2 → v2",              cmd(sock, "get", "mn2"), "v2")

    # one key exists → sets nothing, returns 0
    r.check("msetnx one exists → 0", cmd(sock, "msetnx", "mn1", "new", "mn3", "v3"), 0)
    r.check("mn1 unchanged → v1",    cmd(sock, "get", "mn1"), "v1")
    r.check_none("mn3 not created",  cmd(sock, "get", "mn3"))

    # any existing type blocks msetnx
    cmd(sock, "sadd", "mk_wt", "x")
    r.check("msetnx blocks on any type → 0",
            cmd(sock, "msetnx", "mk_wt", "v", "mn3", "v3"), 0)
    r.check_none("mn3 still not set", cmd(sock, "get", "mn3"))
    cmd(sock, "del", "mk_wt")

    for k in ("mk1", "mk2", "mk3", "mk4", "mn1", "mn2", "mn3", "mk_wt"):
        cmd(sock, "del", k)


def test_bulkrange_commands(r: TestRunner, sock: socket.socket):
    r.section("String Bulk/Range: APPEND / STRLEN / GETRANGE / SETRANGE")

    for k in ("br1", "br2", "br3", "br_wt"):
        cmd(sock, "del", k)

    # ── APPEND ────────────────────────────────────────────────────────────────
    # append to missing key → creates it
    r.check("append missing → 5",  cmd(sock, "append", "br1", "hello"), 5)
    r.check("get br1 → hello",     cmd(sock, "get", "br1"), "hello")

    # append to existing
    r.check("append ` world` → 11", cmd(sock, "append", "br1", " world"), 11)
    r.check("get br1 → hello world", cmd(sock, "get", "br1"), "hello world")

    # append empty string → length unchanged
    r.check("append '' → 11",  cmd(sock, "append", "br1", ""), 11)

    # append on wrong type → WRONGTYPE
    cmd(sock, "sadd", "br_wt", "x")
    r.expect_error("append on set → WRONGTYPE", sock, "append", "br_wt", "v")

    # ── STRLEN ────────────────────────────────────────────────────────────────
    r.check("strlen br1 → 11",       cmd(sock, "strlen", "br1"), 11)
    r.check("strlen missing → 0",    cmd(sock, "strlen", "br2"), 0)

    cmd(sock, "set", "br2", "")
    r.check("strlen empty str → 0",  cmd(sock, "strlen", "br2"), 0)

    r.expect_error("strlen on set → WRONGTYPE", sock, "strlen", "br_wt")

    # ── GETRANGE ──────────────────────────────────────────────────────────────
    cmd(sock, "set", "br3", "Hello, World!")   # len=13, indices 0-12

    r.check("getrange 0 4 → Hello",     cmd(sock, "getrange", "br3", "0",  "4"),  "Hello")
    r.check("getrange 7 11 → World",    cmd(sock, "getrange", "br3", "7",  "11"), "World")
    r.check("getrange 0 -1 → full str", cmd(sock, "getrange", "br3", "0",  "-1"), "Hello, World!")
    r.check("getrange -6 -1 → World!",  cmd(sock, "getrange", "br3", "-6", "-1"), "World!")
    r.check("getrange 0 0 → H",         cmd(sock, "getrange", "br3", "0",  "0"),  "H")
    r.check("getrange -1 -1 → !",       cmd(sock, "getrange", "br3", "-1", "-1"), "!")

    # out-of-range: clamp, never error
    r.check("getrange 0 999 → full",    cmd(sock, "getrange", "br3", "0",   "999"),  "Hello, World!")
    r.check("getrange 5 3 → ''",        cmd(sock, "getrange", "br3", "5",   "3"),    "")
    r.check("getrange 99 100 → ''",     cmd(sock, "getrange", "br3", "99",  "100"),  "")
    r.check("getrange -99 -99 → ''",    cmd(sock, "getrange", "br3", "-99", "-99"),  "")

    # missing key → empty string (not nil)
    r.check("getrange missing → ''",    cmd(sock, "getrange", "no_such_key", "0", "-1"), "")

    r.expect_error("getrange on set → WRONGTYPE", sock, "getrange", "br_wt", "0", "-1")

    # ── SETRANGE ──────────────────────────────────────────────────────────────
    cmd(sock, "set", "br3", "Hello World")   # len=11

    r.check("setrange offset 6 → 11",   cmd(sock, "setrange", "br3", "6", "Redis"), 11)
    r.check("get br3 → Hello Redis",    cmd(sock, "get", "br3"), "Hello Redis")

    # setrange extending the string (zero-pad)
    cmd(sock, "del", "br2")
    r.check("setrange offset 5 on empty → 8",
            cmd(sock, "setrange", "br2", "5", "abc"), 8)
    result = cmd(sock, "get", "br2")
    r.check("first 5 bytes are null-padded",
            result[5:], "abc")
    r.check("setrange result length", len(result), 8)

    # setrange on missing key (create + zero-pad)
    cmd(sock, "del", "br3")
    r.check("setrange missing key → 5", cmd(sock, "setrange", "br3", "0", "hello"), 5)
    r.check("get br3 → hello",          cmd(sock, "get", "br3"), "hello")

    # setrange with empty value on missing → creates zero-padded key
    cmd(sock, "del", "br3")
    r.check("setrange empty val offset=3 → 3",
            cmd(sock, "setrange", "br3", "3", ""), 3)
    r.check("strlen br3 → 3", cmd(sock, "strlen", "br3"), 3)

    # error cases
    r.expect_error("setrange offset -1 → error", sock, "setrange", "br3", "-1", "v")
    r.expect_error("setrange on set → WRONGTYPE", sock, "setrange", "br_wt", "0", "v")

    for k in ("br1", "br2", "br3", "br_wt"):
        cmd(sock, "del", k)


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


def test_generic_commands(r: TestRunner, sock: socket.socket):
    r.section("Generic: EXISTS / TYPE / EXPIRE / TTL / PERSIST")

    cmd(sock, "del", "gk")
    r.check("exists missing → 0", cmd(sock, "exists", "gk"), 0)
    cmd(sock, "set", "gk", "v")
    r.check("exists present → 1", cmd(sock, "exists", "gk"), 1)

    # TYPE for each value type
    r.check("type string", cmd(sock, "type", "gk"), "string")
    cmd(sock, "del", "gz"); cmd(sock, "zadd", "gz", "1", "m")
    r.check("type zset", cmd(sock, "type", "gz"), "zset")
    cmd(sock, "del", "gl"); cmd(sock, "rpush", "gl", "a")
    r.check("type list", cmd(sock, "type", "gl"), "list")
    cmd(sock, "del", "gh"); cmd(sock, "hset", "gh", "f", "v")
    r.check("type hash", cmd(sock, "type", "gh"), "hash")
    r.check("type missing → none", cmd(sock, "type", "nope"), "none")
    for k in ("gz", "gl", "gh"):
        cmd(sock, "del", k)

    # EXPIRE (seconds) + TTL (seconds) + PERSIST
    r.check("expire gk 100 → 1", cmd(sock, "expire", "gk", "100"), 1)
    ttl = cmd(sock, "ttl", "gk")
    r.check_true("ttl in (0,100]", isinstance(ttl, int) and 0 < ttl <= 100)
    r.check("ttl no-such-key → -2", cmd(sock, "ttl", "nope"), -2)
    r.check("persist gk → 1", cmd(sock, "persist", "gk"), 1)
    r.check("ttl after persist → -1", cmd(sock, "ttl", "gk"), -1)
    r.check("persist again → 0", cmd(sock, "persist", "gk"), 0)

    # EXPIRE with non-positive ttl deletes the key (Redis semantics)
    r.check("expire gk -1 deletes → 1", cmd(sock, "expire", "gk", "-1"), 1)
    r.check("exists after expire -1 → 0", cmd(sock, "exists", "gk"), 0)

    cmd(sock, "del", "gk")


def test_hash_commands(r: TestRunner, sock: socket.socket):
    r.section("Hashes: HSET / HGET / HDEL / HEXISTS / HLEN / HGETALL / HKEYS / HVALS / HMGET")

    h = "stress_hash"
    cmd(sock, "del", h)

    r.check("hset a b (2 new) → 2", cmd(sock, "hset", h, "a", "1", "b", "2"), 2)
    r.check("hset a update → 0",    cmd(sock, "hset", h, "a", "9"), 0)
    r.check("hget a → 9",          cmd(sock, "hget", h, "a"), "9")
    r.check_none("hget missing → nil", cmd(sock, "hget", h, "zzz"))
    r.check("hlen → 2",            cmd(sock, "hlen", h), 2)
    r.check("hexists a → 1",       cmd(sock, "hexists", h, "a"), 1)
    r.check("hexists zzz → 0",     cmd(sock, "hexists", h, "zzz"), 0)

    # HMGET: value or nil per field
    r.check("hmget a b zzz", cmd(sock, "hmget", h, "a", "b", "zzz"), ["9", "2", None])

    # HKEYS / HVALS (order not guaranteed → compare as sets)
    keys = cmd(sock, "hkeys", h)
    r.check("hkeys = {a,b}", set(keys), {"a", "b"})
    vals = cmd(sock, "hvals", h)
    r.check("hvals = {9,2}", set(vals), {"9", "2"})

    # HGETALL → flat [field,value,...]; rebuild a dict to compare
    flat = cmd(sock, "hgetall", h)
    d = {flat[i]: flat[i + 1] for i in range(0, len(flat), 2)}
    r.check("hgetall = {a:9,b:2}", d, {"a": "9", "b": "2"})

    # HDEL + empty-hash deletes the key
    r.check("hdel a → 1",   cmd(sock, "hdel", h, "a"), 1)
    r.check("hdel a again → 0", cmd(sock, "hdel", h, "a"), 0)
    r.check("hdel b → 1",   cmd(sock, "hdel", h, "b"), 1)
    r.check("type after empty → none", cmd(sock, "type", h), "none")  # key gone

    # missing-key behavior
    cmd(sock, "del", h)
    r.check("hlen missing → 0",    cmd(sock, "hlen", h), 0)
    r.check_none("hget missing → nil", cmd(sock, "hget", h, "a"))
    r.check("hexists missing → 0", cmd(sock, "hexists", h, "a"), 0)
    r.check("hgetall missing → []", cmd(sock, "hgetall", h), [])
    r.check("hmget missing → [nil,nil]", cmd(sock, "hmget", h, "a", "b"), [None, None])

    # wrong-type
    cmd(sock, "del", "hstr"); cmd(sock, "set", "hstr", "x")
    r.expect_error("hget on string → WRONGTYPE", sock, "hget", "hstr", "f")
    r.expect_error("hset on string → WRONGTYPE", sock, "hset", "hstr", "f", "v")
    cmd(sock, "del", "hstr")


def test_scan_command(r: TestRunner, sock: socket.socket):
    r.section("SCAN (cursor iteration + MATCH)")

    # seed a known keyspace
    for k in ("user:1", "user:2", "user:3", "order:1", "order:2"):
        cmd(sock, "del", k)
        cmd(sock, "set", k, "v")

    def scan_all(*opts):
        """Drive a full SCAN loop and return the set of keys seen."""
        seen, cursor = set(), "0"
        for _ in range(10000):  # safety bound
            reply = cmd(sock, "scan", cursor, *opts)
            cursor, batch = reply[0], reply[1]
            for k in batch:
                seen.add(k)
            if cursor == "0":
                break
        return seen

    all_keys = scan_all()
    for k in ("user:1", "user:2", "user:3", "order:1", "order:2"):
        r.check_true(f"scan sees {k}", k in all_keys)

    users = scan_all("match", "user:*")
    r.check("scan match user:* → only users",
            {k for k in users if k.startswith("user:") or k.startswith("order:")},
            {"user:1", "user:2", "user:3"})
    r.check_true("scan match excludes orders",
                 not any(k.startswith("order:") for k in users))

    one = scan_all("match", "order:?")  # ? = exactly one char
    r.check("scan match order:? → both orders",
            {k for k in one if k.startswith("order:")}, {"order:1", "order:2"})

    for k in ("user:1", "user:2", "user:3", "order:1", "order:2"):
        cmd(sock, "del", k)


def test_extended_generic_commands(r: TestRunner, sock: socket.socket):
    r.section("Generic: DBSIZE / RANDOMKEY / RENAME / RENAMENX / TOUCH")

    # flush to get a clean, known DB state
    cmd(sock, "flushall")
    r.check("dbsize empty DB → 0", cmd(sock, "dbsize"), 0)
    r.check_none("randomkey empty DB → nil", cmd(sock, "randomkey"))

    cmd(sock, "set", "eg1", "a")
    cmd(sock, "set", "eg2", "b")
    cmd(sock, "set", "eg3", "c")
    r.check("dbsize after 3 sets → 3", cmd(sock, "dbsize"), 3)

    # RANDOMKEY — can't check exact value, but must be a live key
    rk = cmd(sock, "randomkey")
    r.check_true("randomkey returns string", isinstance(rk, str))
    r.check_true("randomkey is a real key", cmd(sock, "get", rk) is not None)

    # RENAME
    r.check("rename eg1 → eg1new", cmd(sock, "rename", "eg1", "eg1new"), "OK")
    r.check("get eg1new → a",       cmd(sock, "get", "eg1new"), "a")
    r.check_none("eg1 gone after rename", cmd(sock, "get", "eg1"))
    r.expect_error("rename missing → error", sock, "rename", "nope", "x")

    # RENAME preserves TTL
    cmd(sock, "set", "ttlsrc", "v")
    cmd(sock, "expire", "ttlsrc", "100")
    cmd(sock, "rename", "ttlsrc", "ttldst")
    ttl = cmd(sock, "ttl", "ttldst")
    r.check_true("rename preserves TTL", isinstance(ttl, int) and ttl > 0)
    cmd(sock, "del", "ttldst")

    # RENAMENX
    cmd(sock, "set", "nx_src", "hello")
    cmd(sock, "set", "nx_dst", "world")
    r.check("renamenx existing dst → 0", cmd(sock, "renamenx", "nx_src", "nx_dst"), 0)
    r.check("nx_src still alive",        cmd(sock, "get", "nx_src"), "hello")
    r.check("renamenx free dst → 1",     cmd(sock, "renamenx", "nx_src", "nx_new"), 1)
    r.check("nx_new has value",          cmd(sock, "get", "nx_new"), "hello")
    r.check_none("nx_src gone",          cmd(sock, "get", "nx_src"))
    for k in ("nx_dst", "nx_new"): cmd(sock, "del", k)

    # TOUCH
    cmd(sock, "set", "tk1", "a")
    cmd(sock, "set", "tk2", "b")
    r.check("touch 2 existing → 2",          cmd(sock, "touch", "tk1", "tk2"), 2)
    r.check("touch 1 existing 1 missing → 1", cmd(sock, "touch", "tk1", "nope"), 1)
    r.check("touch all missing → 0",          cmd(sock, "touch", "nope1", "nope2"), 0)
    for k in ("tk1", "tk2"): cmd(sock, "del", k)

    r.section("Generic: EXPIREAT / PEXPIREAT")

    now_s  = int(time.time())
    now_ms = int(time.time() * 1000)

    # EXPIREAT future timestamp
    cmd(sock, "set", "eat1", "v")
    r.check("expireat future → 1", cmd(sock, "expireat", "eat1", str(now_s + 120)), 1)
    ttl = cmd(sock, "ttl", "eat1")
    r.check_true("ttl after expireat in (0,120]",
                 isinstance(ttl, int) and 0 < ttl <= 120)
    cmd(sock, "del", "eat1")

    # EXPIREAT past timestamp deletes key immediately
    cmd(sock, "set", "eat2", "v")
    r.check("expireat past → 1", cmd(sock, "expireat", "eat2", "1"), 1)
    r.check("key gone after past expireat", cmd(sock, "exists", "eat2"), 0)

    # EXPIREAT missing key → 0
    r.check("expireat missing → 0",
            cmd(sock, "expireat", "nope", str(now_s + 60)), 0)

    # PEXPIREAT future timestamp (milliseconds)
    cmd(sock, "set", "peat1", "v")
    r.check("pexpireat future → 1",
            cmd(sock, "pexpireat", "peat1", str(now_ms + 60000)), 1)
    pttl = cmd(sock, "pttl", "peat1")
    r.check_true("pttl after pexpireat in (0,60000]",
                 isinstance(pttl, int) and 0 < pttl <= 60000)
    cmd(sock, "del", "peat1")

    # PEXPIREAT past ms → deletes
    cmd(sock, "set", "peat2", "v")
    r.check("pexpireat past → 1", cmd(sock, "pexpireat", "peat2", "1"), 1)
    r.check("key gone after past pexpireat", cmd(sock, "exists", "peat2"), 0)

    r.section("Generic: FLUSHALL")

    for i in range(5):
        cmd(sock, "set", f"fg{i}", "x")
    r.check_true("dbsize > 0 before flush", cmd(sock, "dbsize") > 0)
    r.check("flushall → OK",              cmd(sock, "flushall"), "OK")
    r.check("dbsize 0 after flush",       cmd(sock, "dbsize"), 0)
    r.check_none("randomkey after flush → nil", cmd(sock, "randomkey"))


def test_extended_hash_commands(r: TestRunner, sock: socket.socket):
    r.section("Hashes extended: HSETNX / HINCRBY / HSTRLEN / HSCAN")

    h = "stress_hash_ext"
    cmd(sock, "del", h)

    # HSETNX
    r.check("hsetnx new field → 1",    cmd(sock, "hsetnx", h, "score", "10"), 1)
    r.check("hsetnx existing → 0",     cmd(sock, "hsetnx", h, "score", "99"), 0)
    r.check("score unchanged after nx", cmd(sock, "hget", h, "score"), "10")

    # HINCRBY
    r.check("hincrby score +5 → 15",  cmd(sock, "hincrby", h, "score", "5"), 15)
    r.check("hincrby score -3 → 12",  cmd(sock, "hincrby", h, "score", "-3"), 12)
    r.check("hincrby new field → 7",   cmd(sock, "hincrby", h, "counter", "7"), 7)
    # non-integer increment
    r.expect_error("hincrby non-int increment → error",
                   sock, "hincrby", h, "counter", "five")
    # non-integer stored value
    cmd(sock, "hset", h, "name", "alice")
    r.expect_error("hincrby on string value → error",
                   sock, "hincrby", h, "name", "1")

    # HSTRLEN
    cmd(sock, "hset", h, "greeting", "hello")
    r.check("hstrlen greeting → 5",    cmd(sock, "hstrlen", h, "greeting"), 5)
    r.check("hstrlen missing field → 0", cmd(sock, "hstrlen", h, "nope"), 0)
    r.check("hstrlen missing key → 0",  cmd(sock, "hstrlen", "nohash", "f"), 0)

    # HSCAN
    cmd(sock, "del", h)
    cmd(sock, "hset", h,
        "field1", "v1", "field2", "v2", "field3", "v3", "other", "vx")

    def hscan_all(key, *opts):
        """Drive a full HSCAN loop; return dict of field→value."""
        seen, cursor = {}, "0"
        for _ in range(10000):
            reply  = cmd(sock, "hscan", key, cursor, *opts)
            cursor = reply[0]
            items  = reply[1]
            for i in range(0, len(items), 2):
                seen[items[i]] = items[i + 1]
            if cursor == "0":
                break
        return seen

    all_fields = hscan_all(h)
    r.check("hscan sees all 4 fields",   len(all_fields), 4)
    r.check("hscan field1 value",        all_fields.get("field1"), "v1")

    filtered = hscan_all(h, "match", "field*")
    r.check("hscan match field* → 3",    len(filtered), 3)
    r.check_true("hscan match excludes other", "other" not in filtered)

    # missing key → cursor "0", empty array
    reply = cmd(sock, "hscan", "nohash", "0")
    r.check("hscan missing key cursor → 0", reply[0], "0")
    r.check("hscan missing key array → []", reply[1], [])

    # wrong type
    cmd(sock, "del", "hstr2"); cmd(sock, "set", "hstr2", "x")
    r.expect_error("hscan on string → WRONGTYPE", sock, "hscan", "hstr2", "0")
    cmd(sock, "del", "hstr2")

    cmd(sock, "del", h)


def test_unlink_command(r: TestRunner, sock: socket.socket):
    r.section("UNLINK Command (async delete)")

    cmd(sock, "del", "unlinktest")
    r.check("unlink missing → 0", cmd(sock, "unlink", "unlinktest"), 0)

    cmd(sock, "set", "unlinktest", "hello")
    r.check("unlink string → 1", cmd(sock, "unlink", "unlinktest"), 1)
    r.check_none("string gone", cmd(sock, "get", "unlinktest"))

    # small zset — synchronous path
    small = "small_async_zset"
    cmd(sock, "del", small)
    for i in range(10):
        cmd(sock, "zadd", small, str(float(i)), f"m{i}")
    r.check("unlink small zset → 1", cmd(sock, "unlink", small), 1)
    r.check_none("small zset gone", cmd(sock, "zscore", small, "m0"))

    # large zset — thread pool path (>1000 entries)
    large = "large_async_zset"
    cmd(sock, "del", large)
    print(f"  {YELLOW}ℹ{RESET}  inserting {ZSET_LARGE_SIZE} entries...")
    for i in range(ZSET_LARGE_SIZE):
        cmd(sock, "zadd", large, str(float(i)), f"member{i}")

    r.check_approx("large zset created",
                   cmd(sock, "zscore", large, "member0"), 0.0)

    print(f"  {YELLOW}ℹ{RESET}  sending unlink (thread pool path)...")
    t0      = time.time()
    result  = cmd(sock, "unlink", large)
    elapsed = time.time() - t0
    r.check("unlink large → 1", result, 1)
    r.check_true("unlink fast (<100ms)", elapsed < 0.1)
    r.check_none("large zset immediately gone",
                 cmd(sock, "zscore", large, "member0"))
    print(f"  {YELLOW}ℹ{RESET}  returned in {elapsed*1000:.1f}ms")
    time.sleep(0.5)


def test_set_commands(r: TestRunner, sock: socket.socket):
    r.section("Sets: SADD / SREM / SISMEMBER / SMISMEMBER / SCARD / SMEMBERS")

    for k in ["ts1", "ts2", "ts3", "tsdest", "tsmv_src", "tsmv_dst", "ts_str"]:
        cmd(sock, "del", k)
    cmd(sock, "set", "ts_str", "hello")

    # SADD
    r.check("sadd 3 new → 3",       cmd(sock, "sadd", "ts1", "a", "b", "c"), 3)
    r.check("sadd 1 new 1 dup → 1", cmd(sock, "sadd", "ts1", "c", "d"), 1)
    r.check("scard after sadd → 4", cmd(sock, "scard", "ts1"), 4)
    r.check("type ts1 → set",       cmd(sock, "type", "ts1"), "set")
    r.expect_error("sadd on string → WRONGTYPE", sock, "sadd", "ts_str", "x")

    # SISMEMBER / SMISMEMBER
    r.check("sismember existing → 1",    cmd(sock, "sismember", "ts1", "a"), 1)
    r.check("sismember missing → 0",     cmd(sock, "sismember", "ts1", "z"), 0)
    r.check("sismember missing key → 0", cmd(sock, "sismember", "nosuch", "a"), 0)
    r.check("smismember a d z",          cmd(sock, "smismember", "ts1", "a", "d", "z"), [1, 1, 0])
    r.check("smismember missing key",    cmd(sock, "smismember", "nosuch", "a", "b"), [0, 0])

    # SCARD / SMEMBERS
    r.check("scard → 4",         cmd(sock, "scard", "ts1"), 4)
    r.check("scard missing → 0", cmd(sock, "scard", "nosuch"), 0)
    members = cmd(sock, "smembers", "ts1")
    r.check_true("smembers returns 4 items", isinstance(members, list) and len(members) == 4)
    r.check_true("smembers has a",           members is not None and "a" in members)
    r.check_true("smembers has d",           members is not None and "d" in members)
    r.check("smembers missing → []",         cmd(sock, "smembers", "nosuch"), [])

    # SREM
    r.check("srem existing → 1",     cmd(sock, "srem", "ts1", "a"), 1)
    r.check("srem same again → 0",   cmd(sock, "srem", "ts1", "a"), 0)
    r.check("scard after srem → 3",  cmd(sock, "scard", "ts1"), 3)
    r.check("srem multi: b c → 2",   cmd(sock, "srem", "ts1", "b", "c"), 2)
    r.check("scard → 1",             cmd(sock, "scard", "ts1"), 1)
    r.check("srem missing key → 0",  cmd(sock, "srem", "nosuch", "a"), 0)
    r.expect_error("srem on string → WRONGTYPE", sock, "srem", "ts_str", "x")

    # restore ts1 for next sections
    cmd(sock, "del", "ts1")
    cmd(sock, "sadd", "ts1", "a", "b", "c", "d", "e")

    r.section("Sets: SPOP / SRANDMEMBER")

    # SPOP single
    before = cmd(sock, "scard", "ts1")
    popped = cmd(sock, "spop", "ts1")
    after  = cmd(sock, "scard", "ts1")
    r.check_true("spop returns string",       isinstance(popped, str))
    r.check_true("spop reduces card by 1",    after == before - 1)
    r.check("spop missing → nil",             cmd(sock, "spop", "nosuch"), None)

    # SPOP with count
    cmd(sock, "del", "ts1")
    cmd(sock, "sadd", "ts1", "a", "b", "c", "d", "e")
    popped2 = cmd(sock, "spop", "ts1", "3")
    r.check_true("spop count=3 list of 3",   isinstance(popped2, list) and len(popped2) == 3)
    r.check_true("spop count=3 distinct",    len(set(popped2)) == 3)
    r.check("scard after spop 3 → 2",        cmd(sock, "scard", "ts1"), 2)

    # SRANDMEMBER single (no removal)
    cmd(sock, "del", "ts1")
    cmd(sock, "sadd", "ts1", "a", "b", "c", "d", "e")
    rnd = cmd(sock, "srandmember", "ts1")
    r.check_true("srandmember returns string",  isinstance(rnd, str))
    r.check("card unchanged after srandmember", cmd(sock, "scard", "ts1"), 5)

    # SRANDMEMBER positive count (distinct)
    rnd3 = cmd(sock, "srandmember", "ts1", "3")
    r.check_true("srandmember count=3 list",    isinstance(rnd3, list) and len(rnd3) == 3)
    r.check_true("srandmember count=3 distinct", len(set(rnd3)) == 3)

    # SRANDMEMBER negative count (with replacement)
    rnd_neg = cmd(sock, "srandmember", "ts1", "-5")
    r.check_true("srandmember -5 returns 5",    isinstance(rnd_neg, list) and len(rnd_neg) == 5)

    # SRANDMEMBER count > cardinality → all distinct members
    rnd_big = cmd(sock, "srandmember", "ts1", "100")
    r.check_true("srandmember count>size → all", isinstance(rnd_big, list) and len(rnd_big) == 5)

    r.check("srandmember missing → nil", cmd(sock, "srandmember", "nosuch"), None)

    r.section("Sets: SSCAN")

    def sscan_all(key: str, *opts) -> list:
        seen, cursor = [], "0"
        for _ in range(10000):
            reply  = cmd(sock, "sscan", key, cursor, *opts)
            cursor = reply[0]
            seen.extend(reply[1])
            if cursor == "0":
                break
        return seen

    cmd(sock, "del", "ts1")
    cmd(sock, "sadd", "ts1", "apple", "apricot", "banana", "cherry")

    all_m = sscan_all("ts1")
    r.check_true("sscan sees all 4",      len(all_m) == 4)
    r.check_true("sscan has apple",       "apple" in all_m)
    r.check_true("sscan has cherry",      "cherry" in all_m)

    ap_m = sscan_all("ts1", "match", "a*")
    r.check_true("sscan match a* → 2",    len(ap_m) == 2)
    r.check_true("sscan excludes banana", "banana" not in ap_m)

    miss = cmd(sock, "sscan", "nosuch", "0")
    r.check("sscan missing key cursor → 0", miss[0], "0")
    r.check("sscan missing key array → []", miss[1], [])
    r.expect_error("sscan on string → WRONGTYPE", sock, "sscan", "ts_str", "0")

    r.section("Sets: SINTER / SUNION / SDIFF")

    cmd(sock, "del", "ts1")
    cmd(sock, "del", "ts2")
    cmd(sock, "del", "ts3")
    cmd(sock, "sadd", "ts1", "a", "b", "c", "d")
    cmd(sock, "sadd", "ts2", "b", "c", "e")
    cmd(sock, "sadd", "ts3", "c", "f")

    inter = cmd(sock, "sinter", "ts1", "ts2", "ts3")
    r.check_true("sinter s1∩s2∩s3 = {c}",      sorted(inter) == ["c"])

    inter2 = cmd(sock, "sinter", "ts1", "ts2")
    r.check_true("sinter s1∩s2 = {b,c}",        sorted(inter2) == ["b", "c"])

    union = cmd(sock, "sunion", "ts1", "ts2", "ts3")
    r.check_true("sunion = {a,b,c,d,e,f}",      sorted(union) == ["a", "b", "c", "d", "e", "f"])

    diff = cmd(sock, "sdiff", "ts1", "ts2")
    r.check_true("sdiff s1-s2 = {a,d}",         sorted(diff) == ["a", "d"])

    diff3 = cmd(sock, "sdiff", "ts1", "ts2", "ts3")
    r.check_true("sdiff s1-s2-s3 = {a,d}",      sorted(diff3) == ["a", "d"])

    r.expect_error("sinter wrong type → WRONGTYPE", sock, "sinter", "ts1", "ts_str")

    r.section("Sets: SINTERSTORE / SUNIONSTORE / SDIFFSTORE")

    r.check("sinterstore tsdest ts1 ts2 → 2",
            cmd(sock, "sinterstore", "tsdest", "ts1", "ts2"), 2)
    r.check_true("sinterstore result = {b,c}",
                 sorted(cmd(sock, "smembers", "tsdest")) == ["b", "c"])

    r.check("sunionstore tsdest ts1 ts2 → 5",
            cmd(sock, "sunionstore", "tsdest", "ts1", "ts2"), 5)
    r.check_true("sunionstore result = {a,b,c,d,e}",
                 sorted(cmd(sock, "smembers", "tsdest")) == ["a", "b", "c", "d", "e"])

    r.check("sdiffstore tsdest ts1 ts2 → 2",
            cmd(sock, "sdiffstore", "tsdest", "ts1", "ts2"), 2)
    r.check_true("sdiffstore result = {a,d}",
                 sorted(cmd(sock, "smembers", "tsdest")) == ["a", "d"])

    # dest == source (compute-then-store)
    r.check("sinterstore ts1←ts1∩ts2 → 2",
            cmd(sock, "sinterstore", "ts1", "ts1", "ts2"), 2)
    r.check_true("ts1 is now {b,c}",
                 sorted(cmd(sock, "smembers", "ts1")) == ["b", "c"])

    r.section("Sets: SMOVE")

    cmd(sock, "del", "tsmv_src")
    cmd(sock, "del", "tsmv_dst")
    cmd(sock, "sadd", "tsmv_src", "x", "y", "z")
    cmd(sock, "sadd", "tsmv_dst", "p", "q")

    r.check("smove existing → 1",
            cmd(sock, "smove", "tsmv_src", "tsmv_dst", "x"), 1)
    r.check("src no longer has x",     cmd(sock, "sismember", "tsmv_src", "x"), 0)
    r.check("dst now has x",           cmd(sock, "sismember", "tsmv_dst", "x"), 1)
    r.check("smove non-existent → 0",  cmd(sock, "smove", "tsmv_src", "tsmv_dst", "nope"), 0)
    r.check("smove missing src → 0",   cmd(sock, "smove", "nosuch", "tsmv_dst", "y"), 0)

    # move when dst already has the member (y added to dst; move y from src→dst)
    cmd(sock, "sadd", "tsmv_dst", "y")
    r.check("smove already-in-dst → 1",
            cmd(sock, "smove", "tsmv_src", "tsmv_dst", "y"), 1)
    r.check("src size → 1 (just z)",   cmd(sock, "scard", "tsmv_src"), 1)

    for k in ["ts1", "ts2", "ts3", "tsdest", "tsmv_src", "tsmv_dst", "ts_str"]:
        cmd(sock, "del", k)


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
              "used_memory:", "maxmemory:", "maxmemory_policy:",
              "evicted_keys:", "total_commands:", "keys_total:",
              "keys_with_ttl:", "aof_enabled:", "aof_current_size:",
              "aof_last_write_status:"]
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
    s = open_socket(host, port)
    try:
        send_request(s, "auth", "definitely_wrong_password")
        recv_response(s)
        r.check("wrong password → error", False, True)
    except RespError:
        r.check("wrong password → error", True, True)
    finally:
        s.close()

    # unauthenticated command should fail
    s = open_socket(host, port)
    try:
        send_request(s, "get", "anything")
        recv_response(s)
        r.check("unauthenticated → error", False, True)
    except RespError:
        r.check("unauthenticated → NOAUTH error", True, True)
    finally:
        s.close()

    # correct password then a real command
    s = open_socket(host, port)
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


def test_acl_commands(r: TestRunner, sock: socket.socket, host: str, port: int):
    r.section("ACL: users, auth, key patterns")

    username = "stress_acl_user"
    password = "stress_acl_pass"
    restricted = None

    # Cleanup first so reruns are deterministic.
    try:
        cmd(sock, "acl", "deluser", username)
    except RespError:
        pass

    r.check("acl whoami -> default", cmd(sock, "acl", "whoami"), "default")

    users = cmd(sock, "acl", "users")
    r.check_type("acl users -> array", users, list)
    if isinstance(users, list):
        r.check_true("acl users contains default", "default" in users)

    listing = cmd(sock, "acl", "list")
    r.check_type("acl list -> array", listing, list)
    if isinstance(listing, list):
        r.check_true("acl list includes default",
                     any(isinstance(row, str) and row.startswith("user default")
                         for row in listing))

    default_user = cmd(sock, "acl", "getuser", "default")
    r.check_type("acl getuser default -> array", default_user, list)
    if isinstance(default_user, list):
        r.check_true("acl getuser exposes flags", "flags" in default_user)
        r.check_true("acl getuser exposes commands", "commands" in default_user)
        r.check_true("acl getuser exposes keys", "keys" in default_user)

    token = cmd(sock, "acl", "genpass", "64")
    r.check_true("acl genpass 64 -> 16 hex chars",
                 isinstance(token, str)
                 and len(token) == 16
                 and all(c in "0123456789abcdef" for c in token))
    r.expect_error("acl genpass invalid bits -> error",
                   sock, "acl", "genpass", "0")

    r.check("acl setuser restricted -> OK",
            cmd(sock, "acl", "setuser", username, "on", f">{password}",
                "~acl:*", "+get", "+set", "+del"),
            "OK")

    try:
        restricted = open_socket(host, port)
        send_request(restricted, "auth", username, password)
        r.check("auth user password -> OK", recv_response(restricted), "OK")

        r.check("restricted SET allowed key -> OK",
                cmd(restricted, "set", "acl:allowed", "1"), "OK")
        r.check("restricted GET allowed key -> 1",
                cmd(restricted, "get", "acl:allowed"), "1")
        r.expect_error("restricted SET blocked by key pattern",
                       restricted, "set", "outside:acl", "1")
        r.expect_error("restricted ACL command denied",
                       restricted, "acl", "whoami")
    finally:
        if restricted is not None:
            restricted.close()

    r.check("acl deluser restricted -> 1",
            cmd(sock, "acl", "deluser", username), 1)
    r.check("cleanup acl:allowed -> 1", cmd(sock, "del", "acl:allowed"), 1)

    r.expect_error("acl setuser bad modifier -> error",
                   sock, "acl", "setuser", "stress_acl_bad", "not-a-rule")
    try:
        cmd(sock, "acl", "deluser", "stress_acl_bad")
    except RespError:
        pass


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
        self.by_op     = {}
        self.lock      = threading.Lock()

    def record(self, op_name: str, ms: float):
        with self.lock:
            self.ops += 1
            self.latencies.append(ms)
            row = self.by_op.setdefault(op_name, {
                "ops": 0,
                "errors": 0,
                "latencies": [],
            })
            row["ops"] += 1
            row["latencies"].append(ms)

    def record_error(self, op_name: str = "unknown"):
        with self.lock:
            self.errors += 1
            row = self.by_op.setdefault(op_name, {
                "ops": 0,
                "errors": 0,
                "latencies": [],
            })
            row["errors"] += 1

    def report(self, top_n: int = 10):
        with self.lock:
            ops = self.ops
            errors = self.errors
            latencies = list(self.latencies)
            by_op = {
                k: {
                    "ops": v["ops"],
                    "errors": v["errors"],
                    "latencies": list(v["latencies"]),
                }
                for k, v in self.by_op.items()
            }

        if not latencies:
            print("  No operations recorded.")
            print(f"  Total ops:   {ops}")
            print(f"  Errors:      {errors}")
            if by_op:
                print("  Error breakdown:")
                for name, row in sorted(by_op.items()):
                    if row["errors"]:
                        print(f"    {name:<18} {row['errors']:>4} errors")
            return
        srt = sorted(latencies)
        avg = sum(srt) / len(srt)
        print(f"  Total ops:   {ops}")
        print(f"  Errors:      {errors}")
        print(f"  Latency avg: {avg:.2f}ms")
        print(f"  Latency min: {srt[0]:.2f}ms")
        print(f"  Latency max: {srt[-1]:.2f}ms")
        print(f"  Latency p50: {_percentile(srt, 0.50):.2f}ms")
        print(f"  Latency p95: {_percentile(srt, 0.95):.2f}ms")
        print(f"  Latency p99: {_percentile(srt, 0.99):.2f}ms")
        if errors == 0:
            print(f"  {GREEN}No errors!{RESET}")
        else:
            print(f"  {RED}{errors} errors!{RESET}")

        by_count = sorted(by_op.items(),
                          key=lambda item: item[1]["ops"],
                          reverse=True)[:top_n]
        print("  Operation mix:")
        for name, row in by_count:
            print(f"    {name:<18} {row['ops']:>6} ok  {row['errors']:>4} errors")

        by_slow = sorted(
            [(name, row) for name, row in by_op.items() if row["latencies"]],
            key=lambda item: (
                sum(item[1]["latencies"]) / len(item[1]["latencies"])
            ),
            reverse=True,
        )[:top_n]
        print("  Slowest operations by average latency:")
        for name, row in by_slow:
            vals = row["latencies"]
            avg_ms = sum(vals) / len(vals)
            print(f"    {name:<18} {avg_ms:>8.2f}ms avg over {len(vals)} ops")


def random_key(prefix: str = "stress") -> str:
    return f"{prefix}_{random.randint(0, 200)}"


def random_string(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


STRESS_OP_NAMES = (
    "set", "get", "del", "zadd", "zscore", "zrank", "zquery", "zrevquery",
    "ttl_triplet", "keys", "info", "rpush", "lpush", "lrange",
    "list_pop_trim", "hset", "hget", "hgetall", "keyspace_scan",
    "sadd", "sismember", "smembers", "srem", "incr", "setnx", "getdel",
    "mset", "mget", "append", "strlen", "zpopmin", "memory_usage",
    "object_encoding", "config_get", "ping", "sscan", "hscan",
    "srandmember", "getex_px",
)


def stress_worker(host: str, port: int, ops: int,
                  stats: StressStats, wid: int):
    try:
        sock = make_conn(host, port)
    except Exception as e:
        print(f"  {RED}Worker {wid} connect failed: {e}{RESET}")
        stats.record_error("connect")
        return

    zset = f"stress_zset_{wid}"
    lst  = f"stress_list_{wid}"
    hsh  = f"stress_hash_{wid}"
    sset = f"stress_set_{wid}"
    ctr  = f"stress_ctr_{wid}"
    try:
        cmd(sock, "del", zset)
        cmd(sock, "del", lst)
        cmd(sock, "del", hsh)
        cmd(sock, "del", sset)
        cmd(sock, "del", ctr)
        for i in range(10):
            cmd(sock, "zadd", zset, str(float(i)), f"m{i}")
        cmd(sock, "rpush", lst, "a", "b", "c", "d", "e")
        cmd(sock, "hset", hsh, "f0", "0", "f1", "1")
        cmd(sock, "sadd", sset, "x", "y", "z", "w")
        cmd(sock, "set", ctr, "0")
    except Exception:
        stats.record_error("setup")

    for _ in range(ops):
        op = random.randrange(len(STRESS_OP_NAMES))
        op_name = STRESS_OP_NAMES[op]
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
            elif op == 15:
                cmd(sock, "hset", hsh, f"f{random.randint(0, 20)}", random_string(5))
            elif op == 16:
                cmd(sock, "hget", hsh, f"f{random.randint(0, 20)}")
            elif op == 17:
                cmd(sock, "hgetall", hsh)
            elif op == 18:
                k = random_key()
                cmd(sock, "set", k, "v")
                cmd(sock, "exists", k)
                cmd(sock, "type", k)
                cmd(sock, "scan", "0", "count", "20")
            elif op == 19:
                cmd(sock, "sadd", sset, random_string(4))
            elif op == 20:
                cmd(sock, "sismember", sset, random_string(4))
            elif op == 21:
                cmd(sock, "smembers", sset)
            elif op == 22:
                cmd(sock, "srem", sset, random_string(4))
            elif op == 23:
                cmd(sock, "incr", ctr)
            elif op == 24:
                cmd(sock, "setnx", random_key(), random_string(4))
            elif op == 25:
                cmd(sock, "getdel", random_key())
            elif op == 26:
                k1, k2 = random_key(), random_key()
                cmd(sock, "mset", k1, random_string(4), k2, random_string(4))
            elif op == 27:
                cmd(sock, "mget", random_key(), random_key(), random_key())
            elif op == 28:
                cmd(sock, "append", random_key(), random_string(4))
            elif op == 29:
                cmd(sock, "strlen", random_key())
            elif op == 30:
                cmd(sock, "zpopmin", zset, str(random.randint(1, 3)))
            elif op == 31:
                cmd(sock, "memory", "usage", random_key())
            elif op == 32:
                k = random_key()
                cmd(sock, "set", k, "v")
                cmd(sock, "object", "encoding", k)
            elif op == 33:
                cmd(sock, "config", "get", "maxmemory")
            elif op == 34:
                cmd(sock, "ping")
            elif op == 35:
                cmd(sock, "sscan", sset, "0", "count", "10")
            elif op == 36:
                cmd(sock, "hscan", hsh, "0", "count", "10")
            elif op == 37:
                cmd(sock, "srandmember", sset, "-3")
            elif op == 38:
                k = random_key()
                cmd(sock, "set", k, "v")
                cmd(sock, "getex", k, "PX", "5000")

            stats.record(op_name, (time.perf_counter() - t0) * 1000)
        except Exception as e:
            stats.record_error(op_name)
            print(f"  {RED}op={op_name} failed: {e!r}{RESET}")

    try:
        cmd(sock, "del", zset)
        cmd(sock, "del", lst)
        cmd(sock, "del", hsh)
        cmd(sock, "del", sset)
        cmd(sock, "del", ctr)
        sock.close()
    except Exception:
        pass


def run_stress_test(host: str, port: int, threads_count: int,
                    ops_per_thread: int, metrics_top: int) -> bool:
    print(f"\n{BOLD}{BLUE}── Stress Test {'─' * 40}{RESET}")
    print(f"  Threads:    {threads_count}")
    print(f"  Ops/thread: {ops_per_thread}")
    print(f"  Total ops:  {threads_count * ops_per_thread}")

    stats   = StressStats()
    threads = [
        threading.Thread(target=stress_worker,
                         args=(host, port, ops_per_thread, stats, i))
        for i in range(threads_count)
    ]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed    = time.time() - t0
    throughput = stats.ops / elapsed if elapsed > 0 else 0

    print(f"\n  Elapsed:    {elapsed:.2f}s")
    print(f"  Throughput: {throughput:.0f} ops/sec")
    stats.report(metrics_top)
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


def test_zset_extended(r: TestRunner, sock: socket.socket):
    r.section("Sorted Set: variadic ZADD / ZPOPMIN")

    zset = "zext"
    cmd(sock, "del", zset)

    # variadic ZADD: 3 score/member pairs in one command → 3 new members
    r.check("zadd variadic 3 pairs → 3",
            cmd(sock, "zadd", zset, "1", "a", "2", "b", "3", "c"), 3)
    # mix: 'a' exists (update → 0), 'd' new (→ 1) ⇒ returns 1 (only new counted)
    r.check("zadd variadic new+update → 1",
            cmd(sock, "zadd", zset, "1.5", "a", "4", "d"), 1)
    r.check_approx("zscore a updated → 1.5", cmd(sock, "zscore", zset, "a"), 1.5)

    # odd arg count (dangling score) → error
    r.expect_error("zadd odd args → error", sock, "zadd", zset, "9", "x", "8")
    # atomicity: a bad score must add nothing
    r.expect_error("zadd bad score → error", sock, "zadd", zset, "notnum", "z")
    r.check_none("zadd atomic: z not added", cmd(sock, "zscore", zset, "z"))

    # scores now: a=1.5, b=2, c=3, d=4
    res = cmd(sock, "zpopmin", zset)
    r.check_type("zpopmin → list", res, list)
    r.check("zpopmin min member → a", res[0] if isinstance(res, list) and res else None, "a")
    r.check_approx("zpopmin min score → 1.5",
                   res[1] if isinstance(res, list) and len(res) > 1 else None, 1.5)

    # zpopmin count=2 → b, c (4 items: member,score,member,score)
    res2 = cmd(sock, "zpopmin", zset, "2")
    r.check("zpopmin 2 → 4 items", len(res2) if isinstance(res2, list) else -1, 4)
    r.check("zpopmin 2 members → b,c",
            res2[0::2] if isinstance(res2, list) else None, ["b", "c"])

    # only 'd' left — popping it should drop the now-empty key
    cmd(sock, "zpopmin", zset)
    r.check("zset emptied → key dropped", cmd(sock, "exists", zset), 0)

    # zpopmin on a missing key → empty array
    r.check("zpopmin missing → []", cmd(sock, "zpopmin", "ghost_zset"), [])

    # wrong type
    cmd(sock, "del", "ztype")
    cmd(sock, "set", "ztype", "x")
    r.expect_error("zpopmin wrong type → error", sock, "zpopmin", "ztype")
    cmd(sock, "del", "ztype")


def test_ping_command(r: TestRunner, sock: socket.socket):
    r.section("PING (multibulk + inline)")

    r.check("ping → PONG", cmd(sock, "ping"), "PONG")
    r.check("ping msg → echo", cmd(sock, "ping", "hello"), "hello")
    r.check("mixed-case ping -> PONG", cmd(sock, "PiNg"), "PONG")
    r.expect_error("ping too many args -> error", sock, "ping", "a", "b")

    # inline protocol: a bare "PING\r\n" (not a RESP array)
    sock.sendall(b"PING\r\n")
    r.check("inline PING → PONG", recv_response(sock), "PONG")

    # inline with argument
    sock.sendall(b"PING inlinemsg\r\n")
    r.check("inline PING msg → echo", recv_response(sock), "inlinemsg")

    # inline parser tolerates LF-only input.
    sock.sendall(b"PING\n")
    r.check("inline LF-only PING -> PONG", recv_response(sock), "PONG")


def test_config_command(r: TestRunner, sock: socket.socket):
    r.section("CONFIG")

    r.check("config set maxmemory 0 -> OK",
            cmd(sock, "config", "set", "maxmemory", "0"), "OK")
    res = cmd(sock, "config", "get", "maxmemory")
    r.check_type("config get maxmemory -> array", res, list)
    r.check("config get maxmemory value", res, ["maxmemory", "0"])

    all_res = cmd(sock, "config", "get", "*")
    r.check_type("config get * -> array", all_res, list)
    if isinstance(all_res, list):
        r.check_true("config get * includes maxmemory", "maxmemory" in all_res)
        r.check_true("config get * includes maxmemory-policy",
                     "maxmemory-policy" in all_res)

    r.check("config get unknown -> []",
            cmd(sock, "config", "get", "does-not-exist"), [])

    r.check("config set maxmemory-policy allkeys-random -> OK",
            cmd(sock, "config", "set", "maxmemory-policy", "allkeys-random"),
            "OK")
    r.check("config get maxmemory-policy allkeys-random",
            cmd(sock, "config", "get", "maxmemory-policy"),
            ["maxmemory-policy", "allkeys-random"])
    r.check("config set maxmemory-policy noeviction -> OK",
            cmd(sock, "config", "set", "maxmemory-policy", "noeviction"),
            "OK")

    r.expect_error("config set maxmemory invalid -> error",
                   sock, "config", "set", "maxmemory", "not-bytes")
    r.expect_error("config set invalid policy -> error",
                   sock, "config", "set", "maxmemory-policy", "bogus")
    try:
        r.check("config set unknown parameter handled",
                cmd(sock, "config", "set", "unknown-setting", "1"), "OK")
    except RespError:
        r.check_true("config set unknown parameter rejected", True)
    r.check("config resetstat -> OK", cmd(sock, "config", "resetstat"), "OK")
    r.expect_error("config bad subcommand -> error", sock, "config", "frobnicate")

    _config_setget_probe(r, sock)


# [REG] V9.8.2 shipped k_config_table with the `appendonly` row's getter reading
# g_config.protected_mode instead of g_config.aof_enable. Since V9.8.1 routes
# CONFIG REWRITE through the getter, that is a persistence bug, not a display
# one: a rewrite writes `appendonly <protected-mode's value>`, so a server with
# protected-mode yes + appendonly no silently gains AOF, and the reverse silently
# loses it on the next restart. It passed every suite because myred.conf happens
# to set both to yes.
#
# A format->apply->format round-trip cannot catch this -- reading the wrong field
# is perfectly self-consistent. Only writing a value and reading it back does, so
# each probe below sets a value DISTINCT from the current one. Originals are
# restored afterwards.
CONFIG_PROBES = [
    ("maxmemory-samples", "7"),
    ("maxmemory-policy", "allkeys-random"),
    ("maxmemory", "12345678"),
    ("appendfsync", "always"),
    ("appendfilename", "probe-only.aof"),
    ("dbfilename", "probe-only.rdb"),
    ("auto-aof-rewrite-percentage", "77"),
    ("auto-aof-rewrite-min-size", "12345678"),
    ("notify-keyspace-events", "AKE"),
]


def _config_get1(sock: socket.socket, name: str):
    res = cmd(sock, "config", "get", name)
    return res[1] if isinstance(res, list) and len(res) == 2 else None


def _config_setget_probe(r: TestRunner, sock: socket.socket):
    """Every directive must read back exactly what was written to it."""
    for name, probe in CONFIG_PROBES:
        original = _config_get1(sock, name)
        if original is None:
            r.check_true(f"[REG] {name} is gettable", False, f"CONFIG GET {name} -> not a pair")
            continue
        try:
            cmd(sock, "config", "set", name, probe)
            r.check(f"[REG] {name} reads back what was set", _config_get1(sock, name), probe)
        finally:
            cmd(sock, "config", "set", name, original)

    # appendonly and protected-mode are probed as a pair, opposed, because the
    # bug that motivated this test is invisible whenever the two agree.
    orig_ao = _config_get1(sock, "appendonly")
    orig_pm = _config_get1(sock, "protected-mode")
    try:
        cmd(sock, "config", "set", "protected-mode", "yes")
        cmd(sock, "config", "set", "appendonly", "no")
        r.check("[REG] appendonly no while protected-mode yes",
                _config_get1(sock, "appendonly"), "no")
        cmd(sock, "config", "set", "protected-mode", "no")
        cmd(sock, "config", "set", "appendonly", "yes")
        r.check("[REG] appendonly yes while protected-mode no",
                _config_get1(sock, "appendonly"), "yes")
    finally:
        if orig_ao is not None: cmd(sock, "config", "set", "appendonly", orig_ao)
        if orig_pm is not None: cmd(sock, "config", "set", "protected-mode", orig_pm)


def test_bgrewriteaof_command(r: TestRunner, sock: socket.socket):
    r.section("BGREWRITEAOF")

    # returns a status string whether or not AOF is enabled; must not crash
    res = cmd(sock, "bgrewriteaof")
    r.check_type("bgrewriteaof → string", res, str)
    # server must stay responsive afterward
    r.check("server responsive after bgrewriteaof", cmd(sock, "ping"), "PONG")


# ═══════════════════════════════════════════════════════════════════════════════
#  MEMORY MANAGEMENT (v7): accounting, introspection, maxmemory eviction / OOM
# ═══════════════════════════════════════════════════════════════════════════════

def info_field(sock: socket.socket, name: str) -> Optional[str]:
    for ln in cmd(sock, "info").splitlines():
        if ln.startswith(name + ":"):
            return ln.split(":", 1)[1]
    return None


def used_memory(sock: socket.socket) -> int:
    v = info_field(sock, "used_memory") or info_field(sock, "used_memory_bytes")
    return int(v) if v is not None else -1


def evicted_keys(sock: socket.socket) -> int:
    v = info_field(sock, "evicted_keys")
    return int(v) if v is not None else 0


def set_result(sock: socket.socket, key: str, val: str) -> str:
    """SET returning 'OK' or 'OOM'; any other error propagates."""
    try:
        cmd(sock, "set", key, val)
        return "OK"
    except RespError as e:
        if "OOM" in str(e):
            return "OOM"
        raise


def test_memory_accounting(r: TestRunner, sock: socket.socket):
    r.section("Memory: accounting (used_memory)")
    cmd(sock, "flushall")
    r.check("empty DB → used_memory 0", used_memory(sock), 0)

    base = used_memory(sock)
    cmd(sock, "set", "mem:s", "x" * 5000)
    r.check_true("used_memory grows after SET", used_memory(sock) > base)
    cmd(sock, "del", "mem:s")
    r.check("used_memory back to baseline after DEL", used_memory(sock), base)

    # aggregate grow + drain (exercises the shrink-path reaccount)
    for i in range(200):
        cmd(sock, "rpush", "mem:L", f"e{i}")
    r.check_true("used_memory grows after RPUSH x200", used_memory(sock) > base)
    cmd(sock, "del", "mem:L")
    r.check("used_memory back to baseline after list DEL", used_memory(sock), base)

    # mixed load then FLUSHALL must return to EXACTLY 0 (leak / double-count detector)
    for i in range(100):
        cmd(sock, "set",  f"m:s:{i}", "v" * (i + 1))
        cmd(sock, "hset", f"m:h:{i}", "a", "1", "b", str(i))
        cmd(sock, "sadd", f"m:t:{i}", "x", "y", str(i))
        cmd(sock, "zadd", f"m:z:{i}", "1", "a", "2", "b")
    r.check_true("mixed load grew used_memory", used_memory(sock) > 0)
    cmd(sock, "flushall")
    r.check("FLUSHALL returns used_memory to 0", used_memory(sock), 0)


def test_memory_introspection(r: TestRunner, sock: socket.socket):
    r.section("Memory: MEMORY / OBJECT introspection")
    cmd(sock, "flushall")
    cmd(sock, "set",   "o:str",  "hello")
    cmd(sock, "set",   "o:int",  "12345")
    cmd(sock, "rpush", "o:list", "a", "b", "c")
    cmd(sock, "hset",  "o:hash", "f", "v")
    cmd(sock, "sadd",  "o:set",  "m1", "m2")
    cmd(sock, "zadd",  "o:zset", "1", "a")

    r.check_type("memory usage o:str → int", cmd(sock, "memory", "usage", "o:str"), int)
    r.check_none("memory usage missing → nil", cmd(sock, "memory", "usage", "nope"))
    r.check_type("memory usage ... samples 3 → int",
                 cmd(sock, "memory", "usage", "o:hash", "samples", "3"), int)
    # uppercase subcommand must work (regression for the tolower no-op bug)
    r.check_type("MEMORY USAGE (uppercase) → int", cmd(sock, "MEMORY", "USAGE", "o:str"), int)

    doctor = cmd(sock, "memory", "doctor")
    r.check_type("memory doctor → string", doctor, str)
    r.check_true("memory doctor reports no drift", "drift" not in doctor.lower())
    r.check_type("memory stats → array", cmd(sock, "memory", "stats"), list)

    r.check("object encoding o:str → raw",       cmd(sock, "object", "encoding", "o:str"),  "raw")
    r.check("object encoding o:int → int",       cmd(sock, "object", "encoding", "o:int"),  "int")
    r.check("object encoding o:list → deque",    cmd(sock, "object", "encoding", "o:list"), "deque")
    r.check("object encoding o:hash → hashtable",cmd(sock, "object", "encoding", "o:hash"), "hashtable")
    r.check("object encoding o:set → hashtable", cmd(sock, "object", "encoding", "o:set"),  "hashtable")
    r.check("object encoding o:zset → skiplist", cmd(sock, "object", "encoding", "o:zset"), "skiplist")
    r.check("OBJECT ENCODING (uppercase) works", cmd(sock, "OBJECT", "ENCODING", "o:str"), "raw")

    r.check("object refcount → 1", cmd(sock, "object", "refcount", "o:str"), 1)
    r.check_type("object idletime → int", cmd(sock, "object", "idletime", "o:str"), int)
    r.expect_error("object on missing key → error", sock, "object", "encoding", "nope")
    r.expect_error("object bad subcommand → error", sock, "object", "frobnicate", "o:str")
    cmd(sock, "flushall")


def test_maxmemory_eviction(r: TestRunner, sock: socket.socket):
    r.section("Memory: maxmemory eviction + OOM")
    CAP   = 512 * 1024          # 512 KB
    VAL   = "y" * 400
    BOUND = CAP * 2             # bounded vs runaway growth (a leaked OOM would blow past this)
    N     = 1500               # ~825 KB attempted -> well over the cap
    cmd(sock, "config", "set", "maxmemory", str(CAP))

    # noeviction: writes succeed until full, then OOM; memory bounded; nothing evicted
    cmd(sock, "flushall")
    cmd(sock, "config", "set", "maxmemory-policy", "noeviction")
    ev0 = evicted_keys(sock)
    oks = ooms = 0
    for i in range(N):
        rr = set_result(sock, f"ne:{i}", VAL)
        oks += (rr == "OK"); ooms += (rr == "OOM")
        if ooms >= 50:
            break
    r.check_true("noeviction: writes succeed then OOM", oks > 0 and ooms > 0)
    r.check_true("noeviction: used_memory bounded", used_memory(sock) <= BOUND)
    r.check("noeviction: nothing evicted", evicted_keys(sock), ev0)
    # a memory-FREEING command must still work while over the cap (no deadlock)
    r.check("FLUSHALL allowed over cap", cmd(sock, "flushall"), "OK")

    # allkeys-lru: never OOMs, memory bounded, evictions climb
    cmd(sock, "config", "set", "maxmemory-policy", "allkeys-lru")
    ev1 = evicted_keys(sock)
    oks = ooms = 0
    for i in range(N):
        rr = set_result(sock, f"lru:{i}", VAL)
        oks += (rr == "OK"); ooms += (rr == "OOM")
    r.check("allkeys-lru: no OOM", ooms, 0)
    r.check_true("allkeys-lru: used_memory bounded", used_memory(sock) <= BOUND)
    r.check_true("allkeys-lru: evicted_keys climbed", evicted_keys(sock) - ev1 > 0)

    # allkeys-random on keys with NO TTL (regression: it used to call the volatile sampler)
    cmd(sock, "flushall")
    cmd(sock, "config", "set", "maxmemory-policy", "allkeys-random")
    ev2 = evicted_keys(sock)
    ooms = 0
    for i in range(N):
        ooms += (set_result(sock, f"rnd:{i}", VAL) == "OOM")
    r.check("allkeys-random: no OOM on non-TTL keys", ooms, 0)
    r.check_true("allkeys-random: evicted_keys climbed", evicted_keys(sock) - ev2 > 0)

    # restore unlimited so later tests aren't capped
    cmd(sock, "config", "set", "maxmemory", "0")
    cmd(sock, "config", "set", "maxmemory-policy", "noeviction")
    cmd(sock, "flushall")


def test_eviction_incremental(r: TestRunner, sock: socket.socket):
    r.section("Memory: incremental eviction (EVICT_RUNNING semantics)")
    CAP = 512 * 1024
    VAL = "z" * 400
    cmd(sock, "flushall")
    cmd(sock, "config", "set", "maxmemory", "0")
    cmd(sock, "config", "set", "maxmemory-policy", "allkeys-random")
    for i in range(4000):                       # ~2 MB -> 4x over the cap
        cmd(sock, "set", f"ev:{i}", VAL)
    over = used_memory(sock)

    cmd(sock, "config", "set", "maxmemory", str(CAP))
    # the very next write must be admitted; pre-fix behavior OOM'd until
    # repeated writes had chipped the overshoot off 100 keys at a time
    r.check("write admitted during overshoot", set_result(sock, "ev:probe", "1"), "OK")

    # idle drain: no further writes — evict_tick alone must get under the cap
    deadline = time.time() + 5.0
    um = used_memory(sock)
    while um > CAP and time.time() < deadline:
        time.sleep(0.2)
        um = used_memory(sock)
    r.check_true(f"idle drain under cap ({over} -> {um} <= {CAP})", um <= CAP)

    cmd(sock, "config", "set", "maxmemory", "0")
    cmd(sock, "config", "set", "maxmemory-policy", "noeviction")
    cmd(sock, "flushall")


def test_memory_commands(r: TestRunner, sock: socket.socket):
    test_memory_accounting(r, sock)
    test_memory_introspection(r, sock)
    test_maxmemory_eviction(r, sock)
    test_eviction_incremental(r, sock)


# ═══════════════════════════════════════════════════════════════════════════════
#  V9.6.5 ADDITIONS — ECHO/inline protocol, FLUSHDB, SPOP/SRANDMEMBER edge
#  semantics, O(1) INFO keyspace stats, redis-benchmark speed baseline
# ═══════════════════════════════════════════════════════════════════════════════

def test_echo_and_inline(r: TestRunner, host: str, port: int):
    r.section("ECHO + inline protocol")
    sock = make_conn(host, port)
    try:
        r.check("echo roundtrip",       cmd(sock, "echo", "hello"), "hello")
        r.check("echo empty string",    cmd(sock, "echo", ""), "")
        marker = "m" * 20               # redis-cli --pipe uses a 20-byte marker
        r.check("echo 20-byte marker",  cmd(sock, "echo", marker), marker)
        r.check("echo whitespace-safe", cmd(sock, "echo", "a\tb c"), "a\tb c")
        r.expect_error("echo no args → arity error",  sock, "echo")
        r.expect_error("echo 2 args → arity error",   sock, "echo", "a", "b")

        # inline commands: newline-terminated text instead of RESP framing
        def raw_reply(payload: bytes):
            sock.sendall(payload)
            try:
                return recv_response(sock)
            except RespError as e:
                return f"ERR:{e}"

        r.check("inline ping → PONG",        raw_reply(b"ping\r\n"), "PONG")
        r.check("inline \\n-only tolerated", raw_reply(b"echo inlinearg\n"), "inlinearg")
        # a bare \r\n (redis-cli --pipe epilogue) must be ignored silently;
        # if the server answered it, this reply would be that answer, not PONG
        r.check("empty inline line ignored", raw_reply(b"\r\nping\r\n"), "PONG")
    finally:
        sock.close()


def test_flushdb_command(r: TestRunner, sock: socket.socket):
    r.section("FLUSHDB")
    cmd(sock, "mset", "fdb1", "a", "fdb2", "b")
    r.check_true("keys exist before flushdb", cmd(sock, "dbsize") >= 2)
    r.check("flushdb → OK",       cmd(sock, "flushdb"), "OK")
    r.check("dbsize → 0",         cmd(sock, "dbsize"), 0)
    r.check("flushed key gone",   cmd(sock, "get", "fdb1"), None)


def test_set_random_semantics(r: TestRunner, sock: socket.socket):
    r.section("Sets: SPOP/SRANDMEMBER edge semantics (hm_random paths)")
    cmd(sock, "del", "tsr")
    cmd(sock, "sadd", "tsr", "a", "b", "c", "d", "e")

    r.check("spop count=0 → []", cmd(sock, "spop", "tsr", "0"), [])
    r.expect_error("spop negative count → error", sock, "spop", "tsr", "-1")
    r.expect_error("spop non-int count → error",  sock, "spop", "tsr", "x")

    # count > cardinality pops everything and deletes the key
    popped = cmd(sock, "spop", "tsr", "100")
    r.check_true("spop count>size returns all 5",
                 isinstance(popped, list) and sorted(popped) == ["a", "b", "c", "d", "e"])
    r.check("emptied set key is deleted", cmd(sock, "exists", "tsr"), 0)

    cmd(sock, "sadd", "tsr", "a", "b", "c", "d", "e")
    r.check("srandmember count=0 → []", cmd(sock, "srandmember", "tsr", "0"), [])

    # distribution sanity: 200 single draws must reach every member
    seen = set()
    for _ in range(200):
        seen.add(cmd(sock, "srandmember", "tsr"))
    r.check_true("srandmember reaches all members", seen == {"a", "b", "c", "d", "e"})
    r.check("card unchanged by draws", cmd(sock, "scard", "tsr"), 5)

    # positive-count draws must never mutate (pop-and-reinsert must restore)
    distinct_ok = True
    for _ in range(50):
        got = cmd(sock, "srandmember", "tsr", "3")
        distinct_ok = distinct_ok and len(set(got)) == 3
    r.check_true("50 count-draws all distinct",    distinct_ok)
    r.check("card unchanged after count-draws",    cmd(sock, "scard", "tsr"), 5)
    r.check_true("membership intact after draws",
                 sorted(cmd(sock, "smembers", "tsr")) == ["a", "b", "c", "d", "e"])

    # single pops drain every member exactly once
    drained = sorted(cmd(sock, "spop", "tsr") for _ in range(5))
    r.check_true("5 single pops drain all distinct", drained == ["a", "b", "c", "d", "e"])
    r.check("spop on emptied key → nil", cmd(sock, "spop", "tsr"), None)
    cmd(sock, "del", "tsr")


def test_info_keyspace_stats(r: TestRunner, sock: socket.socket):
    r.section("INFO: O(1) keyspace stats (heap-backed keys_with_ttl)")
    cmd(sock, "flushall")

    def stats():
        return (int(info_field(sock, "keys_total") or -1),
                int(info_field(sock, "keys_with_ttl") or -1))

    r.check("empty db → (0,0)",             stats(), (0, 0))
    cmd(sock, "mset", "ik1", "v", "ik2", "v", "ik3", "v")
    r.check("3 keys, no ttl → (3,0)",       stats(), (3, 0))
    cmd(sock, "expire", "ik1", "100")
    cmd(sock, "expire", "ik2", "100")
    r.check("2 ttls set → (3,2)",           stats(), (3, 2))
    cmd(sock, "persist", "ik1")
    r.check("persist decrements → (3,1)",   stats(), (3, 1))
    cmd(sock, "set", "ik2", "v2")           # SET clears the TTL
    r.check("SET clears ttl → (3,0)",       stats(), (3, 0))
    cmd(sock, "pexpire", "ik3", "50")
    time.sleep(0.3)                         # active reaper fires on the ttl heap
    r.check("expired key leaves both → (2,0)", stats(), (2, 0))
    cmd(sock, "del", "ik1", "ik2")
    r.check("del → (0,0)",                  stats(), (0, 0))


# ─── Pub/Sub (milestone V8) ────────────────────────────────────────────────────
#
# Pub/Sub replies arrive asynchronously on a connection nobody is "asking", so
# these need a timeout-aware reader rather than the request/response `cmd()`.

def _push(sock: socket.socket, timeout: float = 1.0) -> Any:
    """One pushed reply, or None if nothing arrives before `timeout`.

    A timeout always fires on the first byte of a frame, never mid-frame, so the
    stream stays parseable afterwards.
    """
    old = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        return recv_response(sock)
    except (socket.timeout, TimeoutError, OSError, RespError):
        return None
    finally:
        try:
            sock.settimeout(old)
        except OSError:
            pass


def _drain(sock: socket.socket, timeout: float = 1.0, limit: int = 4000) -> list:
    """Every push currently pending, stopping at the first silence."""
    out = []
    while len(out) < limit:
        p = _push(sock, timeout)
        if p is None:
            break
        out.append(p)
    return out


def _delivery(push: Any):
    """Normalize message/pmessage to (channel, payload); None for anything else."""
    if isinstance(push, list) and len(push) == 3 and push[0] == "message":
        return (push[1], push[2])
    if isinstance(push, list) and len(push) == 4 and push[0] == "pmessage":
        return (push[2], push[3])
    return None


def _sub(sock: socket.socket, kind: str, *names: str) -> list:
    """Send (P)SUBSCRIBE/(P)UNSUBSCRIBE and read one confirmation per name."""
    send_request(sock, kind, *names)
    return [_push(sock) for _ in names]


def test_pubsub_commands(r: TestRunner, host: str, port: int):
    r.section("Pub/Sub: SUBSCRIBE / UNSUBSCRIBE / PUBLISH (V8.1)")
    sub = make_conn(host, port)
    pub = make_conn(host, port)
    try:
        r.check("subscribe confirmation",   _sub(sub, "subscribe", "news")[0],
                ["subscribe", "news", 1])
        r.check("second subscribe → count 2", _sub(sub, "subscribe", "sports")[0],
                ["subscribe", "sports", 2])

        r.check("publish reports 1 receiver", cmd(pub, "publish", "news", "hello"), 1)
        r.check("subscriber receives message", _push(sub), ["message", "news", "hello"])
        r.check("publish to empty channel → 0", cmd(pub, "publish", "nobody", "x"), 0)
        r.check("no stray push for empty channel", _push(sub, 0.3), None)

        # RESP2 subscribe-mode gate
        r.expect_error("GET refused while subscribed", sub, "get", "foo")
        r.check("PING allowed while subscribed", cmd(sub, "ping"), "PONG")

        # arity: do_publish reads cmd[2], so 2-arg PUBLISH must be rejected, not
        # allowed through into an out-of-bounds read
        r.expect_error("publish with no message → arity error", pub, "publish", "chan")
        r.check("server alive after arity probe", cmd(pub, "ping"), "PONG")

        r.check("unsubscribe one channel", _sub(sub, "unsubscribe", "news")[0],
                ["unsubscribe", "news", 1])
        send_request(sub, "unsubscribe")            # no args = leave everything
        r.check("bare unsubscribe drains to 0", _push(sub), ["unsubscribe", "sports", 0])
        r.check("subscribe mode ends with the last subscription",
                cmd(sub, "set", "pubsub:post", "v"), "OK")

        # teardown: a dead subscriber must be unlinked from every channel set,
        # or PUBLISH dereferences a freed Conn*
        ghost = make_conn(host, port)
        _sub(ghost, "subscribe", "haunted")
        r.check("publish reaches subscriber before close",
                cmd(pub, "publish", "haunted", "boo"), 1)
        ghost.close()
        time.sleep(0.2)
        r.check("publish after disconnect → 0 (registry unlinked)",
                cmd(pub, "publish", "haunted", "boo"), 0)
        r.check("server alive after teardown", cmd(pub, "ping"), "PONG")
    finally:
        sub.close()
        pub.close()
        try:
            s = make_conn(host, port)
            cmd(s, "del", "pubsub:post")
            s.close()
        except Exception:
            pass


def test_pubsub_patterns(r: TestRunner, host: str, port: int):
    r.section("Pub/Sub: PSUBSCRIBE patterns (V8.2a)")
    psub = make_conn(host, port)
    esub = make_conn(host, port)
    pub  = make_conn(host, port)
    try:
        r.check("psubscribe confirmation", _sub(psub, "psubscribe", "news.*")[0],
                ["psubscribe", "news.*", 1])
        r.check("exact subscribe on a covered channel",
                _sub(esub, "subscribe", "news.sports")[0],
                ["subscribe", "news.sports", 1])

        # the headline property: one PUBLISH, two independent deliveries
        r.check("one publish counts exact + pattern",
                cmd(pub, "publish", "news.sports", "goal"), 2)
        r.check("pattern subscriber gets 4-element pmessage", _push(psub),
                ["pmessage", "news.*", "news.sports", "goal"])
        r.check("exact subscriber gets 3-element message", _push(esub),
                ["message", "news.sports", "goal"])

        r.check("non-matching channel reaches nobody",
                cmd(pub, "publish", "weather.today", "rain"), 0)
        r.check("...and no stray push arrives", _push(psub, 0.3), None)

        # counts are the conn TOTAL: channels + patterns
        r.check("subscribe count includes held patterns",
                _sub(psub, "subscribe", "extra")[0], ["subscribe", "extra", 2])
        r.check("unsubscribe count still includes the pattern",
                _sub(psub, "unsubscribe", "extra")[0], ["unsubscribe", "extra", 1])
        r.expect_error("pattern-only conn is still in subscribe mode",
                       psub, "get", "foo")

        send_request(psub, "punsubscribe")
        r.check("bare punsubscribe drains to 0", _push(psub),
                ["punsubscribe", "news.*", 0])
        r.check("pattern subscriber leaves subscribe mode", cmd(psub, "ping"), "PONG")
    finally:
        psub.close()
        esub.close()
        pub.close()


def test_pubsub_channel_acl(r: TestRunner, sock: socket.socket, host: str, port: int):
    r.section("Pub/Sub: channel ACL &pattern (V8.2b)")
    username = "stress_chan_user"
    password = "stress_chan_pass"

    try:
        cmd(sock, "acl", "deluser", username)
    except RespError:
        pass

    created = False
    try:
        cmd(sock, "acl", "setuser", username, "on", f">{password}", "~*",
            "+@all", "resetchannels", "&news.*")
        created = True
    except RespError as e:
        r.check("acl setuser with &pattern accepted", f"error: {e}", "OK")

    if not created:
        return

    c = None
    try:
        c = open_socket(host, port)
        send_request(c, "auth", username, password)
        r.check("auth as channel-restricted user → OK", recv_response(c), "OK")

        r.check("granted channel allowed", _sub(c, "subscribe", "news.sports")[0],
                ["subscribe", "news.sports", 1])
        _sub(c, "unsubscribe", "news.sports")
        r.expect_error("ungranted channel denied", c, "subscribe", "other")

        r.check("literally-granted pattern allowed", _sub(c, "psubscribe", "news.*")[0],
                ["psubscribe", "news.*", 1])
        _sub(c, "punsubscribe", "news.*")
        # a &news.* grant must not be widenable into a firehose
        r.expect_error("psubscribe '*' denied (cannot widen the grant)",
                       c, "psubscribe", "*")

        r.check("publish to granted channel allowed",
                cmd(c, "publish", "news.x", "hi"), 0)
        r.expect_error("publish to ungranted channel denied",
                       c, "publish", "other", "hi")

        listing = cmd(sock, "acl", "list")
        joined = "\n".join(listing) if isinstance(listing, list) else str(listing)
        # acl_format_user once emitted "&*" with no leading space, fusing it onto
        # the previous token so a reload parsed "~*&*" as one key pattern
        r.check_true("no fused '~*&*' token in ACL LIST", "~*&*" not in joined)
        r.check_true("channel grant rendered", "&news.*" in joined)
    except RespError as e:
        r.check("channel ACL enforcement", f"error: {e}", "no error")
    finally:
        if c is not None:
            c.close()
        try:
            cmd(sock, "acl", "deluser", username)
        except RespError:
            pass


def test_keyspace_notifications(r: TestRunner, host: str, port: int):
    r.section("Pub/Sub: keyspace notifications (V8.3)")
    admin = make_conn(host, port)
    listener = None
    old_flags = ""
    try:
        cur = cmd(admin, "config", "get", "notify-keyspace-events")
        if not (isinstance(cur, list) and len(cur) == 2):
            r.check("CONFIG GET notify-keyspace-events returns a pair", cur, "[name, value]")
            return
        old_flags = cur[1]
        r.check("CONFIG SET notify-keyspace-events KEA",
                cmd(admin, "config", "set", "notify-keyspace-events", "KEA"), "OK")

        listener = make_conn(host, port)
        _sub(listener, "psubscribe", "__key*@0__:*")

        # K and E are independent forms of the same event
        cmd(admin, "set", "notif:foo", "bar")
        got = {d for d in (_delivery(p) for p in _drain(listener)) if d}
        r.check_true("SET emits __keyspace__ form (payload = event)",
                     ("__keyspace@0__:notif:foo", "set") in got)
        r.check_true("SET emits __keyevent__ form (payload = key)",
                     ("__keyevent@0__:set", "notif:foo") in got)

        cmd(admin, "lpush", "notif:list", "a")
        got = {d for d in (_delivery(p) for p in _drain(listener)) if d}
        r.check_true("LPUSH emits the list-class event",
                     ("__keyevent@0__:lpush", "notif:list") in got)

        cmd(admin, "del", "notif:foo")
        got = {d for d in (_delivery(p) for p in _drain(listener)) if d}
        r.check_true("DEL emits the generic-class event",
                     ("__keyevent@0__:del", "notif:foo") in got)

        # the dirty-counter gate: a write that changed nothing stays silent
        cmd(admin, "del", "notif:definitely-absent")
        r.check("no-op write emits nothing", _push(listener, 0.4), None)

        # per-class filtering: 'Ex' = keyevent + expired only
        cmd(admin, "config", "set", "notify-keyspace-events", "Ex")
        cmd(admin, "set", "notif:quiet", "v")
        r.check("with 'Ex' a string write is filtered out", _push(listener, 0.4), None)

        # TTL must outlast the silence window above or the two race
        cmd(admin, "psetex", "notif:ttl", "1200", "v")
        r.check("with 'Ex' PSETEX itself is filtered out", _push(listener, 0.4), None)
        got = {d for d in (_delivery(p) for p in _drain(listener, 2.5)) if d}
        r.check_true("expired hook fires on TTL expiry",
                     ("__keyevent@0__:expired", "notif:ttl") in got)
        r.check_true("with 'Ex' the __keyspace__ form is suppressed",
                     not any(c.startswith("__keyspace@") for c, _ in got))

        # off means silent
        cmd(admin, "config", "set", "notify-keyspace-events", "")
        cmd(admin, "set", "notif:silent", "v")
        r.check("notifications off → nothing emitted", _push(listener, 0.4), None)
    except RespError as e:
        r.check("keyspace notifications", f"error: {e}", "no error")
    finally:
        if listener is not None:
            listener.close()
        try:
            cmd(admin, "config", "set", "notify-keyspace-events", old_flags)
            cmd(admin, "del", "notif:list", "notif:quiet", "notif:silent")
        except Exception:
            pass
        admin.close()


def test_pubsub_fanout_concurrency(r: TestRunner, host: str, port: int,
                                   publishers: int = 4, subscribers: int = 4,
                                   per_publisher: int = 250):
    """Fan-out under load: every subscriber must receive every message.

    This is the concurrency case Pub/Sub actually stresses — PUBLISH writes into
    *other* connections' outgoing buffers and flips their want_write, so a lost
    message here would mean the poll loop missed a flag flip or a buffer grew
    incorrectly under interleaving.
    """
    r.section("Pub/Sub: fan-out under concurrent publishers")
    channel = "stress:fanout"
    expected = publishers * per_publisher

    subs = []
    try:
        for _ in range(subscribers):
            s = make_conn(host, port)
            _sub(s, "subscribe", channel)
            subs.append(s)
    except Exception as e:
        for s in subs:
            s.close()
        r.check("fan-out setup", f"error: {e}", "connected")
        return

    received = [0] * subscribers
    errors: list = []

    def reader(idx: int):
        sock = subs[idx]
        count = 0
        try:
            while count < expected:
                p = _push(sock, 3.0)
                if p is None:
                    break
                if _delivery(p) is not None:
                    count += 1
        except Exception as e:                     # pragma: no cover
            errors.append(f"reader {idx}: {e}")
        received[idx] = count

    def publisher():
        try:
            c = make_conn(host, port)
            for i in range(per_publisher):
                cmd(c, "publish", channel, f"m{i}")
            c.close()
        except Exception as e:
            errors.append(f"publisher: {e}")

    readers = [threading.Thread(target=reader, args=(i,)) for i in range(subscribers)]
    for t in readers:
        t.start()
    pubs = [threading.Thread(target=publisher) for _ in range(publishers)]
    started = time.perf_counter()
    for t in pubs:
        t.start()
    for t in pubs:
        t.join()
    for t in readers:
        t.join()
    elapsed = time.perf_counter() - started

    for s in subs:
        s.close()

    r.check_true("no publisher/reader errors", not errors)
    if errors:
        for e in errors[:5]:
            print(f"    {e}")
    r.check("every subscriber received every message",
            received, [expected] * subscribers)
    total = expected * subscribers
    if elapsed > 0:
        print(f"    {publishers} publishers × {per_publisher} msgs → "
              f"{subscribers} subscribers = {total} deliveries in {elapsed:.2f}s "
              f"({total / elapsed:,.0f} deliveries/s)")


# ---------------------------------------------------------------------------
# Transactions (V8.4 - V8.7)
# ---------------------------------------------------------------------------

def _raw_reply(sock: socket.socket, *args: str) -> str:
    """Send a command and return its first raw RESP line, prefix included.

    recv_response() collapses both `$-1` and `*-1` to None, so a null array is
    indistinguishable from a null bulk string at the Python level. EXEC's
    watch-invalidation reply must be a null ARRAY, so this pins the wire bytes.
    Only safe for replies that are exactly one line.
    """
    send_request(sock, *args)
    return _recv_line(sock).decode(errors="replace")


def test_transactions(r: TestRunner, host: str, port: int):
    r.section("Transactions: MULTI / QUEUED / DISCARD / EXEC (V8.4-V8.5)")
    c     = make_conn(host, port)      # runs the transactions
    other = make_conn(host, port)      # observes from outside
    keys  = ("tx:a", "tx:b", "tx:list", "tx:str", "tx:after")
    try:
        for k in keys:
            cmd(other, "del", k)

        # --- V8.4: queueing -------------------------------------------------
        r.check("MULTI opens a transaction",   cmd(c, "multi"), "OK")
        r.check("queued write replies QUEUED", cmd(c, "set", "tx:a", "v1"), "QUEUED")
        r.check("queued read replies QUEUED",  cmd(c, "get", "tx:a"), "QUEUED")
        r.expect_error("nested MULTI is refused", c, "multi")
        r.check("a refused nested MULTI leaves the transaction open",
                cmd(c, "incr", "tx:b"), "QUEUED")
        r.check("queued commands have not run", cmd(other, "exists", "tx:a"), 0)

        r.check("DISCARD closes the transaction", cmd(c, "discard"), "OK")
        r.check("DISCARD ran nothing",            cmd(other, "exists", "tx:a"), 0)
        r.expect_error("DISCARD without MULTI is an error", c, "discard")
        r.check("connection still usable after DISCARD", cmd(c, "ping"), "PONG")

        # --- V8.4: queue-time rejection poisons the batch --------------------
        cmd(c, "multi")
        r.expect_error("unknown command is rejected at queue time",
                       c, "definitely_not_a_command")
        r.expect_error("bad arity is rejected at queue time", c, "set")
        # [REG] mode switches are rejected outright, never deferred to EXEC
        r.expect_error("SUBSCRIBE inside MULTI is rejected",
                       c, "subscribe", "tx:chan")
        r.check("queuing continues after a rejection",
                cmd(c, "set", "tx:a", "late"), "QUEUED")
        r.expect_error("EXEC on a poisoned transaction aborts", c, "exec")
        r.check("EXECABORT ran nothing", cmd(other, "exists", "tx:a"), 0)

        # --- V8.5: EXEC -------------------------------------------------------
        r.expect_error("EXEC without MULTI is an error", c, "exec")
        cmd(c, "multi")
        r.check("empty transaction commits as an empty array", cmd(c, "exec"), [])

        cmd(c, "multi")
        cmd(c, "set",   "tx:a", "v1")
        cmd(c, "incr",  "tx:b")
        cmd(c, "rpush", "tx:list", "x")
        cmd(c, "get",   "tx:a")
        r.check("EXEC returns one array of results, in order",
                cmd(c, "exec"), ["OK", 1, 1, "v1"])
        r.check("EXEC applied the writes", cmd(other, "get", "tx:a"), "v1")

        # A command that queues cleanly but fails at runtime: Redis does NOT roll
        # back. The error is one element of the array and later commands still run.
        # Read raw because recv_response() raises on a '-' element mid-array.
        cmd(other, "set", "tx:str", "abc")
        cmd(c, "multi")
        cmd(c, "incr", "tx:str")             # runtime error: not an integer
        cmd(c, "set",  "tx:after", "ran")
        send_request(c, "exec")
        lines = [_recv_line(c).decode(errors="replace") for _ in range(3)]
        r.check("EXEC header counts every queued command", lines[0], "*2")
        r.check("a failing element is an inline error",    lines[1][:1], "-")
        r.check("commands after a failing one still run",  lines[2], "+OK")
        r.check("no rollback: the later write is visible",
                cmd(other, "get", "tx:after"), "ran")
    finally:
        try:
            for k in keys:
                cmd(other, "del", k)
        except Exception:
            pass
        c.close()
        other.close()


def test_transaction_watch(r: TestRunner, host: str, port: int):
    r.section("Transactions: WATCH / UNWATCH (V8.6-V8.7)")
    a = make_conn(host, port)          # the watcher
    b = make_conn(host, port)          # the interfering writer
    # "watch" is deliberately the literal command name - see the [REG] below
    keys = ("tx:wk", "tx:wk2", "tx:wk3", "tx:ttl", "tx:ghost", "watch")
    try:
        for k in keys:
            cmd(b, "del", k)
        cmd(b, "set", "tx:wk", "original")

        # --- V8.7: commit-time invalidation ----------------------------------
        r.check("WATCH replies OK", cmd(a, "watch", "tx:wk"), "OK")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA")
        cmd(b, "set", "tx:wk", "fromB")          # a DIFFERENT conn dirties it
        # [REG] null ARRAY (*-1), not a null bulk ($-1) - both parse as None
        r.check("EXEC aborts with a null array after a watched write",
                _raw_reply(a, "exec"), "*-1")
        r.check("the aborted transaction ran nothing",
                cmd(b, "get", "tx:wk"), "fromB")

        # --- watches are cleared by EXEC (else every later txn would fail) ----
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA2")
        r.check("a fresh transaction commits, watches cleared by EXEC",
                cmd(a, "exec"), ["OK"])
        r.check("...and its write landed", cmd(b, "get", "tx:wk"), "fromA2")

        # --- an untouched watch commits normally ------------------------------
        cmd(a, "watch", "tx:wk")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA3")
        r.check("EXEC commits when the watched key was never touched",
                cmd(a, "exec"), ["OK"])

        # --- UNWATCH ----------------------------------------------------------
        cmd(a, "watch", "tx:wk")
        r.check("UNWATCH replies OK", cmd(a, "unwatch"), "OK")
        cmd(b, "set", "tx:wk", "fromB2")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA4")
        r.check("EXEC commits after UNWATCH despite the outside write",
                cmd(a, "exec"), ["OK"])

        # --- DISCARD clears watches too ---------------------------------------
        cmd(a, "watch", "tx:wk")
        cmd(a, "multi")
        cmd(a, "discard")
        cmd(b, "set", "tx:wk", "fromB3")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA5")
        r.check("DISCARD cleared the watch", cmd(a, "exec"), ["OK"])

        # [REG] multi-key writes must dirty EVERY key they touch, not just cmd[1]
        # - this is what cmd_collect_keys() exists for
        cmd(b, "mset", "tx:wk2", "1", "tx:wk3", "1")
        cmd(a, "watch", "tx:wk3")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk3", "fromA")
        cmd(b, "del", "tx:wk2", "tx:wk3")        # tx:wk3 is the LAST argument
        r.check("a multi-key write dirties a watcher of its last key",
                _raw_reply(a, "exec"), "*-1")

        # [REG] do_watch must skip cmd[0]; looping from 0 watched a key named
        # "watch" and any write to it aborted unrelated transactions
        cmd(a, "watch", "tx:wk")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "fromA6")
        cmd(b, "set", "watch", "notakey")        # the literal command name
        r.check("writing a key named 'watch' does not abort a transaction",
                cmd(a, "exec"), ["OK"])

        # --- WATCH inside MULTI is refused, and must NOT poison ---------------
        cmd(a, "multi")
        r.expect_error("WATCH inside MULTI is refused", a, "watch", "tx:wk")
        cmd(a, "set", "tx:wk", "fromA7")
        r.check("a refused WATCH did not poison the transaction",
                cmd(a, "exec"), ["OK"])

        # --- natural expiry does NOT invalidate (deliberate divergence) -------
        cmd(b, "psetex", "tx:ttl", "400", "gone")
        cmd(a, "watch", "tx:ttl")
        cmd(a, "multi")
        cmd(a, "set", "tx:wk", "after-expiry")
        time.sleep(0.9)                          # comfortably past the TTL
        r.check("a watched key expiring on its own does not abort",
                cmd(a, "exec"), ["OK"])

        # --- teardown: watchers holds raw Conn*, so a dead watcher must unlink -
        ghost = make_conn(host, port)
        cmd(ghost, "watch", "tx:ghost")
        ghost.close()
        time.sleep(0.2)
        r.check("write to a dead watcher's key is safe",
                cmd(b, "set", "tx:ghost", "v"), "OK")
        r.check("server alive after watcher teardown", cmd(b, "ping"), "PONG")
    finally:
        try:
            for k in keys:
                cmd(b, "del", k)
        except Exception:
            pass
        a.close()
        b.close()


def run_redis_benchmark(host: str, port: int, password: Optional[str],
                        requests: int, clients: int, pipeline: int) -> bool:
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  Speed baseline (redis-benchmark){RESET}")
    print(f"{'═' * 55}")
    exe = shutil.which("redis-benchmark")
    if not exe:
        print(f"{YELLOW}redis-benchmark not found — skipping (install redis-tools){RESET}")
        return True

    # redis-benchmark opens `clients` connections and AUTHs them all at once, and
    # it does NOT retry BUSY. The server throttles concurrent AUTH to 4
    # (k_max_auth_inflight, bounds Argon2 memory), so an authed run with >4 clients
    # bounces. Throughput is best measured on a passwordless instance.
    if password and clients > 4:
        print(f"{YELLOW}note: AUTH is throttled to 4 concurrent (k_max_auth_inflight) and "
              f"redis-benchmark won't retry BUSY across {clients} clients. For a real "
              f"throughput number benchmark a passwordless instance, or pass "
              f"--bench-clients 4.{RESET}")

    # start clean: leftover keys from an earlier run (e.g. a 100k-element mylist)
    # make every later number meaningless
    try:
        s = make_conn(host, port)
        cmd(s, "flushall")
        s.close()
    except Exception as e:
        print(f"{YELLOW}pre-bench flushall failed: {e}{RESET}")

    tests = ["ping", "set", "get", "incr", "lpush", "rpush", "lpop", "rpop",
             "sadd", "hset", "spop", "zadd", "zpopmin", "lrange", "mset"]
    # generous for a Release build; a Debug build will trip this on the list tests
    per_test_timeout = max(120, requests // 1000)
    ok = True
    for t in tests:
        argv = [exe, "-h", host, "-p", str(port), "-t", t,
                "-n", str(requests), "-c", str(clients), "-P", str(pipeline), "-q"]
        if password:
            argv += ["-a", password]
        if G_TLS:
            argv += ["--tls"]
            if G_TLS_INSECURE:
                argv += ["--insecure"]
            if G_TLS_CA and not G_TLS_INSECURE:
                argv += ["--cacert", G_TLS_CA]
            if G_TLS_CERT:
                argv += ["--cert", G_TLS_CERT, "--key", G_TLS_KEY]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=per_test_timeout)
        except subprocess.TimeoutExpired:
            print(f"  {RED}{t.upper()}: TIMED OUT after {per_test_timeout}s{RESET} — "
                  f"server is far below expected speed. Benchmarking needs a "
                  f"Release build (cmake -B build-rel -DCMAKE_BUILD_TYPE=Release); "
                  f"a Debug build audits the whole keyspace after every command.")
            ok = False
            continue
        for ln in proc.stdout.splitlines():
            if ln.strip():
                print(f"  {ln}")
        if proc.returncode != 0:
            for ln in proc.stderr.splitlines():
                print(f"  {RED}{ln}{RESET}")
            ok = False
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global G_PASSWORD, G_TLS, G_TLS_INSECURE, G_TLS_CA, G_TLS_CERT, G_TLS_KEY

    ap = argparse.ArgumentParser(description="Redis server RESP stress test")
    ap.add_argument("--host",             default=DEFAULT_HOST)
    ap.add_argument("--port",             default=DEFAULT_PORT, type=int)
    ap.add_argument("--password",         default=None,
                    help="server password (if auth is enabled)")
    ap.add_argument("--tls",              action="store_true",
                    help="connect over TLS (wraps client sockets, passes --tls to redis-benchmark); "
                         "point --port at the tls-port")
    ap.add_argument("--tls-insecure",     action="store_true",
                    help="skip server certificate verification (for self-signed test certs)")
    ap.add_argument("--tls-ca",           default=None,
                    help="CA cert file to verify the server (omit with --tls-insecure)")
    ap.add_argument("--tls-cert",         default=None,
                    help="client certificate for mTLS (requires --tls-key)")
    ap.add_argument("--tls-key",          default=None,
                    help="client private key for mTLS (requires --tls-cert)")
    ap.add_argument("--correctness-only", action="store_true")
    ap.add_argument("--stress-only",      action="store_true")
    ap.add_argument("--stress-threads",   default=STRESS_THREADS, type=int,
                    help="worker threads for the random stress phase")
    ap.add_argument("--stress-ops",       default=STRESS_OPS, type=int,
                    help="operations per worker thread in the stress phase")
    ap.add_argument("--metrics-top",      default=12, type=int,
                    help="number of command/operation rows to show in metrics")
    ap.add_argument("--bench",            action="store_true",
                    help="run a redis-benchmark speed baseline after the other phases")
    ap.add_argument("--bench-requests",   default=100000, type=int,
                    help="requests per redis-benchmark test")
    ap.add_argument("--bench-clients",    default=50, type=int,
                    help="parallel clients for redis-benchmark")
    ap.add_argument("--bench-pipeline",   default=16, type=int,
                    help="pipeline depth for redis-benchmark")
    ap.add_argument("--log",              default="auto",
                    help="write a copy of all output here (ANSI stripped). "
                         "'auto' (default) derives docs/<kind>_<plain|tls>.md so a "
                         "TLS run never overwrites the plaintext one; "
                         "pass --log '' to disable")
    args = ap.parse_args()

    host, port  = args.host, args.port
    G_PASSWORD  = args.password
    G_TLS          = args.tls
    G_TLS_INSECURE = args.tls_insecure
    G_TLS_CA       = args.tls_ca
    G_TLS_CERT     = args.tls_cert
    G_TLS_KEY      = args.tls_key

    if bool(args.tls_cert) != bool(args.tls_key):
        print(f"{RED}--tls-cert and --tls-key must be given together{RESET}")
        sys.exit(2)
    if (args.tls_ca or args.tls_cert or args.tls_insecure) and not args.tls:
        print(f"{RED}--tls-* options require --tls{RESET}")
        sys.exit(2)
    if args.tls and not (args.tls_insecure or args.tls_ca):
        print(f"{YELLOW}note: --tls without --tls-ca or --tls-insecure verifies against the "
              f"system CA store and will reject a self-signed cert — use --tls-insecure "
              f"for local test certs{RESET}")

    if args.stress_threads < 1 or args.stress_ops < 1 or args.metrics_top < 1:
        print(f"{RED}--stress-threads, --stress-ops, and --metrics-top must be >= 1{RESET}")
        sys.exit(2)

    # mirror everything to a shareable markdown log, named per transport+mode
    log_path = default_log_path(args) if args.log == "auto" else args.log
    if log_path:
        start_logging(log_path, run_label(args, host, port))
        print(f"(logging output to {log_path})")

    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  MYRED — {run_label(args, host, port)}{RESET}")
    print(f"{'═' * 55}")
    print(f"  Target:    {host}:{port}")
    print(f"  Transport: {'TLS' + (' (insecure — cert not verified)' if G_TLS_INSECURE else '') if G_TLS else 'plaintext'}")
    print(f"  Auth:      {'password' if G_PASSWORD else 'none'}")
    if log_path:
        print(f"  Log:       {log_path}")
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
            test_numeric_commands(r,      sock)
            test_setvariant_commands(r,   sock)
            test_multikey_commands(r,     sock)
            test_bulkrange_commands(r,    sock)
            test_keys_command(r,          sock)
            test_ttl_commands(r,          sock)
            test_zset_commands(r,         sock)
            test_zquery_commands(r,       sock)
            test_zset_extended(r,         sock)
            test_list_commands(r,         sock)
            test_hash_commands(r,                  sock)
            test_extended_hash_commands(r,         sock)
            test_generic_commands(r,               sock)
            test_scan_command(r,                   sock)
            test_extended_generic_commands(r,      sock)
            test_unlink_command(r,                 sock)
            test_set_commands(r,                   sock)
            test_set_random_semantics(r,           sock)
            test_edge_cases(r,            sock)
            test_ping_command(r,          sock)
            test_config_command(r,        sock)
            test_acl_commands(r,          sock, host, port)
            test_pubsub_channel_acl(r,    sock, host, port)
            test_info_command(r,          sock)
            test_info_keyspace_stats(r,   sock)
            test_flushdb_command(r,       sock)
            test_save_command(r,          sock)
            test_bgsave_command(r,        sock)
            test_bgrewriteaof_command(r,  sock)
            test_memory_commands(r,       sock)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False
        finally:
            sock.close()

        # tests that manage their own connections
        try:
            test_echo_and_inline(r,       host, port)
            test_auth_command(r,          host, port)
            test_pubsub_commands(r,       host, port)
            test_pubsub_patterns(r,       host, port)
            test_keyspace_notifications(r, host, port)
            test_transactions(r,          host, port)
            test_transaction_watch(r,     host, port)
            test_persistence_roundtrip(r, host, port)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False

        all_ok = r.summary() and all_ok

    # ── concurrent safety ──────────────────────────────────────────────────────
    if not args.stress_only and not args.correctness_only:
        all_ok = test_concurrent_writes(host, port) and all_ok
        rp = TestRunner(host, port)
        try:
            test_pubsub_fanout_concurrency(rp, host, port)
        except Exception as e:
            print(f"\n{RED}Unexpected error: {e}{RESET}")
            all_ok = False
        all_ok = rp.summary() and all_ok

    # ── stress ─────────────────────────────────────────────────────────────────
    if not args.correctness_only:
        all_ok = run_stress_test(host, port, args.stress_threads,
                                 args.stress_ops, args.metrics_top) and all_ok
        cleanup_stress_keys(host, port)

    # ── speed baseline (redis-benchmark) ───────────────────────────────────────
    if args.bench:
        all_ok = run_redis_benchmark(host, port, G_PASSWORD, args.bench_requests,
                                     args.bench_clients, args.bench_pipeline) and all_ok
        try:
            s = make_conn(host, port)
            cmd(s, "flushall")              # benchmark keys are junk
            s.close()
        except Exception:
            pass

    COMMAND_METRICS.report(args.metrics_top)

    # ── final verdict ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    if all_ok:
        print(f"{BOLD}{GREEN}  ALL TESTS PASSED{RESET}")
    else:
        print(f"{BOLD}{RED}  SOME TESTS FAILED{RESET}")
    print(f"  {run_label(args, host, port)}")
    if log_path:
        print(f"  {BOLD}Results saved to {log_path}{RESET}")
    print(f"{'═' * 55}\n")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
