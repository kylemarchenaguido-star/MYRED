#pragma once
#include "buffer.h"
#include "hashtable.h"
#include "zset.h"
#include "list.h"
#include "heap.h"
#include "thread_pool.h"
#include "deque.h"
#include "set.h"
#include <variant>
#include <atomic>
#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>
#include <unordered_map>
#include <functional>
#include <utility>
#include <unordered_set>

// Constants
constexpr size_t k_max_msg = 32 << 20;
static constexpr size_t k_max_incoming = 2 * k_max_msg; // generous pipeline room
constexpr size_t k_max_args = 65536;
// secondes * miliseconds (5s -> 5000ms)
constexpr uint64_t k_idle_timeout_ms = 30 * 1000;
constexpr uint64_t k_io_timeout_ms = 30 * 1000;
// Compress only if < 1KB
constexpr size_t k_compress_threshold = 1024; 
// limit of failed auths
constexpr uint32_t k_max_failed_auth = 3;
// NO_TTL 
static constexpr size_t NO_TTL = (size_t)-1;
// 24-bit clock, wraps -194
static constexpr uint32_t LRU_CLOCK_MAX = (1u << 24) - 1; // maximun of a 24 bit number 
// New keys start here
static constexpr uint8_t LFU_INIT_VAL = 5;


// ACL command categories. BItflags shared by User::allow_cats
constexpr uint64_t CAT_READ       = 1ull << 0;
constexpr uint64_t CAT_WRITE      = 1ull << 1;
constexpr uint64_t CAT_KEYSPACE   = 1ull << 2;
constexpr uint64_t CAT_ADMIN      = 1ull << 3;
constexpr uint64_t CAT_DANGEROUS  = 1ull << 4;
constexpr uint64_t CAT_FAST       = 1ull << 5;
constexpr uint64_t CAT_SLOW       = 1ull << 6;
constexpr uint64_t CAT_CONNECTION = 1ull << 7;
constexpr uint64_t CAT_TRANSACTION= 1ull << 8;
constexpr uint64_t CAT_ALL        = ~0ull;

// Keyspace notificacion classes (Redis notify-keyspace-events flag chars)
constexpr int NOTIFY_KEYSPACE = 1 << 0; // K: __keyspace@0__:<key> msg = event
constexpr int NOTIFY_KEYEVENT = 1 << 1; // E: __keyevent@0__:<event> msg = key
constexpr int NOTIFY_GENERIC  = 1 << 2; // g
constexpr int NOTIFY_STRING   = 1 << 3; // $
constexpr int NOTIFY_LIST     = 1 << 4; // l
constexpr int NOTIFY_SET      = 1 << 5; // s
constexpr int NOTIFY_HASH     = 1 << 6; // h 
constexpr int NOTIFY_ZSET     = 1 << 7; // z
constexpr int NOTIFY_EXPIRED  = 1 << 8; // x
constexpr int NOTIFY_EVICTED  = 1 << 9; // e
constexpr int NOTIFY_ALL = NOTIFY_GENERIC | NOTIFY_STRING | NOTIFY_LIST | NOTIFY_SET |
                           NOTIFY_HASH | NOTIFY_ZSET | NOTIFY_EXPIRED | NOTIFY_EVICTED; // A

//value types 
enum {
  T_INIT = 0,
  T_STR = 1, // string
  T_ZSET = 2, // sorted set
  T_DLIST = 3, // deque list
  T_HASH = 4, // hash set
  T_SET = 5, // set
};

// Timer for both linked lists
enum class ConnTimer {
  IDLE,
  IO,
  HANDSHAKE, // TLS conns pre-handshake: tighet deadline, own list
};

// Modes for the aof
enum class Aoffsync{
  ALWAYS,
  EVERYSEC,
  NO
};

// Eviction policies
enum class MaxmemoryPolicy {
  NOEVICTION,
  ALLKEYS_LRU,
  ALLKEYS_LFU,
  ALLKEYS_RANDOM,
  VOLATILE_LRU,
  VOLATILE_LFU,
  VOLATILE_RANDOM,
  VOLATILE_TTL
};

enum class CfgResult {
  OK,
  UNKNOWN,
  BADVALUE
};

enum class TlsAuthClients {
  NO,
  YES,
  OPTIONAL
};

typedef struct ssl_st SSL; // OpenSSL's own typedef; we keep openss/ss.h out of this header

struct User {
  uint64_t allow_cats = 0; // granted category bits
  std::unordered_map<std::string, bool> cmd_overrides; // explicit +cmd / -cmd
  std::vector<std::string> pw_hashes; // SHA-256 digets; AUTH matches any (rotation)
  std::vector<std::string> key_patterns; // glob patterns for reacheable keys
  std::vector<std::string> channel_patterns; // glob patterns for reachable pub/sub channels
  std::string name;
  bool enable = true;
  bool all_keys = false; // ~* : any key
  bool all_channels = false; // &* : any channel
 };

// Connections state and buffers 
struct Conn {
  // Hot metadata: checked every event-loop iteration (fits in first cache line)
  int fd = -1;
  bool want_read = false;
  bool want_write = false;
  bool want_close = false;
  bool tr_want_read = false; // transport demands POLLIN to finish its current op
  bool tr_want_write = false; // transport demands POLLOUT (TLS: a read can need it)
  bool tls_handshaking = false; // SSL_do_handshake not yet done; data path is gated
  DList idle_node;
  uint64_t last_active_ms = 0;
  ConnTimer timer_type = ConnTimer::IO;
  uint32_t failed_attemps = 0;
  // 48 bytes total above → Buffer needs align 8, no padding needed
  Buffer incoming;
  Buffer outgoing;
  User *user = nullptr; 
  std::string peer; // "ip:port", set once at accept - no inet_ntoa() later
  bool auth_pending = false; // a worker is verifyng this conn's auth; parsing is gated
  uint64_t id = 0; // monotonic, stamped at accept - completions check it
  SSL *ssl = nullptr; // non-null = TLS conn; owned by the transport, freed in tr_close
  std::unordered_set<std::string> sub_channels; // channels this conn is SUBSCRIBEd to.
  std::unordered_set <std::string> sub_patterns; // glob patterns this conn PSUBSCRIBEd to.
  bool in_multi = false; // MULTI is open: ordinary commands queue instead of running
  bool multi_dirty = false; // a command was rejected at queue time
  bool in_exec = false; // inside EXEC's dispatch loop; blocking cmds must not block
  bool watch_dirty = false; // a watched key changed -> exec aborts
  std::unordered_set<std::string> watched_keys; // keys WATCHed; also the teardown index
  std::vector<std::vector<std::string>> queue_cmds; // stored as TYPED (not canonicalized)
};

// global hashtable
struct GlobalData{
  HMap db; // top level hashtable 
  std::vector<Conn *> fd2conn; 
  // channel name -> subscribed conns. Direct lookup on PUBLISH, no scanning
  std::unordered_map<std::string, std::unordered_set<Conn*>> channels;
  // pattern -> subscribed conns. PUBLISH scans these linearly
  std::unordered_map<std::string, std::unordered_set<Conn*>> patterns;
  // watched key -> conns watching it. Same chape as above
  std::unordered_map<std::string, std::unordered_set<Conn*>> watchers;
  //timers and connection
  DList idle_list; // list of waiting connections 
  DList io_list;  // list of waiting io (read and write)
  DList hs_list; // TLS conns mid-handshake (ConnTimer::HANDSHAKE deadlines)
  // timers for ttls
  std::vector<HeapItem> heap;
  ThreadPool thread_pool;
  // Data base globals
  uint64_t g_last_save_ms = 0; // timestamp last succesful save
  uint64_t evicted_keys = 0; // total keys removed by the maxmemory eviction policy
  bool g_evict_pending = false; // over maxmemory with victims left; event loop keeps evicting
  uint32_t g_writes_since_save = 0; // how many keys we written
  uint32_t g_dirty_at_save = 0; // g_writes_since_save captured when a save starts
  uint32_t g_lru_clock = 0; // coarse seconds, 24-bit; bumped each event-loop tick
  size_t g_last_save_size_bytes = 0; // size of last dump
  size_t used_memory = 0;
  bool g_last_save_ok = true; // did the last save succeded
  uint64_t next_conn_id = -1; // 0 = "never a real conn"  
  // statdistics// written per command
  uint64_t g_server_start_ms = 0;
  uint64_t g_total_commands = 0;
  uint64_t g_total_connections =0;
  uint32_t connected_clients = 0;
  // AOF
  uint64_t g_aof_check_ms = 0; // throttles the auto-rewrite check
  uint64_t g_aof_last_fsync_ms = 0;
  int g_aof_fd = -1; // -1 when disable
  size_t g_aof_current_size = 0;
  size_t g_aof_base_size = 0;
  std::string g_aof_buf; // pending bytes that aren't write()
  std::string g_aof_rewrite_buf; // delta captured during a rewrite
  std::atomic<bool> g_aof_fsync_pending{false}; // true while a pool fdatasync is running
  bool g_loading = false; // true when replaying
  bool g_aof_write_err = false; // last AOF flush failed -> refuse rewrties until recovery
  bool g_aof_last_rewrite_ok = true; // For do_info
};

struct SaveCondition {
  uint64_t seconds; // window lenght
  uint32_t changes; // min writes within that window
};

//global config
struct Config {
  int port = 1234;
  // TLS - all boot-only; CONFIG SET rejects tls-*
  int tls_port = 0; // 0 = disable; coexists with plaintext port
  std::string tls_cert_file;
  std::string tls_key_file;
  std::string tls_ca_cert_file;
  TlsAuthClients tls_auth_clients = TlsAuthClients::NO;
  int tls_handshake_timeout_ms = 10 * 1000; // boot-only, like all tls-*
  int  notify_keyspace_events = 0; // 0 = off (default); runtinme-settable, unlike tls-*
  std::string config_path;
  std::string auditlog_path;
  std::string password = "";
  std::vector<std::string> binds = {"0.0.0.0"}; // one listen fd per address
  std::string dump_path = "dump.rdb";
  std::string aof_path = "appendonly.aof";
  Aoffsync aof_fysnc = Aoffsync::EVERYSEC;
  size_t  aof_rewrite_min_size = 64 * 1024 * 1024; // never auto_rewrite below 64MB
  int aof_rewrite_perc = 100; // ... or until it has doubled
  int maxmemory_samples = 10; // eviction sample size (best-of-n)
  int lfu_log_factor = 10; // LFU: higher = counter saturates slower
  int lfu_decay_time = 1; // LFU: minutes of idleness per counter decrement
  // Memory managment 
  size_t maxmemory = 0;
  MaxmemoryPolicy maxmemory_policy = MaxmemoryPolicy::NOEVICTION;

  // Redis defaults
  std::vector<SaveCondition> save_conditions = {
    {3600, 1}, // 1 change in 1 hour
    {300, 100}, // 100 changes in 5 min
    {60, 10000}, // 10000 changes in 1 min
  };
  // (network, maks), host byte order
  std::vector<std::pair<uint32_t, uint32_t>> allowlist;

  // rename-command OLD->NEW (lowercase; NEW="" = disable)
  std::vector<std::pair<std::string, std::string>> renames;

  // ACL registry - stable addresses for Conn::User
  std::unordered_map<std::string, User> users;

  // Enable modes
  bool aof_enable = false;
  bool protected_mode = true; // refuse remote peer when no password
};

// HMap is not unique
struct EntryHash { HMap hmap; };
struct EntrySet { HMap hmap; };

// One payload for entry (Mathes T_* enum)
using EntryValue = std::variant<
  std::monostate, // T_INIT
  std::string, // T_STR
  ZSet, // T_ZSET
  Deque, // T_DLIST
  EntryHash, // T_HASH
  EntrySet // T_SET
>;

// kv pair for the top level hashtable
struct Entry {
  struct HNode node; // hashtable node
  std::string key;
  size_t heap_idx = NO_TTL;
  size_t mem = 0;
  uint32_t lru = 0; // 
  uint32_t type; 
  EntryValue val;\
};

// Key for searching in the hashtable
struct LookupKey {
  struct HNode node; // hashtable node
  std::string key;
};

// Shared globals 
extern GlobalData g_data;
extern Config g_config;

// Functions declarations

#ifndef NDEBUG
void mem_selfcheck(const char *where);
#endif

uint64_t get_monotonic_msec();
uint64_t get_wall_msec();
bool expire_if_needed(Entry *ent);
inline bool entry_has_ttl(const Entry *e){ return e->heap_idx != NO_TTL; }

bool entry_eq(HNode *node, HNode *key);
void entry_del(Entry *ent);
void entry_set_ttl(Entry *ent, int64_t ttl_ms);
void entry_del_sync(Entry *ent);
void entry_del_func(void *arg);
Entry *entry_new(uint32_t type);
size_t entry_mem_usage(Entry *ent);
size_t entry_mem_usage_sampled(Entry *ent, size_t samples);

void entry_init_access(Entry *ent);
void entry_touch_access(Entry *ent);
Entry *evict_pick_victim();

void mem_reaccount(Entry *ent);
bool hnode_same(HNode *node, HNode *key);

inline std::string &entry_str(Entry *e){ return std::get<std::string>(e->val); }
inline ZSet &entry_zset(Entry *e){ return std::get<ZSet>(e->val); }
inline Deque &entry_deque(Entry *e){ return std::get<Deque>(e->val); }
inline HMap &entry_hash(Entry *e){ return std::get<EntryHash>(e->val).hmap; }
inline HMap &entry_set(Entry *e){ return std::get<EntrySet>(e->val).hmap; }

// Apply one directive to g_config. Shared by the file parser and 'CONFIG SET'
CfgResult config_apply(const std::string &name, const std::vector<std::string> &args, std::string &err);
bool config_load_file(const char *path);
bool config_rewrite(const char *path);

bool parse_memory_size(const std::string &s, size_t *out);
bool parse_maxmemory_policy(const std::string &s, MaxmemoryPolicy *out);
const char *maxmemory_policy_name(MaxmemoryPolicy p);

bool parse_cidr(const std::string &S, uint32_t *net, uint32_t *mask); // "10.0.0.0/8" , net/mask
bool ip_is_loopback(uint32_t peer_host); // 127.0.0.0/8
bool ip_allowed(uint32_t peer_host); // allowlist check 

void acl_bootstrap_default(); // (re)build the built in default user from require pass
void acl_init_categories(); // stamp acl_cats + KeySpec onto every CmdSpec (call at boot)
User *acl_initial_user(); // starting identity for a new conn: default if no pass, else nullptr
std::string acl_format_user(const std::string &name, const User &u, bool for_config);
bool acl_apply_rule(User &u, const std::string &t); // shared by ACL SETUSER + `user` config directive

void loop_post(std::function<void()> fn); // worker, main loop completion channel
void conn_resume(Conn *conn); // drain buffered requests + flush after an async completion

void audit_open(const std::string &path); // (re)open the sink
void audit_event(const char *event, const Conn *conn, const std::string &extra);
void audit_reject(const std::string &peer, const char *reason); // accept-time, no Conn yet

void dispatch_build(); // build the live command map from k_cmd_table + renames
bool command_is_known(const std::string &name);
void metadata_selfcheck(); // assert ACL/category invariants at boot; die() on violation

bool parse_int_strict(const char *s, long *out);
bool parse_bool_strict(const std::string &s, bool *out);

bool parse_notify_flags(const std::string &s, int *out);
std::string notify_flags_string(int flags);
// fire a keyspace/keyevent notification; no-op unless the class is enabled
void notify_keyspace_event(int type, const char *event, const std::string &key);
