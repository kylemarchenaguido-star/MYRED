#include "buffer.h"
#include "list.h"
#include "hashtable.h"
#include "heap.h"
#include "thread_pool.h"
#include "state.h"
#include "transport.h"

#include <errno.h>
#include <unistd.h>
#include <openssl/ssl.h>
#include <openssl/err.h>

static SSL_CTX *g_tls_ctx = nullptr;

static std::string tls_err_string(const std::string &what){
    char buf[256] = "unknown error";
    ERR_error_string_n(ERR_get_error(), buf, sizeof(buf));
    return what + ": " + buf;
}

bool tr_tls_init(std::string &err){
    if (g_config.tls_port == 0){ return true; } // TLS disable
    if (g_config.tls_cert_file.empty() || g_config.tls_key_file.empty()){
        err = "tls-port is set but tls-cert-file/tls-key-file are missing";
        return false;
    }
    if (g_config.tls_auth_clients != TlsAuthClients::NO && g_config.tls_ca_cert_file.empty()){
        err = "tls-auth-clients needs tls-ca-cert-file to verify client certs";
        return false;
    }
    
    g_tls_ctx = SSL_CTX_new(TLS_server_method());
    if (!g_tls_ctx){ err = tls_err_string("SSL_CTX_new"); return false; }

    if (SSL_CTX_set_min_proto_version(g_tls_ctx, TLS1_2_VERSION) != 1){
        err = tls_err_string("SSL_CTX_set_min_proto_version"); return false;
    }
    SSL_CTX_set_options(g_tls_ctx, SSL_OP_NO_RENEGOTIATION);
    // ACCEPT_MOVING_WRITE_BUFFER: Buffer slides/reallocs between write retries
    // (buf_consume/buf_append); vanilla OpenSSL aborts a retried SSL_write whose
    // buffer address changed. PARTIAL_WRITE matches write()'s short-write contract.
    SSL_CTX_set_mode(g_tls_ctx, SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER);

    if (SSL_CTX_use_certificate_chain_file(g_tls_ctx, g_config.tls_cert_file.c_str()) != 1){
        err = tls_err_string("loading tls-cert-file '" + g_config.tls_cert_file + "'");
        return false;
    }

    if (SSL_CTX_use_PrivateKey_file(g_tls_ctx, g_config.tls_key_file.c_str(), SSL_FILETYPE_PEM) != 1){
        err = tls_err_string("loading tls-key-file '" + g_config.tls_key_file + "'");
        return false;
    }


    if (SSL_CTX_check_private_key(g_tls_ctx) != 1){
        err = "tls-key-file does not match tls-cert-file";
        return false;
    }

    if (!g_config.tls_ca_cert_file.empty()){
        if (SSL_CTX_load_verify_locations(g_tls_ctx, g_config.tls_ca_cert_file.c_str(), nullptr) != 1){
            err = tls_err_string("loading tls-ca-cert-file '" + g_config.tls_ca_cert_file + "'");
            return false;            
        }
    }
    int vmode = SSL_VERIFY_NONE;
    if (g_config.tls_auth_clients == TlsAuthClients::YES){
        vmode = SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT;
    } else if (g_config.tls_auth_clients == TlsAuthClients::OPTIONAL){
        vmode = SSL_VERIFY_PEER;
    }
    SSL_CTX_set_verify(g_tls_ctx, vmode, nullptr);
    return true;
}

bool tr_tls_attach(Conn *c){
    c->ssl = SSL_new(g_tls_ctx);
    if(!c->ssl){ return false; }
    if (SSL_set_fd(c->ssl, c->fd) != 1){
        SSL_free(c->ssl);
        c->ssl = nullptr;
        return false;
    }
    // we manage the handshake driven by the loop, not here
    SSL_set_accept_state(c->ssl); // we put it in server role
    return true;
}

// Plaintext transport. each call refreshes the conn's transport demand flags
// they describe only the most recent operation, never a stale one

IoResult tr_read(Conn *c, uint8_t *buf, size_t cap, size_t *n){
  c->tr_want_read = false;
  c->tr_want_write = false;
  *n = 0;

  // plaintext part
  if (!c->ssl) {
    ssize_t rv = read(c->fd, buf, cap);
    if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
    if (rv == 0){ return IoResult::PEER_CLOSED; }
    if (errno == EAGAIN || errno == EINTR){
        c->tr_want_read = true;
        return IoResult::WANT_READ;
    }
    return IoResult::ERR;
  }

  // TLS: one SSL_read; classify with SSL_get_error, NEVER errno, handle_read
  // loop while tr_has_pending() reports more buffered records
  ERR_clear_error();
  int rv = SSL_read(c->ssl, buf, (int)cap);
  if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
  int e = SSL_get_error(c->ssl, rv);
  if (e == SSL_ERROR_WANT_READ){ c->tr_want_read = true; return IoResult::WANT_READ; }
  if (e == SSL_ERROR_WANT_WRITE){ c->tr_want_write = true; return IoResult::WANT_WRITE; }
  if (e == SSL_ERROR_ZERO_RETURN){ return IoResult::PEER_CLOSED; } // clean close-notify
  return IoResult::ERR; // SSL_ERROR_SYSCALL (dirty EOF) or SSL_ERROR_SSL

}

IoResult tr_write(Conn *c, const uint8_t *buf, size_t len, size_t *n){
    c->tr_want_read = false;
    c->tr_want_write = false;
    *n = 0;

    // plaintext
    if (!c->ssl){
        ssize_t rv = write(c->fd, buf, len);
        if (rv >= 0){ *n = (size_t)rv; return IoResult::OK; }
        if (errno == EAGAIN || errno == EINTR){
            c->tr_want_write = true;
            return IoResult::WANT_WRITE;
        }
        return IoResult::ERR;
    }
    
    // TLS, SSL_MODE_ENABLE_PARTIAL_WRITE (set in tr_tls_init) makes short writes
    // legal, so rv < len is fine - handle_writes consumes n and keeps want_write
    ERR_clear_error();
    int rv = SSL_write(c->ssl, buf, (int)len);
    if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
    int e = SSL_get_error(c->ssl, rv);
    if (e == SSL_ERROR_WANT_READ){ c->tr_want_read = true; return IoResult::WANT_READ; }
    if (e == SSL_ERROR_WANT_WRITE){ c->tr_want_write = true; return IoResult::WANT_WRITE; }
    if (e == SSL_ERROR_ZERO_RETURN){ return IoResult::PEER_CLOSED; }
    return IoResult::ERR;
}

// Do we can read without another poll wake?
bool tr_has_pending(Conn *c){
    // plaintext: the socket is the only buffer; poll re-fires
    if (!c->ssl){ return false; }
    return SSL_has_pending(c->ssl) == 1;
}

IoResult tr_handshake(Conn *c){
    c->tr_want_read = false;
    c->tr_want_write = false;
    ERR_clear_error(); // stale queue entries would misattribute this call's failure
    int rv = SSL_do_handshake(c->ssl);
    if (rv == 1){ return IoResult::OK; }
    int e = SSL_get_error(c->ssl, rv);
    if (e == SSL_ERROR_WANT_READ){ c->tr_want_read = true; return IoResult::WANT_READ; }
    if (e == SSL_ERROR_WANT_WRITE){ c->tr_want_write = true; return IoResult::WANT_WRITE; }
    return IoResult::ERR;
}

std::string tr_tls_error(){
    char buf[256] = "unknown error";
    unsigned long e = ERR_get_error();
    if (e) { ERR_error_string_n(e, buf, sizeof(buf)); }
    ERR_clear_error();
    return buf;
}

void tr_close(Conn *c){
    if (c->ssl){
        // one-shot close-notifyl do NOT retry or wait for the peer's
        SSL_shutdown(c->ssl);
        SSL_free(c->ssl);
        c->ssl = nullptr;
    }
    (void)close(c->fd);
}