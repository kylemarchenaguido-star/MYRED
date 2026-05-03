#include "libraries.h"

static void msg(const char* message){
	fprintf(stderr, "%s\n", message);
}

static void die(const char *msg){
	int err = errno;
	fprintf(stderr, "[%d] %s\n", err, msg);
	abort();
}


static int32_t read_full (int fd, uint8_t *buf, size_t n){
	while (n > 0){
		ssize_t rv = read(fd, buf, n);
		if (rv <= 0){return -1;} // error

		assert((size_t)rv <= n);
		n -= (size_t)rv;
		buf += rv;
	}
	return 0;
}

static int32_t write_all (int fd, const uint8_t *buf, size_t n){
	while (n > 0) {
		ssize_t rv = write(fd, buf, n);

		if (rv <= 0){return -1;}

		assert((size_t)rv <= n);
		n -= (size_t)rv;
		buf += rv;
	}
	return 0;
}

// static void buf_append(std::vector<uint8_t>  &buf, const uint8_t *data, size_t len){
//   buf.insert(buf.end(), data, data + len);
// }

constexpr size_t k_max_msg = 4096;

static int32_t send_req(int fd, const std::vector<std::string> &cmd){
  uint32_t len = 4;
  for (const std::string &s : cmd){
    len += 4 + s.size();
  }
  if (len > k_max_msg){
	return -1;
  }

  char wbuf[4 + k_max_msg];

  // writes len at the beggining of the buffer
  memcpy(&wbuf[0], &len, 4); // assume little endian 
  uint32_t n = (uint32_t)cmd.size();
  //how many strings are after the len 
  memcpy(&wbuf[4], &n, 4);

  size_t cur = 8;// skips thw 8 bytes header from before 

  for (const std::string &s : cmd){
	uint32_t p = (uint32_t)s.size();
	memcpy(&wbuf[cur], &p, 4);
	memcpy(&wbuf[cur + 4], s.data(), s.size());
	cur += 4 + s.size();
  }
  return write_all(fd, (const uint8_t *)wbuf, size_t(4 + len));
}

// data types for tag types
enum {
  TAG_NIL = 0, // nil
  TAG_ERR = 1, //err + msg
  TAG_STR = 2, //string
  TAG_INT = 3, //integer
  TAG_DBL = 4, //double
  TAG_ARR = 5, //array
};

static int32_t print_response(const uint8_t *data, size_t size){
	if (size < 1){
		msg("bad response");
	}
	switch (data[0])
	{
	case TAG_NIL:
		printf("(nil)\n");
		return 1;
	case TAG_ERR:
		if (size < 1 + 8){
			msg("bad response");
			return -1;
		}
		int32_t code = 0;
		uint32_t len = 0;
		memcpy(&code, &data[1], 4);
		memcpy(&len, &data[1 + 4], 4);
		if (size < 1 + 8 + len){
			msg("bad response");
			return -1;
		}
		printf("(err) %d %.*s\n", code, len, &data[1 + 8]);
		return 1 + 8 + len;
	case TAG_STR:
		/* code */
		break;
	case TAG_INT:
		/* code */
		break;
	case TAG_DBL:
		/* code */
		break;
	case TAG_ARR:
		/* code */
		break;
	default:
		break;
	}
}

// This the client side of the server

int main(){
	int fd = socket(AF_INET, SOCK_STREAM,0);
	if (fd < 0){die("socket()");}

	struct sockaddr_in addr = {};
	addr.sin_family = AF_INET;
	addr.sin_port = ntohs(1234);
	addr.sin_addr.s_addr = ntohl(INADDR_LOOPBACK); // 127.0.0.1
  
	int rv = connect(fd, (const struct sockaddr *)&addr, sizeof(addr));
	if (rv){die("connect");}

  std::vector<std::string> query_list = {
    "hola a todos kekers",
  };

  for(const std::string &s : query_list){
    int32_t err = send_req(fd,(uint8_t *)s.data(),s.size());
    if (err) {goto L_DONE;}
  }
  for (size_t i = 0; i < query_list.size(); ++i){
    int32_t err = read_res(fd);
    if (err) {goto L_DONE;}

  }

  L_DONE:

  close(fd);
	return 0;
}
