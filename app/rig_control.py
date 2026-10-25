# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Fail-closed, per-GPU user-systemd miner control.

Configuration is local JSON, never supplied by a dashboard request::

  {"gpus": {"GPU-...": {"profiles": {"PRL": "prl-gpu0.service"}}},
   "managed_services": {"prl-gpu0.service": ["GPU-..."]},
   "miners": {}}

Every managed service must appear in at least one profile, and its complete GPU
set must be declared. Update adapters are optional; see ``_update_config``.
This module does not create systemd units or edit miner/wallet configuration.
"""

from __future__ import annotations

import contextlib
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile


_UNIT = re.compile(r"[A-Za-z0-9_.@-]+\.service\Z")
_UUID = re.compile(r"GPU-[A-Za-z0-9-]+\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
_INACTIVE = {"inactive", "failed"}
_MAX_ARCHIVE = 256 * 1024 * 1024
_MAX_EXPANDED = 512 * 1024 * 1024
_QTC_TERMS = "https://www.quanpool.com/api/terms"
_QTC_DOWNLOAD = "https://download.quanpool.com/quanpool-miner-linux-x86_64"
_NOID_REPO = "getrigeos/INVminer-Release"
_NOID_KEY_FILE = str(Path.home() / ".local/state/noid-cpu/trusted-invminer-2026.pem")
_NOID_KEY_SHA256 = "8ed5fdabb5a77b7ecc0c11e09151ac11a60c863959f95e56d45e1fea2ef2c156"  # gitleaks:allow -- public verification-key SHA256, not a credential


class RigError(RuntimeError):
    pass


def _run(argv, timeout=20):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


class HostProbe:
    """Read GPU UUIDs and compute PIDs. Unattributed processes remain unknown."""

    def __init__(self, runner=_run):
        self.runner = runner

    def gpus(self):
        result = self.runner(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"], timeout=10)
        if result.returncode:
            raise RigError("GPU inventory unavailable")
        return set(result.stdout.split())

    def gpu_indices(self):
        result = self.runner(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], timeout=10)
        if result.returncode:
            raise RigError("GPU index inventory unavailable")
        indices = {}
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2 or not parts[0].isdigit() or not _UUID.fullmatch(parts[1]):
                raise RigError("Malformed GPU index inventory")
            indices[parts[1]] = int(parts[0])
        return indices

    @staticmethod
    def _cgroup_owners(cgroup):
        owners = set()
        for line in cgroup.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            services = [segment for segment in parts[2].split("/")
                        if _UNIT.fullmatch(segment) and not re.fullmatch(r"user@\d+\.service", segment)]
            if services:
                owners.add(services[-1])
        return owners or {"unknown"}

    def compute(self):
        result = self.runner(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"], timeout=10)
        if result.returncode:
            raise RigError("GPU compute inventory unavailable")
        found = {}
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2 or not _UUID.fullmatch(parts[0]) or not parts[1].isdigit():
                raise RigError("Malformed GPU compute inventory")
            pid = parts[1]
            try:
                cgroup = Path("/proc", pid, "cgroup").read_text(encoding="utf-8")
            except OSError:
                cgroup = ""
            found.setdefault(parts[0], set()).update(self._cgroup_owners(cgroup))
        return found


class RigController:
    def __init__(self, config_path, state_dir, runner=None, probe=None, fetcher=None):
        self.config_path = Path(config_path)
        self.state_dir = Path(state_dir)
        self.runner = runner or _run
        self.probe = probe or HostProbe(self.runner)
        self.fetcher = fetcher or self._fetch
        self.config_error = None
        self.gpus = {}
        self.services = {}
        self.cpu_services = set()
        self.miners = {}
        try:
            self._configure()
        except (RigError, ValueError, TypeError) as exc:
            self.config_error = str(exc)
            self.gpus = {}
            self.services = {}
            self.cpu_services = set()
            self.miners = {}

    def _configure(self):
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RigError("Rig configuration unavailable or invalid") from exc
        if not isinstance(config, dict) or set(config) - {"gpus", "managed_services", "miners", "cpu_services"}:
            raise RigError("Invalid rig configuration keys")
        self.gpus = config.get("gpus")
        self.services = config.get("managed_services")
        self.miners = config.get("miners", {})
        cpu_services = config.get("cpu_services", [])
        if (not isinstance(cpu_services, list) or any(not isinstance(unit, str) or not _UNIT.fullmatch(unit) for unit in cpu_services)
                or len(cpu_services) != len(set(cpu_services))):
            raise RigError("Invalid CPU service inventory")
        self.cpu_services = set(cpu_services)
        if not isinstance(self.gpus, dict) or not self.gpus or not isinstance(self.services, dict) or not isinstance(self.miners, dict):
            raise RigError("Incomplete rig configuration")
        referenced = set()
        declared_indices = []
        for gpu, item in self.gpus.items():
            if not _UUID.fullmatch(gpu) or not isinstance(item, dict) or set(item) - {"profiles", "label", "index"} or "profiles" not in item:
                raise RigError("Invalid GPU profile")
            if "label" in item and (not isinstance(item["label"], str) or len(item["label"]) > 40):
                raise RigError("Invalid GPU label")
            if "index" in item:
                if type(item["index"]) is not int or item["index"] < 0:
                    raise RigError("Invalid GPU index")
                declared_indices.append(item["index"])
            profiles = item["profiles"]
            if not isinstance(profiles, dict) or not profiles:
                raise RigError("Missing GPU profiles")
            for symbol, unit in profiles.items():
                if not _NAME.fullmatch(symbol) or symbol == "STOP" or not isinstance(unit, str) or not _UNIT.fullmatch(unit):
                    raise RigError("Invalid profile mapping")
                referenced.add(unit)
        if len(declared_indices) != len(set(declared_indices)):
            raise RigError("Duplicate declared GPU index")
        if not referenced <= set(self.services):
            raise RigError("Profile service missing from managed inventory")
        for unit, cards in self.services.items():
            if (not _UNIT.fullmatch(unit) or not isinstance(cards, list) or
                    (not cards and unit not in self.cpu_services) or
                    (cards and unit in self.cpu_services) or
                    len(cards) != len(set(cards)) or not set(cards) <= set(self.gpus)):
                raise RigError("Invalid service GPU set")
            if unit in referenced:
                for gpu in cards:
                    if unit not in self.gpus[gpu]["profiles"].values():
                        raise RigError("Profile service GPU set disagrees with profiles")
        for gpu, item in self.gpus.items():
            for unit in item["profiles"].values():
                if gpu not in self.services[unit]:
                    raise RigError("Profile GPU set disagrees with service")
        if not self.cpu_services <= set(self.services):
            raise RigError("CPU service missing from managed inventory")

    def _require_config(self):
        if self.config_error:
            raise RigError(self.config_error)

    @property
    def expected_indices(self):
        if self.config_error:
            return None
        if all("index" in item for item in self.gpus.values()):
            return tuple(sorted(item["index"] for item in self.gpus.values()))
        try:
            indices = self.probe.gpu_indices() if hasattr(self.probe, "gpu_indices") else {}
        except (OSError, RuntimeError, subprocess.SubprocessError):
            indices = {}
        found = tuple(sorted(self.gpus[gpu].get("index", indices.get(gpu)) for gpu in self.gpus
                             if self.gpus[gpu].get("index", indices.get(gpu)) is not None))
        return found or None

    @contextlib.contextmanager
    def _locked(self, external_locks=()):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state_dir / "rig.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with contextlib.ExitStack() as stack:
                    for path in sorted(external_locks):
                        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                        handle = stack.enter_context(os.fdopen(fd, "a+"))
                        fcntl.flock(handle, fcntl.LOCK_EX)
                    yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _atomic_json(self, name, value):
        fd, temp = tempfile.mkstemp(prefix=".rig-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.state_dir / name)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @staticmethod
    def _replace_link(link, target):
        temporary = Path(tempfile.mkdtemp(prefix=".rig-link-", dir=link.parent))
        try:
            candidate = temporary / "candidate"
            candidate.symlink_to(target)
            os.replace(candidate, link)
        finally:
            shutil.rmtree(temporary)

    def _command(self, *args):
        try:
            result = self.runner(["systemctl", "--user", *args], timeout=25)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RigError("systemd operation unavailable") from exc
        if result.returncode:
            raise RigError("systemd operation failed: " + " ".join(args[:2]))
        return result.stdout.strip()

    def _status(self, unit):
        try:
            result = self.runner(["systemctl", "--user", "is-active", unit], timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RigError("systemd status unavailable") from exc
        status = result.stdout.strip()
        if status not in {"active", "inactive", "failed", "activating", "deactivating"}:
            raise RigError("Unrecognized service state")
        return status

    def _enabled(self, unit):
        try:
            result = self.runner(["systemctl", "--user", "is-enabled", unit], timeout=10)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        state = result.stdout.strip()
        return state if state in {"enabled", "enabled-runtime", "disabled", "static", "indirect",
                                  "linked", "linked-runtime", "masked", "masked-runtime", "generated",
                                  "transient", "alias", "bad", "not-found"} else "unknown"

    def _profile_boot(self, unit):
        state = self._enabled(unit)
        if state not in {"enabled", "disabled"}:
            raise RigError("Profile service cannot be safely enabled for boot")
        return state

    def _static_unlinked(self, unit):
        try:
            result = self.runner(["systemctl", "--user", "show", unit,
                                  "-p", "WantedBy", "-p", "RequiredBy", "-p", "TriggeredBy", "--value"], timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RigError("Static service boot links unavailable") from exc
        if result.returncode != 0 or result.stdout.strip():
            raise RigError("Static service has boot or trigger links")

    def _wait(self, unit, wanted, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._status(unit) in (wanted if isinstance(wanted, set) else {wanted}):
                return
            time.sleep(0.2)
        raise RigError("Service transition timed out")

    def _read_local(self):
        self._require_config()
        present = self.probe.gpus()
        compute = self.probe.compute()
        states = {unit: self._status(unit) for unit in self.services}
        return present, compute, states

    def snapshot(self):
        if self.config_error:
            return {"configured": False, "error": self.config_error, "gpus": {}, "updates": {}, "expected_indices": {}}
        present, compute, states = self._read_local()
        try:
            indices = self.probe.gpu_indices() if hasattr(self.probe, "gpu_indices") else {}
        except (OSError, RuntimeError, subprocess.SubprocessError):
            indices = {}
        boot = {unit: self._enabled(unit) for unit in self.services}
        try:
            desired = json.loads((self.state_dir / "desired.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            desired = {}
        if not isinstance(desired, dict):
            desired = {}
        try:
            updates = json.loads((self.state_dir / "updates.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            updates = {}
        if not isinstance(updates, dict):
            updates = {}
        for miner_id in self.miners:
            row = updates.get(miner_id)
            row = dict(row) if isinstance(row, dict) else {"status": "unavailable"}
            try:
                item = self._update_config(miner_id)
                link = Path(item["current_link"])
                current = link.resolve(strict=True) if link.is_symlink() else None
                row["current_version"] = current.name if current and current.parent == Path(item["versions_dir"]).resolve() else None
            except (RigError, OSError):
                row["current_version"] = None
            updates[miner_id] = row
        return {"configured": True, "expected_indices": {gpu: self.gpus[gpu].get("index", indices.get(gpu)) for gpu in self.gpus},
                "missing_gpus": sorted(set(self.gpus) - present),
                "gpus": {gpu: {"present": gpu in present, "index": indices.get(gpu),
                               "expected_index": self.gpus[gpu].get("index"),
                               "label": self.gpus[gpu].get("label", gpu),
                               "profiles": sorted(self.gpus[gpu]["profiles"]),
                               "active_symbols": sorted(symbol for symbol, unit in self.gpus[gpu]["profiles"].items() if states[unit] == "active"),
                               "desired": desired.get(gpu),
                               "compute_owners": sorted(compute.get(gpu, set())),
                               "boot": {unit: boot[unit] for unit in self.services if gpu in self.services[unit]},
                               "services": {unit: states[unit] for unit in self.services if gpu in self.services[unit]}}
                         for gpu in self.gpus}, "updates": updates}

    def assign(self, gpu_uuid, symbol):
        self._require_config()
        if gpu_uuid not in self.gpus or symbol not in {*self.gpus[gpu_uuid]["profiles"], "STOP"}:
            raise ValueError("Unknown GPU or profile")
        target = None if symbol == "STOP" else self.gpus[gpu_uuid]["profiles"][symbol]
        with self._locked():
            present, compute, states = self._read_local()
            if gpu_uuid not in present:
                raise RigError("GPU is absent")
            related = {unit for unit, cards in self.services.items() if gpu_uuid in cards}
            profile_units = set(self.gpus[gpu_uuid]["profiles"].values())
            if any(len(self.services[unit]) != 1 for unit in profile_units):
                raise RigError("Profile uses a multi-GPU service")
            if any(states[unit] not in {"active", "inactive", "failed"} for unit in related):
                raise RigError("GPU service is transitioning")
            active = {unit for unit in related if states[unit] == "active"}
            if len(active) > 1:
                raise RigError("Multiple miners active on GPU")
            source = next(iter(active), None)
            if source and source not in profile_units:
                raise RigError("Unselectable managed service occupies GPU")
            for unit in related - profile_units:
                boot = self._enabled(unit)
                if boot == "static":
                    self._static_unlinked(unit)
                elif boot not in {"disabled", "masked", "masked-runtime"}:
                    raise RigError("Unselectable managed service may start at boot")
            owners = compute.get(gpu_uuid, set())
            if target is not None and owners - ({source} if source else set()):
                raise RigError("GPU occupied by unknown or unrelated process")
            if source and any(source in owners for gpu, owners in compute.items() if gpu != gpu_uuid):
                raise RigError("Current profile process also occupies another GPU")
            enabled = {unit: self._profile_boot(unit) for unit in profile_units}
            if source == target:
                try:
                    for unit in profile_units:
                        desired_boot = "enabled" if unit == target else "disabled"
                        if self._profile_boot(unit) != desired_boot:
                            self._command("enable" if desired_boot == "enabled" else "disable", unit)
                        if self._profile_boot(unit) != desired_boot:
                            raise RigError("Profile boot state did not change")
                    self._save_desired(gpu_uuid, symbol)
                    return {"status": "already_assigned", "gpu_uuid": gpu_uuid, "symbol": symbol,
                            "mining": bool(target and owners), "pending": not bool(owners) and target is not None,
                            "remaining_compute": bool(owners) if target is None else False}
                except Exception as exc:
                    restored = True
                    for unit, state in enabled.items():
                        try:
                            if self._profile_boot(unit) != state:
                                self._command("enable" if state == "enabled" else "disable", unit)
                        except Exception:
                            restored = False
                    raise RigError("Boot reconciliation failed; rollback " + ("completed" if restored else "incomplete")) from exc
            try:
                if source:
                    self._command("stop", source)
                    self._wait(source, _INACTIVE)
                    after_stop = self.probe.compute()
                    if (target is None and any(source in row for row in after_stop.values())) or (
                            target is not None and after_stop.get(gpu_uuid)):
                        raise RigError("Miner process remains after stop")
                if target:
                    if states[target] == "failed":
                        self._command("stop", target)
                    self._command("start", target)
                    self._wait(target, "active")
                for unit in profile_units:
                    desired = "enabled" if unit == target else "disabled"
                    if self._profile_boot(unit) != desired:
                        self._command("enable" if desired == "enabled" else "disable", unit)
                    if self._profile_boot(unit) != desired:
                        raise RigError("Profile boot state did not change")
                if source and self._status(source) == "active":
                    raise RigError("Source restarted during switch")
                if target and self._status(target) != "active":
                    raise RigError("Target stopped during switch")
                after_compute = self.probe.compute()
                owners = after_compute.get(gpu_uuid, set())
                if target is not None and owners - {target}:
                    raise RigError("Unexpected GPU compute process after switch")
                if target and any(target in other for gpu, other in after_compute.items() if gpu != gpu_uuid):
                    raise RigError("Target miner also occupies another GPU")
                if target is None and source and any(source in other for other in after_compute.values()):
                    raise RigError("Stopped miner still occupies a GPU")
                if any(self._status(unit) != state for unit, state in states.items() if unit not in related):
                    raise RigError("Another GPU service changed during switch")
                self._save_desired(gpu_uuid, symbol)
                return {"status": "assigned", "gpu_uuid": gpu_uuid, "symbol": symbol,
                        "mining": bool(target and owners), "pending": bool(target and not owners),
                        "remaining_compute": bool(owners) if target is None else False}
            except Exception as exc:
                errors = []
                try:
                    if target and self._status(target) == "active":
                        self._command("stop", target)
                        self._wait(target, _INACTIVE)
                except Exception:
                    errors.append("target service stop failed")
                try:
                    if source and self._status(source) != "active":
                        self._command("start", source)
                        self._wait(source, "active")
                except Exception:
                    errors.append("source service restart failed")
                for unit, state in enabled.items():
                    try:
                        if self._profile_boot(unit) != state:
                            self._command("enable" if state == "enabled" else "disable", unit)
                            if self._profile_boot(unit) != state:
                                raise RigError("Boot restoration did not take effect")
                    except Exception:
                        errors.append("boot state restoration failed")
                raise RigError("Assignment failed; rollback " + ("incomplete" if errors else "completed")) from exc

    def _save_desired(self, gpu_uuid, symbol):
        try:
            desired = json.loads((self.state_dir / "desired.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            desired = {}
        if not isinstance(desired, dict):
            desired = {}
        desired[gpu_uuid] = symbol
        self._atomic_json("desired.json", desired)

    @staticmethod
    def _fetch(url, max_bytes):
        asset = bool(re.fullmatch(r"https://api\.github\.com/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/assets/\d+", url))
        if url in {_QTC_TERMS, _QTC_DOWNLOAD}:
            asset = url == _QTC_DOWNLOAD
        elif not re.fullmatch(r"https://api\.github\.com/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/(?:latest|assets/\d+)", url):
            raise RigError("Untrusted release URL")
        accept = "application/octet-stream" if asset else "application/json" if url == _QTC_TERMS else "application/vnd.github+json"
        request = urllib.request.Request(url, headers={"Accept": accept,
                                                       "User-Agent": "rig-control/1"})
        with urllib.request.urlopen(request, timeout=12) as response:
            allowed = ({"www.quanpool.com"} if url == _QTC_TERMS else
                       {"download.quanpool.com"} if url == _QTC_DOWNLOAD else
                       {"api.github.com", "github.com", "release-assets.githubusercontent.com"})
            from urllib.parse import urlsplit
            actual = urlsplit(response.url)
            if actual.scheme != "https" or actual.hostname not in allowed:
                raise RigError("Untrusted download host")
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RigError("Download exceeds limit")
        return data

    def _update_config(self, miner_id):
        self._require_config()
        item = self.miners.get(miner_id)
        provider = item.get("provider", "github") if isinstance(item, dict) else None
        if provider not in {"github", "json_manifest"}:
            raise RigError("Invalid release provider")
        required = {"versions_dir", "current_link", "services", "health_argv"}
        provider_keys = {"repo", "asset_regex"} if provider == "github" else {"provider"}
        if not isinstance(item, dict) or not required | provider_keys <= set(item) or set(item) - required - provider_keys - {"health_exit_codes", "health_output_regex", "asset_format", "binary_name", "provider", "external_locks", "signature_key_file", "signature_key_sha256"}:
            raise RigError("Update adapter unavailable")
        external_locks = item.get("external_locks", [])
        if (not isinstance(external_locks, list) or
                any(not isinstance(path, str) or not Path(path).is_absolute() or
                    Path(path).name in {"", ".", ".."} for path in external_locks) or
                len(external_locks) != len(set(external_locks))):
            raise RigError("Invalid external update locks")
        asset_format = item.get("asset_format", "binary" if provider == "json_manifest" else "tar")
        if asset_format not in {"tar", "zip", "binary"}:
            raise RigError("Invalid release asset format")
        binary_name = item.get("binary_name")
        if asset_format == "binary":
            if not isinstance(binary_name, str) or not _NAME.fullmatch(binary_name) or binary_name in {".", ".."}:
                raise RigError("Invalid release binary name")
        elif binary_name is not None:
            raise RigError("Binary name requires binary asset format")
        if provider == "github":
            repo = item["repo"]
            if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise RigError("Invalid trusted repository")
            if not isinstance(item["asset_regex"], str) or len(item["asset_regex"]) > 160:
                raise RigError("Invalid asset filter")
            re.compile(item["asset_regex"])
        elif asset_format != "binary" or item.get("binary_name") != "quanpool-miner":
            raise RigError("Invalid manifest binary adapter")
        key_file = item.get("signature_key_file")
        key_sha = item.get("signature_key_sha256")
        signed = key_file is not None or key_sha is not None
        if signed:
            if (provider != "github" or not isinstance(key_file, str) or not Path(key_file).is_absolute() or
                    not isinstance(key_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key_sha)):
                raise RigError("Invalid release signature adapter")
        if provider == "github" and item["repo"] == _NOID_REPO:
            if not signed or key_file != _NOID_KEY_FILE or key_sha.lower() != _NOID_KEY_SHA256:
                raise RigError("NOID pinned release signature required")
        units = item["services"]
        if not isinstance(units, list) or not units or len(set(units)) != len(units) or not set(units) <= set(self.services):
            raise RigError("Invalid update service scope")
        if any(set(self.services[unit]) - set(self.gpus) for unit in units):
            raise RigError("Unmanaged GPU in update service")
        versions = Path(item["versions_dir"])
        link = Path(item["current_link"])
        if not versions.is_absolute() or not link.is_absolute() or link.parent != versions.parent or versions.is_symlink():
            raise RigError("Invalid update installation paths")
        argv = item["health_argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise RigError("Invalid health command")
        codes = item.get("health_exit_codes", [0])
        if not isinstance(codes, list) or not codes or any(type(code) is not int or code not in {0, 1} for code in codes):
            raise RigError("Invalid health exit codes")
        pattern = item.get("health_output_regex")
        if pattern is not None:
            if not isinstance(pattern, str) or "{version}" not in pattern or len(pattern) > 160:
                raise RigError("Health output must include release version")
            re.compile(pattern.replace("{version}", "0.0.0"))
        if 1 in codes and not pattern:
            raise RigError("Nonzero health exit requires version output check")
        return item

    def _manifest_release(self):
        try:
            terms = json.loads(self.fetcher(_QTC_TERMS, 2 * 1024 * 1024))
            version = terms["miner_version"]
            digest = terms["miner_sha256"]["linux-x86_64"]
            download = terms["pool_miner_download"]["linux"]
        except (ValueError, TypeError, KeyError) as exc:
            raise RigError("Official miner manifest unavailable") from exc
        if (not isinstance(version, str) or not re.fullmatch(r"v?\d+(?:\.\d+)+", version) or
                not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or
                download != _QTC_DOWNLOAD):
            raise RigError("Official miner manifest invalid")
        return version, digest.lower()

    def _verify_release_signature(self, item, archive, cached):
        key = Path(item["signature_key_file"])
        if key.is_symlink() or not key.is_file():
            raise RigError("Pinned release public key unavailable")
        try:
            key_bytes = key.read_bytes()
        except OSError as exc:
            raise RigError("Pinned release public key unavailable") from exc
        if hashlib.sha256(key_bytes).hexdigest() != item["signature_key_sha256"].lower():
            raise RigError("Pinned release public key digest mismatch")
        metadata = cached.get("signature")
        if (not isinstance(metadata, dict) or type(metadata.get("asset_id")) is not int or
                type(metadata.get("size")) is not int or not 0 < metadata["size"] <= 16384 or
                not isinstance(metadata.get("sha256"), str) or
                not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])):
            raise RigError("Checked release signature unavailable")
        signature_url = ("https://api.github.com/repos/" + item["repo"] +
                         "/releases/assets/" + str(metadata["asset_id"]))
        signature = self.fetcher(signature_url, 16384)
        if (len(signature) != metadata["size"] or
                hashlib.sha256(signature).hexdigest() != metadata["sha256"]):
            raise RigError("Release signature asset digest mismatch")
        with tempfile.TemporaryDirectory(prefix=".rig-signature-", dir=self.state_dir) as temporary:
            root = Path(temporary)
            archive_path = root / "archive"
            signature_path = root / "archive.sig"
            archive_path.write_bytes(archive)
            signature_path.write_bytes(signature)
            try:
                result = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(key),
                                         "-signature", str(signature_path), str(archive_path)],
                                        capture_output=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RigError("Release signature verification unavailable") from exc
            if result.returncode != 0:
                raise RigError("Release signature verification failed")

    def _noid_cpu_units(self, item):
        if item.get("repo") != _NOID_REPO:
            return ()
        units = tuple(sorted(set(item["services"]) & self.cpu_services))
        if len(units) > 1:
            raise RigError("NOID CPU update requires one managed CPU service")
        return units

    def _noid_cpu_policy(self, old, staging, tag):
        """Preserve the former NOID CPU updater's candidate policy before stop."""
        if old is None or not (old / "invminer").is_file() or not (staging / "invminer").is_file():
            raise RigError("NOID CPU build-info binary unavailable")

        def build_info(binary):
            result = self.runner([str(binary), "--build-info"], timeout=20)
            if result.returncode:
                raise RigError("NOID CPU build-info failed")
            try:
                value = json.loads(result.stdout)
            except (TypeError, ValueError) as exc:
                raise RigError("NOID CPU build-info invalid") from exc
            if not isinstance(value, dict):
                raise RigError("NOID CPU build-info invalid")
            return value

        current = build_info(old / "invminer")
        candidate = build_info(staging / "invminer")
        help_result = self.runner([str(staging / "invminer"), "--help"], timeout=20)
        flags = ("--cpu-only", "--coin", "--api-port", "--state-dir", "--no-color")
        if help_result.returncode or any(flag not in (help_result.stdout or "") for flag in flags):
            raise RigError("NOID candidate lacks required CPU options")
        if candidate.get("version") != tag.removeprefix("v") or candidate.get("release_channel") != "public":
            raise RigError("NOID candidate is not the expected public build")
        old_coins, new_coins = current.get("coins"), candidate.get("coins")
        old_fees, new_fees = current.get("fee_assets"), candidate.get("fee_assets")
        if (not isinstance(old_coins, list) or not isinstance(new_coins, list) or
                not isinstance(old_fees, dict) or not isinstance(new_fees, dict)):
            raise RigError("NOID developer fee policy unavailable")
        old_noid = next((coin for coin in old_coins if isinstance(coin, dict) and coin.get("id") == "noid"), None)
        new_noid = next((coin for coin in new_coins if isinstance(coin, dict) and coin.get("id") == "noid"), None)
        fee_fields = ("release_mode", "commercial_default_ppm", "user_minimum_ppm",
                      "unconfigured_coin_default_ppm")
        old_configured = old_fees.get("configured_coin_ppm")
        new_configured = new_fees.get("configured_coin_ppm")
        if (not old_noid or not new_noid or old_noid.get("fee_ppm") != new_noid.get("fee_ppm") or
                any(old_fees.get(field) != new_fees.get(field) for field in fee_fields) or
                not isinstance(old_configured, dict) or not isinstance(new_configured, dict) or
                old_configured.get("noid") != new_configured.get("noid")):
            raise RigError("NOID developer fee policy changed")
        test = self.runner([str(staging / "invminer"), "--coin", "noid", "--cpu-only", "--self-test"], timeout=40)
        if test.returncode or "CPU_SELF_TEST_OK" not in (test.stdout or "") + (test.stderr or ""):
            raise RigError("NOID candidate CPU self-test failed")

    def _noid_cpu_mainpid(self, unit):
        result = self.runner(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], timeout=10)
        value = (result.stdout or "").strip()
        if result.returncode or not value.isdigit() or int(value) <= 0:
            raise RigError("NOID CPU MainPID unavailable")
        return int(value)

    def _noid_cpu_ready(self, unit, since):
        pid = self._noid_cpu_mainpid(unit)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self._status(unit) != "active" or self._noid_cpu_mainpid(unit) != pid:
                raise RigError("NOID CPU service or MainPID changed during readiness")
            result = self.runner(["journalctl", "--user", "-u", unit, f"_PID={pid}",
                                  "--since", since, "-o", "cat", "--no-pager", "-n", "120"], timeout=10)
            lines = (result.stdout or "").splitlines()
            if (result.returncode == 0 and
                    any("CPU_READY" in line and "mode=cpu_only" in line for line in lines) and
                    any("CPU_SELF_TEST_OK" in line for line in lines)):
                return
            time.sleep(1)
        raise RigError("NOID CPU did not reach CPU_READY")

    @staticmethod
    def _extract_asset(asset, staging, item):
        """Expand only regular files and directories, before touching services."""
        asset_format = item.get("asset_format", "binary" if item.get("provider") == "json_manifest" else "tar")
        if asset_format == "binary":
            if not asset or len(asset) > _MAX_EXPANDED:
                raise RigError("Invalid release binary")
            target = staging / item["binary_name"]
            target.write_bytes(asset)
            target.chmod(0o755)
            return
        try:
            if asset_format == "tar":
                with tarfile.open(fileobj=io.BytesIO(asset), mode="r:*") as archive:
                    members = archive.getmembers()
                    entries = [(m.name, m.size, m.isdir(), m.isfile(), bool(m.mode & 0o111), m) for m in members]
                    RigController._extract_entries(staging, entries, archive.extractfile)
            else:
                with zipfile.ZipFile(io.BytesIO(asset)) as archive:
                    members = archive.infolist()
                    entries = []
                    for member in members:
                        unix_mode = member.external_attr >> 16
                        kind = unix_mode & 0o170000
                        regular = kind in {0, 0o100000} and not member.is_dir()
                        directory = member.is_dir() and kind in {0, 0o040000}
                        entries.append((member.filename, member.file_size, directory, regular,
                                        bool(unix_mode & 0o111), member))
                    RigController._extract_entries(staging, entries, archive.open)
        except RigError:
            raise
        except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, RuntimeError, ValueError) as exc:
            raise RigError("Invalid or unsafe release archive") from exc

    @staticmethod
    def _extract_entries(staging, entries, opener):
        total = 0
        seen = {}
        for name, size, directory, regular, executable, member in entries:
            if name in {".", "./"} and directory:
                continue
            while name.startswith("./"):
                name = name[2:]
            path = PurePosixPath(name)
            parts = path.parts
            if (not name or "\\" in name or path.is_absolute() or
                    any(part in {"", ".", ".."} for part in name.rstrip("/").split("/")) or
                    not (directory or regular) or size < 0 or any(part in {"", "."} for part in parts)):
                raise RigError("Unsafe release archive")
            key = tuple(parts)
            if key in seen or any(key[:index] in seen and seen[key[:index]] == "file"
                                  for index in range(1, len(key))):
                raise RigError("Unsafe release archive")
            seen[key] = "dir" if directory else "file"
            total += size
            if total > _MAX_EXPANDED:
                raise RigError("Expanded release exceeds limit")
        for name, size, directory, regular, executable, member in entries:
            if name in {".", "./"} and directory:
                continue
            while name.startswith("./"):
                name = name[2:]
            target = staging.joinpath(*PurePosixPath(name).parts)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with opener(member) as src, target.open("xb") as dst:
                    copied = 0
                    while chunk := src.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > size or copied > _MAX_EXPANDED:
                            raise RigError("Expanded release exceeds limit")
                        dst.write(chunk)
                    if copied != size:
                        raise RigError("Release file size mismatch")
                target.chmod(0o755 if executable else 0o644)

    def check_updates(self):
        """Explicit network check; safe to call from a timer, never from snapshot."""
        self._require_config()
        result = {}
        for miner_id in self.miners:
            try:
                item = self._update_config(miner_id)
                provider = item.get("provider", "github")
                if provider == "json_manifest":
                    tag, digest = self._manifest_release()
                    release_id, asset_id, size = tag, tag, None
                    signature = None
                else:
                    url = "https://api.github.com/repos/" + item["repo"] + "/releases/latest"
                    release = json.loads(self.fetcher(url, 2 * 1024 * 1024))
                    if release.get("draft") or release.get("prerelease"):
                        raise RigError("Release is not stable")
                    release_id = release.get("id")
                    tag = release.get("tag_name")
                    if not isinstance(release_id, int) or not isinstance(tag, str) or not _NAME.fullmatch(tag):
                        raise RigError("Invalid release identity")
                    assets = [a for a in release.get("assets", []) if re.fullmatch(item["asset_regex"], a.get("name", ""))]
                    if len(assets) != 1:
                        raise RigError("Release asset unavailable or ambiguous")
                    asset = assets[0]
                    digest = asset.get("digest", "")
                    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                        raise RigError("Publisher SHA256 digest unavailable")
                    digest = digest[7:]
                    size = asset.get("size")
                    asset_id = asset.get("id")
                    if not isinstance(size, int) or not 0 < size <= _MAX_ARCHIVE or not isinstance(asset_id, int):
                        raise RigError("Invalid release asset size or id")
                    signature = None
                    if item.get("signature_key_file"):
                        signatures = [a for a in release.get("assets", [])
                                      if a.get("name") == asset["name"] + ".sig"]
                        if len(signatures) != 1:
                            raise RigError("Publisher release signature unavailable")
                        sig = signatures[0]
                        sig_digest = sig.get("digest", "")
                        sig_size = sig.get("size")
                        sig_id = sig.get("id")
                        if (not isinstance(sig_digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", sig_digest) or
                                type(sig_size) is not int or not 0 < sig_size <= 16384 or type(sig_id) is not int):
                            raise RigError("Publisher release signature metadata invalid")
                        signature = {"asset_id": sig_id, "sha256": sig_digest[7:].lower(), "size": sig_size}
                link = Path(item["current_link"])
                versions = Path(item["versions_dir"]).resolve()
                if not link.is_symlink():
                    raise RigError("Current release symlink unavailable")
                current = link.resolve(strict=True)
                if current.parent != versions:
                    raise RigError("Current release escapes version directory")
                same_version = current.name == tag
                if current.name != tag:
                    if (not re.fullmatch(r"v?\d+(?:\.\d+)+", current.name) or
                            not re.fullmatch(r"v?\d+(?:\.\d+)+", tag)):
                        raise RigError("Release version is not newer")
                    current_tuple = tuple(map(int, re.findall(r"\d+", current.name)))
                    tag_tuple = tuple(map(int, re.findall(r"\d+", tag)))
                    same_version = current_tuple == tag_tuple
                    if tag_tuple < current_tuple:
                        raise RigError("Release version is not newer")
                result[miner_id] = {"status": "up_to_date" if same_version else "available",
                                    "current_version": current.name, "release_id": release_id, "tag": tag,
                                    "asset_id": asset_id, "sha256": digest.lower(), "size": size,
                                    "provider": provider,
                                    "signature": signature,
                                    "checked_at": int(time.time())}
            except Exception as exc:
                reason = str(exc) if isinstance(exc, RigError) else "Release check or network request failed"
                result[miner_id] = {"status": "unavailable", "error": reason[:160], "checked_at": int(time.time())}
        with self._locked():
            for miner_id, row in result.items():
                if row.get("status") not in {"available", "up_to_date"}:
                    continue
                try:
                    item = self._update_config(miner_id)
                    versions = Path(item["versions_dir"]).resolve()
                    link = Path(item["current_link"])
                    current = link.resolve(strict=True) if link.is_symlink() else None
                    if current is None or current.parent != versions:
                        raise RigError("Current release symlink unavailable")
                    row["current_version"] = current.name
                    tag = row["tag"]
                    if current.name == tag:
                        row["status"] = "up_to_date"
                    elif (re.fullmatch(r"v?\d+(?:\.\d+)+", current.name) and
                          re.fullmatch(r"v?\d+(?:\.\d+)+", tag)):
                        current_tuple = tuple(map(int, re.findall(r"\d+", current.name)))
                        tag_tuple = tuple(map(int, re.findall(r"\d+", tag)))
                        if current_tuple == tag_tuple:
                            row["status"] = "up_to_date"
                        elif current_tuple > tag_tuple:
                            raise RigError("Release version is not newer")
                        else:
                            row["status"] = "available"
                    else:
                        raise RigError("Release version is not newer")
                except (RigError, OSError, ValueError) as exc:
                    row.clear()
                    row.update({"status": "unavailable", "error": str(exc)[:160], "checked_at": int(time.time())})
            self._atomic_json("updates.json", result)
        return result

    def apply_update(self, miner_id, release_id):
        """Install a cached, digest-pinned release. No arbitrary URL, path or command input."""
        self._require_config()
        item = self._update_config(miner_id)
        with self._locked(item.get("external_locks", [])):
            try:
                cached_all = json.loads((self.state_dir / "updates.json").read_text(encoding="utf-8"))
                cached = cached_all[miner_id]
            except (OSError, ValueError, KeyError) as exc:
                raise RigError("No checked release") from exc
            if cached.get("status") != "available" or cached.get("release_id") != release_id:
                raise RigError("Release id was not checked")
            if time.time() - cached["checked_at"] > 86400:
                raise RigError("Release check expired")
            versions = Path(item["versions_dir"]).resolve()
            link = Path(item["current_link"])
            old = link.resolve(strict=True) if link.is_symlink() else None
            if old and (old.parent != versions or not _NAME.fullmatch(old.name)):
                raise RigError("Current release link escapes version directory")
            tag = cached["tag"]
            if old and old.name == tag:
                return {"status": "already_current", "tag": tag}
            # Numeric release tags are comparable; all other tags require explicit
            # current-version order in the adapter, which is intentionally absent.
            if old:
                if (not re.fullmatch(r"v?\d+(?:\.\d+)+", old.name) or
                        not re.fullmatch(r"v?\d+(?:\.\d+)+", tag)):
                    raise RigError("Incomparable version refused")
                old_tuple = tuple(map(int, re.findall(r"\d+", old.name)))
                tag_tuple = tuple(map(int, re.findall(r"\d+", tag)))
                if tag_tuple == old_tuple:
                    return {"status": "already_current", "tag": tag}
                if tag_tuple < old_tuple:
                    raise RigError("Downgrade refused")
            provider = item.get("provider", "github")
            if cached.get("provider", "github") != provider:
                raise RigError("Release provider changed")
            if provider == "json_manifest":
                current_tag, current_digest = self._manifest_release()
                if current_tag != cached["tag"] or current_digest != cached["sha256"]:
                    raise RigError("Official miner manifest changed; check again")
                url = _QTC_DOWNLOAD
            else:
                url = "https://api.github.com/repos/" + item["repo"] + "/releases/assets/" + str(cached["asset_id"])
            archive = self.fetcher(url, _MAX_ARCHIVE)
            if (not archive or len(archive) > _MAX_ARCHIVE or
                    (cached["size"] is not None and len(archive) != cached["size"]) or
                    hashlib.sha256(archive).hexdigest() != cached["sha256"]):
                raise RigError("Release asset digest mismatch")
            if item.get("signature_key_file"):
                self._verify_release_signature(item, archive, cached)
            versions.mkdir(parents=True, exist_ok=True)
            destination = versions / tag
            if destination.exists() or destination.is_symlink():
                raise RigError("Release destination already exists")
            staging = Path(tempfile.mkdtemp(prefix=".rig-update-", dir=versions))
            try:
                self._extract_asset(archive, staging, item)
                health = item["health_argv"]
                # Health command must reside in the extracted tree; no config path
                # and no command from the UI can be injected.
                if not health[0].startswith("./") or ".." in PurePosixPath(health[0]).parts:
                    raise RigError("Health command must be relative to release")
                health_path = staging / health[0][2:]
                if not health_path.is_file() or health_path.is_symlink() or not os.access(health_path, os.X_OK):
                    raise RigError("Health binary missing from release")
                statuses = {u: self._status(u) for u in item["services"]}
                enabled = {u: self._enabled(u) for u in item["services"]}
                affected = {gpu for unit in item["services"] if statuses[unit] == "active"
                            for gpu in self.services[unit]}
                before_compute = self.probe.compute()
                if any(before_compute.get(gpu, set()) - set(item["services"]) for gpu in affected):
                    raise RigError("Another process occupies an affected GPU")
                result = self.runner([str(health_path), *health[1:]], timeout=30)
                pattern = item.get("health_output_regex")
                version = tag.removeprefix("v")
                output_ok = not pattern or re.search(pattern.replace("{version}", re.escape(version)),
                                                     (result.stdout or "") + (result.stderr or ""))
                if result.returncode not in item.get("health_exit_codes", [0]) or not output_ok:
                    raise RigError("New miner health command failed")
                noid_cpu_units = self._noid_cpu_units(item)
                if noid_cpu_units:
                    self._noid_cpu_policy(old, staging, tag)
                os.replace(staging, destination)
                switched = False
                try:
                    for unit, state in statuses.items():
                        if state == "active":
                            self._command("stop", unit)
                            self._wait(unit, _INACTIVE)
                    if any(self.probe.compute().get(gpu) for gpu in affected):
                        raise RigError("GPU still computing after miner stop")
                    self._replace_link(link, destination)
                    switched = True
                    for unit, state in statuses.items():
                        if state == "active":
                            since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) if unit in noid_cpu_units else None
                            self._command("start", unit)
                            self._wait(unit, "active")
                            if since is not None:
                                self._noid_cpu_ready(unit, since)
                    if any(self._enabled(u) != state for u, state in enabled.items()):
                        raise RigError("Boot state changed unexpectedly")
                    after_compute = self.probe.compute()
                    for gpu, owners in after_compute.items():
                        if any(unit in owners and gpu not in self.services[unit]
                               for unit in item["services"]):
                            raise RigError("Updated miner occupies an undeclared GPU")
                    pending = []
                    for gpu in affected:
                        allowed = {unit for unit, state in statuses.items()
                                   if state == "active" and gpu in self.services[unit]}
                        owners = after_compute.get(gpu, set())
                        if owners - allowed:
                            raise RigError("Unexpected GPU process after update")
                        if allowed and not owners:
                            pending.append(gpu)
                    cached_all[miner_id] = {**cached, "status": "up_to_date", "current_version": tag}
                    self._atomic_json("updates.json", cached_all)
                    return {"status": "updated", "tag": tag, "pending_gpus": sorted(pending),
                            "restarted_services": sorted(u for u, state in statuses.items() if state == "active"),
                            "kept_stopped": sorted(u for u, state in statuses.items() if state in _INACTIVE)}
                except Exception as exc:
                    restore_errors = []
                    for unit in statuses:
                        try:
                            if self._status(unit) == "active":
                                self._command("stop", unit)
                                self._wait(unit, _INACTIVE)
                        except Exception:
                            restore_errors.append(unit + " stop")
                    if switched:
                        try:
                            if old:
                                self._replace_link(link, old)
                            else:
                                link.unlink(missing_ok=True)
                        except Exception:
                            restore_errors.append("release link")
                    for unit, state in statuses.items():
                        try:
                            if state == "active":
                                since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) if unit in noid_cpu_units else None
                                self._command("start", unit)
                                self._wait(unit, "active")
                                if since is not None:
                                    self._noid_cpu_ready(unit, since)
                        except Exception:
                            restore_errors.append(unit + " restart")
                        try:
                            if self._enabled(unit) != enabled[unit]:
                                if enabled[unit] not in {"enabled", "disabled"}:
                                    raise RigError("Boot state cannot be restored automatically")
                                self._command("enable" if enabled[unit] == "enabled" else "disable", unit)
                        except Exception:
                            restore_errors.append(unit + " boot")
                    try:
                        restored_link = (link.is_symlink() and link.resolve(strict=True) == old) if old else (
                            not link.exists() and not link.is_symlink())
                    except OSError:
                        restored_link = False
                    if not restored_link:
                        restore_errors.append("release link verification")
                    if not restore_errors:
                        try:
                            if destination.is_symlink() or not destination.is_dir():
                                raise RigError("Release destination changed during rollback")
                            shutil.rmtree(destination)
                        except (OSError, RigError):
                            restore_errors.append("release destination cleanup")
                    raise RigError("Update failed; rollback " + ("incomplete" if restore_errors else "completed")) from exc
            finally:
                if staging.exists():
                    shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local rig configuration and release check")
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check-config", action="store_true")
    actions.add_argument("--check-updates", action="store_true")
    args = parser.parse_args(argv)
    try:
        rig = RigController(args.config, args.state_dir)
        rig._require_config()
        output = {"configured": True, "gpus": len(rig.gpus), "managed_services": len(rig.services)} if args.check_config else rig.check_updates()
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (RigError, ValueError) as exc:
        print(json.dumps({"configured": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
