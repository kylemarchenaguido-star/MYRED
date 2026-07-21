#pragma once
#include <sys/types.h>
#include <cstdio>
#include <string>
#include <string_view>
#include <initializer_list>

extern pid_t g_aof_child_pid;

void aof_rewrite_background();
void aof_check_background_rewrite();
bool aof_load(const char *path);
bool aof_check(const char *path, bool fix);
void aof_rewrite_wait_shutdown();

template <typename Range>
static void aof_encode(std::string &dst, const Range &args){
  char hdr[32];
  int n = snprintf(hdr, sizeof(hdr), "*%zu\r\n", args.size());
  dst.append(hdr, (size_t)n);
  for (std::string_view a : args){
    int h = snprintf(hdr, sizeof(hdr), "$%zu\r\n", a.size());
    dst.append(hdr, (size_t)h);
    dst.append(a.data(), a.size());
    dst.append("\r\n", 2);
  }
}

inline void aof_encode(std::string &dst, std::initializer_list<std::string_view> args){
    aof_encode<std::initializer_list<std::string_view>>(dst, args);
}