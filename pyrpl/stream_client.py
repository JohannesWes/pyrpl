###############################################################################
#    pyrpl - DSP servo controller for quantum optics with the RedPitaya
#    Copyright (C) 2014-2016  Leonhard Neuhaus  (neuhaus@spectro.jussieu.fr)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
###############################################################################
"""PC-side client for the Red Pitaya scan-module push-streaming server.

This complements the request/response :class:`MonitorClient`. Instead of the PC
polling the FPGA write pointer and pulling batches across the network (a hard
real-time deadline against a 134 ms ring buffer), the streaming server runs the
drain loop *locally on the ARM* and pushes a continuous framed byte stream. The
PC just does blocking ``recv()`` in a background thread - no polling, no
per-batch round trips.

The class is deliberately transport-only and free of Qt / pyrpl-package
dependencies (stdlib + numpy), so it can be unit tested in isolation. Enabling
and disabling the FPGA stream (writing ``STREAM_CONTROL``) stays on the normal
register path; see :class:`~pyrpl.hardware_modules.scan.Scan`.

Wire protocol: see ``pyrpl/monitor_server/stream_server.c``.

Gap policy
----------
Data loss is never silently hidden. The ARM server, which knows its own read
pointer and the FPGA's cumulative sample counter with no network in between,
reports any overrun as an explicit ``gap`` count. This client inserts exactly
that many NaN samples so the time axis stays truthful. Strict per-frame
sequence numbers additionally guarantee no frame was dropped in transit.
"""
import socket
import struct
import threading
import logging
import time

import numpy as np

logger = logging.getLogger(name=__name__)

REQ_MAGIC = 0x52505353    # 'RPSS'
FRAME_MAGIC = 0x52505346  # 'RPSF'
REQUEST_WORDS = 11        # uint32 words in the request header

# Scan-module register/BRAM offsets relative to its base address (scan_new.v).
SCAN_STREAM_WR_PTR_OFFSET = 0x28
SCAN_STREAM_SAMPLES_OFFSET = 0x2C
SCAN_DATA3_OFFSET = 0x30000
SCAN_MMAP_SIZE = 0x00100000   # 1 MB covers the module's registers + BRAM banks
SCAN_RING_DEPTH = 4096


class StreamClient(object):
    """Background-thread receiver turning the ARM push stream into a NaN-aware
    sample buffer.

    Parameters
    ----------
    host : str
        Red Pitaya hostname / IP.
    port : int
        TCP port the streaming server listens on (separate from the register
        ``monitor_server`` port).
    addr_base : int
        Physical base address of the scan module (e.g. ``0x40500000``).
    depth : int
        Ring-buffer depth in samples (``scan_new.v`` BRAM depth, 4096).
    poll_us : int
        Idle polling interval the ARM drain loop uses when caught up.
    sndbuf, rcvbuf : int
        Optional socket buffer overrides (0 = OS default). Mainly for testing
        backpressure behaviour.
    ring_bytes : int
        Size of the ARM-side userspace DRAM ring that decouples the BRAM drain
        from TCP send (0 = server default, 16 MB). This sets the PC-stall
        headroom: ``ring_bytes / wire_rate`` seconds before any sample is lost
        (e.g. 16 MB at ~1 MB/s wire rate => ~15 s).
    coalesce_us : int
        Max latency (microseconds) the ARM waits to batch samples into one frame
        (0 = server default, 5000). Larger reduces per-frame header overhead and
        wire rate at the cost of latency; 1 ~ no coalescing.

    Notes
    -----
    The buffer accumulates the whole session by default (fine at ~122 KB/s).
    Drain it periodically with :meth:`read` for long runs.
    """

    def __init__(self, host, port, addr_base,
                 depth=SCAN_RING_DEPTH, poll_us=200, sndbuf=0, rcvbuf=0,
                 ring_bytes=0, coalesce_us=0):
        self.host = host
        self.port = int(port)
        self._rcvbuf = rcvbuf
        self._req = struct.pack(
            "<%dI" % REQUEST_WORDS, REQ_MAGIC,
            addr_base, SCAN_MMAP_SIZE,
            addr_base + SCAN_STREAM_WR_PTR_OFFSET,
            addr_base + SCAN_STREAM_SAMPLES_OFFSET,
            addr_base + SCAN_DATA3_OFFSET,
            int(depth), int(poll_us), int(sndbuf),
            int(ring_bytes), int(coalesce_us))
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._chunks = []
        # statistics (read-only for callers)
        self.n_samples = 0       # real samples received
        self.n_gap = 0           # NaN-filled (lost) samples
        self.n_frames = 0
        self.n_keepalive = 0
        self.n_seq_skips = 0     # frames missing per seq counter (TCP: must be 0)
        self._last_seq = None
        self._err = None
        self._stall_request = 0.0  # test hook: freeze before next recv

    # ------------------------------------------------------------------ #
    def start(self):
        """Connect, send the request, and start the background receive thread."""
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
        self._thread = threading.Thread(target=self._run, name="ScanStreamClient",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        """Stop the receive thread and close the socket."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
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
                    s = self._stall_request
                    self._stall_request = 0.0
                    time.sleep(s)
                magic, seq, gap, n = struct.unpack("<4I", self._recv_all(16))
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
                    pieces.append(np.frombuffer(payload, dtype="<i4").astype(np.float64))
                    self.n_samples += n
                block = pieces[0] if len(pieces) == 1 else np.concatenate(pieces)
                with self._lock:
                    self._chunks.append(block)
        except Exception as e:  # noqa: BLE001 - surface unexpected failures
            if self._running:
                self._err = e
                logger.error("ScanStreamClient thread error: %s", e)
        finally:
            self._running = False

    # ------------------------------------------------------------------ #
    def read(self):
        """Return and clear all samples accumulated so far.

        Returns
        -------
        np.ndarray (float64)
            Samples in order, with NaN where the FPGA overran the drainer.
        """
        with self._lock:
            if not self._chunks:
                return np.empty(0, dtype=np.float64)
            out = np.concatenate(self._chunks)
            self._chunks = []
        return out

    def available(self):
        """Number of samples currently buffered (not yet read())."""
        with self._lock:
            return sum(len(c) for c in self._chunks)

    def stats(self):
        """Return a dict of streaming statistics/counters."""
        return dict(n_samples=self.n_samples, n_gap=self.n_gap,
                    n_frames=self.n_frames, n_keepalive=self.n_keepalive,
                    n_seq_skips=self.n_seq_skips, error=self._err,
                    running=self._running)

    @property
    def error(self):
        return self._err

    @property
    def running(self):
        return self._running
