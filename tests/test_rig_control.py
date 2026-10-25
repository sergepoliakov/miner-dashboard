# SPDX-License-Identifier: GPL-3.0-only
import io
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from rig_control import HostProbe, RigController, RigError


class FakeHost:
    def __init__(self, cards, services):
        self.cards = set(cards)
        self.states = {unit: "inactive" for unit in services}
        self.boot = {unit: "disabled" for unit in services}
        self.owners = {}
        self.fail_start = None
        self.static_links = {}
        self.fail_show = False
        self.commands = []

    def gpus(self):
        return set(self.cards)

    def gpu_indices(self):
        return {gpu: index for index, gpu in enumerate(sorted(self.cards))}

    def compute(self):
        return {gpu: set(owners) for gpu, owners in self.owners.items()}

    def run(self, argv, timeout=20):
        self.commands.append(tuple(argv))
        if argv[:2] == ["systemctl", "--user"]:
            if argv[2] == "show":
                return subprocess.CompletedProcess(argv, 1 if self.fail_show else 0,
                                                   self.static_links.get(argv[3], "\n\n\n"), "")
            action, unit = argv[2:]
            if action == "is-active":
                return subprocess.CompletedProcess(argv, 0 if self.states[unit] == "active" else 3, self.states[unit] + "\n", "")
            if action == "is-enabled":
                return subprocess.CompletedProcess(argv, 0 if self.boot[unit] == "enabled" else 1, self.boot[unit] + "\n", "")
            if action == "start" and unit == self.fail_start:
                return subprocess.CompletedProcess(argv, 1, "", "failed")
            if action in {"start", "stop"}:
                self.states[unit] = "active" if action == "start" else "inactive"
                if action == "stop":
                    for owners in self.owners.values():
                        owners.discard(unit)
            elif action in {"enable", "disable"}:
                self.boot[unit] = "enabled" if action == "enable" else "disabled"
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "healthy", "")


class RigControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cards = [f"GPU-{i:04d}" for i in range(6)]
        self.gpus = {gpu: {"label": f"Card {i}", "profiles": {"PRL": f"prl-{i}.service", "TSC": f"tsc-{i}.service"}}
                     for i, gpu in enumerate(self.cards)}
        self.services = {f"{symbol.lower()}-{i}.service": [gpu]
                         for i, gpu in enumerate(self.cards) for symbol in ("PRL", "TSC")}
        self.services["legacy-all.service"] = list(self.cards)
        self.config = {"gpus": self.gpus, "managed_services": self.services, "miners": {}}
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host = FakeHost(self.cards, self.services)

    def rig(self, **kwargs):
        return RigController(self.path, self.root / "state", runner=self.host.run, probe=self.host, **kwargs)

    def test_host_probe_uses_leaf_miner_service_not_user_ancestor(self):
        rows = f"{self.cards[0]}, 101\n{self.cards[1]}, 202\n"
        runner = lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, rows, "")
        miner = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/prl-gpu0-miner.service\n"
        comfy = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-comfyui.scope\n"
        with patch("rig_control.Path.read_text", side_effect=[miner, comfy]):
            owners = HostProbe(runner).compute()
        self.assertEqual(owners[self.cards[0]], {"prl-gpu0-miner.service"})
        self.assertEqual(owners[self.cards[1]], {"unknown"})

    def test_v1_cgroup_uses_deepest_service_per_line(self):
        cgroup = ("8:cpu:/user.slice/user@1000.service/app.slice/prl-gpu0-miner.service\n"
                  "7:memory:/user.slice/user@1000.service/app.slice/prl-gpu0-miner.service\n")
        self.assertEqual(HostProbe._cgroup_owners(cgroup), {"prl-gpu0-miner.service"})
        self.assertEqual(HostProbe._cgroup_owners("0::/user.slice/user@1000.service/app.slice/comfyui.scope\n"),
                         {"unknown"})

    def test_missing_and_incomplete_config_fail_closed(self):
        absent = RigController(self.root / "absent", self.root / "state")
        self.assertFalse(absent.snapshot()["configured"])
        with self.assertRaises(RigError):
            absent.assign(self.cards[0], "PRL")
        self.config["managed_services"].pop("prl-0.service")
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertFalse(self.rig().snapshot()["configured"])

    def test_six_cards_mixed_profiles_and_boot_persistence(self):
        rig = self.rig()
        for i, gpu in enumerate(self.cards):
            result = rig.assign(gpu, "PRL" if i % 2 == 0 else "TSC")
            self.assertTrue(result["pending"])
        view = self.rig().snapshot()
        self.assertTrue(view["configured"])
        self.assertEqual(len(view["gpus"]), 6)
        for i, gpu in enumerate(self.cards):
            selected = "PRL" if i % 2 == 0 else "TSC"
            unit = f"{selected.lower()}-{i}.service"
            self.assertEqual(view["gpus"][gpu]["active_symbols"], [selected])
            self.assertEqual(view["gpus"][gpu]["desired"], selected)
            self.assertEqual(view["gpus"][gpu]["boot"][unit], "enabled")
            self.assertEqual(view["expected_indices"][gpu], i)
        self.assertEqual(self.host.states["legacy-all.service"], "inactive")

    def test_unknown_compute_and_legacy_multigpu_block_switch(self):
        rig = self.rig()
        self.host.owners[self.cards[1]] = {"unknown"}
        with self.assertRaises(RigError):
            rig.assign(self.cards[1], "PRL")
        self.host.owners.clear()
        self.host.states["legacy-all.service"] = "active"
        with self.assertRaises(RigError):
            rig.assign(self.cards[0], "PRL")
        self.assertNotIn(("systemctl", "--user", "stop", "legacy-all.service"), self.host.commands)

    def test_enabled_legacy_service_blocks_conflicting_boot_plan(self):
        self.host.boot["legacy-all.service"] = "enabled"
        with self.assertRaises(RigError):
            self.rig().assign(self.cards[0], "TSC")
        self.assertEqual(self.host.boot["legacy-all.service"], "enabled")
        self.assertEqual(self.host.states["tsc-0.service"], "inactive")

    def test_inactive_static_legacy_is_visible_and_allows_assignment(self):
        self.host.boot["legacy-all.service"] = "static"
        view = self.rig().snapshot()
        self.assertTrue(view["configured"])
        self.assertEqual(len(view["gpus"]), 6)
        self.assertEqual(view["gpus"][self.cards[0]]["boot"]["legacy-all.service"], "static")
        result = self.rig().assign(self.cards[0], "PRL")
        self.assertEqual(result["status"], "assigned")
        self.assertEqual(self.host.boot["legacy-all.service"], "static")
        self.assertIn(("systemctl", "--user", "show", "legacy-all.service", "-p", "WantedBy",
                       "-p", "RequiredBy", "-p", "TriggeredBy", "--value"), self.host.commands)

    def test_static_legacy_with_timer_link_blocks_assignment(self):
        self.host.boot["legacy-all.service"] = "static"
        self.host.static_links["legacy-all.service"] = "legacy-all.timer\n\n\n"
        with self.assertRaisesRegex(RigError, "boot or trigger links"):
            self.rig().assign(self.cards[0], "PRL")
        self.assertEqual(self.host.states["prl-0.service"], "inactive")

    def test_static_legacy_show_failure_blocks_assignment(self):
        self.host.boot["legacy-all.service"] = "static"
        self.host.fail_show = True
        with self.assertRaisesRegex(RigError, "boot or trigger links"):
            self.rig().assign(self.cards[0], "PRL")

    def test_active_static_legacy_still_blocks_assignment(self):
        self.host.boot["legacy-all.service"] = "static"
        self.host.states["legacy-all.service"] = "active"
        with self.assertRaisesRegex(RigError, "Unselectable managed service occupies GPU"):
            self.rig().assign(self.cards[0], "PRL")

    def test_static_profile_fails_before_any_start(self):
        self.host.boot["prl-0.service"] = "static"
        with self.assertRaisesRegex(RigError, "cannot be safely enabled"):
            self.rig().assign(self.cards[0], "PRL")
        self.assertEqual(self.host.states["prl-0.service"], "inactive")

    def test_expected_indices_use_config_when_probe_fails(self):
        for i, card in enumerate(self.cards):
            self.config["gpus"][card]["index"] = i
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host.gpu_indices = lambda: (_ for _ in ()).throw(RigError("index query failed"))
        rig = self.rig()
        self.assertEqual(rig.expected_indices, tuple(range(6)))
        self.assertEqual(len(rig.snapshot()["gpus"]), 6)

    def test_target_process_on_other_card_rolls_back(self):
        self.host.states["prl-0.service"] = "active"
        self.host.boot["prl-0.service"] = "enabled"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        original = self.host.run
        def wrong_card(argv, timeout=20):
            result = original(argv, timeout)
            if argv == ["systemctl", "--user", "start", "tsc-0.service"]:
                self.host.owners[self.cards[1]] = {"tsc-0.service"}
            return result
        rig = RigController(self.path, self.root / "state", runner=wrong_card, probe=self.host)
        with self.assertRaisesRegex(RigError, "rollback completed"):
            rig.assign(self.cards[0], "TSC")
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(self.host.states["tsc-0.service"], "inactive")
        self.assertNotIn("tsc-0.service", self.host.compute().get(self.cards[1], set()))

    def test_github_accept_header_distinguishes_metadata_and_binary(self):
        class Response:
            url = "https://api.github.com/repos/owner/miner/releases/assets/1"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return b"ok"
        seen = []
        def open_request(request, timeout):
            seen.append(request.get_header("Accept"))
            return Response()
        with patch("rig_control.urllib.request.urlopen", side_effect=open_request):
            RigController._fetch("https://api.github.com/repos/owner/miner/releases/latest", 10)
            RigController._fetch("https://api.github.com/repos/owner/miner/releases/assets/1", 10)
        self.assertEqual(seen, ["application/vnd.github+json", "application/octet-stream"])

    def test_target_failure_restores_source_and_boot(self):
        rig = self.rig()
        self.host.states["prl-0.service"] = "active"
        self.host.boot["prl-0.service"] = "enabled"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        self.host.fail_start = "tsc-0.service"
        with self.assertRaisesRegex(RigError, "rollback completed"):
            rig.assign(self.cards[0], "TSC")
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(self.host.boot["prl-0.service"], "enabled")
        self.assertEqual(self.host.states["tsc-0.service"], "inactive")
        self.assertFalse((self.root / "state" / "desired.json").exists())

    def test_already_active_reconciles_boot_and_desired(self):
        self.host.states["prl-0.service"] = "active"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        result = self.rig().assign(self.cards[0], "PRL")
        self.assertEqual(result["status"], "already_assigned")
        self.assertEqual(self.host.boot["prl-0.service"], "enabled")
        self.assertEqual(self.rig().snapshot()["gpus"][self.cards[0]]["desired"], "PRL")

    def test_stop_only_selected_card(self):
        rig = self.rig()
        for i in (0, 1):
            self.host.states[f"prl-{i}.service"] = "active"
            self.host.boot[f"prl-{i}.service"] = "enabled"
            self.host.owners[self.cards[i]] = {f"prl-{i}.service"}
        result = rig.assign(self.cards[0], "STOP")
        self.assertEqual(result["symbol"], "STOP")
        self.assertEqual(self.host.states["prl-1.service"], "active")
        self.assertEqual(self.host.boot["prl-1.service"], "enabled")
        self.assertFalse(any(cmd[-1] == "prl-1.service" and cmd[2] in {"start", "stop", "enable", "disable"}
                             for cmd in self.host.commands))

    def test_stop_preserves_foreign_compute_and_disables_only_miner(self):
        self.host.states["prl-1.service"] = "active"
        self.host.boot["prl-1.service"] = "enabled"
        self.host.owners[self.cards[1]] = {"prl-1.service", "unknown"}
        result = self.rig().assign(self.cards[1], "STOP")
        self.assertEqual(result["status"], "assigned")
        self.assertFalse(result["mining"])
        self.assertTrue(result["remaining_compute"])
        self.assertEqual(self.host.states["prl-1.service"], "inactive")
        self.assertEqual(self.host.boot["prl-1.service"], "disabled")
        self.assertEqual(self.host.compute()[self.cards[1]], {"unknown"})
        again = self.rig().assign(self.cards[1], "STOP")
        self.assertEqual(again["status"], "already_assigned")
        self.assertFalse(again["mining"])
        self.assertTrue(again["remaining_compute"])

    def test_update_unavailable_without_adapter(self):
        self.assertEqual(self.rig().check_updates(), {})
        with self.assertRaises(RigError):
            self.rig().apply_update("wildrig", 1)

    def test_official_manifest_binary_is_rechecked_before_install(self):
        versions = self.root / "versions"
        old = versions / "6.3.0"
        old.mkdir(parents=True)
        link = self.root / "current"
        link.symlink_to(old)
        binary = b"official binary bytes"
        digest = hashlib.sha256(binary).hexdigest()
        self.config["miners"] = {"qtc": {"provider": "json_manifest", "binary_name": "quanpool-miner",
                                           "versions_dir": str(versions), "current_link": str(link),
                                           "services": ["prl-0.service"], "health_argv": ["./quanpool-miner", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        manifest = {"miner_version": "6.4.0", "miner_sha256": {"linux-x86_64": digest},
                    "pool_miner_download": {"linux": "https://download.quanpool.com/quanpool-miner-linux-x86_64"}}
        requests = []
        def fetch(url, limit):
            requests.append(url)
            return json.dumps(manifest).encode() if url.endswith("/api/terms") else binary
        rig = self.rig(fetcher=fetch)
        checked = rig.check_updates()["qtc"]
        self.assertEqual(checked["status"], "available")
        manifest["miner_sha256"]["linux-x86_64"] = "0" * 64
        with self.assertRaisesRegex(RigError, "manifest changed"):
            rig.apply_update("qtc", "6.4.0")
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(link.resolve(), old.resolve())
        manifest["miner_sha256"]["linux-x86_64"] = digest
        updated = rig.apply_update("qtc", "6.4.0")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["restarted_services"], [])
        self.assertEqual(updated["kept_stopped"], ["prl-0.service"])
        self.assertEqual((versions / "6.4.0" / "quanpool-miner").read_bytes(), binary)
        self.assertEqual(requests.count("https://www.quanpool.com/api/terms"), 3)
        self.assertEqual(rig.snapshot()["updates"]["qtc"]["status"], "up_to_date")
        self.assertEqual(json.loads((self.root / "state" / "updates.json").read_text())["qtc"]["current_version"], "6.4.0")

    def test_check_rechecks_current_link_before_overwriting_update_result(self):
        versions = self.root / "versions"
        old = versions / "1.0.0"
        new = versions / "1.1.0"
        old.mkdir(parents=True)
        new.mkdir()
        link = self.root / "current"
        link.symlink_to(old)
        self.config["miners"] = {"miner": {"repo": "owner/miner", "asset_regex": "miner.tar.gz",
                                           "versions_dir": str(versions), "current_link": str(link),
                                           "services": ["prl-0.service"], "health_argv": ["./miner", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        release = {"id": 2, "tag_name": "1.1.0", "draft": False, "prerelease": False,
                   "assets": [{"id": 3, "name": "miner.tar.gz", "size": 10,
                               "digest": "sha256:" + "a" * 64}]}
        rig = self.rig(fetcher=lambda url, limit: json.dumps(release).encode())
        original_locked = rig._locked
        @contextlib.contextmanager
        def concurrent_install():
            rig._replace_link(link, new)
            with original_locked():
                yield
        rig._locked = concurrent_install
        checked = rig.check_updates()["miner"]
        self.assertEqual(checked["status"], "up_to_date")
        self.assertEqual(checked["current_version"], "1.1.0")
        self.assertEqual(json.loads((self.root / "state" / "updates.json").read_text())["miner"]["status"], "up_to_date")

    def test_zip_rejects_traversal_and_symlink(self):
        for name, mode in (("../escape", 0o100755), ("miner", 0o120777)):
            with self.subTest(name=name, mode=mode):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    archive.writestr(info, b"payload")
                with self.assertRaisesRegex(RigError, "Unsafe release archive"):
                    RigController._extract_asset(output.getvalue(), self.root / "staging", {"asset_format": "zip"})

    def test_zip_and_tar_extract_nested_health_binary(self):
        zip_output = io.BytesIO()
        with zipfile.ZipFile(zip_output, "w") as archive:
            info = zipfile.ZipInfo("racer/racer")
            info.create_system = 3
            info.external_attr = 0o100755 << 16
            archive.writestr(info, b"binary")
        zip_staging = self.root / "zip-staging"
        zip_staging.mkdir()
        RigController._extract_asset(zip_output.getvalue(), zip_staging, {"asset_format": "zip"})
        self.assertEqual((zip_staging / "racer/racer").read_bytes(), b"binary")
        tar_output = io.BytesIO()
        with tarfile.open(fileobj=tar_output, mode="w:gz") as archive:
            root = tarfile.TarInfo("./")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            miner = tarfile.TarInfo("./racer/racer")
            miner.size = 6
            miner.mode = 0o755
            archive.addfile(miner, io.BytesIO(b"binary"))
        tar_staging = self.root / "tar-staging"
        tar_staging.mkdir()
        RigController._extract_asset(tar_output.getvalue(), tar_staging, {"asset_format": "tar"})
        self.assertEqual((tar_staging / "racer/racer").read_bytes(), b"binary")

    def test_inactive_miner_updates_while_other_coin_uses_gpu(self):
        versions = self.root / "qtc-versions"
        old = versions / "6.3.1"
        old.mkdir(parents=True)
        link = self.root / "qtc-current"
        link.symlink_to(old)
        binary = b"new qtc miner"
        digest = hashlib.sha256(binary).hexdigest()
        self.config["miners"] = {"qtc": {"provider": "json_manifest", "binary_name": "quanpool-miner",
                                           "versions_dir": str(versions), "current_link": str(link),
                                           "services": ["tsc-0.service"], "health_argv": ["./quanpool-miner", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host.states["prl-0.service"] = "active"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        manifest = {"miner_version": "6.4.0", "miner_sha256": {"linux-x86_64": digest},
                    "pool_miner_download": {"linux": "https://download.quanpool.com/quanpool-miner-linux-x86_64"}}
        rig = self.rig(fetcher=lambda url, limit: json.dumps(manifest).encode() if url.endswith("/api/terms") else binary)
        rig.check_updates()
        updated = rig.apply_update("qtc", "6.4.0")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["restarted_services"], [])
        self.assertEqual(updated["kept_stopped"], ["tsc-0.service"])
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(self.host.owners[self.cards[0]], {"prl-0.service"})
        self.assertFalse(any(cmd[2] in {"start", "stop"} for cmd in self.host.commands if cmd[0] == "systemctl"))

    def test_cpu_update_scope_does_not_enter_gpu_assignment(self):
        self.config["cpu_services"] = ["noid-cpu-miner.service"]
        self.config["managed_services"]["noid-cpu-miner.service"] = []
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host.states["noid-cpu-miner.service"] = "active"
        self.host.boot["noid-cpu-miner.service"] = "enabled"
        self.rig().assign(self.cards[0], "PRL")
        self.assertEqual(self.host.states["noid-cpu-miner.service"], "active")
        self.assertFalse(any(cmd[-1] == "noid-cpu-miner.service" and cmd[2] in {"start", "stop", "enable", "disable"}
                             for cmd in self.host.commands))
        self.config["cpu_services"] = []
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertFalse(self.rig().snapshot()["configured"])

    def test_active_cpu_update_preserves_other_gpu_miner(self):
        self.config["cpu_services"] = ["noid-cpu-miner.service"]
        self.config["managed_services"]["noid-cpu-miner.service"] = []
        versions = self.root / "noid-versions"
        old = versions / "v0.2.21"
        old.mkdir(parents=True)
        link = self.root / "noid-current"
        link.symlink_to(old)
        lock = self.root / "manage.lock"
        self.config["miners"] = {"noid": {"repo": "owner/noid-miner",
                                            "asset_regex": "invminer-v0.2.22-linux-x86_64-cuda13.tar.gz",
                                            "versions_dir": str(versions), "current_link": str(link),
                                            "services": ["noid-cpu-miner.service", "tsc-0.service"],
                                            "health_argv": ["./invminer", "--version"],
                                            "external_locks": [str(lock)]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host.states["noid-cpu-miner.service"] = "active"
        self.host.boot["noid-cpu-miner.service"] = "enabled"
        self.host.states["prl-0.service"] = "active"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo("invminer")
            member.size = 6
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(b"binary"))
        asset = output.getvalue()
        release = {"id": 2, "tag_name": "v0.2.22", "draft": False, "prerelease": False,
                   "assets": [{"id": 3, "name": "invminer-v0.2.22-linux-x86_64-cuda13.tar.gz",
                               "size": len(asset), "digest": "sha256:" + hashlib.sha256(asset).hexdigest()}]}
        rig = self.rig(fetcher=lambda url, limit: asset if "/assets/" in url else json.dumps(release).encode())
        self.assertEqual(rig.check_updates()["noid"]["status"], "available")
        self.assertEqual(rig.apply_update("noid", 2)["status"], "updated")
        self.assertTrue(lock.exists())
        self.assertEqual(self.host.states["noid-cpu-miner.service"], "active")
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(self.host.owners[self.cards[0]], {"prl-0.service"})
        self.assertFalse(any(cmd[-1] == "prl-0.service" and cmd[2] in {"start", "stop"}
                             for cmd in self.host.commands))

    def test_noid_repository_requires_pinned_signature_adapter(self):
        versions = self.root / "versions"
        versions.mkdir()
        link = self.root / "current"
        link.symlink_to(versions / "0.2.21")
        self.config["miners"] = {"noid": {"repo": "getrigeos/INVminer-Release",
                                            "asset_regex": "invminer.tar.gz", "versions_dir": str(versions),
                                            "current_link": str(link), "services": ["prl-0.service"],
                                            "health_argv": ["./invminer", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertIn("pinned release signature required", self.rig().check_updates()["noid"]["error"])

    def _noid_policy_fixture(self, *, active=True, fee_changed=False, self_test_ok=True, candidate_ready=True):
        self.config["cpu_services"] = ["noid-cpu-miner.service"]
        self.config["managed_services"]["noid-cpu-miner.service"] = []
        self.host.states["noid-cpu-miner.service"] = "active" if active else "inactive"
        self.host.boot["noid-cpu-miner.service"] = "enabled" if active else "disabled"
        versions = self.root / "noid-policy-versions"
        old = versions / "v0.2.21"
        old.mkdir(parents=True)
        (old / "invminer").write_bytes(b"old binary")
        link = self.root / "noid-policy-current"
        link.symlink_to(old)
        key = self.root / "pinned.pem"
        key.write_bytes(b"test-only-public-key")
        digest = hashlib.sha256(key.read_bytes()).hexdigest()
        key_path_patch = patch("rig_control._NOID_KEY_FILE", str(key))
        key_sha_patch = patch("rig_control._NOID_KEY_SHA256", digest)
        verifier_patch = patch.object(RigController, "_verify_release_signature", return_value=None)
        for active_patch in (key_path_patch, key_sha_patch, verifier_patch):
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.config["miners"] = {"noid": {"repo": "getrigeos/INVminer-Release",
                                            "asset_regex": "invminer-v0.2.22-linux-x86_64-cuda13.tar.gz",
                                            "versions_dir": str(versions), "current_link": str(link),
                                            "services": ["noid-cpu-miner.service", "tsc-0.service"],
                                            "health_argv": ["./invminer", "--version"],
                                            "health_output_regex": "invminer {version}\\b",
                                            "signature_key_file": str(key), "signature_key_sha256": digest}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo("invminer")
            member.size = 6
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(b"binary"))
        asset = output.getvalue()
        release = {"id": 2, "tag_name": "v0.2.22", "draft": False, "prerelease": False,
                   "assets": [{"id": 3, "name": "invminer-v0.2.22-linux-x86_64-cuda13.tar.gz",
                               "size": len(asset), "digest": "sha256:" + hashlib.sha256(asset).hexdigest()},
                              {"id": 4, "name": "invminer-v0.2.22-linux-x86_64-cuda13.tar.gz.sig",
                               "size": 384, "digest": "sha256:" + "a" * 64}]}
        fee_assets = {"release_mode": "public", "commercial_default_ppm": 0,
                      "user_minimum_ppm": 0, "unconfigured_coin_default_ppm": 0,
                      "configured_coin_ppm": {"noid": 0}}
        host_run = self.host.run
        def run(argv, timeout=20):
            if argv[:2] == ["systemctl", "--user"] and argv[2] == "show" and "MainPID" in argv:
                self.host.commands.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, "1234\n", "")
            if argv[0] == "journalctl":
                self.host.commands.append(tuple(argv))
                ready = link.resolve().name == "v0.2.21" or candidate_ready
                output = "CPU_READY mode=cpu_only\nCPU_SELF_TEST_OK\n" if ready else "starting\n"
                return subprocess.CompletedProcess(argv, 0, output, "")
            if argv[0].endswith("/invminer"):
                self.host.commands.append(tuple(argv))
                candidate = ".rig-update-" in argv[0]
                if argv[1:] == ["--version"]:
                    return subprocess.CompletedProcess(argv, 0, "invminer 0.2.22\n", "")
                if argv[1:] == ["--build-info"]:
                    info = {"version": "0.2.22" if candidate else "0.2.21", "release_channel": "public",
                            "coins": [{"id": "noid", "fee_ppm": 1 if candidate and fee_changed else 0}],
                            "fee_assets": fee_assets}
                    return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
                if argv[1:] == ["--help"]:
                    return subprocess.CompletedProcess(argv, 0,
                                                       "--cpu-only --coin --api-port --state-dir --no-color", "")
                if argv[1:] == ["--coin", "noid", "--cpu-only", "--self-test"]:
                    return subprocess.CompletedProcess(argv, 0,
                                                       "CPU_SELF_TEST_OK" if self_test_ok else "self test failed", "")
            return host_run(argv, timeout=timeout)
        def fetch(url, limit):
            return asset if url.endswith("/assets/3") else b"signature" if url.endswith("/assets/4") else json.dumps(release).encode()
        rig = RigController(self.path, self.root / "state", runner=run, probe=self.host, fetcher=fetch)
        self.assertEqual(rig.check_updates()["noid"]["status"], "available")
        return rig, link, old

    def test_noid_cpu_wrong_fee_fails_before_stop(self):
        rig, link, old = self._noid_policy_fixture(fee_changed=True)
        with self.assertRaisesRegex(RigError, "fee policy changed"):
            rig.apply_update("noid", 2)
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(link.resolve(), old.resolve())
        self.assertFalse((old.parent / "v0.2.22").exists())

    def test_noid_cpu_self_test_fails_before_stop(self):
        rig, link, old = self._noid_policy_fixture(self_test_ok=False)
        with self.assertRaisesRegex(RigError, "self-test failed"):
            rig.apply_update("noid", 2)
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(link.resolve(), old.resolve())
        self.assertFalse((old.parent / "v0.2.22").exists())

    def test_noid_cpu_no_ready_rolls_back_and_old_cpu_reaches_ready(self):
        rig, link, old = self._noid_policy_fixture(candidate_ready=False)
        ticks = [0]
        def monotonic():
            ticks[0] += 1
            return ticks[0]
        with patch("rig_control.time.monotonic", side_effect=monotonic), patch("rig_control.time.sleep"):
            with self.assertRaisesRegex(RigError, "rollback completed"):
                rig.apply_update("noid", 2)
        self.assertEqual(link.resolve(), old.resolve())
        self.assertEqual(self.host.states["noid-cpu-miner.service"], "active")
        self.assertGreaterEqual(len([cmd for cmd in self.host.commands if cmd[0] == "journalctl"]), 2)
        self.assertFalse((old.parent / "v0.2.22").exists())

    def test_noid_cpu_inactive_stays_stopped_after_preflight(self):
        rig, link, old = self._noid_policy_fixture(active=False)
        result = rig.apply_update("noid", 2)
        self.assertEqual(result["kept_stopped"], ["noid-cpu-miner.service", "tsc-0.service"])
        self.assertFalse(any(cmd[2] in {"start", "stop"} and cmd[-1] == "noid-cpu-miner.service"
                             for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertTrue(any(cmd[-1] == "--self-test" for cmd in self.host.commands))

    def test_signed_release_missing_bad_then_valid_signature(self):
        private = self.root / "private.pem"
        public = self.root / "public.pem"
        subprocess.run(["/usr/bin/openssl", "genrsa", "-out", str(private), "2048"], check=True, capture_output=True)
        subprocess.run(["/usr/bin/openssl", "rsa", "-in", str(private), "-pubout", "-out", str(public)],
                       check=True, capture_output=True)
        versions = self.root / "signed-versions"
        old = versions / "1.0.0"
        old.mkdir(parents=True)
        link = self.root / "signed-current"
        link.symlink_to(old)
        archive_file = self.root / "archive.tar.gz"
        with tarfile.open(archive_file, "w:gz") as archive:
            member = tarfile.TarInfo("miner")
            member.size = 6
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(b"binary"))
        archive_bytes = archive_file.read_bytes()
        signature_file = self.root / "archive.sig"
        subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(private),
                        "-out", str(signature_file), str(archive_file)], check=True, capture_output=True)
        real_signature = signature_file.read_bytes()
        signature = [b"\x00" * len(real_signature)]
        self.config["miners"] = {"signed": {"repo": "owner/signed", "asset_regex": "miner.tar.gz",
                                              "versions_dir": str(versions), "current_link": str(link),
                                              "services": ["prl-0.service"], "health_argv": ["./miner", "--version"],
                                              "signature_key_file": str(public),
                                              "signature_key_sha256": hashlib.sha256(public.read_bytes()).hexdigest()}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        release = {"id": 2, "tag_name": "1.1.0", "draft": False, "prerelease": False,
                   "assets": [{"id": 3, "name": "miner.tar.gz", "size": len(archive_bytes),
                               "digest": "sha256:" + hashlib.sha256(archive_bytes).hexdigest()}]}
        def fetch(url, limit):
            if url.endswith("/assets/3"):
                return archive_bytes
            if url.endswith("/assets/4"):
                return signature[0]
            return json.dumps(release).encode()
        rig = self.rig(fetcher=fetch)
        self.assertEqual(rig.check_updates()["signed"]["status"], "unavailable")
        self.assertIn("signature unavailable", rig.check_updates()["signed"]["error"])
        release["assets"].append({"id": 4, "name": "miner.tar.gz.sig", "size": len(signature[0]),
                                  "digest": "sha256:" + hashlib.sha256(signature[0]).hexdigest()})
        self.assertEqual(rig.check_updates()["signed"]["status"], "available")
        with self.assertRaisesRegex(RigError, "signature verification failed"):
            rig.apply_update("signed", 2)
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(link.resolve(), old.resolve())
        signature[0] = real_signature
        release["assets"][1]["digest"] = "sha256:" + hashlib.sha256(real_signature).hexdigest()
        self.assertEqual(rig.check_updates()["signed"]["status"], "available")
        self.assertEqual(rig.apply_update("signed", 2)["status"], "updated")
        self.assertEqual(link.resolve().name, "1.1.0")

    def test_bad_version_output_fails_before_stopping_active_miner(self):
        versions = self.root / "versions"
        old = versions / "1.0.0"
        old.mkdir(parents=True)
        link = self.root / "current"
        link.symlink_to(old)
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo("miner")
            member.size = 6
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(b"binary"))
        asset = output.getvalue()
        self.config["miners"] = {"miner": {"repo": "owner/miner", "asset_regex": "miner.tar.gz",
                                           "versions_dir": str(versions), "current_link": str(link),
                                           "services": ["prl-0.service"], "health_argv": ["./miner", "--version"],
                                           "health_output_regex": "miner {version}\\b"}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.host.states["prl-0.service"] = "active"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        release = {"id": 2, "tag_name": "1.1.0", "draft": False, "prerelease": False,
                   "assets": [{"id": 3, "name": "miner.tar.gz", "size": len(asset),
                               "digest": "sha256:" + hashlib.sha256(asset).hexdigest()}]}
        rig = self.rig(fetcher=lambda url, limit: asset if "/assets/" in url else json.dumps(release).encode())
        self.assertEqual(rig.check_updates()["miner"]["status"], "available")
        with self.assertRaisesRegex(RigError, "New miner health command failed"):
            rig.apply_update("miner", 2)
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(link.resolve(), old.resolve())
        self.assertFalse((versions / "1.1.0").exists())

    def test_release_check_distinguishes_current_from_newer(self):
        versions = self.root / "versions"
        current_version = versions / "0.51.5"
        current_version.mkdir(parents=True)
        link = self.root / "current"
        link.symlink_to(current_version)
        self.config["miners"] = {"wildrig": {"repo": "owner/wildrig", "asset_regex": "wildrig.tar.gz",
                                           "versions_dir": str(versions), "current_link": str(link),
                                           "services": ["prl-0.service"], "health_argv": ["./wildrig", "-V"],
                                           "health_exit_codes": [0, 1],
                                           "health_output_regex": "WildRig Multi {version}\\b"}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        def release(tag):
            return json.dumps({"id": 77, "tag_name": tag, "draft": False, "prerelease": False,
                               "assets": [{"id": 91, "name": "wildrig.tar.gz", "size": 10,
                                           "digest": "sha256:" + "a" * 64}]}).encode()
        current = self.rig(fetcher=lambda url, limit: release("v0.51.5")).check_updates()["wildrig"]
        self.assertEqual(current["status"], "up_to_date")
        self.assertEqual(current["current_version"], "0.51.5")
        newer = self.rig(fetcher=lambda url, limit: release("v0.51.6")).check_updates()["wildrig"]
        self.assertEqual(newer["status"], "available")
        older = self.rig(fetcher=lambda url, limit: release("v0.51.4")).check_updates()["wildrig"]
        self.assertEqual(older["status"], "unavailable")
        self.assertIn("not newer", older["error"])

    def test_archive_link_rejected_before_service_stop(self):
        versions = self.root / "versions"
        current = self.root / "current"
        old = versions / "1.0.0"
        old.mkdir(parents=True)
        current.symlink_to(old)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            member = tarfile.TarInfo("escape")
            member.type = tarfile.SYMTYPE
            member.linkname = "../wallet"
            tar.addfile(member)
        archive = stream.getvalue()
        import hashlib
        self.config["miners"] = {"miner": {"repo": "owner/miner", "asset_regex": "miner.tar.gz",
                                           "versions_dir": str(versions), "current_link": str(current),
                                           "services": ["prl-0.service"], "health_argv": ["./miner", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        state = self.root / "state"
        state.mkdir()
        (state / "updates.json").write_text(json.dumps({"miner": {"status": "available", "release_id": 2,
            "tag": "1.1.0", "asset_id": 3, "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
            "checked_at": __import__("time").time()}}), encoding="utf-8")
        rig = self.rig(fetcher=lambda url, limit: archive)
        with self.assertRaisesRegex(RigError, "Unsafe release archive"):
            rig.apply_update("miner", 2)
        self.assertEqual(current.resolve(), old.resolve())
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))

    def test_update_wrong_card_rolls_back_despite_stale_link_names(self):
        import hashlib
        import time
        versions = self.root / "versions"
        old = versions / "1.0.0"
        old.mkdir(parents=True)
        current = self.root / "current"
        current.symlink_to(old)
        (self.root / "current.rig-old").symlink_to(old)
        (self.root / "current.rig-new").symlink_to(old)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            data = b"binary"
            member = tarfile.TarInfo("miner")
            member.size = len(data)
            member.mode = 0o755
            tar.addfile(member, io.BytesIO(data))
        archive = stream.getvalue()
        self.config["miners"] = {"miner": {"repo": "owner/miner", "asset_regex": "miner.tar.gz",
                                           "versions_dir": str(versions), "current_link": str(current),
                                           "services": ["prl-0.service"], "health_argv": ["./miner", "--version"]}}
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        state = self.root / "state"
        state.mkdir()
        (state / "updates.json").write_text(json.dumps({"miner": {"status": "available", "release_id": 2,
            "tag": "1.1.0", "asset_id": 3, "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
            "checked_at": time.time()}}), encoding="utf-8")
        self.host.states["prl-0.service"] = "active"
        self.host.boot["prl-0.service"] = "enabled"
        self.host.owners[self.cards[0]] = {"prl-0.service"}
        original = self.host.run
        starts = [0]
        def fail_after_restart(argv, timeout=20):
            result = original(argv, timeout)
            if argv == ["systemctl", "--user", "start", "prl-0.service"]:
                starts[0] += 1
                if starts[0] == 1:
                    self.host.owners[self.cards[1]] = {"prl-0.service"}
                else:
                    self.host.owners[self.cards[0]] = {"prl-0.service"}
            return result
        rig = RigController(self.path, state, runner=fail_after_restart, probe=self.host,
                            fetcher=lambda url, limit: archive)
        self.host.owners[self.cards[0]].add("unknown")
        with self.assertRaisesRegex(RigError, "Another process occupies an affected GPU"):
            rig.apply_update("miner", 2)
        self.assertFalse(any(cmd[2] == "stop" for cmd in self.host.commands if cmd[0] == "systemctl"))
        self.assertEqual(current.resolve(), old.resolve())
        self.assertFalse((versions / "1.1.0").exists())
        self.host.owners[self.cards[0]].remove("unknown")
        with self.assertRaisesRegex(RigError, "rollback completed"):
            rig.apply_update("miner", 2)
        self.assertEqual(current.resolve(), old.resolve())
        self.assertFalse((versions / "1.1.0").exists())
        self.assertEqual(self.host.states["prl-0.service"], "active")
        self.assertEqual(self.host.boot["prl-0.service"], "enabled")
        self.assertNotIn("prl-0.service", self.host.compute().get(self.cards[1], set()))
        retry = rig.apply_update("miner", 2)
        self.assertEqual(retry["status"], "updated")
        self.assertEqual(retry["restarted_services"], ["prl-0.service"])
        self.assertEqual(current.resolve().name, "1.1.0")


if __name__ == "__main__":
    unittest.main()
