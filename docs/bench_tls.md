# MYRED stress test — 2026-07-25 20:20:42

**Run:** correctness + concurrency + stress + redis-benchmark over TLS (passwordless) → 127.0.0.1:1337

```
(logging output to docs/bench_tls.md)
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + stress + redis-benchmark over TLS (passwordless) → 127.0.0.1:1337
═══════════════════════════════════════════════════════
  Target:    127.0.0.1:1337
  Transport: TLS (insecure — cert not verified)
  Auth:      none
  Log:       docs/bench_tls.md
═══════════════════════════════════════════════════════
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
  ℹ  returned in 0.0ms

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
  ✓ config get * -> array → ['maxmemory', '0', 'maxmemory-policy', 'noeviction', 'notify-keyspace-events', '']
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
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:109\r\nuptime_minutes:1\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:1064\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:181846016\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:4227\r\n\r\n# Stats\r\ntotal_commands:2021720\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:1956\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:19\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:109
    uptime_minutes:1
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:1064
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:181846016
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:4227
    # Stats
    total_commands:2021720
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:1956
    rdb_changes_since_save:1993
    rdb_last_save_ok:1
    rdb_last_save_size_bytes:19
    aof_enabled:0
    aof_current_size:0
    aof_base_size:0
    aof_pending_rewrite:0
    aof_last_write_status:ok
    aof_last_bgrewrite_status:ok
    # Replication
    role:master

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
  ℹ  dump.rdb size: 75 bytes
  ✓ magic number correct

── BGSAVE (fork-based background save) ───────────────
  ✓ bgsave returns string → 'Background saving started'
  ✓ bgsave returns fast (<50ms)
  ℹ  bgsave returned in 3.1ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 4.1ms
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
  ✓ memory stats → array → ['used_memory', 1666, 'keys.count', 6, 'maxmemory', 0, 'maxmemory.policy', 'noeviction', 'evicted.keys', 4227]
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
  ✓ idle drain under cap (2268000 -> 523908 <= 524288)

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

── Persistence Round-trip (in-memory) ────────────────
  ✓ save → OK
  ✓ string still readable
  ✓ zset alice still readable → 10
  ✓ zset bob still readable → 20
  ✓ zrank alice → 0
  ✓ ttl preserved after save

═══════════════════════════════════════════════════════
Results: 599/599 passed
Runtime: 12.73s (47.1 assertions/sec)
All tests passed!
Slowest sections:
  7.92s  11/11  Pub/Sub: keyspace notifications (V8.3)
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.54s  9/9  UNLINK Command (async delete)
  0.52s  7/7  BGSAVE (fork-based background save)
  0.52s  16/16  Pub/Sub: SUBSCRIBE / UNSUBSCRIBE / PUBLISH (V8.1)
  0.52s  6/6  Persistence Round-trip (in-memory)
  0.51s  4/4  SAVE / RDB Persistence
  0.41s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Pub/Sub: fan-out under concurrent publishers ──────
  ✓ no publisher/reader errors
  ✓ every subscriber received every message
    4 publishers × 250 msgs → 4 subscribers = 4000 deliveries in 0.27s (15,058 deliveries/s)

═══════════════════════════════════════════════════════
Results: 2/2 passed
Runtime: 0.27s (7.4 assertions/sec)
All tests passed!
Slowest sections:
  0.27s  2/2  Pub/Sub: fan-out under concurrent publishers
═══════════════════════════════════════════════════════

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    0.74s
  Throughput: 5381 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 1.36ms
  Latency min: 0.03ms
  Latency max: 40.29ms
  Latency p50: 0.30ms
  Latency p95: 6.08ms
  Latency p99: 22.13ms
  No errors!
  Operation mix:
    zrank                 127 ok     0 errors
    sadd                  120 ok     0 errors
    set                   118 ok     0 errors
    zscore                117 ok     0 errors
    zrevquery             115 ok     0 errors
    info                  115 ok     0 errors
    del                   114 ok     0 errors
    smembers              112 ok     0 errors
    keyspace_scan         112 ok     0 errors
    memory_usage          111 ok     0 errors
    mset                  110 ok     0 errors
    getdel                109 ok     0 errors
  Slowest operations by average latency:
    keys                  18.78ms avg over 105 ops
    keyspace_scan         14.03ms avg over 112 ops
    sscan                  1.68ms avg over 105 ops
    hscan                  1.64ms avg over 88 ops
    hgetall                1.45ms avg over 109 ops
    smembers               1.33ms avg over 112 ops
    zrevquery              0.84ms avg over 115 ops
    ttl_triplet            0.84ms avg over 109 ops
    lrange                 0.80ms avg over 89 ops
    zquery                 0.76ms avg over 105 ops
    list_pop_trim          0.67ms avg over 93 ops
    srandmember            0.62ms avg over 89 ops
  ℹ  cleaned 162 leftover keys

═══════════════════════════════════════════════════════
  Speed baseline (redis-benchmark)
═══════════════════════════════════════════════════════
  PING_INLINE: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  PING_INLINE: 1282051.25 requests per second, p50=0.199 msec
  PING_MBULK: 1515151.50 requests per second, p50=0.207 msec
  SET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SET: 1408450.62 requests per second, p50=0.391 msec
  GET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  GET: 1428571.38 requests per second, p50=0.383 msec
  INCR: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  INCR: 1136363.62 requests per second, p50=0.423 msec
  LPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH: 1190476.25 requests per second, p50=0.407 msec
  RPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPUSH: 1298701.25 requests per second, p50=0.415 msec
  LPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPOP: 1298701.25 requests per second, p50=0.391 msec
  RPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPOP: 1204819.38 requests per second, p50=0.439 msec
  SADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SADD: 1162790.62 requests per second, p50=0.463 msec
  HSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  HSET: 1162790.62 requests per second, p50=0.487 msec
  SPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SPOP: 1282051.25 requests per second, p50=0.311 msec
  ZADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZADD: 1136363.62 requests per second, p50=0.503 msec
  ZPOPMIN: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZPOPMIN: 1298701.25 requests per second, p50=0.423 msec
  LPUSH (needed to benchmark LRANGE): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH (needed to benchmark LRANGE): 1136363.62 requests per second, p50=0.455 msec
  LRANGE_100 (first 100 elements): rps=112127.5 (overall: 166532.5) avg_msec=4.354 (overall: 4.354) 28144 requests
  LRANGE_100 (first 100 elements): rps=189952.0 (overall: 180506.0) avg_msec=4.166 (overall: 4.236) 75632 requests
  LRANGE_100 (first 100 elements): 184501.84 requests per second, p50=4.055 msec
  LRANGE_300 (first 300 elements): rps=22191.2 (overall: 47203.4) avg_msec=5.756 (overall: 5.756) 5570 requests
  LRANGE_300 (first 300 elements): rps=56880.0 (overall: 53777.2) avg_msec=4.700 (overall: 4.997) 19790 requests
  LRANGE_300 (first 300 elements): rps=54956.2 (overall: 54255.2) avg_msec=4.940 (overall: 4.974) 33584 requests
  LRANGE_300 (first 300 elements): rps=57761.0 (overall: 55266.7) avg_msec=4.731 (overall: 4.901) 48082 requests
  LRANGE_300 (first 300 elements): rps=56392.0 (overall: 55517.9) avg_msec=4.718 (overall: 4.859) 62180 requests
  LRANGE_300 (first 300 elements): rps=59376.0 (overall: 56221.9) avg_msec=4.609 (overall: 4.811) 77024 requests
  LRANGE_300 (first 300 elements): rps=55386.5 (overall: 56092.5) avg_msec=4.821 (overall: 4.813) 90926 requests
  LRANGE_300 (first 300 elements): 56211.35 requests per second, p50=4.359 msec
  LRANGE_500 (first 500 elements): rps=6436.0 (overall: 19385.5) avg_msec=11.086 (overall: 11.086) 1609 requests
  LRANGE_500 (first 500 elements): rps=27116.0 (overall: 25189.2) avg_msec=8.081 (overall: 8.657) 8388 requests
  LRANGE_500 (first 500 elements): rps=28533.9 (overall: 26626.7) avg_msec=7.773 (overall: 8.250) 15550 requests
  LRANGE_500 (first 500 elements): rps=27508.0 (overall: 26890.9) avg_msec=8.154 (overall: 8.220) 22427 requests
  LRANGE_500 (first 500 elements): rps=28252.0 (overall: 27204.8) avg_msec=7.853 (overall: 8.132) 29490 requests
  LRANGE_500 (first 500 elements): rps=28684.0 (overall: 27482.0) avg_msec=8.063 (overall: 8.119) 36661 requests
  LRANGE_500 (first 500 elements): rps=27892.4 (overall: 27547.0) avg_msec=8.069 (overall: 8.111) 43662 requests
  LRANGE_500 (first 500 elements): rps=27784.0 (overall: 27579.3) avg_msec=7.849 (overall: 8.075) 50608 requests
  LRANGE_500 (first 500 elements): rps=28302.8 (overall: 27666.3) avg_msec=7.865 (overall: 8.049) 57712 requests
  LRANGE_500 (first 500 elements): rps=26880.5 (overall: 27581.9) avg_msec=8.016 (overall: 8.046) 64459 requests
  LRANGE_500 (first 500 elements): rps=29252.0 (overall: 27743.3) avg_msec=7.920 (overall: 8.033) 71772 requests
  LRANGE_500 (first 500 elements): rps=28200.8 (overall: 27784.2) avg_msec=7.775 (overall: 8.009) 78935 requests
  LRANGE_500 (first 500 elements): rps=27132.0 (overall: 27731.5) avg_msec=8.258 (overall: 8.029) 85718 requests
  LRANGE_500 (first 500 elements): rps=28293.7 (overall: 27773.9) avg_msec=7.945 (overall: 8.023) 92848 requests
  LRANGE_500 (first 500 elements): rps=28374.5 (overall: 27815.8) avg_msec=7.729 (overall: 8.002) 99970 requests
  LRANGE_500 (first 500 elements): 27824.15 requests per second, p50=7.839 msec
  LRANGE_600 (first 600 elements): rps=19672.0 (overall: 20406.6) avg_msec=10.463 (overall: 10.463) 4918 requests
  LRANGE_600 (first 600 elements): rps=22370.5 (overall: 21408.5) avg_msec=9.758 (overall: 10.087) 10533 requests
  LRANGE_600 (first 600 elements): rps=22488.0 (overall: 21772.2) avg_msec=9.783 (overall: 9.981) 16155 requests
  LRANGE_600 (first 600 elements): rps=22388.0 (overall: 21927.4) avg_msec=9.809 (overall: 9.937) 21752 requests
  LRANGE_600 (first 600 elements): rps=22370.5 (overall: 22016.9) avg_msec=9.625 (overall: 9.873) 27367 requests
  LRANGE_600 (first 600 elements): rps=22340.0 (overall: 22071.0) avg_msec=9.947 (overall: 9.886) 32952 requests
  LRANGE_600 (first 600 elements): rps=22310.8 (overall: 22105.5) avg_msec=9.877 (overall: 9.884) 38552 requests
  LRANGE_600 (first 600 elements): rps=22352.0 (overall: 22136.4) avg_msec=9.670 (overall: 9.857) 44140 requests
  LRANGE_600 (first 600 elements): rps=22376.0 (overall: 22163.1) avg_msec=9.840 (overall: 9.855) 49734 requests
  LRANGE_600 (first 600 elements): rps=22388.0 (overall: 22185.6) avg_msec=9.657 (overall: 9.835) 55331 requests
  LRANGE_600 (first 600 elements): rps=22274.9 (overall: 22193.8) avg_msec=9.715 (overall: 9.824) 60922 requests
  LRANGE_600 (first 600 elements): rps=22328.0 (overall: 22205.0) avg_msec=9.898 (overall: 9.830) 66504 requests
  LRANGE_600 (first 600 elements): rps=22251.0 (overall: 22208.6) avg_msec=9.793 (overall: 9.827) 72089 requests
  LRANGE_600 (first 600 elements): rps=22134.9 (overall: 22203.3) avg_msec=9.852 (overall: 9.829) 77667 requests
  LRANGE_600 (first 600 elements): rps=22552.0 (overall: 22226.5) avg_msec=9.773 (overall: 9.825) 83305 requests
  LRANGE_600 (first 600 elements): rps=22753.0 (overall: 22259.6) avg_msec=9.879 (overall: 9.829) 89016 requests
  LRANGE_600 (first 600 elements): rps=22256.0 (overall: 22259.4) avg_msec=9.937 (overall: 9.835) 94580 requests
  LRANGE_600 (first 600 elements): 22251.89 requests per second, p50=9.719 msec
  MSET (10 keys): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  MSET (10 keys): 636942.62 requests per second, p50=1.079 msec

-- Command Metrics -------------------------------------
  Commands observed: 18929
  RESP errors:       111 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.34ms
  Latency p50/p95/p99: 0.02/0.74/8.01ms
  Latency max:       40.28ms
  Most used commands:
    set              9341 calls
    zadd             1821 calls
    publish          1009 calls
    get               787 calls
    del               699 calls
    srandmember       345 calls
    rpush             317 calls
    sadd              251 calls
    hset              204 calls
    info              149 calls
    zrank             133 calls
    zscore            129 calls
  Slowest commands by average latency:
    keys              18.43ms avg over 107 calls
    scan              12.88ms avg over 115 calls
    save              10.13ms avg over 2 calls
    acl                3.12ms avg over 16 calls
    bgsave             3.00ms avg over 2 calls
    sscan              1.62ms avg over 109 calls
    hscan              1.56ms avg over 92 calls
    hgetall            1.42ms avg over 111 calls
    smembers           1.25ms avg over 119 calls
    zrevquery          0.83ms avg over 117 calls
    zquery             0.72ms avg over 111 calls
    lrange             0.72ms avg over 100 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
  correctness + concurrency + stress + redis-benchmark over TLS (passwordless) → 127.0.0.1:1337
  Results saved to docs/bench_tls.md
═══════════════════════════════════════════════════════


```
