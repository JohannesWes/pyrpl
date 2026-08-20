"""PC-side receiver for the PyRPL scan-module push-streaming server.

Transport-only: it owns the streaming TCP socket, parses frames, NaN-fills
declared gaps, and exposes the accumulated samples through a thread-safe API.
Enabling/disabling the FPGA stream (writing STREAM_CONTROL) is *not* done here -
that stays on the normal register path - so this class can be reused as-is when
integrated into pyrpl's Scan module.

Wire protocol: see pyrpl/monitor_server/stream_server.c.
"""
import socket
import struct
import threading
import logging
import time

import numpy as np

logger = logging.getLogger(__name__)

REQ_MAGIC = 0x52505353    # 'RPSS'
FRAME_MAGIC = 0x52505346  # 'RPSF'

# Scan-module defaults (relative offsets from scan_new.v, base 0x40500000)
SCAN_BASE = 0x40500000
SCAN_MMAP_SIZE = 0x00100000          # 1 MB covers regs + data3 BRAM
ADDR_STREAM_WR_PTR = SCAN_BASE + 0x28
ADDR_STREAM_SAMPLES = SCAN_BASE + 0x2C
ADDR_DATA3 = SCAN_BASE + 0x30000
RING_DEPTH = 4096


class StreamReceiver:
    """Background-thread receiver that turns the push stream into a NaN-aware
    sample buffer.

    Usage:
        rx = StreamReceiver(host, port)
        rx.start()                 # enable FPGA streaming *before* this
        ...
        data = rx.read()           # float64 array, NaN where samples were lost
        rx.stop()

    The buffer keeps the full session by default (fine for ~122 KB/s). For long
    runs, drain it periodically with read().
    """

    def __init__(self, host, port,
                 mmap_base=SCAN_BASE, mmap_size=SCAN_MMAP_SIZE,
                 wrptr_addr=ADDR_STREAM_WR_PTR, samples_addr=ADDR_STREAM_SAMPLES,
                 data_addr=ADDR_DATA3, depth=RING_DEPTH, poll_us=200,
                 sndbuf=0, rcvbuf=0):
        self.host = host
        self.port = port
        self._rcvbuf = rcvbuf
        self._req = struct.pack("<9I", REQ_MAGIC, mmap_base, mmap_size,
                                wrptr_addr, samples_addr, data_addr,
                                depth, poll_us, sndbuf)
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._chunks = []          # list of np.float64 arrays
        # stats
        self.n_samples = 0         # real samples received
        self.n_gap = 0             # NaN-filled (lost) samples
        self.n_frames = 0
        self.n_keepalive = 0
        self.n_seq_skips = 0       # frames missing per the seq counter (TCP: should be 0)
        self._last_seq = None
        self._err = None
        self._stall_request = 0.0  # test hook: seconds to freeze before next recv

    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self._rcvbuf:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._rcvbuf)
        self._sock.settimeout(5.0)
        self._sock.connect((self.host, self.port))
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.sendall(self._req)
        self._sock.settimeout(2.0)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="StreamReceiver",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ------------------------------------------------------------------ #
    def _recv_all(self, n):
        buf = bytearray()
        while len(buf) < n:
            if not self._running:
                raise ConnectionError("receiver stopped")
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("stream socket closed by peer")
            buf += chunk
        return bytes(buf)

    def _run(self):
        try:
            while self._running:
                if self._stall_request:
                    # Test hook: simulate a PC stall (GC pause / busy thread) by
                    # not servicing the socket. The kernel recv buffer fills, the
                    # server's send() blocks, it falls behind the FPGA, and on
                    # resume reports the loss as a gap (-> NaN fill).
                    s = self._stall_request
                    self._stall_request = 0.0
                    time.sleep(s)
                hdr = self._recv_all(16)
                magic, seq, gap, n = struct.unpack("<4I", hdr)
                if magic != FRAME_MAGIC:
                    raise ValueError("bad frame magic 0x%08x" % magic)
                self.n_frames += 1
                if self._last_seq is not None:
                    expected = (self._last_seq + 1) & 0xFFFFFFFF
                    if seq != expected:
                        skipped = (seq - expected) & 0xFFFFFFFF
                        self.n_seq_skips += skipped
                        logger.error("frame seq discontinuity: expected %d got %d "
                                     "(%d missing)", expected, seq, skipped)
                self._last_seq = seq
                if gap == 0 and n == 0:
                    self.n_keepalive += 1
                    continue
                pieces = []
                if gap:
                    pieces.append(np.full(gap, np.nan, dtype=np.float64))
                    self.n_gap += gap
                    logger.warning("stream gap: %d samples lost (NaN-filled)", gap)
                if n:
                    payload = self._recv_all(n * 4)
                    samples = np.frombuffer(payload, dtype="<i4").astype(np.float64)
                    pieces.append(samples)
                    self.n_samples += n
                block = pieces[0] if len(pieces) == 1 else np.concatenate(pieces)
                with self._lock:
                    self._chunks.append(block)
        except Exception as e:  # noqa: BLE001 - surface any failure to caller
            # Only a failure while we *intended* to run is a real error; an
            # exception raised because stop() flipped _running is expected.
            if self._running:
                self._err = e
                logger.error("StreamReceiver thread error: %s", e)
        finally:
            self._running = False

    # ------------------------------------------------------------------ #
    def read(self):
        """Return and clear all samples accumulated so far (float64, NaN gaps)."""
        with self._lock:
            if not self._chunks:
                return np.empty(0, dtype=np.float64)
            out = np.concatenate(self._chunks)
            self._chunks = []
        return out

    def available(self):
        with self._lock:
            return sum(len(c) for c in self._chunks)

    @property
    def error(self):
        return self._err
