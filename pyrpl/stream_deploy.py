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
"""Deploy / start / stop the scan-module push-streaming server on the board.

Kept free of Qt and the pyrpl package so it can be tested standalone. Operates
on a raw ``paramiko.SSHClient`` (``RedPitaya.ssh.ssh``). The server is compiled
natively on the board with its own gcc, which guarantees the right ABI for
whatever Red Pitaya OS is installed (no cross-toolchain needed). It runs as a
*separate* process from the register ``monitor_server`` on its own port, so the
proven register path is never touched.
"""
import os
import logging

logger = logging.getLogger(name=__name__)

# C source shipped alongside monitor_server.c
STREAM_SERVER_C = os.path.join(os.path.dirname(__file__),
                               'monitor_server', 'stream_server.c')
STREAM_SERVER_BIN = 'stream_server'


def _exec(ssh, cmd, timeout=30):
    """Run a command via a raw paramiko SSHClient, return (rc, out, err)."""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def _match_pattern(port):
    # Bracket the first char so the regex does not match pgrep/pkill's own
    # command line (which would otherwise contain the literal pattern).
    return "[%s]%s %d" % (STREAM_SERVER_BIN[0], STREAM_SERVER_BIN[1:], port)


def is_running(ssh, port):
    """True if a stream_server is listening on ``port``."""
    rc, out, _ = _exec(ssh, "pgrep -f '%s' || true" % _match_pattern(port))
    return bool(out.strip())


def deploy_and_start(ssh, port, remote_dir, c_source=STREAM_SERVER_C,
                     force_recompile=False):
    """Ensure stream_server is built and running on ``port``.

    Idempotent: uploads + compiles the source only if the binary is missing
    (or ``force_recompile``), then starts a detached instance if not already
    running. Returns the port on success, raises RuntimeError on failure.
    """
    remote_dir = remote_dir.rstrip('/') + '/'
    remote_src = remote_dir + 'stream_server.c'
    remote_bin = remote_dir + STREAM_SERVER_BIN

    if is_running(ssh, port):
        logger.debug("stream_server already running on port %d", port)
        return port

    _exec(ssh, "mkdir -p %s" % remote_dir)

    # (re)compile if needed
    rc, out, _ = _exec(ssh, "test -x %s && echo yes || echo no" % remote_bin)
    need_build = force_recompile or out.strip() != "yes"
    if need_build:
        if not os.path.isfile(c_source):
            raise RuntimeError("stream_server source not found: %s" % c_source)
        logger.info("uploading and compiling stream_server on the board ...")
        sftp = ssh.open_sftp()
        try:
            sftp.put(c_source, remote_src)
        finally:
            sftp.close()
        rc, out, err = _exec(
            ssh, "cd %s && gcc -O2 -o %s stream_server.c && echo BUILD_OK"
                 % (remote_dir, STREAM_SERVER_BIN))
        if "BUILD_OK" not in out:
            raise RuntimeError("stream_server build failed:\n%s\n%s" % (out, err))

    # start detached so it survives the SSH channel closing
    logfile = remote_dir + "stream_server_%d.log" % port
    _exec(ssh, "setsid nohup %s %d >%s 2>&1 </dev/null &"
               % (remote_bin, port, logfile))

    # verify it came up
    import time
    for _ in range(20):
        if is_running(ssh, port):
            logger.info("stream_server started on port %d", port)
            return port
        time.sleep(0.1)
    rc, log, _ = _exec(ssh, "tail -n 20 %s 2>/dev/null" % logfile)
    raise RuntimeError("stream_server failed to start on port %d:\n%s" % (port, log))


def stop(ssh, port):
    """Stop the stream_server instance on ``port`` (no error if not running)."""
    _exec(ssh, "pkill -f '%s' || true" % _match_pattern(port))
