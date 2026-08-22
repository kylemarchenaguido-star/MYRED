#!/usr/bin/env python3
"""
Shared helpers for MYRED integration tests that manage their own server
instances (private port, temp workdir) so they are safe to run while a real
server is up on 1234. Used by test_restart_matrix.py and test_security.py;
test_aof_restart.py predates this module and keeps its own copies.
"""

import os
import signal
import socket
import subprocess
import time

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
TIMEOUT = 5.0

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  {GREEN}ok{RESET}   {name}")
    else:
        FAIL += 1
        print(f"  {RED}FAIL{RESET} {name}" + (f" -- {detail}" if detail else ""))
    return ok


def summary():
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


# ---------------------------------------------------------------- RESP client

def enc(*args) -> bytes:
    out = bytearray(f"*{len(args)}\r\n".encode())
    for a in args:
        b = str(a).encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return bytes(out)


def _line(sock) -> bytes:
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            raise ConnectionError("server closed the connection")
        if c == b"\r":
            sock.recv(1)  # the \n
            return bytes(buf)
        buf += c


def recv(sock):
    line = _line(sock)
    p, body = line[0:1], line[1:]
    if p == b"+": return body.decode()
    if p == b"-": raise RuntimeError(body.decode())
    if p == b":": return int(body)
    if p == b"$":
        n = int(body)
        if n < 0: return None
        d = bytearray()
        while len(d) < n + 2:
            chunk = sock.recv(n + 2 - len(d))
            if not chunk: raise ConnectionError("server closed the connection")
            d += chunk
        return bytes(d[:n]).decode()
    if p == b"*":
        n = int(body)
        return None if n < 0 else [recv(sock) for _ in range(n)]
    raise RuntimeError(f"bad RESP prefix {line!r}")


def cmd(sock, *args):
    sock.sendall(enc(*args))
    return recv(sock)


def connect(port, password=None, user=None):
    s = socket.socket()
    s.settimeout(TIMEOUT)
    s.connect(("127.0.0.1", port))
    if password is not None:
        r = cmd(s, "AUTH", user, password) if user else cmd(s, "AUTH", password)
        if r != "OK":
            raise RuntimeError(f"AUTH failed: {r!r}")
    return s


def expect_error(sock, *args):
    """Send a command; return the error text, or None if it did NOT error."""
    try:
        cmd(sock, *args)
        return None
    except RuntimeError as e:
        return str(e)


# ---------------------------------------------------------------- server

class Server:
    """One server lifetime: spawn in `workdir`, SIGTERM on stop, keep stderr."""

    def __init__(self, binary, workdir, conf, tag, port):
        self.port = port
        self.stderr_path = os.path.join(workdir, f"stderr-{tag}.log")
        self.log = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [binary, conf], cwd=workdir,
            stdout=self.log, stderr=self.log)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.log.close()
                tail = self.stderr_text().strip().splitlines()[-4:]
                raise RuntimeError(
                    f"server exited at startup (rc={self.proc.returncode}), "
                    f"see {self.stderr_path}\n    stderr tail:\n      "
                    + "\n      ".join(tail))
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"server never opened port {port}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()

    def kill9(self):
        """Simulate a crash: SIGKILL, no shutdown save."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()

    def alive(self):
        return self.proc.poll() is None

    def stderr_text(self):
        with open(self.stderr_path, "rb") as f:
            return f.read().decode(errors="replace")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
