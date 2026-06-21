#include "rdb.h"
#include "state.h"      
#include "buffer.h"
#include "common.h"
#include "buffer.h"
#include "hash.h"
#include "set.h"
#include <zlib.h>  
#include <stdio.h> 
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

pid_t g_rdb_child_pid = -1;

// Convert an in memort monotonic expiry into a wall clock expirary for the rdb
static uint64_t mono_expired_to_wall(uint64_t mono_expire){
  uint64_t mono_now = get_monotonic_msec();
  // ms left until expirary 
  int64_t remaining = (int64_t)(mono_expire - mono_now);
  // already expired
  if (remaining < 0) { remaining = 0; }
  return get_wall_msec() + (uint64_t)remaining;
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

// Compress and decompress functions
static uint8_t *rdb_compress(const uint8_t *src, size_t src_len, size_t *out_len){
  uLongf dest_len = compressBound((uLong)src_len);
  uint8_t *dest = new uint8_t[dest_len];

  int rv = compress2(dest, &dest_len, src, (uLong)src_len, Z_BEST_COMPRESSION);
  if (rv != Z_OK){
    fprintf(stderr, "rdb_compress: failed (%d)\n", rv);
    delete [] dest;
    return NULL;
  }
  *out_len = (size_t)dest_len;
  return dest;
}

static uint8_t *rdb_decompress(const uint8_t *src, size_t src_len, size_t expected_len){
  uint8_t *dest = new uint8_t[expected_len];
  uLongf dest_len = (uLongf)expected_len;

  int rv = uncompress(dest, &dest_len, src, (uLong)src_len);
  if (rv != Z_OK){
    fprintf(stderr, "rdb_decompress: failed (%d)\n", rv);
    delete [] dest;
    return NULL;
  }
  return dest;
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

/*
  T_DLIST does not need a iterator because this data structure has a count implement in it
*/

// for the hash iterator
struct HashSaveCtx {
  Buffer *buf;
  uint32_t count;
};

// for the set iterator
struct SetSaveCtx {
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

static bool cb_hash_member(HNode *node, void *arg){
  HashSaveCtx *ctx = (HashSaveCtx *)arg;
  HashNode *hn = container_of(node, HashNode, node);
  buf_append_str(ctx->buf, hn->field.data(), (uint32_t)hn->field.size());
  buf_append_str(ctx->buf, hn->value.data(), (uint32_t)hn->value.size());
  ctx->count++;
  return true;
}

static bool cb_set_member(HNode *node, void *arg){
  SetSaveCtx *ctx = (SetSaveCtx *)arg;
  SetNode *sn = container_of(node, SetNode, node);
  buf_append_str(ctx->buf, sn->member.data(), (uint32_t)sn->member.size());
  ctx->count++;
  return true;
}

static bool cb_rdb_write(HNode *node, void *arg){
  RDBWriteCtx *ctx = (RDBWriteCtx *)arg;
  Entry *ent = container_of(node, Entry, node);
  // strings
  if (ent->type == T_STR){
    // type tag byte
    buf_append(ctx->buf, 0);
    
    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1); // has ttl
      buf_append_u64(ctx->buf, mono_expired_to_wall(g_data.heap[ent->heap_idx].val)); // expire at
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
      buf_append_u64(ctx->buf, mono_expired_to_wall(g_data.heap[ent->heap_idx].val));
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

  } else if (ent->type == T_DLIST){
    buf_append(ctx->buf, 2); // type list
    // ttl
    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1);
      buf_append_u64(ctx->buf, mono_expired_to_wall(g_data.heap[ent->heap_idx].val));
    } else {
      buf_append(ctx->buf, 0);
    }

    // key
    buf_append_str(ctx->buf, ent->key.data(), ent->key.size());

    // element count
    uint32_t n = (uint32_t)ent->deque.count;
    buf_append(ctx->buf, (const uint8_t *)&n, 4);

    // element in logical order
    for (size_t i = 0; i < ent->deque.count; ++i){
      const std::string *val = deque_get(&ent->deque, i);
      buf_append_str(ctx->buf, val->data(), (uint32_t)val->size());
    }
  } else if (ent->type == T_HASH){
      buf_append(ctx->buf, 3);
    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1);
      buf_append_u64(ctx->buf, mono_expired_to_wall(g_data.heap[ent->heap_idx].val));
    } else {
      buf_append(ctx->buf, 0);
    }
    buf_append_str(ctx->buf, ent->key.data(), (uint32_t)ent->key.size());

    size_t cnt_idx = buf_size(ctx->buf);
    buf_append(ctx->buf, (const uint8_t *)"\0\0\0\0", 4);
    HashSaveCtx hctx { ctx->buf, 0 };
    hm_foreach(&ent->hash, cb_hash_member, &hctx);
    memcpy(ctx->buf->data_begin + cnt_idx, &hctx.count, 4);
  } else if (ent->type == T_SET){
    buf_append(ctx->buf, 4); 
    if (ent->heap_idx != (size_t)-1){
      buf_append(ctx->buf, 1);
      buf_append_u64(ctx->buf, mono_expired_to_wall(g_data.heap[ent->heap_idx].val));
    } else {
      buf_append(ctx->buf, 0);
    }
    buf_append_str(ctx->buf, ent->key.data(), (uint32_t)ent->key.size());
    size_t cnt_idx = buf_size(ctx->buf);
    buf_append(ctx->buf, (const uint8_t *)"\0\0\0\0", 4);
    SetSaveCtx sctx { ctx->buf, 0 };
    hm_foreach(&ent->set, cb_set_member, &sctx);
    memcpy(ctx->buf->data_begin + cnt_idx, &sctx.count, 4);
  }
  ctx->count++;
  return true;
}

struct RDBStats{
  uint32_t entries;
  size_t bytes;
};

static void rdb_serialize(Buffer *buf, RDBStats *stats){
  // magic
  const char *magic = "MYRED";
  buf_append(buf, (const uint8_t *)magic, 5);

  // version
  uint32_t version = 3;
  buf_append(buf, (const uint8_t *)&version, 4);

  // flags placeholder
  size_t flags_index = buf_size(buf);
  // 0x01 compressed , 0x00 uncompressed
  buf_append(buf, 1);

  // index of the buffer (even if the buffer reallocates)
  size_t count_index = buf_size(buf);
  uint32_t dummy = 0;
  // we put dummy bytes
  buf_append(buf, (const uint8_t *)&dummy, 4);

  // serialize entries into separate payload
  Buffer payload = buf_create(4096);

  // this is what can cause the buffer to reallocate
  RDBWriteCtx ctx;
  ctx.buf = &payload;
  ctx.count = 0;
  hm_foreach(&g_data.db, &cb_rdb_write, &ctx);

  // eof marker goes in payload
  buf_append(&payload, 255);

  //we repatch the dummy bytes
  memcpy(buf->data_begin + count_index, &ctx.count, 4);

  size_t payload_size = buf_size(&payload);

  // compress or store raw
  if (payload_size >= k_compress_threshold){
    size_t compressed_size = 0;
    uint8_t *compressed = rdb_compress(payload.data_begin, payload_size, &compressed_size);
    if (compressed && compressed_size < payload_size){
      buf->data_begin[flags_index] = 0x01;

      // uncompressed size so decompress knows how much to allocate
      uint32_t uncompressed_u32 = (uint32_t)payload_size;
      buf_append(buf, (const uint8_t *)&uncompressed_u32, 4);

      // compressed data
      buf_append(buf, compressed, compressed_size);

      fprintf(stderr, "rdb_serialize: compressed %zu -> %zu bytes (%.1f%%)\n",
      payload_size, compressed_size, 100.0 * compressed_size / payload_size);
    } else {
      // compression made it bigger
      buf->data_begin[flags_index] = 0x00;
      buf_append(buf, payload.data_begin, payload_size);
    }

    if (compressed) { delete [] compressed; }
  } else {
    // too small to compress
    buf->data_begin[flags_index] = 0x00;
    buf_append(buf, payload.data_begin, payload_size);
  }
  buf_destroy(&payload);

  // CRC32 
  size_t data_size = buf_size(buf);
  uint32_t crc = crc32_compute(buf->data_begin, buf_size(buf));
  buf_append(buf, (const uint8_t *)&crc, 4);

  stats->entries = ctx.count;
  stats->bytes = data_size + 4;
  fprintf(stderr, "rdb_serialize: %zu bytes, %u entries, crc=0x%08x\n", stats->bytes, stats->entries, crc);
}

// we build the rdb function
bool rdb_save(const char* filename){
  // build the buffer
  Buffer buf = buf_create(64 * 1024);
  RDBStats stats = {};
  rdb_serialize(&buf, &stats);

  char tmp[256];
  snprintf(tmp, sizeof(tmp), "%s.tmp.%d", filename, (int)getpid());
  FILE *fp = fopen(tmp, "wb");
  if (!fp){
    fprintf(stderr, "rdb_save: cannot open %s: %s\n", tmp, strerror(errno));
    buf_destroy(&buf);
    return false;
  }
  size_t data_size = buf_size(&buf);
  size_t written = fwrite(buf.data_begin, 1, buf_size(&buf), fp);
  buf_destroy(&buf); 

  if (written != data_size) {
    fprintf(stderr, "rdb_save: short write\n");
    fclose(fp);
    remove(tmp);
    return false;
  }

  // force the data into disk
  if (fsync((fileno(fp))) != 0){
    fprintf(stderr, "rdb_save; fsync failed: %s\n", strerror(errno));
    fclose(fp);
    remove(tmp);
    return false;
  }
  fclose(fp);

  if (rename(tmp, filename) != 0){
    fprintf(stderr, "rdb_save: rename failed: %s\n", strerror(errno));
    remove(tmp);
    return false;
  }
  g_data.g_last_save_size_bytes = stats.bytes;
  fprintf(stderr, "rdb_save: done (%zu bytes, %u entries)\n", stats.bytes, stats.entries);
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

// Entries 
static bool rdb_load_string_entry(RDBCursor *c){
  // has a ttl flag
  uint8_t has_ttl = 0;
  if (!cursor_read_u8(c, &has_ttl)){ return false; }

  // read expire_at if TTL exists
  uint64_t expire_at = 0;
  if (has_ttl){
    if (!cursor_read_u64(c, &expire_at)){ return false; }
    // check if expired 
    if (expire_at <= get_wall_msec()){
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
    uint64_t now_ms = get_wall_msec();
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
    uint64_t now_ms = get_wall_msec();
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
    uint64_t now_ms = get_wall_msec();
    int64_t remaining_ms = (int64_t)(expire_at - now_ms);
    entry_set_ttl(ent, remaining_ms);
  }
  
  return true;
}

static bool rdb_load_deque_entry(RDBCursor *c){
  uint8_t has_ttl = 0;
  if (!cursor_read_u8(c, &has_ttl)) {return false; }

  uint64_t expire_at = 0;
  if (has_ttl){
     if (!cursor_read_u64(c, &expire_at)) { return false; }
     if (expire_at <= get_wall_msec()){
      // expired so we read and discard
      std::string key;
      uint32_t n = 0;
      cursor_read_str(c, &key);
      cursor_read_u32(c, &n);
      for (uint32_t i = 0; i < n; ++i){
        std::string tmp;
        cursor_read_str(c, &tmp);
      }
      return true;
     }
  }

  std::string key;
  if (!cursor_read_str(c, &key)) { return false; }
  
  uint32_t n = 0;
  if (!cursor_read_u32(c, &n)) { return false; }

  Entry  *ent = entry_new(T_DLIST);
  deque_init(&ent->deque);
  ent->key = key;
  ent->node.hcode = str_hash((uint8_t *)ent->key.data(), ent->key.size());

  for (uint32_t i = 0; i < n; ++i){
    std::string val;
    if (!cursor_read_str(c, &val)) {
      entry_del_sync(ent);
      return false;
    }
    deque_push_back(&ent->deque, val);
  }

  hm_insert(&g_data.db, &ent->node);
  if (has_ttl){
    uint64_t now_ms = get_wall_msec();
    entry_set_ttl(ent, (int64_t)(expire_at - now_ms));
  }
  return true;
}

static bool rdb_load_hash_entry(RDBCursor *c){
  uint8_t has_ttl = 0;

  if (!cursor_read_u8(c, &has_ttl)){ return false; }

  uint64_t expire_at = 0;
  if (has_ttl){
    if (!cursor_read_u64(c, &expire_at)){ return false; }
    // if expired, read and discard
    if (expire_at <= get_wall_msec()){
      std::string key; uint32_t n = 0;
      cursor_read_str(c, &key);
      cursor_read_u32(c, &n);
      for (uint32_t i = 0; i < n; ++i){
        std::string f, v;
        cursor_read_str(c, &f);
        cursor_read_str(c, &v);
      }
      return true;
    }
  }

  std::string key;
  if (!cursor_read_str(c, &key)){ return false; }
  uint32_t n = 0;
  if (!cursor_read_u32(c, &n)){ return false; }

  Entry *ent = entry_new(T_HASH);
  ent->key = key;
  ent->node.hcode = str_hash((uint8_t *)ent->key.data(), ent->key.size());

  for (uint32_t i = 0; i < n; ++i){
    std::string f, v;
    if (!cursor_read_str(c, &f) || !cursor_read_str(c, &v)){
      entry_del(ent);
      return false;
    }
    hash_set(&ent->hash, f, v);
  }

  hm_insert(&g_data.db, &ent->node);
  if (has_ttl){
    entry_set_ttl(ent, (int64_t)(expire_at - get_wall_msec()));
  }
  return true;
}

static bool rdb_load_set_entry(RDBCursor *c){
  uint8_t has_ttl = 0;

  if (!cursor_read_u8(c, &has_ttl)){ return false; }
  uint64_t expire_at = 0;
  if (has_ttl){
    if (!cursor_read_u64(c, &expire_at)){ return false; }
    if (expire_at <= get_wall_msec()){
      std::string key; uint32_t n = 0;
      cursor_read_str(c, &key);
      cursor_read_u32(c, &n);
      for (uint32_t i = 0; i < n; ++i){ std::string m; cursor_read_str(c, &m); }
      fprintf(stderr, "rdb_load: skipping expired set\n");
      return true;
    }
  }
  std::string key;
  if (!cursor_read_str(c, &key)){ return false; }
  uint32_t n = 0;
  if (!cursor_read_u32(c, &n)) { return false; }

  Entry *ent = entry_new(T_SET);
  ent->key = key;
  ent->node.hcode = str_hash((uint8_t *)ent->key.data(), ent->key.size());
  for (uint32_t i = 0; i < n; ++i){
    std::string m;
    if (!cursor_read_str(c, &m)){ return false; }
    set_add(&ent->set, m);
  }
  hm_insert(&g_data.db, &ent->node);
  if (has_ttl){
    entry_set_ttl(ent, (int64_t)(expire_at - get_wall_msec()));
  }
  return true;
}

static bool rdb_parse_entries(const uint8_t *payload, size_t payload_size, uint32_t n_entries){
  RDBCursor c;
  c.pos = payload;
  c.end = payload + payload_size; 

  uint32_t loaded = 0;
  for (uint32_t i = 0; i < n_entries; ++i){
    uint8_t type = 0;
    if (!cursor_read_u8(&c, &type)) { return false; }

    bool ok = false;
    if (type == 0x00){
      ok = rdb_load_string_entry(&c);
    } else if (type == 0x01){
      ok = rdb_load_zset_entry(&c);
    } else if (type == 0x02){
      ok = rdb_load_deque_entry(&c);
    } else if (type == 0x03){
      ok = rdb_load_hash_entry(&c);
    } else if (type == 0x04){
      ok = rdb_load_set_entry(&c);
    } else {
      fprintf(stderr, "rdb_parse: unknown type 0x%02x\n", type);
      return false;
    }
    if (!ok) { return false; }
    loaded++;
  }
  // verify eof marker
  uint8_t eof = 0;
  if (!cursor_read_u8(&c, &eof) || eof != 0xFF){
    fprintf(stderr, "rdb_parse: bad EOF marker\n");
    return false;
  }

  fprintf(stderr, "rdb_load: loaded %u entries\n", loaded);
  return true;

}

static bool rdb_load_file(const char *filename){
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
  if (file_size < 19){
    fprintf(stderr, "rdb_load: file too small, corrupted?\n");
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
  if (version != 3){
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
  fprintf(stderr, "rdb_load: CRC OK\n");

  // read header files
  // cursor starts after magic(5)+version(4)+flags(1)+count(4) = 14
  // cursor ends before eof(1) + crc(4)
  uint8_t flags = data[9];
  bool compressed = (flags & 0x01) != 0;
  uint32_t n_entries = 0;
  memcpy(&n_entries, data + 10, 4);

  const uint8_t *payload = data + 14;
  size_t payload_size = content_size - 14;

  // decompress if needed
  bool ok = false;
  uint8_t *decompressed = NULL;

  if (compressed){
    // read uncompressed size
    uint32_t uncompressed_size = 0;
    memcpy(&uncompressed_size, payload, 4);

    // compressed data starts after the 4 byte size field
    const uint8_t *compressed_data = payload + 4;
    size_t compressed_size = payload_size - 4;

    decompressed = rdb_decompress(compressed_data, compressed_size, uncompressed_size);

    if (!decompressed){
      delete [] data;
      return false;
    }
    fprintf(stderr, "rdb_load: decompressed %zu -> %u bytes\n", compressed_size, uncompressed_size);
    ok = rdb_parse_entries(decompressed, (size_t)uncompressed_size, n_entries);

    delete [] decompressed;
  } else {
    ok = rdb_parse_entries(payload, payload_size, n_entries);
  }
 
  delete [] data;
  return ok;
}

bool rdb_load(const char *filename){
  // we try primary file first
  if (access(filename, F_OK) == 0){
    fprintf(stderr, "rdb_load: loading %s\n", filename);
    if (rdb_load_file(filename)){ return true; }
    fprintf(stderr, "rdb_load: primary file failed, trying backup\n");
  }
  
  // try backup file
  std::string backup = std::string(filename) + ".bak";
  if (access(backup.c_str(), F_OK) == 0){
    fprintf(stderr, "rdb_load: loading backup %s\n", backup.c_str());
    if (rdb_load_file(backup.c_str())){
      fprintf(stderr, "rdb_load: recovered from backup\n");
      return true;
    }
    fprintf(stderr, "rdb_load: backup also failed\n");
  }

  fprintf(stderr, "rdb_load: no valid dump found, starting fresh\n");
  return true; // start empty 
}

//  rdb save with fork 
void rdb_save_background(){
  // dont start a fork is one is running
  if (g_rdb_child_pid != -1){
    fprintf(stderr, "rdb_save_background: save already in progress (pid=%d)\n", g_rdb_child_pid);
    return;
  }

  pid_t pid = fork();

  if (pid < 0){
    fprintf(stderr, "rdb_save_background: fork failed: %s\n", strerror(errno));
    return;
  }

  if (pid == 0){
    // this is the child proccess and cannot touch the event loop......
    bool ok = rdb_save("dump.rdb");

    // exits with status code
    _exit(ok ? 0 : 1);
  }

  g_rdb_child_pid = pid;
  fprintf(stderr, "rdb_save_background: started (pid=%d)\n", pid);
}   

void rdb_check_background_save(){
  if (g_rdb_child_pid == -1){
    return; // no save running
  }

  int status = 0;
  // returns immediately - don't block
  pid_t result = waitpid(g_rdb_child_pid, &status, WNOHANG);

  if (result == 0){
    // child still running
    return;
  }

  if (result == g_rdb_child_pid){
    // child finished
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0){
      fprintf(stderr, "rdb_save_background: completed succesfully\n");
      g_data.g_last_save_ms = get_monotonic_msec();
      g_data.g_writes_since_save = 0;
      g_data.g_last_save_ok = true;
    } else {
      fprintf(stderr, "rdb_save_background: child failed (status=%d\n", status);
      g_data.g_last_save_ok = false;
    }
    g_rdb_child_pid = -1; // ready for the next save
  }

  if (result < 0){
    // error - clear the pid
    fprintf(stderr, "rdb_save_background: waitpid failed: %s\n", strerror(errno));
    g_rdb_child_pid = -1;
  }
}
