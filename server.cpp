// stdlib
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include <signal.h>
#include <string.h>
#include <pthread.h>
// system
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/tcp.h>
#include <sys/wait.h>   // for waitpid
#include <sys/stat.h>  // fstat
#include <sys/eventfd.h>
// C++
#include <string>
#include <vector>
#include <sstream>
#include <functional>
#include <utility>
#include <algorithm>
// project
#include "hashtable.h"
#include "common.h"
#include "list.h"
#include "heap.h"
#include "thread_pool.h"
#include "buffer.h"
#include "state.h"
#include "resp.h"
#include "rdb.h"
#include "commands.h"
#include "aof.h"
#include "sha256.h"
#include "cred.h"
#include "transport.h"

struct Listener {
  int fd;
  bool is_tls;
};  

// Forward declaration to use it in handle_accept
static void conn_set_timer(Conn *conn, ConnTimer type);

// worker constant 
static int g_loop_efd = -1;
static pthread_mutex_t g_loop_mu = PTHREAD_MUTEX_INITIALIZER;
static std::vector<std::function<void()>> g_loop_jobs;

// global flag — set to true when Ctrl+C is pressed
static bool g_stop = false;

static void signal_handler(int sig) {
    (void)sig;
    g_stop = true;
}

// operational failure (bad config, port taken, ...): report and exit cleanly
static void fatal_exit(const char *msg){
  int err =  errno;
  if (err){ fprintf(stderr, "fatal: %s (%s)\n", msg, strerror(errno)); }
  else { fprintf(stderr, "fatal: %s\n", msg); }
  exit(1);
}

// internal invariant violated: abort so there is a core dump to debug
static void panic(const char *msg){
  fprintf(stderr, "panic: [%d] %s\n", errno, msg);
  abort();
}

//Helper function for syscalls 
static void msg_errno(const char *msg) {
  fprintf(stderr, "[errno:%s]\n", msg);
}

static void fd_set_nb(int fd){
  errno = 0;
  int flags = fcntl(fd, F_GETFL,0);
  if (errno){
    fatal_exit("fcntl error");
    return;
  }
  flags |= O_NONBLOCK;

  errno = 0;
  (void) fcntl(fd, F_SETFL, flags);
  if (errno){ fatal_exit("fcntl error"); }
}

static void conn_destroy(Conn *conn){
  tr_close(conn);
  pubsub_remove_conn(conn); // drop from every channel before the Conn dies
  watch_clear_conn(conn);
  g_data.fd2conn[conn->fd] = NULL;
  dlist_detach(&conn->idle_node);
  delete conn;
  g_data.connected_clients--;
}

// callable from any worker thread
void loop_post(std::function<void()> fn){
  pthread_mutex_lock(&g_loop_mu);
  g_loop_jobs.push_back(std::move(fn));
  pthread_mutex_unlock(&g_loop_mu);
  uint64_t one = 1;
  (void)!write(g_loop_efd, &one, sizeof(one));
}


// main thread only
static void loop_drain(){
  uint64_t n = 0;
  // clears the counter
  (void)!read(g_loop_efd, &n, sizeof(n)); 
  std::vector<std::function<void()>> jobs;
  pthread_mutex_lock(&g_loop_mu);
  // run outside the lock
  jobs.swap(g_loop_jobs);
  pthread_mutex_unlock(&g_loop_mu);
  for (auto &fn : jobs){ fn(); }
}

// callback when the socket is ready
static int32_t handle_accept(int fd, bool is_tls){
  // accept logic
  struct sockaddr_in client_addr =  {};
  socklen_t addrlen = sizeof(client_addr);

  int connfd = accept(fd, (struct sockaddr *)&client_addr, &addrlen);
  if (connfd < 0) {
    if (errno != EAGAIN){ msg_errno("accept() error"); }
    return -1;
  }

  uint32_t peer_host = ntohl(client_addr.sin_addr.s_addr);

  char peerbuf[32];
  snprintf(peerbuf, sizeof(peerbuf), "%u.%u.%u.%u:%u", 
    (peer_host >> 24) & 255, (peer_host >> 16) & 255, (peer_host >> 8) & 255, peer_host & 255,
    ntohs(client_addr.sin_port));
  std::string peer = peerbuf;

  // IP allowlist (loopback always allowed; empty list = allowed all)
  if (!ip_allowed(peer_host)){
    audit_reject(peer, "allowlist");
    fprintf(stderr, "rejected %s: not in allowlist\n", inet_ntoa(client_addr.sin_addr));
    close(connfd);
    // keep acepting other pending conections
    return 0;
  }

  // protected mode: no password + remote peer -> refuse witn an explanation
  if (g_config.protected_mode && g_config.password.empty() && !ip_is_loopback(peer_host)){
    audit_reject(peer, "protected-mode");
    static const char deny[] = 
      "-DENIED MYRED is in protected mode with no password set. "
      "Connect from localhost, set 'requirepass', or 'protected-mode no'.\r\n";
    (void)!write(connfd, deny, sizeof(deny) - 1);
    close(connfd);
    return 0;
  }

  uint32_t ip = client_addr.sin_addr.s_addr;
  fprintf(stderr, "new client from %u.%u.%u.%u:%u\n",
    ip & 255, (ip >> 8) & 255, (ip >> 16) & 255, ip >> 24,
    ntohs(client_addr.sin_port)
  );

  fd_set_nb(connfd);  // now we set the new connection to namb 
  // we create a new conn struct 
  Conn *conn = new Conn();
  conn->fd = connfd;
  conn->id = g_data.next_conn_id++;
  conn->peer = peer;
  conn->user = acl_initial_user(); 
  conn->want_read = true;
  conn->incoming = buf_create(64 * 1024);
  conn->outgoing = buf_create(64 * 1024);
  conn->timer_type = ConnTimer::IO;
  conn->last_active_ms = get_monotonic_msec();
  dlist_insert_before(&g_data.io_list, &conn->idle_node);

  //put it into the map
  if (g_data.fd2conn.size() <= (size_t)conn->fd){
    // resize if neccesary
    g_data.fd2conn.resize(conn->fd + 1);
  }
  assert(!g_data.fd2conn[conn->fd]);
  // return the conn 
  g_data.g_total_connections++;
  g_data.connected_clients++;
  g_data.fd2conn[conn->fd] = conn;

  if (is_tls){
    if (!tr_tls_attach(conn)){
      conn_destroy(conn); // SSL_new failed, wew drop this conn only
      return 0; 
    }
    conn->tls_handshaking = true;
    // transport owns this conn until the handshake completes
    conn->want_read = false;
    conn->tr_want_read = true;
    conn_set_timer(conn, ConnTimer::HANDSHAKE); // moves from io_list to hs_list
  }
  return 0;
}

static int listen_on(const std::string &addr, int port){
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0){ fprintf(stderr, "socket(): %s\n", strerror(errno)); return -1; }

  int val = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &val, sizeof(val));
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &val, sizeof(val));

  struct sockaddr_in a = {};
  a.sin_family = AF_INET;
  a.sin_port   = htons((uint16_t)port);
  if (inet_pton(AF_INET, addr.c_str(), &a.sin_addr) != 1){
    fprintf(stderr, "invalid bind address '%s'\n", addr.c_str()); close(fd); return -1;
  }
  if (bind(fd, (const struct sockaddr *)&a, sizeof(a)) != 0){
    fprintf(stderr, "bind %s:%d %s\n", addr.c_str(), port, strerror(errno)); 
    close(fd);
    return -1;
  }
  fd_set_nb(fd);
  if (listen(fd, SOMAXCONN)){
    fprintf(stderr, "listen %s:%d: %s\n", addr.c_str(), port, strerror(errno));
    close(fd);
    return -1;
  }
  return fd;
}

// Timers logic 
static int32_t next_timer_ms() {
  if (g_data.g_evict_pending){ return 0; }
  uint64_t now_ms = get_monotonic_msec();
  uint64_t next_ms = (uint64_t)-1; // maximun value

  // check the front of the idle_list 
  if (!dlist_empty(&g_data.idle_list)){
    Conn *conn = container_of(g_data.idle_list.next, &Conn::idle_node);
    next_ms = std::min(next_ms, conn->last_active_ms + k_idle_timeout_ms);
  }
  // check the front of the io_list 
  if (!dlist_empty(&g_data.io_list)){
    Conn *conn = container_of(g_data.io_list.next, &Conn::idle_node);
    next_ms = std::min(next_ms, conn->last_active_ms + k_io_timeout_ms);
  }
  // check the front of the hs_list
  if (!dlist_empty(&g_data.hs_list)){
    Conn *conn = container_of(g_data.hs_list.next, &Conn::idle_node);
    next_ms = std::min(next_ms, conn->last_active_ms + (uint64_t)g_config.tls_handshake_timeout_ms);
  }
  // check the heap
  if (!g_data.heap.empty()){
    next_ms = std::min(next_ms, g_data.heap[0].val);
  }
  // check the periodic wake up
  if (g_data.g_writes_since_save > 0){
    uint64_t soonest = (uint64_t)-1;
    for (const SaveCondition &sc : g_config.save_conditions){
      if (g_data.g_writes_since_save >= sc.changes){
        soonest = std::min(soonest, g_data.g_last_save_ms + sc.seconds * 1000);
      }
    }
    if (soonest != (uint64_t)-1){
      next_ms = std::min(next_ms, soonest);
    }
  }

  if (g_rdb_child_pid != -1 || g_aof_child_pid != -1){
    next_ms = std::min(next_ms, now_ms + 100);
  }

  // no timers 
  if (next_ms == (uint64_t)-1){ return -1; }
  // already expired
  if (next_ms <= now_ms){ return 0; }
  // rare case idk ??
  if (next_ms == UINT64_MAX) {return -1;}

  uint64_t wait = next_ms - now_ms;
  if (wait > (uint64_t)INT32_MAX){ return INT32_MAX; }
  return (int32_t)wait;
}

static bool aof_flush(){
  std::string &buf = g_data.g_aof_buf;
  if (g_data.g_aof_fd < 0 || buf.empty()){ return false; }
  size_t off = 0;
  bool err = false;
  while (off < buf.size()){
    ssize_t rv = write(g_data.g_aof_fd, buf.data() + off, buf.size() - off);
    if (rv < 0){
      if (errno == EINTR){ continue; }
      if (!g_data.g_aof_write_err){
        // log only on the transition into the error state
        fprintf(stderr, "aof: write failed: %s - refusing writes until recovery\n", strerror(errno));
      }
      err = true;
      break; // keep remaining bytes buffered, retry the next tick
    }
    off += (size_t)rv;
  }
  buf.erase(0, off); // drop only what was actually written
  g_data.g_aof_current_size += off; // we tracked with no syscall

  if (err){
    g_data.g_aof_write_err =true;
  } else if (g_data.g_aof_write_err && buf.empty()){
    fprintf(stderr, "aof: write recovered, accepting writes again\n");
    g_data.g_aof_write_err = false;
  }
  return off > 0;
}

static void aof_fsync_job(void *arg){
  (void)arg;
  if (g_data.g_aof_fd >= 0){
    fdatasync(g_data.g_aof_fd);
  }
  // This guarantees that the other threads see the fdatasync done
  g_data.g_aof_fsync_pending.store(false, std::memory_order_release);
}

static void process_timers(){
  uint64_t now_ms = get_monotonic_msec();
  g_data.g_lru_clock = (uint32_t)((now_ms / 1000) & LRU_CLOCK_MAX);
  // This handles expired idle timers
  while (!dlist_empty(&g_data.idle_list)){
    Conn *conn = container_of(g_data.idle_list.next, &Conn::idle_node);
    uint64_t expired = conn->last_active_ms + k_idle_timeout_ms;
    if (expired > now_ms){
      //expired
      break;
    }
    fprintf(stderr, "Idle timeout: closing fd %d\n", conn->fd);
    conn->want_close = true;
    conn_destroy(conn);
  }
  // This handles expired io timers
  while (!dlist_empty(&g_data.io_list)){
    Conn *conn = container_of(g_data.io_list.next, &Conn::idle_node);
    uint64_t expired = conn->last_active_ms + k_io_timeout_ms;
    if (expired > now_ms){
      //expired
      break;
    }
    fprintf(stderr, "IO timeout: closing fd %d\n", conn->fd);
    conn->want_close = true;
    conn_destroy(conn);
  }
  // a TCP connect thet never speaks TLS must not hold a slot for 30s
  while (!dlist_empty(&g_data.hs_list)){
    Conn *conn = container_of(g_data.hs_list.next, &Conn::idle_node);
    uint64_t expired = conn->last_active_ms + (uint64_t)g_config.tls_handshake_timeout_ms;
    if (expired > now_ms){ 
      break; 
    }
    fprintf(stderr, "TLS handshake timeout: closing fd %d\n", conn->fd);
    audit_event("tls_handshake_timeout", conn, "");
    conn_destroy(conn);
  }

  // TTL timers using a heap
  const size_t k_max_works = 2000;
  size_t nworks = 0;
  const std::vector<HeapItem> &heap = g_data.heap;
  // This handles TTL timers
  while(!heap.empty() && heap[0].val <= now_ms){
    Entry *ent = container_of(heap[0].ref, &Entry::heap_idx);
    HNode *node = hm_delete(&g_data.db, &ent->node, &hnode_same);
    assert(node == &ent->node);
    (void)node; // assert vanishes in release (NDEBUG)
    fprintf(stderr, "key expired: %s\n", ent->key.c_str());
    notify_keyspace_event(NOTIFY_EXPIRED, "expired", ent->key);
    entry_del(ent);
    if (nworks++ >= k_max_works){
      break;
    }
  }
  // periodic RDB save
  
  if ( g_rdb_child_pid == -1 && g_data.g_writes_since_save > 0){
    uint64_t elapsed_ms = now_ms - g_data.g_last_save_ms;
    for (const SaveCondition &sc : g_config.save_conditions){
      if (g_data.g_writes_since_save >= sc.changes && elapsed_ms >= sc.seconds * 1000){
        fprintf(stderr, "save: %u changes within %llus -> bgsave\n", 
                 g_data.g_writes_since_save, (unsigned long long)sc.seconds);
        // gate against retry-storm if the save fails
        g_data.g_last_save_ms = now_ms;
        // snapshots g_dirty_at_save internally already
        rdb_save_background();
        // one save per tick
        break;
      }
    }
  }
  // periodic AOF fsync (everysec mode)
  if (g_config.aof_enable && g_config.aof_fysnc  == Aoffsync::EVERYSEC){
    if (now_ms - g_data.g_aof_last_fsync_ms >= 1000){
      g_data.g_aof_last_fsync_ms  = now_ms;
      bool expected = false;
      if (g_data.g_aof_fsync_pending.compare_exchange_strong(expected, true)){
        thread_pool_queue(&g_data.thread_pool, aof_fsync_job, nullptr);
      }
      // else previous fdatasync still in  flight, we skip and try again next second
    }
  }

  // auto AOF rewrite
  if (g_config.aof_enable && g_aof_child_pid == -1 && g_rdb_child_pid == -1 && now_ms  - g_data.g_aof_check_ms >= 1000){
    g_data.g_aof_check_ms = now_ms;

    size_t cur = g_data.g_aof_current_size;
    size_t base = g_data.g_aof_base_size;
    if (cur >= g_config.aof_rewrite_min_size){
      // base == 0 -> no prior rewwrite -> treat as 100% grown so we establish a baseline
      long long growth = (base != 0) ? (long long)((cur - base) * 100 / base) : 100;
      if (growth >= g_config.aof_rewrite_perc){
        fprintf(stderr, "aof_rewrite: auto-trigger (size=%zu base=%zu growth=%lld%%)\n",
                cur, base, growth);
        aof_rewrite_background();
      }
    }
  }
}

static void conn_set_timer(Conn *conn, ConnTimer type){
  dlist_detach(&conn->idle_node);

  // record when it joined the new list
  conn->timer_type = type;
  conn->last_active_ms = get_monotonic_msec();

  // insert at the back
  if (type == ConnTimer::IDLE){
    dlist_insert_before(&g_data.idle_list, &conn->idle_node);
  } else if (type == ConnTimer::HANDSHAKE){
    dlist_insert_before(&g_data.hs_list, &conn->idle_node);
  } else {
    dlist_insert_before(&g_data.io_list, &conn->idle_node);
  }
}

// Process cmds functions

// we will try to proccess if theres enough data
static bool try_one_request(Conn *conn){
  // nothing runs with the pre-auth identity
  if (conn->auth_pending){ return false; }

  std::vector<std::string> cmd;
  int32_t consumed = parse_resp_request(&conn->incoming, cmd);

  if (consumed == 0) { return false; }
  if (consumed < 0){
    fprintf(stderr, "bad RESP from fd %d\n", conn->fd);
    buf_append(&conn->outgoing, "-ERR Protocol error\r\n", 21);
    conn->want_close = true;
    return false;
  }

  // empty inline line (bare \r\n)
  if (cmd.empty()){
    buf_consume(&conn->incoming, (size_t)consumed);
    return true;
  }

  // capture the raw frame BEFORE consuming/dispatching. logged verbatim for aof
  const char *raw = (const char *)buf_data(&conn->incoming);
  size_t raw_len = (size_t)consumed;

  g_data.g_total_commands++;
  do_request(cmd, &conn->outgoing, conn, raw, raw_len);

  // moved after do_request, raw must stay valid
  buf_consume(&conn->incoming, raw_len);
  if (conn->auth_pending){
    conn->want_read = false;
    conn->want_write = false;  
    return false; 
  }
  conn->want_read = false;
  conn->want_write = true;
  return true;
}

static void handle_tls_handshake(Conn *conn){
  IoResult r = tr_handshake(conn);
  // POLL again
  if (r == IoResult::WANT_READ || r == IoResult::WANT_WRITE){ return; }
  if (r != IoResult::OK){
    audit_event("tls_handshake_fail", conn, " reason=" + tr_tls_error());
    conn->want_close = true;
    return;
  }
  // tunnel up: hand the conn to the application in its normal read-intent state
  conn->tls_handshaking = false;
  conn->want_read = true;
  conn->want_write = false;
  conn_set_timer(conn, ConnTimer::IO);
}

static void handle_write(Conn *conn){
  assert(buf_size(&conn->outgoing) > 0);
  size_t n = 0;
  IoResult r = tr_write(conn, buf_data(&conn->outgoing), buf_size(&conn->outgoing), &n);
  if (r == IoResult::WANT_READ || r == IoResult::WANT_WRITE){ return; }
  if (r != IoResult::OK){ 
    conn->want_close = true;
    return;
   }

  // remove the data from outgoing
  buf_consume(&conn->outgoing, n);

  if (buf_size(&conn->outgoing) == 0){ // all data writen 
    conn->want_read = true;
    conn->want_write = false;
    conn_set_timer(conn, ConnTimer::IDLE); 
  }// want to keep writing 
}

static void handle_read(Conn *conn){
  // leaving idle as soon as any byte arrives
  if (conn->timer_type == ConnTimer::IDLE){
    conn_set_timer(conn, ConnTimer::IO);
  }

  // Drain everything currently decryptable.
  for (;;){
    uint8_t buf [64 * 1024];
    size_t n = 0;
    IoResult r = tr_read(conn, buf, sizeof(buf), &n);    
    if (n > 0){ buf_append(&conn->incoming, buf, n); }

    // a client that streams framing without ever completing a command must not grow us unbounded
    if (buf_size(&conn->incoming) > k_max_incoming){
      fprintf(stderr, "fd %d: incoming buffer over %zu bytes, closing\n", conn->fd, k_max_incoming);
      buf_append(&conn->outgoing, "-ERR Protocol error: input buffer exceeded\r\n", 44);
      conn->want_close = true;
      return;
    }

    if (r == IoResult::OK){
      // more buffered records, keep draining
      if (tr_has_pending(conn)){ continue; }
      break;
    }
    // poll again
    if (r == IoResult::WANT_READ || r == IoResult::WANT_WRITE){ break; }
    conn->want_close = true; // PEER_CLOSED OR ERR
    return;
  }

  while (try_one_request(conn)) {}

  if(buf_size(&conn->outgoing) > 0){
    conn->want_read = false;
    conn->want_write = true;
    // this is a optimization 
    return handle_write(conn);
  } // else wants to keep reading.
}

// After an async completion wrote a reply: drain any request that were buffered
// while gated, then flush - the mirror of handle_read's tail.
void conn_resume(Conn *conn){
  if (!conn->want_close){
    while (try_one_request(conn)){}
  }
  if (buf_size(&conn->outgoing) > 0){
    conn->want_read = false;
    conn->want_write =true;
    handle_write(conn); // same as handle_read
  } else {
    conn->want_read = true;
    conn->want_write = false;
  }
  // poll loop won't see this conn's flags otherwise
  if (conn->want_close){ conn_destroy(conn); }
}

int main(int argc, char **argv){
  // control for Ctrl-c
  signal(SIGINT,  signal_handler);
  signal(SIGTERM, signal_handler);
  signal(SIGXFSZ, SIG_IGN);
  signal(SIGPIPE, SIG_IGN);

  for (int i = 1; i < argc; ++i){
    if (strcmp(argv[i], "--check-aof") == 0){
      bool fix = false;
      const char *path = "appendonly.aof";
      for (int j = i + 1; j < argc; ++j){
        if (strcmp(argv[j], "--fix") == 0){ fix = true; }
        else { path = argv[j]; }
      }
      return aof_check(path, fix) ? 0 : 1;
    }
  }

  // Timers start
  g_data.g_server_start_ms = get_monotonic_msec();
  g_data.g_last_save_ms = g_data.g_server_start_ms;

  g_data.g_aof_last_fsync_ms = g_data.g_server_start_ms;
  g_data.g_aof_check_ms      = g_data.g_server_start_ms;


  // initialiaze idle connection list and io waiting list and tls handshake list
  dlist_init(&g_data.idle_list);
  dlist_init(&g_data.io_list);
  dlist_init(&g_data.hs_list);

  thread_pool_init(&g_data.thread_pool, 8);

  const char *cfg_path = getenv("MYRED_CONFIG");
  for (int i = 1; i < argc; ++i){
    if (argv[i][0] != '-'){ 
      cfg_path = argv[i];
      break; 
    } 
  }
  
  if (cfg_path){ fprintf(stderr, "startup: loading config %s\n", cfg_path); }
  if (cfg_path && !config_load_file(cfg_path)){ fatal_exit("invalid config file"); }


  if (const char *e = getenv("MYRED_PASSWORD")){ g_config.password = cred_hash_new(e); }
  if (const char *e = getenv("MYRED_PORT")){
    long p = 0;
    if (parse_int_strict(e, &p) && p > 0 && p < 65536){ g_config.port = (int)p; }
    else { fprintf(stderr, "warning: bad MYRED_PORT '%s' ignored\n", e); }
  }
  if (const char *e = getenv("MYRED_AOF")){ g_config.aof_enable = (e[0] == '1' || e[0] == 'y'); }

  #ifndef MYRED_HAVE_ARGON2
    fprintf(stderr, "startup: build without libargon2 - new password hashes fall back to lgeacy SHA-256\n");
  #endif

  acl_bootstrap_default();
  acl_init_categories(); 
  metadata_selfcheck();    
  dispatch_build();
  repl_init(); // it sizes the backlog from repl_backlog_size

  // AOF takes priority over RDB
  bool aof_exists = (access(g_config.aof_path.c_str(), F_OK) == 0);
  bool rdb_exists = (access(g_config.dump_path.c_str(), F_OK) == 0);

  if (g_config.aof_enable && aof_exists){
    if (rdb_exists){
      fprintf(stderr, "startup: AOF and RDB both present -> loading AOF, ignording RDB\n");
    }
    if (!aof_load(g_config.aof_path.c_str())){
      fatal_exit("AOF load failed - refusing to serve partial data "
                 "(inspect/repair with: ./build/server --check-aof --fix)");
     }
  } else if (rdb_exists){
    if (!rdb_load(g_config.dump_path.c_str())){
      fprintf(stderr, "startup: RDB load failed, starting empty\n");
    }
  } else {
    fprintf(stderr, "startup: no persistence file, starting empty\n");
  }
  
  g_data.g_aof_buf.reserve(64 * 1024);

  if (g_config.aof_enable){
    g_data.g_aof_fd = open(g_config.aof_path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (g_data.g_aof_fd < 0){
      fprintf(stderr, "fatal: cannot open AOF %s: %s\n", g_config.aof_path.c_str(), strerror(errno));
      return 1;
    }
    struct stat st;
    if (fstat(g_data.g_aof_fd, &st) == 0){
      g_data.g_aof_current_size  = (size_t)st.st_size;
      g_data.g_aof_base_size  = (size_t)st.st_size;
    }
  }

  const char *save_env = getenv("MYRED_SAVE");
  if (save_env){
    g_config.save_conditions.clear();           // "" disables automatic saves entirely
    std::istringstream iss(save_env);
    uint64_t sec; uint32_t ch;
    while (iss >> sec >> ch){
      g_config.save_conditions.push_back({sec, ch});
    }
  }

  
  const char *fsync_env = getenv("MYRED_AOF_FSYNC");
  if (fsync_env){
    if (strcmp(fsync_env, "always") == 0){ g_config.aof_fysnc = Aoffsync::ALWAYS; }
    else if (strcmp(fsync_env, "no") == 0){ g_config.aof_fysnc = Aoffsync::NO; }
    else { g_config.aof_fysnc = Aoffsync::EVERYSEC; }
  }

  const char *rmin_env = getenv("MYRED_AOF_REWRITE_MIN");
  if (rmin_env){
    long v = 0;
    if (parse_int_strict(rmin_env, &v) && v > 0){ g_config.aof_rewrite_min_size = (size_t)v; }
    else { fprintf(stderr, "warning: bad MYRED_AOF_REWRITE_MIN '%s' ignored\n", rmin_env); }
  }

  const char *rperc_env = getenv("MYRED_AOF_REWRITE_PERC");
  if (rperc_env){
    long v = 0;
    if (parse_int_strict(rperc_env, &v) && v > 0){ g_config.aof_rewrite_perc = (int)v; }
    else { fprintf(stderr, "warning: bad MYRED_AOF_REWRITE_PERC '%s' ignored\n", rperc_env); }
  }

  const char *maxmen_env = getenv("MYRED_MAXMEMORY");
  if (maxmen_env){
    size_t bytes = 0;
    if (parse_memory_size(maxmen_env, &bytes)){ g_config.maxmemory = bytes; }
    else { fprintf(stderr, "warning: bad MYRED_MAXMEMORY '%s'\n", maxmen_env); }
  }
  const char *policy_env = getenv("MYRED_MAXMEMORY_POLICY");
  if (policy_env){
    MaxmemoryPolicy p;
    if (parse_maxmemory_policy(policy_env, &p)){ g_config.maxmemory_policy = p; }
    else { fprintf(stderr, "warning: bad MYRED_MAXMEMORY_POLICY '%s'\n", policy_env); }
  }

  {
    std::string tls_err;
    if (!tr_tls_init(tls_err)){ fatal_exit(tls_err.c_str()); }
  }

  std::vector<Listener> listeners;
  for (const std::string &addr : g_config.binds){
    int lfd = listen_on(addr, g_config.port);
    // fail fast if any addrees can't bind 
    if (lfd < 0){ fatal_exit("listener setup"); }
    listeners.push_back({lfd, false});
    fprintf(stderr,"listening on %s:%d\n", addr.c_str(), g_config.port);
  }
  if (g_config.tls_port != 0){
    for (const std::string &addr : g_config.binds){
      int lfd = listen_on(addr, g_config.tls_port);
      if (lfd < 0){ fatal_exit("tls listener setup"); }
      listeners.push_back({lfd, true});
      fprintf(stderr, "listening on %s:%d (TLS)\n", addr.c_str(), g_config.tls_port);
    }
  }

  // pre-warm and pay the KDF cost at boot
  (void)cred_dummy(); 
  g_loop_efd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
  if (g_loop_efd < 0){ fatal_exit("eventfd"); }

  const size_t nlisten = listeners.size();
  std::vector<struct pollfd> poll_args; // This a vector of structs for arguments for poll_args

  while(!g_stop){
    
    poll_args.clear(); //This just clean the arguments for poll.
    // listen fds occupy [0, nlisten]; eventfd at [nlisten]; conns after
    for (const Listener &l : listeners){
      poll_args.push_back({l.fd, POLLIN, 0});
    }

    poll_args.push_back({ g_loop_efd, POLLIN, 0});

    for (Conn *conn : g_data.fd2conn){
      if(!conn){continue;}
      struct pollfd pfd = {conn->fd, POLLERR, 0}; // This is for the flags of the aplication
      if (conn->want_read || conn->tr_want_read)  { pfd.events |= POLLIN; }
      if (conn->want_write || conn->tr_want_write){ pfd.events |= POLLOUT; }
      poll_args.push_back(pfd);
    }

    int32_t timeout_ms = next_timer_ms();
    int rv = poll(poll_args.data(), (nfds_t)poll_args.size(), timeout_ms);

    if(rv < 0 && errno == EINTR){continue;}
    if(rv < 0){ panic("poll");}

    // any listening socket ready -> drain its backlog
    for (size_t i = 0; i < nlisten; i++){
      if (poll_args[i].revents) {
        // we accept on that fd
        while (handle_accept(listeners[i].fd, listeners[i].is_tls) == 0) {}
      }
    }

    // run completetions
    if (poll_args[nlisten].revents & POLLIN){ loop_drain(); }

    //This is for to handle the connections of sockets
    for (size_t i = nlisten + 1; i < poll_args.size(); i++){ // we skip the 1st
      uint32_t ready = poll_args[i].revents;
      if (ready == 0){ // no events fired up
        continue;
      }


      // retrieve the object of every fd (in this case only i)
      Conn *conn = g_data.fd2conn[poll_args[i].fd];
      if (!conn){continue;}

      // update the idle timer and putting the conn at the end of the list
      conn_set_timer(conn, conn->timer_type);

      if (conn->tls_handshaking){
        if (ready & (POLLIN | POLLOUT)){ handle_tls_handshake(conn); }
        if ((ready & POLLERR) || conn->want_close){ conn_destroy(conn); }
        // no data path until the handshake is done
        continue;
      }

      if (ready & (POLLIN | POLLOUT)){
        if      (conn->want_read) { handle_read(conn); }
        else if (conn->want_write){ handle_write(conn); }
      }

      //Close the socket from erros 
      if((ready & POLLERR) || conn->want_close){
        conn_destroy(conn);

      }
    } // this if for each connection socket (fd)
    bool wrote = aof_flush();
    if (wrote && g_config.aof_fysnc == Aoffsync::ALWAYS){
      if (fdatasync(g_data.g_aof_fd) < 0){
        fprintf(stderr, "aof: fdatasync failed: %s\n", strerror(errno));
      }
    }
    // handle timers
    process_timers();
    evict_tick();
    rdb_check_background_save();
    aof_check_background_rewrite();
  }
  thread_pool_destroy(&g_data.thread_pool);

  if (g_data.g_aof_fd >= 0){
    aof_flush();
    fdatasync(g_data.g_aof_fd);
  }
  aof_rewrite_wait_shutdown();
  if (g_data.g_aof_fd >= 0){ close(g_data.g_aof_fd); }
  fprintf(stderr, "Shutting down, saving...\n");

  // if a background save is running, wait for it first
  if (g_rdb_child_pid != -1){
    int status = 0;
    waitpid(g_rdb_child_pid, &status, 0); // blocking wait
    g_rdb_child_pid = -1;
  }

  rdb_save(g_config.dump_path.c_str());
  fprintf(stderr, "Saved. Goodbye.\n");
  return 0;
}