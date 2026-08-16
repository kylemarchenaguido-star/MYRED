#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string>

struct Conn;

// WANT_READ / WANT_WRITE mean "retry this same operation when the socket is
// ready in that direction". With TLS a read can demand POLLOUT and a write
// can demand POLLIN, so tr_read/tr_write record their demand in
// Conn::tr_want_read / tr_want_write; the poll loop ORs those into pfd.events.

enum class IoResult {
    OK,
    WANT_READ,
    WANT_WRITE,
    PEER_CLOSED,
    ERR,
};

IoResult tr_read(Conn *c, uint8_t *buf, size_t cap, size_t *n);
IoResult tr_write(Conn *c, const uint8_t *buf, size_t len, size_t *n);
void tr_close(Conn *c);

// tr_tls_init builds the one global SSL_CTX from g_config
// tr_tls_attach wraps a freshly accepted fd in a server-state SSL
bool tr_tls_init(std::string &err);
bool tr_tls_attach(Conn *c);

// rebuild the global SSL_CTX from the current g_config so a certificate can be rotated without restart
bool tr_tls_reload(std::string &err);

// OK = done, WANT_* = poll and come back,
// ERR = fatal (audit + destroy). Only called while Conn::tls_handshaking.
IoResult tr_handshake(Conn *c);
// last openSSL error as text, for audit event; drains the error queue
std::string tr_tls_error();

// true if the transport buffered, immediately-readble bytes the
// next poll() won't announce. Plaintext: always false
bool tr_has_pending(Conn *c);
