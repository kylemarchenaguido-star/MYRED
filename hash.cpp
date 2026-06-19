#include "hash.h"
#include "common.h"
#include <vector>

// lookup dummy, holds a pointer to the filed we're searching for no copy
struct HKey {
    HNode node;
    const std::string *field;
};

// equaility for the hash's Hmap
// compare field string
static bool hnode_field_eq(HNode *a, HNode *b){
    HashNode *hn = container_of(a, HashNode, node);
    HKey *k = container_of(b, HKey, node);
    return hn->field == *k->field;
}

bool hash_set(HMap *h, const std::string &field, const std::string &value){
    HKey key;
    key.field = &field;
    key.node.hcode = str_hash((const uint8_t *)field.data(), field.size());

    HNode *found = hm_lookup(h, &key.node, &hnode_field_eq);
    if (found){
        container_of(found, HashNode, node)->value = value; // update existing
        return false;
    }
    // create a new field for the hash 
    HashNode *hn = new HashNode();
    hn->field = field;
    hn->value = value;
    hn->node.hcode = key.node.hcode;
    hm_insert(h, &hn->node);
    return true;
}

HashNode *hash_get(HMap *h, const std::string &field){
    HKey key;
    key.field = &field;
    key.node.hcode = str_hash((const uint8_t *)field.data(), field.size());

    HNode *found = hm_lookup(h, &key.node, &hnode_field_eq);
    return found ? container_of(found, HashNode, node) : nullptr;
}

bool hash_del(HMap *h, const std::string &field){
    HKey key;
    key.field = &field;
    key.node.hcode = str_hash((const uint8_t *)field.data(), field.size());

    HNode *found = hm_delete(h, &key.node, &hnode_field_eq);
    if(!found){ return false; }

    delete container_of(found, HashNode, node);
    return true;
}

// we collect first, then delete 
static bool cb_collect(HNode *node, void  *arg){
    ((std::vector<HashNode *> *)arg)->push_back(container_of(node, HashNode, node));
    return true;
}

void hash_clear(HMap *h){
    std::vector<HashNode *> nodes;
    hm_foreach(h, cb_collect, &nodes); // gather pointers
    hm_clear(h); // free the table arrays 
    for (HashNode *n : nodes) { delete n; } // free the field nodes
} 