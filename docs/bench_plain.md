# MYRED stress test — 2026-07-25 20:19:04

**Run:** correctness + concurrency + stress + redis-benchmark over plaintext (passwordless) → 127.0.0.1:1336

```
(logging output to docs/bench_plain.md)
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + stress + redis-benchmark over plaintext (passwordless) → 127.0.0.1:1336
═══════════════════════════════════════════════════════
  Target:    127.0.0.1:1336
  Transport: plaintext
  Auth:      none
  Log:       docs/bench_plain.md
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
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:11\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:4\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:70197248\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:0\r\n\r\n# Stats\r\ntotal_commands:2743\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:1874\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:11
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:4
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:70197248
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:0
    # Stats
    total_commands:2743
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:1874
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
  ℹ  bgsave returned in 1.1ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 5.0ms
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
Runtime: 12.64s (47.4 assertions/sec)
All tests passed!
Slowest sections:
  7.92s  11/11  Pub/Sub: keyspace notifications (V8.3)
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.53s  9/9  UNLINK Command (async delete)
  0.52s  7/7  BGSAVE (fork-based background save)
  0.51s  6/6  Persistence Round-trip (in-memory)
  0.51s  4/4  SAVE / RDB Persistence
  0.51s  16/16  Pub/Sub: SUBSCRIBE / UNSUBSCRIBE / PUBLISH (V8.1)
  0.41s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Pub/Sub: fan-out under concurrent publishers ──────
  ✓ no publisher/reader errors
  ✓ every subscriber received every message
    4 publishers × 250 msgs → 4 subscribers = 4000 deliveries in 0.34s (11,665 deliveries/s)

═══════════════════════════════════════════════════════
Results: 2/2 passed
Runtime: 0.34s (5.8 assertions/sec)
All tests passed!
Slowest sections:
  0.34s  2/2  Pub/Sub: fan-out under concurrent publishers
═══════════════════════════════════════════════════════

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    1.10s
  Throughput: 3626 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 2.07ms
  Latency min: 0.03ms
  Latency max: 43.82ms
  Latency p50: 0.28ms
  Latency p95: 13.76ms
  Latency p99: 38.42ms
  No errors!
  Operation mix:
    incr                  121 ok     0 errors
    setnx                 118 ok     0 errors
    hgetall               118 ok     0 errors
    keyspace_scan         118 ok     0 errors
    sadd                  117 ok     0 errors
    mget                  115 ok     0 errors
    zquery                114 ok     0 errors
    zscore                111 ok     0 errors
    srandmember           111 ok     0 errors
    info                  110 ok     0 errors
    lpush                 108 ok     0 errors
    sismember             108 ok     0 errors
  Slowest operations by average latency:
    keys                  34.83ms avg over 94 ops
    keyspace_scan         23.20ms avg over 118 ops
    hscan                  2.57ms avg over 102 ops
    sscan                  2.30ms avg over 101 ops
    smembers               2.14ms avg over 99 ops
    hgetall                2.06ms avg over 118 ops
    zrevquery              1.49ms avg over 102 ops
    zquery                 1.10ms avg over 114 ops
    lrange                 0.85ms avg over 98 ops
    zpopmin                0.79ms avg over 97 ops
    srandmember            0.72ms avg over 111 ops
    ttl_triplet            0.72ms avg over 79 ops
  ℹ  cleaned 162 leftover keys

═══════════════════════════════════════════════════════
  Speed baseline (redis-benchmark)
═══════════════════════════════════════════════════════
  PING_INLINE: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  PING_INLINE: 2000000.00 requests per second, p50=0.199 msec
  PING_MBULK: 2857142.75 requests per second, p50=0.159 msec
  SET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SET: 2222222.25 requests per second, p50=0.303 msec
  GET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  GET: 2040816.38 requests per second, p50=0.287 msec
  INCR: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  INCR: 2173913.00 requests per second, p50=0.327 msec
  LPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH: 2000000.00 requests per second, p50=0.343 msec
  RPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPUSH: 1538461.62 requests per second, p50=0.351 msec
  LPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPOP: 2083333.38 requests per second, p50=0.319 msec
  RPOP: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  RPOP: 2083333.38 requests per second, p50=0.303 msec
  SADD: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  SADD: 2083333.38 requests per second, p50=0.359 msec
  HSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  HSET: 1960784.38 requests per second, p50=0.359 msec
  SPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SPOP: 2702702.75 requests per second, p50=0.231 msec
  ZADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZADD: 1923076.88 requests per second, p50=0.399 msec
  ZPOPMIN: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZPOPMIN: 2173913.00 requests per second, p50=0.287 msec
  LPUSH (needed to benchmark LRANGE): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH (needed to benchmark LRANGE): 1960784.38 requests per second, p50=0.343 msec
  LRANGE_100 (first 100 elements): rps=166183.3 (overall: 208560.0) avg_msec=3.737 (overall: 3.737) 41712 requests
  LRANGE_100 (first 100 elements): rps=216000.0 (overall: 212693.3) avg_msec=3.661 (overall: 3.695) 95712 requests
  LRANGE_100 (first 100 elements): 212765.95 requests per second, p50=3.599 msec
  LRANGE_300 (first 300 elements): rps=67544.0 (overall: 73738.0) avg_msec=10.401 (overall: 10.401) 16886 requests
  LRANGE_300 (first 300 elements): rps=73394.4 (overall: 73558.3) avg_msec=10.668 (overall: 10.541) 35308 requests
  LRANGE_300 (first 300 elements): rps=73600.0 (overall: 73572.6) avg_msec=10.678 (overall: 10.588) 53708 requests
  LRANGE_300 (first 300 elements): rps=73128.0 (overall: 73459.2) avg_msec=10.750 (overall: 10.629) 71990 requests
  LRANGE_300 (first 300 elements): rps=72936.0 (overall: 73352.8) avg_msec=10.793 (overall: 10.662) 90224 requests
  LRANGE_300 (first 300 elements): 73421.44 requests per second, p50=10.495 msec
  LRANGE_500 (first 500 elements): rps=20940.0 (overall: 44743.6) avg_msec=16.255 (overall: 16.255) 5235 requests
  LRANGE_500 (first 500 elements): rps=45119.5 (overall: 45000.0) avg_msec=17.337 (overall: 16.995) 16560 requests
  LRANGE_500 (first 500 elements): rps=43788.0 (overall: 44509.7) avg_msec=17.929 (overall: 17.366) 27507 requests
  LRANGE_500 (first 500 elements): rps=43756.0 (overall: 44292.6) avg_msec=17.959 (overall: 17.535) 38446 requests
  LRANGE_500 (first 500 elements): rps=45104.0 (overall: 44474.1) avg_msec=17.438 (overall: 17.513) 49722 requests
  LRANGE_500 (first 500 elements): rps=45832.7 (overall: 44723.2) avg_msec=17.094 (overall: 17.434) 61226 requests
  LRANGE_500 (first 500 elements): rps=45812.0 (overall: 44891.3) avg_msec=17.136 (overall: 17.387) 72679 requests
  LRANGE_500 (first 500 elements): rps=42884.0 (overall: 44622.8) avg_msec=17.219 (overall: 17.366) 83400 requests
  LRANGE_500 (first 500 elements): rps=46792.0 (overall: 44878.7) avg_msec=13.246 (overall: 16.859) 95098 requests
  LRANGE_500 (first 500 elements): 44903.46 requests per second, p50=16.847 msec
  LRANGE_600 (first 600 elements): rps=20892.4 (overall: 36929.6) avg_msec=19.491 (overall: 19.491) 5244 requests
  LRANGE_600 (first 600 elements): rps=38172.0 (overall: 37721.9) avg_msec=20.544 (overall: 20.170) 14787 requests
  LRANGE_600 (first 600 elements): rps=37540.0 (overall: 37651.1) avg_msec=20.967 (overall: 20.480) 24172 requests
  LRANGE_600 (first 600 elements): rps=38172.0 (overall: 37797.1) avg_msec=20.569 (overall: 20.505) 33715 requests
  LRANGE_600 (first 600 elements): rps=38004.0 (overall: 37842.5) avg_msec=20.612 (overall: 20.528) 43254 requests
  LRANGE_600 (first 600 elements): rps=38116.0 (overall: 37891.6) avg_msec=20.600 (overall: 20.541) 52783 requests
  LRANGE_600 (first 600 elements): rps=37764.0 (overall: 37872.2) avg_msec=20.798 (overall: 20.580) 62224 requests
  LRANGE_600 (first 600 elements): rps=37900.0 (overall: 37875.9) avg_msec=20.714 (overall: 20.598) 71699 requests
  LRANGE_600 (first 600 elements): rps=37812.8 (overall: 37868.5) avg_msec=20.688 (overall: 20.608) 81190 requests
  LRANGE_600 (first 600 elements): rps=37912.0 (overall: 37873.0) avg_msec=20.707 (overall: 20.619) 90668 requests
  LRANGE_600 (first 600 elements): 37864.45 requests per second, p50=20.239 msec
  MSET (10 keys): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  MSET (10 keys): 869565.19 requests per second, p50=0.887 msec

-- Command Metrics -------------------------------------
  Commands observed: 18920
  RESP errors:       111 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.49ms
  Latency p50/p95/p99: 0.02/0.84/16.49ms
  Latency max:       43.81ms
  Most used commands:
    set              9322 calls
    zadd             1813 calls
    publish          1009 calls
    get               801 calls
    del               680 calls
    srandmember       367 calls
    rpush             298 calls
    sadd              248 calls
    hset              216 calls
    info              144 calls
    incr              128 calls
    exists            125 calls
  Slowest commands by average latency:
    keys              34.11ms avg over 96 calls
    scan              21.92ms avg over 121 calls
    save              10.37ms avg over 2 calls
    acl                3.20ms avg over 16 calls
    hscan              2.48ms avg over 106 calls
    sscan              2.21ms avg over 105 calls
    hgetall            2.02ms avg over 120 calls
    smembers           2.00ms avg over 106 calls
    zrevquery          1.46ms avg over 104 calls
    bgsave             1.13ms avg over 2 calls
    zquery             1.05ms avg over 120 calls
    lrange             0.76ms avg over 109 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
  correctness + concurrency + stress + redis-benchmark over plaintext (passwordless) → 127.0.0.1:1336
  Results saved to docs/bench_plain.md
═══════════════════════════════════════════════════════


```
