#pragma once
#include <stdint.h>
#include <stddef.h>

// Buffer for the tcp protocol
struct Buffer {
  uint8_t *buffer_begin; // start of memory
  uint8_t *buffer_end; // end of memory
  uint8_t *data_begin; // start of data in memory 
  uint8_t *data_end; // end of data in memory
};

Buffer buf_create(size_t capacity);
void buf_append(Buffer *buf, const uint8_t *data, size_t len);
void buf_append(Buffer *buf, const char *s, size_t len);
void buf_append(Buffer *buf, uint8_t Byte);
void buf_append_u64(Buffer *buf, uint64_t val);
void buf_append_str(Buffer *buf, const char *str, uint32_t len);
void buf_consume(Buffer *buf, size_t n);
size_t buf_size(const Buffer *buf);
uint8_t* buf_data(const Buffer *buf);
void buf_destroy(Buffer *buf);