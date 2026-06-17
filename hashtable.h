#pragma once 

#include <stddef.h>
#include <stdint.h>

//hashtable node
struct HNode
{
    HNode *next = NULL; // next node in the same bucket (NULL = end of chain)
    uint64_t hcode = 0; // This is the hash of the key
};

// a fixed-size hashtable
struct  HTab
{
    HNode **tab = NULL; // array of buckets heads
    size_t mask = 0; // always n - 1 where n is slot count
    size_t size = 0; // current number of keys stored
};

// the public hashtable struct
// it uses 2 hashtables for progressive rehashing
struct HMap 
{
    HTab newer; // active table
    HTab older; // draining table, only exists on rehashing
    size_t migrate_pos = 0; // next bucket index in older to migrate 
};

//get
HNode *hm_lookup(HMap *hmap, HNode *key, bool(*eq)(HNode *,HNode *));
//set
void hm_insert(HMap *hmap, HNode *node);
//del
HNode *hm_delete(HMap *hmap, HNode *key, bool(*eq)(HNode *,HNode *));
void hm_clear(HMap *hmap);
size_t hm_size(HMap *hmap);
//invokes the callback on each node until it returns false
void hm_foreach(HMap *hmap, bool(*f)(HNode *, void *), void *arg);
uint64_t hm_scan(HMap *hmap, uint64_t cursor, size_t count, void(*cb)(HNode *, void *), void *arg);