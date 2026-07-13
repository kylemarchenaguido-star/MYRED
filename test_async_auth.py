#!/usr/bin/env python3
"""
Async AUTH tests for MYRED (V9.6.2).

What each test proves:
  1. pipeline gating + resume: AUTH + PING + SET sent in ONE packet must produce
     all three replies IN ORDER. The verify runs on a worker, so the pipelined
     commands must stay buffered (never run pre-auth) and then be drained by the
     completion path - not lost, not reordered.
  2. wrong password is rejected, and repeated failures actually CLOSE the conn.
  3. completion delivery under concurrency: N parallel conns AUTH at once; every
     one must get a reply (no lost/misrouted completions). BUSY is a legal answer
     when the inflight cap is hit, so we retry it.
  4. (informational) PING latency on an authed conn while others spam AUTH. On an
     Argon2 build this is the number that proves the event loop is not stalled.

Every test is isolated: a hang or crash fails THAT test with a diagnostic and the
suite continues, so one deadlock does not hide the other results.

Usage:
    ./build/server myred.conf &
    python3 test_async_auth.py --password s3cret
"""

import socket, argparse, sys, time, threading, hashlib

HOST, PORT, PASSWORD = "127.0.0.1", 1234, None
AUDITLOG = "/tmp/myred-audit.log"
TIMEOUT = 5.0
GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
PASS = FAIL = 0


class Hang(Exception):
    """Server accepted the connection but never replied."""


def enc(*args) -> bytes:
    out = bytearray(f"*{len(args)}\r\n".encode())
    for a in args:
        b = str(a).encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return bytes(out)


def _line(sock) -> bytes:
    buf = bytearray()
    while True:
        try:
            c = sock.recv(1)
        except socket.timeout:
            raise Hang(f"no reply within {TIMEOUT}s")
        if not c:
            raise ConnectionError("server closed the connection")
        if c == b"\r":
            sock.recv(1)  # the \n
            return bytes(buf)
        buf += c


def recv(sock):
    """Returns str/int/list/None. Raises RuntimeError on a -ERR reply."""
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
            try:
                chunk = sock.recv(n + 2 - len(d))
            except socket.timeout:
                raise Hang(f"no reply within {TIMEOUT}s")
            if not chunk: raise ConnectionError("server closed the connection")
            d += chunk
        return bytes(d[:n]).decode()
    if p == b"*":
        n = int(body)
        return None if n < 0 else [recv(sock) for _ in range(n)]
    raise RuntimeError(f"bad RESP prefix {line!r}")


def connect():
    s = socket.socket()
    s.settimeout(TIMEOUT)
    s.connect((HOST, PORT))
    return s


def auth(sock, password, tries=6):
    """AUTH, retrying on -BUSY (the inflight cap is a valid, bounded answer)."""
    for _ in range(tries):
        sock.sendall(enc("auth", password))
        try:
            return recv(sock)
        except RuntimeError as e:
            if "BUSY" in str(e):
                time.sleep(0.25)
                continue
            raise
    raise RuntimeError("still -BUSY after retries (inflight cap never cleared)")


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  {GREEN}PASS{RESET} {name}")
    else:
        FAIL += 1; print(f"  {RED}FAIL{RESET} {name}" + (f"   {detail}" if detail else ""))


def run(name, fn):
    """Isolate a test: a hang/crash fails it and the suite keeps going."""
    print(f"\n{name}")
    try:
        fn()
    except Hang as e:
        global FAIL
        FAIL += 1
        print(f"  {RED}FAIL{RESET} {name} -- SERVER HUNG ({e})")
        print(f"  {YELLOW}hint{RESET} the event loop is not replying at all. Check that "
              f"loop_post() unlocks g_loop_mu, and that the eventfd slot is polled.")
    except (ConnectionError, OSError) as e:
        FAIL += 1
        print(f"  {RED}FAIL{RESET} {name} -- connection error: {e}")
    except Exception as e:
        FAIL += 1
        print(f"  {RED}FAIL{RESET} {name} -- {type(e).__name__}: {e}")


# ---------------------------------------------------------------- tests

def test_pipeline_gating():
    """AUTH+PING+SET in one packet -> three ordered replies."""
    s = connect()
    s.sendall(enc("auth", PASSWORD) + enc("ping") + enc("set", "async:k", "1"))
    r1 = recv(s)
    if r1 != "OK":
        check("AUTH accepted", False, f"got {r1!r}")
        s.close(); return
    r2, r3 = recv(s), recv(s)
    check("three replies, in order (OK, PONG, OK)",
          r2 == "PONG" and r3 == "OK",
          f"got PING={r2!r} SET={r3!r} -- pipelined cmds were dropped or reordered")

    # the SET must actually have taken effect (it ran post-auth, with the real identity)
    s.sendall(enc("get", "async:k"))
    check("pipelined SET actually executed", recv(s) == "1")
    s.close()


def test_wrong_password():
    """Wrong password rejected; repeated failures close the connection."""
    s = connect()
    s.sendall(enc("auth", "definitely-wrong"))
    try:
        r = recv(s)
        check("wrong password rejected", False, f"got a success reply {r!r}")
    except RuntimeError as e:
        check("wrong password rejected", "WRONGPASS" in str(e), str(e))

    # Keep failing until the server closes us. This is a REAL assertion now:
    # k_max_failed_auth must eventually terminate the connection.
    closed = False
    for _ in range(30):
        try:
            s.sendall(enc("auth", "definitely-wrong"))
            recv(s)
        except RuntimeError:
            continue                      # WRONGPASS / BUSY -> keep going
        except (ConnectionError, OSError):
            closed = True; break          # server hung up = correct behavior
    check("connection closed after repeated auth failures", closed,
          "server kept answering forever -- failed_attemps/want_close not enforced")
    try: s.close()
    except OSError: pass


def test_parallel_auth():
    """N concurrent AUTHs: every completion must be delivered to the right conn."""
    N = 8
    results: list[object] = [None] * N   # holds "OK", an error string, or None

    def worker(i):
        try:
            s = connect()
            results[i] = auth(s, PASSWORD)
            s.sendall(enc("ping"))
            if recv(s) != "PONG":
                results[i] = "ping-failed-after-auth"
            s.close()
        except Exception as e:
            results[i] = f"{type(e).__name__}: {e}"

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=TIMEOUT * 3)
    ok = sum(1 for r in results if r == "OK")
    check(f"all {N} concurrent AUTHs completed", ok == N, f"results={results}")


def test_latency_under_storm():
    """PING latency on an authed conn while other conns spam AUTH."""
    stop = threading.Event()

    def storm():
        while not stop.is_set():
            try:
                s = connect()
                for _ in range(3):
                    if stop.is_set(): break
                    s.sendall(enc("auth", "wrong-pw"))
                    try: recv(s)
                    except RuntimeError: pass       # WRONGPASS / BUSY expected
                s.close()
            except Exception:
                time.sleep(0.05)

    stormers = [threading.Thread(target=storm, daemon=True) for _ in range(4)]
    for t in stormers: t.start()

    try:
        s = connect()
        auth(s, PASSWORD)
        lat = []
        for _ in range(200):
            t0 = time.perf_counter()
            s.sendall(enc("ping"))
            recv(s)
            lat.append((time.perf_counter() - t0) * 1000.0)
        s.close()
    finally:
        stop.set()
        for t in stormers: t.join(timeout=2.0)

    lat.sort()
    p50 = lat[len(lat) // 2]
    p99 = lat[int(len(lat) * 0.99)]
    print(f"  PING during AUTH storm: p50={p50:.2f}ms p99={p99:.2f}ms")
    print(f"  {YELLOW}note{RESET} argon2 build: p99 should stay single-digit ms. "
          f"A SYNC argon2 verify would show 20-60ms+ here (that is the whole point of V9.6.2).")
    check("all 200 PINGs answered during the storm", len(lat) == 200)


def test_rehash_migration():
    """V9.6.3: two sequential AUTHs on fresh conns. If the first one triggered a
    rehash-on-AUTH (legacy digest -> Argon2id), the second proves the swapped-in
    credential still verifies. Also checks the audit log never leaks a secret."""
    for i in (1, 2):
        s = connect()
        r = auth(s, PASSWORD)
        check(f"AUTH #{i} accepted (credential survives any rehash)", r == "OK",
              f"got {r!r}")
        s.close()

    try:
        data = open(AUDITLOG).read()
    except OSError as e:
        print(f"  {YELLOW}skip{RESET} audit log not readable ({e}) -- redaction not checked")
        return
    legacy_digest = hashlib.sha256(PASSWORD.encode()).hexdigest()
    leaked = ("$argon2id$" in data) or (PASSWORD in data) or (legacy_digest in data)
    check("audit log contains no plaintext, digest, or PHC hash", not leaked)
    n = data.count("event=cred_rehash")
    print(f"  {YELLOW}info{RESET} cred_rehash events this server lifetime: {n} "
          f"(1 = legacy credential upgraded on first successful AUTH; 0 = already PHC "
          f"or V9.6.3 not built yet)")


def main():
    global HOST, PORT, PASSWORD, AUDITLOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--password", required=True)
    ap.add_argument("--auditlog", default=AUDITLOG,
                    help="audit log path for the redaction check ('' to skip)")
    a = ap.parse_args()
    HOST, PORT, PASSWORD, AUDITLOG = a.host, a.port, a.password, a.auditlog

    try:
        connect().close()
    except OSError as e:
        print(f"{RED}cannot reach {HOST}:{PORT}{RESET} -- is the server running? ({e})")
        sys.exit(2)

    run("pipeline gating + resume", test_pipeline_gating)
    run("wrong password + lockout", test_wrong_password)
    run("parallel AUTH completions", test_parallel_auth)
    run("event-loop latency under AUTH storm", test_latency_under_storm)
    run("rehash-on-AUTH migration + audit redaction", test_rehash_migration)

    print(f"\n{GREEN if FAIL == 0 else RED}{'PASS' if FAIL == 0 else 'FAIL'}{RESET}: "
          f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
