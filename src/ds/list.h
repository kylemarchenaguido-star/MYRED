#pragma once

#include <stddef.h>

struct DList {
    DList *prev = NULL;
    DList *next = NULL;
};

// ? i gotta be dumb
inline void dlist_init(DList *node){
    node->prev = node->next = node;
}

// checks if the pointer is pointing to itself (empty)
inline bool dlist_empty(DList *node){
    return node->next == node;
}

//
 inline void dlist_detach(DList *node){
    DList *prev = node->prev;
    DList *next = node->next;
    prev->next = next;
    next->prev = prev;
}

// inserts rookie before target
inline void dlist_insert_before(DList *target, DList *rookie){
    DList *prev = target->prev;
    prev->next = rookie;
    rookie->prev = prev;
    rookie->next = target;
    target->prev = rookie;
}