#pragma once 

#include <stddef.h>
#include <stdint.h>

//hashtable node

struct HNode
{
    HNode *next = NULL;
    uint64_t hcode = 0;
};

struct  HTab
{
    HNode **tab = NULL; // array of slots 
    size_t mask = 0;
    size_t size = 0;
};

