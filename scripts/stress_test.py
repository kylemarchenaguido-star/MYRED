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
import json
import select
import shutil
import signal
import hashlib
import subprocess
import tempfile
import atexit
import ssl
import weakref
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
    """Which phase mix this invocation runs.

    'full' is the everything run: the managed-instance phases only exist when a
    binary was named, so naming one is what distinguishes a complete run from a
    live-server one.
    """
    if args.stress_only:
        return "stress"
    if args.correctness_only:
        return "correctness"
    if getattr(args, "server", None):
        return "full"
    if args.bench:
        return "bench"
    return "stress_results"


def default_log_path(args, facts: dict) -> str:
    """Per-run log path, split by environment first.

        <log-dir>/{WSL,Native}/{full,bench,stress,correctness,stress_results}_{plain,tls}.md

    The environment directory is the point: a throughput number from a VM and
    one from bare metal are not the same measurement, and filing them under one
    name makes the difference disappear the moment the second run finishes.
    """
    root = getattr(args, "log_dir", None) or os.path.join("docs", "logs")
    return os.path.join(root, env_slug(facts),
                        f"{run_kind(args)}_{'tls' if args.tls else 'plain'}.md")


def run_label(args, host: str, port: int) -> str:
    """One-line human description of what this run actually covers."""
    phases = {
        "full":           "correctness + concurrency + managed-instance phases "
                          "+ stress" + (" + redis-benchmark" if args.bench else ""),
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
        # str() rather than a bare .encode(): callers pass ints for counts and
        # timeouts often enough that requiring strings only ever produces a
        # TypeError somewhere far from the mistake.
        encoded = str(a).encode()
        out += f"${len(encoded)}\r\n".encode()
        out += encoded
        out += b"\r\n"
    sock.sendall(bytes(out))


# ─── buffered reads ───────────────────────────────────────────────────────────
#
# RESP is read a line or a declared length at a time, and the obvious
# implementation asks the kernel for ONE BYTE per call. A 100k-element KEYS
# reply then costs over a million syscalls, and what comes back is a measurement
# of CPython rather than of the server: 1422 ms client-side against 19 ms of
# actual server CPU for the same command, and the distortion scales with the
# host's syscall cost — 26x between two machines the C client puts 2.3x apart.
#
# So every socket gets a read buffer. A WeakKeyDictionary because sockets are
# opened and closed all over this file and nobody is going to remember to
# unregister one; when the socket is collected its buffer goes with it.
#
# A second thing this fixes: the old line reader accumulated bytes into a LOCAL
# buffer, so a socket timeout part-way through a line discarded the bytes it had
# already taken from the kernel and left the stream desynchronised. Buffered,
# a timeout is resumable — which is what the pub/sub push readers rely on.

class _ReadBuf:
    __slots__ = ("buf", "pos")

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0


_READ_BUFS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_READ_CHUNK = 64 * 1024


def _readbuf(sock: socket.socket) -> "_ReadBuf":
    r = _READ_BUFS.get(sock)
    if r is None:
        r = _ReadBuf()
        _READ_BUFS[sock] = r
    return r


def _consumed(r: "_ReadBuf") -> None:
    """Reclaim the consumed prefix. Cheap when the buffer is fully drained,
    which is the common case between commands."""
    if r.pos == len(r.buf):
        del r.buf[:]
        r.pos = 0
    elif r.pos > (1 << 20):
        del r.buf[:r.pos]
        r.pos = 0


def _fill(sock: socket.socket, r: "_ReadBuf") -> None:
    chunk = sock.recv(_READ_CHUNK)
    if not chunk:
        raise ConnectionError("Server closed connection")
    r.buf += chunk


def _recv_line(sock: socket.socket) -> bytes:
    """Read one CRLF-terminated line, returning the content without CRLF.

    Scans for the CRLF pair rather than treating a bare \\r as a framing error:
    RESP simple strings and errors cannot contain either byte, so the pair IS
    the delimiter, and a length-prefixed bulk payload never reaches this path.
    """
    r = _readbuf(sock)
    while True:
        i = r.buf.find(b"\r\n", r.pos)
        if i >= 0:
            line = bytes(r.buf[r.pos:i])
            r.pos = i + 2
            _consumed(r)
            return line
        _fill(sock, r)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes."""
    r = _readbuf(sock)
    while len(r.buf) - r.pos < n:
        _fill(sock, r)
    out = bytes(r.buf[r.pos:r.pos + n])
    r.pos += n
    _consumed(r)
    return out


def _skip_exact(sock: socket.socket, n: int) -> None:
    """Discard exactly n bytes without materialising them — the trailing CRLF
    of every bulk string, which is one throwaway allocation per element on a
    reply that can hold 100k of them."""
    r = _readbuf(sock)
    while len(r.buf) - r.pos < n:
        _fill(sock, r)
    r.pos += n
    _consumed(r)


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
        _skip_exact(sock, 2)             # consume trailing \r\n
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
    # a start/end below -len clamps to 0 — it does NOT collapse to empty.
    # This asserted "" until the differential harness showed Redis returns the
    # first byte; the test was encoding MYRED's own bug.
    r.check("getrange -99 -99 → 'H' (clamped)",
            cmd(sock, "getrange", "br3", "-99", "-99"), "H")

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

    # Setting NOTHING never creates a key and never pads: a missing key answers
    # 0 and stays missing. This asserted 3 with a comment claiming a zero-padded
    # key was created — MYRED's behaviour written down as if it were the spec.
    cmd(sock, "del", "br3")
    r.check("setrange empty val on missing → 0",
            cmd(sock, "setrange", "br3", "3", ""), 0)
    r.check("...and the key was not created", cmd(sock, "exists", "br3"), 0)
    # on an EXISTING key it answers the current length, unchanged
    cmd(sock, "set", "br3", "hello")
    r.check("setrange empty val on existing → 5",
            cmd(sock, "setrange", "br3", "3", ""), 5)
    r.check("strlen br3 → 5", cmd(sock, "strlen", "br3"), 5)

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
                # A key this worker owns, NOT one from the shared random pool.
                # `random_key()` draws from 201 names shared by every worker, and
                # another worker's DEL between the SET and the OBJECT lands often
                # enough to fail roughly one run in three. OBJECT ENCODING
                # answering "ERR no such key" for a key that is gone is correct
                # Redis behaviour, so the race was in the test's expectation, not
                # in the server. Concurrent deletion is still exercised by every
                # other op in the mix.
                k = f"stress_obj_{wid}"
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
    # Said here, at the number, because the warning being elsewhere has not
    # stopped anyone reading it as a server measurement — twice now.
    #
    # This figure is CLIENT-BOUND: eight Python threads contending for the GIL,
    # parsing RESP in the interpreter. Measured on one machine, five runs each,
    # it reports TLS ~13% FASTER than plaintext, well outside a 3-6% spread —
    # a genuine and repeatable property of the *client*, since the ssl module
    # releases the GIL across longer C sections than a bare socket does. The
    # server cannot be faster with encryption added.
    #
    # Use it to compare the same transport across runs. For anything about
    # MYRED's own speed, read the redis-benchmark table (--bench).
    print(f"  {YELLOW}note{RESET} client-bound (GIL-contended); never use this "
          f"to compare transports — see --bench for server throughput")
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


# Parsed redis-benchmark output, kept so the run summary carries comparable
# numbers instead of only a wall of text. `-q` prints one line per test:
#   SET: 123456.78 requests per second, p50=0.207 msec
#   LRANGE_100 (first 100 elements): 45678.12 requests per second, p50=1.7 msec
_BENCH_LINE = re.compile(
    r"^(?P<name>[^:]+):\s+(?P<rps>[\d.]+)\s+requests per second"
    r"(?:.*?p50=(?P<p50>[\d.]+))?", re.I)


def _bench_name(raw: str) -> Optional[str]:
    """Normalize a redis-benchmark label into a stable key, or None to drop it.

    The lrange tests carry a parenthetical ("first 100 elements") that is part
    of the label, not of the identity, and the run prints an extra LPUSH line
    for the data it has to load first — a real measurement of something nobody
    asked for, which would otherwise sit in the table next to the real LPUSH row.
    """
    name = raw.strip()
    if "needed to benchmark" in name.lower():
        return None
    return name.split(" (")[0].strip().lower()

BENCH_RESULTS = {"params": None, "tests": {}}


def run_redis_benchmark(host: str, port: int, password: Optional[str],
                        requests: int, clients: int, pipeline: int) -> bool:
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  Speed baseline (redis-benchmark){RESET}")
    print(f"{'═' * 55}")
    BENCH_RESULTS["params"] = {"requests": requests, "clients": clients,
                               "pipeline": pipeline,
                               "transport": "tls" if G_TLS else "plain"}
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
            if not ln.strip():
                continue
            print(f"  {ln}")
            m = _BENCH_LINE.match(ln.strip())
            if m:
                key = _bench_name(m.group("name"))
                if key:
                    BENCH_RESULTS["tests"][key] = {
                        "rps": float(m.group("rps")),
                        "p50": float(m.group("p50")) if m.group("p50") else None,
                    }
        if proc.returncode != 0:
            for ln in proc.stderr.splitlines():
                print(f"  {RED}{ln}{RESET}")
            ok = False
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
#  PLATFORM — which machine produced these numbers
#
#  Everything here is read straight out of the kernel (/proc, /sys) rather than
#  guessed from `platform.uname()`, because the two questions that decide whether
#  a throughput number is comparable — "is this WSL?" and "is the CPU allowed to
#  run flat out?" — are only answerable from the kernel's own view.
#
#  The environment kind picks the log directory (docs/logs/WSL vs docs/logs/
#  Native), so a run from a VM and a run from bare metal never overwrite each
#  other's results.
# ═══════════════════════════════════════════════════════════════════════════════

def _read(path: str, limit: int = 65536) -> Optional[str]:
    """Read a /proc or /sys file, or None if it does not exist / is unreadable."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return None


def _proc_kv(path: str, keys) -> dict:
    """Pull `key: value` lines (the /proc/meminfo and /proc/cpuinfo shape)."""
    out = {}
    text = _read(path)
    if not text:
        return out
    wanted = set(keys)
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if k in wanted and k not in out:
            out[k] = v.strip()
    return out


# The ISA extensions that decide TLS throughput, in the order they matter.
# OpenSSL dispatches AES-GCM onto whichever of these the CPU advertises, and the
# difference between them is large: a part with VAES + VPCLMULQDQ processes
# several AES blocks per instruction where plain AES-NI does one.
#
# This list exists because a WSL/Native comparison came out with the TLS gap
# 20% WIDER than the plaintext gap, and the explanation turned out to be Tiger
# Lake having VAES while Zen 2 does not — a fact that was nowhere in the run
# summary and had to be dug out of /proc/cpuinfo by hand afterwards. Recording
# `cpu_model` is not enough: the model name does not tell you what the crypto
# path will be.
CRYPTO_FLAGS = ("aes", "vaes", "pclmulqdq", "vpclmulqdq", "sha_ni",
                "avx2", "avx512f", "avx512vl")


def _cpu_flags(flags: str) -> list:
    have = set(flags.split())
    return [f for f in CRYPTO_FLAGS if f in have]


def _cpu_count_from_proc() -> Optional[int]:
    text = _read("/proc/cpuinfo")
    if not text:
        return None
    n = sum(1 for l in text.splitlines() if l.startswith("processor"))
    return n or None


def _is_wsl(osrelease: str, version: str) -> Optional[str]:
    """Return 'WSL1'/'WSL2'/None. Four independent signals, because any one of
    them can be absent: a custom-built WSL2 kernel drops 'microsoft' from
    osrelease, and a container inside WSL has no WSL_DISTRO_NAME."""
    blob = f"{osrelease} {version}".lower()
    hit = ("microsoft" in blob or "wsl" in blob
           or os.environ.get("WSL_DISTRO_NAME")
           or os.environ.get("WSL_INTEROP")
           or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
           or os.path.exists("/run/WSL"))
    if not hit:
        return None
    # WSL1 emulates the syscall surface on an NT kernel and reports a 4.4
    # osrelease; WSL2 is a real Linux kernel in a Hyper-V VM. They are not
    # comparable to each other, let alone to bare metal.
    if "wsl2" in blob:
        return "WSL2"
    if osrelease.startswith("4.4.") and "microsoft" in blob:
        return "WSL1"
    return "WSL2"


def _in_container() -> Optional[str]:
    if os.path.exists("/.dockerenv"):
        return "docker"
    cg = _read("/proc/1/cgroup") or ""
    for marker in ("docker", "kubepods", "containerd", "lxc", "podman"):
        if marker in cg:
            return marker
    return None


def platform_facts() -> dict:
    """Kernel-sourced description of this machine. Values that are unavailable
    come back None rather than being invented."""
    osrelease = _read("/proc/sys/kernel/osrelease") or ""
    version = _read("/proc/version") or ""
    cpu = _proc_kv("/proc/cpuinfo", ("model name", "cpu MHz", "flags"))
    mem = _proc_kv("/proc/meminfo", ("MemTotal", "SwapTotal"))
    wsl = _is_wsl(osrelease, version)

    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    try:
        import resource
        nofile = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        nofile = None

    flags = cpu.get("flags", "")
    facts = {
        # identity
        "env":            "WSL" if wsl else "Native",
        "wsl_version":    wsl,
        "container":      _in_container(),
        "kernel":         osrelease or None,
        "kernel_build":   (version.split(" #")[0] or None) if version else None,
        "product":        _read("/sys/class/dmi/id/product_name"),
        "init":           _read("/proc/1/comm"),
        "virtualized":    ("hypervisor" in flags) if flags else None,
        # capacity
        "cpu_model":      cpu.get("model name"),
        "cpu_threads":    _cpu_count_from_proc() or os.cpu_count(),
        "cpu_affinity":   affinity,
        "cpu_mhz":        cpu.get("cpu MHz"),
        "crypto_isa":     _cpu_flags(flags) if flags else None,
        "mem_total":      mem.get("MemTotal"),
        "swap_total":     mem.get("SwapTotal"),
        # things that silently change a benchmark
        "governor":       _read("/sys/devices/system/cpu/cpu0/cpufreq/"
                                "scaling_governor"),
        "no_turbo":       _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "loadavg":        _read("/proc/loadavg"),
        "somaxconn":      _read("/proc/sys/net/core/somaxconn"),
        "overcommit":     _read("/proc/sys/vm/overcommit_memory"),
        "tcp_ulp":        _read("/proc/sys/net/ipv4/tcp_available_ulp"),
        "nofile_soft":    nofile,
        # who ran it
        "python":         sys.version.split()[0],
        "uname":          " ".join(os.uname()) if hasattr(os, "uname") else None,
    }
    return facts


def env_slug(facts: dict) -> str:
    """The log subdirectory: 'WSL' or 'Native'. Exactly two buckets on purpose —
    the split exists so a WSL number is never mistaken for a bare-metal one."""
    return "WSL" if facts.get("env") == "WSL" else "Native"


def print_platform(facts: dict):
    print(f"\n{BOLD}{BLUE}-- Platform (read from the kernel) {'-' * 21}{RESET}")
    kind = facts["env"]
    detail = facts.get("wsl_version") or ""
    if facts.get("container"):
        detail = f"{detail} in {facts['container']}".strip()
    elif not detail and facts.get("virtualized"):
        detail = "virtualized"
    print(f"  Environment:  {BOLD}{kind}{RESET}" + (f" ({detail})" if detail else ""))
    print(f"  Kernel:       {facts.get('kernel')}")
    if facts.get("product"):
        print(f"  Product:      {facts['product']}")
    print(f"  CPU:          {facts.get('cpu_model')}")
    print(f"  Threads:      {facts.get('cpu_threads')} "
          f"(usable by this process: {facts.get('cpu_affinity')})")
    isa = facts.get("crypto_isa")
    if isa is not None:
        note = "" if "vaes" in isa else "  (no vaes — one AES block per instruction)"
        print(f"  Crypto ISA:   {' '.join(isa) or 'none'}{note}")
    print(f"  Memory:       {facts.get('mem_total')}"
          + (f"  swap {facts['swap_total']}" if facts.get("swap_total") else ""))
    print(f"  Governor:     {facts.get('governor') or 'n/a'}"
          + (f"  no_turbo={facts['no_turbo']}" if facts.get("no_turbo") else ""))
    print(f"  Load average: {facts.get('loadavg')}")
    print(f"  somaxconn:    {facts.get('somaxconn')}   "
          f"nofile={facts.get('nofile_soft')}   "
          f"tcp_ulp={facts.get('tcp_ulp') or 'none'}")

    # The two caveats that actually invalidate a throughput comparison.
    if facts.get("governor") not in (None, "performance"):
        print(f"  {YELLOW}note{RESET} the CPU governor is "
              f"'{facts.get('governor')}' — throughput will vary with how warm "
              f"the machine is. 'performance' is the comparable setting.")
    try:
        busy = float((facts.get("loadavg") or "0").split()[0])
        if busy > 1.0:
            print(f"  {YELLOW}note{RESET} load average is {busy} before the run "
                  f"started — something else is using this machine and the "
                  f"numbers below are not a clean baseline.")
    except (ValueError, IndexError):
        pass


# ─── build type of the binary under test ──────────────────────────────────────
#
# A Debug build runs mem_selfcheck() after every command and it walks the whole
# keyspace, so it is O(keyspace) per command. Correctness runs on Debug are
# valuable (that walk is what catches accounting drift); speed runs on Debug are
# meaningless. We can only tell when the caller names the binary.

OPTIMIZED_BUILD_TYPES = {"release", "relwithdebinfo", "minsizerel"}


def build_facts(binary: Optional[str]) -> dict:
    """CMAKE_BUILD_TYPE from the CMakeCache.txt beside the binary, plus whether
    the binary still carries debug info. Either alone can mislead: RelWithDebInfo
    has debug info and is optimized; a hand-compiled binary has no cache file."""
    out = {"binary": binary, "cmake_build_type": None, "debug_info": None,
           "optimized": None}
    if not binary or not os.path.exists(binary):
        return out
    out["binary"] = os.path.abspath(binary)
    cache = os.path.join(os.path.dirname(out["binary"]), "CMakeCache.txt")
    text = _read(cache) or ""
    for line in text.splitlines():
        if line.startswith("CMAKE_BUILD_TYPE:"):
            out["cmake_build_type"] = line.split("=", 1)[1].strip() or None
            break
    exe = shutil.which("file")
    if exe:
        try:
            res = subprocess.run([exe, "-b", out["binary"]],
                                 capture_output=True, text=True, timeout=10)
            out["debug_info"] = "with debug_info" in res.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    t = (out["cmake_build_type"] or "").lower()
    if t:
        out["optimized"] = t in OPTIMIZED_BUILD_TYPES
    elif out["debug_info"] is not None:
        out["optimized"] = not out["debug_info"]
    return out


def warn_if_unmeasurable(bf: dict, benching: bool) -> bool:
    """Print the build type; return False when it is unfit for a speed run."""
    label = bf.get("cmake_build_type") or (
        "unknown (no CMakeCache.txt beside the binary)")
    print(f"  Build:        {label}"
          + (f"  [{os.path.basename(os.path.dirname(bf['binary']))}/]"
             if bf.get("binary") else ""))
    if bf.get("optimized") is False:
        print(f"  {YELLOW}warning{RESET} this is a Debug build: mem_selfcheck() "
              f"walks the whole keyspace after every command, so every timing "
              f"below is O(keyspace) per op. Correctness is still valid — speed "
              f"is not. Build with "
              f"-DCMAKE_BUILD_TYPE=Release for numbers.")
        return not benching
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVER CONTROL — phases that own their own instances
#
#  Everything above this line talks to a server somebody else started. The
#  phases below need process control: a restart is the only way to prove the AOF
#  replays, a SIGKILL is the only way to prove crash recovery, and replication
#  needs two instances and a link between them that can be cut.
#
#  Each instance runs on a private high port in its own temp directory with its
#  own config, so a run is safe while a real server is up on 1234, and two
#  concurrent runs on different --base-port values cannot collide.
# ═══════════════════════════════════════════════════════════════════════════════

SPAWN_TIMEOUT = 15.0          # boot-to-listening; generous for a Debug build


class Instance:
    """One server lifetime: spawn in `workdir`, SIGTERM on stop, keep stderr.

    stderr is kept in a file rather than a pipe because several phases assert on
    what the server logged, and because a failure without the server's own words
    is unusable evidence.
    """

    def __init__(self, binary: str, workdir: str, conf: str, tag: str, port: int):
        self.binary = binary
        self.workdir = workdir
        self.conf = conf
        self.tag = tag
        self.port = port
        self.stderr_path = os.path.join(workdir, f"stderr-{tag}.log")
        self.log = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen([binary, conf], cwd=workdir,
                                     stdout=self.log, stderr=self.log)
        deadline = time.time() + SPAWN_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.log.close()
                tail = "\n      ".join(
                    self.stderr_text().strip().splitlines()[-6:])
                raise RuntimeError(
                    f"[{tag}] server exited at startup "
                    f"(rc={self.proc.returncode}), see {self.stderr_path}\n"
                    f"    stderr tail:\n      {tail}")
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"[{tag}] server never opened port {port}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if not self.log.closed:
            self.log.close()

    def kill9(self):
        """Simulate a crash: SIGKILL, no shutdown save, possibly torn AOF tail."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if not self.log.closed:
            self.log.close()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stderr_text(self) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                return f.read().decode(errors="replace")
        except OSError:
            return ""

    def stderr_tail(self, n: int = 12) -> list:
        return self.stderr_text().strip().splitlines()[-n:]


def write_conf(path: str, lines) -> str:
    with open(path, "w") as f:
        f.write("".join(str(l) + "\n" for l in lines))
    return path


# ─── raw client for spawned instances ─────────────────────────────────────────
#
# The live-server client above reads the --tls/--password globals, which is
# exactly wrong here: these instances have their own passwords and are
# plaintext unless the phase asked for TLS. So these open a bare socket and
# authenticate with what the caller passes.

def raw_conn(port: int, password: Optional[str] = None,
             user: Optional[str] = None, host: str = "127.0.0.1",
             timeout: float = TIMEOUT_SEC) -> socket.socket:
    s = socket.create_connection((host, port), timeout)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if password is not None:
        args = ("auth", user, password) if user else ("auth", password)
        deadline = time.time() + TIMEOUT_SEC
        while True:
            send_request(s, *args)
            try:
                reply = recv_response(s)
                break
            except RespError as e:
                # k_max_auth_inflight is 4; a burst of connects can legally bounce
                if "BUSY" in str(e) and time.time() < deadline:
                    time.sleep(0.05)
                    continue
                s.close()
                raise
        if reply != "OK":
            s.close()
            raise RespError(f"AUTH failed: {reply!r}")
    return s


def err_of(sock: socket.socket, *args: str) -> Optional[str]:
    """Send a command; return the error text, or None if it did NOT error."""
    try:
        cmd(sock, *args)
        return None
    except RespError as e:
        return str(e)


def reply_or_err(sock: socket.socket, *args: str):
    """(reply, error_text) — exactly one is None.

    `cmd` raises on -ERR, which inside a long sequence would abandon every check
    after it. A phase has to be able to record "this was rejected" and carry on.
    """
    try:
        return cmd(sock, *args), None
    except RespError as e:
        return None, str(e)


def info_dict(sock: socket.socket, section: Optional[str] = None) -> dict:
    """INFO as a dict, falling back to the whole dump if the section is unknown."""
    try:
        raw = cmd(sock, "INFO", section) if section else cmd(sock, "INFO")
    except RespError:
        raw = cmd(sock, "INFO")
    out = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k] = v
    return out


def wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll until pred() is truthy. Swallows the transient errors that a
    reconnecting instance produces while it is between links."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except (RuntimeError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


def has_directive(sock: socket.socket, name: str) -> bool:
    """Capability probe: does this binary know the directive at all? Phases gate
    on this rather than on a version string, so the suite stays runnable while a
    milestone is half-applied and says which half is missing."""
    try:
        r = cmd(sock, "CONFIG", "GET", name)
    except RespError:
        return False
    return isinstance(r, list) and len(r) >= 2


def get_directive(sock: socket.socket, name: str):
    r = cmd(sock, "CONFIG", "GET", name)
    return r[1] if isinstance(r, list) and len(r) >= 2 else None


# ─── phase context ────────────────────────────────────────────────────────────

class PhaseCtx:
    """What a spawned phase gets: the binary, a private port range, a temp
    workdir, and the shared TestRunner.

    Instances register themselves here so the evidence dump can find them even
    when a phase dies halfway through — which is precisely when their stderr is
    worth reading.
    """

    def __init__(self, r: "TestRunner", server_bin: str, root: str,
                 base_port: int, destructive: bool = False,
                 diff_rounds: int = 8, diff_ops: int = 150,
                 diff_seed: Optional[int] = None, fuzz_runs: int = 200000):
        self.r = r
        self.server_bin = server_bin
        self.root = root
        self.destructive = destructive
        self.diff_rounds = diff_rounds
        self.diff_ops = diff_ops
        self.fuzz_runs = fuzz_runs
        # A run with no seed picks one and PRINTS it, so a failure found by a
        # random stream is always replayable with --diff-seed.
        self.diff_seed = (random.randrange(1 << 30) if diff_seed is None
                          else diff_seed)
        self._next_port = base_port
        self.instances = []          # [(tag, Instance)]
        self.skipped = 0

    # -- ports -------------------------------------------------------------
    def port(self) -> int:
        """Next free private port. Bind-tests each candidate: a stale instance
        from an interrupted run would otherwise be adopted as ours."""
        for _ in range(200):
            p = self._next_port
            self._next_port += 1
            try:
                probe = socket.socket()
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", p))
                probe.close()
                return p
            except OSError:
                continue
        raise RuntimeError("no free port in range")

    def ports(self, n: int) -> list:
        return [self.port() for _ in range(n)]

    # -- directories -------------------------------------------------------
    def dir(self, name: str) -> str:
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        return d

    # -- instances ---------------------------------------------------------
    def start(self, tag: str, workdir: str, conf: str, port: int,
              binary: Optional[str] = None) -> Instance:
        """Spawn an instance and register it for the evidence dump.

        `binary` overrides the binary under test — the differential phase needs
        a real redis-server standing beside MYRED, and everything else about
        the lifecycle (boot wait, SIGTERM, stderr kept in a file) is identical.
        """
        inst = Instance(binary or self.server_bin, workdir, conf, tag, port)
        self.instances.append((tag, inst))
        return inst

    def restart(self, inst: Instance, tag: Optional[str] = None) -> Instance:
        """Stop and re-spawn from the same config — the shape every persistence
        assertion needs. Returns the NEW instance; the old one is dead."""
        inst.stop()
        return self.start(tag or f"{inst.tag}-restart", inst.workdir,
                          inst.conf, inst.port)

    def stop_all(self):
        for _, inst in reversed(self.instances):
            try:
                inst.stop()
            except Exception:
                pass

    # -- assertions --------------------------------------------------------
    def ok(self, name: str, condition, detail: str = "") -> bool:
        """check(name, cond) with a failure detail — the shape the ported
        phases are written in. Anything falsy fails."""
        if condition:
            print(f"  {GREEN}✓{RESET} {name}")
            self.r.passed += 1
            self.r._record_result(True)
            return True
        print(f"  {RED}✗{RESET} {name}" + (f"\n    {detail}" if detail else ""))
        self.r.errors.append(name)
        self.r.failed += 1
        self.r._record_result(False)
        return False

    def skip(self, name: str, why: str):
        """Not a pass and not a failure: this binary does not have the feature.
        Counted and reported separately so a skipped milestone can never be
        mistaken for a green one."""
        self.skipped += 1
        print(f"  {YELLOW}skip{RESET} {name} — {why}")

    def section(self, title: str):
        self.r.section(title)

    # -- evidence ----------------------------------------------------------
    def dump_evidence(self, limit: int = 12):
        for tag, inst in self.instances:
            tail = inst.stderr_tail(limit)
            if not tail:
                continue
            print(f"\n{YELLOW}{tag} stderr tail ({inst.stderr_path}){RESET}")
            for line in tail:
                print("   " + line)


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: PERSISTENCE — AOF gating, replay, rewrite, hybrid, restart matrix
#
#  Every check here needs a restart, a crash, or the bytes on disk, so none of
#  them can be expressed against a live server. The invariant they share: what
#  the server answers before a restart and what it answers after must be the
#  same thing.
# ═══════════════════════════════════════════════════════════════════════════════

AOF_MAGIC = b"MYAOFRDB"


def _aof_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _frame(name: str) -> bytes:
    """The exact bulk-string encoding of one AOF argument, so a search for the
    SET frame cannot match the letters 's','e','t' inside a value."""
    b = name.encode()
    return b"$%d\r\n%s\r\n" % (len(b), b)


def _wait_for_aof(path: str, needle: bytes, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in _aof_bytes(path):
            return True
        time.sleep(0.05)
    return False


def _aof_settled(sock: socket.socket, path: str, tag: str,
                 timeout: float = 10.0) -> bool:
    """Write a sentinel, then wait for it to appear in the AOF on disk.

    Reading the file straight after a command is a race even with `appendfsync
    always`: the reply and the flush are not the same event, and under load the
    gap is wide enough to lose the last frame or two. Since the AOF is written
    in command order, a sentinel that has landed proves everything before it
    has too — which is the only cheap way to snapshot the file at a point that
    is guaranteed to contain the writes under test.
    """
    key = f"__aof_sentinel__:{tag}"
    cmd(sock, "SET", key, tag)
    ok = _wait_for_aof(path, _frame(key), timeout)
    cmd(sock, "DEL", key)
    return ok


def _wait_for_hybrid(path: str, timeout: float = 10.0) -> bool:
    """BGREWRITEAOF finalizes asynchronously and renames the tmp over the AOF;
    wait for the magic rather than for a fixed sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _aof_bytes(path)[:8] == AOF_MAGIC:
            return True
        time.sleep(0.1)
    return False


def _rewrite_diagnostics(sock: socket.socket, inst: "Instance", aof: str) -> str:
    """Everything needed to tell a broken auto-rewrite from a mistimed test.

    The trigger reads four numbers and is blocked by a fifth condition, none of
    which are visible from "it did not fire". Printing them turns one failure
    into a diagnosis instead of a reproduction attempt.
    """
    try:
        info = info_dict(sock, "persistence")
        cur = info.get("aof_current_size")
        base = info.get("aof_base_size")
        pending = info.get("aof_pending_rewrite")
        min_size = get_directive(sock, "auto-aof-rewrite-min-size")
        perc = get_directive(sock, "auto-aof-rewrite-percentage")
        growth = "n/a"
        if cur and base and int(base) > 0:
            growth = f"{(int(cur) - int(base)) * 100 // int(base)}%"
        state = (f"aof_current_size={cur} aof_base_size={base} growth={growth} "
                 f"(needs >= {perc}%, and size >= {min_size}); "
                 f"aof_pending_rewrite={pending}; on-disk={len(_aof_bytes(aof))}B")
    except Exception as e:
        state = f"could not read INFO/CONFIG: {type(e).__name__}: {e}"
    tail = [l for l in inst.stderr_tail(40) if "aof_rewrite" in l][-6:]
    return state + ("\n    server said: " + " | ".join(tail) if tail
                    else "\n    server logged nothing about aof_rewrite")


def _replay_log_clean(ctx: "PhaseCtx", inst: "Instance", label: str):
    err = inst.stderr_text()
    ctx.ok(f"{label}: stderr shows a replay happened",
           "aof_load: replayed" in err,
           f"stderr had no 'aof_load: replayed' line ({inst.stderr_path})")
    bad = [l for l in err.splitlines() if "aof_load: WARNING" in l]
    ctx.ok(f"{label}: no replay-error WARNING in stderr", not bad,
           bad[0] if bad else "")


def _snapshot(sock: socket.socket) -> dict:
    """Full keyspace snapshot: {key: (type, value-repr)} for exact comparison.
    Exact rather than "the keys I remember writing", because eviction picks its
    own victims and the post-restart keyspace must match whatever it chose."""
    snap = {}
    for k in sorted(cmd(sock, "KEYS") or []):
        t = cmd(sock, "TYPE", k)
        if t == "string":
            v = cmd(sock, "GET", k)
        elif t == "zset":
            v = tuple((m, cmd(sock, "ZSCORE", k, m)) for m in ("a", "b", "c"))
        elif t == "hash":
            v = tuple(cmd(sock, "HGETALL", k) or [])
        elif t == "set":
            v = tuple(sorted(cmd(sock, "SMEMBERS", k) or []))
        elif t == "list":
            v = tuple(cmd(sock, "LRANGE", k, "0", "-1") or [])
        else:
            v = f"<{t}>"
        snap[k] = (t, v)
    return snap


def phase_aof_gating(ctx: "PhaseCtx"):
    """What reaches the AOF and what must not.

    The gate is the whole point: a read in the log is a correctness bug on
    replay (it re-runs as a write on a replica), and a no-op write in the log
    means the command's "did anything change?" test is wrong.
    """
    ctx.section("Persistence: AOF write gating")
    port = ctx.port()
    d = ctx.dir("aof-gate")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        "appendonly yes",
        "appendfilename appendonly.aof",
        # 'always' rather than 'everysec': the assertions are about the bytes on
        # disk, and a 1s window would turn every one of them into a race.
        "appendfsync always",
        "dbfilename dump.rdb",
        'save ""',
    ])
    aof = os.path.join(d, "appendonly.aof")
    srv = ctx.start("aof-gate", d, conf, port)
    s = raw_conn(port)

    cmd(s, "SET", "gate:foo", "bar")
    cmd(s, "INCR", "gate:counter")
    cmd(s, "GET", "gate:foo")                       # read  -> must NOT be logged
    cmd(s, "SETEX", "gate:sess", "100", "hi")       # write -> SET + PEXPIREAT
    cmd(s, "SETNX", "gate:foo", "NOPE")             # no-op -> must NOT be logged
    cmd(s, "DEL", "gate:missing")                   # no-op -> must NOT be logged
    cmd(s, "EXPIRE", "gate:foo", "5000")            # -> absolute PEXPIREAT

    ctx.ok("AOF exists and is non-empty",
           _aof_settled(s, aof, "gate"),
           f"nothing landed in {aof}")
    blob = _aof_bytes(aof)

    ctx.ok("the write was logged", _frame("gate:foo") in blob)
    ctx.ok("[REG] a read is never logged", _frame("get") not in blob,
           "a GET frame reached the AOF — on replay it would run as a command "
           "against a loading keyspace")
    ctx.ok("[REG] a failed SETNX is not logged", _frame("NOPE") not in blob,
           "SETNX that changed nothing still wrote a frame — the no-op gate is "
           "not consulted")
    ctx.ok("[REG] a DEL of a missing key is not logged",
           _frame("gate:missing") not in blob,
           "DEL of a nonexistent key wrote a frame")

    # SETEX must decompose: a relative TTL replayed verbatim would restart the
    # clock on every boot and the key would never expire.
    ctx.ok("SETEX is logged as SET + absolute PEXPIREAT",
           _frame("SET") in blob and _frame("PEXPIREAT") in blob,
           "no SET/PEXPIREAT pair in the AOF")
    ctx.ok("SETEX's relative TTL is not in the log", _frame("setex") not in blob,
           "the verbatim SETEX frame is there — its TTL restarts on every replay")
    ctx.ok("EXPIRE is rewritten to an absolute PEXPIREAT",
           _frame("expire") not in blob and blob.count(_frame("PEXPIREAT")) >= 2,
           f"PEXPIREAT frames found: {blob.count(_frame('PEXPIREAT'))}")

    # ordering: the value has to exist before its deadline is applied
    ctx.ok("PEXPIREAT follows the SET it belongs to",
           blob.find(_frame("SET")) < blob.find(_frame("PEXPIREAT")),
           "the deadline frame precedes the value frame")

    # the TTL survives a restart as an absolute deadline
    ttl_before = cmd(s, "TTL", "gate:sess")
    s.close()
    srv = ctx.restart(srv, "aof-gate-replay")
    s = raw_conn(port)
    _replay_log_clean(ctx, srv, "replay")
    ttl_after = cmd(s, "TTL", "gate:sess")
    ctx.ok("SETEX ttl survived the restart without resetting",
           isinstance(ttl_after, int) and 0 < ttl_after <= (ttl_before or 100),
           f"before={ttl_before} after={ttl_after}")
    ctx.ok("INCR replayed to the same value", cmd(s, "GET", "gate:counter") == "1",
           f"got {cmd(s, 'GET', 'gate:counter')!r}")
    s.close()
    srv.stop()


def phase_aof_rewrite(ctx: "PhaseCtx"):
    """BGREWRITEAOF: manual and automatic, and what the compacted file has to
    still reconstruct."""
    ctx.section("Persistence: BGREWRITEAOF (manual + auto-trigger)")
    port = ctx.port()
    d = ctx.dir("aof-rewrite")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        "appendonly yes",
        "appendfilename appendonly.aof",
        "appendfsync always",
        "dbfilename dump.rdb",
        'save ""',
        # The auto-trigger is deliberately LEFT AT ITS DEFAULT (64 MB floor)
        # for the manual half. Arming it here would let a background rewrite
        # land in the middle of the measurement, and then "the file did not
        # shrink" means "it had already been compacted", not a bug.
    ])
    aof = os.path.join(d, "appendonly.aof")
    srv = ctx.start("aof-rewrite", d, conf, port)
    s = raw_conn(port)

    # bloat: one key rewritten 2000 times is 2000 frames that compact to one
    for i in range(2000):
        cmd(s, "SET", "rw:k", str(i))
    cmd(s, "RPUSH", "rw:list", "a", "b", "c", "d", "e")
    cmd(s, "HSET", "rw:h", "f1", "v1", "f2", "v2")
    cmd(s, "SADD", "rw:s", "m1", "m2", "m3")
    # The TTL goes on a key nothing overwrites afterwards: SET discards a TTL by
    # design, so putting it on rw:k would make the test assert Redis semantics
    # are broken when they are working.
    cmd(s, "SET", "rw:ttl", "keepme")
    cmd(s, "EXPIRE", "rw:ttl", "10000")
    _aof_settled(s, aof, "rewrite")
    before = len(_aof_bytes(aof))

    cmd(s, "BGREWRITEAOF")
    got_hybrid = _wait_for_hybrid(aof)
    ctx.ok("manual BGREWRITEAOF produced a hybrid file (MYAOFRDB preamble)",
           got_hybrid, f"first 8 bytes: {_aof_bytes(aof)[:8]!r}")
    after = len(_aof_bytes(aof))
    ctx.ok(f"the rewrite compacted the log ({before} -> {after} bytes)",
           after < before, f"before={before} after={after}")

    # Finalize renames the temp over the AOF, so a .tmp during the rewrite is
    # correct and only one that OUTLIVES it is a leak. Poll for its absence.
    def _no_tmp():
        return not [f for f in os.listdir(d) if f.endswith(".tmp")]
    ctx.ok("[REG] the rewrite leaves no temp file behind",
           wait_until(_no_tmp, 10.0),
           f"leftovers: {[f for f in os.listdir(d) if f.endswith('.tmp')]}")
    ctx.ok("[REG] no misspelled AOF file was created",
           not [f for f in os.listdir(d) if f.startswith("appebdonly")],
           f"found: {[f for f in os.listdir(d) if f.startswith('appebdonly')]}")

    # Auto-trigger: arm it only now, then keep writing until it fires.
    #
    # The needle is the server's own "auto-trigger" line, which `server_cron`
    # prints immediately before calling `aof_rewrite_background()`. Three
    # properties of that call site decide the shape of this test:
    #
    #   - The whole block is gated on `g_aof_child_pid == -1 && g_rdb_child_pid
    #     == -1`, so while ANY fork is in flight the condition is not even
    #     evaluated.
    #   - It is rate-limited to once a second, and the clock only advances when
    #     the block actually runs.
    #   - `aof_rewrite_background()` itself can still bail with "already
    #     running" and never log "started" — so "started" is the wrong needle;
    #     "auto-trigger" is emitted unconditionally once the condition holds.
    #
    # Which is why the writes continue INSIDE the wait. A fixed burst followed
    # by a passive wait asks the trigger to fire in one specific window: if a
    # rewrite completes during the burst it resets the base, and with the writes
    # stopped no further growth is ever generated, so the deadline expires
    # against a server behaving perfectly. Writing throughout regenerates growth
    # continuously, and then the only way to miss is a trigger that genuinely
    # does not work. (This is the third shape of this check. The first asserted
    # the file shrank — a coin flip, because the sample lands at an arbitrary
    # point in some later growth cycle. The second watched the wrong log line
    # and still failed under load only.)
    auto_needle = "aof_rewrite: auto-trigger"
    mark = srv.stderr_text().count(auto_needle)
    cmd(s, "CONFIG", "SET", "auto-aof-rewrite-min-size", "4096")
    cmd(s, "CONFIG", "SET", "auto-aof-rewrite-percentage", "100")

    fired = False
    churn = 0
    deadline = time.time() + 30.0
    while time.time() < deadline:
        for _ in range(200):
            cmd(s, "SET", "rw:churn", str(churn))
            churn += 1
        if srv.stderr_text().count(auto_needle) > mark:
            fired = True
            break
        time.sleep(0.2)          # let server_cron reach its once-a-second check
    ctx.ok("the auto-trigger fired at the configured growth percentage", fired,
           f"{churn} writes over {30.0:.0f}s never tripped "
           f"auto-aof-rewrite-percentage=100 above a 4096-byte floor.\n"
           f"    {_rewrite_diagnostics(s, srv, aof)}")

    # Disarm before the restart: a rewrite still in flight when the server stops
    # is a race between finalize and shutdown, and it belongs to a different test
    # than the one below.
    cmd(s, "CONFIG", "SET", "auto-aof-rewrite-min-size", "67108864")
    wait_until(_no_tmp, 10.0)

    ttl_before = cmd(s, "TTL", "rw:ttl")
    ctx.ok("the TTL is still live before the restart",
           isinstance(ttl_before, int) and ttl_before > 0, f"ttl={ttl_before}")
    churn_last = cmd(s, "GET", "rw:churn")
    s.close()
    srv = ctx.restart(srv, "aof-rewrite-replay")
    s = raw_conn(port)
    _replay_log_clean(ctx, srv, "post-rewrite replay")
    ctx.ok("the compacted file reconstructs the string",
           cmd(s, "GET", "rw:k") == "1999", f"got {cmd(s, 'GET', 'rw:k')!r}")
    # rw:churn was written after the last auto-rewrite, so it is the delta the
    # preamble does not contain — the half a hybrid AOF loses silently.
    ctx.ok("the writes made after the last auto-rewrite survived too",
           cmd(s, "GET", "rw:churn") == churn_last,
           f"before={churn_last!r} after={cmd(s, 'GET', 'rw:churn')!r}")
    ttl_after = cmd(s, "TTL", "rw:ttl")
    ctx.ok("[REG] the TTL survived the rewrite as an absolute deadline",
           isinstance(ttl_after, int) and 0 < ttl_after <= (ttl_before or 10000),
           f"before={ttl_before} after={ttl_after} — a rewrite that emits no "
           f"PEXPIREAT loses every TTL in the snapshot")
    ctx.ok("compacted file reconstructs the list",
           cmd(s, "LRANGE", "rw:list", "0", "-1") == ["a", "b", "c", "d", "e"])
    ctx.ok("compacted file reconstructs the hash",
           cmd(s, "HGET", "rw:h", "f1") == "v1")
    ctx.ok("compacted file reconstructs the set", cmd(s, "SCARD", "rw:s") == 3)
    s.close()
    srv.stop()


def phase_aof_hybrid(ctx: "PhaseCtx"):
    """The hybrid format: RDB preamble + RESP delta.

    Three distinct failure modes live here, and only the third is loud:
      - the delta written after a rewrite is lost (silent: the preamble loads
        and the dataset looks plausible, just older),
      - a plain RESP file with no preamble stops loading (breaks every existing
        deployment on upgrade),
      - a torn tail eats the preamble instead of truncating (total data loss).
    """
    ctx.section("Persistence: hybrid AOF (RDB preamble + RESP delta)")
    port = ctx.port()
    d = ctx.dir("aof-hybrid")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        "appendonly yes",
        "appendfilename appendonly.aof",
        "appendfsync always",
        "dbfilename dump.rdb",
        'save ""',
    ])
    aof = os.path.join(d, "appendonly.aof")
    srv = ctx.start("aof-hybrid", d, conf, port)
    s = raw_conn(port)

    cmd(s, "SET", "hy:s", "hello")
    cmd(s, "EXPIRE", "hy:s", "9000")
    cmd(s, "RPUSH", "hy:list", "a", "b", "c", "d")
    cmd(s, "HSET", "hy:h", "f1", "v1", "f2", "v2")
    cmd(s, "SADD", "hy:set", "x", "y", "z")
    cmd(s, "ZADD", "hy:z", "1", "a", "2", "b", "3", "c")
    cmd(s, "BGREWRITEAOF")
    ctx.ok("rewrite produced the MYAOFRDB preamble", _wait_for_hybrid(aof),
           f"first 8 bytes: {_aof_bytes(aof)[:8]!r}")

    # the delta: written AFTER the rewrite, so it appends as RESP behind the RDB
    cmd(s, "SET", "hy:delta", "123")
    cmd(s, "LPUSH", "hy:list", "FRONT")
    _aof_settled(s, aof, "delta")
    s.close()

    srv = ctx.restart(srv, "aof-hybrid-replay")
    s = raw_conn(port)
    err = srv.stderr_text()
    ctx.ok("stderr reports loading the RDB preamble",
           "RDB preamble" in err or "preamble" in err,
           f"no preamble line in {srv.stderr_path}")
    ctx.ok("preamble restored the string", cmd(s, "GET", "hy:s") == "hello")
    ctx.ok("preamble restored the TTL",
           isinstance(cmd(s, "TTL", "hy:s"), int) and cmd(s, "TTL", "hy:s") > 0)
    ctx.ok("preamble restored the hash", cmd(s, "HGET", "hy:h", "f2") == "v2")
    ctx.ok("preamble restored the set", cmd(s, "SCARD", "hy:set") == 3)
    ctx.ok("preamble restored the zset",
           as_float(cmd(s, "ZSCORE", "hy:z", "b")) == 2.0)
    ctx.ok("[REG] the RESP delta replayed on top of the preamble",
           cmd(s, "GET", "hy:delta") == "123",
           "the preamble loaded but the delta written after the rewrite is "
           "gone — the silent half of the hybrid bug")
    ctx.ok("delta ordering survived (LPUSH landed at the front)",
           cmd(s, "LRANGE", "hy:list", "0", "-1")
           == ["FRONT", "a", "b", "c", "d"])
    s.close()
    srv.stop()

    # backward compatibility: a plain RESP AOF with no preamble must still load
    os.remove(aof)
    for junk in ("dump.rdb",):
        p = os.path.join(d, junk)
        if os.path.exists(p):
            os.remove(p)
    with open(aof, "wb") as f:
        f.write(b"*3\r\n$3\r\nset\r\n$6\r\ncompat\r\n$3\r\nyes\r\n")
    srv = ctx.start("aof-compat", d, conf, port)
    s = raw_conn(port)
    ctx.ok("[REG] a plain RESP AOF with no preamble still loads",
           cmd(s, "GET", "compat") == "yes",
           "a pre-hybrid AOF stopped loading — every existing deployment would "
           "come up empty on upgrade")
    _replay_log_clean(ctx, srv, "plain-RESP load")
    s.close()
    srv.stop()

    # torn tail: a half-written command must truncate, not condemn the preamble
    os.remove(aof)
    p = os.path.join(d, "dump.rdb")
    if os.path.exists(p):
        os.remove(p)
    srv = ctx.start("aof-torn-seed", d, conf, port)
    s = raw_conn(port)
    cmd(s, "SET", "hy:keep", "me")
    cmd(s, "BGREWRITEAOF")
    made = _wait_for_hybrid(aof)
    s.close()
    srv.stop()
    if not made:
        ctx.skip("torn-tail recovery",
                 "the rewrite never produced a hybrid file, so the test would "
                 "not be exercising the preamble path")
        return
    with open(aof, "ab") as f:
        f.write(b"*3\r\n$3\r\nset")          # half a command, as a crash leaves it
    srv = ctx.start("aof-torn", d, conf, port)
    s = raw_conn(port)
    ctx.ok("[REG] a torn RESP tail truncates and leaves the preamble intact",
           cmd(s, "GET", "hy:keep") == "me",
           "the partial trailing frame took the whole file down with it")
    ctx.ok("server is serving after recovering from the torn tail",
           cmd(s, "PING") == "PONG")
    s.close()
    srv.stop()


def phase_restart_matrix(ctx: "PhaseCtx"):
    """Every write path whose AOF frame differs from the command that caused it.

    A command that logs itself verbatim is uninteresting here. These are the
    ones that translate — a relative TTL into an absolute deadline, an alias
    into its canonical name, a pop into a delete — because a translation is
    where the replay and the live reply can silently disagree.
    """
    ctx.section("Persistence: restart matrix (translated AOF frames)")
    password = "restart-matrix-pass"
    port = ctx.port()
    d = ctx.dir("restart-matrix")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        f'requirepass "{password}"',
        "appendonly yes",
        "appendfilename appendonly.aof",
        "appendfsync everysec",
        "dbfilename dump.rdb",
        'save ""',
        "rename-command getdel gdel",
    ])
    aof = os.path.join(d, "appendonly.aof")
    srv = ctx.start("restart-matrix", d, conf, port)
    s = raw_conn(port, password)

    # Eviction FIRST: allkeys-random can take ANY key, so the sentinels for the
    # checks below must not exist yet — only the ev:* fodder may be sacrificed.
    val = "e" * 400
    for i in range(200):
        cmd(s, "SET", f"ev:{i}", val)
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "allkeys-random")
    cmd(s, "CONFIG", "SET", "maxmemory", "65536")
    for i in range(200, 300):
        try:
            cmd(s, "SET", f"ev:{i}", val)
        except RespError:
            pass                        # OOM is fine; we only need evictions
    time.sleep(0.5)                     # let evict_tick drain
    cmd(s, "CONFIG", "SET", "maxmemory", "0")
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "noeviction")
    dbsize = cmd(s, "DBSIZE")
    ctx.ok("eviction actually removed keys", dbsize < 300, f"dbsize={dbsize}")

    cmd(s, "SET", "rm:str", "v1")

    # GETEX EX: the TTL must be logged as an absolute deadline
    cmd(s, "SET", "rm:ttl", "vttl")
    ctx.ok("GETEX rm:ttl ex 100 returns the value",
           cmd(s, "GETEX", "rm:ttl", "ex", "100") == "vttl")
    ttl1 = cmd(s, "TTL", "rm:ttl")
    ctx.ok("GETEX set a TTL", isinstance(ttl1, int) and 0 < ttl1 <= 100,
           f"ttl={ttl1}")

    # GETDEL exists only under its alias; the AOF must carry the canonical name
    cmd(s, "SET", "rm:gone", "bye")
    ctx.ok("canonical 'getdel' is renamed away",
           err_of(s, "GETDEL", "rm:gone") is not None)
    ctx.ok("alias gdel returns the value", cmd(s, "gdel", "rm:gone") == "bye")
    ctx.ok("gdel deleted the key", cmd(s, "EXISTS", "rm:gone") == 0)

    # ZPOPMIN: the popped member must stay popped
    cmd(s, "ZADD", "rm:z", "1", "a", "2", "b", "3", "c")
    cmd(s, "ZPOPMIN", "rm:z")
    ctx.ok("zpopmin removed the min member", cmd(s, "ZSCORE", "rm:z", "a") is None)
    ctx.ok("zpopmin kept b", cmd(s, "ZSCORE", "rm:z", "b") is not None)

    # SPOP: aof_self, and the case that had no coverage anywhere until now. SPOP
    # picks its victim at random, so the frame it logs cannot be the command it
    # received — it has to log the member it actually removed.
    cmd(s, "SADD", "rm:spop", "m0", "m1", "m2", "m3", "m4")
    popped = cmd(s, "SPOP", "rm:spop")
    ctx.ok("SPOP returned a member", popped in ("m0", "m1", "m2", "m3", "m4"),
           f"got {popped!r}")
    ctx.ok("SPOP left 4 members", cmd(s, "SCARD", "rm:spop") == 4)
    survivors = tuple(sorted(cmd(s, "SMEMBERS", "rm:spop") or []))

    # SPOP with a count, and SPOP that empties the key — the empty-key path is
    # the one that produced a malformed frame once already (via SREM).
    cmd(s, "SADD", "rm:spopall", "a", "b", "c")
    cmd(s, "SPOP", "rm:spopall", "3")
    ctx.ok("SPOP <count> emptied the set", cmd(s, "EXISTS", "rm:spopall") == 0)

    # SREM down to empty: the frame that shipped broken from V9.6.4 to V10
    cmd(s, "SADD", "rm:srem", "only")
    cmd(s, "SREM", "rm:srem", "only")
    ctx.ok("SREM to empty removed the key", cmd(s, "EXISTS", "rm:srem") == 0)

    cmd(s, "HSET", "rm:h", "f", "v")
    cmd(s, "LPUSH", "rm:l", "x", "y", "z")

    before = _snapshot(s)
    ttl_before = cmd(s, "TTL", "rm:ttl")
    s.close()
    srv.stop()
    ctx.ok("AOF exists and is non-empty",
           os.path.exists(aof) and os.path.getsize(aof) > 0)

    srv = ctx.start("restart-matrix-replay", d, conf, port)
    s = raw_conn(port, password)
    _replay_log_clean(ctx, srv, "restart")

    after = _snapshot(s)
    ctx.ok("keyspace size matches pre-shutdown", len(after) == len(before),
           f"{len(before)} -> {len(after)}")
    missing = [k for k in before if k not in after]
    extra = [k for k in after if k not in before]
    ctx.ok("no key lost by replay", not missing, f"missing: {missing[:5]}")
    ctx.ok("no key resurrected by replay", not extra, f"extra: {extra[:5]}")
    diff = [k for k in before if k in after and before[k] != after[k]]
    ctx.ok("every surviving value is identical", not diff,
           f"first diff: {diff[0] if diff else ''} "
           f"{before.get(diff[0]) if diff else ''} != "
           f"{after.get(diff[0]) if diff else ''}")

    ttl2 = cmd(s, "TTL", "rm:ttl")
    ctx.ok("GETEX ttl survived within its original bound",
           isinstance(ttl2, int) and 0 < ttl2 <= (ttl_before or 100),
           f"ttl before={ttl_before} after={ttl2}")
    ctx.ok("gdel'd key is still gone", cmd(s, "EXISTS", "rm:gone") == 0)
    ctx.ok("zpopmin'd member is still gone", cmd(s, "ZSCORE", "rm:z", "a") is None)
    ctx.ok("[REG] the SPOP'd member is still gone after replay",
           tuple(sorted(cmd(s, "SMEMBERS", "rm:spop") or [])) == survivors,
           f"before={survivors} after="
           f"{tuple(sorted(cmd(s, 'SMEMBERS', 'rm:spop') or []))} — SPOP logged "
           f"the command instead of the member it actually removed, so the "
           f"replay popped a different one")
    ctx.ok("[REG] the set SPOP emptied stayed empty",
           cmd(s, "EXISTS", "rm:spopall") == 0)
    ctx.ok("[REG] the set SREM emptied stayed empty",
           cmd(s, "EXISTS", "rm:srem") == 0)
    ctx.ok("the alias still resolves after restart",
           cmd(s, "SET", "rm:gone2", "x") == "OK"
           and cmd(s, "gdel", "rm:gone2") == "x")

    if ctx.destructive:
        ctx.section("Persistence: crash recovery (SIGKILL mid-traffic)")
        for i in range(50):
            cmd(s, "SET", f"crash:{i}", "x")
        s.close()
        srv.kill9()                     # no shutdown save, AOF tail may be torn
        try:
            srv = ctx.start("crash-reboot", d, conf, port)
            booted = True
        except RuntimeError as e:
            booted = False
            ctx.ok("server boots after SIGKILL", False, str(e))
        if booted:
            ctx.ok("server boots after SIGKILL", True)
            s = raw_conn(port, password)
            n = sum(1 for i in range(50)
                    if cmd(s, "EXISTS", f"crash:{i}") == 1)
            # everysec fsync means a partial tail is CORRECT, not a failure —
            # what must hold is that the boot succeeds and the pre-crash data
            # is intact.
            ctx.ok("the pre-crash keyspace is intact after the crash boot",
                   cmd(s, "GET", "rm:str") == "v1",
                   "data written long before the crash did not come back")
            print(f"  {YELLOW}info{RESET} crash writes recovered: {n}/50 "
                  f"(everysec fsync makes fewer than 50 correct)")
            s.close()
    else:
        s.close()
    srv.stop()


def phase_rdb_roundtrip(ctx: "PhaseCtx"):
    """RDB alone, with the AOF off: SAVE, restart, and every type has to come
    back — including the TTL, which is the field an RDB writer forgets."""
    ctx.section("Persistence: RDB save/load round-trip")
    port = ctx.port()
    d = ctx.dir("rdb")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        "appendonly no",
        "dbfilename dump.rdb",
        'save ""',
    ])
    srv = ctx.start("rdb", d, conf, port)
    s = raw_conn(port)

    cmd(s, "SET", "rdb:s", "value")
    cmd(s, "EXPIRE", "rdb:s", "8000")
    cmd(s, "RPUSH", "rdb:l", "a", "b", "c")
    cmd(s, "HSET", "rdb:h", "f", "v")
    cmd(s, "SADD", "rdb:set", "m1", "m2")
    cmd(s, "ZADD", "rdb:z", "1.5", "a", "2.5", "b")
    ctx.ok("SAVE returns OK", cmd(s, "SAVE") == "OK")
    ctx.ok("dump.rdb was written",
           os.path.getsize(os.path.join(d, "dump.rdb")) > 0)
    before = _snapshot(s)
    ttl_before = cmd(s, "TTL", "rdb:s")
    s.close()

    srv = ctx.restart(srv, "rdb-load")
    s = raw_conn(port)
    after = _snapshot(s)
    ctx.ok("every key came back from the RDB", set(after) == set(before),
           f"missing: {sorted(set(before) - set(after))} "
           f"extra: {sorted(set(after) - set(before))}")
    diff = [k for k in before if k in after and before[k] != after[k]]
    ctx.ok("every value came back identical", not diff,
           f"first diff: {diff[0] if diff else ''}")
    ttl_after = cmd(s, "TTL", "rdb:s")
    ctx.ok("the TTL came back from the RDB as an absolute deadline",
           isinstance(ttl_after, int) and 0 < ttl_after <= (ttl_before or 8000),
           f"before={ttl_before} after={ttl_after}")
    ctx.ok("zset scores survived at full precision",
           as_float(cmd(s, "ZSCORE", "rdb:z", "a")) == 1.5)
    s.close()
    srv.stop()


def phase_persistence(ctx: "PhaseCtx"):
    phase_aof_gating(ctx)
    phase_aof_rewrite(ctx)
    phase_aof_hybrid(ctx)
    phase_rdb_roundtrip(ctx)
    phase_restart_matrix(ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: SECURITY — ACL enforcement, renamed commands, audit log, protocol abuse
#
#  The instance here is disposable on purpose: several of these checks lock a
#  connection out, rewrite the config file, and (with --destructive) throw
#  malformed frames at the parser. None of that belongs anywhere near a server
#  somebody is using.
# ═══════════════════════════════════════════════════════════════════════════════

SEC_ADMIN_PW = "sec-admin-pass"
SEC_LIMITED_PW = "sec-limited-pass"
SEC_KEYED_PW = "sec-keyed-pass"
SEC_SMOVER_PW = "sec-smover-pass"
SEC_WRONG_PW = "sec-wrong-pass-attempt"
SEC_ALL_SECRETS = [SEC_ADMIN_PW, SEC_LIMITED_PW, SEC_KEYED_PW, SEC_SMOVER_PW,
                   SEC_WRONG_PW]

ACL_CATEGORIES = ["read", "write", "keyspace", "admin", "dangerous", "fast",
                  "slow", "connection", "transaction"]


def _noperm(sock: socket.socket, *args: str) -> bool:
    """True iff the command is refused specifically with NOPERM. A plain -ERR
    is not good enough: an unknown command would also 'fail'."""
    e = err_of(sock, *args)
    return e is not None and "NOPERM" in e


def phase_security(ctx: "PhaseCtx"):
    ctx.section("Security: ACL enforcement, renames, audit log")
    port = ctx.port()
    d = ctx.dir("security")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        f'requirepass "{SEC_ADMIN_PW}"',
        'auditlog "audit.log"',
        "appendonly no",
        "dbfilename dump.rdb",
        'save ""',
        "rename-command flushall wipeall",
        'rename-command object ""',
    ])
    audit_path = os.path.join(d, "audit.log")
    srv = ctx.start("security", d, conf, port)
    admin = raw_conn(port, SEC_ADMIN_PW)

    ctx.ok("setuser limited (+@read +@write)",
           cmd(admin, "ACL", "SETUSER", "limited", "on", f">{SEC_LIMITED_PW}",
               "~*", "+@read", "+@write") == "OK")
    ctx.ok("setuser keyed (~data:*)",
           cmd(admin, "ACL", "SETUSER", "keyed", "on", f">{SEC_KEYED_PW}",
               "~data:*", "+@read", "+@write") == "OK")
    ctx.ok("setuser smover (~src:* ~dst:*)",
           cmd(admin, "ACL", "SETUSER", "smover", "on", f">{SEC_SMOVER_PW}",
               "~src:*", "~dst:*", "+@read", "+@write") == "OK")
    ctx.ok("setuser ghost (passwordless — must survive a round-trip)",
           cmd(admin, "ACL", "SETUSER", "ghost", "on", "~*", "+@read") == "OK")

    def seed():
        cmd(admin, "SET", "data:1", "d1")
        cmd(admin, "SET", "other:1", "o1")
        cmd(admin, "SADD", "src:s", "m")
        cmd(admin, "SADD", "src:s2", "m2")
    seed()

    # --- control plane is not reachable from a data-plane grant --------------
    # +@read +@write is the grant every application gets. If any admin command
    # answers to it, the category split does nothing.
    lim = raw_conn(port, SEC_LIMITED_PW, user="limited")
    ctx.ok("limited: GET works", cmd(lim, "GET", "data:1") == "d1")
    ctx.ok("limited: SET works", cmd(lim, "SET", "lim:k", "v") == "OK")
    ctx.ok("limited: CONFIG GET denied", _noperm(lim, "CONFIG", "GET", "maxmemory"))
    ctx.ok("limited: ACL WHOAMI denied", _noperm(lim, "ACL", "WHOAMI"))
    ctx.ok("limited: KEYS denied", _noperm(lim, "KEYS"))
    ctx.ok("limited: MEMORY denied", _noperm(lim, "MEMORY", "USAGE", "data:1"))
    ctx.ok("limited: the flushall alias is denied too", _noperm(lim, "wipeall"),
           "renaming a command must not launder its category")
    lim.close()

    # --- rename-command: canonical gone, alias live, '' disables -------------
    ctx.ok("canonical FLUSHALL is unknown",
           (err_of(admin, "FLUSHALL") or "").startswith("ERR"))
    ctx.ok("a command renamed to '' is unreachable",
           (err_of(admin, "OBJECT", "ENCODING", "data:1") or "").startswith("ERR"))
    cmd(admin, "SET", "wipe:k", "x")
    ctx.ok("the alias works for admin", cmd(admin, "wipeall") == "OK")
    ctx.ok("the alias really flushed", cmd(admin, "EXISTS", "wipe:k") == 0)
    seed()

    # --- audit log: the events are there, the secrets are not ----------------
    anon = raw_conn(port)
    bad = err_of(anon, "AUTH", SEC_WRONG_PW)
    anon.close()
    ctx.ok("a wrong password is rejected", bad is not None)
    time.sleep(0.3)                    # async auth completion writes the event
    try:
        with open(audit_path, "r", errors="replace") as f:
            log = f.read()
    except OSError as e:
        log = ""
        ctx.ok("audit log is readable", False, str(e))
    ctx.ok("audit records auth_success", "event=auth_success" in log)
    ctx.ok("audit records auth_fail", "event=auth_fail" in log)
    ctx.ok("audit records acl_change", "sub=setuser" in log)
    ctx.ok("audit records acl_deny", "event=acl_deny" in log)
    leaked = [p for p in SEC_ALL_SECRETS if p and p in log]
    ctx.ok("[REG] no plaintext password reaches the audit log", not leaked,
           f"leaked: {leaked} — an audit log is copied around freely, so a "
           f"password in it is worse than no log")

    # --- key patterns, including the two-key resolver ------------------------
    kd = raw_conn(port, SEC_KEYED_PW, user="keyed")
    ctx.ok("keyed: a key inside the pattern is allowed",
           cmd(kd, "GET", "data:1") == "d1")
    ctx.ok("keyed: a key outside the pattern is denied",
           _noperm(kd, "GET", "other:1"))
    ctx.ok("keyed: writing outside the pattern is denied",
           _noperm(kd, "SET", "other:2", "x"))
    kd.close()
    sm = raw_conn(port, SEC_SMOVER_PW, user="smover")
    ctx.ok("smover: SMOVE with both keys granted is allowed",
           cmd(sm, "SMOVE", "src:s", "dst:s", "m") == 1)
    ctx.ok("[REG] smover: SMOVE to an ungranted destination is denied",
           _noperm(sm, "SMOVE", "src:s2", "forbidden:d", "m2"),
           "the key resolver only checked the source — a two-key command needs "
           "both sides checked")
    sm.close()

    # --- ACL CAT: advertise and parse are a matched pair ---------------------
    # A category emitted by ACL CAT but not accepted by ACL SETUSER writes a
    # +@cat into the config that the NEXT boot rejects, silently dropping the
    # grant. Adding a category means updating this list.
    cats = cmd(admin, "ACL", "CAT")
    ctx.ok("ACL CAT returns a list", isinstance(cats, list), f"got {cats!r}")
    ctx.ok(f"ACL CAT lists exactly the {len(ACL_CATEGORIES)} categories",
           sorted(cats or []) == sorted(ACL_CATEGORIES), f"got {cats!r}")
    # 'off' first: User::enable defaults to true, so a bare SETUSER would leave
    # an enabled +@admin +@dangerous user behind. Deleted immediately so it
    # never reaches the CONFIG REWRITE below.
    ctx.ok("[REG] every advertised category is also parseable",
           all(cmd(admin, "ACL", "SETUSER", "cattest", "off", f"+@{c}") == "OK"
               for c in (cats or [])),
           f"got {cats!r}")
    cmd(admin, "ACL", "DELUSER", "cattest")

    # --- config round-trip: emit and parse are a matched pair too ------------
    ctx.ok("CONFIG REWRITE returns OK", cmd(admin, "CONFIG", "REWRITE") == "OK")
    with open(conf) as f:
        newconf = f.read()
    ctx.ok("the rewritten config keeps the port", f"port {port}" in newconf)
    ctx.ok("the rewritten config keeps the users", "user limited" in newconf)
    admin.close()
    if f"port {port}" not in newconf:
        ctx.ok("skipping the restart: the rewritten config lost its port",
               False, "restarting would bind the default port instead")
    else:
        srv = ctx.restart(srv, "security-roundtrip")
        admin = raw_conn(port, SEC_ADMIN_PW)
        ctx.ok("admin auth survives the round-trip", cmd(admin, "PING") == "PONG")
        ctx.ok("a passwordless user survives the round-trip",
               cmd(admin, "ACL", "GETUSER", "ghost") is not None)
        lim = raw_conn(port, SEC_LIMITED_PW, user="limited")
        ctx.ok("limited auth survives the round-trip",
               cmd(lim, "GET", "data:1") == "d1")
        ctx.ok("limited is still denied the control plane after the round-trip",
               _noperm(lim, "CONFIG", "GET", "maxmemory"),
               "the grant widened across a rewrite — the emitted rule and the "
               "parsed rule disagree")
        lim.close()
        admin.close()

    if ctx.destructive:
        ctx.section("Security: protocol abuse (server must keep serving)")

        def abuse(name, payload):
            raw = socket.socket()
            raw.settimeout(TIMEOUT_SEC)
            try:
                raw.connect(("127.0.0.1", port))
                raw.sendall(payload)
                raw.recv(256)          # an error reply or a close — both fine
            except OSError:
                pass                   # killing its own connection is allowed
            finally:
                raw.close()
            try:
                probe = raw_conn(port, SEC_ADMIN_PW)
                alive = cmd(probe, "PING") == "PONG"
                probe.close()
            except Exception:
                alive = False
            ctx.ok(f"server survives {name}", alive,
                   "the malformed frame took the whole server down, not just "
                   "its own connection")

        abuse("an absurd multibulk count", b"*99999999\r\n")
        abuse("a garbage array header", b"*abc\r\n")
        abuse("a negative bulk length", b"*1\r\n$-5\r\n")
        abuse("a bulk length that overflows int64",
              b"*1\r\n$99999999999999999999\r\n")
        abuse("an oversized inline line", b"x" * (2 * 1024 * 1024))
        abuse("a key name full of RESP control bytes",
              b"*3\r\n$3\r\nset\r\n$5\r\na\r\nb\r\n$1\r\nv\r\n")
        ctx.ok("the server process is still alive", srv.alive())

    srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: ASYNC AUTH — the KDF runs on a worker, the event loop must not stall
#
#  Argon2id costs tens of milliseconds by design. Verifying it on the event loop
#  would stall every other connection for that long, so the verify runs on a
#  worker and the connection is resumed by a completion. That design has three
#  failure modes and each check below is one of them: commands pipelined behind
#  the AUTH get lost or reordered, a completion is delivered to the wrong
#  connection, or the loop stalls anyway.
# ═══════════════════════════════════════════════════════════════════════════════

AUTH_PW = "async-auth-pass"


class _Hang(Exception):
    """The server accepted the connection and then never replied."""


def _auth_retry(sock: socket.socket, password: str, tries: int = 8) -> str:
    """AUTH, retrying past -BUSY: the inflight cap is a valid, bounded answer,
    not a failure."""
    for _ in range(tries):
        send_request(sock, "auth", password)
        try:
            return recv_response(sock)
        except RespError as e:
            if "BUSY" in str(e):
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError("still -BUSY after retries (the inflight cap never cleared)")


def phase_async_auth(ctx: "PhaseCtx"):
    ctx.section("Auth: async verify, pipelining, lockout, loop latency")
    port = ctx.port()
    d = ctx.dir("auth")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        f'requirepass "{AUTH_PW}"',
        'auditlog "audit.log"',
        "appendonly no",
        'save ""',
    ])
    audit_path = os.path.join(d, "audit.log")
    srv = ctx.start("auth", d, conf, port)

    # --- pipeline gating + resume -------------------------------------------
    # AUTH + PING + SET in ONE packet. The verify runs on a worker, so the two
    # commands behind it must stay buffered (never executed pre-auth) and then
    # be drained in order by the completion path.
    s = socket.create_connection(("127.0.0.1", port), TIMEOUT_SEC)
    s.settimeout(TIMEOUT_SEC)
    try:
        payload = bytearray()
        for args in (("auth", AUTH_PW), ("ping",), ("set", "auth:k", "1")):
            buf = bytearray()
            buf += f"*{len(args)}\r\n".encode()
            for a in args:
                buf += f"${len(a)}\r\n{a}\r\n".encode()
            payload += buf
        s.sendall(bytes(payload))
        r1 = recv_response(s)
        if r1 != "OK":
            ctx.ok("AUTH accepted", False, f"got {r1!r}")
        else:
            r2, r3 = recv_response(s), recv_response(s)
            ctx.ok("[REG] three replies arrive in order (OK, PONG, OK)",
                   r2 == "PONG" and r3 == "OK",
                   f"got PING={r2!r} SET={r3!r} — the commands pipelined behind "
                   f"AUTH were dropped or reordered")
            send_request(s, "get", "auth:k")
            ctx.ok("the pipelined SET actually executed with the authed identity",
                   recv_response(s) == "1")
    except OSError as e:
        ctx.ok("pipelined AUTH did not hang", False, f"{type(e).__name__}: {e}")
    finally:
        try:
            s.close()
        except OSError:
            pass

    # --- wrong password + lockout -------------------------------------------
    s = socket.create_connection(("127.0.0.1", port), TIMEOUT_SEC)
    s.settimeout(TIMEOUT_SEC)
    e = err_of(s, "auth", "definitely-wrong")
    ctx.ok("a wrong password answers WRONGPASS",
           e is not None and "WRONGPASS" in e, f"got {e!r}")
    closed = False
    for _ in range(40):
        try:
            send_request(s, "auth", "definitely-wrong")
            recv_response(s)
        except RespError:
            continue                       # WRONGPASS / BUSY: keep going
        except (ConnectionError, OSError):
            closed = True                  # the server hung up: correct
            break
    ctx.ok("the connection is closed after repeated auth failures", closed,
           "the server answered forever — k_max_failed_auth never terminated "
           "the connection")
    try:
        s.close()
    except OSError:
        pass

    # --- completion delivery under concurrency ------------------------------
    N = 8
    results = [None] * N

    def worker(i):
        try:
            w = socket.create_connection(("127.0.0.1", port), TIMEOUT_SEC)
            w.settimeout(TIMEOUT_SEC)
            results[i] = _auth_retry(w, AUTH_PW)
            send_request(w, "ping")
            if recv_response(w) != "PONG":
                results[i] = "ping-failed-after-auth"
            w.close()
        except Exception as ex:
            results[i] = f"{type(ex).__name__}: {ex}"

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=TIMEOUT_SEC * 4)
    got = sum(1 for r in results if r == "OK")
    ctx.ok(f"all {N} concurrent AUTHs completed", got == N,
           f"results={results} — a lost or misrouted completion leaves its "
           f"connection hanging forever")

    # --- the loop keeps answering while the KDF is busy ---------------------
    stop = threading.Event()

    def storm():
        while not stop.is_set():
            try:
                w = socket.create_connection(("127.0.0.1", port), TIMEOUT_SEC)
                w.settimeout(TIMEOUT_SEC)
                for _ in range(3):
                    if stop.is_set():
                        break
                    try:
                        send_request(w, "auth", "wrong-pw")
                        recv_response(w)
                    except RespError:
                        pass               # WRONGPASS / BUSY are expected
                w.close()
            except Exception:
                time.sleep(0.05)

    stormers = [threading.Thread(target=storm, daemon=True) for _ in range(4)]
    for t in stormers:
        t.start()
    lat = []
    try:
        s = raw_conn(port, AUTH_PW)
        for _ in range(200):
            t0 = time.perf_counter()
            send_request(s, "ping")
            recv_response(s)
            lat.append((time.perf_counter() - t0) * 1000.0)
        s.close()
    except Exception as ex:
        ctx.ok("the event loop answered during the AUTH storm", False,
               f"{type(ex).__name__}: {ex}")
    finally:
        stop.set()
        for t in stormers:
            t.join(timeout=2.0)

    ctx.ok("all 200 PINGs were answered during the AUTH storm", len(lat) == 200,
           f"only {len(lat)} came back")
    if lat:
        lat.sort()
        p50 = _percentile(lat, 0.50)
        p99 = _percentile(lat, 0.99)
        print(f"  {YELLOW}info{RESET} PING during an AUTH storm: "
              f"p50={p50:.2f}ms p99={p99:.2f}ms")
        print(f"         a synchronous Argon2 verify would put p99 at 20-60ms+; "
              f"that gap is the whole point of the async path.")

    # --- credential rehash + redaction --------------------------------------
    # Two sequential AUTHs on fresh connections. If the first triggered a
    # rehash (legacy digest -> Argon2id), the second proves the swapped-in
    # credential still verifies.
    for i in (1, 2):
        w = raw_conn(port, AUTH_PW)
        ctx.ok(f"AUTH #{i} accepted (the credential survives any rehash)",
               cmd(w, "PING") == "PONG")
        w.close()
    try:
        with open(audit_path, errors="replace") as f:
            data = f.read()
    except OSError:
        data = None
    if data is None:
        ctx.skip("audit redaction", "the audit log was not written")
    else:
        legacy = hashlib.sha256(AUTH_PW.encode()).hexdigest()
        leaked = ("$argon2id$" in data) or (AUTH_PW in data) or (legacy in data)
        ctx.ok("[REG] the audit log carries no plaintext, digest, or PHC hash",
               not leaked,
               "a credential in any form reached the audit log")
        n = data.count("event=cred_rehash")
        print(f"  {YELLOW}info{RESET} cred_rehash events this lifetime: {n} "
              f"(0 is correct for a credential already stored as Argon2id)")

    srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: CONFIG ROUND-TRIP — what CONFIG REWRITE writes must be what boots
#
#  The live-server CONFIG section above writes a value and reads it back inside
#  one process. That catches a getter wired to the wrong field, and it cannot
#  catch anything else: a directive whose *emit* is broken reads back perfectly
#  for as long as the process lives, and only loses its value at the next boot.
#  Making the value cross a restart is the only way to test the emit at all.
# ═══════════════════════════════════════════════════════════════════════════════

# Directives whose value is set at runtime, rewritten, and then has to come back
# after a restart. Each probe is deliberately DIFFERENT from the default, since
# a value that matches the default survives a completely missing emit.
REWRITE_PROBES = [
    ("maxmemory-samples", "7"),
    ("maxmemory-policy", "allkeys-random"),
    ("maxmemory", "12345678"),
    ("appendfsync", "always"),
    ("auto-aof-rewrite-percentage", "77"),
    ("auto-aof-rewrite-min-size", "12345678"),
    ("notify-keyspace-events", "AKE"),
    ("repl-timeout", "77"),
    ("repl-ping-replica-period", "13"),
    ("min-replicas-to-write", "0"),
    ("min-replicas-max-lag", "17"),
]

# Boot-only directives: CONFIG SET must refuse them, so the only way to probe
# their emit is to set them in the file the server boots from and make the value
# cross a rewrite. Both of these own a hand-written emit, and a hand-written
# emit is the one thing config_selfcheck's round-trip deliberately skips.
BOOT_ONLY_PROBES = [
    ("repl-backlog-size", "1048576"),
    ("tls-handshake-timeout", "45"),
]

CFG_PW = "config-roundtrip-pass"


def phase_config_roundtrip(ctx: "PhaseCtx"):
    ctx.section("Config: CONFIG REWRITE survives a restart")
    port = ctx.port()
    d = ctx.dir("config-rt")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        f'requirepass "{CFG_PW}"',
        "appendonly no",
        "dbfilename dump.rdb",
        'save ""',
    ] + [f"{n} {v}" for n, v in BOOT_ONLY_PROBES])
    srv = ctx.start("config-rt", d, conf, port)
    s = raw_conn(port, CFG_PW)

    # A boot-only directive must refuse CONFIG SET — that refusal IS the
    # contract, so it is asserted rather than skipped past.
    for name, want in BOOT_ONLY_PROBES:
        if not has_directive(s, name):
            ctx.skip(f"{name} round-trip", "this binary has no such directive")
            continue
        ctx.ok(f"{name} read its boot value from the config file",
               get_directive(s, name) == want,
               f"got {get_directive(s, name)!r}, wrote {want!r}")
        ctx.ok(f"{name} is boot-only and refuses CONFIG SET",
               err_of(s, "CONFIG", "SET", name, want) is not None,
               "a boot-only directive that accepts CONFIG SET reports a value "
               "the running server is not actually using")

    # A channel ACL, because its emitted form is where two tokens once fused.
    cmd(s, "ACL", "SETUSER", "chan", "on", ">chanpass", "resetchannels",
        "&news.*", "~*", "+@read")
    cmd(s, "ACL", "SETUSER", "allch", "on", ">allchpass", "allchannels", "~*",
        "+@read")

    # Set each probe and record what the RUNNING server says the value is. That
    # — not the literal we sent — is what must survive, so a directive that
    # normalizes its input (notify-keyspace-events reorders its flags) is
    # compared against its own normalized form instead of failing spuriously.
    live = {}
    for name, probe in REWRITE_PROBES:
        if not has_directive(s, name):
            ctx.skip(f"{name} round-trip", "this binary has no such directive")
            continue
        try:
            cmd(s, "CONFIG", "SET", name, probe)
        except RespError as e:
            ctx.ok(f"CONFIG SET {name} {probe}", False, str(e))
            continue
        live[name] = get_directive(s, name)
        ctx.ok(f"{name} reads back after the set", live[name] is not None)

    ctx.ok("CONFIG REWRITE returns OK", cmd(s, "CONFIG", "REWRITE") == "OK")
    with open(conf) as f:
        text = f.read()
    ctx.ok("[REG] the rewritten config still carries requirepass",
           "requirepass" in text,
           "a rewrite that drops requirepass restarts the server passwordless, "
           "and every connection then auto-authenticates as the nopass default "
           "user with +@all ~*")
    ctx.ok("the rewritten config carries the channel grant", "&news.*" in text,
           text[:400])
    ctx.ok("[REG] no fused '~*&*' token in the rewritten config",
           "~*&*" not in text,
           "the channel token was emitted with no separating space, so the next "
           "boot parses '~*&*' as one key pattern and the grant silently changes")
    s.close()

    srv = ctx.restart(srv, "config-rt-boot")
    try:
        s = raw_conn(port, CFG_PW)
        authed = True
    except Exception as e:
        authed = False
        ctx.ok("[REG] the old password still authenticates after the round-trip",
               False, f"{type(e).__name__}: {e}")
    if not authed:
        srv.stop()
        return
    ctx.ok("[REG] the old password still authenticates after the round-trip", True)

    anon = socket.create_connection(("127.0.0.1", port), TIMEOUT_SEC)
    anon.settimeout(TIMEOUT_SEC)
    ctx.ok("[REG] an unauthenticated connection is still refused",
           err_of(anon, "GET", "anything") is not None,
           "the restarted server accepts unauthenticated commands — the "
           "rewrite lost requirepass")
    anon.close()

    for name, want in list(live.items()) + [
            (n, v) for n, v in BOOT_ONLY_PROBES if has_directive(s, n)]:
        got = get_directive(s, name)
        ctx.ok(f"[REG] {name} survived the rewrite ({want!r})", got == want,
               f"was {want!r} before the rewrite, {got!r} after the restart — "
               f"this directive's emit is wrong, which a same-process "
               f"set/get round-trip cannot detect")

    listing = cmd(s, "ACL", "LIST") or []
    joined = " ".join(listing) if isinstance(listing, list) else str(listing)
    ctx.ok("the channel grant survived the restart", "&news.*" in joined, joined)
    ctx.ok("[REG] no fused '~*&*' token in ACL LIST", "~*&*" not in joined, joined)
    ctx.ok("the allchannels user survived the restart", " &*" in joined, joined)

    s.close()
    srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: MEMORY — the accounting invariant, per type
#
#  The strongest statement the accounting can make is "drain everything, and
#  used_memory returns to exactly what it was", because entry_del subtracts the
#  exact bytes last charged. A non-zero residual means a discharge path is
#  missing or something is double-counted — and doing it one type at a time is
#  what localizes the broken handler instead of just proving one exists.
#
#  It runs on its own instance because every check here starts with a FLUSHALL
#  and ends by moving maxmemory.
# ═══════════════════════════════════════════════════════════════════════════════

# Both of these read the WHOLE INFO dump rather than a named section. A section
# name that is merely wrong — evicted_keys lives under Memory, not Stats — comes
# back as a missing key, and `.get(name, 0)` then reads as a counter that never
# moved, which is indistinguishable from a real result.

def _used_memory(sock: socket.socket) -> int:
    v = info_field(sock, "used_memory") or info_field(sock, "used_memory_bytes")
    if v is None:
        raise RuntimeError("no used_memory field in INFO")
    return int(v)


def _evicted_keys(sock: socket.socket) -> int:
    v = info_field(sock, "evicted_keys")
    try:
        return int(v) if v is not None else 0
    except ValueError:
        return 0


def _set_or_oom(sock: socket.socket, key: str, val: str) -> str:
    try:
        cmd(sock, "SET", key, val)
        return "OK"
    except RespError as e:
        if "OOM" in str(e):
            return "OOM"
        raise


def _drain_to_zero(ctx: "PhaseCtx", s: socket.socket, name: str, build, drain):
    base = _used_memory(s)
    build(s)
    grew = _used_memory(s)
    drain(s)
    back = _used_memory(s)
    ctx.ok(f"{name}: grows on build", grew > base, f"base={base} grew={grew}")
    ctx.ok(f"{name}: back to baseline after drain", back == base,
           f"base={base} after={back} (leak={back - base})")


def phase_memory(ctx: "PhaseCtx"):
    ctx.section("Memory: per-type accounting invariants")
    port = ctx.port()
    d = ctx.dir("memory")
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {port}",
        "appendonly no",
        'save ""',
    ])
    srv = ctx.start("memory", d, conf, port)
    s = raw_conn(port)

    cmd(s, "FLUSHALL")
    ctx.ok("an empty database accounts for 0 bytes", _used_memory(s) == 0,
           f"got {_used_memory(s)}")

    # per-type create -> delete round-trips
    _drain_to_zero(ctx, s, "string (set/del)",
                   lambda c: cmd(c, "SET", "k", "x" * 5000),
                   lambda c: cmd(c, "DEL", "k"))
    _drain_to_zero(ctx, s, "list (rpush/lpop-all)",
                   lambda c: [cmd(c, "RPUSH", "L", f"item-{i}") for i in range(500)],
                   lambda c: [cmd(c, "LPOP", "L") for _ in range(500)])
    _drain_to_zero(ctx, s, "hash (hset/hdel-all)",
                   lambda c: cmd(c, "HSET", "H", *sum(
                       ([f"f{i}", f"v{i}"] for i in range(300)), [])),
                   lambda c: [cmd(c, "HDEL", "H", f"f{i}") for i in range(300)])
    _drain_to_zero(ctx, s, "set (sadd/srem-all)",
                   lambda c: cmd(c, "SADD", "S", *[f"m{i}" for i in range(300)]),
                   lambda c: [cmd(c, "SREM", "S", f"m{i}") for i in range(300)])
    _drain_to_zero(ctx, s, "zset (zadd/zpopmin-all)",
                   lambda c: cmd(c, "ZADD", "Z", *sum(
                       ([str(i), f"m{i}"] for i in range(300)), [])),
                   lambda c: cmd(c, "ZPOPMIN", "Z", "300"))

    # the may-delete branches: a key that reaches empty through a value command
    # rather than through DEL takes a different discharge path
    _drain_to_zero(ctx, s, "list emptied via lrem",
                   lambda c: [cmd(c, "RPUSH", "LR", "dup") for _ in range(200)],
                   lambda c: cmd(c, "LREM", "LR", "0", "dup"))
    _drain_to_zero(ctx, s, "list emptied via ltrim",
                   lambda c: [cmd(c, "RPUSH", "LT", f"i{i}") for i in range(200)],
                   lambda c: cmd(c, "LTRIM", "LT", "5", "1"))   # start>stop clears
    _drain_to_zero(ctx, s, "set emptied via spop",
                   lambda c: cmd(c, "SADD", "SP", *[f"m{i}" for i in range(200)]),
                   lambda c: cmd(c, "SPOP", "SP", "200"))

    # overwrite stability: replacing a value repeatedly must not leak
    cmd(s, "FLUSHALL")
    cmd(s, "SET", "ov", "start")
    before = _used_memory(s)
    for i in range(1000):
        cmd(s, "SET", "ov", ("v%d" % i) * (i % 50 + 1))
    cmd(s, "SET", "ov", "start")
    after = _used_memory(s)
    ctx.ok("a string overwritten 1000 times leaks nothing",
           abs(after - before) <= 64, f"before={before} after={after}")

    _drain_to_zero(ctx, s, "append growth",
                   lambda c: [cmd(c, "APPEND", "AP", "z" * 100) for _ in range(200)],
                   lambda c: cmd(c, "DEL", "AP"))
    _drain_to_zero(ctx, s, "sinterstore dest",
                   lambda c: (cmd(c, "SADD", "A", *[str(i) for i in range(200)]),
                              cmd(c, "SADD", "B", *[str(i) for i in range(100, 300)]),
                              cmd(c, "SINTERSTORE", "DST", "A", "B")),
                   lambda c: cmd(c, "DEL", "A", "B", "DST"))
    _drain_to_zero(ctx, s, "rename re-key",
                   lambda c: (cmd(c, "SET", "old", "y" * 1000),
                              cmd(c, "RENAME", "old", "new")),
                   lambda c: cmd(c, "DEL", "new"))

    # a big mixed load, then FLUSHALL must land on exactly 0
    cmd(s, "FLUSHALL")
    for i in range(200):
        cmd(s, "SET", f"str:{i}", "v" * (i + 1))
        cmd(s, "RPUSH", f"list:{i}", *[f"e{j}" for j in range(i % 20 + 1)])
        cmd(s, "HSET", f"hash:{i}", "a", "1", "b", "2", "c", str(i))
        cmd(s, "SADD", f"set:{i}", *[f"m{j}" for j in range(i % 15 + 1)])
        cmd(s, "ZADD", f"zset:{i}", "1", "x", "2", "y", str(i), "z")
    loaded = _used_memory(s)
    cmd(s, "FLUSHALL")
    residual = _used_memory(s)
    ctx.ok("a mixed load grows used_memory", loaded > 0, f"loaded={loaded}")
    ctx.ok("FLUSHALL returns used_memory to exactly 0", residual == 0,
           f"residual={residual} — a discharge path is missing or something is "
           f"counted twice")

    # --- the cap is enforced two ways, and they must not be confusable -------
    # Both floods push ~6 MB into a 1 MB cap. Staying near the cap proves the
    # limit is enforced; growing to multiples of it means -OOM is being ignored.
    ctx.section("Memory: maxmemory (noeviction vs allkeys-lru)")
    CAP = 1024 * 1024
    VAL = "y" * 1000
    BOUND = CAP + CAP // 2          # tolerate one write of overshoot
    cmd(s, "CONFIG", "SET", "maxmemory", str(CAP))

    cmd(s, "FLUSHALL")
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "noeviction")
    ev_before = _evicted_keys(s)
    oks = ooms = 0
    for i in range(6000):
        r = _set_or_oom(s, f"ne:{i}", VAL)
        oks += (r == "OK")
        ooms += (r == "OOM")
        if ooms >= 50:
            break
    ctx.ok("noeviction: writes succeed, then start refusing",
           oks > 0 and ooms > 0, f"ok={oks} oom={ooms}")
    ctx.ok("noeviction: used_memory stays bounded near the cap",
           _used_memory(s) <= BOUND, f"used={_used_memory(s)} bound={BOUND}")
    ctx.ok("noeviction: nothing was evicted",
           _evicted_keys(s) == ev_before,
           f"evicted_delta={_evicted_keys(s) - ev_before} — noeviction must "
           f"refuse the write, not make room for it")
    ctx.ok("noeviction: a write over the cap is refused",
           _set_or_oom(s, "over", VAL) == "OOM")

    cmd(s, "FLUSHALL")
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "allkeys-lru")
    ev0 = _evicted_keys(s)
    ooms = 0
    for i in range(6000):
        ooms += (_set_or_oom(s, f"lru:{i}", VAL) == "OOM")
    ctx.ok("allkeys-lru: no write is ever refused", ooms == 0, f"oom={ooms}")
    ctx.ok("allkeys-lru: used_memory held near the cap",
           _used_memory(s) <= BOUND, f"used={_used_memory(s)} bound={BOUND}")
    ctx.ok("allkeys-lru: evicted_keys climbed",
           _evicted_keys(s) - ev0 > 0, f"evicted={_evicted_keys(s) - ev0}")

    # --- incremental eviction: the overshoot must not lock writes out --------
    ctx.section("Memory: incremental eviction under a large overshoot")
    cmd(s, "CONFIG", "SET", "maxmemory", "0")
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "allkeys-random")
    cmd(s, "FLUSHALL")
    for i in range(6000):
        cmd(s, "SET", f"ev:{i}", VAL)        # ~6 MB, then drop the cap to 1 MB
    over_keys = cmd(s, "DBSIZE")
    cmd(s, "CONFIG", "SET", "maxmemory", str(CAP))
    ctx.ok("[REG] the write right after a 6x overshoot is admitted",
           _set_or_oom(s, "ev:probe", "1") == "OK",
           "the server refused writes until repeated attempts had chipped the "
           "overshoot away 100 keys at a time")

    # idle drain: no further writes at all — evict_tick alone has to get under
    deadline = time.time() + 15.0
    um = _used_memory(s)
    while um > CAP and time.time() < deadline:
        time.sleep(0.2)
        um = _used_memory(s)
    ctx.ok(f"the keyspace drains while completely idle "
           f"({over_keys} keys -> {cmd(s, 'DBSIZE')})",
           um <= CAP, f"used_memory stalled at {um}, cap is {CAP} — evict_tick "
                      f"is not doing work between commands")

    cmd(s, "CONFIG", "SET", "maxmemory", "0")
    cmd(s, "CONFIG", "SET", "maxmemory-policy", "noeviction")
    cmd(s, "FLUSHALL")
    s.close()
    srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: REPLICATION — a master, a replica, and a link that can be cut
#
#  Runs a master and a replica on private ports with a killable TCP proxy
#  between them, so every replication path is reachable from one command with no
#  manual setup.
#
#  Two rules run through all of it:
#    - A partial resync and a full resync BOTH leave the replica holding correct
#      data, so every partial-resync check asserts on the master's sync_*
#      counters and never on "the keys are there".
#    - A proxy, not a kill: the master has to stay up across the gap. Killing it
#      would destroy the backlog and mint a new replid, forcing a full resync and
#      making the partial-resync checks silently vacuous.
# ═══════════════════════════════════════════════════════════════════════════════

# The minimum the server accepts (k_repl_backlog_min). Deliberately tiny: it is
# what makes "a gap larger than the backlog" reachable with a few KB of writes.
BACKLOG_BYTES = 16 * 1024

# Replication is asynchronous, so every cross-instance assertion polls.
SYNC_WAIT = 5.0

# What the repl-timeout phases wind the timeout down to. It has to clear
# k_repl_ack_period_ms (1s) with room to spare or the master reaps a healthy
# replica between two of its own ACKs, and it has to stay small or the phase crawls.
SHORT_TIMEOUT = 3
SHORT_LAG = 2


class Proxy:
    """A killable TCP hop: replica -> proxy -> master.

    stop() closes the listener AND every live pair, so the replica sees a FIN and
    its master link really dies; start() reopens on the same port.

    freeze() is the other failure mode, and the interesting one: stop moving
    bytes but leave every socket OPEN. A link that closes is already handled —
    poll() reports it immediately. A link that just goes quiet is invisible to
    poll() and only a clock can catch it.
    """

    def __init__(self, listen_port: int, target_port: int):
        self.listen_port = listen_port
        self.target_port = target_port
        self._lock = threading.Lock()
        self._conns = []
        self._lsock = None
        self._thread = None
        self._stop = threading.Event()
        # survives stop()/start() on purpose: thawing is always explicit
        self._frozen = threading.Event()

    def freeze(self):
        self._frozen.set()

    def thaw(self):
        self._frozen.clear()

    def start(self):
        self._stop = threading.Event()
        with self._lock:
            self._conns = []
        self._lsock = socket.socket()
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", self.listen_port))
        self._lsock.listen(8)
        self._lsock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            socks = ([self._lsock] + self._conns) if self._lsock else list(self._conns)
            self._conns = []
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        self._lsock = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._lsock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(
                    ("127.0.0.1", self.target_port), 2)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._conns += [client, upstream]
            threading.Thread(target=self._pump, args=(client, upstream),
                             daemon=True).start()

    def _pump(self, a, b):
        pair = [a, b]
        try:
            while not self._stop.is_set():
                if self._frozen.is_set():
                    # Deliberately do NOT read: bytes pile up in the kernel
                    # buffers exactly as they would against a black-holed path,
                    # and neither end sees an error, an EOF, or a reset.
                    time.sleep(0.05)
                    continue
                ready, _, _ = select.select(pair, [], [], 0.2)
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            for s in pair:
                try:
                    s.close()
                except OSError:
                    pass


# ─── replication helpers ──────────────────────────────────────────────────────

def _link_up(sock: socket.socket) -> bool:
    d = info_dict(sock, "replication")
    return d.get("role") == "slave" and d.get("master_link_status") == "up"


def _counters(sock: socket.socket):
    d = info_dict(sock, "stats")
    return (int(d.get("sync_full", -1)),
            int(d.get("sync_partial_ok", -1)),
            int(d.get("sync_partial_err", -1)))


def _conf_has_replicaof(path: str) -> bool:
    with open(path) as f:
        return any(l.strip().startswith("replicaof ") for l in f)


def _set_if_present(sock: socket.socket, name: str, value) -> bool:
    """Set a directive the binary may not have yet. True if it took."""
    if not has_directive(sock, name):
        return False
    cmd(sock, "CONFIG", "SET", name, str(value))
    return True


def _log_has(inst: "Instance", needle: str) -> bool:
    """Poll the server's stderr FILE, not the server.

    The point of the timeout phases is that a deadline fires on an IDLE
    instance. Polling INFO to find out would wake the event loop and hide
    exactly the bug being tested — a deadline missing from next_timer_ms() only
    misbehaves while nothing else wakes the loop — so the observation has to
    happen off to the side.
    """
    return needle in inst.stderr_text()


def _slave0(sock: socket.socket) -> dict:
    out = {}
    for part in info_dict(sock, "replication").get("slave0", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


# ─── the phases ───────────────────────────────────────────────────────────────

def _repl_full_resync(ctx, master, replica, replica_srv):
    ctx.section("Replication: full resync")
    ctx.ok("the replica booted into the role from its config file",
           wait_until(lambda: _link_up(replica), SYNC_WAIT),
           f"INFO replication = {info_dict(replica, 'replication')}")
    rd = info_dict(replica, "replication")
    md = info_dict(master, "replication")
    ctx.ok("the replica reports role:slave", rd.get("role") == "slave",
           rd.get("role"))
    ctx.ok("[REG] the replica adopted the master's replid",
           rd.get("master_replid") == md.get("master_replid"),
           f"replica={rd.get('master_replid')} master={md.get('master_replid')}")
    ctx.ok("the master counts one connected replica",
           wait_until(lambda: info_dict(master, "replication")
                      .get("connected_slaves") == "1", SYNC_WAIT),
           info_dict(master, "replication").get("connected_slaves"))
    for k, v in (("pre1", "a"), ("pre2", "b"), ("pre3", "c")):
        ctx.ok(f"the pre-resync key {k} arrived",
               wait_until(lambda k=k, v=v: cmd(replica, "GET", k) == v, SYNC_WAIT))
    log = replica_srv.stderr_text()
    ctx.ok("the replica logged the resync", "streaming from master" in log,
           (log.strip().splitlines() or ["<empty>"])[-1])


def _repl_streaming(ctx, master, replica):
    ctx.section("Replication: live streaming")
    cmd(master, "SET", "live1", "v1")
    ctx.ok("SET propagates",
           wait_until(lambda: cmd(replica, "GET", "live1") == "v1", SYNC_WAIT))
    cmd(master, "SADD", "liveset", "m1", "m2")
    ctx.ok("SADD propagates",
           wait_until(lambda: cmd(replica, "SCARD", "liveset") == 2, SYNC_WAIT))
    cmd(master, "DEL", "live1")
    ctx.ok("DEL propagates",
           wait_until(lambda: cmd(replica, "GET", "live1") is None, SYNC_WAIT))
    cmd(master, "SET", "ttlkey", "x")
    cmd(master, "EXPIRE", "ttlkey", "1000")
    ctx.ok("[REG] a TTL replicates as an absolute time, not a relative one",
           wait_until(lambda: 0 < int(cmd(replica, "TTL", "ttlkey")) <= 1000,
                      SYNC_WAIT),
           "a relative EXPIRE re-applied on the replica drifts, or misses entirely")
    # SPOP and SREM are chosen by the server, not by the command: what the
    # replica must receive is the member that was actually removed.
    cmd(master, "SADD", "replset", "a", "b", "c", "d")
    popped = cmd(master, "SPOP", "replset")
    ctx.ok("[REG] SPOP replicates the member it removed, not the command",
           wait_until(lambda: sorted(cmd(replica, "SMEMBERS", "replset") or [])
                      == sorted(cmd(master, "SMEMBERS", "replset") or []),
                      SYNC_WAIT),
           f"master popped {popped!r}; the replica popped something else, so the "
           f"two sets have diverged while both look plausible")


def _repl_readonly(ctx, master, replica):
    ctx.section("Replication: read-only gate")
    err = err_of(replica, "SET", "nope", "1")
    ctx.ok("a write from an ordinary client is refused",
           err is not None and err.startswith("READONLY"), err)
    err = err_of(replica, "FLUSHALL")
    ctx.ok("[REG] FLUSHALL is refused too (it carries is_write)",
           err is not None and err.startswith("READONLY"), err)
    ctx.ok("reads are still served", cmd(replica, "GET", "pre1") == "a")
    ctx.ok("MULTI itself is allowed", err_of(replica, "MULTI") is None)
    ctx.ok("a queued write is refused at queue time",
           err_of(replica, "SET", "nope", "1") is not None)
    err = err_of(replica, "EXEC")
    ctx.ok("the poisoned transaction aborts",
           err is not None and "EXECABORT" in err, err)
    cmd(master, "SET", "after_gate", "ok")
    ctx.ok("[REG] the replication stream still applies through the gate",
           wait_until(lambda: cmd(replica, "GET", "after_gate") == "ok", SYNC_WAIT),
           "the gate must exempt the stream, or the replica silently forks")


def _repl_link_loss(ctx, replica, proxy):
    ctx.section("Replication: link loss must not promote")
    proxy.stop()
    ctx.ok("the link goes down",
           wait_until(lambda: info_dict(replica, "replication")
                      .get("master_link_status") == "down", SYNC_WAIT),
           info_dict(replica, "replication").get("master_link_status"))
    d = info_dict(replica, "replication")
    ctx.ok("[REG] a dropped socket does NOT promote the replica",
           d.get("role") == "slave",
           "the role went to master on link loss — both instances would then "
           "accept writes")
    ctx.ok("[REG] the master address survives the drop",
           bool(d.get("master_host")) and bool(d.get("master_port")), str(d))
    err = err_of(replica, "SET", "nope", "1")
    ctx.ok("[REG] still read-only while disconnected",
           err is not None and err.startswith("READONLY"), err)


def _repl_partial(ctx, master, replica, proxy, proxy_port):
    ctx.section("Replication: partial resync")
    full0, ok0, _ = _counters(master)
    for i in range(20):
        cmd(master, "SET", f"gap{i}", f"v{i}")
    proxy.start()
    cmd(replica, "REPLICAOF", "127.0.0.1", str(proxy_port))
    ctx.ok("the link comes back", wait_until(lambda: _link_up(replica), SYNC_WAIT),
           str(info_dict(replica, "replication")))
    ctx.ok("the gap keys arrived",
           wait_until(lambda: cmd(replica, "GET", "gap19") == "v19", SYNC_WAIT))
    full1, ok1, _ = _counters(master)
    ctx.ok("[REG] the reconnect was a PARTIAL resync", ok1 == ok0 + 1,
           f"sync_partial_ok {ok0} -> {ok1} (correct data alone proves nothing "
           f"here — both resync paths produce it)")
    ctx.ok("[REG] no RDB was retransferred", full1 == full0,
           f"sync_full {full0} -> {full1}")


def _repl_gap_too_large(ctx, master, replica, proxy, proxy_port):
    ctx.section("Replication: a gap larger than the backlog falls back")
    proxy.stop()
    ctx.ok("the link is down again",
           wait_until(lambda: info_dict(replica, "replication")
                      .get("master_link_status") == "down", SYNC_WAIT))
    full0, ok0, err0 = _counters(master)
    blob = "x" * 1024
    for i in range(BACKLOG_BYTES // 1024 * 3):
        cmd(master, "SET", f"big{i}", blob)
    cmd(master, "SET", "past_the_gap", "yes")
    proxy.start()
    cmd(replica, "REPLICAOF", "127.0.0.1", str(proxy_port))
    ctx.ok("the link comes back", wait_until(lambda: _link_up(replica), SYNC_WAIT))
    ctx.ok("the replica caught up",
           wait_until(lambda: cmd(replica, "GET", "past_the_gap") == "yes",
                      SYNC_WAIT * 2))
    full1, ok1, err1 = _counters(master)
    ctx.ok("[REG] an unservable offset degrades to a FULL resync",
           full1 == full0 + 1,
           f"sync_full {full0} -> {full1}: a +CONTINUE here is silent divergence")
    ctx.ok("the refusal was counted", err1 == err0 + 1,
           f"sync_partial_err {err0} -> {err1}")


def _repl_auto_reconnect(ctx, master, replica, proxy):
    ctx.section("Replication: automatic reconnect")
    full0, ok0, _ = _counters(master)
    proxy.stop()
    ctx.ok("the link is down",
           wait_until(lambda: info_dict(replica, "replication")
                      .get("master_link_status") == "down", SYNC_WAIT))
    for i in range(10):
        cmd(master, "SET", f"auto{i}", f"v{i}")
    proxy.start()
    # deliberately NO manual REPLICAOF: the replica must re-dial on its own
    ctx.ok("[REG] the replica re-dials without being told",
           wait_until(lambda: _link_up(replica), 25.0),
           "repl_cron never fired — check that next_timer_ms() wakes poll() for "
           "a disconnected replica, or it never runs on an idle keyspace")
    ctx.ok("writes missed during the outage arrived",
           wait_until(lambda: cmd(replica, "GET", "auto9") == "v9", SYNC_WAIT))
    full1, ok1, _ = _counters(master)
    ctx.ok("the automatic reconnect used a partial resync",
           ok1 == ok0 + 1 and full1 == full0,
           f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1}")


def _repl_wait(ctx, master, master_port):
    ctx.section("Replication: REPLCONF ACK + WAIT")
    cmd(master, "SET", "ackkey", "v")
    ctx.ok("the master learns the replica's offset from periodic acks",
           wait_until(lambda: int(_slave0(master).get("offset", 0)) > 0, 6.0),
           f"slave0 = {_slave0(master)}")

    t0 = time.time()
    n = cmd(master, "WAIT", "1", "4000")
    ctx.ok("WAIT 1 counts the caught-up replica", n == 1, str(n))
    ctx.ok("...and returned well inside its timeout", time.time() - t0 < 3.5,
           f"{time.time() - t0:.1f}s")

    t0 = time.time()
    n = cmd(master, "WAIT", "5", "1000")
    dt = time.time() - t0
    ctx.ok("[REG] an unsatisfiable WAIT returns a short count, not an error",
           n == 1, str(n))
    ctx.ok("...after roughly its timeout", 0.7 < dt < 3.0, f"{dt:.1f}s")
    ctx.ok("the connection still works afterwards", cmd(master, "PING") == "PONG")

    blocker = raw_conn(master_port)
    send_request(blocker, "WAIT", "5", "2000")     # cannot be satisfied
    other = raw_conn(master_port)
    ctx.ok("[REG] a pending WAIT does not block the event loop",
           cmd(other, "PING") == "PONG",
           "the loop stalled: WAIT must defer its reply, not wait in the handler")
    ctx.ok("the deferred client is resumed on timeout", recv_response(blocker) == 1)
    blocker.close()
    other.close()

    cmd(master, "MULTI")
    cmd(master, "WAIT", "5", "5000")
    t0 = time.time()
    res = cmd(master, "EXEC")
    ctx.ok("[REG] WAIT inside EXEC answers immediately instead of deferring",
           isinstance(res, list) and len(res) == 1 and time.time() - t0 < 2.0,
           f"{res!r} after {time.time() - t0:.1f}s — the reply is one element of "
           f"an array whose size has already been written")


def _repl_min_replicas(ctx, master, replica, proxy):
    """The durability floor. The interesting half is not "does it refuse" but
    WHICH replicas count: one that is connected and has stopped acking must stop
    counting while still in g_data.replicas. Hence a freeze rather than a drop —
    dropping it would prove repl-timeout and say nothing about the lag gate."""
    ctx.section("Replication: min-replicas-to-write durability floor")
    to_write0 = get_directive(master, "min-replicas-to-write")
    max_lag0 = get_directive(master, "min-replicas-max-lag")
    ctx.ok("min-replicas-to-write defaults to 0 (feature off)",
           to_write0 == "0", str(to_write0))
    ctx.ok("min-replicas-max-lag defaults to 10 seconds", max_lag0 == "10",
           f"{max_lag0!r}: 0 means 'do not judge on lag', so every connected "
           f"replica counts — including one still loading its resync image, "
           f"which can neither ack nor serve")

    ctx.ok("a negative count is rejected",
           err_of(master, "CONFIG", "SET", "min-replicas-to-write", "-1") is not None)
    ctx.ok("a count past the cap is rejected",
           err_of(master, "CONFIG", "SET", "min-replicas-to-write", "2000") is not None)
    ctx.ok("a lag past the cap is rejected",
           err_of(master, "CONFIG", "SET", "min-replicas-max-lag", "99999") is not None)
    ctx.ok("a lag of 0 is accepted (do not judge on lag)",
           err_of(master, "CONFIG", "SET", "min-replicas-max-lag", "0") is None)

    ctx.ok("the link is healthy before the floor goes up",
           wait_until(lambda: _link_up(replica), SYNC_WAIT),
           info_dict(replica, "replication").get("master_link_status"))
    cmd(master, "CONFIG", "SET", "min-replicas-max-lag", str(SHORT_LAG))
    cmd(master, "CONFIG", "SET", "min-replicas-to-write", "1")

    ctx.ok("a replica that is acking satisfies the floor",
           err_of(master, "SET", "floor_ok", "1") is None)
    d = info_dict(master, "replication")
    ctx.ok("[REG] INFO reports the good-replica count on a MASTER",
           d.get("min_slaves_good_slaves") == "1",
           f"min_slaves_good_slaves={d.get('min_slaves_good_slaves')!r} — this "
           f"is the field an operator reads to find out why writes are refused")

    proxy.freeze()                       # present, but no longer acking
    time.sleep(SHORT_LAG + 1.5)
    err = err_of(master, "SET", "floor_bad", "1")
    ctx.ok("[REG] a replica that stopped acking stops counting",
           err is not None and err.startswith("NOREPLICAS"), err)
    ctx.ok("...and the refusal is the whole cost: reads are untouched",
           cmd(master, "GET", "floor_ok") == "1")
    d = info_dict(master, "replication")
    ctx.ok("min_slaves_good_slaves fell to 0",
           d.get("min_slaves_good_slaves") == "0",
           d.get("min_slaves_good_slaves"))
    ctx.ok("[REG] the lagging replica is still CONNECTED",
           d.get("connected_slaves") == "1",
           "the master reaped it instead, so this phase proved repl-timeout and "
           "said nothing at all about the lag gate")

    proxy.thaw()
    ctx.ok("writes resume once the acks come back",
           wait_until(lambda: err_of(master, "SET", "floor_back", "1") is None,
                      15.0),
           "the floor never lifted after the link recovered")

    # a replica applying its master's stream has no replicas of its own, so a
    # floor evaluated there would drop the write
    cmd(replica, "CONFIG", "SET", "min-replicas-to-write", "1")
    cmd(master, "SET", "stream_through_floor", "yes")
    ctx.ok("[REG] the floor never refuses the replication stream itself",
           wait_until(lambda: cmd(replica, "GET", "stream_through_floor") == "yes",
                      SYNC_WAIT),
           "the gate must test !replica_mode AND !g_loading — a replica that "
           "drops a write it was sent has silently forked from its master")
    cmd(replica, "CONFIG", "SET", "min-replicas-to-write", "0")
    cmd(master, "CONFIG", "SET", "min-replicas-to-write", to_write0)
    cmd(master, "CONFIG", "SET", "min-replicas-max-lag", max_lag0)


def _repl_timeout_config(ctx, master):
    ctx.section("Replication: repl-timeout directive")
    before = get_directive(master, "repl-timeout")
    ctx.ok("repl-timeout defaults to 60 seconds", before == "60", str(before))
    # The units check a same-process round-trip CANNOT do: get->apply->get still
    # agrees with itself when the getter forgets its /1000, because apply() and
    # get() are wrong in the same direction.
    cmd(master, "CONFIG", "SET", "repl-timeout", "5")
    got = get_directive(master, "repl-timeout")
    ctx.ok("[REG] repl-timeout round-trips in SECONDS, not milliseconds",
           got == "5", f"got {got!r}; 5000 means the getter is missing its /1000")
    ctx.ok("a value past the cap is rejected",
           err_of(master, "CONFIG", "SET", "repl-timeout", "99999") is not None,
           "out-of-range values must fail loudly, not clamp silently")
    ctx.ok("0 is accepted (disabled)",
           err_of(master, "CONFIG", "SET", "repl-timeout", "0") is None)
    cmd(master, "CONFIG", "SET", "repl-timeout", before)


def _repl_idle_keepalive(ctx, master, replica, replica_srv):
    """An idle master is a HEALTHY master and the timeout must not say otherwise.

    Nothing travels master->replica on a link with no writes: REPLCONF ACK is
    replica->master only and the master never answers it. So a timeout measured
    on inbound bytes expires on a perfectly good link the moment traffic stops —
    the replica drops it, resyncs, and does it again every repl-timeout seconds
    forever. repl-ping-replica-period is what buys the difference.
    """
    ctx.section("Replication: an idle link must survive")
    pinged = _set_if_present(master, "repl-ping-replica-period", 1)
    cmd(replica, "CONFIG", "SET", "repl-timeout", str(SHORT_TIMEOUT))
    ctx.ok("the link is up before the idle window",
           wait_until(lambda: _link_up(replica), SYNC_WAIT))
    mark = replica_srv.stderr_text().count("no data from master")
    cmd(master, "SET", "idle_probe", "1")      # one write, then nothing at all
    time.sleep(SHORT_TIMEOUT * 2 + 1)          # deliberately past the timeout
    drops = replica_srv.stderr_text().count("no data from master") - mark
    ctx.ok("[REG] a quiet master is not mistaken for a dead one", drops == 0,
           f"the replica dropped a healthy master {drops}x in "
           f"{SHORT_TIMEOUT * 2 + 1}s"
           + ("" if pinged else
              " — and there is no repl-ping-replica-period directive, so the "
              "master sends nothing at all on an idle link: this also flaps on "
              "the 60s default, just once a minute instead of twice in seven "
              "seconds"))
    ctx.ok("...and the link is still up afterwards", _link_up(replica),
           info_dict(replica, "replication").get("master_link_status"))
    cmd(replica, "CONFIG", "SET", "repl-timeout", "60")


def _repl_wedged(ctx, master, replica, master_srv, replica_srv, proxy):
    """A link that goes silent without closing. Nothing on either side has a
    reason to look at the clock, so before repl-timeout this state was permanent."""
    ctx.section("Replication: a wedged link (silent, not closed)")
    ctx.ok("healthy before the freeze", wait_until(lambda: _link_up(replica),
                                                   SYNC_WAIT),
           info_dict(replica, "replication").get("master_link_status"))
    _set_if_present(master, "repl-ping-replica-period", 1)
    for s in (master, replica):
        cmd(s, "CONFIG", "SET", "repl-timeout", str(SHORT_TIMEOUT))

    io_before = info_dict(replica, "replication").get("master_last_io_seconds_ago")
    ctx.ok("master_last_io_seconds_ago is present and small",
           io_before is not None and 0 <= int(io_before) <= 2, str(io_before))

    proxy.freeze()
    # From here until the asserts, NOTHING touches either server. Both are fully
    # idle, so the only thing that can fire the timeout is a deadline
    # next_timer_ms() actually knows about. Watching stderr keeps the
    # observation off the wire — an INFO poll would wake the loop and mask the
    # bug outright.
    ctx.ok("[REG] the replica drops a silent master with no traffic to wake it",
           wait_until(lambda: _log_has(replica_srv, "no data from master"),
                      SHORT_TIMEOUT + 4),
           "still STREAMING: either repl_cron has no timeout check, or "
           "next_timer_ms() has no deadline for it and poll() slept through it")
    ctx.ok("[REG] the master drops a replica that stopped acking",
           wait_until(lambda: _log_has(master_srv, "silent for"),
                      SHORT_TIMEOUT + 4),
           "a dead replica left in g_data.replicas keeps counting toward WAIT")

    d = info_dict(replica, "replication")
    ctx.ok("the replica reports the link down",
           d.get("master_link_status") == "down", d.get("master_link_status"))
    # -1 only while there is no socket at all. By now the replica has usually
    # re-dialled into the frozen proxy and sits in HANDSHAKE with a fresh stamp,
    # so the deterministic property is the negative one: it must never still be
    # reporting the real age of the master's last word.
    io_down = d.get("master_last_io_seconds_ago")
    ctx.ok("master_last_io_seconds_ago reflects the drop, not a stale age",
           io_down == "-1" or 0 <= int(io_down) <= 2,
           f"{io_down} (expected -1, or small after the re-dial)")
    ctx.ok("[REG] a dropped link does NOT promote the replica",
           d.get("role") == "slave",
           "losing the master must never make an instance writable")
    ctx.ok("the master shows no replicas",
           info_dict(master, "replication").get("connected_slaves") == "0",
           info_dict(master, "replication").get("connected_slaves"))
    ctx.ok("a reaped replica cannot satisfy WAIT",
           cmd(master, "WAIT", "1", "500") == 0)

    proxy.thaw()
    ctx.ok("it reconnects on its own once the path comes back",
           wait_until(lambda: _link_up(replica), 15.0),
           str(info_dict(replica, "replication")))
    cmd(master, "SET", "after_wedge", "ok")
    ctx.ok("streaming resumed",
           wait_until(lambda: cmd(replica, "GET", "after_wedge") == "ok", SYNC_WAIT))
    for s in (master, replica):
        cmd(s, "CONFIG", "SET", "repl-timeout", "60")
    _set_if_present(master, "repl-ping-replica-period", 10)


def _repl_promotion(ctx, master, replica, replica_conf, proxy_port):
    ctx.section("Replication: promotion")
    before = info_dict(replica, "replication").get("master_replid")
    cmd(replica, "REPLICAOF", "NO", "ONE")
    d = info_dict(replica, "replication")
    ctx.ok("promoted to master", d.get("role") == "master", d.get("role"))
    ctx.ok("[REG] promotion mints a NEW replid",
           d.get("master_replid") != before,
           "keeping the old master's replid would let a later reconnect accept "
           "an unsafe +CONTINUE")
    ctx.ok("writable again", err_of(replica, "SET", "now", "writable") is None)

    cmd(replica, "CONFIG", "REWRITE")
    ctx.ok("[REG] CONFIG REWRITE drops the replicaof line after promotion",
           not _conf_has_replicaof(replica_conf),
           "emit is reading the staged g_config instead of the live g_data role")

    full0, ok0, _ = _counters(master)
    cmd(replica, "REPLICAOF", "127.0.0.1", str(proxy_port))
    ctx.ok("re-attached", wait_until(lambda: _link_up(replica), SYNC_WAIT))
    full1, ok1, _ = _counters(master)
    ctx.ok("[REG] a promoted instance forfeits its history (full resync)",
           full1 == full0 + 1 and ok1 == ok0,
           f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1}")
    cmd(replica, "CONFIG", "REWRITE")
    ctx.ok("CONFIG REWRITE restores the line once it is a replica again",
           _conf_has_replicaof(replica_conf))


def _repl_restart(ctx, replica_dir, replica_conf, replica_port, master):
    ctx.section("Replication: a restart keeps the role")
    srv = ctx.start("replica-restart", replica_dir, replica_conf, replica_port)
    replica = raw_conn(replica_port)
    ctx.ok("[REG] a restarted replica comes back a REPLICA, not a writable master",
           wait_until(lambda: _link_up(replica), SYNC_WAIT * 2),
           str(info_dict(replica, "replication")))
    cmd(master, "SET", "after_restart", "yes")
    ctx.ok("streaming resumed after the restart",
           wait_until(lambda: cmd(replica, "GET", "after_restart") == "yes",
                      SYNC_WAIT))
    err = err_of(replica, "SET", "nope", "1")
    ctx.ok("still read-only after the restart",
           err is not None and err.startswith("READONLY"), err)
    replica.close()
    return srv


def _repl_failover(ctx, m_port, r_port, p_port):
    """The coordinated, zero-loss handover.

    On its own pair, because a handover swaps both roles and every later phase
    on the main pair would then be talking to the wrong instance.

    The target sits behind a proxy so its ACKs can be stopped WITHOUT stopping
    the target itself: the write pause, ABORT and the TIMEOUT abort all need a
    replica that is present but not caught up. The handover dial does not use
    the proxy — it goes to the address named in FAILOVER TO — so a frozen path
    can never hide a broken handover.
    """
    ctx.section("Replication: coordinated FAILOVER")
    fdir = ctx.dir("failover")
    mdir, rdir = ctx.dir("failover/master"), ctx.dir("failover/replica")
    mconf = write_conf(os.path.join(mdir, "fo-master.conf"),
                       [f"port {m_port}", "appendonly no", 'save ""'])
    rconf = write_conf(os.path.join(rdir, "fo-replica.conf"),
                       [f"port {r_port}", "appendonly no", 'save ""',
                        f"replicaof 127.0.0.1 {p_port}"])
    proxy = Proxy(p_port, m_port)
    proxy.start()
    m = r = None
    msrv = rsrv = None
    try:
        msrv = ctx.start("fo-master", mdir, mconf, m_port)
        m = raw_conn(m_port)
        for i in range(5):
            cmd(m, "SET", f"fo{i}", f"v{i}")
        rsrv = ctx.start("fo-replica", rdir, rconf, r_port)
        r = raw_conn(r_port)
        ctx.ok("the failover pair is linked", wait_until(lambda: _link_up(r),
                                                          SYNC_WAIT * 2),
               str(info_dict(r, "replication")))

        for args, want in (
            (("FAILOVER", "TO", "127.0.0.1"), "needs a host and a port"),
            (("FAILOVER", "TO", "127.0.0.1", "0"), "invalid FAILOVER target port"),
            (("FAILOVER", "TO", "127.0.0.1", "70000"), "invalid FAILOVER target port"),
            (("FAILOVER", "TIMEOUT", "abc"), "invalid FAILOVER TIMEOUT"),
            (("FAILOVER", "WAT"), "syntax error"),
            (("FAILOVER", "FORCE", "TIMEOUT", "1000"), "FORCE requires TO"),
            (("FAILOVER", "ABORT"), "No failover in progress"),
        ):
            err = err_of(m, *args)
            ctx.ok(f"{' '.join(args)} -> {want}",
                   err is not None and want.lower() in err.lower(), str(err))

        err = err_of(m, "FAILOVER", "TO", "127.0.0.1", str(r_port), "FORCE")
        ctx.ok("FORCE without TIMEOUT is refused",
               err is not None and "TIMEOUT" in err, str(err))
        err = err_of(m, "FAILOVER", "TO", "127.0.0.1", "9999")
        ctx.ok("[REG] a valid port with no replica behind it is 'not a connected "
               "replica'",
               err is not None and "not a connected replica" in err,
               f"{err!r} — 'invalid FAILOVER target port' for an ordinary port "
               f"means the range check reads `port < 65535` where it means "
               f"`port > 65535`, which rejects every port anyone would use")
        err = err_of(r, "FAILOVER")
        ctx.ok("FAILOVER on a replica is refused",
               err is not None and "requires being a master" in err, str(err))

        proxy.freeze()                     # present, but no longer acking
        for i in range(5):
            cmd(m, "SET", f"pause{i}", "x")

        rep, err = reply_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                                "TIMEOUT", "30000")
        if not ctx.ok("FAILOVER TO <the connected replica> accepted", rep == "OK",
                      err or repr(rep)):
            ctx.skip("the rest of the failover phase",
                     f"FAILOVER TO was rejected: {err}")
            return

        d = info_dict(m, "replication")
        ctx.ok("[REG] INFO reports the pause on the MASTER",
               d.get("failover_state") == "waiting-for-sync",
               f"failover_state={d.get('failover_state')!r} — that state only "
               f"ever exists on a master, so a field emitted inside the "
               f"`if (replica)` block can never be seen in the one state an "
               f"operator needs it for")
        err = err_of(m, "SET", "during_pause", "1")
        ctx.ok("[REG] writes are paused while the handover waits",
               err is not None and err.startswith("FAILOVER"),
               f"{err!r} — every write accepted here moves the offset the target "
               f"is trying to reach, and the handover never converges")
        ctx.ok("reads are served throughout the pause", cmd(m, "GET", "fo0") == "v0")
        err = err_of(m, "FAILOVER", "TO", "127.0.0.1", str(r_port))
        ctx.ok("a second FAILOVER is refused",
               err is not None and "already in progress" in err, str(err))

        rep, err = reply_or_err(m, "FAILOVER", "ABORT")
        ctx.ok("FAILOVER ABORT unwinds a waiting handover", rep == "OK", str(err))
        ctx.ok("writes flow again after the abort",
               err_of(m, "SET", "after_abort", "1") is None)
        ctx.ok("the role never changed",
               info_dict(m, "replication").get("role") == "master")

        needle = "timed out waiting for the target"
        mark = msrv.stderr_text().count(needle)
        rep, err = reply_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                                "TIMEOUT", "1500")
        ctx.ok("FAILOVER with a short TIMEOUT accepted", rep == "OK", str(err))
        # NOTHING may touch either server until the log says so: a deadline that
        # only fires because an INFO poll woke poll() is not wired.
        ctx.ok("[REG] the TIMEOUT fires with no traffic to wake the loop",
               wait_until(lambda: msrv.stderr_text().count(needle) > mark, 8.0),
               "still waiting: next_timer_ms() has no entry for "
               "failover_deadline_ms, so poll() slept straight past it")
        ctx.ok("the timed-out master is writable again",
               wait_until(lambda: err_of(m, "SET", "after_timeout", "1") is None,
                          SYNC_WAIT),
               "failover_reset never ran, or it left the pause gate up")
        ctx.ok("...still a master, having handed over to nobody",
               info_dict(m, "replication").get("role") == "master")
        ctx.ok("failover_state is back to no-failover",
               info_dict(m, "replication").get("failover_state", "no-failover")
               == "no-failover")

        for i in range(5):
            cmd(m, "SET", f"lost{i}", "x")    # never reaches the frozen target
        full0, ok0, _ = _counters(r)
        rep, err = reply_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                                "FORCE", "TIMEOUT", "1500")
        ctx.ok("FAILOVER ... FORCE accepted", rep == "OK", str(err))
        ctx.ok("[REG] FORCE hands over past a target that never caught up",
               wait_until(lambda: _log_has(msrv, "FORCE, handing over"), 10.0),
               "without FORCE this must abort; with it, it must say how many "
               "bytes it is stepping over")
        ctx.ok("the old master demoted itself",
               wait_until(lambda: info_dict(m, "replication").get("role")
                          == "slave", 15.0),
               info_dict(m, "replication").get("role"))
        ctx.ok("the target promoted itself on PSYNC ... FAILOVER",
               wait_until(lambda: info_dict(r, "replication").get("role")
                          == "master", 15.0),
               "the 4th PSYNC argument never reached do_psync, or repl_shift_id "
               "was not called before the resync logic read the identity")
        ctx.ok("the demoted master re-attached to it",
               wait_until(lambda: _link_up(m), 15.0),
               str(info_dict(m, "replication")))
        ctx.ok("[REG] a forced handover is NOT served a +CONTINUE",
               _counters(r)[0] == full0 + 1 and _counters(r)[1] == ok0,
               f"sync_full {full0}->{_counters(r)[0]} — the demoted master is "
               f"AHEAD of the offset the target promoted at, so serving it from "
               f"the backlog would keep writes the new master never saw: two "
               f"instances, one replid, different data")
        ctx.ok("the writes FORCE stepped over are gone",
               wait_until(lambda: cmd(m, "GET", "lost4") is None, 15.0),
               "that loss is what the keyword buys and what the log line counts; "
               "if they survived, the resync did not replace the dataset")
        ctx.ok("failover_state cleared on the demoted master",
               info_dict(m, "replication").get("failover_state") == "no-failover",
               "failover_reset('complete') is missing from the HANDSHAKE outcome "
               "that ran — the pause gate then stays up forever after a handover")
        proxy.thaw()

        # Whichever instance holds the master role NOW drives the clean handover.
        # FORCE may have been refused or silently dropped, and the zero-RDB
        # handover is the headline of the milestone: it must not become
        # collateral damage of the phase above it.
        src, dst, dst_port = ((m, r, r_port)
                              if info_dict(m, "replication").get("role") == "master"
                              else (r, m, m_port))
        ctx.ok("the pair is healthy again before the clean handover",
               wait_until(lambda: _link_up(dst), 30.0),
               str(info_dict(dst, "replication")))
        rep, err = reply_or_err(src, "SET", "pre_clean", "1")
        ctx.ok("the master takes writes before the handover", rep == "OK", str(err))
        ctx.ok("...and they reach the replica",
               wait_until(lambda: cmd(dst, "GET", "pre_clean") == "1", SYNC_WAIT))
        err = err_of(dst, "SET", "nope", "1")
        ctx.ok("the replica is read-only going in",
               err is not None and err.startswith("READONLY"),
               f"{err!r} — an instance that still accepts writes on the losing "
               f"side of a handover is the split brain this all exists to avoid")

        full0, ok0, _ = _counters(dst)
        shared_id = info_dict(src, "replication").get("master_replid")
        rep, err = reply_or_err(src, "FAILOVER", "TO", "127.0.0.1", str(dst_port),
                                "TIMEOUT", "10000")
        ctx.ok("the clean FAILOVER is accepted", rep == "OK", str(err))
        ctx.ok("the roles swapped",
               wait_until(lambda: info_dict(dst, "replication").get("role")
                          == "master"
                          and info_dict(src, "replication").get("role") == "slave",
                          20.0),
               f"target={info_dict(dst, 'replication').get('role')} "
               f"source={info_dict(src, 'replication').get('role')}")
        ctx.ok("the demoted instance re-attached",
               wait_until(lambda: _link_up(src), 15.0),
               str(info_dict(src, "replication")))

        full1, ok1, _ = _counters(dst)
        ctx.ok("[REG] a coordinated handover moves NO RDB image",
               full1 == full0 and ok1 == ok0 + 1,
               f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1} — this "
               f"is the entire point of the command. A full resync here means "
               f"the pause let a write through, the ack wait finished early, or "
               f"PSYNC FAILOVER promoted after the resync logic instead of before")

        dd = info_dict(dst, "replication")
        ctx.ok("promotion retired the shared history into master_replid2",
               dd.get("master_replid2") == shared_id,
               f"master_replid2={dd.get('master_replid2')} shared={shared_id}")
        ctx.ok("[REG] the demoted instance adopted the new replid from +CONTINUE",
               info_dict(src, "replication").get("master_replid")
               == dd.get("master_replid"),
               f"demoted={info_dict(src, 'replication').get('master_replid')} "
               f"promoted={dd.get('master_replid')} — quoting the old name on the "
               f"next reconnect asks for a history that has expired past "
               f"second_repl_offset, and every reconnect after this one is full")
        ctx.ok("no data was lost across the coordinated handover",
               cmd(dst, "GET", "pre_clean") == "1",
               "the write acked before the pause must survive it")
        ctx.ok("the new master is writable",
               err_of(dst, "SET", "post_clean", "1") is None)
        ctx.ok("...and streams to the demoted one",
               wait_until(lambda: cmd(src, "GET", "post_clean") == "1", SYNC_WAIT))

        rep, err = reply_or_err(dst, "FAILOVER", "TIMEOUT", "10000")
        ctx.ok("a bare FAILOVER is accepted (the target is chosen automatically)",
               rep == "OK", str(err))
        ctx.ok("...and it handed over to the only replica there is",
               wait_until(lambda: info_dict(src, "replication").get("role")
                          == "master"
                          and info_dict(dst, "replication").get("role") == "slave",
                          20.0),
               "the auto-pick walks g_data.replicas for the highest ack_offset "
               "and needs a replica that reported its listening-port")
    finally:
        for s in (m, r):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        proxy.stop()
        for srv in (rsrv, msrv):
            if srv is not None:
                srv.stop()


def _repl_promotion_history(ctx, master_srv, master, master_port, r1_port, r2_port):
    """Two replicas of one master; the master dies; one replica is promoted; the
    other is repointed at it. The survivor must serve that sibling from the
    history it retired, not force a full RDB out of a cluster already a node down.
    """
    ctx.section("Replication: promotion keeps the history")
    r1 = raw_conn(r1_port)
    r2_dir = ctx.dir("replica2")
    r2_conf = write_conf(os.path.join(r2_dir, "replica2.conf"), [
        f"port {r2_port}",
        "appendonly no",
        'save ""',
        f"replicaof 127.0.0.1 {master_port}",     # straight at the master
    ])
    r2_srv = ctx.start("replica2", r2_dir, r2_conf, r2_port)
    r2 = raw_conn(r2_port)
    try:
        ctx.ok("the second replica attached", wait_until(lambda: _link_up(r2),
                                                          SYNC_WAIT * 2),
               str(info_dict(r2, "replication")))
        for i in range(20):
            cmd(master, "SET", f"hist{i}", "x" * 64)
        ctx.ok("both replicas caught up",
               wait_until(lambda: cmd(r1, "GET", "hist19") == "x" * 64
                          and cmd(r2, "GET", "hist19") == "x" * 64, SYNC_WAIT))

        # repl_backlog_feed() advances master_repl_offset itself, so a surviving
        # manual += counts every byte twice. Nothing downstream can tell you
        # that — the data is still correct — but the offset the replica reports
        # is silently double.
        m_off = info_dict(master, "replication").get("master_repl_offset")
        ctx.ok("[REG] the replica's offset matches the master's exactly",
               wait_until(lambda: info_dict(r1, "replication")
                          .get("master_repl_offset") == m_off, SYNC_WAIT),
               f"master {m_off}, replica "
               f"{info_dict(r1, 'replication').get('master_repl_offset')} — "
               f"roughly double means the STREAMING branch still has its own +=")

        # With no feed the ring is empty at the instant of promotion, and it
        # cannot be backfilled afterwards, so every sibling full-resyncs no
        # matter what repl_id2 says.
        histlen = int(info_dict(r1, "replication").get("repl_backlog_histlen", 0))
        ctx.ok("[REG] a replica feeds its own backlog while streaming",
               histlen > 0,
               "repl_backlog_feed is only called from propagate(), which "
               "propagate_enabled() gates off while g_loading is set")

        old_replid = info_dict(r1, "replication").get("master_replid")

        # Kill the master for real. The proxy stays up with nothing upstream,
        # which is a dead master rather than a partitioned one.
        master_srv.stop()
        ctx.ok("the surviving replica noticed the master is gone",
               wait_until(lambda: not _link_up(r1), 10.0))

        # The promotion gate. This is the ONLY case anyone promotes in, and
        # gating on repl_state (the link phase) instead of replica_mode (the
        # role) makes the command a silent no-op precisely here.
        cmd(r1, "REPLICAOF", "NO", "ONE")
        d = info_dict(r1, "replication")
        ctx.ok("[REG] REPLICAOF NO ONE promotes a replica whose master is DOWN",
               d.get("role") == "master",
               "gated on repl_state instead of replica_mode: repl_link_lost() "
               "sets repl_state NONE and keeps replica_mode, so this no-ops")
        ctx.ok("...and it is writable", err_of(r1, "SET", "k", "v") is None)
        ctx.ok("[REG] promotion RETIRES the old replid into master_replid2",
               d.get("master_replid2") == old_replid,
               f"master_replid2={d.get('master_replid2')} old={old_replid} — 40 "
               f"zeros means repl_shift_id() was never called")
        ctx.ok("second_repl_offset marks the handover point",
               d.get("second_repl_offset", "-1") != "-1"
               and int(d["second_repl_offset"]) > 0,
               d.get("second_repl_offset"))
        ctx.ok("a new replid was still minted", d.get("master_replid") != old_replid)

        # The payoff: the sibling must NAME the old history and the promoted
        # instance must HONOUR it. Both resync paths leave r2 with correct data,
        # so only the counters can tell them apart.
        full0, ok0, _ = _counters(r1)
        cmd(r2, "REPLICAOF", "127.0.0.1", str(r1_port))
        ctx.ok("the sibling re-attached to the promoted instance",
               wait_until(lambda: _link_up(r2), 15.0),
               str(info_dict(r2, "replication")))
        full1, ok1, _ = _counters(r1)
        ctx.ok("[REG] the sibling PARTIAL-resyncs off the retired history",
               ok1 == ok0 + 1 and full1 == full0,
               f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1} — a full "
               f"resync here means repl_id2 was not offered or not honoured")

        cmd(r1, "SET", "post_failover", "yes")
        ctx.ok("the new master streams to the sibling",
               wait_until(lambda: cmd(r2, "GET", "post_failover") == "yes",
                          SYNC_WAIT))

        # A promoted master answers +CONTINUE under its NEW replid and the
        # sibling has to adopt it off that line. Not adopting it is invisible
        # exactly once — the data is right, the counters say partial — and then
        # every reconnect after this one full-resyncs. The storm this exists to
        # prevent comes back on the second reconnect, not the first.
        new_id = info_dict(r1, "replication").get("master_replid")
        ctx.ok("[REG] the sibling adopted the promoted master's replid",
               wait_until(lambda: info_dict(r2, "replication")
                          .get("master_replid") == new_id, SYNC_WAIT),
               f"r2={info_dict(r2, 'replication').get('master_replid')} "
               f"r1={new_id} — the +CONTINUE branch never read the id off the "
               f"line it arrived on")

        if _set_if_present(r2, "repl-timeout", 1):
            _set_if_present(r1, "repl-ping-replica-period", 60)   # real silence
            full2, ok2, _ = _counters(r1)
            # Watch the master's counters, not the replica's link state: the
            # re-dial is faster than any poll interval, so "down" is a state
            # this can miss entirely, while a resync the master SERVED stays put.
            ctx.ok("the sibling's link bounced at least once",
                   wait_until(lambda: sum(_counters(r1)[:2]) > full2 + ok2, 15.0),
                   f"sync_full/ok still {_counters(r1)[:2]} — repl-timeout 1 "
                   f"never dropped a link silent for longer than that")
            _set_if_present(r2, "repl-timeout", 60)
            ctx.ok("the sibling re-dialled on its own",
                   wait_until(lambda: _link_up(r2), 25.0),
                   str(info_dict(r2, "replication")))
            full3, ok3, _ = _counters(r1)
            # However many times it flapped, not one of them may have cost an
            # image. The ZERO is the contract; the other number is weather.
            ctx.ok("[REG] every reconnect after the promotion is still partial",
                   full3 == full2 and ok3 > ok2,
                   f"sync_full {full2}->{full3} sync_partial_ok {ok2}->{ok3} — "
                   f"one partial and then a full on every reconnect after it is "
                   f"the signature of a replica still quoting the dead master's "
                   f"replid")
            _set_if_present(r1, "repl-ping-replica-period", 10)
        else:
            ctx.skip("second reconnect stays partial",
                     "no repl-timeout directive to bounce the link with")
    finally:
        for s in (r1, r2):
            try:
                s.close()
            except OSError:
                pass
    return r2_srv


def phase_replication(ctx: "PhaseCtx"):
    m_port, r_port, p_port = ctx.ports(3)
    r2_port = ctx.port()
    fo_m, fo_r, fo_p = ctx.ports(3)

    mdir, rdir = ctx.dir("repl/master"), ctx.dir("repl/replica")
    mconf = write_conf(os.path.join(mdir, "master.conf"), [
        f"port {m_port}",
        "appendonly no",
        'save ""',
        # small on purpose: it makes "a gap larger than the backlog" cheap
        f"repl-backlog-size {BACKLOG_BYTES}",
    ])
    rconf = write_conf(os.path.join(rdir, "replica.conf"), [
        f"port {r_port}",
        "appendonly no",
        'save ""',
        f"replicaof 127.0.0.1 {p_port}",
    ])
    print(f"  master :{m_port}   proxy :{p_port}   replica :{r_port}")

    proxy = Proxy(p_port, m_port)
    master_srv = replica_srv = None
    master = replica = None
    try:
        master_srv = ctx.start("master", mdir, mconf, m_port)
        master = raw_conn(m_port)
        # seed BEFORE the replica exists, so the resync image has something in it
        for k, v in (("pre1", "a"), ("pre2", "b"), ("pre3", "c")):
            cmd(master, "SET", k, v)

        # Capability probes rather than version strings: the suite stays useful
        # while a milestone is half-applied, and says which half is missing.
        has_counters = "sync_full" in info_dict(master, "stats")
        has_history = "master_replid2" in info_dict(master, "replication")
        has_timeout = has_directive(master, "repl-timeout")
        has_floor = has_directive(master, "min-replicas-to-write")
        fo_err = err_of(master, "FAILOVER", "ABORT")
        has_failover = fo_err is not None and "unknown command" not in fo_err

        proxy.start()
        replica_srv = ctx.start("replica", rdir, rconf, r_port)
        replica = raw_conn(r_port)

        _repl_full_resync(ctx, master, replica, replica_srv)
        _repl_streaming(ctx, master, replica)
        _repl_readonly(ctx, master, replica)
        _repl_link_loss(ctx, replica, proxy)

        if has_counters:
            _repl_partial(ctx, master, replica, proxy, p_port)
            _repl_gap_too_large(ctx, master, replica, proxy, p_port)
            _repl_auto_reconnect(ctx, master, replica, proxy)
        else:
            ctx.skip("partial resync", "INFO has no sync_* counters, so a partial "
                                       "and a full resync are indistinguishable")
            proxy.start()
            cmd(replica, "REPLICAOF", "127.0.0.1", str(p_port))
            wait_until(lambda: _link_up(replica), SYNC_WAIT)

        _repl_wait(ctx, master, m_port)

        if has_floor:
            _repl_min_replicas(ctx, master, replica, proxy)
        else:
            ctx.skip("durability floor", "no min-replicas-to-write directive")

        if has_timeout:
            _repl_timeout_config(ctx, master)
            _repl_idle_keepalive(ctx, master, replica, replica_srv)
            _repl_wedged(ctx, master, replica, master_srv, replica_srv, proxy)
        else:
            ctx.skip("wedged-link detection", "no repl-timeout directive")

        _repl_promotion(ctx, master, replica, rconf, p_port)

        replica.close()
        replica = None
        replica_srv.stop()
        replica_srv = _repl_restart(ctx, rdir, rconf, r_port, master)

        if has_failover:
            _repl_failover(ctx, fo_m, fo_r, fo_p)
        else:
            ctx.skip("coordinated handover", "no FAILOVER command in this binary")

        # LAST: it stops the master on purpose, so nothing may run after it.
        if has_history:
            _repl_promotion_history(ctx, master_srv, master, m_port, r_port, r2_port)
            master = None                  # the phase above stopped the master
        else:
            ctx.skip("sibling partial-resync after promotion",
                     "INFO has no master_replid2")
    finally:
        for s in (master, replica):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        proxy.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: TLS — the handshake, and rotating a certificate without a restart
#
#  The throughput side of TLS is a measurement, not an assertion, and lives
#  outside this suite. What belongs here is the part that can break silently:
#  a certificate swap that is accepted and does nothing, or one that is refused
#  and leaves the configuration pointing at material the server never loaded.
# ═══════════════════════════════════════════════════════════════════════════════

def _selfsigned(dirpath: str, cn: str):
    """A throwaway cert/key pair. Returns (cert, key), or (None, None) when
    openssl is not installed."""
    exe = shutil.which("openssl")
    if not exe:
        return None, None
    key = os.path.join(dirpath, f"{cn}-key.pem")
    crt = os.path.join(dirpath, f"{cn}-cert.pem")
    try:
        subprocess.run(
            [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", crt, "-days", "2", "-subj", f"/CN={cn}"],
            check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None, None
    return crt, key


def _bare_tls_ctx() -> "ssl.SSLContext":
    """Self-signed test material: verification is off by design."""
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _tls_conn(port: int, timeout: float = TIMEOUT_SEC) -> socket.socket:
    raw = socket.create_connection(("127.0.0.1", port), timeout)
    raw.settimeout(timeout)
    return _bare_tls_ctx().wrap_socket(raw, server_hostname=None)


def _peer_fingerprint(port: int, timeout: float = TIMEOUT_SEC):
    s = _tls_conn(port, timeout)
    try:
        der = s.getpeercert(binary_form=True)
    finally:
        s.close()
    return hashlib.sha256(der).hexdigest()[:16] if der else None


def phase_tls(ctx: "PhaseCtx"):
    ctx.section("TLS: handshake and live certificate rotation")
    d = ctx.dir("tls")
    crt, key = _selfsigned(d, "myred-test")
    if not crt:
        ctx.skip("TLS", "openssl is not on PATH, so no test certificate could "
                        "be generated")
        return

    # The server runs on COPIES: rotation overwrites them in place, which is how
    # every real rotation works (certbot, cert-manager, a mounted secret all
    # write new bytes to the same path), and it keeps the repo's own material
    # out of reach.
    live_cert = os.path.join(d, "server-cert.pem")
    live_key = os.path.join(d, "server-key.pem")
    shutil.copyfile(crt, live_cert)
    shutil.copyfile(key, live_key)

    plain_port, tls_port = ctx.ports(2)
    conf = write_conf(os.path.join(d, "srv.conf"), [
        f"port {plain_port}",
        f"tls-port {tls_port}",
        f'tls-cert-file "{live_cert}"',
        f'tls-key-file "{live_key}"',
        "appendonly no",
        'save ""',
    ])
    try:
        srv = ctx.start("tls", d, conf, plain_port)
    except RuntimeError as e:
        ctx.ok("the server boots with TLS configured", False, str(e))
        return
    ctx.ok("the server boots with TLS configured", True)

    # --- both listeners work, and neither confuses the other -----------------
    try:
        t = _tls_conn(tls_port)
        ctx.ok("a TLS handshake completes on the tls-port",
               cmd(t, "PING") == "PONG")
        ctx.ok("RESP works over TLS",
               cmd(t, "SET", "tls:k", "v") == "OK" and cmd(t, "GET", "tls:k") == "v")
        ver = t.version()
        print(f"  {YELLOW}info{RESET} negotiated {ver}, "
              f"cipher {(t.cipher() or ('?',))[0]}")
        t.close()
    except Exception as e:
        ctx.ok("a TLS handshake completes on the tls-port", False,
               f"{type(e).__name__}: {e}")

    p = raw_conn(plain_port)
    ctx.ok("the plaintext port still serves plaintext", cmd(p, "PING") == "PONG")

    # A plaintext client on the TLS port sends a RESP array where a ClientHello
    # is expected. That connection must die and nothing else may.
    junk = socket.create_connection(("127.0.0.1", tls_port), TIMEOUT_SEC)
    junk.settimeout(2.0)
    try:
        junk.sendall(b"*1\r\n$4\r\nPING\r\n")
        junk.recv(64)
    except OSError:
        pass
    finally:
        junk.close()
    ctx.ok("a plaintext client on the TLS port kills only its own connection",
           cmd(p, "PING") == "PONG")

    # ...and the reverse: a TLS ClientHello at the plaintext port.
    try:
        bad = _tls_conn(plain_port, timeout=3.0)
        bad.close()
        handshook = True
    except Exception:
        handshook = False
    ctx.ok("a TLS client on the plaintext port does not get a handshake",
           not handshook)
    ctx.ok("the server survives both mismatches", cmd(p, "PING") == "PONG")

    # --- live rotation -------------------------------------------------------
    if not has_directive(p, "tls-cert-file"):
        ctx.skip("certificate rotation", "no tls-cert-file directive")
        p.close()
        srv.stop()
        return

    fp0 = _peer_fingerprint(tls_port)
    ctx.ok("the presented certificate can be fingerprinted", fp0 is not None)

    # An established connection, held open across the rotation. Surviving it is
    # the entire difference between a reload and a restart.
    held = _tls_conn(tls_port)
    cmd(held, "SET", "tls:held", "before")

    new_crt, new_key = _selfsigned(d, "rotated")
    shutil.copyfile(new_crt, live_cert)
    shutil.copyfile(new_key, live_key)

    t0 = time.perf_counter()
    rep, err = reply_or_err(p, "CONFIG", "SET", "tls-cert-file", live_cert)
    reload_ms = (time.perf_counter() - t0) * 1000.0
    ctx.ok("CONFIG SET tls-cert-file is accepted", rep == "OK", str(err))
    if rep == "OK":
        fp1 = _peer_fingerprint(tls_port)
        ctx.ok("[REG] new connections are served the NEW certificate",
               fp1 is not None and fp1 != fp0,
               f"still presenting {fp0} — the directive was accepted but the "
               f"SSL_CTX was never swapped")
        print(f"  {YELLOW}info{RESET} hot reload took {reload_ms:.2f}ms "
              f"(a restart costs tens of ms and drops every connection)")

    # [REG] tls-key-file must write the KEY field. Its apply() and its get() were
    # once wired to the cert field, which round-trips perfectly and only shows up
    # as a server that will not boot after the next rewrite.
    rep, err = reply_or_err(p, "CONFIG", "SET", "tls-key-file", live_key)
    ctx.ok("CONFIG SET tls-key-file is accepted", rep == "OK", str(err))
    got = cmd(p, "CONFIG", "GET", "tls-key-file")
    got = got[1] if isinstance(got, list) and len(got) >= 2 else None
    ctx.ok("[REG] tls-key-file reads back the key path, not the cert path",
           got == live_key,
           f"tls-key-file reads {got!r}, expected {live_key!r} — the row is "
           f"wired to the wrong g_config field")

    ctx.ok("[REG] the connection established before the rotation still works",
           cmd(held, "GET", "tls:held") == "before",
           "rotating the certificate tore down live sessions — that is a "
           "restart with extra steps")
    held.close()

    # A refused swap must change nothing at all.
    fp_now = _peer_fingerprint(tls_port)
    err = err_of(p, "CONFIG", "SET", "tls-cert-file",
                 os.path.join(d, "does-not-exist.pem"))
    ctx.ok("a nonexistent certificate file is refused", err is not None,
           "CONFIG SET accepted a path that does not exist")
    ctx.ok("the server keeps serving the old certificate after a refusal",
           _peer_fingerprint(tls_port) == fp_now,
           "a failed build must leave the live context untouched")
    cur = cmd(p, "CONFIG", "GET", "tls-cert-file")
    cur = cur[1] if isinstance(cur, list) and len(cur) >= 2 else None
    ctx.ok("[REG] a rejected CONFIG SET rolls the path back",
           cur == live_cert,
           f"tls-cert-file is left as {cur!r} — the next CONFIG REWRITE would "
           f"persist a path the server never loaded, and the boot after that "
           f"fails")
    ctx.ok("the server is still serving plaintext too", cmd(p, "PING") == "PONG")
    p.close()
    srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: UNIT — the incremental-rehash invariant, below the protocol
#
#  A server-level test cannot see this: a hash map that never finishes draining
#  still answers every query correctly, it just degrades toward O(n) and never
#  frees the old table. The source is written out and compiled against the
#  repo's own hashtable.cpp, so it tests the real implementation.
# ═══════════════════════════════════════════════════════════════════════════════

HASHTABLE_UNIT_SRC = r"""
// HMap rehash unit test. What it proves: after a rehash completes, migrate_pos
// is left at the end of the drained table, and the NEXT rehash must restart the
// drain from bucket 0. If it does not, the low buckets of `older` are stranded
// forever, older.tab is never freed, and hm_insert can never trigger a resize
// again. Several full rehash cycles are driven and the draining table must
// always empty out.
#include <cstdio>
#include <cstdint>
#include <cstddef>
#include "hashtable.h"

static int g_pass = 0, g_fail = 0;

static void check(const char *name, bool ok, const char *detail = ""){
    if (ok){ g_pass++; printf("  ok   %s\n", name); }
    else   { g_fail++; printf("  FAIL %s %s\n", name, detail); }
}

struct TestNode {
    HNode node;
    uint64_t key = 0;
};

static uint64_t hash_key(uint64_t k){
    return k * 0x9E3779B97F4A7C15ULL;   // Fibonacci spread, deterministic
}

static TestNode *container_of_test(HNode *n){
    return (TestNode *)((char *)n - offsetof(TestNode, node));
}

static bool node_eq(HNode *a, HNode *b){
    return container_of_test(a)->key == container_of_test(b)->key;
}

static TestNode *lookup(HMap *m, uint64_t key){
    TestNode probe;
    probe.key = key;
    probe.node.hcode = hash_key(key);
    HNode *hit = hm_lookup(m, &probe.node, node_eq);
    return hit ? container_of_test(hit) : nullptr;
}

int main(){
    // enough nodes for several doublings: 4 -> 8 -> 16 -> 32 -> 64 buckets
    constexpr size_t N = 600;
    static TestNode nodes[N];

    HMap map{};

    printf("phase 1: insert %zu keys (drives ~5 rehash cycles)\n", N);
    for (size_t i = 0; i < N; ++i){
        nodes[i].key = i;
        nodes[i].node.hcode = hash_key(i);
        hm_insert(&map, &nodes[i].node);
        (void)lookup(&map, i / 2);   // incremental-drain opportunities
    }
    check("all keys inserted (hm_size)", hm_size(&map) == N);

    printf("phase 2: give the drain every chance to finish\n");
    for (int round = 0; round < 1000; ++round){
        (void)lookup(&map, (uint64_t)round % N);
    }

    check("draining table fully emptied (older.size == 0)",
          map.older.size == 0);
    check("draining table released (older.tab == NULL)",
          map.older.tab == nullptr,
          "-- stranded entries: the rehash never completes");

    printf("phase 3: every key still reachable\n");
    size_t found = 0;
    for (size_t i = 0; i < N; ++i){
        TestNode *t = lookup(&map, i);
        if (t && t->key == i){ found++; }
    }
    check("all keys found after rehash cycles", found == N);

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
"""


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def phase_hashtable_unit(ctx: "PhaseCtx"):
    ctx.section("Unit: HMap incremental rehash")
    root = repo_root()
    impl = os.path.join(root, "hashtable.cpp")
    header = os.path.join(root, "hashtable.h")
    if not (os.path.exists(impl) and os.path.exists(header)):
        ctx.skip("HMap rehash", f"hashtable.cpp/.h not found under {root}")
        return
    cxx = shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        ctx.skip("HMap rehash", "no g++ or clang++ on PATH")
        return

    d = ctx.dir("unit")
    src = os.path.join(d, "test_hashtable.cpp")
    with open(src, "w") as f:
        f.write(HASHTABLE_UNIT_SRC)
    exe = os.path.join(d, "test_hashtable")
    build = subprocess.run(
        [cxx, "-std=c++17", "-O1", "-I", root, "-o", exe, src, impl],
        capture_output=True, text=True, timeout=180)
    if build.returncode != 0:
        ctx.ok("the unit test compiles against hashtable.cpp", False,
               (build.stderr or build.stdout).strip()[-800:])
        return
    ctx.ok("the unit test compiles against hashtable.cpp", True)

    run = subprocess.run([exe], capture_output=True, text=True, timeout=120)
    for line in (run.stdout or "").splitlines():
        if line.strip():
            print("    " + line)
    ctx.ok("[REG] the incremental rehash always finishes draining",
           run.returncode == 0,
           "a rehash that never completes strands the low buckets of the old "
           "table forever: lookups stay correct, the table is never freed, and "
           "no later insert can trigger a resize")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: DIFFERENTIAL — the same operations against MYRED and against Redis
#
#  Hand-written assertions can only check what somebody thought to check. This
#  phase checks against an oracle instead: run an operation on both servers and
#  require the replies to agree. It catches the "SET should discard the TTL"
#  class — where MYRED is self-consistent, every existing test passes, and the
#  behaviour is simply not what Redis does.
#
#  Two rules make the comparison meaningful rather than noisy:
#
#  1. **Only send what MYRED accepts.** MYRED splits what Redis overloads —
#     `SET` is arity 3 with no EX/NX/XX (those are `setex`/`setnx`/`getex`),
#     `LPOP` takes no count, `EXPIRE` no NX|XX|GT|LT. Generating Redis-shaped
#     commands would produce a wall of "divergences" that are feature gaps, not
#     semantic differences. The interesting question is the narrower one: for
#     input MYRED accepts, does it answer what Redis answers?
#
#  2. **Normalize the differences that are deliberate**, and nothing else. Every
#     entry in the tables below is a decision that MYRED is allowed to make
#     differently; anything not listed is compared exactly.
#
#  `ZQUERY`/`ZREVQUERY` have no Redis equivalent at all, so the zset range path
#  has no oracle here and keeps relying on its hand-written assertions.
# ═══════════════════════════════════════════════════════════════════════════════

# Error TEXT is implementation-defined; the error CLASS is the contract. Redis
# says "WRONGTYPE Operation against a key holding the wrong kind of value",
# MYRED says "WRONGTYPE wrong type" — those agree on everything that matters.
# Answering ERR where Redis answers WRONGTYPE does not.
def _diff_err_class(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return text.split()[0].upper()


# Float formatting is deliberate: MYRED prints through %g, Redis emits 17
# significant digits. Applied by REPLY POSITION rather than to anything that
# looks numeric — normalizing every numeric-looking string would also hide a
# GET that returned "1" for a stored "1.0", which is a real corruption.
FLOAT_REPLY = {"zscore", "incrbyfloat"}
FLOAT_AT_ODD_INDEX = {"zpopmin"}          # [member, score, member, score, ...]

# Replies whose ORDER is implementation-defined. Redis makes no ordering promise
# for any of these, so a different order is not a divergence.
UNORDERED_REPLY = {"smembers", "sdiff", "sinter", "sunion", "keys",
                   "hkeys", "hvals"}
PAIRED_UNORDERED_REPLY = {"hgetall"}       # sort by field, keep pairs together

# TTL replies cannot match to the millisecond across two processes. The contract
# is the three-way distinction: -2 no key, -1 no TTL, positive alive.
TTL_REPLY = {"ttl", "pttl"}


def _diff_float(v):
    try:
        return round(float(v), 9)
    except (TypeError, ValueError):
        return v


def _diff_norm(name: str, reply):
    """Apply the deliberate-divergence table to one reply."""
    name = name.lower()

    if name in TTL_REPLY and isinstance(reply, int):
        # -2 and -1 mean different things and must stay distinct; any positive
        # value means the same thing on both servers.
        return reply if reply < 0 else ("alive" if reply > 0 else 0)

    if name in FLOAT_REPLY:
        return _diff_float(reply)

    if name in FLOAT_AT_ODD_INDEX and isinstance(reply, list):
        return [v if i % 2 == 0 else _diff_float(v) for i, v in enumerate(reply)]

    if name in UNORDERED_REPLY and isinstance(reply, list):
        return sorted(reply, key=lambda x: (x is None, x))

    if name in PAIRED_UNORDERED_REPLY and isinstance(reply, list):
        pairs = sorted(zip(reply[0::2], reply[1::2]))
        return [x for pair in pairs for x in pair]

    return reply


def _diff_cmd(ctx: "PhaseCtx", my: socket.socket, rd: socket.socket,
              *args) -> bool:
    """Run one command on both servers and require the replies to agree."""
    name = str(args[0])
    m_rep, m_err = reply_or_err(my, *args)
    r_rep, r_err = reply_or_err(rd, *args)
    shown = " ".join(str(a) for a in args)

    if m_err or r_err:
        ok = _diff_err_class(m_err) == _diff_err_class(r_err)
        return ctx.ok(f"= {shown}", ok,
                      f"MYRED: {m_err or ('reply ' + repr(m_rep))}\n"
                      f"    redis: {r_err or ('reply ' + repr(r_rep))}")

    m, r = _diff_norm(name, m_rep), _diff_norm(name, r_rep)
    return ctx.ok(f"= {shown}", m == r,
                  f"MYRED: {m_rep!r}\n    redis: {r_rep!r}"
                  + (f"\n    (normalized: {m!r} vs {r!r})" if m != m_rep or r != r_rep
                     else ""))


def _diff_dump(sock: socket.socket, myred: bool) -> dict:
    """Whole-keyspace snapshot, in a form comparable across implementations.

    Replies can agree op by op while the two states drift apart — that is the
    shape of the SPOP-propagation bug — so the states get compared directly too.
    """
    out = {}
    # Second asymmetric read: MYRED's KEYS takes no pattern, Redis's requires
    # one. Like the zset dump below, each side is read with the command it has.
    everything = cmd(sock, "KEYS") if myred else cmd(sock, "KEYS", "*")
    for k in sorted(everything or []):
        t = cmd(sock, "TYPE", k)
        if t == "string":
            v = cmd(sock, "GET", k)
        elif t == "list":
            v = cmd(sock, "LRANGE", k, "0", "-1")
        elif t == "set":
            v = sorted(cmd(sock, "SMEMBERS", k) or [])
        elif t == "hash":
            flat = cmd(sock, "HGETALL", k) or []
            v = sorted(zip(flat[0::2], flat[1::2]))
        elif t == "zset":
            # The one asymmetric read: MYRED has no ZRANGE and Redis has no
            # ZQUERY, so each side is dumped with the command it actually has.
            if myred:
                # "-inf", not a large negative literal: a member scored -inf
                # sorts BELOW -1e308, so seeding the seek with a finite score
                # silently omits it and reports a phantom state divergence.
                flat = cmd(sock, "ZQUERY", k, "-inf", "", "0", "10000") or []
            else:
                flat = cmd(sock, "ZRANGE", k, "0", "-1", "WITHSCORES") or []
            v = sorted((m, _diff_float(s))
                       for m, s in zip(flat[0::2], flat[1::2]))
        else:
            v = f"<{t}>"
        ttl = cmd(sock, "PTTL", k)
        out[k] = (t, v, ttl if ttl < 0 else "alive")
    return out


def _diff_state(ctx: "PhaseCtx", my: socket.socket, rd: socket.socket,
                label: str, reported: set):
    """Compare the two keyspaces, reporting each diverged key exactly once.

    Then DELETE the diverged keys from both servers. Without that, one early
    divergence is re-reported by every later group — the first run of this phase
    turned two root causes into nine failures — and worse, every subsequent
    operation touching that key inherits the difference, so the noise grows.
    Deleting re-converges the two states and lets the rest of the stream keep
    testing what it was written to test.
    """
    m, r = _diff_dump(my, True), _diff_dump(rd, False)

    only_m = sorted(k for k in set(m) - set(r) if k not in reported)
    only_r = sorted(k for k in set(r) - set(m) if k not in reported)
    ctx.ok(f"[state] {label}: same key set", not only_m and not only_r,
           f"only in MYRED: {only_m[:8]}\n    only in redis: {only_r[:8]}")

    diff = [k for k in sorted(set(m) & set(r))
            if m[k] != r[k] and k not in reported]
    ctx.ok(f"[state] {label}: same value for every shared key", not diff,
           "\n    ".join(f"{k}: MYRED {m[k]!r} vs redis {r[k]!r}"
                         for k in diff[:5]))

    for k in only_m + only_r + diff:
        reported.add(k)
        for sock in (my, rd):
            try:
                cmd(sock, "DEL", k)
            except RespError:
                pass


# ─── the operation stream ─────────────────────────────────────────────────────
#
# Hand-written for now, deliberately. Randomized generation comes next, and
# going straight there would mean debugging the normalization table and the
# generator at the same time, on ground where nobody knows the right answer.
# These are the categories where the two implementations are most likely to
# disagree, and where I already know what the answer should be.
#
# Deliberately absent: SPOP/SRANDMEMBER/RANDOMKEY (they pick a different victim
# on each server, so the states diverge on the first call and everything after
# is noise) and MULTI/EXEC (EXEC's reply is an array that needs normalizing per
# queued command — worth doing, but not while the plumbing is unproven).

DIFF_OPS = [
    ("strings", [
        ("SET", "s", "hello"), ("GET", "s"), ("STRLEN", "s"),
        ("APPEND", "s", " world"), ("GET", "s"), ("STRLEN", "s"),
        ("APPEND", "fresh", "made-by-append"), ("GET", "fresh"),
        ("GETRANGE", "s", "0", "4"), ("GETRANGE", "s", "-5", "-1"),
        ("GETRANGE", "s", "0", "-1"), ("GETRANGE", "s", "5", "3"),
        ("GETRANGE", "s", "99", "200"), ("GETRANGE", "missing", "0", "-1"),
        ("SETRANGE", "s", "6", "WORLD"), ("GET", "s"),
        ("SETRANGE", "padded", "5", "x"), ("GET", "padded"),
        ("STRLEN", "padded"),
        ("GETSET", "s", "replaced"), ("GET", "s"),
        ("GETSET", "brand_new", "first"), ("GET", "brand_new"),
        ("SETNX", "s", "nope"), ("GET", "s"),
        ("SETNX", "setnx_new", "yes"), ("GET", "setnx_new"),
        ("GETDEL", "setnx_new"), ("EXISTS", "setnx_new"),
        ("GETDEL", "never_existed"),
        ("SET", "empty", ""), ("GET", "empty"), ("STRLEN", "empty"),
        ("GET", "definitely_missing"),
    ]),
    ("numeric edges", [
        ("SET", "n", "10"), ("INCR", "n"), ("DECR", "n"), ("INCRBY", "n", "40"),
        ("DECRBY", "n", "15"), ("GET", "n"),
        ("INCRBY", "n", "-5"), ("DECRBY", "n", "-5"), ("GET", "n"),
        ("INCR", "counter_from_nothing"), ("GET", "counter_from_nothing"),
        ("SET", "notnum", "abc"), ("INCR", "notnum"), ("INCRBY", "notnum", "1"),
        # str2int() is strtoll() with only an end-of-string check, so it accepts
        # everything strtoll skips or tolerates: leading whitespace, a '+' sign,
        # and leading zeros. Redis's string2ll rejects all three. Every failure
        # in this block is the same root cause, at commands.cpp:57.
        ("SET", "spacey", " 1"), ("INCR", "spacey"),
        ("SET", "plussed", "+1"), ("INCR", "plussed"),
        ("SET", "leadzero", "01"), ("INCR", "leadzero"),
        ("SET", "tabbed", "\t3"), ("INCR", "tabbed"),
        # ...and strtoll SATURATES on overflow while errno is never checked, so
        # a 20-digit value parses as INT64_MIN and increments to a number that
        # has nothing to do with what was stored. The positive side is caught
        # only by accident, when INCR's own overflow check trips on INT64_MAX+1.
        ("SET", "huge_neg", "-99999999999999999999"), ("INCR", "huge_neg"),
        ("SET", "huge_pos", "99999999999999999999"), ("INCR", "huge_pos"),
        ("SET", "floaty", "1.5"), ("INCR", "floaty"),
        # int64 boundaries: the classic overflow pair
        ("SET", "maxi", "9223372036854775807"), ("INCR", "maxi"),
        ("SET", "mini", "-9223372036854775808"), ("DECR", "mini"),
        ("SET", "maxi2", "9223372036854775806"), ("INCRBY", "maxi2", "2"),
        ("INCRBY", "n", "9223372036854775807"),
        ("SET", "f", "10.5"), ("INCRBYFLOAT", "f", "0.1"),
        ("INCRBYFLOAT", "f", "-3.5"), ("INCRBYFLOAT", "f", "0"),
        ("INCRBYFLOAT", "float_from_nothing", "1.5"),
        ("INCRBYFLOAT", "f", "abc"),
        ("INCRBYFLOAT", "notnum", "1"),
    ]),
    ("expiry", [
        ("SET", "e", "v"), ("TTL", "e"), ("PTTL", "e"),
        ("EXPIRE", "e", "100"), ("TTL", "e"),
        ("PERSIST", "e"), ("TTL", "e"), ("PERSIST", "e"),
        ("TTL", "no_such_key"), ("PTTL", "no_such_key"),
        ("PERSIST", "no_such_key"),
        # a past deadline deletes the key; both must agree it is gone
        ("SET", "e2", "v"), ("EXPIRE", "e2", "0"), ("EXISTS", "e2"),
        ("SET", "e3", "v"), ("EXPIRE", "e3", "-1"), ("EXISTS", "e3"),
        ("SET", "e4", "v"), ("PEXPIRE", "e4", "-1"), ("EXISTS", "e4"),
        ("EXPIRE", "no_such_key", "100"),
        # the same lax str2int, on the ARGUMENT side: 34 call sites parse
        # counts, indices, offsets and TTLs through it, so MYRED accepts
        # commands Redis refuses outright
        ("SET", "argp", "v"), ("EXPIRE", "argp", " 100"), ("TTL", "argp"),
        ("SET", "e5", "v"), ("EXPIREAT", "e5", "1"), ("EXISTS", "e5"),
        ("SET", "e6", "v"), ("PEXPIREAT", "e6", "1"), ("EXISTS", "e6"),
        ("SETEX", "sx", "100", "v"), ("GET", "sx"), ("TTL", "sx"),
        ("SETEX", "sx_bad", "0", "v"), ("SETEX", "sx_bad", "-1", "v"),
        ("PSETEX", "px", "100000", "v"), ("TTL", "px"),
        # SET must discard an existing TTL — the class this phase exists for
        ("SET", "sx", "overwritten"), ("TTL", "sx"),
    ]),
    ("lists", [
        ("RPUSH", "l", "a", "b", "c"), ("LLEN", "l"),
        ("LPUSH", "l", "z"), ("LRANGE", "l", "0", "-1"),
        ("LINDEX", "l", "0"), ("LINDEX", "l", "-1"), ("LINDEX", "l", "99"),
        ("LRANGE", "l", "0", "0"), ("LRANGE", "l", "-2", "-1"),
        ("LRANGE", "l", "5", "1"), ("LRANGE", "l", "-100", "100"),
        ("LRANGE", "no_list", "0", "-1"),
        ("LRANGE", "l", "+0", "-1"), ("LINDEX", "l", "01"),
        ("LSET", "l", "1", "B"), ("LRANGE", "l", "0", "-1"),
        ("LSET", "l", "99", "nope"), ("LSET", "no_list", "0", "x"),
        ("LINSERT", "l", "BEFORE", "B", "beforeB"),
        ("LINSERT", "l", "AFTER", "B", "afterB"),
        ("LINSERT", "l", "BEFORE", "absent", "nope"),
        ("LINSERT", "no_list", "BEFORE", "a", "x"),
        ("LRANGE", "l", "0", "-1"),
        ("RPUSH", "dups", "x", "x", "y", "x"),
        ("LREM", "dups", "2", "x"), ("LRANGE", "dups", "0", "-1"),
        ("LREM", "dups", "0", "x"), ("LRANGE", "dups", "0", "-1"),
        ("RPUSH", "trim", "1", "2", "3", "4", "5"),
        ("LTRIM", "trim", "1", "3"), ("LRANGE", "trim", "0", "-1"),
        ("LTRIM", "trim", "5", "1"), ("EXISTS", "trim"),
        ("LPOP", "l"), ("RPOP", "l"), ("LRANGE", "l", "0", "-1"),
        ("LPOP", "no_list"), ("RPOP", "no_list"),
        # draining a list to empty must drop the key on both
        ("RPUSH", "drain", "only"), ("LPOP", "drain"), ("EXISTS", "drain"),
    ]),
    ("hashes", [
        ("HSET", "h", "f1", "v1", "f2", "v2"), ("HLEN", "h"),
        ("HGET", "h", "f1"), ("HGET", "h", "absent"), ("HGET", "no_hash", "f"),
        ("HEXISTS", "h", "f1"), ("HEXISTS", "h", "absent"),
        ("HSET", "h", "f1", "changed"), ("HGET", "h", "f1"),
        ("HSETNX", "h", "f1", "nope"), ("HGET", "h", "f1"),
        ("HSETNX", "h", "f3", "yes"), ("HGET", "h", "f3"),
        ("HSTRLEN", "h", "f1"), ("HSTRLEN", "h", "absent"),
        ("HKEYS", "h"), ("HVALS", "h"), ("HGETALL", "h"),
        ("HKEYS", "no_hash"), ("HVALS", "no_hash"), ("HGETALL", "no_hash"),
        ("HMGET", "h", "f1", "absent", "f2"),
        ("HMGET", "no_hash", "a", "b"),
        ("HSET", "hn", "num", "10"), ("HINCRBY", "hn", "num", "5"),
        ("HINCRBY", "hn", "num", "-15"), ("HGET", "hn", "num"),
        ("HINCRBY", "hn", "fresh", "7"), ("HGET", "hn", "fresh"),
        ("HSET", "hn", "notnum", "abc"), ("HINCRBY", "hn", "notnum", "1"),
        ("HDEL", "h", "f1"), ("HDEL", "h", "absent"), ("HLEN", "h"),
        # emptying a hash must drop the key
        ("HSET", "hdrain", "only", "v"), ("HDEL", "hdrain", "only"),
        ("EXISTS", "hdrain"),
    ]),
    ("sets", [
        ("SADD", "st", "a", "b", "c"), ("SCARD", "st"),
        ("SADD", "st", "a"), ("SCARD", "st"),
        ("SISMEMBER", "st", "a"), ("SISMEMBER", "st", "zzz"),
        ("SISMEMBER", "no_set", "a"),
        ("SMISMEMBER", "st", "a", "zzz", "b"),
        ("SMEMBERS", "st"), ("SMEMBERS", "no_set"),
        ("SREM", "st", "a"), ("SREM", "st", "absent"), ("SMEMBERS", "st"),
        ("SADD", "s2", "b", "c", "d"),
        ("SDIFF", "st", "s2"), ("SINTER", "st", "s2"), ("SUNION", "st", "s2"),
        ("SDIFF", "st", "no_set"), ("SINTER", "st", "no_set"),
        ("SUNION", "no_set", "no_set2"),
        ("SDIFFSTORE", "d_dst", "st", "s2"), ("SMEMBERS", "d_dst"),
        ("SINTERSTORE", "i_dst", "st", "s2"), ("SMEMBERS", "i_dst"),
        ("SUNIONSTORE", "u_dst", "st", "s2"), ("SMEMBERS", "u_dst"),
        # a store whose result is empty must not leave an empty key behind
        ("SINTERSTORE", "empty_dst", "st", "no_set"), ("EXISTS", "empty_dst"),
        ("SMOVE", "st", "s2", "c"), ("SMEMBERS", "st"), ("SMEMBERS", "s2"),
        ("SMOVE", "st", "s2", "absent"),
        ("SMOVE", "no_set", "s2", "x"),
        ("SADD", "sdrain", "only"), ("SREM", "sdrain", "only"),
        ("EXISTS", "sdrain"),
    ]),
    ("sorted sets", [
        ("ZADD", "z", "1", "a", "2", "b", "3", "c"),
        ("ZSCORE", "z", "a"), ("ZSCORE", "z", "absent"),
        ("ZSCORE", "no_zset", "a"),
        ("ZADD", "z", "1.5", "a"), ("ZSCORE", "z", "a"),
        ("ZRANK", "z", "a"), ("ZRANK", "z", "c"), ("ZRANK", "z", "absent"),
        ("ZRANK", "no_zset", "a"),
        ("ZADD", "z", "notanumber", "bad"),
        ("ZADD", "z", "1"),
        ("ZREM", "z", "a"), ("ZREM", "z", "absent"), ("ZSCORE", "z", "a"),
        ("ZPOPMIN", "z"), ("ZPOPMIN", "z", "2"), ("ZPOPMIN", "no_zset"),
        ("ZADD", "zdrain", "1", "only"), ("ZPOPMIN", "zdrain"),
        ("EXISTS", "zdrain"),
        ("ZADD", "zneg", "-1.5", "neg", "0", "zero"),
        ("ZSCORE", "zneg", "neg"), ("ZSCORE", "zneg", "zero"),
    ]),
    ("wrong types", [
        ("SET", "wt_str", "v"), ("RPUSH", "wt_list", "v"),
        ("HSET", "wt_hash", "f", "v"), ("SADD", "wt_set", "v"),
        ("ZADD", "wt_zset", "1", "v"),
        ("GET", "wt_list"), ("GET", "wt_hash"), ("GET", "wt_set"),
        ("GET", "wt_zset"),
        ("LPUSH", "wt_str", "x"), ("LRANGE", "wt_str", "0", "-1"),
        ("LLEN", "wt_hash"),
        ("HGET", "wt_str", "f"), ("HGETALL", "wt_list"),
        ("SADD", "wt_str", "x"), ("SMEMBERS", "wt_hash"),
        ("SCARD", "wt_zset"),
        ("ZADD", "wt_str", "1", "m"), ("ZSCORE", "wt_list", "m"),
        ("INCR", "wt_list"), ("APPEND", "wt_set", "x"),
        ("STRLEN", "wt_hash"), ("GETRANGE", "wt_set", "0", "-1"),
        ("SETRANGE", "wt_list", "0", "x"),
        ("EXPIRE", "wt_list", "100"), ("TTL", "wt_list"),
        ("TYPE", "wt_str"), ("TYPE", "wt_list"), ("TYPE", "wt_hash"),
        ("TYPE", "wt_set"), ("TYPE", "wt_zset"), ("TYPE", "no_such_key"),
        # [FUZZ] found by the randomized rounds, pinned here so they reproduce
        # without a seed. The SET family REPLACES a key of any type in Redis —
        # refusing with WRONGTYPE makes SET unusable on a key an application
        # reused for something else. GETSET is correctly WRONGTYPE on both,
        # because it has to return the old value as a string.
        ("SET", "wt_list", "replaced"), ("GET", "wt_list"),
        ("SETEX", "wt_hash", "100", "replaced"), ("GET", "wt_hash"),
        ("PSETEX", "wt_set", "100000", "replaced"), ("GET", "wt_set"),
        ("MSET", "wt_zset", "replaced"), ("GET", "wt_zset"),
        # [FUZZ] Redis validates numeric ARGUMENTS before it looks the key up,
        # so a bad index outranks both a wrong type and a missing key
        ("RPUSH", "wt_list2", "a"),
        ("LRANGE", "wt_str", "abc", "1"), ("LTRIM", "no_such_key", "abc", "1"),
        ("LRANGE", "no_such_key", "abc", "1"),
        # [FUZZ] SDIFF/SINTER short-circuit on an empty or missing key and never
        # type-check the rest. SUNION has to read every key, so it agrees.
        ("SADD", "wt_realset", "m"),
        ("SDIFF", "no_such_key", "wt_str"),
        ("SINTER", "wt_realset", "no_such_key", "wt_str"),
        ("SDIFFSTORE", "wt_dst", "no_such_key", "wt_str"),
        ("SUNION", "no_such_key", "wt_str"),
    ]),
    ("multi-key and generic", [
        ("MSET", "m1", "a", "m2", "b", "m3", "c"),
        ("MGET", "m1", "m2", "m3"), ("MGET", "m1", "absent", "m3"),
        ("MGET", "absent1", "absent2"),
        ("MSETNX", "m4", "d", "m5", "e"), ("MGET", "m4", "m5"),
        ("MSETNX", "m1", "clobber", "m6", "f"), ("GET", "m1"), ("EXISTS", "m6"),
        ("EXISTS", "m1"), ("EXISTS", "m1", "m2", "absent"),
        ("EXISTS", "m1", "m1"),
        ("TOUCH", "m1", "absent"), ("DBSIZE",),
        ("RENAME", "m1", "m1_renamed"), ("GET", "m1_renamed"), ("EXISTS", "m1"),
        ("RENAME", "absent_src", "whatever"),
        ("RENAMENX", "m2", "m3"), ("RENAMENX", "m2", "m2_new"),
        ("GET", "m2_new"),
        ("DEL", "m3"), ("DEL", "m3"), ("DEL", "m4", "m5", "absent"),
        ("UNLINK", "m2_new"), ("EXISTS", "m2_new"),
        ("ECHO", "round-trip"), ("PING",), ("PING", "custom"),
    ]),
]


# ─── randomized generation ────────────────────────────────────────────────────
#
# The hand-written list above only checks what somebody thought of. This part
# generates operation streams instead, from a small pool of keys so that type
# collisions, overwrites and drains happen constantly — those are where the
# implementations disagree, not in long runs of unrelated keys.
#
# Everything the generator emits must be DETERMINISTIC across two processes, so
# three families are excluded by construction:
#   - value-nondeterministic mutations (SPOP without a member, SRANDMEMBER,
#     RANDOMKEY) pick a different victim on each server, and the states diverge
#     on the first call,
#   - short TTLs, because a key can expire between the two sends,
#   - anything implementation-defined (INFO/CONFIG/OBJECT/MEMORY/SCAN cursors)
#     or connection-scoped (MULTI, SUBSCRIBE).

DIFF_KEYS = [f"k{i}" for i in range(6)]
DIFF_FIELDS = ["f0", "f1", "f2", ""]
DIFF_MEMBERS = ["m0", "m1", "m2", "a", "z", ""]
DIFF_VALUES = [
    "", "0", "1", "-1", "10", "007", "+7", " 1", "1 ", "3.14", "-0",
    "abc", "a b", "x" * 200, "\x01\x02", "héllo",
    "9223372036854775807", "-9223372036854775808", "99999999999999999999",
]
DIFF_INTS = ["0", "1", "-1", "2", "3", "-3", "100", "-100",
             "9223372036854775807", "-9223372036854775808", "01", " 1", "abc"]
DIFF_SCORES = ["0", "1", "-1", "1.5", "-2.5", "3e3", "inf", "-inf", "abc"]
DIFF_TTLS = ["100", "10000", "0", "-1"]      # 0 and -1 delete, deterministically


def _diff_gen(rng: "random.Random"):
    """One random operation, always within MYRED's own arity."""
    k = rng.choice(DIFF_KEYS)
    k2 = rng.choice(DIFF_KEYS)
    v = rng.choice(DIFF_VALUES)
    f = rng.choice(DIFF_FIELDS)
    m = rng.choice(DIFF_MEMBERS)
    i = rng.choice(DIFF_INTS)
    sc = rng.choice(DIFF_SCORES)

    shapes = [
        # strings
        ("SET", k, v), ("GET", k), ("APPEND", k, v), ("STRLEN", k),
        ("GETSET", k, v), ("SETNX", k, v), ("GETDEL", k),
        ("GETRANGE", k, i, rng.choice(DIFF_INTS)),
        ("SETRANGE", k, rng.choice(["0", "1", "5", "-1"]), v),
        # numeric
        ("INCR", k), ("DECR", k), ("INCRBY", k, i), ("DECRBY", k, i),
        ("INCRBYFLOAT", k, sc),
        # expiry — long TTLs only, plus the deterministic past deadlines
        ("EXPIRE", k, rng.choice(DIFF_TTLS)), ("TTL", k), ("PERSIST", k),
        ("PEXPIRE", k, rng.choice(["100000", "-1"])), ("PTTL", k),
        ("SETEX", k, rng.choice(["100", "0", "-1"]), v),
        # lists
        ("RPUSH", k, v), ("LPUSH", k, v), ("LPOP", k), ("RPOP", k),
        ("LLEN", k), ("LINDEX", k, i), ("LRANGE", k, i, rng.choice(DIFF_INTS)),
        ("LSET", k, i, v), ("LREM", k, i, v),
        ("LTRIM", k, i, rng.choice(DIFF_INTS)),
        ("LINSERT", k, rng.choice(["BEFORE", "AFTER"]), v, rng.choice(DIFF_VALUES)),
        # hashes
        ("HSET", k, f, v), ("HGET", k, f), ("HDEL", k, f), ("HEXISTS", k, f),
        ("HLEN", k), ("HSTRLEN", k, f), ("HSETNX", k, f, v),
        ("HINCRBY", k, f, i), ("HKEYS", k), ("HVALS", k), ("HGETALL", k),
        ("HMGET", k, f, rng.choice(DIFF_FIELDS)),
        # sets — SREM with a chosen member, never SPOP
        ("SADD", k, m), ("SREM", k, m), ("SCARD", k), ("SISMEMBER", k, m),
        ("SMEMBERS", k), ("SMISMEMBER", k, m, rng.choice(DIFF_MEMBERS)),
        ("SMOVE", k, k2, m),
        ("SDIFF", k, k2), ("SINTER", k, k2), ("SUNION", k, k2),
        ("SDIFFSTORE", k2, k, rng.choice(DIFF_KEYS)),
        ("SINTERSTORE", k2, k, rng.choice(DIFF_KEYS)),
        ("SUNIONSTORE", k2, k, rng.choice(DIFF_KEYS)),
        # sorted sets (ZQUERY/ZREVQUERY have no Redis equivalent, so no oracle)
        ("ZADD", k, sc, m), ("ZSCORE", k, m), ("ZRANK", k, m), ("ZREM", k, m),
        ("ZPOPMIN", k), ("ZPOPMIN", k, rng.choice(["1", "2"])),
        # generic
        ("EXISTS", k), ("EXISTS", k, k2), ("TYPE", k), ("DEL", k),
        ("UNLINK", k), ("TOUCH", k, k2), ("DBSIZE",),
        ("RENAME", k, k2), ("RENAMENX", k, k2),
        ("MSET", k, v, k2, rng.choice(DIFF_VALUES)), ("MGET", k, k2),
        ("MSETNX", k, v, k2, rng.choice(DIFF_VALUES)),
    ]
    return rng.choice(shapes)


def _diff_signature(my: socket.socket, rd: socket.socket, ops) -> Optional[tuple]:
    """Replay `ops` on both servers from empty and return the FIRST divergence
    as a comparable signature, or None if the two agreed throughout.

    The signature is what makes shrinking honest: a smaller op list that
    diverges for a *different* reason is not a reduction of this failure, it is
    a second bug wearing the first one's clothes.
    """
    cmd(my, "FLUSHALL")
    cmd(rd, "FLUSHALL")
    for args in ops:
        name = str(args[0])
        m_rep, m_err = reply_or_err(my, *args)
        r_rep, r_err = reply_or_err(rd, *args)
        if m_err or r_err:
            if _diff_err_class(m_err) != _diff_err_class(r_err):
                return ("reply", name, _diff_err_class(m_err),
                        _diff_err_class(r_err))
            continue
        m, r = _diff_norm(name, m_rep), _diff_norm(name, r_rep)
        if m != r:
            return ("reply", name, repr(m), repr(r))

    dm, dr = _diff_dump(my, True), _diff_dump(rd, False)
    if set(dm) != set(dr):
        return ("state-keys", tuple(sorted(set(dm) ^ set(dr))[:4]))
    bad = [k for k in sorted(set(dm) & set(dr)) if dm[k] != dr[k]]
    if bad:
        return ("state-value", bad[0], repr(dm[bad[0]]), repr(dr[bad[0]]))
    return None


def _diff_shrink(my: socket.socket, rd: socket.socket, ops, signature):
    """Delta-debug the op list down to a minimal sequence that still produces
    the SAME divergence. A 200-op repro is a curiosity; a 3-op repro is a bug
    report."""
    n = max(len(ops) // 2, 1)
    while n >= 1:
        i = 0
        while i < len(ops):
            candidate = ops[:i] + ops[i + n:]
            if candidate and _diff_signature(my, rd, candidate) == signature:
                ops = candidate           # still fails the same way: keep it off
            else:
                i += n
        if n == 1:
            break
        n //= 2
    return ops


def _diff_random_rounds(ctx: "PhaseCtx", my: socket.socket, rd: socket.socket,
                        rounds: int, per_round: int, seed: int):
    ctx.section(f"Differential: randomized streams (seed {seed})")
    print(f"  {rounds} rounds x {per_round} ops, pool of {len(DIFF_KEYS)} keys")
    found = 0
    for r_i in range(rounds):
        rng = random.Random(seed + r_i)
        ops = [_diff_gen(rng) for _ in range(per_round)]
        sig = _diff_signature(my, rd, ops)
        if sig is None:
            ctx.ok(f"round {r_i} ({per_round} ops, seed {seed + r_i}) agrees", True)
            continue
        found += 1
        minimal = _diff_shrink(my, rd, ops, sig)
        repro = "\n      ".join(" ".join(str(a) for a in o) for o in minimal)
        ctx.ok(f"round {r_i} ({per_round} ops, seed {seed + r_i}) agrees", False,
               f"{sig[0]} divergence, shrunk from {len(ops)} ops to "
               f"{len(minimal)}:\n      {repro}\n"
               f"    MYRED {sig[-2]}  vs  redis {sig[-1]}"
               if len(sig) >= 3 else f"{sig}")
    if not found:
        print(f"  {YELLOW}info{RESET} no divergence in "
              f"{rounds * per_round} generated operations")
    # Leave both servers empty so the caller's state is what it expects.
    cmd(my, "FLUSHALL")
    cmd(rd, "FLUSHALL")


# ─── transactions and cursor iteration ────────────────────────────────────────
#
# Two shapes the plain op-by-op diff cannot express.
#
# EXEC returns an ARRAY of replies, one per queued command, so normalizing it
# needs to know which command produced each element — the per-command tables
# above are indexed by name, and by the time EXEC answers, the names are gone
# unless they were recorded on the way in.
#
# SCAN's cursor values are implementation-defined and will never match, so
# comparing a single call is meaningless. What IS comparable is the set of
# elements a full iteration yields, which is the actual contract.

def _diff_txn(ctx: "PhaseCtx", my: socket.socket, rd: socket.socket,
              label: str, queued):
    """MULTI, queue commands, EXEC — comparing EXEC's array element by element.

    Only commands that succeed are queued here: a runtime error inside EXEC
    arrives as a `-ERR` element mid-array, and recv_response raises on it, which
    would desynchronise the stream rather than report a difference. The abort
    paths are compared separately, at the top level, where an error is the whole
    reply.
    """
    for sock in (my, rd):
        cmd(sock, "MULTI")
    ok = True
    for args in queued:
        m_rep, m_err = reply_or_err(my, *args)
        r_rep, r_err = reply_or_err(rd, *args)
        if (m_err is None) != (r_err is None) or m_rep != r_rep:
            ok = ctx.ok(f"[txn {label}] queued {' '.join(map(str, args))}", False,
                        f"MYRED: {m_err or m_rep!r}\n    redis: {r_err or r_rep!r}"
                        ) and ok
    m_exec, m_err = reply_or_err(my, "EXEC")
    r_exec, r_err = reply_or_err(rd, "EXEC")

    if m_err or r_err:
        return ctx.ok(f"[txn {label}] EXEC",
                      _diff_err_class(m_err) == _diff_err_class(r_err),
                      f"MYRED: {m_err or m_exec!r}\n    redis: {r_err or r_exec!r}")

    if not isinstance(m_exec, list) or not isinstance(r_exec, list):
        return ctx.ok(f"[txn {label}] EXEC returns an array",
                      type(m_exec) is type(r_exec),
                      f"MYRED: {m_exec!r}\n    redis: {r_exec!r}")
    if len(m_exec) != len(r_exec):
        return ctx.ok(f"[txn {label}] EXEC returns one reply per queued command",
                      False, f"MYRED {len(m_exec)} vs redis {len(r_exec)} "
                             f"for {len(queued)} queued")

    bad = []
    for args, m_el, r_el in zip(queued, m_exec, r_exec):
        name = str(args[0])
        if _diff_norm(name, m_el) != _diff_norm(name, r_el):
            bad.append(f"{' '.join(map(str, args))}: MYRED {m_el!r} vs redis {r_el!r}")
    return ctx.ok(f"[txn {label}] every element of EXEC agrees ({len(queued)})",
                  not bad, "\n    ".join(bad[:4])) and ok


def _diff_scan_all(sock: socket.socket, name: str, *prefix) -> list:
    """Drive a SCAN-family cursor to completion and return everything it yielded.

    The cursor values themselves are implementation-defined — Redis's are
    reverse-binary increments over its table size, MYRED's are its own — so a
    per-call comparison is meaningless. The set of elements a full iteration
    produces is the part that is actually promised.
    """
    seen, cursor, guard = [], "0", 0
    while True:
        reply = cmd(sock, name, *prefix, cursor, "COUNT", "7")
        if not isinstance(reply, list) or len(reply) != 2:
            raise RespError(f"{name} did not return [cursor, items]: {reply!r}")
        cursor, items = reply[0], reply[1] or []
        seen.extend(items)
        guard += 1
        if cursor == "0":
            return seen
        if guard > 2000:
            raise RespError(f"{name} cursor never returned to 0 after {guard} calls")


def _diff_scan_cmp(ctx: "PhaseCtx", my: socket.socket, rd: socket.socket,
                   label: str, name: str, *prefix):
    try:
        m = _diff_scan_all(my, name, *prefix)
        r = _diff_scan_all(rd, name, *prefix)
    except RespError as e:
        return ctx.ok(f"[scan] {label}: full iteration completes", False, str(e))
    # SCAN may return an element more than once across calls; what it promises
    # is that everything present throughout is returned at least once.
    ctx.ok(f"[scan] {label}: same elements over a full iteration",
           sorted(set(m)) == sorted(set(r)),
           f"only in MYRED: {sorted(set(m) - set(r))[:6]}\n"
           f"    only in redis: {sorted(set(r) - set(m))[:6]}")


def _diff_transactions_and_scan(ctx: "PhaseCtx", my: socket.socket,
                                rd: socket.socket):
    ctx.section("Differential: transactions")
    for sock in (my, rd):
        cmd(sock, "FLUSHALL")
        cmd(sock, "SET", "t:str", "v")
        cmd(sock, "RPUSH", "t:list", "a", "b", "c")
        cmd(sock, "HSET", "t:hash", "f", "v")
        cmd(sock, "SADD", "t:set", "m1", "m2")
        cmd(sock, "ZADD", "t:zset", "1", "a", "2", "b")

    _diff_txn(ctx, my, rd, "mixed reads", [
        ("GET", "t:str"), ("LLEN", "t:list"), ("HGET", "t:hash", "f"),
        ("SCARD", "t:set"), ("ZSCORE", "t:zset", "a"), ("TYPE", "t:list"),
        ("EXISTS", "t:str"), ("TTL", "t:str"),
    ])
    _diff_txn(ctx, my, rd, "writes", [
        ("SET", "t:new", "v"), ("INCR", "t:ctr"), ("APPEND", "t:new", "!"),
        ("RPUSH", "t:list", "d"), ("LRANGE", "t:list", "0", "-1"),
        ("HSET", "t:hash", "f2", "v2"), ("HGETALL", "t:hash"),
        ("SADD", "t:set", "m3"), ("SMEMBERS", "t:set"),
        ("DEL", "t:new"),
    ])
    # A reply that is an ERROR inside EXEC is the interesting shape, and it is
    # exactly what the array parser cannot read — so it is checked one command
    # at a time rather than through _diff_txn.
    for sock in (my, rd):
        cmd(sock, "MULTI")
    m_q, _ = reply_or_err(my, "LPUSH", "t:str", "x")     # queues fine, fails later
    r_q, _ = reply_or_err(rd, "LPUSH", "t:str", "x")
    ctx.ok("[txn] a command that will fail still QUEUEs", m_q == r_q,
           f"MYRED {m_q!r} vs redis {r_q!r}")
    m_line = _raw_reply(my, "EXEC")
    r_line = _raw_reply(rd, "EXEC")
    ctx.ok("[txn] EXEC with a failing element still returns an array",
           m_line[0] == r_line[0] == "*",
           f"MYRED {m_line!r} vs redis {r_line!r}")
    for sock in (my, rd):                    # drain the one element
        try:
            _recv_line(sock)
        except OSError:
            pass

    for sock in (my, rd):
        cmd(sock, "MULTI")
    m_rep, m_err = reply_or_err(my, "DISCARD")
    r_rep, r_err = reply_or_err(rd, "DISCARD")
    ctx.ok("[txn] DISCARD inside MULTI", m_rep == r_rep and
           (m_err is None) == (r_err is None), f"{m_rep!r} vs {r_rep!r}")
    m_err = err_of(my, "EXEC")
    r_err = err_of(rd, "EXEC")
    ctx.ok("[txn] EXEC without MULTI is refused",
           _diff_err_class(m_err) == _diff_err_class(r_err),
           f"MYRED {m_err!r} vs redis {r_err!r}")
    m_err = err_of(my, "DISCARD")
    r_err = err_of(rd, "DISCARD")
    ctx.ok("[txn] DISCARD without MULTI is refused",
           _diff_err_class(m_err) == _diff_err_class(r_err),
           f"MYRED {m_err!r} vs redis {r_err!r}")

    _diff_state(ctx, my, rd, "transactions", set())

    ctx.section("Differential: cursor iteration")
    for sock in (my, rd):
        cmd(sock, "FLUSHALL")
        for i in range(97):                  # not a round number, on purpose
            cmd(sock, "SET", f"sc:{i}", "v")
        cmd(sock, "HSET", "sc:hash", *sum(([f"f{i}", f"v{i}"] for i in range(53)), []))
        cmd(sock, "SADD", "sc:set", *[f"m{i}" for i in range(61)])

    _diff_scan_cmp(ctx, my, rd, "SCAN over the keyspace", "SCAN")
    _diff_scan_cmp(ctx, my, rd, "HSCAN over a 53-field hash", "HSCAN", "sc:hash")
    _diff_scan_cmp(ctx, my, rd, "SSCAN over a 61-member set", "SSCAN", "sc:set")

    # The property that makes SCAN usable: a full iteration returns every key
    # that was present throughout, no matter how the cursor is chunked.
    everything = set(cmd(my, "KEYS") or [])
    scanned = set(_diff_scan_all(my, "SCAN"))
    ctx.ok("[scan] a full iteration yields every key KEYS reports",
           everything <= scanned,
           f"missed by SCAN: {sorted(everything - scanned)[:6]}")

    for sock in (my, rd):
        cmd(sock, "FLUSHALL")


def phase_differential(ctx: "PhaseCtx"):
    ctx.section("Differential: MYRED against a real redis-server")

    redis_bin = shutil.which("redis-server")
    if not redis_bin:
        ctx.skip("differential",
                 "no redis-server on PATH — there is no oracle to diff against "
                 "(apt install redis-server)")
        return

    d = ctx.dir("differential")
    my_port, rd_port = ctx.ports(2)

    my_conf = write_conf(os.path.join(d, "myred.conf"), [
        f"port {my_port}", "appendonly no", 'save ""',
    ])
    # Matched on everything that could change a reply: no persistence, no
    # eviction, no auth, no notifications. RESP2 on both, because neither side
    # is ever sent HELLO.
    rd_dir = ctx.dir("differential/redis")
    rd_conf = write_conf(os.path.join(rd_dir, "redis.conf"), [
        f"port {rd_port}", "bind 127.0.0.1", "protected-mode no",
        'save ""', "appendonly no", "maxmemory 0",
        'notify-keyspace-events ""', f"dir {rd_dir}", 'logfile ""',
    ])

    my_srv = ctx.start("myred-diff", d, my_conf, my_port)
    try:
        rd_srv = ctx.start("redis-ref", rd_dir, rd_conf, rd_port,
                           binary=redis_bin)
    except RuntimeError as e:
        ctx.ok("reference redis-server starts", False, str(e))
        my_srv.stop()
        return

    ver = "?"
    try:
        out = subprocess.run([redis_bin, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
        ver = out.split("v=")[1].split()[0] if "v=" in out else out.strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    print(f"  oracle: redis-server {ver} on :{rd_port}   "
          f"MYRED on :{my_port}")

    my = raw_conn(my_port)
    rd = raw_conn(rd_port)
    cmd(my, "FLUSHALL")
    cmd(rd, "FLUSHALL")

    total = 0
    reported = set()
    for group, ops in DIFF_OPS:
        ctx.section(f"Differential: {group}")
        for args in ops:
            _diff_cmd(ctx, my, rd, *args)
            total += 1
        # State is compared per group so a divergence names the group that
        # caused it rather than the whole run.
        _diff_state(ctx, my, rd, group, reported)

    print(f"  {YELLOW}info{RESET} {total} operations compared against "
          f"redis-server {ver}"
          + (f"; {len(reported)} key(s) diverged and were re-synced: "
             f"{sorted(reported)}" if reported else ""))

    _diff_transactions_and_scan(ctx, my, rd)

    if ctx.diff_rounds > 0:
        _diff_random_rounds(ctx, my, rd, ctx.diff_rounds, ctx.diff_ops,
                            ctx.diff_seed)

    my.close()
    rd.close()
    rd_srv.stop()
    my_srv.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE: FUZZ — libFuzzer against the two functions that read untrusted bytes
#
#  `parse_resp_request` and `rdb_load_buffer` are the only places MYRED consumes
#  a byte buffer it did not produce: one from the network, one from a file that
#  may be truncated, corrupt, or hostile. Both are pure functions over (data,
#  size), which is exactly the shape libFuzzer wants.
#
#  Built under AddressSanitizer and UndefinedBehaviorSanitizer, because a parser
#  bug that does not crash outright is the dangerous kind — a read one byte past
#  a buffer is silent on a normal build and loud under ASan.
#
#  The harness sources live here as strings, the same way the HMap unit test
#  does, so the whole suite stays one tracked file.
# ═══════════════════════════════════════════════════════════════════════════════

FUZZ_RESP_SRC = r"""
#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>
#include "buffer.h"
#include "resp.h"

// One RESP frame parsed out of an arbitrary byte string. parse_resp_request
// reads a declared length off the wire and has to survive it being a lie:
// negative, enormous, or longer than what actually arrived.
extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  Buffer buf = buf_create(size + 16);
  buf_append(&buf, data, size);
  std::vector<std::string> cmd;
  parse_resp_request(&buf, cmd);
  buf_destroy(&buf);
  return 0;
}
"""

FUZZ_RDB_SRC = r"""
#include <cstdint>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include "state.h"
#include "rdb.h"
#include "commands.h"

// state.cpp calls into commands.cpp for ACL, audit and notification work, and
// commands.cpp in turn needs the event loop that lives in server.cpp. Linking
// all of that in would drag a network stack into a file-format fuzzer, so the
// six symbols are stubbed instead. They ABORT rather than return: if
// rdb_load_buffer ever grows a path that reaches one, that is a real change in
// what the loader does and the fuzzer should report it, not paper over it.
static void unreachable(const char *who) {
  fprintf(stderr, "fuzz stub reached: %s\n", who);
  abort();
}
std::string acl_format_user(const std::string &, const User &, bool) {
  unreachable("acl_format_user"); return std::string();
}
bool acl_apply_rule(User &, const std::string &) {
  unreachable("acl_apply_rule"); return false;
}
void audit_open(const std::string &) { unreachable("audit_open"); }
bool command_is_known(const std::string &) {
  unreachable("command_is_known"); return false;
}
void notify_keyspace_event(int, const char *, const std::string &) {
  unreachable("notify_keyspace_event");
}
void do_request(std::vector<std::string> &, Buffer *, Conn *, const char *,
                size_t) { unreachable("do_request"); }

// A whole RDB image, loaded straight into the keyspace. Every length, type tag
// and compressed block in it is attacker-controlled if the file is.
extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  rdb_load_buffer(data, size);
  return 0;
}
"""

# Seed frames for the RESP target. A corpus of VALID inputs is what lets the
# mutator reach the interesting branches — from scratch it spends its budget
# rediscovering that a frame starts with '*'.
FUZZ_RESP_SEEDS = {
    "ping":       b"*1\r\n$4\r\nPING\r\n",
    "set":        b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n",
    "inline":     b"PING\r\n",
    "null_array": b"*-1\r\n",
    "null_bulk":  b"$-1\r\n",
    "empty_bulk": b"*1\r\n$0\r\n\r\n",
    "nested":     b"*2\r\n$3\r\nGET\r\n$100\r\n" + b"x" * 100 + b"\r\n",
    "big_len":    b"*1\r\n$2147483647\r\n",
    "neg_len":    b"*1\r\n$-5\r\n",
    "huge_count": b"*99999999\r\n",
}

# The symbolizer is installed as llvm-symbolizer-NN, and the sanitizer runtime
# looks for a bare `llvm-symbolizer`. When it cannot find one it spends a FIXED
# ~90 seconds hunting for it at exit — the same 90s whether the run did 3k or
# 50k iterations, with the process asleep the whole time. ASan takes the
# external path fine; UBSan does not, and only symbolize=0 avoids the stall.
# A UBSan report still names the file and line, which is enough to act on.
FUZZ_ENV = {
    "UBSAN_OPTIONS": "symbolize=0:halt_on_error=1",
}


def _fuzz_symbolizer() -> Optional[str]:
    for name in ("llvm-symbolizer", "llvm-symbolizer-18", "llvm-symbolizer-17",
                 "llvm-symbolizer-16", "llvm-symbolizer-15"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _fuzz_build(ctx: "PhaseCtx", cxx: str, root: str, d: str, name: str,
                src: str, sources) -> Optional[str]:
    path = os.path.join(d, f"{name}.cc")
    with open(path, "w") as f:
        f.write(src)
    exe = os.path.join(d, f"fuzz_{name}")
    argv = [cxx, "-std=c++17", "-g", "-O1",
            "-fsanitize=fuzzer,address,undefined",
            # clang is stricter than gcc about transitive includes and
            # state.cpp relies on gcc's: ULLONG_MAX wants <climits>, fsync and
            # unlink want <unistd.h>. Forced in here so the fuzz build does not
            # depend on that being fixed first.
            "-include", "climits", "-include", "unistd.h",
            "-I", root, "-o", exe, path] + \
           [os.path.join(root, s) for s in sources] + ["-lz"]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        ctx.ok(f"[fuzz] {name} harness builds", False,
               (res.stderr or res.stdout).strip()[-900:])
        return None
    ctx.ok(f"[fuzz] {name} harness builds", True)
    return exe


def _fuzz_run(ctx: "PhaseCtx", exe: str, name: str, corpus: str, runs: int,
              max_len: int, env: dict) -> None:
    art = os.path.join(os.path.dirname(exe), f"crash-{name}-")
    argv = [exe, corpus, f"-runs={runs}", f"-max_len={max_len}",
            f"-artifact_prefix={art}", "-print_final_stats=1"]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                             env=env)
    except subprocess.TimeoutExpired:
        ctx.ok(f"[fuzz] {name}: {runs} runs find no crash", False,
               "the fuzzer timed out — a hang is a finding too; the input is "
               f"under {art}*")
        return
    out = (res.stdout or "") + (res.stderr or "")
    execs = ""
    for line in out.splitlines():
        if "exec/s:" in line and "DONE" in line:
            execs = line.strip()[:110]
    ok = res.returncode == 0
    ctx.ok(f"[fuzz] {name}: {runs:,} runs find no crash", ok,
           "\n    ".join(out.strip().splitlines()[-14:]))
    if ok and execs:
        print(f"    {YELLOW}info{RESET} {execs}")
    crashers = [f for f in os.listdir(os.path.dirname(exe))
                if f.startswith(f"crash-{name}-")]
    if crashers:
        print(f"    {RED}artifacts kept:{RESET} "
              f"{[os.path.join(os.path.dirname(exe), c) for c in crashers]}")


def phase_fuzz(ctx: "PhaseCtx"):
    ctx.section("Fuzz: RESP and RDB parsers under ASan + UBSan")

    cxx = shutil.which("clang++")
    if not cxx:
        ctx.skip("fuzz", "no clang++ on PATH — libFuzzer needs it "
                         "(apt install clang)")
        return
    root = repo_root()
    probe = os.path.join(ctx.dir("fuzz"), "probe.cc")
    with open(probe, "w") as f:
        f.write("extern \"C\" int LLVMFuzzerTestOneInput"
                "(const unsigned char*d,unsigned long s){return 0;}\n")
    if subprocess.run([cxx, "-fsanitize=fuzzer", "-o", probe + ".out", probe],
                      capture_output=True, timeout=300).returncode != 0:
        ctx.skip("fuzz", f"{cxx} cannot link -fsanitize=fuzzer "
                         f"(libFuzzer runtime not installed)")
        return

    d = ctx.dir("fuzz")
    env = dict(os.environ)
    env.update(FUZZ_ENV)
    sym = _fuzz_symbolizer()
    if sym:
        env["ASAN_OPTIONS"] = f"external_symbolizer_path={sym}"
    runs = ctx.fuzz_runs

    # ── RESP: needs almost nothing linked ────────────────────────────────────
    exe = _fuzz_build(ctx, cxx, root, d, "resp", FUZZ_RESP_SRC,
                      ["resp.cpp", "buffer.cpp"])
    if exe:
        corpus = os.path.join(d, "corpus-resp")
        os.makedirs(corpus, exist_ok=True)
        for nm, blob in FUZZ_RESP_SEEDS.items():
            with open(os.path.join(corpus, nm), "wb") as f:
                f.write(blob)
        _fuzz_run(ctx, exe, "resp", corpus, runs, 8192, env)

    # ── RDB: everything except the files that own main() and the event loop ──
    sources = [s for s in sorted(os.listdir(root))
               if s.endswith(".cpp") and s not in
               ("server.cpp", "client.cpp", "commands.cpp")]
    exe = _fuzz_build(ctx, cxx, root, d, "rdb", FUZZ_RDB_SRC, sources)
    if exe:
        corpus = os.path.join(d, "corpus-rdb")
        os.makedirs(corpus, exist_ok=True)
        # Seed with an image the server itself wrote, covering every type plus a
        # TTL and a value long enough to be compressed. A corpus of real files
        # is what gets the mutator past the header into the type handlers.
        seed_dir = ctx.dir("fuzz/seed")
        port = ctx.port()
        conf = write_conf(os.path.join(seed_dir, "seed.conf"), [
            f"port {port}", "appendonly no", 'save ""', "dbfilename dump.rdb",
        ])
        try:
            srv = ctx.start("fuzz-seed", seed_dir, conf, port)
            s = raw_conn(port)
            cmd(s, "SET", "s", "hello")
            cmd(s, "EXPIRE", "s", "9000")
            cmd(s, "RPUSH", "l", "a", "b", "c")
            cmd(s, "HSET", "h", "f1", "v1", "f2", "v2")
            cmd(s, "SADD", "st", "m1", "m2", "m3")
            cmd(s, "ZADD", "z", "1", "a", "2.5", "b", "-inf", "c")
            cmd(s, "SET", "big", "x" * 4000)      # long enough to compress
            cmd(s, "SAVE")
            s.close()
            srv.stop()
            made = os.path.join(seed_dir, "dump.rdb")
            if os.path.exists(made):
                shutil.copyfile(made, os.path.join(corpus, "real.rdb"))
                ctx.ok("[fuzz] seeded the RDB corpus from a real image",
                       os.path.getsize(made) > 0)
            else:
                ctx.ok("[fuzz] seeded the RDB corpus from a real image", False,
                       "SAVE produced no dump.rdb")
        except Exception as e:
            ctx.ok("[fuzz] seeded the RDB corpus from a real image", False,
                   f"{type(e).__name__}: {e}")
        _fuzz_run(ctx, exe, "rdb", corpus, runs, 8192, env)


# ═══════════════════════════════════════════════════════════════════════════════
#  RUN SUMMARY — the machine-readable half of a run
#
#  The markdown log is for reading; this is for comparing. Two runs of the same
#  build on different machines differ in exactly one interesting way, and it is
#  a number, so the numbers get written somewhere a diff can reach them.
# ═══════════════════════════════════════════════════════════════════════════════

def write_summary(path: str, payload: dict):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def print_bench_table(results: dict):
    if not results:
        return
    print(f"\n{BOLD}{BLUE}-- Throughput summary {'-' * 34}{RESET}")
    print(f"  {'test':<16} {'ops/sec':>14}  {'p50 ms':>8}")
    for name in sorted(results, key=lambda k: -results[k].get("rps", 0)):
        row = results[name]
        p50 = row.get("p50")
        print(f"  {name:<16} {row.get('rps', 0):>14,.0f}  "
              f"{(f'{p50:.3f}' if p50 is not None else '-'):>8}")


def compare_summaries(old_path: str, new_path: str) -> int:
    """Diff the throughput of two summary files — typically the same build on
    two different machines.

    No verdict column, on purpose. Each side is a single run, so there is no
    noise floor to judge a delta against; what the table is good for is the
    large, structural difference (a VM's network stack against bare metal),
    not a few percent either way.
    """
    try:
        with open(old_path) as f:
            old = json.load(f)
        with open(new_path) as f:
            new = json.load(f)
    except (OSError, ValueError) as e:
        print(f"{RED}cannot read a summary: {e}{RESET}")
        return 2

    def tag(d):
        p = d.get("platform", {})
        b = d.get("bench") or {}
        bits = [p.get("env", "?"), p.get("kernel", "?"),
                (p.get("cpu_model") or "?")[:40]]
        gov = p.get("governor")
        if gov:
            bits.append(f"governor={gov}")
        tr = (b.get("params") or {}).get("transport") or d.get("transport")
        if tr:
            bits.append(tr)
        # Only on a TLS side, and only when it is the discriminating fact: this
        # is the line that explains a TLS gap the plaintext numbers do not.
        isa = p.get("crypto_isa")
        if tr == "tls" and isa is not None:
            bits.append("vaes" if "vaes" in isa else "no-vaes")
        return " / ".join(bits)

    print(f"{BOLD}A{RESET}  {old_path}\n   {tag(old)}")
    print(f"{BOLD}B{RESET}  {new_path}\n   {tag(new)}")
    ob, nb = old.get("bench") or {}, new.get("bench") or {}

    # `transport` is deliberately NOT part of this check. It is the one
    # difference somebody legitimately wants to measure — plaintext against TLS
    # on the same box is the whole point — and it does not scale throughput the
    # way -n/-c/-P do. It is reported in the label above instead.
    def scaling_params(p):
        return {k: v for k, v in (p or {}).items() if k != "transport"}

    if (ob.get("params") and nb.get("params")
            and scaling_params(ob["params"]) != scaling_params(nb["params"])):
        print(f"\n{RED}refusing to compare: the benchmark parameters differ{RESET}")
        print(f"  A: {ob['params']}")
        print(f"  B: {nb['params']}")
        print("  Throughput scales with -n/-c/-P, so a mismatch here "
              "manufactures whatever result you want.")
        return 2

    oa, na = ob.get("tests") or {}, nb.get("tests") or {}
    shared = sorted(set(oa) & set(na))
    if not shared:
        print(f"\n{YELLOW}neither summary carries benchmark results "
              f"(run with --bench){RESET}")
    else:
        print(f"\n  {'test':<10} {'A ops/sec':>14} {'B ops/sec':>14} {'B/A':>8}")
        for k in shared:
            a, b = oa[k].get("rps", 0), na[k].get("rps", 0)
            ratio = (b / a) if a else 0.0
            print(f"  {k:<10} {a:>14,.0f} {b:>14,.0f} {ratio:>7.2f}x")
        ratios = [na[k].get("rps", 0) / oa[k]["rps"]
                  for k in shared if oa[k].get("rps")]
        if ratios:
            ratios.sort()
            print(f"\n  median B/A across {len(ratios)} tests: "
                  f"{BOLD}{ratios[len(ratios) // 2]:.2f}x{RESET}")

    for label, d in (("A", old), ("B", new)):
        bt = (d.get("build") or {}).get("cmake_build_type")
        if bt and bt.lower() not in OPTIMIZED_BUILD_TYPES:
            print(f"  {YELLOW}note{RESET} {label} was a {bt} build — its "
                  f"throughput is not a result")
    print(f"\n  {YELLOW}note{RESET} one run per side: there is no noise floor "
          f"here, so read the structural differences and ignore small ones.")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

def phase_table():
    """(name, description, fn). Ordered cheapest-first so a failure that is
    going to happen anyway happens early; replication runs last because it is by
    far the slowest and it stops its own master on the way out."""
    return [
        ("unit",        "HMap incremental rehash (compiles hashtable.cpp)",
         phase_hashtable_unit),
        ("memory",      "per-type accounting, maxmemory, incremental eviction",
         phase_memory),
        ("config",      "CONFIG REWRITE survives a restart",
         phase_config_roundtrip),
        ("auth",        "async AUTH: pipelining, lockout, loop latency",
         phase_async_auth),
        ("security",    "ACL enforcement, renamed commands, audit log",
         phase_security),
        ("persistence", "AOF gating, rewrite, hybrid, RDB, restart matrix",
         phase_persistence),
        ("tls",         "TLS handshake and live certificate rotation",
         phase_tls),
        ("replication", "resync paths, durability floor, failover, promotion",
         phase_replication),
        ("differential", "same ops against MYRED and a real redis-server",
         phase_differential),
        ("fuzz", "libFuzzer on the RESP and RDB parsers under ASan+UBSan",
         phase_fuzz),
    ]


def select_phases(spec: str):
    """Resolve --phases into a list of (name, fn). 'all' or '' means every one."""
    table = phase_table()
    if not spec or spec == "all":
        return [(n, f) for n, _, f in table]
    if spec == "none":
        return []
    known = {n: f for n, _, f in table}
    chosen, unknown = [], []
    for name in [p.strip() for p in spec.split(",") if p.strip()]:
        if name in known:
            chosen.append((name, known[name]))
        else:
            unknown.append(name)
    if unknown:
        print(f"{RED}unknown phase(s): {', '.join(unknown)}{RESET}")
        print(f"  known: {', '.join(n for n, _, _ in table)}")
        sys.exit(2)
    return chosen


def run_spawned_phases(r: "TestRunner", args, phases) -> dict:
    """Run the phases that manage their own servers. Returns per-phase results."""
    root = tempfile.mkdtemp(prefix="myred-suite-")
    ctx = PhaseCtx(r, os.path.abspath(args.server), root, args.base_port,
                   destructive=args.destructive,
                   diff_rounds=args.diff_rounds, diff_ops=args.diff_ops,
                   diff_seed=args.diff_seed, fuzz_runs=args.fuzz_runs)
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  Managed-instance phases{RESET}")
    print(f"{'═' * 55}")
    print(f"  Binary:   {ctx.server_bin}")
    print(f"  Workdir:  {root}")
    print(f"  Ports:    from {args.base_port}")
    print(f"  Phases:   {', '.join(n for n, _ in phases)}")
    if not args.destructive:
        print(f"  {YELLOW}note{RESET} --destructive adds the SIGKILL crash-recovery "
              f"and protocol-abuse checks")

    results = {}
    any_failed = False
    for name, fn in phases:
        p0, f0, s0 = r.passed, r.failed, ctx.skipped
        t0 = time.perf_counter()
        crashed = None
        try:
            fn(ctx)
        except Exception as e:
            crashed = f"{type(e).__name__}: {e}"
            ctx.ok(f"phase '{name}' ran to completion", False, crashed)
        finally:
            # A phase that dies partway leaves instances running; they own ports
            # the next phase wants.
            ctx.stop_all()
            ctx.instances = [] if not (r.failed - f0) else ctx.instances
        results[name] = {
            "passed": r.passed - p0,
            "failed": r.failed - f0,
            "skipped": ctx.skipped - s0,
            "duration": round(time.perf_counter() - t0, 2),
            "error": crashed,
        }
        if r.failed - f0:
            any_failed = True
            ctx.dump_evidence()
            ctx.instances = []

    if any_failed or args.keep:
        print(f"\n{YELLOW}workdir kept for inspection: {root}{RESET}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    if ctx.skipped:
        print(f"{YELLOW}{ctx.skipped} check(s) skipped: this binary predates "
              f"them{RESET}")
    results["_skipped_total"] = ctx.skipped
    return results


def spawn_primary(args, workdir: str):
    """Spawn the instance the live-server sections run against, so the whole
    suite is one command with no manual setup. Returns (Instance, host, port)."""
    plain_port = args.base_port + 90
    lines = [f"port {plain_port}",
             "appendonly no",
             # No requirepass: k_max_auth_inflight is 4 against Argon2id's
             # memory bound, and redis-benchmark opens 50 connections that all
             # AUTH at once and never retry BUSY. Auth is covered by its own
             # phase, on its own instance.
             'save ""']
    port = plain_port
    if args.tls:
        crt, key = _selfsigned(workdir, "myred-primary")
        if not crt:
            print(f"{YELLOW}openssl not found: running the primary instance in "
                  f"plaintext despite --tls{RESET}")
            args.tls = False
        else:
            port = args.base_port + 91
            lines += [f"tls-port {port}", f'tls-cert-file "{crt}"',
                      f'tls-key-file "{key}"']
    conf = write_conf(os.path.join(workdir, "primary.conf"), lines)
    inst = Instance(os.path.abspath(args.server), workdir, conf, "primary",
                    plain_port)
    return inst, "127.0.0.1", port


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global G_PASSWORD, G_TLS, G_TLS_INSECURE, G_TLS_CA, G_TLS_CERT, G_TLS_KEY

    ap = argparse.ArgumentParser(
        description="MYRED regression, stress and speed suite")
    ap.add_argument("--host",             default=DEFAULT_HOST)
    ap.add_argument("--port",             default=None, type=int,
                    help=f"server port (default {DEFAULT_PORT}; ignored when "
                         f"--server spawns its own instance)")
    ap.add_argument("--password",         default=None,
                    help="server password (if auth is enabled)")
    ap.add_argument("--tls",              action="store_true",
                    help="connect over TLS (wraps client sockets, passes --tls to "
                         "redis-benchmark); point --port at the tls-port")
    ap.add_argument("--tls-insecure",     action="store_true",
                    help="skip server certificate verification (self-signed test certs)")
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

    ap.add_argument("--server",           default=None,
                    help="path to the server binary. Enables the phases that "
                         "manage their own instances (restarts, crashes, "
                         "replication, TLS rotation) and, unless --host/--port "
                         "say otherwise, spawns the instance the rest of the "
                         "suite runs against — so one command runs everything")
    ap.add_argument("--phases",           default="all",
                    help="which managed-instance phases to run: 'all' (default), "
                         "'none', or a comma-separated list. --list-phases prints them")
    ap.add_argument("--list-phases",      action="store_true",
                    help="print the managed-instance phases and exit")
    ap.add_argument("--destructive",      action="store_true",
                    help="also run the SIGKILL crash-recovery and protocol-abuse "
                         "checks")
    ap.add_argument("--base-port",        default=12500, type=int,
                    help="first private port for spawned instances")
    ap.add_argument("--diff-rounds",      default=8, type=int,
                    help="randomized operation streams the differential phase "
                         "runs against a real redis-server (0 disables)")
    ap.add_argument("--diff-ops",         default=150, type=int,
                    help="operations per randomized differential round")
    ap.add_argument("--fuzz-runs",        default=200000, type=int,
                    help="libFuzzer iterations per target in the fuzz phase; "
                         "raise it for a real campaign (1M takes ~30s)")
    ap.add_argument("--diff-seed",        default=None, type=int,
                    help="seed for the randomized differential rounds; a run "
                         "without one picks a seed and prints it, so any "
                         "failure is replayable")
    ap.add_argument("--keep",             action="store_true",
                    help="keep the temp workdir even when everything passes")

    ap.add_argument("--log",              default="auto",
                    help="write a copy of all output here (ANSI stripped). "
                         "'auto' (default) derives "
                         "<log-dir>/<WSL|Native>/<kind>_<plain|tls>.md, so a run "
                         "from a VM never overwrites one from bare metal; "
                         "pass --log '' to disable")
    ap.add_argument("--log-dir",          default=os.path.join("docs", "logs"),
                    help="root for the per-environment log directories")
    ap.add_argument("--compare",          nargs=2, metavar=("A.json", "B.json"),
                    default=None,
                    help="diff the throughput of two summary files and exit")
    args = ap.parse_args()

    if args.list_phases:
        print("Managed-instance phases (need --server <binary>):\n")
        for name, desc, _ in phase_table():
            print(f"  {name:<12} {desc}")
        print("\n  --phases a,b,c   run only these      "
              "--phases none    skip them all")
        return 0
    if args.compare:
        return compare_summaries(args.compare[0], args.compare[1])

    host = args.host
    port = args.port if args.port is not None else DEFAULT_PORT
    G_PASSWORD     = args.password
    G_TLS          = args.tls
    G_TLS_INSECURE = args.tls_insecure
    G_TLS_CA       = args.tls_ca
    G_TLS_CERT     = args.tls_cert
    G_TLS_KEY      = args.tls_key

    if bool(args.tls_cert) != bool(args.tls_key):
        print(f"{RED}--tls-cert and --tls-key must be given together{RESET}")
        return 2
    if (args.tls_ca or args.tls_cert or args.tls_insecure) and not args.tls:
        print(f"{RED}--tls-* options require --tls{RESET}")
        return 2
    if args.stress_threads < 1 or args.stress_ops < 1 or args.metrics_top < 1:
        print(f"{RED}--stress-threads, --stress-ops and --metrics-top must be "
              f">= 1{RESET}")
        return 2
    if args.server and not os.path.exists(args.server):
        print(f"{RED}server binary not found: {args.server}{RESET}")
        return 2

    # Does this run spawn the instance the live sections talk to? Only when the
    # caller named a binary and did NOT name a server to talk to.
    spawn_own = bool(args.server) and args.port is None and host == DEFAULT_HOST
    if spawn_own and args.tls:
        G_TLS_INSECURE = args.tls_insecure = True     # self-signed test material

    facts = platform_facts()
    bf = build_facts(args.server)

    log_path = default_log_path(args, facts) if args.log == "auto" else args.log
    if log_path:
        start_logging(log_path, run_label(args, host, port))

    print(f"{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}  MYRED — {run_label(args, host, port)}{RESET}")
    print(f"{'═' * 55}")
    print_platform(facts)
    if args.server:
        warn_if_unmeasurable(bf, args.bench)
    if log_path:
        print(f"  Log:          {log_path}")

    primary = None
    primary_dir = None
    started = time.time()
    try:
        if spawn_own:
            primary_dir = tempfile.mkdtemp(prefix="myred-primary-")
            try:
                primary, host, port = spawn_primary(args, primary_dir)
            except RuntimeError as e:
                print(f"{RED}could not start the primary instance: {e}{RESET}")
                return 1
            print(f"\n{GREEN}✓ Spawned the primary instance on "
                  f"{host}:{port}{RESET}  ({primary_dir})")
        else:
            print(f"\n  Target:       {host}:{port}")

        print(f"  Transport:    "
              f"{'TLS' + (' (cert not verified)' if G_TLS_INSECURE else '') if G_TLS else 'plaintext'}")
        print(f"  Auth:         {'password' if G_PASSWORD else 'none'}")

        try:
            s = make_conn(host, port)
            s.close()
            print(f"{GREEN}✓ Server is reachable{RESET}")
        except RespError as e:
            print(f"{RED}✗ Auth failed: {e}{RESET}  — check --password")
            return 1
        except Exception as e:
            print(f"{RED}✗ Cannot connect: {e}{RESET}")
            print("  Start a server first, or pass --server <binary> to have "
                  "this suite start one.")
            return 1

        # Named parts rather than one bare bool. Three things here fail OUTSIDE
        # the TestRunner — the concurrency probe, the stress phase and the
        # benchmark — so a run could print "1022/1022 passed" and then "SOME
        # TESTS FAILED" with nothing anywhere saying which of them it was.
        failed_parts = []

        def note(part: str, ok) -> bool:
            if not ok:
                failed_parts.append(part)
            return bool(ok)

        r = TestRunner(host, port)
        phase_results = {}

        # ── correctness ─────────────────────────────────────────────────────
        if not args.stress_only:
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
                note("correctness (unexpected error)", False)
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
                note("correctness (unexpected error)", False)

        # ── concurrent safety ───────────────────────────────────────────────
        if not args.stress_only and not args.correctness_only:
            note("concurrent writes", test_concurrent_writes(host, port))
            try:
                test_pubsub_fanout_concurrency(r, host, port)
            except Exception as e:
                print(f"\n{RED}Unexpected error: {e}{RESET}")
                note("pub/sub fan-out concurrency", False)

        # ── managed-instance phases ─────────────────────────────────────────
        # These own their servers, so they run against --server rather than
        # against whatever the live sections were pointed at.
        phases = select_phases(args.phases)
        if args.server and phases and not (args.stress_only or args.correctness_only):
            phase_results = run_spawned_phases(r, args, phases)
        elif phases and not args.server:
            print(f"\n{YELLOW}note{RESET} the phases that manage their own "
                  f"instances (restart, crash recovery, replication, TLS "
                  f"rotation, ACL round-trip) need the server binary — re-run "
                  f"with --server build-rel/server to include them.")

        note("assertions", r.summary())

        # ── stress ──────────────────────────────────────────────────────────
        if not args.correctness_only:
            note("stress phase", run_stress_test(host, port, args.stress_threads,
                                                args.stress_ops, args.metrics_top))
            cleanup_stress_keys(host, port)

        # ── speed baseline ──────────────────────────────────────────────────
        if args.bench:
            note("redis-benchmark", run_redis_benchmark(
                host, port, G_PASSWORD, args.bench_requests, args.bench_clients,
                args.bench_pipeline))
            try:
                s = make_conn(host, port)
                cmd(s, "flushall")            # benchmark keys are junk
                s.close()
            except Exception:
                pass
            print_bench_table(BENCH_RESULTS["tests"])

        COMMAND_METRICS.report(args.metrics_top)

        # ── artifacts ───────────────────────────────────────────────────────
        summary_path = None
        if log_path:
            summary_path = os.path.splitext(log_path)[0] + ".json"
            write_summary(summary_path, {
                "generated":   time.strftime("%Y-%m-%dT%H:%M:%S"),
                "run_kind":    run_kind(args),
                "transport":   "tls" if G_TLS else "plain",
                "authed":      bool(G_PASSWORD),
                "duration_s":  round(time.time() - started, 1),
                "platform":    facts,
                "build":       bf,
                "server":      {"host": host, "port": port,
                                "mode": "spawned" if spawn_own else "live"},
                "phases":      phase_results,
                "totals":      {"passed": r.passed, "failed": r.failed},
                "failed_parts": failed_parts,
                "bench":       BENCH_RESULTS if args.bench else None,
            })

        print(f"\n{BOLD}{'═' * 55}{RESET}")
        if not failed_parts:
            print(f"{BOLD}{GREEN}  ALL TESTS PASSED{RESET}")
        else:
            print(f"{BOLD}{RED}  SOME TESTS FAILED{RESET}")
            for part in failed_parts:
                print(f"    {RED}•{RESET} {part}")
        print(f"  {run_label(args, host, port)}")
        print(f"  {facts['env']} — {facts.get('kernel')}")
        if log_path:
            print(f"  {BOLD}Log:     {log_path}{RESET}")
        if summary_path:
            print(f"  {BOLD}Summary: {summary_path}{RESET}")
            print(f"  Compare two machines with: "
                  f"--compare <A.json> <B.json>")
        print(f"{'═' * 55}\n")
        return 0 if not failed_parts else 1
    finally:
        if primary is not None:
            primary.stop()
        if primary_dir:
            if args.keep:
                print(f"{YELLOW}primary workdir kept: {primary_dir}{RESET}")
            else:
                shutil.rmtree(primary_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
