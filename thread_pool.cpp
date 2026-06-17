#include <assert.h>
#include "thread_pool.h"

static void *worker(void *arg){
    ThreadPool *tp = (ThreadPool *)arg; // cast to real type
    while (true){
        pthread_mutex_lock(&tp->mu);
        // now wait for a non-empty queue
        while (tp->queue.empty()){
            pthread_cond_wait(&tp->not_empty, &tp->mu);
        }

        // got a task
        Work w = tp->queue.front();
        tp->queue.pop_front();
        pthread_mutex_unlock(&tp->mu); 

        // do the task
        w.f(w.arg);
    }
}

void thread_pool_init(ThreadPool *tp, size_t num_threads){
    assert(num_threads > 0);
    
    int rv = pthread_mutex_init(&tp->mu, NULL);
    assert(rv == 0);
    rv = pthread_cond_init(&tp->not_empty, NULL);
    assert(rv == 0);
    (void)rv; // asserts vanish in release (NDEBUG); avoid set-but-unused warning

    tp->threads.resize(num_threads);
    for (size_t i = 0; i < num_threads; ++i){
        int rv  = pthread_create(&tp->threads[i],  NULL, &worker, tp);
        assert(rv == 0);
        (void)rv;
    }
}

void thread_pool_queue(ThreadPool *tp, void (*f)(void *), void *arg){
    pthread_mutex_lock(&tp->mu);
    tp->queue.push_back(Work {f, arg});
    pthread_cond_signal(&tp->not_empty);
    pthread_mutex_unlock(&tp->mu);
}