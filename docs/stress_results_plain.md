# MYRED stress test — 2026-07-25 20:22:17

**Run:** correctness + concurrency + stress over plaintext (authenticated) → 127.0.0.1:1234

```
(logging output to docs/stress_results_plain.md)
═══════════════════════════════════════════════════════
  MYRED — correctness + concurrency + stress over plaintext (authenticated) → 127.0.0.1:1234
═══════════════════════════════════════════════════════
  Target:    127.0.0.1:1234
  Transport: plaintext
  Auth:      password
  Log:       docs/stress_results_plain.md
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
  ✓ keys returns list → ['kc', 'ka', 'kb']
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
  ✓ config get * -> array → ['maxmemory', '0', 'maxmemory-policy', 'allkeys-lru', 'notify-keyspace-events', '']
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
  ✓ acl users -> array → ['default', 'alice']
  ✓ acl users contains default
  ✓ acl list -> array → ['user default on #<hash> ~* &* +@all', 'user alice on #<hash> ~* &news.* +@all']
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
  ✓ info returns string → '# Server\r\nversion:1.0.0\r\nuptime_seconds:13\r\nuptime_minutes:0\r\nuptime_hours:0\r\n\r\n# Clients\r\nconnected_clients:1\r\ntotal_connections:4\r\n\r\n# Memory\r\nused_memory:0\r\nused_memory_human:0.00M\r\nused_memory_rss:111480832\r\nmem_fragmentation_ratio:0.00\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\nevicted_keys:0\r\n\r\n# Stats\r\ntotal_commands:2754\r\n\r\n# Keyspace\r\nkeys_total:0\r\nkeys_with_ttl:0\r\nkeys_no_ttl:0\r\n\r\n# Persistence\r\nrdb_last_save_time:2066\r\nrdb_changes_since_save:1993\r\nrdb_last_save_ok:1\r\nrdb_last_save_size_bytes:0\r\naof_enabled:1\r\naof_current_size:3827399\r\naof_base_size:3705124\r\naof_pending_rewrite:0\r\naof_last_write_status:ok\r\naof_last_bgrewrite_status:ok\r\n\r\n# Replication\r\nrole:master\r\n'
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
    uptime_seconds:13
    uptime_minutes:0
    uptime_hours:0
    # Clients
    connected_clients:1
    total_connections:4
    # Memory
    used_memory:0
    used_memory_human:0.00M
    used_memory_rss:111480832
    mem_fragmentation_ratio:0.00
    maxmemory:0
    maxmemory_policy:noeviction
    evicted_keys:0
    # Stats
    total_commands:2754
    # Keyspace
    keys_total:0
    keys_with_ttl:0
    keys_no_ttl:0
    # Persistence
    rdb_last_save_time:2066
    rdb_changes_since_save:1993
    rdb_last_save_ok:1
    rdb_last_save_size_bytes:0
    aof_enabled:1
    aof_current_size:3827399
    aof_base_size:3705124
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
  ℹ  bgsave returned in 1.2ms: 'Background saving started'
  ✓ server responsive during save
  ℹ  100 ops during save took 4.4ms
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
  ✓ wrong password → error
  ✓ unauthenticated → NOAUTH error
  ✓ correct password → OK
  ✓ authenticated set works

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
Results: 603/603 passed
Runtime: 13.14s (45.9 assertions/sec)
All tests passed!
Slowest sections:
  7.97s  11/11  Pub/Sub: keyspace notifications (V8.3)
  0.62s  16/16  Pub/Sub: SUBSCRIBE / UNSUBSCRIBE / PUBLISH (V8.1)
  0.60s  10/10  TTL Commands: PEXPIRE / PTTL
  0.55s  6/6  Persistence Round-trip (in-memory)
  0.54s  9/9  UNLINK Command (async delete)
  0.52s  7/7  BGSAVE (fork-based background save)
  0.51s  4/4  SAVE / RDB Persistence
  0.41s  42/42  String Variants: SETNX / SETEX / PSETEX / GETSET / GETEX / GETDEL
═══════════════════════════════════════════════════════

── Concurrent Write Safety ─────────────────────────────
  ✓ 10 threads × 50 ops, no errors

── Pub/Sub: fan-out under concurrent publishers ──────
  ✓ no publisher/reader errors
  ✓ every subscriber received every message
    4 publishers × 250 msgs → 4 subscribers = 4000 deliveries in 0.36s (11,255 deliveries/s)

═══════════════════════════════════════════════════════
Results: 2/2 passed
Runtime: 0.45s (4.4 assertions/sec)
All tests passed!
Slowest sections:
  0.45s  2/2  Pub/Sub: fan-out under concurrent publishers
═══════════════════════════════════════════════════════

── Stress Test ────────────────────────────────────────
  Threads:    8
  Ops/thread: 500
  Total ops:  4000

  Elapsed:    1.10s
  Throughput: 3623 ops/sec
  Total ops:   4000
  Errors:      0
  Latency avg: 1.89ms
  Latency min: 0.02ms
  Latency max: 46.20ms
  Latency p50: 0.27ms
  Latency p95: 4.90ms
  Latency p99: 39.05ms
  No errors!
  Operation mix:
    append                119 ok     0 errors
    incr                  117 ok     0 errors
    ping                  116 ok     0 errors
    rpush                 113 ok     0 errors
    srem                  113 ok     0 errors
    hset                  113 ok     0 errors
    zadd                  112 ok     0 errors
    getdel                112 ok     0 errors
    zrevquery             112 ok     0 errors
    zpopmin               110 ok     0 errors
    list_pop_trim         110 ok     0 errors
    zrank                 110 ok     0 errors
  Slowest operations by average latency:
    keys                  32.63ms avg over 108 ops
    keyspace_scan         21.27ms avg over 93 ops
    hscan                  2.79ms avg over 100 ops
    hgetall                2.35ms avg over 101 ops
    sscan                  2.08ms avg over 80 ops
    smembers               1.68ms avg over 107 ops
    zrevquery              1.23ms avg over 112 ops
    lrange                 0.75ms avg over 105 ops
    zquery                 0.74ms avg over 94 ops
    zpopmin                0.73ms avg over 110 ops
    ttl_triplet            0.69ms avg over 108 ops
    srandmember            0.67ms avg over 95 ops
  ℹ  cleaned 160 leftover keys

-- Command Metrics -------------------------------------
  Commands observed: 18914
  RESP errors:       111 (expected negative tests included)
  Transport errors:  0
  Latency avg:       0.44ms
  Latency p50/p95/p99: 0.03/0.63/6.04ms
  Latency max:       46.19ms
  Most used commands:
    set              9298 calls
    zadd             1826 calls
    publish          1009 calls
    get               797 calls
    del               679 calls
    srandmember       351 calls
    rpush             327 calls
    sadd              234 calls
    hset              229 calls
    config            137 calls
    info              128 calls
    ping              125 calls
  Slowest commands by average latency:
    keys              32.05ms avg over 110 calls
    scan              18.23ms avg over 105 calls
    save               8.11ms avg over 2 calls
    acl                3.56ms avg over 16 calls
    hscan              2.68ms avg over 104 calls
    hgetall            2.30ms avg over 103 calls
    sscan              1.98ms avg over 84 calls
    smembers           1.58ms avg over 114 calls
    bgsave             1.45ms avg over 2 calls
    zrevquery          1.21ms avg over 114 calls
    zpopmin            0.70ms avg over 115 calls
    zquery             0.70ms avg over 100 calls

═══════════════════════════════════════════════════════
  ALL TESTS PASSED
  correctness + concurrency + stress over plaintext (authenticated) → 127.0.0.1:1234
  Results saved to docs/stress_results_plain.md
═══════════════════════════════════════════════════════


```
