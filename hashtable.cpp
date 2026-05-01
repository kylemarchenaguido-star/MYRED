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