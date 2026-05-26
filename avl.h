#pragma once

#include <stddef.h>
#include <stdint.h>

struct AVLNode
{
    AVLNode *parent = NULL; 
    AVLNode *left = NULL;
    AVLNode *right = NULL;
    uint32_t height = 0; // auxiliary data and height of the tree
    uint32_t cnt = 0; // subtree size 
};

inline void avl_init(AVLNode *node){
    node->left = node->right = node->parent = NULL;
    node->height = 1;
    node->cnt = 1;
}

//helpers functions

inline uint32_t avl_height(AVLNode *node){
    return node ? node->height : 0; // if the node is null then it's height is zero
}

inline uint32_t avl_cnt (AVLNode *node){
    return node ? node->cnt : 0; // same as above if it is 0
}

// API 
AVLNode *avl_fix(AVLNode *node);
AVLNode *avl_del(AVLNode *node);
