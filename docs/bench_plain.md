# MYRED stress test — 2026-07-21 14:33:48

```
(logging output to docs/bench_plain.md)
═══════════════════════════════════════════════════════
  Redis Server RESP Stress Test
  Connecting to 127.0.0.1:1336
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
  ℹ  returned in 0.1ms

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
  ✓ config get * -> array → ['maxmemory', '0', 'maxmemory-policy', 'noeviction']
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
  ✓ acl list -> array → ['user default on nopass ~* +@all']
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

── INFO Command ──────────────────────────────────────
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:101\r\nuptime_minutes:1\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:1045\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:146485248\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:4227\r\n\r\n# Stats\r\ntotal_commands:2020668\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:4450\r\nrdb_changes_since_save:1804932\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:115\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:101
    uptime_minutes:1
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:1045
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:146485248
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:4227
    # Stats
    total_commands:2020668
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:4450
    rdb_changes_since_save:1804932
    rdb_last_save_ok:1
    rdb_last_save_size_bytes:115
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
  ℹ  bgsave returned in 3.4ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 12.5ms
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

── Persistence Round-trip (in-memory) ────────────────
  ✓ save → OK
  ✓ string still readable
  ✓ zset alice still readable → 10
  ✓ zset bob still readable → 20
  ✓ zrank alice → 0
  ✓ ttl preserved after save

═══════════════════════════════════════════════════════
Results: 551/551 passed
Runtime: 4.03s (136.7 assertions/sec)
All tests passed!
Slowest sections:
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.54s  9/9  UNLINK Command (async delete)
  0.53s  7/7  BGSAVE (fork-based background save)
  0.51s  6/6  Persistence Round-trip (in-memory)
  0.51s  4/4  SAVE / RDB Persistence
  0.41s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
  0.31s  2/2  Memory: incremental eviction (EVICT_RUNNING semantics)
  0.30s  7/7  INFO: O(1) keyspace stats (heap-backed keys_with_ttl)
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    1.54s
  Throughput: 2597 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 2.86ms
  Latency min: 0.02ms
  Latency max: 65.01ms
  Latency p50: 0.39ms
  Latency p95: 18.24ms
  Latency p99: 53.92ms
  No errors!
  Operation mix:
    getex_px              124 ok     0 errors
    set                   123 ok     0 errors
    keyspace_scan         121 ok     0 errors
    smembers              117 ok     0 errors
    memory_usage          113 ok     0 errors
    get                   113 ok     0 errors
    ping                  112 ok     0 errors
    object_encoding       111 ok     0 errors
    incr                  110 ok     0 errors
    del                   110 ok     0 errors
    mset                  109 ok     0 errors
    append                109 ok     0 errors
  Slowest operations by average latency:
    keys                  48.42ms avg over 88 ops
    keyspace_scan         32.35ms avg over 121 ops
    hscan                  4.09ms avg over 109 ops
    hgetall                3.48ms avg over 109 ops
    sscan                  3.11ms avg over 98 ops
    smembers               2.68ms avg over 117 ops
    zrevquery              2.22ms avg over 104 ops
    zquery                 1.76ms avg over 89 ops
    lrange                 1.13ms avg over 108 ops
    zpopmin                1.13ms avg over 90 ops
    srandmember            0.99ms avg over 106 ops
    mget                   0.99ms avg over 82 ops
  ℹ  cleaned 168 leftover keys

═══════════════════════════════════════════════════════
  Speed baseline (redis-benchmark)
═══════════════════════════════════════════════════════
  PING_INLINE: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  PING_INLINE: 1000000.00 requests per second, p50=0.447 msec
  PING_MBULK: 1428571.38 requests per second, p50=0.271 msec
  SET: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  SET: 1250000.00 requests per second, p50=0.375 msec
  GET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  GET: 840336.12 requests per second, p50=0.663 msec
  INCR: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  INCR: 1149425.38 requests per second, p50=0.479 msec
  LPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH: 1162790.62 requests per second, p50=0.455 msec
  RPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPUSH: 1098901.12 requests per second, p50=0.471 msec
  LPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPOP: 1282051.25 requests per second, p50=0.439 msec
  RPOP: rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  RPOP: 1136363.62 requests per second, p50=0.511 msec
  SADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SADD: 1234567.88 requests per second, p50=0.479 msec
  HSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  HSET: 917431.19 requests per second, p50=0.807 msec
  SPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SPOP: 1492537.25 requests per second, p50=0.359 msec
  ZADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZADD: 961538.44 requests per second, p50=0.559 msec
  ZPOPMIN: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZPOPMIN: 1075268.75 requests per second, p50=0.687 msec
  LPUSH (needed to benchmark LRANGE): rps=0.0 (overall: -nan) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH (needed to benchmark LRANGE): 1176470.62 requests per second, p50=0.471 msec
  LRANGE_100 (first 100 elements): rps=101418.3 (overall: 155219.5) avg_msec=5.010 (overall: 5.010) 25456 requests
  LRANGE_100 (first 100 elements): rps=156544.0 (overall: 156019.3) avg_msec=5.051 (overall: 5.035) 64592 requests
  LRANGE_100 (first 100 elements): 156006.25 requests per second, p50=4.943 msec
  LRANGE_300 (first 300 elements): rps=4510.0 (overall: 49217.4) avg_msec=10.197 (overall: 10.197) 1132 requests
  LRANGE_300 (first 300 elements): rps=56064.0 (overall: 55487.2) avg_msec=14.050 (overall: 13.762) 15148 requests
  LRANGE_300 (first 300 elements): rps=56208.0 (overall: 55831.7) avg_msec=14.004 (overall: 13.879) 29200 requests
  LRANGE_300 (first 300 elements): rps=56304.0 (overall: 55984.5) avg_msec=13.973 (overall: 13.909) 43276 requests
  LRANGE_300 (first 300 elements): rps=55944.2 (overall: 55974.6) avg_msec=14.007 (overall: 13.933) 57318 requests
  LRANGE_300 (first 300 elements): rps=56280.0 (overall: 56034.5) avg_msec=13.991 (overall: 13.945) 71388 requests
  LRANGE_300 (first 300 elements): rps=55744.0 (overall: 55986.9) avg_msec=14.101 (overall: 13.970) 85324 requests
  LRANGE_300 (first 300 elements): rps=55968.1 (overall: 55984.2) avg_msec=14.016 (overall: 13.977) 99372 requests
  LRANGE_300 (first 300 elements): 55991.04 requests per second, p50=13.703 msec
  LRANGE_500 (first 500 elements): rps=31552.0 (overall: 33142.9) avg_msec=22.449 (overall: 22.449) 7888 requests
  LRANGE_500 (first 500 elements): rps=34024.0 (overall: 33594.3) avg_msec=23.050 (overall: 22.761) 16394 requests
  LRANGE_500 (first 500 elements): rps=33444.0 (overall: 33543.4) avg_msec=23.486 (overall: 23.006) 24755 requests
  LRANGE_500 (first 500 elements): rps=33812.8 (overall: 33611.7) avg_msec=23.152 (overall: 23.043) 33242 requests
  LRANGE_500 (first 500 elements): rps=33984.0 (overall: 33686.8) avg_msec=23.117 (overall: 23.058) 41738 requests
  LRANGE_500 (first 500 elements): rps=33936.0 (overall: 33728.7) avg_msec=23.133 (overall: 23.071) 50222 requests
  LRANGE_500 (first 500 elements): rps=33792.0 (overall: 33737.8) avg_msec=23.223 (overall: 23.092) 58670 requests
  LRANGE_500 (first 500 elements): rps=33757.0 (overall: 33740.2) avg_msec=23.137 (overall: 23.098) 67143 requests
  LRANGE_500 (first 500 elements): rps=34032.0 (overall: 33772.8) avg_msec=23.103 (overall: 23.099) 75651 requests
  LRANGE_500 (first 500 elements): rps=33920.0 (overall: 33787.6) avg_msec=23.179 (overall: 23.107) 84131 requests
  LRANGE_500 (first 500 elements): rps=33784.9 (overall: 33787.3) avg_msec=23.174 (overall: 23.113) 92611 requests
  LRANGE_500 (first 500 elements): 33806.62 requests per second, p50=22.623 msec
  LRANGE_600 (first 600 elements): rps=2828.0 (overall: 22806.5) avg_msec=15.365 (overall: 15.365) 707 requests
  LRANGE_600 (first 600 elements): rps=28300.0 (overall: 27694.0) avg_msec=27.981 (overall: 26.835) 7782 requests
  LRANGE_600 (first 600 elements): rps=28504.0 (overall: 28075.3) avg_msec=27.570 (overall: 27.186) 14908 requests
  LRANGE_600 (first 600 elements): rps=28302.8 (overall: 28148.3) avg_msec=27.572 (overall: 27.311) 22012 requests
  LRANGE_600 (first 600 elements): rps=28508.0 (overall: 28235.5) avg_msec=27.551 (overall: 27.369) 29139 requests
  LRANGE_600 (first 600 elements): rps=28480.0 (overall: 28283.2) avg_msec=27.510 (overall: 27.397) 36259 requests
  LRANGE_600 (first 600 elements): rps=28402.4 (overall: 28302.7) avg_msec=27.532 (overall: 27.419) 43388 requests
  LRANGE_600 (first 600 elements): rps=28520.0 (overall: 28333.1) avg_msec=27.517 (overall: 27.433) 50518 requests
  LRANGE_600 (first 600 elements): rps=28468.0 (overall: 28349.7) avg_msec=27.555 (overall: 27.448) 57635 requests
  LRANGE_600 (first 600 elements): rps=28378.5 (overall: 28352.9) avg_msec=27.502 (overall: 27.454) 64758 requests
  LRANGE_600 (first 600 elements): rps=28520.0 (overall: 28369.4) avg_msec=27.460 (overall: 27.454) 71888 requests
  LRANGE_600 (first 600 elements): rps=28540.0 (overall: 28384.7) avg_msec=27.615 (overall: 27.469) 79023 requests
  LRANGE_600 (first 600 elements): rps=28496.0 (overall: 28393.9) avg_msec=27.527 (overall: 27.474) 86147 requests
  LRANGE_600 (first 600 elements): rps=28366.5 (overall: 28391.8) avg_msec=25.763 (overall: 27.343) 93267 requests
  LRANGE_600 (first 600 elements): 28401.02 requests per second, p50=26.959 msec
  MSET (10 keys): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  MSET (10 keys): 584795.31 requests per second, p50=1.279 msec

-- Command Metrics -------------------------------------
  Commands observed: 17993
  RESP errors:       105 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.69ms
  Latency p50/p95/p99: 0.03/1.29/25.73ms
  Latency max:       65.00ms
  Most used commands:
    set              9396 calls
    zadd             1817 calls
    get               807 calls
    del               697 calls
    srandmember       362 calls
    rpush             318 calls
    sadd              234 calls
    hset              209 calls
    info              133 calls
    getex             132 calls
    exists            128 calls
    type              128 calls
  Slowest commands by average latency:
    keys              47.35ms avg over 90 calls
    scan              30.60ms avg over 124 calls
    save               7.69ms avg over 2 calls
    hscan              3.94ms avg over 113 calls
    acl                3.63ms avg over 12 calls
    bgsave             3.44ms avg over 2 calls
    hgetall            3.41ms avg over 111 calls
    sscan              2.99ms avg over 102 calls
    smembers           2.52ms avg over 124 calls
    zrevquery          2.18ms avg over 106 calls
    zquery             1.66ms avg over 95 calls
    zpopmin            1.07ms avg over 95 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
═══════════════════════════════════════════════════════


```
