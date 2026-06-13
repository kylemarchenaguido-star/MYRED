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

// Constants
constexpr size_t k_max_msg = 32 << 20;
constexpr uint64_t k_save_interval_ms  = 5 * 60 * 1000; // 5 minutes 5 * 60 * 1000
constexpr uint32_t k_save_after_writes = 100; // or after 100 writes
// secondes * miliseconds (5s -> 5000ms)
constexpr uint64_t k_idle_timeout_ms = 30 * 1000;
constexpr uint64_t k_io_timeout_ms = 30 * 1000;
// Compress only if < 1KB
constexpr size_t k_compress_threshold = 1024; 
// limit of failed auths
constexpr uint32_t k_max_failed_auth = 3;

//value types 
enum {
  T_INIT = 0,
  T_STR = 1, // string
  T_ZSET = 2, // sorted set
};

// Timer for both linked lists
enum class ConnTimer {
  IDLE,
  IO,
};

// Connections state and buffers 
struct Conn {
  int fd = -1; // this is for the event loop

  bool want_read = false; // The the read and the write, is waiting for the fd api readiness
  bool want_write = false;
  bool want_close = false;
  // buffered input, output
  Buffer incoming; // This two are for the buffers that we are gonna parse // data
  Buffer outgoing; // the response 
  //Info commands stadistics and functions
  uint64_t last_active_ms = 0;
  ConnTimer timer_type = ConnTimer::IO;
  DList idle_node;
  bool authenticaded = false;
  uint32_t failed_attemps = 0;
};

// global hashtable
struct GlobalData{
  std::vector<Conn *> fd2conn; // this a pointer to all conecctions in the file descriptor [3,4,5], and is key by this aswell
  HMap db; // top level hashtable 
  //timers and connection
  DList idle_list; // list of waiting connections 
  DList io_list;  // list of waiting io (read and write)
  // timers for ttls
  std::vector<HeapItem> heap;
  ThreadPool thread_pool;
  // Data base globals
  bool g_last_save_ok = true; // did the last save succeded
  uint32_t g_writes_since_save = 0; // how many keys we written
  uint64_t g_last_save_ms = 0; // timestamp last succesful save
  uint64_t g_server_start_ms = 0;
  uint64_t g_total_commands = 0;
  uint64_t g_total_connections =0;
  uint32_t connected_clients = 0;
  size_t g_last_save_size_bytes = 0;
};

//global config
struct Config {
  std::string password = "";
};

// kv pair for the top level hashtable
struct Entry {
  struct HNode node; // hashtable node
  std::string key;
  // for ttl
  size_t heap_idx = -1;
  // value
  uint32_t type = 0;
  // one of the following 
  std::string str;
  ZSet zset;
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
bool entry_eq(HNode *node, HNode *key);
Entry *entry_new(uint32_t type);
void entry_del(Entry *ent);
void entry_set_ttl(Entry *ent, int64_t ttl_ms);
void entry_del_sync(Entry *ent);
void entry_del_func(void *arg);
bool hnode_same(HNode *node, HNode *key);