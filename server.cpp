// stdlib
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include <signal.h>
#include <string.h>
// system
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/tcp.h>
#include <sys/wait.h>   // for waitpid
#include <sys/stat.h>  // fstat
// C++
#include <string>
#include <vector>
#include <sstream>
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

//Helper function for syscalls 
static void msg_errno(const char *msg) {
  fprintf(stderr, "[errno:%s]\n", msg);
}

static void die(const char *msg){
	int err = errno;
	fprintf(stderr, "[%d] %s\n", err, msg);
	abort();
}

static void fd_set_nb(int fd){
  errno = 0;
  int flags = fcntl(fd, F_GETFL,0);
  if (errno){
    die("fcntl error");
    return;
  }
  flags |= O_NONBLOCK;

  errno = 0;
  (void) fcntl(fd, F_SETFL, flags);
  if (errno){ die("fcntl error"); }
}

// global flag — set to true when Ctrl+C is pressed
static bool g_stop = false;

static void signal_handler(int sig) {
    (void)sig;
    g_stop = true;
}


// callback when the socket is ready
static int32_t handle_accept(int fd){
  // accept logic
  struct sockaddr_in client_addr =  {};
  socklen_t addrlen = sizeof(client_addr);

  int connfd = accept(fd, (struct sockaddr *)&client_addr, &addrlen);
  if (connfd < 0) {
    if (errno != EAGAIN){ msg_errno("accept() error"); }
    return -1;
  }

  uint32_t peer_host = ntohl(client_addr.sin_addr.s_addr);

  // IP allowlist (loopback always allowed; empty list = allowed all)
  if (!ip_allowed(peer_host)){
    fprintf(stderr, "rejected %s: not in allowlist\n", inet_ntoa(client_addr.sin_addr));
    close(connfd);
    // keep acepting other pending conections
    return 0;
  }

  // protected mode: no password + remote peer -> refuse witn an explanation
  if (g_config.protected_mode && g_config.password.empty() && !ip_is_loopback(peer_host)){
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

static void conn_destroy(Conn *conn){
  (void)close(conn->fd);
  g_data.fd2conn[conn->fd] = NULL;
  dlist_detach(&conn->idle_node);
  delete conn;
  g_data.connected_clients--;
}

// Timers logic 
static int32_t next_timer_ms() {
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
  // check the heap
  if (!g_data.heap.empty()){
    next_ms = std::min(next_ms, g_data.heap[0].val);
  }
  // check the periodic wake up
  if (g_data.g_writes_since_save > 0){
    uint64_t soonest = (uint8_t)-1;
    for (const SaveCondition &sc : g_config.save_conditions){
      if (g_data.g_writes_since_save >= sc.changes){
        soonest = std::min(soonest, g_data.g_last_save_ms + sc.seconds * 1000);
      }
    }
    if (soonest != (uint64_t)-1){
      next_ms = std::min(next_ms, soonest);
    }
  }
  // no timers 
  if (next_ms == (uint64_t)-1){ return -1; }
  // already expired
  if (next_ms <= now_ms){ return 0; }
  // rare case idk ??
  if (next_ms == UINT64_MAX) {return -1;}

  return (int32_t)(next_ms - now_ms);
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
  } else {
    dlist_insert_before(&g_data.io_list, &conn->idle_node);
  }
}

// Process cmds functions

// we will try to proccess if theres enough data
static bool try_one_request(Conn *conn){
  std::vector<std::string> cmd;
  int32_t consumed = parse_resp_request(&conn->incoming, cmd);

  if (consumed == 0) { return false; }
  if (consumed < 0){
    fprintf(stderr, "bad RESP from fd %d\n", conn->fd);
    return false;
  }

  // capture the raw frame BEFORe consuming/dispatching. logged berbatim for aof
  const char *raw = (const char *)buf_data(&conn->incoming);
  size_t raw_len = (size_t)consumed;

  g_data.g_total_commands++;
  do_request(cmd, &conn->outgoing, conn, raw, raw_len);

  // moved after do_request, raw must stay valid
  buf_consume(&conn->incoming, raw_len);
  conn->want_read = false;
  conn->want_write = true;
  return true;
}

static void handle_write(Conn *conn){
  assert(buf_size(&conn->outgoing) > 0);
  ssize_t rv = write(conn->fd, buf_data(&conn->outgoing), buf_size(&conn->outgoing));
  if(rv < 0 && errno == EAGAIN){ return; }
  if (rv < 0 && errno == EINTR){ return; }

  if (rv < 0) {
    conn->want_close = true;
    return;
  }
  // remove the data from outgoing
  buf_consume(&conn->outgoing, (size_t)rv);

  if(buf_size(&conn->outgoing) == 0){ // all data writen 
    conn->want_read = true;
    conn->want_write = false;
  }// want to keep writing 
}

static void handle_read(Conn *conn){
  uint8_t buf [64 * 1024];
  ssize_t rv = read(conn->fd, buf, sizeof(buf));
  if (rv < 0 && errno == EAGAIN){return;}
  if (rv < 0 && errno == EINTR){ return; }
  if(rv <= 0) {
    conn->want_close = true;
    return;
  }
  // add new data to the incoming buffer
  buf_append(&conn->incoming, buf, (size_t)rv);

  // We set the conn to IO (stop the idle)
  if (conn->timer_type == ConnTimer::IDLE){
    conn_set_timer(conn, ConnTimer::IO);
  }
  
  while (try_one_request(conn)) {};

  if(buf_size(&conn->outgoing) > 0){
    conn->want_read = false;
    conn->want_write = true;
    // this is a optimization 
    return handle_write(conn);
  } // else wants to keep reading.
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


  // initialiaze idle connection list and io waiting list
  dlist_init(&g_data.idle_list);
  dlist_init(&g_data.io_list);

  thread_pool_init(&g_data.thread_pool, 8);

  const char *cfg_path = getenv("MYRED_CONFIG");
  for (int i = 1; i < argc; ++i){
    if (argv[i][0] != '-'){ 
      cfg_path = argv[i];
      break; 
    } 
  }
  
  if (cfg_path){ fprintf(stderr, "startup: loading config %s\n", cfg_path); }
  if (cfg_path && !config_load_file(cfg_path)){ die("invalid config file"); }


  if (const char *e = getenv("MYRED_PASSWORD")){ g_config.password = sha256_hex(e); }
  if (const char *e = getenv("MYRED_PORT")){ int p = atoi(e); if (p > 0 && p < 65536){ g_config.port = p; } }
  if (const char *e = getenv("MYRED_AOF")){ g_config.aof_enable = (e[0] == '1' || e[0] == 'y'); }


  if (g_config.password.empty()){ g_config.password = sha256_hex("kek1234"); }   // historical default

  acl_bootstrap_default();
  acl_init_categories();      

  // AOF takes priority over RDB
  bool aof_exists = (access(g_config.aof_path.c_str(), F_OK) == 0);
  bool rdb_exists = (access(g_config.dump_path.c_str(), F_OK) == 0);

  if (g_config.aof_enable && aof_exists){
    if (rdb_exists){
      fprintf(stderr, "startup: AOF and RDB both present -> loading AOF, ignording RDB\n");
    }
    if (!aof_load(g_config.aof_path.c_str())){
      fprintf(stderr, "startup: AOF load failed\n");
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

  const char *rmin_enve = getenv("MYRED_AOF_REWRITE_MIN");
  if (rmin_enve){
    long long v = atoll(rmin_enve);
    if (v > 0){ g_config.aof_rewrite_min_size = (size_t)v; }
  }

  const char *rperc_env = getenv("MYRED_AOF_REWRITE_PERC");
  if (rperc_env){
    int v = atoi(rperc_env);
    if (v > 0){ g_config.aof_rewrite_perc = v; }
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

  std::vector<int> listen_fds;
  for (const std::string &addr : g_config.binds){
    int lfd = listen_on(addr, g_config.port);
    // fail fast if any addrees can't bind 
    if (lfd < 0){ die("listener setup"); }
    listen_fds.push_back(lfd);
    fprintf(stderr,"listening on %s:%d\n", addr.c_str(), g_config.port);
  }

  const size_t nlisten = listen_fds.size();
  std::vector<struct pollfd> poll_args; // This a vector of structs for arguments for poll_args

  while(!g_stop){
    
    poll_args.clear(); //This just clean the arguments for poll.
    // listen fds occupy [0, nlisten]
    for (int lfd : listen_fds){
      struct pollfd pfd = {lfd, POLLIN, 0};
      poll_args.push_back(pfd);
    }

    for (Conn *conn : g_data.fd2conn){
      if(!conn){continue;}
      struct pollfd pfd = {conn->fd, POLLERR, 0}; // This is for the flags of the aplication
      if (conn->want_read){ pfd.events |= POLLIN; }
      if (conn->want_write){ pfd.events |= POLLOUT; }
      poll_args.push_back(pfd);
    }

    int32_t timeout_ms = next_timer_ms();
    int rv = poll(poll_args.data(), (nfds_t)poll_args.size(), timeout_ms);

    if(rv < 0 && errno == EINTR){continue;}
    if(rv < 0){die("poll");}

    // any listening socket ready -> drain its backlog
    for (size_t i = 0; i < nlisten; i++){
      if (poll_args[i].revents) {
        // we accept on that fd
        while (handle_accept(poll_args[i].fd) == 0) {}
      }
    }

    //This is for to handle the connections of sockets
    for(size_t i = nlisten; i < poll_args.size(); i++){ // we skip the 1st
      uint32_t ready = poll_args[i].revents;
      if (ready == 0){ // no events fired up
        continue;
      }


      // retrieve the object of every fd (in this case only i)
      Conn *conn = g_data.fd2conn[poll_args[i].fd];
      if (!conn){continue;}

      // update the idle timer and putting the conn at the end of the list
      conn->last_active_ms = get_monotonic_msec();
      dlist_detach(&conn->idle_node);
      dlist_insert_before(&g_data.idle_list, &conn->idle_node);

      // Connection are ready to write and read
      if(ready & POLLIN){
        assert(conn->want_read);
        handle_read(conn);
      }
      if(ready & POLLOUT){
        assert(conn->want_write);
        handle_write(conn);
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
    rdb_check_background_save();
    aof_check_background_rewrite();
  }
  thread_pool_destroy(&g_data.thread_pool);

  if (g_data.g_aof_fd >= 0){
    aof_flush();
    fdatasync(g_data.g_aof_fd);
    close(g_data.g_aof_fd);
  }

  fprintf(stderr, "Shutting down, saving...\n");

  // if a background save is running, wait for it first
  if (g_rdb_child_pid != -1){
    int status = 0;
    waitpid(g_rdb_child_pid, &status, 0); // blocking wait
    g_rdb_child_pid = -1;
  }

  rdb_save("dump.rdb");
  fprintf(stderr, "Saved. Goodbye.\n");
  return 0;
}