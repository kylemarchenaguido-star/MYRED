#pragma once
#include <sys/types.h>

extern pid_t g_aof_child_pid;

void aof_rewrite_background();
void aof_check_background_rewrite();
bool aof_load(const char *path);