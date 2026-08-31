#include "rdb.h"
#include "state.h"      
#include "buffer.h"
//#include "common.h"
//#include "hash.h"
//#include "set.h"
#include "aof.h"
#include "resp.h"
#include "commands.h"
#include <cstdlib>
#include <stdio.h> 
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <cerrno>
#include <vector>
#include <string_view>

// AOF
pid_t g_aof_child_pid = -1;

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

// // member callbacks (Same node types as your RDB callbacks)
// static bool cb_aof_zset(HNode *node, void *arg){
//   AofBatch *b = (AofBatch *)arg;
//   ZNode *z = container_of(node, &ZNode::hmap);
//   char sc[64];
//   int n = snprintf(sc, sizeof(sc), "%.17g", z->score);
//   b->args.emplace_back(sc, (size_t)n); // score
//   b->args.emplace_back(z->name, z->len); // member
//   aof_batch_maybe_flush(b);
//   return true;
// }

// static bool cb_aof_hash(HNode *node, void *arg){
//   AofBatch *b = (AofBatch *)arg;
//   HashNode *h = container_of(node, &HashNode::node);
//   b->args.emplace_back(h->field);
//   b->args.emplace_back(h->value);
//   aof_batch_maybe_flush(b);
//   return true;
// }

// static bool cb_aof_set(HNode *node, void *arg){
//   AofBatch *b = (AofBatch *)arg;
//   SetNode *s = container_of(node, &SetNode::node);
//   b->args.emplace_back(s->member);
//   aof_batch_maybe_flush(b);
//   return true;
// }

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
  rdb_build_aof_preamble(&buf);

  g_data.g_aof_rewrite_buf.clear(); // start the delta clean
  g_data.g_aof_rewrite_buf.reserve(64 * 1024);

  std::string tmp = g_config.aof_path + ".tmp";

  pid_t pid = fork();
  if (pid < 0){
    fprintf(stderr, "aof_rewrite: fork failed: %s\n", strerror(errno));
    buf_destroy(&buf);
    return;
  }
  if (pid == 0){
    child_close_inherited_fds();
    aof_write_snapshot(&buf, tmp.c_str()); // never returns
  }
  buf_destroy(&buf);
  g_aof_child_pid = pid; // from here, aof_feed mirrors writes into g_aof_rewrite_buf
  fprintf(stderr, "aof_rewrite: started (pid=%d)\n", pid);
}

// parent side, child hax exited: append the delta, swap files, repoint the live fd
static void aof_rewrite_reap(pid_t r, int status){
  std::string tmp = g_config.aof_path + ".tmp";
  bool ok = false;
  if (r != g_aof_child_pid){
    fprintf(stderr, "aof_rewrite: waitpid failed: %s\n", strerror(errno));
  } else if (!WIFEXITED(status)){
    fprintf(stderr, "aof_rewrite: child killed by signal %d, keeping old AOF\n", WTERMSIG(status));
  } else if (WEXITSTATUS(status) != 0){
    fprintf(stderr, "aof_rewrite: child exited %d, keeping old AOF\n", WEXITSTATUS(status));
  } else {
    int fd  = open(tmp.c_str(), O_WRONLY | O_APPEND);
    if (fd < 0){
      fprintf(stderr, "aof_rewrite: cannot open %s: %s\n", tmp.c_str(), strerror(errno));
    } else {
      const std::string &d = g_data.g_aof_rewrite_buf;
      size_t off = 0;
      bool werr = false;
      while (off < d.size()){
        ssize_t n = write(fd, d.data() + off, d.size() - off);
        if (n < 0){
          if (errno == EINTR){ continue; }
          fprintf(stderr, "aof_rewrite: delta write failed: %s\n", strerror(errno));
          werr = true;
          break;
        }
        off += (size_t)n;
      }
      if (!werr && fsync(fd) != 0){
        fprintf(stderr, "aof_rewrite: fsync failed: %s\n", strerror(errno));
        werr = true;
      }
      close(fd);
      if (!werr){
        if (rename(tmp.c_str(), g_config.aof_path.c_str()) != 0){
          fprintf(stderr, "aof_rewrite: rename failed: %s\n", strerror(errno));
        } else {
          // swap done - rpoint the live fd at the new, compacted file
          if (g_data.g_aof_fd >= 0){ close(g_data.g_aof_fd); }
          g_data.g_aof_fd = open(g_config.aof_path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
          if (g_data.g_aof_fd < 0){
            fprintf(stderr, "aof_rewrite: cannot open %s: %s\n", g_config.aof_path.c_str(), strerror(errno));
          }
          struct stat st;
          if (g_data.g_aof_fd >= 0 && fstat(g_data.g_aof_fd, &st) == 0){
            g_data.g_aof_current_size = (size_t)st.st_size;
            g_data.g_aof_base_size = (size_t)st.st_size;
          }
          ok = true;
          fprintf(stderr, "aof_rewrite: completed %zu delta bytes\n", d.size());
        }
      }
    }
  }
  if (!ok){ unlink(tmp.c_str()); }
  g_data.g_aof_last_rewrite_ok = ok;
  g_data.g_aof_rewrite_buf.clear();
  g_data.g_aof_rewrite_buf.shrink_to_fit();
  g_aof_child_pid = -1;

}

void aof_check_background_rewrite(){
  if (g_aof_child_pid == -1){ return; }
  int status = 0;
  pid_t r = waitpid(g_aof_child_pid, &status, WNOHANG);
  if (r == 0){ return; } // still running
  aof_rewrite_reap(r, status);
}

// shutdown: an in-flight rewrite is finished work - wait for it and finalize
void aof_rewrite_wait_shutdown(){
  if (g_aof_child_pid == -1){ return; }
  fprintf(stderr, "aof_rewrite: waiting for in-flight rewrite before shutdown\n");
  int status = 0;
  pid_t r = waitpid(g_aof_child_pid, &status, 0); // blocking
  aof_rewrite_reap(r, status);

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

    std::vector<uint8_t> raw((size_t)sz); 
    if (fread(raw.data(), 1, (size_t)sz, fp) != (size_t)sz){
        fprintf(stderr, "aof_load: short read\n");
        fclose(fp); 
        return false;
    }
    fclose(fp);

    size_t resp_offset = 0;
    if (sz >= 16 && memcmp(raw.data(), "MYAOFRDB", 8) == 0){
      uint64_t rdb_len = 0;
      memcpy(&rdb_len, raw.data() + 8, 8);
      if (rdb_len > (uint64_t)sz - 16){
        fprintf(stderr, "aof_load: truncated RDB preamble\n");
        return false;
      }
      if (!rdb_load_buffer(raw.data() + 16, (size_t)rdb_len)){
        fprintf(stderr, "aof_load; RDB preamble failed\n");
        return false;
      }
      resp_offset = 16 + (size_t)rdb_len;
      fprintf(stderr, "aof_load: RDB preamble %llu bytes, replaying RESP tail\n", (unsigned long long)rdb_len);
    }

    // build the resp vuffer from the tail only
    Buffer buf = buf_create(sz - resp_offset + 1);
    buf_append(&buf, raw.data() + resp_offset, sz - resp_offset);


    // reply setup -> suppress re-logging, bypass auth, discard replies
    g_data.g_loading = true;
    Conn fake{};
    // synthetic superuser so replay bypasses auth AND ACL — the log is trusted internal
    User replay_user;
    replay_user.name = "__aof_load__";
    replay_user.enable = true;
    replay_user.allow_cats = CAT_ALL;
    replay_user.all_keys = true;
    fake.user = &replay_user;
    Buffer sink = buf_create(4096);

    // bytes consumed by fully parsed commands
    size_t good_offset = 0;
    size_t replayed = 0;
    size_t replay_error = 0;
    size_t ttl_races = 0;
    std::string first_err, first_err_cmd;
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

        do_request(cmd, &sink, &fake, nullptr, 0);
        // an error reply during replay means memory is diverging from the log
        if (buf_size(&sink) && buf_data(&sink)[0] == '-'){
            // only rename/renamenx can hard-error on a missing key, expected not corruption
            bool ttl_race = (!cmd.empty() && (cmd[0] == "rename" || cmd[0] == "renamenx") &&
                   buf_size(&sink) >= 16 &&
                   memcmp(buf_data(&sink), "-ERR no such key", 16) == 0);
            if (ttl_race){
              ttl_races++;
            } else {
              if (replay_error == 0){
                size_t n = buf_size(&sink);
                if (n > 128){ n = 128; }
                first_err.assign((const char *)buf_data(&sink), n);
                while (!first_err.empty() && (first_err.back() == '\r' || first_err.back() == '\n')){
                  first_err.pop_back();
                }
                first_err_cmd = cmd.empty() ? "?" : cmd[0];
              }
              replay_error++;
          }
        }
        // we drain the replay so sink do not grow
        buf_consume(&sink, buf_size(&sink));
        replayed++;
    }
    buf_destroy(&buf);
    buf_destroy(&sink);
    g_data.g_loading = false;
    // fake is a stack Conn that never passes through conn_destroy
    pubsub_remove_conn(&fake);
    watch_clear_conn(&fake);
    repl_remove_conn(&fake);
    wait_remove_conn(&fake);
    // replay isn't "unsaved work"
    g_data.g_writes_since_save = 0;

    if (ttl_races){
      fprintf(stderr,
        "aof_load: %zu rename/renamenx skipped - source key's TTL elapsed between "
        "the original write and this replay (expected, not corruption)\n", ttl_races);
    }

    if (replay_error){
      fprintf(stderr,
        "aof_load: WARNING %zu of %zu replayed commands returned an error "
        "(first: %s -> %s). In-memory state does NOT match the AOF.\n",
        replay_error, replayed, first_err_cmd.c_str(), first_err.c_str());
    }

    // crash-recovery: if there was a bad/partial tail, trim the file to the last good command
    size_t good_total = resp_offset + good_offset;
    if (good_total < (size_t)sz){
        if (truncate(path, (off_t)good_total) == 0){
            fprintf(stderr, "aof_load: truncated AOF to %zu good bytes\n", good_total);
        }
    }
    fprintf(stderr, "aof_load: replayed %zu commands (%zu bytes)\n", replayed, good_offset);

    return replay_error == 0;
}

bool aof_check(const char *path, bool fix){
  FILE *fp = fopen(path,  "rb");
  if (!fp){ fprintf(stderr, "check-aof: cannot open %s: %s\n", path, strerror(errno)); return false; }

  fseek(fp, 0, SEEK_END);
  long sz = ftell(fp);
  fseek(fp, 0 , SEEK_SET);

  if (sz <= 0){ fclose(fp); fprintf(stderr, "check-aof: %s is empty\n", path); return true; }

  Buffer buf = buf_create((size_t)sz);
  std::vector<uint8_t> raw((size_t)sz);
  if (fread(raw.data(), 1, (size_t)sz, fp) != (size_t)sz){
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
