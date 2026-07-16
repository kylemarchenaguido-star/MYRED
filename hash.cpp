#include "hash.h"
#include "common.h"
#include "str_node.h"
#include <vector>

bool hash_set(HMap *h, const std::string &field, std::string value){
    StringKey key;
    sk_init(&key, field);

    HNode *found = hm_lookup(h, &key.node, &str_node_eq<HashNode, &HashNode::field>);
    if (found){
        container_of(found, &HashNode::node)->value = std::move(value); // update existing
        return false;
    }
    // create a new field for the hash 
    HashNode *hn = new HashNode();
    hn->field = field;
    hn->value = std::move(value);
    hn->node.hcode = key.node.hcode;
    hm_insert(h, &hn->node);
    return true;
}

HashNode *hash_get(HMap *h, const std::string &field){
    StringKey key;
    sk_init(&key, field);

    HNode *found = hm_lookup(h, &key.node, &str_node_eq<HashNode, &HashNode::field>);
    return found ? container_of(found, &HashNode::node) : nullptr;
}

bool hash_del(HMap *h, const std::string &field){
    StringKey key;
    sk_init(&key, field);

    HNode *found = hm_delete(h, &key.node, &str_node_eq<HashNode, &HashNode::field>);
    if(!found){ return false; }

    delete container_of(found, &HashNode::node);
    return true;
}

void hash_clear(HMap *h){
    hmap_clear_nodes<HashNode>(h);
} 