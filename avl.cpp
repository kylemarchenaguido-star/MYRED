#include <assert.h>
#include "avl.h"

static uint32_t max(uint32_t lhs, uint32_t rhs){
    return lhs < rhs ? rhs : lhs;
}

static void avl_update(AVLNode *node){
    node->height = 1 + max(avl_height(node->left), avl_height(node->right));
}