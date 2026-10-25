#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Conservative, unattended INVminer updates for the NOID CPU user service."""
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

REPO = "getrigeos/INVminer-Release"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
KEY_URL = f"https://github.com/{REPO}/releases/download/v0.2.22/invminer-2026.pem"
KEY_SHA256 = "8ed5fdabb5a77b7ecc0c11e09151ac11a60c863959f95e56d45e1fea2ef2c156"  # gitleaks:allow -- public verification-key SHA256, not a credential
ROOT = Path.home() / "miners/noid-cpu"
STATE = Path.home() / ".local/state/noid-cpu"
LOCK = STATE / "manage.lock"
CURRENT = ROOT / "current"
TRANSACTION = STATE / "update-transaction.json"
RESULT = STATE / "update-result.json"
KEY = STATE / "trusted-invminer-2026.pem"
UNIT = "noid-cpu-miner.service"
VERSION = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_ARCHIVE = 80 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024
MAX_METADATA = 128 * 1024


class RolledBack(RuntimeError):
    """The candidate failed, and the previous miner was restored."""


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname or ""
        if urlparse(newurl).scheme != "https" or host not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        }:
            raise RuntimeError("release redirect outside approved GitHub hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SafeRedirect())


def fetch(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "noid-cpu-updater/1"})
    with OPENER.open(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("release download failed")
        content = response.read(limit + 1)
    if len(content) > limit:
        raise RuntimeError("release item exceeds size limit")
    return content


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".noid-update-", delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.chmod(temp, mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def record(**values):
    atomic_bytes(RESULT, json.dumps({"checked_at": int(time.time()), **values}, sort_keys=True).encode())


def run(*args, timeout=30):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout, check=False)


def systemctl(*args):
    result = run("systemctl", "--user", *args)
    if result.returncode:
        raise RuntimeError(f"systemctl {args[0]} failed")
    return result.stdout.strip()


def state(name):
    result = run("systemctl", "--user", name, UNIT)
    value = result.stdout.strip()
    if value not in {"active", "inactive", "failed", "enabled", "disabled"}:
        raise RuntimeError(f"unit {name} returned an unknown state")
    return value


def mainpid():
    return int(systemctl("show", "-p", "MainPID", "--value", UNIT) or "0")


def version_of(link):
    target = link.resolve(strict=True)
    if target.parent != ROOT.resolve() or not VERSION.fullmatch(target.name):
        raise RuntimeError("current points outside a version directory")
    if not (target / "invminer").is_file():
        raise RuntimeError("current miner missing")
    return target.name


def switch(version):
    target = ROOT / version
    if not (target / "invminer").is_file():
        raise RuntimeError("candidate miner missing")
    temp = ROOT / f".current.{os.getpid()}"
    try:
        temp.symlink_to(target)
        os.replace(temp, CURRENT)
    finally:
        temp.unlink(missing_ok=True)


def published_asset(release, name, max_size):
    assets = [a for a in release.get("assets", []) if a.get("name") == name]
    if len(assets) != 1:
        raise RuntimeError(f"release asset missing: {name}")
    asset = assets[0]
    tag = release["tag_name"]
    expected = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
    digest = asset.get("digest", "")
    if (asset.get("browser_download_url") != expected or
            not 0 < asset.get("size", 0) <= max_size or
            not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise RuntimeError(f"invalid release asset metadata: {name}")
    data = fetch(expected, max_size)
    if len(data) != asset["size"] or sha(data) != digest[7:]:
        raise RuntimeError(f"release digest mismatch: {name}")
    return data


def trusted_key():
    if KEY.exists():
        data = KEY.read_bytes()
    else:
        data = fetch(KEY_URL, 4096)
        if sha(data) == KEY_SHA256:
            atomic_bytes(KEY, data)
    if sha(data) != KEY_SHA256:
        raise RuntimeError("pinned release key mismatch")
    return data


def verify_signature(key, body, signature):
    with tempfile.TemporaryDirectory(prefix="noid-signature-") as directory:
        directory = Path(directory)
        (directory / "key.pem").write_bytes(key)
        (directory / "item").write_bytes(body)
        (directory / "item.sig").write_bytes(signature)
        result = run("openssl", "dgst", "-sha256", "-verify", str(directory / "key.pem"),
                     "-signature", str(directory / "item.sig"), str(directory / "item"))
        if result.returncode or result.stdout.strip() != "Verified OK":
            raise RuntimeError("release signature verification failed")


def safe_extract(archive, destination):
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 1000:
            raise RuntimeError("archive has too many members")
        for member in members:
            parts = PurePosixPath(member.name).parts
            if (member.name.startswith("/") or ".." in parts or
                    not (member.isfile() or member.isdir())):
                raise RuntimeError("unsafe archive member")
            total += member.size
            if total > MAX_EXPANDED:
                raise RuntimeError("archive expands beyond size limit")
        for member in members:
            parts = [part for part in PurePosixPath(member.name).parts if part != "."]
            if not parts:
                continue
            path = destination.joinpath(*parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(path, 0o700 if path.name == "invminer" else 0o600)
    if not (destination / "invminer").is_file():
        raise RuntimeError("archive lacks invminer")


def build_info(binary):
    result = run(str(binary), "--build-info", timeout=20)
    if result.returncode:
        raise RuntimeError("build-info failed")
    return json.loads(result.stdout)


def policy(binary, version):
    current = build_info(CURRENT / "invminer")
    candidate = build_info(binary)
    help_result = run(str(binary), "--help", timeout=20)
    if help_result.returncode or any(flag not in help_result.stdout for flag in
                                     ("--cpu-only", "--coin", "--api-port", "--state-dir", "--no-color")):
        raise RuntimeError("candidate lacks required CPU options")
    if candidate.get("version") != version[1:] or candidate.get("release_channel") != "public":
        raise RuntimeError("candidate is not the expected stable build")
    old_noid = next((c for c in current.get("coins", []) if c.get("id") == "noid"), None)
    new_noid = next((c for c in candidate.get("coins", []) if c.get("id") == "noid"), None)
    fee_fields = ("release_mode", "commercial_default_ppm", "user_minimum_ppm",
                  "unconfigured_coin_default_ppm")
    if (not old_noid or not new_noid or
            old_noid.get("fee_ppm") != new_noid.get("fee_ppm") or
            any(current.get("fee_assets", {}).get(f) != candidate.get("fee_assets", {}).get(f)
                for f in fee_fields) or
            current.get("fee_assets", {}).get("configured_coin_ppm", {}).get("noid") !=
            candidate.get("fee_assets", {}).get("configured_coin_ppm", {}).get("noid")):
        raise RuntimeError("NOID developer fee policy changed")
    test = run(str(binary), "--coin", "noid", "--cpu-only", "--self-test", timeout=40)
    if test.returncode or "CPU_SELF_TEST_OK" not in test.stderr + test.stdout:
        raise RuntimeError("candidate CPU self-test failed")


def cpu_ready(pid, version, since):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if state("is-active") == "active" and mainpid() == pid and pid > 0:
            result = run("journalctl", "--user", "-u", UNIT, f"_PID={pid}", "--since", since,
                         "-o", "cat", "--no-pager", "-n", "120")
            lines = result.stdout.splitlines()
            ready = any("CPU_READY" in line and "mode=cpu_only" in line for line in lines)
            tested = any("CPU_SELF_TEST_OK" in line for line in lines)
            if result.returncode == 0 and ready and tested:
                return
        time.sleep(2)
    raise RuntimeError(f"candidate {version} did not reach CPU_READY")


def recover():
    if not TRANSACTION.exists():
        return
    pending = json.loads(TRANSACTION.read_text())
    old = pending["previous"]
    if not VERSION.fullmatch(old) or not (ROOT / old / "invminer").is_file():
        raise RuntimeError("pending rollback target invalid")
    switch(old)
    # A prior timer crash must not revive a service that is now stopped or disabled.
    if state("is-active") == "active" and state("is-enabled") == "enabled":
        systemctl("restart", UNIT)
    TRANSACTION.unlink()
    record(status="recovered", current=old)


def check():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    with LOCK.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        recover()
        installed = version_of(CURRENT)
        release = json.loads(fetch(API, MAX_METADATA))
        tag = release.get("tag_name", "")
        if not VERSION.fullmatch(tag) or release.get("draft") or release.get("prerelease"):
            raise RuntimeError("latest release is not stable semver")
        old_version = tuple(map(int, installed[1:].split(".")))
        new_version = tuple(map(int, tag[1:].split(".")))
        if new_version <= old_version:
            record(status="current", current=installed, latest=tag)
            return
        # Manual stop and disabled state take precedence over the update timer.
        if state("is-active") != "active" or state("is-enabled") != "enabled":
            record(status="deferred-inactive", current=installed, latest=tag)
            return
        name = f"invminer-{tag}-linux-x86_64-cuda13.tar.gz"
        key = trusted_key()
        sums = published_asset(release, "SHA256SUMS.txt", MAX_METADATA)
        sums_sig = published_asset(release, "SHA256SUMS.txt.sig", 4096)
        verify_signature(key, sums, sums_sig)
        archive = published_asset(release, name, MAX_ARCHIVE)
        signature = published_asset(release, name + ".sig", 4096)
        verify_signature(key, archive, signature)
        matches = [line.split() for line in sums.decode("ascii").splitlines()
                   if line.endswith("  " + name)]
        if len(matches) != 1 or matches[0] != [sha(archive), name]:
            raise RuntimeError("signed checksum mismatch")
        candidate = ROOT / tag
        if candidate.exists():
            raise RuntimeError("candidate directory already exists; inspect before retry")
        with tempfile.TemporaryDirectory(prefix=".noid-candidate-", dir=ROOT) as tmp:
            staging = Path(tmp)
            safe_extract(archive, staging)
            policy(staging / "invminer", tag)
            # Recheck state under the shared lock just before stopping the CPU miner.
            if state("is-active") != "active" or state("is-enabled") != "enabled":
                record(status="deferred-inactive", current=installed, latest=tag)
                return
            os.rename(staging, candidate)
            # Keep old release and an on-disk rollback breadcrumb across interruption.
            atomic_bytes(TRANSACTION, json.dumps({"previous": installed, "candidate": tag}).encode())
            try:
                systemctl("stop", UNIT)
                if state("is-enabled") != "enabled":
                    raise RuntimeError("service disabled during update")
                switch(tag)
                since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                if state("is-enabled") != "enabled":
                    raise RuntimeError("service disabled before candidate start")
                systemctl("start", UNIT)
                pid = mainpid()
                cpu_ready(pid, tag, since)
                if state("is-enabled") != "enabled":
                    raise RuntimeError("service disabled during update")
                TRANSACTION.unlink()
                record(status="updated", previous=installed, current=tag,
                       archive_sha256=sha(archive), signature_verified=True,
                       checksum_verified=True, cpu_self_test=True, pid=pid)
            except Exception as error:
                switch(installed)
                if state("is-active") == "active":
                    systemctl("stop", UNIT)
                if state("is-enabled") == "enabled":
                    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    systemctl("start", UNIT)
                    cpu_ready(mainpid(), installed, since)
                TRANSACTION.unlink(missing_ok=True)
                shutil.rmtree(candidate)
                record(status="rolled-back", current=installed, failed=tag,
                       reason=type(error).__name__ + ": " + str(error)[:160])
                raise RolledBack(str(error)) from error


def main():
    try:
        if sys.argv[1:] != ["check"]:
            raise RuntimeError("usage: noid-cpu-update check")
        check()
        print(RESULT.read_text())
    except Exception as error:
        try:
            if not isinstance(error, RolledBack):
                record(status="error", error=type(error).__name__ + ": " + str(error)[:160])
        except Exception:
            pass
        print(f"NOID CPU update failed: {type(error).__name__}: {str(error)[:160]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
