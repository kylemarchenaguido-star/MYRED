#pragma once
#include <string>
#include <vector>
#include "buffer.h"
#include "state.h"   

void do_request(std::vector<std::string> &cmd, Buffer *out, Conn *conn, const char *raw, size_t raw_len);
void acl_init_categories();
void evict_tick();
void pubsub_remove_conn(Conn *conn); // drop conn from every channel it joined (teardown)
void watch_clear_conn(Conn *conn); // drop conn from every watched key (teardown)