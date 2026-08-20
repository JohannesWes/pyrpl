"""End-to-end test of scan.push_stream_* through a REAL Pyrpl instance.

This is the seam qudi's RedPitayaDataInStream actually uses but that the
monitor-tool integration test stubbed out:
  - scan.push_stream_start() -> rp.ensure_stream_server() via real self.ssh.ssh
                             -> scan._stream_ctrl_write() via real MonitorClient
  - real StreamClient receive thread
  - scan.push_stream_read() / push_stream_stats() / push_stream_stop()

We connect headless and do NOT reload the FPGA (the correct bitstream is already
loaded -- proven by the streaming integration test), only ensuring the register
server is up. Mirrors qudi's resource_manager connection otherwise.

Run:  venv/Scripts/python.exe real_pyrpl_pushtest.py [seconds]
"""
import sys
import time
import numpy as np

HOST = "10.203.129.28"
RECORD_S = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

import pyrpl

print("Connecting to %s (headless, no FPGA reload) ..." % HOST)
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False,
                reload_server=True, gui=False)
rp = p.rp
scan = rp.scan
print("Connected. scan module:", scan)

try:
    print("push_stream_start('demod') ...")
    rx = scan.push_stream_start(input_source="demod")
    print("  StreamClient:", rx, "running:", rx.running)

    collected = []
    t0 = time.time()
    while time.time() - t0 < RECORD_S:
        time.sleep(0.25)
        block = scan.push_stream_read()
        if block.size:
            collected.append(block)
    elapsed = time.time() - t0

    stats = scan.push_stream_stats()
    print("push_stream_stop() ...")
    scan.push_stream_stop()
    # final tail
    tail = scan.push_stream_read()
    if tail.size:
        collected.append(tail)

    data = np.concatenate(collected) if collected else np.empty(0)
    rate = data.size / elapsed if elapsed else 0.0
    nan_count = int(np.isnan(data).sum())

    print("=" * 60)
    print("REAL-PYRPL PUSH TEST RESULTS")
    print("=" * 60)
    print("  duration        : %.1f s" % elapsed)
    print("  samples         : %d" % data.size)
    print("  effective rate  : %.1f Hz (expect ~30517)" % rate)
    print("  gap(NaN) samples: %d" % stats.get("n_gap", -1))
    print("  seq skips       : %d" % stats.get("n_seq_skips", -1))
    print("  frames          : %d" % stats.get("n_frames", -1))
    print("  receiver error  : %s" % stats.get("error"))
    print("  NaNs in data    : %d" % nan_count)
    if data.size:
        finite = data[np.isfinite(data)]
        print("  sample stats    : min=%.0f max=%.0f mean=%.1f"
              % (finite.min(), finite.max(), finite.mean()))

    ok = (stats.get("error") is None and stats.get("n_seq_skips", 1) == 0
          and 27000 < rate < 34000 and data.size > RECORD_S * 27000)
    print("\n  RESULT: %s" % ("PASS" if ok else "CHECK"))
    rc = 0 if ok else 2
finally:
    try:
        scan.push_stream_stop()
    except Exception:
        pass
    try:
        rp.stop_stream_server()
    except Exception:
        pass

sys.exit(rc)
