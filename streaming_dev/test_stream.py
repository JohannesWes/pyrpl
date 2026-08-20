"""End-to-end test for the scan-module push-streaming path.

Orchestrates the whole thing against the live board (read-only on the FPGA
except for the reversible STREAM_CONTROL enable/disable):

  1. (re)compile stream_server natively on the board
  2. start stream_server detached on a spare port
  3. enable FPGA demod streaming via the monitor tool
  4. receive for DURATION seconds with StreamReceiver, NaN-filling gaps
  5. disable streaming, kill the server
  6. cross-check sample count against the FPGA's own counter and report

Run:  python test_stream.py
"""
import time
import sys
import numpy as np

import rp_ssh
from stream_receiver import (StreamReceiver, SCAN_BASE,
                             ADDR_STREAM_SAMPLES, ADDR_STREAM_WR_PTR)

PORT = 2223
DURATION = 10.0
MON = "/opt/redpitaya/bin/monitor"
SRV = "/root/streaming_dev/stream_server"
ADDR_INPUT_SELECT = SCAN_BASE + 0x1C
ADDR_STREAM_CONTROL = SCAN_BASE + 0x20


def mon_read(addr):
    rc, out, err = rp_ssh.run("%s 0x%08x" % (MON, addr))
    return int(out.strip(), 16)


def mon_write(addr, val):
    rp_ssh.run("%s 0x%08x 0x%x" % (MON, addr, val))


def main():
    print("=" * 64)
    print("scan-module push-streaming end-to-end test")
    print("=" * 64)

    # 1. compile (idempotent)
    rc, out, err = rp_ssh.run(
        "cd /root/streaming_dev && gcc -O2 -Wall -o stream_server stream_server.c "
        "&& echo BUILD_OK")
    if "BUILD_OK" not in out:
        print("BUILD FAILED:\n", out, err); sys.exit(1)
    print("[1] stream_server compiled on board")

    # clean any stale server / free the port
    rp_ssh.run("killall stream_server 2>/dev/null; sleep 0.2; true")

    # 2. start detached
    rp_ssh.run_detached("%s %d" % (SRV, PORT), logfile="/root/streaming_dev/srv.log")
    time.sleep(0.5)
    rc, out, err = rp_ssh.run("ps aux | grep stream_server | grep -v grep | wc -l")
    print("[2] stream_server running instances:", out.strip())

    # 3. enable demod streaming (reversible)
    mon_write(ADDR_INPUT_SELECT, 0x2)      # demod
    mon_write(ADDR_STREAM_CONTROL, 0x3)    # enable + reset
    print("[3] FPGA streaming enabled; status=0x%x" % mon_read(SCAN_BASE + 0x24))

    # 4. receive. The server syncs its baseline to the FPGA counter at connect,
    #    so read the FPGA counter as close to start() as possible.
    rx = StreamReceiver("10.203.129.28", PORT)
    rx.start()
    fpga_samples_start = mon_read(ADDR_STREAM_SAMPLES)
    print("[4] receiving for %.1f s ..." % DURATION)
    t0 = time.time()
    time.sleep(DURATION)

    # 5. Snapshot the FPGA counter *while streaming is still live* (disabling it
    #    resets the counter to 0 in the FPGA, so it must be read here). Then keep
    #    receiving until the PC timeline catches up to that snapshot, proving no
    #    samples were dropped, before disabling.
    fpga_samples_end = mon_read(ADDR_STREAM_SAMPLES)
    elapsed = time.time() - t0
    target = (fpga_samples_end - fpga_samples_start) & 0xFFFFFFFF
    t_drain = time.time()
    while (rx.n_samples + rx.n_gap) < target and (time.time() - t_drain) < 2.0:
        time.sleep(0.02)
    rx.stop()
    mon_write(ADDR_STREAM_CONTROL, 0x0)
    rp_ssh.run("killall stream_server 2>/dev/null; true")
    print("[5] caught up to live snapshot, streaming disabled, server killed")

    # 6. report
    data = rx.read()
    fpga_produced = target
    n_real = rx.n_samples
    n_gap = rx.n_gap
    rate = (n_real + n_gap) / elapsed if elapsed else 0.0

    print("\n" + "=" * 64)
    print("RESULTS")
    print("=" * 64)
    print("  duration            : %.2f s" % elapsed)
    print("  frames received     : %d (keepalives: %d)" % (rx.n_frames, rx.n_keepalive))
    print("  frame seq skips     : %d  <- losslessness invariant (must be 0)" % rx.n_seq_skips)
    print("  real samples        : %d" % n_real)
    print("  gap (NaN) samples   : %d  <- FPGA-side overrun (must be 0)" % n_gap)
    print("  total timeline       : %d" % (n_real + n_gap))
    print("  effective rate      : %.1f Hz (expect ~30517)" % rate)
    print("  FPGA snapshot (ctr) : %d  (informational; +-SSH-read jitter)" % fpga_produced)
    print("  array len / NaNs    : %d / %d" % (data.size, int(np.isnan(data).sum())))
    if data.size:
        finite = data[np.isfinite(data)]
        if finite.size:
            print("  sample stats        : min=%d max=%d mean=%.1f" % (
                finite.min(), finite.max(), finite.mean()))

    # The PC timeline reaching at least the live FPGA snapshot is a sanity check
    # (>=0 = nothing dropped; the overshoot is just SSH-read jitter on the
    # baseline, ~hundreds of ms x 30.5 kHz, so it is not a pass gate).
    diff = (n_real + n_gap) - fpga_produced
    print("\n  timeline - FPGA snapshot : %d samples (>=0 expected; jitter ok)" % diff)

    # Pass criteria are timing-independent: every frame arrived in order (TCP +
    # strict seq), the FPGA never overran the ARM drainer, and the sustained
    # rate matches the physical demod rate.
    ok = (rx.error is None and rx.n_seq_skips == 0 and n_gap == 0
          and 27000 < rate < 34000)
    print("\n  RESULT: %s" % ("PASS" if ok else "CHECK"))
    if rx.error:
        print("  receiver error:", rx.error)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
