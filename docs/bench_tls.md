# MYRED stress test — 2026-07-21 13:36:44

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
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:8\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:3\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:49070080\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:0\r\n\r\n# Stats\r\ntotal_commands:2730\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:22346\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\naof_enabled:0\r\naof_current_size:0\r\naof_base_size:0\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:8
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:3
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:49070080
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
    rdb_last_save_time:22346
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
  ℹ  bgsave returned in 1.4ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 18.0ms
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
Runtime: 5.79s (95.2 assertions/sec)
All tests passed!
Slowest sections:
  0.91s  2/2  Memory: incremental eviction (EVICT_RUNNING semantics)
  0.77s  9/9  UNLINK Command (async delete)
  0.68s  9/9  Memory: maxmemory eviction + OOM
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.54s  7/7  BGSAVE (fork-based background save)
  0.52s  6/6  Persistence Round-trip (in-memory)
  0.50s  4/4  SAVE / RDB Persistence
  0.42s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    11.39s
  Throughput: 351 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 24.88ms
  Latency min: 0.14ms
  Latency max: 659.66ms
  Latency p50: 4.33ms
  Latency p95: 62.85ms
  Latency p99: 464.44ms
  No errors!
  Operation mix:
    rpush                 117 ok     0 errors
    memory_usage          117 ok     0 errors
    strlen                116 ok     0 errors
    incr                  115 ok     0 errors
    zrevquery             114 ok     0 errors
    lpush                 113 ok     0 errors
    hscan                 113 ok     0 errors
    smembers              112 ok     0 errors
    del                   111 ok     0 errors
    srandmember           110 ok     0 errors
    info                  109 ok     0 errors
    mset                  109 ok     0 errors
  Slowest operations by average latency:
    keys                 401.91ms avg over 97 ops
    keyspace_scan        285.33ms avg over 102 ops
    hscan                 33.79ms avg over 113 ops
    hgetall               30.40ms avg over 94 ops
    sscan                 28.32ms avg over 95 ops
    smembers              24.66ms avg over 112 ops
    zrevquery             21.10ms avg over 114 ops
    zquery                14.81ms avg over 107 ops
    lrange                14.76ms avg over 100 ops
    zpopmin               10.52ms avg over 97 ops
    list_pop_trim         10.10ms avg over 95 ops
    srandmember            9.93ms avg over 110 ops
  ℹ  cleaned 163 leftover keys

═══════════════════════════════════════════════════════
  Speed baseline (redis-benchmark)
═══════════════════════════════════════════════════════
  PING_INLINE: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  PING_INLINE: 549450.56 requests per second, p50=0.567 msec
  PING_MBULK: rps=157248.0 (overall: 405278.3) avg_msec=1.092 (overall: 1.092)
  PING_MBULK: 552486.19 requests per second, p50=0.575 msec
  SET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  SET: 512820.53 requests per second, p50=0.615 msec
  GET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  GET: 510204.09 requests per second, p50=0.647 msec
  INCR: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  INCR: 396825.38 requests per second, p50=0.887 msec
  LPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  LPUSH: 515463.91 requests per second, p50=0.655 msec
  RPUSH: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  RPUSH: 465116.28 requests per second, p50=0.735 msec
  LPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  LPOP: 485436.91 requests per second, p50=0.719 msec
  RPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  RPOP: 495049.50 requests per second, p50=0.655 msec
  SADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  SADD: 510204.09 requests per second, p50=0.679 msec
  HSET: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  HSET: 469483.56 requests per second, p50=0.855 msec
  SPOP: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  SPOP: 502512.56 requests per second, p50=0.639 msec
  ZADD: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  ZADD: 458715.59 requests per second, p50=0.791 msec
  ZPOPMIN: rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  ZPOPMIN: 480769.22 requests per second, p50=0.727 msec
  LPUSH (needed to benchmark LRANGE): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  LPUSH (needed to benchmark LRANGE): 400000.00 requests per second, p50=0.999 msec
  LRANGE_100 (first 100 elements): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  LRANGE_100 (first 100 elements): rps=84717.1 (overall: 70880.0) avg_msec=9.990 (overall: 9.990)
  LRANGE_100 (first 100 elements): rps=82688.0 (overall: 76247.3) avg_msec=9.595 (overall: 9.795)
  LRANGE_100 (first 100 elements): rps=82816.0 (overall: 78300.0) avg_msec=9.556 (overall: 9.716)
  LRANGE_100 (first 100 elements): rps=84717.1 (overall: 79832.5) avg_msec=9.311 (overall: 9.613)
  LRANGE_100 (first 100 elements): 80710.25 requests per second, p50=9.087 msec
  LRANGE_300 (first 300 elements): rps=406.4 (overall: 1888.9) avg_msec=37.913 (overall: 37.913)
  LRANGE_300 (first 300 elements): rps=27346.6 (overall: 22839.3) avg_msec=11.595 (overall: 11.981)
  LRANGE_300 (first 300 elements): rps=27824.7 (overall: 25089.9) avg_msec=10.213 (overall: 11.096)
  LRANGE_300 (first 300 elements): rps=31520.0 (overall: 27084.4) avg_msec=8.971 (overall: 10.329)
  LRANGE_300 (first 300 elements): rps=27729.1 (overall: 27237.5) avg_msec=10.687 (overall: 10.415)
  LRANGE_300 (first 300 elements): rps=25952.2 (overall: 26990.8) avg_msec=10.671 (overall: 10.462)
  LRANGE_300 (first 300 elements): rps=27665.3 (overall: 27099.4) avg_msec=10.113 (overall: 10.405)
  LRANGE_300 (first 300 elements): rps=28984.0 (overall: 27359.9) avg_msec=10.168 (overall: 10.370)
  LRANGE_300 (first 300 elements): rps=29450.2 (overall: 27614.6) avg_msec=9.979 (overall: 10.319)
  LRANGE_300 (first 300 elements): rps=31134.9 (overall: 27998.3) avg_msec=9.425 (overall: 10.211)
  LRANGE_300 (first 300 elements): rps=31496.0 (overall: 28339.6) avg_msec=9.010 (overall: 10.081)
  LRANGE_300 (first 300 elements): rps=30613.5 (overall: 28542.5) avg_msec=9.261 (overall: 10.002)
  LRANGE_300 (first 300 elements): rps=28088.0 (overall: 28505.4) avg_msec=10.186 (overall: 10.017)
  LRANGE_300 (first 300 elements): rps=28000.0 (overall: 28467.1) avg_msec=10.303 (overall: 10.038)
  LRANGE_300 (first 300 elements): 28457.60 requests per second, p50=8.903 msec
  LRANGE_500 (first 500 elements): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  LRANGE_500 (first 500 elements): rps=13828.0 (overall: 11485.0) avg_msec=19.537 (overall: 19.537)
  LRANGE_500 (first 500 elements): rps=16008.0 (overall: 13541.7) avg_msec=15.773 (overall: 17.514)
  LRANGE_500 (first 500 elements): rps=16128.0 (overall: 14347.9) avg_msec=15.564 (overall: 16.830)
  LRANGE_500 (first 500 elements): rps=15547.2 (overall: 14636.4) avg_msec=16.442 (overall: 16.731)
  LRANGE_500 (first 500 elements): rps=15890.2 (overall: 14880.2) avg_msec=16.196 (overall: 16.620)
  LRANGE_500 (first 500 elements): rps=16502.0 (overall: 15140.8) avg_msec=16.533 (overall: 16.605)
  LRANGE_500 (first 500 elements): rps=15160.2 (overall: 15143.6) avg_msec=16.096 (overall: 16.533)
  LRANGE_500 (first 500 elements): rps=12364.7 (overall: 14801.7) avg_msec=19.251 (overall: 16.812)
  LRANGE_500 (first 500 elements): rps=15132.0 (overall: 14837.3) avg_msec=16.762 (overall: 16.807)
  LRANGE_500 (first 500 elements): rps=16394.4 (overall: 14989.1) avg_msec=15.771 (overall: 16.696)
  LRANGE_500 (first 500 elements): rps=16072.0 (overall: 15085.0) avg_msec=15.931 (overall: 16.624)
  LRANGE_500 (first 500 elements): rps=16019.9 (overall: 15161.3) avg_msec=16.095 (overall: 16.578)
  LRANGE_500 (first 500 elements): rps=15752.0 (overall: 15206.4) avg_msec=16.078 (overall: 16.539)
  LRANGE_500 (first 500 elements): rps=15792.0 (overall: 15247.3) avg_msec=15.698 (overall: 16.478)
  LRANGE_500 (first 500 elements): rps=15470.4 (overall: 15262.0) avg_msec=16.067 (overall: 16.451)
  LRANGE_500 (first 500 elements): rps=15876.5 (overall: 15299.8) avg_msec=16.379 (overall: 16.446)
  LRANGE_500 (first 500 elements): rps=15617.5 (overall: 15318.2) avg_msec=16.010 (overall: 16.420)
  LRANGE_500 (first 500 elements): rps=15996.0 (overall: 15355.3) avg_msec=15.241 (overall: 16.353)
  LRANGE_500 (first 500 elements): rps=16208.0 (overall: 15399.4) avg_msec=16.043 (overall: 16.336)
  LRANGE_500 (first 500 elements): rps=16195.2 (overall: 15438.7) avg_msec=17.095 (overall: 16.375)
  LRANGE_500 (first 500 elements): rps=16244.0 (overall: 15476.4) avg_msec=16.256 (overall: 16.370)
  LRANGE_500 (first 500 elements): rps=16260.0 (overall: 15511.5) avg_msec=16.826 (overall: 16.391)
  LRANGE_500 (first 500 elements): rps=16191.2 (overall: 15540.7) avg_msec=16.577 (overall: 16.399)
  LRANGE_500 (first 500 elements): rps=16243.0 (overall: 15569.6) avg_msec=16.376 (overall: 16.398)
  LRANGE_500 (first 500 elements): rps=16328.0 (overall: 15599.6) avg_msec=16.208 (overall: 16.391)
  LRANGE_500 (first 500 elements): 15612.80 requests per second, p50=16.007 msec
  LRANGE_600 (first 600 elements): rps=4566.9 (overall: 6480.4) avg_msec=62.482 (overall: 62.482)
  LRANGE_600 (first 600 elements): rps=13553.4 (overall: 10622.7) avg_msec=19.134 (overall: 30.092)
  LRANGE_600 (first 600 elements): rps=13689.0 (overall: 11758.0) avg_msec=19.124 (overall: 25.364)
  LRANGE_600 (first 600 elements): rps=13410.4 (overall: 12200.6) avg_msec=19.492 (overall: 23.635)
  LRANGE_600 (first 600 elements): rps=13260.0 (overall: 12423.8) avg_msec=19.746 (overall: 22.761)
  LRANGE_600 (first 600 elements): rps=12932.3 (overall: 12512.5) avg_msec=20.054 (overall: 22.272)
  LRANGE_600 (first 600 elements): rps=13200.0 (overall: 12614.3) avg_msec=18.890 (overall: 21.748)
  LRANGE_600 (first 600 elements): rps=13083.7 (overall: 12675.1) avg_msec=19.334 (overall: 21.426)
  LRANGE_600 (first 600 elements): rps=12740.0 (overall: 12682.5) avg_msec=19.841 (overall: 21.244)
  LRANGE_600 (first 600 elements): rps=13119.5 (overall: 12727.5) avg_msec=19.329 (overall: 21.041)
  LRANGE_600 (first 600 elements): rps=13031.9 (overall: 12755.9) avg_msec=18.924 (overall: 20.839)
  LRANGE_600 (first 600 elements): rps=13188.0 (overall: 12792.6) avg_msec=18.997 (overall: 20.678)
  LRANGE_600 (first 600 elements): rps=13236.0 (overall: 12827.3) avg_msec=19.274 (overall: 20.564)
  LRANGE_600 (first 600 elements): rps=14121.6 (overall: 12923.1) avg_msec=18.882 (overall: 20.428)
  LRANGE_600 (first 600 elements): rps=13446.2 (overall: 12958.6) avg_msec=19.016 (overall: 20.329)
  LRANGE_600 (first 600 elements): rps=11613.5 (overall: 12873.1) avg_msec=18.764 (overall: 20.239)
  LRANGE_600 (first 600 elements): rps=13909.4 (overall: 12935.7) avg_msec=19.314 (overall: 20.179)
  LRANGE_600 (first 600 elements): rps=13892.4 (overall: 12989.7) avg_msec=19.431 (overall: 20.134)
  LRANGE_600 (first 600 elements): rps=13056.0 (overall: 12993.2) avg_msec=18.592 (overall: 20.051)
  LRANGE_600 (first 600 elements): rps=13111.6 (overall: 12999.2) avg_msec=18.784 (overall: 19.987)
  LRANGE_600 (first 600 elements): rps=13091.6 (overall: 13003.7) avg_msec=19.644 (overall: 19.970)
  LRANGE_600 (first 600 elements): rps=13176.0 (overall: 13011.5) avg_msec=19.176 (overall: 19.933)
  LRANGE_600 (first 600 elements): rps=13015.9 (overall: 13011.7) avg_msec=19.079 (overall: 19.896)
  LRANGE_600 (first 600 elements): rps=12996.0 (overall: 13011.1) avg_msec=18.542 (overall: 19.839)
  LRANGE_600 (first 600 elements): rps=13047.8 (overall: 13012.6) avg_msec=19.422 (overall: 19.822)
  LRANGE_600 (first 600 elements): rps=13179.3 (overall: 13019.0) avg_msec=18.647 (overall: 19.776)
  LRANGE_600 (first 600 elements): rps=13200.0 (overall: 13025.8) avg_msec=19.699 (overall: 19.773)
  LRANGE_600 (first 600 elements): rps=12944.2 (overall: 13022.8) avg_msec=19.455 (overall: 19.761)
  LRANGE_600 (first 600 elements): rps=13104.0 (overall: 13025.7) avg_msec=18.973 (overall: 19.734)
  LRANGE_600 (first 600 elements): rps=13226.2 (overall: 13032.4) avg_msec=19.000 (overall: 19.709)
  LRANGE_600 (first 600 elements): 13036.11 requests per second, p50=19.119 msec
  MSET (10 keys): rps=0.0 (overall: 0.0) avg_msec=-nan (overall: -nan)
  MSET (10 keys): rps=238720.0 (overall: 207944.2) avg_msec=3.504 (overall: 3.504)
  MSET (10 keys): 224215.23 requests per second, p50=3.143 msec

-- Command Metrics -------------------------------------
  Commands observed: 17869
  RESP errors:       105 (expected negative tests included)
  Transport errors:  0
  Latency avg:       5.95ms
  Latency p50/p95/p99: 0.18/11.50/170.79ms
  Latency max:       659.61ms
  Most used commands:
    set              9311 calls
    zadd             1807 calls
    get               797 calls
    del               693 calls
    srandmember       366 calls
    rpush             331 calls
    sadd              226 calls
    hset              210 calls
    info              143 calls
    memory            123 calls
    incr              122 calls
    config            122 calls
  Slowest commands by average latency:
    keys             393.79ms avg over 99 calls
    scan             266.68ms avg over 105 calls
    hscan             32.62ms avg over 117 calls
    hgetall           29.76ms avg over 96 calls
    sscan             27.17ms avg over 99 calls
    smembers          23.21ms avg over 119 calls
    zrevquery         20.72ms avg over 116 calls
    zquery            14.01ms avg over 113 calls
    lrange            13.30ms avg over 111 calls
    zpopmin           10.00ms avg over 102 calls
    mget               8.86ms avg over 99 calls
    save               7.45ms avg over 2 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
═══════════════════════════════════════════════════════


```
