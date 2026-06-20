#include "set.h"
#include "common.h"
#include <vector>

struct SKey {
    HNode node;
    const std::string *member;
};

// Equality for nodes (true if equal)
static bool snode_eq(HNode *a, HNode *b){
    SetNode *sn = container_of(a, SetNode, node);
    SKey *k = container_of(b, SKey, node);
    return sn->member == *k->member;
}

// True if a set is added
bool set_add(HMap *h, const std::string &member){
    SKey key;
    key.member = &member;
    key.node.hcode = str_hash((const uint8_t *)member.data(), member.size());
    if (hm_lookup(h, &key.node, &snode_eq)){ return false; }
    SetNode *sn = new SetNode();
    sn->member = member;
    sn->node.hcode = key.node.hcode;
    hm_insert(h, &sn->node);
    return true;
}

// check if member exist if not returns null
bool set_is_member(HMap *h, const std::string &member){
    SKey key;
    key.member = &member;
    key.node.hcode = str_hash((const uint8_t *)member.data(), member.size());
    return hm_lookup(h, &key.node, &snode_eq) != nullptr;
}

// Remove a set true if succeed
bool set_remove(HMap *h, const std::string &member){
    SKey key;
    key.member = &member;
    key.node.hcode = str_hash((const uint8_t *)member.data(), member.size());
    HNode *found = hm_delete(h, &key.node, &snode_eq);
    if (!found){ return false; }
    delete container_of(found, SetNode, node);
    return true;
}

static bool cb_set_collect(HNode *node, void *arg){
    ((std::vector<SetNode *> *)arg)->push_back(container_of(node, SetNode, node));
    return true;
}

void set_clear(HMap *h){
    std::vector<SetNode *> nodes;
    hm_foreach(h, cb_set_collect, &nodes);
    hm_clear(h);
    for (SetNode *sn : nodes){ delete sn; }
}
