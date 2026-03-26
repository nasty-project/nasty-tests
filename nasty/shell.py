import shutil
import subprocess


def run(cmd: list[str], check=True, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None
