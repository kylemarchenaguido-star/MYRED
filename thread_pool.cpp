#include <assert.h>
#include <stdlib.h>
#include "thread_pool.h"
#include <unistd.h>


static void *worker(void *arg){
    ThreadPool *tp = (ThreadPool *)arg; // cast to real type
    while (true){
        pthread_mutex_lock(&tp->mu);
        // wait until there's work OR we're shutting down
        while (tp->queue.empty() &&  !tp->stop){
            pthread_cond_wait(&tp->not_empty, &tp->mu);
        }
        // drain remaining work even if stop is set - then exit
        if (tp->stop && tp->queue.empty()){
            pthread_mutex_unlock(&tp->mu);
            return NULL;
        }

        // do the actual work
        Work w = tp->queue.front();
        tp->queue.pop_front();
        pthread_mutex_unlock(&tp->mu); 

        // do the task
        try {
            w.f(w.arg);
        } catch (...){
            static const char msg[] = "thread_pool: task threw exception, thread continuing\n";
            (void)write(STDERR_FILENO, msg, sizeof(msg) - 1);
        }
        
    }
}

void thread_pool_init(ThreadPool *tp, size_t num_threads){
    assert(num_threads > 0);    
    if (pthread_mutex_init(&tp->mu, NULL) != 0){ abort();}
    if (pthread_cond_init(&tp->not_empty, NULL) != 0){ abort();}

    tp->threads.resize(num_threads);
    for (size_t i = 0; i < num_threads; ++i){
        if (pthread_create(&tp->threads[i],  NULL, &worker, tp) != 0){ abort(); };
    }
}

void thread_pool_queue(ThreadPool *tp, void (*f)(void *), void *arg){
    pthread_mutex_lock(&tp->mu);
    tp->queue.push_back(Work {f, arg});
    pthread_cond_signal(&tp->not_empty);
    pthread_mutex_unlock(&tp->mu);
}

void thread_pool_destroy(ThreadPool *tp){
    pthread_mutex_lock(&tp->mu);
    tp->stop = true;
    // wake up all the sleeping threads
    pthread_cond_broadcast(&tp->not_empty);
    pthread_mutex_unlock(&tp->mu);

    for (pthread_t &t : tp->threads){
        // wew wait for each one to finish
        pthread_join(t, NULL);
    }
    pthread_mutex_destroy(&tp->mu);
    pthread_cond_destroy(&tp->not_empty);
}