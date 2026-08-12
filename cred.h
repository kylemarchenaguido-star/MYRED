#pragma once
#include <string>

// Stored-credential abstraction (V9.6.1). A credential is an opaque std::string
// in User::pw_hashes / Config::password, in one of two self-describing forms:
//   legacy: 64 lowercase hex chars           = unsalted SHA-256 (verified
//   forever) PHC:    $argon2id$v=19$m=..,t=..,p=..$.. = Argon2id (requires
//   libargon2 build)
// All hashing/verification goes through these three functions - no format
// checks anywhere else in the codebase.
std::string cred_hash_new(
    const std::string &plain); // hash with current policy; "" on failure
bool cred_verify(const std::string &plain,
                 const std::string &stored); // constant time tag compare
bool cred_needs_rehash(
    const std::string &stored); // true if weaker than current policy
const std::string &cred_dummy(); // baked-in unmatchable credential
std::string cred_random_hex(size_t nhex); // Cryptographically secure lowercase hex string, nhex chars from getrandom()
