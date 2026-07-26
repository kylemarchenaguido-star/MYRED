#!/usr/bin/env python3
"""
Pub/Sub + keyspace-notification suite for MYRED (milestone V8).

Covers, in order:
  1. V8.1 core: SUBSCRIBE/UNSUBSCRIBE/PUBLISH — confirmation shapes, running
     counts, cross-connection delivery, subscribe-mode gating, and teardown
     (a disconnected subscriber must leave no dangling Conn* in the registry).
  2. V8.2a patterns: PSUBSCRIBE/PUNSUBSCRIBE, `pmessage` shape, and the rule
     that one PUBLISH fires the exact-channel and pattern paths independently.
     Counts are the conn TOTAL (channels + patterns).
  3. V8.2b channel ACL: &pattern / allchannels / resetchannels, the literal-vs-
     glob asymmetry (PSUBSCRIBE cannot widen a grant), and CONFIG REWRITE
     round-trip across a restart.
  4. V8.3 keyspace notifications: K/E channel semantics, per-class filtering,
     the expired hook, and (with --evict) the eviction hook.

Every regression test marked [REG] pins a bug that actually shipped during
development and was caught by grep-verify, not by a passing smoke test.

Runs its own server on a private port in a temp dir; safe alongside a real
server on 1234.

Usage:
    python3 scripts/test_pubsub.py [--server build/server]
                                   [--port 12403] [--keep] [--evict]
"""

import argparse
import os
import shutil
import socket
import sys
import tempfile
import time

from myred_testlib import (RED, YELLOW, RESET, Server, check, cmd, connect,
                           enc, expect_error, recv, repo_root, summary)

ADMIN_PW = "pubsub-admin-pass"
CHAN_PW = "pubsub-chan-pass"
ALLCH_PW = "pubsub-allch-pass"

# how long to wait for an async push before declaring "nothing arrived"
PUSH_WAIT = 1.0
# generous wait for event-loop driven events (active expiry runs on a timer tick)
SLOW_WAIT = 2.5


# ------------------------------------------------------------ push plumbing

def send(sock, *args):
    """Fire a command without reading its reply."""
    sock.sendall(enc(*args))


def read_push(sock, timeout=PUSH_WAIT):
    """Read one pushed reply, or None if nothing arrives before `timeout`.

    On timeout the stream is still clean: the timeout always fires on the very
    first byte of a frame, never mid-frame.
    """
    old = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        return recv(sock)
    except (socket.timeout, TimeoutError, OSError):
        return None
    finally:
        try:
            sock.settimeout(old)
        except OSError:
            pass


def read_n(sock, n, timeout=PUSH_WAIT):
    """Read exactly n pushed replies (missing ones come back as None)."""
    return [read_push(sock, timeout) for _ in range(n)]


def drain(sock, timeout=PUSH_WAIT, limit=200):
    """Collect every push currently pending, stopping at the first silence."""
    out = []
    while len(out) < limit:
        p = read_push(sock, timeout)
        if p is None:
            break
        out.append(p)
    return out


def as_delivery(push):
    """Normalize a message/pmessage push to (channel, payload); else None."""
    if not isinstance(push, list):
        return None
    if len(push) == 3 and push[0] == "message":
        return (push[1], push[2])
    if len(push) == 4 and push[0] == "pmessage":
        return (push[2], push[3])
    return None


def deliveries(pushes):
    return {d for d in (as_delivery(p) for p in pushes) if d is not None}


def subscribe(sock, kind, *names):
    """Send (P)SUBSCRIBE/(P)UNSUBSCRIBE and read one confirmation per name."""
    send(sock, kind, *names)
    return read_n(sock, len(names))


def confirm_ok(reply, kind, name, count):
    return (isinstance(reply, list) and len(reply) == 3
            and reply[0] == kind and reply[1] == name and reply[2] == count)


# ------------------------------------------------------------------- phases

def phase_core(port):
    print("\n[1] V8.1 core: SUBSCRIBE / UNSUBSCRIBE / PUBLISH")
    sub = connect(port, ADMIN_PW)
    pub = connect(port, ADMIN_PW)

    r = subscribe(sub, "SUBSCRIBE", "news")
    check("SUBSCRIBE confirmation shape", confirm_ok(r[0], "subscribe", "news", 1),
          f"got {r[0]!r}")

    r = subscribe(sub, "SUBSCRIBE", "sports")
    check("second SUBSCRIBE bumps the running count",
          confirm_ok(r[0], "subscribe", "sports", 2), f"got {r[0]!r}")

    check("PUBLISH reports 1 receiver", cmd(pub, "PUBLISH", "news", "hello") == 1)
    check("subscriber receives the message",
          as_delivery(read_push(sub)) == ("news", "hello"))

    check("PUBLISH to a channel nobody holds reports 0",
          cmd(pub, "PUBLISH", "nobody-here", "x") == 0)

    # --- subscribe-mode gating (RESP2 rule)
    e = expect_error(sub, "GET", "foo")
    check("GET is refused while subscribed", e is not None and "subscribe mode" in e,
          f"got {e!r}")
    check("PING is still allowed while subscribed", cmd(sub, "PING") == "PONG")

    # [REG] do_unsubscribe once looped from i=0 and emitted a phantom
    # confirmation for a channel literally named "unsubscribe".
    r = subscribe(sub, "UNSUBSCRIBE", "news")
    check("[REG] UNSUBSCRIBE <chan> confirms only that channel",
          confirm_ok(r[0], "unsubscribe", "news", 1), f"got {r[0]!r}")

    # [REG] UNSUBSCRIBE min_args was 2, making the no-arg branch dead code.
    send(sub, "UNSUBSCRIBE")
    left = read_n(sub, 1)
    check("[REG] bare UNSUBSCRIBE drops the last channel and reaches 0",
          confirm_ok(left[0], "unsubscribe", "sports", 0), f"got {left[0]!r}")

    check("subscribe mode ends once the last subscription is gone",
          cmd(sub, "SET", "post-sub", "v") == "OK")

    # [REG] PUBLISH min_args was 2 while do_publish reads cmd[2] -> OOB read.
    e = expect_error(pub, "PUBLISH", "only-one-arg")
    check("[REG] PUBLISH with no message is an arity error, not a crash",
          e is not None and "arguments" in e, f"got {e!r}")
    check("server survives the arity probe", cmd(pub, "PING") == "PONG")

    # --- teardown: a dead subscriber must be unlinked from every channel
    ghost = connect(port, ADMIN_PW)
    subscribe(ghost, "SUBSCRIBE", "haunted")
    check("PUBLISH reaches the soon-to-die subscriber",
          cmd(pub, "PUBLISH", "haunted", "boo") == 1)
    ghost.close()
    time.sleep(0.2)
    check("PUBLISH after subscriber disconnect reports 0 (no dangling Conn*)",
          cmd(pub, "PUBLISH", "haunted", "boo") == 0)
    check("server alive after the teardown path", cmd(pub, "PING") == "PONG")

    sub.close()
    pub.close()


def phase_patterns(port):
    print("\n[2] V8.2a pattern subscriptions")
    psub = connect(port, ADMIN_PW)
    esub = connect(port, ADMIN_PW)
    pub = connect(port, ADMIN_PW)

    r = subscribe(psub, "PSUBSCRIBE", "news.*")
    check("PSUBSCRIBE confirmation shape", confirm_ok(r[0], "psubscribe", "news.*", 1),
          f"got {r[0]!r}")

    r = subscribe(esub, "SUBSCRIBE", "news.sports")
    check("exact SUBSCRIBE on a channel the pattern also covers",
          confirm_ok(r[0], "subscribe", "news.sports", 1), f"got {r[0]!r}")

    # the headline V8.2 property: one PUBLISH, two independent deliveries
    check("one PUBLISH counts both the exact and the pattern subscriber",
          cmd(pub, "PUBLISH", "news.sports", "goal") == 2)

    p = read_push(psub)
    check("pattern subscriber gets a 4-element pmessage carrying the pattern",
          isinstance(p, list) and p == ["pmessage", "news.*", "news.sports", "goal"],
          f"got {p!r}")
    e = read_push(esub)
    check("exact subscriber independently gets a 3-element message",
          isinstance(e, list) and e == ["message", "news.sports", "goal"], f"got {e!r}")

    check("a channel outside the pattern reaches nobody",
          cmd(pub, "PUBLISH", "weather.today", "rain") == 0)
    check("...and no stray push arrives", read_push(psub, 0.4) is None)

    # [REG] the confirmation count is channels + patterns, not channels alone.
    r = subscribe(psub, "SUBSCRIBE", "extra")
    check("[REG] SUBSCRIBE count includes patterns already held",
          confirm_ok(r[0], "subscribe", "extra", 2), f"got {r[0]!r}")
    r = subscribe(psub, "UNSUBSCRIBE", "extra")
    check("[REG] UNSUBSCRIBE count still includes the held pattern",
          confirm_ok(r[0], "unsubscribe", "extra", 1), f"got {r[0]!r}")

    check("a pattern-only conn is still in subscribe mode",
          (lambda e: e is not None and "subscribe mode" in e)(
              expect_error(psub, "GET", "foo")))

    # [REG] do_punsubscribe built its snapshot from sub_patterns.begin() to
    # sub_channels.end() -- iterators into two different containers (UB).
    send(psub, "PUNSUBSCRIBE")
    r = read_n(psub, 1)
    check("[REG] bare PUNSUBSCRIBE drops the pattern and reaches 0",
          confirm_ok(r[0], "punsubscribe", "news.*", 0), f"got {r[0]!r}")
    check("pattern subscriber leaves subscribe mode",
          cmd(psub, "PING") == "PONG")

    psub.close()
    esub.close()
    pub.close()


def phase_channel_acl(port, admin):
    print("\n[3] V8.2b channel ACL: &pattern / allchannels / resetchannels")
    check("setuser chan (resetchannels &news.*)",
          cmd(admin, "ACL", "SETUSER", "chan", "on", f">{CHAN_PW}", "~*",
              "+@all", "resetchannels", "&news.*") == "OK")
    check("setuser allch (allchannels)",
          cmd(admin, "ACL", "SETUSER", "allch", "on", f">{ALLCH_PW}", "~*",
              "+@all", "allchannels") == "OK")

    c = connect(port, CHAN_PW, user="chan")
    r = subscribe(c, "SUBSCRIBE", "news.sports")
    check("granted channel: SUBSCRIBE news.sports allowed",
          confirm_ok(r[0], "subscribe", "news.sports", 1), f"got {r[0]!r}")
    subscribe(c, "UNSUBSCRIBE", "news.sports")

    e = expect_error(c, "SUBSCRIBE", "other")
    check("ungranted channel: SUBSCRIBE other denied", e is not None and "NOPERM" in e,
          f"got {e!r}")

    r = subscribe(c, "PSUBSCRIBE", "news.*")
    check("PSUBSCRIBE with a literally-granted pattern allowed",
          confirm_ok(r[0], "psubscribe", "news.*", 1), f"got {r[0]!r}")
    subscribe(c, "PUNSUBSCRIBE", "news.*")

    # the security-relevant case: a narrow grant must not be widenable
    e = expect_error(c, "PSUBSCRIBE", "*")
    check("PSUBSCRIBE '*' denied (cannot widen a &news.* grant)",
          e is not None and "NOPERM" in e, f"got {e!r}")

    check("PUBLISH to a granted channel allowed",
          cmd(c, "PUBLISH", "news.x", "hi") == 0)
    e = expect_error(c, "PUBLISH", "other", "hi")
    check("PUBLISH to an ungranted channel denied", e is not None and "NOPERM" in e,
          f"got {e!r}")

    a = connect(port, ALLCH_PW, user="allch")
    r = subscribe(a, "SUBSCRIBE", "anything.at.all")
    check("allchannels user reaches an arbitrary channel",
          confirm_ok(r[0], "subscribe", "anything.at.all", 1), f"got {r[0]!r}")
    a.close()
    c.close()

    # [REG] acl_format_user emitted "&*" with no leading space, fusing it onto
    # the previous token ("~*&*") so a reload parsed it as key-pattern "*&*"
    # and silently dropped both the allkeys and the allchannels grant.
    lines = cmd(admin, "ACL", "LIST")
    joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
    check("[REG] no token fused into '~*&*' in ACL LIST", "~*&*" not in joined,
          joined)
    check("allch line carries a standalone &*", " &*" in joined, joined)
    check("chan line carries &news.*", "&news.*" in joined, joined)


def phase_acl_roundtrip(port, admin, server_bin, workdir, conf):
    print("\n[4] V8.2b channel ACL survives CONFIG REWRITE + restart")
    check("CONFIG REWRITE", cmd(admin, "CONFIG", "REWRITE") == "OK")
    with open(conf) as f:
        text = f.read()
    check("rewritten config keeps &news.*", "&news.*" in text)
    check("[REG] rewritten config has no fused '~*&*' token", "~*&*" not in text)
    return text


def phase_notifications(port, admin, evict):
    print("\n[5] V8.3 keyspace notifications")
    check("CONFIG SET notify-keyspace-events KEA",
          cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "KEA") == "OK")

    listener = connect(port, ADMIN_PW)
    subscribe(listener, "PSUBSCRIBE", "__key*@0__:*")

    # K and E are independent: one SET must produce both forms
    cmd(admin, "SET", "foo", "bar")
    got = deliveries(drain(listener))
    check("SET emits the __keyspace__ form (payload = event name)",
          ("__keyspace@0__:foo", "set") in got, str(got))
    check("SET emits the __keyevent__ form (payload = key)",
          ("__keyevent@0__:set", "foo") in got, str(got))

    cmd(admin, "LPUSH", "mylist", "a")
    got = deliveries(drain(listener))
    check("LPUSH emits the list-class event",
          ("__keyevent@0__:lpush", "mylist") in got, str(got))

    cmd(admin, "PEXPIRE", "foo", "100000")
    got = deliveries(drain(listener))
    check("PEXPIRE emits the generic-class event",
          ("__keyevent@0__:pexpire", "foo") in got, str(got))

    cmd(admin, "DEL", "foo")
    got = deliveries(drain(listener))
    check("DEL emits the generic-class event",
          ("__keyevent@0__:del", "foo") in got, str(got))

    # a command that changes nothing must stay silent (dirty-counter gate)
    cmd(admin, "DEL", "definitely-not-here")
    check("a no-op write emits nothing", read_push(listener, 0.4) is None)

    # --- per-class filtering: 'Ex' = keyevent + expired only
    check("CONFIG SET notify-keyspace-events Ex",
          cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "Ex") == "OK")
    cmd(admin, "SET", "quiet", "v")
    check("with 'Ex', a string write emits nothing", read_push(listener, 0.4) is None)

    # TTL must outlast the "nothing arrives" window below, or the expired event
    # races into it and both checks read the wrong thing.
    cmd(admin, "PSETEX", "ttlkey", "1200", "v")
    check("with 'Ex', PSETEX itself emits nothing", read_push(listener, 0.4) is None)
    got = deliveries(drain(listener, SLOW_WAIT))
    check("expired hook fires on TTL expiry",
          ("__keyevent@0__:expired", "ttlkey") in got, str(got))
    check("with 'Ex' the __keyspace__ form is suppressed",
          not any(c.startswith("__keyspace@") for c, _ in got), str(got))

    # --- fully off
    check("CONFIG SET notify-keyspace-events '' (off)",
          cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "") == "OK")
    cmd(admin, "SET", "silent", "v")
    check("with notifications off, nothing is emitted",
          read_push(listener, 0.4) is None)

    if evict:
        print("\n[5b] V8.3 eviction hook (--evict)")
        # 'Ee' keeps the stream clean: only evictions notify, not the writes
        cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "Ee")
        cmd(admin, "CONFIG", "SET", "maxmemory-policy", "allkeys-random")
        cmd(admin, "CONFIG", "SET", "maxmemory", "2mb")
        blob = "x" * 2048
        for i in range(1200):
            cmd(admin, "SET", f"evict:{i}", blob)
        got = deliveries(drain(listener, SLOW_WAIT, limit=2000))
        evicted = [k for c, k in got if c == "__keyevent@0__:evicted"]
        check("eviction hook fires under maxmemory pressure",
              len(evicted) > 0, f"{len(got)} events, none evicted")
        cmd(admin, "CONFIG", "SET", "maxmemory", "0")
        cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "")

    listener.close()

    # --- config surface
    v = cmd(admin, "CONFIG", "GET", "notify-keyspace-events")
    check("CONFIG GET returns the directive", isinstance(v, list) and len(v) == 2,
          f"got {v!r}")

    cmd(admin, "CONFIG", "SET", "notify-keyspace-events", "KEA")
    check("CONFIG REWRITE with notifications on", cmd(admin, "CONFIG", "REWRITE") == "OK")


def phase_after_restart(port, conf):
    print("\n[6] state after restart (ACL + notify round-trip)")
    with open(conf) as f:
        text = f.read()
    check("rewritten config carries notify-keyspace-events", "notify-keyspace-events" in text,
          text)

    # [REG] config_rewrite once dropped requirepass entirely (regression from the
    # TLS plumbing commit): after a rewrite the server restarted passwordless,
    # so the old password answered WRONGPASS and every new conn auto-authed as
    # a nopass default user with +@all ~*.
    admin = None
    try:
        admin = connect(port, ADMIN_PW)
        check("[REG] requirepass survives CONFIG REWRITE + restart", True)
    except (RuntimeError, ConnectionError) as e:
        check("[REG] requirepass survives CONFIG REWRITE + restart", False, str(e))

    if admin is not None:
        v = cmd(admin, "CONFIG", "GET", "notify-keyspace-events")
        flags = v[1] if isinstance(v, list) and len(v) == 2 else ""
        check("notify flags survived the restart",
              set("KE").issubset(set(flags)) and ("A" in flags or "g" in flags),
              f"got {flags!r}")

        lines = cmd(admin, "ACL", "LIST")
        joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
        check("[REG] channel grants survived without token fusion", "~*&*" not in joined,
              joined)
        check("chan still has &news.*", "&news.*" in joined, joined)
    else:
        print(f"  {YELLOW}skipping admin-only checks (no authenticated admin){RESET}")

    # functional re-check: the restored grant must still be enforced
    c = connect(port, CHAN_PW, user="chan")
    r = subscribe(c, "SUBSCRIBE", "news.sports")
    check("restored grant still allows news.sports",
          confirm_ok(r[0], "subscribe", "news.sports", 1), f"got {r[0]!r}")
    subscribe(c, "UNSUBSCRIBE", "news.sports")
    e = expect_error(c, "SUBSCRIBE", "other")
    check("restored grant still denies other", e is not None and "NOPERM" in e, f"got {e!r}")
    c.close()

    a = connect(port, ALLCH_PW, user="allch")
    r = subscribe(a, "SUBSCRIBE", "literally.anything")
    check("[REG] allchannels user still unrestricted after restart",
          confirm_ok(r[0], "subscribe", "literally.anything", 1), f"got {r[0]!r}")
    a.close()
    if admin is not None:
        admin.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.path.join(repo_root(), "build", "server"))
    ap.add_argument("--port", type=int, default=12403)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evict", action="store_true",
                    help="also exercise the eviction hook (writes ~2.5MB)")
    a = ap.parse_args()
    server_bin = os.path.abspath(a.server)
    port = a.port

    if not os.path.exists(server_bin):
        print(f"{RED}server binary not found: {server_bin}{RESET}")
        return 1

    workdir = tempfile.mkdtemp(prefix="myred-pubsub-")
    conf = os.path.join(workdir, "test.conf")
    with open(conf, "w") as f:
        f.write(f'port {port}\n'
                f'requirepass "{ADMIN_PW}"\n'
                f'appendonly no\n'
                f'dbfilename dump.rdb\n')
    print(f"workdir: {workdir}")
    srv = None
    try:
        srv = Server(server_bin, workdir, conf, "main", port)
        admin = connect(port, ADMIN_PW)

        phase_core(port)
        phase_patterns(port)
        phase_channel_acl(port, admin)
        phase_acl_roundtrip(port, admin, server_bin, workdir, conf)
        phase_notifications(port, admin, a.evict)
        admin.close()

        # restart and re-verify everything that was supposed to persist
        srv.stop()
        srv = Server(server_bin, workdir, conf, "restart", port)
        phase_after_restart(port, conf)

        srv.stop()
        srv = None
    except Exception as e:
        check("unexpected error (workdir kept)", False, f"{type(e).__name__}: {e}")
    finally:
        if srv is not None:
            srv.stop()
        if a.keep or sys.modules['myred_testlib'].FAIL:
            print(f"\n{YELLOW}workdir kept for inspection: {workdir}{RESET}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    return summary()


if __name__ == "__main__":
    sys.exit(main())
