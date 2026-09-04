#!/usr/bin/env python3
"""
AOF restart regression test for MYRED (V9.6.4 / N1).

What it proves:
  1. plain-AOF replay: keys written to a fresh AOF survive a full server
     restart. Under N1 (replay user without all_keys) every keyed command in
     the tail is NOPERM'd into the sink, so the dataset comes back EMPTY.
  2. hybrid-AOF replay: after BGREWRITEAOF, keys written AFTER the rewrite
     (the RESP tail / delta) survive a restart. Under N1 the RDB preamble
     loads fine and only the delta vanishes - the silent version of the bug.
  3. the replay-error counter stays quiet: server stderr on restart must
     contain "aof_load: replayed" and must NOT contain "aof_load: WARNING".

Unlike the other suites this script starts its OWN server instances on a
private port inside a temp directory (own config, own AOF, own dump.rdb),
so it is safe to run while your real server is up on 1234.

Usage (from anywhere; --server defaults to <repo root>/build/server):
    python3 scripts/test_aof_restart.py [--server build/server] [--port 12399]
"""

import argparse, os, shutil, signal, socket, subprocess, sys, tempfile, time

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
PASS = FAIL = 0
TIMEOUT = 5.0
PASSWORD = "n1-regression-pass"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO_ROOT, "build", "server")
PORT = 12399


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


def connect_authed():
    s = socket.socket()
    s.settimeout(TIMEOUT)
    s.connect(("127.0.0.1", PORT))
    r = cmd(s, "AUTH", PASSWORD)
    if r != "OK":
        raise RuntimeError(f"AUTH failed: {r!r}")
    return s


# ---------------------------------------------------------------- harness

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  {GREEN}ok{RESET}   {name}")
    else:
        FAIL += 1
        print(f"  {RED}FAIL{RESET} {name}" + (f" -- {detail}" if detail else ""))


class Server:
    """One server lifetime: spawn in `workdir`, SIGTERM on stop, keep stderr."""

    def __init__(self, binary, workdir, conf, tag):
        self.stderr_path = os.path.join(workdir, f"stderr-{tag}.log")
        self.log = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [binary, conf], cwd=workdir,
            stdout=self.log, stderr=self.log)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"server exited at startup (rc={self.proc.returncode}), "
                    f"see {self.stderr_path}")
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"server never opened port {PORT}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()

    def stderr_text(self):
        with open(self.stderr_path, "rb") as f:
            return f.read().decode(errors="replace")


def wait_for_hybrid_aof(path, deadline_s=10.0):
    """BGREWRITEAOF finalize renames tmp over the AOF; wait for the magic."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with open(path, "rb") as f:
                if f.read(8) == b"MYAOFRDB":
                    return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------- the test

BASE_KEYS = {  # written before any rewrite -> replayed from the RESP log
    ("GET",    "n1:str"):            "plain-tail-value",
    ("HGET",   "n1:hash", "field"):  "hval",
    ("ZSCORE", "n1:zset", "alice"):  "10",
}

DELTA_KEYS = {  # written AFTER BGREWRITEAOF -> the hybrid delta N1 loses
    ("GET",    "n1:delta:str"):           "delta-value",
    ("HGET",   "n1:delta:hash", "field"): "dval",
}


def verify(sock, table, label):
    for args, want in table.items():
        try:
            got = cmd(sock, *args)
        except RuntimeError as e:
            check(f"{label}: {' '.join(args)}", False, f"error reply: {e}")
            continue
        ok = got == want or (want.isdigit() and got is not None
                             and float(got) == float(want))
        check(f"{label}: {' '.join(args)} -> {want!r}", ok, f"got {got!r}")


def check_replay_log(srv, label):
    err = srv.stderr_text()
    check(f"{label}: stderr shows a replay happened",
          "aof_load: replayed" in err)
    bad = [l for l in err.splitlines() if "aof_load: WARNING" in l]
    check(f"{label}: no replay-error WARNING in stderr",
          not bad, bad[0] if bad else "")


def main():
    global SERVER, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp workdir for inspection")
    a = ap.parse_args()
    SERVER, PORT = os.path.abspath(a.server), a.port

    if not os.path.exists(SERVER):
        print(f"{RED}server binary not found: {SERVER}{RESET}")
        return 1

    workdir = tempfile.mkdtemp(prefix="myred-aof-restart-")
    conf = os.path.join(workdir, "test.conf")
    with open(conf, "w") as f:
        f.write(f'port {PORT}\n'
                f'requirepass "{PASSWORD}"\n'
                f'appendonly yes\n'
                f'appendfilename appendonly.aof\n'
                f'appendfsync everysec\n'
                f'dbfilename dump.rdb\n')
    aof_path = os.path.join(workdir, "appendonly.aof")
    print(f"workdir: {workdir}")
    srv = None
    try:
        # --- lifetime 1: seed a pure-RESP AOF -------------------------------
        print("\nphase 1: write keys to a fresh AOF, stop the server")
        srv = Server(SERVER, workdir, conf, "seed")
        s = connect_authed()
        check("SET n1:str", cmd(s, "SET", "n1:str", "plain-tail-value") == "OK")
        check("HSET n1:hash", cmd(s, "HSET", "n1:hash", "field", "hval") in (0, 1))
        check("ZADD n1:zset", cmd(s, "ZADD", "n1:zset", "10", "alice") in (0, 1))
        s.close()
        srv.stop()
        check("AOF exists and is non-empty",
              os.path.exists(aof_path) and os.path.getsize(aof_path) > 0)

        # --- lifetime 2: replay the plain RESP tail -------------------------
        print("\nphase 2: restart -> plain-AOF replay must restore every key")
        srv = Server(SERVER, workdir, conf, "replay1")
        s = connect_authed()
        verify(s, BASE_KEYS, "after restart 1")
        check_replay_log(srv, "restart 1")

        # --- same lifetime: build the hybrid scenario -----------------------
        print("\nphase 3: BGREWRITEAOF, then write the delta")
        cmd(s, "BGREWRITEAOF")
        check("AOF became hybrid (MYAOFRDB preamble)",
              wait_for_hybrid_aof(aof_path))
        check("SET n1:delta:str",
              cmd(s, "SET", "n1:delta:str", "delta-value") == "OK")
        check("HSET n1:delta:hash",
              cmd(s, "HSET", "n1:delta:hash", "field", "dval") in (0, 1))
        s.close()
        srv.stop()

        # --- lifetime 3: replay preamble + delta ----------------------------
        print("\nphase 4: restart -> hybrid replay must restore base AND delta")
        srv = Server(SERVER, workdir, conf, "replay2")
        s = connect_authed()
        verify(s, BASE_KEYS, "after restart 2 (preamble)")
        verify(s, DELTA_KEYS, "after restart 2 (delta)")
        check_replay_log(srv, "restart 2")
        s.close()
        srv.stop()
        srv = None
    finally:
        if srv is not None:
            srv.stop()
        if a.keep or FAIL:
            print(f"\n{YELLOW}workdir kept for inspection: {workdir}{RESET}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
