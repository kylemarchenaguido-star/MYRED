#pragma once
#include <vector>
#include <string>
#include "hashtable.h"
#include "common.h"

// Single lookup key for any hmap keyed by a string
struct StringKey {
    HNode node;
    const std::string *str;
};

inline void sk_init(StringKey *k, const std::string &s){
    k->str = &s;
    k->node.hcode = str_hash((const uint8_t *)s.data(), s.size());
}

// One comparator template for both nodes types
template<typename Node, std::string Node::*Field>

static bool str_node_eq(HNode *a, HNode *b){
    Node *n = container_of(a, Node, node);
    StringKey *k = container_of(b, StringKey, node);
    return (n->*Field) == *k->str;
}

// One clear template for both node types
template<typename Node>

static void hmap_clear_nodes(HMap *h) {
    std::vector<Node *> nodes;
    hm_foreach(h, [](HNode *n, void *arg) -> bool {
        static_cast<std::vector<Node *> *>(arg)->push_back(container_of(n, Node, node));
        return true;
    }, &nodes);
    hm_clear(h);
    for (Node *n : nodes) delete n;
}
