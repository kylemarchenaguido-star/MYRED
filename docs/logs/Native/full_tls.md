# MYRED stress test — 2026-08-15 22:39:20

**Run:** correctness + concurrency + managed-instance phases + stress over TLS (passwordless) → 127.0.0.1:1234

```
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + managed-instance phases + stress over TLS (passwordless) → 127.0.0.1:1234
═══════════════════════════════════════════════════════

-- Platform (read from the kernel) ---------------------
  Environment:  Native
  Kernel:       6.18.35-1-lts
  Product:      HP ProBook 450 G8 Notebook PC
  CPU:          11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
  Threads:      8 (usable by this process: 8)
  Memory:       16045708 kB  swap 0 kB
  Governor:     powersave  no_turbo=0
  Load average: 0.92 0.51 0.28 1/785 16570
  somaxconn:    4096   nofile=524288   tcp_ulp=espintcp mptcp
  note the CPU governor is 'powersave' — throughput will vary with how warm the machine is. 'performance' is the comparable setting.
  Build:        RELEASE  [build-rel/]
  Log:          docs/logs/Native/full_tls.md

✓ Spawned the primary instance on 127.0.0.1:12591  (/tmp/myred-primary-4_05gq4m)
  Transport:    TLS (cert not verified)
  Auth:         none
✓ Server is reachable

── String Commands: GET / SET / DEL ──────────────────
  ✓ set k1 hello → OK
  ✓ get k1 → hello
  ✓ set k1 world → OK
  ✓ get k1 → world
  ✓ get missing → nil
  ✓ del k1 → 1
  ✓ get after del → nil
  ✓ del missing → 0
  ✓ get empty → ''
  ✓ get long value

── String Numerics: INCR / DECR / INCRBY / DECRBY / INCRBYFLOAT 
  ✓ incr missing → 1
  ✓ incr again → 2
  ✓ incr again → 3
  ✓ decr → 2
  ✓ decr → 1
  ✓ incrby 10 → 11
  ✓ incrby -5 → 6
  ✓ decrby 3 → 3
  ✓ decrby -2 → 5
  ✓ incr from '100' → 101
  ✓ decr from '0' → -1
  ✓ incr on non-int value → error
  ✓ incrby on non-int value → error
  ✓ decrby on non-int value → error
  ✓ incr on set → WRONGTYPE
  ✓ incrby on set → WRONGTYPE
  ✓ incrbyfloat 0.1 → ~10.6 → 10.6
  ✓ incrbyfloat -3.5 → ~7.1 → 7.0999999999999996
  ✓ incrbyfloat 0 → ~7.1 → 7.0999999999999996
  ✓ incrbyfloat missing → 1.5 → 1.5
  ✓ incrbyfloat returns str → '7.0999999999999996'
  ✓ incrbyfloat inf → error
  ✓ incrbyfloat -inf → error
  ✓ incr at INT64_MAX → overflow
  ✓ decr at INT64_MIN → overflow

── String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL 
  ✓ setnx missing → 1
  ✓ get after setnx → hello
  ✓ setnx existing → 0
  ✓ value unchanged → hello
  ✓ setnx on set → 0 (key exists)
  ✓ setex 10s → OK
  ✓ get sv2 → exval
  ✓ setex ttl > 0
  ✓ setex ttl ≤ 10000
  ✓ setex ttl=0 → error
  ✓ setex ttl=-1 → error
  ✓ setex non-int → error
  ✓ psetex 5000ms → OK
  ✓ get sv3 → msval
  ✓ psetex ttl > 0
  ✓ psetex ttl ≤ 5000
  ✓ psetex ttl=0 → error
  ✓ psetex 200ms → OK
  ℹ  waiting 400ms for psetex key to expire...
  ✓ sv3 expired → nil
  ✓ getset returns old value
  ✓ get after getset → new
  ✓ getset missing → nil
  ✓ key created by getset → first
  ✓ getset on set → WRONGTYPE
  ✓ getex bare → value
  ✓ pttl unchanged → -1
  ✓ getex EX 5 → value
  ✓ getex EX set ttl > 0
  ✓ getex EX set ttl ≤ 5000
  ✓ getex PERSIST → value
  ✓ pttl after PERSIST → -1
  ✓ getex PX 3000 → value
  ✓ getex PX set ttl > 0
  ✓ getex PX set ttl ≤ 3000
  ✓ getex missing → nil
  ✓ getex bad opt → error
  ✓ getex EX 0 → error
  ✓ getex PX -1 → error
  ✓ getdel → value
  ✓ key gone after getdel
  ✓ getdel missing → nil
  ✓ getdel on set → WRONGTYPE

── String Multi-key: MSET / MGET / MSETNX ────────────
  ✓ mset 3 pairs → OK
  ✓ get mk1 → a
  ✓ get mk2 → b
  ✓ get mk3 → c
  ✓ mset overwrites → OK
  ✓ mk1 now x
  ✓ mk2 now y
  ✓ mset dup key → OK
  ✓ mk4 → second (last wins)
  ✓ mget 3 keys → list
  ✓ mget returns list → ['x', None, 'c']
  ✓ mget[0] → x
  ✓ mget[1] → nil (missing)
  ✓ mget[2] → c
  ✓ mget list has 3 elements
  ✓ mget[0] → x
  ✓ mget wrong-type → nil
  ✓ mget[2] → c
  ✓ mget 1 key → [x]
  ✓ msetnx all missing → 1
  ✓ mn1 → v1
  ✓ mn2 → v2
  ✓ msetnx one exists → 0
  ✓ mn1 unchanged → v1
  ✓ mn3 not created
  ✓ msetnx blocks on any type → 0
  ✓ mn3 still not set

── String Bulk/Range: APPEND / STRLEN / GETRANGE / SETRANGE 
  ✓ append missing → 5
  ✓ get br1 → hello
  ✓ append ` world` → 11
  ✓ get br1 → hello world
  ✓ append '' → 11
  ✓ append on set → WRONGTYPE
  ✓ strlen br1 → 11
  ✓ strlen missing → 0
  ✓ strlen empty str → 0
  ✓ strlen on set → WRONGTYPE
  ✓ getrange 0 4 → Hello
  ✓ getrange 7 11 → World
  ✓ getrange 0 -1 → full str
  ✓ getrange -6 -1 → World!
  ✓ getrange 0 0 → H
  ✓ getrange -1 -1 → !
  ✓ getrange 0 999 → full
  ✓ getrange 5 3 → ''
  ✓ getrange 99 100 → ''
  ✓ getrange -99 -99 → ''
  ✓ getrange missing → ''
  ✓ getrange on set → WRONGTYPE
  ✓ setrange offset 6 → 11
  ✓ get br3 → Hello Redis
  ✓ setrange offset 5 on empty → 8
  ✓ first 5 bytes are null-padded
  ✓ setrange result length
  ✓ setrange missing key → 5
  ✓ get br3 → hello
  ✓ setrange empty val offset=3 → 3
  ✓ strlen br3 → 3
  ✓ setrange offset -1 → error
  ✓ setrange on set → WRONGTYPE

── KEYS Command ──────────────────────────────────────
  ✓ keys returns list → ['kb', 'ka', 'kc']
  ✓ ka in keys
  ✓ kb in keys
  ✓ kc in keys

── TTL Commands: PEXPIRE / PTTL ──────────────────────
  ✓ pexpire ttlkey 5000 → 1
  ✓ pttl returns int → 5000
  ✓ pttl > 0
  ✓ pttl <= 5000
  ℹ  remaining TTL: 5000ms
  ✓ pttl no-ttl → -1
  ✓ pttl missing → -2
  ℹ  waiting 600ms for key to expire...
  ✓ expired key → nil
  ✓ pexpire -1 deletes key → 1
  ✓ pttl after delete → -2
  ✓ get after delete → nil

── Sorted Set: ZADD / ZSCORE / ZREM / ZRANK ──────────
  ✓ zadd n1 1.0 → 1
  ✓ zadd n2 2.0 → 1
  ✓ zadd n3 3.0 → 1
  ✓ zadd n4 0.5 → 1
  ✓ zadd n1 update → 0
  ✓ zscore n1 → 1.5 → 1.5
  ✓ zscore missing → nil
  ✓ zrank n4 → 0
  ✓ zrank n1 → 1
  ✓ zrank n2 → 2
  ✓ zrank n3 → 3
  ✓ zrank missing → nil
  ✓ zrem n1 → 1
  ✓ zscore after zrem → nil
  ✓ zrem missing → 0

── Sorted Set: ZQUERY / ZREVQUERY ────────────────────
  ✓ zquery returns list → ['a', '1', 'b', '2', 'c', '3', 'd', '4', 'e', '5']
  ✓ zquery all → 10 items
  ✓ zquery order correct
  ✓ zquery offset=1 → 8 items
  ✓ zquery limit=4 → 8 items (4 pairs)
  ✓ zquery from 3.0 → 6 items
  ✓ zquery no results → 0
  ✓ zrevquery returns list → ['e', '5', 'd', '4', 'c', '3', 'b', '2', 'a', '1']
  ✓ zrevquery all → 10 items
  ✓ zrevquery order correct
  ✓ zrevquery from 3.5 → 6 items

── Sorted Set: variadic ZADD / ZPOPMIN ───────────────
  ✓ zadd variadic 3 pairs → 3
  ✓ zadd variadic new+update → 1
  ✓ zscore a updated → 1.5 → 1.5
  ✓ zadd odd args → error
  ✓ zadd bad score → error
  ✓ zadd atomic: z not added
  ✓ zpopmin → list → ['a', '1.5']
  ✓ zpopmin min member → a
  ✓ zpopmin min score → 1.5 → 1.5
  ✓ zpopmin 2 → 4 items
  ✓ zpopmin 2 members → b,c
  ✓ zset emptied → key dropped
  ✓ zpopmin missing → []
  ✓ zpopmin wrong type → error

── Lists: LPUSH/RPUSH/LPOP/RPOP/LLEN/LINDEX/LRANGE ───
  ✓ rpush a b c → 3
  ✓ llen → 3
  ✓ lrange 0 -1 → [a,b,c]
  ✓ lpush x y → 5
  ✓ lrange after lpush
  ✓ lindex 0 → y
  ✓ lindex -1 → c
  ✓ lindex 2 → a
  ✓ lindex 100 → nil
  ✓ lpop → y
  ✓ rpop → c
  ✓ lrange after pops

── Lists: LSET / LINSERT ─────────────────────────────
  ✓ lset 1 B → OK
  ✓ lindex 1 → B
  ✓ lset out of range → error
  ✓ linsert before B → 4
  ✓ lrange after insert before
  ✓ linsert after c → 5
  ✓ lrange after insert after
  ✓ linsert pivot missing → -1
  ✓ linsert missing key → 0

── Lists: LREM / LTRIM ───────────────────────────────
  ✓ lrem 2 a (head) → 2
  ✓ lrange after lrem head
  ✓ lrem -1 a (tail) → 1
  ✓ lrange after lrem tail
  ✓ ltrim 1 3 → OK
  ✓ lrange after ltrim
  ✓ ltrim 0 -1 keeps all
  ✓ lrange unchanged
  ✓ llen after empty ltrim → 0

── Lists: wrong-type + missing-key behavior ──────────
  ✓ lpush on string → WRONGTYPE
  ✓ lrange on string → WRONGTYPE
  ✓ llen missing → 0
  ✓ lrange missing → []
  ✓ lpop missing → nil
  ✓ lrem missing → 0

── Hashes: HSET / HGET / HDEL / HEXISTS / HLEN / HGETALL / HKEYS / HVALS / HMGET 
  ✓ hset a b (2 new) → 2
  ✓ hset a update → 0
  ✓ hget a → 9
  ✓ hget missing → nil
  ✓ hlen → 2
  ✓ hexists a → 1
  ✓ hexists zzz → 0
  ✓ hmget a b zzz
  ✓ hkeys = {a,b}
  ✓ hvals = {9,2}
  ✓ hgetall = {a:9,b:2}
  ✓ hdel a → 1
  ✓ hdel a again → 0
  ✓ hdel b → 1
  ✓ type after empty → none
  ✓ hlen missing → 0
  ✓ hget missing → nil
  ✓ hexists missing → 0
  ✓ hgetall missing → []
  ✓ hmget missing → [nil,nil]
  ✓ hget on string → WRONGTYPE
  ✓ hset on string → WRONGTYPE

── Hashes extended: HSETNX / HINCRBY / HSTRLEN / HSCAN 
  ✓ hsetnx new field → 1
  ✓ hsetnx existing → 0
  ✓ score unchanged after nx
  ✓ hincrby score +5 → 15
  ✓ hincrby score -3 → 12
  ✓ hincrby new field → 7
  ✓ hincrby non-int increment → error
  ✓ hincrby on string value → error
  ✓ hstrlen greeting → 5
  ✓ hstrlen missing field → 0
  ✓ hstrlen missing key → 0
  ✓ hscan sees all 4 fields
  ✓ hscan field1 value
  ✓ hscan match field* → 3
  ✓ hscan match excludes other
  ✓ hscan missing key cursor → 0
  ✓ hscan missing key array → []
  ✓ hscan on string → WRONGTYPE

── Generic: EXISTS / TYPE / EXPIRE / TTL / PERSIST ───
  ✓ exists missing → 0
  ✓ exists present → 1
  ✓ type string
  ✓ type zset
  ✓ type list
  ✓ type hash
  ✓ type missing → none
  ✓ expire gk 100 → 1
  ✓ ttl in (0,100]
  ✓ ttl no-such-key → -2
  ✓ persist gk → 1
  ✓ ttl after persist → -1
  ✓ persist again → 0
  ✓ expire gk -1 deletes → 1
  ✓ exists after expire -1 → 0

── SCAN (cursor iteration + MATCH) ───────────────────
  ✓ scan sees user:1
  ✓ scan sees user:2
  ✓ scan sees user:3
  ✓ scan sees order:1
  ✓ scan sees order:2
  ✓ scan match user:* → only users
  ✓ scan match excludes orders
  ✓ scan match order:? → both orders

── Generic: DBSIZE / RANDOMKEY / RENAME / RENAMENX / TOUCH 
  ✓ dbsize empty DB → 0
  ✓ randomkey empty DB → nil
  ✓ dbsize after 3 sets → 3
  ✓ randomkey returns string
  ✓ randomkey is a real key
  ✓ rename eg1 → eg1new
  ✓ get eg1new → a
  ✓ eg1 gone after rename
  ✓ rename missing → error
  ✓ rename preserves TTL
  ✓ renamenx existing dst → 0
  ✓ nx_src still alive
  ✓ renamenx free dst → 1
  ✓ nx_new has value
  ✓ nx_src gone
  ✓ touch 2 existing → 2
  ✓ touch 1 existing 1 missing → 1
  ✓ touch all missing → 0

── Generic: EXPIREAT / PEXPIREAT ─────────────────────
  ✓ expireat future → 1
  ✓ ttl after expireat in (0,120]
  ✓ expireat past → 1
  ✓ key gone after past expireat
  ✓ expireat missing → 0
  ✓ pexpireat future → 1
  ✓ pttl after pexpireat in (0,60000]
  ✓ pexpireat past → 1
  ✓ key gone after past pexpireat

── Generic: FLUSHALL ─────────────────────────────────
  ✓ dbsize > 0 before flush
  ✓ flushall → OK
  ✓ dbsize 0 after flush
  ✓ randomkey after flush → nil

── UNLINK Command (async delete) ─────────────────────
  ✓ unlink missing → 0
  ✓ unlink string → 1
  ✓ string gone
  ✓ unlink small zset → 1
  ✓ small zset gone
  ℹ  inserting 1500 entries...
  ✓ large zset created → 0
  ℹ  sending unlink (thread pool path)...
  ✓ unlink large → 1
  ✓ unlink fast (<100ms)
  ✓ large zset immediately gone
  ℹ  returned in 0.2ms

── Sets: SADD / SREM / SISMEMBER / SMISMEMBER / SCARD / SMEMBERS 
  ✓ sadd 3 new → 3
  ✓ sadd 1 new 1 dup → 1
  ✓ scard after sadd → 4
  ✓ type ts1 → set
  ✓ sadd on string → WRONGTYPE
  ✓ sismember existing → 1
  ✓ sismember missing → 0
  ✓ sismember missing key → 0
  ✓ smismember a d z
  ✓ smismember missing key
  ✓ scard → 4
  ✓ scard missing → 0
  ✓ smembers returns 4 items
  ✓ smembers has a
  ✓ smembers has d
  ✓ smembers missing → []
  ✓ srem existing → 1
  ✓ srem same again → 0
  ✓ scard after srem → 3
  ✓ srem multi: b c → 2
  ✓ scard → 1
  ✓ srem missing key → 0
  ✓ srem on string → WRONGTYPE

── Sets: SPOP / SRANDMEMBER ──────────────────────────
  ✓ spop returns string
  ✓ spop reduces card by 1
  ✓ spop missing → nil
  ✓ spop count=3 list of 3
  ✓ spop count=3 distinct
  ✓ scard after spop 3 → 2
  ✓ srandmember returns string
  ✓ card unchanged after srandmember
  ✓ srandmember count=3 list
  ✓ srandmember count=3 distinct
  ✓ srandmember -5 returns 5
  ✓ srandmember count>size → all
  ✓ srandmember missing → nil

── Sets: SSCAN ───────────────────────────────────────
  ✓ sscan sees all 4
  ✓ sscan has apple
  ✓ sscan has cherry
  ✓ sscan match a* → 2
  ✓ sscan excludes banana
  ✓ sscan missing key cursor → 0
  ✓ sscan missing key array → []
  ✓ sscan on string → WRONGTYPE

── Sets: SINTER / SUNION / SDIFF ─────────────────────
  ✓ sinter s1∩s2∩s3 = {c}
  ✓ sinter s1∩s2 = {b,c}
  ✓ sunion = {a,b,c,d,e,f}
  ✓ sdiff s1-s2 = {a,d}
  ✓ sdiff s1-s2-s3 = {a,d}
  ✓ sinter wrong type → WRONGTYPE

── Sets: SINTERSTORE / SUNIONSTORE / SDIFFSTORE ──────
  ✓ sinterstore tsdest ts1 ts2 → 2
  ✓ sinterstore result = {b,c}
  ✓ sunionstore tsdest ts1 ts2 → 5
  ✓ sunionstore result = {a,b,c,d,e}
  ✓ sdiffstore tsdest ts1 ts2 → 2
  ✓ sdiffstore result = {a,d}
  ✓ sinterstore ts1←ts1∩ts2 → 2
  ✓ ts1 is now {b,c}

── Sets: SMOVE ───────────────────────────────────────
  ✓ smove existing → 1
  ✓ src no longer has x
  ✓ dst now has x
  ✓ smove non-existent → 0
  ✓ smove missing src → 0
  ✓ smove already-in-dst → 1
  ✓ src size → 1 (just z)

── Sets: SPOP/SRANDMEMBER edge semantics (hm_random paths) 
  ✓ spop count=0 → []
  ✓ spop negative count → error
  ✓ spop non-int count → error
  ✓ spop count>size returns all 5
  ✓ emptied set key is deleted
  ✓ srandmember count=0 → []
  ✓ srandmember reaches all members
  ✓ card unchanged by draws
  ✓ 50 count-draws all distinct
  ✓ card unchanged after count-draws
  ✓ membership intact after draws
  ✓ 5 single pops drain all distinct
  ✓ spop on emptied key → nil

── Edge Cases ────────────────────────────────────────
  ✓ zscore negative → -5
  ✓ zscore zero → 0
  ✓ same score sorted by name
  ✓ special chars in value
  ✓ get on zset → WRONGTYPE error
  ✓ 100 rapid get correct
  ℹ  100 rapid set/get/del complete

── PING (multibulk + inline) ─────────────────────────
  ✓ ping → PONG
  ✓ ping msg → echo
  ✓ mixed-case ping -> PONG
  ✓ ping too many args -> error
  ✓ inline PING → PONG
  ✓ inline PING msg → echo
  ✓ inline LF-only PING -> PONG

── CONFIG ────────────────────────────────────────────
  ✓ config set maxmemory 0 -> OK
  ✓ config get maxmemory -> array → ['maxmemory', '0']
  ✓ config get maxmemory value
  ✓ config get * -> array → ['port', '12590', 'protected-mode', 'yes', 'bind', '0.0.0.0', 'allow-ip', '', 'requirepass', '', 'tls-port', '12591', 'tls-cert-file', '/tmp/myred-primary-4_05gq4m/myred-primary-cert.pem', 'tls-key-file', '/tmp/myred-primary-4_05gq4m/myred-primary-key.pem', 'tls-ca-cert-file', '', 'tls-auth-clients', 'no', 'tls-handshake-timeout', '10', 'dbfilename', 'dump.rdb', 'appendonly', 'no', 'appendfilename', 'appendonly.aof', 'appendfsync', 'everysec', 'maxmemory', '0', 'maxmemory-policy', 'noeviction', 'maxmemory-samples', '10', 'notify-keyspace-events', '', 'save', '', 'auto-aof-rewrite-percentage', '100', 'auto-aof-rewrite-min-size', '67108864', 'repl-backlog-size', '1048576', 'repl-timeout', '60', 'repl-ping-replica-period', '10', 'min-replicas-to-write', '0', 'min-replicas-max-lag', '10', 'masterauth', '', 'auditlog', '']
  ✓ config get * includes maxmemory
  ✓ config get * includes maxmemory-policy
  ✓ config get unknown -> []
  ✓ config set maxmemory-policy allkeys-random -> OK
  ✓ config get maxmemory-policy allkeys-random
  ✓ config set maxmemory-policy noeviction -> OK
  ✓ config set maxmemory invalid -> error
  ✓ config set invalid policy -> error
  ✓ config set unknown parameter rejected
  ✓ config resetstat -> OK
  ✓ config bad subcommand -> error
  ✓ [REG] maxmemory-samples reads back what was set
  ✓ [REG] maxmemory-policy reads back what was set
  ✓ [REG] maxmemory reads back what was set
  ✓ [REG] appendfsync reads back what was set
  ✓ [REG] appendfilename reads back what was set
  ✓ [REG] dbfilename reads back what was set
  ✓ [REG] auto-aof-rewrite-percentage reads back what was set
  ✓ [REG] auto-aof-rewrite-min-size reads back what was set
  ✓ [REG] notify-keyspace-events reads back what was set
  ✓ [REG] appendonly no while protected-mode yes
  ✓ [REG] appendonly yes while protected-mode no

── ACL: users, auth, key patterns ────────────────────
  ✓ acl whoami -> default
  ✓ acl users -> array → ['default']
  ✓ acl users contains default
  ✓ acl list -> array → ['user default on nopass ~* &* +@all']
  ✓ acl list includes default
  ✓ acl getuser default -> array → ['flags', 'on', 'commands', '+@all', 'keys', '~*']
  ✓ acl getuser exposes flags
  ✓ acl getuser exposes commands
  ✓ acl getuser exposes keys
  ✓ acl genpass 64 -> 16 hex chars
  ✓ acl genpass invalid bits -> error
  ✓ acl setuser restricted -> OK
  ✓ auth user password -> OK
  ✓ restricted SET allowed key -> OK
  ✓ restricted GET allowed key -> 1
  ✓ restricted SET blocked by key pattern
  ✓ restricted ACL command denied
  ✓ acl deluser restricted -> 1
  ✓ cleanup acl:allowed -> 1
  ✓ acl setuser bad modifier -> error

── Pub/Sub: channel ACL &pattern (V8.2b) ─────────────
  ✓ auth as channel-restricted user → OK
  ✓ granted channel allowed
  ✓ ungranted channel denied
  ✓ literally-granted pattern allowed
  ✓ psubscribe '*' denied (cannot widen the grant)
  ✓ publish to granted channel allowed
  ✓ publish to ungranted channel denied
  ✓ no fused '~*&*' token in ACL LIST
  ✓ channel grant rendered

── INFO Command ──────────────────────────────────────
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:2\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:5\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:72572928\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:0\r\n\r\n# Stats\r\ntotal_commands:2789\r\nsync_full:0\r\nsync_partial_ok:0\r\nsync_partial_err:0\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:5946\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\nfailover_state:no-failover\r\nmaster_replid:dd35219bf2001f7c251f45e1fffcf75746aca519\r\nmaster_replid2:0000000000000000000000000000000000000000\r\nsecond_repl_offset:-1\r\nconnected_slaves:0\r\nmaster_repl_offset:122299\r\nrepl_backlog_active:1\r\nrepl_backlog_size:1048576\r\nrepl_backlog_first_byte_offset:1\r\nmin_slaves_good_slaves:0\r\nrepl_backlog_histlen:122299\r\n\r\n'
  ✓ has # Server section
  ✓ has # Clients section
  ✓ has # Memory section
  ✓ has # Stats section
  ✓ has # Keyspace section
  ✓ has # Persistence section
  ✓ has version field
  ✓ has uptime_seconds field
  ✓ has connected_clients field
  ✓ has used_memory field
  ✓ has maxmemory field
  ✓ has maxmemory_policy field
  ✓ has evicted_keys field
  ✓ has total_commands field
  ✓ has keys_total field
  ✓ has keys_with_ttl field
  ✓ has aof_enabled field
  ✓ has aof_current_size field
  ✓ has aof_last_write_status field

  INFO output:
    # Server
    version:1.0.0
    uptime_seconds:2
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:5
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:72572928
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:0
    # Stats
    total_commands:2789
    sync_full:0
    sync_partial_ok:0
    sync_partial_err:0
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:5946
    rdb_changes_since_save:1993
    rdb_last_save_ok:1
    rdb_last_save_size_bytes:0
    aof_enabled:0
    aof_current_size:0
    aof_base_size:0
    aof_pending_rewrite:0
    aof_last_write_status:ok
    aof_last_bgrewrite_status:ok
    # Replication
    role:master
    failover_state:no-failover
    master_replid:dd35219bf2001f7c251f45e1fffcf75746aca519
    master_replid2:0000000000000000000000000000000000000000
    second_repl_offset:-1
    connected_slaves:0
    master_repl_offset:122299
    repl_backlog_active:1
    repl_backlog_size:1048576
    repl_backlog_first_byte_offset:1
    min_slaves_good_slaves:0
    repl_backlog_histlen:122299

── INFO: O(1) keyspace stats (heap-backed keys_with_ttl) 
  ✓ empty db → (0,0)
  ✓ 3 keys, no ttl → (3,0)
  ✓ 2 ttls set → (3,2)
  ✓ persist decrements → (3,1)
  ✓ SET clears ttl → (3,0)
  ✓ expired key leaves both → (2,0)
  ✓ del → (0,0)

── FLUSHDB ───────────────────────────────────────────
  ✓ keys exist before flushdb
  ✓ flushdb → OK
  ✓ dbsize → 0
  ✓ flushed key gone

── SAVE / RDB Persistence ────────────────────────────
  ✓ save → OK
  ✓ dump.rdb exists
  ✓ dump.rdb not empty
  ℹ  dump.rdb size: 19 bytes
  ✓ magic number correct

── BGSAVE (fork-based background save) ───────────────
  ✓ bgsave returns string → 'Background saving started'
  ✓ bgsave returns fast (<50ms)
  ℹ  bgsave returned in 1.5ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 12.7ms
  ✓ save did not block event loop (burst <500ms)
  ✓ dump.rdb exists after bgsave
  ✓ bgsave file has magic
  ✓ second bgsave handled gracefully

── BGREWRITEAOF ──────────────────────────────────────
  ✓ bgrewriteaof → string → 'Background append only file rewriting started'
  ✓ server responsive after bgrewriteaof

── Memory: accounting (used_memory) ──────────────────
  ✓ empty DB → used_memory 0
  ✓ used_memory grows after SET
  ✓ used_memory back to baseline after DEL
  ✓ used_memory grows after RPUSH x200
  ✓ used_memory back to baseline after list DEL
  ✓ mixed load grew used_memory
  ✓ FLUSHALL returns used_memory to 0

── Memory: MEMORY / OBJECT introspection ─────────────
  ✓ memory usage o:str → int → 182
  ✓ memory usage missing → nil
  ✓ memory usage ... samples 3 → int → 285
  ✓ MEMORY USAGE (uppercase) → int → 182
  ✓ memory doctor → string → "Can't find any memory problem. used_memory=1666 matches a full sweep."
  ✓ memory doctor reports no drift
  ✓ memory stats → array → ['used_memory', 1666, 'keys.count', 6, 'maxmemory', 0, 'maxmemory.policy', 'noeviction', 'evicted.keys', 0]
  ✓ object encoding o:str → raw
  ✓ object encoding o:int → int
  ✓ object encoding o:list → deque
  ✓ object encoding o:hash → hashtable
  ✓ object encoding o:set → hashtable
  ✓ object encoding o:zset → skiplist
  ✓ OBJECT ENCODING (uppercase) works
  ✓ object refcount → 1
  ✓ object idletime → int → 0
  ✓ object on missing key → error
  ✓ object bad subcommand → error

── Memory: maxmemory eviction + OOM ──────────────────
  ✓ noeviction: writes succeed then OOM
  ✓ noeviction: used_memory bounded
  ✓ noeviction: nothing evicted
  ✓ FLUSHALL allowed over cap
  ✓ allkeys-lru: no OOM
  ✓ allkeys-lru: used_memory bounded
  ✓ allkeys-lru: evicted_keys climbed
  ✓ allkeys-random: no OOM on non-TTL keys
  ✓ allkeys-random: evicted_keys climbed

── Memory: incremental eviction (EVICT_RUNNING semantics) 
  ✓ write admitted during overshoot
  ✓ idle drain under cap (2268000 -> 524090 <= 524288)

── ECHO + inline protocol ────────────────────────────
  ✓ echo roundtrip
  ✓ echo empty string
  ✓ echo 20-byte marker
  ✓ echo whitespace-safe
  ✓ echo no args → arity error
  ✓ echo 2 args → arity error
  ✓ inline ping → PONG
  ✓ inline \n-only tolerated
  ✓ empty inline line ignored

── Authentication ────────────────────────────────────
  ℹ  no password configured — skipping auth tests
  ℹ  run with --password to test auth

── Pub/Sub: SUBSCRIBE / UNSUBSCRIBE / PUBLISH (V8.1) ─
  ✓ subscribe confirmation
  ✓ second subscribe → count 2
  ✓ publish reports 1 receiver
  ✓ subscriber receives message
  ✓ publish to empty channel → 0
  ✓ no stray push for empty channel
  ✓ GET refused while subscribed
  ✓ PING allowed while subscribed
  ✓ publish with no message → arity error
  ✓ server alive after arity probe
  ✓ unsubscribe one channel
  ✓ bare unsubscribe drains to 0
  ✓ subscribe mode ends with the last subscription
  ✓ publish reaches subscriber before close
  ✓ publish after disconnect → 0 (registry unlinked)
  ✓ server alive after teardown

── Pub/Sub: PSUBSCRIBE patterns (V8.2a) ──────────────
  ✓ psubscribe confirmation
  ✓ exact subscribe on a covered channel
  ✓ one publish counts exact + pattern
  ✓ pattern subscriber gets 4-element pmessage
  ✓ exact subscriber gets 3-element message
  ✓ non-matching channel reaches nobody
  ✓ ...and no stray push arrives
  ✓ subscribe count includes held patterns
  ✓ unsubscribe count still includes the pattern
  ✓ pattern-only conn is still in subscribe mode
  ✓ bare punsubscribe drains to 0
  ✓ pattern subscriber leaves subscribe mode

── Pub/Sub: keyspace notifications (V8.3) ────────────
  ✓ CONFIG SET notify-keyspace-events KEA
  ✓ SET emits __keyspace__ form (payload = event)
  ✓ SET emits __keyevent__ form (payload = key)
  ✓ LPUSH emits the list-class event
  ✓ DEL emits the generic-class event
  ✓ no-op write emits nothing
  ✓ with 'Ex' a string write is filtered out
  ✓ with 'Ex' PSETEX itself is filtered out
  ✓ expired hook fires on TTL expiry
  ✓ with 'Ex' the __keyspace__ form is suppressed
  ✓ notifications off → nothing emitted

── Transactions: MULTI / QUEUED / DISCARD / EXEC (V8.4-V8.5) 
  ✓ MULTI opens a transaction
  ✓ queued write replies QUEUED
  ✓ queued read replies QUEUED
  ✓ nested MULTI is refused
  ✓ a refused nested MULTI leaves the transaction open
  ✓ queued commands have not run
  ✓ DISCARD closes the transaction
  ✓ DISCARD ran nothing
  ✓ DISCARD without MULTI is an error
  ✓ connection still usable after DISCARD
  ✓ unknown command is rejected at queue time
  ✓ bad arity is rejected at queue time
  ✓ SUBSCRIBE inside MULTI is rejected
  ✓ queuing continues after a rejection
  ✓ EXEC on a poisoned transaction aborts
  ✓ EXECABORT ran nothing
  ✓ EXEC without MULTI is an error
  ✓ empty transaction commits as an empty array
  ✓ EXEC returns one array of results, in order
  ✓ EXEC applied the writes
  ✓ EXEC header counts every queued command
  ✓ a failing element is an inline error
  ✓ commands after a failing one still run
  ✓ no rollback: the later write is visible

── Transactions: WATCH / UNWATCH (V8.6-V8.7) ─────────
  ✓ WATCH replies OK
  ✓ EXEC aborts with a null array after a watched write
  ✓ the aborted transaction ran nothing
  ✓ a fresh transaction commits, watches cleared by EXEC
  ✓ ...and its write landed
  ✓ EXEC commits when the watched key was never touched
  ✓ UNWATCH replies OK
  ✓ EXEC commits after UNWATCH despite the outside write
  ✓ DISCARD cleared the watch
  ✓ a multi-key write dirties a watcher of its last key
  ✓ writing a key named 'watch' does not abort a transaction
  ✓ WATCH inside MULTI is refused
  ✓ a refused WATCH did not poison the transaction
  ✓ a watched key expiring on its own does not abort
  ✓ write to a dead watcher's key is safe
  ✓ server alive after watcher teardown

── Persistence Round-trip (in-memory) ────────────────
  ✓ save → OK
  ✓ string still readable
  ✓ zset alice still readable → 10
  ✓ zset bob still readable → 20
  ✓ zrank alice → 0
  ✓ ttl preserved after save

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Pub/Sub: fan-out under concurrent publishers ──────
  ✓ no publisher/reader errors
  ✓ every subscriber received every message
    4 publishers × 250 msgs → 4 subscribers = 4000 deliveries in 0.66s (6,074 deliveries/s)

═══════════════════════════════════════════════════════
  Managed-instance phases
═══════════════════════════════════════════════════════
  Binary:   /home/kylemg/testfiles/testfiles/build-rel/server
  Workdir:  /tmp/myred-suite-u8ksgg62
  Ports:    from 12500
  Phases:   unit, memory, config, auth, security, persistence, tls, replication

── Unit: HMap incremental rehash ─────────────────────
  ✓ the unit test compiles against hashtable.cpp
    phase 1: insert 600 keys (drives ~5 rehash cycles)
      ok   all keys inserted (hm_size)
    phase 2: give the drain every chance to finish
      ok   draining table fully emptied (older.size == 0)
      ok   draining table released (older.tab == NULL)
    phase 3: every key still reachable
      ok   all keys found after rehash cycles
    4 passed, 0 failed
  ✓ [REG] the incremental rehash always finishes draining

── Memory: per-type accounting invariants ────────────
  ✓ an empty database accounts for 0 bytes
  ✓ string (set/del): grows on build
  ✓ string (set/del): back to baseline after drain
  ✓ list (rpush/lpop-all): grows on build
  ✓ list (rpush/lpop-all): back to baseline after drain
  ✓ hash (hset/hdel-all): grows on build
  ✓ hash (hset/hdel-all): back to baseline after drain
  ✓ set (sadd/srem-all): grows on build
  ✓ set (sadd/srem-all): back to baseline after drain
  ✓ zset (zadd/zpopmin-all): grows on build
  ✓ zset (zadd/zpopmin-all): back to baseline after drain
  ✓ list emptied via lrem: grows on build
  ✓ list emptied via lrem: back to baseline after drain
  ✓ list emptied via ltrim: grows on build
  ✓ list emptied via ltrim: back to baseline after drain
  ✓ set emptied via spop: grows on build
  ✓ set emptied via spop: back to baseline after drain
  ✓ a string overwritten 1000 times leaks nothing
  ✓ append growth: grows on build
  ✓ append growth: back to baseline after drain
  ✓ sinterstore dest: grows on build
  ✓ sinterstore dest: back to baseline after drain
  ✓ rename re-key: grows on build
  ✓ rename re-key: back to baseline after drain
  ✓ a mixed load grows used_memory
  ✓ FLUSHALL returns used_memory to exactly 0

── Memory: maxmemory (noeviction vs allkeys-lru) ─────
  ✓ noeviction: writes succeed, then start refusing
  ✓ noeviction: used_memory stays bounded near the cap
  ✓ noeviction: nothing was evicted
  ✓ noeviction: a write over the cap is refused
  ✓ allkeys-lru: no write is ever refused
  ✓ allkeys-lru: used_memory held near the cap
  ✓ allkeys-lru: evicted_keys climbed

── Memory: incremental eviction under a large overshoot 
  ✓ [REG] the write right after a 6x overshoot is admitted
  ✓ the keyspace drains while completely idle (6000 keys -> 898)

── Config: CONFIG REWRITE survives a restart ─────────
  ✓ repl-backlog-size read its boot value from the config file
  ✓ repl-backlog-size is boot-only and refuses CONFIG SET
  ✓ tls-handshake-timeout read its boot value from the config file
  ✓ tls-handshake-timeout is boot-only and refuses CONFIG SET
  ✓ maxmemory-samples reads back after the set
  ✓ maxmemory-policy reads back after the set
  ✓ maxmemory reads back after the set
  ✓ appendfsync reads back after the set
  ✓ auto-aof-rewrite-percentage reads back after the set
  ✓ auto-aof-rewrite-min-size reads back after the set
  ✓ notify-keyspace-events reads back after the set
  ✓ repl-timeout reads back after the set
  ✓ repl-ping-replica-period reads back after the set
  ✓ min-replicas-to-write reads back after the set
  ✓ min-replicas-max-lag reads back after the set
  ✓ CONFIG REWRITE returns OK
  ✓ [REG] the rewritten config still carries requirepass
  ✓ the rewritten config carries the channel grant
  ✓ [REG] no fused '~*&*' token in the rewritten config
  ✓ [REG] the old password still authenticates after the round-trip
  ✓ [REG] an unauthenticated connection is still refused
  ✓ [REG] maxmemory-samples survived the rewrite ('7')
  ✓ [REG] maxmemory-policy survived the rewrite ('allkeys-random')
  ✓ [REG] maxmemory survived the rewrite ('12345678')
  ✓ [REG] appendfsync survived the rewrite ('always')
  ✓ [REG] auto-aof-rewrite-percentage survived the rewrite ('77')
  ✓ [REG] auto-aof-rewrite-min-size survived the rewrite ('12345678')
  ✓ [REG] notify-keyspace-events survived the rewrite ('AKE')
  ✓ [REG] repl-timeout survived the rewrite ('77')
  ✓ [REG] repl-ping-replica-period survived the rewrite ('13')
  ✓ [REG] min-replicas-to-write survived the rewrite ('0')
  ✓ [REG] min-replicas-max-lag survived the rewrite ('17')
  ✓ [REG] repl-backlog-size survived the rewrite ('1048576')
  ✓ [REG] tls-handshake-timeout survived the rewrite ('45')
  ✓ the channel grant survived the restart
  ✓ [REG] no fused '~*&*' token in ACL LIST
  ✓ the allchannels user survived the restart

── Auth: async verify, pipelining, lockout, loop latency 
  ✓ [REG] three replies arrive in order (OK, PONG, OK)
  ✓ the pipelined SET actually executed with the authed identity
  ✓ a wrong password answers WRONGPASS
  ✓ the connection is closed after repeated auth failures
  ✓ all 8 concurrent AUTHs completed
  ✓ all 200 PINGs were answered during the AUTH storm
  info PING during an AUTH storm: p50=0.04ms p99=0.08ms
         a synchronous Argon2 verify would put p99 at 20-60ms+; that gap is the whole point of the async path.
  ✓ AUTH #1 accepted (the credential survives any rehash)
  ✓ AUTH #2 accepted (the credential survives any rehash)
  ✓ [REG] the audit log carries no plaintext, digest, or PHC hash
  info cred_rehash events this lifetime: 0 (0 is correct for a credential already stored as Argon2id)

── Security: ACL enforcement, renames, audit log ─────
  ✓ setuser limited (+@read +@write)
  ✓ setuser keyed (~data:*)
  ✓ setuser smover (~src:* ~dst:*)
  ✓ setuser ghost (passwordless — must survive a round-trip)
  ✓ limited: GET works
  ✓ limited: SET works
  ✓ limited: CONFIG GET denied
  ✓ limited: ACL WHOAMI denied
  ✓ limited: KEYS denied
  ✓ limited: MEMORY denied
  ✓ limited: the flushall alias is denied too
  ✓ canonical FLUSHALL is unknown
  ✓ a command renamed to '' is unreachable
  ✓ the alias works for admin
  ✓ the alias really flushed
  ✓ a wrong password is rejected
  ✓ audit records auth_success
  ✓ audit records auth_fail
  ✓ audit records acl_change
  ✓ audit records acl_deny
  ✓ [REG] no plaintext password reaches the audit log
  ✓ keyed: a key inside the pattern is allowed
  ✓ keyed: a key outside the pattern is denied
  ✓ keyed: writing outside the pattern is denied
  ✓ smover: SMOVE with both keys granted is allowed
  ✓ [REG] smover: SMOVE to an ungranted destination is denied
  ✓ ACL CAT returns a list
  ✓ ACL CAT lists exactly the 9 categories
  ✓ [REG] every advertised category is also parseable
  ✓ CONFIG REWRITE returns OK
  ✓ the rewritten config keeps the port
  ✓ the rewritten config keeps the users
  ✓ admin auth survives the round-trip
  ✓ a passwordless user survives the round-trip
  ✓ limited auth survives the round-trip
  ✓ limited is still denied the control plane after the round-trip

── Security: protocol abuse (server must keep serving) 
  ✓ server survives an absurd multibulk count
  ✓ server survives a garbage array header
  ✓ server survives a negative bulk length
  ✓ server survives a bulk length that overflows int64
  ✓ server survives an oversized inline line
  ✓ server survives a key name full of RESP control bytes
  ✓ the server process is still alive

── Persistence: AOF write gating ─────────────────────
  ✓ AOF exists and is non-empty
  ✓ the write was logged
  ✓ [REG] a read is never logged
  ✓ [REG] a failed SETNX is not logged
  ✓ [REG] a DEL of a missing key is not logged
  ✓ SETEX is logged as SET + absolute PEXPIREAT
  ✓ SETEX's relative TTL is not in the log
  ✓ EXPIRE is rewritten to an absolute PEXPIREAT
  ✓ PEXPIREAT follows the SET it belongs to
  ✓ replay: stderr shows a replay happened
  ✓ replay: no replay-error WARNING in stderr
  ✓ SETEX ttl survived the restart without resetting
  ✓ INCR replayed to the same value

── Persistence: BGREWRITEAOF (manual + auto-trigger) ─
  ✓ manual BGREWRITEAOF produced a hybrid file (MYAOFRDB preamble)
  ✓ the rewrite compacted the log (65246 -> 195 bytes)
  ✓ [REG] the rewrite leaves no temp file behind
  ✓ [REG] no misspelled AOF file was created
  ✓ the auto-trigger fired at the configured growth percentage
  ✓ the TTL is still live before the restart
  ✓ post-rewrite replay: stderr shows a replay happened
  ✓ post-rewrite replay: no replay-error WARNING in stderr
  ✓ the compacted file reconstructs the string
  ✓ the writes made after the last auto-rewrite survived too
  ✓ [REG] the TTL survived the rewrite as an absolute deadline
  ✓ compacted file reconstructs the list
  ✓ compacted file reconstructs the hash
  ✓ compacted file reconstructs the set

── Persistence: hybrid AOF (RDB preamble + RESP delta) 
  ✓ rewrite produced the MYAOFRDB preamble
  ✓ stderr reports loading the RDB preamble
  ✓ preamble restored the string
  ✓ preamble restored the TTL
  ✓ preamble restored the hash
  ✓ preamble restored the set
  ✓ preamble restored the zset
  ✓ [REG] the RESP delta replayed on top of the preamble
  ✓ delta ordering survived (LPUSH landed at the front)
  ✓ [REG] a plain RESP AOF with no preamble still loads
  ✓ plain-RESP load: stderr shows a replay happened
  ✓ plain-RESP load: no replay-error WARNING in stderr
  ✓ [REG] a torn RESP tail truncates and leaves the preamble intact
  ✓ server is serving after recovering from the torn tail

── Persistence: RDB save/load round-trip ─────────────
  ✓ SAVE returns OK
  ✓ dump.rdb was written
  ✓ every key came back from the RDB
  ✓ every value came back identical
  ✓ the TTL came back from the RDB as an absolute deadline
  ✓ zset scores survived at full precision

── Persistence: restart matrix (translated AOF frames) 
  ✓ eviction actually removed keys
  ✓ GETEX rm:ttl ex 100 returns the value
  ✓ GETEX set a TTL
  ✓ canonical 'getdel' is renamed away
  ✓ alias gdel returns the value
  ✓ gdel deleted the key
  ✓ zpopmin removed the min member
  ✓ zpopmin kept b
  ✓ SPOP returned a member
  ✓ SPOP left 4 members
  ✓ SPOP <count> emptied the set
  ✓ SREM to empty removed the key
  ✓ AOF exists and is non-empty
  ✓ restart: stderr shows a replay happened
  ✓ restart: no replay-error WARNING in stderr
  ✓ keyspace size matches pre-shutdown
  ✓ no key lost by replay
  ✓ no key resurrected by replay
  ✓ every surviving value is identical
  ✓ GETEX ttl survived within its original bound
  ✓ gdel'd key is still gone
  ✓ zpopmin'd member is still gone
  ✓ [REG] the SPOP'd member is still gone after replay
  ✓ [REG] the set SPOP emptied stayed empty
  ✓ [REG] the set SREM emptied stayed empty
  ✓ the alias still resolves after restart

── Persistence: crash recovery (SIGKILL mid-traffic) ─
  ✓ server boots after SIGKILL
  ✓ the pre-crash keyspace is intact after the crash boot
  info crash writes recovered: 50/50 (everysec fsync makes fewer than 50 correct)

── TLS: handshake and live certificate rotation ──────
  ✓ the server boots with TLS configured
  ✓ a TLS handshake completes on the tls-port
  ✓ RESP works over TLS
  info negotiated TLSv1.3, cipher TLS_AES_256_GCM_SHA384
  ✓ the plaintext port still serves plaintext
  ✓ a plaintext client on the TLS port kills only its own connection
  ✓ a TLS client on the plaintext port does not get a handshake
  ✓ the server survives both mismatches
  ✓ the presented certificate can be fingerprinted
  ✓ CONFIG SET tls-cert-file is accepted
  ✓ [REG] new connections are served the NEW certificate
  info hot reload took 0.79ms (a restart costs tens of ms and drops every connection)
  ✓ CONFIG SET tls-key-file is accepted
  ✓ [REG] tls-key-file reads back the key path, not the cert path
  ✓ [REG] the connection established before the rotation still works
  ✓ a nonexistent certificate file is refused
  ✓ the server keeps serving the old certificate after a refusal
  ✓ [REG] a rejected CONFIG SET rolls the path back
  ✓ the server is still serving plaintext too
  master :12511   proxy :12513   replica :12512

── Replication: full resync ──────────────────────────
  ✓ the replica booted into the role from its config file
  ✓ the replica reports role:slave
  ✓ [REG] the replica adopted the master's replid
  ✓ the master counts one connected replica
  ✓ the pre-resync key pre1 arrived
  ✓ the pre-resync key pre2 arrived
  ✓ the pre-resync key pre3 arrived
  ✓ the replica logged the resync

── Replication: live streaming ───────────────────────
  ✓ SET propagates
  ✓ SADD propagates
  ✓ DEL propagates
  ✓ [REG] a TTL replicates as an absolute time, not a relative one
  ✓ [REG] SPOP replicates the member it removed, not the command

── Replication: read-only gate ───────────────────────
  ✓ a write from an ordinary client is refused
  ✓ [REG] FLUSHALL is refused too (it carries is_write)
  ✓ reads are still served
  ✓ MULTI itself is allowed
  ✓ a queued write is refused at queue time
  ✓ the poisoned transaction aborts
  ✓ [REG] the replication stream still applies through the gate

── Replication: link loss must not promote ───────────
  ✓ the link goes down
  ✓ [REG] a dropped socket does NOT promote the replica
  ✓ [REG] the master address survives the drop
  ✓ [REG] still read-only while disconnected

── Replication: partial resync ───────────────────────
  ✓ the link comes back
  ✓ the gap keys arrived
  ✓ [REG] the reconnect was a PARTIAL resync
  ✓ [REG] no RDB was retransferred

── Replication: a gap larger than the backlog falls back 
  ✓ the link is down again
  ✓ the link comes back
  ✓ the replica caught up
  ✓ [REG] an unservable offset degrades to a FULL resync
  ✓ the refusal was counted

── Replication: automatic reconnect ──────────────────
  ✓ the link is down
  ✓ [REG] the replica re-dials without being told
  ✓ writes missed during the outage arrived
  ✓ the automatic reconnect used a partial resync

── Replication: REPLCONF ACK + WAIT ──────────────────
  ✓ the master learns the replica's offset from periodic acks
  ✓ WAIT 1 counts the caught-up replica
  ✓ ...and returned well inside its timeout
  ✓ [REG] an unsatisfiable WAIT returns a short count, not an error
  ✓ ...after roughly its timeout
  ✓ the connection still works afterwards
  ✓ [REG] a pending WAIT does not block the event loop
  ✓ the deferred client is resumed on timeout
  ✓ [REG] WAIT inside EXEC answers immediately instead of deferring

── Replication: min-replicas-to-write durability floor 
  ✓ min-replicas-to-write defaults to 0 (feature off)
  ✓ min-replicas-max-lag defaults to 10 seconds
  ✓ a negative count is rejected
  ✓ a count past the cap is rejected
  ✓ a lag past the cap is rejected
  ✓ a lag of 0 is accepted (do not judge on lag)
  ✓ the link is healthy before the floor goes up
  ✓ a replica that is acking satisfies the floor
  ✓ [REG] INFO reports the good-replica count on a MASTER
  ✓ [REG] a replica that stopped acking stops counting
  ✓ ...and the refusal is the whole cost: reads are untouched
  ✓ min_slaves_good_slaves fell to 0
  ✓ [REG] the lagging replica is still CONNECTED
  ✓ writes resume once the acks come back
  ✓ [REG] the floor never refuses the replication stream itself

── Replication: repl-timeout directive ───────────────
  ✓ repl-timeout defaults to 60 seconds
  ✓ [REG] repl-timeout round-trips in SECONDS, not milliseconds
  ✓ a value past the cap is rejected
  ✓ 0 is accepted (disabled)

── Replication: an idle link must survive ────────────
  ✓ the link is up before the idle window
  ✓ [REG] a quiet master is not mistaken for a dead one
  ✓ ...and the link is still up afterwards

── Replication: a wedged link (silent, not closed) ───
  ✓ healthy before the freeze
  ✓ master_last_io_seconds_ago is present and small
  ✓ [REG] the replica drops a silent master with no traffic to wake it
  ✓ [REG] the master drops a replica that stopped acking
  ✓ the replica reports the link down
  ✓ master_last_io_seconds_ago reflects the drop, not a stale age
  ✓ [REG] a dropped link does NOT promote the replica
  ✓ the master shows no replicas
  ✓ a reaped replica cannot satisfy WAIT
  ✓ it reconnects on its own once the path comes back
  ✓ streaming resumed

── Replication: promotion ────────────────────────────
  ✓ promoted to master
  ✓ [REG] promotion mints a NEW replid
  ✓ writable again
  ✓ [REG] CONFIG REWRITE drops the replicaof line after promotion
  ✓ re-attached
  ✓ [REG] a promoted instance forfeits its history (full resync)
  ✓ CONFIG REWRITE restores the line once it is a replica again

── Replication: a restart keeps the role ─────────────
  ✓ [REG] a restarted replica comes back a REPLICA, not a writable master
  ✓ streaming resumed after the restart
  ✓ still read-only after the restart

── Replication: coordinated FAILOVER ─────────────────
  ✓ the failover pair is linked
  ✓ FAILOVER TO 127.0.0.1 -> needs a host and a port
  ✓ FAILOVER TO 127.0.0.1 0 -> invalid FAILOVER target port
  ✓ FAILOVER TO 127.0.0.1 70000 -> invalid FAILOVER target port
  ✓ FAILOVER TIMEOUT abc -> invalid FAILOVER TIMEOUT
  ✓ FAILOVER WAT -> syntax error
  ✓ FAILOVER FORCE TIMEOUT 1000 -> FORCE requires TO
  ✓ FAILOVER ABORT -> No failover in progress
  ✓ FORCE without TIMEOUT is refused
  ✓ [REG] a valid port with no replica behind it is 'not a connected replica'
  ✓ FAILOVER on a replica is refused
  ✓ FAILOVER TO <the connected replica> accepted
  ✓ [REG] INFO reports the pause on the MASTER
  ✓ [REG] writes are paused while the handover waits
  ✓ reads are served throughout the pause
  ✓ a second FAILOVER is refused
  ✓ FAILOVER ABORT unwinds a waiting handover
  ✓ writes flow again after the abort
  ✓ the role never changed
  ✓ FAILOVER with a short TIMEOUT accepted
  ✓ [REG] the TIMEOUT fires with no traffic to wake the loop
  ✓ the timed-out master is writable again
  ✓ ...still a master, having handed over to nobody
  ✓ failover_state is back to no-failover
  ✓ FAILOVER ... FORCE accepted
  ✓ [REG] FORCE hands over past a target that never caught up
  ✓ the old master demoted itself
  ✓ the target promoted itself on PSYNC ... FAILOVER
  ✓ the demoted master re-attached to it
  ✓ [REG] a forced handover is NOT served a +CONTINUE
  ✓ the writes FORCE stepped over are gone
  ✓ failover_state cleared on the demoted master
  ✓ the pair is healthy again before the clean handover
  ✓ the master takes writes before the handover
  ✓ ...and they reach the replica
  ✓ the replica is read-only going in
  ✓ the clean FAILOVER is accepted
  ✓ the roles swapped
  ✓ the demoted instance re-attached
  ✓ [REG] a coordinated handover moves NO RDB image
  ✓ promotion retired the shared history into master_replid2
  ✓ [REG] the demoted instance adopted the new replid from +CONTINUE
  ✓ no data was lost across the coordinated handover
  ✓ the new master is writable
  ✓ ...and streams to the demoted one
  ✓ a bare FAILOVER is accepted (the target is chosen automatically)
  ✓ ...and it handed over to the only replica there is

── Replication: promotion keeps the history ──────────
  ✓ the second replica attached
  ✓ both replicas caught up
  ✓ [REG] the replica's offset matches the master's exactly
  ✓ [REG] a replica feeds its own backlog while streaming
  ✓ the surviving replica noticed the master is gone
  ✓ [REG] REPLICAOF NO ONE promotes a replica whose master is DOWN
  ✓ ...and it is writable
  ✓ [REG] promotion RETIRES the old replid into master_replid2
  ✓ second_repl_offset marks the handover point
  ✓ a new replid was still minted
  ✓ the sibling re-attached to the promoted instance
  ✓ [REG] the sibling PARTIAL-resyncs off the retired history
  ✓ the new master streams to the sibling
  ✓ [REG] the sibling adopted the promoted master's replid
  ✓ the sibling's link bounced at least once
  ✓ the sibling re-dialled on its own
  ✓ [REG] every reconnect after the promotion is still partial

═══════════════════════════════════════════════════════
Results: 1023/1023 passed
Runtime: 56.00s (18.3 assertions/sec)
All tests passed!
Slowest sections:
  7.93s  11/11  Pub/Sub: keyspace notifications (V8.3)
  7.00s  3/3  Replication: an idle link must survive
  5.36s  47/47  Replication: coordinated FAILOVER
  5.35s  7/7  Security: protocol abuse (server must keep serving)
  3.98s  9/9  Replication: REPLCONF ACK + WAIT
  3.61s  15/15  Replication: min-replicas-to-write durability floor
  2.98s  11/11  Replication: a wedged link (silent, not closed)
  2.91s  9/9  Auth: async verify, pipelining, lockout, loop latency
═══════════════════════════════════════════════════════

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    2.38s
  Throughput: 1681 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 4.41ms
  Latency min: 0.07ms
  Latency max: 90.89ms
  Latency p50: 1.01ms
  Latency p95: 15.86ms
  Latency p99: 77.47ms
  No errors!
  Operation mix:
    append                131 ok     0 errors
    zquery                124 ok     0 errors
    strlen                118 ok     0 errors
    hset                  117 ok     0 errors
    srandmember           114 ok     0 errors
    set                   114 ok     0 errors
    zadd                  114 ok     0 errors
    lrange                112 ok     0 errors
    incr                  110 ok     0 errors
    sismember             109 ok     0 errors
    keyspace_scan         109 ok     0 errors
    zscore                108 ok     0 errors
  Slowest operations by average latency:
    keys                  68.03ms avg over 98 ops
    keyspace_scan         45.46ms avg over 109 ops
    hscan                  6.03ms avg over 94 ops
    hgetall                5.39ms avg over 100 ops
    sscan                  4.91ms avg over 105 ops
    smembers               4.11ms avg over 95 ops
    zrevquery              3.49ms avg over 84 ops
    zquery                 2.76ms avg over 124 ops
    lrange                 2.41ms avg over 112 ops
    ttl_triplet            2.28ms avg over 97 ops
    list_pop_trim          2.03ms avg over 90 ops
    zpopmin                1.88ms avg over 94 ops
  ℹ  cleaned 164 leftover keys

-- Command Metrics -------------------------------------
  Commands observed: 40898
  RESP errors:       214 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.61ms
  Latency p50/p95/p99: 0.05/1.23/5.68ms
  Latency max:       1000.83ms
  Most used commands:
    set             27030 calls
    zadd             2032 calls
    rpush            1408 calls
    get              1067 calls
    publish          1009 calls
    del               712 calls
    lpop              592 calls
    sadd              448 calls
    hset              438 calls
    srem              406 calls
    type              370 calls
    srandmember       370 calls
  Slowest commands by average latency:
    wait             620.22ms avg over 4 calls
    keys              63.72ms avg over 105 calls
    auth              59.09ms avg over 2 calls
    scan              41.83ms avg over 112 calls
    acl                8.29ms avg over 36 calls
    hscan              5.78ms avg over 98 calls
    hgetall            5.08ms avg over 106 calls
    sscan              4.73ms avg over 109 calls
    smembers           3.48ms avg over 113 calls
    zrevquery          3.40ms avg over 86 calls
    zquery             2.63ms avg over 130 calls
    lrange             2.11ms avg over 129 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
  correctness + concurrency + managed-instance phases + stress over TLS (passwordless) → 127.0.0.1:12591
  Native — 6.18.35-1-lts
  Log:     docs/logs/Native/full_tls.md
  Summary: docs/logs/Native/full_tls.json
  Compare two machines with: --compare <A.json> <B.json>
═══════════════════════════════════════════════════════


```
