#include "state.h"
#include "common.h"
#include "hash.h"
#include <time.h>
#include <cctype>
#include <cstdlib>

GlobalData g_data;
Config g_config;

// forward declarations: defined lower in this file, used by entry_set_ttl
static void heap_delete(std::vector<HeapItem> &a, size_t pos);
static void heap_upsert(std::vector<HeapItem> &a, size_t pos, HeapItem t);

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
