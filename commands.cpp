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
#include "hash.h"
#include "set.h"
#include <algorithm>
#include <random>
#include <unordered_map>
#include <string_view>

static constexpr const char *MSG_WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value";
static constexpr const char *MSG_NOT_INT   = "ERR value is not an integer or out of range";
static constexpr const char *MSG_NOT_FLOAT = "ERR value is not a valid float";
static constexpr const char *MSG_SYNTAX    = "ERR syntax error";
static constexpr const char *MSG_OUT_OF_RANGE = "ERR index out of range";

static std::mt19937_64 g_rng{std::random_device{}()};

static size_t rand_idx(size_t n){
  return std::uniform_int_distribution<size_t>(0 , n - 1)(g_rng);
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


// WARNING: swaps cmd[i] into the LookupKey, leaving cmd[i] empty after the call.
// If you need cmd[i] after the call, use a non-destructive hm_lookup copy instead.
static Lookup lookup_entry(std::string &keystr, uint32_t want_type, bool create, Entry **out_ent){
  LookupKey key;
  key.key.swap(keystr);
  key.node.hcode = str_hash((const uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (node){
    Entry *ent = container_of(node, Entry, node);
    if (!expire_if_needed(ent)){                 // alive
      if (ent->type != want_type) return Lookup::WRONGTYPE;
      if (out_ent) *out_ent = ent;
      return Lookup::OK;
    }
    // expired & deleted -> fall through as missing
  }

  if (!create) return Lookup::MISSING;

  Entry *ent = entry_new(want_type);
  ent->key.swap(key.key);                        // key.key holds the real key
  ent->node.hcode = key.node.hcode;
  hm_insert(&g_data.db, &ent->node);
  if (out_ent) *out_ent = ent;
  return Lookup::OK;                             // create never returns MISSING
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
    Entry *e = container_of(node, Entry, node);
    if (!expire_if_needed(e)){
      return resp_int(out, 0); // alive so we dont do nothing
    }
    // expired -> fall through
  }
  // now we use the cmd[1]
  Entry *ent;
  lookup_entry(cmd[1], T_STR, true, &ent);
  entry_str(ent).swap(cmd[2]);
  g_data.g_writes_since_save++;
  resp_int(out, 1);
}

static void do_getset(std::vector<std::string> &cmd, Buffer *out){
  LookupKey lk;
  lk.key = cmd[1]; // we copy
  lk.node.hcode = str_hash((uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);

  if (node){
    Entry *ent = container_of(node, Entry, node);
    if (!expire_if_needed(ent)){
      if (ent->type != T_STR){
        return resp_err(out, MSG_WRONGTYPE);
      }
      std::string old = std::move(entry_str(ent)); // extract the old value
      entry_str(ent).swap(cmd[2]); // set the new value
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
    entry_set_ttl(ent, -1);
    return resp_str(out, entry_str(ent).data(), entry_str(ent).size());
  }

  if (cmd.size() != 4){ return resp_err(out, MSG_SYNTAX); }
  int64_t v = 0;
  if (!str2int(cmd[3], v)){ return resp_err(out, MSG_NOT_INT); }

  int64_t ttl_ms = 0;
  if (opt == "ex"){ if (v <= 0) return resp_err(out, "ERR invalid expire time in getex command"); ttl_ms = v * 1000; }
  else if (opt == "px"){ if (v <= 0) return resp_err(out, "ERR invalid expire time in getex command"); ttl_ms = v; }
  else if (opt == "exat"){ ttl_ms = v * 1000 - (int64_t)get_wall_msec(); }
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
      Entry *e = container_of(node, Entry, node);
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
      Entry *e = container_of(node, Entry, node);
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
      entry_del(container_of(node, Entry, node));
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

// WARNING: O(N) full keyspace scan — blocks the event loop.
// Never run against a large DB in production; steer clients to SCAN instead.
static bool cb_keys_emit(HNode *node, void *arg) {
    Buffer *out = (Buffer *)arg;
    Entry *ent = container_of(node, Entry, node);
    resp_str(out, ent->key.data(), ent->key.size());
    return true;
}


// Gets all the keys for the stats 
static bool cb_count_keys(HNode *node, void *args){
  KeyStats *stats = (KeyStats *)args;
  Entry *ent = container_of(node, Entry, node);
  stats->total++;
  if (entry_has_ttl(ent)){
    stats->with_ttl++;
  }
  return true;
}

static void do_keys(std::vector<std::string> &cmd, Buffer *out) {
  (void)cmd;
  resp_arr(out, (uint32_t)hm_size(&g_data.db));
  hm_foreach(&g_data.db, cb_keys_emit, out);
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

  Entry *ent = container_of(node, Entry, node);
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

  Entry *ent = container_of(node, Entry, node);
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
    Entry *ent = container_of(node, Entry, node);
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
  Entry *ent = container_of(node, Entry, node);

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
  Entry *ent = container_of(node, Entry, node);

  if (expire_if_needed(ent)){ return resp_int(out, 0); } // expired -> gone
  if (!entry_has_ttl(ent)){ return resp_int(out, 0); } // no TTL to remove
  entry_set_ttl(ent, -1); // detach from the TTL heap

  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

// zadd and zset (score, name)
static void do_zadd(std::vector<std::string> &cmd, Buffer *out){
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return resp_err(out, "ERR invalid score");
  }
  Entry *ent;
  if (lookup_entry(cmd[1], T_ZSET, true, &ent) == Lookup::WRONGTYPE){
    return resp_err(out, "WRONGTYPE wrong type");
  }

  // add or update the tuple
  bool added = zset_insert(&entry_zset(ent), cmd[3].data(), cmd[3].size(), score);
  g_data.g_writes_since_save++;
  return resp_int(out, (int64_t)added);
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
  rdb_check_background_save(); // reap child before checking
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
static void do_asyncdel(std::vector<std::string> &cmd, Buffer *out){
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
    case Lookup::MISSING:   return resp_int(out, 0);
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
  entry_deque(ent).buf[deque_phys(&entry_deque(ent), (size_t)idx)] = cmd[3];
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

  // we ensure capaxity before opening the gap
  if (entry_deque(ent).count == entry_deque(ent).cap){ deque_grow(&entry_deque(ent)); }

  deque_open_gap(&entry_deque(ent), insert_idx);
  entry_deque(ent).buf[deque_phys(&entry_deque(ent), insert_idx)] = value;

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
  }

  if (removed > 0){ g_data.g_writes_since_save++; }
  resp_int(out, removed);
}

// LTRIM key start stop
void do_ltrim(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_DLIST, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING:   return resp_int(out, 0);
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
    entry_deque(ent).buf[deque_phys(&entry_deque(ent), (size_t)i)].clear();
  }
  for (int64_t i = stop + 1; i < n; ++i){
    entry_deque(ent).buf[deque_phys(&entry_deque(ent), (size_t)i)].clear();
  }

  entry_deque(ent).head = deque_phys(&entry_deque(ent), (size_t)start); // compute before changing count
  entry_deque(ent).count = keep;


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
  Entry *ent = container_of(node, Entry, node);

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
  HashNode *hn = container_of(node, HashNode, node);
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
  HashNode *hn = container_of(node, HashNode, node);
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
  ((std::vector<Entry *> *)arg)->push_back(container_of(node, Entry, node));
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
    ctx->key = container_of(node, Entry, node)->key;
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
  Entry *sent = container_of(snode, Entry, node);
  if (expire_if_needed(sent)){ return -1; } // source expired
  if (src == dst){ return 1; } // rename to self

  LookupKey dk; dk.key = dst;
  dk.node.hcode = str_hash((const u_int8_t *)dst.data(), dst.size());
  HNode *dnode = hm_lookup(&g_data.db, &dk.node, &entry_eq);
  if (dnode){
    Entry *dent = container_of(dnode, Entry, node);
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
      Entry *ent = container_of(node, Entry, node);
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

  Entry *ent = container_of(node, Entry, node);
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
  v->push_back(container_of(node, SetNode, node)->member);
  return true;
}

static bool cb_members_emit(HNode *node, void *arg){
  Buffer *out = (Buffer *)arg;
  resp_str(out, container_of(node, SetNode, node)->member.data(), container_of(node, SetNode, node)->member.size());
  return true;
}

// Non-destructive — takes const ref, makes an internal copy of the key for hashing.
// Use for all read-only lookups (GET, HGET, EXPIRE, etc.)
static Entry *lookup_set_ro(const std::string &key, bool *wrongtype){
  LookupKey lk; lk.key = key;
  lk.node.hcode = str_hash((const uint8_t *)lk.key.data(), lk.key.size());
  HNode *node = hm_lookup(&g_data.db, &lk.node, &entry_eq);

  if (!node){ return nullptr; }
  Entry *ent= container_of(node, Entry, node);
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
    Entry *old = container_of(node, Entry, node);
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
    if (set_add(&entry_set(ent), cmd[i])){ ++added;}
  }
  g_data.g_writes_since_save++;
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

// SPOP key [count]
static void do_spop(std::vector<std::string> &cmd, Buffer *out){
  Entry *ent;
  switch (lookup_entry(cmd[1], T_SET, false, &ent)){
    case Lookup::WRONGTYPE: return resp_err(out, "WRONGTYPE wrong type");
    case Lookup::MISSING: return (cmd.size() >= 3) ? resp_arr(out, 0): resp_nil(out);
    case Lookup::OK:  break;
  }
  std::vector<std::string> members;
  hm_foreach(&entry_set(ent), cb_collect_members, &members);
  if (members.empty()){
    return (cmd.size() >= 3) ? resp_arr(out, 0): resp_nil(out);
  }

  if (cmd.size() == 2){
    // single pop - pick one random member
    size_t idx = (size_t)(rand_idx(members.size()));
    const std::string &picked = members[idx];
    resp_str(out, picked.data(), picked.size());
    set_remove(&entry_set(ent), picked);
  } else {
    int64_t count = 0;
    if (!str2int(cmd[2], count) || count < 0){
      return resp_arr(out, 0);
    }
    if (count == 0){ return resp_arr(out, 0); }
    // partial Fisher-Yates: shuffle the first 'n' slots
    size_t n = ((size_t)count < members.size()) ? (size_t)count : members.size();
    for (size_t i =0; i < n; ++i){
      size_t j = i + (size_t)(rand_idx(members.size() - i));
      std::swap(members[i], members[j]);
    }
    resp_arr(out, (uint32_t)n);
    for (size_t i = 0; i < n; ++i){
      resp_str(out, members[i].data(), members[i].size());
      set_remove(&entry_set(ent), members[i]);
    }
  }
  if (hm_size(&entry_set(ent)) == 0){
    hm_delete(&g_data.db, &ent->node, &hnode_same);
    entry_del(ent);
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
  std::vector<std::string> members;
  hm_foreach(&entry_set(ent), cb_collect_members, &members);
  if (members.empty()){
    return (cmd.size() >= 3) ? resp_arr(out, 0): resp_nil(out);
  }
 if (cmd.size() == 2){
    size_t idx = (size_t)(rand_idx(members.size()));
    return resp_str(out, members[idx].data(), members[idx].size());
 }

 int64_t count = 0;
 if (!str2int(cmd[2], count)){
    return resp_err(out, MSG_NOT_INT);
 }
 if (count == 0){ return resp_arr(out, 0); }

 if (count > 0){
  // distinct members up to min(count, size)
  size_t n = ((size_t)count < members.size() ? (size_t)count : members.size());
  for (size_t i = 0; i < n; ++i){
    size_t j = i + (size_t)(rand_idx(members.size() - i));
    std::swap(members[i], members[j]);
  }
  resp_arr(out, (uint32_t)n);
  for (size_t i = 0; i < n; ++i){
    resp_str(out, members[i].data(), members[i].size());
  }
 } else {
  // negative count: |count| members with replacement
  size_t n = (size_t)(-count);
  resp_arr(out, (uint32_t)n);
  for (size_t i = 0; i < n; ++i){
    size_t idx = (size_t)(rand_idx(members.size()));
    resp_str(out, members[idx].data(), members[idx].size());
  }
 }
}

static void cb_sscan(HNode *node, void *arg){
  ScanCtx *c = (ScanCtx *)arg;
  SetNode *sn = container_of(node, SetNode, node);
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

// SINTERSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sinterstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sinter_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  Entry *dest = set_make_dest(cmd[1]);
  for (auto &m : result){ set_add(&entry_set(dest), m); }
  g_data.g_writes_since_save++;
  return resp_int(out, (int64_t)hm_size(&entry_set(dest)));
}

// SUNIONSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sunionstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sunion_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  Entry *dest = set_make_dest(cmd[1]);
  for (auto &m : result){ set_add(&entry_set(dest), m); }
  g_data.g_writes_since_save++;
  return resp_int(out, (int64_t)hm_size(&entry_set(dest)));
}

// SDIFFSTORE dest key [key...] -- compute first, then store, so dest can be a source
static void do_sdiffstore(std::vector<std::string> &cmd, Buffer *out){
  std::vector<std::string> result;
  if (!sdiff_impl(cmd, 2, result)){ return resp_err(out, "WRONGTYPE wrong type"); }
  Entry *dest = set_make_dest(cmd[1]);
  for (auto &m : result){ set_add(&entry_set(dest), m); }
  g_data.g_writes_since_save++;
  return resp_int(out, (int64_t)hm_size(&entry_set(dest)));
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
  }
  if (!dst_ent){
    dst_ent = entry_new(T_SET);
    dst_ent->key = cmd[2];
    dst_ent->node.hcode = str_hash((const uint8_t *)dst_ent->key.data(), dst_ent->key.size());
    hm_insert(&g_data.db, &dst_ent->node);
  }
  set_add(&entry_set(dst_ent), cmd[3]);
  g_data.g_writes_since_save++;
  return resp_int(out, 1);
}

using CmdFn = void(*)(std::vector<std::string> &, Buffer *);

struct CmdSpec {
  CmdFn fn;
  int min_args;
  int max_args;
};

static const std::unordered_map<std::string_view, CmdSpec> k_cmd_table = {
  // strings
  {"get",          {do_get,           2,  2}},
  {"set",          {do_set,           3,  3}},
  {"incr",         {do_incr,          2,  2}},
  {"decr",         {do_decr,          2,  2}},
  {"incrby",       {do_incrby,        3,  3}},
  {"decrby",       {do_decrby,        3,  3}},
  {"incrbyfloat",  {do_incrbyfloat,   3,  3}},
  {"setex",        {do_setex,         4,  4}},
  {"psetex",       {do_psetex,        4,  4}},
  {"setnx",        {do_setnx,         3,  3}},
  {"getset",       {do_getset,        3,  3}},
  {"getdel",       {do_getdel,        2,  2}},
  {"getex",        {do_getex,         2, -1}},
  {"append",       {do_append,        3,  3}},
  {"strlen",       {do_strlen,        2,  2}},
  {"getrange",     {do_getrange,      4,  4}},
  {"setrange",     {do_setrange,      4,  4}},
  {"mget",         {do_mget,          2, -1}},
  {"mset",         {do_mset,          3, -1}},   // odd-argc check is inside handler
  {"msetnx",       {do_msetnx,        3, -1}},   // odd-argc check is inside handler
  // generic key
  {"del",          {do_del,           2, -1}},
  {"exists",       {do_exists,        2, -1}},
  {"type",         {do_type,          2,  2}},
  {"rename",       {do_rename,        3,  3}},
  {"renamenx",     {do_renamenx,      3,  3}},
  {"touch",        {do_touch,         2, -1}},
  {"unlink",       {do_asyncdel,      2,  2}},
  {"keys",         {do_keys,          1,  1}},
  {"scan",         {do_scan,          2, -1}},
  {"randomkey",    {do_randomkey,     1,  1}},
  {"dbsize",       {do_dbsize,        1,  1}},
  {"flushall",     {do_flushall,      1,  1}},
  {"flushdb",      {do_flushall,      1,  1}},
  // ttl
  {"expire",       {do_expire,        3,  3}},
  {"pexpire",      {do_pexpire,       3,  3}},
  {"expireat",     {do_expireat,      3,  3}},
  {"pexpireat",    {do_pexpireat,     3,  3}},
  {"ttl",          {do_ttl_seconds,   2,  2}},
  {"pttl",         {do_ttl,           2,  2}},
  {"persist",      {do_persist,       2,  2}},
  // sorted set
  {"zadd",         {do_zadd,          4,  4}},
  {"zrem",         {do_zrem,          3,  3}},
  {"zscore",       {do_zscore,        3,  3}},
  {"zrank",        {do_zrank,         3,  3}},
  {"zquery",       {do_zquery,        6,  6}},
  {"zrevquery",    {do_zquery_reversed, 6, 6}},
  // list
  {"lpush",        {do_lpush,         3, -1}},
  {"rpush",        {do_rpush,         3, -1}},
  {"lpop",         {do_lpop,          2,  2}},
  {"rpop",         {do_rpop,          2,  2}},
  {"llen",         {do_llen,          2,  2}},
  {"lindex",       {do_lindex,        3,  3}},
  {"lrange",       {do_lrange,        4,  4}},
  {"lset",         {do_lset,          4,  4}},
  {"linsert",      {do_linsert,       5,  5}},
  {"lrem",         {do_lrem,          4,  4}},
  {"ltrim",        {do_ltrim,         4,  4}},
  // hash
  {"hset",         {do_hset,          4, -1}},
  {"hget",         {do_hget,          3,  3}},
  {"hdel",         {do_hdel,          3, -1}},
  {"hexists",      {do_hexists,       3,  3}},
  {"hlen",         {do_hlen,          2,  2}},
  {"hgetall",      {do_hgetall,       2,  2}},
  {"hkeys",        {do_hkeys,         2,  2}},
  {"hvals",        {do_hvals,         2,  2}},
  {"hmget",        {do_hmget,         3, -1}},
  {"hsetnx",       {do_hsetnx,        4,  4}},
  {"hincrby",      {do_hincrby,       4,  4}},
  {"hstrlen",      {do_hstrlen,       3,  3}},
  {"hscan",        {do_hscan,         3, -1}},
  // set
  {"sadd",         {do_sadd,          3, -1}},
  {"srem",         {do_srem,          3, -1}},
  {"sismember",    {do_sismember,     3,  3}},
  {"smismember",   {do_smismember,    3, -1}},
  {"scard",        {do_scard,         2,  2}},
  {"smembers",     {do_smembers,      2,  2}},
  {"spop",         {do_spop,          2,  3}},
  {"srandmember",  {do_srandmember,   2,  3}},
  {"sscan",        {do_sscan,         3, -1}},
  {"sinter",       {do_sinter,        2, -1}},
  {"sunion",       {do_sunion,        2, -1}},
  {"sdiff",        {do_sdiff,         2, -1}},
  {"sinterstore",  {do_sinterstore,   3, -1}},
  {"sunionstore",  {do_sunionstore,   3, -1}},
  {"sdiffstore",   {do_sdiffstore,    3, -1}},
  {"smove",        {do_smove,         4,  4}},
  // server
  {"info",         {do_info,          1,  1}},
  {"save",         {do_save,          1,  1}},
  {"bgsave",       {do_bgsave,        1,  1}},
};

void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn) {
  if (cmd.empty()) {
    return resp_err(out, "ERR empty command");
  }

  // command names are case-insesitve
  for (char &ch : cmd[0]){
    ch = (char)tolower((unsigned char)ch);
  }

  // AUTH always allowed
  if (cmd[0] == "auth") {
    return do_auth(cmd, out, conn);
  }

  // check authentication
  if (!conn->authenticaded) {
    return resp_err(out, "NOAUTH authentication required");
  }

  auto it = k_cmd_table.find(cmd[0]);
  if (it == k_cmd_table.end()){
    return resp_err(out, "ERR unknown command");
  }

  const CmdSpec &spec = it->second;
  int argc = (int)cmd.size();
  if (argc < spec.min_args || (spec.max_args != -1 && argc > spec.max_args)){
    return resp_err(out, "ERR wrong number of arguments");
  }
  spec.fn(cmd, out);
}