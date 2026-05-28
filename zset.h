#pragma once

#include "hashtable.h"
#include "avl.h"

//Is the sorted set
struct ZSet {
    AVLNode *root = NULL; // index by score, name
    HMap hmap; // index by name
};

struct ZNode {
    // data structures use
    AVLNode tree; // binary tree
    HNode hmap; // hashtable 
    // data 
    double score = 0; // tuples
    size_t len = 0; // tuples
    char name[0]; // array (can use extra space because is at the end of the struct)
};

// point queries and updates
bool zset_insert(ZSet *zset, const char *name, size_t len, double score);
ZNode *zset_lookup(ZSet *zset, const char *name, size_t len);
void zset_delete(ZSet *zset, ZNode *node);
ZNode *zset_seekge(ZSet *zset, double score, const char *name, size_t len);
void zset_clear(ZSet *zset);
ZNode *znode_offset(ZNode *node, int64_t offset);