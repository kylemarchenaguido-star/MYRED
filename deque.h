#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string>

// double ended queue with ring buffer
struct Deque {
    std::string *buf = nullptr; // ring buffer of elements
    size_t cap = 0; // allocated slots
    size_t head = 0; // index of the first elements
    size_t count = 0; // number of elements
};

inline void deque_init(Deque *d){
    d->buf = nullptr;
    d->cap = 0;
    d->head = 0;
    d->count = 0;
}

inline void deque_free(Deque *d){
    delete [] d->buf;
    d->buf = nullptr;
    d->cap = 0;
    d->head = 0;
    d->count = 0;
}

inline size_t deque_phys(const Deque *d, size_t logical){
    return (d->head + logical) & (d->cap - 1);
}
void deque_grow(Deque *d);
void deque_push_front(Deque *d, const std::string &val);
void deque_push_back(Deque *d, const std::string &val);
bool deque_pop_front(Deque *d, std::string *out);
bool deque_pop_back(Deque *d, std::string *out);
const std::string *deque_get(const Deque *d, size_t idx);
int64_t deque_normalize(const Deque *d, int64_t idx);