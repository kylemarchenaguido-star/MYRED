#pragma once

#include <stddef.h>
#include <stdint.h>

struct HeapItem {
    uint64_t val; // heap value, the ttl
    size_t *ref = NULL; // data associated with value
};

void heap_update(HeapItem *a, size_t pos, size_t len);