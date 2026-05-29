#include <assert.h>
#include <string.h>
#include <stdlib.h>

#include "zset.h"
#include "common.h"

static ZNode *znode_new(const char *name, size_t len, double score){
    ZNode *node = (ZNode *)malloc(sizeof(ZNode) + len); // struct + array
    avl_init(&node->tree);
    node->hmap.next = NULL;
    node->hmap.hcode = str_hash((uint8_t *)name, len);
    node->score = score;
    node->len = len;
    memcpy(&node->name[0], name, len);
    return node;
}

static void znode_del(ZNode *node){ free(node); }

static size_t min(size_t lhs, size_t rhs) {
    return lhs < rhs ? lhs : rhs;
}

// (lhs.score, lhs.name) < (rhs.score, rhs.name)
static bool zless(AVLNode *lhs, double score, const char *name, size_t len) {
    ZNode *zl = container_of(lhs, ZNode, tree);
    if (zl->score != score){
        return zl->score < score;
    }
    int rv = memcmp(zl->name, name, min(zl->len, len));
    if (rv != 0) {
        return rv < 0;
    }
    return zl->len < len;
}

static bool zless(AVLNode *lhs, AVLNode *rhs){
    ZNode *zr = container_of(rhs, ZNode, tree);
    //overload of zless
    return zless(lhs, zr->score, zr->name, zr->len);
}

//insert into the avl tree
static void tree_insert(ZSet *zset, ZNode *node){
    AVLNode *parent = NULL; // insert under this node
    AVLNode **from = &zset->root; // the incoming pointer to the next node
    while (*from) {
        parent = *from;
        //Search a empty node, left or right
        from = zless(&node->tree, parent) ? &parent->left : &parent->right;
    }
    *from = &node->tree; // attach the new node
    node->tree.parent = parent;
    zset->root = avl_fix(&node->tree);
}
// Update the score of a node by re-inserting the tree
static void zset_update(ZSet *zset, ZNode *node, double score) {
    if (node->score== score) {
        return;
    }
    // detaching a node
    zset->root = avl_del(&node->tree);
    avl_init(&node->tree);
    //reinsert the tree node
    node->score = score;
    tree_insert(zset, node);
}

//add a new tuple (score, name) or update the score of an exsisting tuple
// true if new node, false if an existing node is modify
bool zset_insert(ZSet *zset, const char *name, size_t len, double score) {
    if (ZNode *node = zset_lookup(zset, name, len)) {
        zset_update(zset, node, score);
        return false;
    }
    ZNode *node = znode_new(name, len, score);
    hm_insert(&zset->hmap, &node->hmap);
    tree_insert(zset, node);
    return true;

}

// Helper structure for the hashtable lookup (ticket for search)
struct HKey {
  HNode node;
  const char *name = NULL;
  size_t len = 0;
};

static bool hcmp(HNode *node, HNode *key){
    // recover the data from znode
    ZNode *znode = container_of(node, ZNode, hmap);
    // recover the data from heky
    HKey *hkey = container_of(key, HKey, node);
    // check if the lens for optimization
    if (znode->len != hkey->len) {
        return false;
    }
    return 0 == memcmp(znode->name, hkey->name, znode->len);
}

//lookup for name 
ZNode *zset_lookup(ZSet *zset, const char *name, size_t len) {
    if (!zset->root){
        return NULL;
    }
    HKey key;
    key.node.hcode = str_hash((uint8_t *)name, len);
    key.name = name;
    key.len = len;
    HNode *found = hm_lookup(&zset->hmap, &key.node, &hcmp);
    return found ? container_of(found, ZNode, hmap) : NULL;
}

//delete a node
void zset_delete(ZSet *zset, ZNode *node) {
    //remove from the hashtable
    HKey key;
    key.node.hcode = node->hmap.hcode;
    key.name = node->name;
    key.len = node->len;
    HNode *found = hm_delete(&zset->hmap, &key.node, &hcmp);
    assert(found);
    //remove from the tree
    zset->root = avl_del(&node->tree);
    //deallocate the node
    znode_del(node);
}

// find the first pair that pair >= (score, name)
ZNode *zset_seekge(ZSet *zset, double score, const char *name, size_t len){
    AVLNode *found = NULL;
    for (AVLNode *node = zset->root; node; ){
        if (zless(node, score, name, len)) {
            node = node->right; // node < key
        } else {
            found = node; // candidate
            node = node->left;
        }
    }
    return found ? container_of(found, ZNode, tree) : NULL;
}

//offsset into the succeding or preceding node
ZNode *znode_offset(ZNode *node, int64_t offset) {
    AVLNode *tnode = node ? avl_offset(&node->tree, offset) : NULL;
    return tnode ? container_of(tnode, ZNode, tree) : NULL;
}