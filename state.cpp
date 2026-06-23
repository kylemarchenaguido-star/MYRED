#include "state.h"
#include "common.h"
#include <time.h>
#include "hash.h"

GlobalData g_data;
Config g_config;

// forward declarations: defined lower in this file, used by entry_set_ttl
static void heap_delete(std::vector<HeapItem> &a, size_t pos);
static void heap_upsert(std::vector<HeapItem> &a, size_t pos, HeapItem t);

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
  Entry *ent = container_of(node, Entry, node);
  LookupKey *keydata = container_of(key, LookupKey, node);
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
