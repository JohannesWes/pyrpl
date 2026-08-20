"""Hardware test: deliberately overrun the FPGA ring and prove the loss is
reported as NaNs (never silently closed) and that streaming recovers.

Method: run clean for 2 s, then SIGSTOP the ARM stream_server for 0.4 s. While
the drainer is frozen the 30.5 kHz FPGA writer laps the 4096-sample ring
(~134 ms capacity) ~3x, overwriting unread data. On SIGCONT the server's next
loop sees produced >= depth, reports the lost span as a gap, and the receiver
NaN-fills it; real samples resume afterwards. SIGSTOP makes the overrun
deterministic and independent of socket-buffer sizes.

Run:  python test_overrun.py
"""
import time
import sys
import numpy as np

import rp_ssh
from stream_receiver import StreamReceiver, SCAN_BASE

PORT = 2224
MON = "/opt/redpitaya/bin/monitor"
SRV = "/root/streaming_dev/stream_server"
ADDR_INPUT_SELECT = SCAN_BASE + 0x1C
ADDR_STREAM_CONTROL = SCAN_BASE + 0x20
HOST = "10.203.129.28"


def mon_write(addr, val):
    rp_ssh.run("%s 0x%08x 0x%x" % (MON, addr, val))


def main():
    print("=" * 64)
    print("scan-module overrun / NaN-recovery test")
    print("=" * 64)

    rc, out, err = rp_ssh.run(
        "cd /root/streaming_dev && gcc -O2 -Wall -o stream_server stream_server.c "
        "&& echo BUILD_OK")
    if "BUILD_OK" not in out:
        print("BUILD FAILED:\n", out, err); sys.exit(1)
    rp_ssh.run("killall stream_server 2>/dev/null; sleep 0.2; true")
    rp_ssh.run_detached("%s %d" % (SRV, PORT), logfile="/root/streaming_dev/srv2.log")
    time.sleep(0.5)
    rc, pid_out, err = rp_ssh.run("pgrep -f '%s %d'" % (SRV, PORT))
    pid = pid_out.strip().split()[0] if pid_out.strip() else None
    print("[*] stream_server pid =", pid)

    mon_write(ADDR_INPUT_SELECT, 0x2)
    mon_write(ADDR_STREAM_CONTROL, 0x3)

    rx = StreamReceiver(HOST, PORT)
    rx.start()
    print("[*] running clean for 2 s ...")
    time.sleep(2.0)
    n_before = rx.n_samples
    print("[*] SIGSTOP the ARM drainer for 0.4 s (freezes draining -> ring laps) ...")
    rp_ssh.run("kill -STOP %s" % pid)
    time.sleep(0.4)
    rp_ssh.run("kill -CONT %s" % pid)
    print("[*] SIGCONT; letting the stream recover for 2 s ...")
    time.sleep(2.0)
    n_after = rx.n_samples

    mon_write(ADDR_STREAM_CONTROL, 0x0)
    rx.stop()
    rp_ssh.run("killall stream_server 2>/dev/null; true")

    data = rx.read()
    nan_idx = np.where(np.isnan(data))[0]
    finite_after_gap = 0
    if nan_idx.size:
        last_nan = nan_idx[-1]
        finite_after_gap = int(np.isfinite(data[last_nan + 1:]).sum())

    print("\n" + "=" * 64)
    print("RESULTS")
    print("=" * 64)
    print("  real samples          : %d" % rx.n_samples)
    print("  gap (NaN) samples     : %d" % rx.n_gap)
    print("  frame seq skips       : %d (must be 0; TCP intact)" % rx.n_seq_skips)
    print("  NaNs in data          : %d" % int(np.isnan(data).sum()))
    print("  real samples post-gap : %d (stream recovered)" % finite_after_gap)
    print("  samples before freeze : %d, after recovery : %d" % (n_before, n_after))
    print("  receiver error        : %s" % rx.error)

    ok = (rx.error is None
          and rx.n_gap > 0                                   # overrun happened
          and rx.n_seq_skips == 0                            # no frame loss
          and int(np.isnan(data).sum()) == rx.n_gap          # gap == NaNs
          and finite_after_gap > 1000)                       # recovered
    print("\n  RESULT: %s" % ("PASS" if ok else "CHECK"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
