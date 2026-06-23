#include "buffer.h"
#include <string.h>

// Initialize the buffer protocol 
Buffer buf_create(size_t capacity){
  uint8_t *mem = new uint8_t[capacity];
  return Buffer {
    .buffer_begin = mem,
    .buffer_end = mem + capacity,
    .data_begin = mem,
    .data_end = mem,
  };
}

//Helper functions // Buffer

// append to the front of the buffer 
void buf_append(Buffer *buf, const uint8_t *data, size_t len){

  size_t data_size = buf->data_end - buf->data_begin;

  size_t space_at_back = buf->buffer_end - buf->data_end;

  if (space_at_back < len){
    //  Option A slide the data to the front 
    memmove(buf->buffer_begin, buf->data_begin, data_size);
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin + data_size;

    space_at_back = buf->buffer_end - buf->data_end;

    if (space_at_back < len){
      // Option B still not enough
      size_t old_cap = buf->buffer_end - buf->buffer_begin;
      size_t new_cap = old_cap * 2;

      while (new_cap < data_size + len) new_cap *= 2;

      uint8_t *new_mem = new uint8_t[new_cap];
      memcpy(new_mem, buf->data_begin, data_size);

      delete[] buf->buffer_begin; // free old block 
      
      buf->buffer_begin = new_mem;
      buf->buffer_end = new_mem + new_cap;
      buf->data_begin = new_mem;
      buf->data_end = new_mem + data_size;
    }
  }

  memcpy(buf->data_end, data, len);
  buf->data_end += len;
}

// overload for the const uint8_t *
void buf_append(Buffer *buf, const char *s, size_t len){
  buf_append(buf, (const uint8_t *)s, len);
}

// overload for bytes for the rdb file
void buf_append(Buffer *buf, uint8_t Byte){
  buf_append(buf, &Byte, 1);
}

// append 64 bytes
void buf_append_u64(Buffer *buf, uint64_t val) {
    buf_append(buf, (const uint8_t *)&val, 8);
}

// Append strings
void buf_append_str(Buffer *buf, const char *str, uint32_t len){
  buf_append(buf, (const uint8_t *)&len, 4);
  buf_append(buf, (const uint8_t *)str, len);
}

// remove form the front of the buffer and resize 
void buf_consume(Buffer *buf, size_t n){
  buf->data_begin += n; // we are just moving the pointer forward
  // This chunk is just only to reclaim espace 
  if (buf->data_begin == buf->data_end){
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin;
  } else if (buf->data_begin >= buf->buffer_begin + (buf->buffer_end - buf->buffer_begin) / 2){
    size_t data_size = buf_size(buf);
    memmove(buf->buffer_begin, buf->data_begin, data_size);
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin + data_size;
  }
}

//bytes of the data available 
size_t buf_size(const Buffer *buf){
  return buf->data_end - buf->data_begin;
}

//pointer to readble data 
uint8_t* buf_data(const Buffer *buf){
return buf->data_begin;
}

//free memory
void buf_destroy(Buffer *buf){
  delete[] buf->buffer_begin;
  buf->buffer_begin = NULL;
  buf->buffer_end = NULL;
  buf->data_begin = NULL;
  buf->data_end= NULL;
}