#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/ip.h>
#include <poll.h>
#include <string>
#include <vector>
#include <iostream>
#include <sstream>

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

  size_t cur = 8;// skips the 8 bytes header from before 

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
		msg("size too small");
		return -1;
	}
	
	switch (data[0])
	{
	case TAG_NIL:
		printf("(nil)\n");
		return 1;

	case TAG_ERR:
		if (size < 1 + 8){
			msg("bad response tag err 1");
			return -1;
		}
		{
			int32_t code = 0;
			uint32_t len = 0;
			memcpy(&code, &data[1], 4);
			memcpy(&len, &data[1 + 4], 4);
			if (size < 1 + 8 + len){
				msg("bad response tag err 2");
				return -1;
			}
			printf("(err) %d %.*s\n", code, (int)len, &data[1 + 8]);
			return 1 + 8 + len;
		}

	case TAG_STR:
		if (size < 1 + 4){
			msg("bad response tag str 1");
			return -1;
		}
		{
			uint32_t len = 0;
			memcpy(&len, &data[1], 4);
			if (size < 1 + 4 + len){
				msg("bad response tag str 2");
				return -1;
			}
			printf("(str) %.*s\n", (int)len, &data[1 + 4]);
			return 1 + 4 + len;
		}

	case TAG_INT:
		if (size < 1 + 8){
			msg("bad response tag int");
			return -1;
		}
		{
			int64_t val = 0;
			memcpy(&val, &data[1], 8);
			printf("(int) %ld\n", val);
			return 1 + 8;
		}

	case TAG_DBL:
		if (size < 1 + 8){
			msg("bad response tag dbl");
			return -1;
		}
		{
			double val = 0;
			memcpy(&val, &data[1], 8);
			printf("(dbl) %g\n", val);
			return 1 + 8;
		}

	case TAG_ARR:
		if(size < 1 + 4){
			msg("bad response tag err");
			return -1; 
		}
		{
			uint32_t len = 0;
			memcpy(&len, &data[1], 4);
			printf("(arr) len=%u\n", len);
			size_t arr_bytes = 1 + 4;
			for (uint32_t i = 0; i < len; i++){
				int32_t rv = print_response(&data[arr_bytes], size - arr_bytes);
				if (rv < 0){ return rv;}
				arr_bytes += (size_t)rv;
			}
			printf("(arr) end\n");
			return (int32_t)arr_bytes;
		}

	default:
		msg("bad response print response 2");
		return -1;
	}
}

static int32_t read_res(int fd){
	char rbuf[4 + k_max_msg];
	errno = 0;
	int32_t err = read_full(fd, (uint8_t *)rbuf, 4);
	if (err){
		if (errno == 0){msg("EOF");} else {msg("read() error");}
		return err;
	}
	uint32_t len = 0;
	memcpy(&len, rbuf, 4);
	if (len > k_max_msg){
		msg("too long");
		return -1;
	}

	err = read_full(fd, (uint8_t *)&rbuf[4], len);
	if (err){
		msg("read() error");
		return err;
	}

	//print the result
	int32_t rv = print_response((uint8_t *)&rbuf[4], len);
	if ( rv > 0 && (uint32_t)rv != len){
		msg("bad response read res");
		rv = -1;
	}
	return rv;
}

static bool do_auth(int fd){
	const char *password = getenv("REDIS_PASSWORD");
	if (!password || strlen(password) == 0){ return true; }
	std::vector<std::string>  cmd = {"auth", password};
	if (send_req(fd, cmd) < 0) { return false; }
	return read_res(fd) >= 0;
}

// This the client side of the server

int main(int argc, char **argv){
	int fd = socket(AF_INET, SOCK_STREAM,0);
	if (fd < 0){die("socket()");}

	struct sockaddr_in addr = {};
	addr.sin_family = AF_INET;
	addr.sin_port = ntohs(1234);
	addr.sin_addr.s_addr = ntohl(INADDR_LOOPBACK); // 127.0.0.1
  
	int rv = connect(fd, (const struct sockaddr *)&addr, sizeof(addr));
	if (rv){die("connect");}

	if (argc > 1){
		if (!do_auth(fd)) { close(fd); return 1;}

		std::vector<std::string> cmd;
		for (int i = 1; i < argc; ++i){
			cmd.push_back(argv[i]);
		}
		if (send_req(fd, cmd) < 0) { close(fd); return 1; }
		if (read_res(fd) < 0){
			fprintf(stderr, "server closes connection\n");
			close(fd);
			return 1;
		}
		close(fd);
		return 0;
	} else {
		if (!do_auth(fd)) { close(fd); return 1; }
		// REPL mode
		printf("Connected to server. Type commands or 'quit' to exit.\n");
		printf("> ");
		fflush(stdout);

		while (true){
			struct pollfd pfds[2] = {
				{fd,           POLLIN, 0},
				{STDIN_FILENO, POLLIN, 0},
			};
			if (poll(pfds, 2, -1) < 0){ break; }

			// server closed or sent unexpected data
			if (pfds[0].revents & (POLLIN | POLLHUP)){
				char c;
				if (recv(fd, &c, 1, MSG_PEEK) == 0){
					fprintf(stderr, "server closed connection\n");
					break;
				}
			}

			// user typed a line
			if (pfds[1].revents & POLLIN){
				std::string line;
				if (!std::getline(std::cin, line)){ break; }

				if (line.empty()){
					printf("> ");
					fflush(stdout);
					continue;
				}
				if (line == "quit" || line == "exit"){ break; }

				std::vector<std::string> cmd;
				std::istringstream iss(line);
				std::string token;
				while (iss >> token){ cmd.push_back(token); }

				if (cmd.empty()){
					printf("> ");
					fflush(stdout);
					continue;
				}

				if (send_req(fd, cmd) < 0){ break; }
				if (read_res(fd) < 0){
					fprintf(stderr, "server closed connection\n");
					break;
				}
				printf("> ");
				fflush(stdout);
			}
		}
	}
	close(fd);
	return 0;
}
