#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>
#include "buffer.h"

int32_t parse_resp_request(Buffer *buf, std::vector<std::string> &cmd);
void resp_nil(Buffer *out);
void resp_nil_arr(Buffer *out);
void resp_ok(Buffer *out);
void resp_simple(Buffer *out, const char *s);
void resp_err(Buffer *out, const char *msg);
void resp_int(Buffer *out, int64_t val);
void resp_str(Buffer *out, const char *s, size_t len);
void resp_dbl(Buffer *out, double val);
void resp_arr(Buffer *out, uint32_t n);