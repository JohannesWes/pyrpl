"""Deterministic unit tests for StreamReceiver frame parsing, NaN gap-fill, and
sequence-continuity detection. No hardware: a local loopback "fake server"
feeds crafted frames so behaviour is fully reproducible.

Run:  python test_gap_unit.py
"""
import socket
import struct
import threading
import time
import sys

import numpy as np

from stream_receiver import StreamReceiver, FRAME_MAGIC


def _frame(seq, gap, samples):
    n = len(samples)
    hdr = struct.pack("<4I", FRAME_MAGIC, seq, gap, n)
    body = struct.pack("<%di" % n, *samples) if n else b""
    return hdr + body


class FakeServer:
    """Accepts one connection, swallows the request, sends a scripted list of
    frames (bytes), then optionally lingers so the client can drain."""

    def __init__(self, frames, linger=5.0):
        self._frames = frames
        self._linger = linger
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        conn, _ = self._srv.accept()
        try:
            # read the 9-word (36-byte) request
            req = b""
            while len(req) < 36:
                chunk = conn.recv(36 - len(req))
                if not chunk:
                    return
                req += chunk
            for f in self._frames:
                conn.sendall(f)
                time.sleep(0.005)
            time.sleep(self._linger)
        finally:
            conn.close()
            self._srv.close()


def _collect(frames, settle=0.6, expect_frames=None):
    srv = FakeServer(frames)
    srv.start()
    rx = StreamReceiver("127.0.0.1", srv.port)
    rx.start()
    # Wait until all expected frames have been parsed (robust to scheduling),
    # falling back to a fixed settle when no count is given.
    if expect_frames is not None:
        deadline = time.time() + max(settle, 3.0)
        while rx.n_frames < expect_frames and time.time() < deadline:
            time.sleep(0.01)
    else:
        time.sleep(settle)
    rx.stop()
    return rx, rx.read()


PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  PASS  %s" % name)
    else:
        FAILED += 1
        print("  FAIL  %s   %s" % (name, detail))


def test_nan_fill():
    print("\n[test_nan_fill] gap becomes exactly the right NaNs in place")
    frames = [
        _frame(0, 0, [10, 20, 30]),
        _frame(1, 5, []),            # 5 lost samples
        _frame(2, 0, [40, 50]),
        _frame(3, 0, []),            # keepalive
        _frame(4, 0, [60]),
    ]
    rx, data = _collect(frames)
    expected = np.array([10, 20, 30, np.nan, np.nan, np.nan, np.nan, np.nan,
                         40, 50, 60], dtype=np.float64)
    check("length", data.size == expected.size, "got %d want %d" % (data.size, expected.size))
    check("values+positions",
          np.array_equal(np.nan_to_num(data, nan=-999),
                         np.nan_to_num(expected, nan=-999)),
          "got %s" % data)
    check("NaN count == gap", int(np.isnan(data).sum()) == 5)
    check("n_samples counter", rx.n_samples == 6, "got %d" % rx.n_samples)
    check("n_gap counter", rx.n_gap == 5, "got %d" % rx.n_gap)
    check("keepalive counted", rx.n_keepalive == 1, "got %d" % rx.n_keepalive)
    check("no seq skips", rx.n_seq_skips == 0, "got %d" % rx.n_seq_skips)
    check("no error", rx.error is None, "%s" % rx.error)


def test_seq_skip_detection():
    print("\n[test_seq_skip_detection] a missing frame is flagged")
    frames = [
        _frame(0, 0, [1, 2]),
        _frame(1, 0, [3]),
        # seq 2 deliberately missing -> jump to 3 (2 -> next expected, got 3)
        _frame(3, 0, [4]),
    ]
    rx, data = _collect(frames)
    check("seq skip detected", rx.n_seq_skips == 1, "got %d" % rx.n_seq_skips)
    # real samples still delivered (1,2,3,4); skip is a separate signal
    check("real samples delivered", rx.n_samples == 4, "got %d" % rx.n_samples)


def test_clean_stream():
    print("\n[test_clean_stream] no gaps/skips on a clean run")
    frames = [_frame(i, 0, [i * 10, i * 10 + 1]) for i in range(50)]
    rx, data = _collect(frames, expect_frames=50)
    check("all samples", rx.n_samples == 100, "got %d" % rx.n_samples)
    check("zero gaps", rx.n_gap == 0)
    check("zero skips", rx.n_seq_skips == 0)
    check("no NaNs", int(np.isnan(data).sum()) == 0)


def main():
    print("=" * 64)
    print("StreamReceiver unit tests (NaN gap-fill / seq continuity)")
    print("=" * 64)
    test_nan_fill()
    test_seq_skip_detection()
    test_clean_stream()
    print("\n" + "=" * 64)
    print("RESULT: %d passed, %d failed" % (PASSED, FAILED))
    print("=" * 64)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
