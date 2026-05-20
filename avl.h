#pragma once

#include <stddef.h>
#include <stdint.h>

struct AVLNode
{
    AVLNode *parent = NULL; 
    AVLNode *left = NULL;
    AVLNode *right = NULL;
    uint32_t height = 0; // auxiliary data and height of the tree
};

inline void avl_init(AVLNode *node){
    node->left = node->right = node->parent = NULL;
    node->height = 1;
}

//helpers functions

static uint32_t avl_height(AVLNode *node){
    return node ? node->height : 0; // if the node is null then it's height is zero
}

