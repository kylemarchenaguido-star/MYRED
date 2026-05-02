#include <assert.h>
#include <stdlib.h>     // calloc(), free()
#include "hashtable.h"

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

const size_t k_rehashing_work = 128;

static void hm_help_rehashing(HMap *hmap){
    size_t nwork = 0;
    while (nwork < k_rehashing_work && hmap->older.size > 0){
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

const size_t k_max_load_factor = 8;

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
    *hmap = HMap{};
}

size_t hm_size(HMap *hmap){
    return hmap->newer.size + hmap->older.size;
}