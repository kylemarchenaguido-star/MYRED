#include "libraries.h"

const size_t k_max_msg = 32 << 20;

//Helper function for syscalls 
static void msg(const char* message){
	fprintf(stderr, "%s\n", message);
}

static void die(const char *msg){
	int err = errno;
	fprintf(stderr, "[%d] %s\n", err, msg);
	abort();
}

static void fd_set_nb(int fd){
  errno = 0;
  int flags = fcntl(fd, F_GETFL,0);
  if (errno){
    die("fcntl error");
    return;
  }
  flags |= O_NONBLOCK;

  errno = 0;
  (void) fcntl(fd, F_SETFL, flags);
  if(errno) {
    die("fcntl error");
  }
}

// Connections state and buffers 
struct Conn {
    int fd = -1; // this is for the event loop
                //
    bool want_read = false; // The the read and the write, is waiting for the fd api readiness
    bool want_write = false;
    bool want_close = false;

    Buffer incoming; // This two are for the buffers that we are gonna parse 
    Buffer outgoing; // 

};


// Buffer for the protocol
struct Buffer {
  uint8_t *buffer_begin; // start of memory
  uint8_t *buffer_end; // end of memory
  uint8_t *data_begin; // start of data in memory 
  uint8_t *data_end; // end of data in memory
};

// Initialize the buffer protocol 
 Buffer buf_create(size_t capacity){
      uint8_t *mem = new uint8_t[capacity];
      return Buffer {
        .buffer_begin = mem,
        .buffer_end = mem + capacity,
        .data_begin = mem,
        .data_end = mem,
    
      };

    }


//Helper functions // Buffer

//bytes of the data available 
size_t buf_size(Buffer *buf){
  return buf->data_end - buf->data_begin;
}

//pointer to readble data 
uint8_t* buf_data(Buffer *buf){
  return buf->data_begin;
}

//free memory
void buf_destroy(Buffer *buf){
  delete[] buf->buffer_begin;
}

//Helper functions // Arrays 

// Reads data from string 
static bool read_u32(const uint8_t *&cur, const uint8_t *end, uint32_t &out){
  if (cur + 4 > end){
    return false;
  }
  memcpy(&out, cur, 4);
  cur += 4;

  return true;
}

//Reads data length
static bool read_str(onst uint8_t *&cur, const uint8_t *end, size_t n, string &out){
  if  (cur + n > end){
    return false;
  }
  out.assign(cur, cur + n);
  cur += n;
  return true;


}

// append to the front of the buffer
static void buf_append(Buffer *buf, const uint8_t *data, size_t len){

  size_t data_size = buf->data_end - buf->data_begin;

  size_t space_at_back = buf->buffer_end - buf->data_end;

  if (space_at_back < len){
    //  Option A slide the data to the front 
    memmove(buf->buffer_begin, buf->data_begin, data_size);
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin + data_size;

    space_at_back = buf->buffer_end - buf->data_end;

    if (space_at_back < len){
      // Option B still not enough
      size_t old_cap = buf->buffer_end - buf->buffer_begin;
      size_t new_cap = old_cap * 2;

      while (new_cap < data_size + len) new_cap *= 2;

      uint8_t *new_mem = new uint8_t[new_cap];
      memcpy(new_mem, buf->data_begin, data_size);

      delete[] buf->buffer_begin; // free old block 
      
      buf->buffer_begin = new_mem;
      buf->buffer_end = new_mem + new_cap;
      buf->data_begin = new_mem;
      buf->data_end = new_mem + data_size;
    }
  }

  memcpy(buf->data_end, data, len);
  buf->data_end += len;
}

// remove form the front of the buffer and resize 
static void buf_consume(Buffer *buf, size_t n){
  buf->data_begin += n; // we are just moving the pointer forward
  
  // This chunk is just only to reclaim espace 
  if (buf->data_begin == buf->data_end){
    buf->data_begin = buf->buffer_begin;
    buf->data_end = buf->buffer_begin;
  }
}

static Conn *handle_accept(int fd){

   struct sockaddr_in client_addr =  {};
   socklen_t addrlen = sizeof(client_addr);

   int connfd = accept(fd, (struct sockaddr *)&client_addr, &addrlen);
   if (connfd < 0) {return NULL;}

   fd_set_nb(connfd);  // now we set the new connection to namb 
   Conn *conn = new Conn(); // we create a new conn struct 
    
   conn->fd = connfd;
   conn->want_read = true;
   conn->incoming = buf_create(64 * 1024);
   conn->outgoing = buf_create(64 * 1024);
   return conn;
}

static int32_t parse_req(const uint8_t *data, size_t size, std::vector<std::string> &out){
  const uint8_t *end = data + size;
  uint32_t nstr = 0;

  if(!read_u32(data, end, nstr)){return -1;}
  if(nstr > k_max_msg){return -1;}
  

  while (out.size() < nstr) {
    uint32_t len = 0;
    if(!read_u32(data, end, len)){
      return -1;
    }
    out.push_back(std::string());
    if (!read_str(data, end, len, out.back())){return 1;}
  }
  if (data != end){return -1;}
  return 0;

}

enum {
  RES_OK = 0, // Ok response
  RES_ERR = 1, // Error in response 
  RES_NX = 2, // Response not found 
};

struct Response{
  uint32_t status = 0;
  std::vector<uint8_t> data;
}

static std::map<std::string, std::string> g_data;

static void do_request(std::vector<std::string> &cmd, Response &out){

  size_t header_pos = buf_size(out);

  //placeholder for resp_len 
  uint32_t placeholder = 0;
  buf_append(out, (const uint8_t *)&placeholder, 4);

  uint32_t status = RES_OK;

  if(cmd.size() == 2 && cmd[0] == "get"){
    auto it = g_data.find(cmd[1]);
    if (it == g_data.end()){
      status = RES_NX;
      return;
    }
    const std::string &val = it->second;
    buf_append(out, (const uint8_t *)val.data(), val.size());
  } else if (cmd.size() == 3 && cmd[0] == "set"){
    g_data[cmd[1]].swap(cmd[2]);
  } else if (cmd.soze() == 2 && cmd[0] == "del"){
    g_data.erase(cmd[1]);
  } else {
    status = RES_ERR;
  }

  buf_append(out, (const uint8_t *)&status, 4);

  uint32_t resp_len = (uint32_t)(buf_size(out) - header_pos - 4);
  memcpy(buf_data(out) + header_pos, &resp_len, 4);
}

static void make_response(const Response &resp, Buffer *out ){
  uint32_t resp_len = 4 + (uint32_t) resp.data.size();
  buf_append(out, (const uint8_t *)&resp_len, 4);
  buf_append(out, (const uint8_t *)&resp.status, 4);
  buf_append(out, resp.data.data(), resp.data.size());
}

// we will try to proccess if theres enough data
static bool try_one_request(Conn *conn){
  // try to parse the accumulated buffer 
  if(buf_size(&conn->incoming) < 4){return false;}

  uint32_t len = 0;
  memcpy(&len, buf_data(&conn->incoming), 4);

  //len is the message header
  if (len > k_max_msg) {
    conn->want_close = true;
    return false;
  }
  // this is the message body
  if (4 + len > buf_size(&conn->incoming)){return false;}

  
  const uint8_t *request = buf_data(&conn->incoming) + 4;
  // here we are going to procces the parsed message
  std::vector<std::string> cmd ;
  if (parse_req(request, len, cmd) < 0){
    conn->want_close = true;
    return false;
  }
  Response resp;
  do_request(cmd, resp);
  make_response(resp, &conn->outgoing);

  // ...
  // generate the response
  buf_append(&conn->outgoing, (const uint8_t *)&len, 4); // appends header 
  buf_append(&conn->outgoing, request, len); // appends body

  buf_consume(&conn->incoming, 4 + len);
  return true;

}

static void handle_write(Conn *conn){
  assert(buf_size(&conn->outgoing) > 0);
  ssize_t rv = write(conn->fd, buf_data(&conn->outgoing), buf_size(&conn->outgoing));
  if(rv < 0 && errno == EAGAIN){return;}

  if (rv < 0) {
    conn->want_close = true;
    return;
  }
  // remove the data from outgoing
  buf_consume(&conn->outgoing, (size_t)rv);

  if(buf_size(&conn->outgoing) == 0){ // all data writen 
    conn->want_read = true;
    conn->want_write = false;
  }// want to keep writing 
}

static void handle_read(Conn *conn){
  uint8_t buf [64 * 1024];
  ssize_t rv = read(conn->fd, buf, sizeof(buf));
  if (rv < 0 && errno == EAGAIN){return;}
  if(rv <= 0) {
    conn->want_close = true;
    return;
  }
  // add new data to the incoming buffer
  buf_append(&conn->incoming, buf, (size_t)rv);
  // try to parse
  // procces the parsed message
  // remove the message from the buffer(incoming)
  while (try_one_request(conn)) {};

  if(buf_size(&conn->outgoing) > 0){
    conn->want_read = false;
    conn->want_write = true;
    // this is a optimization 
    return handle_write(conn);
  } // else wants to keep reading.
}

  // This is the server cpp

  int main(){

    int fd = socket(AF_INET,SOCK_STREAM,0); // obtain a socket handle
    if (fd < 0) {die("socket()");}

    int val = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &val, sizeof(val)); // set the socket option like the time wait for the socket

    // the is the parameter bind to 0.0.0.0: 1234
    struct sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = ntohs(1234);
    addr.sin_addr.s_addr = ntohl(0);

    int rv = bind(fd, (const struct sockaddr *)&addr, sizeof(addr));
    if (rv) {die("bind()");}

    // listen for connections on the socket
    rv = listen(fd, SOMAXCONN);
    if (rv) {die("listen()");}

   
    std::vector<Conn *> fd2conn; // this a pointer to all conecctions in the file descriptor [3,4,5], and is key by this aswell
    std::vector<struct pollfd> poll_args; // This a vector of structs for arguments for poll_args

    while(true){
      
      poll_args.clear(); //This just clean the arguments for poll.
      struct pollfd pfd = {fd, POLLIN, 0};
      poll_args.push_back(pfd);
      //So everething else are just connected sockets 

      for (Conn *conn : fd2conn){
        if(!conn){continue;}

        struct pollfd pfd = {conn->fd, 0, 0}; // This is for the flags of the aplication
        if (conn->want_read){
          pfd.events |= POLLIN;
        }
        if (conn->want_write){
          pfd.events |= POLLOUT;
        }
        poll_args.push_back(pfd);
      }
      int rv = poll(poll_args.data(), (nfds_t)poll_args.size(), -1);

      if(rv < 0 && errno == EINTR){continue;}

      if(rv < 0){die("poll");}

      // This code for a socket that is listening 
      if(poll_args[0].revents){
        if(Conn *conn = handle_accept(fd)){
            if(fd2conn.size() <= (size_t)conn->fd){
              fd2conn.resize(conn->fd + 1);
            }
            fd2conn[conn->fd] = conn;
        }
      }

      //This is for to handle the connections of sockets
      for(size_t i = 1;i < poll_args.size(); i++){

        uint32_t ready = poll_args[i].revents;
        Conn *conn = fd2conn[poll_args[i].fd];// So the [] inside the fd2conn is just the way to retrieve the object of the original conn 
        // fd2conn it just the pointer of all connections   
        // See if the connections are ready to write or read
        if(ready & POLLIN){handle_read(conn);}
        if(ready & POLLOUT){handle_write(conn);}
        
        //Close the socket from erros 
        if((ready & POLLERR) || conn->want_close){
          (void)close(conn->fd);
          fd2conn[conn->fd] = NULL;
          delete conn;

        }
      }
  }




  return 0;
}


