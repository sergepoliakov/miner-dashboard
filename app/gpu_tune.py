#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""gpu-tune：GPU 核心频率上限、功耗上限与机箱风扇调速的 root 辅助程序。

部署为 /usr/local/sbin/gpu-tune，由监控大屏经受限 Unix socket 调用；只用标准库。
每个子命令都向 stdout 输出一个 JSON 对象：
  成功 {"ok": true, ...} 退出码 0；参数校验失败退出码 2；执行失败退出码 1。
status 无需 root；其余写操作需要 root（nvidia-smi -lgc/-pl、写 sysfs pwm）。
监控服务带 NoNewPrivileges 不能 sudo，写操作经 systemd 套接字 /run/gpu-tune.sock（gpu-tune.socket，
Accept=yes，组 configured-user 0660）进入 `gpu-tune serve`：核对对端 UID 后按同一套参数校验执行一次。
"""

import argparse
import contextlib
import csv
import fcntl
import io
import json
import os
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


NVIDIA_SMI = "/usr/bin/nvidia-smi"
SYSFS_ROOT = "/"
STATE_PATH = "/var/lib/gpu-tune/state.json"
LOG_PATH = "/var/log/gpu-tune.log"
NVIDIA_SMI_TIMEOUT = 10

# Optional root-owned hardware profile; no fan channels enabled by default.
_PROFILE_PATH = Path('/etc/miner-dashboard/hardware.json')
def load_hardware_profile():
    if not _PROFILE_PATH.exists(): return {}
    st = _PROFILE_PATH.stat()
    if st.st_uid != 0 or st.st_mode & 0o022 or _PROFILE_PATH.is_symlink():
        raise RuntimeError('Hardware profile must be root-owned and not group/world-writable')
    data = json.loads(_PROFILE_PATH.read_text())
    if not isinstance(data, dict): raise RuntimeError('Invalid hardware profile')
    return data
HARDWARE = load_hardware_profile()
HWMON_CHIP = HARDWARE.get('hwmon_chip', '')
CLOCK_MIN_ALLOWED_MHZ = 1500
CLOCK_LOCK_FLOOR_MHZ = 210
CLOCK_STEP_MHZ = 50
POWER_STEP_W = 10
FAN_MIN_PERCENT = 30
FAN_MAX_PERCENT = 100
FAN_STEP_PERCENT = 5
FAN_GROUPS = HARDWARE.get('fan_groups', {})
QUERY_FIELDS = (
    "index", "uuid", "name", "clocks.sm", "clocks.max.sm", "power.draw",
    "power.limit", "power.default_limit", "power.min_limit", "power.max_limit",
)
INT_RE = re.compile(r"^[0-9]{1,6}$")
# serve 只接受这些账户（再加 root）；参数逐个过字符白名单，命令限于写操作与 status。
SERVE_ALLOWED_USERS = tuple(HARDWARE.get("allowed_users", []))
SERVE_MAX_REQUEST = 4096
SERVE_TIMEOUT = 10
SERVE_ARG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UUID_RE = re.compile(r"^GPU-[0-9a-fA-F-]{8,64}$")


class ValidationError(Exception):
    """参数不合法，退出码 2。"""


class ExecError(Exception):
    """执行失败（驱动、sysfs、状态文件），退出码 1。"""


def _paths():
    """root 下一律使用固定路径；非 root（测试/开发）才允许环境变量覆盖。"""
    paths = {
        "nvidia_smi": NVIDIA_SMI,
        "sysfs_root": SYSFS_ROOT,
        "state": STATE_PATH,
        "log": LOG_PATH,
    }
    if os.geteuid() != 0:
        overrides = {
            "nvidia_smi": "GPU_TUNE_NVIDIA_SMI",
            "sysfs_root": "GPU_TUNE_SYSFS_ROOT",
            "state": "GPU_TUNE_STATE",
            "log": "GPU_TUNE_LOG",
        }
        for key, env in overrides.items():
            value = os.environ.get(env)
            if value:
                paths[key] = value
    return paths


def strict_int(value, name):
    """只接受 int（非 bool）或纯数字字符串；拒绝浮点、科学计数、负数、布尔。"""
    if isinstance(value, bool):
        raise ValidationError(f"{name} 必须是整数")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError(f"{name} 不能为负数")
        return value
    if isinstance(value, str) and INT_RE.match(value):
        return int(value)
    raise ValidationError(f"{name} 必须是整数")


def pwm_to_percent(pwm):
    return int(round(pwm * 100 / 255))


def percent_to_pwm(percent):
    return max(0, min(255, int(round(percent * 255 / 100))))


# ---------- nvidia-smi ----------

def run_nvidia_smi(args):
    cmd = [_paths()["nvidia_smi"], *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT, check=False
        )
    except FileNotFoundError:
        raise ExecError("找不到 nvidia-smi")
    except subprocess.TimeoutExpired:
        raise ExecError("nvidia-smi 超时")
    except OSError as exc:
        raise ExecError(f"nvidia-smi 无法执行：{exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ExecError(f"nvidia-smi 执行失败（{result.returncode}）：{detail[-1] if detail else '无输出'}")
    return result.stdout


def _num(text, cast=float):
    text = (text or "").strip()
    if not text or text.startswith("[") or text.lower() in ("n/a", "not supported"):
        return None
    try:
        return cast(float(text))
    except ValueError:
        return None


def query_gpus():
    out = run_nvidia_smi([
        "--query-gpu=" + ",".join(QUERY_FIELDS), "--format=csv,noheader,nounits",
    ])
    gpus = []
    for row in csv.reader(out.splitlines(), skipinitialspace=True):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != len(QUERY_FIELDS):
            raise ExecError("nvidia-smi 输出格式无法解析")
        item = dict(zip(QUERY_FIELDS, (cell.strip() for cell in row)))
        index = _num(item["index"], int)
        if index is None or not UUID_RE.match(item["uuid"]):
            raise ExecError("nvidia-smi 输出格式无法解析")
        gpus.append({
            "index": index,
            "uuid": item["uuid"],
            "name": item["name"],
            "clock_current": _num(item["clocks.sm"], int),
            "clock_hw_max": _num(item["clocks.max.sm"], int),
            "draw_w": _num(item["power.draw"]),
            "limit_w": _num(item["power.limit"]),
            "default_w": _num(item["power.default_limit"]),
            "min_w": _num(item["power.min_limit"]),
            "max_w": _num(item["power.max_limit"]),
        })
    return gpus


def find_gpu(gpus, index):
    for gpu in gpus:
        if gpu["index"] == index:
            return gpu
    raise ValidationError(f"GPU {index} 不存在（当前可用：{', '.join(str(g['index']) for g in gpus) or '无'}）")


# ---------- hwmon ----------

def find_hwmon():
    base = Path(_paths()["sysfs_root"]) / "sys" / "class" / "hwmon"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        raise ExecError(f"找不到 hwmon 目录：{base}")
    for entry in entries:
        try:
            if (entry / "name").read_text().strip() == HWMON_CHIP:
                return entry
        except OSError:
            continue
    raise ExecError(f"找不到风扇控制芯片 {HWMON_CHIP}")


def _read_int(path):
    return int(Path(path).read_text().strip())


def _write_sysfs(path, value):
    try:
        with open(path, "w") as handle:
            handle.write(f"{value}\n")
    except OSError as exc:
        raise ExecError(f"写入 {Path(path).name} 失败：{exc.strerror or exc}")


def read_fan_group(hwmon, group, saved_percent):
    spec = FAN_GROUPS[group]
    pwms, manual, rpms = [], [], []
    for ch in spec["channels"]:
        try:
            pwms.append(_read_int(hwmon / f"pwm{ch}"))
            manual.append(_read_int(hwmon / f"pwm{ch}_enable") == 1)
            rpms.append(_read_int(hwmon / f"fan{ch}_input"))
        except (OSError, ValueError) as exc:
            raise ExecError(f"读取 {spec['label']} 通道 {ch} 失败：{exc}")
    pwm = pwms[0]
    return {
        "group": group,
        "label": spec["label"],
        "channels": list(spec["channels"]),
        "pwm": pwm,
        "percent": pwm_to_percent(pwm),
        "manual": all(manual),
        "rpm": rpms,
        "default_percent": pwm_to_percent(spec["default_pwm"]),
        "min_percent": FAN_MIN_PERCENT,
        "max_percent": FAN_MAX_PERCENT,
        "saved_percent": saved_percent,
        "step_percent": FAN_STEP_PERCENT,
    }


def write_fan_pwm(group, pwm):
    hwmon = find_hwmon()
    for ch in FAN_GROUPS[group]["channels"]:
        _write_sysfs(hwmon / f"pwm{ch}_enable", 1)
        _write_sysfs(hwmon / f"pwm{ch}", pwm)


# ---------- 状态文件 ----------

def empty_state():
    return {
        "version": 1,
        "gpus": {},
        "fans": {group: {"percent": None} for group in FAN_GROUPS},
    }


def _optional_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_state(raw):
    state = empty_state()
    if not isinstance(raw, dict):
        raise ValueError("顶层不是对象")
    gpus = raw.get("gpus")
    if isinstance(gpus, dict):
        for uuid, entry in gpus.items():
            if isinstance(uuid, str) and UUID_RE.match(uuid) and isinstance(entry, dict):
                state["gpus"][uuid] = {
                    "clock_max_mhz": _optional_int(entry.get("clock_max_mhz")),
                    "power_limit_w": _optional_int(entry.get("power_limit_w")),
                }
    fans = raw.get("fans")
    if isinstance(fans, dict):
        for group in FAN_GROUPS:
            entry = fans.get(group)
            if isinstance(entry, dict):
                state["fans"][group]["percent"] = _optional_int(entry.get("percent"))
    return state


def load_state(errors=None):
    """缺失或损坏一律按空状态处理，记录错误但不中断。"""
    path = Path(_paths()["state"])
    try:
        text = path.read_text()
    except FileNotFoundError:
        return empty_state()
    except OSError as exc:
        _note(errors, f"状态文件无法读取，按空状态处理：{exc.strerror or exc}")
        return empty_state()
    try:
        return normalize_state(json.loads(text))
    except (ValueError, TypeError) as exc:
        _note(errors, f"状态文件损坏，按空状态处理：{exc}")
        return empty_state()


def _note(errors, message):
    if errors is not None:
        errors.append(message)
    print(f"gpu-tune: {message}", file=sys.stderr)


def save_state(state):
    path = Path(_paths()["state"])
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StateLock:
    """读-改-写期间持有 fcntl 独占锁（锁文件与状态文件同目录）。"""

    def __init__(self):
        self.path = Path(_paths()["state"]).with_name(".state.lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.handle = open(self.path, "a")
        try:
            os.chmod(self.path, 0o644)
        except OSError:
            pass
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        return False


def update_state(mutate):
    try:
        with StateLock():
            state = load_state()
            mutate(state)
            save_state(state)
    except OSError as exc:
        raise ExecError(f"状态文件写入失败：{exc.strerror or exc}")


def _gpu_entry(state, uuid):
    return state["gpus"].setdefault(uuid, {"clock_max_mhz": None, "power_limit_w": None})


# ---------- 操作日志 ----------

def append_log(action, args, ok, error=None):
    record = {"ts": time.time(), "action": action, "args": args, "ok": ok, "error": error}
    path = _paths()["log"]
    try:
        # /var/log 对 syslog 组可写且无粘滞位：不跟随符号链接，并确认是 root 独占（单链接）的普通文件再追加。
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    (os.geteuid() == 0 and info.st_uid != 0)):
                raise OSError(f"{path} 不是 root 独占的普通文件，拒绝写入")
            handle = os.fdopen(fd, "a")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"gpu-tune: 操作日志写入失败：{exc}", file=sys.stderr)


# ---------- 状态汇总 ----------

def build_status():
    errors = []
    state = load_state(errors)
    gpus_out, fans_out = [], []
    gpu_ok = fan_ok = False
    try:
        for gpu in query_gpus():
            saved = state["gpus"].get(gpu["uuid"], {})
            hw_max = gpu["clock_hw_max"]
            gpus_out.append({
                "index": gpu["index"],
                "uuid": gpu["uuid"],
                "name": gpu["name"],
                "clock": {
                    "current_mhz": gpu["clock_current"],
                    "hw_max_mhz": hw_max,
                    "locked_max_mhz": saved.get("clock_max_mhz"),
                    "min_allowed_mhz": CLOCK_MIN_ALLOWED_MHZ,
                    "max_allowed_mhz": hw_max,
                    "step_mhz": CLOCK_STEP_MHZ,
                },
                "power": {
                    "draw_w": gpu["draw_w"],
                    "limit_w": gpu["limit_w"],
                    "default_w": gpu["default_w"],
                    "min_w": gpu["min_w"],
                    "max_w": gpu["max_w"],
                    "saved_w": saved.get("power_limit_w"),
                    "step_w": POWER_STEP_W,
                },
            })
        gpu_ok = True
    except ExecError as exc:
        errors.append(f"GPU：{exc}")
    try:
        hwmon = find_hwmon()
        for group in FAN_GROUPS:
            try:
                fans_out.append(read_fan_group(hwmon, group, state["fans"][group]["percent"]))
                fan_ok = True
            except ExecError as exc:
                errors.append(f"风扇：{exc}")
    except ExecError as exc:
        errors.append(f"风扇：{exc}")
    status = {"ok": gpu_ok or fan_ok, "gpus": gpus_out, "fans": fans_out, "updated_at": time.time()}
    if errors:
        status["errors"] = errors
    if not status["ok"]:
        status["error"] = "；".join(errors)
    return status


# ---------- 写操作 ----------

def _clock_target(gpu, mhz):
    hw_max = gpu["clock_hw_max"]
    if hw_max is None:
        raise ExecError(f"GPU {gpu['index']} 未报告最高核心频率")
    if not CLOCK_MIN_ALLOWED_MHZ <= mhz <= hw_max:
        raise ValidationError(f"核心频率上限须在 {CLOCK_MIN_ALLOWED_MHZ}–{hw_max} MHz 之间")


def _power_target(gpu, watts):
    lo, hi = gpu["min_w"], gpu["max_w"]
    if lo is None or hi is None:
        raise ExecError(f"GPU {gpu['index']} 未报告功耗范围")
    if not lo <= watts <= hi:
        raise ValidationError(f"功耗上限须在 {lo:g}–{hi:g} W 之间")


def cmd_set_clock(args):
    index = strict_int(args.gpu, "GPU 编号")
    mhz = strict_int(args.mhz, "核心频率")
    gpu = find_gpu(query_gpus(), index)
    _clock_target(gpu, mhz)
    run_nvidia_smi(["-i", gpu["uuid"], "-lgc", f"{CLOCK_LOCK_FLOOR_MHZ},{mhz}"])
    update_state(lambda s: _gpu_entry(s, gpu["uuid"]).update(clock_max_mhz=mhz))
    return f"GPU {index} 核心频率上限已设为 {mhz} MHz"


def cmd_reset_clock(args):
    index = strict_int(args.gpu, "GPU 编号")
    gpu = find_gpu(query_gpus(), index)
    run_nvidia_smi(["-i", gpu["uuid"], "-rgc"])
    update_state(lambda s: _gpu_entry(s, gpu["uuid"]).update(clock_max_mhz=None))
    return f"GPU {index} 核心频率已恢复原厂"


def cmd_set_power(args):
    index = strict_int(args.gpu, "GPU 编号")
    watts = strict_int(args.watts, "功耗")
    gpu = find_gpu(query_gpus(), index)
    _power_target(gpu, watts)
    run_nvidia_smi(["-i", gpu["uuid"], "-pl", str(watts)])
    update_state(lambda s: _gpu_entry(s, gpu["uuid"]).update(power_limit_w=watts))
    return f"GPU {index} 功耗上限已设为 {watts} W"


def cmd_reset_power(args):
    index = strict_int(args.gpu, "GPU 编号")
    gpu = find_gpu(query_gpus(), index)
    if gpu["default_w"] is None:
        raise ExecError(f"GPU {index} 未报告默认功耗")
    watts = int(round(gpu["default_w"]))
    run_nvidia_smi(["-i", gpu["uuid"], "-pl", str(watts)])
    update_state(lambda s: _gpu_entry(s, gpu["uuid"]).update(power_limit_w=None))
    return f"GPU {index} 功耗上限已恢复默认 {watts} W"


def _check_group(group):
    if not isinstance(group, str) or group not in FAN_GROUPS:
        raise ValidationError("风扇组只能是 cpu 或 gpu_ext")
    return group


def cmd_set_fan(args):
    group = _check_group(args.group)
    percent = strict_int(args.percent, "风扇转速")
    if not FAN_MIN_PERCENT <= percent <= FAN_MAX_PERCENT:
        raise ValidationError(f"风扇转速须在 {FAN_MIN_PERCENT}–{FAN_MAX_PERCENT}% 之间")
    write_fan_pwm(group, percent_to_pwm(percent))
    update_state(lambda s: s["fans"][group].update(percent=percent))
    return f"{FAN_GROUPS[group]['label']}已设为 {percent}%"


def cmd_reset_fan(args):
    group = _check_group(args.group)
    pwm = FAN_GROUPS[group]["default_pwm"]
    write_fan_pwm(group, pwm)
    update_state(lambda s: s["fans"][group].update(percent=None))
    return f"{FAN_GROUPS[group]['label']}已恢复默认 {pwm_to_percent(pwm)}%"


def cmd_apply(_args):
    """开机重放已保存的设置；逐项报告，不抛异常。"""
    results = []
    errors = []
    state = load_state(errors)
    saved_gpus = {u: e for u, e in state["gpus"].items()
                  if e.get("clock_max_mhz") is not None or e.get("power_limit_w") is not None}
    if saved_gpus:
        try:
            current = {g["uuid"]: g for g in query_gpus()}
        except Exception as exc:  # noqa: BLE001 - apply 永不中断
            current = None
            errors.append(f"GPU：{exc}")
        for uuid, entry in saved_gpus.items():
            for field, label in (("clock_max_mhz", "clock"), ("power_limit_w", "power")):
                value = entry.get(field)
                if value is None:
                    continue
                item = {"target": f"gpu:{uuid}", "kind": label, "value": value}
                gpu = current.get(uuid) if current is not None else None
                if current is None:
                    item.update(result="failed", error="无法查询 GPU")
                elif gpu is None:
                    item.update(result="skipped", error="GPU 当前不存在")
                else:
                    try:
                        if label == "clock":
                            _clock_target(gpu, value)
                            run_nvidia_smi(["-i", uuid, "-lgc", f"{CLOCK_LOCK_FLOOR_MHZ},{value}"])
                        else:
                            _power_target(gpu, value)
                            run_nvidia_smi(["-i", uuid, "-pl", str(value)])
                        item["result"] = "applied"
                    except Exception as exc:  # noqa: BLE001
                        item.update(result="failed", error=str(exc))
                results.append(item)
    for group in FAN_GROUPS:
        percent = state["fans"][group]["percent"]
        if percent is None:
            continue
        item = {"target": f"fan:{group}", "kind": "fan", "value": percent}
        try:
            if not FAN_MIN_PERCENT <= percent <= FAN_MAX_PERCENT:
                raise ValidationError("保存的风扇转速超出范围")
            write_fan_pwm(group, percent_to_pwm(percent))
            item["result"] = "applied"
        except Exception as exc:  # noqa: BLE001
            item.update(result="failed", error=str(exc))
        results.append(item)
    failed = [r for r in results if r["result"] == "failed"]
    applied = sum(1 for r in results if r["result"] == "applied")
    skipped = sum(1 for r in results if r["result"] == "skipped")
    message = f"已重放 {applied} 项，跳过 {skipped} 项，失败 {len(failed)} 项"
    return {"ok": not failed, "message": message, "results": results, "errors": errors}


MUTATIONS = {
    "set-clock": cmd_set_clock,
    "reset-clock": cmd_reset_clock,
    "set-power": cmd_set_power,
    "reset-power": cmd_reset_power,
    "set-fan": cmd_set_fan,
    "reset-fan": cmd_reset_fan,
}


class JsonArgumentParser(argparse.ArgumentParser):
    """参数错误也按 JSON 输出，退出码 2。"""

    def error(self, message):
        raise ValidationError(f"参数错误：{message}")


def build_parser():
    parser = JsonArgumentParser(prog="gpu-tune", description="GPU 频率/功耗与机箱风扇调节")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    sub.add_parser("status")
    sub.add_parser("apply")
    sub.add_parser("serve")
    for name in ("set-clock", "reset-clock", "set-power", "reset-power"):
        p = sub.add_parser(name)
        p.add_argument("--gpu", required=True)
        if name == "set-clock":
            p.add_argument("--mhz", required=True)
        if name == "set-power":
            p.add_argument("--watts", required=True)
    for name in ("set-fan", "reset-fan"):
        p = sub.add_parser(name)
        p.add_argument("--group", required=True)
        if name == "set-fan":
            p.add_argument("--percent", required=True)
    return parser


# ---------- 套接字服务（systemd Accept=yes） ----------

def _allowed_uids():
    uids = {0}
    for name in SERVE_ALLOWED_USERS:
        try:
            uids.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            continue
    return uids


def _peer_uid(conn):
    pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return uid


def _serve_reply(conn, exit_code, output):
    conn.sendall(json.dumps({"exit_code": exit_code, "output": output}, ensure_ascii=False).encode() + b"\n")


def cmd_serve(fd=0):
    """处理一个已连接的套接字请求 {"argv": [...]}，回复 {"exit_code": n, "output": "<命令的 JSON 输出>"}。"""
    conn = socket.socket(fileno=os.dup(fd))
    try:
        conn.settimeout(SERVE_TIMEOUT)
        try:
            uid = _peer_uid(conn)
        except OSError as exc:
            _serve_reply(conn, 3, json.dumps({"ok": False, "error": f"无法核对调用方：{exc}"}, ensure_ascii=False))
            return 3
        if uid not in _allowed_uids():
            append_log("serve", {"uid": uid}, False, "调用方不在白名单")
            _serve_reply(conn, 3, json.dumps({"ok": False, "error": "调用方无权调节"}, ensure_ascii=False))
            return 3
        raw = b""
        while b"\n" not in raw and len(raw) <= SERVE_MAX_REQUEST:
            chunk = conn.recv(SERVE_MAX_REQUEST)
            if not chunk:
                break
            raw += chunk
        argv = None
        if len(raw) <= SERVE_MAX_REQUEST:
            try:
                request = json.loads(raw.split(b"\n", 1)[0] or b"null")
                argv = request.get("argv") if isinstance(request, dict) else None
            except (ValueError, UnicodeDecodeError):
                argv = None
        if not (isinstance(argv, list) and 1 <= len(argv) <= 8 and
                all(isinstance(arg, str) and SERVE_ARG_RE.match(arg) for arg in argv) and
                argv[0] in set(MUTATIONS) | {"status"}):
            _serve_reply(conn, 2, json.dumps({"ok": False, "error": "请求格式无效"}, ensure_ascii=False))
            return 2
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(argv)
        _serve_reply(conn, exit_code, buffer.getvalue())
        return exit_code
    except (OSError, socket.timeout) as exc:
        print(f"gpu-tune: 套接字请求处理失败：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except ValidationError as exc:
        emit({"ok": False, "error": str(exc)})
        return 2
    command = args.command
    if command == "serve":
        return cmd_serve()
    if command != "status":
        # 写操作开始后不因调用方断开（sudo 被杀、pty 挂断）中途退出，保证驱动/sysfs 与状态文件一致；
        # SIG_IGN 跨 exec 继承，nvidia-smi 子进程同样不受影响。
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if command == "status":
        try:
            status = build_status()
        except Exception as exc:  # noqa: BLE001
            emit({"ok": False, "error": f"状态读取失败：{exc}"})
            return 1
        emit(status)
        return 0 if status["ok"] else 1
    if command == "apply":
        try:
            result = cmd_apply(args)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "message": "重放失败", "results": [], "errors": [str(exc)]}
        append_log("apply", {}, result["ok"], None if result["ok"] else "；".join(
            [r.get("error", "") for r in result["results"] if r["result"] == "failed"] + result["errors"]))
        try:
            result["status"] = build_status()
        except Exception as exc:  # noqa: BLE001
            result["status"] = {"ok": False, "error": str(exc)}
        emit(result)
        return 0 if result["ok"] else 1
    log_args = {k: v for k, v in vars(args).items() if k != "command"}
    try:
        message = MUTATIONS[command](args)
    except ValidationError as exc:
        append_log(command, log_args, False, str(exc))
        emit({"ok": False, "error": str(exc)})
        return 2
    except ExecError as exc:
        append_log(command, log_args, False, str(exc))
        emit({"ok": False, "error": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001
        append_log(command, log_args, False, f"未预期错误：{exc}")
        emit({"ok": False, "error": f"未预期错误：{exc}"})
        return 1
    append_log(command, log_args, True)
    emit({"ok": True, "message": message, "status": build_status()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
