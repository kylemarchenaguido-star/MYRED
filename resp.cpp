#include <stdio.h>
#include <stddef.h>
#include <cstring>
#include "resp.h"
#include "state.h"

//Parse the RESP protocol
int32_t parse_resp_request(Buffer *buf, std::vector<std::string> &cmd){
  const char *data = (const char *)buf->data_begin;
  size_t size = buf_size(buf);
  size_t pos = 0;

  if (size == 0){ return 0; }

  // must start with '*'
  if  (data[0] != '*') { return -1; }
  pos = 1;

  // read n_args
  size_t n_start = pos;
  while (pos < size && data[pos] != '\r') { pos++; }
  if (pos + 1 >= size) { return 0; } // needs more data
  if (data[pos + 1] != '\n') { return -1; }

  int32_t n_args = 0;
  for (size_t i = n_start; i < pos; ++i){
    if (data[i] < '0' || data[i] > '9') { return -1; }
    n_args = n_args * 10 + (data[i] - '0');
    if (n_args > (int32_t)k_max_args){ return -1; }
  }
  if (n_args < 1) { return -1;}
  pos += 2;

  // read each bulk string
  for (int32_t i = 0; i < n_args; ++i){
    if (pos >= size) { return 0; }
    if (data[pos] != '$') { return -1;}
    pos++;

    size_t len_start = pos;
    while (pos < size && data[pos] != '\r') { pos++; }
    if (pos + 1 >= size) { return 0; }
    if (data[pos + 1] != '\n') { return -1; }

    int32_t  str_len = 0;
    for (size_t j = len_start; j < pos; ++j){
      if (data[j] < '0' || data[j] > '9') { return -1; }
      str_len = str_len * 10 + (data[j]- '0');
      if (str_len > (int32_t)k_max_msg) { return -1;}
    }
    pos += 2; // this skips the \r\n

    if (pos + (size_t)str_len + 2 > size) { return 0; } 

    cmd.push_back(std::string(data + pos, (size_t)str_len));
    pos += (size_t)str_len;

    if (data[pos] != '\r' || data[pos + 1] != '\n') { return -1; }
    pos += 2;
  }
  return (int32_t)pos;
}

//RESP response helpers
// NIL response
void resp_nil(Buffer *out){
  buf_append(out, "$-1\r\n", sizeof("$-1\r\n") - 1);
}

// OK response
void resp_ok(Buffer *out){
  buf_append(out, "+OK\r\n", sizeof("+OK\r\n") - 1);
}

// simple-string response: +<s>\r\n   (used by TYPE)
void resp_simple(Buffer *out, const char *s){
  buf_append(out, "+", 1);
  buf_append(out, s, strlen(s));
  buf_append(out, "\r\n", sizeof("\r\n") - 1);
}

// ERR response
void resp_err(Buffer *out, const char *msg){
  buf_append(out, "-", 1);
  buf_append(out, msg, strlen(msg));
  buf_append(out, "\r\n", sizeof("\r\n") - 1);
}

// INT response
void resp_int(Buffer *out, int64_t val){
  char tmp[32];
  // this put the null terminator and writes into the buffer 
  int len = snprintf(tmp, sizeof(tmp), ":%lld\r\n", (long long)val);
  buf_append(out, tmp, (size_t)len);
}

// STR response
void resp_str(Buffer *out, const char *s, size_t len){
  char tmp[32];
  int hlen = snprintf(tmp, sizeof(tmp), "$%zu\r\n", len);
  buf_append(out, tmp, (size_t)hlen);
  buf_append(out, s, len);
  buf_append(out, "\r\n", sizeof("\r\n") - 1);
}

// DBL response
void resp_dbl(Buffer *out, double val){
  char tmp[64];
  int len = snprintf(tmp, sizeof(tmp), "%.17g", val);
  resp_str(out, tmp, (size_t)len);
}

// ARR response
void resp_arr(Buffer *out, uint32_t n){
  char tmp[32];
  int len = snprintf(tmp, sizeof(tmp), "*%u\r\n", n);
  buf_append(out, tmp, (size_t)len);
}