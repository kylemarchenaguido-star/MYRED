// Standalone HMap unit test (V9.6.4 / N2: migrate_pos never reset).
//
// Not part of the server build. Compile and run on its own (from the repo root):
//     g++ -std=c++17 -O1 -o /tmp/test_hashtable scripts/test_hashtable.cpp hashtable.cpp
//     /tmp/test_hashtable
//
// What it proves: after a rehash completes, migrate_pos is left at the end of
// the drained table. The NEXT rehash must restart the drain from bucket 0; if
// it doesn't (N2), the low buckets of `older` are stranded forever, `older.tab`
// is never freed, and hm_insert can never trigger a resize again — the map
// degrades toward O(n). The test drives several full rehash cycles and asserts
// the draining table always empties out.

#include <cstdio>
#include <cstdint>
#include <cstddef>
#include "../hashtable.h"

static int g_pass = 0, g_fail = 0;

static void check(const char *name, bool ok, const char *detail = ""){
    if (ok){ g_pass++; printf("  ok   %s\n", name); }
    else   { g_fail++; printf("  FAIL %s %s\n", name, detail); }
}

struct TestNode {
    HNode node;
    uint64_t key = 0;
};

static uint64_t hash_key(uint64_t k){
    return k * 0x9E3779B97F4A7C15ULL;   // Fibonacci spread, deterministic
}

// -- tiny container_of, kept local so the test has no other dependencies
static TestNode *container_of_test(HNode *n){
    return (TestNode *)((char *)n - offsetof(TestNode, node));
}

static bool node_eq(HNode *a, HNode *b){
    return container_of_test(a)->key == container_of_test(b)->key;
}

static TestNode *lookup(HMap *m, uint64_t key){
    TestNode probe;
    probe.key = key;
    probe.node.hcode = hash_key(key);
    HNode *hit = hm_lookup(m, &probe.node, node_eq);
    return hit ? container_of_test(hit) : nullptr;
}

int main(){
    // enough nodes for several doublings: 4 -> 8 -> 16 -> 32 -> 64 buckets
    // (load factor 8 triggers at 32, 64, 128, 256, 512 entries)
    constexpr size_t N = 600;
    static TestNode nodes[N];

    HMap map{};

    printf("phase 1: insert %zu keys (drives ~5 rehash cycles)\n", N);
    for (size_t i = 0; i < N; ++i){
        nodes[i].key = i;
        nodes[i].node.hcode = hash_key(i);
        hm_insert(&map, &nodes[i].node);

        // plenty of incremental-drain opportunities between inserts
        (void)lookup(&map, i / 2);
    }
    check("all keys inserted (hm_size)", hm_size(&map) == N);

    printf("phase 2: give the drain every chance to finish\n");
    for (int round = 0; round < 1000; ++round){
        (void)lookup(&map, (uint64_t)round % N);
    }

    // Under N2: from the second rehash on, migrate_pos starts past the stranded
    // low buckets, older never empties, and this stays non-null forever.
    check("draining table fully emptied (older.size == 0)",
          map.older.size == 0);
    check("draining table released (older.tab == NULL)",
          map.older.tab == nullptr,
          "-- stranded entries: rehash never completes (N2)");

    printf("phase 3: every key still reachable\n");
    size_t found = 0;
    for (size_t i = 0; i < N; ++i){
        TestNode *t = lookup(&map, i);
        if (t && t->key == i){ found++; }
    }
    check("all keys found after rehash cycles", found == N);

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
