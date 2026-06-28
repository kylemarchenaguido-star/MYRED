#include "rdb.h"
#include "state.h"      
#include "buffer.h"
#include "common.h"
#include "hash.h"
#include "set.h"
#include "aof.h"
#include "resp.h"
#include "commands.h"
#include <stdio.h> 
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <fcntl.h>

// AOF
pid_t g_aof_child_pid = -1;

static uint64_t mono_expiry_to_wall(uint64_t mono_expire){
  uint64_t mono_now = get_monotonic_msec();
  // ms left until expirary 
  int64_t remaining = (int64_t)(mono_expire - mono_now);
  // already expired
  if (remaining < 0) { remaining = 0; }
  return get_wall_msec() + (uint64_t)remaining;
}

struct AofBatch {
  Buffer *buf;
  const char *cmd; // ZADD / HSET / SADD
  std::string_view key;
  std::vector<std::string> args; // OWNS the strings (score are formated here)
  size_t per_elem; // 1 for SADD, 2 for ZADD/HSET
};

static constexpr size_t k_aof_batch = 64; // max elements per emitted command

// emit one RESP array command into a Buffer
static void aof_emit_vec(Buffer *buf, const std::vector<std::string_view> &parts){
  char hdr[32];
  int n = snprintf(hdr, sizeof(hdr), "*%zu\r\n", parts.size());
  buf_append(buf, hdr, (size_t)n);
  for (std::string_view a : parts){
    int h = snprintf(hdr, sizeof(hdr), "$%zu\r\n", a.size());
    buf_append(buf, hdr, (size_t)h);
    buf_append(buf, a.data(), a.size());
    buf_append(buf, "\r\n", (size_t)2);
  }
}

static void aof_batch_flush(AofBatch *b){
  if (b->args.empty()){ return; }
  std::vector<std::string_view> v;
  v.reserve(b->args.size() + 2);
  v.push_back(b->cmd);
  v.push_back(b->key);
  for (auto &s : b->args){ v.push_back(s); }
  aof_emit_vec(b->buf, v);
  b->args.clear();
}

static inline void aof_batch_maybe_flush(AofBatch *b){
  if (b->args.size() >= k_aof_batch * b->per_elem) aof_batch_flush(b);
}

// member callbacks (Same node types as your RDB callbacks)
static bool cb_aof_zset(HNode *node, void *arg){
  AofBatch *b = (AofBatch *)arg;
  ZNode *z = container_of(node, &ZNode::hmap);
  char sc[64];
  int n = snprintf(sc, sizeof(sc), "%.17g", z->score);
  b->args.emplace_back(sc, (size_t)n); // score
  b->args.emplace_back(z->name, z->len); // member
  aof_batch_maybe_flush(b);
  return true;
}

static bool cb_aof_hash(HNode *node, void *arg){
  AofBatch *b = (AofBatch *)arg;
  HashNode *h = container_of(node, &HashNode::node);
  b->args.emplace_back(h->field);
  b->args.emplace_back(h->value);
  aof_batch_maybe_flush(b);
  return true;
}

static bool cb_aof_set(HNode *node, void *arg){
  AofBatch *b = (AofBatch *)arg;
  SetNode *s = container_of(node, &SetNode::node);
  b->args.emplace_back(s->member);
  aof_batch_maybe_flush(b);
  return true;
}

static bool cb_aof_rewrite(HNode *node, void *arg){
  Buffer *buf = (Buffer *)arg;
  Entry *ent = container_of(node, &Entry::node);
  std::string_view key(ent->key.data(), ent->key.size());

  switch (ent->type){
    case T_STR: 
      aof_emit_vec(buf, { "SET", key, std::string_view(entry_str(ent).data(), entry_str(ent).size()) });
      break;
    case T_ZSET: {
      AofBatch b {buf, "ZADD", key, {}, 2 };
      hm_foreach(&entry_zset(ent).hmap, cb_aof_zset, &b);
      aof_batch_flush(&b);
      break;
    }
    case T_DLIST: {
      AofBatch b {buf, "RPUSH", key, {}, 1 };
      for (size_t i = 0; i < entry_deque(ent).count; ++i){
        b.args.emplace_back(*deque_get(&entry_deque(ent), i));
        aof_batch_maybe_flush(&b);
      }
      aof_batch_flush(&b);
      break;
    }
    case T_HASH: {
      AofBatch b {buf, "HSET", key, {}, 2 };
      hm_foreach(&entry_hash(ent), cb_aof_hash, &b);
      aof_batch_flush(&b);
      break;
    }
    case T_SET: {
      AofBatch b {buf, "SADD", key, {}, 1 };
      hm_foreach(&entry_set(ent), cb_aof_set, &b);
      aof_batch_flush(&b);
      break;
    }
  }
  // TTL -> absolute PEXPIREAT
  if (entry_has_ttl(ent)){
    uint64_t abs = mono_expiry_to_wall(g_data.heap[ent->heap_idx].val);
    char ts[32];
    int n = snprintf(ts, sizeof(ts), "%llu", (unsigned long long)abs);
    aof_emit_vec(buf, { "PEXPIREAT", key, std::string_view(ts, (size_t)n) });
  }
  return true;
}

// child only - write the snapshot buffer, fsync, exit. No rename (parent finalizes)
static void aof_write_snapshot(const Buffer *buf, const char *tmp){
  int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0){ _exit(1); }
  const uint8_t *data = buf->data_begin;
  size_t remaning = buf_size(buf);
  while (remaning > 0){
    ssize_t n = write(fd, data, remaning);
    if (n < 0){ if (errno == EINTR) continue; close(fd); unlink(tmp); _exit(1); }
    data += n; remaning -= (size_t)n;
  }
  if (fsync(fd) != 0){ close(fd); unlink(tmp); _exit(1); }
  close(fd);
  _exit(0);
}


void aof_rewrite_background(){
  if (!g_config.aof_enable){ return; }  
  // wew doin't overlap 2 fork process
  if (g_aof_child_pid != -1 || g_rdb_child_pid != -1){
    fprintf(stderr, "aof_rewrite: a background save/rewrite is already running\n");
    return;
  }

  // serialize the WHOLE dataset to resp in the parent, before fork (all malloc here)
  Buffer buf = buf_create(64 * 1024);
  hm_foreach(&g_data.db, cb_aof_rewrite, &buf);

  g_data.g_aof_rewrite_buf.clear(); // start the delta clean

  pid_t pid = fork();
  if (pid < 0){
    fprintf(stderr, "aof_rewrite: fork failed: %s\n", strerror(errno));
    buf_destroy(&buf);
    return;
  }
  if (pid == 0){
    aof_write_snapshot(&buf, "appendonly.aof.tmp"); // never returns
  }
  buf_destroy(&buf);
  g_aof_child_pid = pid; // from here, aof_feed mirrors writes into g_aof_rewrite_buf
  fprintf(stderr, "aof_rewrite: started (pid=%d)\n", pid);
}


void aof_check_background_rewrite(){
  if (g_aof_child_pid == -1){ return; }
  int status = 0;
  pid_t r = waitpid(g_aof_child_pid, &status, WNOHANG);
  if (r == 0){ return; } // still running

  if (r == g_aof_child_pid && WIFEXITED(status) && WEXITSTATUS(status) == 0){
    const char *tmp = "appendonly.aof.tmp";
    // append the delta that accumulated during the rewrite
    int fd = open(tmp, O_WRONLY | O_APPEND);
    if (fd >= 0){
      const std::string &d = g_data.g_aof_rewrite_buf;
      size_t off = 0;
      while (off < d.size()){
        ssize_t n = write(fd, d.data() + off, d.size() - off);
        if (n < 0){ if (errno  == EINTR) { continue; } break;}
        off += (size_t)n;
      }  
      fsync(fd);
      close(fd);
      // atomic swap
      rename(tmp, g_config.aof_path.c_str());
      // repoint the live fd at the new, compacted file
      if (g_data.g_aof_fd >= 0){ close(g_data.g_aof_fd); }
      g_data.g_aof_fd = open(g_config.aof_path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);

      struct stat st;
      if (g_data.g_aof_fd >= 0 && fstat(g_data.g_aof_fd, &st) ==  0){
        g_data.g_aof_current_size  = (size_t)st.st_size;
        g_data.g_aof_base_size  = (size_t)st.st_size;
      }
      fprintf(stderr, "aof_rewrite: completed, %zu delta bytes\n", d.size());
    }
  } else {
    fprintf(stderr, "aof_rewrite: child failed (status=%d), keeping old AOF\n", status);
    unlink("appendonly.aof.tmp");
  }
  g_data.g_aof_rewrite_buf.clear();
  g_aof_child_pid = -1;
}

bool aof_load(const char *path){
    FILE *fp = fopen(path, "rb");
    // no AOF file 
    if (!fp){ return false; } 
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    // empty aof file, nothing to replay
    if (sz <= 0){ fclose(fp); return true; }

    Buffer buf = buf_create((size_t)sz);
    std::vector<uint8_t> raw((size_t)sz); 
    if (fread(raw.data(), 1, (size_t)sz, fp) != (size_t)sz){
        fprintf(stderr, "aof_load: short read\n");
        fclose(fp); buf_destroy(&buf); return false;
    }
    fclose(fp);
    buf_append(&buf, raw.data(), (size_t)sz);

    // reply setup -> suppress re-logging, bypass auth, discard replies
    g_data.g_loading = true;
    Conn fake{};
    fake.authenticaded = true;
    Buffer sink = buf_create(4096);

    // bytes consumed by fully parsed commands
    size_t good_offset = 0;
    size_t replayed = 0;
    while (buf_size(&buf)){
        std::vector<std::string> cmd;
        int32_t consumed = parse_resp_request(&buf, cmd);
        // incomplete trailing command = crash truncation
        if (consumed == 0){
            fprintf(stderr, "aof_load: truncated tail at offset %zu, ignoring\n", good_offset);
            break;
        }
        // corrupt frame
        if (consumed < 0){
            fprintf(stderr, "aof_load: malformed command at the offset %zu\n", good_offset);
            break;
        }
        buf_consume(&buf, (size_t)consumed);
        good_offset += (size_t)consumed;

        do_request(cmd, &sink, &fake);
        // we drain the replay so sink do not grow
        buf_consume(&sink, buf_size(&sink));
        replayed++;
    }
    buf_destroy(&buf);
    buf_destroy(&sink);
    g_data.g_loading = false;
    // replay isn't "unsaved work"
    g_data.g_writes_since_save = 0;

    fprintf(stderr, "aof_load: replayed %zu commands (%zu bytes)\n", replayed, good_offset);

    // crash-recovery: if there was a bad/partial tail, trim the file to the last good command
    if (good_offset < (size_t)sz){
        if (truncate(path, (off_t)good_offset) == 0){
            fprintf(stderr, "aof_load: truncated AOF to %zu good bytes\n", good_offset);
        }
    }
    return true;
}

bool aof_check(const char *path, bool fix){
  FILE *fp = fopen(path,  "rb");
  if (!fp){ fprintf(stderr, "check-aof: cannot open %s: %s\n", path, strerror(errno)); }

  fseek(fp, 0, SEEK_END);
  long sz = ftell(fp);
  fseek(fp, 0 , SEEK_SET);

  if (sz <= 0){ fclose(fp); fprintf(stderr, "check-aof: %s is empty\n", path); return true; }

  Buffer buf = buf_create((size_t)sz);
  std::vector<uint8_t> raw((size_t)sz);
  if (fread(raw.data(), 1, (size_t)sz, fp)){
    fprintf(stderr, "check-aof: short read\n"); fclose(fp); buf_destroy(&buf); return false;
  }
  fclose(fp);
  buf_append(&buf, raw.data(), (size_t)sz);

  size_t good_offset = 0, n = 0;
  const char *reason = nullptr;
  while (buf_size(&buf)){
    std::vector<std::string> cmd;
    int32_t consumed = parse_resp_request(&buf, cmd);
    if (consumed == 0){
      reason = "truncated trailing command";
      break;
    }
    // corrupt frame
    if (consumed < 0){
      reason = "malformed command"; 
      break;
    }
    buf_consume(&buf, (size_t)consumed);
    good_offset += (size_t)consumed;
    n++;
  }  
  buf_destroy(&buf);

  if (!reason){
    fprintf(stderr, "check-aof: OK - %zu commands, %ld bytes, no errors\n", n, sz);
    return true;
  }
  fprintf(stderr, "check-aof: %s at offset %zu (%zu of %ld bytes valid, %zu commands)\n",
         reason, good_offset, good_offset, sz, n);
  if (!fix){
    fprintf(stderr, "check-aof: re-rerun with --fix to truncate to the last valid command\n");
    return false;
  }
  if (truncate(path, (off_t)good_offset) == 0){
    fprintf(stderr, "check-aof: truncated %s to %zu bytes\n", path, good_offset);
    return true;
  }
  fprintf(stderr, "check-aof: truncate failed: %s\n", strerror(errno));
  return false;
}