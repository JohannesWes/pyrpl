"""Tiny reusable SSH/SCP helper for the development Red Pitaya board.

Read-only by default; used during scan-streaming development. Password auth
(root/root) because the board has no key installed. See memory:
redpitaya-board-facts.
"""
import sys
import paramiko

HOST = "10.203.129.28"
USER = "root"
PASSWORD = "root"


def connect(timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PASSWORD,
              timeout=timeout, allow_agent=False, look_for_keys=False)
    return c


def run(cmd, timeout=30):
    """Run a command, return (exit_status, stdout, stderr)."""
    c = connect()
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        c.close()


def run_detached(cmd, logfile="/dev/null"):
    """Start a long-running command fully detached (survives the SSH channel)."""
    full = "setsid nohup %s >%s 2>&1 </dev/null &" % (cmd, logfile)
    c = connect()
    try:
        c.exec_command(full, timeout=10)
        # give the shell a moment to fork before the channel closes
        import time as _t
        _t.sleep(0.3)
    finally:
        c.close()


def put(local, remote):
    c = connect()
    try:
        sftp = c.open_sftp()
        sftp.put(local, remote)
        sftp.close()
    finally:
        c.close()


if __name__ == "__main__":
    cmd = " ".join(sys.argv[1:]) or "echo hello from $(hostname)"
    rc, out, err = run(cmd)
    sys.stdout.write(out)
    if err.strip():
        sys.stderr.write("\n[STDERR]\n" + err)
    sys.exit(rc)
