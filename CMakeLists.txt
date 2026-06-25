cmake_minimum_required(VERSION 3.16)
project(myred LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# default to an optimized build unless told otherwise
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wshadow)


# dependencies: zlib (RDB compression) and pthreads (thread pool)
find_package(ZLIB REQUIRED)
find_package(Threads REQUIRED)

# server: main + the data-structure translation units
add_executable(server
  server.cpp
  hashtable.cpp
  zset.cpp
  avl.cpp
  heap.cpp
  thread_pool.cpp
  buffer.cpp
  state.cpp
  resp.cpp
  rdb.cpp
  commands.cpp
  deque.cpp
  hash.cpp
  set.cpp
)
target_link_libraries(server PRIVATE ZLIB::ZLIB Threads::Threads)

# client is standalone (sockets only)
add_executable(client client.cpp)
