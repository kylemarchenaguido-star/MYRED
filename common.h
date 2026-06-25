#pragma once
#include <cstddef>
#include <stdint.h>
#include <stddef.h>

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
  uint32_t h = 0x811C9DC5;
  for (size_t i = 0; i < len; i++){
    h = (h ^ data[i]) * 0x01000193;
  }
  return h;
}