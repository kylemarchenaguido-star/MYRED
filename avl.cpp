#include <assert.h>
#include "avl.h"

static uint32_t max(uint32_t lhs, uint32_t rhs){
    return lhs < rhs ? rhs : lhs;
}

static void avl_update(AVLNode *node){
    node->height = 1 + max(avl_height(node->left), avl_height(node->right));
    node->cnt =  1 + avl_cnt(node->left) + avl_cnt(node->right);
}

static AVLNode *rot_left(AVLNode *node) {
    AVLNode *parent = node->parent;
    AVLNode *new_node = node->right;
    AVLNode *inner = new_node->left;
    // node -- inner
    node->right = inner;
    if (inner) {inner->parent = node;}
    // parent -- new_node
    new_node->parent = parent; //may be null
    // new_node -- node
    new_node->left = node;
    node->parent = new_node;

    //auxiliary
    avl_update(node);
    avl_update(new_node);
    return new_node;
}
// same logic as rot_left
static AVLNode *rot_right(AVLNode *node) {
    AVLNode *parent = node->parent;
    AVLNode *new_node = node->left;
    AVLNode *inner = new_node->right;
    // node -- inner
    node->left = inner;
    if (inner) {inner->parent = node;}
    // parent -- new_node
    new_node->parent = parent; //may be null
    // new_node -- node
    new_node->right = node;
    node->parent = new_node;

    //auxiliary
    avl_update(node);
    avl_update(new_node);
    return new_node;
}

// left tree is tall by 2

static AVLNode *avl_fix_left(AVLNode *node){
    if (avl_height(node->left->left) < avl_height(node->left->right)){ // checks if it is a straight line
        node->left = rot_left(node->left);
    }
    return rot_right(node);
}
// same logic as avl_fix_left
static AVLNode *avl_fix_right(AVLNode *node){
    if (avl_height(node->right->right) < avl_height(node->right->left)){ // checks if it is a straight line
        node->right = rot_right(node->right);
    }
    return rot_left(node);
}

// fix imbalanced nodes and maintain invariants until root is reached
AVLNode *avl_fix(AVLNode *node){
    while (true){
        AVLNode **from = &node; // saved the fixed subtree
        AVLNode *parent = node->parent;
        if(parent){
            //subtree points to the parent
            from = parent->left == node ? &parent->left : &parent->right;
        } // else : save to the local variable node (?)
        avl_update(node);
        // now we fix the height difference of 2
        uint32_t l = avl_height(node->left);
        uint32_t r = avl_height(node->right);
        if (l == r + 2){
            *from = avl_fix_left(node);
        } else if (l + 2 == r){
            *from = avl_fix_right(node);
        }
        //stop if root node
        if (!parent){
            return *from;
        }
        // parent node height might me changed
        node = parent;
    }
}
// detach a node when 1 children is empty
static AVLNode *avl_del_easy(AVLNode *node){
    assert(!node->left || !node->right); // this is making sure at least one child
    AVLNode *child = node->left ? node->left : node->right; // if null
    AVLNode *parent = node->parent;
    if (child){ // update the child pointer if not null
        child->parent = parent;
    }
    if (!parent){ // attach child to the grandparent
        return child;
    }
    AVLNode **from = parent->left == node ? &parent->left : &parent->right;
    *from = child;
    // fix the inbalance tree
    return avl_fix(parent);
}

//detach a node and returns the new root of the tree
AVLNode *avl_del(AVLNode *node){
    // easy case
    if (!node->left || !node->right){
        return avl_del_easy(node);
    }
    // find the successor
    AVLNode *victim = node->right;
    while (victim->left){
        victim = victim->left;
    }
    //detach the successor
    AVLNode *root = avl_del_easy(victim);
    //swapt
    *victim = *node;
    if (victim->left){
        victim->left->parent = victim;        
    }
    if(victim->right){
        victim->right->parent = victim;        
    }
    // attach the successor to the parent, or update root ptr
    AVLNode **from = &root;
    AVLNode *parent = node->parent;
    if (parent){
        from = parent->left == node ? &parent->left : &parent->right;
    }
    *from = victim;
    return root;
}



