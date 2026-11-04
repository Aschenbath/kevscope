/* kevpush: send a file to a TCP listener on the host. Lets a QEMU guest hand back trace files.
 * Usage: kevpush host port file */
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s host port file\n", argv[0]);
        return 2;
    }
    int in = open(argv[3], O_RDONLY);
    if (in < 0) {
        perror(argv[3]);
        return 1;
    }
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)atoi(argv[2]));
    inet_pton(AF_INET, argv[1], &a.sin_addr);
    for (int tries = 0; connect(s, (struct sockaddr *)&a, sizeof(a)) < 0; tries++) {
        if (tries == 20) {
            perror("connect");
            return 1;
        }
        close(s);
        s = socket(AF_INET, SOCK_STREAM, 0);
        sleep(1);
    }
    /* First line tells the receiver what to call the file. */
    const char *base = strrchr(argv[3], '/');
    base = base ? base + 1 : argv[3];
    dprintf(s, "%s\n", base);
    char buf[65536];
    long total = 0;
    for (;;) {
        ssize_t n = read(in, buf, sizeof(buf));
        if (n <= 0) break;
        for (ssize_t off = 0; off < n;) {
            ssize_t w = write(s, buf + off, (size_t)(n - off));
            if (w <= 0) {
                perror("write");
                return 1;
            }
            off += w;
        }
        total += n;
    }
    close(s);
    printf("kevpush: sent %ld bytes of %s\n", total, base);
    return 0;
}
