#include "hash.h"
#include "common.h"
#include "str_node.h"
#include <vector>

bool hash_set(HMap *h, const std::string &field, std::string value){
    StringKey key;
    sk_init(&key, field);

    HNode *found = hm_lookup(h, &key.node, &str_node_eq<HashNode, &HashNode::field>);
    if (found){
        HashNode *hn = container_of(found, &HashNode::node);
        h->elem_bytes -= hn->value.capacity();
        hn->value = std::move(value);
        h->elem_bytes += hn->value.capacity();
        return false;
    }
    // create a new field for the hash 
    HashNode *hn = new HashNode();
    hn->field = field;
    hn->value = std::move(value);
    hn->node.hcode = key.node.hcode;
    hm_insert(h, &hn->node);
    h->elem_bytes += hash_node_bytes(hn);
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

    h->elem_bytes -= hash_node_bytes(container_of(found, &HashNode::node));
    delete container_of(found, &HashNode::node);
    return true;
}

void hash_clear(HMap *h){
    hmap_clear_nodes<HashNode>(h);
} 