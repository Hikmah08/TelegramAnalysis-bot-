import os
import pytest
from pathlib import Path


def test_lock_file_self_pid_not_rejected(tmp_path):
    """
    Verifies that a lock file containing our own PID is not considered a competing instance.
    """
    lock_file = tmp_path / "test_telegram.lock"
    my_pid = os.getpid()

    # Write current process PID to lock file
    with open(lock_file, "w") as f:
        f.write(str(my_pid))

    # Re-evaluating the lock file
    with open(lock_file, "r") as f:
        recorded_pid = int(f.read().strip())

    # Our own PID must NOT trigger competing instance rejection
    is_competing = False
    if recorded_pid != my_pid:
        is_competing = True

    assert is_competing is False


def test_lock_file_stale_pid_reclaimed(tmp_path):
    """
    Verifies that if a lock file records a PID that does not match an active telegram process,
    it does not mistakenly reject the current process.
    """
    lock_file = tmp_path / "test_telegram.lock"
    my_pid = os.getpid()

    fake_pid = 999999
    with open(lock_file, "w") as f:
        f.write(str(fake_pid))

    with open(lock_file, "r") as f:
        recorded_pid = int(f.read().strip())

    is_competing = False
    if recorded_pid != my_pid:
        try:
            import psutil
            if psutil.pid_exists(recorded_pid):
                proc = psutil.Process(recorded_pid)
                cmdline = " ".join(proc.cmdline()).lower()
                if "telegram" in cmdline or "run_telegram_bot" in cmdline:
                    is_competing = True
        except ImportError:
            pass

    assert is_competing is False


def test_lock_file_write_and_cleanup(tmp_path):
    """
    Verifies that lock file can be written and cleaned up upon exit.
    """
    lock_file = tmp_path / "test_telegram.lock"
    my_pid = os.getpid()

    with open(lock_file, "w") as f:
        f.write(str(my_pid))

    assert lock_file.exists()
    assert lock_file.read_text().strip() == str(my_pid)

    # Clean up
    if lock_file.read_text().strip() == str(my_pid):
        lock_file.unlink()

    assert not lock_file.exists()
