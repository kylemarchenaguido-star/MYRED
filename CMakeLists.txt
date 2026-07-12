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
# optional: Argon2id for new password hashes (falls back to SHA-256 when absent)
option(MYRED_ARGON2 "Hash new passwords with Argon2id (libargon2)" ON)
if(MYRED_ARGON2)
  find_path(ARGON2_INCLUDE_DIR argon2.h)
  find_library(ARGON2_LIBRARY argon2)
  if(ARGON2_INCLUDE_DIR AND ARGON2_LIBRARY)
    set(MYRED_HAVE_ARGON2 TRUE)
    message(STATUS "libargon2 found: new passwords use Argon2id")
  else()
    message(WARNING "MYRED_ARGON2=ON but libargon2 dev files not found "
                    "(sudo apt install libargon2-dev) - falling back to SHA-256")
  endif()
endif()

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
  aof.cpp
  cred.cpp
)
target_link_libraries(server PRIVATE ZLIB::ZLIB Threads::Threads)

if(MYRED_HAVE_ARGON2)
  target_compile_definitions(server PRIVATE MYRED_HAVE_ARGON2)
  target_include_directories(server PRIVATE ${ARGON2_INCLUDE_DIR})
  target_link_libraries(server PRIVATE ${ARGON2_LIBRARY})
endif()

# client is standalone (sockets only)
add_executable(client client.cpp)
