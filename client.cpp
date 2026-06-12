// ── client.cpp ────────────────────────────────────────────────────
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sstream>
#include <string>
#include <vector>
#include <iostream>

static int g_fd = -1;

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

// send RESP array of bulk strings
static void send_cmd(int fd, const std::vector<std::string> &cmd) {
    std::string req;
    req += "*";
    req += std::to_string(cmd.size());
    req += "\r\n";
    for (auto &s : cmd) {
        req += "$";
        req += std::to_string(s.size());
        req += "\r\n";
        req += s;
        req += "\r\n";
    }
    write_all(fd, (const uint8_t *)req.data(), req.size());
}

static std::string read_line(int fd) {
    std::string line;
    char c;
    while (read(fd, &c, 1) == 1) {
        if (c == '\r') {
            if (read(fd, &c, 1)) { break; }   // consume \n
            break;
        }
        line += c;
    }
    return line;
}

static std::string read_response(int fd) {
    std::string line = read_line(fd);
    if (line.empty()) return "(error) empty response";

    char type = line[0];
    std::string data = line.substr(1);

    switch (type) {
    case '+':
        return data;

    case '-':
        return "(error) " + data;

    case ':':
        return "(integer) " + data;

    case '$': {
        int len = std::stoi(data);
        if (len < 0) return "(nil)";
        std::string val(len, '\0');
        // read exactly len bytes
        size_t got = 0;
        while ((int)got < len) {
            int n = read(fd, &val[got], len - (int)got);
            if (n <= 0) break;
            got += n;
        }
        read_line(fd);   // consume trailing \r\n
        return val;
    }

    case '*': {
        int count = std::stoi(data);
        if (count < 0) return "(nil)";
        std::string result;
        for (int i = 0; i < count; i++) {
            result += std::to_string(i + 1) + ") ";
            result += read_response(fd);
            if (i < count - 1) result += "\n";
        }
        return result;
    }

    default:
        return "(error) unknown type " + line;
    }
}

static std::vector<std::string> parse_line(const std::string &line) {
    std::vector<std::string> tokens;
    std::istringstream iss(line);
    std::string token;
    while (iss >> token) {
        tokens.push_back(token);
    }
    return tokens;
}

static bool do_auth(int fd){
	const char *password = getenv("REDIS_PASSWORD");
	if (!password || strlen(password) == 0){ return true; }
	send_cmd(fd, {"auth", password});
    std::string resp = read_response(fd);
    if (resp.rfind("(error)", 0) == 0) {
        fprintf(stderr, "auth failed: %s\n", resp.c_str());
        return false;
    }
    return true;
}


int main(int argc, char **argv) {
    g_fd = socket(AF_INET, SOCK_STREAM, 0);

    struct sockaddr_in addr = {};
    addr.sin_family      = AF_INET;
    addr.sin_port        = htons(1234);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    if (connect(g_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("connect");
        return 1;
    }

    // single command mode
    if (argc > 1) {
        if (!do_auth(g_fd)) { return 1; }
        std::vector<std::string> cmd;
        for (int i = 1; i < argc; i++) {
            cmd.push_back(argv[i]);
        }
        send_cmd(g_fd, cmd);
        printf("%s\n", read_response(g_fd).c_str());
        close(g_fd);
        return 0;
    }

    // REPL mode
    if (!do_auth(g_fd)) { return 1;}
    printf("Connected. Type commands or 'quit'.\n> ");
    fflush(stdout);

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "quit" || line == "exit") { break; }
        if (line.empty()) {
            printf("> ");
            fflush(stdout);
            continue;
        }

        std::vector<std::string> cmd = parse_line(line);
        if (cmd.empty()) {
            printf("> ");
            fflush(stdout);
            continue;
        }

        send_cmd(g_fd, cmd);
        printf("%s\n> ", read_response(g_fd).c_str());
        fflush(stdout);
    }

    close(g_fd);
    return 0;
}

