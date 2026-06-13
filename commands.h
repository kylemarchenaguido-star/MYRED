#pragma once
#include <string>
#include <vector>
#include "buffer.h"
#include "state.h"   

void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn);
