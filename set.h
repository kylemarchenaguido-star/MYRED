#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string>
#include "hashtable.h"

struct SetNode{
    HNode node;
    std::string member;
};

// true if new member
bool set_add(HMap *h, std::string member);
bool set_is_member(HMap *h, const std::string &member);
// true if remove
bool set_remove(HMap *h, const std::string &member);
void set_clear(HMap *h);