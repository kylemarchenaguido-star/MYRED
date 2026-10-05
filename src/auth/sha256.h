#pragma once
#include <cstring>
#include <cstdint>
#include <cstddef>
#include <string>

// minimal SHA-256 implementation
namespace myred_sha {

// Rotate right — the fundamental bit-mixing operation SHA-256 uses everywhere.
inline uint32_t rotate_right(uint32_t value, uint32_t bits) {
    return (value >> bits) | (value << (32 - bits));
}

// Processes exactly one 64-byte (512-bit) block, updating the 8-word state.
inline void transform(uint32_t state[8], const uint8_t block[64]) {

    // Fixed by the SHA-256 spec,
    static const uint32_t round_constants[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
        0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
        0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
        0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
        0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    };

    uint32_t schedule[64];

    for (int i = 0, byte_pos = 0; i < 16; ++i, byte_pos += 4) {
        schedule[i] =
            (uint32_t(block[byte_pos])     << 24) |
            (uint32_t(block[byte_pos + 1]) << 16) |
            (uint32_t(block[byte_pos + 2]) << 8)  |
             uint32_t(block[byte_pos + 3]);
    }

    for (int i = 16; i < 64; ++i) {
        uint32_t sigma0 = rotate_right(schedule[i - 15], 7)
                        ^ rotate_right(schedule[i - 15], 18)
                        ^ (schedule[i - 15] >> 3);

        uint32_t sigma1 = rotate_right(schedule[i - 2], 17)
                        ^ rotate_right(schedule[i - 2], 19)
                        ^ (schedule[i - 2] >> 10);

        schedule[i] = schedule[i - 16] + sigma0 + schedule[i - 7] + sigma1;
    }

    uint32_t a = state[0];
    uint32_t b = state[1];
    uint32_t c = state[2];
    uint32_t d = state[3];
    uint32_t e = state[4];
    uint32_t f = state[5];
    uint32_t g = state[6];
    uint32_t h = state[7];

    for (int i = 0; i < 64; ++i) {
        uint32_t big_sigma1 = rotate_right(e, 6)
                             ^ rotate_right(e, 11)
                             ^ rotate_right(e, 25);
        uint32_t choose = (e & f) ^ (~e & g);

        uint32_t temp1 = h + big_sigma1 + choose
                        + round_constants[i] + schedule[i];
        uint32_t big_sigma0 = rotate_right(a, 2)
                             ^ rotate_right(a, 13)
                             ^ rotate_right(a, 22);
        uint32_t majority = (a & b) ^ (a & c) ^ (b & c);

        uint32_t temp2 = big_sigma0 + majority;

        h = g;
        g = f;
        f = e;
        e = d + temp1;
        d = c;
        c = b;
        b = a;
        a = temp1 + temp2;
    }

    state[0] += a;
    state[1] += b;
    state[2] += c;
    state[3] += d;
    state[4] += e;
    state[5] += f;
    state[6] += g;
    state[7] += h;
}

} // namespace myred_sha

inline std::string sha256_hex(const std::string &msg){
    uint32_t st[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
                      0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    uint8_t data[64];
    uint32_t len = 0;
    uint64_t bitlen = 0;
    for (unsigned char ch : msg){
        data[len++] = ch;
        if (len == 64){ 
            myred_sha::transform(st, data); 
            bitlen += 512; 
            len = 0;
       }
    }
    uint32_t i = len;
    data[i++] = 0x80;
    if (len < 56){ 
        while (i < 56){ data[i++] = 0; }
    } else {
        // 0x80 + 8 byte length dont fit in this block: zero-fill the rest,
        // flush it once, then start a fresh all zero block for the length
        while (i < 64){ data[i++] = 0; }
        myred_sha::transform(st, data);
        for (int k = 0; k < 56; k++){ data[k] = 0; }
    }
    bitlen += uint64_t(len) * 8;
    for (int k = 0; k < 8; ++k){
        // big endian length
        data[63 - k] = uint8_t(bitlen >> (k * 8));
    }
    myred_sha::transform(st, data);
    
    static const char *hx = "0123456789abcdef";
    std::string out; out.reserve(64);
    for (int j = 0; j < 8; ++j){
        for (int k = 3; k >= 0; --k){
            uint8_t byte = uint8_t(st[j] >> (k *8));
            out += hx[byte >> 4];
            out += hx[byte & 0xf];
        }
    }
    // 64 lowercase hex chars
    return out;
}

// constant time string compare (no early return once lenghts match)
inline bool ct_equal(const std::string &a, const std::string &b){
    // both are 64 hex digest, length is public
    if (a.size() != b.size()){ return false; }
    unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i){
        diff |= (unsigned char)a[i] ^ (unsigned char)b[i];
    }
    return diff == 0;
}

// best-effort to wipe that the compiler don't optimize away
inline void secure_zero(void *p, size_t n){
    if (!p || n == 0){ return; }
    #if defined(__STDC_LIB_EXT1__)
        // c11 Aneex K
        memset_s(p, n, 0, n);
    #elif defined(__GLIBC__) && (__GLIBC__ > 2 || (__GLIBC__ == 2 && __GLIBC_MINOR__ >= 25))
        explicit_bzero(p, n);
    #elif defined(__OpenBSD__) || defined(__FreeBSD__) || defined(__APPLE__)
        explicit_bzero(p, n);
    #else
        volatile unsigned char *v = (volatile unsigned char *)p; // portable fallback
        while (n--) *v++ = 0;
    #endif
}