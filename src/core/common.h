#pragma once
#include <cstddef>
#include <stdint.h>
#include <stddef.h>
#include <random>

// Type-safe intrusive pointer recovery — portable alternative to the
// GCC statement-expression macro. The null-pointer arithmetic mirrors
// what the standard offsetof macro does for standard-layout types.
template<typename Container, typename Member>
inline Container *container_of(Member *ptr, Member Container::*member) {
    alignas(Container) char buf[sizeof(Container)];
    Container *base = reinterpret_cast<Container *>(buf);
    std::ptrdiff_t offset =
        reinterpret_cast<char *>(&(base->*member)) -
        reinterpret_cast<char *>(base);
    return reinterpret_cast<Container *>(reinterpret_cast<char *>(ptr) - offset);
}


// FNV hash
inline uint64_t str_hash(const uint8_t *data, size_t len){
  uint64_t h = 0xcbf29ce484222325ULL;
  for (size_t i = 0; i < len; i++){
    h = (h ^ data[i]) * 0x100000001b3ULL;
  }
  return h;
}

// This is for the random generator
inline std::mt19937_64 g_rng{std::random_device{}()};

inline size_t rand_idx(size_t n){
  return std::uniform_int_distribution<size_t>(0 , n - 1)(g_rng);
}