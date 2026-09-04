#!/usr/bin/env python3
"""
Replication suite for MYRED (milestone V10).

Runs a master and a replica on private ports in their own temp dirs, with a
killable TCP proxy standing between them, so every replication path can be
exercised from ONE terminal with no manual setup.

Covers, in order:
  1. V10.2  full resync: the replica boots straight into the role from its
     config file, its dataset matches the master's, and the master reports a
     full sync.
  2. V10.2b live streaming: writes on the master reach the replica.
  3. V10.3a read-only gate: writes from an ordinary client are refused, reads
     are served, and the replication stream itself is NOT refused.
  4. V10.3a link loss: dropping the socket must NOT promote the replica.
  5. V10.4  partial resync: reconnecting after a small gap continues from the
     backlog instead of retransferring the dataset.
  6. V10.4  fallback: a gap larger than the backlog degrades to a full resync
     rather than to a corrupt +CONTINUE.
  7. V10.3a/b promotion: REPLICAOF NO ONE makes it writable, mints a new
     repl_id, and CONFIG REWRITE drops the `replicaof` line from the file.
  8. V10.3b restart: a config file carrying `replicaof` comes back as a replica
     with no manual command.
  9. V10.6b repl-timeout: the directive round-trips in seconds, a link that goes
     silent WITHOUT closing is dropped by both ends, and both notice with no
     traffic at all to wake them.
 10. V10.6c durability floor: min-replicas-to-write / min-replicas-max-lag, the
     -NOREPLICAS refusal, and the guard that keeps the floor from ever refusing
     the replication stream itself.
 11. V10.6a failover: a second replica, the master stopped, the survivor
     promoted, and the sibling repointed at it — which must partial-resync off
     the promoted instance's retired history instead of pulling a full image,
     and must still do so on the SECOND reconnect (site 10: adopt the replid
     that +CONTINUE carries).
 12. V10.6d coordinated FAILOVER, on its own pair of instances: argument
     rejections, the write pause, ABORT, the TIMEOUT deadline firing on an idle
     server, FORCE paying for its lost bytes with a full resync, and the clean
     handover that moves no RDB at all.

Phases 9-12 are skipped, loudly, on a binary that predates them; the suite is
meant to stay runnable while the milestone is half-applied.

Checks marked [REG] pin a bug that actually shipped, or a design decision whose
violation is silent. In particular:
  - A partial resync and a full resync BOTH leave the replica with correct data,
    so every partial-resync check asserts on the master's sync_* counters, never
    on "the keys are there".
  - Until promotion, the staged `g_config.replicaof_*` and the live
    `g_data.master_*` hold identical values, so the rewrite check only means
    something AFTER a REPLICAOF NO ONE.

Why a proxy instead of killing the master: the master must stay up across the
gap. Killing it would destroy the backlog and mint a new repl_id, which forces a
full resync and would make the partial-resync test silently vacuous.

Usage:
    python3 scripts/test_replication.py [--server build/server]
                                        [--master-port 12404]
                                        [--replica-port 12405]
                                        [--proxy-port 12406] [--keep]

The V10.6d phase runs on its own pair (12408/12409 through a proxy on 12410)
because a handover swaps the roles of both instances: doing it on the main pair
would leave every later phase talking to the wrong end.
"""

import argparse
import os
import select
import shutil
import socket
import sys
import tempfile
import threading
import time

from myred_testlib import (GREEN, RED, YELLOW, RESET, Server, check, cmd,
                           connect, enc, expect_error, recv, repo_root, summary)

# The minimum the server accepts (k_repl_backlog_min). Deliberately tiny: it is
# what makes "gap larger than the backlog" reachable with a few KB of writes.
BACKLOG_BYTES = 16 * 1024

# Replication is asynchronous — every cross-instance assertion has to poll.
SYNC_WAIT = 5.0

# What the V10.6b phases wind repl-timeout down to. Has to clear
# k_repl_ack_period_ms (1s) with room to spare or the master reaps a healthy
# replica between two of its own ACKs; has to stay small or the suite crawls.
SHORT_TIMEOUT = 3

# What V10.6c winds min-replicas-max-lag down to. Same trade as SHORT_TIMEOUT,
# against the same 1s ack period.
SHORT_LAG = 2

SKIPPED = 0


# ------------------------------------------------------------------ helpers

def skip(name, why):
    """Not a pass and not a failure: the binary does not have the feature yet."""
    global SKIPPED
    SKIPPED += 1
    print(f"  {YELLOW}skip{RESET} {name} -- {why}")

def info(sock, section=None):
    """INFO as a dict. Falls back to the whole dump on a pre-V10.2.2 binary."""
    try:
        raw = cmd(sock, "INFO", section) if section else cmd(sock, "INFO")
    except RuntimeError:
        raw = cmd(sock, "INFO")
    out = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k] = v
    return out


def wait_until(pred, timeout=SYNC_WAIT, interval=0.05):
    """Poll until pred() is truthy. Swallows transient connection errors."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except (RuntimeError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


def link_up(sock):
    d = info(sock, "replication")
    return d.get("role") == "slave" and d.get("master_link_status") == "up"


def counters(sock):
    d = info(sock, "stats")
    return (int(d.get("sync_full", -1)),
            int(d.get("sync_partial_ok", -1)),
            int(d.get("sync_partial_err", -1)))


def write_conf(path, lines):
    with open(path, "w") as f:
        f.write("".join(l + "\n" for l in lines))


def conf_has_replicaof(path):
    with open(path) as f:
        return any(l.strip().startswith("replicaof ") for l in f)


def has_directive(sock, name):
    """True if CONFIG GET answers for `name` — the capability probe for a
    directive-gated milestone step."""
    try:
        r = cmd(sock, "CONFIG", "GET", name)
    except RuntimeError:
        return False
    return isinstance(r, list) and len(r) >= 2


def get_directive(sock, name):
    return cmd(sock, "CONFIG", "GET", name)[1]


def set_if_present(sock, name, value):
    """Set a directive the binary may not have yet. Returns True if it took."""
    if not has_directive(sock, name):
        return False
    cmd(sock, "CONFIG", "SET", name, str(value))
    return True


def ok_or_err(sock, *args):
    """(reply, error_text) — one of the two is always None.

    `cmd` raises on `-ERR`, which inside a long handover sequence would abandon
    every check after it and leave the pair half-failed-over. A phase has to be
    able to record "this was rejected" and carry on.
    """
    try:
        return cmd(sock, *args), None
    except RuntimeError as e:
        return None, str(e)


def log_has(srv, needle):
    """Poll the server's stderr FILE, not the server.

    The whole point of the V10.6b phases is that the deadline fires on an idle
    instance. Polling INFO to find out would wake the event loop and hide
    exactly the bug being tested (a deadline missing from next_timer_ms only
    misbehaves while nothing else wakes the loop), so the observation has to
    happen off to the side.
    """
    try:
        return needle in srv.stderr_text()
    except OSError:
        return False


# -------------------------------------------------------------------- proxy

class Proxy:
    """A killable TCP hop: replica -> proxy -> master.

    stop() closes the listener AND every live pair, so the replica sees a FIN and
    its master link really dies; start() reopens on the same port.

    freeze() is the other failure mode, and the one V10.6b is about: stop moving
    bytes but leave every socket OPEN. A link that closes is already handled —
    poll() reports it immediately. A link that just goes quiet is invisible to
    poll() and only a clock can catch it.
    """

    def __init__(self, listen_port, target_port):
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
            socks = [self._lsock] + self._conns if self._lsock else list(self._conns)
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
                upstream = socket.create_connection(("127.0.0.1", self.target_port), 2)
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
                    # and neither end sees an error, an EOF or a reset.
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


# -------------------------------------------------------------------- phases

def phase_full_resync(master, replica, replica_srv):
    print("\n-- V10.2 full resync --")
    check("replica booted into the role from its config file (V10.3b)",
          wait_until(lambda: link_up(replica)),
          f"INFO replication = {info(replica, 'replication')}")

    r = info(replica, "replication")
    m = info(master, "replication")
    check("replica reports role:slave", r.get("role") == "slave", r.get("role"))
    check("[REG] replica adopted the master's replid",
          r.get("master_replid") == m.get("master_replid"),
          f"replica={r.get('master_replid')} master={m.get('master_replid')}")
    check("master counts one connected replica",
          wait_until(lambda: info(master, "replication").get("connected_slaves") == "1"),
          info(master, "replication").get("connected_slaves"))

    for k, v in (("pre1", "a"), ("pre2", "b"), ("pre3", "c")):
        check(f"pre-resync key {k} arrived",
              wait_until(lambda k=k, v=v: cmd(replica, "GET", k) == v))

    if replica_srv is not None:
        log = replica_srv.stderr_text()
        check("replica logged the resync", "streaming from master" in log,
              log.strip().splitlines()[-1:] or ["<empty>"])


def phase_streaming(master, replica):
    print("\n-- V10.2b live streaming --")
    cmd(master, "SET", "live1", "v1")
    check("SET propagates", wait_until(lambda: cmd(replica, "GET", "live1") == "v1"))

    cmd(master, "SADD", "liveset", "m1", "m2")
    check("SADD propagates",
          wait_until(lambda: cmd(replica, "SCARD", "liveset") == 2))

    cmd(master, "DEL", "live1")
    check("DEL propagates", wait_until(lambda: cmd(replica, "GET", "live1") is None))

    cmd(master, "SET", "ttlkey", "x")
    cmd(master, "EXPIRE", "ttlkey", 1000)
    check("[REG] TTL replicates as an absolute time, not a relative one",
          wait_until(lambda: 0 < int(cmd(replica, "TTL", "ttlkey")) <= 1000),
          "a relative EXPIRE re-applied on the replica would drift, or miss entirely")


def phase_readonly(master, replica):
    print("\n-- V10.3a read-only gate --")
    err = expect_error(replica, "SET", "nope", "1")
    check("SET on a replica is refused", err is not None and err.startswith("READONLY"), err)

    err = expect_error(replica, "FLUSHALL")
    check("[REG] FLUSHALL is refused too (it carries is_write)",
          err is not None and err.startswith("READONLY"), err)

    check("reads are still served", cmd(replica, "GET", "pre1") == "a")

    err = expect_error(replica, "MULTI")
    check("MULTI itself is allowed", err is None, err)
    err = expect_error(replica, "SET", "nope", "1")
    check("a queued write is refused at queue time", err is not None, err)
    err = expect_error(replica, "EXEC")
    check("the poisoned transaction aborts", err is not None and "EXECABORT" in str(err), err)

    # the whole point of the bypass: the stream must not hit the gate
    cmd(master, "SET", "after_gate", "ok")
    check("[REG] the replication stream still applies through the gate",
          wait_until(lambda: cmd(replica, "GET", "after_gate") == "ok"))


def phase_link_loss(replica, proxy):
    print("\n-- V10.3a link loss must not promote --")
    proxy.stop()
    check("link goes down",
          wait_until(lambda: info(replica, "replication").get("master_link_status") == "down"),
          info(replica, "replication").get("master_link_status"))

    d = info(replica, "replication")
    check("[REG] a dropped socket does NOT promote the replica",
          d.get("role") == "slave",
          "role went to master on link loss: both instances would accept writes")
    check("[REG] the master address survives the drop",
          d.get("master_host") and d.get("master_port"), d)

    err = expect_error(replica, "SET", "nope", "1")
    check("[REG] still read-only while disconnected",
          err is not None and err.startswith("READONLY"), err)


def phase_partial_resync(master, replica, proxy, proxy_port):
    print("\n-- V10.4 partial resync --")
    full0, ok0, err0 = counters(master)

    # a gap small enough to still be in the ring
    for i in range(20):
        cmd(master, "SET", f"gap{i}", f"v{i}")

    proxy.start()
    cmd(replica, "REPLICAOF", "127.0.0.1", proxy_port)  # same host:port -> history claimed
    check("link comes back", wait_until(lambda: link_up(replica)),
          info(replica, "replication"))

    check("gap keys arrived",
          wait_until(lambda: cmd(replica, "GET", "gap19") == "v19"))

    full1, ok1, err1 = counters(master)
    check("[REG] the reconnect was a PARTIAL resync",
          ok1 == ok0 + 1,
          f"sync_partial_ok {ok0} -> {ok1} (correct data alone proves nothing here)")
    check("[REG] no RDB was retransferred",
          full1 == full0,
          f"sync_full {full0} -> {full1}")


def phase_gap_too_large(master, replica, proxy, proxy_port):
    print("\n-- V10.4 gap larger than the backlog falls back --")
    proxy.stop()
    check("link down again",
          wait_until(lambda: info(replica, "replication").get("master_link_status") == "down"))

    full0, ok0, err0 = counters(master)

    # overflow the ring several times over
    blob = "x" * 1024
    for i in range(BACKLOG_BYTES // 1024 * 3):
        cmd(master, "SET", f"big{i}", blob)
    cmd(master, "SET", "past_the_gap", "yes")

    proxy.start()
    cmd(replica, "REPLICAOF", "127.0.0.1", proxy_port)
    check("link comes back", wait_until(lambda: link_up(replica)))
    check("replica caught up", wait_until(lambda: cmd(replica, "GET", "past_the_gap") == "yes"))

    full1, ok1, err1 = counters(master)
    check("[REG] an unservable offset degrades to a FULL resync",
          full1 == full0 + 1,
          f"sync_full {full0} -> {full1}: a +CONTINUE here would be silent divergence")
    check("the refusal was counted", err1 == err0 + 1, f"sync_partial_err {err0} -> {err1}")


def phase_auto_reconnect(master, replica, proxy):
    print("\n-- V10.4c automatic reconnect --")
    full0, ok0, _ = counters(master)

    proxy.stop()
    check("link down",
          wait_until(lambda: info(replica, "replication").get("master_link_status") == "down"))

    for i in range(10):
        cmd(master, "SET", f"auto{i}", f"v{i}")

    proxy.start()
    # deliberately NO manual REPLICAOF here: the replica must re-dial on its own.
    # Generous timeout — the backoff can be sitting at k_repl_retry_max_ms.
    check("[REG] the replica re-dials without being told",
          wait_until(lambda: link_up(replica), timeout=25.0),
          "repl_cron never fired -- check that next_timer_ms() wakes poll() for a "
          "disconnected replica, or it will never run on an idle keyspace")
    check("writes missed during the outage arrived",
          wait_until(lambda: cmd(replica, "GET", "auto9") == "v9"))

    full1, ok1, _ = counters(master)
    check("the automatic reconnect used a partial resync",
          ok1 == ok0 + 1 and full1 == full0,
          f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1}")


def slave0(sock):
    """The master's `slave0:` INFO line as a dict."""
    out = {}
    for part in info(sock, "replication").get("slave0", "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def phase_wait(master, master_port):
    print("\n-- V10.5 REPLCONF ACK + WAIT --")
    cmd(master, "SET", "ackkey", "v")

    # the replica must report progress on its own, without being asked
    check("master learns the replica's offset from periodic acks",
          wait_until(lambda: int(slave0(master).get("offset", 0)) > 0, timeout=6.0),
          f"slave0 = {slave0(master)}")

    t0 = time.time()
    n = cmd(master, "WAIT", 1, 4000)
    check("WAIT 1 counts the caught-up replica", n == 1, n)
    check("...and returned well inside its timeout", time.time() - t0 < 3.5,
          f"{time.time() - t0:.1f}s")

    # an unsatisfiable request must time out with a SHORT COUNT, never an error
    t0 = time.time()
    n = cmd(master, "WAIT", 5, 1000)
    dt = time.time() - t0
    check("[REG] an unsatisfiable WAIT returns a short count, not an error", n == 1, n)
    check("...after roughly its timeout", 0.7 < dt < 3.0, f"{dt:.1f}s")
    check("the connection still works afterwards", cmd(master, "PING") == "PONG")

    # the whole point of deferring instead of blocking
    blocker = connect(master_port)
    blocker.sendall(enc("WAIT", 5, 2000))       # cannot be satisfied
    other = connect(master_port)
    check("[REG] a pending WAIT does not block the event loop",
          cmd(other, "PING") == "PONG",
          "the loop stalled: WAIT must defer the reply, not wait in the handler")
    check("the deferred client is resumed on timeout", recv(blocker) == 1)
    blocker.close()
    other.close()

    # inside EXEC the reply is one element of an already-sized array
    cmd(master, "MULTI")
    cmd(master, "WAIT", 5, 5000)
    t0 = time.time()
    res = cmd(master, "EXEC")
    check("[REG] WAIT inside EXEC answers immediately instead of deferring",
          isinstance(res, list) and len(res) == 1 and time.time() - t0 < 2.0,
          f"{res!r} after {time.time() - t0:.1f}s")


def phase_min_replicas(master, replica, proxy):
    """V10.6c — refuse writes when too few replicas look healthy.

    The interesting half is not "does it refuse", it is WHICH replicas count.
    A replica that is connected but has stopped acking must stop counting while
    it is still in g_data.replicas, so the phase freezes the link rather than
    dropping it: a dropped replica would prove repl-timeout, not the lag gate.
    """
    print("\n-- V10.6c min-replicas-to-write --")
    to_write0 = get_directive(master, "min-replicas-to-write")
    max_lag0 = get_directive(master, "min-replicas-max-lag")
    check("min-replicas-to-write defaults to 0 (feature off)",
          to_write0 == "0", to_write0)
    check("min-replicas-max-lag defaults to 10 seconds",
          max_lag0 == "10",
          f"{max_lag0!r}: 0 means 'do not judge on lag', so every connected "
          f"replica counts — including one still loading its resync image, "
          f"which can neither ack nor serve. That is a weaker floor than the "
          f"directive is supposed to buy, and it is the default nobody sets")

    check("rejects a negative count",
          expect_error(master, "CONFIG", "SET", "min-replicas-to-write", "-1")
          is not None)
    check("rejects a count past the cap",
          expect_error(master, "CONFIG", "SET", "min-replicas-to-write", "2000")
          is not None)
    check("rejects a lag past the cap (3600s)",
          expect_error(master, "CONFIG", "SET", "min-replicas-max-lag", "99999")
          is not None)
    check("accepts 0 lag (do not judge on lag)",
          expect_error(master, "CONFIG", "SET", "min-replicas-max-lag", "0")
          is None)

    check("link healthy before the floor goes up",
          wait_until(lambda: link_up(replica)),
          info(replica, "replication").get("master_link_status"))
    cmd(master, "CONFIG", "SET", "min-replicas-max-lag", str(SHORT_LAG))
    cmd(master, "CONFIG", "SET", "min-replicas-to-write", "1")

    check("a replica that is acking satisfies the floor",
          expect_error(master, "SET", "floor_ok", "1") is None)
    d = info(master, "replication")
    check("[REG] INFO reports the count on a MASTER",
          d.get("min_slaves_good_slaves") == "1",
          f"min_slaves_good_slaves={d.get('min_slaves_good_slaves')!r} — this is "
          f"the field an operator reads to find out why writes are being refused")

    # Freeze, do not drop: the replica stays in g_data.replicas the whole time.
    proxy.freeze()
    time.sleep(SHORT_LAG + 1.5)

    err = expect_error(master, "SET", "floor_bad", "1")
    check("[REG] a replica that stopped acking stops counting",
          err is not None and err.startswith("NOREPLICAS"), err)
    check("...and the refusal is the whole cost: reads are untouched",
          cmd(master, "GET", "floor_ok") == "1")
    d = info(master, "replication")
    check("min_slaves_good_slaves fell to 0",
          d.get("min_slaves_good_slaves") == "0",
          d.get("min_slaves_good_slaves"))
    check("[REG] the lagging replica is still CONNECTED",
          d.get("connected_slaves") == "1",
          "the master reaped it instead: this phase then proved repl-timeout "
          "and said nothing at all about the lag gate")

    proxy.thaw()
    check("writes resume once the acks come back",
          wait_until(lambda: expect_error(master, "SET", "floor_back", "1") is None,
                     timeout=15.0),
          "the floor never lifted after the link recovered")

    # The guard the md calls out: a replica applying its master's stream has no
    # replicas of its own, so a floor evaluated there would drop the write.
    cmd(replica, "CONFIG", "SET", "min-replicas-to-write", "1")
    cmd(master, "SET", "stream_through_floor", "yes")
    check("[REG] the floor never refuses the replication stream itself",
          wait_until(lambda: cmd(replica, "GET", "stream_through_floor") == "yes"),
          "the gate must test !replica_mode AND !g_loading — a replica that "
          "drops a write it was sent has silently forked from its master")
    cmd(replica, "CONFIG", "SET", "min-replicas-to-write", "0")

    cmd(master, "CONFIG", "SET", "min-replicas-to-write", to_write0)
    cmd(master, "CONFIG", "SET", "min-replicas-max-lag", max_lag0)


def phase_promotion(master, replica, replica_conf, proxy_port):
    print("\n-- V10.3a/b promotion --")
    before = info(replica, "replication").get("master_replid")

    cmd(replica, "REPLICAOF", "NO", "ONE")
    d = info(replica, "replication")
    check("promoted to master", d.get("role") == "master", d.get("role"))
    check("[REG] promotion mints a NEW replid",
          d.get("master_replid") != before,
          "keeping the old master's replid would let V10.4 accept an unsafe +CONTINUE")
    check("writable again", expect_error(replica, "SET", "now", "writable") is None)

    cmd(replica, "CONFIG", "REWRITE")
    check("[REG] CONFIG REWRITE drops the replicaof line after promotion",
          not conf_has_replicaof(replica_conf),
          "emit is reading the staged g_config instead of the live g_data role")

    # re-pointing at the same master must NOT claim the old history
    full0, ok0, _ = counters(master)
    cmd(replica, "REPLICAOF", "127.0.0.1", proxy_port)
    check("re-attached", wait_until(lambda: link_up(replica)))
    full1, ok1, _ = counters(master)
    check("[REG] a promoted instance forfeits its history (full resync)",
          full1 == full0 + 1 and ok1 == ok0,
          f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1}")

    cmd(replica, "CONFIG", "REWRITE")
    check("CONFIG REWRITE restores the line when a replica again",
          conf_has_replicaof(replica_conf))


def phase_restart(server_bin, replica_dir, replica_conf, replica_port, master):
    print("\n-- V10.3b restart keeps the role --")
    srv = Server(server_bin, replica_dir, replica_conf, "replica-restart", replica_port)
    replica = connect(replica_port)
    check("[REG] a restarted replica comes back a REPLICA, not a writable master",
          wait_until(lambda: link_up(replica)),
          f"INFO replication = {info(replica, 'replication')}")

    cmd(master, "SET", "after_restart", "yes")
    check("streaming resumed after the restart",
          wait_until(lambda: cmd(replica, "GET", "after_restart") == "yes"))
    err = expect_error(replica, "SET", "nope", "1")
    check("still read-only after the restart",
          err is not None and err.startswith("READONLY"), err)
    replica.close()
    return srv


def phase_repl_timeout_config(master):
    print("\n-- V10.6b repl-timeout directive --")
    before = get_directive(master, "repl-timeout")
    check("defaults to 60 seconds", before == "60", before)

    # The units check config_selfcheck CANNOT do: its get->apply->get probe still
    # agrees with itself when the getter forgets its /1000, because apply() and
    # get() are wrong in the same direction. Same shape as the V9.8 `appendonly`
    # getter bug — only an external observer catches it.
    cmd(master, "CONFIG", "SET", "repl-timeout", "5")
    got = get_directive(master, "repl-timeout")
    check("[REG] CONFIG SET/GET round-trips in SECONDS, not milliseconds",
          got == "5",
          f"got {got!r}; 5000 means the getter is missing its /1000")

    check("rejects a value past the cap",
          expect_error(master, "CONFIG", "SET", "repl-timeout", "99999") is not None,
          "out-of-range values must fail loudly, not clamp silently")
    check("accepts 0 (disabled)",
          expect_error(master, "CONFIG", "SET", "repl-timeout", "0") is None)
    cmd(master, "CONFIG", "SET", "repl-timeout", before)


def phase_idle_keepalive(master, replica, replica_srv):
    """An idle master is a HEALTHY master, and the timeout must not say otherwise.

    Nothing travels master->replica on a link with no writes: REPLCONF ACK is
    replica->master only, and the master never answers it. So a timeout measured
    on inbound bytes expires on a perfectly good link the moment traffic stops —
    the replica drops it, resyncs, and does it again every repl-timeout seconds
    forever. Redis buys the difference with repl-ping-replica-period.
    """
    print("\n-- V10.6b an idle link must survive --")
    # The keepalive period has to stay well under the timeout — that ordering IS
    # the contract. Squeezing both down keeps the phase quick without changing it.
    pinged = set_if_present(master, "repl-ping-replica-period", 1)
    cmd(replica, "CONFIG", "SET", "repl-timeout", str(SHORT_TIMEOUT))
    check("link up before the idle window",
          wait_until(lambda: link_up(replica)))

    mark = replica_srv.stderr_text().count("no data from master")
    cmd(master, "SET", "idle_probe", "1")     # one write, then nothing at all

    # Deliberately longer than repl-timeout: the whole question is whether
    # silence alone is enough to condemn the link.
    time.sleep(SHORT_TIMEOUT * 2 + 1)

    drops = replica_srv.stderr_text().count("no data from master") - mark
    check("[REG] a quiet master is not mistaken for a dead one",
          drops == 0,
          f"the replica dropped a healthy master {drops}x in "
          f"{SHORT_TIMEOUT * 2 + 1}s"
          + ("" if pinged else
             " — no repl-ping-replica-period directive: the master sends nothing "
             "at all on an idle link, so this also flaps on the 60s default, "
             "just once a minute instead of twice in seven seconds"))
    check("...and the link is still up afterwards", link_up(replica),
          info(replica, "replication").get("master_link_status"))
    cmd(replica, "CONFIG", "SET", "repl-timeout", "60")


def phase_wedged_master(master, replica, master_srv, replica_srv, proxy):
    """A link that goes silent without closing. Before V10.6b nothing on either
    side had a reason to look at the clock, so this state persisted forever."""
    print("\n-- V10.6b wedged link (silent, not closed) --")
    check("healthy before the freeze", wait_until(lambda: link_up(replica)),
          info(replica, "replication").get("master_link_status"))
    set_if_present(master, "repl-ping-replica-period", 1)
    for s in (master, replica):
        cmd(s, "CONFIG", "SET", "repl-timeout", str(SHORT_TIMEOUT))

    io_before = info(replica, "replication").get("master_last_io_seconds_ago")
    check("master_last_io_seconds_ago is present and small",
          io_before is not None and 0 <= int(io_before) <= 2, io_before)

    proxy.freeze()

    # From here until the asserts, NOTHING touches either server. Both are fully
    # idle, so the only thing that can fire the timeout is a deadline that
    # next_timer_ms() actually knows about. Watching stderr keeps the
    # observation off the wire — an INFO poll would wake the loop and mask the
    # bug. This is the milestone's recurring lesson, stated as a test.
    dropped = wait_until(lambda: log_has(replica_srv, "no data from master"),
                         timeout=SHORT_TIMEOUT + 4)
    check("[REG] the replica drops a silent master with no traffic to wake it",
          dropped,
          "still STREAMING: either repl_cron has no timeout check, or "
          "next_timer_ms() has no deadline for it and poll() slept through it")

    reaped = wait_until(lambda: log_has(master_srv, "silent for"),
                        timeout=SHORT_TIMEOUT + 4)
    check("[REG] the master drops a replica that stopped acking",
          reaped,
          "a dead replica left in g_data.replicas keeps counting toward WAIT")

    # only now do we talk to them
    d = info(replica, "replication")
    check("replica reports the link down", d.get("master_link_status") == "down",
          d.get("master_link_status"))
    # -1 only while there is no socket at all. By now the replica has usually
    # re-dialled into the frozen proxy and is sitting in HANDSHAKE with a fresh
    # stamp, so the deterministic property is the negative one: it must never
    # still be reporting the real age of the master's last word.
    io_down = d.get("master_last_io_seconds_ago")
    check("master_last_io_seconds_ago reflects the drop, not a stale age",
          io_down == "-1" or 0 <= int(io_down) <= 2,
          f"{io_down} (expected -1, or small after the re-dial)")
    check("[REG] a dropped link does NOT promote the replica",
          d.get("role") == "slave",
          "losing the master must never make an instance writable")
    check("master shows no replicas",
          info(master, "replication").get("connected_slaves") == "0",
          info(master, "replication").get("connected_slaves"))
    n = cmd(master, "WAIT", 1, 500)
    check("a reaped replica cannot satisfy WAIT", n == 0, n)

    # and it must heal by itself once the path comes back
    proxy.thaw()
    check("reconnects on its own after the link recovers",
          wait_until(lambda: link_up(replica), timeout=15.0),
          f"INFO replication = {info(replica, 'replication')}")
    cmd(master, "SET", "after_wedge", "ok")
    check("streaming resumed", wait_until(
        lambda: cmd(replica, "GET", "after_wedge") == "ok"))

    for s in (master, replica):
        cmd(s, "CONFIG", "SET", "repl-timeout", "60")
    set_if_present(master, "repl-ping-replica-period", 10)


def phase_failover(server_bin, workdir, srvs, m_port, r_port, p_port):
    """V10.6d — the coordinated, zero-loss handover.

    Its own pair, because a handover swaps both roles and every later phase on
    the main pair would then be talking to the wrong instance.

    The target sits behind a proxy so its ACKs can be stopped WITHOUT stopping
    the target itself: WAIT_FOR_SYNC, the write pause, ABORT and the TIMEOUT
    abort all need a replica that is present but not caught up. The handover
    dial does not use the proxy — `repl_start` goes to the address named in
    FAILOVER TO — so a frozen path never hides a broken handover.

    Direction matters and is deliberate. FORCE runs first, master -> target,
    while the path is frozen; the clean handover then runs back the other way
    on a link that is genuinely caught up. Two handovers, both directions, and
    the second one starts from an instance that was demoted by the first.
    """
    print("\n-- V10.6d coordinated FAILOVER --")
    fdir = os.path.join(workdir, "failover")
    mdir, rdir = os.path.join(fdir, "master"), os.path.join(fdir, "replica")
    os.makedirs(mdir, exist_ok=True)
    os.makedirs(rdir, exist_ok=True)
    mconf = os.path.join(mdir, "fo-master.conf")
    rconf = os.path.join(rdir, "fo-replica.conf")
    write_conf(mconf, [f"port {m_port}", "appendonly no"])
    write_conf(rconf, [f"port {r_port}", "appendonly no",
                       f"replicaof 127.0.0.1 {p_port}"])

    proxy = Proxy(p_port, m_port)
    proxy.start()
    m = r = None
    msrv = rsrv = None
    try:
        msrv = Server(server_bin, mdir, mconf, "fo-master", m_port)
        srvs.append((msrv, "fo-master"))
        m = connect(m_port)
        for i in range(5):
            cmd(m, "SET", f"fo{i}", f"v{i}")

        rsrv = Server(server_bin, rdir, rconf, "fo-replica", r_port)
        srvs.append((rsrv, "fo-replica"))
        r = connect(r_port)
        check("failover pair is linked", wait_until(lambda: link_up(r)),
              f"INFO replication = {info(r, 'replication')}")

        # ---------------------------------------------------------- rejections
        for args, want in (
            (("FAILOVER", "TO", "127.0.0.1"), "needs a host and a port"),
            (("FAILOVER", "TO", "127.0.0.1", "0"), "invalid FAILOVER target port"),
            (("FAILOVER", "TO", "127.0.0.1", "70000"), "invalid FAILOVER target port"),
            (("FAILOVER", "TIMEOUT", "abc"), "invalid FAILOVER TIMEOUT"),
            (("FAILOVER", "WAT"), "syntax error"),
            (("FAILOVER", "FORCE", "TIMEOUT", "1000"), "FORCE requires TO"),
            (("FAILOVER", "ABORT"), "No failover in progress"),
        ):
            err = expect_error(m, *args)
            check(f"{' '.join(args)} -> {want}",
                  err is not None and want.lower() in err.lower(), err)

        err = expect_error(m, "FAILOVER", "TO", "127.0.0.1", str(r_port), "FORCE")
        check("FORCE without TIMEOUT is refused",
              err is not None and "TIMEOUT" in err, err)
        # The discriminating one: 9999 is a perfectly well-formed port that
        # simply has no replica behind it.
        err = expect_error(m, "FAILOVER", "TO", "127.0.0.1", "9999")
        check("[REG] a valid port with no replica behind it -> not a connected replica",
              err is not None and "not a connected replica" in err,
              f"{err!r} — 'invalid FAILOVER target port' for an ordinary port "
              f"means the range check reads `port < 65535` where it means "
              f"`port > 65535`, which rejects every port anyone would ever use")
        err = expect_error(r, "FAILOVER")
        check("FAILOVER on a replica is refused",
              err is not None and "requires being a master" in err, err)

        # ------------------------------------------- WAIT_FOR_SYNC, pause, ABORT
        proxy.freeze()          # present, but no longer acking
        for i in range(5):
            cmd(m, "SET", f"pause{i}", "x")   # move the finish line past it

        rep, err = ok_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                             "TIMEOUT", "30000")
        if not check("FAILOVER TO <the connected replica> accepted",
                     rep == "OK", err or repr(rep)):
            skip("the rest of V10.6d", f"FAILOVER TO was rejected: {err}")
            return

        d = info(m, "replication")
        check("[REG] INFO reports the pause on the MASTER",
              d.get("failover_state") == "waiting-for-sync",
              f"failover_state={d.get('failover_state')!r} — WAIT_FOR_SYNC only "
              f"ever exists on a master, so a field emitted inside the "
              f"`if (replica)` block can never be seen in the one state an "
              f"operator needs it for")
        err = expect_error(m, "SET", "during_pause", "1")
        check("[REG] writes are paused while the handover waits",
              err is not None and err.startswith("FAILOVER"),
              f"{err!r} — every write accepted here moves the offset the target "
              f"is trying to reach, and the handover never converges")
        check("reads are served throughout the pause", cmd(m, "GET", "fo0") == "v0")
        err = expect_error(m, "FAILOVER", "TO", "127.0.0.1", str(r_port))
        check("a second FAILOVER is refused",
              err is not None and "already in progress" in err, err)

        rep, err = ok_or_err(m, "FAILOVER", "ABORT")
        check("FAILOVER ABORT unwinds a waiting handover", rep == "OK", err)
        check("writes flow again after the abort",
              expect_error(m, "SET", "after_abort", "1") is None)
        check("the role never changed",
              info(m, "replication").get("role") == "master")

        # ------------------------------------- the TIMEOUT deadline, on an idle box
        needle = "timed out waiting for the target"
        mark = msrv.stderr_text().count(needle)
        rep, err = ok_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                             "TIMEOUT", "1500")
        check("FAILOVER with a short TIMEOUT accepted", rep == "OK", err)
        # NOTHING may touch either server until the log says so. A deadline that
        # only fires because an INFO poll happened to wake poll() is not wired —
        # this milestone's recurring bug, in its sixth form.
        check("[REG] the TIMEOUT fires with no traffic to wake the loop",
              wait_until(lambda: msrv.stderr_text().count(needle) > mark,
                         timeout=8.0),
              "still waiting: next_timer_ms() has no entry for "
              "failover_deadline_ms, so poll() slept straight past it")
        check("the timed-out master is writable again",
              wait_until(lambda: expect_error(m, "SET", "after_timeout", "1")
                         is None),
              "failover_reset never ran, or it left the pause gate up")
        check("...still a master, having handed over to nobody",
              info(m, "replication").get("role") == "master")
        check("failover_state is back to no-failover",
              info(m, "replication").get("failover_state", "no-failover")
              == "no-failover")

        # ------------------------------------------------------ FORCE, and its cost
        for i in range(5):
            cmd(m, "SET", f"lost{i}", "x")     # never reaches the frozen target
        full0, ok0, _ = counters(r)            # the target serves the next resync
        rep, err = ok_or_err(m, "FAILOVER", "TO", "127.0.0.1", str(r_port),
                             "FORCE", "TIMEOUT", "1500")
        check("FAILOVER ... FORCE accepted", rep == "OK", err)
        check("[REG] FORCE hands over past a target that never caught up",
              wait_until(lambda: log_has(msrv, "FORCE, handing over"), timeout=10.0),
              "without FORCE this must abort; with it, it must say how many "
              "bytes it is stepping over")
        check("the old master demoted itself",
              wait_until(lambda: info(m, "replication").get("role") == "slave",
                         timeout=15.0),
              info(m, "replication").get("role"))
        check("the target promoted itself on PSYNC ... FAILOVER",
              wait_until(lambda: info(r, "replication").get("role") == "master",
                         timeout=15.0),
              "the 4th PSYNC argument never reached do_psync, or repl_shift_id "
              "was not called before the resync logic read the identity")
        check("the demoted master re-attached to it",
              wait_until(lambda: link_up(m), timeout=15.0),
              f"INFO replication = {info(m, 'replication')}")
        check("[REG] a forced handover is NOT served a +CONTINUE",
              counters(r)[0] == full0 + 1 and counters(r)[1] == ok0,
              f"sync_full {full0}->{counters(r)[0]} — the demoted master is AHEAD "
              f"of the offset the target promoted at, so serving it from the "
              f"backlog would keep writes the new master never saw: two "
              f"instances, one replid, different data")
        check("the writes FORCE stepped over are gone",
              wait_until(lambda: cmd(m, "GET", "lost4") is None, timeout=15.0),
              "that loss is what the keyword buys and what the log line counts; "
              "if they survived, the resync did not replace the dataset")
        check("failover_state cleared on the demoted master",
              info(m, "replication").get("failover_state") == "no-failover",
              "failover_reset('complete') is missing from the HANDSHAKE outcome "
              "that ran — the pause gate stays up forever after a handover")
        proxy.thaw()   # nothing needs it frozen any more

        # -------------------------------------- the clean handover: no RDB at all
        # Whichever instance holds the master role NOW drives it. FORCE may have
        # been refused or silently dropped, and the zero-RDB handover is the
        # headline of the whole milestone — it must not become collateral damage
        # of the phase above it.
        src, dst, dst_port = ((m, r, r_port)
                              if info(m, "replication").get("role") == "master"
                              else (r, m, m_port))
        check("the pair is healthy again before the clean handover",
              wait_until(lambda: link_up(dst), timeout=30.0),
              f"INFO replication = {info(dst, 'replication')}")

        rep, err = ok_or_err(src, "SET", "pre_clean", "1")
        check("the master takes writes before the handover", rep == "OK", err)
        check("...and they reach the replica",
              wait_until(lambda: cmd(dst, "GET", "pre_clean") == "1"))
        err = expect_error(dst, "SET", "nope", "1")
        check("the replica is read-only going in",
              err is not None and err.startswith("READONLY"),
              f"{err!r} — an instance that still accepts writes on the losing "
              f"side of a handover is the split brain this all exists to avoid")

        full0, ok0, _ = counters(dst)   # the target serves the resync afterwards
        shared_id = info(src, "replication").get("master_replid")
        rep, err = ok_or_err(src, "FAILOVER", "TO", "127.0.0.1", str(dst_port),
                             "TIMEOUT", "10000")
        check("clean FAILOVER accepted", rep == "OK", err)
        check("the roles swapped",
              wait_until(lambda: info(dst, "replication").get("role") == "master"
                         and info(src, "replication").get("role") == "slave",
                         timeout=20.0),
              f"target={info(dst, 'replication').get('role')} "
              f"source={info(src, 'replication').get('role')}")
        check("the demoted instance re-attached",
              wait_until(lambda: link_up(src), timeout=15.0),
              f"INFO replication = {info(src, 'replication')}")

        full1, ok1, _ = counters(dst)
        check("[REG] a coordinated handover moves NO RDB image",
              full1 == full0 and ok1 == ok0 + 1,
              f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1} — this is "
              f"the entire point of the command. A full resync here means the "
              f"pause let a write through, the ack wait finished early, or "
              f"PSYNC FAILOVER promoted after the resync logic instead of before")

        dd = info(dst, "replication")
        check("promotion retired the shared history into master_replid2",
              dd.get("master_replid2") == shared_id,
              f"master_replid2={dd.get('master_replid2')} shared={shared_id}")
        check("[REG] the demoted instance adopted the new replid from +CONTINUE",
              info(src, "replication").get("master_replid")
              == dd.get("master_replid"),
              f"demoted={info(src, 'replication').get('master_replid')} "
              f"promoted={dd.get('master_replid')} — quoting the old name on the "
              f"next reconnect asks for a history that has expired past "
              f"second_repl_offset, and every reconnect after this one is full")
        check("no data was lost across the coordinated handover",
              cmd(dst, "GET", "pre_clean") == "1",
              "the write that was acked before the pause must survive it")
        check("the new master is writable",
              expect_error(dst, "SET", "post_clean", "1") is None)
        check("...and streams to the demoted one",
              wait_until(lambda: cmd(src, "GET", "post_clean") == "1"))

        # ------------------------------------------- no TO: it picks for itself
        rep, err = ok_or_err(dst, "FAILOVER", "TIMEOUT", "10000")
        check("bare FAILOVER accepted (target chosen automatically)",
              rep == "OK", err)
        check("...and it handed over to the only replica there is",
              wait_until(lambda: info(src, "replication").get("role") == "master"
                         and info(dst, "replication").get("role") == "slave",
                         timeout=20.0),
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


def phase_promotion_history(server_bin, workdir, master_srv, master,
                            master_port, r1_port, r2_port, proxy):
    """The reason V10.6a exists, and the phase that needs three terminals by hand.

    Two replicas of one master; the master dies; one replica is promoted; the
    other is repointed at it. The survivor must serve that sibling from the
    history it retired, not force a full RDB out of a cluster that is already a
    node down.
    """
    print("\n-- V10.6a promotion keeps the history --")
    r1 = connect(r1_port)

    r2_dir = os.path.join(workdir, "replica2")
    os.makedirs(r2_dir, exist_ok=True)
    r2_conf = os.path.join(r2_dir, "replica2.conf")
    write_conf(r2_conf, [
        f"port {r2_port}",
        "appendonly no",
        f"replicaof 127.0.0.1 {master_port}",   # straight at the master, no proxy
    ])
    r2_srv = Server(server_bin, r2_dir, r2_conf, "replica2", r2_port)
    r2 = connect(r2_port)
    try:
        check("second replica attached", wait_until(lambda: link_up(r2)),
              f"INFO replication = {info(r2, 'replication')}")

        for i in range(20):
            cmd(master, "SET", f"hist{i}", "x" * 64)
        check("both replicas caught up",
              wait_until(lambda: cmd(r1, "GET", "hist19") == "x" * 64
                         and cmd(r2, "GET", "hist19") == "x" * 64))

        # Site 0a: repl_backlog_feed() advances master_repl_offset itself, so a
        # surviving manual += counts every byte twice. Nothing downstream can
        # tell you that directly — the data is still correct — but the offset
        # the replica reports to its master is silently double.
        m_off = info(master, "replication").get("master_repl_offset")
        check("[REG] the replica's offset matches the master's exactly",
              wait_until(lambda: info(r1, "replication").get(
                  "master_repl_offset") == m_off),
              f"master {m_off}, replica "
              f"{info(r1, 'replication').get('master_repl_offset')} — roughly "
              f"double means the STREAMING branch still has its own +=")

        # Site 4: with no feed the ring is empty at the instant of promotion, and
        # it cannot be backfilled afterwards, so every sibling full-resyncs no
        # matter what repl_id2 says.
        histlen = int(info(r1, "replication").get("repl_backlog_histlen", 0))
        check("[REG] a replica feeds its own backlog while streaming",
              histlen > 0,
              "repl_backlog_feed is only called from propagate(), which "
              "propagate_enabled() gates off while g_loading is set")

        old_replid = info(r1, "replication").get("master_replid")

        # Kill the master for real. The proxy stays up but has nothing upstream,
        # which is exactly a dead master rather than a partitioned one.
        master_srv.stop()

        check("surviving replica noticed the master is gone",
              wait_until(lambda: not link_up(r1), timeout=10.0))

        # Site 6: the promotion gate. This is the ONLY case anyone promotes in,
        # and gating on repl_state (the link phase) instead of replica_mode (the
        # role) makes the command a silent no-op precisely here.
        cmd(r1, "REPLICAOF", "NO", "ONE")
        d = info(r1, "replication")
        check("[REG] REPLICAOF NO ONE promotes a replica whose master is DOWN",
              d.get("role") == "master",
              "gated on repl_state instead of replica_mode: repl_link_lost() "
              "sets repl_state NONE and keeps replica_mode, so this no-ops")
        check("...and it is writable", expect_error(r1, "SET", "k", "v") is None)

        # Site 0b / 3: the identity handover.
        check("[REG] promotion RETIRES the old replid into master_replid2",
              d.get("master_replid2") == old_replid,
              f"master_replid2={d.get('master_replid2')} old={old_replid} — 40 "
              f"zeros means repl_shift_id() was never called (repl_new_id() "
              f"discards the history instead)")
        check("second_repl_offset marks the handover point",
              d.get("second_repl_offset", "-1") != "-1"
              and int(d["second_repl_offset"]) > 0,
              d.get("second_repl_offset"))
        check("a new replid was still minted",
              d.get("master_replid") != old_replid)

        # The payoff. Sites 7 and 9 together: the sibling must NAME the old
        # history (9, replica side) and the promoted instance must HONOUR it
        # (7, master side). Both resync paths leave r2 with correct data, so
        # only the counters can tell them apart.
        full0, ok0, _ = counters(r1)
        cmd(r2, "REPLICAOF", "127.0.0.1", r1_port)
        check("sibling re-attached to the promoted instance",
              wait_until(lambda: link_up(r2), timeout=15.0),
              f"INFO replication = {info(r2, 'replication')}")
        full1, ok1, _ = counters(r1)
        check("[REG] the sibling PARTIAL-resyncs off the retired history",
              ok1 == ok0 + 1 and full1 == full0,
              f"sync_full {full0}->{full1} sync_partial_ok {ok0}->{ok1} — a full "
              f"resync here means repl_id2 was not offered (repl_start's "
              f"have_history still requires the address to match) or not "
              f"honoured (do_psync's id_ok)")

        cmd(r1, "SET", "post_failover", "yes")
        check("the new master streams to the sibling",
              wait_until(lambda: cmd(r2, "GET", "post_failover") == "yes"))

        # Site 10. A promoted master answers +CONTINUE under its NEW replid, and
        # the sibling has to adopt it off that line. Not adopting it is invisible
        # exactly once — the data is right, the counters say partial — and then
        # every reconnect after this one full-resyncs, because the name it keeps
        # quoting has expired past second_repl_offset. The storm V10.6a exists to
        # prevent comes back on the second reconnect, not the first.
        new_id = info(r1, "replication").get("master_replid")
        check("[REG] the sibling adopted the promoted master's replid",
              wait_until(lambda: info(r2, "replication").get("master_replid")
                         == new_id),
              f"r2={info(r2, 'replication').get('master_replid')} r1={new_id} — "
              f"the +CONTINUE branch never read the id off the line it arrived on")

        # ...and prove it costs nothing on the NEXT reconnect either. Bounce the
        # link by winding the sibling's own repl-timeout down: no proxy stands
        # between these two, and REPLICAOF NO ONE would forfeit the history the
        # test is about.
        if set_if_present(r2, "repl-timeout", 1):
            set_if_present(r1, "repl-ping-replica-period", 60)  # real silence
            full2, ok2, _ = counters(r1)
            # Watch the master's counters, not the replica's link state. The
            # re-dial is faster than any poll interval, so "down" is a state
            # this test can miss completely while the reconnect it was waiting
            # for has already come and gone; a resync the master SERVED is a
            # fact that stays put. A 1s timeout also flaps more than once in
            # the window, which is fine — the count is timing.
            check("the sibling's link bounced at least once",
                  wait_until(lambda: sum(counters(r1)[:2]) > full2 + ok2,
                             timeout=15.0),
                  f"sync_full/ok still {counters(r1)[:2]} — repl-timeout 1 never "
                  f"dropped a link that had been silent for longer than that")
            set_if_present(r2, "repl-timeout", 60)
            check("the sibling re-dialled on its own",
                  wait_until(lambda: link_up(r2), timeout=25.0),
                  f"INFO replication = {info(r2, 'replication')}")
            full3, ok3, _ = counters(r1)
            # However many times it flapped, not one of them may have cost an
            # image. The ZERO is the contract; the other number is weather.
            check("[REG] every reconnect after the promotion is still partial",
                  full3 == full2 and ok3 > ok2,
                  f"sync_full {full2}->{full3} sync_partial_ok {ok2}->{ok3} — one "
                  f"partial and then a full on every reconnect after it is the "
                  f"signature of a replica still quoting the dead master's replid")
            set_if_present(r1, "repl-ping-replica-period", 10)
        else:
            skip("second reconnect stays partial",
                 "no repl-timeout directive to bounce the link with")
    finally:
        for s in (r1, r2):
            try:
                s.close()
            except OSError:
                pass
    # deliberately outside the finally: `return` there would swallow a failure
    return r2_srv


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.path.join(repo_root(), "build", "server"))
    ap.add_argument("--master-port", type=int, default=12404)
    ap.add_argument("--replica-port", type=int, default=12405)
    ap.add_argument("--proxy-port", type=int, default=12406)
    ap.add_argument("--replica2-port", type=int, default=12407,
                    help="second replica, for the V10.6a failover phase")
    ap.add_argument("--failover-master-port", type=int, default=12408,
                    help="V10.6d runs on its own pair: a handover swaps roles")
    ap.add_argument("--failover-replica-port", type=int, default=12409)
    ap.add_argument("--failover-proxy-port", type=int, default=12410)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    server_bin = os.path.abspath(a.server)
    if not os.path.exists(server_bin):
        print(f"{RED}server binary not found: {server_bin}{RESET}")
        return 1

    workdir = tempfile.mkdtemp(prefix="myred-repl-")
    master_dir = os.path.join(workdir, "master")
    replica_dir = os.path.join(workdir, "replica")
    os.makedirs(master_dir)
    os.makedirs(replica_dir)

    master_conf = os.path.join(master_dir, "master.conf")
    replica_conf = os.path.join(replica_dir, "replica.conf")
    write_conf(master_conf, [
        f"port {a.master_port}",
        "appendonly no",
        # small on purpose: makes "gap larger than the backlog" cheap to reach
        f"repl-backlog-size {BACKLOG_BYTES}",
    ])
    write_conf(replica_conf, [
        f"port {a.replica_port}",
        "appendonly no",
        f"replicaof 127.0.0.1 {a.proxy_port}",
    ])

    print(f"workdir: {workdir}")
    print(f"master :{a.master_port}   proxy :{a.proxy_port}   replica :{a.replica_port}")

    master_srv = replica_srv = replica2_srv = None
    extra_srvs = []      # phases that spawn their own instances register here,
                         # so the evidence dump below still finds them if the
                         # phase dies partway through
    proxy = Proxy(a.proxy_port, a.master_port)
    master = replica = None
    try:
        master_srv = Server(server_bin, master_dir, master_conf, "master", a.master_port)
        master = connect(a.master_port)

        # seed BEFORE the replica exists, so the resync image has something in it
        for k, v in (("pre1", "a"), ("pre2", "b"), ("pre3", "c")):
            cmd(master, "SET", k, v)

        has_v104 = "sync_full" in info(master, "stats")
        if not has_v104:
            print(f"{YELLOW}note: INFO has no sync_* counters — V10.4a not applied, "
                  f"skipping the partial-resync phases{RESET}")

        # Capability probes, not version strings: the suite has to stay useful
        # while a milestone is half-applied, and it should say which half.
        has_v106a = "master_replid2" in info(master, "replication")
        has_v106b = has_directive(master, "repl-timeout")
        has_v106c = has_directive(master, "min-replicas-to-write")
        # FAILOVER ABORT with nothing running is a rejection either way; only the
        # TEXT of it says whether the command exists at all.
        fo_err = expect_error(master, "FAILOVER", "ABORT")
        has_v106d = fo_err is not None and "unknown command" not in fo_err
        for ok, tag, why in (
            (has_v106a, "V10.6a", "INFO has no master_replid2"),
            (has_v106b, "V10.6b", "CONFIG has no repl-timeout"),
            (has_v106c, "V10.6c", "CONFIG has no min-replicas-to-write"),
            (has_v106d, "V10.6d", "no FAILOVER command"),
        ):
            if not ok:
                print(f"{YELLOW}note: {tag} not applied ({why}){RESET}")

        proxy.start()
        replica_srv = Server(server_bin, replica_dir, replica_conf,
                             "replica", a.replica_port)
        replica = connect(a.replica_port)

        phase_full_resync(master, replica, replica_srv)
        phase_streaming(master, replica)
        phase_readonly(master, replica)
        phase_link_loss(replica, proxy)

        if has_v104:
            phase_partial_resync(master, replica, proxy, a.proxy_port)
            phase_gap_too_large(master, replica, proxy, a.proxy_port)
            phase_auto_reconnect(master, replica, proxy)
        else:
            proxy.start()
            cmd(replica, "REPLICAOF", "127.0.0.1", a.proxy_port)
            wait_until(lambda: link_up(replica))

        phase_wait(master, a.master_port)

        if has_v106c:
            phase_min_replicas(master, replica, proxy)
        else:
            print("\n-- V10.6c min-replicas-to-write --")
            skip("durability floor",
                 "no min-replicas-to-write directive in this binary")

        if has_v106b:
            phase_repl_timeout_config(master)
            phase_idle_keepalive(master, replica, replica_srv)
            phase_wedged_master(master, replica, master_srv, replica_srv, proxy)
        else:
            print("\n-- V10.6b repl-timeout --")
            skip("wedged-link detection",
                 "no repl-timeout directive in this binary")

        phase_promotion(master, replica, replica_conf, a.proxy_port)

        replica.close()
        replica = None
        replica_srv.stop()
        replica_srv = phase_restart(server_bin, replica_dir, replica_conf,
                                    a.replica_port, master)

        if has_v106d:
            phase_failover(server_bin, workdir, extra_srvs,
                           a.failover_master_port, a.failover_replica_port,
                           a.failover_proxy_port)
        else:
            print("\n-- V10.6d coordinated FAILOVER --")
            skip("coordinated handover", "no FAILOVER command in this binary")

        # LAST: it stops the master on purpose, so nothing may run after it.
        if has_v106a:
            replica2_srv = phase_promotion_history(
                server_bin, workdir, master_srv, master,
                a.master_port, a.replica_port, a.replica2_port, proxy)
            master = None      # phase_promotion_history stopped the master
        else:
            print("\n-- V10.6a promotion keeps the history --")
            skip("sibling partial-resync after failover",
                 "INFO has no master_replid2 — V10.6a not applied")
    except Exception as e:
        check("unexpected error (workdir kept)", False, f"{type(e).__name__}: {e}")
    finally:
        for s in (master, replica):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        proxy.stop()
        failed = sys.modules["myred_testlib"].FAIL
        if failed:
            # evidence rule: a failure without the server's own words is unusable
            for srv, tag in ([(master_srv, "master"), (replica_srv, "replica"),
                              (replica2_srv, "replica2")] + extra_srvs):
                if srv is None:
                    continue
                tail = srv.stderr_text().strip().splitlines()[-12:]
                print(f"\n{YELLOW}{tag} stderr tail ({srv.stderr_path}){RESET}")
                for line in tail:
                    print("   " + line)
        for srv in ([replica2_srv, replica_srv, master_srv]
                    + [s for s, _ in extra_srvs]):
            if srv is not None:
                srv.stop()
        if a.keep or failed:
            print(f"\n{YELLOW}workdir kept for inspection: {workdir}{RESET}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    rc = summary()
    if SKIPPED:
        print(f"{YELLOW}{SKIPPED} phase(s) skipped: the binary predates them{RESET}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
