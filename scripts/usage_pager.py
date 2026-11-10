#!/usr/bin/env python3
"""Forward pager keys, translating a standalone Escape into a clean interrupt."""

import errno
import fcntl
import os
import pty
import select
import signal
import sys
import termios
import tty


def main() -> None:
    window_size = fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))
    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, window_size)
        os.execvp("bash", [
            "bash", "-c", 'set -o pipefail; python3 "$@" | less -RK',
            "usage-popup", *sys.argv[1:],
        ])

    def resize(signum, frame):
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8))
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)

    saved = termios.tcgetattr(0)
    previous_resize = signal.signal(signal.SIGWINCH, resize)
    escaped = False
    try:
        tty.setraw(0)
        while True:
            ready, _, _ = select.select([0, master], [], [])
            if 0 in ready:
                data = os.read(0, 4096)
                if not data:
                    break
                # Arrow and page keys also start with Escape; wait for their suffix.
                if data.endswith(b"\x1b") and not select.select([0], [], [], 0.08)[0]:
                    data = data[:-1] + b"\x03"
                    escaped = True
                os.write(master, data)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
    finally:
        signal.signal(signal.SIGWINCH, previous_resize)
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        os.close(master)
        _, status = os.waitpid(pid, 0)
    raise SystemExit(0 if escaped else os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
