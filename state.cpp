#include "state.h"
#include "common.h"
#include "hash.h"
// #include "sha256.h"
#include "cred.h"
#include "transport.h"
#include "unistd.h"
#include <arpa/inet.h>  
#include <climits>
#include <cstddef>
#include <time.h>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <cerrno>
#include <utility>
#include <vector>
#include <algorithm>

GlobalData g_data;
Config g_config;

// forward declarations: defined lower in this file, used by entry_set_ttl
static void heap_delete(std::vector<HeapItem> &a, size_t pos);
static void heap_upsert(std::vector<HeapItem> &a, size_t pos, HeapItem t);

// whole-string decimal integer, no trailing garbage, no silent overflow
bool parse_int_strict(const char *s, long *out){
  if (!s || !*s){ return false; }
  char *end = nullptr;
  errno = 0;
  long v =  strtol(s, &end, 10);
  if (errno == ERANGE || end == s || *end != '\0'){ return false; }
  *out = v;
  return true;
}

bool parse_bool_strict(const std::string &s, bool *out){
  if (s == "yes") { *out = true; return true; }
  if (s == "no") { *out = false; return true; }
  return false;
}


// Build/refresh the built in default user from the current requirepass digest
void acl_bootstrap_default(){
  auto it = g_config.users.find("default");
  if (it != g_config.users.end()){
    // config defined 'user default'
    if (it->second.pw_hashes.empty() && !g_config.password.empty()){
      it->second.pw_hashes.push_back(g_config.password);
    }
    return;
  }
  User &u = g_config.users["default"]; // operator [] insearts if absent; address stays stable
  u.name ="default";
  u.enable = true;
  u.allow_cats = CAT_ALL;
  u.all_keys = true;
  u.all_channels = true;
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

bool parse_notify_flags(const std::string &s, int *out){
  int f = 0;
  for (char c : s){
    switch (c){
      case 'K': f |= NOTIFY_KEYSPACE; break;  case 'E': f |= NOTIFY_KEYEVENT; break;
      case 'g': f |= NOTIFY_GENERIC;  break;  case '$': f |= NOTIFY_STRING;   break;
      case 'l': f |= NOTIFY_LIST;     break;  case 's': f |= NOTIFY_SET;      break;
      case 'h': f |= NOTIFY_HASH;     break;  case 'z': f |= NOTIFY_ZSET;     break;
      case 'x': f |= NOTIFY_EXPIRED;  break;  case 'e': f |= NOTIFY_EVICTED;  break;
      case 'A': f |= NOTIFY_ALL;      break;
      default: return false; // unknown flag char
    }
  }
  *out = f; // "" -> 0 -> disable
  return true;
}

std::string notify_flags_string(int f){
  std::string s;
  if ((f & NOTIFY_ALL) == NOTIFY_ALL){ s += 'A'; }
  else {
    if (f & NOTIFY_GENERIC){ s += 'g'; }  if (f & NOTIFY_STRING){ s += '$'; }
    if (f & NOTIFY_LIST){ s += 'l'; }     if (f & NOTIFY_SET){ s += 's'; }
    if (f & NOTIFY_HASH){ s += 'h'; }     if (f & NOTIFY_ZSET){ s += 'z'; }
    if (f & NOTIFY_EXPIRED){ s += 'x'; }  if (f & NOTIFY_EVICTED){ s += 'e'; }
  }
  if (f & NOTIFY_KEYSPACE){ s += 'K'; }   if (f & NOTIFY_KEYEVENT){ s += 'E'; }
  return s; // class flags first, then K/E - same ordering as REDIS
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

// One row per directive, owning its name, arity, parse/assign and CONFIG GET
// form. The dispatcher checks arity before calling apply(), so apply() may index
// args freely — that is why the per-branch need1() calls disappear.
struct ConfigDirective {
  const char * name;
  int min_args; // inclusive
  int max_args; // inlucisve. -1 = unbounded
  CfgResult (*apply)(const std::vector<std::string> &args, std::string &err);
  bool (*get)(std::string &out);
  bool boot_only; // Cconfig set refuses it 
  bool masked; // get() answers a placeholder, never the stored value 
  void (*emit) (FILE *fp);
};

// tls-cert-file / tls-key-file are the only TLS directive that may change in runtime
static CfgResult tls_material_set(std::string &field, const std::string &val, std::string &e){
  std::string prev = field;
  field = val;
  if (!tr_tls_reload(e)){
    field = prev;
    return CfgResult::BADVALUE;
  }
  return CfgResult::OK;
}

static const ConfigDirective k_config_table[] = {
  { "port", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long p = 0;
      if (!parse_int_strict(a[0].c_str(), &p) || p < 1 || p > 65535){
        e = "invalid port"; return CfgResult::BADVALUE;
      }
      g_config.port = (int)p; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.port); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "maxclients", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long n = 0;
      if (!parse_int_strict(a[0].c_str(), &n) || n < 1 || n > 1000000){
        e = "invalid maxclients (1-1000000)"; return CfgResult::BADVALUE;
      }
      g_config.maxclients = (int)n; return CfgResult::OK;
     },
    [](std::string &o) -> bool { o = std::to_string(g_config.maxclients); return true; },
    // maxclients
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (g_config.maxclients != 10000){
        fprintf(fp, "maxclients %d\n", g_config.maxclients);
      }
  } },

  { "protected-mode", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      bool b = false;
      if (!parse_bool_strict(a[0], &b)){ e = "expected yes/no"; return CfgResult::BADVALUE; }
      g_config.protected_mode = b; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.protected_mode ? "yes" : "no"; return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "bind", 1, -1,
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.binds = a; return CfgResult::OK;   // assigns; arity guarantees non-empty
    },
    [](std::string &o) -> bool {
      o.clear();
      for (const std::string &b : g_config.binds){ if (!o.empty()){ o += ' '; } o += b; }
      return true;
    },
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (g_config.binds.empty()){ return; }
      fprintf(fp, "bind");
      for (const std::string &b : g_config.binds){ fprintf(fp, " %s", b.c_str()); }
      fprintf(fp, "\n");
    } },

  { "allow-ip", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      uint32_t net, mask;
      if (!parse_cidr(a[0], &net, &mask)){ e = "invalid CIDR '" + a[0] + "'"; return CfgResult::BADVALUE; }
      g_config.allowlist.push_back({ net, mask });
      return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      char b[64];
      o.clear();
      for (const auto &a : g_config.allowlist){
        struct in_addr ia;
        ia.s_addr = htonl(a.first);
        if (!o.empty()){ o += ' '; }
        o += inet_ntoa(ia);
        snprintf(b, sizeof(b), "/%d", __builtin_popcount(a.second));
        o += b;
      }
      return true;
    },
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      for (const auto &a : g_config.allowlist){
        struct in_addr ia;
        ia.s_addr = htonl(a.first);
        fprintf(fp, "allow-ip %s/%d\n", inet_ntoa(ia), __builtin_popcount(a.second));
      }
    } },
    
  { "requirepass", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      if (a[0].empty()){
        g_config.password.clear();
      } else if (a[0].size() == 65 && a[0][0] == '#'){
        g_config.password = a[0].substr(1);
      } else if (a[0].rfind("$argon2id$", 0) == 0){
        g_config.password = a[0];
      } else {
        g_config.password = cred_hash_new(a[0]);   // plaintext: current policy
        if (g_config.password.empty()){ e = "password hashing failed"; return CfgResult::BADVALUE; }
      }
      // default's hash list mirrors requirepass. acl_bootstrap_default() rebuilds
      // it at boot; this keeps them in step on a live CONFIG SET too.
      auto du = g_config.users.find("default");
      if (du != g_config.users.end()){
        du->second.pw_hashes.clear();
        if (!g_config.password.empty()){ du->second.pw_hashes.push_back(g_config.password); }
      }
      return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.password.empty() ? "" : "<set>"; return true; },
    /*boot_only*/ false, /*masked*/ true,
    /*emit*/ [](FILE *fp){
      if (g_config.password.empty()){ return; }
      if (g_config.password.rfind("$argon2id$", 0) == 0){
        fprintf(fp, "requirepass \"%s\"\n", g_config.password.c_str());
      } else {
        fprintf(fp, "requirepass \"#%s\"\n", g_config.password.c_str());
      }
    } },

  { "tls-port", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long p = 0;
      if (!parse_int_strict(a[0].c_str(), &p) || p < 0 || p > 65535){
        e = "invalid tls-port"; return CfgResult::BADVALUE;
      }
      g_config.tls_port = (int)p; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.tls_port); return true; },
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (g_config.tls_port != 0){ fprintf(fp, "tls-port %d\n", g_config.tls_port); }
    } },

  { "tls-cert-file", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      return tls_material_set(g_config.tls_cert_file, a[0], e);
    },
    [](std::string &o) -> bool { o = g_config.tls_cert_file; return true; },
    // tls-cert-file
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (!g_config.tls_cert_file.empty()){
        fprintf(fp, "tls-cert-file \"%s\"\n", g_config.tls_cert_file.c_str());
      }
    } },

  { "tls-key-file", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      return tls_material_set(g_config.tls_key_file, a[0], e);
    },
    [](std::string &o) -> bool { o = g_config.tls_key_file; return true; },
    // tls-key-file
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (!g_config.tls_key_file.empty()){
        fprintf(fp, "tls-key-file \"%s\"\n", g_config.tls_key_file.c_str());
      }
    } },

  { "tls-ca-cert-file", 1, 1,
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.tls_ca_cert_file = a[0]; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.tls_ca_cert_file; return true; },
    // tls-ca-cert-file
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (!g_config.tls_ca_cert_file.empty()){
        fprintf(fp, "tls-ca-cert-file \"%s\"\n", g_config.tls_ca_cert_file.c_str());
      }
    } },


  { "tls-auth-clients", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      std::string v = a[0];
      for (char &c : v){ c = (char)tolower((unsigned char)c); }
      if      (v == "yes")     { g_config.tls_auth_clients = TlsAuthClients::YES; }
      else if (v == "no")      { g_config.tls_auth_clients = TlsAuthClients::NO; }
      else if (v == "optional"){ g_config.tls_auth_clients = TlsAuthClients::OPTIONAL; }
      else { e = "tls-auth-clients must be yes, no, or optional"; return CfgResult::BADVALUE; }
      return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      o = g_config.tls_auth_clients == TlsAuthClients::YES ? "yes"
        : g_config.tls_auth_clients == TlsAuthClients::NO  ? "no" : "optional";
      return true;
    },
    // tls-auth-clients
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (g_config.tls_auth_clients != TlsAuthClients::NO){
        fprintf(fp, "tls-auth-clients %s\n",
                g_config.tls_auth_clients == TlsAuthClients::YES ? "yes" : "optional");
      }
    } },

  { "tls-handshake-timeout", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long s = 0;
      if (!parse_int_strict(a[0].c_str(), &s) || s < 1 || s > 3600){
        e = "invalid tls-handshake-timeout (seconds, 1-3600)"; return CfgResult::BADVALUE;
      }
      g_config.tls_handshake_timeout_ms = (int)s * 1000; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.tls_handshake_timeout_ms / 1000); return true; },
    // tls-handshake-timeout
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (g_config.tls_handshake_timeout_ms != 10 * 1000){
        fprintf(fp, "tls-handshake-timeout %d\n", g_config.tls_handshake_timeout_ms / 1000);
      }
    } },



  { "dbfilename", 1, 1, 
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.dump_path = a[0]; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.dump_path; return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "appendonly", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      bool b = false;
      if (!parse_bool_strict(a[0], &b)){ e = "expected yes/no"; return CfgResult::BADVALUE; }
      g_config.aof_enable = b; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.aof_enable ? "yes" : "no"; return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

    { "appendfilename", 1, 1,
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.aof_path = a[0]; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.aof_path; return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "appendfsync", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      if      (a[0] == "always")  { g_config.aof_fysnc = Aoffsync::ALWAYS; }
      else if (a[0] == "no")      { g_config.aof_fysnc = Aoffsync::NO; }
      else if (a[0] == "everysec"){ g_config.aof_fysnc = Aoffsync::EVERYSEC; }
      else { e = "invalid appendfsync"; return CfgResult::BADVALUE; }
      return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      o = g_config.aof_fysnc == Aoffsync::ALWAYS ? "always"
        : g_config.aof_fysnc == Aoffsync::NO     ? "no" : "everysec";
      return true;
    },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "maxmemory", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      size_t b = 0;
      if (!parse_memory_size(a[0], &b)){ e = "invalid maxmemory"; return CfgResult::BADVALUE; }
      g_config.maxmemory = b; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.maxmemory); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "maxmemory-policy", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      MaxmemoryPolicy pol;
      if (!parse_maxmemory_policy(a[0], &pol)){ e = "invalid policy"; return CfgResult::BADVALUE; }
      g_config.maxmemory_policy = pol; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = maxmemory_policy_name(g_config.maxmemory_policy); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "maxmemory-samples", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long p = 0;
      if (!parse_int_strict(a[0].c_str(), &p) || p < 1){
        e = "invalid memory samples"; return CfgResult::BADVALUE;
      }
      g_config.maxmemory_samples = (int)p; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.maxmemory_samples); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  // 0 args is legal here and means "off" — the one directive whose empty value is
  // meaningful, which is also why config_write_scalar() must quote it.
  { "notify-keyspace-events", 0, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      std::string v = a.empty() ? std::string() : a[0];
      int f = 0;
      if (!parse_notify_flags(v, &f)){
        e = "invalid notify-keyspace-events flags"; return CfgResult::BADVALUE;
      }
      g_config.notify_keyspace_events = f; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = notify_flags_string(g_config.notify_keyspace_events); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

    { "save", 1, 2,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      if (a.size() == 1 && a[0].empty()){        // save '' disables all conditions
        g_config.save_conditions.clear(); return CfgResult::OK;
      }
      if (a.size() != 2){ e = "save needs <seconds> <changes>"; return CfgResult::BADVALUE; }
      g_config.save_conditions.push_back({
        strtoull(a[0].c_str(), nullptr, 10),
        (uint32_t)strtoul(a[1].c_str(), nullptr, 10)
      });
      return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      char b[64];
      o.clear();
      for (const SaveCondition &s : g_config.save_conditions){
        if (!o.empty()){ o += ' '; }
        snprintf(b, sizeof(b), "%llu %u", (unsigned long long)s.seconds, s.changes);
        o += b;
      }
      return true;
    },
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      for (const SaveCondition &s : g_config.save_conditions){
        fprintf(fp, "save %llu %u\n", (unsigned long long)s.seconds, s.changes);
      }
    } },

  { "auto-aof-rewrite-percentage", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long p = 0;
      if (!parse_int_strict(a[0].c_str(), &p) || p < 0){ e = "invalid"; return CfgResult::BADVALUE; }
      g_config.aof_rewrite_perc = (int)p; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.aof_rewrite_perc); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "auto-aof-rewrite-min-size", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      size_t b = 0;
      if (!parse_memory_size(a[0], &b)){ e = "invalid"; return CfgResult::BADVALUE; }
      g_config.aof_rewrite_min_size = b; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.aof_rewrite_min_size); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

      // Not exposed by CONFIG GET (get == nullptr): write-only config-file
      // constructs with no single-value form.

  { "repl-backlog-size", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      size_t b = 0;
      if (!parse_memory_size(a[0], &b)){ e = "invalid repl-backlog-size"; return CfgResult::BADVALUE; }
      if (b != 0 && b < k_repl_backlog_min){
          e = "repl-backlog-size must be 0 or at least " + std::to_string(k_repl_backlog_min);
          return CfgResult::BADVALUE;
      }
      g_config.repl_backlog_size = b; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.repl_backlog_size); return true; },
    /*boot_only*/ true, /*masked*/ false, /*emit*/ nullptr},

  { "repl-timeout", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long s = 0;
      if (!parse_int_strict(a[0].c_str(), &s) || s < 0 || s > 3600){
        e = "invalid repl-timeout (seconds, 0 to disable, max 3600)";
        return CfgResult::BADVALUE;
      }
      g_config.repl_timeout_ms = (int)s * 1000; return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      o = std::to_string(g_config.repl_timeout_ms / 1000); return true;
    },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "repl-ping-replica-period", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long s = 0;
      if (!parse_int_strict(a[0].c_str(), &s) || s < 0 || s > 3600){
        e = "invalid repl-ping-replica-period (seconds, 0 to disable, max 3600)";
        return CfgResult::BADVALUE;
      }
      g_config.repl_ping_period_ms = (int)s * 1000; 
      g_data.repl_ping_at_ms = get_monotonic_msec() + g_config.repl_ping_period_ms;
      return CfgResult::OK;
    },
    [](std::string &o) -> bool {
      o = std::to_string(g_config.repl_ping_period_ms / 1000); return true;
    },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

{ "min-replicas-to-write", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long n = 0;
      if (!parse_int_strict(a[0].c_str(), &n) || n < 0 || n > 1024){
        e = "invalid min-replicas-to-write (0 to disable)";
        return CfgResult::BADVALUE;
      }
      g_config.min_replicas_to_write = (int)n; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.min_replicas_to_write); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "min-replicas-max-lag", 1, 1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long s = 0;
      if (!parse_int_strict(a[0].c_str(), &s) || s < 0 || s > 3600){
        e = "invalid min-replicas-max-lag (seconds, 0 to ignore lag)";
        return CfgResult::BADVALUE;
      }
      g_config.min_replicas_max_lag = (int)s; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = std::to_string(g_config.min_replicas_max_lag); return true; },
    /*boot_only*/ false, /*masked*/ false, /*emit*/ nullptr },

  { "replicaof", 2, 2,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      long p = 0;
      if (!parse_int_strict(a[1].c_str(), &p) || p <= 0 || p > 65535){
        e = "invalid master port '" + a[1] + "'";
        return CfgResult::BADVALUE;
      }
      // RECORD ONLY, never connect: this runs before repl_init() has minted a
      // repl_id, before the local RDB/AOF load, and before the poll loop exists.
      // main() performs the connect once all three are done.
      g_config.replicaof_host = a[0];
      g_config.replicaof_port = (int)p;
      return CfgResult::OK;
    },
    /*get*/ nullptr,   // two tokens: no single-value form, like user/rename-command
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (!g_data.replica_mode){ return; }
      // the host is an IPv4 literal (inet_pton validates it), so it never needs quoting
      fprintf(fp, "replicaof %s %d\n", g_data.master_host.c_str(), g_data.master_port);
    } },

  { "masterauth", 1, 1,
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.masterauth = a[0]; return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.masterauth.empty() ? "" : "<set>"; return true; },
    /*boot_only*/ false, /*masked*/ true,
    /*emit*/ [](FILE *fp){
      if (g_config.masterauth.empty()){ return; }
      std::string esc;
      for (char c : g_config.masterauth){
        if (c == '"' || c == '\\'){ esc += '\\'; }
        esc += c;
      }
      fprintf(fp, "masterauth \"%s\"\n", esc.c_str());   // always quoted: it is a password
    } },

  // constructs with no single-value form.
  { "user", 1, -1,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      User &u = g_config.users[a[0]];
      if (u.name.empty()){ u.name = a[0]; }
      for (size_t i = 1; i < a.size(); ++i){
        if (!acl_apply_rule(u, a[i])){
          e = "Invalid ACL rule '" + a[i] + "' for user '" + a[0] + "'";
          return CfgResult::BADVALUE;
        }
      }
      if (u.name.empty()){ u.name = a[0]; }   // a reset token wipes name mid-line
      return CfgResult::OK;
    },
    nullptr,
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      // 'default' is skipped: rebuilt from requirepass + acl_bootstrap_default() at boot
      for (const auto &kv : g_config.users){
        if (kv.first == "default"){ continue; }
        std::string line = acl_format_user(kv.first, kv.second, true);  // real hashes, quoted
        fprintf(fp, "%s\n", line.c_str());
      }
    } },

  { "rename-command", 2, 2,
    [](const std::vector<std::string> &a, std::string &e) -> CfgResult {
      std::string oldn = a[0], neu = a[1];
      for (char &c : oldn){ c = (char)tolower((unsigned char)c); }
      for (char &c : neu){ c = (char)tolower((unsigned char)c); }

      if (oldn.empty()){ e = "rename-command OLD is empty"; return CfgResult::BADVALUE; }
      if (!command_is_known(oldn)){ e = "rename-command: unknown command '" + oldn + "'"; return CfgResult::BADVALUE; }
      for (auto &r : g_config.renames){
        if (r.first == oldn){ e = "rename-command: '" + oldn + "' renamed twice"; return CfgResult::BADVALUE; }
      }
      if (!neu.empty()){
        for (unsigned char c : neu){
          if (c < 0x20 || c == 0x7f){ e = "rename-command: NEW has control chars"; return CfgResult::BADVALUE; }
        }
        if (command_is_known(neu)){ e = "rename-command: NEW '" + neu + "' collides with a command"; return CfgResult::BADVALUE; }
        for (auto &r : g_config.renames){
          if (r.second == neu){ e = "rename-command: NEW '" + neu + "' already used"; return CfgResult::BADVALUE; }
        }
      } else if (oldn == "auth" && !g_config.password.empty()){
        e = "refusing to disable AUTH while a password is set (would lock out clients)";
        return CfgResult::BADVALUE;
      }
      g_config.renames.emplace_back(oldn, neu);
      return CfgResult::OK;
    },
    nullptr,
    /*boot_only*/ true, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      for (const auto &r : g_config.renames){
        if (r.second.empty()){ fprintf(fp, "rename-command %s \"\"\n", r.first.c_str()); }
        else { fprintf(fp, "rename-command %s %s\n", r.first.c_str(), r.second.c_str()); }
      }
    } },

  { "auditlog", 1, 1,
    [](const std::vector<std::string> &a, std::string &) -> CfgResult {
      g_config.auditlog_path = a[0];   // "" disables, "stderr", or a path
      audit_open(g_config.auditlog_path);
      return CfgResult::OK;
    },
    [](std::string &o) -> bool { o = g_config.auditlog_path; return true; },
    /*boot_only*/ false, /*masked*/ false,
    /*emit*/ [](FILE *fp){
      if (!g_config.auditlog_path.empty()){
        fprintf(fp, "auditlog \"%s\"\n", g_config.auditlog_path.c_str());
      }
    } },

};

// Structural invariasnt of k_config_table. Returns the number of problems/
// Structural invariants of k_config_table. Returns the number of problems.
int config_selfcheck(){ 
  int problems = 0;
  for (const ConfigDirective &d : k_config_table){
    if (!d.apply){
      fprintf(stderr, "selfcheck: config '%s' has no apply()\n", d.name);
      problems++;
    }
    if (d.masked && !d.emit){
      fprintf(stderr, "selfcheck: masked config '%s' has no emit() — CONFIG REWRITE "
                      "would write its placeholder to disk\n", d.name);
      problems++;
    }
    if (d.max_args >= 0 && d.max_args < d.min_args){
      fprintf(stderr, "selfcheck: config '%s' has an impossible arity\n", d.name);
      problems++;
    }
  }
  for (const ConfigDirective &d : k_config_table){
    if (d.emit || !d.get){ continue; }
    std::string v1, v2, err;
    if (!d.get(v1)){ continue; }
    std::vector<std::string> args;
    if (!v1.empty()){ args.push_back(v1); }
    if ((int)args.size() < d.min_args){ continue; }   // e.g. auditlog unset
    if (d.apply(args, err) != CfgResult::OK){
      fprintf(stderr, "selfcheck: config '%s' emits \"%s\", which its own apply() rejects: %s\n",
              d.name, v1.c_str(), err.c_str());
      problems++;
      continue;
    }
    if (!d.get(v2) || v1 != v2){
      fprintf(stderr, "selfcheck: config '%s' does not round-trip: \"%s\" -> \"%s\"\n",
              d.name, v1.c_str(), v2.c_str());
      problems++;
    }
  }
  return problems;
}


bool config_is_boot_only(const std::string &name){
  for (const ConfigDirective &d : k_config_table){
    if (name == d.name){ return d.boot_only; } 
  }
  return false; 
}

bool config_is_masked(const std::string &name){
  for (const ConfigDirective &d : k_config_table){
    if (name == d.name){ return d.masked; } 
  }
  return false; 
}

// Hoisted out of the table so the legacy underscore sppelings can

CfgResult config_apply(const std::string &name_in, const std::vector<std::string> &args, std::string &err){
  std::string name = name_in;
  for (char &c : name){ c = (char)tolower((unsigned char)c); }

  for (const ConfigDirective &d : k_config_table){
    if (name != d.name){ continue; }
    if ((int)args.size() < d.min_args ||
        (d.max_args >= 0 && (int)args.size() > d.max_args)){
      err = "wrong number of args for '" + name + "'";
      return CfgResult::BADVALUE;
    }
    return d.apply(args, err);
  }
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

// Every directive config_apply() accepts must appear here AND in
// config_all_names(), or CONFIG GET silently answers [].
bool config_get_value(const std::string &name, std::string &out){
  for (const ConfigDirective &d : k_config_table){
    if (name == d.name){ return d.get ? d.get(out) : false; }
  }
  return false;
}


// Config-file order. CONFIG GET walks this and glob-matches the requested name.
// Table order is config-file order. CONFIG GET walks this and glob-matches.
void config_all_names(std::vector<std::string> &out){
  for (const ConfigDirective &d : k_config_table){
    if (d.get){ out.emplace_back(d.name); }
  }
}

// The one place a scalar directive reaches disk. Values comes from
// config_get_value(), so CONFIG GET and CONFIG REWRITE cannot drift apart
static bool config_write_scalar(FILE *fp, const std::string &name){
  if (config_is_masked(name)){ return false; }
  std::string v;
  if (!config_get_value(name, v)){ return false; }
  // Quote only what config_tokenize() would otherwise mangle: an empty value
  // tokenizes to zero args (need1() rejects it at the next boot), whitespace
  // splits it in two, '#' starts a comment, and '"'/'\' are the escape chars.
  // Everything else stays bare, so `port 1234` keeps reading like a config file.
  bool quote = v.empty();
  for (char c : v){
    if (isspace((unsigned char)c) || c == '"' || c == '\\' || c == '#'){ quote = true; break; }
  }
  if (!quote){
    fprintf(fp, "%s %s\n", name.c_str(), v.c_str());
    return true;
  }
  std::string esc;
  for (char c : v){
    if (c == '"' || c == '\\'){ esc += '\\'; }
    esc += c;
  }
  fprintf(fp, "%s \"%s\"\n", name.c_str(), esc.c_str());
  return true;
}

// serialize live config back
bool config_rewrite(const char * path){
  std::string tmp = std::string(path) + ".tmp";
  FILE *fp = fopen(tmp.c_str() ,"w");
  if (!fp){ return false; }

  bool ok = true;
  for (const ConfigDirective &d : k_config_table){
    if (d.emit){ d.emit(fp); continue; }
    ok = config_write_scalar(fp, d.name) && ok;
  }

  if (!ok || fflush(fp) != 0 || fsync(fileno(fp)) != 0){
    fclose(fp);
    unlink(tmp.c_str());
    return false;
  }
  fclose(fp);
  if (rename(tmp.c_str(), path) != 0){ unlink(tmp.c_str()); return false; }
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
      n += d.cap * sizeof(std::string) + d.elem_bytes;
      break;
    }
    case T_HASH: n += entry_hash(ent).elem_bytes; break;
    case T_SET:  n += entry_set(ent).elem_bytes; break;
    case T_ZSET: n += entry_zset(ent).hmap.elem_bytes; break;
    default: break;
  }
  return n;
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

static bool cb_mem_sum(HNode *node, void *arg){
  Entry *e = container_of(node, &Entry::node);
  *(size_t *)arg += entry_mem_usage(e);
  return true;
}

static bool cb_mem_hash(HNode *node, void *arg){
  *(size_t *)arg += hash_node_bytes(container_of(node, &HashNode::node));
  return true;
}

// independent recount of deque element bytes, catches elem-bytes drift
// independent recount vs the maintained elem_bytes counters — catches drift
static bool cb_bytes_check(HNode *node, void *arg){
  (void)arg;
  Entry *e = container_of(node, &Entry::node);
  size_t sum = 0, counter = 0;
  const char *what = nullptr;
  switch (e->type){
    case T_DLIST: {
      Deque &d = entry_deque(e);
      for (size_t i = 0; i < d.count; ++i){ sum += deque_get(&d, i)->capacity(); }
      counter = d.elem_bytes; what = "deque";
      break;
    }
    case T_HASH:
      hm_foreach(&entry_hash(e), cb_mem_hash, &sum);
      counter = entry_hash(e).elem_bytes; what = "hash";
      break;
    case T_SET:
      hm_foreach(&entry_set(e), cb_mem_set, &sum);
      counter = entry_set(e).elem_bytes; what = "set";
      break;
    case T_ZSET:
      hm_foreach(&entry_zset(e).hmap, cb_mem_zset, &sum);
      counter = entry_zset(e).hmap.elem_bytes; what = "zset";
      break;
    default: return true;
  }
  if (sum != counter){
    fprintf(stderr, "[mem] %s elem_bytes drift on '%s': counter=%zu walk=%zu\n",
            what, e->key.c_str(), counter, sum);
  }
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
  hm_foreach(&g_data.db, cb_bytes_check, nullptr);

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
    if (parsed < 0 || parsed > 32){ return false; }
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
  notify_keyspace_event(NOTIFY_EXPIRED, "expired", ent->key);
  hm_delete(&g_data.db, &ent->node, &hnode_same);
  entry_del(ent);
  return true;
}

Conn *replica_by_addr(const std::string &host, int port){
  for (Conn *r : g_data.replicas){
    const std::string ip = r->peer.substr(0, r->peer.find(':'));
    if (ip == host && r->replica_port == port){ return r; }
  }
  return nullptr; 
}

// Replication
// 40 hex chars, the shape redis uses. not a secret - it is published in INFO 
// and exists only so a reconnecting replica can tell "same master history"
// from "a different one "
static std::string repl_gen_id(){
  static const char hx[] = "0123456789abcdef";
  std::string s;
  s.reserve(40);
  for (int i = 0; i < 40; ++i){ s += hx[rand_idx(16)]; }
  return s;
}

void repl_new_id(){
  g_data.repl_id = repl_gen_id();
}

// promotion to master, the Identity we served under becomes history rather than being overwritten
void repl_shift_id(){
  g_data.repl_id2 = g_data.repl_id;
  g_data.second_repl_offset = g_data.master_repl_offset + 1;
  repl_new_id();
}

void repl_init(){
  repl_new_id();
  g_data.repl_backlog.assign(g_config.repl_backlog_size, '\0');
  g_data.repl_backlog_pos = 0;
  g_data.repl_backlog_histlen = 0;
}

// Stream offset of the oldest byte the backlog can still server
uint64_t repl_backlog_start_offset(){
  return g_data.repl_backlog_histlen
         ? g_data.master_repl_offset - g_data.repl_backlog_histlen + 1
         : 0;
}

// Every byte of the write stream passes through here.
void repl_backlog_feed(const char *bytes, size_t len){
  if (len == 0){ return; }
  g_data.master_repl_offset += len;

  const size_t cap = g_data.repl_backlog.size();
  if (cap == 0){ return; }

  // a write larger than the ring can only leave its last cap bytes behind
  if (len >= cap){
    bytes += len - cap;
    len = cap;
  }

  // at most two memcpys: up to the end of the ring, then the wrapped tail.
  const size_t first = std::min(len, cap - g_data.repl_backlog_pos);
  memcpy(g_data.repl_backlog.data() + g_data.repl_backlog_pos, bytes, first);
  if (len > first){
    memcpy(g_data.repl_backlog.data(), bytes + first, len - first);
  }
  g_data.repl_backlog_pos = (g_data.repl_backlog_pos + len) % cap;
  g_data.repl_backlog_histlen = std::min(g_data.repl_backlog_histlen + len, cap);
}

// The mirror of repl_backlog_feed hand back the last need bytes, oldest first
void repl_backlog_copy(uint64_t need, Buffer *out){
  if (need == 0){ return; }
  const size_t cap = g_data.repl_backlog.size();
  // repl_backlog_pos is the write cursor, so the newest byte is at pos - 1
  const size_t start = (g_data.repl_backlog_pos + cap - (size_t)need) % cap;
  const size_t first = std::min((size_t)need, cap - start);
  buf_append(out, g_data.repl_backlog.data() + start, first);
  if ((size_t)need > first){
    buf_append(out, g_data.repl_backlog.data(), (size_t)need - first);
  }
}
