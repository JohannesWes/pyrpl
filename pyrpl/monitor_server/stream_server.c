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
 * Decoupled drain / send (DRAM ring) -- the headroom feature
 * ----------------------------------------------------------
 * The BRAM drain and the TCP send are decoupled by a large userspace ring
 * buffer in ordinary DRAM (default 16 MB). Every iteration the server drains
 * the FPGA BRAM ring into the userspace ring (microsecond memcpy, always keeps
 * the hard 134 ms FPGA deadline met) and *separately* tries a NON-BLOCKING send
 * of the userspace ring to TCP. If the PC's receive path stalls (GC pause, OS
 * freeze, network hiccup), send() returns EAGAIN but the drain keeps running -
 * the userspace ring absorbs the backlog. Headroom is therefore
 *   ring_bytes / wire_rate
 * e.g. 16 MB / ~1 MB/s ~= 15+ s, vs. the ~1.3 s a blocking send + 160 KB
 * SO_SNDBUF gives. Only if the *userspace ring itself* fills do we drop, and
 * that drop is reported as an explicit gap (NaN-filled on the PC).
 *
 * The previous design did drain->blocking-send in one step, so a stalled PC
 * blocked send() which halted the drain, and the FPGA lapped the reader after
 * 134 ms. That coupling is what limited headroom; the ring removes it.
 *
 * Frame coalescing
 * ----------------
 * Samples are accumulated in a staging buffer and emitted as one frame when the
 * staging reaches max_frame_samples OR coalesce_us elapses (default 5 ms). This
 * keeps the 16-byte frame header from dominating the wire (the old per-iteration
 * framing sent ~3 samples/frame = >50% header overhead). At ~5 ms latency the
 * frames hold ~150 samples @30 kHz / ~500 @100 kHz, dropping the overhead to a
 * few percent. coalesce_us=1 effectively disables coalescing (flush every loop).
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
 * Data loss is never silently hidden. Two loss sources, both reported as gap:
 *   1. userspace ring full (PC stalled longer than the ring holds): the staged
 *      samples that cannot be enqueued are counted into pending_gap.
 *   2. BRAM overrun (produced >= depth, i.e. the drain itself fell >134 ms
 *      behind -- only under severe CPU starvation now): the whole produced span
 *      is counted into pending_gap, after flushing any still-valid staged data.
 * pending_gap is carried in the `gap` field of the next emitted frame (or a
 * dedicated gap-only frame), so the PC inserts exactly that many NaN samples.
 *
 * Wire protocol (all 32-bit words little-endian; ARM and x86 are both LE)
 * ----------------------------------------------------------------------
 * Client -> server, once after connect: 11-word (44-byte) request:
 *   [0] magic       = 0x52505353 ('RPSS')
 *   [1] mmap_base   physical base to map, page aligned (e.g. 0x40500000)
 *   [2] mmap_size   bytes to map (e.g. 0x00100000)
 *   [3] wrptr_addr  absolute addr of STREAM_WR_PTR  (e.g. 0x40500028)
 *   [4] samples_addr absolute addr of STREAM_SAMPLES(e.g. 0x4050002C)
 *   [5] data_addr   absolute addr of ring buffer    (e.g. 0x40530000)
 *   [6] depth       FPGA ring depth in samples       (e.g. 4096)
 *   [7] poll_us     idle sleep between polls in us    (e.g. 200)
 *   [8] sndbuf      SO_SNDBUF bytes, 0 = default (1 MB)
 *   [9] ring_bytes  userspace DRAM ring size, 0 = default 16 MB
 *   [10] coalesce_us max latency before flushing a partial frame, 0 = 5000
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
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#define REQ_MAGIC   0x52505353u  /* 'RPSS' */
#define FRAME_MAGIC 0x52505346u  /* 'RPSF' */
#define REQ_WORDS   11u
#define MAX_DEPTH   65536u           /* sanity cap on FPGA ring depth */
#define KEEPALIVE_US 500000ull       /* send keepalive after this much idle */
#define DEFAULT_RING (16u << 20)     /* 16 MB userspace ring */
#define MIN_RING     (1u << 16)      /* 64 KB floor */
#define MAX_RING     (256u << 20)    /* 256 MB ceiling */
#define DEFAULT_COALESCE_US 5000u    /* 5 ms */

static int g_listen = -1;
static int g_conn = -1;

static void die(const char *msg) {
    perror(msg);
    if (g_conn >= 0) close(g_conn);
    if (g_listen >= 0) close(g_listen);
    exit(1);
}

static uint64_t now_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000000ull + (uint64_t)tv.tv_usec;
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

/* ------------------------------------------------------------------------- *
 * Per-connection streaming context
 * ------------------------------------------------------------------------- */
typedef struct {
    int fd;
    volatile uint32_t *p_wrptr;
    volatile uint32_t *p_samples;
    volatile uint32_t *p_data;
    uint32_t depth;
    uint32_t poll_us;
    uint32_t coalesce_us;
    uint32_t max_fs;          /* max samples per frame (== depth) */

    /* userspace byte ring of already-framed bytes ready for TCP */
    uint8_t *ring;
    size_t   cap;
    size_t   rtail;           /* next byte to send */
    size_t   rused;           /* bytes currently buffered */

    /* staging buffer of samples not yet framed (coalescing) */
    int32_t *stage;
    uint32_t stage_n;
    uint64_t stage_start;     /* now_us() when staging began */

    uint32_t pending_gap;     /* lost samples awaiting report */
    uint32_t seq;
    uint32_t rd;              /* FPGA ring read index */
    uint32_t total_read;      /* last seen *p_samples */
    uint64_t last_active;     /* for keepalive timing */
} sctx;

static inline size_t ring_room(const sctx *c) { return c->cap - c->rused; }

/* write len bytes into the ring; caller must ensure room */
static void ring_write(sctx *c, const uint8_t *src, size_t len) {
    size_t head = (c->rtail + c->rused) % c->cap;
    size_t first = c->cap - head;
    if (first > len) first = len;
    memcpy(c->ring + head, src, first);
    if (len > first) memcpy(c->ring, src + first, len - first);
    c->rused += len;
}

/* try to enqueue one frame (header + n samples). Returns 1 if enqueued, 0 if
 * the ring has no room. */
static int try_enqueue(sctx *c, uint32_t gap, uint32_t n, const int32_t *payload) {
    size_t flen = 16 + (size_t)n * 4;
    if (ring_room(c) < flen) return 0;
    uint32_t hdr[4];
    hdr[0] = FRAME_MAGIC; hdr[1] = c->seq; hdr[2] = gap; hdr[3] = n;
    ring_write(c, (const uint8_t *)hdr, 16);
    if (n) ring_write(c, (const uint8_t *)payload, (size_t)n * 4);
    c->seq++;
    return 1;
}

/* flush staged samples (and any pending gap) into the ring as one frame. If the
 * ring is full, the staged samples are themselves lost and folded into
 * pending_gap so nothing is silently dropped. */
static void flush_stage(sctx *c) {
    if (c->stage_n == 0 && c->pending_gap == 0) return;
    if (try_enqueue(c, c->pending_gap, c->stage_n, c->stage)) {
        c->pending_gap = 0;
        c->stage_n = 0;
    } else {
        /* no room: the staged samples are lost too (chronologically after the
         * existing pending_gap), keep accumulating for a later report */
        c->pending_gap += c->stage_n;
        c->stage_n = 0;
    }
}

/* copy `produced` samples from the FPGA BRAM ring into the staging buffer,
 * flushing to the userspace ring whenever a frame's worth has accumulated */
static void append_produced(sctx *c, uint32_t produced) {
    while (produced > 0) {
        if (c->stage_n >= c->max_fs) flush_stage(c);
        uint32_t space = c->max_fs - c->stage_n;
        uint32_t take = (produced < space) ? produced : space;
        if (c->stage_n == 0) c->stage_start = now_us();
        uint32_t first = c->depth - c->rd;
        if (first > take) first = take;
        uint32_t i;
        for (i = 0; i < first; i++)
            c->stage[c->stage_n + i] = (int32_t)c->p_data[c->rd + i];
        for (i = 0; i < take - first; i++)
            c->stage[c->stage_n + first + i] = (int32_t)c->p_data[i];
        c->stage_n += take;
        c->rd = (c->rd + take) % c->depth;
        produced -= take;
    }
}

/* non-blocking drain of the userspace ring to the socket. Returns 0 on success
 * (incl. EAGAIN), -1 on fatal socket error. */
static int ring_to_socket(sctx *c) {
    while (c->rused > 0) {
        size_t first = c->cap - c->rtail;
        if (first > c->rused) first = c->rused;
        ssize_t s = send(c->fd, c->ring + c->rtail, first, MSG_DONTWAIT);
        if (s > 0) {
            c->rtail = (c->rtail + (size_t)s) % c->cap;
            c->rused -= (size_t)s;
        } else if (s < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;  /* PC busy */
            if (errno == EINTR) continue;
            return -1;
        } else {
            return 0;
        }
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

        uint32_t req[REQ_WORDS];
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
        uint32_t ring_bytes  = req[9];
        uint32_t coalesce_us = req[10];

        if (depth == 0 || depth > MAX_DEPTH) { fprintf(stderr, "bad depth %u\n", depth); close(g_conn); g_conn=-1; continue; }
        if (poll_us == 0 || poll_us > 100000) poll_us = 200;
        if (sndbuf == 0) sndbuf = 1 << 20; /* default 1 MB */
        setsockopt(g_conn, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
        if (ring_bytes == 0) ring_bytes = DEFAULT_RING;
        if (ring_bytes < MIN_RING) ring_bytes = MIN_RING;
        if (ring_bytes > MAX_RING) ring_bytes = MAX_RING;
        /* the ring must hold at least one maximum-size frame */
        if (ring_bytes < 16 + depth * 4) ring_bytes = 16 + depth * 4;
        if (coalesce_us == 0) coalesce_us = DEFAULT_COALESCE_US;
        if (coalesce_us > 1000000u) coalesce_us = 1000000u;

        /* page-align the mmap (mmap_base is expected page aligned already) */
        long pagesz = sysconf(_SC_PAGESIZE);
        uint32_t map_off = mmap_base & ~(uint32_t)(pagesz - 1);
        uint32_t pad = mmap_base - map_off;
        size_t map_len = mmap_size + pad;
        volatile uint8_t *map = (volatile uint8_t *)mmap(NULL, map_len, PROT_READ,
                                                         MAP_SHARED, memfd, map_off);
        if (map == (void *)-1) { perror("mmap"); close(g_conn); g_conn=-1; continue; }
        volatile uint8_t *region = map + pad;  /* region[0] == mmap_base */

        sctx c;
        memset(&c, 0, sizeof(c));
        c.fd = g_conn;
        c.p_wrptr   = (volatile uint32_t *)(region + (wrptr_addr   - mmap_base));
        c.p_samples = (volatile uint32_t *)(region + (samples_addr - mmap_base));
        c.p_data    = (volatile uint32_t *)(region + (data_addr    - mmap_base));
        c.depth = depth;
        c.poll_us = poll_us;
        c.coalesce_us = coalesce_us;
        c.max_fs = depth;
        c.cap = ring_bytes;
        c.ring = (uint8_t *)malloc(c.cap);
        c.stage = (int32_t *)malloc((size_t)depth * 4);
        if (!c.ring || !c.stage) {
            fprintf(stderr, "alloc failed (ring=%zu stage=%u)\n", c.cap, depth * 4);
            free(c.ring); free(c.stage);
            munmap((void *)map, map_len); close(g_conn); g_conn=-1; continue;
        }

        /* sync to current writer position */
        c.rd = (*c.p_wrptr) % depth;
        c.total_read = *c.p_samples;
        c.last_active = now_us();
        int alive = 1;

        fprintf(stderr, "client connected: base=0x%08x depth=%u poll=%uus ring=%zuB "
                "coalesce=%uus, start total=%u rd=%u\n",
                mmap_base, depth, poll_us, c.cap, coalesce_us, c.total_read, c.rd);

        while (alive) {
            uint32_t cnt = *c.p_samples;
            uint32_t produced = cnt - c.total_read;  /* unsigned wrap-safe */

            if (produced >= depth) {
                /* hard fall-behind: FPGA lapped the drainer. Flush still-valid
                 * staged data first (it precedes the loss), then count the loss. */
                flush_stage(&c);
                c.pending_gap += produced;
                c.rd = (*c.p_wrptr) % depth;
                c.total_read = cnt;
                c.last_active = now_us();
            } else if (produced > 0) {
                append_produced(&c, produced);
                c.total_read = cnt;
                c.last_active = now_us();
            }

            uint64_t t = now_us();
            if (c.stage_n > 0 &&
                (c.stage_n >= c.max_fs || (t - c.stage_start) >= c.coalesce_us))
                flush_stage(&c);
            if (c.pending_gap > 0)
                flush_stage(&c);  /* report loss asap (gap-only frame if needed) */

            if (produced == 0 && c.stage_n == 0 && c.pending_gap == 0 &&
                c.rused == 0 && (t - c.last_active) >= KEEPALIVE_US) {
                int32_t dummy = 0;
                if (try_enqueue(&c, 0, 0, &dummy)) c.last_active = t;
            }

            if (ring_to_socket(&c) < 0) { alive = 0; break; }

            if (produced == 0) usleep(poll_us);
        }

        fprintf(stderr, "client disconnected (seq=%u, pending_gap=%u)\n",
                c.seq, c.pending_gap);
        free(c.ring);
        free(c.stage);
        munmap((void *)map, map_len);
        close(g_conn);
        g_conn = -1;
    }
    return 0;
}
