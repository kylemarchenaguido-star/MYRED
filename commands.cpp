#include "commands.h"
#include "state.h"
#include "resp.h"
#include "rdb.h"
#include "buffer.h"
#include "common.h"
#include "string.h"
#include "stdio.h"
#include "stdlib.h"
#include "math.h"
#include "ctype.h"

//gets a value from key
static void do_get(std::vector<std::string> &cmd, Buffer *out){
  // we create a dummy entry just for the lookup
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable lookup
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){
    return resp_nil(out);
  }
  // we copy the value
  Entry *ent = container_of(node, Entry, node);
  if (ent->type != T_STR) {
    return resp_err(out, "WRONGTYPE wrong type");
  }
  return resp_str(out, ent->str.data(), ent->str.size());
}

// sets a key with value in the hashtab
static void do_set(std::vector<std::string> &cmd, Buffer *out){

  // again with the dummy
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable lookup
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (node){
    //found, update the value
    Entry *ent = container_of(node, Entry, node);
    if (ent->type != T_STR) {
      return resp_err(out, "WRONGTYPE wrong type");
    }
    ent->str.swap(cmd[2]);
  } else {
    //not found allocate & insert a new pair
    Entry *ent = entry_new(T_STR);
    ent->key.swap(key.key);
    ent->node.hcode = key.node.hcode;
    ent->str.swap(cmd[2]);

    hm_insert(&g_data.db, &ent->node);
  }
  g_data.g_writes_since_save++;
  return resp_ok(out);
}

// deletes a key and value
static void do_del(std::vector<std::string> &cmd, Buffer *out){
  // a dummy again
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable delete
  HNode *node = hm_delete(&g_data.db, &key.node, &entry_eq);
  if (node){
    //deallocate the memory
    entry_del(container_of(node, Entry, node));
    g_data.g_writes_since_save++;
    return resp_int(out, 1);
  }
  return resp_int(out, 0); // number of deleted keys
}

struct KeyStats {
  uint32_t total;
  uint32_t with_ttl;
};

struct KeysCtx {
  std::vector<std::string> *keys;
};

// Returns all the keys from the hashtable
static bool cb_keys(HNode *node, void *arg){
  auto *ctx = (KeysCtx *)arg;
  Entry *ent = container_of(node, Entry, node);
  ctx->keys->push_back(ent->key);
  return true;
}

// Gets all the keys for the stats 
static bool cb_count_keys(HNode *node, void *args){
  KeyStats *stats = (KeyStats *)args;
  Entry *ent = container_of(node, Entry, node);
  stats->total++;
  if (ent->heap_idx != (size_t)-1){
    stats->with_ttl++;
  }
  return true;
}

static void do_keys(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  std::vector<std::string> keys;
  KeysCtx ctx = {&keys};
  hm_foreach(&g_data.db, cb_keys, &ctx);

  resp_arr(out, (uint32_t)keys.size());
  for (const auto &k : keys){
    resp_str(out, k.data(), k.size());
  }
}

static KeyStats get_keys_stats(){
  KeyStats stats = {0,0};
  hm_foreach(&g_data.db, &cb_count_keys, &stats);
  return stats;
}

// This is for the info command
static size_t get_memory_usage(){
  // we read it from proc/self/status on linux
  FILE *fp = fopen("/proc/self/status", "r");
  if (!fp){ return 0; }

  char line[128];
  size_t mem = 0;
  while (fgets(line, sizeof(line), fp)){
    if (strncmp(line, "VmRSS:", 6) == 0){
      // VmRSS is in kb 
      sscanf(line + 6,"%zu", &mem); // we skip the VmRSS; = 6 bytes
      // we convert it to bytes
      mem *= 1024;
      break;
    }
  }
  fclose(fp);
  return mem;
}

static bool str2dbl(const std::string &s, double &out){
  char *endp = NULL;
  out = strtod(s.c_str(), &endp); // endp points to the first wrong character
  return endp == s.c_str() + s.size() && !isnan(out); // NaN = not a number
}

static bool str2int(const std::string &s, int64_t &out){
  char *endp = NULL;
  out = strtoll(s.c_str(), &endp, 10);
  return endp == s.c_str() + s.size();
}

// PEXPIRE key ttl_ms
static void do_pexpire(std::vector<std::string> &cmd, Buffer *out){
  int64_t ttl_ms = 0;
  if (!str2int(cmd[2], ttl_ms)){
    return resp_err(out, "ERR invalid TTL");
  }
  // lookup the key
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  // we set the ttl
  if (!node) { return resp_int(out, 0); }

  Entry *ent = container_of(node, Entry, node);
  entry_set_ttl(ent, ttl_ms);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// PTTL key
static void do_ttl(std::vector<std::string> &cmd, Buffer *out){
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){ return resp_int(out, -2); } // not found

  Entry *ent = container_of(node, Entry, node);
  if (ent->heap_idx == (size_t)-1){
    return resp_int(out, -1); // not ttl
  }
  uint64_t expire_at = g_data.heap[ent->heap_idx].val;
  uint64_t now_ms = get_monotonic_msec();
  return resp_int(out, expire_at > now_ms ? (expire_at - now_ms) : 0);
}

// zadd and zset (score, name)
static void do_zadd(std::vector<std::string> &cmd, Buffer *out){
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return resp_err(out, "ERR invalid score");
  }

  // look up or create the zset
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);

  Entry *ent = NULL;
  //insert a new key
  if (!hnode){
    ent = entry_new(T_ZSET);
    ent->key.swap(key.key);
    ent->node.hcode = key.node.hcode;
    hm_insert(&g_data.db, &ent->node);
    
  } else { // check the existing key
    ent = container_of(hnode, Entry, node);
    if (ent->type != T_ZSET){
      return resp_err(out, "WRONGTYPE wrong type");
    }
  }

  // add or update the tuple
  const std::string &name = cmd[3];
  bool added = zset_insert(&ent->zset, name.data(), name.size(), score);
  g_data.g_writes_since_save++;
  return resp_int(out, (int64_t)added);
}

// empty zset (?? i am going to explode myself)
static constexpr ZSet k_empty_zset;

// search an expected zset
static ZSet *expect_zset(std::string &s){
  LookupKey key;
  key.key.swap(s);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!hnode) { // no key == treated as an empty zset
    return (ZSet *)&k_empty_zset;
  }
  Entry *ent = container_of(hnode, Entry, node);
  return ent->type == T_ZSET ? &ent->zset : NULL;
}

// zrem zset name (search and remove)
static void do_zrem( std::vector<std::string> &cmd, Buffer *out){
  ZSet *zset = expect_zset(cmd[1]);

  if (!zset) { return resp_err(out, "WRONGTYPE wrong type"); }
  const std::string &name = cmd[2];

  ZNode *znode = zset_lookup(zset, name.data(), name.size());
  if (!znode) { return resp_int(out, 0); }
  
  zset_delete(zset, znode);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// zscore zset name (search the score of a name)
static void do_zscore(std::vector<std::string> &cmd, Buffer *out){
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){ return resp_err(out, "WRONGTYPE wrong type"); }

  const std::string &name = cmd[2];
  ZNode *znode = zset_lookup(zset, name.data(), name.size());
  if (!znode) { return resp_nil(out); }
  return resp_dbl(out, znode->score);
  
}

//zrank key name (how many nodes comes before the actual one)
static void do_zrank(std::vector<std::string> &cmd, Buffer *out){
  // cmd[1] = key
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){ return resp_err(out,"WRONGTYPE wrong type"); }

  // point query by name using the hashtable (cmd[2] = name)
  const std::string &name = cmd[2];
  ZNode *znode = zset_lookup(zset, name.data(), name.size());
  if (!znode){ return resp_nil(out); }

  int64_t rank = avl_rank(&znode->tree);
  return resp_int(out, rank);
}

struct ZQueryResult {
  std::string name;
  double score;
};

// zquery zset score name offset limit (search by ascending order)
static void do_zquery(std::vector<std::string> &cmd, Buffer *out){
  // we parse the args
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return resp_err(out, "ERR invalid score");
  }
  const std::string &name = cmd[3];
  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return resp_err(out, "ERR invalid offset/limit");
  }

  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){ return resp_err(out, "WRONGTYPE wrong type"); }

  // we collect into a vector first so we know the count up front
  // (RESP wants the array length before the elements)
  std::vector<ZQueryResult> results;

  ZNode *znode = zset_seekge(zset, score, name.data(), name.size());
  znode = znode_offset(znode, offset);

  // walk forward collecting until we hit the end (NULL) or fill the page (limit)
  while (znode && (int64_t)results.size() < limit){
    // znode->name is the flexible char[0] array, znode->len its length
    results.push_back({std::string(znode->name, znode->len), znode->score});
    znode = znode_offset(znode, +1); // next node in ascending order
  }

  // flat output: each result = name + score, so element count is size * 2
  // -> [name1, score1, name2, score2, ...]
  resp_arr(out, (uint32_t)(results.size() * 2));
  for (auto &r : results){
    resp_str(out, r.name.data(), r.name.size());
    resp_dbl(out, r.score);
  }
}

// reverse order from do_zquery (descending order)
static void do_zquery_reversed(std::vector<std::string> &cmd, Buffer *out){
  // we parse the args
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return resp_err(out, "ERR invalid score");
  }
  const std::string &name = cmd[3];
  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return resp_err(out, "ERR invalid offset/limit");
  }

  // we get the zset 
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){ return resp_err(out, "WRONGTYPE wrong type"); }

  std::vector<ZQueryResult> results;
  ZNode *znode = zset_seekle(zset, score, name.data(), name.size());
  znode = znode_offset(znode, -offset);

  while (znode && (int64_t)results.size() < limit){
    results.push_back({std::string(znode->name, znode->len), znode->score});
    znode = znode_offset(znode, -1);
  }

  resp_arr(out, (uint32_t)(results.size() * 2));
  for (auto &r : results){
    resp_str(out, r.name.data(), r.name.size());
    resp_dbl(out, r.score);
  }
  
}
// Authenticate 
static void do_auth(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  if (g_config.password.empty()){
    return resp_err(out, "ERR no password configured");
  }
  if (cmd[1] == g_config.password){
    conn->authenticaded = true;
    conn->failed_attemps = 0;
    return resp_ok(out);
  }
  conn->failed_attemps++;
  if (conn->failed_attemps >= k_max_failed_auth){
    conn->want_close =true;
  }
  return resp_err(out, "ERR invalid password");
}

// SAVE - Do save - stays blocking
static void do_save(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  if (g_rdb_child_pid != -1){
    return resp_err(out, "ERR background save in progress");
  }
  if (rdb_save("dump.rdb")){
    g_data.g_last_save_ms = get_monotonic_msec();
    g_data.g_writes_since_save = 0;
    resp_ok(out);
  } else {
    resp_err(out, "ERR save failed");
  }
}

// BGSAVE - fork , returns immediately
static void do_bgsave(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  if (g_rdb_child_pid != -1){
    return resp_err(out, "ERR background save already in progress");
  }
  rdb_save_background();
  resp_str(out, "Background saving started", sizeof("Background saving started")-1);
}

// asyncdel key - delete in background
static void do_asyncdel(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  // look the key
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);

  if (!hnode){
    return resp_int(out, 0); // key does not exit
  }
  Entry *ent = container_of(hnode, Entry, node);
  // remove from hashtable and heap
  hm_delete(&g_data.db, &ent->node, &hnode_same);
  entry_set_ttl(ent, -1);

  // check if need offloading
  size_t set_size = (ent->type ==  T_ZSET) ? hm_size(&ent->zset.hmap) : 0;

  if (set_size > 1000){
    thread_pool_queue(&g_data.thread_pool, entry_del_func, ent);
  } else {
    entry_del_sync(ent);
  }
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

static void do_info(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;

  uint64_t now_ms = get_monotonic_msec();
  uint64_t uptime_s = (now_ms - g_data.g_server_start_ms) / 1000;
  KeyStats keystats = get_keys_stats();
  size_t memory = get_memory_usage();

  // build the info string 
  char buf[2048];
  int len = snprintf(buf, sizeof(buf),
    "# Server\r\n"
    "version:1.0.0\r\n"
    "uptime_seconds:%llu\r\n"
    "uptime_minutes:%llu\r\n"
    "uptime_hours:%llu\r\n"
    "\r\n"
    "# Clients\r\n"
    "connected_clients:%u\r\n"
    "total_connections:%llu\r\n"
    "\r\n"
    "# Memory\r\n"
    "used_memory_bytes:%zu\r\n"
    "used_memory_mb:%.2f\r\n"
    "\r\n"
    "# Stats\r\n"
    "total_commands:%llu\r\n"
    "\r\n"
    "# Keyspace\r\n"
    "keys_total:%u\r\n"
    "keys_with_ttl:%u\r\n"
    "keys_no_ttl:%u\r\n"
    "\r\n"
    "# Persistence\r\n"
    "rdb_last_save_time:%llu\r\n"
    "rdb_changes_since_save:%u\r\n"
    "rdb_last_save_ok:%d\r\n"
    "rdb_last_save_size_bytes:%zu\r\n"
    "\r\n"
    "# Replication\r\n"
    "role:master\r\n",
    //server
    (unsigned long long)uptime_s,
    (unsigned long long)uptime_s / 60,
    (unsigned long long)uptime_s / 3600,

    // clients 
    g_data.connected_clients,
    (unsigned long long)g_data.g_total_connections,

    // memory
    memory,
    (double)memory / (1024.0 * 1024.0),

    //stats
    (unsigned long long)g_data.g_total_commands,

    // keyspace 
    keystats.total,
    keystats.with_ttl,
    keystats.total - keystats.with_ttl,

    // persistence
    (unsigned long long)(g_data.g_last_save_ms / 1000),
    g_data.g_writes_since_save,
    (int)g_data.g_last_save_ok,
    g_data.g_last_save_size_bytes
  );
  resp_str(out, buf, (size_t)len);         

}

static constexpr Deque k_empty_deque; // cap = 0, count = 0, 

// helper for list (deque) 
// if create is true, makes a new list entry if the key is missing
Deque *get_deque(const std::string &key, bool create, Entry **out_ent = nullptr){
  LookupKey lk;
  lk.key = key;
  lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
  HNode *found = hm_lookup(&g_data.db, &lk.node, &entry_eq);

  if (found){
    Entry *ent = container_of(found, Entry, node);
    if (ent->type != T_DLIST){
      return nullptr;
    }
    if (out_ent) *out_ent = ent;
    return &ent->deque;
  }

  if (!create) {
    return (Deque *)&k_empty_deque;
  }

  // Create a new list (deque) entry
  Entry *ent = entry_new(T_DLIST);
  deque_init(&ent->deque);
  ent->key = key;
  ent->node.hcode = lk.node.hcode;
  hm_insert(&g_data.db, &ent->node);
  if (out_ent) *out_ent = ent;
  return &ent->deque;
}

// LPUSH key
void do_lpush(std::vector<std::string> &cmd, Buffer *out){
  Deque *deque = get_deque(cmd[1], true);
  if (!deque) {
    return resp_err(out, "WRONGTYPE wrong type");
  }
  // push all values
  for (size_t i = 2; i < cmd.size(); ++i){
    deque_push_front(deque, cmd[i]);
  }
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)deque->count);
}

// RPUSH key
void do_rpush(std::vector<std::string> &cmd, Buffer *out){
  Deque *deque = get_deque(cmd[1], true);
  if (!deque){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  for (size_t i = 2; i < cmd.size(); ++i){
    deque_push_back(deque, cmd[i]);
  }
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)deque->count);
}

// LPOP key
void do_lpop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent = nullptr;
  Deque *deque = get_deque(cmd[1], false, &ent);
  if (!deque) {
    return resp_err(out, "WRONGTYPE wrong type");
  }

  std::string val;
  if (!deque_pop_front(deque, &val)){
    return resp_nil(out);
  }

  // if list is now empty, delete the key
  if (deque->count == 0){
    hm_delete(&g_data.db, &ent->node, &entry_eq);
    entry_del(ent);
  }

  g_data.g_writes_since_save++;
  resp_str(out, val.data(), val.size());
}

// RPOP key
void do_rpop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent = nullptr;
  Deque *deque = get_deque(cmd[1], false, &ent);
  if (!deque) {
    return resp_err(out, "WRONGTYPE wrong type");
  }

  std::string val;
  if (!deque_pop_back(deque, &val)){
    return resp_nil(out);
  }

  // if list is now empty, delete the key
  if (deque->count == 0){
    hm_delete(&g_data.db, &ent->node, &entry_eq);
    entry_del(ent);
  }

  g_data.g_writes_since_save++;
  resp_str(out, val.data(), val.size());
}

// LLEN key
void do_llen(std::vector<std::string> &cmd, Buffer *out){
  Deque *deque = get_deque(cmd[1], false);
  if (!deque){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  resp_int(out, (int64_t)deque->count);
}

// LINDEX key index
void do_lindex(std::vector<std::string> &cmd, Buffer *out){
  Deque *deque = get_deque(cmd[1], false);
  if (!deque){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  int64_t idx = 0;
  if (!str2int(cmd[2], idx)){
    return resp_err(out, "ERR invalid index");
  }
  idx = deque_normalize(deque, idx);

  if (idx < 0 || idx >= (int64_t)deque->count){
    return resp_nil(out);
  }

  const std::string *val = deque_get(deque, (size_t)idx);
  resp_str(out, val->data(), val->size());
}

// LRANGE key start stop
void do_lrange(std::vector<std::string> &cmd, Buffer *out){
  Deque *deque =  get_deque(cmd[1], false);
  if  (!deque){
    return resp_err(out, "WRONGTYPE wrong type");
  }

  int64_t start = 0, stop = 0;
  if (!str2int(cmd[2], start) || !str2int(cmd[3], stop)){
    return resp_err(out, "ERR invalid range");
  }

  int64_t n = (int64_t)deque->count;
  start = deque_normalize(deque, start);
  stop = deque_normalize(deque, stop);

  // clamp to valid bounds
  if (start < 0) start = 0;
  if (stop >= n) stop = n - 1;

  if (start > stop || start >= n){
    return resp_arr(out, 0); // empty range
  }

  uint32_t range_len = (uint32_t)(stop - start + 1);
  resp_arr(out, range_len);
  for (int64_t i = start; i <= stop; ++i){
    const std::string *val = deque_get(deque, (size_t)i);
    resp_str(out, val->data(), val->size());
  }
}

void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn) {
  if (cmd.empty()) {
    return resp_err(out, "ERR empty command");
  }

  // command names are case-insesitve
  for (char &ch : cmd[0]){
    ch = (char)tolower((unsigned char)ch);
  }

  // AUTH always allowed
  if (cmd.size() == 2 && cmd[0] == "auth") {
    return do_auth(cmd, out, conn);
  }

  // check authentication
  if (!conn->authenticaded) {
    return resp_err(out, "NOAUTH authentication required");
  }

  if (cmd.size() == 2 && cmd[0] == "get") {
    do_get(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "set") {
    do_set(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "del") {
    do_del(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "asyncdel") {
    do_asyncdel(cmd, out, conn);
  } else if (cmd.size() == 3 && cmd[0] == "pexpire") {
    do_pexpire(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "pttl") {
    do_ttl(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "keys") {
    do_keys(cmd, out);
  } else if (cmd.size() == 4 && cmd[0] == "zadd") {
    do_zadd(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zrem") {
    do_zrem(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zscore") {
    do_zscore(cmd, out);
  } else if (cmd.size() == 6 && cmd[0] == "zquery") {
    do_zquery(cmd, out);
  } else if (cmd.size() == 6 && cmd[0] == "zrevquery") {
    do_zquery_reversed(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zrank") {
    do_zrank(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "info") {
    do_info(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "save") {
    do_save(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "bgsave") {
    do_bgsave(cmd,out);
  } else if (cmd.size() >= 3 && cmd[0] == "lpush") {
    do_lpush(cmd, out);
  } else if (cmd.size() >= 3 && cmd[0] == "rpush") {
    do_rpush(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "lpop") {
    do_lpop(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "rpop") {
    do_rpop(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "llen") {
    do_llen(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "lindex") {
    do_lindex(cmd, out);
  } else if (cmd.size() == 4 && cmd[0] == "lrange") {
    do_lrange(cmd, out);
  } else {
    resp_err(out, "ERR unknown command");
  }
}