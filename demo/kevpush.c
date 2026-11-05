/* Length-checked transfer with a receiver acknowledgement. Usage: kevpush host port file */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

static int send_all(int sock, const char *data, size_t size) {
    while (size) {
        ssize_t n = write(sock, data, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        data += n;
        size -= (size_t)n;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s host port file\n", argv[0]);
        return 2;
    }
    char *end;
    long port = strtol(argv[2], &end, 10);
    struct sockaddr_in address = {0};
    if (!*argv[2] || *end || port < 1 || port > 65535 || inet_pton(AF_INET, argv[1], &address.sin_addr) != 1) {
        fprintf(stderr, "invalid IPv4 address or port\n");
        return 2;
    }
    int input = open(argv[3], O_RDONLY), sock = -1, status = 1;
    struct stat info;
    if (input < 0 || fstat(input, &info) < 0) {
        perror(argv[3]);
        if (input >= 0) close(input);
        return 1;
    }
    if (!S_ISREG(info.st_mode) || info.st_size < 0) {
        fprintf(stderr, "input must be a regular file\n");
        close(input);
        return 1;
    }
    signal(SIGPIPE, SIG_IGN);
    address.sin_family = AF_INET;
    address.sin_port = htons((unsigned short)port);
    for (int attempt = 0; attempt <= 20; attempt++) {
        sock = socket(AF_INET, SOCK_STREAM, 0);
        if (sock < 0) goto failure;
        struct timeval timeout = {5, 0};
        if (setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0 ||
            setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0) goto failure;
        if (connect(sock, (struct sockaddr *)&address, sizeof(address)) == 0) break;
        if (attempt == 20) goto failure;
        close(sock);
        sock = -1;
        sleep(1);
    }
    const char *base = strrchr(argv[3], '/');
    base = base ? base + 1 : argv[3];
    char header[256], buffer[65536];
    int length = snprintf(header, sizeof(header), "KEVSCOPE/1 %lld %s\n", (long long)info.st_size, base);
    if (length < 0 || (size_t)length >= sizeof(header)) {
        fprintf(stderr, "filename is too long\n");
        goto done;
    }
    if (send_all(sock, header, (size_t)length) < 0) goto failure;
    long long total = 0;
    for (;;) {
        ssize_t n = read(input, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) goto failure;
        if (n == 0) break;
        if (send_all(sock, buffer, (size_t)n) < 0) goto failure;
        total += n;
    }
    if (total != (long long)info.st_size) {
        fprintf(stderr, "input size changed during transfer\n");
        goto done;
    }
    if (shutdown(sock, SHUT_WR) < 0) goto failure;
    char ack[3];
    size_t received = 0;
    while (received < sizeof(ack)) {
        ssize_t n = read(sock, ack + received, sizeof(ack) - received);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) {
            fprintf(stderr, "receiver did not acknowledge the complete file\n");
            goto done;
        }
        received += (size_t)n;
    }
    if (memcmp(ack, "OK\n", sizeof(ack)) != 0) {
        fprintf(stderr, "receiver rejected the transfer\n");
        goto done;
    }
    printf("kevpush: sent and verified %lld bytes of %s\n", total, base);
    status = 0;
    goto done;
failure:
    perror("kevpush");
done:
    if (sock >= 0) close(sock);
    close(input);
    return status;
}
