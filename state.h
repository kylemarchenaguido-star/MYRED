#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>
#include "buffer.h"
#include "hashtable.h"
#include "zset.h"
#include "list.h"
#include "heap.h"
#include "thread_pool.h"
#include "deque.h"
#include "set.h"
#include <variant>

// Constants
constexpr size_t k_max_msg = 32 << 20;
constexpr size_t k_max_args = 65536;
constexpr uint64_t k_save_interval_ms  = 5 * 60 * 1000; // 5 minutes 5 * 60 * 1000
constexpr uint32_t k_save_after_writes = 100; // or after 100 writes
// secondes * miliseconds (5s -> 5000ms)
constexpr uint64_t k_idle_timeout_ms = 30 * 1000;
constexpr uint64_t k_io_timeout_ms = 30 * 1000;
// Compress only if < 1KB
constexpr size_t k_compress_threshold = 1024; 
// limit of failed auths
constexpr uint32_t k_max_failed_auth = 3;
// NO_TTL 
static constexpr size_t NO_TTL = (size_t)-1;


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
};

// Connections state and buffers 
struct Conn {
  // buffered input, output
  Buffer incoming; // This two are for the buffers that we are gonna parse // data
  Buffer outgoing; // the response 
  int fd = -1; // this is for the event loop
  bool want_read = false; // The the read and the write, is waiting for the fd api readiness
  bool want_write = false;
  bool want_close = false;
  bool authenticaded = false;

  DList idle_node;
  //Info commands stadistics and functions
  uint64_t last_active_ms = 0;
  ConnTimer timer_type = ConnTimer::IO;

  uint32_t failed_attemps = 0;
};

// global hashtable
struct GlobalData{
  HMap db; // top level hashtable 
  std::vector<Conn *> fd2conn; 
  //timers and connection
  DList idle_list; // list of waiting connections 
  DList io_list;  // list of waiting io (read and write)
  // timers for ttls
  std::vector<HeapItem> heap;
  ThreadPool thread_pool;
  // Data base globals
  uint64_t g_last_save_ms = 0; // timestamp last succesful save
  uint32_t g_writes_since_save = 0; // how many keys we written
  size_t g_last_save_size_bytes = 0; // size of last dump
  bool g_last_save_ok = true; // did the last save succeded
  // statdistics// written per command
  uint64_t g_server_start_ms = 0;
  uint64_t g_total_commands = 0;
  uint64_t g_total_connections =0;
  uint32_t connected_clients = 0;
};

//global config
struct Config {
  std::string password = "";
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
  EntryValue val;
  uint32_t type; 
};

// Key for searching in the hashtable
struct LookupKey {
  struct HNode node; // hashtable node
  std::string key;
};

// Shared globals 
extern GlobalData g_data;
extern Config g_config;

// Functions
uint64_t get_monotonic_msec();
uint64_t get_wall_msec();
bool entry_eq(HNode *node, HNode *key);
Entry *entry_new(uint32_t type);
void entry_del(Entry *ent);
void entry_set_ttl(Entry *ent, int64_t ttl_ms);
void entry_del_sync(Entry *ent);
void entry_del_func(void *arg);
bool hnode_same(HNode *node, HNode *key);
bool expire_if_needed(Entry *ent);
inline bool entry_has_ttl(const Entry *e){ return e->heap_idx != NO_TTL; }
inline std::string &entry_str(Entry *e){ return std::get<std::string>(e->val); }
inline ZSet &entry_zset(Entry *e){ return std::get<ZSet>(e->val); }
inline Deque &entry_deque(Entry *e){ return std::get<Deque>(e->val); }
inline HMap &entry_hash(Entry *e){ return std::get<EntryHash>(e->val).hmap; }
inline HMap &entry_set(Entry *e){ return std::get<EntrySet>(e->val).hmap; }
