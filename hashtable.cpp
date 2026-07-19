#include <assert.h>
#include <stdlib.h>     // calloc(), free()
#include "hashtable.h"
#include "common.h"

// Initialiazer for the hashtable 
static void h_init(HTab *htab, size_t n){
    assert(n > 0 && ((n -1) & n) == 0); // n must be a power of 2 (modulo)
    htab->tab = (HNode **)calloc(n, sizeof(HNode *));
    htab->mask = n - 1;
    htab->size = 0;
}
//Functions for older table when rehashing is needed
//hashtable way of insertion 
static void h_insert(HTab *htab, HNode *node){
    size_t pos = node->hcode & htab->mask; // hcode & (n - 1)
    HNode *next = htab->tab[pos];
    node->next = next;
    htab->tab[pos] = node;
    htab->size++;
}

//hashtable look up routine (older table)
static HNode **h_lookup(HTab *htab, HNode *key, bool (*eq)(HNode *, HNode *)){
    if (!htab->tab){
        return NULL; // empty table
    }
    size_t pos = key->hcode & htab->mask;
    HNode **from = &htab->tab[pos];
    for ( HNode *cur; (cur = *from) != NULL; from = &(cur->next)){
        if(cur->hcode == key->hcode && eq(cur,key)){
            return from;
        }

    }
    return NULL;
}

//remove a node from the chain (Older table)
static HNode * h_detach(HTab *htab, HNode **from){
    HNode *node = *from; // current node to be removed
    *from = node->next;
    htab->size--;
    return node;
}

//Functions for the newer table when rehashing

static constexpr size_t k_rehashing_work = 128;

static void hm_help_rehashing(HMap *hmap){
    size_t nwork = 0;
    while (nwork < k_rehashing_work && hmap->older.size > 0){
        if (hmap->migrate_pos > hmap->older.mask){break;} // dont walt past the end of the table
        //find a non empty list
        HNode **from = &hmap->older.tab[hmap->migrate_pos];
        if (!*from){
            hmap->migrate_pos++;
            continue; // empty 
        }
        // moves the first list item into newer list
        h_insert(&hmap->newer, h_detach(&hmap->older, from));
        nwork++;
    }
    //discard old table
    if (hmap->older.size == 0 && hmap->older.tab ){
        free(hmap->older.tab);
        hmap->older = HTab{};
    }
}

// rehashing where the table hits the number factor
static void hm_trigger_rehashing(HMap *hmap){
    hmap->older = hmap->newer; // move the older table into the newer one
    hmap->migrate_pos = 0;
    h_init(&hmap->newer, (hmap->newer.mask + 1) * 2); // create a new empty table with double size of the old one
}

HNode *hm_lookup(HMap *hmap, HNode *key, bool(* eq)(HNode *, HNode *)){
    hm_help_rehashing(hmap);
    HNode **from = h_lookup(&hmap->newer, key, eq); // try to find in the new table
    if (!from){
        from = h_lookup(&hmap->older, key, eq); // try to find in the older table
    }
    return from ? *from : NULL;
}

HNode *hm_delete(HMap *hmap, HNode *key, bool(*eq)(HNode *, HNode *)){
    hm_help_rehashing(hmap);
    if (HNode **from = h_lookup(&hmap->newer, key, eq )){
        return h_detach(&hmap->newer, from);
    }
    if (HNode **from = h_lookup(&hmap->older, key, eq )){
        return h_detach(&hmap->older, from);
    }
    return NULL;
}

static constexpr size_t k_max_load_factor = 8;

void hm_insert(HMap *hmap, HNode *node){
    if (!hmap->newer.tab){
        h_init(&hmap->newer, 4); // initialized if empty
    }
    h_insert(&hmap->newer, node); // always insert on new table 
    if (!hmap->older.tab){ //this is the check if we need to rehash 
        size_t shreshold = (hmap->newer.mask + 1) * k_max_load_factor;
        if (hmap->newer.size >= shreshold){
            hm_trigger_rehashing(hmap);
        }
    }
    hm_help_rehashing(hmap); // this is the help function for rehashing
}

void hm_clear(HMap *hmap){
    free(hmap->newer.tab);
    free(hmap->older.tab);
    hmap->elem_bytes = 0;
    *hmap = HMap{};
}

size_t hm_size(HMap *hmap){
    return hmap->newer.size + hmap->older.size;
}
// iterates every bucket in the table; returns false early if the callback returns false
static bool h_foreach(HTab *htab, bool(*f)(HNode *, void *), void *arg){
    for (size_t i = 0; htab->mask != 0 && i <= htab->mask; ++i){
        for (HNode *node = htab->tab[i]; node != NULL; node = node->next){
            if (!f(node, arg)){
                return false;
            }
        }
    }
    return true;
}

// "short-circuit stops scanning older table if callback returns false; early-stop is not visible to the caller — all current callbacks return true unconditionally".
void hm_foreach(HMap *hmap, bool(*f)(HNode *, void *), void *arg){
    h_foreach(&hmap->newer, f, arg) && h_foreach(&hmap->older, f, arg);
}

// this is db_random_entry for HNode's
HNode *hm_random(HMap *hmap){
    size_t total = hmap->newer.size + hmap->older.size;
    if (total == 0){ return NULL; }
    // pick a table weighted by its share of the keys
    HTab *t = (rand_idx(total) < hmap->newer.size) ? &hmap->newer : &hmap->older;
    if (!t->tab || t->size == 0){ t = (t == &hmap->newer) ? &hmap->older : &hmap->newer; }
    if (!t->tab || t->size == 0){ return NULL; }

    size_t start = rand_idx(t->mask + 1);
    for (size_t probe = 0; probe <= t->mask; ++probe){
        HNode *h = t->tab[(start + probe) & t->mask];
        if (h){
            size_t len = 0;
            for (HNode *c = h; c; c = c->next){ ++len; }
            size_t pick = rand_idx(len);
            for (size_t i = 0; i < pick; ++i){ h = h->next; }
            return h;
        }
    }
    return NULL;
}

// 64-bit bit reversal helper (just reverses a 64 bit with a mask in 6 steps)
static uint64_t rev_bits(uint64_t v){
    // swaps adjacent bits 
  v = ((v >> 1)  & 0x5555555555555555ULL) | ((v & 0x5555555555555555ULL) << 1);
    // swaps adjacents pair
  v = ((v >> 2)  & 0x3333333333333333ULL) | ((v & 0x3333333333333333ULL) << 2);
    // swaps nibbles (4 bits-groups)
  v = ((v >> 4)  & 0x0F0F0F0F0F0F0F0FULL) | ((v & 0x0F0F0F0F0F0F0F0FULL) << 4);
    // swaps bytes
  v = ((v >> 8)  & 0x00FF00FF00FF00FFULL) | ((v & 0x00FF00FF00FF00FFULL) << 8);
    // swap 16-bit Groups
  v = ((v >> 16) & 0x0000FFFF0000FFFFULL) | ((v & 0x0000FFFF0000FFFFULL) << 16);
    // swap 32-bit Halves
  v = (v >> 32) | (v << 32);
  return v;
}

uint64_t hm_scan(HMap *hmap, uint64_t cursor, size_t count, void(*cb)(HNode *, void *), void *arg){
    // empty db
    if (hmap->newer.tab == NULL) { return 0; }

    bool rehashing = (hmap->older.tab != NULL);
    size_t mbig = hmap->newer.mask;
    // during shrink case
    if (rehashing && hmap->older.mask > mbig) { mbig = hmap->older.mask; }

    size_t scanned = 0;
    do {
        // scan the bucket in the active table (callback on every node)
        for (HNode *n = hmap->newer.tab[cursor & hmap->newer.mask]; n; n = n->next){
            cb(n, arg);
        }
        // and the corresponding bucket in the draining table, if mid rehash
        if (rehashing){
            for (HNode *n = hmap->older.tab[cursor & hmap->older.mask]; n; n = n->next){
                cb(n, arg);
            }
        }
        // picks the next bucket to visit in an order that survives table resizes.
        cursor |= ~mbig;
        cursor = rev_bits(cursor);
        cursor += 1;
        cursor = rev_bits(cursor);
        scanned++;
    } while (cursor != 0 && scanned < count);

    return cursor;
}