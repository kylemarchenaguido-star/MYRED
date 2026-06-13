#include "deque.h"

// INVARIANT: cap is always a power of two (so deque_phys can use & instead of %)
void deque_grow(Deque *d){
    size_t new_cap = d->cap == 0 ? 8 : d->cap * 2;
    std::string *new_buf = new std::string[new_cap];

    // copy elements in logical order
    for (size_t i = 0; i < d->count ; ++i){
        new_buf[i] = std::move(d->buf[deque_phys(d,i)]);
    }

    delete [] d->buf;
    d->buf = new_buf;
    d->cap = new_cap;
    d->head = 0; // reanchored to 0 after rebuild
}
// Push operation
// add to the head (left)
void deque_push_front(Deque *d, const std::string &val){
    if (d->count == d->cap){
        deque_grow(d);
    }
    // move head back by one (wrapping)
    d->head = (d->head + d->cap - 1) & (d->cap - 1);
    d->buf[d->head] = val;
    d->count++;
}

// add to the tail (right)
void deque_push_back(Deque *d, const std::string &val){
    if (d->count == d->cap){
        deque_grow(d);
    }
    size_t tail = deque_phys(d, d->count);
    d->buf[tail] = val;
    d->count++;
}

// POP operation
// remove from the head
bool deque_pop_front(Deque *d, std::string *out){
    if (d->count == 0){
        return false;
    }
    *out = std::move(d->buf[d->head]);
    d->buf[d->head].clear(); // release memory
    d->head = (d->head + 1) & (d->cap - 1);
    d->count--;
    return true;
}

// remove from tha tail
bool deque_pop_back(Deque *d, std::string *out){
    if (d->count == 0){
        return false;
    }
    size_t tail = deque_phys(d, d->count - 1);
    *out = std::move(d->buf[tail]);
    d->buf[tail].clear();
    d->count--;
    return true;
}

// Index and range access
// get element at logical index
const std::string *deque_get(const Deque *d, size_t idx){
    if (idx == d->count){
        return nullptr;
    }
    return &d->buf[deque_phys(d, idx)];
}

// normalize a possibly negative index
int64_t deque_normalize(const Deque *d, int64_t idx){
    if (idx < 0){
        idx += (int64_t)d->count;
    }
    return idx;
}