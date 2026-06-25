#include "set.h"
#include "str_node.h"
#include "common.h"
#include <vector>

// True if a set is added
bool set_add(HMap *h, const std::string &member){
    StringKey key;
    sk_init(&key, member);

    if (hm_lookup(h, &key.node, &str_node_eq<SetNode, &SetNode::member>)){ return false; }

    SetNode *sn = new SetNode();
    sn->member = member;
    sn->node.hcode = key.node.hcode;
    hm_insert(h, &sn->node);
    return true;
}

// check if member exist if not returns null
bool set_is_member(HMap *h, const std::string &member){
    StringKey key;
    sk_init(&key, member);
    return hm_lookup(h, &key.node, &str_node_eq<SetNode, &SetNode::member>) != nullptr;
}

// Remove a set true if succeed
bool set_remove(HMap *h, const std::string &member){
    StringKey key;
    sk_init(&key, member);
    HNode *found = hm_delete(h, &key.node, &str_node_eq<SetNode, &SetNode::member>);
    if (!found){ return false; }
    delete container_of(found, &SetNode::node);
    return true;
}


void set_clear(HMap *h){
    hmap_clear_nodes<SetNode>(h);
}
