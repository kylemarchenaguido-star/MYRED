# MYRED stress test — 2026-07-21 14:32:47

```
(logging output to docs/bench_tls.md)
═══════════════════════════════════════════════════════
  Redis Server RESP Stress Test
  Connecting to 127.0.0.1:1337
  Using TLS (insecure — cert not verified)
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
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:40\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:3\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:51433472\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:0\r\n\r\n# Stats\r\ntotal_commands:2730\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:4407\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:40
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:3
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:51433472
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:0
    # Stats
    total_commands:2730
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:4407
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
  ℹ  bgsave returned in 0.9ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 14.1ms
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

── Persistence Round-trip (in-memory) ────────────────
  ✓ save → OK
  ✓ string still readable
  ✓ zset alice still readable → 10
  ✓ zset bob still readable → 20
  ✓ zrank alice → 0
  ✓ ttl preserved after save

═══════════════════════════════════════════════════════
Results: 551/551 passed
Runtime: 4.16s (132.4 assertions/sec)
All tests passed!
Slowest sections:
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.56s  9/9  UNLINK Command (async delete)
  0.53s  7/7  BGSAVE (fork-based background save)
  0.52s  6/6  Persistence Round-trip (in-memory)
  0.51s  4/4  SAVE / RDB Persistence
  0.42s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
  0.33s  2/2  Memory: incremental eviction (EVICT_RUNNING semantics)
  0.30s  7/7  INFO: O(1) keyspace stats (heap-backed keys_with_ttl)
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    0.96s
  Throughput: 4152 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 1.77ms
  Latency min: 0.03ms
  Latency max: 36.90ms
  Latency p50: 0.40ms
  Latency p95: 7.22ms
  Latency p99: 29.94ms
  No errors!
  Operation mix:
    info                  139 ok     0 errors
    sadd                  125 ok     0 errors
    mset                  119 ok     0 errors
    strlen                118 ok     0 errors
    lpush                 117 ok     0 errors
    hgetall               117 ok     0 errors
    srem                  111 ok     0 errors
    ping                  110 ok     0 errors
    getex_px              107 ok     0 errors
    zpopmin               107 ok     0 errors
    zquery                106 ok     0 errors
    hscan                 105 ok     0 errors
  Slowest operations by average latency:
    keys                  26.62ms avg over 104 ops
    keyspace_scan         18.52ms avg over 105 ops
    hscan                  2.37ms avg over 105 ops
    hgetall                2.16ms avg over 117 ops
    sscan                  1.94ms avg over 97 ops
    smembers               1.66ms avg over 87 ops
    zrevquery              1.29ms avg over 89 ops
    lrange                 0.98ms avg over 87 ops
    zquery                 0.95ms avg over 106 ops
    ttl_triplet            0.91ms avg over 99 ops
    list_pop_trim          0.82ms avg over 104 ops
    mget                   0.70ms avg over 93 ops
  ℹ  cleaned 169 leftover keys

═══════════════════════════════════════════════════════
  Speed baseline (redis-benchmark)
═══════════════════════════════════════════════════════
  PING_INLINE: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  PING_INLINE: 990099.00 requests per second, p50=0.295 msec
  PING_MBULK: 1020408.19 requests per second, p50=0.295 msec
  SET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SET: 900900.88 requests per second, p50=0.519 msec
  GET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  GET: 970873.81 requests per second, p50=0.511 msec
  INCR: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  INCR: 806451.62 requests per second, p50=0.671 msec
  LPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH: 636942.62 requests per second, p50=0.663 msec
  RPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPUSH: 757575.75 requests per second, p50=0.615 msec
  LPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPOP: 917431.19 requests per second, p50=0.591 msec
  RPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  RPOP: 729927.06 requests per second, p50=0.599 msec
  SADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SADD: 826446.31 requests per second, p50=0.639 msec
  HSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  HSET: 833333.38 requests per second, p50=0.663 msec
  SPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  SPOP: 925925.88 requests per second, p50=0.343 msec
  ZADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZADD: 833333.38 requests per second, p50=0.671 msec
  ZPOPMIN: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  ZPOPMIN: 917431.19 requests per second, p50=0.559 msec
  LPUSH (needed to benchmark LRANGE): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LPUSH (needed to benchmark LRANGE): 847457.62 requests per second, p50=0.623 msec
  LRANGE_100 (first 100 elements): rps=66304.0 (overall: 109774.8) avg_msec=6.348 (overall: 6.348) 16576 requests
  LRANGE_100 (first 100 elements): rps=138709.2 (overall: 127840.8) avg_msec=5.682 (overall: 5.897) 51392 requests
  LRANGE_100 (first 100 elements): rps=139520.0 (overall: 132319.0) avg_msec=5.668 (overall: 5.804) 86272 requests
  LRANGE_100 (first 100 elements): 133333.33 requests per second, p50=5.543 msec
  LRANGE_300 (first 300 elements): rps=14624.0 (overall: 26302.2) avg_msec=11.153 (overall: 11.153) 3656 requests
  LRANGE_300 (first 300 elements): rps=35123.5 (overall: 31979.5) avg_msec=7.303 (overall: 8.432) 12472 requests
  LRANGE_300 (first 300 elements): rps=36744.0 (overall: 33840.6) avg_msec=7.177 (overall: 7.899) 21658 requests
  LRANGE_300 (first 300 elements): rps=29149.6 (overall: 32507.8) avg_msec=9.041 (overall: 8.190) 29062 requests
  LRANGE_300 (first 300 elements): rps=35094.5 (overall: 33080.1) avg_msec=7.396 (overall: 8.004) 37976 requests
  LRANGE_300 (first 300 elements): rps=35581.0 (overall: 33531.8) avg_msec=7.671 (overall: 7.940) 46978 requests
  LRANGE_300 (first 300 elements): rps=35402.4 (overall: 33816.0) avg_msec=7.273 (overall: 7.834) 55864 requests
  LRANGE_300 (first 300 elements): rps=36784.0 (overall: 34206.1) avg_msec=7.166 (overall: 7.739) 65060 requests
  LRANGE_300 (first 300 elements): rps=33474.1 (overall: 34120.8) avg_msec=7.918 (overall: 7.760) 73462 requests
  LRANGE_300 (first 300 elements): rps=29368.0 (overall: 33626.3) avg_msec=8.854 (overall: 7.859) 80804 requests
  LRANGE_300 (first 300 elements): rps=35936.3 (overall: 33844.8) avg_msec=7.738 (overall: 7.847) 89824 requests
  LRANGE_300 (first 300 elements): rps=36422.3 (overall: 34067.5) avg_msec=6.909 (overall: 7.760) 98966 requests
  LRANGE_300 (first 300 elements): 34083.16 requests per second, p50=7.103 msec
  LRANGE_500 (first 500 elements): rps=12210.9 (overall: 14607.5) avg_msec=16.954 (overall: 16.954) 3126 requests
  LRANGE_500 (first 500 elements): rps=11000.0 (overall: 12646.1) avg_msec=17.077 (overall: 17.012) 5931 requests
  LRANGE_500 (first 500 elements): rps=11692.0 (overall: 12314.3) avg_msec=20.399 (overall: 18.130) 8854 requests
  LRANGE_500 (first 500 elements): rps=10418.3 (overall: 11823.7) avg_msec=20.175 (overall: 18.597) 11469 requests
  LRANGE_500 (first 500 elements): rps=10015.9 (overall: 11452.1) avg_msec=20.159 (overall: 18.877) 13983 requests
  LRANGE_500 (first 500 elements): rps=9761.0 (overall: 11163.7) avg_msec=21.209 (overall: 19.225) 16433 requests
  LRANGE_500 (first 500 elements): rps=9912.7 (overall: 10980.9) avg_msec=20.466 (overall: 19.389) 18931 requests
  LRANGE_500 (first 500 elements): rps=11844.6 (overall: 11090.6) avg_msec=19.688 (overall: 19.429) 21904 requests
  LRANGE_500 (first 500 elements): rps=10163.3 (overall: 10986.1) avg_msec=18.024 (overall: 19.283) 24455 requests
  LRANGE_500 (first 500 elements): rps=11766.8 (overall: 11065.8) avg_msec=18.294 (overall: 19.175) 27432 requests
  LRANGE_500 (first 500 elements): rps=10380.0 (overall: 11002.9) avg_msec=18.356 (overall: 19.105) 30027 requests
  LRANGE_500 (first 500 elements): rps=10537.8 (overall: 10963.8) avg_msec=18.783 (overall: 19.079) 32672 requests
  LRANGE_500 (first 500 elements): rps=12406.4 (overall: 11075.8) avg_msec=19.385 (overall: 19.105) 35786 requests
  LRANGE_500 (first 500 elements): rps=12992.0 (overall: 11213.4) avg_msec=18.149 (overall: 19.026) 39034 requests
  LRANGE_500 (first 500 elements): rps=12438.2 (overall: 11295.8) avg_msec=18.852 (overall: 19.013) 42156 requests
  LRANGE_500 (first 500 elements): rps=10039.8 (overall: 11216.7) avg_msec=18.357 (overall: 18.976) 44676 requests
  LRANGE_500 (first 500 elements): rps=11656.2 (overall: 11243.2) avg_msec=18.478 (overall: 18.945) 47660 requests
  LRANGE_500 (first 500 elements): rps=10824.0 (overall: 11219.9) avg_msec=18.477 (overall: 18.919) 50366 requests
  LRANGE_500 (first 500 elements): rps=11193.8 (overall: 11218.5) avg_msec=20.645 (overall: 19.013) 53254 requests
  LRANGE_500 (first 500 elements): rps=12594.6 (overall: 11289.7) avg_msec=17.332 (overall: 18.916) 56516 requests
  LRANGE_500 (first 500 elements): rps=11812.0 (overall: 11314.5) avg_msec=17.929 (overall: 18.867) 59469 requests
  LRANGE_500 (first 500 elements): rps=12521.7 (overall: 11369.9) avg_msec=19.436 (overall: 18.896) 62637 requests
  LRANGE_500 (first 500 elements): rps=11917.0 (overall: 11394.0) avg_msec=15.092 (overall: 18.721) 65652 requests
  LRANGE_500 (first 500 elements): rps=11796.8 (overall: 11410.8) avg_msec=17.337 (overall: 18.661) 68613 requests
  LRANGE_500 (first 500 elements): rps=11366.1 (overall: 11409.0) avg_msec=20.196 (overall: 18.723) 71500 requests
  LRANGE_500 (first 500 elements): rps=10761.0 (overall: 11384.0) avg_msec=18.950 (overall: 18.732) 74201 requests
  LRANGE_500 (first 500 elements): rps=11844.6 (overall: 11401.1) avg_msec=18.284 (overall: 18.714) 77174 requests
  LRANGE_500 (first 500 elements): rps=10669.3 (overall: 11374.9) avg_msec=17.772 (overall: 18.683) 79852 requests
  LRANGE_500 (first 500 elements): rps=12064.0 (overall: 11398.6) avg_msec=19.374 (overall: 18.708) 82868 requests
  LRANGE_500 (first 500 elements): rps=10386.5 (overall: 11364.8) avg_msec=18.322 (overall: 18.696) 85475 requests
  LRANGE_500 (first 500 elements): rps=11778.2 (overall: 11378.5) avg_msec=19.247 (overall: 18.715) 88502 requests
  LRANGE_500 (first 500 elements): rps=10948.0 (overall: 11365.1) avg_msec=18.781 (overall: 18.717) 91239 requests
  LRANGE_500 (first 500 elements): rps=12560.0 (overall: 11401.2) avg_msec=18.795 (overall: 18.720) 94379 requests
  LRANGE_500 (first 500 elements): rps=10684.8 (overall: 11379.6) avg_msec=18.625 (overall: 18.717) 97125 requests
  LRANGE_500 (first 500 elements): 11392.12 requests per second, p50=17.375 msec
  LRANGE_600 (first 600 elements): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  LRANGE_600 (first 600 elements): rps=12988.0 (overall: 10976.4) avg_msec=21.801 (overall: 21.801) 3260 requests
  LRANGE_600 (first 600 elements): rps=8502.0 (overall: 9833.3) avg_msec=26.190 (overall: 23.554) 5428 requests
  LRANGE_600 (first 600 elements): rps=8711.5 (overall: 9474.1) avg_msec=24.194 (overall: 23.743) 7693 requests
  LRANGE_600 (first 600 elements): rps=9043.1 (overall: 9371.1) avg_msec=23.957 (overall: 23.792) 9999 requests
  LRANGE_600 (first 600 elements): rps=9470.6 (overall: 9390.3) avg_msec=22.418 (overall: 23.525) 12414 requests
  LRANGE_600 (first 600 elements): rps=8352.0 (overall: 9225.2) avg_msec=26.986 (overall: 24.023) 14502 requests
  LRANGE_600 (first 600 elements): rps=8519.4 (overall: 9125.7) avg_msec=25.735 (overall: 24.248) 16700 requests
  LRANGE_600 (first 600 elements): rps=8624.0 (overall: 9063.7) avg_msec=24.142 (overall: 24.236) 18925 requests
  LRANGE_600 (first 600 elements): rps=8294.8 (overall: 8981.2) avg_msec=24.109 (overall: 24.223) 21007 requests
  LRANGE_600 (first 600 elements): rps=9300.0 (overall: 9012.0) avg_msec=25.014 (overall: 24.302) 23332 requests
  LRANGE_600 (first 600 elements): rps=9773.8 (overall: 9079.5) avg_msec=23.229 (overall: 24.200) 25795 requests
  LRANGE_600 (first 600 elements): rps=9513.9 (overall: 9114.8) avg_msec=23.321 (overall: 24.125) 28183 requests
  LRANGE_600 (first 600 elements): rps=9264.0 (overall: 9126.0) avg_msec=25.154 (overall: 24.203) 30499 requests
  LRANGE_600 (first 600 elements): rps=8242.1 (overall: 9064.0) avg_msec=24.335 (overall: 24.212) 32576 requests
  LRANGE_600 (first 600 elements): rps=8586.6 (overall: 9032.5) avg_msec=24.129 (overall: 24.207) 34757 requests
  LRANGE_600 (first 600 elements): rps=9095.6 (overall: 9036.4) avg_msec=24.761 (overall: 24.241) 37040 requests
  LRANGE_600 (first 600 elements): rps=9298.8 (overall: 9051.5) avg_msec=23.947 (overall: 24.223) 39374 requests
  LRANGE_600 (first 600 elements): rps=9358.6 (overall: 9068.2) avg_msec=25.257 (overall: 24.281) 41723 requests
  LRANGE_600 (first 600 elements): rps=8695.7 (overall: 9048.8) avg_msec=24.184 (overall: 24.277) 43923 requests
  LRANGE_600 (first 600 elements): rps=8738.1 (overall: 9033.5) avg_msec=25.351 (overall: 24.328) 46125 requests
  LRANGE_600 (first 600 elements): rps=10523.8 (overall: 9103.6) avg_msec=24.598 (overall: 24.343) 48777 requests
  LRANGE_600 (first 600 elements): rps=8704.3 (overall: 9085.3) avg_msec=24.158 (overall: 24.335) 51014 requests
  LRANGE_600 (first 600 elements): rps=8235.1 (overall: 9048.9) avg_msec=24.237 (overall: 24.331) 53081 requests
  LRANGE_600 (first 600 elements): rps=9302.8 (overall: 9059.3) avg_msec=26.393 (overall: 24.418) 55416 requests
  LRANGE_600 (first 600 elements): rps=9310.8 (overall: 9069.3) avg_msec=24.752 (overall: 24.431) 57753 requests
  LRANGE_600 (first 600 elements): rps=8337.3 (overall: 9041.4) avg_msec=24.002 (overall: 24.416) 59854 requests
  LRANGE_600 (first 600 elements): rps=8175.3 (overall: 9009.8) avg_msec=24.802 (overall: 24.429) 61906 requests
  LRANGE_600 (first 600 elements): rps=9191.2 (overall: 9016.1) avg_msec=24.715 (overall: 24.439) 64213 requests
  LRANGE_600 (first 600 elements): rps=9414.3 (overall: 9029.7) avg_msec=25.520 (overall: 24.478) 66576 requests
  LRANGE_600 (first 600 elements): rps=8343.8 (overall: 9006.7) avg_msec=24.144 (overall: 24.467) 68712 requests
  LRANGE_600 (first 600 elements): rps=9793.7 (overall: 9031.8) avg_msec=23.366 (overall: 24.429) 71180 requests
  LRANGE_600 (first 600 elements): rps=8474.7 (overall: 9014.3) avg_msec=24.827 (overall: 24.441) 73358 requests
  LRANGE_600 (first 600 elements): rps=8690.5 (overall: 9004.5) avg_msec=24.739 (overall: 24.449) 75548 requests
  LRANGE_600 (first 600 elements): rps=9298.8 (overall: 9013.1) avg_msec=24.750 (overall: 24.458) 77882 requests
  LRANGE_600 (first 600 elements): rps=8486.4 (overall: 8997.9) avg_msec=24.796 (overall: 24.468) 80063 requests
  LRANGE_600 (first 600 elements): rps=8464.0 (overall: 8983.3) avg_msec=23.907 (overall: 24.453) 82179 requests
  LRANGE_600 (first 600 elements): rps=8654.8 (overall: 8974.5) avg_msec=24.021 (overall: 24.442) 84360 requests
  LRANGE_600 (first 600 elements): rps=9346.6 (overall: 8984.1) avg_msec=24.710 (overall: 24.449) 86706 requests
  LRANGE_600 (first 600 elements): rps=8984.3 (overall: 8984.2) avg_msec=24.966 (overall: 24.463) 88997 requests
  LRANGE_600 (first 600 elements): rps=8400.8 (overall: 8969.4) avg_msec=23.507 (overall: 24.440) 91156 requests
  LRANGE_600 (first 600 elements): rps=8136.0 (overall: 8949.4) avg_msec=24.863 (overall: 24.449) 93190 requests
  LRANGE_600 (first 600 elements): rps=9273.8 (overall: 8957.1) avg_msec=24.082 (overall: 24.440) 95527 requests
  LRANGE_600 (first 600 elements): rps=9178.6 (overall: 8962.2) avg_msec=24.933 (overall: 24.452) 97840 requests
  LRANGE_600 (first 600 elements): 8976.66 requests per second, p50=23.759 msec
  MSET (10 keys): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan) 0 requests
  MSET (10 keys): 411522.62 requests per second, p50=1.535 msec

-- Command Metrics -------------------------------------
  Commands observed: 17901
  RESP errors:       105 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.46ms
  Latency p50/p95/p99: 0.04/0.90/15.29ms
  Latency max:       39.34ms
  Most used commands:
    set              9327 calls
    zadd             1811 calls
    get               797 calls
    del               677 calls
    srandmember       356 calls
    rpush             308 calls
    sadd              256 calls
    hset              216 calls
    info              173 calls
    mset              124 calls
    strlen            123 calls
    config            122 calls
  Slowest commands by average latency:
    keys              26.12ms avg over 106 calls
    scan              17.02ms avg over 108 calls
    save               7.38ms avg over 2 calls
    acl                3.37ms avg over 12 calls
    hscan              2.28ms avg over 109 calls
    hgetall            2.12ms avg over 119 calls
    sscan              1.88ms avg over 101 calls
    smembers           1.54ms avg over 94 calls
    bgsave             1.52ms avg over 2 calls
    zrevquery          1.26ms avg over 91 calls
    zquery             0.90ms avg over 112 calls
    lrange             0.87ms avg over 98 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
═══════════════════════════════════════════════════════


```
