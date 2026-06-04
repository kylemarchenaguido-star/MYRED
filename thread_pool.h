#pragma once

#include <stddef.h>
#include <pthread.h>
#include <deque>
#include <vector>

struct Work {
    void (*f)(void *) = NULL;
    void *arg = NULL;
};

struct ThreadPool{
    std::vector<pthread_t> threads;
    std::deque<Work> queue;
    pthread_mutex_t mu;
    pthread_cond_t not_empty; 
};

// Results from worker to event loop
struct AsyncResult {
    int fd; // which client to reply
    bool success; // Lolololo
};

// Thread safe queue for the results
struct ResultQueue {
    std::deque<AsyncResult> rqueue;
    pthread_mutex_t rmu;
};

void thread_pool_queue(ThreadPool *tp, void(*f)(void *), void *arg);
void thread_pool_init(ThreadPool *tp, size_t num_threads);