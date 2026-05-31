#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <set>
#include "avl.h"

#define container_of(ptr, type, member) ({                  \
    const typeof( ((type *)0)->member ) *__mptr = (ptr);    \
    (type *)( (char *)__mptr - offsetof(type, member) );})

struct Data {
    AVLNode node;
    uint32_t val;
};

struct Container {
    AVLNode *root = NULL;
};

// insert a node 
static void add(Container &c, uint32_t val){
    Data *data =  new Data(); // allocate the data 
    avl_init(&data->node);
    data->val = val;

    AVLNode *cur = NULL; // current node
    AVLNode **from = &c.root; // incoming ptr to next node
    while (*from) { // search
        cur = *from;
        uint32_t node_val = container_of(cur, Data, node)->val;
        from = (val < node_val) ? &cur->left : &cur->right;
    }

    *from = &data->node; // attach the new node 
    data->node.parent = cur;
    c.root = avl_fix(&data->node);
} 

// delete a node in a tree
static bool del(Container &c, uint32_t val){
    AVLNode *cur = c.root;
    while (cur) {
        uint32_t node_val =  container_of(cur, Data, node)->val;
        if (val == node_val){ break;}
        cur = val < node_val ? cur->left : cur->right;
    }
    if (!cur) {return false;}
    
    c.root = avl_del(cur);
    delete container_of(cur, Data, node);
    return true;
}

// verify the structure of the respective tree of a node
static void avl_verify(AVLNode *parent, AVLNode *node){
    if (!node) {return;}
    
    assert(node->parent == parent);
    avl_verify(node, node->left);
    avl_verify(node, node->right);

    assert(node->cnt == 1 + avl_cnt(node->left) + avl_cnt(node->right));

    uint32_t l =  avl_height(node->left);
    uint32_t r =  avl_height(node->right);
    assert(l == r || l + 1 == r || l == r + 1);
    assert(node->height == 1 + std::max(l,r));

    uint32_t val = container_of(node, Data, node)->val;
    if (node->left) {
        assert(node->left->parent == node);
        assert(container_of(node->left, Data, node)->val <= val);
    }
    if (node->right) {
        assert(node->right->parent == node);
        assert(container_of(node->right, Data, node)->val >= val);
    }
}
// extract the corresponding tree in sorted order
static void extract(AVLNode *node, std::multiset<uint32_t> &extracted){
    if (!node){return;}

    extract(node->left, extracted);
    extracted.insert(container_of(node, Data, node)->val);
    extract(node->right, extracted);
 }

// verify the structure and the values in the multiset
static void container_verify(Container &c, const std::multiset<uint32_t> &ref){
    avl_verify(NULL, c.root);
    assert(avl_cnt(c.root) == ref.size());
    std::multiset<uint32_t> extracted;
    extract(c.root, extracted);
    assert(extracted ==  ref);
}

static void dispose(Container &c){
    while(c.root){
        AVLNode *node = c.root;
        c.root = avl_del(c.root);
        delete container_of(node, Data, node);
    }
}


// test the tree with inserting values in it (smallest case to biggest case)
static void test_insert(uint32_t sz){ // sz == size
    for (uint32_t val = 0; val < sz; ++val){
        Container c;
        std::multiset<uint32_t> ref;
        for (uint32_t i = 0; i < sz ; ++i){
            if (i == val){
                continue;
            }
            add(c, i);
            ref.insert(i);
        }
        container_verify(c,ref);

        add(c,val);
        ref.insert(val);
        container_verify(c, ref);
        dispose(c);
    }
}
// test if duplicates work
static void test_insert_dup(uint32_t sz){
    for (uint32_t val = 0; val < sz; ++val){
        Container c;
        std::multiset<uint32_t> ref;
        for (uint32_t i = 0; i < sz; ++i){
            add(c,i);
            ref.insert(i);
        }
        container_verify(c, ref);

        add(c, val);
        ref.insert(val);
        container_verify(c, ref);
        dispose(c);
    }
}        
//test if removing works
static void test_remove(uint32_t sz){
    for (uint32_t val = 0; val < sz; ++val){
        Container c;
        std::multiset<uint32_t> ref;
        for (uint32_t i = 0; i < sz; ++i){
            add(c,i);
            ref.insert(i);
        }
        container_verify(c, ref);

        assert(del(c,val));
        ref.erase(val);
        container_verify(c, ref);
        dispose(c);
    }
}        

int main() {
    Container c;

    //quick test
    container_verify(c, {});
    add(c, 123);
    container_verify(c, {123});
    assert(!del(c, 124));
    assert(del(c, 123));

    container_verify(c, {});

    // sequential insertion
    std::multiset<uint32_t> ref;
    for (uint32_t i = 0; i < 1000; i += 3){
        add(c, i);
        ref.insert(i);
        container_verify(c, ref);
    }

    // random insertion
    for (uint32_t i = 0; i < 100; ++i){
        uint32_t val = (uint32_t)rand() % 1000;
        add(c, val);
        ref.insert(val);
        container_verify(c,ref);
    }

    //random deletion
    for(uint32_t i = 0; i < 200; ++i){
        uint32_t val = (uint32_t)rand() & 1000;
        auto it = ref.find(val);
        if (it == ref.end()){
            assert(!del(c,val));
        } else {
            assert(del(c,val));
            ref.erase(it);
        }
        container_verify(c, ref);
    }

    //insertion/deletion at various positions 
    for (uint32_t i = 0; i < 200; ++i){
        test_insert(i);
        test_insert_dup(i);
        test_remove(i);
    }
    dispose(c);
    return 0;
}