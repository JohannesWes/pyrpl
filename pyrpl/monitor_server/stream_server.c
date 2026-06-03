/*
 * stream_server.c - ARM-side push-streaming server for the PyRPL scan module.
 *
 * Purpose
 * -------
 * The default monitor_server uses a request/response model: the PC must poll
 * the FPGA write pointer and pull batches of samples across the network. For
 * the scan module's ~30.5 kHz ring buffer (4096 samples = 134 ms), that puts a
 * hard real-time deadline on the PC, which is fragile to network/OS jitter.
 *
 * This server instead runs the deadline-critical drain loop *locally on the
 * ARM*, where access to the FPGA BRAM is microseconds and deterministic, and
 * *pushes* a continuous framed byte stream to the PC. The PC just does blocking
 * recv() - no polling, no per-batch round trips.
 *
 * Safety
 * ------
 * - Opens /dev/mem with O_RDONLY and maps PROT_READ only: this process can
 *   never write to the FPGA. Enabling/disabling streaming (writing
 *   STREAM_CONTROL) is the responsibility of the normal register path on the
 *   PC side. This keeps the streaming server a pure reader.
 * - Runs as a *separate* process on its own port; the proven monitor_server
 *   register path is untouched.
 *
 * Gap / overrun policy
 * --------------------
 * Data loss is never silently hidden. The server tracks its own read pointer
 * and the FPGA's cumulative sample counter. If the FPGA writer laps the reader
 * (produced >= depth since last sync), the server emits a frame with gap > 0
 * and zero payload, declaring exactly how many samples were lost, then
 * resyncs. The PC inserts that many NaN samples so the time axis stays honest.
 *
 * Wire protocol (all 32-bit words little-endian; ARM and x86 are both LE)
 * ----------------------------------------------------------------------
 * Client -> server, once after connect: 9-word (36-byte) request:
 *   [0] magic       = 0x52505353 ('RPSS')
 *   [1] mmap_base   physical base to map, page aligned (e.g. 0x40500000)
 *   [2] mmap_size   bytes to map (e.g. 0x00100000)
 *   [3] wrptr_addr  absolute addr of STREAM_WR_PTR  (e.g. 0x40500028)
 *   [4] samples_addr absolute addr of STREAM_SAMPLES(e.g. 0x4050002C)
 *   [5] data_addr   absolute addr of ring buffer    (e.g. 0x40530000)
 *   [6] depth       ring depth in samples           (e.g. 4096)
 *   [7] poll_us     idle sleep between polls in us   (e.g. 200)
 *   [8] sndbuf      SO_SNDBUF bytes, 0 = default (1 MB). Small values make
 *                   backpressure reach the ARM quickly (used to test overrun).
 *
 * Server -> client, repeated frames: 4-word (16-byte) header + payload:
 *   [0] magic = 0x52505346 ('RPSF')
 *   [1] seq   = frame sequence number, increments by 1 every frame (incl.
 *               keepalives). The PC checks strict continuity to prove no frame
 *               was lost - a network-independent losslessness invariant.
 *   [2] gap   = lost samples before this batch (NaN-fill count on PC)
 *   [3] n     = number of int32 samples that follow
 *   then n * int32 payload.
 * A gap==0, n==0 frame is a keepalive (sent when idle).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#define REQ_MAGIC   0x52505353u  /* 'RPSS' */
#define FRAME_MAGIC 0x52505346u  /* 'RPSF' */
#define MAX_DEPTH   65536u       /* sanity cap on ring depth */
#define KEEPALIVE_US 500000u     /* send keepalive after this much idle */

static int g_listen = -1;
static int g_conn = -1;

static void die(const char *msg) {
    perror(msg);
    if (g_conn >= 0) close(g_conn);
    if (g_listen >= 0) close(g_listen);
    exit(1);
}

/* recv exactly n bytes, return 0 on success, -1 on closed/error */
static int recv_all(int fd, void *buf, size_t n) {
    size_t got = 0;
    char *p = (char *)buf;
    while (got < n) {
        ssize_t r = recv(fd, p + got, n - got, 0);
        if (r == 0) return -1;          /* peer closed */
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        got += (size_t)r;
    }
    return 0;
}

/* send exactly n bytes, return 0 on success, -1 on error */
static int send_all(int fd, const void *buf, size_t n) {
    size_t sent = 0;
    const char *p = (const char *)buf;
    while (sent < n) {
        ssize_t s = send(fd, p + sent, n - sent, 0);
        if (s < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        sent += (size_t)s;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s PORT\n", argv[0]);
        exit(1);
    }
    signal(SIGPIPE, SIG_IGN);

    int portno = atoi(argv[1]);
    g_listen = socket(AF_INET, SOCK_STREAM, 0);
    if (g_listen < 0) die("socket");
    int enable = 1;
    setsockopt(g_listen, SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));

    struct sockaddr_in serv;
    memset(&serv, 0, sizeof(serv));
    serv.sin_family = AF_INET;
    serv.sin_addr.s_addr = INADDR_ANY;
    serv.sin_port = htons(portno);
    if (bind(g_listen, (struct sockaddr *)&serv, sizeof(serv)) < 0) die("bind");
    if (listen(g_listen, 1) < 0) die("listen");

    int memfd = open("/dev/mem", O_RDONLY | O_SYNC);
    if (memfd < 0) die("open /dev/mem");

    fprintf(stderr, "stream_server listening on port %d\n", portno);

    /* accept loop: serve one client at a time, survive reconnects */
    for (;;) {
        struct sockaddr_in cli;
        socklen_t clilen = sizeof(cli);
        g_conn = accept(g_listen, (struct sockaddr *)&cli, &clilen);
        if (g_conn < 0) { if (errno == EINTR) continue; die("accept"); }

        int one = 1;
        setsockopt(g_conn, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

        uint32_t req[9];
        if (recv_all(g_conn, req, sizeof(req)) < 0) { close(g_conn); g_conn = -1; continue; }
        if (req[0] != REQ_MAGIC) {
            fprintf(stderr, "bad request magic 0x%08x\n", req[0]);
            close(g_conn); g_conn = -1; continue;
        }
        uint32_t mmap_base   = req[1];
        uint32_t mmap_size   = req[2];
        uint32_t wrptr_addr  = req[3];
        uint32_t samples_addr= req[4];
        uint32_t data_addr   = req[5];
        uint32_t depth       = req[6];
        uint32_t poll_us     = req[7];
        uint32_t sndbuf      = req[8];

        if (depth == 0 || depth > MAX_DEPTH) { fprintf(stderr, "bad depth %u\n", depth); close(g_conn); g_conn=-1; continue; }
        if (poll_us == 0 || poll_us > 100000) poll_us = 200;
        if (sndbuf == 0) sndbuf = 1 << 20; /* default 1 MB */
        setsockopt(g_conn, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

        /* page-align the mmap (mmap_base is expected page aligned already) */
        long pagesz = sysconf(_SC_PAGESIZE);
        uint32_t map_off = mmap_base & ~(uint32_t)(pagesz - 1);
        uint32_t pad = mmap_base - map_off;
        size_t map_len = mmap_size + pad;
        volatile uint8_t *map = (volatile uint8_t *)mmap(NULL, map_len, PROT_READ,
                                                         MAP_SHARED, memfd, map_off);
        if (map == (void *)-1) { perror("mmap"); close(g_conn); g_conn=-1; continue; }
        volatile uint8_t *region = map + pad;  /* region[0] == mmap_base */

        volatile uint32_t *p_wrptr   = (volatile uint32_t *)(region + (wrptr_addr   - mmap_base));
        volatile uint32_t *p_samples = (volatile uint32_t *)(region + (samples_addr - mmap_base));
        volatile uint32_t *p_data    = (volatile uint32_t *)(region + (data_addr    - mmap_base));

        /* payload buffer: header (4 words) + up to depth samples */
        size_t framebuf_bytes = 16 + (size_t)depth * 4;
        uint8_t *framebuf = (uint8_t *)malloc(framebuf_bytes);
        if (!framebuf) { munmap((void *)map, map_len); close(g_conn); g_conn=-1; continue; }

        /* sync to current writer position */
        uint32_t rd = (*p_wrptr) % depth;
        uint32_t total_read = *p_samples;
        uint32_t seq = 0;
        uint32_t idle_us = 0;
        int alive = 1;

        fprintf(stderr, "client connected: base=0x%08x depth=%u poll=%uus, start total=%u rd=%u\n",
                mmap_base, depth, poll_us, total_read, rd);

        while (alive) {
            uint32_t wr  = (*p_wrptr) % depth;
            uint32_t cnt = *p_samples;
            uint32_t produced = cnt - total_read;  /* unsigned wrap-safe */

            if (produced == 0) {
                usleep(poll_us);
                idle_us += poll_us;
                if (idle_us >= KEEPALIVE_US) {
                    uint32_t *h = (uint32_t *)framebuf;
                    h[0] = FRAME_MAGIC; h[1] = seq++; h[2] = 0; h[3] = 0;
                    if (send_all(g_conn, framebuf, 16) < 0) alive = 0;
                    idle_us = 0;
                }
                continue;
            }
            idle_us = 0;

            if (produced >= depth) {
                /* overrun: FPGA lapped us. Honest over-report: declare the
                 * whole produced span as lost, deliver nothing, resync. */
                uint32_t *h = (uint32_t *)framebuf;
                h[0] = FRAME_MAGIC; h[1] = seq++; h[2] = produced; h[3] = 0;
                if (send_all(g_conn, framebuf, 16) < 0) { alive = 0; break; }
                rd = wr;
                total_read = cnt;
                continue;
            }

            /* normal: deliver `produced` samples from rd, handling wrap */
            uint32_t n = produced;
            uint32_t *h = (uint32_t *)framebuf;
            int32_t *payload = (int32_t *)(framebuf + 16);
            uint32_t first = (n < depth - rd) ? n : (depth - rd);
            uint32_t i;
            for (i = 0; i < first; i++)
                payload[i] = (int32_t)p_data[rd + i];
            for (i = 0; i < n - first; i++)
                payload[first + i] = (int32_t)p_data[i];

            h[0] = FRAME_MAGIC; h[1] = seq++; h[2] = 0; h[3] = n;
            if (send_all(g_conn, framebuf, 16 + (size_t)n * 4) < 0) { alive = 0; break; }

            rd = (rd + n) % depth;
            total_read = cnt;
        }

        fprintf(stderr, "client disconnected (seq=%u)\n", seq);
        free(framebuf);
        munmap((void *)map, map_len);
        close(g_conn);
        g_conn = -1;
    }
    return 0;
}
