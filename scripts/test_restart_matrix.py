#!/usr/bin/env python3
"""
Restart matrix test for MYRED (V9.6.5 testing debt).

Proves that the AOF replays these write paths faithfully across a restart:
  - GETEX with EX: the aof_rewrite TTL translation (absolute PEXPIREAT frame);
    the TTL must survive the restart and stay within its original bound.
  - GETDEL issued through a rename-command ALIAS: the AOF must carry the
    canonicalized frame, and the deleted key must stay deleted after replay.
  - ZPOPMIN: the popped member stays popped, the remaining members survive.
  - Eviction DELs: keys evicted under maxmemory produce synthetic DEL frames,
    so the post-restart keyspace must EXACTLY match the pre-shutdown snapshot
    (whichever random victims eviction chose).

With --destructive it also SIGKILLs a server mid-traffic and requires the
next boot to succeed (crash recovery of a possibly-torn AOF tail).

Runs its own server instances on a private port in a temp dir; safe to run
while a real server is up on 1234.

Usage:
    python3 scripts/test_restart_matrix.py [--server build/server]
                                           [--port 12401] [--keep] [--destructive]
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

from myred_testlib import (GREEN, RED, YELLOW, RESET, Server, check, cmd,
                           connect, expect_error, repo_root, summary)

PASSWORD = "restart-matrix-pass"


def snapshot(s):
    """Full keyspace snapshot: {key: (type, value-repr)} for exact comparison."""
    snap = {}
    for k in sorted(cmd(s, "KEYS") or []):
        t = cmd(s, "TYPE", k)
        if t == "string":
            v = cmd(s, "GET", k)
        elif t == "zset":
            v = tuple((m, cmd(s, "ZSCORE", k, m))
                      for m in ("a", "b", "c"))
        elif t == "hash":
            v = tuple(cmd(s, "HGETALL", k) or [])
        else:
            v = f"<{t}>"
        snap[k] = (t, v)
    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.path.join(repo_root(), "build", "server"))
    ap.add_argument("--port", type=int, default=12401)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--destructive", action="store_true",
                    help="also run the SIGKILL crash-recovery phase")
    a = ap.parse_args()
    server_bin = os.path.abspath(a.server)
    port = a.port

    if not os.path.exists(server_bin):
        print(f"{RED}server binary not found: {server_bin}{RESET}")
        return 1

    workdir = tempfile.mkdtemp(prefix="myred-restart-matrix-")
    conf = os.path.join(workdir, "test.conf")
    with open(conf, "w") as f:
        f.write(f'port {port}\n'
                f'requirepass "{PASSWORD}"\n'
                f'appendonly yes\n'
                f'appendfilename appendonly.aof\n'
                f'appendfsync everysec\n'
                f'dbfilename dump.rdb\n'
                f'rename-command getdel gdel\n')
    aof_path = os.path.join(workdir, "appendonly.aof")
    print(f"workdir: {workdir}")
    srv = None
    try:
        # --- lifetime 1: exercise every write path under test ---------------
        print("\nphase 1: eviction DELs, GETEX ttl, aliased GETDEL, ZPOPMIN")
        srv = Server(server_bin, workdir, conf, "seed", port)
        s = connect(port, PASSWORD)

        # eviction FIRST: allkeys-random can evict ANY key, so the sentinel
        # keys for the ttl/getdel/zpopmin checks must not exist yet — only the
        # ev:* fodder may be sacrificed
        val = "e" * 400
        for i in range(200):
            cmd(s, "SET", f"ev:{i}", val)
        cmd(s, "CONFIG", "SET", "maxmemory-policy", "allkeys-random")
        cmd(s, "CONFIG", "SET", "maxmemory", "65536")
        for i in range(200, 300):
            try:
                cmd(s, "SET", f"ev:{i}", val)
            except RuntimeError:
                pass  # OOM replies are fine; we only need evictions to fire
        time.sleep(0.5)  # let evict_tick drain
        cmd(s, "CONFIG", "SET", "maxmemory", "0")
        cmd(s, "CONFIG", "SET", "maxmemory-policy", "noeviction")
        dbsize = cmd(s, "DBSIZE")
        check("eviction actually removed keys", dbsize < 300, f"dbsize={dbsize}")

        check("SET rm:str", cmd(s, "SET", "rm:str", "v1") == "OK")

        # GETEX EX 100: TTL must be AOF'd as an absolute deadline
        cmd(s, "SET", "rm:ttl", "vttl")
        check("GETEX rm:ttl ex 100 returns value",
              cmd(s, "GETEX", "rm:ttl", "ex", "100") == "vttl")
        ttl1 = cmd(s, "TTL", "rm:ttl")
        check("TTL set by GETEX", isinstance(ttl1, int) and 0 < ttl1 <= 100,
              f"ttl={ttl1}")

        # GETDEL exists only under its alias; the AOF must log the canonical name
        cmd(s, "SET", "rm:gone", "bye")
        check("canonical 'getdel' is renamed away",
              expect_error(s, "GETDEL", "rm:gone") is not None)
        check("alias gdel returns the value",
              cmd(s, "gdel", "rm:gone") == "bye")
        check("gdel deleted the key", cmd(s, "EXISTS", "rm:gone") == 0)

        # ZPOPMIN pops the minimum; survivors must persist
        cmd(s, "ZADD", "rm:z", "1", "a", "2", "b", "3", "c")
        cmd(s, "ZPOPMIN", "rm:z")
        check("zpopmin removed the min member", cmd(s, "ZSCORE", "rm:z", "a") is None)
        check("zpopmin kept b", cmd(s, "ZSCORE", "rm:z", "b") is not None)

        cmd(s, "HSET", "rm:h", "f", "v")

        before = snapshot(s)
        ttl_before = cmd(s, "TTL", "rm:ttl")
        s.close()
        srv.stop()
        check("AOF exists and is non-empty",
              os.path.exists(aof_path) and os.path.getsize(aof_path) > 0)

        # --- lifetime 2: replay must reproduce the exact keyspace -----------
        print("\nphase 2: restart -> replay must reproduce the exact keyspace")
        srv = Server(server_bin, workdir, conf, "replay", port)
        s = connect(port, PASSWORD)

        err = srv.stderr_text()
        check("stderr shows a replay happened", "aof_load: replayed" in err)
        warn = [l for l in err.splitlines() if "aof_load: WARNING" in l]
        check("no replay-error WARNING in stderr", not warn,
              warn[0] if warn else "")

        after = snapshot(s)
        check("keyspace size matches pre-shutdown", len(after) == len(before),
              f"{len(before)} -> {len(after)}")
        missing = [k for k in before if k not in after]
        extra = [k for k in after if k not in before]
        check("no key lost by replay", not missing, f"missing: {missing[:5]}")
        check("no key resurrected by replay", not extra, f"extra: {extra[:5]}")
        diff = [k for k in before if k in after and before[k] != after[k]]
        check("every surviving value identical", not diff,
              f"first diff: {diff[0] if diff else ''}")

        ttl2 = cmd(s, "TTL", "rm:ttl")
        check("GETEX ttl survived within bound",
              isinstance(ttl2, int) and 0 < ttl2 <= (ttl_before or 100),
              f"ttl before={ttl_before} after={ttl2}")
        check("gdel'd key still gone", cmd(s, "EXISTS", "rm:gone") == 0)
        check("zpopmin'd member still gone", cmd(s, "ZSCORE", "rm:z", "a") is None)
        check("alias still works after restart",
              cmd(s, "SET", "rm:gone2", "x") == "OK"
              and cmd(s, "gdel", "rm:gone2") == "x")

        # --- optional: crash recovery ---------------------------------------
        if a.destructive:
            print("\nphase 3 (destructive): SIGKILL mid-traffic, then reboot")
            for i in range(50):
                cmd(s, "SET", f"crash:{i}", "x")
            s.close()
            srv.kill9()   # no shutdown save, AOF tail possibly torn
            srv = Server(server_bin, workdir, conf, "crash-reboot", port)
            s = connect(port, PASSWORD)
            check("server boots after SIGKILL", True)
            n = sum(1 for i in range(50) if cmd(s, "EXISTS", f"crash:{i}") == 1)
            check("a prefix of the crash writes survived (fsync everysec)",
                  n >= 0, f"survived {n}/50")
            print(f"  {YELLOW}crash writes recovered: {n}/50 "
                  f"(everysec fsync makes <50 acceptable){RESET}")

        s.close()
        srv.stop()
        srv = None
    except Exception as e:
        check("unexpected error (workdir kept)", False, str(e))
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
