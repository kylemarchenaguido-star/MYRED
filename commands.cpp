#include "commands.h"
#include "state.h"
#include "resp.h"
#include "rdb.h"
#include "aof.h"
#include "buffer.h"
#include "common.h"
#include "string.h"
#include "stdio.h"
#include "stdlib.h"
#include "math.h"
#include "ctype.h"
#include "hash.h"
#include "set.h"
#include "fcntl.h"     
#include "sha256.h" 
#include "cred.h"
#include <unistd.h> 
#include <algorithm>
#include <random>
#include <unordered_map>
#include <string_view>
#include <unordered_set>
#include <vector>
#include <utility>
#include <ctime>
#include <cerrno>

static constexpr const char *MSG_WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value";
static constexpr const char *MSG_NOT_INT   = "ERR value is not an integer or out of range";
static constexpr const char *MSG_NOT_FLOAT = "ERR value is not a valid float";
static constexpr const char *MSG_SYNTAX    = "ERR syntax error";
static constexpr const char *MSG_OUT_OF_RANGE = "ERR index out of range";

static void die(const char *msg){
	int err = errno;
	fprintf(stderr, "[%d] %s\n", err, msg);
	abort();
}

enum class Lookup{
  OK,
  MISSING,
  WRONGTYPE
};

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

// LFU and LRU helpers for lookup_entry

// LRU and LFU access accounting
static uint16_t lfu_now_minutes(){
  // we reuse the cached clock
  return (uint16_t)((g_data.g_lru_clock / 60) & 0xFFFF);
}

// halve-ish decay: drop the counter by one per lfu_decay_time minutes of idleness
static uint8_t lfu_decay(uint32_t lru){
  uint16_t last = (uint16_t)(lru >> 8);
  uint8_t counter = (uint8_t)(lru & 0xFF);
  if (g_config.lfu_decay_time <= 0){ return counter; }
  // wraparound is fine
  uint16_t elapsed = (uint16_t)(lfu_now_minutes() - last);
  uint16_t periods = elapsed / (uint16_t)g_config.lfu_decay_time;
  if (periods){ counter = (periods > counter) ? 0 : (uint8_t)(counter - periods); }
  return counter;
}

// Probalistic log increment: harder to advance as the counter grows
static uint8_t lfu_log_incr(uint8_t counter){
  if (counter == 255){ return 255; }
  double baseval = (double)counter - LFU_INIT_VAL;
  if (baseval < 0){ baseval = 0; }
  double p = 1.0 / (baseval * g_config.lfu_log_factor + 1); 
  double r = (double)rand_idx(1000000) / 1000000.0;
  if (r < p){ counter++;}
  return counter;
}

static bool policy_is_lfu(){
  return g_config.maxmemory_policy == MaxmemoryPolicy::ALLKEYS_LFU ||
         g_config.maxmemory_policy == MaxmemoryPolicy::VOLATILE_LFU;
}

void entry_init_access(Entry *ent){
  if (policy_is_lfu()){
    ent->lru = ((uint32_t)lfu_now_minutes() << 8 | LFU_INIT_VAL);
  } else {
    ent->lru = g_data.g_lru_clock;
  }
}

void entry_touch_access(Entry *ent){
  if (policy_is_lfu()){
    uint8_t c = lfu_decay(ent->lru);
    c = lfu_log_incr(c);
    ent->lru = ((uint32_t)lfu_now_minutes() << 8) | c;
  } else {
    ent->lru = g_data.g_lru_clock;
  }
}

// WARNING: swaps cmd[i] into the LookupKey, leaving cmd[i] empty after the call.
// If you need cmd[i] after the call, use a non-destructive hm_lookup copy instead.
static Lookup lookup_entry(std::string &keystr, uint32_t want_type, bool create, Entry **out_ent){
  LookupKey key;
  key.key.swap(keystr);
  key.node.hcode = str_hash((const uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (node){
    Entry *ent = container_of(node, &Entry::node);
    if (!expire_if_needed(ent)){                 // alive
      if (ent->type != want_type) return Lookup::WRONGTYPE;
      // we stamp on every hit
      entry_touch_access(ent);
      if (out_ent) *out_ent = ent;
      return Lookup::OK;
    }
    // expired & deleted -> fall through as missing
  }

  if (!create) return Lookup::MISSING;

  Entry *ent = entry_new(want_type);
  ent->key.swap(key.key);                        // key.key holds the real key
  ent->node.hcode = key.node.hcode;
  entry_init_access(ent);
  hm_insert(&g_data.db, &ent->node);
  if (out_ent) *out_ent = ent;
  return Lookup::OK;                             // create never returns MISSING
}

// Random samplers

// A unifromly-ish random live entry from the ehole keyspace (both rehash tables).
static Entry *db_random_entry(){
  HNode *h = hm_random(&g_data.db);
  return h ? container_of(h, &Entry::node) : nullptr; 
}

// A random key that has a TTL - the ttl heap holds exactly those.
static Entry *volatile_random_entry(){
  if (g_data.heap.empty()){ return nullptr; }
  size_t i = rand_idx(g_data.heap.size());
  // ref == &ent.heap_idx
  return container_of(g_data.heap[i].ref, &Entry::heap_idx);
}

// Eviction score: Higher = better victim
static uint64_t evict_score(const Entry *ent){
  if (policy_is_lfu()){
    // lowest frecuency wins
    return 255 - (ent->lru & 0xFF);
  } 
  // largest idle wins
  return (uint64_t)((g_data.g_lru_clock - ent->lru) & LRU_CLOCK_MAX);
}

// Pick a key to evict for the active policy, or nullptr if there's nothing to take
// (noeviction, or volatile-* policy with no TTL keys -> caller must return -OOM)
Entry *evict_pick_victim(){
  using P = MaxmemoryPolicy;
  P pol = g_config.maxmemory_policy;

  switch (pol){
    case P::NOEVICTION:
      return nullptr;
    case P::ALLKEYS_RANDOM:
      // nullptr if no volatile keys -> OOM
      return db_random_entry();
    case P::VOLATILE_RANDOM:
      return volatile_random_entry();
    case P::VOLATILE_TTL:
      // nearest expiry == heap root, nearly free
      return g_data.heap.empty() ? nullptr : container_of(g_data.heap[0].ref, &Entry::heap_idx);
    default: break; // LRU / LFU: best of N sampling below
  }

  bool volatile_only = (pol == P::VOLATILE_LRU || pol == P::VOLATILE_LFU);
  Entry *best = nullptr;
  uint64_t best_score = 0;
  for (int i = 0; i < g_config.maxmemory_samples; ++i){
    Entry *e = volatile_only ? volatile_random_entry() : db_random_entry();
    if (!e){ break; }
    uint64_t s = evict_score(e);
    if (!best || s > best_score){ best = e; best_score = s; }
  }
  // nullptr -> caller OOMs (volatile feedback)
  return best;
}


//gets a value from key
static void do_get(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_STR, false, &ent)){
  case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
  case Lookup::MISSING: return resp_nil(out);
  case Lookup::OK:  break;
  }
  resp_str(out, entry_str(ent).data(), entry_str(ent).size());
}

// sets a key with value in the hashtab
static void do_set(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  entry_str(ent).swap(cmd[2]);
  entry_set_ttl(ent, -1);
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  return resp_ok(out);
}

// mult = 1000 for SETEX (s), mult = 1 for PSETEX (ms)
static void setex_generic(std::vector<std::string> &cmd, Buffer *out, int64_t mult){
  int64_t ttl = 0;
  if (!str2int(cmd[2], ttl) || ttl <= 0){
    return resp_err(out, "ERR invalid expire time in setex command");
  }
  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, MSG_WRONGTYPE);
  }
  entry_str(ent).swap(cmd[3]);
  entry_set_ttl(ent, ttl * mult);
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  return resp_ok(out);
}

static void do_setex(std::vector<std::string> &cmd, Buffer *out){ setex_generic(cmd, out, 1000); }
static void do_psetex(std::vector<std::string> &cmd, Buffer *out){ setex_generic(cmd, out, 1); }

static void do_setnx(std::vector<std::string> &cmd, Buffer *out){
  LookupKey lk;
  lk.key = cmd[1]; // we copy, not swap we need cmd[1]
  lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
  if (node){
    Entry *e = container_of(node, &Entry::node);
    if (!expire_if_needed(e)){
      return resp_int(out, 0); // alive so we dont do nothing
    }
    // expired -> fall through
  }
  // now we use the cmd[1]
  Entry *ent;
  lookup_entry(cmd[1], T_STR, true, &ent);
  entry_str(ent).swap(cmd[2]);
  mem_reaccount(ent); 
  g_data.g_writes_since_save++;
  resp_int(out, 1);
}

static void do_getset(std::vector<std::string> &cmd, Buffer *out){
  LookupKey lk;
  lk.key = cmd[1]; // we copy
  lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);

  if (node){
    Entry *ent = container_of(node, &Entry::node);
    if (!expire_if_needed(ent)){
      if (ent->type != T_STR){
        return resp_err(out, MSG_WRONGTYPE);
      }
      std::string old = std::move(entry_str(ent)); // extract the old value
      entry_str(ent).swap(cmd[2]); // set the new value
      entry_set_ttl(ent, -1);
      mem_reaccount(ent);
      g_data.g_writes_since_save++;
      return resp_str(out, old.data(), old.size());
    }
    // expired and deleted, falls through
  }
  // key missing create entry and return  nil
  Entry *ent = entry_new(T_STR);
  ent->key = std::move(lk.key);
  ent->node.hcode = lk.node.hcode;
  entry_str(ent).swap(cmd[2]);
  mem_reaccount(ent);
  hm_insert(&g_data.db, &ent->node);
  g_data.g_writes_since_save++;
  resp_nil(out);
}

static void do_getdel(std::vector<std::string> &cmd, Buffer *out){
 Entry *ent;
  switch (lookup_entry(cmd[1], T_STR, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }
  std::string val = std::move(entry_str(ent)); // we extract the values before freeing
  hm_delete(&g_data.db, &ent->node, &hnode_same);
  entry_del(ent);
  g_data.g_writes_since_save++;
  resp_str(out, val.data(), val.size());
}

static void do_getex(std::vector<std::string> &cmd, Buffer *out){
 Entry *ent;
  switch (lookup_entry(cmd[1], T_STR, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }
  // it just a bare getex key, just get no ttl change
  if (cmd.size() == 2){
    return resp_str(out, entry_str(ent).data(), entry_str(ent).size());
  }

  std::string opt = cmd[2];
  for (char &c : opt){ c = (char)tolower((unsigned char)c); }

  if (opt == "persist"){
    if (cmd.size() != 3){ return resp_err(out, MSG_SYNTAX); }
    if (entry_has_ttl(ent)){
      entry_set_ttl(ent, -1);
      g_data.g_writes_since_save++;
    }
    return resp_str(out, entry_str(ent).data(), entry_str(ent).size());
  }

  if (cmd.size() != 4){ return resp_err(out, MSG_SYNTAX); }
  int64_t v = 0;
  if (!str2int(cmd[3], v)){ return resp_err(out, MSG_NOT_INT); }

  int64_t ttl_ms = 0;
  if (opt == "ex"){ if (v <= 0 || v > INT64_MAX / 1000) return resp_err(out, "ERR invalid expire time in getex command"); ttl_ms = v * 1000; }
  else if (opt == "px"){ if (v <= 0) return resp_err(out, "ERR invalid expire time in getex command"); ttl_ms = v; }
  else if (opt == "exat"){ 
    if (v > INT64_MAX / 1000){ return resp_err(out, "ERR invalid expire time in getex command"); }
    ttl_ms = v * 1000 - (int64_t)get_wall_msec(); 
  }
  else if (opt == "pxat"){ ttl_ms = v - (int64_t)get_wall_msec(); }
  else { return resp_err(out, MSG_SYNTAX); }

  if (ttl_ms <= 0){
    // past timestamp so we get the value then delete the key
    std::string val = std::move(entry_str(ent));
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
    g_data.g_writes_since_save++;
    return resp_str(out, val.data(), val.size());
  }
  entry_set_ttl(ent, ttl_ms);
  g_data.g_writes_since_save++;
  resp_str(out, entry_str(ent).data(), entry_str(ent).size());
}

// MGET key [key..]
static void do_mget(std::vector<std::string> &cmd, Buffer *out){
  resp_arr(out, (uint32_t)(cmd.size() - 1)); // emit N header first
  for (size_t i = 1; i < cmd.size(); ++i){
    Entry *ent;
    switch (lookup_entry(cmd[i], T_STR, false, &ent)){
      case Lookup::WRONGTYPE: resp_nil(out); break; // type mismatch -> nil, NOT error
      case Lookup::MISSING:   resp_nil(out); break;
      case Lookup::OK:        resp_str(out, entry_str(ent).data(), entry_str(ent).size()); break;
    }
  }
}

// APPEND key value -> new lenght (integer)
static void do_append(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, MSG_WRONGTYPE);
  }
  entry_str(ent) += cmd[2];
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)entry_str(ent).size());
}

// STRLEN key -> byte length (integer), 0 if missing
static void do_strlen(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_STR, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, MSG_WRONGTYPE);
    case Lookup::MISSING:   return resp_int(out, 0);  // missing -> 0, not nil
    case Lookup::OK:        break;
  }
  resp_int(out, (int64_t)entry_str(ent).size());
}

// GETRANGE key start end -> bulk string
static void do_getrange(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_STR, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, MSG_WRONGTYPE);
    case Lookup::MISSING:   return resp_str(out, "", 0);
    case Lookup::OK:        break;
  }

  int64_t start, end;
  if (!str2int(cmd[2], start) || !str2int(cmd[3], end)){
    return resp_err(out, MSG_NOT_INT);
  }

  int64_t len = (int64_t)entry_str(ent).size();
  // resolve negatives (Redis: -1 = last byte and .....)
  if (start < 0){ start += len; }
  if (end < 0){ end += len; }
  // clamp to valid range
  if (start < 0){ start = 0; }
  if (end <  0){ return resp_str(out, "", 0); } // still negative negative -> empty 
  if (end >= len){ end = len - 1; }
  if (start > end){ return resp_str(out, "", 0); }

  size_t offset = (size_t)start;
  size_t count = (size_t)(end - start + 1);
  resp_str(out, entry_str(ent).data() + offset, count);
}

// SETRANGE key offset value -> new length (integer)
static void do_setrange(std::vector<std::string> &cmd, Buffer *out){
  int64_t offset = 0 ;
  if (!str2int(cmd[2], offset)){
    return resp_err(out, MSG_NOT_INT);
  }
  if (offset < 0){
    return resp_err(out, "ERR offset is not an integer or out of range");
  }
  const int64_t MAX_OFFSET = 512LL * 1024 * 1024;
  if (offset >= MAX_OFFSET){
    return resp_err(out, "ERR string exceeds maximun allowed size (512MB)");
  }

  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, MSG_WRONGTYPE);
  }

  size_t new_end = (size_t)offset + cmd[3].size();
  if (entry_str(ent).size() < new_end){
    entry_str(ent).resize(new_end, '\0'); // zero-pad any gap
  }
  if (!cmd[3].empty()){
    memcpy(&entry_str(ent)[offset], cmd[3].data(), cmd[3].size());
  }
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)entry_str(ent).size());
}

// MSET key val [key val ...]
static void do_mset(std::vector<std::string> &cmd, Buffer *out){
  if (cmd.size() < 3 || (cmd.size() & 1) == 0) {
    return resp_err(out, "ERR wrong number of arguments for 'mset' command");
  }
  // pre-scan: non-destructive check (copy, not swap) so cmd[i] survives for write pass
  for (size_t i = 1; i < cmd.size(); i += 2){
    LookupKey lk;
    lk.key = cmd[i];
    lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
    HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
    if (node){
      Entry *e = container_of(node, &Entry::node);
      if (!expire_if_needed(e) && e->type != T_STR){
        return resp_err(out, MSG_WRONGTYPE);
      }
    }
  }
  // write pass: lookup_entry consumes cmd[i] here, that's fine
  for (size_t i = 1; i < cmd.size(); i += 2){
    Entry *ent;
    lookup_entry(cmd[i], T_STR, true, &ent);
    entry_str(ent).swap(cmd[i + 1]);
    mem_reaccount(ent);
  }
  g_data.g_writes_since_save += (uint32_t)((cmd.size() - 1) / 2);
  resp_ok(out);
}

// MSETNX key val [key val ...]
static void do_msetnx(std::vector<std::string> &cmd, Buffer *out){
  if (cmd.size() < 3 || (cmd.size() & 1) == 0) {
    return resp_err(out, "ERR wrong number of arguments for 'mset' command");
  }
  // existence check: copy, don't swap — cmd[i] must survive for the write pass
  for (size_t i = 1; i < cmd.size(); i += 2){
    LookupKey lk;
    lk.key = cmd[i];   // was cmd[1] — always checked the same key
    lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
    HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
    if (node){
      Entry *e = container_of(node, &Entry::node);
      if (!expire_if_needed(e)){
        return resp_int(out, 0);  // any key exists -> set nothing
      }
    }
  }
  // no key exists -> set all
  for (size_t i = 1; i < cmd.size(); i += 2){
    Entry *ent;
    lookup_entry(cmd[i], T_STR, true, &ent);  // was cmd[1]
    entry_str(ent).swap(cmd[i + 1]);
    mem_reaccount(ent);
  }
  g_data.g_writes_since_save += (uint32_t)((cmd.size() - 1) / 2);
  resp_int(out, 1);
}

// delta already carries the correct sign (+1, -1, +N, -N)
static void incr_generic(std::vector<std::string> &cmd, Buffer *out, int64_t delta){
  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) ==  Lookup::WRONGTYPE){
    return resp_err(out, MSG_WRONGTYPE);
  }
  int64_t cur = 0;
  if (!entry_str(ent).empty() && !str2int(entry_str(ent), cur)){
    return resp_err(out, MSG_NOT_INT);
  }
  if ((delta > 0 && cur > INT64_MAX - delta) || (delta < 0 && cur < INT64_MIN - delta)){
    return resp_err(out, "ERR increment or decrement would overflow");
  }
  cur += delta;
  entry_str(ent) = std::to_string(cur);
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, cur);
}

static void do_incr(std::vector<std::string> &cmd, Buffer *out){
  incr_generic(cmd, out, 1);
}

static void do_decr(std::vector<std::string> &cmd, Buffer *out){
  incr_generic(cmd, out, -1);
}

static void do_incrby(std::vector<std::string> &cmd, Buffer *out){
  int64_t delta = 0;
  if (!str2int(cmd[2], delta)){
    return resp_err(out, MSG_NOT_INT);
  }
  incr_generic(cmd, out, delta);
} 

static void do_decrby(std::vector<std::string> &cmd, Buffer *out){
  int64_t by = 0;
  if (!str2int(cmd[2], by)){
    return resp_err(out, MSG_NOT_INT);
  }
  // -INT64_MIN overflows int64
  if (by == INT64_MIN){
    return resp_err(out, "ERR increment or decrement would overflow");
  }
  incr_generic(cmd, out, -by);
} 

static void do_incrbyfloat(std::vector<std::string> &cmd, Buffer *out){
  double delta;
  if (!str2dbl(cmd[2], delta)){
    return resp_err(out, "ERR value is not a float or out of range");
  }
  Entry *ent;
  if (lookup_entry(cmd[1], T_STR, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, MSG_WRONGTYPE);
  }
  double cur = 0.0;
  if (!entry_str(ent).empty() && !str2dbl(entry_str(ent), cur)){
    return resp_err(out, "ERR value is not a float or out of range");
  }
  double result = cur + delta;
  if (isinf(result) || isnan(result)){
    return resp_err(out, "ERR increment would produce NaN or infinity");
  }
  char buf[64];
  snprintf(buf, sizeof(buf), "%.17g", result);
  entry_str(ent) = buf;
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_str(out, entry_str(ent).data(), entry_str(ent).size());
}

// deletes a key and value
static void do_del(std::vector<std::string> &cmd, Buffer *out){
  int64_t deleted = 0;
  for (size_t i = 1; i < cmd.size(); ++i){
    LookupKey lk;
    lk.key.swap(cmd[i]);
    lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
    HNode *node = hm_delete(&g_data.db, &lk.node, &entry_eq);
    if (node){
      entry_del(container_of(node, &Entry::node));
      ++deleted;
    }
  }
  if (deleted > 0){ g_data.g_writes_since_save += (uint32_t)deleted; }
  resp_int(out, deleted);
}

struct KeyStats {
  uint32_t total;
  uint32_t with_ttl;
};

struct KeysCtx {
  const std::string *pattern; // the glob to match against
  std::vector<std::pair<const char *, size_t>> *hits; // matched key views
};

// forwarod declariton for do_keys
static bool glob_match(const char *p, size_t plen, const char *s, size_t slen);

// WARNING: O(N) full keyspace scan — blocks the event loop.
// Never run against a large DB in production; steer clients to SCAN instead.
static bool cb_keys_emit(HNode *node, void *arg) {
    Buffer *out = (Buffer *)arg;
    Entry *ent = container_of(node, &Entry::node);
    resp_str(out, ent->key.data(), ent->key.size());
    return true;
}

static bool cb_keys_collect(HNode *node, void *arg){
  KeysCtx *ctx = (KeysCtx*)arg;
  Entry *ent = container_of(node, &Entry::node);
  if (glob_match(ctx->pattern->data(), ctx->pattern->size(), ent->key.data(), ent->key.size())){
    ctx->hits->emplace_back(ent->key.data(), ent->key.size());
  }
  return true;
}

static void do_keys(std::vector<std::string> &cmd, Buffer *out) {
  // bare keys or keys * -> every key, streamed
  if (cmd.size() < 2 || cmd[1] == "*"){
    resp_arr(out, (uint32_t)hm_size(&g_data.db));
    hm_foreach(&g_data.db, cb_keys_emit, out);    
    return;
  }
  // keys <pattern> collect matches in one pass, then emit the exact count
  std::vector<std::pair<const char *, size_t>> hits;
  KeysCtx ctx { &cmd[1], &hits };
  hm_foreach(&g_data.db, cb_keys_collect, &ctx);
  resp_arr(out, (uint32_t)hits.size());
  for (auto &h : hits){ resp_str(out, h.first, h.second); }
}


static KeyStats get_keys_stats(){
  KeyStats stats;
  stats.total = (uint32_t)hm_size(&g_data.db);
  stats.with_ttl = (uint32_t)g_data.heap.size();
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

// shared by PEXPIRE (mult=1) and EXPIRE (mult=1000): set a TTL on a key.
// non-positive TTL deletes the key (Redis semantics). returns 1 if the key
// existed (ttl set or key deleted), 0 if missing.
static void expire_generic(std::vector<std::string> &cmd, Buffer *out, int64_t mult){
  int64_t ttl = 0;
  if (!str2int(cmd[2], ttl)){
    return resp_err(out, "ERR invalid TTL");
  }
  int64_t ttl_ms = ttl * mult;

  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node) { return resp_int(out, 0); }

  Entry *ent = container_of(node, &Entry::node);
  if (expire_if_needed(ent)){ return resp_int(out, 0); }
  if (ttl_ms <= 0){
    // non-positive TTL means "already expired" -> delete now
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
    g_data.g_writes_since_save++;
    return resp_int(out, 1);
  }
  entry_set_ttl(ent, ttl_ms);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// PEXPIRE key ttl_ms (set a ttl in seconds)
static void do_pexpire(std::vector<std::string> &cmd, Buffer *out){
  return expire_generic(cmd, out, 1);
}

// EXPIRE key ttl_seconds (set a ttl in miliseconds)
static void do_expire(std::vector<std::string> &cmd, Buffer *out){
  return expire_generic(cmd, out, 1000);
}

// shared by PTTL (div=1, ms) and TTL (div=1000, seconds rounded).
// -2 = no such key, -1 = no TTL, else remaining time.
static void ttl_generic(std::vector<std::string> &cmd, Buffer *out, int64_t div){
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){ return resp_int(out, -2); } // not found

  Entry *ent = container_of(node, &Entry::node);
  if (expire_if_needed(ent)){ return resp_int(out, -2); } // expired -> gone

  if (!entry_has_ttl(ent)){
    return resp_int(out, -1); // no ttl
  }

  uint64_t expire_at = g_data.heap[ent->heap_idx].val;
  uint64_t now_ms = get_monotonic_msec();
  int64_t remaining = expire_at > now_ms ? (int64_t)(expire_at - now_ms) : 0;

  if (div > 1){ remaining = (remaining + div / 2) / div; } // round to seconds
  return resp_int(out, remaining);
}

// PTTL key  (remaining time in milliseconds)
static void do_ttl(std::vector<std::string> &cmd, Buffer *out){
  return ttl_generic(cmd, out, 1);
}

// TTL key  (remaining time in seconds)
static void do_ttl_seconds(std::vector<std::string> &cmd, Buffer *out){
  return ttl_generic(cmd, out, 1000);
}

// EXISTS key  -> 1 if present (and not expired), else 0
static void do_exists(std::vector<std::string> &cmd, Buffer *out){
  int64_t count = 0;
  for (size_t i = 1; i < cmd.size(); ++i){
    LookupKey lk;
    lk.key = cmd[i];
    lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
    HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
    if (!node){ continue; }
    Entry *ent = container_of(node, &Entry::node);
    if (!expire_if_needed(ent)){ ++count; }
  }
  resp_int(out, count);
}

// TYPE key  -> simple string: string | zset | list | none
static void do_type(std::vector<std::string> &cmd, Buffer *out){
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);

  if (!node){ return resp_simple(out, "none"); }
  Entry *ent = container_of(node, &Entry::node);

  if (expire_if_needed(ent)){ return resp_simple(out, "none"); }
  const char *t = "none";

  if (ent->type == T_STR)        { t = "string"; }
  else if (ent->type == T_ZSET)  { t = "zset"; }
  else if (ent->type == T_DLIST) { t = "list"; }
  else if (ent->type == T_HASH)  { t = "hash"; }
  else if (ent->type == T_SET)   { t = "set"; }

  return resp_simple(out, t);
}

// PERSIST key  -> remove the TTL but keep the key. 1 if a TTL was removed, else 0
static void do_persist(std::vector<std::string> &cmd, Buffer *out){
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);

  if (!node){ return resp_int(out, 0); }
  Entry *ent = container_of(node, &Entry::node);

  if (expire_if_needed(ent)){ return resp_int(out, 0); } // expired -> gone
  if (!entry_has_ttl(ent)){ return resp_int(out, 0); } // no TTL to remove
  entry_set_ttl(ent, -1); // detach from the TTL heap

  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// zadd and zset (score, name)
static void do_zadd(std::vector<std::string> &cmd, Buffer *out){
  if (cmd.size() < 4 || (cmd.size() % 2 ) != 0){
    return resp_err(out, "ERR wrong number of arguments for 'zadd command");
  }
  // validate all the scores first, ZADD is atomic, a bad score adds nothing
  for (size_t i = 2; i < cmd.size(); i += 2){
    double tmp;
    if (!str2dbl(cmd[i], tmp)){ return resp_err(out, "ERR value is not a valid float"); }
  }
  

  Entry *ent;
  if (lookup_entry(cmd[1], T_ZSET, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }

  // add or update the tuple
  int64_t added = 0;
  bool changed = false;
  ZSet *zs = &entry_zset(ent);
  for (size_t i = 2; i + 1 < cmd.size(); i += 2){
    double score;
    str2dbl(cmd[i], score);
    if (ZNode *zn = zset_lookup(zs, cmd[i + 1].data(), cmd[i + 1].size())){
      if (zn->score != score){
        zset_update(zs, zn, score);
        changed = true;
      }
    } else {
      zset_insert(zs, cmd[i + 1].data(), cmd[i + 1].size(), score);
      ++added;
      changed = true;
    }
  }
  if (changed){
    mem_reaccount(ent);
    g_data.g_writes_since_save++;
  }
  return resp_int(out, added);
}

// zrem zset name (search and remove)
static void do_zrem( std::vector<std::string> &cmd, Buffer *out){
 Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_int(out, 0);
    case Lookup::OK:        break;
  }
  
  ZNode *znode = zset_lookup(&entry_zset(ent), cmd[2].data(), cmd[2].size());
  if (!znode) { return resp_int(out, 0); }
  
  zset_delete(&entry_zset(ent), znode);
  g_data.g_writes_since_save++;
  if (hm_size(&entry_zset(ent).hmap) == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else {
    mem_reaccount(ent);
  }
  return resp_int(out, 1);
}

// zscore zset name (search the score of a name)
static void do_zscore(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }
  ZNode *znode = zset_lookup(&entry_zset(ent), cmd[2].data(), cmd[2].size());
  return znode ? resp_dbl(out, znode->score) : resp_nil(out);
  
}

//zrank key name (how many nodes comes before the actual one)
static void do_zrank(std::vector<std::string> &cmd, Buffer *out){
  // cmd[1] = key
 Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }
  // point query by name using the hashtable (cmd[2] = name)
  ZNode *znode = zset_lookup(&entry_zset(ent), cmd[2].data(), cmd[2].size());
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

  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return resp_err(out, "ERR invalid offset/limit");
  }

 Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_arr(out, 0);
    case Lookup::OK:        break;
  }
  // we collect into a vector first so we know the count up front
  // (RESP wants the array length before the elements)
  std::vector<ZQueryResult> results;

  ZNode *znode = zset_seekge(&entry_zset(ent), score, cmd[3].data(), cmd[3].size());
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
  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return resp_err(out, "ERR invalid offset/limit");
  }

  // we get the zset 
  Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_arr(out, 0);
    case Lookup::OK:        break;
  }
  std::vector<ZQueryResult> results;
  ZNode *znode = zset_seekle(&entry_zset(ent), score, cmd[3].data(), cmd[3].size());
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

static void do_zpopmin(std::vector<std::string> &cmd, Buffer *out){
  int64_t count = 1;
  if (cmd.size() >= 3){
    if (!str2int(cmd[2], count)){ return resp_err(out, MSG_NOT_INT); }
    if (count < 0){ count = 0; }
  }
  Entry *ent;
  switch (lookup_entry(cmd[1], T_ZSET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, MSG_WRONGTYPE);
    case Lookup::MISSING: return resp_arr(out, 0);
    case Lookup::OK: break;
  }
  ZSet *zset = &entry_zset(ent);

  // collect the lowest-score members first - RESP needs the array lenght up front
  std::vector<ZQueryResult> popped;
  for (int64_t i = 0; i < count && zset->root; ++i){
    AVLNode *node = zset->root;
    // leftmost = min 
    while (node->left){ node = node->left; }
    ZNode *zn = container_of(node, &ZNode::tree);
    popped.push_back({ std::string(zn->name, zn->len), zn->score} );
    zset_delete(zset, zn);
  }
  // pairs of (member, score)
  resp_arr(out, (uint32_t)(popped.size() * 2));
  for (const auto &p : popped){
    resp_str(out, p.name.data(), p.name.size());
    resp_dbl(out, p.score);
  }

  if (!popped.empty()){
    g_data.g_writes_since_save++;
    if (hm_size(&zset->hmap) == 0){
      hm_delete(&g_data.db, &ent->node, &hnode_same);
      entry_del(ent);
    } else {
      mem_reaccount(ent);
    }
  }

}

static int g_audit_fd = -1; 
std::string g_audit_last_error;

void audit_open(const std::string &path){
  if (g_audit_fd >= 0 && g_audit_fd != STDERR_FILENO){ close(g_audit_fd); }
  g_audit_fd = -1;
  if (path.empty()){ return; } // disable
  if (path == "stderr"){ g_audit_fd = STDERR_FILENO; return; }
  int fd = open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0640);
  if (fd < 0){ g_audit_last_error = std::string("open ") + path + ": " + strerror(errno); return; }
  g_audit_fd = fd;
}

// one write() per line: best-effort, recors a sticky error on failure
static void audit_write(const char *event, const std::string &peer,
                        const std::string &user, const std::string &extra){
  if (g_audit_fd < 0){ return; }
  char ts[32];
  time_t now = time(nullptr);
  struct tm tmv;
  gmtime_r(&now, &tmv);
  strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", &tmv);
  std::string line = "ts=";
  line += ts; line += " event="; line += event;
  line += " peer="; line += peer;
  line += " user="; line += user;
  line += extra; line += "\n";
  if (write(g_audit_fd, line.data(), line.size()) < 0){
    g_audit_last_error = std::string ("write: ") + strerror(errno);
  }
}

void audit_event(const char *event, const Conn *conn, const std::string &extra){
  audit_write(event, conn ? conn->peer : "-",
              (conn && conn->user) ? conn->user->name : std::string("-"), extra);
}

void audit_reject(const std::string &peer, const char *reason){
  audit_write("accept_reject", peer, "-", std::string(" reason=") + reason);
}

// async auth: parse on the loop, KDF on a worker, apply on the loop
struct AuthJob {
  int fd; // where to find the conn again
  uint64_t conn_id; // unique: dropped if the fd was reused
  std::string uname;
  std::string pass; // worker owned plaintext: worker wipes it
  std::vector<std::string> hashes; // snapshot, the worker does not touch g_config.users
  bool user_known = false; // snapshot of exits+enable+has_password
  bool ok = false; 
  std::string matched; // which stored credential verified
  std::string rehashed; // its argon2id replacement, empty = no upgraded needed
};  

static int g_auth_inflight = 0; // main-thread only (queue++ / completetion--)
static const int k_max_auth_inflight = 4; // bounds argon2 memory to 4 x 19MIB

// main thread (via loop_post)
static void auth_complete(AuthJob *job){
  g_auth_inflight--;
  Conn *c = (job->fd >= 0 && (size_t)job->fd < g_data.fd2conn.size())
             ? g_data.fd2conn[job->fd] : nullptr; 
  // conn died mid-verify
  if (!c || c->id != job->conn_id){ delete job; return; }
  c->auth_pending = false;

  // re-resolve now, the user may have been deleted/disable during the verify
  // and unknown-user jobs must never authenticate even if ok were somehow true
  auto it = g_config.users.find(job->uname);
  bool ok = job->ok && job->user_known
            && it != g_config.users.end() && it->second.enable;
  if (ok){
    c->user = &it->second;
    c->failed_attemps = 0;
    // rehash on auth: swap in the upgrade only if the exact old entry still exists
    if (!job->rehashed.empty()){
      auto &v = it->second.pw_hashes;
      auto slot = std::find(v.begin(), v.end(), job->matched);
      if (slot != v.end()){
        *slot = job->rehashed;
        if (job->uname == "default" && g_config.password == job->matched){
          // keep requirepass rewwrite in sync
          g_config.password = job->rehashed;
        }
        audit_event("cred_rehash", c, " target=" + job->uname);
      }
    }
    audit_event("auth_success", c, "");
    resp_ok(&c->outgoing);
  } else {
    c->failed_attemps++;
    if (c->failed_attemps >= k_max_failed_auth){ c->want_close =true; }
    audit_event("auth_fail", c, " target=" + job->uname + " result=wrongpass");
    resp_err(&c->outgoing, "WRONGPASS invalid username-password pair or user is disable");
  }
  delete job;
  conn_resume(c);
}

// worker thread: KDF only, no shared state
static void auth_verify_job(void *arg){
  AuthJob *job = (AuthJob *)arg; 
  for (const std::string &stored : job->hashes){
    if (cred_verify(job->pass, stored)){ 
      job->ok = true;
      // migration window: the plaintext exists here, upgrade weak entries now
      if (job->user_known && cred_needs_rehash(stored)){
        job->matched = stored;
        job->rehashed = cred_hash_new(job->pass); // empty on failure, skipped
      }
      break;
    }
  }
  if (!job->pass.empty()){ secure_zero(&job->pass[0], job->pass.size()); }
  loop_post([job](){ auth_complete(job); });
}

// Authenticate 
static void do_auth(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  std::string uname, pass;
  // AUTH <pass>
  if (cmd.size() == 2){ uname = "default"; pass = cmd[1]; }
  //AUTH <user> <pass>
  else if (cmd.size() == 3){ 
    uname = cmd[1]; // AUTH <pass>
    pass = cmd[2]; // AUTH <user ><pass>
  } else { return resp_err(out, "ERR wwrong number of arguments for 'auth' command"); }

  // Dos bound
  if (g_auth_inflight >= k_max_auth_inflight){
    secure_zero(&cmd[cmd.size() - 1][0], cmd[cmd.size() - 1].size());
    if (!pass.empty()){ secure_zero(&pass[0], pass.size()); }
    return resp_err(out, "BUSY too many pending AUTH attempts, try again");
  }

  AuthJob *job = new AuthJob();
  job->fd = conn->fd;
  job->conn_id = conn->id;
  job->uname = uname;
  job->pass = std::move(pass);

  auto it = g_config.users.find(uname);
  if (it != g_config.users.end() && it->second.enable && !it->second.pw_hashes.empty()){
    job->user_known = true;
    // deep copy: registry can mutate mid-verify
    job->hashes = it->second.pw_hashes;
  } else {
    job->user_known = false;
    job->hashes.push_back(cred_dummy());
  }

  // wipe the cmd copy now
  secure_zero(&cmd[cmd.size() - 1][0], cmd[cmd.size() - 1].size());

  conn->auth_pending =true;
  g_auth_inflight++;
  thread_pool_queue(&g_data.thread_pool, auth_verify_job, job);
  // no reply here, auth_complete() writes it into conn->outgoing
  (void)out;
}

// SAVE - Do save - stays blocking
static void do_save(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  rdb_check_background_save(); // reap child before checking
  if (g_rdb_child_pid != -1){
    return resp_err(out, "ERR background save in progress");
  }
  if (rdb_save(g_config.dump_path.c_str())){
    resp_ok(out);
  } else {
    resp_err(out, "ERR save failed");
  }
}

// BGREWRITEAOF - aof fork save
static void do_bgrewriteaof(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  aof_rewrite_background();
  resp_simple(out, "Background append only file rewriting started");
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
static void do_asyncdel(std::vector<std::string> &cmd, Buffer *out){
  // look the key
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);

  if (!hnode){
    return resp_int(out, 0); // key does not exit
  }
  Entry *ent = container_of(hnode, &Entry::node);
  // remove from hashtable and heap
  hm_delete(&g_data.db, &ent->node, &hnode_same);

  // check if need offloading
  entry_del(ent);
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
  char buf[4096];
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
    "used_memory:%zu\r\n"
    "used_memory_human:%.2fM\r\n"
    "used_memory_rss:%zu\r\n"
    "mem_fragmentation_ratio:%.2f\r\n"
    "maxmemory:%zu\r\n"
    "maxmemory_policy:%s\r\n"
    "evicted_keys:%llu\r\n"
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
    "aof_enabled:%d\r\n"
    "aof_current_size:%zu\r\n"
    "aof_base_size:%zu\r\n"
    "aof_pending_rewrite:%d\r\n"
    "aof_last_write_status:%s\r\n"
    "aof_last_bgrewrite_status:%s\r\n"
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
    g_data.used_memory,
    (double)g_data.used_memory / (1024.0 * 1024.0),
    memory,                                                   // RSS from get_memory_usage()
    g_data.used_memory ? (double)memory / (double)g_data.used_memory : 0.0,
    g_config.maxmemory,
    maxmemory_policy_name(g_config.maxmemory_policy),
    (unsigned long long)g_data.evicted_keys,

    //stats
    (unsigned long long)g_data.g_total_commands,

    // keyspace 
    keystats.total,
    keystats.with_ttl,
    keystats.total - keystats.with_ttl,

    // persistence (rdb)
    (unsigned long long)(g_data.g_last_save_ms / 1000),
    g_data.g_writes_since_save,
    (int)g_data.g_last_save_ok,
    g_data.g_last_save_size_bytes,
    // persistence (aof)
    (int)g_config.aof_enable,
    g_data.g_aof_current_size,
    g_data.g_aof_base_size,
    (int)(g_aof_child_pid != -1),
    g_data.g_aof_write_err ? "err" : "ok",
    g_data.g_aof_last_rewrite_ok ? "ok" : "err"
  );
  if (len < 0){ return resp_err(out, "ERR info formatting"); }
  // clamp, never over read
  if (len >= (int)sizeof(buf)){ len = sizeof(buf) - 1; }
  resp_str(out, buf, (size_t)len);         
}

// LPUSH key
void do_lpush(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  if (lookup_entry(cmd[1], T_DLIST, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  // push all values
  for (size_t i = 2; i < cmd.size(); ++i){
    deque_push_front(&entry_deque(ent), cmd[i]);
  }
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)entry_deque(ent).count); 
}

// RPUSH key
void do_rpush(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  if (lookup_entry(cmd[1], T_DLIST, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  for (size_t i = 2; i < cmd.size(); ++i){
    deque_push_back(&entry_deque(ent), cmd[i]);
  }
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)entry_deque(ent).count);
}

// LPOP key
void do_lpop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }

  std::string val;
  if (!deque_pop_front(&entry_deque(ent), &val)){ return resp_nil(out); }

  // if list is now empty, delete the key
  if (entry_deque(ent).count == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else {
    mem_reaccount(ent);
  }
  g_data.g_writes_since_save++;
  resp_str(out, val.data(), val.size());
}

// RPOP key
void do_rpop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }

  std::string val;
  if (!deque_pop_back(&entry_deque(ent), &val)){ return resp_nil(out); }
  // if list is now empty, delete the key
  if (entry_deque(ent).count == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else {
    mem_reaccount(ent);
  }
  g_data.g_writes_since_save++;
  resp_str(out, val.data(), val.size());
}

// LLEN key
void do_llen(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_int(out, 0);
    case Lookup::OK:        break;
  }
  resp_int(out, (int64_t)entry_deque(ent).count);
}

// LINDEX key index
void do_lindex(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_nil(out);
    case Lookup::OK:        break;
  }
  int64_t idx = 0;
  if (!str2int(cmd[2], idx)){
    return resp_err(out, "ERR invalid index");
  }
  idx = deque_normalize(&entry_deque(ent), idx);

  if (idx < 0 || idx >= (int64_t)entry_deque(ent).count){
    return resp_nil(out);
  }

  const std::string *val = deque_get(&entry_deque(ent), (size_t)idx);
  resp_str(out, val->data(), val->size());
}

// LRANGE key start stop
void do_lrange(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_arr(out, 0);
    case Lookup::OK:        break;
  }

  int64_t start = 0, stop = 0;
  if (!str2int(cmd[2], start) || !str2int(cmd[3], stop)){
    return resp_err(out, "ERR invalid range");
  }

  int64_t n = (int64_t)entry_deque(ent).count;
  start = deque_normalize(&entry_deque(ent), start);
  stop = deque_normalize(&entry_deque(ent), stop);

  // clamp to valid bounds
  if (start < 0) { start = 0; }
  if (stop >= n) { stop = n - 1; }

  if (start > stop || start >= n){
    return resp_arr(out, 0); // empty range
  }

  uint32_t range_len = (uint32_t)(stop - start + 1);
  resp_arr(out, range_len);
  for (int64_t i = start; i <= stop; ++i){
    const std::string *val = deque_get(&entry_deque(ent), (size_t)i);
    resp_str(out, val->data(), val->size());
  }
}

// LSET key index value -- replace at index
void do_lset(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_err(out, "ERR no such key");
    case Lookup::OK:        break;
  }

  int64_t idx = 0;
  if (!str2int(cmd[2], idx)){
    return resp_err(out, "ERR invalid index");
  }
  idx = deque_normalize(&entry_deque(ent), idx);

  if (idx < 0 || idx >= (int64_t)entry_deque(ent).count){
    return resp_err(out, MSG_OUT_OF_RANGE);
  }

  // direct write
  Deque &d = entry_deque(ent);
  std::string &slot = d.buf[deque_phys(&d, (size_t)idx)];
  d.elem_bytes -= slot.capacity();
  slot = cmd[3];
  d.elem_bytes += slot.capacity();
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_ok(out);
}

// Shifting helpers
// helper that makes room for one new element at logical index idx
static void deque_open_gap(Deque *d, size_t idx){
  if (idx < d->count / 2){
    // closer to the front - shift [ 0, idx] left by one, move head back
    size_t new_head = (d->head + d->cap - 1) & (d->cap - 1);
    for (size_t i = 0; i < idx; ++i){
      size_t from = (new_head + 1 + i) & (d->cap - 1);
      size_t  to = (new_head + i) & (d->cap - 1);
      d->buf[to] = std::move(d->buf[from]);
    }
    d->head = new_head;
  } else {
    // closer to back - we shift [idx, count]to the right by one
    for (size_t i = d->count; i > idx; --i ){
      size_t from = deque_phys(d, i - 1);
      size_t to = deque_phys(d, i);
      d->buf[to] = std::move(d->buf[from]);
    }
  }
  d->count++;
}

// helper that removes an item of the middle
static void deque_close_gap(Deque *d, size_t idx){
  if (idx < d->count / 2){
    // closer to the front
    for (size_t i = idx; i > 0; --i){
      size_t from = deque_phys(d, i - 1);
      size_t to = deque_phys(d, i);
      d->buf[to] = std::move(d->buf[from]);
    }
    d->buf[d->head].clear();
    d->head = (d->head + 1) & (d->cap - 1);
  } else {
    // closer to the back
    for (size_t i = idx; i + 1 < d->count; ++i){
      size_t from = deque_phys(d, i + 1);
      size_t to = deque_phys(d, i);
      d->buf[to] = std::move(d->buf[from]);
    }
    size_t last = deque_phys(d, d->count - 1);
    d->buf[last].clear();
  }
  d->count--;
}

// LINSERT key before|after pivot value
void do_linsert(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_int(out, 0);
    case Lookup::OK:        break;
  }
  for (char &ch : cmd[2]) { ch = (char)tolower((unsigned char)ch); }

  bool before = false;
  if (cmd[2] == "before") { before = true; }
  else if (cmd[2] == "after") { before = false; }
  else { return resp_err(out, MSG_SYNTAX); }

  if (!ent){ return resp_int(out, 0); } // key does not exist

  const std::string &pivot = cmd[3];
  const std::string &value = cmd[4];

  // find the pivot - linear
  size_t pivot_idx = SIZE_MAX;
  for ( size_t i = 0; i < entry_deque(ent).count; ++i){
    if (*deque_get(&entry_deque(ent), i) == pivot){
      pivot_idx = i;
      break;
    }
  }
  if (pivot_idx == SIZE_MAX) { return resp_int(out, -1); } // pivot not found

  size_t insert_idx = before ? pivot_idx : pivot_idx + 1;

  // we ensure capacity before opening the gap
  if (entry_deque(ent).count == entry_deque(ent).cap){ deque_grow(&entry_deque(ent)); }

  deque_open_gap(&entry_deque(ent), insert_idx);
  std::string &slot = entry_deque(ent).buf[deque_phys(&entry_deque(ent), insert_idx)];
  slot = value;
  entry_deque(ent).elem_bytes += slot.capacity();

  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)entry_deque(ent).count); // new length
}

// LREM key count value
void do_lrem(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_int(out, 0);
    case Lookup::OK:        break;
  }

  int64_t count = 0;
  if (!str2int(cmd[2], count)){ return resp_err(out, "ERR invalid count"); }

  const std::string &value = cmd[3];
  int64_t removed = 0;
  int64_t limit = (count < 0) ? -count : count;
  bool from_tail = (count < 0);

  if (from_tail){
    // scan from the back
    size_t i = entry_deque(ent).count;
    while (i > 0){
      --i;
      if (*deque_get(&entry_deque(ent), i) == value){
        entry_deque(ent).elem_bytes -= deque_get(&entry_deque(ent), i)->capacity();
        deque_close_gap(&entry_deque(ent), i);
        removed++;
        if (limit > 0 && removed >= limit) { break; }
      }
    }
  } else {
    // scan from the front
    size_t i = 0;
    while (i < entry_deque(ent).count){
      if (*deque_get(&entry_deque(ent), i) == value){
        entry_deque(ent).elem_bytes -= deque_get(&entry_deque(ent), i)->capacity();
        deque_close_gap(&entry_deque(ent), i);
        removed++;
        if (limit > 0 && removed >= limit) { break; }
        // don't advance i - the gap closed
      } else {
        ++i;
      }
    }
  }

  // delete the key if the list became empty
  if (ent && entry_deque(ent).count == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else if (removed > 0){
    mem_reaccount(ent);
  }

  if (removed > 0){ g_data.g_writes_since_save++; }
  resp_int(out, removed);
}

// LTRIM key start stop
void do_ltrim(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_ok(out);
    case Lookup::OK:        break;
  }

  int64_t start = 0, stop = 0;
  if (!str2int(cmd[2], start) || !str2int(cmd[3], stop)){
    return resp_err(out, "ERR invalid range");
  }

  int64_t n = (int64_t)entry_deque(ent).count;
  start = deque_normalize(&entry_deque(ent), start);
  stop = deque_normalize(&entry_deque(ent), stop);

  if (start < 0) { start = 0; }
  if (stop >= n) { stop = n - 1; }

  // empty result - clear the whole list and delete the key
  if (start > stop || start >= n){
    if (ent){
      hm_delete(&g_data.db, &ent->node, &hnode_same);
      entry_del(ent);
      g_data.g_writes_since_save++;
    }
    return resp_ok(out);
  }

  size_t keep = (size_t)(stop - start + 1);

  // if we are keeping everything, no work needed
if (start == 0 && (size_t)(stop + 1) == entry_deque(ent).count){
    return resp_ok(out);
  }

  // clear dropped slots, then resposition head/count, no alloc
  for (int64_t i = 0; i < start; ++i){
    std::string &slot = entry_deque(ent).buf[deque_phys(&entry_deque(ent), (size_t)i)];
    entry_deque(ent).elem_bytes -= slot.capacity();
    std::string().swap(slot); // releases the buffer; clear() would keep it
  }
  for (int64_t i = stop + 1; i < n; ++i){
    std::string &slot = entry_deque(ent).buf[deque_phys(&entry_deque(ent), (size_t)i)];
    entry_deque(ent).elem_bytes -= slot.capacity();
    std::string().swap(slot); // releases the buffer; clear() would keep it
  }

  entry_deque(ent).head = deque_phys(&entry_deque(ent), (size_t)start); // compute before changing count
  entry_deque(ent).count = keep;

  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_ok(out);
}

// glob-style pattern match (Redis MATCH semantics), case-sensitive:
//   *        any sequence (including empty)
//   ?        exactly one char
//   [abc]    one of a set;  [a-z] range;  [^...] negation
//   \x       escape: match x literally
// p/plen = pattern, s/slen = string. recursive on '*'.
static bool glob_match(const char *p, size_t plen, const char *s, size_t slen) {
  const char *bp = nullptr; size_t bplen = 0;
  const char *bs = nullptr; size_t bslen = 0;

  while (true) {
      while (plen > 0 && p[0] == '*') {
          while (plen > 1 && p[1] == '*') { p++; plen--; }
          if (plen == 1) return true;
          bp = p + 1; bplen = plen - 1;
          bs = s;     bslen = slen;
          p++; plen--;
      }

      if (plen == 0 && slen == 0) return true;
      if (plen == 0 || slen == 0) {
          if (!bp) return false;
          bs++; bslen--;
          p = bp; plen = bplen;
          s = bs; slen = bslen;
          continue;
      }

      bool matched = false;
      size_t pat_step = 1;
      switch (p[0]) {
          case '?':
              matched = true;
              break;
          case '[': {
              const char *cp = p + 1; size_t crem = plen - 1;
              bool negate = (crem > 0 && cp[0] == '^');
              if (negate) { cp++; crem--; }
              bool hit = false;
              while (crem > 0 && cp[0] != ']') {
                  if (cp[0] == '\\' && crem >= 2) {
                      if (cp[1] == s[0]) hit = true;
                      cp += 2; crem -= 2;
                  } else if (crem >= 3 && cp[1] == '-' && cp[2] != ']') {
                      char lo = cp[0], hi = cp[2];
                      if (lo > hi) { char t = lo; lo = hi; hi = t; }
                      if (s[0] >= lo && s[0] <= hi) hit = true;
                      cp += 3; crem -= 3;
                  } else {
                      if (cp[0] == s[0]) hit = true;
                      cp++; crem--;
                  }
              }
              if (crem > 0 && cp[0] == ']') { cp++; crem--; }
              matched = negate ? !hit : hit;
              pat_step = plen - crem;
              break;
          }
          case '\\':
              if (plen < 2) { matched = false; break; }
              matched = (p[1] == s[0]);
              pat_step = 2;
              break;
          default:
              matched = (p[0] == s[0]);
              break;
      }

      if (matched) {
          p += pat_step; plen -= pat_step;
          s++; slen--;
      } else {
          if (!bp) return false;
          bs++; bslen--;
          p = bp; plen = bplen;
          s = bs; slen = bslen;
      }
  }
}

struct ScanCtx {
  std::vector<std::string> *out;
  const std::string *pattern; // nullptr if no match
};

static void cb_scan(HNode *node, void *arg){
  ScanCtx *ctx = (ScanCtx *)arg;
  Entry *ent = container_of(node, &Entry::node);

  // skip expired keys, read only check
  if (entry_has_ttl(ent) && g_data.heap[ent->heap_idx].val <= get_monotonic_msec()){ return; }

  // MATCH filter (if a pattern was given)
  if (ctx->pattern && !glob_match(ctx->pattern->data(), ctx->pattern->size(), ent->key.data(), ent->key.size())){
    return;
  }

  ctx->out->push_back(ent->key);
}

// SCAN cursor [MATCH PATTERN] [COUNT n]
static void do_scan(std::vector<std::string> &cmd, Buffer *out){
  int64_t cursor = 0;
  if (!str2int(cmd[1], cursor)) { return resp_err(out, "ERR invalid cursor"); }
  if (cursor < 0){ return resp_err(out, "ERR invalid cursor"); }

  size_t count = 10;
  const std::string *pattern = nullptr;
  std::string pat;
  // search in pair
  for (size_t i = 2; i + 1 < cmd.size(); i += 2){
    std::string opt = cmd[i];
    for (char &c: opt) { c = (char)tolower((unsigned char)c); }
    if (opt == "count"){
      int64_t n;
      if (!str2int(cmd[i+1], n) || n <= 0) { return resp_err(out, "ERR invalid count"); }
      count = (size_t)n;
    } else if (opt == "match"){
      pat = cmd[i+1];
      pattern = &pat;
    } else {
      return resp_err(out, MSG_SYNTAX);
    }
  }

  std::vector<std::string> keys;
  ScanCtx ctx { &keys, pattern };
  uint64_t next = hm_scan(&g_data.db, (uint64_t)cursor, count, cb_scan, &ctx);

  // reply a 2 element array 
  resp_arr(out, 2);
  std::string cur = std::to_string(next);
  resp_str(out, cur.data(), cur.size()); // element 1 cursor as string
  resp_arr(out, (uint32_t)keys.size()); // element 2 array of keys
  for (const auto &k : keys) { resp_str(out, k.data(), k.size()); }
}

// set a key in a hash
// HSET key field value 
static void do_hset(std::vector<std::string> &cmd, Buffer *out){
  if ((cmd.size() - 2) % 2 != 0){
    return resp_err(out, "ERR wrong number of arguments for 'hset'");
  }
  Entry *ent;
  if (lookup_entry(cmd[1], T_HASH, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  int64_t added = 0;
  for (size_t i = 2; i + 1 < cmd.size(); i += 2){
    if (hash_set(&entry_hash(ent), cmd[i], cmd[i + 1])){ added++; }
  }
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  resp_int(out, added); 
}

// HGET key field
static void do_hget(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_nil(out);
    case Lookup::OK:  break;
  }

  HashNode *hn = hash_get(&entry_hash(ent), cmd[2]);
  if (!hn){ return resp_nil(out); }
  resp_str(out, hn->value.data(), hn->value.size());
}

// HDEL key field 
static void do_hdel(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  int64_t removed = 0;
  for (size_t i = 2; i < cmd.size(); ++i){
    if (hash_del(&entry_hash(ent), cmd[i])){ removed++; }
  }
  // if empty hash we drop the key
  if (hm_size(&entry_hash(ent)) == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else if (removed){
    mem_reaccount(ent);
  }
  if (removed) { g_data.g_writes_since_save++; }
  resp_int(out, removed);
}

// HEXISTS key field
static void do_hexists(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  resp_int(out, hash_get(&entry_hash(ent), cmd[2]) ? 1 : 0);
}

// HLEN key field
static void do_hlen(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  resp_int(out, (int64_t)hm_size(&entry_hash(ent)));
}

// shared by HGETALL(0), HKEYS(1), HVALS(2)
struct HEmit { 
  Buffer *out;
  int mode;
};

static bool cb_hemit(HNode *node, void *arg){
  HEmit *c = (HEmit *)arg;
  HashNode *hn = container_of(node, &HashNode::node);
  if (c->mode == 0){ 
    resp_str(c->out, hn->field.data(), hn->field.size());
    resp_str(c->out, hn->value.data(), hn->value.size());
  } else if (c->mode == 1){
    resp_str(c->out, hn->field.data(), hn->field.size());
  } else { 
    resp_str(c->out, hn->value.data(), hn->value.size());
  }
  return true;
}

static void h_collect_reply(std::vector<std::string> &cmd, Buffer *out, int mode){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_arr(out, 0);
    case Lookup::OK:  break;
  }
  size_t n = hm_size(&entry_hash(ent));
  resp_arr(out, (uint32_t)(mode == 0 ? n * 2 : n));
  HEmit c { out, mode };
  hm_foreach(&entry_hash(ent), cb_hemit, &c);
}

static void do_hgetall(std::vector<std::string> &cmd, Buffer *out){ h_collect_reply(cmd, out, 0); }
static void do_hkeys(std::vector<std::string> &cmd, Buffer *out){ h_collect_reply(cmd, out, 1); }
static void do_hvals(std::vector<std::string> &cmd, Buffer *out){ h_collect_reply(cmd, out, 2); }

// HMGET key field [field..] -> array
static void do_hmget(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent = nullptr;
  Lookup r = lookup_entry(cmd[1], T_HASH,  false, &ent);
  if (r == Lookup::WRONGTYPE){ return resp_err(out, "WRONGTYPE wrong type"); }

  resp_arr(out, (uint32_t)(cmd.size() - 2));
  for (size_t i = 2; i < cmd.size(); ++i){
    HashNode *hn = (r == Lookup::OK) ? hash_get(&entry_hash(ent), cmd[i]) : nullptr;
    if (hn){ resp_str(out, hn->value.data(), hn->value.size()); }
    else { resp_nil(out); }
  }
}
// HSETNX key field value, 1 if set and 0 if already existed
static void do_hsetnx(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  if (lookup_entry(cmd[1], T_HASH, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  // already there
  if (hash_get(&entry_hash(ent), cmd[2])){ return resp_int(out, 0); } 

  hash_set(&entry_hash(ent), cmd[2], cmd[3]);
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// HINCRBY key field increment -> new integer value
static void do_hincrby(std::vector<std::string> &cmd, Buffer *out){
  int64_t incr = 0;
  if (!str2int(cmd[3], incr)){ 
    return resp_err(out, MSG_NOT_INT); 
  }

  Entry *ent;
  if (lookup_entry(cmd[1], T_HASH, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }

  int64_t cur = 0;
  HashNode *hn = hash_get(&entry_hash(ent), cmd[2]);
  if (hn && !str2int(hn->value, cur)){
    return resp_err(out, "ERR hash value is not an integer");
  }
  if ((incr > 0 && cur > INT64_MAX - incr) || (incr < 0 && cur < INT64_MIN - incr)){
    return resp_err(out, "ERR increment or decrement would overflow");
  }
  cur += incr;
  hash_set(&entry_hash(ent), cmd[2], std::to_string(cur));
  mem_reaccount(ent);
  g_data.g_writes_since_save++;
  return resp_int(out, cur);
}

// HSTRLEN key field -> lenght of the fiels value (0 if field/key is missing)
static void do_hstrlen(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_HASH, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  HashNode *hn = hash_get(&entry_hash(ent), cmd[2]);
  return resp_int(out, hn ? (int64_t)hn->value.size() : 0);
}

static void cb_hscan(HNode *node, void *arg){
  ScanCtx *c = (ScanCtx *)arg;
  HashNode *hn = container_of(node, &HashNode::node);
  if (c->pattern && !glob_match(c->pattern->data(), c->pattern->size(), hn->field.data(), hn->field.size())){
    return;
  }
  c->out->push_back(hn->field);
  c->out->push_back(hn->value);
}

// HSCAN key cursor [MATCH pat] [COUNT n] -> [cursor, [field, value, ...]]
static void do_hscan(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  Lookup r = lookup_entry(cmd[1], T_HASH, false, &ent);
  if (r == Lookup::WRONGTYPE){ return resp_err(out, "WRONGTYPE wrong type"); }

  int64_t cursor = 0;
  if (!str2int(cmd[2], cursor)){ return resp_err(out, "ERR invalid cursor"); }
  if (cursor < 0){ return resp_err(out, "ERR invalid cursor"); }

  size_t count = 10;
  const std::string *pattern = nullptr;
  std::string pat;
  for (size_t i = 3; i + 1 < cmd.size(); i += 2){
    std::string opt = cmd[i];
    for (char &c : opt){ c = (char)tolower((unsigned char)c); }
    if (opt == "count"){
      int64_t n;
      if (!str2int(cmd[i + 1], n) || n <= 0){ return resp_err(out, "ERR invalid count"); }
      count = (size_t)n;
    } else if (opt == "match"){
      pat = cmd[i + 1];
      pattern = &pat;
    } else {
      return resp_err(out, MSG_SYNTAX);
    }
  }

  resp_arr(out, 2);
  // missing key -> cursor 0, empty list
  if (r == Lookup::MISSING){
    resp_str(out, "0", 1);
    resp_arr(out, 0);
    return;
  }
  std::vector<std::string> items;
  ScanCtx ctx { &items, pattern };
  uint64_t next = hm_scan(&entry_hash(ent), (uint64_t)cursor, count ,cb_hscan, &ctx);

  std::string cur = std::to_string(next);
  resp_str(out, cur.data(), cur.size());
  resp_arr(out, (uint32_t)items.size());
  for (const auto &s : items){ resp_str(out, s.data(), s.size()); }
}

// DBSIZE -> number of keys
static void do_dbsize(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  return resp_int(out, (int64_t)hm_size(&g_data.db));
}

// FLUSHALL / FLUSHDB -> delete every key
static bool cb_collect_entry(HNode *node, void *arg){
  ((std::vector<Entry *> *)arg)->push_back(container_of(node, &Entry::node));
  return true;
}

static void do_flushall(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  std::vector<Entry *> ents;
  // collect (dont free mid iteration)
  hm_foreach(&g_data.db, cb_collect_entry, &ents);
  // free the table arrays
  hm_clear(&g_data.db);
  // free each entry (per-type + TTL heap)
  for (Entry *e : ents){ entry_del(e); }
  g_data.g_writes_since_save++;
  return resp_ok(out);
}

// RANDOMKEY -> a random key 
struct RandKeyCtx{ 
  std::string key;
  size_t seen;
};

static bool cb_randomkey(HNode *node, void *arg){
  RandKeyCtx *ctx = (RandKeyCtx *)arg; 
  ctx->seen++;
  if ((size_t)(rand_idx(ctx->seen)) == 0){
    ctx->key = container_of(node, &Entry::node)->key;
  }
  return true;
}

static void do_randomkey(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  if (hm_size(&g_data.db) == 0){ return resp_nil(out); }
  RandKeyCtx ctx { std::string(), 0};
  hm_foreach(&g_data.db, cb_randomkey, &ctx);
  return resp_str(out, ctx.key.data(), ctx.key.size());
}

// sharead rename: nx=true -> fail(0) if dest exists. returns 1 ok, 0 dest-exits, -1 no source
static int rename_key(const std::string &src, const std::string &dst, bool nx){
  LookupKey sk; sk.key = src;
  sk.node.hcode = str_hash((const uint8_t *)src.data(), src.size());
  HNode *snode = hm_lookup(&g_data.db, &sk.node, &entry_eq);
  if (!snode){ return -1; }
  Entry *sent = container_of(snode, &Entry::node);
  if (expire_if_needed(sent)){ return -1; } // source expired
  if (src == dst){ return 1; } // rename to self

  LookupKey dk; dk.key = dst;
  dk.node.hcode = str_hash((const u_int8_t *)dst.data(), dst.size());
  HNode *dnode = hm_lookup(&g_data.db, &dk.node, &entry_eq);
  if (dnode){
    Entry *dent = container_of(dnode, &Entry::node);
    // if dest exist & alive
    if (!expire_if_needed(dent)){
      // nx refuses
      if (nx){ return 0;}
      hm_delete(&g_data.db, &dent->node, &hnode_same);
      // we are gonna overwrite so we dtop the old dest
      entry_del(dent);
    }
  }
  // we just move the source
  hm_delete(&g_data.db, &sent->node, &hnode_same);
  sent->key = dst;
  sent->node.hcode = dk.node.hcode;
  hm_insert(&g_data.db, &sent->node);
  mem_reaccount(sent);
  return 1;
}

static void do_rename(std::vector<std::string> &cmd, Buffer *out){
  int rc = rename_key(cmd[1], cmd[2], false);
  if (rc == -1){ return resp_err(out, "ERR no such key"); }
  g_data.g_writes_since_save++;
  return resp_ok(out);
}

static void do_renamenx(std::vector<std::string> &cmd, Buffer *out){
  int rc = rename_key(cmd[1], cmd[2], true);
  if (rc == -1){ return resp_err(out, "ERR no such key"); }
  if (rc == 1){ g_data.g_writes_since_save++; }
  return resp_int(out, rc);
}

// TOUCH key [key...] -> count of keys that exists
static void do_touch(std::vector<std::string> &cmd, Buffer *out){
  int64_t n = 0;
  for (size_t i = 1; i < cmd.size(); ++i){
    LookupKey key; key.key = cmd[i];
    key.node.hcode = str_hash((const uint8_t *)cmd[i].data(), cmd[i].size());
    HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
    if (node){
      Entry *ent = container_of(node, &Entry::node);
      if (!expire_if_needed(ent)){ ++n;}
    }
  }
  return resp_int(out, n);
}

// EXPIREAT (mult=10000, seconds) / PEXPIREAT (multi=1, ms): absoulute wall clock expiry
static void expireat_generic(std::vector<std::string> &cmd, Buffer *out, int64_t mult){
  int64_t when = 0;
  if (!str2int(cmd[2], when)){ return resp_err(out, "ERR invalid expire time"); }
  int64_t abs_ms = when * mult;

  LookupKey key; key.key.swap(cmd[1]);
  key.node.hcode = str_hash((const uint8_t *)key.key.data(), key.key.size());
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){ return resp_int(out, 0); }

  Entry *ent = container_of(node, &Entry::node);
  if (expire_if_needed(ent)){ return resp_int(out, 0); }

  // absolute -> remamining 
  int64_t remaining = abs_ms - (int64_t)get_wall_msec();
  // already past -> delete
  if (remaining <= 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
    g_data.g_writes_since_save++;
    return resp_int(out, 1);
  }
  entry_set_ttl(ent, remaining);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);

}

// Both expire at (ms and s)
static void do_expireat(std::vector<std::string> &cmd, Buffer *out){ expireat_generic(cmd, out, 1000); }
static void do_pexpireat(std::vector<std::string> &cmd, Buffer *out){ expireat_generic(cmd, out, 1); }


// Set commands

// Collects all member strings from a set's HMap (used by multi-key ops and bulk commands)
static bool cb_collect_members(HNode *node, void *arg){
  auto *v = (std::vector<std::string> *)arg;
  v->push_back(container_of(node, &SetNode::node)->member);
  return true;
}

static bool cb_members_emit(HNode *node, void *arg){
  Buffer *out = (Buffer *)arg;
  resp_str(out, container_of(node, &SetNode::node)->member.data(), container_of(node, &SetNode::node)->member.size());
  return true;
}

// Non-destructive — takes const ref, makes an internal copy of the key for hashing.
// Use for all read-only lookups (GET, HGET, EXPIRE, etc.)
static Entry *lookup_set_ro(const std::string &key, bool *wrongtype){
  LookupKey lk; lk.key = key;
  lk.node.hcode = str_hash((const uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);

  if (!node){ return nullptr; }
  Entry *ent= container_of(node, &Entry::node);
  // key is missing ?? 
  if (expire_if_needed(ent)){ return nullptr; }
  if (ent->type != T_SET){ *wrongtype = true; return nullptr; }
  return ent;
}

// Delete a key (any type) and create a fresh empty T_SET
static Entry *set_make_dest(const std::string &key){
  LookupKey lk; lk.key = key;
  lk.node.hcode = str_hash((const uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
  if (node){
    Entry *old = container_of(node, &Entry::node);
    hm_delete(&g_data.db, &old->node, &hnode_same);
    entry_del(old);
  }
  Entry *ent = entry_new(T_SET);
  ent->key = lk.key;
  ent->node.hcode = lk.node.hcode;
  hm_insert(&g_data.db, &ent->node);
  return ent;
}

// Result is filled on success (may be empty)
// return false if wrongtype
static bool sinter_impl(std::vector<std::string> &cmd, size_t start, std::vector<std::string> &result){
  bool wt = false;
  std::vector<Entry *> sets;
  for (size_t i = start; i < cmd.size(); ++i){
    Entry *e = lookup_set_ro(cmd[i], &wt);
    if (wt){ return false; }
    if (!e){ result.clear(); return true; } // intersection with empty = empty
    sets.push_back(e);
  }
  size_t smallest_idx = 0;
  for (size_t i = 1; i < sets.size(); ++i){
    if (hm_size(&entry_set(sets[i])) < hm_size(&entry_set(sets[smallest_idx]))){
      smallest_idx = i;
    }
  }

  std::vector<std::string> candidates;
  hm_foreach(&entry_set(sets[smallest_idx]), cb_collect_members, &candidates);
  for (auto &m : candidates){
    bool in_all = true;
    for (size_t i = 0; i < sets.size(); ++i){
      if (i == smallest_idx){ continue; }
      if (!set_is_member(&entry_set(sets[i]), m)){ in_all = false; break; }
    }
    if (in_all){ result.push_back(m); }
  }
  return true;
}

static bool sunion_impl(std::vector<std::string> &cmd, size_t start, std::vector<std::string> &result){
  bool wt = false;
  for (size_t i = start; i < cmd.size(); ++i){
    Entry *e = lookup_set_ro(cmd[i], &wt);
    if (wt){ return false; }
    if (!e){ continue; } // missing key contributes nothing
    hm_foreach(&entry_set(e), cb_collect_members, &result);
  }
  // deduplicate in O(N log N);
  std::sort(result.begin(), result.end());
  result.erase(std::unique(result.begin(), result.end()), result.end());
  return true;
}

static bool sdiff_impl(std::vector<std::string> &cmd, size_t start, std::vector<std::string> &result){
  bool wt = false;
  Entry *base = lookup_set_ro(cmd[start], &wt);
  if (wt){ return false;}
  if (!base){ return true; }// empty base, empty diff

  std::vector<Entry *> others;
  for (size_t i = start + 1; i < cmd.size(); ++i){
    Entry *e = lookup_set_ro(cmd[i], &wt);
    if (wt){ return false; }
    if (e){ others.push_back(e); }
  }

  std::vector<std::string> candidates;
  hm_foreach(&entry_set(base), cb_collect_members, &candidates);
  for (auto &m : candidates){
    bool found = false;
    for (Entry *e : others){
      if (set_is_member(&entry_set(e), m)){ found = true; break; }
    }
    if (!found){ result.push_back(m); }
  }
  return true;
}

// SADD key member [member...]
static void do_sadd(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent; 
  if (lookup_entry(cmd[1], T_SET, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }
  int64_t added = 0;
  for (size_t i = 2; i < cmd.size(); ++i){
    if (set_add(&entry_set(ent), std::move(cmd[i]))){ ++added;}
  }
  if (added > 0){
    mem_reaccount(ent);
    g_data.g_writes_since_save++;  
  }
  return resp_int(out, added);
}

// SREM key member [member...]
static void do_srem(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  int64_t removed = 0;
  for (size_t i = 2; i < cmd.size(); ++i){
    if (set_remove(&entry_set(ent), cmd[i])){ ++removed; }
  }
  if (hm_size(&entry_set(ent)) == 0){
    hm_delete
    (&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else if (removed){
    mem_reaccount(ent);
  }
  if (removed){ g_data.g_writes_since_save++; }
  return resp_int(out, removed);
}

// SISMEMBER key member
static void do_sismember(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  return resp_int(out, set_is_member(&entry_set(ent), cmd[2]) ? 1 : 0);
}

// SMISMEMBER key member [member...] -> array of 0/1
static void do_smismember(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent = nullptr;
  Lookup r = lookup_entry(cmd[1], T_SET, false, &ent);
  if (r == Lookup::WRONGTYPE){ return resp_err(out, "WRONGTYPE wrong type"); }
  resp_arr(out, (uint32_t)(cmd.size() - 2));
  for (size_t i = 2; i < cmd.size(); ++i){
    bool found = (r == Lookup::OK) && set_is_member(&entry_set(ent), cmd[i]);
    resp_int(out, found ? 1 : 0);
  }
}

// SCARD key
static void do_scard(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  return resp_int(out, (int64_t)hm_size(&entry_set(ent)));
}

// SMEMBERS key
static void do_smembers(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_arr(out, 0);
    case Lookup::OK:  break;
  }
  resp_arr(out, (uint32_t)hm_size(&entry_set(ent)));
  hm_foreach(&entry_set(ent), cb_members_emit, out);
}

// forward declaration just for this function
static void aof_feed(const std::vector<std::string> &cmd);

// SPOP key [count]
static void do_spop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return (cmd.size() >= 3) ? resp_arr(out, 0): resp_nil(out);
    case Lookup::OK:  break;
  }
  HMap *set = &entry_set(ent);
  if (hm_size(set) == 0){   // defensive; empty sets are dropped by mutators
    return (cmd.size() >= 3) ? resp_arr(out, 0) : resp_nil(out);
  }

  bool feed = g_config.aof_enable && !g_data.g_loading;
  std::vector<std::string> synth;
  if (feed){ synth = { "srem", cmd[1] }; }

  if (cmd.size() == 2){
    // single pop
    SetNode *sn = container_of(hm_random(set), &SetNode::node);
    resp_str(out, sn->member.data(), sn->member.size());
    set->elem_bytes -= set_node_bytes(sn);
    hm_delete(set, &sn->node, &hnode_same);
    if (feed){ synth.push_back(std::move(sn->member)); }
    delete sn;
  } else {
    int64_t count = 0;
    if (!str2int(cmd[2], count)){ return resp_err(out, MSG_NOT_INT); }
    if (count < 0){ return resp_err(out, "ERR value is out of range, must be positive"); }
    if (count == 0){ return resp_arr(out, 0); }

    size_t n = ((size_t)count < hm_size(set)) ? (size_t)count : hm_size(set);
    resp_arr(out, (uint32_t)n);
    for (size_t i = 0; i < n; ++i){
      SetNode *sn = container_of(hm_random(set), &SetNode::node);
      resp_str(out, sn->member.data(), sn->member.size());
      set->elem_bytes -= set_node_bytes(sn);
      hm_delete(set, &sn->node, &hnode_same);
      if (feed){ synth.push_back(std::move(sn->member)); }
      delete sn;
    }
  }
  if (feed){ aof_feed(synth); }
  if (hm_size(&entry_set(ent)) == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
  } else {
    mem_reaccount(ent);
  }
  g_data.g_writes_since_save++;
}

// SRANDMEMBER key [count]
static void do_srandmember(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return (cmd.size() >= 3) ? resp_arr(out, 0): resp_nil(out);
    case Lookup::OK:  break;
  }
  HMap *set = &entry_set(ent);
  size_t size = hm_size(set);
  if (size == 0){   // defensive; empty sets are dropped by mutators
    return (cmd.size() >= 3) ? resp_arr(out, 0) : resp_nil(out);
  }

  if (cmd.size() == 2){
    SetNode *sn = container_of(hm_random(set), &SetNode::node);
    return resp_str(out, sn->member.data(), sn->member.size());
  }

  int64_t count = 0;
  if (!str2int(cmd[2], count)){ return resp_err(out, MSG_NOT_INT); }
  if (count == 0){ return resp_arr(out, 0); }

  if (count > 0){
    // distinct members up to min(count, size): pop k random nodes, emit, reinsert
    size_t n = ((size_t)count < size) ? (size_t)count : size;
    std::vector<SetNode *> picked;
    picked.reserve(n);
    for (size_t i = 0; i < n; ++i){
      SetNode *sn = container_of(hm_random(set), &SetNode::node);
      hm_delete(set, &sn->node, &hnode_same);
      picked.push_back(sn);
    }
    resp_arr(out, (uint32_t)n);
    for (SetNode *sn : picked){
      resp_str(out, sn->member.data(), sn->member.size());
      hm_insert(set, &sn->node);
    }
  } else {
    // negative count: |count| members with replacement
    if ((count == INT64_MIN) || -count > (int64_t)k_max_args){
      return resp_err(out, "ERR count is out of range");
    }
    size_t n = (size_t)(-count);
    resp_arr(out, (uint32_t)n);
    for (size_t i = 0; i < n; ++i){
      SetNode *sn = container_of(hm_random(set), &SetNode::node);
      resp_str(out, sn->member.data(), sn->member.size());
    }
  }
}

static void cb_sscan(HNode *node, void *arg){
  ScanCtx *c = (ScanCtx *)arg;
  SetNode *sn = container_of(node, &SetNode::node);
  if (c->pattern && !glob_match(c->pattern->data(),c->pattern->size(), sn->member.data(),sn->member.size())){
    return;
  }
  c->out->push_back(sn->member);
}

static void do_sscan(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  Lookup r = lookup_entry(cmd[1], T_SET, false, &ent);
  if (r == Lookup::WRONGTYPE){ return resp_err(out, "WRONGTYPE wrong type"); }

  int64_t cursor = 0;
  if (!str2int(cmd[2], cursor)){ return resp_err(out, "ERR invalid cursor"); }
  if (cursor < 0){ return resp_err(out, "ERR invalid cursor"); }

  size_t count = 10;
  const std::string *pattern = nullptr;
  std::string pat;
  for (size_t i = 3; i + 1 < cmd.size(); i += 2){
    std::string opt = cmd[i];
    for (char &c : opt){ c = (char)tolower((unsigned char)c); }
    if (opt == "count"){
      int64_t n;
      if (!str2int(cmd[i + 1], n) || n <= 0){ return resp_err(out, "ERR invalid count"); }
      count = (size_t)n;
    } else if (opt == "match"){
      pat = cmd[i + 1];
      pattern = &pat;
    } else {
      return resp_err(out, MSG_SYNTAX);
    }
  }
  resp_arr(out, 2);
  if (r == Lookup::MISSING){
    resp_str(out, "0", 1);
    resp_arr(out, 0);
    return;
  }
  std::vector<std::string> items;
  ScanCtx ctx { &items, pattern };
  uint64_t next = hm_scan(&entry_set(ent), (uint64_t)cursor, count, cb_sscan, &ctx);
  std::string cur = std::to_string(next);
  resp_str(out, cur.data(), cur.size());
  resp_arr(out, (uint32_t)items.size());
  for (auto &s : items){ resp_str(out, s.data(), s.size()); }
}

// SINTER key [key...]
static void do_sinter(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sinter_impl(cmd, 1, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  resp_arr(out, (uint32_t)result.size());
  for (auto &m : result){ resp_str(out, m.data(), m.size()); }
}

// SUNION key [key...]
static void do_sunion(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sunion_impl(cmd, 1, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  resp_arr(out, (uint32_t)result.size());
  for (auto &m : result){ resp_str(out, m.data(), m.size()); }
}

// SDIFF key [key...]
static void do_sdiff(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sdiff_impl(cmd, 1, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  resp_arr(out, (uint32_t)result.size());
  for (auto &m : result){ resp_str(out, m.data(), m.size()); }
}

// helper for store handlers
static void set_store_result(const std::string &dkey, std::vector<std::string> &result, Buffer *out){
  if (result.empty()){
    LookupKey lk; lk.key = dkey;
    lk.node.hcode = str_hash((const uint8_t *)lk.key.data(), lk.key.size());
    if (HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq)){
      Entry *old = container_of(node, &Entry::node);
      hm_delete(&g_data.db, &old->node, &hnode_same);
      entry_del(old);
      g_data.g_writes_since_save++;
    }
    return resp_int(out, 0);
  }
  Entry *dest = set_make_dest(dkey);
  for (auto &m : result){ set_add(&entry_set(dest), std::move(m)); }
  mem_reaccount(dest);
  g_data.g_writes_since_save++;
  resp_int(out, (int64_t)hm_size(&entry_set(dest)));
}

// SINTERSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sinterstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sinter_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  set_store_result(cmd[1], result, out);
}

// SUNIONSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sunionstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sunion_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  set_store_result(cmd[1], result, out);
}

// SDIFFSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sdiffstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sdiff_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  set_store_result(cmd[1], result, out);
}

// SMOVE src dst member
static void do_smove(std::vector<std::string> &cmd, Buffer *out){
  std::string src_name = cmd[1]; // save before swapping
  Entry *src_ent;
  switch (lookup_entry(cmd[1], T_SET, false, &src_ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return resp_int(out, 0);
    case Lookup::OK:  break;
  }
  if (!set_is_member(&entry_set(src_ent), cmd[3])){ return resp_int(out, 0); }

  // type check dst before modifying src
  bool wt = false;
  Entry *dst_ent = lookup_set_ro(cmd[2], &wt);
  if (wt){ return resp_err(out, "WRONGTYPE wrong type"); }

  if (src_name == cmd[2]){ return resp_int(out, 1); } // move to self

  set_remove(&entry_set(src_ent), cmd[3]);
  if (hm_size(&entry_set(src_ent)) == 0){
    // if move and 0 the set is empty
    hm_delete(&g_data.db, &src_ent->node, &hnode_same);
    entry_del(src_ent);
  } else {
    mem_reaccount(src_ent);
  }
  if (!dst_ent){
    dst_ent = entry_new(T_SET);
    dst_ent->key = cmd[2];
    dst_ent->node.hcode = str_hash((const uint8_t *)dst_ent->key.data(), dst_ent->key.size());
    hm_insert(&g_data.db, &dst_ent->node);
  }
  set_add(&entry_set(dst_ent), std::move(cmd[3]));
  mem_reaccount(dst_ent);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

static void do_ping(std::vector<std::string> &cmd, Buffer *out){
  if (cmd.size() >= 2){
    // PING msg -> bulk echo
    return resp_str(out, cmd[1].data(), cmd[1].size()); 
  }
  return resp_simple(out, "PONG");
}

static void do_echo(std::vector<std::string> &cmd, Buffer *out){
  return resp_str(out, cmd[1].data(), cmd[1].size());
}

static void do_config(std::vector<std::string> &cmd, Buffer *out){
  std::string sub = cmd[1];
  for (char &c : sub){ c = (char)tolower((unsigned char)c); }

  if (sub == "rewrite"){
    if (g_config.config_path.empty()){
      return resp_err(out, "ERR the server is running without a config file");
    }
    return config_rewrite(g_config.config_path.c_str()) ? resp_ok(out)
                                                        : resp_err(out, "ERR rewriting config failed");
  }

  if (sub == "get"){
    if (cmd.size() < 3){ return resp_err(out, "ERR wrong number of arguments for 'config|get'"); }
    std::string param = cmd[2];
    for (char &c : param){ c = (char)tolower((unsigned char)c); }

    // collect (name, value) for the params we actually support; '*' = all
    std::vector<std::pair<std::string , std::string>> kv;
    if (param == "maxmemory" || param == "*"){
      kv.emplace_back("maxmemory", std::to_string(g_config.maxmemory));
    }
    if (param == "maxmemory-policy" || param == "*"){
      kv.emplace_back("maxmemory-policy", maxmemory_policy_name(g_config.maxmemory_policy));
    }
    // empty array for unknown params
    resp_arr(out, (uint32_t)(kv.size() * 2));
    for (auto &p : kv){
      resp_str(out, p.first.data(), p.first.size());
      resp_str(out, p.second.data(), p.second.size());
    }
    return;
  }

    if (sub == "set"){
    if (cmd.size() < 4){ return resp_err(out, "ERR wrong number of arguments of 'config|set'"); }
    std::string err;
    std::string p = cmd[2]; for (char &c : p){ c = (char)tolower((unsigned char)c); }
    if (p == "rename-command"){
      return resp_err(out, "ERR 'rename-command' can only be set in the config file");
    }
    if (p.rfind("tls-", 0) == 0){
      return resp_err(out, "ERR TLS parameters can only be set in the config file");
    }
    CfgResult res = config_apply(cmd[2], { cmd[3] }, err);
    if (res == CfgResult::BADVALUE){ return resp_err(out, ("ERR " + err).c_str()); }
    if (res == CfgResult::UNKNOWN){ return resp_err(out, ("ERR Unknown config parameter '" + cmd[2] + "'").c_str()); }
    return resp_ok(out);
  }

  if (sub == "resetstat"){
    return resp_ok(out);
  }
  return resp_err(out, "ERR Unknown CONFIG subcommand");
}

// Non creating, type-agnostic lookup with lazy expiry. Does not stamp LRU access
// (object idletime must report idle time, not reset it)
// same as lookup_entry but wwithout the stamp, just a helper 
static Entry *lookup_any(const std::string &key){
  LookupKey lk; lk.key = key;
  lk.node.hcode = str_hash((const uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);
  if (!node){ return nullptr; }
  Entry *ent = container_of(node, &Entry::node);
  if (expire_if_needed(ent)){ return nullptr; }
  return ent;
}

// MYRED encodings (we dont gave redis listpack/intset/quicklist duality)
static const char *object_encoding(Entry *ent){
  switch (ent->type){
    case T_STR: { int64_t tmp; return str2int(entry_str(ent), tmp) ? "int" : "raw"; }
    case T_DLIST: return "deque";
    case T_HASH: return "hashtable";
    case T_SET: return "hashtable";
    case T_ZSET: return "skiplist";
  }
  return "unknown";
}
// Full sweep sum for memory doctor (callback)
static bool cb_mem_sweep(HNode *node, void *arg){
  *(size_t *)arg += entry_mem_usage(container_of(node, &Entry::node));
  return true;
}

static void do_memory(std::vector<std::string> &cmd, Buffer *out){
  std::string sub = cmd[1];
  for (char &c : sub){ c = (char)tolower((unsigned char)c); }

  if (sub == "usage"){
    if (cmd.size() >= 4){
      std::string opt = cmd[3];
      for (char &c : opt){ c = (char)tolower((unsigned char)c); } 
      if (opt != "samples" || cmd.size() < 5){ return resp_err(out, MSG_SYNTAX); }
      int64_t n = 0;
      if (!str2int(cmd[4], n) || n < 0){ return resp_err(out, MSG_SYNTAX); }
    }
    Entry *ent = lookup_any(cmd[2]);
    if (!ent){ return resp_nil(out); }
    return resp_int(out, (int64_t)entry_mem_usage(ent));
  }
  if (sub == "doctor"){
    size_t sweep = 0;
    hm_foreach(&g_data.db, &cb_mem_sweep, &sweep);
    char buf [192];
    int n;
    if (sweep == g_data.used_memory){
      n = snprintf(buf, sizeof(buf),
      "Can't find any memory problem. used_memory=%zu matches a full sweep.",
      g_data.used_memory);
    } else {
      n = snprintf(buf, sizeof(buf),
      "Accounting drift detected: counter=%zu sweep=%zu (delta=%zd) - a write handler "  
      "is likely a missing a mem_reaccount.", g_data.used_memory, sweep,
      (ssize_t)sweep - ((ssize_t)g_data.used_memory));
    }
    if (n < 0){ n = 0; }
    return resp_str(out, buf, (size_t)n);
  }

  if (sub == "stats"){
    resp_arr(out, 10);
    resp_str(out, "used_memory", 11); resp_int(out, (int64_t)g_data.used_memory);
    resp_str(out, "keys.count", 10); resp_int(out, (int64_t)hm_size(&g_data.db));
    resp_str(out, "maxmemory", 9); resp_int(out, (int64_t)g_config.maxmemory);
    resp_str(out, "maxmemory.policy", 16);
    { const char *p = maxmemory_policy_name(g_config.maxmemory_policy); resp_str(out, p, strlen(p)); }
    resp_str(out, "evicted.keys", 12); resp_int(out, (int64_t)g_data.evicted_keys);
    return;
  }
  return resp_err(out, "ERR unknown MEMORY subcommand");
}

static void do_object(std::vector<std::string> &cmd, Buffer *out){
  if (cmd.size() < 3){ return resp_err(out, "ERR wrong nubmer of arguments"); }
  std::string sub = cmd[1];
  for (char &c : sub){ c = (char)tolower((unsigned char)c); }

  Entry *ent = lookup_any(cmd[2]);
  if (!ent){ return resp_err(out, "ERR no such key"); }

  if (sub == "encoding"){
    const char *enc = object_encoding(ent);
    return resp_str(out, enc, strlen(enc));
  }
  if (sub == "idletime"){
    if (policy_is_lfu()){
      return resp_err(out, "ERR an LFU maxmemory policy is selected, idle time not tracked");
    }
    uint32_t idle = (g_data.g_lru_clock - ent->lru) & LRU_CLOCK_MAX; // seconds
    return resp_int(out, (int64_t)idle);
  }
  if (sub == "freq"){
    if (!policy_is_lfu()){
      return resp_err(out, "ERR An LFU maxmemory policy is not selected, access frequency not tracked.");
    }
    return resp_int(out, (int64_t)(ent->lru & 0xFF));
  }

  if (sub == "refcount"){
    return resp_int(out, 1);
  }
  return resp_err(out, "ERR unknown object subcommand");

}

// Append cmd to the aof buffer, rewriting relative TTls to absolute PEXPIREAT
static void aof_feed(const std::vector<std::string> &cmd){
  std::string frame;
  const std::string &name = cmd[0];              // already lower-cased
  int64_t now = (int64_t)get_wall_msec();

  if (name == "expire" || name == "pexpire" || name == "expireat"){
    int64_t v = 0;
    if (str2int(cmd[2], v)){
      int64_t abs_ms = (name == "expire")  ? now + v * 1000
                     : (name == "pexpire") ? now + v
                     :                        v * 1000;      // expireat (abs seconds)
      char ts[32]; int n = snprintf(ts, sizeof(ts), "%lld", (long long)abs_ms);
      aof_encode(frame, { "PEXPIREAT", cmd[1], std::string_view(ts, (size_t)n) });
    } else { aof_encode(frame, cmd); }
  }
  else if (name == "setex" || name == "psetex"){
    int64_t v = 0;
    if (str2int(cmd[2], v)){
      int64_t abs_ms = (name == "setex") ? now + v * 1000 : now + v;
      aof_encode(frame, { "SET", cmd[1], cmd[3] });
      char ts[32]; int n = snprintf(ts, sizeof(ts), "%lld", (long long)abs_ms);
      aof_encode(frame, { "PEXPIREAT", cmd[1], std::string_view(ts, (size_t)n) });
    } else { aof_encode(frame, cmd); }
  }
  else if (name == "getex" && cmd.size() >= 3){
    std::string opt = cmd[2];
    for (char &c : opt){ c = (char)tolower((unsigned char)c); }
    if (opt == "persist"){
      aof_encode(frame, { "PERSIST", cmd[1] });
    } else if (cmd.size() >= 4){
      int64_t v = 0;
      if (str2int(cmd[3], v)){
        int64_t abs_ms = (opt == "ex")   ? now + v * 1000
                       : (opt == "px")   ? now + v
                       : (opt == "exat") ? v * 1000
                       :                    v;               // pxat (abs ms)
        if (abs_ms <= now){
          aof_encode(frame, { "DEL", cmd[1] });             // past → key was deleted
        } else {
          char ts[32]; int n = snprintf(ts, sizeof(ts), "%lld", (long long)abs_ms);
          aof_encode(frame, { "PEXPIREAT", cmd[1], std::string_view(ts, (size_t)n) });
        }
      } else { aof_encode(frame, cmd); }
    } else { aof_encode(frame, cmd); }
  }
  else { aof_encode(frame, cmd); }               // verbatim fallback

  g_data.g_aof_buf += frame;
  if (g_aof_child_pid != -1){                    // FIXED
    g_data.g_aof_rewrite_buf += frame;
  }
}

static void aof_append_raw(const char *raw, size_t len){
  g_data.g_aof_buf.append(raw, len);
  // Dual write during a rewrite
  if (g_aof_child_pid != -1){
    g_data.g_aof_rewrite_buf.append(raw, len);
  }
}

// Evict keys unitl we're back under maxmemory. Returns false if we can't get under
// (noeviction, or a volatile-* policy with nothing evictable) -> caller return -OOM.
// NEW, if the batcj runs out while victims remain, arms g_evict_pending and return true
static bool free_memory_if_needed(){
  // unlimited
  if (g_config.maxmemory == 0){ g_data.g_evict_pending = false; return true; }
  // never during replay
  if (g_data.g_loading){ return true; }
  // already under
  if (g_data.used_memory <= g_config.maxmemory){ g_data.g_evict_pending = false; return true; }
  if (g_config.maxmemory_policy == MaxmemoryPolicy::NOEVICTION){ 
    g_data.g_evict_pending = false;
    return false;
  }

  // bounded, we don't stall the loop
  int attempts = 100;
  while (g_data.used_memory > g_config.maxmemory && --attempts > 0){
    Entry *victim = evict_pick_victim();
    // volatile-* with no TTL keys -> OOM; background eviction can't help either
    if (!victim){ g_data.g_evict_pending = false; return false; }

    // CRITICAL: log an explicit DEL so AOF replay / replicas don't resurrect the key.
    // aof_feed already handles the rewrite dual-buffer; gate on aof_eanble + not loading.
    if (g_config.aof_enable && !g_data.g_loading){
      // copy key before we free the entry
      aof_feed({ "del", victim->key });
    }

    hm_delete(&g_data.db, &victim->node, &hnode_same);
    // discharges used_memory; async-frees big values
    entry_del(victim);
    g_data.evicted_keys++;
    // eviction is a change woth persisting
    g_data.g_writes_since_save++;
  }
  if (g_data.used_memory <= g_config.maxmemory){
    g_data.g_evict_pending = false;
    return true;
  }
  g_data.g_evict_pending = true; // batch exhausted, victims remain: continue on ticks
  return true;
}

// one bounded eviction batch per event-loop tick while over maxmemory
void evict_tick(){
  if (g_data.g_evict_pending){ (void)free_memory_if_needed(); }
}

enum class KeySpec : uint8_t {
  NONE,
  FIRST,
  ALL_FROM_1,
  STRIDE2_FROM_1
};

using CmdFn = void(*)(std::vector<std::string> &, Buffer *);
using KeyResolver = void(*)(const std::vector<std::string> &cmd, std::vector<std::string_view> &keys);

struct CmdSpec {
  CmdFn fn;
  int min_args;
  int max_args;
  bool is_write = false;
  bool aof_rewrite = false; // can't be logged verbatim- needs TTL translation
  bool aof_self = false; // non deterministic: handler feeds a deterministic form itself
  uint64_t acl_cats = 0; // filled by acl_init_categories() at boot
  KeySpec keys = KeySpec::FIRST;
  KeyResolver key_resolver = nullptr; // when set overrides keys for extraction
};


static bool acl_key_allowed(const User *u, const std::string_view &key){
  for (const std::string &pat : u->key_patterns){
    if (glob_match(pat.data(), pat.size(), key.data(), key.size())){ return true; }
  }
  return false;
}

// nullptr = allowed, otherwise a NOPERM message
static const char *acl_check(const User *u, const std::string &name, 
                             const CmdSpec &spec, const std::vector<std::string> &cmd){
  if (!u || !u->enable){ return "NOPERM this user is disable"; }

  // (a) command permission: explicit +cmd/-cmd override wins, else category grant
  auto ov = u->cmd_overrides.find(name);
  bool ok = (ov != u->cmd_overrides.end()) ? ov->second 
                                           : ((spec.acl_cats & u->allow_cats) != 0);
  if (!ok){ return "NOPERM this user has no permissions to run this command"; }

  // (b) key patterns (skipped for all_keys users and no key commands)
  if (!u->all_keys){
    const char *kdeny = "NOPERM no permissions to access one of the keys used as arguments";
    if (spec.key_resolver){
      std::vector<std::string_view> keys;
      spec.key_resolver(cmd, keys);
      for (std::string_view k : keys){ if (!acl_key_allowed(u, k)){ return kdeny; } }
    } else { 
      switch (spec.keys){
        case KeySpec::NONE: break;
        case KeySpec::FIRST: 
          if (cmd.size() > 1 && !acl_key_allowed(u, cmd[1])){ return kdeny; }
          break;
        case KeySpec::ALL_FROM_1:
          for (size_t i = 1; i < cmd.size(); ++i){
            if (!acl_key_allowed(u, cmd[i])){ return kdeny; }
          }
          break;
        case KeySpec::STRIDE2_FROM_1:
          for (size_t i = 1; i < cmd.size(); i += 2){
            if (!acl_key_allowed(u, cmd[i])){ return kdeny; }
          }
          break;
      }
    }
  }
  return nullptr; 
}

static uint64_t acl_cat_bit(const std::string &n){
  if (n == "read"){ return CAT_READ;}             if (n == "write"){ return CAT_WRITE; }
  if (n == "keyspace"){ return CAT_KEYSPACE; }    if (n == "admin"){ return CAT_ADMIN; } 
  if (n == "dangerous"){ return CAT_DANGEROUS; }  if (n == "fast"){ return CAT_FAST; }
  if (n == "slow"){ return CAT_SLOW; }            if(n == "connection"){ return CAT_CONNECTION; }
  if (n == "all"){ return CAT_ALL; }              return 0;
}

// apply one SETUSER modifier; false on parse error
bool acl_apply_rule(User &u, const std::string &t){

  if (t == "on"){ u.enable = true; return true; }
  if (t == "off"){ u.enable = false; return true; }
  // clears everything (name reset by caller)
  if (t == "reset"){ u = User(); u.name.clear(); return true; }
  if (t == "resetpass"){ u.pw_hashes.clear(); return true; }
  if (t == "nopass"){ u.pw_hashes.clear(); return true; }
  if (t == "resetkeys"){ u.key_patterns.clear(); u.all_keys = false; return true; }

  // pre-hashes SHA-256 digest, '#<64 hex>'
  if (t.size() == 65 && t[0] == '#'){
    for (size_t i = 1; i < t.size(); ++i){
      // we reject junk
      if (!isxdigit((unsigned char)t[i])){ return false; }
    }
    u.pw_hashes.push_back(t.substr(1));
    return true;
  }

  if (t == "allchannels" || t == "&*"){ u.all_channels = true; u.channel_patterns.clear(); return true; }
  if (t == "resetchannels"){ u.channel_patterns.clear(); u.all_channels = false; return true; }
  if (t.size() > 1 && t[0] == '&'){ u.channel_patterns.push_back(t.substr(1)); return true; }

  if (t == "allkeys" || t == "~*"){ u.all_keys = true; u.key_patterns.clear(); return true; }

  if (t == "allcommands" || t == "+@all"){ u.allow_cats = CAT_ALL; u.cmd_overrides.clear(); return true; }
  if (t == "nocommands" || t == "-@all"){ u.allow_cats = 0; u.cmd_overrides.clear(); return true; }

  // >plain : add password hashed with th ecurrent policy
  if (t.size() > 1 && t[0] == '>'){
    std::string h = cred_hash_new(t.substr(1));
    if (h.empty()){ return false; }// entropy/KDF failure, reject failure
    u.pw_hashes.push_back(std::move(h));
    return true;
  }

  // <plain : remove any credential this plaintext matches. Salted PHC means hash
  // equality no longer works - verify per entry
  if (t.size() > 1 && t[0] == '<'){
      const std::string plain = t.substr(1);
      auto &v = u.pw_hashes;
      v.erase(std::remove_if(v.begin(), v.end(),
              [&] (const std::string &stored){ return cred_verify(plain, stored); }), v.end());
      return true;
  }

  // pre-hashed PHC credential (condig round-trip / ACL setuser  user $argon2id@...)
  if (t.rfind("$argon2id$", 0) == 0){
    if (t.size() > 512){ return false; } // sanity cap; garbage just never verifies
    u.pw_hashes.push_back(t);
    return true;
  }

  if (t.size() > 1 && t[0] == '~'){ u.key_patterns.push_back(t.substr(1)); return true; }

  if (t.rfind("+@", 0) == 0){ uint64_t b = acl_cat_bit(t.substr(2)); if (!b){ return false; } 
  u.allow_cats |= b; return true; }
  if (t.rfind("-@", 0) == 0){ uint64_t b = acl_cat_bit(t.substr(2)); if (!b){ return false; } 
  u.allow_cats &= ~b; return true; }

  if (t.size() > 1 && t[0] == '+'){ std::string c = t.substr(1); for (char & ch : c){ ch = (char)tolower((unsigned char)ch); }
  u.cmd_overrides[c] = true; return true; }
  if (t.size() > 1 && t[0] == '-'){ std::string c = t.substr(1); for (char & ch : c){ ch = (char)tolower((unsigned char)ch); }
  u.cmd_overrides[c] = false; return true; }

  return false;
}

// Single source of truth for rendering a user's rule
std::string acl_format_user(const std::string &name, const User &u, bool for_config){
  std::string s = "user " + name + (u.enable ? " on" : " off");
  if (u.pw_hashes.empty()){ s += " nopass"; }
  else { for (const std::string &h : u.pw_hashes){
      if (!for_config){ s += " #<hash>"; } // display: always redacted
      else if (h.rfind("$argon2id$", 0) == 0){ s += " \"" + h + "\""; } // PHC: verbatim token
      else { s += " \"#" + h + "\""; }} 
  }                             
  // keys
  if (u.all_keys){ s += " ~*"; }
  else { for (const std::string &p : u.key_patterns){ s += " ~" + p; } }

  if (u.all_channels){ s += " &*"; }
  else { for (const std::string &p : u.channel_patterns){ s += " &" + p; } }
  // commands: full grant, or explicit deny-all base + the granted categories
  if (u.allow_cats == CAT_ALL){ s += " +@all"; }
  else {
    s += " -@all";
    static const std::pair<uint64_t, const char *> cats[] = {
      {CAT_READ,"read"}, {CAT_WRITE,"write"}, {CAT_KEYSPACE,"keyspace"}, {CAT_ADMIN,"admin"},
      {CAT_DANGEROUS,"dangerous"}, {CAT_FAST,"fast"}, {CAT_SLOW,"slow"}, {CAT_CONNECTION,"connection"},
    };
    for (const auto &c : cats){ if (u.allow_cats & c.first){ s += " +@"; s += c.second; } }
  }
  // per-command exceptions on top
  for (const auto &kv : u.cmd_overrides){ s += ' '; s += (kv.second ? '+' : '-'); s += kv.first; }
  return s;
}

static void do_acl(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  std::string sub = cmd[1];
  for (char &c : sub){ c = (char)tolower((unsigned char)c); }

  if (sub == "whoami"){
    return resp_str(out, conn->user->name.data(), conn->user->name.size());
  }

  if (sub == "users"){
    resp_arr(out, (uint32_t)g_config.users.size());
    for (auto &kv : g_config.users){ resp_str(out, kv.first.data(), kv.first.size()); }
    return; 
  }

  if (sub == "cat"){
    static const char *cats[] = {"read", "write", "keyspace", "admin", "dangerous", "fast", "slow", "connection" };
    resp_arr(out, (uint32_t)(sizeof(cats) / sizeof(cats[0]))); // resp array header
    for (const char *c : cats){ resp_str(out, c, strlen(c)); }
    return; 
  }

  if (sub == "genpass"){
    int bits = 256;
    if (cmd.size() >= 3){ 
      int64_t b = 0;
      if (!str2int(cmd[2], b) || b < 1 || b > 4096){ 
        return resp_err(out, MSG_SYNTAX); 
      }
      bits = (int)b;
    }
    int nhex = (bits + 3) / 4;
    static const char *hx = "0123456789abcdef";
    std::string s; s.reserve(nhex);
    for (int i = 0; i < nhex; ++i){ s += hx[rand_idx(16)]; }
    return resp_str(out, s.data(), s.size());
  }


  if (sub == "setuser"){
    if (cmd.size() < 3){ return resp_err(out, "ERR wrong number of arguments for 'acl|setuser'"); }
    // create if absent, stable address
    User &u = g_config.users[cmd[2]];
    // new user. disable, no perms, no keys
    if (u.name.empty()){ u.name = cmd[2]; }
    for (size_t i = 3; i < cmd.size(); ++i){
      if (!acl_apply_rule(u, cmd[i])){
        return resp_err(out, ("ERR Error in ACL SETUSER modificer '" + cmd[i] + "'").c_str());
      }
    }
    if (u.name.empty()){ u.name = cmd[2]; }
    audit_event("acl_change", conn, " sub=setuser target=" + cmd[2] +
                " rules=" + std::to_string(cmd.size() - 3) + " result=ok"); // we count the rules, do not show
    return resp_ok(out);
  }

  if (sub == "deluser"){
    if (cmd.size() < 3){ return resp_err(out, "ERR wrong number of arguments for 'acl|deluser'"); }
    if (cmd[2] == "default"){ return resp_err(out, "ERR the 'default' user cannot be removed"); }
    auto it = g_config.users.find(cmd[2]);
    if (it == g_config.users.end()){ return resp_int(out, 0); }
    User *victim = &it->second;
    // critical: no dangling Conn::user
    for (Conn *c : g_data.fd2conn){
      if (c && c->user == victim){ 
        c->user = nullptr;
        c->want_close = true;
      }
    }
    audit_event("acl_change", conn, " sub=deluser target=" + cmd[2] + " result=ok");
    g_config.users.erase(it);
    return resp_int(out, 1);
  }

  if (sub == "getuser"){
    if (cmd.size() < 3){ return resp_err(out, "ERR wrong number og arguments for 'acl|getuser'"); }
    auto it = g_config.users.find(cmd[2]);
    if (it == g_config.users.end()){ return resp_nil(out); }
    const User &u = it->second;
    resp_arr(out, 6);
    resp_str(out, "flags", 5);
    resp_str(out, u.enable ? "on" : "off", u.enable ? 2 : 3);
    resp_str(out, "commands", 8);
    { std::string c = (u.allow_cats == CAT_ALL ?  "+@all" : (u.allow_cats ? "+<cats>" : "-@all")); 
      resp_str(out, c.data(), c.size()); }
    resp_str(out, "keys", 4);
    { std::string k = u.all_keys ? "~*" : ""; 
      for (auto &p : u.key_patterns){ k += (k.empty() ? "" : " ") + ("~" + p); }
      resp_str(out, k.data(), k.size()); }
    return;
  }

  if (sub == "list"){
    resp_arr(out, (uint32_t)g_config.users.size());
    for (auto &kv : g_config.users){
      std::string line = acl_format_user(kv.first, kv.second, false);
      resp_str(out, line.data(), line.size());
    }
    return;
  }
  return resp_err(out, "ERR Unknown ACL subcommand or wrong number of arguments");
}

// SUBSCRIBE/UNSUBSCRIBE/PUBLISH need the Conn (register/deregister, and PUBLISH
// writes into *other* conns' buffers), so do_request special-cases them by
// canonical name exactly like AUTH/ACL. 
static void do_pubsub_stub(std::vector<std::string> &, Buffer *){}

// The count Redis reports in  every (p)subscribe/(p)unsubscribe confirmation is the conn's
// TOTAL subscription count - channels + patterns (clientSubscriptionCount)
static size_t pubsub_count(const Conn *conn){
  return conn->sub_channels.size() + conn->sub_patterns.size();
}

// one "<kind> <channel> <count>" confirmation array (kind = subscribe/unsubscribe)
static void pubsub_confirm(Buffer *out, const char *kind, const std::string *chan, int64_t count){
  resp_arr(out, 3);
  resp_str(out, kind, strlen(kind));
  if (chan){ resp_str(out, chan->data(), chan->size()); }
  else { resp_nil(out); } // UNSUBSCRIBE with nothing subscribed
  resp_int(out, count);
}

// nullptr = allowed, else a NOPERM message 
static const char *acl_channel_check(const User *u, const std::string &chan, bool literal){
  static const char *deny = "NOPERM no permissions to access one of the channels used as arguments";
  if (!u){ return deny; }
  if (u->all_channels){ return nullptr; }
  for (const std::string &pat : u->channel_patterns){
    if (literal ? (pat == chan)
                : glob_match(pat.data(), pat.size(), chan.data(), chan.size())){
      return nullptr;
    }
  }
  return deny;
}

static void do_subscribe(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  // pre-scan every argument
  for (size_t i = 1; i < cmd.size(); ++i){
    if (const char *deny = acl_channel_check(conn->user, cmd[i], false)){
      audit_event("acl_deny", conn, " cmd=subscribe reaseon=channel");
      return resp_err(out, deny);
    }
  }
  // SUBSCRIBE channel [channel ...] (min_args = 2 already checked)
  for (size_t i = 1; i < cmd.size(); ++i){
    const std::string &ch = cmd[i];
    // idempotent: set dedupes; only touch the registry on first join
    if (conn->sub_channels.insert(ch).second){
      g_data.channels[ch].insert(conn);
    }
    pubsub_confirm(out, "subscribe", &ch, (int64_t)pubsub_count(conn));
  }
}

// remove one (conn, channel) edge from the global registry, erasing the channel
// entry once its last subscriber leaves so the map dosen't accumulate empties
static void pubsub_unlink(const std::string &ch, Conn *conn){
  auto it = g_data.channels.find(ch);
  if (it == g_data.channels.end()){ return; }
  it->second.erase(conn);
  if (it->second.empty()){ g_data.channels.erase(it); }
}

static void do_unsubscribe(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  if (cmd.size() == 1){
    // no argsL unsubscribe from everything currently held
    if (conn->sub_channels.empty()){
      return pubsub_confirm(out, "unsubscribe", nullptr, (int64_t)pubsub_count(conn));
    }
    // snapshot first: we mutate sub_channels while iterating
    std::vector<std::string> all(conn->sub_channels.begin(), conn->sub_channels.end());
    for (const std::string &ch : all){
      pubsub_unlink(ch, conn);
      conn->sub_channels.erase(ch);
      pubsub_confirm(out, "unsubscribe", &ch, (int64_t)pubsub_count(conn));
    }
    return;
  }  
  // explicit channels: reply for each arg even if we weren't subscribed (like redis ":3")
  for (size_t i = 1; i < cmd.size(); ++i){
    const std::string &ch = cmd[i];
    if (conn->sub_channels.erase(ch)){ pubsub_unlink(ch, conn); }
    pubsub_confirm(out, "unsubscribe", &ch, (int64_t)pubsub_count(conn)); 
  }
}

static void do_psubscribe(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  // pre-scan every argument
  for (size_t i = 1; i < cmd.size(); ++i){
    if (const char *deny = acl_channel_check(conn->user, cmd[i], true)){
      audit_event("acl_deny", conn, " cmd=psubscribe reaseon=channel");
      return resp_err(out, deny);
    }
  }
  // PSUBSCRIBE pattern [pattern ...]
  for (size_t i = 1; i < cmd.size(); ++i){
    const std::string &pat = cmd[i];
    // idempotent, like SUBSCRIBE
    if (conn->sub_patterns.insert(pat).second){
      g_data.patterns[pat].insert(conn);
    }
    pubsub_confirm(out, "psubscribe", &pat, (int64_t)pubsub_count(conn));
  }
}

// mirror of pubsub_unlink for the pattern registry
static void pubsub_punlink(const std::string &pat, Conn *conn){
  auto it = g_data.patterns.find(pat);
  if (it == g_data.patterns.end()){ return; }
  it->second.erase(conn);
  if (it->second.empty()){ g_data.patterns.erase(it); }
}

static void do_punsubscribe(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  if (cmd.size() == 1){
    if (conn->sub_patterns.empty()){
      return pubsub_confirm(out, "punsubscribe", nullptr, (int64_t)pubsub_count(conn));
    }
    // snapshot: we mutate sub_patterns while walking it
    std::vector<std::string> all(conn->sub_patterns.begin(), conn->sub_patterns.end());
    for (const std::string &pat : all){
      pubsub_punlink(pat, conn);
      conn->sub_patterns.erase(pat);
      pubsub_confirm(out, "punsubscribe", &pat, (int64_t)pubsub_count(conn));
    }
    return;
  }
  for (size_t i = 1; i < cmd.size(); ++i){
    const std::string &pat = cmd[i];
    if (conn->sub_patterns.erase(pat)){ pubsub_punlink(pat, conn); }
    pubsub_confirm(out, "punsubscribe", &pat, (int64_t)pubsub_count(conn));
  }
}

static void do_publish(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  if (const char *deny = acl_channel_check(conn->user, cmd[1], false)){
    audit_event("acl_deny", conn, " cmd=publish reason=channel");
    return resp_err(out, deny);
  }
  const std::string &chan = cmd[1];
  const std::string &msg = cmd[2];
  int64_t receivers = 0;
  // exact-channel subscribers -> 3-element 'message'
  auto it = g_data.channels.find(chan);
  if (it != g_data.channels.end()){
    // we only read the set here - no invalidation
    for (Conn *sub : it->second){
      // push straigh into the subscriber's outgoing: *3 message <chan> <msg>
      resp_arr(&sub->outgoing, 3);
      resp_str(&sub->outgoing, "message", 7);
      resp_str(&sub->outgoing, chan.data(), chan.size());
      resp_str(&sub->outgoing,msg.data(), msg.size());
      // poll rebuilds events from this each tick
      sub->want_write = true; 
      ++receivers;
    }
  }

  // pattern subscribers -> 4-element 'pmessage'
  for (const auto &pe : g_data.patterns){
    const std::string &pat = pe.first;
    if (!glob_match(pat.data(), pat.size(), chan.data(), chan.size())){ continue; }
    for (Conn *sub : pe.second){
      resp_arr(&sub->outgoing, 4);
      resp_str(&sub->outgoing, "pmessage", 8);
      resp_str(&sub->outgoing, pat.data(), pat.size());
      resp_str(&sub->outgoing, chan.data(), chan.size());
      resp_str(&sub->outgoing, msg.data(), msg.size());
      sub->want_write = true;
      ++receivers;
    }
  }
  resp_int(out, receivers);
}

// teardown: called from conn_destroy so PUBLISH never dereferences a freed Conn
void pubsub_remove_conn(Conn *conn){
  for (const std::string &ch : conn->sub_channels){ pubsub_unlink(ch, conn); }
  conn->sub_channels.clear(); 
  for (const std::string &pat : conn->sub_patterns){ pubsub_punlink(pat, conn); }
  conn->sub_patterns.clear(); 
}

static void do_acl_placeholder(std::vector<std::string> &, Buffer *){} // real dispact is conn-aware

static std::unordered_map<std::string_view, CmdSpec> k_cmd_table = {
  // strings
  {"get",          {do_get,           2,  2}},
  {"set",          {do_set,           3,  3, true}},
  {"incr",         {do_incr,          2,  2, true}},
  {"decr",         {do_decr,          2,  2, true}},
  {"incrby",       {do_incrby,        3,  3, true}},
  {"decrby",       {do_decrby,        3,  3, true}},
  {"incrbyfloat",  {do_incrbyfloat,   3,  3, true}},
  {"setex",        {do_setex,         4, 4, true, true}},
  {"psetex",       {do_psetex,        4, 4, true, true}},
  {"setnx",        {do_setnx,         3,  3, true}},
  {"getset",       {do_getset,        3,  3, true}},
  {"getdel",       {do_getdel,        2,  2, true}},
  {"getex",        {do_getex,         2, -1, true, true}},
  {"append",       {do_append,        3,  3, true}},
  {"strlen",       {do_strlen,        2,  2}},
  {"getrange",     {do_getrange,      4,  4}},
  {"setrange",     {do_setrange,      4,  4, true}},
  {"mget",         {do_mget,          2, -1}},
  {"mset",         {do_mset,          3, -1, true}},   // odd-argc check is inside handler
  {"msetnx",       {do_msetnx,        3, -1, true}},   // odd-argc check is inside handler
  // generic key
  {"del",          {do_del,           2, -1, true}},
  {"exists",       {do_exists,        2, -1}},
  {"type",         {do_type,          2,  2}},
  {"rename",       {do_rename,        3,  3, true}},
  {"renamenx",     {do_renamenx,      3,  3, true}},
  {"touch",        {do_touch,         2, -1}},
  {"unlink",       {do_asyncdel,      2,  2, true}},
  {"keys",         {do_keys,          1,  2,}},
  {"scan",         {do_scan,          2, -1}},
  {"randomkey",    {do_randomkey,     1,  1}},
  {"dbsize",       {do_dbsize,        1,  1}},
  {"flushall",     {do_flushall,      1,  1, true}},
  {"flushdb",      {do_flushall,      1,  1, true}},
  // ttl
  {"expire",       {do_expire,        3, 3, true, true}},
  {"pexpire",      {do_pexpire,       3, 3, true, true}},
  {"expireat",     {do_expireat,      3, 3, true, true}},
  {"pexpireat",    {do_pexpireat,     3, 3, true}},
  {"ttl",          {do_ttl_seconds,   2,  2}},
  {"pttl",         {do_ttl,           2,  2}},
  {"persist",      {do_persist,       2,  2, true}},
  // sorted set
  {"zadd",         {do_zadd,          4,  -1, true}},
  {"zrem",         {do_zrem,          3,  3, true}},
  {"zscore",       {do_zscore,        3,  3}},
  {"zrank",        {do_zrank,         3,  3}},
  {"zquery",       {do_zquery,        6,  6}},
  {"zrevquery",    {do_zquery_reversed,6, 6}},
  {"zpopmin",      {do_zpopmin,       2,  3, true}},
  // list
  {"lpush",        {do_lpush,         3, -1, true}},
  {"rpush",        {do_rpush,         3, -1, true}},
  {"lpop",         {do_lpop,          2,  2, true}},
  {"rpop",         {do_rpop,          2,  2, true}},
  {"llen",         {do_llen,          2,  2}},
  {"lindex",       {do_lindex,        3,  3}},
  {"lrange",       {do_lrange,        4,  4}},
  {"lset",         {do_lset,          4,  4, true}},
  {"linsert",      {do_linsert,       5,  5, true}},
  {"lrem",         {do_lrem,          4,  4, true}},
  {"ltrim",        {do_ltrim,         4,  4, true}},
  // hash
  {"hset",         {do_hset,          4, -1, true}},
  {"hget",         {do_hget,          3,  3}},
  {"hdel",         {do_hdel,          3, -1, true}},
  {"hexists",      {do_hexists,       3,  3}},
  {"hlen",         {do_hlen,          2,  2}},
  {"hgetall",      {do_hgetall,       2,  2}},
  {"hkeys",        {do_hkeys,         2,  2}},
  {"hvals",        {do_hvals,         2,  2}},
  {"hmget",        {do_hmget,         3, -1}},
  {"hsetnx",       {do_hsetnx,        4,  4, true}},
  {"hincrby",      {do_hincrby,       4,  4, true}},
  {"hstrlen",      {do_hstrlen,       3,  3}},
  {"hscan",        {do_hscan,         3, -1}},
  // set
  {"sadd",         {do_sadd,          3, -1, true}},
  {"srem",         {do_srem,          3, -1, true}},
  {"sismember",    {do_sismember,     3,  3}},
  {"smismember",   {do_smismember,    3, -1}},
  {"scard",        {do_scard,         2,  2}},
  {"smembers",     {do_smembers,      2,  2}},
  {"spop",         {do_spop,          2,  3, true, false, true}},
  {"srandmember",  {do_srandmember,   2,  3}},
  {"sscan",        {do_sscan,         3, -1}},
  {"sinter",       {do_sinter,        2, -1}},
  {"sunion",       {do_sunion,        2, -1}},
  {"sdiff",        {do_sdiff,         2, -1}},
  {"sinterstore",  {do_sinterstore,   3, -1, true}},
  {"sunionstore",  {do_sunionstore,   3, -1, true}},
  {"sdiffstore",   {do_sdiffstore,    3, -1, true}},
  {"smove",        {do_smove,         4,  4, true}},
  // server
  {"info",         {do_info,          1,  1}},
  {"save",         {do_save,          1,  1}},
  {"bgsave",       {do_bgsave,        1,  1}},
  {"bgrewriteaof", {do_bgrewriteaof,  1,  1}},
  {"ping",         {do_ping,          1,  2}},
  {"echo",         {do_echo,          2,  2}},
  {"config",       {do_config,        2, -1}},
  {"memory",       {do_memory,        2, -1}},
  {"object",       {do_object,        2, -1}},
  {"acl",          {do_acl_placeholder, 2, -1}},
  // pubsub
  {"subscribe",    {do_pubsub_stub,   2, -1}},
  {"unsubscribe",  {do_pubsub_stub,   1, -1}},
  {"publish",      {do_pubsub_stub,   3,  3}},
  {"psubscribe",   {do_pubsub_stub,   2, -1}},
  {"punsubscribe", {do_pubsub_stub,   1, -1}},
};

struct DispatchEntry {
  std::string canonical; // original command name - used for ACL + AOF, never the alies
  CmdSpec spec; // a copy of the (already acl-stamped) canonical spec
};

static std::unordered_map<std::string, DispatchEntry> g_dispatch;

bool command_is_known(const std::string &name){ return k_cmd_table.count(name) != 0; }

// build the frozen live dispatch map: canonical specs first, then apply rename-command
void dispatch_build(){
  g_dispatch.clear();
  g_dispatch.reserve(k_cmd_table.size() * 2);
  for (auto &kv : k_cmd_table){
    std::string name(kv.first);
    // canonical == its own name
    g_dispatch.emplace(name, DispatchEntry{ name, kv.second });
  }
  // r.first =  old (lowercase), r.second new ("" disables)
  for (const auto &r : g_config.renames){
    auto it = g_dispatch.find(r.first);
    if (it == g_dispatch.end()){ continue; }
    // preserve the original canonical
    DispatchEntry entry = it->second;
    // the old name stops working
    g_dispatch.erase(it);
    // else disable
    if (!r.second.empty()){ g_dispatch.emplace(r.second, std::move(entry)); }
  }
}

void metadata_selfcheck(){
  int problems = 0;
  for (const auto &kv : k_cmd_table){
    const CmdSpec &s = kv.second;
    const std::string_view n = kv.first;
    // every commmand must be grantable by some category
    if (s.acl_cats == 0){
      fprintf(stderr, "selfcheck: '%.*s' has acl_cats==0 (was acl_init_categories() run?)\n",
              (int)n.size(), n.data());
      problems++;
    }
    // control plane commands must not carry @read/@write
    if((s.acl_cats & (CAT_ADMIN | CAT_DANGEROUS)) && (s.acl_cats & (CAT_READ | CAT_WRITE))){
      fprintf(stderr, "selfcheck: '%.*s' is admin/dangerous but still carries @read/@write\n",
            (int)n.size(), n.data());
      problems++;
    }
  }
  if (problems){
    fprintf(stderr, "selfcheck: %d command-metadata problem(s)\n", problems);
    die("command metadata self-check failed");
  }
}

// Commands that can only free or leave memory unchanged are never denied under
static bool cmd_can_grow_memory(const std::string &name){
  // name is already lower-cased
  static const std::unordered_set<std::string_view> no_grow {
    "del", "unlink", "flushall", "flushdb",
    "expire", "pexpire", "expireat", "pexpireat", "persist",
    "getdel", "getex",
    "lpop", "rpop", "lrem", "ltrim",
    "spop", "srem", "hdel", "zrem", "zpopmin",
    "rename", "renamenx",
  };
  return no_grow.count(name) == 0;
}

// commands allowed while a conn is in subscribe mode (RESP2 rule)
static bool cmd_ok_in_subscribe(const std::string &canonical){
  return canonical == "subscribe" || canonical == "unsubscribe" ||
         canonical == "psubscribe"|| canonical == "punsubscribe"|| // this is for v8.2
         canonical == "ping"      || canonical == "quit" || canonical == "reset";
}

void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn, const char *raw, size_t raw_len) {
  if (cmd.empty()) {
    return resp_err(out, "ERR empty command");
  }

  // command names are case-insesitve
  for (char &ch : cmd[0]){
    ch = (char)tolower((unsigned char)ch);
  }

  if (cmd[0] == "auth"){
    return do_auth(cmd, out, conn);
  }

  // resolve the typed name against the live map
  auto it = g_dispatch.find(cmd[0]);
  bool found = (it != g_dispatch.end());
  const CmdSpec *specp = found ? &it->second.spec : nullptr;
  const std::string canonical = found ? it->second.canonical : cmd[0];

  // AOF frames always carry canonical names
  if (!found && g_data.g_loading){
    auto kt = k_cmd_table.find(cmd[0]);
    if (kt != k_cmd_table.end()){ specp = &kt->second; found = true; }
  }

  // AUTH always allowed, even under an alias is always allowed
  if (found && canonical == "auth") {
    return do_auth(cmd, out, conn);
  }

  // check authentication
  if (!conn->user) {
    return resp_err(out, "NOAUTH authentication required");
  }

  // Subscribe-mode gate : once subscribed, only pub/sub + PING/QUIT/RESET run.
  if (!(conn->sub_channels.empty() && conn->sub_patterns.empty()) && !cmd_ok_in_subscribe(canonical)){
    return resp_err(out,
      "ERR only (P)SUBSCRIBE / (P)UNSUBSCRIBE / PING / QUIT / RESET are allowed in subscribe mode");
  }

  if (!found){ return resp_err(out, "ERR unknown command"); }
  const CmdSpec &spec = *specp;

  // permission by canonical name
  if (const char *deny = acl_check(conn->user, canonical, spec, cmd)){
    audit_event("acl_deny", conn, " cmd=" + canonical +
                " reason=" + (strstr(deny, "key") ? "key" : "command"));
    return resp_err(out, deny);
  }

  int argc = (int)cmd.size();
  if (argc < spec.min_args || (spec.max_args != -1 && argc > spec.max_args)){
    return resp_err(out, "ERR wrong number of arguments");
  }

  if (canonical == "acl"){          return do_acl(cmd, out, conn); } // permission checked above; needs conn

  if (canonical == "subscribe"){    return do_subscribe(cmd, out, conn); }
  if (canonical == "unsubscribe"){  return do_unsubscribe(cmd, out, conn); }
  if (canonical == "publish"){      return do_publish(cmd, out, conn); }
  if (canonical == "psubscribe"){   return do_psubscribe(cmd, out, conn); }
  if (canonical == "punsubscribe"){ return do_punsubscribe(cmd, out, conn); }
  

  // audit sensitive commands
  if (spec.acl_cats & (CAT_ADMIN | CAT_DANGEROUS)){
    audit_event((spec.acl_cats & CAT_ADMIN) ? "admin_command" : "dangerous_command",
                conn, " cmd=" + canonical);
  }

  if (spec.is_write && g_config.aof_enable && g_data.g_aof_write_err && !g_data.g_loading){
    return resp_err(out, "MISCONF Errors writing to the AOF file, can't accept writes");
  }

  // maxmemory enforcement: make room before a write, or refuse it
  if (spec.is_write && cmd_can_grow_memory(canonical) && !free_memory_if_needed()){
    return resp_err(out, "OOM commands not allowed when used memory > 'maxmemory'");
  }

  uint32_t dirty_before = g_data.g_writes_since_save;

  // Snapshot before running swap() handlers/ consume cmd's
  bool may_log = g_config.aof_enable && spec.is_write && !g_data.g_loading && !spec.aof_self;
  bool renamed = (cmd[0] != canonical);
  std::vector<std::string> snapshot;
  if (may_log && (spec.aof_rewrite || renamed)){ snapshot  = cmd; }
  spec.fn(cmd, out);

  if (may_log && g_data.g_writes_since_save != dirty_before){
    if (spec.aof_rewrite || renamed){
      if (renamed){ snapshot[0] = canonical; }
      // rare: re-encode with absolute PEXPIREAT
      aof_feed(snapshot);
    } else {
      // common: verbatim memcpy, no copy/re-encode
      aof_append_raw(raw, raw_len);
    }
  }
  #ifndef NDEBUG
  mem_selfcheck(canonical.c_str());   // prints "[mem] drift..." if any handler mis-accounted
  #endif
}

// case insensitive subcommand compare
static bool acl_sub_is(const std::string &s, const char *lit){
  // cmd[1] is not lowercase
  size_t n = strlen(lit);
  if (s.size() != n){ return false; }
  for (size_t i = 0; i < n; ++i){ if (tolower((unsigned char)s[i]) != lit[i]){ return false; } }
  return true;
}

static void kr_smove(const std::vector<std::string> &cmd, std::vector<std::string_view> &keys){
  if (cmd.size() > 1){ keys.emplace_back(cmd[1]); } // source
  if (cmd.size() > 2){ keys.emplace_back(cmd[2]); } // destination - cmd[3] is a member not a key
}

static void kr_object(const std::vector<std::string> &cmd, std::vector<std::string_view> &keys){
  // OBJECT ENCODING|REFCOUNT|IDLETIME|FREQ key -> key at cmd[2];  HELP/STATS -> no key
  if (cmd.size() > 2 && (acl_sub_is(cmd[1],"encoding") || acl_sub_is(cmd[1],"refcount")
                      || acl_sub_is(cmd[1],"idletime") || acl_sub_is(cmd[1],"freq"))){
    keys.emplace_back(cmd[2]);
  }
}

static void kr_memory(const std::vector<std::string> &cmd, std::vector<std::string_view> &keys){
  // MEMORY USAGE key [samples n] -> key at cmd[2]; DOCTOR/STATS -> no key
  if (cmd.size() > 2 && acl_sub_is(cmd[1], "usage")){
    keys.emplace_back(cmd[2]);
  }
}

// Populate CmdSpec::Acl_cats once at boot, the  @read/@write base is derived from the
// existing is_write flag, simplified taxonomy vs redis
void acl_init_categories(){

  static const std::unordered_map<std::string_view, KeyResolver> resolvers = {
    {"smove", kr_smove}, {"object", kr_object}, {"memory", kr_memory},
  };

  static const std::unordered_map<std::string_view, KeySpec> ks = {
    // no key argument at all -> skip key checks (category-gated only)
    {"ping",KeySpec::NONE}, {"info",KeySpec::NONE}, {"config",KeySpec::NONE},
    {"dbsize",KeySpec::NONE}, {"randomkey",KeySpec::NONE},{"flushall",KeySpec::NONE},
    {"flushdb",KeySpec::NONE}, {"save",KeySpec::NONE}, {"bgsave",KeySpec::NONE},
    {"bgrewriteaof",KeySpec::NONE}, {"auth",KeySpec::NONE}, {"keys",KeySpec::NONE},
    {"scan",KeySpec::NONE}, {"acl", KeySpec::NONE},
    // every arg from index 1 is a key
    {"del",KeySpec::ALL_FROM_1}, {"unlink",KeySpec::ALL_FROM_1}, {"exists",KeySpec::ALL_FROM_1},
    {"touch",KeySpec::ALL_FROM_1}, {"mget",KeySpec::ALL_FROM_1},
    {"rename",KeySpec::ALL_FROM_1}, {"renamenx",KeySpec::ALL_FROM_1},
    {"sinter",KeySpec::ALL_FROM_1}, {"sunion",KeySpec::ALL_FROM_1}, {"sdiff",KeySpec::ALL_FROM_1},
    {"sinterstore",KeySpec::ALL_FROM_1}, {"sunionstore",KeySpec::ALL_FROM_1},
    {"sdiffstore",KeySpec::ALL_FROM_1},
    // key value key value ...
    {"mset",KeySpec::STRIDE2_FROM_1},{"msetnx",KeySpec::STRIDE2_FROM_1},
    {"subscribe",KeySpec::NONE}, {"unsubscribe",KeySpec::NONE}, {"publish",KeySpec::NONE},
    {"psubscribe",KeySpec::NONE}, {"punsubscribe",KeySpec::NONE},
  };

    // category bits OR'd on TOP of the READ/WRITE base (this is where acl's line lives)
  static const std::unordered_map<std::string_view, uint64_t> extra = {
    {"acl",     CAT_ADMIN | CAT_DANGEROUS},
    {"config",  CAT_ADMIN | CAT_DANGEROUS},
    {"save",    CAT_ADMIN | CAT_DANGEROUS}, {"bgsave", CAT_ADMIN | CAT_DANGEROUS},
    {"bgrewriteaof", CAT_ADMIN | CAT_DANGEROUS},
    {"flushall",CAT_KEYSPACE | CAT_DANGEROUS}, {"flushdb", CAT_KEYSPACE | CAT_DANGEROUS},
    {"keys",    CAT_KEYSPACE | CAT_DANGEROUS | CAT_SLOW},
    {"scan",    CAT_KEYSPACE | CAT_SLOW},
    {"dbsize",  CAT_KEYSPACE}, {"randomkey", CAT_KEYSPACE},
    {"del",     CAT_KEYSPACE}, {"unlink", CAT_KEYSPACE}, {"exists", CAT_KEYSPACE},
    {"touch",   CAT_KEYSPACE}, {"rename", CAT_KEYSPACE}, {"renamenx", CAT_KEYSPACE},
    {"ping",    CAT_CONNECTION}, {"auth", CAT_CONNECTION},
    {"info",    CAT_ADMIN},     {"memory", CAT_ADMIN}, {"object", CAT_ADMIN},
    {"echo", CAT_CONNECTION},
    {"subscribe", CAT_FAST}, {"unsubscribe", CAT_FAST}, {"publish", CAT_FAST},
    {"psubscribe", CAT_FAST}, {"punsubscribe", CAT_FAST},
  };

  for (auto &kv : k_cmd_table){
    CmdSpec &s = kv.second;
    s.acl_cats = s.is_write ? CAT_WRITE : CAT_READ; // base axis
    auto eit = extra.find(kv.first);
    if (eit != extra.end()){ 
      // OR the extra bits
      s.acl_cats |= eit->second; 
      if (eit->second & (CAT_ADMIN | CAT_DANGEROUS)){
        s.acl_cats &= ~(CAT_READ | CAT_WRITE);
      }
    }
    auto kit = ks.find(kv.first);
    s.keys = (kit != ks.end()) ? kit->second : KeySpec::FIRST;

    auto rit = resolvers.find(kv.first);
    s.key_resolver = (rit != resolvers.end()) ? rit->second : nullptr;
  }
}