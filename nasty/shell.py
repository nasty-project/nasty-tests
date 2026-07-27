import shutil
import subprocess
import os
import re


def run(cmd: list[str], check=True, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def validate_format_device(device: str, pattern: str, expected_bytes: int) -> str | None:
    """Return an error unless a discovered test device is safe to format."""
    if os.path.realpath(device) != device or not re.fullmatch(pattern, device):
        return f"unexpected device path: {device}"

    size = run(["blockdev", "--getsize64", device], check=False)
    if size.returncode != 0:
        return f"cannot read device size: {size.stderr.strip()}"
    try:
        actual_bytes = int(size.stdout.strip())
    except ValueError:
        return f"invalid device size: {size.stdout.strip()}"
    if actual_bytes != expected_bytes:
        return f"device is {actual_bytes} bytes, expected {expected_bytes}"

    topology = run(["lsblk", "-nrpo", "NAME,MOUNTPOINTS", device], check=False)
    if topology.returncode != 0:
        return f"cannot inspect device mounts: {topology.stderr.strip()}"
    for line in topology.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2 and fields[1].strip():
            return f"device or child is mounted: {line.strip()}"

    mounted = run(["findmnt", "-rn", "-S", device], check=False)
    if mounted.returncode == 0 and mounted.stdout.strip():
        return f"device is already mounted: {mounted.stdout.strip()}"
    if mounted.returncode not in (0, 1):
        return f"cannot inspect device mount: {mounted.stderr.strip()}"

    holders = f"/sys/class/block/{os.path.basename(device)}/holders"
    try:
        if os.path.isdir(holders) and os.listdir(holders):
            return f"device has active holders: {', '.join(os.listdir(holders))}"
    except OSError as e:
        return f"cannot inspect device holders: {e}"
    return None


def cleanup_mount(path: str, was_mounted: bool) -> str | None:
    """Unmount and remove one runner-owned mountpoint without following links."""
    if not path.startswith("/tmp/nasty-test-") or os.path.islink(path):
        return f"refusing unsafe mountpoint: {path}"
    errors = []
    if was_mounted:
        result = run(["umount", path], check=False)
        if result.returncode != 0:
            errors.append(f"umount: {result.stderr.strip()}")
    if os.path.isdir(path):
        try:
            os.rmdir(path)
        except OSError as e:
            errors.append(f"rmdir: {e}")
    return "; ".join(errors) or None
