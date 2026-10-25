#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Change only NOID CPU thread limits in the user unit, under the updater lock."""
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path


UNIT = "noid-cpu-miner.service"
DROPIN = Path.home() / ".config/systemd/user/noid-cpu-miner.service.d/50-threads.conf"
LOCK = Path.home() / ".local/state/noid-cpu/manage.lock"
BASE_AFFINITY = ()  # Use available CPUs in order; no machine-specific affinity.


def systemctl(*args):
    result = subprocess.run(("systemctl", "--user", *args), text=True,
                            capture_output=True, timeout=25, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "systemctl failed").strip()[:240])
    return result.stdout.strip()


def unit_state(verb):
    result = subprocess.run(("systemctl", "--user", verb, UNIT), text=True,
                            capture_output=True, timeout=25, check=False)
    value = result.stdout.strip()
    if value not in {"active", "inactive", "failed", "enabled", "disabled"}:
        raise RuntimeError((result.stderr or value or "服务状态不可读").strip()[:240])
    return value


def online_cpus():
    return sorted(os.sched_getaffinity(0))


def choose_affinity(threads):
    available = online_cpus()
    ordered = [cpu for cpu in BASE_AFFINITY if cpu in available]
    ordered += [cpu for cpu in available if cpu not in ordered]
    if len(ordered) < threads:
        raise ValueError(f"仅有 {len(ordered)} 个可用逻辑 CPU")
    return ordered[:threads]


def current_threads():
    environment = systemctl("show", "-p", "Environment", "--value", UNIT)
    match = re.search(r"(?:^|\s)RAYON_NUM_THREADS=(\d+)(?:\s|$)", environment)
    return int(match.group(1)) if match else None


def status():
    return {"service": unit_state("is-active"),
            "enabled": unit_state("is-enabled"),
            "pid": int(systemctl("show", "-p", "MainPID", "--value", UNIT) or "0"),
            "threads": current_threads(), "max_threads": min(12, len(online_cpus()))}


def atomic_write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        temp.write_text(content, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def set_threads(value):
    if not re.fullmatch(r"(?:[1-9]|1[0-2])", value):
        raise ValueError("线程数只允许 1–12 的整数")
    threads = int(value)
    affinity = choose_affinity(threads)
    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        before = status()
        if before["service"] not in {"active", "inactive", "failed"}:
            raise RuntimeError("NOID 服务状态不确定，拒绝修改")
        old = DROPIN.read_text(encoding="utf-8") if DROPIN.exists() else None
        if before["threads"] == threads and old is not None:
            return before
        content = ("[Service]\n"
                   f"Environment=RAYON_NUM_THREADS={threads} OMP_NUM_THREADS={threads}\n"
                   f"CPUQuota={threads * 100}%\n"
                   "CPUAffinity=\n"
                   "CPUAffinity=" + " ".join(map(str, affinity)) + "\n")
        try:
            atomic_write(DROPIN, content)
            systemctl("daemon-reload")
            if before["service"] == "active":
                systemctl("restart", UNIT)
            after = status()
            if (after["threads"] != threads or
                    (before["service"] == "active" and after["service"] != "active") or
                    (before["service"] != "active" and after["service"] == "active") or
                    after["enabled"] != before["enabled"]):
                raise RuntimeError("应用后状态不符")
            return after
        except Exception:
            if old is None:
                DROPIN.unlink(missing_ok=True)
            else:
                atomic_write(DROPIN, old)
            systemctl("daemon-reload")
            if before["service"] == "active":
                systemctl("restart", UNIT)
            raise


def main():
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "status":
            result = status()
        elif len(sys.argv) == 3 and sys.argv[1] == "set-threads":
            result = set_threads(sys.argv[2])
        else:
            raise ValueError("用法: noid-cpu-control status | set-threads N")
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)[:240]}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
