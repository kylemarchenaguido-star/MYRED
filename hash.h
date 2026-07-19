#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>
#include "hashtable.h"

// intrusive node + field/value strings
struct HashNode {
    HNode node;
    std::string field;
    std::string value;
};

inline size_t hash_node_bytes(const HashNode *hn){
    return sizeof(HashNode) + sizeof(HNode *) + hn->field.capacity() + hn->value.capacity();
}

// returns true if a NEW field was created, false if an exisiting one was updated
bool hash_set(HMap *h, const std::string &field, std::string value);
HashNode *hash_get(HMap *h, const std::string &field);
bool hash_del(HMap *h, const std::string &field); // true if removed
void hash_clear(HMap *h); // free all the filed nodes + table

