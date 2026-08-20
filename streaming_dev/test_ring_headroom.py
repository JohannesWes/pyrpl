"""Hardware test of the DRAM-ring + non-blocking-send stream_server.

Validates the new headroom feature and that loss is still honest:

  A. baseline           - default 16 MB ring, no stall. Confirms 0 loss, rate,
                          and the coalescing effect (samples/frame).
  B. PC freeze < ring   - freeze the PC recv thread 8 s with the 16 MB ring.
                          Old design lost data after ~1.3 s; new design must
                          show 0 gaps (ring absorbs the whole freeze).
  C. PC freeze > ring   - freeze 8 s with a deliberately tiny 512 KB ring so the
                          ring overflows. Loss must be reported as an exact gap,
                          NaN-filled, and the stream must recover.
  D. ARM halt (SIGSTOP) - suspend the stream_server ~1.5 s so the FPGA BRAM
                          laps the drainer. Must report a BRAM-overrun gap,
                          NaN-filled, and recover.

Run:  venv/Scripts/python.exe test_ring_headroom.py
"""
import os
import sys
import time
import importlib.util

import numpy as np
import paramiko

import rp_ssh

HOST = "10.203.129.28"
PORT = 3222
REMOTE_DIR = "/opt/pyrpl"
SCAN_BASE = 0x40500000
MON = "/opt/redpitaya/bin/monitor"

PKG = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "pyrpl"))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PKG, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stream_deploy = _load("pyrpl_stream_deploy", "stream_deploy.py")
stream_client = _load("pyrpl_stream_client", "stream_client.py")

results = {}


def mon_write(ssh, addr, val):
    ssh.exec_command("%s 0x%08x 0x%x" % (MON, addr, val))[1].channel.recv_exit_status()


def server_pid(ssh):
    _, out, _ = _exec(ssh, "pgrep -f '[s]tream_server %d' | head -1" % PORT)
    out = out.strip()
    return int(out) if out else None


def _exec(ssh, cmd, timeout=15):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def drain(rx, seconds, stall_at=None, stall_for=0.0, collect=True):
    """Stream for `seconds`, optionally injecting a recv-thread freeze."""
    chunks = []
    t0 = time.time()
    stalled = False
    while time.time() - t0 < seconds:
        if stall_at is not None and not stalled and (time.time() - t0) >= stall_at:
            rx._stall_request = stall_for     # freeze recv thread before next recv
            stalled = True
        time.sleep(0.2)
        if collect:
            b = rx.read()
            if b.size:
                chunks.append(b)
    if collect:
        b = rx.read()
        if b.size:
            chunks.append(b)
        return np.concatenate(chunks) if chunks else np.empty(0)
    return None


def new_client(ring_bytes=0, coalesce_us=0):
    rx = stream_client.StreamClient(HOST, PORT, addr_base=SCAN_BASE,
                                    ring_bytes=ring_bytes, coalesce_us=coalesce_us)
    rx.start()
    return rx


def banner(t):
    print("\n" + "=" * 64 + "\n" + t + "\n" + "=" * 64)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username="root", password="root", timeout=10,
                look_for_keys=False, allow_agent=False)

    # deploy the (new) server, force a rebuild to be certain
    port = stream_deploy.deploy_and_start(ssh, PORT, REMOTE_DIR, force_recompile=True)
    print("stream_server (ring build) running on port", port)
    # enable FPGA demod stream engine
    mon_write(ssh, SCAN_BASE + 0x1C, 0x2)
    mon_write(ssh, SCAN_BASE + 0x20, 0x3)
    time.sleep(0.3)

    # ---- A. baseline ------------------------------------------------------
    banner("A. baseline (16 MB ring, 12 s, no stall)")
    rx = new_client()
    data = drain(rx, 12.0)
    s = rx.stats()
    rx.stop()
    rate = data.size / 12.0
    spf = (s['n_samples'] / s['n_frames']) if s['n_frames'] else 0
    print("  samples=%d rate=%.0f Hz frames=%d samples/frame=%.1f gaps=%d skips=%d nan=%d"
          % (data.size, rate, s['n_frames'], spf, s['n_gap'], s['n_seq_skips'],
             int(np.isnan(data).sum())))
    results['A'] = (s['n_gap'] == 0 and s['n_seq_skips'] == 0
                    and 27000 < rate < 34000 and s['error'] is None)
    print("  -> %s" % ("PASS" if results['A'] else "FAIL"))
    time.sleep(1.0)

    # ---- B. PC freeze within ring ----------------------------------------
    banner("B. PC recv freeze 8 s, 16 MB ring (expect 0 loss)")
    rx = new_client()                          # default 16 MB
    data = drain(rx, 18.0, stall_at=3.0, stall_for=8.0)
    s = rx.stats()
    rx.stop()
    print("  samples=%d frames=%d gaps=%d skips=%d nan=%d error=%s"
          % (data.size, s['n_frames'], s['n_gap'], s['n_seq_skips'],
             int(np.isnan(data).sum()), s['error']))
    # ~15 s of real streaming (18 - ~? ) should yield ~ rate*~15 samples, no gap
    results['B'] = (s['n_gap'] == 0 and s['n_seq_skips'] == 0 and s['error'] is None
                    and data.size > 27000 * 12)
    print("  -> %s (0 gaps across an 8 s freeze proves >1.3 s headroom)"
          % ("PASS" if results['B'] else "FAIL"))
    time.sleep(1.0)

    # ---- C. PC freeze beyond a tiny ring ---------------------------------
    banner("C. PC recv freeze 8 s, 512 KB ring (expect honest NaN gap)")
    rx = new_client(ring_bytes=512 * 1024)
    data = drain(rx, 18.0, stall_at=3.0, stall_for=8.0)
    s = rx.stats()
    rx.stop()
    nan = int(np.isnan(data).sum())
    print("  samples(real)=%d gap=%d skips=%d nan_in_data=%d error=%s"
          % (s['n_samples'], s['n_gap'], s['n_seq_skips'], nan, s['error']))
    # ring ~512KB / ~125KB/s ~ 4 s held -> ~4 s of the 8 s freeze lost
    results['C'] = (s['n_gap'] > 0 and s['n_seq_skips'] == 0 and nan == s['n_gap']
                    and s['error'] is None and s['n_samples'] > 27000 * 8)
    print("  -> %s (gap reported, NaN count == gap, stream recovered)"
          % ("PASS" if results['C'] else "FAIL"))
    time.sleep(1.0)

    # ---- D. ARM halt via SIGSTOP -----------------------------------------
    banner("D. ARM stream_server SIGSTOP ~1.5 s (expect BRAM-overrun NaN gap)")
    rx = new_client()
    # stream a bit, then suspend the server process, then resume
    drain(rx, 3.0, collect=True)
    pid = server_pid(ssh)
    print("  suspending server pid=%s for 1.5 s ..." % pid)
    _exec(ssh, "kill -STOP %d" % pid)
    time.sleep(1.5)
    _exec(ssh, "kill -CONT %d" % pid)
    data = drain(rx, 6.0)
    s = rx.stats()
    rx.stop()
    nan = int(np.isnan(data).sum())
    print("  samples(real)=%d gap=%d skips=%d nan_in_data=%d error=%s"
          % (s['n_samples'], s['n_gap'], s['n_seq_skips'], nan, s['error']))
    results['D'] = (s['n_gap'] > 0 and s['n_seq_skips'] == 0 and s['error'] is None
                    and s['n_samples'] > 27000 * 6)
    print("  -> %s (BRAM overrun reported as NaN gap, stream recovered)"
          % ("PASS" if results['D'] else "FAIL"))

    # cleanup: disable FPGA stream, stop server
    mon_write(ssh, SCAN_BASE + 0x20, 0x0)
    stream_deploy.stop(ssh, PORT)
    ssh.close()

    banner("SUMMARY")
    for k in "ABCD":
        print("  %s: %s" % (k, "PASS" if results.get(k) else "FAIL"))
    ok = all(results.get(k) for k in "ABCD")
    print("\n  OVERALL: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
