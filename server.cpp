// stdlib
#include <assert.h>
#include <cstdint>
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
#include <string_view>
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
// #include "sha256.h"
#include "cred.h"
#include "transport.h"

struct Listener {
  int fd;
  bool is_tls;
};  

// Forward declaration to use it in handle_accept
static void conn_set_timer(Conn *conn, ConnTimer type);

// Forward declaration to use it in conn_destroy
static void repl_link_lost(Conn *conn);

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
  repl_remove_conn(conn);
  wait_remove_conn(conn);   // waiters holds raw Conn* too
  repl_link_lost(conn);
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

// Replication side (putting this here beacause a lot of code goes in here)

// Privileged identity for applying the replication stream. same as aof replay user
static User *reply_apply_user(){
  // we build this onece under concurrent calls
  static User u = []{
    User x;
    x.name = "__replication__";
    x.enable = true;
    x.allow_cats = CAT_ALL;
    x.all_keys = true;
    x.all_channels = true;
    return x;
  }();
  return &u;
}

// The master closed the link, or it errored. 
static void repl_link_lost(Conn *conn){
  if (!conn->is_master_link){ return; }
  fprintf(stderr, "replication: lost link to master %s:%d\n",
           g_data.master_host.c_str(), g_data.master_port);
  conn->is_master_link = false;
  g_data.master_link = nullptr;
  g_data.repl_state = ReplState::NONE; // no auto-reconnect yet
  g_data.repl_rdb_left = 0;
  g_data.repl_rdb_buf.clear();
  g_data.repl_rdb_buf.shrink_to_fit();
}

void repl_stop(){
  if (g_data.master_link){
    Conn *c = g_data.master_link;
    c->is_master_link = false;
    g_data.master_link = nullptr;
    conn_destroy(c);
  }
  g_data.replica_mode = false;
  g_data.repl_state = ReplState::NONE;
  g_data.master_host.clear();
  g_data.master_port = 0;
  g_data.repl_rdb_left = 0;
  g_data.repl_rdb_buf.clear();
  g_data.repl_rdb_buf.shrink_to_fit();
}

static void failover_reset(const std::string &why) {
  fprintf(stderr, "failover: %s\n", why.c_str());
  g_data.failover_state =FailoverState::NONE;
  g_data.failover_host.clear();
  g_data.failover_port = 0;
  g_data.failover_deadline_ms = 0;
  g_data.failover_force = false;
}

bool repl_start(const std::string &host, int port, std::string &err){
  const bool have_history =
      (g_data.replica_mode || g_data.failover_state == FailoverState::IN_PROGRESS) &&
      !g_data.repl_id.empty();
  const std::string hist_id = g_data.repl_id;
  const uint64_t hist_off = g_data.master_repl_offset;

  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0){ err = std::string("Socket (): ") + strerror(errno); return false; } 

  struct sockaddr_in a = {};
  a.sin_family = AF_INET;
  a.sin_port = htons((uint16_t)port);
  if (inet_pton(AF_INET, host.c_str(), &a.sin_addr) != 1){
    close(fd); err = "invalid master address '" + host + "'"; return false;
  }
  int val = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &val, sizeof(val));
  fd_set_nb(fd);

  if (connect(fd, (const struct sockaddr *)&a, sizeof(a)) != 0 && errno != EINPROGRESS){
    err = std::string("connect(): ") + strerror(errno); close(fd); return false;
  }

  // Only now tear down the old role: every early return above must leave the instance exactly as it was
  repl_stop();  

  Conn *c = new Conn();
  c->fd = fd;
  c->id = g_data.next_conn_id++;
  c->peer = host + ":" + std::to_string(port);
  c->user = reply_apply_user();
  c->is_master_link = true;
  c->incoming = buf_create(64 * 1024);
  c->outgoing = buf_create(64 * 1024); 
  c->want_read = true;
  c->want_write = true;
  // Deliberately in NO timer list: an idle master link is healthy, and the
  // io/idle sweeps would reap it after 30s. dlist_init keeps conn_destroy's
  // detach safe on a node that was never inserted anywhere.

  dlist_init(&c->idle_node);

  if (g_data.fd2conn.size() <= (size_t)fd){ g_data.fd2conn.resize(fd + 1); }
  assert(!g_data.fd2conn[fd]);
  g_data.fd2conn[fd] = c;
  g_data.connected_clients++; // balances conn_Destroy's unconditional decrement

  // Queue the whole handshake at once, we control both ends and the replies 
  // are consumed in order by the state machine below
  std::string hs;
  std::string myport = std::to_string(g_config.port);
  if (!g_config.masterauth.empty()){
    aof_encode(hs, { "AUTH", std::string_view(g_config.masterauth) });
  }
  aof_encode(hs, { "REPLCONF", "listening-port", std::string_view(myport) });
  // PSYNC <replid> <first byte we still need>. "? -1" means "no history, send it all".
  const std::string psync_off = std::to_string(hist_off + 1);
  if (g_data.failover_state == FailoverState::IN_PROGRESS){
    aof_encode(hs, { "PSYNC", std::string_view(hist_id), std::string_view(psync_off), "FAILOVER" });  
  } else if (have_history){
    aof_encode(hs, { "PSYNC", std::string_view(hist_id), std::string_view(psync_off)});  
  } else {
    aof_encode(hs, { "PSYNC", "?", "-1"});
  }
  buf_append(&c->outgoing, hs.data(), hs.size());

  g_data.master_host = host;
  g_data.master_port = port;
  g_data.replica_mode = true; // role 
  g_data.repl_state = ReplState::HANDSHAKE;
  g_data.master_link = c;
  g_data.master_last_io_ms = get_monotonic_msec(); // from the dial, not from the first byte
  fprintf(stderr, "replication: connecting to master %s (%s)\n", c->peer.c_str(),
          have_history ? "attempting partial resync" : "full resync");
  return true;
}

// Periodic, from proccess_timers, a replica whose link died re-dials its master
static void repl_cron(uint64_t now_ms){
  const uint64_t timeout_ms = (uint64_t)g_config.repl_timeout_ms;

  if (!g_data.replicas.empty() && g_config.repl_ping_period_ms > 0 && now_ms >= g_data.repl_ping_at_ms){
    g_data.repl_ping_at_ms = now_ms + (uint64_t)g_config.repl_ping_period_ms;
    repl_ping_replicas();
  }

  if (timeout_ms > 0 && !g_data.replicas.empty()){
    std::vector<Conn *> dead;
    for (Conn *r : g_data.replicas){
      if (r->ack_time_ms && now_ms - r->ack_time_ms > timeout_ms){
        dead.push_back(r);
      }
    }
    // collect first, conn_destroy -> repl_remove_conn erases from the set above
    for (Conn *r : dead){
      fprintf(stderr, "replication: replica %s silent for %llu ms, dropping it\n",
              r->peer.c_str(), (unsigned long long)(now_ms - r->ack_time_ms));
      conn_destroy(r); // the poll always spinns without this
    }
  }

  if (!g_data.replica_mode){ return; }

  if (g_data.master_link){

  if (timeout_ms > 0 && now_ms - g_data.master_last_io_ms > timeout_ms){
      Conn *c = g_data.master_link;
      fprintf(stderr, "replication: no data from master %s for %llu ms, dropping the link\n",
              c->peer.c_str(),
              (unsigned long long)(now_ms - g_data.master_last_io_ms));
      conn_destroy(c);
      return;
  } 

    // healthy link
    if (g_data.repl_state == ReplState::STREAMING){ 
      g_data.repl_retry_delay_ms = 0;
      // report progress on the same sweep
      if (now_ms >= g_data.repl_ack_at_ms){
        g_data.repl_ack_at_ms = now_ms + k_repl_ack_period_ms;
        const std::string off =std::to_string(g_data.master_repl_offset);
        std::string ack;
        aof_encode(ack, { "REPLCONF", "ACK", std::string_view(off) });
        buf_append(&g_data.master_link->outgoing, ack.data(), ack.size());
        g_data.master_link->want_write = true;
      }
    }
    return;
  }

  if (now_ms < g_data.repl_retry_at_ms){ return; }

  // grow the backoff before dialing
  g_data.repl_retry_delay_ms = g_data.repl_retry_delay_ms
      ? std::min<uint32_t>(g_data.repl_retry_delay_ms * 2, k_repl_retry_max_ms)
      : k_repl_retry_min_ms;
  g_data.repl_retry_at_ms = now_ms + g_data.repl_retry_delay_ms;

  // repl_start() calls repl_stop(), whicj clears g_data.master_host
  const std::string host = g_data.master_host;
  const int port = g_data.master_port;

  std::string err;
  if (!repl_start(host, port, err)){
    fprintf(stderr,"replication: reconnect to %s:%d failed: %s (retry in %u ms)\n",
             host.c_str(), port, err.c_str(), g_data.repl_retry_delay_ms);
  }
}

// Drives a FAILOVER from WAIT_FOR_SYNC to IN_PROGRESS, called every timer sweep
static void failover_cron(uint64_t now_ms) {
  if (g_data.failover_state != FailoverState::WAIT_FOR_SYNC){ return; }

  Conn *t = replica_by_addr(g_data.failover_host, g_data.failover_port);
  if (!t) {
    // repl-timeout may have dropped it out from under us, or it went away on its own
    failover_reset("target replica is gone, aborting");
    return;
  }

  const bool synced = t->ack_offset >= g_data.master_repl_offset;
  const bool expired = g_data.failover_deadline_ms && now_ms >= g_data.failover_deadline_ms;
  if (!synced && !expired){ return; }
  if (!synced && !g_data.failover_force){
    failover_reset("timed out waiting for the target to catch up, aborting (no FORCE)");
    return;
  }
  if (!synced){
    fprintf(stderr, "failover: FORCE, handing over at %llu with the target at %llu, losing %llu bytes\n",
            (unsigned long long)g_data.master_repl_offset,
            (unsigned long long)t->ack_offset,
            (unsigned long long)(g_data.master_repl_offset - t->ack_offset));
  }
  
  const std::string host = g_data.failover_host; // t dies below, and these with it 
  const int port = g_data.failover_port;
  g_data.failover_state = FailoverState::IN_PROGRESS; // before repl_start, it reads this

  // our replicas have to go 
  std::vector<Conn *> old(g_data.replicas.begin(), g_data.replicas.end());
  for (Conn *r: old){ conn_destroy(r); }
  
  std::string err;
  if (!repl_start(host, port, err)) {
    // we have already dropped every replica; there is no clean way back
    failover_reset("could not dial the new master (" + err + "), still the master with no replicas");
    return;
  }
  fprintf(stderr, "failover: demoted, PSYNC FAILOVER sent to %s:%d\n", host.c_str(), port);
}

// one CRLF-terminated line out of the incoming; false = need more bytes
static bool repl_take_line(Conn *c, std::string &line){
  const char *p = (const char *)buf_data(&c->incoming);
  size_t n = buf_size(&c->incoming);
  for (size_t i = 0; i + 1 < n; ++i){
    if (p[i] == '\r' && p[i + 1] == '\n') {
      line.assign(p, i);  
      buf_consume(&c->incoming, i + 2);
      return true;
    }
  }
  return false; 
}

// Applies one command from the master through the ordinary dispatch
static void repl_apply(std::vector<std::string> &cmd){
  Buffer sink = buf_create(256);
  Conn fake{};
  fake.user = reply_apply_user();
  bool was_loading = g_data.g_loading;
  g_data.g_loading = true; // supress re_propagation, exactly as aof_load does
  do_request(cmd, &sink, &fake, nullptr, 0);
  g_data.g_loading = was_loading;
  buf_destroy(&sink);
}

// The replica's read path, replacing try_one_request for the master link
static void repl_master_data(Conn *c){
  for (;;){
    switch (g_data.repl_state){

      case ReplState::NONE:
        return; // link torn down mid-drain

      case ReplState::HANDSHAKE: {
        std::string line;
        if (!repl_take_line(c, line)){ return; }
        if (line.empty()){ break; }
        if (line[0] == '-'){
          fprintf(stderr, "replication: master rejected the handshake: %s\n", line.c_str() + 1);
          c->want_close = true;
          return;
        }
        // +CONTINUE [<replid>] — the master kept our history: no image follows
        if (line.compare(0, 9, "+CONTINUE") == 0){
          // a promoted master answers CONTINUE under its new replid, adopt it 
          const size_t sp = line.find(' ', 9);
          if (sp != std::string::npos){
            const std::string newid = line.substr(sp + 1);
            if (!newid.empty() && newid != g_data.repl_id){
              fprintf(stderr, "replication: master history renamed %s -> %s\n",
                      g_data.repl_id.c_str(), newid.c_str());
              g_data.repl_id = newid;
              g_data.repl_id2.clear();
              g_data.second_repl_offset = 0;
            }
          }
          g_data.repl_state = ReplState::STREAMING;
          if (g_data.failover_state != FailoverState::NONE){
            failover_reset("complete");
          }
          fprintf(stderr, "replication: partial resync accepted, continuing at offset %llu\n",
                  (unsigned long long)g_data.master_repl_offset);
          break;
        }

        // the +OK acks for AUTH / REPLCONF arrive first and carry nothing we need
        if (line.compare(0, 12, "+FULLRESYNC ") != 0){ break; }

        const size_t sp = line.find(' ', 12);
        if (sp == std::string::npos){
          fprintf(stderr, "replication: malformed '%s'\n", line.c_str());
          c->want_close =true;
          return;
        }
        // adopt the master's history: from here our won past writes are irrevelant, we are its copy
        g_data.repl_id = line.substr(12, sp - 12);
        g_data.master_repl_offset = strtoull(line.c_str() + sp + 1, nullptr, 10);
        g_data.repl_id2.clear();
        g_data.second_repl_offset = 0;
        g_data.repl_backlog_pos = 0;
        g_data.repl_backlog_histlen = 0;
        g_data.repl_state = ReplState::RDB_LEN;
        if (g_data.failover_state != FailoverState::NONE){
          failover_reset("complete");
        }
        fprintf(stderr, "replication: full resync from %s at offset %llu\n",
                 g_data.repl_id.c_str(), (unsigned long long)g_data.master_repl_offset);
        break;
      }

      case ReplState::RDB_LEN: {
        std::string line;
        if (!repl_take_line(c, line)){ return; }
        if (line.empty() || line[0] != '$'){
          fprintf(stderr, "replication: expected the RDB bulk header, got '%s'\n", line.c_str());
          c->want_close = true;
          return;
        }
        char *end = nullptr;
        errno = 0;
        const unsigned long long len = strtoull(line.c_str() + 1, &end, 10);
        if (errno || end == line.c_str() + 1 || *end != '\0'){
          fprintf(stderr, "replication: bad RDB length '%s'\n", line.c_str());
          c->want_close = true;
          return;
        }
        g_data.repl_rdb_left = len;
        g_data.repl_rdb_buf.clear();
        // the reserve is capped for absurd values that can come
        g_data.repl_rdb_buf.reserve((size_t)std::min<uint64_t>(len, k_repl_rdb_reserve_max));
        g_data.repl_state = ReplState::RDB_BODY;
        break;
      }

      case ReplState ::RDB_BODY: {
        if (g_data.repl_rdb_left){
          const size_t have = buf_size(&c->incoming);
          if (have == 0){ return; }
          const size_t take = (size_t)std::min<uint64_t>(g_data.repl_rdb_left, have) ;
          g_data.repl_rdb_buf.append((const char *)buf_data(&c->incoming), take);
          buf_consume(&c->incoming, take);
          g_data.repl_rdb_left -= take;
          if (g_data.repl_rdb_left){ return; } // image stil arriving
        }

        // Tha master's image REPLACES our dataset 
        std::vector<std::string> wipe = { "flushall" };
        repl_apply(wipe);

        const size_t img = g_data.repl_rdb_buf.size();
        const bool ok = rdb_load_buffer((const uint8_t *)g_data.repl_rdb_buf.data(), img);
        g_data.repl_rdb_buf.clear();
        g_data.repl_rdb_buf.shrink_to_fit(); // a resync image must not stay resident
        if (!ok){
          fprintf(stderr, "replication: RDB image failed to load, dropping the link\n");
          c->want_close = true;
          return;
        }
        g_data.repl_state = ReplState::STREAMING;
        fprintf(stderr, "replication: loaded %zu byte image, streaming from master\n", img);
        break;
      }

      case ReplState::STREAMING: {
        std::vector<std::string> cmd;
        const int32_t consumed = parse_resp_request(&c->incoming, cmd);
        if (consumed == 0){ return; } // partial frame, wait for the rest
        if (consumed < 0){
          fprintf(stderr, "replication: malformed frame from the master, dropping the link\n");
          c->want_close = true;
          return;
        }
        repl_backlog_feed((const char *)buf_data(&c->incoming), (size_t)consumed);
        buf_consume(&c->incoming, (size_t)consumed);
        // our offset is the MASTER's: count the bytes we accepted
        if (!cmd.empty()){ repl_apply(cmd); }
        break;
      }
    }
  }
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

  // a disconnected replica must wake up to re-dial, even with nothing else pending
  if (g_data.replica_mode){
    if (!g_data.master_link){
      next_ms = std::min<uint64_t>(next_ms, g_data.repl_retry_at_ms);
    } else {
      // every phase, not just streaming, the point is to expire a handshake that is never going to get a reply
      if (g_config.repl_timeout_ms > 0){
        next_ms = std::min<uint64_t>(next_ms, g_data.master_last_io_ms + (uint64_t)g_config.repl_timeout_ms);
      }
      if (g_data.repl_state == ReplState::STREAMING){
        next_ms = std::min<uint64_t>(next_ms, g_data.repl_ack_at_ms);
      }
      if (!g_data.replicas.empty() && g_config.repl_ping_period_ms > 0){
        next_ms = std::min<uint64_t>(next_ms, g_data.repl_ping_at_ms);
      }
    } 
  }

  // a master stays awake only because its replicas ACL once a second.
  if (g_config.repl_timeout_ms > 0){
    for (const Conn *r: g_data.replicas){
      if (r->ack_time_ms){
        next_ms = std::min<uint64_t>(next_ms, r->ack_time_ms + (uint64_t)g_config.repl_timeout_ms);
      }
    } 
  }

  // a failover waiting on a target that has gone quite must still time out
  if (g_data.failover_state != FailoverState::NONE && g_data.failover_deadline_ms){
    next_ms = std::min(next_ms, g_data.failover_deadline_ms);
  }

  // a pending WAIT must expire on time even on a completely idle server
  for (const Conn *c : g_data.waiters){
    if (c->wait_deadline_ms){ next_ms = std::min(next_ms, c->wait_deadline_ms); }
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
  wait_try_resume(); // expire any WAIT whose deadline has passed
  repl_cron(now_ms);
  failover_cron(now_ms); 
}

static void conn_set_timer(Conn *conn, ConnTimer type){
  // the master link is never reaped: an idle master is a healthy master
  if (conn->is_master_link || conn->is_replica){
    dlist_detach(&conn->idle_node);
    dlist_init(&conn->idle_node);
    return;
  }

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
  if (conn->auth_pending || conn->wait_pending){ return false; }

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
  if (conn->auth_pending || conn->wait_pending){
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

    // The master link drains per chunk: a full-resync image is unbounded, so it
    // must never accumulate in `incoming`
    if (conn->is_master_link){
      if (n > 0){
        g_data.master_last_io_ms = get_monotonic_msec();
        repl_master_data(conn); 
      }
      if (conn->want_close){ return; }
    } else if (buf_size(&conn->incoming) > k_max_incoming){
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
  
  if (!conn->is_master_link){ while (try_one_request(conn)) {} }

  if(buf_size(&conn->outgoing) > 0){
    conn->want_read = false;
    conn->want_write = true;
    // this is a optimization 
    return handle_write(conn);
  } 
  // Nothing to say — REPLCONF ACK is the one command that answers nothing. Undo
  // try_one_request's optimistic write intent, or poll() reports POLLOUT, dispatch
  // takes the want_write arm, and handle_write asserts on an empty buffer.
  conn->want_read = true;
  conn->want_write = false;
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
  #ifndef MYRED_HAVE_TLS
    fprintf(stderr, "startup: build without OpenSSL - TLS disabled (tls-port unavailable)\n");
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

  // Deferred replica connect
  if (!g_config.replicaof_host.empty()){
    std::string err;
    if (!repl_start(g_config.replicaof_host, g_config.replicaof_port, err)){
      errno = 0;
      fatal_exit(("replicaof: " + err).c_str());
    }
  }

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
