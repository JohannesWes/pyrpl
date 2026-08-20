"""Integration test of the PyRPL push-streaming infrastructure against the live
board, exercising the REAL shipping modules (pyrpl/stream_deploy.py and
pyrpl/stream_client.py) loaded directly (pyrpl's package __init__ needs Qt,
which isn't available here, so we import the Qt-free submodules by path).

It mirrors exactly what Scan.push_stream_start / push_stream_stop do:
  - deploy + start stream_server on the board (the redpitaya.ensure_stream_server
    path), compiled natively
  - enable the FPGA stream engine (here via the monitor tool, standing in for the
    register MonitorClient path)
  - run a StreamClient, recording ~RECORD_S seconds, draining to disk
  - stop, disable, and verify losslessness invariants

Run:  python test_integration.py [seconds]
"""
import os
import sys
import time
import importlib.util

import numpy as np
import paramiko

import rp_ssh

HOST = "10.203.129.28"
PORT = 3222                  # = monitor port (2222) + 1000, as redpitaya.py derives
REMOTE_DIR = "/opt/pyrpl"
SCAN_BASE = 0x40500000
MON = "/opt/redpitaya/bin/monitor"
RECORD_S = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
OUTFILE = os.path.join(os.path.dirname(__file__), "recorded_stream.npy")

PKG = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "pyrpl"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PKG, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stream_deploy = _load("pyrpl_stream_deploy", "stream_deploy.py")
stream_client = _load("pyrpl_stream_client", "stream_client.py")


def mon_write(addr, val):
    rp_ssh.run("%s 0x%08x 0x%x" % (MON, addr, val))


def main():
    print("=" * 66)
    print("PyRPL push-streaming integration test (%.0f s recording)" % RECORD_S)
    print("=" * 66)

    # raw paramiko client == RedPitaya.ssh.ssh in production
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username="root", password="root", timeout=10,
                look_for_keys=False, allow_agent=False)

    # 1. deploy + start (redpitaya.ensure_stream_server path)
    port = stream_deploy.deploy_and_start(ssh, PORT, REMOTE_DIR, force_recompile=True)
    print("[1] stream_server deployed+running on port", port)
    assert stream_deploy.is_running(ssh, port)

    # 2. enable FPGA stream engine (stands in for Scan._stream_ctrl_write)
    mon_write(SCAN_BASE + 0x1C, 0x2)     # input_select = demod
    mon_write(SCAN_BASE + 0x20, 0x3)     # STREAM_CONTROL = enable + reset
    print("[2] FPGA demod stream engine enabled")

    # 3. StreamClient (Scan.push_stream_start path)
    rx = stream_client.StreamClient(HOST, port, addr_base=SCAN_BASE)
    rx.start()
    print("[3] StreamClient receiving; recording for %.0f s ..." % RECORD_S)

    collected = []
    t0 = time.time()
    last_report = t0
    try:
        while time.time() - t0 < RECORD_S:
            time.sleep(0.5)
            block = rx.read()
            if block.size:
                collected.append(block)
            now = time.time()
            if now - last_report >= 10.0:
                s = rx.stats()
                print("    t=%5.1fs  samples=%d  gaps=%d  seq_skips=%d  frames=%d"
                      % (now - t0, s['n_samples'], s['n_gap'], s['n_seq_skips'],
                         s['n_frames']))
                last_report = now
        elapsed = time.time() - t0
    finally:
        # 4. stop (Scan.push_stream_stop path) + disable + stop server
        rx.stop()
        tail = rx.read()
        if tail.size:
            collected.append(tail)
        mon_write(SCAN_BASE + 0x20, 0x0)
        stream_deploy.stop(ssh, port)

    data = np.concatenate(collected) if collected else np.empty(0)
    np.save(OUTFILE, data)
    fsize = os.path.getsize(OUTFILE)

    s = rx.stats()
    rate = data.size / elapsed if elapsed else 0.0
    nan_count = int(np.isnan(data).sum())
    finite = data[np.isfinite(data)]

    print("\n" + "=" * 66)
    print("RESULTS")
    print("=" * 66)
    print("  recorded duration   : %.1f s" % elapsed)
    print("  frames received     : %d (keepalives %d)" % (s['n_frames'], s['n_keepalive']))
    print("  frame seq skips     : %d  (must be 0)" % s['n_seq_skips'])
    print("  real samples        : %d" % s['n_samples'])
    print("  gap (NaN) samples   : %d" % s['n_gap'])
    print("  effective rate      : %.1f Hz (expect ~30517)" % rate)
    print("  saved array         : %d samples -> %s (%.1f MB)"
          % (data.size, os.path.basename(OUTFILE), fsize / 1e6))
    print("  NaNs in saved data  : %d" % nan_count)
    if finite.size:
        print("  sample stats        : min=%d max=%d mean=%.1f std=%.1f"
              % (finite.min(), finite.max(), finite.mean(), finite.std()))
    print("  receiver error      : %s" % s['error'])

    ssh.close()
    ok = (s['error'] is None and s['n_seq_skips'] == 0 and s['n_gap'] == 0
          and 27000 < rate < 34000 and data.size > RECORD_S * 27000)
    print("\n  RESULT: %s" % ("PASS" if ok else "CHECK"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
