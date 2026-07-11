#include "state.h"
#include "common.h"
#include "hash.h"
#include "sha256.h"
#include <arpa/inet.h>  
#include <time.h>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <cstdio>

GlobalData g_data;
Config g_config;

// forward declarations: defined lower in this file, used by entry_set_ttl
static void heap_delete(std::vector<HeapItem> &a, size_t pos);
static void heap_upsert(std::vector<HeapItem> &a, size_t pos, HeapItem t);

// Build/refresh the built in default user from the current requirepass digest
void acl_bootstrap_default(){
  User &u = g_config.users["default"]; // operator [] insearts if absent; address stays stable
  u.name ="default";
  u.enable = true;
  u.allow_cats = CAT_ALL;
  u.all_keys = true;
  u.key_patterns.clear();
  u.cmd_overrides.clear();
  u.pw_hashes.clear();
  if (!g_config.password.empty()){
    u.pw_hashes.push_back(g_config.password);
  }
}

// Identity assigned to a connection *before* any AUTH
// Returns default only when it needs no password (nopass) and is enable -> auto.auth
User *acl_initial_user(){
  // we find not [], never fabricate a new user
  auto it = g_config.users.find("default");
  if (it == g_config.users.end()){ return nullptr; }
  User &def = it->second;
  return (def.enable && def.pw_hashes.empty() ? &def : nullptr);
}

bool parse_memory_size(const std::string &s, size_t *out){
  if (s.empty()){ return false; }
  size_t i = 0;
  while (i < s.size() && isdigit((unsigned char)s[i])) { ++i; }
  // must start with digits
  if (i== 0) { return false; }
  unsigned long long num = strtoull(s.c_str(), nullptr, 10);

  std::string unit = s.substr(i);
  for (char &c : unit){ c = (char)tolower((unsigned char)c); }

  unsigned long long mult = 0;
  if      (unit == ""  || unit == "b") { mult = 1ULL; }
  else if (unit == "k")  { mult = 1000ULL; }
  else if (unit == "kb") { mult = 1024ULL; }
  else if (unit == "m")  { mult = 1000ULL * 1000; }
  else if (unit == "mb") { mult = 1024ULL * 1024; }
  else if (unit == "g")  { mult = 1000ULL * 1000 * 1000; }
  else if (unit == "gb") { mult = 1024ULL * 1024 * 1024; }
  else { return false; }
  if (mult != 0 && num > ULLONG_MAX / mult){ return false; }
  *out = (size_t)(num * mult);
  return true;
}

bool parse_maxmemory_policy(const std::string &s, MaxmemoryPolicy *out){
  std::string p = s;
  for (char &c : p){ c = (char)tolower((unsigned char)c); }
  if      (p == "noeviction")      { *out = MaxmemoryPolicy::NOEVICTION; }
  else if (p == "allkeys-lru")     { *out = MaxmemoryPolicy::ALLKEYS_LRU; }
  else if (p == "allkeys-lfu")     { *out = MaxmemoryPolicy::ALLKEYS_LFU; }
  else if (p == "allkeys-random")  { *out = MaxmemoryPolicy::ALLKEYS_RANDOM; }
  else if (p == "volatile-lru")    { *out = MaxmemoryPolicy::VOLATILE_LRU; }
  else if (p == "volatile-lfu")    { *out = MaxmemoryPolicy::VOLATILE_LFU; }
  else if (p == "volatile-random") { *out = MaxmemoryPolicy::VOLATILE_RANDOM; }
  else if (p == "volatile-ttl")    { *out = MaxmemoryPolicy::VOLATILE_TTL; }
  else { return false; }
  return true;
}
// Tokenizer for the config file
static std::vector<std::string> config_tokenize(const char *line){
  std::vector<std::string> out;
  for (size_t i = 0; line[i]; ){
    while (line[i] && isspace((unsigned char)line[i])){ ++i; }
    // a comment -> #
    if (!line[i] || line[i] == '#'){ break; } 
    std::string tok;
    // quoted
    if (line[i] == '"'){
      for (i++; line[i] && line[i] != '"'; ){
        if (line[i] == '\\' && line[i+1]){ tok += line[i+1]; i += 2; }
        else { tok += line[i++]; }
      }
      if (line[i] == '"'){ ++i; }
    } else {
      while (line[i] && !isspace((unsigned char)line[i])){ tok += line[i++]; }
    }
    out.push_back(std::move(tok));
  }
  return out;
}

CfgResult config_apply(const std::string &name_in, const std::vector<std::string> &args, std::string &err){
  std::string name = name_in;
  for (char &c : name){ c = (char)tolower((unsigned char)c); }

  // Lambda function for checking number of args
  auto need1 = [&](void) -> bool { if (args.size() != 1){ 
      err = "wrong number of args for '" + name + "'";
      return false; 
    } 
    return true; 
  };

  // auth - network
  if (name == "requirepass"){ 
    if (!need1()){ return CfgResult::BADVALUE; }
    // no auth 
    if (args[0].empty()){ 
      g_config.password.clear(); 
      
    } else if (args[0].size() == 65 && args[0][0] == '#'){
      g_config.password = args[0].substr(1);
    } else {
      // hash plaintext
      g_config.password = sha256_hex(args[0]);
    }
    auto du = g_config.users.find("default");
    if (du != g_config.users.end()){
      du->second.pw_hashes.clear();
      if (!g_config.password.empty()){ du->second.pw_hashes.push_back(g_config.password); }
    }
    return CfgResult::OK;
  }

  if (name == "rename-command"){
    if (args.size() != 2){ err = "rename-command needs OLD and NEW"; return CfgResult::BADVALUE; }
    std::string oldn = args[0], neu = args[1];

    for (char &c : oldn){ c = (char)tolower((unsigned char)c); }
    for (char &c : neu){ c = (char)tolower((unsigned char)c); }

    if (oldn.empty()){ err = "rename_command OLD is empty"; return CfgResult::BADVALUE; }
    if (!command_is_known(oldn)){ err = "rename-command: unknown command '" + oldn + "'"; return CfgResult::BADVALUE; } 
    for (auto &r : g_config.renames){ if (r.first == oldn){ err = "rename-command: '" + oldn + "' renamed twice"; return CfgResult::BADVALUE; } }

    if (!neu.empty()){
      for (unsigned char c : neu){ if (c < 0x20 || c == 0x7f){ err = "rename-command: NEW has control chars"; return CfgResult::BADVALUE; } }
      if (command_is_known(neu)){ err = "rename-command: NEW '" + neu + "' collides with a command"; return CfgResult::BADVALUE; }
      for (auto &r : g_config.renames){ if (r.second == neu){ err = "rename-command: New '" + neu + "' already use"; return CfgResult::BADVALUE; } }      
    } else if (oldn == "auth" && !g_config.password.empty()){
      err = "refusing to disable AUTH while a password is set (would lock out clients)";
      return CfgResult::BADVALUE;
    }
    g_config.renames.emplace_back(oldn, neu);
    return CfgResult::OK;
  }

  // ACL user definition 
  if (name == "user"){
    if (args.empty()){ err = "User directive requires a username"; return CfgResult::BADVALUE; }
    // Create or update
    User &u = g_config.users[args[0]]; 
    if (u.name.empty()){ u.name = args[0]; }
    for (size_t i = 1; i < args.size(); ++i){
      if (!acl_apply_rule(u, args[i])){ 
        err = "Invalid ACL rule '" + args[i] + "' for user '" + args[0] + "'";
        return CfgResult::BADVALUE;
      }
    }
    // a reset token wipes name mid-line
    if (u.name.empty()){ u.name = args[0]; }
    return CfgResult::OK;
  }

  if (name == "port"){
    if (!need1()){ return CfgResult::BADVALUE; }
    int p = atoi(args[0].c_str());
    if (p < 1 || p > 65535){
      err = "invalid port";
      return CfgResult::BADVALUE;
    }
    g_config.port = p;
    return CfgResult::OK;
  }

  if (name == "bind"){
    if (args.empty()){ err = "bind needs at least one address"; return CfgResult::BADVALUE; }
    g_config.binds = args;
    return CfgResult::OK;
  }

  if (name == "protected-mode"){
    if(!need1()){ return CfgResult::BADVALUE; }
    g_config.protected_mode = (args[0] == "yes");
    return CfgResult::OK;
  }

  if (name == "allow-ip"){ 
    if (!need1()){ return CfgResult::BADVALUE; }
    uint32_t net, mask;
    if (!parse_cidr(args[0], &net, &mask)){
      err = "invalid CIDR '" + args[0] + "'";
      return CfgResult::BADVALUE;
    }
    g_config.allowlist.push_back({ net, mask });
    return CfgResult::OK;
  }

  // memory / eviction
  if (name == "maxmemory"){
    if (!need1()){ return CfgResult::BADVALUE; }
    size_t b = 0;
    if (!parse_memory_size(args[0], &b)){
      err = "invalid maxmemory";
      return CfgResult::BADVALUE;
    }
    g_config.maxmemory = b;
    return CfgResult::OK;
  }

  if (name == "maxmemory-policy"){
    if (!need1()){ return CfgResult::BADVALUE; }
    MaxmemoryPolicy pol;
    if (!parse_maxmemory_policy(args[0], &pol)){
      err = "invalid policy";
      return CfgResult::BADVALUE;
    }
    g_config.maxmemory_policy = pol;
    return CfgResult::OK;
  }

  if (name == "maxmemory-samples"){
    if (!need1()){ return CfgResult::BADVALUE; }
    int n = atoi(args[0].c_str());
    if (n < 1){
      err = "invalid samples";
      return CfgResult::BADVALUE;
    }
    g_config.maxmemory_samples = n;
    return CfgResult::OK;
  }

  // AOF persistence
  if (name == "appendonly"){
    if (!need1()){ return CfgResult::BADVALUE; }
    g_config.aof_enable = (args[0] == "yes");
    return CfgResult::OK;
  }

  if (name == "appendfsync"){
    if (!need1()){ return CfgResult::BADVALUE; }
    if (args[0] == "always"){
      g_config.aof_fysnc = Aoffsync::ALWAYS;
    } else if (args[0] == "no"){
      g_config.aof_fysnc = Aoffsync::NO;
    } else if (args[0] == "everysec"){
      g_config.aof_fysnc = Aoffsync::EVERYSEC;
    } else {
      err = "invalid appendfsync";
      return CfgResult::BADVALUE;
    }
    return CfgResult::OK;
  }

  if (name == "appendfilename"){
    if (!need1()){ return CfgResult::BADVALUE; }
    g_config.aof_path = args[0];
    return CfgResult::OK;
  }

  if (name == "auto_aof_rewrite-percentage"){
    if (!need1()){ return CfgResult::BADVALUE; }
    int n = atoi(args[0].c_str());
    if (n < 0){
      err = "invalid";
      return CfgResult::BADVALUE;
    }
    g_config.aof_rewrite_perc = n;
    return CfgResult::OK;
  }

  if (name == "auto_aof_rewrite-min-size"){
    if (!need1()){ return CfgResult::BADVALUE; }
    size_t b = 0;
    if (!parse_memory_size(args[0], &b)){
      err = "invalid";
      return CfgResult::BADVALUE;
    }
    g_config.aof_rewrite_min_size = b;
    return CfgResult::OK;
  }

  // RDB persistence
  if (name == "dbfilename"){
    if (!need1()){ return CfgResult::BADVALUE; }
    g_config.dump_path = args[0];
    return CfgResult::OK;
  }

  if (name == "save"){
    // save '' -> disable all save conditions 
    if (args.size() == 1 && args[0].empty()){
      g_config.save_conditions.clear();
      return CfgResult::OK;
    }
    if (args.size() != 2){
      err = "save needs <seconds> <changes>";
      return CfgResult::BADVALUE;
    }
    g_config.save_conditions.push_back({
      strtoull(args[0].c_str(), nullptr, 10),
      (uint32_t)strtoul(args[1].c_str(), nullptr, 10)
    });
    return CfgResult::OK;
  }

  if (name == "auditlog"){ 
    // "" disables, "stderr", or a path
    if (!need1()){ return CfgResult::BADVALUE; }
    g_config.auditlog_path = args[0];
    audit_open(g_config.auditlog_path); // opens the file-load and on config set
    return CfgResult::OK;
  }

  // Unknown directive
  err = "unknown directive '" + name + "'";
  return CfgResult::UNKNOWN;

}

bool config_load_file(const char *path){
  FILE *fp = fopen(path, "r");
  if (!fp){ fprintf(stderr, "fatal: cannot open config %s: %s\n", path, strerror(errno)); return false; }
  char line[1024];
  int lineno = 0; bool ok = true, cleared_save = false;
  while (fgets(line, sizeof(line), fp)){
    lineno++;
    std::vector<std::string> t = config_tokenize(line);
    // detected a blank or comment
    if (t.empty()){ continue; } 
    std::string name = t[0];
    std::vector<std::string> args(t.begin() + 1, t.end());
    std::string ln = name; 
    for (char &c : ln){ c = (char)tolower((unsigned char)c); }
    if (ln == "save" && !cleared_save){ g_config.save_conditions.clear(); cleared_save = true; }
    std::string err;
    if (config_apply(name, args, err) != CfgResult::OK){
      fprintf(stderr, "config %s:%d: %s\n", path, lineno, err.c_str());
      ok = false;
    }
  }
  fclose(fp);
  g_config.config_path = path;
  return ok;
}

// serialize live config back
bool config_rewrite(const char * path){
  FILE *fp = fopen(path ,"w");
  if (!fp){ return false; }
  fprintf(fp, "port %d\n", g_config.port);
  if (!g_config.password.empty()){ fprintf(fp, "requirepass \"#%s\"\n", g_config.password.c_str()); }
  fprintf(fp, "dbfilename %s\n", g_config.dump_path.c_str());
  fprintf(fp, "appendonly %s\n", g_config.aof_enable ? "yes" : "no");
  fprintf(fp, "appendfilename %s\n", g_config.aof_path.c_str());
  fprintf(fp, "appendfsync %s\n",  g_config.aof_fysnc == Aoffsync::ALWAYS ? "always" 
                                : g_config.aof_fysnc == Aoffsync::NO     ? "no" : "everysec");
  fprintf(fp, "maxmemory %zu\n", g_config.maxmemory);
  fprintf(fp, "maxmemory-policy %s\n", maxmemory_policy_name(g_config.maxmemory_policy));
  fprintf(fp, "maxmemory-samples %d\n", g_config.maxmemory_samples);
  for (const SaveCondition &s : g_config.save_conditions){
    fprintf(fp, "save %llu %u\n", (unsigned long long)s.seconds, s.changes);
  }
  // ACL users - skip 'default': it is rebuilt from requirepass + acl_bootstrap_default() at boot
  for (const auto &kv : g_config.users){
    if (kv.first == "default"){ continue; }
    std::string line = acl_format_user(kv.first, kv.second, true);      // real hashes, quoted
    fprintf(fp, "%s\n", line.c_str());
  }
  
  for (const auto &r : g_config.renames){
    if (r.second.empty()){ fprintf(fp, "rename-command %s \"\"\n", r.first.c_str()); }
    else { fprintf(fp, "rename-command %s %s\n", r.first.c_str(), r.second.c_str()); }
  }

  if (!g_config.auditlog_path.empty()){ fprintf(fp, "auditlog \"%s\"\n", g_config.auditlog_path.c_str()); }
  fclose(fp);
  return true;
}

const char *maxmemory_policy_name(MaxmemoryPolicy p){
    switch (p){
    case MaxmemoryPolicy::NOEVICTION:      return "noeviction";
    case MaxmemoryPolicy::ALLKEYS_LRU:     return "allkeys-lru";
    case MaxmemoryPolicy::ALLKEYS_LFU:     return "allkeys-lfu";
    case MaxmemoryPolicy::ALLKEYS_RANDOM:  return "allkeys-random";
    case MaxmemoryPolicy::VOLATILE_LRU:    return "volatile-lru";
    case MaxmemoryPolicy::VOLATILE_LFU:    return "volatile-lfu";
    case MaxmemoryPolicy::VOLATILE_RANDOM: return "volatile-random";
    case MaxmemoryPolicy::VOLATILE_TTL:    return "volatile-ttl";
  }
  return "noeviction";
}

// Per-type element accumalators
static bool cb_mem_hash(HNode *node, void *arg){
  HashNode *hn = container_of(node, &HashNode::node);
  // node + 1 bucket slot
  *(size_t *)arg += sizeof(HashNode) + sizeof(HNode *) + hn->field.capacity() + hn->value.capacity();
  return true;
}

static bool cb_mem_set(HNode *node, void *arg){
  SetNode *sn = container_of(node, &SetNode::node);
  *(size_t *)arg += sizeof(SetNode) + sizeof(HNode *) + sn->member.capacity();
  return true;
}

static bool cb_mem_zset(HNode *node, void *arg){
  ZNode *zn = container_of(node, &ZNode::hmap);
  // ZNode is malloc'd as sizeof(ZNode)+len (name[0] flexible array); + 1 bucket slot
  *(size_t *)arg += sizeof(ZNode) + zn->len + sizeof(HNode *);
  return true;
}

// Approximate byte cost of one entry (key + value). Kinda cheap, walks aggregates once
size_t entry_mem_usage(Entry *ent){
  size_t n = sizeof(Entry) + ent->key.capacity();
  switch (ent->type){
    case T_STR:
      n += entry_str(ent).capacity();
      break;
    case T_DLIST: {
      Deque &d = entry_deque(ent);
      // the ring buffer itself 
      n += d.cap * sizeof(std::string);
      for (size_t i = 0; i < d.count; ++i){
        // live element bytes
        n += deque_get(&d, i)->capacity();
      }
      break;
    }
    case T_HASH: hm_foreach(&entry_hash(ent), cb_mem_hash, &n); break;
    case T_SET: hm_foreach(&entry_set(ent), cb_mem_set, &n); break;
    case T_ZSET: hm_foreach(&entry_zset(ent).hmap, cb_mem_zset, &n); break;
    default: break;
  }
  return n;
}

struct MemSampleCtx { 
  size_t left;
  size_t sum;
  size_t counted;
  uint32_t type;
};

static bool cb_mem_sample(HNode *node, void *arg){
  MemSampleCtx *c = (MemSampleCtx *)arg;
  switch (c->type){
    case T_HASH: { HashNode *hn = container_of(node, &HashNode::node);
      c->sum += sizeof(HashNode) + sizeof(HNode *) + hn->field.capacity() + hn->value.capacity(); break; }
    case T_SET: { SetNode *sn = container_of(node, &SetNode::node);
      c->sum += sizeof(SetNode) + sizeof(HNode *) + sn->member.capacity(); break; }
    case T_ZSET: { ZNode *zn = container_of(node, &ZNode::hmap); 
      c->sum += sizeof(ZNode) + zn->len + sizeof(HNode *); break; }
  }
  c->counted++;
  // stop once we've sampled 'samples' nodes
  return --c->left > 0;
}

size_t entry_mem_usage_sampled(Entry *ent, size_t samples){
  if (samples == 0){ return entry_mem_usage(ent); }
  size_t base = sizeof(Entry) + ent->key.capacity();
  switch (ent->type){
    case T_STR:
      // single value, no sampling
      return base + entry_str(ent).capacity();
    case T_DLIST: {
      Deque &d = entry_deque(ent);
      size_t n = base + d.cap * sizeof(std::string);
      if (d.count == 0){ return n; }
      size_t k = samples < d.count ? samples : d.count, sum = 0;
      for (size_t i = 0; i < k; ++i){ sum += deque_get(&d, i)->capacity(); }
      // extrapolate
      return n + (size_t)((double)sum / (double)k * (double)d.count);
    }
    case T_HASH: case T_SET: case T_ZSET: {
      HMap *m = ent->type == T_HASH ? &entry_hash(ent)
              : ent->type == T_SET ?  &entry_set(ent)
              :                       &entry_zset(ent).hmap;
      size_t total = hm_size(m);
      if (total == 0){ return base; }
      MemSampleCtx c{ samples, 0, 0, ent->type };
      hm_foreach(m, cb_mem_sample, &c);
      if (c.counted == 0){ return base; }
      return base + (size_t)((double)c.sum / (double)c.counted * (double)total);
    }
  }
  return base;
}

// Recompute this entry size and fold the delta into the global counter.
// Add-new-before-subtract-old keeps used_memory from the ever underflowing,
// because the invariant guarantees used_memory >= ent->mem.
void mem_reaccount(Entry *ent){
  size_t now = entry_mem_usage(ent);
  g_data.used_memory += now;
  g_data.used_memory -= ent->mem;
  ent->mem = now; 
}

#ifndef NDEBUG
static bool cb_mem_sum(HNode *node, void *arg){
  Entry *e = container_of(node, &Entry::node);
  *(size_t *)arg += entry_mem_usage(e);
  return true;
}
void mem_selfcheck(const char *where){
  size_t sweep = 0;
  hm_foreach(&g_data.db, cb_mem_sum, &sweep);
  if (sweep != g_data.used_memory){
    fprintf(stderr, "[mem] drift at %s: counter=%zu sweep=%zu delta=%zd)\n",
            where, g_data.used_memory, sweep,
            (ssize_t)sweep - (ssize_t)g_data.used_memory);
  }
}
#endif

bool parse_cidr(const std::string &s, uint32_t *net, uint32_t *mask){
  std::string ip = s;
  int bits = 32;
  size_t slash = s.find('/');
  if (slash != std::string::npos){
    ip = s.substr(0, slash);
    const char *bits_str = s.c_str() + slash + 1;
    char *end = nullptr;
    long parsed = strtol(bits_str, &end, 10);
    // no digits, or trailing garbage
    if (end == bits_str || *end != '\0'){ return false; }
    if (bits < 0 || bits > 32){ return false; }
    bits = (int)parsed;
  }
  struct in_addr a;
  if (inet_pton(AF_INET, ip.c_str(), &a) != 1){ return false; }
  uint32_t m = (bits == 0) ? 0u : (0xFFFFFFFFu << (32 - bits));
  // store netwwork + mask, host byte order
  *net = ntohl(a.s_addr) & m;
  *mask = m;
  return true;
}

bool ip_is_loopback(uint32_t peer_host){
  return (peer_host & 0xFF000000u) == 0x7F000000u; // 127.0.0.0/8
}

bool ip_allowed(uint32_t peer_host){
  if (ip_is_loopback(peer_host)){ return true; }
  if (g_config.allowlist.empty()){ return true; }
  for (const auto &e : g_config.allowlist){
    if ((peer_host & e.second) == e.first){ return true; }
  }
  return false;
}

// because CLOCK_MONOTONIC resets on reboot. in-memory timers stay monotonic.
uint64_t get_monotonic_msec(){
  struct timespec tv = {0,0};
  clock_gettime(CLOCK_MONOTONIC, &tv);
  return uint64_t(tv.tv_sec) * 1000 + tv.tv_nsec / 1000 / 1000;
}

// wall-clock time (CLOCK_REALTIME). used ONLY for persisting TTLs to disk,
uint64_t get_wall_msec(){
  struct timespec tv = {0,0};
  clock_gettime(CLOCK_REALTIME, &tv);
  return uint64_t(tv.tv_sec) * 1000 + tv.tv_nsec / 1000 / 1000;
}

//equality comparison for the top level hash table
bool entry_eq(HNode *node, HNode *key){
  Entry *ent = container_of(node, &Entry::node);
  LookupKey *keydata = container_of(key, &LookupKey::node);
  return ent->key == keydata->key;
}

Entry *entry_new(uint32_t type) {
  Entry *ent = new Entry();
  ent->type = type;
  switch (type){
    case T_STR: ent->val = std::string{}; break;
    case T_ZSET: ent->val = ZSet{}; break;
    case T_DLIST: ent->val = Deque{}; break;
    case T_HASH: ent->val = EntryHash{}; break;
    case T_SET: ent->val = EntrySet{}; break;
  }
  return ent;
}

// set or remove the TTL
void entry_set_ttl(Entry *ent, int64_t ttl_ms){
  if (ttl_ms < 0 && entry_has_ttl(ent)){
    // negative ttl -> remove ttl
    heap_delete(g_data.heap, ent->heap_idx);
    ent->heap_idx = NO_TTL;
  } else if (ttl_ms >= 0){
    // we add or update the data structure
    uint64_t expire_at = get_monotonic_msec() + (uint64_t)ttl_ms;
    HeapItem item = {expire_at, &ent->heap_idx};
    heap_upsert(g_data.heap, ent->heap_idx, item);
  }
}

// When and where to delete
void entry_del(Entry *ent){ 
  // discharge on the main thread (before async free)
  g_data.used_memory -= ent->mem;
  // remove from the heap first
  entry_set_ttl(ent, -1);
  // decide if use thread pool or synchronous
  size_t set_size = 0;
  switch(ent->type){
    case T_ZSET: set_size = hm_size(&entry_zset(ent).hmap); break;
    case T_SET: set_size = hm_size(&entry_set(ent)); break;
    case T_HASH: set_size = hm_size(&entry_hash(ent)); break;
    case T_DLIST: set_size = entry_deque(ent).count; break;
    default: break;
  }
  constexpr size_t k_large_container_size = 1000;
  if (set_size > k_large_container_size){
    thread_pool_queue(&g_data.thread_pool, &entry_del_func, ent);
  } else {
    entry_del_sync(ent);
  }
}

// Delete the actual work
void entry_del_sync(Entry *ent){
  switch(ent->type){
    case T_ZSET: zset_clear(&entry_zset(ent)); break;
    case T_SET: set_clear(&entry_set(ent)); break;
    case T_HASH: hash_clear(&entry_hash(ent)); break;
    case T_DLIST: deque_free(&entry_deque(ent)); break;
    default: break;
  }
  entry_set_ttl(ent, -1);
  delete ent;
}


// a wrapper function for the thread pool
void entry_del_func(void *arg){
  entry_del_sync((Entry *)arg);
}

// we use the duplicate trick
// Before:  [1, 3, 2, 7, 5]   delete pos=1 (value 3)
// Step 1:  [1, 5, 2, 7, 5]   overwrite pos=1 with last (5)
// Step 2:  [1, 5, 2, 7]      pop_back removes duplicate
// Step 3:  [1, 5, 2, 7]      heap_update fixes 5 into correct position
static void heap_delete(std::vector<HeapItem> &a, size_t pos){
  // swap the erased item with the last item
  a[pos] = a.back();
  a.pop_back();
  // we update the swapped item
  if (pos < a.size()){
    heap_update(a.data(), pos, a.size());
  } 
}

// update or append at the front
static void heap_upsert(std::vector<HeapItem> &a, size_t pos, HeapItem t){
  if (pos < a.size()){
    a[pos] = t; // update 
  } else {
    pos = a.size();
    a.push_back(t); // add a new item
  }
  heap_update(a.data(), pos, a.size());
}

bool hnode_same(HNode *node, HNode *key){
  return node == key;
}

// lazy expiration: if the entry's TTL has already passed, delete it and
// report true so the caller can treat the key as missing. no TTL or not yet
// expired -> false (entry stays).
bool expire_if_needed(Entry *ent){
  if (!entry_has_ttl(ent)) { return false; }             // no TTL
  if (g_data.heap[ent->heap_idx].val > get_monotonic_msec()) {    // not expired yet
    return false;
  }
  hm_delete(&g_data.db, &ent->node, &hnode_same);
  entry_del(ent);
  return true;
}
