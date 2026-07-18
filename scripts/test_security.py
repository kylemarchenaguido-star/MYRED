#!/usr/bin/env python3
"""
Security suite for MYRED (V9.6.5 testing debt).

Covers, in order:
  1. Control-plane category gating (V9.5.1): a +@read +@write user can touch
     data but every admin/dangerous command (CONFIG, ACL, KEYS, MEMORY,
     FLUSHALL-alias) answers NOPERM — read/write grants never reach the
     control plane.
  2. rename-command: the canonical name is gone, the alias works, and a
     disabled command ('' target) is unreachable under both names.
  3. Audit log: auth_success / auth_fail / acl_change / acl_deny events are
     written, and NO plaintext password ever appears in the log (redaction).
  4. Precise key ACLs: ~pattern users, including the SMOVE key resolver
     (source AND destination must match).
  5. ACL CAT: reply is a well-formed RESP array with exactly the 8 categories.
  6. Config round-trip: CONFIG REWRITE, restart, users (including a
     passwordless one) survive with identical permissions.
  7. (--destructive) protocol abuse: oversized inline line, absurd multibulk
     count, garbage header — each may kill its own connection but the server
     must keep serving new ones.

Runs its own server on a private port in a temp dir; safe alongside a real
server on 1234.

Usage:
    python3 scripts/test_security.py [--server build/server]
                                     [--port 12402] [--keep] [--destructive]
"""

import argparse
import os
import shutil
import socket
import sys
import tempfile
import time

from myred_testlib import (GREEN, RED, YELLOW, RESET, TIMEOUT, Server, check,
                           cmd, connect, expect_error, repo_root, summary)

ADMIN_PW = "sec-admin-pass"
LIMITED_PW = "sec-limited-pass"
KEYED_PW = "sec-keyed-pass"
SMOVER_PW = "sec-smover-pass"
WRONG_PW = "sec-wrong-pass-attempt"

ALL_SECRETS = [ADMIN_PW, LIMITED_PW, KEYED_PW, SMOVER_PW, WRONG_PW]


def denied(sock, *args):
    """True iff the command is refused with NOPERM."""
    e = expect_error(sock, *args)
    return e is not None and "NOPERM" in e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.path.join(repo_root(), "build", "server"))
    ap.add_argument("--port", type=int, default=12402)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--destructive", action="store_true",
                    help="also run the protocol-abuse phase")
    a = ap.parse_args()
    server_bin = os.path.abspath(a.server)
    port = a.port

    if not os.path.exists(server_bin):
        print(f"{RED}server binary not found: {server_bin}{RESET}")
        return 1

    workdir = tempfile.mkdtemp(prefix="myred-security-")
    conf = os.path.join(workdir, "test.conf")
    with open(conf, "w") as f:
        f.write(f'port {port}\n'
                f'requirepass "{ADMIN_PW}"\n'
                f'auditlog "audit.log"\n'
                f'appendonly no\n'
                f'dbfilename dump.rdb\n'
                f'rename-command flushall wipeall\n'
                f'rename-command object ""\n')
    audit_path = os.path.join(workdir, "audit.log")
    print(f"workdir: {workdir}")
    srv = None
    try:
        srv = Server(server_bin, workdir, conf, "main", port)
        admin = connect(port, ADMIN_PW)

        # --- create the test users -----------------------------------------
        check("setuser limited (+@read +@write)",
              cmd(admin, "ACL", "SETUSER", "limited", "on", f">{LIMITED_PW}",
                  "~*", "+@read", "+@write") == "OK")
        check("setuser keyed (~data:*)",
              cmd(admin, "ACL", "SETUSER", "keyed", "on", f">{KEYED_PW}",
                  "~data:*", "+@read", "+@write") == "OK")
        check("setuser smover (~src:* ~dst:*)",
              cmd(admin, "ACL", "SETUSER", "smover", "on", f">{SMOVER_PW}",
                  "~src:*", "~dst:*", "+@read", "+@write") == "OK")
        check("setuser ghost (passwordless, survives round-trip)",
              cmd(admin, "ACL", "SETUSER", "ghost", "on", "~*", "+@read") == "OK")
        cmd(admin, "SET", "data:1", "d1")
        cmd(admin, "SET", "other:1", "o1")
        cmd(admin, "SADD", "src:s", "m")
        cmd(admin, "SADD", "src:s2", "m2")

        # --- 1. control-plane category gating -------------------------------
        print("\n[1] control-plane gating: +@read +@write must not reach admin")
        lim = connect(port, LIMITED_PW, user="limited")
        check("limited: GET works", cmd(lim, "GET", "data:1") == "d1")
        check("limited: SET works", cmd(lim, "SET", "lim:k", "v") == "OK")
        check("limited: CONFIG GET denied", denied(lim, "CONFIG", "GET", "maxmemory"))
        check("limited: ACL WHOAMI denied", denied(lim, "ACL", "WHOAMI"))
        check("limited: KEYS denied", denied(lim, "KEYS"))
        check("limited: MEMORY denied", denied(lim, "MEMORY", "USAGE", "data:1"))
        check("limited: flushall-alias denied", denied(lim, "wipeall"))
        lim.close()

        # --- 2. renamed / disabled commands ---------------------------------
        print("\n[2] rename-command: canonical gone, alias works, '' disables")
        check("canonical FLUSHALL is unknown",
              (expect_error(admin, "FLUSHALL") or "").startswith("ERR"))
        check("disabled OBJECT is unknown",
              (expect_error(admin, "OBJECT", "ENCODING", "data:1") or "")
              .startswith("ERR"))
        cmd(admin, "SET", "wipe:k", "x")
        check("alias wipeall works for admin", cmd(admin, "wipeall") == "OK")
        check("wipeall really flushed", cmd(admin, "EXISTS", "wipe:k") == 0)
        # re-seed what later phases need
        cmd(admin, "SET", "data:1", "d1")
        cmd(admin, "SET", "other:1", "o1")
        cmd(admin, "SADD", "src:s", "m")
        cmd(admin, "SADD", "src:s2", "m2")

        # --- 3. audit log + redaction ---------------------------------------
        print("\n[3] audit log: events present, secrets absent")
        anon = connect(port)
        bad = expect_error(anon, "AUTH", WRONG_PW)
        anon.close()
        check("wrong password rejected", bad is not None)
        time.sleep(0.3)  # async auth completion writes the event
        with open(audit_path, "r", errors="replace") as f:
            log = f.read()
        check("audit has auth_success", "event=auth_success" in log)
        check("audit has auth_fail", "event=auth_fail" in log)
        check("audit has acl_change setuser", "sub=setuser" in log)
        check("audit has acl_deny", "event=acl_deny" in log)
        leaked = [p for p in ALL_SECRETS if p in log]
        check("no plaintext password in audit log", not leaked,
              f"leaked: {leaked}")

        # --- 4. precise key ACLs --------------------------------------------
        print("\n[4] key-pattern ACLs, incl. the SMOVE source+dest resolver")
        kd = connect(port, KEYED_PW, user="keyed")
        check("keyed: GET data:* allowed", cmd(kd, "GET", "data:1") == "d1")
        check("keyed: GET other:* denied", denied(kd, "GET", "other:1"))
        check("keyed: SET outside pattern denied", denied(kd, "SET", "other:2", "x"))
        kd.close()
        sm = connect(port, SMOVER_PW, user="smover")
        check("smover: SMOVE src->dst allowed",
              cmd(sm, "SMOVE", "src:s", "dst:s", "m") == 1)
        check("smover: SMOVE to un-granted dest denied",
              denied(sm, "SMOVE", "src:s2", "forbidden:d", "m2"))
        sm.close()

        # --- 5. ACL CAT framing ---------------------------------------------
        print("\n[5] ACL CAT is a well-formed array of the 8 categories")
        cats = cmd(admin, "ACL", "CAT")
        check("ACL CAT returns a list", isinstance(cats, list))
        check("ACL CAT lists exactly the 8 categories",
              sorted(cats or []) == sorted(["read", "write", "keyspace", "admin",
                                            "dangerous", "fast", "slow",
                                            "connection"]),
              f"got {cats}")

        # --- 6. config round-trip across a restart --------------------------
        print("\n[6] CONFIG REWRITE -> restart -> users and rules survive")
        check("CONFIG REWRITE ok", cmd(admin, "CONFIG", "REWRITE") == "OK")
        with open(conf) as f:
            newconf = f.read()
        check("rewritten conf keeps the port", f"port {port}" in newconf)
        check("rewritten conf keeps the users", "user limited" in newconf)
        admin.close()
        if f"port {port}" not in newconf:
            print(f"{RED}rewritten conf lost the port — skipping restart "
                  f"phase to avoid binding the default port{RESET}")
        else:
            srv.stop()
            srv = Server(server_bin, workdir, conf, "rt", port)
            admin = connect(port, ADMIN_PW)
            check("admin auth survives restart", True)
            check("ghost user survived round-trip",
                  cmd(admin, "ACL", "GETUSER", "ghost") is not None)
            lim = connect(port, LIMITED_PW, user="limited")
            check("limited auth survives restart",
                  cmd(lim, "GET", "data:1") == "d1")
            check("limited still denied CONFIG after restart",
                  denied(lim, "CONFIG", "GET", "maxmemory"))
            lim.close()
            admin.close()

        # --- 7. destructive protocol abuse ----------------------------------
        if a.destructive:
            print("\n[7] (destructive) protocol abuse: server must survive")

            def abuse(name, payload):
                raw = socket.socket()
                raw.settimeout(TIMEOUT)
                raw.connect(("127.0.0.1", port))
                try:
                    raw.sendall(payload)
                    raw.recv(256)      # error reply or close — both fine
                except OSError:
                    pass
                finally:
                    raw.close()
                try:
                    probe = connect(port, ADMIN_PW)
                    ok = cmd(probe, "PING") == "PONG"
                    probe.close()
                except Exception as e:
                    ok = False
                check(f"server survives {name}", ok)

            abuse("absurd multibulk count", b"*99999999\r\n")
            abuse("garbage array header", b"*abc\r\n")
            abuse("oversized inline line", b"x" * (2 * 1024 * 1024))
            check("server process still alive", srv.alive())

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
