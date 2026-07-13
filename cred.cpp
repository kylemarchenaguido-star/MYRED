#include "cred.h"
#include "sha256.h"
#include <cstdio>
#include <cstring>
#include <cerrno>
#include <sys/random.h>

#ifdef MYRED_HAVE_ARGON2
#include <argon2.h>
#endif

// Argon2id policy: QWASP baseline. raising these later is safe
static constexpr uint32_t k_m_cost = 19456; // KIB (19MB)
static constexpr uint32_t k_t_cost = 2;
static constexpr uint32_t k_par = 1;
static constexpr uint32_t k_salt_len = 16;
static constexpr uint32_t k_tag_len = 32;

// Security grade randomness for salts. NOT g_rng
static bool fill_random(uint8_t *buf, size_t n){
    size_t got = 0;
    while (got < n){
        ssize_t r = getrandom(buf + got, n - got, 0);
        if (r < 0){ if (errno == EINTR){ continue; } return false; }
        got += (size_t)r;
    }
    return true;
}

std::string cred_hash_new(const std::string &plain){
#ifdef MYRED_HAVE_ARGON2
    uint8_t salt[k_salt_len];
    // no entropy, we refuese don't weaken
    if (!fill_random(salt, sizeof(salt))){ return std::string(); } 
    size_t enc_len = argon2_encodedlen(k_t_cost, k_m_cost, k_par, k_salt_len, k_tag_len, Argon2_id);
    std::string enc(enc_len, '\0');
    int rc = argon2id_hash_encoded(k_t_cost, k_m_cost, k_par,
                                  plain.data(), plain.size(),
                                  salt, sizeof(salt), k_tag_len,
                                  &enc[0], enc.size());
    if (rc != ARGON2_OK){ return std::string(); }
    // encodelen includes the NUL; trim to the real string
    enc.resize(strlen(enc.c_str()));
    return enc;
#else
    return sha256_hex(plain);
#endif
}

bool cred_verify(const std::string &plain, const std::string &stored){
    if (stored.rfind("$argon2id$", 0) == 0){
#ifdef MYRED_HAVE_ARGON2
        // re derives wit th embedded salt/params; tag compare inside the lib is constant time
        return argon2id_verify(stored.c_str(), plain.data(), plain.size()) == ARGON2_OK;
#else   
    return false; // PHC credential but build without argon2: cannot verify
#endif
    }
    if (stored.size() == 64){
        return ct_equal(sha256_hex(plain), stored); // legacy digest path
    }
    return false;
}

bool cred_needs_rehash(const std::string &stored){
#ifdef MYRED_HAVE_ARGON2
    // legacy, wew upgrade on next AUTH
    if (stored.rfind("$argon2id$", 0) != 0){ return true; }
    unsigned m = 0, t = 0, p = 0;
    if (sscanf(stored.c_str(), "$argon2id$v=%*u$m=%u,t=%u,p=%u$", &m, &t, &p) != 3){ return true; }
    return m < k_m_cost || t < k_t_cost || p < k_par;
#else
    (void)stored;
    // fallback build cannot produce anything stronger
    return false; 
#endif
}

// A credential hashed from 16 random bytes that are immediately discarded
// Pre-warmed at boot so the first unknow-user AUTH doesn't pay the init cost.
const std::string &cred_dummy(){
    // we use a lambda here because building the dummy takes a lot of steps
    // the lambda calls after the declaring it   
    static const std::string d = [](){
        uint8_t junk[16];
        std::string plain(32, 'x');
        if (fill_random(junk, sizeof(junk))){
        static const char *hx = "0123456789abcdef";
        for (int i = 0; i < 16; ++i){ plain[2*i] = hx[junk[i] >> 4]; plain[2*i+1] = hx[junk[i] & 0xf]; }
        }
        std::string h = cred_hash_new(plain);
        secure_zero(&plain[0], plain.size());
        if (h.empty()){ h = std::string(64, '0'); }   // entropy failure: still a valid-shaped dummy
        return h;
    }();
    return d;
}