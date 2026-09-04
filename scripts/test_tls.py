#!/usr/bin/env python3
"""
TLS measurement harness for MYRED — V10.6.1 Step 0.

This is NOT a pass/fail suite. Every item in V10.6.1 (bounded accept loop, kTLS,
cert reload) is gated on "escalate only when a metric demands it", and none of
those metrics existed. This produces them, machine-comparably, so the same
command run after a change yields a diff instead of an impression. It fails only
when a *measurement* is broken; a number that looks bad is the point, not a
failure.

It spawns its own passwordless instance on private ports in a temp dir, so it is
safe to run beside a live server on 1234.

What it measures, one block per gated decision:

  1. handshake  — fresh vs resumed handshake cost, and the resumption rate.
                  V9.7.5 shipped session resumption; a regression there is
                  otherwise invisible (everything still works, just slower).
  2. storm      — a burst of `--burst` simultaneous arrivals. Three numbers:
                  accept-to-first-reply latency, the server's own CPU over the
                  window (/proc/<pid>/stat), and the stall imposed on a
                  connection that was ALREADY established. The third is the
                  actual subject of the bounded-accept-loop fix: capping accepts
                  per tick does not make handshakes cheaper, it stops one burst
                  from monopolizing a tick and starving the connections already
                  being served. Run on the plaintext port too — the accept loop
                  is shared, so the fix has to be shown not to cost plaintext.
  3. bench      — small-message throughput, TLS vs plaintext, from
                  redis-benchmark only. At -P 16 AND -P 1: kTLS targets
                  per-record copy/syscall overhead, which pipelining hides, so
                  the unpipelined row is the one that would justify it.
  4. cert       — what a certificate rotation costs today: a restart. Wall-clock
                  unavailability plus every established connection dying. The
                  new cert is proven live by comparing the peer certificate's
                  SHA-256, and the hot-reload trigger is probed so that when one
                  exists this reports both rows side by side.

Measurement rules encoded here, each one previously learned the hard way and
each one silent when violated:

  - A Debug build is refused. commands.cpp runs mem_selfcheck() after every
    command when NDEBUG is unset and it walks the keyspace, so a Debug binary is
    O(keyspace) per command. Override with --allow-debug only to smoke-test the
    harness itself; the numbers mean nothing.
  - Passwordless. k_max_auth_inflight is 4 against Argon2id's 76MiB bound, so an
    authed benchmark measures the KDF, not the transport (V9.7.4).
  - `save ""` in the config, so no background BGSAVE fork lands mid-measurement.
  - Throughput comes from redis-benchmark or it is not reported. This harness's
    own Python ops/sec is client-bound (~4k against a server that does 2.2M) and
    has already "proved" TLS faster than plaintext once.
  - Every metric is repeated (--repeat) and reported as a median with the spread
    across repeats. That spread IS the noise floor: a later delta smaller than it
    is not a result.
  - The already-established pinger runs in a separate PROCESS, not a thread. The
    burst workers hold the GIL; measuring the victim from a thread would report
    Python contention as server stall.

Usage:
    python3 scripts/test_tls.py                          # full baseline run
    python3 scripts/test_tls.py --quick                  # smoke-test the harness
    python3 scripts/test_tls.py --tag after-accept-cap \\
            --compare docs/tls_metrics_baseline.json     # after a change
    python3 scripts/test_tls.py --phases storm           # one block only

Writes docs/tls_metrics_<tag>.json (the comparable artifact) and appends a
human-readable section to docs/tls_metrics.md.
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import platform
import re
import select
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from myred_testlib import GREEN, RED, YELLOW, RESET, Server, cmd, connect, repo_root

# Private high ports, clear of test_replication.py's 12404-12410.
PLAIN_PORT = 12420
TLS_PORT = 12421

PING = b"*1\r\n$4\r\nPING\r\n"
PONG = b"+PONG\r\n"

# A build we refuse to measure unless --allow-debug.
OPTIMIZED_BUILD_TYPES = {"release", "relwithdebinfo", "minsizerel"}

# Below this, at -P 16 on a Release build, something is wrong — almost always a
# Debug binary reached through a path the CMakeCache check did not cover.
DEBUG_SUSPECT_OPS = 150_000

ERRORS = []


def fail(msg):
    """A broken measurement. Distinct from a bad number, which is a result."""
    ERRORS.append(msg)
    print(f"  {RED}measurement failed{RESET} {msg}")


# ------------------------------------------------------------------ statistics

def pct(vals, q):
    """Linear-interpolated percentile. None on an empty sample, so a missing
    measurement stays visibly missing instead of becoming a zero."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def dist(vals):
    return {"n": len(vals), "p50": pct(vals, 0.50), "p90": pct(vals, 0.90),
            "p99": pct(vals, 0.99), "max": max(vals) if vals else None}


def aggregate(runs):
    """Collapse N structurally identical run-dicts into one, replacing every
    numeric leaf with {median, min, max, spread_pct}. spread_pct is the noise
    floor for that metric: a later delta below it is not a result."""
    def walk(objs):
        first = objs[0]
        if isinstance(first, dict):
            return {k: walk([o[k] for o in objs]) for k in first}
        nums = [o for o in objs if isinstance(o, (int, float))
                and not isinstance(o, bool)]
        if len(nums) != len(objs) or not nums:
            return first                      # strings, bools, None: keep as-is
        med = statistics.median(nums)
        spread = (max(nums) - min(nums)) / abs(med) * 100 if med else 0.0
        return {"median": med, "min": min(nums), "max": max(nums),
                "spread_pct": spread}
    return walk(runs)


def med(node):
    """Median out of an aggregated leaf, tolerating a missing metric."""
    if isinstance(node, dict) and "median" in node:
        return node["median"]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return node
    return None


def fmt(v, unit="", width=9):
    if v is None:
        return "-".rjust(width)
    return (f"{v:,.2f}{unit}" if abs(v) < 10000 else f"{v:,.0f}{unit}").rjust(width)


# ------------------------------------------------------------- RESP over a sock

def ping_rtt(sock):
    """One PING round trip, in seconds. Deliberately not myred_testlib.recv():
    that reads a byte at a time, which is fine for assertions and far too much
    syscall overhead to sit inside a latency measurement."""
    t0 = time.perf_counter()
    sock.sendall(PING)
    buf = b""
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise ConnectionError("server closed the connection mid-PING")
        buf += chunk
    dt = time.perf_counter() - t0
    if buf != PONG:
        raise RuntimeError(f"expected +PONG, got {buf!r}")
    return dt


def tls_ctx():
    """The certs in tls/ are self-signed test certs; verification is off by
    design. One context per client, never shared across the burst workers, so
    every storm handshake is a genuine full handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def plain_connect(port, timeout=15.0):
    s = socket.create_connection(("127.0.0.1", port), timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def tls_connect(port, ctx, session=None, timeout=15.0):
    raw = plain_connect(port, timeout)
    return ctx.wrap_socket(raw, server_hostname=None, session=session)


def any_connect(port, tls, ctx=None):
    return tls_connect(port, ctx or tls_ctx()) if tls else plain_connect(port)


# --------------------------------------------------------------- server probing

def proc_cpu_s(pid):
    """utime+stime for the whole process (all threads), in seconds. The comm
    field can contain spaces and parens, so split after the LAST ')'."""
    with open(f"/proc/{pid}/stat", "rb") as f:
        data = f.read()
    rest = data[data.rindex(b")") + 2:].split()
    return (int(rest[11]) + int(rest[12])) / os.sysconf("SC_CLK_TCK")


def detect_build(binary):
    out = {"binary": binary, "cmake_build_type": None, "debug_info": None,
           "size_bytes": os.path.getsize(binary)}
    cache = os.path.join(os.path.dirname(os.path.abspath(binary)), "CMakeCache.txt")
    if os.path.exists(cache):
        with open(cache, errors="replace") as f:
            for ln in f:
                if ln.startswith("CMAKE_BUILD_TYPE:"):
                    out["cmake_build_type"] = ln.split("=", 1)[1].strip() or None
                    break
    if shutil.which("file"):
        r = subprocess.run(["file", "-b", binary], capture_output=True, text=True)
        out["debug_info"] = "with debug_info" in r.stdout
    return out


def build_is_measurable(b):
    t = (b["cmake_build_type"] or "").lower()
    if t:
        if t in OPTIMIZED_BUILD_TYPES:
            return True, ""
        return False, f"CMAKE_BUILD_TYPE={b['cmake_build_type']}"
    if b["debug_info"]:
        return False, "no CMakeCache.txt beside the binary and it carries debug_info"
    return True, ""


def peer_cert_sha256(port, timeout=5.0):
    ctx = tls_ctx()
    s = tls_connect(port, ctx, timeout=timeout)
    try:
        der = s.getpeercert(binary_form=True)
    finally:
        s.close()
    return hashlib.sha256(der).hexdigest()[:16] if der else None


# ------------------------------------------------------- 1. handshake / resume

def measure_handshake(port, tls, n):
    """Sequential connects. `fresh` is connect → handshake → first PONG, which
    is the whole accept-to-first-command path. `resumed` re-offers the previous
    session: TLS 1.3 sends its ticket AFTER the handshake and MYRED's protocol is
    client-speaks-first, so the ticket only lands once a reply has been read —
    which is why the session is taken after the PING, not before it. (This is
    the same reason `s_client -reconnect` is a false negative here; V9.7.5.)"""
    ctx = tls_ctx()
    fresh, resumed, reused = [], [], 0

    for _ in range(n):
        t0 = time.perf_counter()
        s = tls_connect(port, ctx) if tls else plain_connect(port)
        ping_rtt(s)
        fresh.append((time.perf_counter() - t0) * 1000)
        s.close()

    out = {"fresh_ms": dist(fresh)}
    if not tls:
        return out

    session = None
    warm = tls_connect(port, ctx)
    ping_rtt(warm)
    session = warm.session
    warm.close()

    for _ in range(n):
        t0 = time.perf_counter()
        s = tls_connect(port, ctx, session=session)
        ping_rtt(s)
        resumed.append((time.perf_counter() - t0) * 1000)
        if s.session_reused:
            reused += 1
        # Re-take the session each time: OpenSSL issues single-use TLS 1.3
        # tickets, so quoting the first one forever would measure a fallback to
        # a full handshake and call it a resumption regression.
        session = s.session or session
        s.close()

    out["resumed_ms"] = dist(resumed)
    out["resumption_rate"] = reused / float(n) if n else 0.0
    p_fresh, p_res = out["fresh_ms"]["p50"], out["resumed_ms"]["p50"]
    out["resume_saving_pct"] = ((p_fresh - p_res) / p_fresh * 100
                                if p_fresh else None)
    return out


# ----------------------------------------------------------------- 2. the storm

def _pinger_proc(port, tls, stop_evt, q):
    """Runs in its OWN PROCESS. The burst workers below hold the GIL while doing
    Python-level work; sampling the victim from a thread would attribute that
    contention to the server. Timestamps are time.time() because that is the
    clock both processes agree on."""
    try:
        s = any_connect(port, tls)
        ping_rtt(s)                                   # warm the path
        samples = []
        while not stop_evt.is_set():
            t = time.time()
            samples.append((t, ping_rtt(s) * 1000))
            # No sleep. The burst window is tens of milliseconds, so any cadence
            # coarse enough to notice yields a handful of samples whose "p99" is
            # just the max — that showed up as a 700% spread across repeats. The
            # loop is self-pacing anyway: it is synchronous request/response, so
            # it cannot go faster than the server answers, and it is one
            # connection against a server that does millions of ops/s. It runs
            # identically in the baseline and burst windows, so whatever load it
            # does add cannot bias the comparison between them.
        s.close()
        q.put(samples)
    except Exception as e:                            # noqa: BLE001 - reported
        q.put({"error": repr(e)})


def _burst_worker(pending, lock, tls, t_start, done, keep, errs):
    ctx = tls_ctx() if tls else None                  # per worker: no shared
    while True:                                       # session cache, so every
        with lock:                                    # storm handshake is full
            if not pending:
                return
            s = pending.pop()
        try:
            # The SYNs were fired non-blocking so they would queue in the accept
            # backlog together; each has to be confirmed established before it
            # can be handed to OpenSSL. POLLOUT + SO_ERROR is the textbook
            # completion check, and poll() has no FD_SETSIZE ceiling to trip on.
            poller = select.poll()
            poller.register(s.fileno(), select.POLLOUT)
            if not poller.poll(30_000):
                raise TimeoutError("connect never completed")
            err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                raise OSError(err, os.strerror(err))
            s.setblocking(True)
            s.settimeout(30.0)
            if tls:
                s = ctx.wrap_socket(s, server_hostname=None)
            ping_rtt(s)
            with lock:
                done.append((time.time() - t_start) * 1000)
                keep.append(s)
        except Exception as e:                        # noqa: BLE001 - reported
            with lock:
                errs.append(repr(e))
            try:
                s.close()
            except OSError:
                pass


def measure_storm(port, tls, pid, burst, workers, baseline_s=1.0):
    """Fire `burst` SYNs as fast as the kernel will take them, so they queue in
    the listen backlog (SOMAXCONN, server.cpp) and the server's accept loop meets
    them all in one tick — that queue is what the unbounded
    `while (handle_accept(...) == 0) {}` drains. Then hand every socket to worker
    threads to complete the handshake and issue one command.

    Latency is measured from the START of the storm to each connection's own
    PONG, which is the honest question: under N simultaneous arrivals, how long
    until connection i is served?"""
    stop = multiprocessing.Event()
    q = multiprocessing.Queue()
    pinger = multiprocessing.Process(target=_pinger_proc,
                                     args=(port, tls, stop, q), daemon=True)
    pinger.start()
    time.sleep(baseline_s)                 # collect a quiescent baseline first
    idle_until = time.time()

    socks = []
    for _ in range(burst):                 # socket() out of the timed loop
        s = socket.socket()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setblocking(False)
        socks.append(s)

    pending, keep, done, errs = list(socks), [], [], []
    lock = threading.Lock()

    cpu0 = proc_cpu_s(pid)
    t_start = time.time()
    for s in socks:
        s.connect_ex(("127.0.0.1", port))  # non-blocking: returns EINPROGRESS

    threads = [threading.Thread(target=_burst_worker,
                                args=(pending, lock, tls, t_start, done, keep, errs))
               for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t_end = time.time()
    cpu_used = proc_cpu_s(pid) - cpu0

    stop.set()
    victim = q.get(timeout=15)
    pinger.join(timeout=10)
    for s in keep:
        try:
            s.close()
        except OSError:
            pass

    if isinstance(victim, dict):
        fail(f"storm pinger died on port {port}: {victim.get('error')}")
        victim = []

    idle = [ms for ts, ms in victim if ts <= idle_until]
    during = [ms for ts, ms in victim if t_start <= ts <= t_end]
    window = t_end - t_start
    ok = len(done)

    idle_p99, burst_p99 = pct(idle, 0.99), pct(during, 0.99)
    return {
        "burst": burst,
        "workers": workers,
        "connected": ok,
        "failed": len(errs),
        "window_s": window,
        "conns_per_s": ok / window if window else None,
        "server_cpu_s": cpu_used,
        "server_cpu_ms_per_conn": (cpu_used * 1000 / ok) if ok else None,
        "accept_to_reply_ms": dist(done),
        "victim_idle_ms": dist(idle),
        "victim_during_burst_ms": dist(during),
        # The headline for the bounded accept loop: how much worse an
        # already-established connection got while the burst was landing.
        "victim_stall_factor_p99": (burst_p99 / idle_p99
                                    if idle_p99 and burst_p99 else None),
    }


# ------------------------------------------------------------ 3. throughput

BENCH_OPS = re.compile(r"^(?P<name>[^:]+):\s*(?P<ops>[\d.]+) requests per second")
BENCH_P50 = re.compile(r"p50=(?P<p50>[\d.]+)")


def run_bench(exe, port, tls, test, requests, clients, pipeline):
    """redis-benchmark is the ONLY trustworthy throughput source here. -t is
    mandatory: MYRED has no INCR/SADD-style coverage for the default suite."""
    argv = [exe, "-h", "127.0.0.1", "-p", str(port), "-t", test,
            "-n", str(requests), "-c", str(clients), "-P", str(pipeline), "-q"]
    if tls:
        argv += ["--tls", "--insecure"]
    timeout = max(120, requests // 500)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"redis-benchmark -t {test} -P {pipeline} timed out after {timeout}s "
             f"on port {port} — that is the signature of a Debug build")
        return {"ops_per_s": None, "p50_ms": None}
    if p.returncode != 0:
        fail(f"redis-benchmark -t {test} exited {p.returncode}: "
             f"{p.stderr.strip()[:200]}")
        return {"ops_per_s": None, "p50_ms": None}
    ops = p50 = None
    for ln in p.stdout.splitlines():
        m = BENCH_OPS.match(ln.strip())
        if m:
            ops = float(m.group("ops"))
            m2 = BENCH_P50.search(ln)
            p50 = float(m2.group("p50")) if m2 else None
    if ops is None:
        fail(f"could not parse redis-benchmark output for {test}: "
             f"{p.stdout.strip()[:200]}")
    return {"ops_per_s": ops, "p50_ms": p50}


def measure_bench(exe, plain_port, tls_port, requests, clients, pipelines, tests):
    out = {}
    for pl in pipelines:
        for test in tests:
            plain = run_bench(exe, plain_port, False, test, requests, clients, pl)
            tls = run_bench(exe, tls_port, True, test, requests, clients, pl)
            ratio = (tls["ops_per_s"] / plain["ops_per_s"]
                     if plain["ops_per_s"] and tls["ops_per_s"] else None)
            out[f"{test}_P{pl}"] = {
                "plain": plain,
                "tls": tls,
                # The kTLS decision is this number and nothing else.
                "tls_over_plain": ratio,
            }
    return out


# --------------------------------------------------------- 4. cert rotation

def make_selfsigned(dirpath, cn):
    key = os.path.join(dirpath, f"{cn}-key.pem")
    crt = os.path.join(dirpath, f"{cn}-cert.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", crt, "-days", "2", "-subj", f"/CN={cn}"],
        check=True, capture_output=True)
    return crt, key


def rotate_in_place(workdir, live_cert, live_key, cn):
    """Overwrite the material the server is configured with, the way a real
    rotation works: certbot, cert-manager and every mounted-secret setup write
    new bytes to the SAME paths. That also sidesteps the one thing CONFIG SET
    cannot do — a cert and its key must swap together, and a single-pair CONFIG
    SET would have to validate a new cert against the old key first."""
    crt, key = make_selfsigned(workdir, cn)
    shutil.copyfile(crt, live_cert)
    shutil.copyfile(key, live_key)


def measure_cert_rotation(server_bin, workdir, conf_path, srv,
                          plain_port, tls_port, live_cert, live_key, register):
    """What rotating a certificate costs, hot vs restart. Runs LAST: it
    restarts the server, so every other measurement must already be taken."""
    out = {"hot_reload_supported": False, "hot_reload_error": None,
           "restart_downtime_ms": None, "restart_conns_survived": None,
           "restart_cert_changed": None}

    fp0 = peer_cert_sha256(tls_port)
    live = tls_connect(tls_port, tls_ctx())
    ping_rtt(live)

    # ---- hot path (V10.6.1c). New material at the same paths, then a trigger.
    rotate_in_place(workdir, live_cert, live_key, "rotated-hot")
    admin = connect(plain_port)
    try:
        t0 = time.time()
        cmd(admin, "CONFIG", "SET", "tls-cert-file", live_cert)
        out["hot_reload_supported"] = True
        out["hot_reload_ms"] = (time.time() - t0) * 1000
    except RuntimeError as e:
        # A binary predating V10.6.1c refuses: tls-* is boot_only (V9.7.2).
        out["hot_reload_error"] = str(e)

    if out["hot_reload_supported"]:
        fp_hot = peer_cert_sha256(tls_port, timeout=5.0)
        out["hot_reload_cert_changed"] = bool(fp_hot and fp_hot != fp0)
        if not out["hot_reload_cert_changed"]:
            fail("CONFIG SET tls-cert-file was accepted but the server still "
                 "presents the OLD certificate — the context was not swapped")
        try:
            ping_rtt(live)
            out["hot_reload_conns_survived"] = True
        except (OSError, ConnectionError, RuntimeError):
            out["hot_reload_conns_survived"] = False

        # [REG] tls-key-file must write the KEY field. Its apply() and its get()
        # read different variables, and the boot round-trip check skips any row
        # that owns an emit — so a setter wired to the cert field round-trips
        # perfectly and is invisible to every other check. Same class as V9.8's
        # appendonly/protected-mode bug, one level down.
        try:
            cmd(admin, "CONFIG", "SET", "tls-key-file", live_key)
            got = cmd(admin, "CONFIG", "GET", "tls-key-file")
            got = got[1] if isinstance(got, list) and len(got) > 1 else None
            out["key_directive_sets_key_field"] = (got == live_key)
            if got != live_key:
                fail(f"[REG] CONFIG SET tls-key-file left tls-key-file as "
                     f"{got!r}, expected {live_key!r} — its apply() is wired to "
                     f"the wrong Config field")
        except RuntimeError as e:
            out["key_directive_sets_key_field"] = False
            fail(f"[REG] CONFIG SET tls-key-file was refused: {e}")

        # A bad path must be refused AND must not disturb the live context.
        try:
            cmd(admin, "CONFIG", "SET", "tls-cert-file",
                os.path.join(workdir, "does-not-exist.pem"))
            out["bad_material_refused"] = False
            fail("CONFIG SET tls-cert-file accepted a nonexistent file")
        except RuntimeError:
            out["bad_material_refused"] = True
        try:
            out["still_serving_after_bad_set"] = (
                peer_cert_sha256(tls_port, timeout=5.0) == fp_hot)
        except OSError:
            out["still_serving_after_bad_set"] = False
        if not out["still_serving_after_bad_set"]:
            fail("a REJECTED CONFIG SET disturbed the live TLS context")
        # and the rejected value must not have been staged into g_config,
        # or CONFIG REWRITE would persist a config file that cannot boot
        cur = cmd(admin, "CONFIG", "GET", "tls-cert-file")
        cur = cur[1] if isinstance(cur, list) and len(cur) > 1 else None
        out["rollback_restored_path"] = (cur == live_cert)
        if cur != live_cert:
            fail(f"[REG] a rejected CONFIG SET left tls-cert-file as {cur!r} — "
                 f"CONFIG REWRITE would persist material that cannot load")
    admin.close()

    # ---- the baseline a rotation pays without the hot path: stop and restart.
    fp1 = peer_cert_sha256(tls_port)
    pre_restart = tls_connect(tls_port, tls_ctx())
    ping_rtt(pre_restart)
    rotate_in_place(workdir, live_cert, live_key, "rotated-restart")

    t0 = time.time()
    srv.stop()
    new_srv = Server(server_bin, workdir, conf_path, "rotated", plain_port)
    register(new_srv)
    fp2 = None
    while time.time() - t0 < 30:
        try:
            fp2 = peer_cert_sha256(tls_port, timeout=2.0)
            break
        except OSError:
            time.sleep(0.005)
    out["restart_downtime_ms"] = (time.time() - t0) * 1000
    out["restart_cert_changed"] = bool(fp2 and fp2 != fp1)
    if fp2 is None:
        fail("TLS port never served again after the rotation restart")

    try:
        ping_rtt(pre_restart)
        out["restart_conns_survived"] = True
    except (OSError, ConnectionError, RuntimeError):
        out["restart_conns_survived"] = False
    for s in (live, pre_restart):
        try:
            s.close()
        except OSError:
            pass
    return out, new_srv


# ------------------------------------------------------------------- reporting

def collect_meta(args, build, server_bin):
    def git(*a):
        try:
            return subprocess.run(["git"] + list(a), cwd=repo_root(),
                                  capture_output=True, text=True).stdout.strip()
        except OSError:
            return None
    return {
        "tag": args.tag,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_rev": git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "server": server_bin,
        "build": build,
        "openssl": ssl.OPENSSL_VERSION,
        "python": platform.python_version(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "params": {"repeat": args.repeat, "burst": args.burst,
                   "workers": args.workers, "handshakes": args.handshakes,
                   "bench_requests": args.bench_requests,
                   "bench_clients": args.bench_clients,
                   "pipelines": args.pipelines, "tests": args.tests},
    }


def print_report(m, meta):
    print(f"\n{GREEN}{'=' * 78}{RESET}")
    print(f"  TLS metrics — tag '{meta['tag']}' — {meta['utc']}")
    print(f"  {meta['git_rev']}{'+dirty' if meta['git_dirty'] else ''}  "
          f"build={meta['build']['cmake_build_type']}  "
          f"{meta['openssl']}  {meta['cpu_count']} cpus")
    print(f"{GREEN}{'=' * 78}{RESET}")
    print("  median of "
          f"{meta['params']['repeat']} repeats; ±spread is the noise floor — a "
          "later delta smaller\n  than it is not a result.")

    def row(label, node, unit=""):
        v = med(node)
        sp = node.get("spread_pct") if isinstance(node, dict) else None
        print(f"    {label:<34}{fmt(v, unit)}"
              + (f"   ±{sp:.1f}%" if isinstance(sp, (int, float)) else ""))

    for kind in ("tls", "plain"):
        h = m.get(f"handshake_{kind}")
        if not h:
            continue
        print(f"\n  {YELLOW}handshake — {kind}{RESET}")
        row("fresh connect→reply p50", h["fresh_ms"]["p50"], " ms")
        row("fresh connect→reply p99", h["fresh_ms"]["p99"], " ms")
        if "resumed_ms" in h:
            row("resumed connect→reply p50", h["resumed_ms"]["p50"], " ms")
            row("resumption rate", h["resumption_rate"])
            row("resume saving", h["resume_saving_pct"], " %")

    for kind in ("tls", "plain"):
        s = m.get(f"storm_{kind}")
        if not s:
            continue
        print(f"\n  {YELLOW}accept storm — {kind} "
              f"({int(med(s['burst']) or 0)} simultaneous){RESET}")
        row("connections completed", s["connected"])
        row("failed", s["failed"])
        row("accept→reply p50", s["accept_to_reply_ms"]["p50"], " ms")
        row("accept→reply p99", s["accept_to_reply_ms"]["p99"], " ms")
        row("accept→reply max", s["accept_to_reply_ms"]["max"], " ms")
        row("drain window", s["window_s"], " s")
        row("conns/s", s["conns_per_s"])
        row("server cpu", s["server_cpu_s"], " s")
        row("server cpu per conn", s["server_cpu_ms_per_conn"], " ms")
        print(f"    {'-' * 34}  established-connection stall")
        row("victim idle p99", s["victim_idle_ms"]["p99"], " ms")
        row("victim during-burst p99", s["victim_during_burst_ms"]["p99"], " ms")
        row("victim during-burst max", s["victim_during_burst_ms"]["max"], " ms")
        # A p99 over a handful of samples is just the max wearing a hat.
        row("victim samples in burst", s["victim_during_burst_ms"]["n"])
        n_seen = med(s["victim_during_burst_ms"]["n"]) or 0
        if n_seen < 100:
            print(f"    {YELLOW}only {n_seen:.0f} victim samples — the burst "
                  f"window is too short to support a p99;\n    raise --burst "
                  f"(default 300) before reading the stall numbers{RESET}")
        # --workers caps how many handshakes are in flight, so a low value means
        # the server never sees the burst as simultaneous and the stall above is
        # an understatement. Measured on an unchanged server, burst 300:
        # workers 8 → 5.0ms p99, 64 → 18.5ms, 300 → 48.1ms.
        n_burst, n_work = med(s["burst"]) or 0, med(s["workers"]) or 0
        if n_work and n_burst and n_work < n_burst / 4:
            print(f"    {YELLOW}--workers {n_work:.0f} against a burst of "
                  f"{n_burst:.0f}: at most {n_work:.0f} handshakes are in "
                  f"flight at once,\n    so the stall above understates the "
                  f"worst case — re-run with --workers {n_burst:.0f}{RESET}")
        row("stall factor (p99 burst/idle)", s["victim_stall_factor_p99"], "x")

    b = m.get("bench")
    if b:
        print(f"\n  {YELLOW}throughput — redis-benchmark{RESET}")
        print(f"    {'case':<20}{'plain ops/s':>14}{'tls ops/s':>14}"
              f"{'tls/plain':>11}")
        for case, d in b.items():
            pl, tl = med(d["plain"]["ops_per_s"]), med(d["tls"]["ops_per_s"])
            r = med(d["tls_over_plain"])
            print(f"    {case:<20}{fmt(pl, '', 14)}{fmt(tl, '', 14)}"
                  + (f"{r:>10.2f}x" if r else f"{'-':>11}"))
        print("    p50 is recorded in the JSON but is NOT comparable across runs "
              "at different\n    throughputs under -P 16 (Little's Law) — compare "
              "throughput only.")

    c = m.get("cert")
    if c:
        print(f"\n  {YELLOW}certificate rotation{RESET}")

        def flag(label, key):
            v = c.get(key)
            if v is None:
                return
            mark = GREEN if v else RED
            print(f"    {label:<34}{mark}{str(v):>9}{RESET}")

        flag("hot reload supported", "hot_reload_supported")
        if c.get("hot_reload_error"):
            print(f"      refused with: {c['hot_reload_error']}")
        if c.get("hot_reload_supported"):
            row("  hot reload latency", c.get("hot_reload_ms"), " ms")
            flag("  new cert served immediately", "hot_reload_cert_changed")
            flag("  established conns survived", "hot_reload_conns_survived")
            flag("  [REG] key directive sets key", "key_directive_sets_key_field")
            flag("  bad material refused", "bad_material_refused")
            flag("  still serving after refusal", "still_serving_after_bad_set")
            flag("  refusal rolled the path back", "rollback_restored_path")
        print(f"    {'-' * 34}  restart, the cost without it")
        row("  restart downtime", c["restart_downtime_ms"], " ms")
        flag("  established conns survived", "restart_conns_survived")
        flag("  new cert proven live", "restart_cert_changed")


def flatten(node, prefix=""):
    """Aggregated tree → {dotted.path: leaf}. Numeric leaves arrive as their
    {median, spread_pct} dict; bool and string leaves come through as-is.

    They have to: the cert phase's entire verdict is boolean, and dropping
    non-numerics left the written log showing two timings and not one word about
    whether the rotation actually worked."""
    out = {}
    if isinstance(node, dict):
        if "median" in node and "spread_pct" in node:
            out[prefix] = node
            return out
        for k, v in node.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif prefix and node is not None:
        out[prefix] = node
    return out


HIGHER_IS_BETTER = ("ops_per_s", "conns_per_s", "resumption_rate",
                    "resume_saving_pct", "connected", "tls_over_plain")

# Knobs and sample counts, not results. Comparing them produces confident
# verdicts on how many times the pinger happened to loop.
NOT_A_RESULT = ("burst", "workers", "n")


def print_compare(new_m, old_path, new_meta):
    with open(old_path) as f:
        old = json.load(f)
    old_meta, old_m = old.get("meta", {}), old.get("metrics", {})
    old_repeat = (old_meta.get("params") or {}).get("repeat", 1)
    new_repeat = (new_meta.get("params") or {}).get("repeat", 1)
    print(f"\n{GREEN}{'=' * 78}{RESET}")
    print(f"  compare vs {old_path}  (tag '{old_meta.get('tag')}', "
          f"{old_meta.get('git_rev')}, {old_meta.get('utc')})")
    if old_meta.get("machine") != platform.machine() or \
       old_meta.get("cpu_count") != os.cpu_count():
        print(f"  {RED}different machine — this comparison is not valid{RESET}")

    # A single repeat measures no spread, so the floor is 0% and every jitter
    # reads as a verdict. Refusing to render the verdict column is the honest
    # answer: the run cannot tell signal from noise and should not pretend to.
    # Comparing runs taken with different knobs is not a comparison. --workers
    # in particular sets how many handshakes are in flight at once, and the
    # storm's stall numbers scale with it (8 → 5ms, 64 → 18.5ms, 300 → 48ms on
    # an unchanged server), so a mismatch there can manufacture or erase any
    # result you like.
    new_params = new_meta.get("params") or {}
    old_params = old_meta.get("params") or {}
    drift = [k for k in ("burst", "workers", "handshakes", "bench_requests",
                         "bench_clients", "pipelines", "tests")
             if k in old_params and k in new_params
             and old_params[k] != new_params[k]]
    if drift:
        print(f"  {RED}parameters differ: "
              + ", ".join(f"{k} {old_params[k]!r} → {new_params[k]!r}"
                          for k in drift)
              + f"{RESET}")
        print(f"  {RED}these runs are not comparable — re-run with the "
              f"baseline's parameters{RESET}")

    floorless = old_repeat < 2 or new_repeat < 2
    if floorless:
        print(f"  {RED}no noise floor: repeats were {old_repeat} (before) and "
              f"{new_repeat} (after){RESET}")
        print(f"  {RED}every delta below is unqualified — re-run both sides "
              f"with --repeat 3 or more{RESET}")
    print(f"{GREEN}{'=' * 78}{RESET}")
    print(f"    {'metric':<46}{'before':>11}{'after':>11}{'delta':>10}   noise")

    a, b = flatten(old_m), flatten(new_m)
    for k in sorted(b):
        if k not in a or k.split(".")[-1] in NOT_A_RESULT:
            continue
        if not isinstance(a[k], dict) or not isinstance(b[k], dict):
            continue                      # bool/str verdicts: reported, not diffed
        ov, nv = a[k]["median"], b[k]["median"]
        if not isinstance(ov, (int, float)) or not isinstance(nv, (int, float)):
            continue
        if ov == 0:
            continue
        delta = (nv - ov) / abs(ov) * 100
        noise = max(a[k].get("spread_pct") or 0, b[k].get("spread_pct") or 0)
        better_up = any(h in k for h in HIGHER_IS_BETTER)
        improved = delta > 0 if better_up else delta < 0
        if floorless:
            color, mark = YELLOW, "?"
        elif abs(delta) <= noise:
            # Below the noise floor it is not a result, whichever way it points.
            color, mark = YELLOW, "~"
        else:
            color, mark = (GREEN, "+") if improved else (RED, "!")
        print(f"    {k:<46}{fmt(ov, '', 11)}{fmt(nv, '', 11)}"
              f"{color}{delta:>9.1f}%{RESET} {mark}  ±{noise:.1f}%")
    if floorless:
        print(f"\n    {YELLOW}?{RESET} no noise floor — none of these deltas "
              f"is qualified as a result")
    else:
        print(f"\n    {GREEN}+{RESET} better   {RED}!{RESET} worse   "
              f"{YELLOW}~{RESET} inside the noise floor, not a result")


def write_artifacts(metrics, meta, json_path, md_path):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w") as f:
        json.dump({"meta": meta, "metrics": metrics}, f, indent=2, default=str)

    lines = [
        f"\n## {meta['utc']} — tag `{meta['tag']}`\n",
        f"- rev `{meta['git_rev']}`{' (dirty)' if meta['git_dirty'] else ''}, "
        f"build `{meta['build']['cmake_build_type']}`, {meta['openssl']}, "
        f"{meta['cpu_count']} cpus, kernel {meta['kernel']}",
        f"- params: {json.dumps(meta['params'])}",
        f"- artifact: `{os.path.relpath(json_path, repo_root())}`\n",
        "| metric | median | noise (±) |",
        "|---|---:|---:|",
    ]
    for k, node in sorted(flatten(metrics).items()):
        if isinstance(node, dict):
            lines.append(f"| `{k}` | {node['median']:,.3f} | "
                         f"{node['spread_pct']:.1f}% |")
        else:
            lines.append(f"| `{k}` | {node} | — |")
    with open(md_path, "a") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="MYRED TLS measurement harness (V10.6.1 Step 0)")
    ap.add_argument("--server", default=os.path.join(repo_root(), "build", "server"))
    ap.add_argument("--plain-port", type=int, default=PLAIN_PORT)
    ap.add_argument("--tls-port", type=int, default=TLS_PORT)
    ap.add_argument("--cert", default=os.path.join(repo_root(), "tls", "cert.pem"))
    ap.add_argument("--key", default=os.path.join(repo_root(), "tls", "key.pem"))
    ap.add_argument("--phases", default="handshake,storm,bench,cert",
                    help="comma list: handshake,storm,bench,cert")
    ap.add_argument("--repeat", type=int, default=3,
                    help="repeats per metric; the spread across them is the "
                         "reported noise floor")
    ap.add_argument("--handshakes", type=int, default=100)
    ap.add_argument("--burst", type=int, default=300,
                    help="simultaneous arrivals in the accept storm")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--bench-requests", type=int, default=100_000)
    ap.add_argument("--bench-clients", type=int, default=50)
    ap.add_argument("--pipelines", default="16,1")
    ap.add_argument("--tests", default="set,get")
    ap.add_argument("--tag", default="baseline",
                    help="names the artifact: docs/tls_metrics_<tag>.json")
    ap.add_argument("--compare", default=None, metavar="JSON",
                    help="print a delta table against a previous artifact")
    ap.add_argument("--quick", action="store_true",
                    help="tiny run to smoke-test the harness itself")
    ap.add_argument("--allow-debug", action="store_true",
                    help="measure a Debug build anyway; the numbers mean nothing")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    if a.quick:
        a.repeat, a.handshakes, a.burst = 1, 10, 40
        a.bench_requests, a.pipelines = 2000, "16"
    a.pipelines = [int(p) for p in a.pipelines.split(",") if p.strip()]
    a.tests = [t.strip() for t in a.tests.split(",") if t.strip()]
    phases = {p.strip() for p in a.phases.split(",") if p.strip()}

    server_bin = os.path.abspath(a.server)
    if not os.path.exists(server_bin):
        print(f"{RED}server binary not found: {server_bin}{RESET}")
        return 1
    for p in (a.cert, a.key):
        if not os.path.exists(p):
            print(f"{RED}missing TLS material: {p}{RESET}")
            return 1

    build = detect_build(server_bin)
    ok, why = build_is_measurable(build)
    if not ok:
        print(f"{RED}refusing to measure this build: {why}{RESET}")
        print(f"  {server_bin} is {build['size_bytes']:,} bytes"
              + (", carries debug_info" if build["debug_info"] else ""))
        print("  A Debug build runs mem_selfcheck() after every command "
              "(commands.cpp, #ifndef NDEBUG)\n  and that walks the keyspace, so "
              "every number here would be a measurement of the\n  audit, not of "
              "TLS. Build optimized first:")
        print(f"\n      cmake -B build-rel -DCMAKE_BUILD_TYPE=Release && "
              f"cmake --build build-rel -j\n"
              f"      python3 scripts/test_tls.py --server build-rel/server\n")
        print("  --allow-debug runs it anyway, to smoke-test this harness.")
        if not a.allow_debug:
            return 2
        print(f"{YELLOW}  --allow-debug given: continuing with meaningless "
              f"numbers{RESET}")

    bench_exe = shutil.which("redis-benchmark")
    if "bench" in phases and not bench_exe:
        print(f"{YELLOW}redis-benchmark not on PATH — skipping the throughput "
              f"block, which is the whole of the kTLS decision{RESET}")
        phases.discard("bench")
    if "cert" in phases and not shutil.which("openssl"):
        print(f"{YELLOW}openssl not on PATH — skipping the cert-rotation "
              f"block{RESET}")
        phases.discard("cert")

    workdir = tempfile.mkdtemp(prefix="myred-tls-")
    conf_path = os.path.join(workdir, "tls-metrics.conf")
    # The server runs on COPIES inside the workdir. The cert phase rotates by
    # overwriting them in place, which is how real rotation works — and doing
    # that to the repo's own tls/cert.pem would destroy the user's material.
    live_cert = os.path.join(workdir, "server-cert.pem")
    live_key = os.path.join(workdir, "server-key.pem")
    shutil.copyfile(os.path.abspath(a.cert), live_cert)
    shutil.copyfile(os.path.abspath(a.key), live_key)
    conf_lines = [
        f"port {a.plain_port}",
        f"tls-port {a.tls_port}",
        f'tls-cert-file "{live_cert}"',
        f'tls-key-file "{live_key}"',
        # No requirepass: k_max_auth_inflight (4) against Argon2id's 76MiB bound
        # turns an authed run into a measurement of the KDF (V9.7.4).
        "appendonly no",
        # No BGSAVE fork is allowed to land in the middle of a measurement.
        'save ""',
    ]
    with open(conf_path, "w") as f:
        f.write("".join(l + "\n" for l in conf_lines))

    print(f"workdir: {workdir}")
    print(f"plaintext :{a.plain_port}   tls :{a.tls_port}   "
          f"phases: {','.join(sorted(phases))}")

    servers = []
    srv = None
    rc = 0
    try:
        srv = Server(server_bin, workdir, conf_path, "tls-metrics", a.plain_port)
        servers.append(srv)
        # Prove the TLS listener is actually up before timing anything against it.
        try:
            peer_cert_sha256(a.tls_port)
        except OSError as e:
            print(f"{RED}TLS port {a.tls_port} did not accept a handshake: "
                  f"{e}{RESET}")
            raise

        runs = []
        for i in range(a.repeat):
            print(f"\n  repeat {i + 1}/{a.repeat}")
            r = {}
            if "handshake" in phases:
                print("    handshake (tls, plaintext)")
                r["handshake_tls"] = measure_handshake(a.tls_port, True, a.handshakes)
                r["handshake_plain"] = measure_handshake(a.plain_port, False, a.handshakes)
            if "storm" in phases:
                print(f"    accept storm ×{a.burst} (tls, plaintext)")
                r["storm_tls"] = measure_storm(a.tls_port, True, srv.proc.pid,
                                               a.burst, a.workers)
                r["storm_plain"] = measure_storm(a.plain_port, False, srv.proc.pid,
                                                 a.burst, a.workers)
            if "bench" in phases:
                print(f"    redis-benchmark {a.tests} at -P {a.pipelines}")
                r["bench"] = measure_bench(bench_exe, a.plain_port, a.tls_port,
                                           a.bench_requests, a.bench_clients,
                                           a.pipelines, a.tests)
            runs.append(r)

        metrics = aggregate(runs) if runs else {}

        if "cert" in phases:
            print("\n  certificate rotation (restarts the server — runs last)")
            cert, srv = measure_cert_rotation(
                server_bin, workdir, conf_path, srv, a.plain_port, a.tls_port,
                live_cert, live_key, servers.append)
            # Measured once — a restart is a restart — but pushed through the
            # same aggregator so every leaf in the artifact has one shape and
            # --compare can diff it like anything else.
            metrics["cert"] = aggregate([cert])

        meta = collect_meta(a, build, server_bin)
        print_report(metrics, meta)

        json_path = os.path.join(repo_root(), "docs", f"tls_metrics_{a.tag}.json")
        md_path = os.path.join(repo_root(), "docs", "tls_metrics.md")
        write_artifacts(metrics, meta, json_path, md_path)
        print(f"\n  artifact: {os.path.relpath(json_path, repo_root())}")
        print(f"  log:      {os.path.relpath(md_path, repo_root())}")

        if a.compare:
            print_compare(metrics, a.compare, meta)

    except Exception as e:                             # noqa: BLE001
        # Route it through ERRORS so the finally below keeps the workdir and
        # prints the server's stderr — a crash is exactly when that evidence
        # matters, and the default cleanup would have deleted it.
        fail(f"harness aborted: {e!r}")
        raise
    finally:
        if ERRORS:
            rc = 1
            print(f"\n{RED}{len(ERRORS)} measurement(s) failed{RESET}")
            for srv_i in servers:
                # evidence rule: a failure without the server's own words is
                # unusable
                try:
                    tail = srv_i.stderr_text().strip().splitlines()[-12:]
                except OSError:
                    continue
                print(f"\n{YELLOW}stderr tail ({srv_i.stderr_path}){RESET}")
                for ln in tail:
                    print("   " + ln)
        for srv_i in servers:
            try:
                srv_i.stop()
            except Exception:                          # noqa: BLE001
                pass
        if a.keep or ERRORS:
            print(f"\n{YELLOW}workdir kept for inspection: {workdir}{RESET}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    return rc


if __name__ == "__main__":
    sys.exit(main())
