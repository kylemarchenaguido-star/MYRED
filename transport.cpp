#include "buffer.h"
#include "list.h"
#include "hashtable.h"
#include "heap.h"
#include "thread_pool.h"
#include "state.h"
#include "transport.h"

#include <cstddef>
#include <cstdint>
#include <errno.h>
#include <openssl/prov_ssl.h>
#include <openssl/types.h>
#include <unistd.h>

// OpenSSL stays private to this TU. Without MYRED_HAVE_TLS the TLS branches
// compile out and Conn::ssl is always null, so only plaintext paths run.
#ifdef MYRED_HAVE_TLS
#include <openssl/ssl.h>
#include <openssl/err.h>

static SSL_CTX *g_tls_ctx = nullptr;

static std::string tls_err_string(const std::string &what){
    char buf[256] = "unknown error";
    ERR_error_string_n(ERR_get_error(), buf, sizeof(buf));
    return what + ": " + buf;
}

static SSL_CTX *tls_ctx_build(std::string &err){
  if (g_config.tls_cert_file.empty() || g_config.tls_key_file.empty()){
    err = "tls-port is set but tls_cert_file/tls_key_file are missing";
    return nullptr;
  }
  if (g_config.tls_auth_clients != TlsAuthClients::NO && g_config.tls_ca_cert_file.empty()){
    err = "tls_auth_clients needs tls_ca_cert_file to verify client certs";
    return nullptr;
  }
  
  SSL_CTX *ctx = SSL_CTX_new(TLS_server_method());
  if (!ctx){ err = tls_err_string("SSL_CTX_new"); return nullptr; }

  // Session resumption: cache sessions server-side so reconnecting is faster
  SSL_CTX_set_session_cache_mode(ctx, SSL_SESS_CACHE_SERVER);

  if (SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION) != 1){
    err = tls_err_string("SSL_CTX_set_min_proto_version");
    SSL_CTX_free(ctx);
    return nullptr;
  }
  SSL_CTX_set_options(ctx, SSL_OP_NO_RENEGOTIATION);

  SSL_CTX_set_mode(ctx, SSL_MODE_ENABLE_PARTIAL_WRITE | SSL_MODE_ACCEPT_MOVING_WRITE_BUFFER | SSL_MODE_RELEASE_BUFFERS);

  if (SSL_CTX_use_certificate_chain_file(ctx, g_config.tls_cert_file.c_str()) != 1){
      err = tls_err_string("loading tls-cert-file '" + g_config.tls_cert_file + "'");
      SSL_CTX_free(ctx);
      return nullptr;
  }

  if (SSL_CTX_use_PrivateKey_file(ctx, g_config.tls_key_file.c_str(), SSL_FILETYPE_PEM) != 1){
      err = tls_err_string("loading tls-key-file '" + g_config.tls_key_file + "'");
      SSL_CTX_free(ctx);
      return nullptr;
  }

  if (SSL_CTX_check_private_key(ctx) != 1){
      err = "tls-key-file does not match tls-cert-file";
      SSL_CTX_free(ctx);
      return nullptr;
  }

  if (!g_config.tls_ca_cert_file.empty()){
      if (SSL_CTX_load_verify_locations(ctx, g_config.tls_ca_cert_file.c_str(), nullptr) != 1){
          err = tls_err_string("loading tls-ca-cert-file '" + g_config.tls_ca_cert_file + "'");
          SSL_CTX_free(ctx);
          return nullptr;
      }
  }
  int vmode = SSL_VERIFY_NONE;
  if (g_config.tls_auth_clients == TlsAuthClients::YES){
      vmode = SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT;
  } else if (g_config.tls_auth_clients == TlsAuthClients::OPTIONAL){
      vmode = SSL_VERIFY_PEER;
  }
  SSL_CTX_set_verify(ctx, vmode, nullptr);
  return ctx;
}

bool tr_tls_init(std::string &err){
  if (g_config.tls_port == 0){ return true; }
  SSL_CTX *ctx = tls_ctx_build(err);
  if (!ctx){ return false; }
  g_tls_ctx = ctx;
  return true;
}

bool tr_tls_reload(std::string &err){
  if (!g_tls_ctx){ return true; }
  SSL_CTX *fresh = tls_ctx_build(err);
  if (!fresh){ return false; } // old ctx untouched, still serving everything
  
  SSL_CTX *old = g_tls_ctx;
  g_tls_ctx = fresh; // new connections handshake on the new material, every other reference frees itself
  SSL_CTX_free(old);
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

#else // !MYRED_HAVE_TLS

// tls-port must fail loudly, not silently serve cleartext on a TLS port.
bool tr_tls_init(std::string &err){
    if (g_config.tls_port == 0){ return true; }
    err = "tls-port is set but this build has no TLS support "
          "(install libssl-dev and re-run cmake, or unset tls-port)";
    return false;
}

bool tr_tls_reload(std::string &){ return true; }

bool tr_tls_attach(Conn *c){ (void)c; return false; }

#endif // MYRED_HAVE_TLS

// Plaintext transport. each call refreshes the conn's transport demand flags
// they describe only the most recent operation, never a stale one

IoResult tr_read(Conn *c, uint8_t *buf, size_t cap, size_t *n){
  c->tr_want_read = false;
  c->tr_want_write = false;
  *n = 0;

#ifdef MYRED_HAVE_TLS
  // TLS: one SSL_read; classify with SSL_get_error, NEVER errno, handle_read
  // loop while tr_has_pending() reports more buffered records
  if (c->ssl) {
    ERR_clear_error();
    int rv = SSL_read(c->ssl, buf, (int)cap);
    if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
    int e = SSL_get_error(c->ssl, rv);
    if (e == SSL_ERROR_WANT_READ){ c->tr_want_read = true; return IoResult::WANT_READ; }
    if (e == SSL_ERROR_WANT_WRITE){ c->tr_want_write = true; return IoResult::WANT_WRITE; }
    if (e == SSL_ERROR_ZERO_RETURN){ return IoResult::PEER_CLOSED; } // clean close-notify
    return IoResult::ERR; // SSL_ERROR_SYSCALL (dirty EOF) or SSL_ERROR_SSL
  }
#endif

  // plaintext part
  ssize_t rv = read(c->fd, buf, cap);
  if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
  if (rv == 0){ return IoResult::PEER_CLOSED; }
  if (errno == EAGAIN || errno == EINTR){
       c->tr_want_read = true;
      return IoResult::WANT_READ;
  }
  return IoResult::ERR;
}

IoResult tr_write(Conn *c, const uint8_t *buf, size_t len, size_t *n){
    c->tr_want_read = false;
    c->tr_want_write = false;
    *n = 0;

#ifdef MYRED_HAVE_TLS
    // TLS, SSL_MODE_ENABLE_PARTIAL_WRITE (set in tr_tls_init) makes short writes
    // legal, so rv < len is fine - handle_writes consumes n and keeps want_write
    if (c->ssl){
        ERR_clear_error();
        size_t chunk = len > (size_t)INT32_MAX ? (size_t)INT32_MAX : len;
        int rv = SSL_write(c->ssl, buf, (int)chunk);
        if (rv > 0){ *n = (size_t)rv; return IoResult::OK; }
        int e = SSL_get_error(c->ssl, rv);
        if (e == SSL_ERROR_WANT_READ){ c->tr_want_read = true; return IoResult::WANT_READ; }
        if (e == SSL_ERROR_WANT_WRITE){ c->tr_want_write = true; return IoResult::WANT_WRITE; }
        if (e == SSL_ERROR_ZERO_RETURN){ return IoResult::PEER_CLOSED; }
        return IoResult::ERR;
    }
#endif

    // plaintext
    ssize_t rv = write(c->fd, buf, len);
    if (rv >= 0){ *n = (size_t)rv; return IoResult::OK; }
    if (errno == EAGAIN || errno == EINTR){
        c->tr_want_write = true;
        return IoResult::WANT_WRITE;
    }
    return IoResult::ERR;
}

// Do we can read without another poll wake?
bool tr_has_pending(Conn *c){
#ifdef MYRED_HAVE_TLS
    if (c->ssl){ return SSL_has_pending(c->ssl) == 1; }
#else
    (void)c;
#endif
    // plaintext: the socket is the only buffer; poll re-fires
    return false;
}

#ifdef MYRED_HAVE_TLS

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

#else // !MYRED_HAVE_TLS

// Unreachable: tr_tls_init refuses to boot with tls-port set, so nothing is
// ever marked tls_handshaking.
IoResult tr_handshake(Conn *c){ (void)c; return IoResult::ERR; }
std::string tr_tls_error(){ return "build without TLS support"; }

#endif // MYRED_HAVE_TLS

void tr_close(Conn *c){
#ifdef MYRED_HAVE_TLS
    if (c->ssl){
        // one-shot close-notifyl do NOT retry or wait for the peer's
        SSL_shutdown(c->ssl);
        SSL_free(c->ssl);
        c->ssl = nullptr;
    }
#endif
    (void)close(c->fd);
}
