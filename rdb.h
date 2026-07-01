#pragma once
#include <sys/types.h>
#include <cstdint> 
#include <cstddef>    
#include "buffer.h"    

// Tracks the current running background save child
extern pid_t g_rdb_child_pid;

bool rdb_save(const char *filename);
bool rdb_load(const char *filename);
void rdb_save_background();
void rdb_check_background_save();
void rdb_on_save_complete(const char *filename);
void rdb_build_aof_preamble(Buffer *out);
bool rdb_load_buffer(const uint8_t *data, size_t size);

