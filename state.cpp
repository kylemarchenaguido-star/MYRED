#include "state.h"
#include "common.h"
#include "hash.h"
#include "sha256.h"
#include "cred.h"
#include <arpa/inet.h>  
#include <time.h>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <cerrno>
#include <utility>
#include <vector>

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
    } else if (args[0].rfind("$argon2id$", 0) == 0){
      // hash plaintext
      g_config.password =args[0];
    } else {
      // plaint text, current policy
      g_config.password = cred_hash_new(args[0]);
      if (g_config.password.empty()){ err = "password hashing failed"; return CfgResult::BADVALUE; }
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
    long p = 0;
    if (!parse_int_strict(args[0].c_str(), &p) || p < 1 || p > 65535){
      err = "invalid port";
      return CfgResult::BADVALUE;  
    }
    g_config.port = (int)p;
    return CfgResult::OK;
  }

  if (name == "tls-port"){
    if (!need1()){ return CfgResult::BADVALUE; }
    long p = 0;
    if (!parse_int_strict(args[0].c_str(), &p) || p < 0 || p > 65535){
      err = "invalid tls-port";
      return CfgResult::BADVALUE;
    }
    g_config.tls_port = (int)p;
    return CfgResult::OK;
  }

  if (name == "tls-cert-file" || name == "tls-key-file" || name == "tls-ca-cert-file"){
    if (!need1()){ return CfgResult::BADVALUE; }
    if      (name == "tls-cert-file") { g_config.tls_cert_file = args[0]; }
    else if (name == "tls-key-file")  { g_config.tls_key_file = args[0]; }
    else                              { g_config.tls_ca_cert_file = args[0]; }
    return CfgResult::OK;
  }

  if (name == "tls-auth-clients"){
    if (!need1()){ return CfgResult::BADVALUE; }
    std::string v = args[0];
    for (char &c : v){ c = (char)tolower((unsigned char)c); }
    if      (v == "yes")     {g_config.tls_auth_clients = TlsAuthClients::YES; }
    else if (v == "nos")     {g_config.tls_auth_clients = TlsAuthClients::NO; }
    else if (v == "optional"){g_config.tls_auth_clients = TlsAuthClients::OPTIONAL; }
    else { err = "tls-auth-clients mus be yes, no, or optional"; return CfgResult::BADVALUE; }
    return CfgResult::OK;
  }
  if (name == "tls-handshake-timeout"){
    if (!need1()){ return CfgResult::BADVALUE; }
    long s = 0;
    if (!parse_int_strict(args[0].c_str(), &s) || s < 1 || s > 3600){
      err = "invalid tls-handshake-timeout (seconds, 1-3600)";
      return CfgResult::BADVALUE;
    }
    g_config.tls_handshake_timeout_ms = (int)s * 1000;
    return CfgResult::OK;
  }

  if (name == "bind"){
    if (args.empty()){ err = "bind needs at least one address"; return CfgResult::BADVALUE; }
    g_config.binds = args;
    return CfgResult::OK;
  }

  if (name == "protected-mode"){
    if(!need1()){ return CfgResult::BADVALUE; }
    bool b = false;
    if (!parse_bool_strict(args[0], &b)){
      err= "expected yes/no";
      return CfgResult::BADVALUE; 
    }
    g_config.protected_mode = b;
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
    long p = 0;
    if (!parse_int_strict(args[0].c_str(), &p) || p < 1){
      err = "invalid memory samples";
      return CfgResult::BADVALUE;
    }
    g_config.maxmemory_samples = (int)p;
    return CfgResult::OK;
  }

  if (name == "notify-keyspace-events"){
    // empty value is legal and means "off"
    std::string v = args.empty() ? std::string() : args[0];
    int f = 0;
    if (!parse_notify_flags(v, &f)) {err = "invalid notify-keyspace-events flags"; return CfgResult::BADVALUE; }
    g_config.notify_keyspace_events = f;
    return CfgResult::OK;
  }

  // AOF persistence
  if (name == "appendonly"){
    if (!need1()){ return CfgResult::BADVALUE; }
    bool b = false;
    if (!parse_bool_strict(args[0], &b)){
      err = "expected yes/no";
      return CfgResult::BADVALUE;
    }
    g_config.aof_enable = b;
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

  if (name == "auto-aof-rewrite-percentage" || name == "auto_aof_rewrite-percentage"){
    if (!need1()){ return CfgResult::BADVALUE; }
    long p = 0;
    if (!parse_int_strict(args[0].c_str(), &p) || p < 0){
      err = "invalid";
      return CfgResult::BADVALUE;
    }
    g_config.aof_rewrite_perc = (int)p;
    return CfgResult::OK;
  }

  if (name == "auto-aof-rewrite-min-size" || name == "auto_aof_rewrite-min-size"){
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

// Every directive config_apply() accepts must appear here AND in
// config_all_names(), or CONFIG GET silently answers [].
bool config_get_value(const std::string &name, std::string &out){
  char buf[64];
  if (name == "port"){ snprintf(buf, sizeof(buf), "%d", g_config.port); out = buf; return true; }
  if (name == "bind"){
    out.clear();
    for (const std::string &b : g_config.binds){ if (!out.empty()){ out += ' '; } out += b; }
    return true;
  }
  if (name == "protected-mode"){ out = g_config.protected_mode ? "yes" : "no"; return true; }
  if (name == "allow-ip"){
    out.clear();
    for (const auto &a : g_config.allowlist){
      struct in_addr ia; 
      ia.s_addr = htonl(a.first);
      if (!out.empty()){ out += ' '; }
      out += inet_ntoa(ia);
      snprintf(buf, sizeof(buf), "/%d", __builtin_popcount(a.second));
      out += buf;
    }
    return true;
  }
  // Deliberately masked: what we store is a password HASH, and CONFIG GET is
  // reachable by any @admin user. Redis returns the value because Redis stores
  // plaintext. Use ACL LIST / ACL GETUSER for credential state.
  if (name == "requirepass"){ out = g_config.password.empty() ? "" : "<set>"; return true; }
  if (name == "tls-port"){ snprintf(buf, sizeof(buf), "%d", g_config.tls_port); out = buf; return true; }
  if (name == "tls-cert-file"){ out = g_config.tls_cert_file; return true; }
  if (name == "tls-key-file"){ out = g_config.tls_key_file; return true; }
  if (name == "tls-ca-cert-file"){ out = g_config.tls_ca_cert_file; return true; }
  if (name == "tls-auth-clients"){
    out = g_config.tls_auth_clients == TlsAuthClients::YES ? "yes"
        : g_config.tls_auth_clients == TlsAuthClients::NO  ? "no" : "optional";
    return true;
  }
  if (name == "tls-handshake-timeout"){
    snprintf(buf, sizeof(buf), "%d", g_config.tls_handshake_timeout_ms / 1000);
    out = buf;
    return true;
  }
  if (name == "dbfilename"){     out = g_config.dump_path; return true; }
  if (name == "appendonly"){     out = g_config.aof_enable ? "yes" : "no"; return true; }
  if (name == "appendfilename"){ out = g_config.aof_path;  return true; }
  if (name == "appendfsync"){
    out = g_config.aof_fysnc == Aoffsync::ALWAYS ? "always"
        : g_config.aof_fysnc == Aoffsync::NO     ? "no" : "everysec";
    return true;
  }
  if (name == "maxmemory"){ snprintf(buf, sizeof(buf), "%zu", g_config.maxmemory); out = buf; return true; }
  if (name == "maxmemory-policy"){ out = maxmemory_policy_name(g_config.maxmemory_policy); return true; }
  if (name == "maxmemory-samples"){
    snprintf(buf, sizeof(buf), "%d", g_config.maxmemory_samples); out = buf; return true;
  }
  if (name == "notify-keyspace-events"){
    out = notify_flags_string(g_config.notify_keyspace_events); return true;
  }

  if (name == "auto-aof-rewrite-percentage"){
    snprintf(buf, sizeof(buf), "%d",g_config.aof_rewrite_perc); out = buf; return true;
  }
  if (name == "auto-aof-rewrite-min-size"){
    snprintf(buf, sizeof(buf), "%zu", g_config.aof_rewrite_min_size); out = buf; return true;
  }
  if (name == "save"){
    out.clear();
    for (const SaveCondition &s : g_config.save_conditions){
      if (!out.empty()){ out += ' '; }
     snprintf(buf, sizeof(buf), "%llu %u", (unsigned long long)s.seconds, s.changes); 
     out += buf;
    }
    return true;
  }
  if (name == "auditlog"){ out = g_config.auditlog_path; return true; }
  return false;
}

// Config-file order. CONFIG GET walks this and glob-matches the requested name.
void config_all_names(std::vector<std::string> &out){
  static const char *names[] = {
    "port", "bind", "protected-mode", "allow-ip", "requirepass",
    "tls-port", "tls-cert-file", "tls-key-file", "tls-ca-cert-file",
    "tls-auth-clients", "tls-handshake-timeout",
    "dbfilename", "appendonly", "appendfilename", "appendfsync",
    "maxmemory", "maxmemory-policy", "maxmemory-samples",
    "notify-keyspace-events", "save",
    "auto-aof-rewrite-percentage", "auto-aof-rewrite-min-size", "auditlog",
  };
  for (const char *n : names){ out.emplace_back(n); }
}

// serialize live config back
bool config_rewrite(const char * path){
  std::string tmp = std::string(path) + ".tmp";
  FILE *fp = fopen(tmp.c_str() ,"w");
  if (!fp){ return false; }
  fprintf(fp, "port %d\n", g_config.port);
  if (!g_config.binds.empty()){
    fprintf(fp, "bind");
    for (const std::string &b : g_config.binds){ fprintf(fp, " %s", b.c_str()); }
    fprintf(fp, "\n");
  }
  fprintf(fp, "protected-mode %s\n", g_config.protected_mode ? "yes" : "no");
  for (const auto &a : g_config.allowlist){
    struct in_addr ia;
    ia.s_addr = htonl(a.first);
    fprintf(fp, "allow-ip %s/%d\n", inet_ntoa(ia), __builtin_popcount(a.second));
  }
  if (!g_config.password.empty()){
    if (g_config.password.rfind("$argon2id$", 0) == 0){
      fprintf(fp, "requirepass \"%s\"\n", g_config.password.c_str());
    } else {
      fprintf(fp, "requirepass \"#%s\"\n", g_config.password.c_str());
    }
  }

  // TLS 
  if (g_config.tls_port != 0){ 
    fprintf(fp, "tls-port %d\n", g_config.tls_port); 
  }
  if (!g_config.tls_cert_file.empty()){ 
    fprintf(fp, "tls-cert-file \"%s\"\n", g_config.tls_cert_file.c_str());
   }
  if (!g_config.tls_key_file.empty()){ 
    fprintf(fp, "tls-key-file \"%s\"\n", g_config.tls_key_file.c_str());
   }
  if (!g_config.tls_ca_cert_file.empty()){ 
    fprintf(fp, "tls-ca-cert-file \"%s\"\n", g_config.tls_ca_cert_file.c_str());
   }
  if (g_config.tls_auth_clients != TlsAuthClients::NO){
    fprintf(fp, "tls-auth-clients %s\n",
      g_config.tls_auth_clients == TlsAuthClients::YES ? "yes" : "optional");
  }
  if (g_config.tls_handshake_timeout_ms != 10 * 1000){
    fprintf(fp, "tls-handshake-timeout %d\n", g_config.tls_handshake_timeout_ms / 1000);
  }
  fprintf(fp, "dbfilename %s\n", g_config.dump_path.c_str());
  fprintf(fp, "appendonly %s\n", g_config.aof_enable ? "yes" : "no");
  fprintf(fp, "appendfilename %s\n", g_config.aof_path.c_str());
  fprintf(fp, "appendfsync %s\n",  g_config.aof_fysnc == Aoffsync::ALWAYS ? "always" 
                                : g_config.aof_fysnc == Aoffsync::NO     ? "no" : "everysec");
  fprintf(fp, "maxmemory %zu\n", g_config.maxmemory);
  fprintf(fp, "maxmemory-policy %s\n", maxmemory_policy_name(g_config.maxmemory_policy));
  fprintf(fp, "maxmemory-samples %d\n", g_config.maxmemory_samples);
  fprintf(fp, "notify-keyspace-events \"%s\"\n", notify_flags_string(g_config.notify_keyspace_events).c_str());
  for (const SaveCondition &s : g_config.save_conditions){
    fprintf(fp, "save %llu %u\n", (unsigned long long)s.seconds, s.changes);
  }

  fprintf(fp, "auto-aof-rewrite-percentage %d\n", g_config.aof_rewrite_perc);
  fprintf(fp, "auto-aof-rewrite-min-size %zu\n", g_config.aof_rewrite_min_size);

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

  if (fflush(fp) != 0 || fsync(fileno(fp)) != 0){
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
