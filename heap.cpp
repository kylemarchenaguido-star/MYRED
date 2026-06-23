#include "heap.h"
#include <assert.h>

static size_t heap_parent(size_t i){
    return (i + 1) / 2 - 1;
}

static size_t heap_left(size_t i){
    return i * 2 + 1;
}

static size_t heap_right(size_t i){
    return i * 2 + 2;
}

 // called after insert
static void heap_up(HeapItem *a, size_t pos){
    HeapItem t = a[pos];
    while (pos > 0 && a[heap_parent(pos)].val > t.val){
        // swap with the parent
        a[pos] = a[heap_parent(pos)];
        *a[pos].ref = pos;
        pos = heap_parent(pos);
    }
    a[pos] = t;
    *a[pos].ref = pos;
}

// called after a remove 
static void heap_down(HeapItem *a, size_t pos, size_t len){
    HeapItem t = a[pos];
    while (true){
        // we find the smallest among the parents and their kids
        size_t l = heap_left(pos);
        size_t r = heap_right(pos);
        size_t min_pos = pos;
        uint64_t min_val = t.val;
        // check left child
        if (l < len && a[l].val < min_val){
            min_pos = l;
            min_val = a[l].val;
        }
        // check right child
        if (r < len && a[r].val < min_val){
            min_pos = r;
            min_val = a[r].val;
        }
        if (min_pos == pos){
            break;
        }
        // swap with the kid
        a[pos] = a[min_pos];
        *a[pos].ref = pos;
        pos = min_pos;
    }
    a[pos] = t;
    *a[pos].ref = pos;
}

void heap_update(HeapItem *a, size_t pos, size_t len){
    assert(pos < len);
    if (pos > 0 && a[heap_parent(pos)].val > a[pos].val){
        heap_up(a, pos);
    } else {
        heap_down(a, pos, len);
    }
}