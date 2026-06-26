#pragma once
#include <sys/types.h>


// Tracks the current running background save child
extern pid_t g_rdb_child_pid;
extern pid_t g_aof_child_pid;

void aof_reqrite();
void aof_check_background_rewrite();
bool rdb_save(const char *filename);
bool rdb_load(const char *filename);
void rdb_save_background();
void rdb_check_background_save();
void rdb_on_save_complete(const char *filename);