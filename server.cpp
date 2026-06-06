// stdlib
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <math.h> // for the isnan
#include <signal.h>
// system
#include <time.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/ip.h>
// C++
#include <string>
#include <vector>
#include <algorithm>
// project 
#include "hashtable.h"
#include "common.h"
#include "zset.h"
#include "list.h"
#include "heap.h"
#include "thread_pool.h"

constexpr size_t k_max_msg = 32 << 20;

constexpr uint64_t k_save_interval_ms  = 5 * 60 * 1000; // 5 minutes 5 * 60 * 1000
constexpr uint32_t k_save_after_writes = 100; // or after 100 writes

// secondes * miliseconds (5s -> 5000ms)
constexpr uint64_t k_idle_timeout_ms = 5 * 1000;
constexpr uint64_t k_io_timeout_ms = 5 * 1000;

//Helper function for syscalls 

static void msg_errno(const char *msg) {
  fprintf(stderr, "[errno:%s\n]", msg);
}

static void die(const char *msg){
	int err = errno;
	fprintf(stderr, "[%d] %s\n", err, msg);
	abort();
}

static uint64_t get_monotonic_msec(){
  struct timespec tv = {0,0};
  clock_gettime(CLOCK_MONOTONIC, &tv);
  return uint64_t(tv.tv_sec) * 1000 + tv.tv_nsec / 1000 / 1000;
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
  if(errno) {
    die("fcntl error");
  }
}

// global flag — set to true when Ctrl+C is pressed
static bool g_stop = false;

static void signal_handler(int sig) {
    (void)sig;
    g_stop = true;
}

static bool hnode_same(HNode *node, HNode *key){
  return node == key;
}

// Buffer for the tcp protocol
struct Buffer {
  uint8_t *buffer_begin; // start of memory
  uint8_t *buffer_end; // end of memory
  uint8_t *data_begin; // start of data in memory 
  uint8_t *data_end; // end of data in memory
};

// Initialize the buffer protocol 
Buffer buf_create(size_t capacity){
  uint8_t *mem = new uint8_t[capacity];
  return Buffer {
    .buffer_begin = mem,
    .buffer_end = mem + capacity,
    .data_begin = mem,
    .data_end = mem,
  };

}

// Timer for both linked lists
enum class ConnTimer {
  IDLE,
  IO,
};

// Connections state and buffers 
struct Conn {
  int fd = -1; // this is for the event loop

  bool want_read = false; // The the read and the write, is waiting for the fd api readiness
  bool want_write = false;
  bool want_close = false;
  // buffered input, output
  Buffer incoming; // This two are for the buffers that we are gonna parse // data
  Buffer outgoing; // the response 
  //time
  uint64_t last_active_ms = 0;
  ConnTimer timer_type = ConnTimer::IO;
  DList idle_node;
};

// global hashtable
static struct {
  HMap db; // top level hashtable 
  std::vector<Conn *> fd2conn; // this a pointer to all conecctions in the file descriptor [3,4,5], and is key by this aswell
  //timers and connection
  DList idle_list; // list of waiting connections 
  DList io_list;  // list of waiting io (read and write)
  // timers for ttls
  std::vector<HeapItem> heap;
  ThreadPool thread_pool;
  // global flag
  uint64_t last_save_ms = 0;
  uint32_t writes_since_save = 0;
} g_data;

// callback when the socket is ready
static int32_t handle_accept(int fd){
  // accept logic
  struct sockaddr_in client_addr =  {};
  socklen_t addrlen = sizeof(client_addr);

  int connfd = accept(fd, (struct sockaddr *)&client_addr, &addrlen);
  if (connfd < 0) {
  msg_errno("accept() error");
  return -1;
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
g_data.fd2conn[conn->fd] = conn;

return 0;
}

static void conn_destroy(Conn *conn){
  (void)close(conn->fd);
  g_data.fd2conn[conn->fd] = NULL;
  dlist_detach(&conn->idle_node);
  delete conn;
}

//Helper functions // Buffer

// append to the front of the buffer 
static void buf_append(Buffer *buf, const uint8_t *data, size_t len){

  size_t data_size = buf->data_end - buf->data_begin;

  size_t space_at_back = buf->buffer_end - buf->data_end;

  if (space_at_back < len){
    //  Option A slide the data to the front 
    memmove(buf->buffer_begin, buf->data_begin, data_size);
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin + data_size;

    space_at_back = buf->buffer_end - buf->data_end;

    if (space_at_back < len){
      // Option B still not enough
      size_t old_cap = buf->buffer_end - buf->buffer_begin;
      size_t new_cap = old_cap * 2;

      while (new_cap < data_size + len) new_cap *= 2;

      uint8_t *new_mem = new uint8_t[new_cap];
      memcpy(new_mem, buf->data_begin, data_size);

      delete[] buf->buffer_begin; // free old block 
      
      buf->buffer_begin = new_mem;
      buf->buffer_end = new_mem + new_cap;
      buf->data_begin = new_mem;
      buf->data_end = new_mem + data_size;
    }
  }

  memcpy(buf->data_end, data, len);
  buf->data_end += len;
}

// overload for bytes for the rdb file
inline static void buf_append(Buffer *buf, uint8_t Byte){
  buf_append(buf, &Byte, 1);
}

// append 32 bytes
// static void buf_append_u32(Buffer *buf, uint32_t val) {
//     buf_append(buf, (const uint8_t *)&val, 4);
// }

// append 64 bytes
static void buf_append_u64(Buffer *buf, uint64_t val) {
    buf_append(buf, (const uint8_t *)&val, 8);
}

// Append strings
static void buf_append_str(Buffer *buf, const char *str, uint32_t len){
  buf_append(buf, (const uint8_t *)&len, 4);
  buf_append(buf, (const uint8_t *)str, len);
}

// remove form the front of the buffer and resize 
static void buf_consume(Buffer *buf, size_t n){
  buf->data_begin += n; // we are just moving the pointer forward
  
  // This chunk is just only to reclaim espace 
  if (buf->data_begin == buf->data_end){
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin;
  }
}

//bytes of the data available 
size_t buf_size(Buffer *buf){
  return buf->data_end - buf->data_begin;
}

//pointer to readble data 
uint8_t* buf_data(Buffer *buf){
return buf->data_begin;
}

//free memory
void buf_destroy(Buffer *buf){
  delete[] buf->buffer_begin;
  buf->buffer_begin = NULL;
  buf->buffer_end = NULL;
  buf->data_begin = NULL;
  buf->data_end= NULL;

}

//Helper functions for parsing
// Reads data from string 
static bool read_u32(const uint8_t *&cur, const uint8_t *end, uint32_t &out){
  if (cur + 4 > end){
    return false;
  }
  memcpy(&out, cur, 4);
  cur += 4;

  return true;
}

//Reads data length
static bool read_str(const uint8_t *&cur, const uint8_t *end, size_t n, std::string &out){
  if  (cur + n > end){
    return false;
  }
  out.assign(cur, cur + n);
  cur += n;
  return true;
}

static int32_t parse_req(const uint8_t *data, size_t size, std::vector<std::string> &out){
  const uint8_t *end = data + size;
  uint32_t nstr = 0;

  if(!read_u32(data, end, nstr)){return -1;}
  if(nstr > k_max_msg){return -1;}
  

  while (out.size() < nstr) {
    uint32_t len = 0;
    if(!read_u32(data, end, len)){
      return -1;
    }
    out.push_back(std::string());
    if (!read_str(data, end, len, out.back())){return 1;}
  }
  if (data != end){return -1;}
  return 0;

}

//error codes for tag_err
enum {
  ERR_UNKNOWN = 1, // unknown command
  ERR_TOO_BIG = 2, // response too big
  ERR_BAD_TYP = 3, // unexpected value
  ERR_BAD_ARG = 4, // bad arguments
};

// data types for tag types
enum {
  TAG_NIL = 0, // nil
  TAG_ERR = 1, //err + msg
  TAG_STR = 2, //string
  TAG_INT = 3, //integer
  TAG_DBL = 4, //double
  TAG_ARR = 5, //array
};

//append for serializaed data 

// NIL values
static void out_nil(Buffer *out){
  uint8_t tag = TAG_NIL;
  buf_append(out, &tag, 1);
}

// STRING values
static void out_str(Buffer *out, const std::string &s){
  uint8_t tag = TAG_STR;
  uint32_t len = (uint32_t)s.size();
  buf_append(out, &tag, 1);
  buf_append(out, (const uint8_t *)&len, 4);
  buf_append(out, (const uint8_t *)s.data(), s.size());
}

// Integer values
static void out_int(Buffer *out, int64_t val){
  uint8_t tag = TAG_INT;
  buf_append(out, &tag, 1);
  buf_append(out, (const uint8_t *)&val, 8);
}

// Double values
static void out_dbl(Buffer *out, double val){
  uint8_t tag = TAG_DBL;
  buf_append(out, &tag, 1);
  buf_append(out, (const uint8_t *)&val, 8);
}

// err values
static void out_err(Buffer *out, uint32_t code, const std::string &msg){
  uint8_t tag = TAG_ERR;
  uint32_t len = (uint32_t )msg.size();
  buf_append(out, &tag, 1);
  buf_append(out,(const uint8_t *)&code, 4);
  buf_append(out,(const uint8_t *)&len, 4);
  buf_append(out,(const uint8_t *)msg.data(), msg.size());
}

// arr values 
static void out_arr(Buffer *out, uint32_t n){
  uint8_t tag = TAG_ARR;
  buf_append(out, &tag, 1);
  buf_append(out, (const uint8_t *)&n, 4); 
};

// reserve 4 bytes with the bookmark at the beggining
static size_t out_begin_arr(Buffer *out){
  uint8_t tag = TAG_ARR;
  buf_append(out, &tag, 1);

  // ctx is the bookmark
  size_t ctx = buf_size(out);
  uint32_t placeholder = 0;

  buf_append(out, (const uint8_t *)&placeholder, 4); // reserves 4 bytes
  return ctx;
}

static void out_end_arr(Buffer *out, size_t ctx, uint32_t n){
  assert(buf_data(out)[ctx - 1] == TAG_ARR);
  memcpy(buf_data(out) + ctx, &n, 4);
}

//value types 
enum {
  T_INIT = 0,
  T_STR = 1, // string
  T_ZSET = 2, // sorted set
};

// kv pair for the top level hashtable
struct Entry {
  struct HNode node; // hashtable node
  std::string key;
  // for ttl
  size_t heap_idx = -1;
  // value
  uint32_t type = 0;
  // one of the following 
  std::string str;
  ZSet zset;
};

static Entry *entry_new(uint32_t type) {
  Entry *ent = new Entry();
  ent->type = type;
  return ent;
}

// Definition declared later....
static void entry_set_ttl(Entry *ent, int64_t ttl_ms);
// Delete the actual work
static void entry_del_sync(Entry *ent) {
  if (ent->type == T_ZSET) {
    zset_clear(&ent->zset);
  }
  entry_set_ttl(ent, -1);
  delete ent;
}

// a wrapper function for the thread pool
static void entry_del_func(void *arg){
  entry_del_sync((Entry *)arg);
}

// When and where to delete
static void entry_del(Entry *ent){ 
  // remove from the heap first
  entry_set_ttl(ent, -1);
  // decide if use thread pool or synchronous
  size_t set_size = (ent->type == T_ZSET) ? hm_size(&ent->zset.hmap) : 0;
  const size_t k_large_container_size = 1000;

  if (set_size > k_large_container_size){
    thread_pool_queue(&g_data.thread_pool, &entry_del_func, ent);
  } else {
    entry_del_sync(ent);
  }
}

// Persistence functions 

// CRC32 
static uint32_t g_crc32_table[256];
static bool g_crc32_ready = false;

static void crc32_init(){
  for (uint32_t i = 0; i < 256; ++i){
    uint32_t crc = i;
    for (int j = 0; j < 8; ++j){
      crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320 : (crc >> 1);
    }
    g_crc32_table[i] = crc;
  }
  g_crc32_ready = true;
}

static uint32_t crc32_compute(const uint8_t *data, size_t len){
  if (!g_crc32_ready){ crc32_init(); }
  uint32_t crc = 0xFFFFFFFF;
  for (size_t i = 0; i < len; ++i){
    crc = (crc >> 8) ^ g_crc32_table[(crc ^ data[i]) & 0xFF];
  }
  return crc ^ 0xFFFFFFFF;
}

// RDB File functions and struct
// Callback struct
struct RDBWriteCtx {
  Buffer *buf;
  uint32_t count; // entry that we wrote
};

// for zset iterator
struct ZSetSaveCtx {
  Buffer *buf;
  uint32_t count;
};

static bool cb_zset_member(HNode *node, void *arg){
  ZSetSaveCtx *ctx = (ZSetSaveCtx *)arg;
  ZNode *znode = container_of(node, ZNode, hmap);

  // score -> 8 bytes
  buf_append(ctx->buf, (const uint8_t *)&znode->score, 8);

  // name - lenght prefixed
  buf_append_str(ctx->buf, znode->name, (uint32_t)znode->len);

  ctx->count++;
  return true;
}

static bool cb_rdb_write(HNode *node, void *arg){
  RDBWriteCtx *ctx = (RDBWriteCtx *)arg;
  Entry *ent = container_of(node, Entry, node);
  // we skip everything only strings
  if (ent->type == T_STR){
    // type tag byte
    buf_append(ctx->buf, 0);
    
    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1); // has ttl
      buf_append_u64(ctx->buf, g_data.heap[ent->heap_idx].val); // expire at
    } else {
      buf_append(ctx->buf, 0); // not ttl
    }
    //append the key and value
    buf_append_str(ctx->buf, ent->key.data(), (uint32_t)ent->key.size());
    buf_append_str(ctx->buf, ent->str.data(), (uint32_t)ent->str.size());
  } else if (ent->type == T_ZSET){
    // type tag byte
    buf_append(ctx->buf, 1);

    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1);
      buf_append_u64(ctx->buf, g_data.heap[ent->heap_idx].val);
    } else {
      buf_append(ctx->buf, 0);
    }
  
    // key
    buf_append_str(ctx->buf, ent->key.data(), (uint32_t)ent->key.size());

    // member count placeholder
    size_t member_count_index = (size_t)(buf_size(ctx->buf));
    buf_append(ctx->buf, (const uint8_t *)"\0\0\0\0", 4);

    // iterate all the members
    ZSetSaveCtx zctx;
    zctx.buf = ctx->buf;
    zctx.count = 0;
    // iterates over the zset and calls cb over each one
    hm_foreach(&ent->zset.hmap, cb_zset_member, &zctx);

    // patch real member count
    memcpy(ctx->buf->data_begin + member_count_index, &zctx.count, 4);

  }
  ctx->count++;
  return true;
}

static void rdb_serialize(Buffer *buf){
  // magic
  const char *magic = "MYRED";
  buf_append(buf, (const uint8_t *)magic, 5);

  // version
  uint32_t version = 2;
  buf_append(buf, (const uint8_t *)&version, 4);

  // index of the buffer (even if the buffer reallocates)
  size_t count_index = buf_size(buf);
  uint32_t dummy = 0;
  // we put dummy bytes
  buf_append(buf, (const uint8_t *)&dummy, 4);

  // this is what can cause the buffer to reallocate
  RDBWriteCtx ctx;
  ctx.buf = buf;
  ctx.count = 0;
  hm_foreach(&g_data.db, &cb_rdb_write, &ctx);

  //we repatch the dummy bytes
  memcpy(buf->data_begin + count_index, &ctx.count, 4);

  //EOF
  buf_append(buf, 255);

  // CRC32 
  size_t data_size = buf_size(buf);
  uint32_t crc = crc32_compute(buf->data_begin, buf_size(buf));
  buf_append(buf, (const uint8_t *)&crc, 4);

  fprintf(stderr, "rdb_serialize: %zu bytes, crc=0x%08x\n", data_size + 4, crc);
}

// we build the rdb function
static bool rdb_save(const char* filename){
  // build the buffer
  Buffer buf = buf_create(64 * 1024);
  rdb_serialize(&buf);

  // write to temp file first
  std::string tmp = std::string(filename) + ".tmp";
  FILE *fp = fopen(tmp.c_str(), "wb");
  if (!fp){
    fprintf(stderr, "rdb_save: cannot open %s: %s\n", tmp.c_str(), strerror(errno));
    buf_destroy(&buf);
    return false;
  }
  size_t data_size = buf_size(&buf);
  size_t written = fwrite(buf.data_begin, 1, buf_size(&buf), fp);
  fclose(fp);
  buf_destroy(&buf);

  if (written != data_size) {
    fprintf(stderr, "rdb_save: short write\n");
    remove(tmp.c_str());
    return false;
  }

  // atomic rename
  if (rename(tmp.c_str(), filename) != 0){
    fprintf(stderr, "rdb_save: rename failed: %s\n", strerror(errno));
    remove(tmp.c_str());
    return false;
  }

  fprintf(stderr, "rdb_save: done (%zu bytes)\n", data_size);
  return true;
}

// Read RDB functions
// tracks the current position in the file bytes
struct RDBCursor {
  const uint8_t *pos; // current
  const uint8_t *end; // one past last byte
};

// read len bytes into dst
static bool cursor_read(RDBCursor *c, void *dst, size_t len){
  if (c->pos + len > c->end){
    fprintf(stderr, "rdb_load: unexpected end of file\n");
    return false;
  }
  memcpy(dst, c->pos, len);
  c->pos += len;
  return true;
}
static bool cursor_read_u8(RDBCursor *c, uint8_t *out){
  return cursor_read(c, out, 1);
}
static bool cursor_read_u32(RDBCursor *c, uint32_t *out){
  return cursor_read(c, out, 4);
}
static bool cursor_read_u64(RDBCursor *c, uint64_t *out){
  return cursor_read(c, out, 8);
}

// read lenght-prefixed string into std::string
static bool cursor_read_str(RDBCursor *c, std::string *out){
  uint32_t len = 0;
  if (!cursor_read_u32(c, &len)){
    return false;
  }

  if (c->pos + len > c->end){
    fprintf(stderr, "rdb_load: string out of bounds\n");
    return false;
  }
  out->assign((const char *)c->pos, len);
  c->pos += len;
  return true;
}

static bool rdb_load_string_entry(RDBCursor *c){
  // has a ttl flag
  uint8_t has_ttl = 0;
  if (!cursor_read_u8(c, &has_ttl)){ return false; }

  // read expire_at if TTL exists
  uint64_t expire_at = 0;
  if (has_ttl){
    if (!cursor_read_u64(c, &expire_at)){ return false; }
    // check if expired 
    uint64_t now_ms = get_monotonic_msec();
    if (expire_at <= now_ms){
      // expired
      std::string key, val;
      cursor_read_str(c, &key);
      cursor_read_str(c, &val);
      fprintf(stderr, "rdb_load: skipping expired key\n");
      return true;
    }
  }

  // read key
  std::string key;
  if (!cursor_read_str(c, &key)){
    return false;
  }
  // read key
  std::string val;
  if (!cursor_read_str(c, &val)){
    return false;
  }

  // reconstruct the entry in the database
  Entry *ent = entry_new(T_STR);
  ent->key = key;
  ent->str = val;
  ent->node.hcode = str_hash((uint8_t *)ent->key.data(), ent->key.size());
  hm_insert(&g_data.db, &ent->node);
  if (has_ttl){
    uint64_t now_ms = get_monotonic_msec();
    int64_t remaining_ms = (int64_t)(expire_at - now_ms);
    entry_set_ttl(ent, remaining_ms);
  }
  return true;
}

static bool rdb_load_zset_entry(RDBCursor *c){
  uint8_t has_ttl = 0;
  if (!cursor_read_u8(c, &has_ttl)){ return false; }

  uint64_t expire_at = 0;
  if (has_ttl){
    if (!cursor_read_u64(c, &expire_at)){ return false; }

    // skip but still read all the bytes
    uint64_t now_ms = get_monotonic_msec();
    if (expire_at <= now_ms){
      std::string key;
      uint32_t n_members = 0;
      cursor_read_str(c, &key); // skip key
      cursor_read_u32(c, &n_members); // skip member count
      for (uint32_t i = 0; i< n_members; ++i){
        double score = 0;
        std::string name;
        cursor_read(c, &score, 8); // skip score
        cursor_read_str(c, &name); // skip name
      }
      fprintf(stderr, "rdb_load: skipping expired zset\n");
      return true;
    }
  }

  // key 
  std::string key;
  if (!cursor_read_str(c, &key)){ return false; }

  uint32_t n_members = 0;
  if (!cursor_read_u32(c, &n_members)){ return false; }

  // create the entry
  Entry *ent = entry_new(T_ZSET);
  ent->key = key;
  ent->node.hcode = str_hash((uint8_t *)ent->key.data(), ent->key.size());

  // read and insert each member
  for (uint32_t i = 0; i < n_members; ++i){
    double score = 0;
    if (!cursor_read(c, &score, 8)){
      entry_del(ent);
      return false;
    }

    std::string name;
    if (!cursor_read_str(c, &name)){
      entry_del(ent);
      return false;
    }

    // insert into the zset
    zset_insert(&ent->zset, name.data(), name.size(), score); 
  }

  // insert entry into main hashtable
  hm_insert(&g_data.db, &ent->node);

  // restore the ttl
  if (has_ttl){
    uint64_t now_ms = get_monotonic_msec();
    int64_t remaining_ms = (int64_t)(expire_at - now_ms);
    entry_set_ttl(ent, remaining_ms);
  }
  
  return true;
}

static bool rdb_load(const char *filename){
  // open and read entire file into memory
  FILE *fp = fopen(filename, "rb");
  if (!fp){
    fprintf(stderr, "rdb_load: no dump file found, starting fresh\n");
    return true;
  }

  // get the file size
  fseek(fp, 0, SEEK_END);
  size_t file_size = (size_t)ftell(fp);
  fseek(fp, 0, SEEK_SET);

  // minimun 18 bytes
  if (file_size < 18){
    fprintf(stderr, "rdb_load: file too samll, corrupted?\n");
    fclose(fp);
    return false;
  }
  // we load everything into memory
  uint8_t *data = new uint8_t[file_size];
  if (fread(data, 1, file_size, fp) != file_size){
    fprintf(stderr, "rdb_load: short read\n");
    fclose(fp);
    delete [] data;
    return false;
  }
  fclose(fp);

  // verify headers
  // magic
  if (memcmp(data, "MYRED", 5) != 0){
    fprintf(stderr, "rdb_load: bad magic number\n");
    delete [] data;
    return false;
  }
  // version
  uint32_t version = 0;
  memcpy(&version, data + 5, 4);
  if (version != 2){
    // old format — no CRC, load normally
    fprintf(stderr, "rdb_load: unsopported version %u\n", version);
    delete [] data;
    return false;
  }

  // verify CRC
  size_t content_size = file_size - 4;
  uint32_t stored_crc = 0;
  memcpy(&stored_crc, data + content_size, 4);

  uint32_t computed_crc = crc32_compute(data, content_size);
  if (stored_crc != computed_crc){
    fprintf(stderr,"rdb_load: CRC mismatch, stored=0x%08x computed=0x%08x File is corrupted!\n", stored_crc, computed_crc);
    delete [] data;
    return false;
  }

  // parse entries 
  // cursor starts after magic(5) + version(4) + count(4)
  // cursor ends before eof(1) + crc(4)
  uint32_t n_entries = 0;
  memcpy(&n_entries, data + 9, 4);

  // set up the cursor
  RDBCursor c;
  c.pos = data + 13;
  c.end = data + content_size; // one past eof byte

  // read entries
  uint32_t loaded = 0;
  for (uint32_t i = 0; i < n_entries ; ++i){
    // read the type byte
    uint8_t type = 0;
    if (!cursor_read_u8(&c, &type)){
      delete [] data;
      return false;
    }

    bool ok = false;
    if (type == 0x00){
      // string entry
      ok = rdb_load_string_entry(&c);
    } else if (type == 0x01){
      // zset entry
      ok = rdb_load_zset_entry(&c);
    } else {
      // unknown type
      fprintf(stderr, "rdb_load: unknown entry, type %u\n", type);
      delete [] data;
      return false;
    }

    if (!ok){
      delete [] data;
      return false;
    }
    loaded++;
  }
  // verify eof
  uint8_t eof_marker = 0;
  if (!cursor_read_u8(&c, &eof_marker)){
    delete [] data;
    return false;
  }
  
  // eof
  uint8_t eof = 0;
  if (!cursor_read_u8(&c, &eof) || eof != 0xFF){
    fprintf(stderr, "rdb_load: bad EOF marker 0x%02x, file may be truncated\n", eof_marker);
    delete [] data;
    return false;
  }
  fprintf(stderr, "rdb_load; loaded %u entries from %s\n", loaded, filename);
  delete [] data;
  return true;
}


// Key for searching in the hashtable
struct LookupKey {
  struct HNode node; // hashtable node
  std::string key;
};

//equality comparison for the top level hash table
static bool entry_eq(HNode *node, HNode *key){
  Entry *ent = container_of(node, Entry, node);
  LookupKey *keydata = container_of(key, LookupKey, node);
  return ent->key == keydata->key;
}

//gets a value from key
static void do_get(std::vector<std::string> &cmd, Buffer *out){
  // we create a dummy entry just for the lookup
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable lookup
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){
    return out_nil(out);
  }
  // we copy the value
  Entry *ent = container_of(node, Entry, node);
  if (ent->type != T_STR) {
    return out_err(out, ERR_BAD_TYP, "not a string value");
  }
  return out_str(out, ent->str);
}

// sets a key with value in the hashtab
static void do_set(std::vector<std::string> &cmd, Buffer *out){
  // again with the dummy
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable lookup
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (node){
    //found, update the value
    Entry *ent = container_of(node, Entry, node);
    if (ent->type != T_STR) {
      return out_err(out, ERR_BAD_TYP, "a non-string value exists");
    }
    ent->str.swap(cmd[2]);
  } else {
    //not found allocate & insert a new pair
    Entry *ent = new Entry();
    ent->key.swap(key.key);
    ent->node.hcode = key.node.hcode;
    ent->str.swap(cmd[2]);
    ent->type = T_STR;
    hm_insert(&g_data.db, &ent->node);
  }
  return out_nil(out);
}

// deletes a key and value
static void do_del(std::vector<std::string> &cmd, Buffer *out){
  // a dummy again
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  //hashtable delete
  HNode *node = hm_delete(&g_data.db, &key.node, &entry_eq);
  if (node){
    //deallocate the memory
    entry_del(container_of(node, Entry, node));
  }
  return out_int(out, node ? 1 : 0); // number of deleted keys
}

// Returns all the keys from the hashtable
static bool cb_keys(HNode *node, void *arg){
  Buffer *out = (Buffer *)arg;
  const std::string &key = container_of(node, Entry, node)->key;
  out_str(out, key);
  return true;
}

static void do_keys(std::vector<std::string> &, Buffer *out){
  out_arr(out, (uint32_t)hm_size(&g_data.db));
  hm_foreach(&g_data.db, &cb_keys, (void *)out);
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

// set or remove the TTL
static void entry_set_ttl(Entry *ent, int64_t ttl_ms){
  if (ttl_ms < 0 && ent->heap_idx != (size_t)-1){
    // negative ttl -> remove ttl
    heap_delete(g_data.heap, ent->heap_idx);
    ent->heap_idx = -1;
  } else if (ttl_ms >= 0){
    // we add or update the data structure
    uint64_t expire_at = get_monotonic_msec() + (uint64_t)ttl_ms;
    HeapItem item = {expire_at, &ent->heap_idx};
    heap_upsert(g_data.heap, ent->heap_idx, item);
  }
}

// PEXPIRE key ttl_ms
static void do_expire(std::vector<std::string> &cmd, Buffer *out){
  int64_t ttl_ms = 0;
  if (!str2int(cmd[2], ttl_ms)){
    return out_err(out, ERR_BAD_ARG, "expected int64");
  }
  // lookup the key
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  // we set the ttl
  if (node){
    Entry *ent = container_of(node, Entry, node);
    entry_set_ttl(ent, ttl_ms);
  }
  return out_int(out, node ? 1 : 0);
}

// PTTL key
static void do_ttl(std::vector<std::string> &cmd, Buffer *out){
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());

  HNode *node = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!node){
    return out_int(out, -2); // not found
  }

  Entry *ent = container_of(node, Entry, node);
  if (ent->heap_idx == (size_t)-1){
    return out_int(out, -1); // not ttl
  }

  uint64_t expire_at = g_data.heap[ent->heap_idx].val;
  uint64_t now_ms = get_monotonic_msec();
  return out_int(out, expire_at > now_ms ? (expire_at - now_ms) : 0);
}

// zadd and zset (score, name)
static void do_zadd(std::vector<std::string> &cmd, Buffer *out){
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return out_err(out, ERR_BAD_ARG, "expect float");
  }

  // look up or create the zset
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);

  Entry *ent = NULL;
  //insert a new key
  if (!hnode){
    ent = entry_new(T_ZSET);
    ent->key.swap(key.key);
    ent->node.hcode = key.node.hcode;
    hm_insert(&g_data.db, &ent->node);
    
  } else { // check the existing key
    ent = container_of(hnode, Entry, node);
    if (ent->type != T_ZSET){
      return out_err(out, ERR_BAD_TYP, "expect zset");
    }
  }

  // add or update the tuple
  const std::string &name = cmd[3];
  bool added = zset_insert(&ent->zset, name.data(), name.size(), score);
  return out_int(out, (int64_t)added);
}

// empty zset (?? i am going to explode myself)
static const ZSet k_empty_zset;

// search an expected zset
static ZSet *expect_zset(std::string &s){
  LookupKey key;
  key.key.swap(s);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);
  if (!hnode) { // no key == treated as an empty zset
    return (ZSet *)&k_empty_zset;
  }
  Entry *ent = container_of(hnode, Entry, node);
  return ent->type == T_ZSET ? &ent->zset : NULL;
}

// zrem zset name (search and remove)
static void do_zrem( std::vector<std::string> &cmd, Buffer *out){
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){
    return out_err(out, ERR_BAD_TYP, "expect zset");
  }

  const std::string &name = cmd[2];
  ZNode *znode = zset_lookup(zset, name.data(), name.size());
  if (znode){
    zset_delete(zset, znode);
  }
  return out_int(out, znode ? 1 : 0);
}

// zscore zset name (search the score of a name)
static void do_zscore(std::vector<std::string> &cmd, Buffer *out){
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){
    return out_err(out, ERR_BAD_TYP, "expecte set");
  }

  const std::string &name = cmd[2];
  ZNode *znode = zset_lookup(zset, name.data(), name.size());
  return znode ? out_dbl(out, znode->score) : out_nil(out);
}

// zquery zset score name offset limit (search by ascending order)
static void do_zquery(std::vector<std::string> &cmd, Buffer *out){
  // we parse the args
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return out_err(out, ERR_BAD_ARG, "expected dbl ");
  }
  const std::string &name = cmd[3];
  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return out_err(out, ERR_BAD_ARG, "expected int");
  }

  // we get the zset 
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){
    return out_err(out, ERR_BAD_TYP, "expected zset");
  }

  //seek the key
  if (limit <= 0){
    return out_arr(out, 0);
  }

  ZNode *znode = zset_seekge(zset, score, name.data(), name.size());
  znode = znode_offset(znode, offset);

  // out put
  size_t ctx = out_begin_arr(out);
  int64_t n = 0;
  while (znode && n < limit){
    out_str(out, std::string(znode->name, znode->len));
    out_dbl(out, znode->score);
    znode = znode_offset(znode, +1);
    n += 2;
  }
  out_end_arr(out, ctx, (uint32_t)n);
}

// reverse order from do_zquery (descending order)
static void do_zquery_reversed(std::vector<std::string> &cmd, Buffer *out){
  // we parse the args
  double score = 0;
  if (!str2dbl(cmd[2], score)){
    return out_err(out, ERR_BAD_ARG, "expected dbl ");
  }
  const std::string &name = cmd[3];
  int64_t offset = 0, limit = 0;
  if (!str2int(cmd[4], offset) || !str2int(cmd[5], limit)){
    return out_err(out, ERR_BAD_ARG, "expected int");
  }

  // we get the zset 
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){
    return out_err(out, ERR_BAD_TYP, "expected zset");
  }

  //seek the key
  if (limit <= 0){
    return out_arr(out, 0);
  }

  ZNode *znode = zset_seekle(zset, score, name.data(), name.size());
  znode = znode_offset(znode, -offset);

  // out put
  size_t ctx = out_begin_arr(out);
  int64_t n = 0;
  while (znode && n < limit){
    out_str(out, std::string(znode->name, znode->len));
    out_dbl(out, znode->score);
    znode = znode_offset(znode, -1);
    n += 2;
  }
  out_end_arr(out, ctx, (uint32_t)n);
}

//zrank key name (how many nodes comes before the actual one)
static void do_zrank(std::vector<std::string> &cmd, Buffer *out){
  // cmd[1] = key
  ZSet *zset = expect_zset(cmd[1]);
  if (!zset){
    return out_err(out, ERR_BAD_TYP, "expected zset");
  }

  // point query by name using the hashtable (cmd[2] = name)
  ZNode *znode = zset_lookup(zset, cmd[2].data(), cmd[2].size());
  if (!znode){
    // name do not exist
    return out_nil(out);
  }

  int64_t rank = avl_rank(&znode->tree);
  return out_int(out, rank);
}

// asyncdel key - delete in background
static void do_asyncdel(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  // look the key
  LookupKey key;
  key.key.swap(cmd[1]);
  key.node.hcode = str_hash((uint8_t *)key.key.data(), key.key.size());
  HNode *hnode = hm_lookup(&g_data.db, &key.node, &entry_eq);

  if (!hnode){
    return out_int(out, 0); // key does not exit
  }
  Entry *ent = container_of(hnode, Entry, node);
  // remove from hashtable and heap
  hm_delete(&g_data.db, &ent->node, &hnode_same);
  entry_set_ttl(ent, -1);

  // check if need offloading
  size_t set_size = (ent->type ==  T_ZSET) ? hm_size(&ent->zset.hmap) : 0;

  if (set_size > 1000){
    thread_pool_queue(&g_data.thread_pool, entry_del_func, ent);
  } else {
    entry_del_sync(ent);
  }
  return out_int(out, 1);

}

static void do_save(std::vector<std::string> &cmd, Buffer *out){
  (void)cmd;
  // we run in the thread pool
  thread_pool_queue(&g_data.thread_pool, [] (void *) { rdb_save("dump.rdb"); }, nullptr);
  out_str(out, "OK");
}

static void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn){
  if (cmd.size() == 2 && cmd[0] == "get"){
    return do_get(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "set"){
    g_data.writes_since_save++;
    return do_set(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "del"){
    g_data.writes_since_save++;
    return do_del (cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "asyncdel" ) {
    g_data.writes_since_save++;
    do_asyncdel(cmd, out, conn);
  }else if (cmd.size() == 3 && cmd[0] == "pexpire") {
    g_data.writes_since_save++;
    return do_expire(cmd, out);
  } else if (cmd.size() == 2 && cmd[0] == "pttl"){
    return do_ttl(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "keys"){
    return do_keys(cmd, out);
  } else if (cmd.size() == 4 && cmd[0] == "zadd"){
    g_data.writes_since_save++;
    return do_zadd(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zrem"){
    g_data.writes_since_save++;
    return do_zrem(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zscore"){
    return do_zscore(cmd, out);
  } else if (cmd.size() == 6 && cmd[0] == "zquery"){
    return do_zquery(cmd, out);
  } else if (cmd.size() == 6 && cmd[0] == "zrevquery"){
    return do_zquery_reversed(cmd, out);
  } else if (cmd.size() == 3 && cmd[0] == "zrank"){
    return do_zrank(cmd, out);
  } else if (cmd.size() == 1 && cmd[0] == "save"){
    return do_save(cmd, out);
  } else {
    return out_err(out, ERR_UNKNOWN, "unknown command");
  }
}

// helper response functions

static void response_begin(Buffer *out, size_t *header){
  *header = buf_size(out); //message header position 
  uint32_t placeholder = 0;
  buf_append(out, (uint8_t *)&placeholder, 4);
}

static size_t response_size(Buffer *out, size_t header){
  return buf_size(out) - header - 4;
}

static void response_end(Buffer *out, size_t header){
  size_t msg_size = response_size(out, header);

  if (msg_size > k_max_msg){
    out->data_end =  out->data_begin + header + 4;
    // reflects the error and not the original message
    out_err(out, ERR_TOO_BIG, "message too big"); 
    msg_size = response_size(out, header); 
  }
  // message header
  uint32_t len = (uint32_t)msg_size;
  memcpy(out->data_begin + header, &len, 4);
}

// Timers logic 
static int32_t next_timer_ms() {
  uint64_t now_ms = get_monotonic_msec();
  uint64_t next_ms = (uint64_t)-1; // maximun value

  // check the front of the idle_list 
  if (!dlist_empty(&g_data.idle_list)){
    Conn *conn = container_of(g_data.idle_list.next, Conn, idle_node);
    next_ms = conn->last_active_ms + k_idle_timeout_ms;
  }
  // check the front of the io_list 
  if (!dlist_empty(&g_data.io_list)){
    Conn *conn = container_of(g_data.io_list.next, Conn, idle_node);
    next_ms = conn->last_active_ms + k_io_timeout_ms;
  }
  // check the heap
  if (!g_data.heap.empty()){
    next_ms = std::min(next_ms, g_data.heap[0].val);
  }
  // check the periodic wake up
  if (g_data.writes_since_save > 0){
    uint64_t next_save = g_data.last_save_ms + k_save_interval_ms;
    next_ms = std::min(next_ms, next_save);
  }
  // no timers 
  if (next_ms == (uint64_t)-1){ return -1; }
  // already expired
  if (next_ms <= now_ms){ return 0; }
  // rare case idk ??
  if (next_ms == UINT64_MAX) {return -1;}

  return (int32_t)(next_ms - now_ms);
}

static void process_timers(){
  uint64_t now_ms = get_monotonic_msec();
  // This handles expired idle timers
  while (!dlist_empty(&g_data.idle_list)){
    Conn *conn = container_of(g_data.idle_list.next, Conn, idle_node);
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
    Conn *conn = container_of(g_data.io_list.next, Conn, idle_node);
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
  while(!heap.empty() && heap[0].val < now_ms){
    Entry *ent = container_of(heap[0].ref, Entry, heap_idx);
    HNode *node = hm_delete(&g_data.db, &ent->node, &hnode_same);
    assert(node == &ent->node);
    fprintf(stderr, "key expired: %s\n", ent->key.c_str());
    entry_del(ent);
    if (nworks++ >= k_max_works){
      break;
    }
  }
  // periodic RDB save
  bool time_elapsed = (now_ms - g_data.last_save_ms) >= k_save_interval_ms;
  bool dirty_enough = g_data.writes_since_save >= k_save_after_writes;

  if (time_elapsed && dirty_enough){
    g_data.last_save_ms = now_ms;
    g_data.writes_since_save = 0;
    // run it in thread pool
    thread_pool_queue(&g_data.thread_pool, [] (void *) { rdb_save("dump.rdb"); }, nullptr);
    fprintf(stderr, "periodic save triggered\n");
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
  // try to parse the accumulated buffer 
  if(buf_size(&conn->incoming) < 4){return false;}

  uint32_t len = 0;
  memcpy(&len, buf_data(&conn->incoming), 4);

  //len is the message header
  if (len > k_max_msg) {
    conn->want_close = true;
    return false;
  }
  // this is the message body
  if (4 + len > buf_size(&conn->incoming)){return false;}

  
  const uint8_t *request = buf_data(&conn->incoming) + 4;
  // here we are going to procces the parsed message
  std::vector<std::string> cmd ;
  if (parse_req(request, len, cmd) < 0){
    conn->want_close = true;
    return false;
  }
 
  size_t header_pos = 0;
  response_begin(&conn->outgoing, &header_pos);
  do_request(cmd, &conn->outgoing, conn);

  response_end(&conn->outgoing, header_pos);

  // application logic done
  buf_consume(&conn->incoming, 4 + len);
  conn_set_timer(conn, ConnTimer::IDLE);
  return true;

}

static void handle_write(Conn *conn){
  assert(buf_size(&conn->outgoing) > 0);
  ssize_t rv = write(conn->fd, buf_data(&conn->outgoing), buf_size(&conn->outgoing));
  if(rv < 0 && errno == EAGAIN){return;}

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

int main(){

  // control for Ctrl-c
  signal(SIGINT,  signal_handler);
  signal(SIGTERM, signal_handler);

  g_data.last_save_ms = get_monotonic_msec();

  // initialiaze idle connection list and io waiting list
  dlist_init(&g_data.idle_list);
  dlist_init(&g_data.io_list);

  // Initialiaze the thread pool and result queue 
  thread_pool_init(&g_data.thread_pool, 4);

  if (!rdb_load("dump.rdb")){
    fprintf(stderr, "rdb_load failed, starting with empty database\n");
  }

  int fd = socket(AF_INET,SOCK_STREAM,0); // obtain a socket handle
  if (fd < 0) {die("socket()");}

  int val = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &val, sizeof(val)); // set the socket option like the time wait for the socket

  // the is the parameter bind to 0.0.0.0: 1234
  struct sockaddr_in addr = {};
  addr.sin_family = AF_INET;
  addr.sin_port = ntohs(1234);
  addr.sin_addr.s_addr = ntohl(0);

  int rv = bind(fd, (const struct sockaddr *)&addr, sizeof(addr));
  if (rv) {die("bind()");}

  fd_set_nb(fd);

  // listen for connections on the socket
  rv = listen(fd, SOMAXCONN);
  if (rv) {die("listen()");}

  std::vector<struct pollfd> poll_args; // This a vector of structs for arguments for poll_args

  while(!g_stop){
    
    poll_args.clear(); //This just clean the arguments for poll.
    struct pollfd pfd = {fd, POLLIN, 0};
    poll_args.push_back(pfd);
    //So everething else are just connected sockets 

    for (Conn *conn : g_data.fd2conn){
      if(!conn){continue;}

      struct pollfd pfd = {conn->fd, POLLERR, 0}; // This is for the flags of the aplication
      if (conn->want_read){
        pfd.events |= POLLIN;
      }
      if (conn->want_write){
        pfd.events |= POLLOUT;
      }
      poll_args.push_back(pfd);
    }
    int32_t timeout_ms = next_timer_ms();
    int rv = poll(poll_args.data(), (nfds_t)poll_args.size(), timeout_ms);

    if(rv < 0 && errno == EINTR){continue;}
    if(rv < 0){die("poll");}

    // handle the listening socket
    if (poll_args[0].revents) {
      handle_accept(fd);
    }

    //This is for to handle the connections of sockets
    for(size_t i = 1;i < poll_args.size(); i++){ // we skip the 1st
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
    // handle timers
    process_timers();
  }
  fprintf(stderr, "shutting down, saving...\n");
  rdb_save("dump.rdb");
  return 0;
}