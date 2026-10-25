#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Read-only, loopback-only GPU monitor driven by per-coin JSON manifests."""

import argparse
import csv
import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from runtime_paths import CONFIG_DIR, STATE_DIR
from rig_control import RigController
from rgb_control import snapshot as rgb_snapshot, update_style as update_rgb_style
from gpu_display import decorate_gpus, display_slots
from pool_notices import NoticeChecker

from safetrade_client import ExchangeError, SafeTradeProxyClient, available_pairs, parse_positive_decimal


HTML_PATH = Path(__file__).with_name("mining_monitor.html")
FAN_VIDEO_PATH = Path(__file__).with_name("gpu_fans_strip.mp4")
FAN_POSTER_PATH = Path(__file__).with_name("gpu_fans_strip.jpg")
DEFAULT_CONFIG_DIR = CONFIG_DIR / "coins.d"
DEFAULT_STATE_DIR = STATE_DIR
PROXY = os.environ.get("MINING_MONITOR_HTTP_PROXY", "")
FEED_INTERVAL = 45
EXCHANGE_CHECK_INTERVAL = 300
FX_URL = "https://api.frankfurter.dev/v1/latest?base=CNY&symbols=USD"
FX_REFRESH_INTERVAL = 900
FX_RETRY_INTERVAL = 60
FX_MAX_AGE = 48 * 60 * 60
ELECTRICITY_CNY_PER_KWH = float(os.environ.get("MINER_ELECTRICITY_CNY_PER_KWH", "0.6"))
# Whole-rig overhead estimate; per-coin profiles may allocate only a share of this amount.
ESTIMATED_HOST_POWER_W = 90.0
# Physical inventory of this monitor's two-card workstation; do not infer it from active miners.
EXPECTED_GPU_INDICES = ()
PROFIT_WINDOW_SECONDS = 24 * 60 * 60
GPU_TEMP_WARN_C = 82
GPU_TEMP_CRITICAL_C = 88
MEMORY_TEMP_WARN_C = 90
MEMORY_TEMP_CRITICAL_C = 100
# 驱动不给 GeForce 显存温度时，由 root 服务 gpu-vramtemp.service 读 GDDR6X 寄存器写出（非 NVIDIA 官方接口）。
VRAM_EXPORT_PATH = Path("/run/gpu-vramtemp.json")
VRAM_EXPORT_MAX_AGE = 60
THERMAL_SAMPLE_INTERVAL = 10
# 频率/功耗/风扇调节统一交给 root 助手 gpu-tune（范围校验以助手为准）；大屏只做形状校验与转发。
GPU_TUNE_PATH = "/usr/local/sbin/gpu-tune"
# 写操作经 root 套接字（gpu-tune.socket，组 configured-user 0660）；本服务带 NoNewPrivileges，不能走 sudo。
GPU_TUNE_SOCKET = "/run/gpu-tune.sock"
GPU_TUNE_STATUS_TTL = 5
GPU_TUNE_STATUS_TIMEOUT = 10
# 助手一次写操作最多三次 nvidia-smi（查询、-lgc/-pl、状态重建），各 ≤10 秒；留足余量，避免大屏先超时放锁而助手仍在执行。
GPU_TUNE_WRITE_TIMEOUT = 45
GPU_TUNE_FAN_GROUPS = ("cpu", "gpu_ext")
# 粗粒度上限只用于挡掉明显异常的输入，真实可调范围由驱动与助手给出。
GPU_TUNE_VALUE_BOUNDS = {"gpu_clock": (1, 10000), "gpu_power": (1, 2000), "fan": (0, 100)}
GPU_TUNE_MAX_GPU_INDEX = 15
NOID_CONTROL_PATH = str(Path.home() / ".local/bin/noid-cpu-control")
NOID_JOURNAL_MAX_AGE = 300
# NOID 每张显卡一个独立矿工服务和只读接口；配置只能引用这里登记的组合
NOID_GPU_MINERS = {}  # GPU API adapter needs a locally audited mapping; generic GPU log adapters work.
THERMAL_LOG_INTERVAL = 60
EXCHANGE_PUBLIC_URL = "https://safe.trade/api/v2/trade/public/tickers"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
SERVICE_RE = re.compile(r"^[a-zA-Z0-9_.@-]+\.service$")
# 各币种 monitor.json 可声明的只读数据源；停矿币种降频抓取，仍用于行情雷达。
FEED_KINDS = ("price", "pool", "earnings", "account", "daily", "network")
FEED_DEFAULT_MAX_AGE = {"price": 150, "pool": 150, "earnings": 180, "account": 300, "daily": 1800, "network": 300}
FEED_LABELS = {"price": "报价", "pool": "矿池份额", "earnings": "矿池记录", "account": "矿池账户",
               "daily": "每日入账", "network": "全网"}
INACTIVE_FEED_INTERVAL = 300
ADDRESS_RE = re.compile(r"^[A-Za-z0-9]{20,128}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RATE_UNIT_RE = re.compile(r"^([kKMGTPE]?)([A-Za-z]{1,8}/s)$")
SI_PREFIX = {"": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
# 历史采样：遥测每分钟一行（走势图、今日电量），余额与行情低频落盘（24 小时产出、涨跌幅）。
TELEMETRY_INTERVAL = 60
TELEMETRY_KEEP_SECONDS = 48 * 60 * 60
BALANCE_INTERVAL = 300
MARKET_INTERVAL = 600
SERIES_KEEP_SECONDS = 8 * 24 * 60 * 60
HISTORY_MAX_BYTES = 6_000_000
OBSERVED_MIN_SECONDS = 2 * 60 * 60
# 24 小时实记是滞后的均值：全网算力快涨快跌时另看近 6 小时，换币比较用它（与停挖币按当下全网的估算同一时点）
OBSERVED_RECENT_SECONDS = 6 * 60 * 60
# 开挖还不满 6 小时就先看最近 2 小时：难度几小时翻倍的币，开挖以来的均值跟不上现在的入账。
# 样本短、受出块运气影响大，只用于显示和比较，不进校准比例
OBSERVED_EARLY_SECONDS = 2 * 60 * 60
# “在亏电费”“换币更划算”要连续成立这么久才提醒，矿工重启预热、接口超时一次不算
ECONOMICS_ALERT_HOLD_SECONDS = 10 * 60
# 校准比例＝在挖时“近 6 小时实记 ÷ 当下理论值”，每次记账按约一天的时间常数平滑后落盘
# （占全网份额小的矿池，几小时的实记受出块运气影响大，要平均得久一点）；
# 停挖后“若改挖”的估算＝理论值 × 这个比例，超过 14 天没更新就不再用
CALIBRATION_SMOOTH_SECONDS = 24 * 60 * 60
CALIBRATION_MAX_AGE = 14 * 24 * 60 * 60
CALIBRATION_MIN, CALIBRATION_MAX = 0.2, 3.0
# 当前算力和记账窗口里的平均算力差出这个比例（加卡、停卡、调功耗），产出改按“每单位算力的记账 × 当前算力”算
OBSERVED_RATE_SHIFT = 0.10
MANUAL_PRICE_MAX_AGE = 24 * 60 * 60
# 提醒阈值：全网算力、币价相对 24 小时前（或数据源自带的 24 小时均值）的变化。
NETWORK_SURGE_PCT = 25.0
NETWORK_DROP_PCT = -20.0
PRICE_MOVE_PCT = 15.0
POOL_SILENT_SECONDS = 15 * 60
# 池端算力是一小时均值，矿工刚启动时必然偏低：连续偏低满一小时才提醒
POOL_LOW_HOLD_SECONDS = 60 * 60
RATE_FAULT_HOLD_SECONDS = 3 * 60
NETWORK_SOURCE_MAX_AGE = 30 * 60
JSONL_MAX_BYTES = 8_000_000
POOL_BAD_SHARE_PCT = 3.0
POOL_LOW_RATIO = 0.7
DISK_FREE_WARN_PCT = 10.0
REBOOT_REQUIRED_PATH = Path("/var/run/reboot-required")
YIELD_INDEX_MOVE_PCT = 20.0
POOL_DOMINANCE_PCT = 50.0
# WhatToMine 公开榜单：发现“还有哪些币值得看”的线索层，按其默认算力口径，不代表本机收益。
LEADERBOARD_URL = "https://whattomine.com/coins.json"
LEADERBOARD_INTERVAL = 600
LEADERBOARD_MAX_AGE = 3600
# 写接口按对端套接字属主 UID 鉴权：CSRF 与 Origin 只挡浏览器，挡不住本机其他账户直接发请求。
PROC_NET_TCP = Path("/proc/net/tcp")


def _proc_tcp_endpoint(address):
    """把 (IPv4, 端口) 转成 /proc/net/tcp 的地址写法（按本机字节序的 8 位十六进制:4 位十六进制端口）。"""
    return f"{int.from_bytes(socket.inet_aton(address[0]), sys.byteorder):08X}:{int(address[1]):04X}"


def loopback_peer_uid(peer, local):
    """在 /proc/net/tcp 找到对端（本机客户端）套接字并返回其属主 UID；找不到或不可读返回 None，调用方按拒绝处理。"""
    try:
        wanted_local, wanted_remote = _proc_tcp_endpoint(peer), _proc_tcp_endpoint(local)
        with PROC_NET_TCP.open(encoding="ascii") as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) > 7 and fields[1] == wanted_local and fields[2] == wanted_remote:
                    return int(fields[7])
    except (OSError, ValueError, TypeError, IndexError):
        return None
    return None


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_nonnegative_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("金额格式无效")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("金额格式无效") from error
    if not number.is_finite() or number < 0:
        raise ValueError("金额格式无效")
    return number


def sanitize_exchange_row(kind, row):
    allowed = {"id", "currency", "market", "side", "state", "amount", "origin_amount",
               "filled_amount", "price", "fee", "txid", "created_at", "done_at"}
    if kind == "beneficiaries":  # 地址簿只露标签、币种、网络和状态，地址和备注不进接口输出
        allowed = {"id", "currency", "name", "state", "blockchain_key"}
    return {key: str(value)[:120] for key, value in row.items()
            if key in allowed and isinstance(value, (str, int, float)) and not isinstance(value, bool)}


def get_path(data, path):
    for key in path.split("."):
        if isinstance(data, list) and re.fullmatch(r"-?\d{1,6}", key):
            try:
                data = data[int(key)]
            except IndexError:
                return None
            continue
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def finite_number(value):
    """矿池接口常把整数金额写成字符串；只接受有限数值或纯十进制字符串，其余一律 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"-?\d{1,40}(?:\.\d{1,40})?(?:[eE][+-]?\d{1,3})?", value.strip()):
            return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def nonnegative_number(value):
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


def scaled_amount(data, path, divisor):
    """按字段路径取非负金额并除以最小单位换算；路径缺失或数值无效返回 None。"""
    if not path:
        return None
    number = nonnegative_number(get_path(data, path))
    return number / divisor if number is not None else None


def split_rate_unit(unit):
    """'MH/s' -> (1e6, 'H/s')；无法识别返回 (None, None)，调用方按不可换算处理。"""
    match = RATE_UNIT_RE.fullmatch(unit.strip()) if isinstance(unit, str) else None
    return (SI_PREFIX[match.group(1)], match.group(2)) if match else (None, None)


def parse_timestamp(value):
    """秒/毫秒时间戳或 ISO 8601 字符串 -> epoch 秒；无法识别返回 None。"""
    number = finite_number(value) if not isinstance(value, str) or re.fullmatch(r"\d{9,14}(\.\d+)?", value.strip()) else None
    if number is not None:
        return number / 1000 if number > 1e11 else number
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", value.strip())
    if not match:
        return None
    try:
        import calendar
        year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
        epoch = calendar.timegm((year, month, day, hour, minute, second))
    except (ValueError, OverflowError):
        return None
    zone = match.group(7)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return float(epoch)


_UI_VERSION = {}


def ui_version():
    """页面文件的短哈希；已打开的大屏发现它变化后自行刷新，部署新页面不必重开窗口。"""
    try:
        stat = HTML_PATH.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if _UI_VERSION.get("key") != key:
            _UI_VERSION.update(key=key, value=hashlib.sha256(HTML_PATH.read_bytes()).hexdigest()[:12])
        return _UI_VERSION["value"]
    except OSError:
        return None


def validate_config(item, filename):
    def fail(message):
        raise ValueError(f"{filename}: {message}")

    if not isinstance(item, dict):
        fail("配置必须是 JSON 对象")
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        fail("symbol 必须是 2–12 位大写字母或数字")
    if not isinstance(item.get("name"), str) or not item["name"].strip():
        fail("缺少 name")
    if not isinstance(item.get("service"), str) or not SERVICE_RE.fullmatch(item["service"]):
        fail("service 必须是 systemd 用户服务名")
    gpu_indices = item.get("gpu_indices", [])
    if not isinstance(gpu_indices, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in gpu_indices) or len(set(gpu_indices)) != len(gpu_indices):
        fail("gpu_indices 必须是不重复的非负整数数组")
    if item.get("kind") == "cpu":
        if symbol != "NOID" or gpu_indices or item["service"] != "noid-cpu-miner.service":
            fail("NOID CPU 配置不能申报 GPU 或其他服务")
        return item
    if item.get("kind") == "cpu_gpu" and (symbol != "NOID" or gpu_indices not in ([0], [1], [0, 1]) or not all(i in NOID_GPU_MINERS for i in gpu_indices) or
                                            item["service"] != NOID_GPU_MINERS[gpu_indices[0]]["service"]):
        fail("NOID 混合配置只允许本机已登记的独立 GPU 服务；CPU 控制仍使用原 CPU 服务")
    profitability = item.get("profitability")
    if profitability is not None:
        if not isinstance(profitability, dict):
            fail("profitability 必须是 JSON 对象")
        power_map = profitability.get("gpu_board_w_per_gpu")
        if not isinstance(power_map, dict) or any(str(index) not in power_map for index in gpu_indices):
            fail("profitability.gpu_board_w_per_gpu 必须覆盖该币种的每张显卡")
        if any(positive_number(power_map[str(index)]) is None for index in gpu_indices):
            fail("profitability.gpu_board_w_per_gpu 必须是正数")
        overhead = profitability.get("base_system_w")
        if isinstance(overhead, bool) or not isinstance(overhead, (int, float)) or not math.isfinite(overhead) or overhead < 0:
            fail("profitability.base_system_w 必须是非负数")
        reference_gpus = profitability.get("reference_gpu_count")
        if isinstance(reference_gpus, bool) or not isinstance(reference_gpus, int) or reference_gpus < 1:
            fail("profitability.reference_gpu_count 必须是正整数")
        if not isinstance(profitability.get("power_basis"), str) or not profitability["power_basis"].strip():
            fail("profitability.power_basis 缺失")
    rate = item.get("rate")
    if not isinstance(rate, dict) or not isinstance(rate.get("log"), str) or not Path(rate["log"]).is_absolute():
        fail("rate.log 必须是绝对路径")
    if rate.get("parser") is not None and (rate["parser"] != "invminer_noid_api" or
            symbol != "NOID" or item.get("kind") != "cpu_gpu" or not gpu_indices or gpu_indices[0] not in NOID_GPU_MINERS or
            rate.get("api_url") != NOID_GPU_MINERS[gpu_indices[0]]["api_url"]):
        fail("矿工 API 只允许本机已登记的 NOID GPU 只读端点")
    for key, groups in (("gpu_regex", {"gpu", "value"}), ("total_regex", {"value"}), ("table_start_regex", set())):
        expression = rate.get(key)
        if not isinstance(expression, str) or not expression or len(expression) > 500:
            fail(f"缺少或过长的 rate.{key}")
        try:
            compiled = re.compile(expression, re.I)
        except re.error as error:
            fail(f"rate.{key} 正则无效：{error}")
        if not groups.issubset(compiled.groupindex):
            fail(f"rate.{key} 缺少命名捕获组 {groups}")
    if not isinstance(rate.get("unit"), str) or not rate["unit"].strip():
        fail("缺少 rate.unit")
    for key, default in (("max_age_seconds", 90), ("max_trailing_lines", 30)):
        value = rate.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
            fail(f"rate.{key} 必须是 1–3600 的整数")
    # 只把一张卡露给矿工时，矿工日志里它是 0 号：gpu_map 把日志里的卡号换成物理卡号，占卡判断才不会认错
    gpu_map = rate.get("gpu_map")
    if gpu_map is not None and (
            not isinstance(gpu_map, dict) or not gpu_map or
            any(not isinstance(key, str) or not key.isdigit() or isinstance(value, bool) or
                not isinstance(value, int) or value < 0 for key, value in gpu_map.items())):
        fail("rate.gpu_map 必须是 {日志里的卡号: 物理卡号} 的对象")
    # 一个币在几张卡上各跑一个矿工服务（PRL：GPU0、GPU1 各一个，各写各的日志）：gpu_services 登记每张卡的服务，
    # rate.log_by_gpu 登记每张卡的日志；只有在跑的那几张卡计入产出和功耗（同 NOID）
    uuid_services = item.get("gpu_uuid_services")
    if uuid_services is not None:
        from per_gpu_miner import UUID
        logs = rate.get("log_by_uuid")
        if (not isinstance(uuid_services, dict) or not uuid_services or item.get("kind") in {"cpu", "cpu_gpu"} or
                any(not isinstance(u,str) or not UUID.fullmatch(u) or not isinstance(unit,str) or not SERVICE_RE.fullmatch(unit) for u,unit in uuid_services.items()) or
                len(set(uuid_services.values())) != len(uuid_services) or item["service"] not in uuid_services.values() or
                item.get("gpu_services") is not None or rate.get("log_by_gpu") is not None or
                not isinstance(logs,dict) or set(logs)!=set(uuid_services) or
                any(not isinstance(v,str) or not Path(v).is_absolute() for v in logs.values())):
            fail("gpu_uuid_services 需要独立单卡服务及对应的绝对 log_by_uuid 路径")
    elif rate.get("log_by_uuid") is not None:
        fail("log_by_uuid 需要 gpu_uuid_services")
    gpu_services = item.get("gpu_services")
    if gpu_services is not None:
        if (not isinstance(gpu_services, dict) or not gpu_services or
                any(not isinstance(key, str) or not key.isdigit() or not isinstance(value, str) or
                    not SERVICE_RE.fullmatch(value) for key, value in gpu_services.items())):
            fail("gpu_services 必须是 {显卡编号: systemd 用户服务名} 的对象")
        if sorted(int(key) for key in gpu_services) != sorted(gpu_indices):
            fail("gpu_services 的显卡编号必须与 gpu_indices 一致")
        if item["service"] not in gpu_services.values():
            fail("service 必须是 gpu_services 里的一个")
        log_by_gpu = rate.get("log_by_gpu")
        if (not isinstance(log_by_gpu, dict) or set(log_by_gpu) != set(gpu_services) or
                any(not isinstance(value, str) or not Path(value).is_absolute() for value in log_by_gpu.values())):
            fail("gpu_services 需要 rate.log_by_gpu：卡号与 gpu_services 一致，值是绝对路径")
        if item.get("kind") in {"cpu", "cpu_gpu"}:
            fail("gpu_services 不能和 NOID 的 kind 一起用")
    elif rate.get("log_by_gpu") is not None:
        fail("rate.log_by_gpu 只能和 gpu_services 一起使用")
    validate_extras(item, fail)
    for kind in FEED_KINDS:
        if kind not in item:
            continue
        feed = item[kind]
        if not isinstance(feed, dict) or not isinstance(feed.get("url"), str) or not feed["url"].startswith("https://"):
            fail(f"{kind}.url 必须是 HTTPS")
        if len(feed["url"]) > 400 or feed["url"].count("{") != feed["url"].count("{address}") or \
                feed["url"].count("}") != feed["url"].count("{address}"):
            fail(f"{kind}.url 只允许 {{address}} 占位符")
        if "{address}" in feed["url"] and "address_source" not in item:
            fail(f"{kind}.url 使用 {{address}} 时必须配置 address_source")
        if not isinstance(feed.get("source"), str) or not feed["source"]:
            fail(f"{kind}.source 缺失")
        max_age = feed.get("max_age_seconds", FEED_DEFAULT_MAX_AGE[kind])
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= 7200:
            fail(f"{kind}.max_age_seconds 无效")
        if "link" in feed and (not isinstance(feed["link"], str) or not feed["link"].startswith("https://")):
            fail(f"{kind}.link 必须是 HTTPS")
        if kind == "price" and "smooth_minutes" in feed and (
                isinstance(feed["smooth_minutes"], bool) or not isinstance(feed["smooth_minutes"], int) or
                not 20 <= feed["smooth_minutes"] <= 360):
            fail("price.smooth_minutes 必须是 20–360 的整数")
        if kind == "price" and "bid_paths" in feed and (
                not isinstance(feed["bid_paths"], list) or not feed["bid_paths"] or
                not all(isinstance(item, str) and item for item in feed["bid_paths"])):
            fail("price.bid_paths 必须是字段路径的列表")
        if kind == "account" and "warmup_minutes" in feed and (
                isinstance(feed["warmup_minutes"], bool) or not isinstance(feed["warmup_minutes"], int) or
                not 0 <= feed["warmup_minutes"] <= 720):
            fail("account.warmup_minutes 必须是 0–720 的整数")
        fields = {"price": ("value_path",), "pool": ("accepted_path", "rejected_path"),
                  "account": ("unit", "pending_path"), "daily": ("unit", "rows_path", "day_field", "amount_field"),
                  "network": ("hashrate_path", "hashrate_unit")}.get(kind, ())
        if any(not isinstance(feed.get(field), str) or not feed[field] for field in fields):
            fail(f"{kind} 缺少字段路径")
        for key, value in feed.items():
            if key.endswith(("_path", "_field")) and (not isinstance(value, str) or not value or len(value) > 120):
                fail(f"{kind}.{key} 无效")
        for key in ("divisor", "reward_divisor"):
            if key in feed and positive_number(feed[key]) is None:
                fail(f"{kind}.{key} 必须是正数")
        if kind == "daily" and feed.get("day_timezone", "UTC") != "UTC":
            fail("daily.day_timezone 目前只支持 UTC 日界")
        if kind == "network":
            if split_rate_unit(feed["hashrate_unit"])[0] is None:
                fail("network.hashrate_unit 无法识别")
            if "block_seconds" in feed and positive_number(feed["block_seconds"]) is None:
                fail("network.block_seconds 必须是正数")
            if "target_block_seconds" in feed and positive_number(feed["target_block_seconds"]) is None:
                fail("network.target_block_seconds 必须是正数")
            if feed.get("difficulty_unit", "hashes_per_block") != "hashes_per_block" or (
                    "difficulty_unit" in feed and "difficulty_path" not in feed):
                fail("network.difficulty_unit 只支持 hashes_per_block，且要配 difficulty_path")
            if "block_reward" in feed and (positive_number(feed["block_reward"]) is None or
                                           not isinstance(feed.get("block_reward_note"), str) or
                                           not feed["block_reward_note"].strip()):
                fail("network.block_reward 是手填的协议参数，必须是正数并用 block_reward_note 写明来源与日期")
            for key in ("pool_fee_percent", "extra_fee_percent"):
                value = feed.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 100:
                    fail(f"network.{key} 必须在 0–100 之间")
            scale = feed.get("yield_scale", 1)
            if positive_number(scale) is None or scale > 100:
                fail("network.yield_scale 必须是正数")
            if not isinstance(feed.get("yield_note", ""), str) or not isinstance(feed.get("calibrated", False), bool) \
                    or not isinstance(feed.get("volatile", False), bool):
                fail("network.yield_note / calibrated / volatile 无效")
        if kind in {"pool", "account"} and "worker_hashrate_field" in feed:
            if not isinstance(feed["worker_hashrate_field"], str) or not feed["worker_hashrate_field"]:
                fail(f"{kind}.worker_hashrate_field 无效")
            for field in ("worker_hashrate_unit", "worker_hashrate_window"):
                if not isinstance(feed.get(field), str) or not feed[field].strip():
                    fail(f"{kind}.{field} 缺失")
            if kind == "account" and (split_rate_unit(feed["worker_hashrate_unit"])[0] is None or
                                      "workers_path" not in feed):
                fail("account.worker_hashrate_unit 无法识别或缺少 workers_path")
        if kind == "earnings" and feed.get("parser") not in {"tsc_wallet_sats", "pearlhash_transactions"}:
            fail("earnings.parser 不支持")
        if kind == "price":
            if "timestamp_path" in feed and not isinstance(feed["timestamp_path"], str):
                fail("price.timestamp_path 无效")
            source_age = feed.get("max_source_age_seconds", 600)
            if isinstance(source_age, bool) or not isinstance(source_age, int) or not 1 <= source_age <= 86400:
                fail("price.max_source_age_seconds 无效")
    return item


def validate_extras(item, fail):
    """展示与估算用的可选声明：主题色、链接、矿池备选、参考算力、收款地址来源。"""
    if "color" in item and (not isinstance(item["color"], str) or not COLOR_RE.fullmatch(item["color"])):
        fail("color 必须是 #RRGGBB")
    for key in ("algorithm", "miner", "market_note", "whattomine_tag"):
        if key in item and (not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 200):
            fail(f"{key} 必须是 200 字以内的文本")
    links = item.get("links", [])
    if not isinstance(links, list) or len(links) > 8 or any(
            not isinstance(link, dict) or not isinstance(link.get("label"), str) or not link["label"].strip() or
            len(link["label"]) > 40 or not isinstance(link.get("url"), str) or
            not link["url"].startswith("https://") or len(link["url"]) > 400 for link in links):
        fail("links 必须是最多 8 个 {label, https url}")
    pools = item.get("pools", [])
    if not isinstance(pools, list) or len(pools) > 12:
        fail("pools 必须是最多 12 项的数组")
    for pool in pools:
        if not isinstance(pool, dict) or not isinstance(pool.get("name"), str) or not pool["name"].strip():
            fail("pools 每项需要 name")
        if "url" in pool and (not isinstance(pool["url"], str) or not pool["url"].startswith("https://")):
            fail("pools.url 必须是 HTTPS")
        for key in ("fee", "min_payout", "scheme", "share", "note", "verified"):
            if key in pool and (not isinstance(pool[key], str) or len(pool[key]) > 240):
                fail(f"pools.{key} 必须是 240 字以内的文本")
        for key in ("current", "avoid"):
            if key in pool and not isinstance(pool[key], bool):
                fail(f"pools.{key} 必须是布尔值")
    reference = item.get("reference")
    if reference is not None:
        rates = reference.get("hashrate_per_gpu") if isinstance(reference, dict) else None
        if not isinstance(rates, dict) or not rates or any(
                not re.fullmatch(r"\d{1,2}", str(index)) or positive_number(value) is None
                for index, value in rates.items()):
            fail("reference.hashrate_per_gpu 必须是 {显卡编号: 正数}")
        if split_rate_unit(reference.get("unit", ""))[0] is None:
            fail("reference.unit 无法识别")
        if not isinstance(reference.get("note"), str) or not reference["note"].strip():
            fail("reference.note 缺失（写明实测时间与功耗档位）")
    source = item.get("address_source")
    if source is not None:
        if not isinstance(source, dict) or not isinstance(source.get("file"), str) or \
                not Path(source["file"]).is_absolute() or not isinstance(source.get("json_path"), str) or \
                not source["json_path"]:
            fail("address_source 需要绝对路径 file 与 json_path")
        if "split" in source and (not isinstance(source["split"], str) or len(source["split"]) != 1):
            fail("address_source.split 必须是单个分隔字符")


_ADDRESS_CACHE = {}


def resolve_address(coin):
    """从本机受限文件取公开收款地址，只用于拼矿池只读接口；不写入配置、接口输出或日志。"""
    source = coin.get("address_source")
    if not source:
        return None
    path = Path(source["file"])
    try:
        stat = path.stat()
        key = (source["file"], source["json_path"], stat.st_mtime_ns)
        if _ADDRESS_CACHE.get(coin["symbol"], (None, None))[0] != key:
            value = get_path(json.loads(path.read_text(encoding="utf-8")), source["json_path"])
            if isinstance(value, str) and source.get("split"):
                value = value.split(source["split"], 1)[0]
            _ADDRESS_CACHE[coin["symbol"]] = (key, value if isinstance(value, str) and ADDRESS_RE.fullmatch(value) else None)
        return _ADDRESS_CACHE[coin["symbol"]][1]
    except (OSError, ValueError):
        return None


def feed_url(coin, config):
    """返回 (抓取地址, 可公开展示的链接)；含 {address} 的地址不外露。"""
    url = config["url"]
    if "{address}" not in url:
        return url, config.get("link", url)
    address = resolve_address(coin)
    return (url.replace("{address}", address) if address else None), config.get("link")


def load_configs(directory):
    paths = sorted(directory.glob("*.json"))
    if not paths:
        if os.environ.get("MINING_MONITOR_ALLOW_EMPTY_COINS") == "1":
            return []
        raise ValueError(f"{directory}: 没有币种配置")
    configs = []
    seen = set()
    for path in paths:
        if path.stat().st_size > 16384:
            raise ValueError(f"{path}: 配置文件过大")
        item = validate_config(json.loads(path.read_text(encoding="utf-8")), path)
        if item["symbol"] in seen:
            raise ValueError(f"{path}: 重复币种 {item['symbol']}")
        seen.add(item["symbol"])
        configs.append(item)
    return configs


def parse_rate(text, rate_config):
    table = re.compile(rate_config["table_start_regex"], re.I)
    gpu = re.compile(rate_config["gpu_regex"], re.I)
    total = re.compile(rate_config["total_regex"], re.I)
    per_gpu = {}
    total_value = None
    unit = rate_config["unit"]
    total_line = -1
    gpu_map = rate_config.get("gpu_map") or {}
    lines = text.splitlines()
    for line_number, line in enumerate(lines):
        if table.search(line):
            per_gpu = {}
        match = gpu.search(line)
        if match:
            number = positive_number(match.group("value"))
            if number is not None:
                per_gpu[str(gpu_map.get(match.group("gpu"), match.group("gpu")))] = number
        match = total.search(line)
        if match:
            total_value = positive_number(match.group("value"))
            unit = match.groupdict().get("unit") or rate_config["unit"]
            total_line = line_number
    return {"total": total_value, "unit": unit, "window": rate_config.get("window", "矿工最近采样"),
            "per_gpu": per_gpu, "trailing_lines": len(lines) - total_line - 1, "_total_line": total_line}


def read_noid_gpu_rate(gpu_indices, now):
    """逐卡读各自矿工的只读接口，只把正常的卡计入；一张卡在预热或暂停不影响另一张的读数。"""
    cards = []
    for index in gpu_indices:
        try:
            with build_opener(ProxyHandler({})).open(NOID_GPU_MINERS[index]["api_url"], timeout=2) as response:
                cards.append(parse_noid_gpu_stats(json.loads(response.read(65536)), now, index))
        except (OSError, ValueError, KeyError):
            cards.append({"status": "NOID GPU 本机接口不可用"})
    normal = [card for card in cards if card["status"] == "正常"]
    if not normal:
        return {"status": cards[0]["status"] if cards else "NOID GPU 本机接口不可用"}
    per_gpu = {gpu: value for card in normal for gpu, value in card["per_gpu"].items()}
    pools = [card["pool"] for card in normal]
    pool = None
    if all(pools):
        pool = {**{key: sum(item[key] for item in pools) for key in ("accepted", "stale", "rejected")},
                "source": pools[0]["source"], "fetched_at": now}
    return {**normal[0], "total": sum(per_gpu.values()), "per_gpu": per_gpu,
            "age_seconds": max(card["age_seconds"] for card in normal), "pool": pool}


def read_gpu_services_rate(coin, now, state):
    """一个币在几张卡上各跑一个矿工、各写各的日志：逐卡读，正常的卡才计入合计；
    一张卡刚启动、还没出表不影响另一张的读数（与 NOID 同样的处理）。"""
    rate = coin["rate"]
    cards = []
    for index in coin["gpu_indices"]:
        sub = {**rate, "log": rate["log_by_gpu"][str(index)]}
        card = read_rate(sub, now, state.setdefault(str(index), {}))
        if rate.get("single_gpu_logs") and card.get("status") == "正常" and card.get("total") is not None:
            card = {**card, "per_gpu": {str(index): card["total"]}}
        cards.append(card)
    normal = [card for card in cards if card["status"] == "正常"]
    if not normal:
        return cards[0] if cards else {"status": "暂无算力采样"}
    if len({card["unit"] for card in normal}) != 1:
        return {"status": "各卡算力单位不一致"}
    per_gpu = {gpu: value for card in normal for gpu, value in card["per_gpu"].items()}
    return {**normal[0], "total": sum(card["total"] for card in normal), "per_gpu": per_gpu,
            "age_seconds": max(card["age_seconds"] for card in normal)}


def read_rate(rate_config, now, state=None, gpu_indices=None):
    if rate_config.get("parser") == "invminer_noid_api":
        return read_noid_gpu_rate(gpu_indices or [0], now)
    path = Path(rate_config["log"])
    try:
        stat = path.stat()
        age = max(0, now - stat.st_mtime)
        if age > rate_config.get("max_age_seconds", 90):
            if state is not None:
                state.clear()
            return {"status": "过期", "age_seconds": round(age)}
        with path.open("rb") as handle:
            start = max(0, stat.st_size - 131072)
            handle.seek(start)
            raw_bytes = handle.read(stat.st_size - start)
        raw = raw_bytes.decode("utf-8", "replace")
        parsed = parse_rate(raw, rate_config)
        if parsed["total"] is None:
            return {"status": "暂无算力采样", "age_seconds": round(age)}
        total_line = parsed.pop("_total_line")
        trailing = parsed.pop("trailing_lines")
        if state is None:
            if trailing > rate_config.get("max_trailing_lines", 30):
                return {"status": "暂无算力采样", "age_seconds": round(age)}
            return {"status": "正常", "age_seconds": round(age), **parsed}

        # Racer may write dozens of diagnostic lines between rate tables. Track the
        # last complete table by its file position, rather than counting those lines.
        if state.get("log_size", -1) > stat.st_size:
            state.clear()
        marker = (stat.st_dev, stat.st_ino,
                  start + sum(len(line) for line in raw_bytes.splitlines(keepends=True)[:total_line]))
        if state.get("marker") != marker:
            first_seen = "marker" not in state
            state.update(marker=marker, seen_at=None if first_seen and trailing > rate_config.get("max_trailing_lines", 30) else now,
                         last_rate=parsed)
        state["log_size"] = stat.st_size
        if state.get("seen_at") is None:
            return {"status": "暂无算力采样", "age_seconds": round(age)}
        sample_age = max(0, now - state["seen_at"])
        if sample_age > rate_config.get("max_age_seconds", 90):
            return {"status": "过期", "age_seconds": round(sample_age)}
        return {"status": "正常", "age_seconds": round(sample_age), **state["last_rate"]}
    except OSError:
        return {"status": "日志不可用"}


def parse_noid_gpu_stats(data, now, index=0):
    """Validate the selected physical GPU before publishing its completed-work rate."""
    if not isinstance(data, dict) or data.get("algo") != "noid":
        return {"status": "NOID GPU 接口币种不符"}
    devices = data.get("gpus")
    if not isinstance(devices, list) or len(devices) != 1 or not isinstance(devices[0], dict) or \
            devices[0].get("uuid") != NOID_GPU_MINERS[index]["uuid"]:
        return {"status": "NOID GPU 物理设备不符"}
    gpu = devices[0]
    seconds = data.get("rate_sample_seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 120:
        return {"status": "等待完整两分钟采样"}
    age = gpu.get("seconds_since_last_completed_work")
    if not isinstance(age, (int, float)) or isinstance(age, bool) or not math.isfinite(age) or not 0 <= age <= 45:
        return {"status": "NOID GPU 完成工作采样已过期"}
    value = positive_number(gpu.get("hs"))
    if value is None or gpu.get("paused") or gpu.get("thermal_paused"):
        return {"status": "NOID GPU 暂停或暂无有效算力"}
    shares = data.get("shares")
    pool = None
    if isinstance(shares, dict) and all(strict_int(shares.get(k)) and shares[k] >= 0
                                       for k in ("accepted", "stale", "rejected")):
        pool = {**{k: shares[k] for k in ("accepted", "stale", "rejected")},
                "source": "当前 NOID GPU 进程的矿池回执，非钱包累计余额", "fetched_at": now}
    return {"status": "正常", "total": value / 1e6, "unit": "MH/s", "per_gpu": {str(index): value / 1e6},
            "age_seconds": round(age), "window": "本机已完成工作量，滚动120秒；未扣1%开发费",
            "sampled_at": now, "pool": pool}


def parse_price(data, config, fetched_at):
    value = positive_number(get_path(data, config["value_path"]))
    if value is None:
        return None
    result = {"value": value, "source": config["source"], "source_url": config.get("link", config["url"]),
              "fetched_at": fetched_at}
    if config.get("timestamp_path"):
        source_at = parse_timestamp(get_path(data, config["timestamp_path"]))
        if source_at is None or source_at > fetched_at + 60:
            return None
        result["source_at"] = source_at
    if config.get("change_24h_path"):
        change = finite_number(get_path(data, config["change_24h_path"]))
        if change is not None and -100 < change < 100000:
            result["change_24h_pct"] = change
    bids = [positive_number(get_path(data, item)) for item in config.get("bid_paths", [])]
    if any(bid is not None for bid in bids):
        result["bid"] = min(bid for bid in bids if bid is not None)
    return result


def parse_fx(data, fetched_at):
    if not isinstance(data, dict) or data.get("base") != "CNY" or not isinstance(data.get("date"), str):
        return None
    rates = data.get("rates")
    rate = positive_number(rates.get("USD")) if isinstance(rates, dict) else None
    if rate is None:
        return None
    return {"cny_to_usd": rate, "rate_date": data["date"], "source": "Frankfurter CNY/USD 参考汇率",
            "source_url": FX_URL, "fetched_at": fetched_at}


def parse_pool(data, config, fetched_at):
    accepted = get_path(data, config["accepted_path"])
    rejected = get_path(data, config["rejected_path"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (accepted, rejected)):
        return None
    workers = get_path(data, config.get("workers_path", "")) if config.get("workers_path") else None
    result = {"accepted": accepted, "rejected": rejected, "workers": len(workers) if isinstance(workers, list) else None,
              "source": config["source"], "source_url": config.get("link"), "fetched_at": fetched_at}
    if isinstance(workers, list) and workers and config.get("worker_hashrate_field"):
        values = [get_path(worker, config["worker_hashrate_field"]) for worker in workers if isinstance(worker, dict)]
        if len(values) == len(workers) and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                                and math.isfinite(value) and value >= 0 for value in values):
            result.update({"effective_hashrate": sum(values), "effective_hashrate_unit": config["worker_hashrate_unit"],
                           "effective_hashrate_window": config["worker_hashrate_window"]})
    return result


def parse_account(data, config, fetched_at):
    """矿池账户：待付、已付、打款门槛与 Worker 池端统计。金额按 divisor 换算成整币。"""
    divisor = float(config.get("divisor", 1))
    pending = scaled_amount(data, config["pending_path"], divisor)
    if pending is None:
        return None
    result = {"unit": config["unit"], "pending": pending, "source": config["source"],
              "source_url": config.get("link"), "fetched_at": fetched_at,
              "paid": scaled_amount(data, config.get("paid_path"), divisor),
              "threshold": scaled_amount(data, config.get("threshold_path"), divisor),
              "immature": scaled_amount(data, config.get("immature_path"), divisor),
              "workers": None, "hashrate": None, "shares": None, "invalid": None, "stale": None, "last_seen": None}
    workers = get_path(data, config["workers_path"]) if config.get("workers_path") else None
    if isinstance(workers, dict):
        workers = list(workers.values())
    if isinstance(workers, list):
        rows = [row for row in workers if isinstance(row, dict)]
        result["workers"] = len(rows)

        def total(field):
            values = [nonnegative_number(get_path(row, config[field])) for row in rows] if config.get(field) else []
            return sum(values) if values and all(value is not None for value in values) else None

        hashrate = total("worker_hashrate_field")
        if hashrate is not None:
            scale, base = split_rate_unit(config["worker_hashrate_unit"])
            result.update({"hashrate": hashrate, "hashrate_unit": config["worker_hashrate_unit"],
                           "hashrate_base": hashrate * scale, "hashrate_base_unit": base,
                           "hashrate_window": config["worker_hashrate_window"]})
        for name, field in (("shares", "worker_shares_field"), ("invalid", "worker_invalid_field"),
                            ("stale", "worker_stale_field")):
            result[name] = total(field)
        if config.get("worker_last_seen_field"):
            seen = [parse_timestamp(get_path(row, config["worker_last_seen_field"])) for row in rows]
            seen = [value for value in seen if value is not None and value <= fetched_at + 300]
            result["last_seen"] = max(seen, default=None)
    # 账户级字段优先于逐 Worker 求和（有的矿池只在账户层给窗口算力和最近份额时间）。
    if config.get("hashrate_path"):
        hashrate = nonnegative_number(get_path(data, config["hashrate_path"]))
        scale, base = split_rate_unit(config.get("hashrate_unit", ""))
        if hashrate is not None and scale is not None:
            result.update({"hashrate": hashrate, "hashrate_unit": config["hashrate_unit"],
                           "hashrate_base": hashrate * scale, "hashrate_base_unit": base,
                           "hashrate_window": config.get("hashrate_window", "矿池窗口")})
    if config.get("shares_path"):
        result["shares"] = nonnegative_number(get_path(data, config["shares_path"]))
    if config.get("last_seen_path"):
        seen = parse_timestamp(get_path(data, config["last_seen_path"]))
        result["last_seen"] = seen if seen is not None and seen <= fetched_at + 300 else None
    return result


def parse_daily(data, config, fetched_at):
    """矿池按日入账序列；只保留日期合法、金额非负的行，按日期升序给出最近 30 天。"""
    rows = get_path(data, config["rows_path"])
    if not isinstance(rows, list):
        return None
    divisor = float(config.get("divisor", 1))
    days = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row.get(config["day_field"])
        amount = nonnegative_number(row.get(config["amount_field"]))
        if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and amount is not None:
            days[day] = amount / divisor
    if not days:
        return None
    ordered = [{"day": day, "amount": days[day]} for day in sorted(days)[-30:]]
    return {"unit": config["unit"], "days": ordered, "source": config["source"],
            "source_url": config.get("link"), "fetched_at": fetched_at,
            "day_timezone": config.get("day_timezone", "UTC")}


def parse_network(data, config, fetched_at):
    """全网算力、出块与矿池占比；任何一项缺失都只是该项为 None，不拿别的数补。"""
    hashrate = positive_number(finite_number(get_path(data, config["hashrate_path"])))
    if hashrate is None:
        return None
    scale, base = split_rate_unit(config["hashrate_unit"])

    def number(key, positive=True):
        if not config.get(key):
            return None
        value = finite_number(get_path(data, config[key]))
        return value if value is not None and (value > 0 if positive else value >= 0) else None

    reward = number("block_reward_path")
    block_seconds = number("block_seconds_path") or positive_number(config.get("block_seconds"))
    pool_fee = number("pool_fee_percent_path", positive=False)
    if pool_fee is None or pool_fee >= 100:  # 费率读不到或离谱时退回配置里写明的数
        pool_fee = float(config.get("pool_fee_percent", 0))
    hashrate_24h = number("hashrate_24h_path")
    pool_hashrate = number("pool_hashrate_path")
    pool_hashrate_24h = number("pool_hashrate_24h_path")
    difficulty = number("difficulty_path")
    result = {"hashrate": hashrate * scale, "hashrate_unit": base,
              "hashrate_24h": hashrate_24h * scale if hashrate_24h else None,
              "difficulty": difficulty, "height": number("height_path"),
              # 难度就是“平均多少次哈希出一块”的链：按难度算产出，比用短窗口实测的出块间隔稳得多
              "difficulty_hashes": difficulty if config.get("difficulty_unit") == "hashes_per_block" else None,
              "block_reward": (reward / float(config.get("reward_divisor", 1)) if reward
                               else positive_number(config.get("block_reward"))),
              "block_reward_note": None if reward else config.get("block_reward_note"),
              "block_seconds": block_seconds,
              "target_block_seconds": number("target_block_seconds_path") or positive_number(config.get("target_block_seconds")),
              "pool_hashrate": pool_hashrate * scale if pool_hashrate else None,
              # 矿池自己给出“按现在的难度一天该出多少块”和同一窗口的池算力时，本机产出按占矿池的份额算，
              # 不靠写死的出块间隔（全网算力变得快时，实际出块快慢会偏离协议目标）
              "pool_expected_blocks_24h": number("pool_expected_blocks_24h_path"),
              "pool_hashrate_24h": pool_hashrate_24h * scale if pool_hashrate_24h else None,
              "pool_fee_percent": pool_fee, "extra_fee_percent": float(config.get("extra_fee_percent", 0)),
              "luck_percent": number("luck_path"),
              "yield_scale": float(config.get("yield_scale", 1)), "calibrated": bool(config.get("calibrated", False)),
              "volatile": bool(config.get("volatile", False)), "yield_note": config.get("yield_note"),
              "source": config["source"], "source_url": config.get("link", config["url"]), "fetched_at": fetched_at}
    if config.get("timestamp_path"):
        source_at = parse_timestamp(get_path(data, config["timestamp_path"]))
        if source_at is None or source_at > fetched_at + 300:
            return None
        result["source_at"] = source_at
    return result


def parse_leaderboard(data, fetched_at, limit=12):
    """WhatToMine 公开榜单：按其默认算力口径的相对收益指数，只用于发现线索，不代表本机收益。"""
    coins = data.get("coins") if isinstance(data, dict) else None
    if not isinstance(coins, dict):
        return None
    rows = []
    for name, row in coins.items():
        if not isinstance(row, dict) or not isinstance(name, str):
            continue
        index = nonnegative_number(row.get("profitability"))
        tag = row.get("tag")
        if index is None or not isinstance(tag, str) or row.get("lagging") is True:
            continue
        rows.append({"name": name[:40], "tag": tag[:16], "algorithm": str(row.get("algorithm", ""))[:32],
                     "index": index, "index_24h": nonnegative_number(row.get("profitability24")),
                     "nethash": nonnegative_number(row.get("nethash")),
                     "timestamp": nonnegative_number(row.get("timestamp"))})
    if not rows:
        return None
    rows.sort(key=lambda row: -row["index"])
    for position, row in enumerate(rows, 1):
        row["rank"] = position
    return {"rows": rows[:limit], "all_tags": {row["tag"].upper(): row for row in rows}, "total": len(rows),
            "source": "WhatToMine 公开榜单（其默认算力口径）", "source_url": "https://whattomine.com/coins",
            "fetched_at": fetched_at}


def parse_earnings(data, config, fetched_at):
    base = {"source": config["source"], "source_url": config.get("link"), "fetched_at": fetched_at}
    if config["parser"] == "tsc_wallet_sats":
        wallet = data.get("wallet") if isinstance(data, dict) else None
        if not isinstance(wallet, dict):
            return None
        names = ("SettledSats", "PaidSats", "BalanceSats", "ImmatureSats")
        values = [wallet.get(name) for name in names]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            return None
        settled, paid, balance, immature = values
        if settled != paid + balance:
            return None
        return {**base, "mode": "settled", "unit": "TSC", "settled": settled / 1e8,
                "paid": paid / 1e8, "balance": balance / 1e8, "immature": immature / 1e8}
    transactions = data.get("balance_transactions") if isinstance(data, dict) else None
    if not isinstance(transactions, list):
        return None
    credits = []
    payouts = []
    valid_timestamps = []
    for row in transactions:
        if not isinstance(row, dict) or row.get("coin_type") != "pearl":
            continue
        timestamp = row.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            continue
        timestamp = timestamp / 1000
        amount = row.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
            continue
        reason = row.get("reason")
        if not isinstance(reason, str):
            continue
        if reason.endswith(" credit") and amount > 0:
            credits.append((amount, timestamp))
            valid_timestamps.append(timestamp)
        elif amount < 0:
            payouts.append(-amount)
            valid_timestamps.append(timestamp)
    cutoff = fetched_at - PROFIT_WINDOW_SECONDS
    credit_24h = sum(amount for amount, timestamp in credits if timestamp >= cutoff)
    oldest_record_at = min(valid_timestamps, default=None)
    return {**base, "mode": "returned_credits", "unit": "PRL", "credit_total": sum(row[0] for row in credits),
            "credit_count": len(credits), "returned_count": len(transactions),
            "first_at": min((row[1] for row in credits), default=None),
            "last_at": max((row[1] for row in credits), default=None),
            "credit_24h": credit_24h,
            # 接口只给流水：入账合计减打款合计是还在矿池里的数；流水若被接口截断，这两个数会不准
            "paid": sum(payouts), "payout_count": len(payouts),
            "balance": max(0.0, sum(row[0] for row in credits) - sum(payouts)),
            "credit_24h_covered": oldest_record_at is not None and oldest_record_at <= cutoff,
            "oldest_record_at": oldest_record_at}


def run_command(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=4, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def run_user_systemctl(*args, timeout=25):
    """Run a bounded user-systemd command without a shell."""
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def read_vram_export():
    """Return {uuid: celsius} from the root VRAM exporter; missing, stale or malformed data yields {}."""
    try:
        data = json.loads(VRAM_EXPORT_PATH.read_text(encoding="utf-8"))
        if time.time() - float(data.get("sampled_at", 0)) > VRAM_EXPORT_MAX_AGE:
            return {}
        result = {}
        for row in data.get("gpus", []):
            value = float(row.get("vram_c"))
            if math.isfinite(value) and -20 <= value <= 150:
                result[str(row.get("uuid"))] = value
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def gpu_status():
    raw = run_command("nvidia-smi", "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.gr,clocks.mem,temperature.memory",
                      "--format=csv,noheader,nounits")
    if not raw or any(len(next(csv.reader([line]))) != 13 for line in raw.splitlines() if line.strip()):
        raw = run_command("nvidia-smi", "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.gr,clocks.mem",
                          "--format=csv,noheader,nounits")
    gpus = []
    for fields in csv.reader(raw.splitlines()):
        if len(fields) not in {12, 13}:
            continue
        index, uuid, name, util, used, capacity, temp, power, limit, fan, core_clock, mem_clock = (x.strip() for x in fields[:12])
        try:
            numbers = [float(x) for x in (util, used, capacity, temp, power, limit)]
            if not all(math.isfinite(x) for x in numbers):
                continue
            optional = []
            for value in (fan, core_clock, mem_clock):
                try:
                    number = float(value)
                    optional.append(number if math.isfinite(number) else None)
                except ValueError:
                    optional.append(None)
            memory_temperature = None
            if len(fields) == 13:
                try:
                    value = float(fields[12].strip())
                    if math.isfinite(value) and -20 <= value <= 150:
                        memory_temperature = value
                except ValueError:
                    pass
            gpus.append({"index": int(index), "uuid": uuid, "name": name, "utilization": numbers[0], "memory_used": numbers[1],
                         "memory_total": numbers[2], "temperature": numbers[3], "power": numbers[4], "power_limit": numbers[5],
                         "memory_temperature": memory_temperature, "memory_temperature_supported": memory_temperature is not None,
                         "fan_percent": optional[0], "core_clock": optional[1], "memory_clock": optional[2]})
        except ValueError:
            continue
    exported = read_vram_export() if any(gpu["memory_temperature"] is None for gpu in gpus) else {}
    for gpu in gpus:
        if gpu["memory_temperature"] is None and gpu["uuid"] in exported:
            gpu["memory_temperature"] = exported[gpu["uuid"]]
            gpu["memory_temperature_supported"] = True
            gpu["memory_temperature_source"] = "GDDR6X 寄存器（非官方）"
    return decorate_gpus(gpus)


def hardware_status():
    """Read fan channels and a small set of labelled temperatures without guessing wiring."""
    fans = []
    temperatures = []
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            chip = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        for sensor in sorted(hwmon.glob("fan*_input")):
            try:
                rpm = int(sensor.read_text().strip())
                label_path = sensor.with_name(sensor.name.replace("_input", "_label"))
                label = label_path.read_text().strip() if label_path.exists() else "FAN " + re.search(r"fan(\d+)", sensor.name).group(1)
                if 0 <= rpm <= 100000:
                    fans.append({"chip": chip, "channel": sensor.stem, "label": label, "rpm": rpm})
            except (OSError, ValueError):
                continue
        if chip not in {"coretemp", "nct6798", "nvme"}:
            continue
        for sensor in sorted(hwmon.glob("temp*_input")):
            try:
                label_path = sensor.with_name(sensor.name.replace("_input", "_label"))
                label = label_path.read_text().strip() if label_path.exists() else sensor.stem
                if (chip, label) not in {("coretemp", "Package id 0"), ("nct6798", "SYSTIN"), ("nvme", "Composite")}:
                    continue
                celsius = int(sensor.read_text().strip()) / 1000
                if -20 <= celsius <= 150:
                    temperatures.append({"chip": chip, "label": label, "celsius": celsius})
            except (OSError, ValueError):
                continue
    return {"fans": fans, "temperatures": temperatures}


def nvidia_fan_rpm():
    """NV-CONTROL exposes RPM channels, but their physical GPU mapping is unspecified."""
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY") or ":0",
           "XAUTHORITY": os.environ.get("XAUTHORITY") or str(Path.home() / ".Xauthority")}
    try:
        targets = subprocess.run(["nvidia-settings", "-q", "fans"], capture_output=True, text=True,
                                 timeout=3, check=False, env=env).stdout
        ids = sorted({int(x) for x in re.findall(r"\[fan:(\d+)\]", targets)})[:16]
        if not ids:
            return []
        args = ["nvidia-settings", "-t"]
        for fan_id in ids:
            args.extend(["-q", f"[fan:{fan_id}]/GPUCurrentFanSpeedRPM"])
        raw = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False, env=env).stdout
        values = raw.splitlines()
        if len(values) != len(ids):
            return []
        return [{"channel": fan_id, "rpm": int(value)} for fan_id, value in zip(ids, values) if value.isdecimal()]
    except (OSError, subprocess.SubprocessError):
        return []


class TuneUnavailable(Exception):
    """gpu-tune 助手缺失、超时、输出异常或执行失败；映射为 HTTP 503。"""


def strict_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def tune_helper_argv(body):
    """把 /api/tune 请求体映射为 gpu-tune 参数；只做形状与类型校验，越界由 root 助手最终裁决。"""
    if not isinstance(body, dict):
        raise ValueError("请求体格式无效")
    kind = body.get("kind")
    action = body.get("action")
    if kind not in GPU_TUNE_VALUE_BOUNDS:
        raise ValueError("kind 只允许 gpu_clock、gpu_power 或 fan")
    if action not in {"set", "reset"}:
        raise ValueError("action 只允许 set 或 reset")
    target_key = "group" if kind == "fan" else "gpu"
    allowed = {"kind", "action", target_key, "value"}
    unknown = [key for key, value in body.items() if key not in allowed and value is not None]
    if unknown:
        raise ValueError("请求包含不支持的字段：" + "、".join(sorted(str(key) for key in unknown))[:120])
    if kind == "fan":
        group = body.get("group")
        if group not in GPU_TUNE_FAN_GROUPS:
            raise ValueError("风扇分组只允许 cpu 或 gpu_ext")
        target = ["--group", group]
    else:
        gpu = body.get("gpu")
        if not strict_int(gpu) or not 0 <= gpu <= GPU_TUNE_MAX_GPU_INDEX:
            raise ValueError("gpu 必须是有效的显卡编号（整数）")
        target = ["--gpu", str(gpu)]
    value = body.get("value")
    if action == "reset":
        if value is not None:
            raise ValueError("恢复默认时不接受 value")
        verb = {"gpu_clock": "reset-clock", "gpu_power": "reset-power", "fan": "reset-fan"}[kind]
        return [verb, *target]
    low, high = GPU_TUNE_VALUE_BOUNDS[kind]
    if not strict_int(value) or not low <= value <= high:
        raise ValueError(f"value 必须是 {low}–{high} 之间的整数")
    verb, flag = {"gpu_clock": ("set-clock", "--mhz"), "gpu_power": ("set-power", "--watts"),
                  "fan": ("set-fan", "--percent")}[kind]
    return [verb, *target, flag, str(value)]


def run_gpu_tune(argv, timeout):
    """经 GPU_TUNE_SOCKET 执行一次 gpu-tune 写操作，返回形同 subprocess.run 的结果（退出码 + 助手 JSON 输出）。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(GPU_TUNE_SOCKET)
            conn.sendall(json.dumps({"argv": list(argv)}).encode() + b"\n")
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # 请求以换行结束，服务端读完一行可能已先回复并关闭；半关闭只是礼节，照常读回复
            chunks, size = [], 0
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 1_000_000:
                    raise OSError("调节服务响应过大")
                chunks.append(chunk)
        except socket.timeout as error:
            raise subprocess.TimeoutExpired(["gpu-tune", *argv], timeout) from error
    reply = parse_tune_output(b"".join(chunks).decode("utf-8", "replace"))
    if reply is None or not isinstance(reply.get("exit_code"), int) or not isinstance(reply.get("output"), str):
        return subprocess.CompletedProcess(argv, 1, "", "调节服务回复无法解析")
    return subprocess.CompletedProcess(argv, reply["exit_code"], reply["output"], "")


def parse_tune_output(text):
    """助手约定只输出一个 JSON 对象；兼容尾部多余行，解析失败返回 None。"""
    text = (text or "").strip()
    for candidate in (text, text.splitlines()[-1] if text else ""):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _tune_failure_detail(result):
    return ((result.stderr or result.stdout or "").strip() or f"退出码 {result.returncode}")[:200]


def read_tuning_status():
    """以普通用户身份读取 gpu-tune status；任何失败都返回 ok:false 结构，不抛异常。"""
    try:
        result = subprocess.run([GPU_TUNE_PATH, "status"], capture_output=True, text=True,
                                timeout=GPU_TUNE_STATUS_TIMEOUT, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": f"调节服务不可用：未安装 {GPU_TUNE_PATH}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "调节服务不可用：状态查询超时"}
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"调节服务不可用：{error}"[:240]}
    data = parse_tune_output(result.stdout)
    if data is None:
        return {"ok": False, "error": "调节服务不可用：输出无法解析（" + _tune_failure_detail(result) + "）"}
    if data.get("ok") is not True:
        error = data.get("error")
        return {"ok": False, "error": "调节服务不可用：" + (str(error)[:200] if error else "状态查询失败")}
    return data


def exchange_connection_status():
    """Public preflight only. Never sends exchange credentials from this monitor."""
    checked_at = time.time()
    try:
        result = subprocess.run(["curl", "-sS", "-o", "/dev/null",
                                 "-w", "%{http_code} %{content_type}", "--max-time", "9", EXCHANGE_PUBLIC_URL],
                                capture_output=True, text=True, timeout=11, check=False)
        parts = result.stdout.strip().split(" ", 1)
        code = int(parts[0]) if parts and parts[0].isdecimal() else None
        ready = result.returncode == 0 and code == 200 and len(parts) > 1 and "json" in parts[1].lower()
        return {"state": "public_api_ready" if ready else "upstream_blocked" if code == 403 else "unavailable",
                "http_status": code, "checked_at": checked_at}
    except (OSError, subprocess.SubprocessError):
        return {"state": "unavailable", "http_status": None, "checked_at": checked_at}


def service_status(unit):
    value = run_command("systemctl", "--user", "is-active", unit)
    return value if value in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def coin_runtime(coin):
    """返回 (服务状态, 实际在挖的配置)。NOID 每张卡一个矿工服务：任一在跑即算在挖，显卡只算在跑的那几张。"""
    if coin.get("gpu_uuid_services"):
        from per_gpu_miner import inventory
        try:
            current = inventory()
        except (OSError, ValueError, subprocess.SubprocessError):
            return "unknown", {**coin, "gpu_indices": [], "gpu_services": {}}
        services = {str(current[u]["index"]): unit for u,unit in coin["gpu_uuid_services"].items() if u in current}
        if not services:
            return "inactive", {**coin, "gpu_indices": [], "gpu_services": {}}
        coin = {**coin, "gpu_indices": [int(i) for i in services], "gpu_services": services,
                "rate": {**coin["rate"], "single_gpu_logs": True,
                         "log_by_gpu": {str(current[u]["index"]): coin["rate"]["log_by_uuid"][u]
                                        for u in coin["gpu_uuid_services"] if u in current}}}
    gpu_services = coin.get("gpu_services")
    if gpu_services:
        indices = coin["gpu_indices"]
        states = {index: service_status(gpu_services[str(index)]) for index in indices}
        running = [index for index in indices if states[index] == "active"]
        if not running:
            return ("failed" if "failed" in states.values() else service_status(coin["service"])), coin
        if running == indices:
            return "active", coin
        partial = {**coin, "gpu_indices": running}
        if coin.get("profitability"):  # 主机估算功耗按在挖的卡数分摊，另一张卡可能在挖别的币
            share = len(running) / len(indices)
            partial["profitability"] = {**coin["profitability"],
                                        "base_system_w": coin["profitability"]["base_system_w"] * share}
        return "active", partial
    status = service_status(coin["service"])
    indices = coin.get("gpu_indices", [])
    if coin.get("kind") != "cpu_gpu" or len(indices) < 2:
        return status, coin
    running = [index for index in indices
               if (status if NOID_GPU_MINERS[index]["service"] == coin["service"]
                   else service_status(NOID_GPU_MINERS[index]["service"])) == "active"]
    if not running or running == indices:
        return ("active" if running else status), coin
    partial = {**coin, "gpu_indices": running}
    if coin.get("profitability"):  # 主机估算功耗按在挖的卡数分摊，另一张卡可能在挖别的币
        share = len(running) / len(indices)
        partial["profitability"] = {**coin["profitability"],
                                    "base_system_w": coin["profitability"]["base_system_w"] * share}
    return "active", partial


def noid_control(*args):
    result = subprocess.run((NOID_CONTROL_PATH, *args), capture_output=True, text=True,
                            timeout=35, check=False)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("NOID 线程控制未返回有效状态") from error
    if result.returncode or not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(str(data.get("error", "NOID 线程控制失败"))[:240])
    return data


def parse_noid_journal(raw, pid, now):
    rows = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            stamp = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
            message = row["MESSAGE"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(message, str):
            rows.append((stamp, message))
    starts = [index for index, (_, message) in enumerate(rows)
              if "INVMINER" in message and "2 MIN STATUS" in message]
    if not starts:
        return {"status": "正在采样，启动或调整线程后约需2分钟", "pid": pid}
    block = rows[starts[-1]:]
    sampled_at = block[0][0]
    age = max(0, now - sampled_at)
    if age > NOID_JOURNAL_MAX_AGE or sampled_at > now + 10:
        return {"status": "采样已过期", "pid": pid, "sampled_at": sampled_at,
                "age_seconds": round(age)}
    content = "\n".join(message for _, message in block)
    rate = re.search(r"Total Hashrate \(2m\)\s+([\d.]+)\s+MH/s", content)
    shares = re.search(r"A/S/R Total\s+(\d+)/(\d+)/(\d+)", content)
    if not rate or not shares:
        return {"status": "状态采样未完整", "pid": pid, "sampled_at": sampled_at,
                "age_seconds": round(age)}
    return {"status": "正常", "pid": pid, "sampled_at": sampled_at,
            "age_seconds": round(age), "hashrate": float(rate.group(1)), "unit": "MH/s",
            "window": "矿工滚动 120 秒", "accepted": int(shares.group(1)),
            "stale": int(shares.group(2)), "rejected": int(shares.group(3))}


def noid_telemetry(pid, now):
    if not isinstance(pid, int) or pid <= 0:
        return {"status": "进程未运行"}
    try:
        result = subprocess.run(("journalctl", "--user", "-u", "noid-cpu-miner.service",
                                 f"_PID={pid}", "--since=-6min", "-n", "120", "-o", "json", "--no-pager"),
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "journal 不可读", "pid": pid}
    if result.returncode:
        return {"status": "journal 不可读", "pid": pid}
    return parse_noid_journal(result.stdout, pid, now)


def feed_max_age(config, kind, active=True):
    """数据源的有效期；停矿币种按 5 分钟低频抓取，相应放宽到 15 分钟以上。"""
    age = config.get("max_age_seconds", FEED_DEFAULT_MAX_AGE[kind])
    return age if active or kind == "price" else max(age, 3 * INACTIVE_FEED_INTERVAL)


def fetch_json(url):
    opener = build_opener(ProxyHandler({"http": PROXY, "https": PROXY}) if PROXY else ProxyHandler())
    request = Request(url, headers={"User-Agent": "GPU-Mining-Monitor/2.0", "Accept": "application/json"})
    with opener.open(request, timeout=8) as response:
        return json.loads(response.read(1_000_001))


class SeriesStore:
    """只追加的小型 JSONL 时间序列：内存只留窗口期内的行，文件超限时按窗口重写。"""

    def __init__(self, path, keep_seconds, max_bytes=HISTORY_MAX_BYTES):
        self.path = Path(path)
        self.keep_seconds = keep_seconds
        self.max_bytes = max_bytes
        self.rows = deque()
        self.lock = threading.Lock()

    def load(self, now):
        try:
            with self.path.open("rb") as handle:
                handle.seek(max(0, self.path.stat().st_size - self.max_bytes))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            lines = []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            stamp = row.get("t") if isinstance(row, dict) else None
            if finite_number(stamp) is not None and now - self.keep_seconds <= stamp <= now + 60:
                rows.append(row)
        rows.sort(key=lambda row: row["t"])
        with self.lock:
            self.rows = deque(rows)

    def append(self, row):
        with self.lock:
            self.rows.append(row)
            while self.rows and self.rows[0]["t"] < row["t"] - self.keep_seconds:
                self.rows.popleft()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    temporary = self.path.with_suffix(".tmp")
                    with temporary.open("w", encoding="utf-8") as handle:
                        os.chmod(temporary, 0o600)
                        for item in self.rows:
                            handle.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    temporary.replace(self.path)
                else:
                    with self.path.open("a", encoding="utf-8") as handle:
                        os.chmod(self.path, 0o600)
                        handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            except (OSError, ValueError) as error:
                print("HISTORY_WRITE_ERROR " + str(error)[:160], flush=True)

    def snapshot(self):
        with self.lock:
            return list(self.rows)


def pct_change(current, previous):
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100


def series_value_at(rows, key, target, tolerance):
    """取离目标时刻最近、且在容差内的一行的字段值；没有则 None。"""
    best = None
    for row in rows:
        value = row.get(key)
        if value is None or abs(row["t"] - target) > tolerance:
            continue
        if best is None or abs(row["t"] - target) < abs(best["t"] - target):
            best = row
    return best.get(key) if best else None


class Monitor:
    def __init__(self, configs, state_dir=None):
        self.configs = configs
        market_path = CONFIG_DIR / "market-public.json"
        public_rows = json.loads(market_path.read_text()) if market_path.exists() else []
        existing = {coin["symbol"] for coin in configs if coin.get("kind") != "cpu"}
        allowed = {"symbol", "name", "algorithm", "color", "network", "pools", "price", "whattomine_tag", "market_note", "links", "rate"}
        self.market_configs = []
        for row in public_rows:
            if row["symbol"] not in existing:
                public = {key: value for key, value in row.items() if key in allowed}
                public.update(service="market-only", gpu_indices=[], watch_only=True)
                public["rate"] = {"unit": public["rate"]["unit"]}
                self.market_configs.append(public)
        self.rig = RigController(Path(os.environ.get("MINING_RIG_CONFIG", CONFIG_DIR / "rig-control.json")),
                                 (Path(state_dir) if state_dir else DEFAULT_STATE_DIR) / "rig-control")
        self.gpu_configs = [coin for coin in configs if coin.get("kind") != "cpu"]
        self.noid_config = next((coin for coin in configs if coin.get("kind") in {"cpu", "cpu_gpu"}), None)
        self.noid_lock = threading.Lock()
        self.cpu_sample = None
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.notices = NoticeChecker(self.state_dir / "rig-control")
        self.thermal_log_path = self.state_dir / "thermal-monitor.jsonl"
        self.operation_log_path = self.state_dir / "mining-switch.jsonl"
        self.earnings_history_path = self.state_dir / "tsc-earnings.jsonl"
        self.profit_settings_path = self.state_dir / "profit-settings.json"
        self.tune_log_path = self.state_dir / "gpu-tuning.jsonl"
        self.tune_lock = threading.Lock()
        self.tune_status_lock = threading.Lock()
        self.tuning_cache = None
        self.tuning_cached_at = 0
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.thermal_lock = threading.Lock()
        self.switch_lock = threading.Lock()
        self.project_active_since = self._load_project_active_since()
        self.profit_alert_symbol = None
        self.profit_threshold_usd = 0.50
        self.last_profit_checked_at = 0
        self.last_profit_current_symbol = None
        self.last_profit_recommendation = None
        try:
            settings = json.loads(self.profit_settings_path.read_text(encoding="utf-8"))
            threshold = positive_number(settings.get("threshold_usd_per_day")) if isinstance(settings, dict) else None
            if threshold is not None and threshold <= 100:
                self.profit_threshold_usd = threshold
        except (OSError, json.JSONDecodeError):
            pass
        self.rate_states = {}
        self.feeds = {}
        self.feed_fingerprints = {}  # 仅存抓取目标的摘要；不把账户地址写入收益历史
        self.fx = None
        self.fx_checked_at = 0
        self.fx_retry_after = 0
        self.tsc_history_lock = threading.Lock()
        self.thermal_states = {}
        self.latest_gpus = []
        self.latest_thermal_alerts = []
        self.thermal_sampled_at = 0
        self.last_thermal_log_at = 0
        self.thermal_events = deque(maxlen=100)
        self.exchange = None
        self.exchange_client = SafeTradeProxyClient()
        self.order_previews = {}
        self.csrf_token = secrets.token_urlsafe(32)
        self.feed_attempts = {}
        self.feed_errors = {}
        self.feed_failures = {}
        self.leaderboard = None
        self.leaderboard_checked_at = 0
        self.telemetry = SeriesStore(self.state_dir / "telemetry-history.jsonl", TELEMETRY_KEEP_SECONDS)
        self.balances = SeriesStore(self.state_dir / "balance-history.jsonl", SERIES_KEEP_SECONDS)
        self.market = SeriesStore(self.state_dir / "market-history.jsonl", SERIES_KEEP_SECONDS)
        self.history_marks = {"telemetry": 0, "balance": 0, "market": 0}
        self.alert_log_path = self.state_dir / "alerts.jsonl"
        self.alert_lock = threading.Lock()
        self.alert_since = {}
        self.alert_titles = {}
        self.alert_recorded = None
        self.condition_since = {}
        self.manual_prices_path = self.state_dir / "manual-prices.json"
        self.manual_prices = self._load_manual_prices()
        self.reward_samples = {}
        self.calibration_path = self.state_dir / "yield-calibration.json"
        self.calibration_lock = threading.Lock()
        self.calibrations = self._load_calibrations()

    def cpu_usage(self):
        try:
            parts = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
            values = [int(value) for value in parts[1:]]
            total = sum(values)
            idle = values[3] + values[4]
        except (OSError, ValueError, IndexError):
            return None
        with self.noid_lock:
            previous = self.cpu_sample
            self.cpu_sample = (total, idle)
        if not previous or total <= previous[0]:
            return None
        return round(100 * (1 - (idle - previous[1]) / (total - previous[0])), 1)

    def _load_project_active_since(self):
        try:
            with self.operation_log_path.open("rb") as handle:
                handle.seek(max(0, self.operation_log_path.stat().st_size - 512_000))
                rows = handle.read().decode("utf-8", "replace").splitlines()[-2000:]
        except OSError:
            return {}
        for line in reversed(rows):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "switch_completed" and event.get("symbol") in {"TSC", "PRL"}:
                return {event["symbol"]: event.get("timestamp", 0)}
        return {}

    def _append_jsonl(self, path, record):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        with self.log_lock:
            try:  # 只追加的本机日志到 8 MB 就换一个文件，留一份 .1
                if path.stat().st_size > JSONL_MAX_BYTES:
                    path.replace(path.with_name(path.name + ".1"))
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()

    def _record_thermal_event(self, record):
        item = {"timestamp": time.time(), **record}
        self.thermal_events.appendleft(item)
        try:
            self._append_jsonl(self.thermal_log_path, item)
        except OSError as error:
            print("THERMAL_LOG_ERROR " + str(error), flush=True)
        if item.get("type") != "sample":  # 每分钟的常规采样只进本机 jsonl，不再刷系统日志
            print("THERMAL " + json.dumps(item, ensure_ascii=False, allow_nan=False), flush=True)

    def read_thermal_log(self, limit=100):
        try:
            with self.thermal_log_path.open("rb") as handle:
                handle.seek(max(0, self.thermal_log_path.stat().st_size - 512_000))
                raw = handle.read()
        except OSError:
            raw = b""
        lines = raw.decode("utf-8", "replace").splitlines()
        records = []
        for line in lines[-max(1, min(int(limit), 200)):]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"file": self.thermal_log_path.name, "events": list(reversed(records))}

    def set_profit_threshold(self, value):
        threshold = positive_number(value)
        if threshold is None or not 0.01 <= threshold <= 100:
            raise ValueError("阈值须在 $0.01–$100/天之间")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            payload = {"threshold_usd_per_day": round(threshold, 2), "updated_at": time.time()}
            temporary = self.profit_settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.profit_settings_path)
            try:
                os.chmod(self.state_dir, 0o700)
            except OSError:
                pass
            self.profit_threshold_usd = payload["threshold_usd_per_day"]
        self._append_jsonl(self.operation_log_path, {"timestamp": payload["updated_at"], "type": "profit_threshold_changed",
                                                     "threshold_usd_per_day": self.profit_threshold_usd})
        return {"threshold_usd_per_day": self.profit_threshold_usd}

    def record_tsc_earnings(self, earnings, account_fingerprint=None):
        if not isinstance(earnings, dict) or earnings.get("mode") != "settled":
            return
        settled, immature = earnings.get("settled"), earnings.get("immature")
        timestamp = earnings.get("fetched_at")
        if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 for value in (settled, immature)) or
                isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or
                not math.isfinite(timestamp)):
            return
        total = settled + immature
        record = {"timestamp": timestamp, "total": total, "settled": settled, "immature": immature,
                  "account_fingerprint": account_fingerprint}
        with self.tsc_history_lock:
            try:
                if self.earnings_history_path.exists():
                    with self.earnings_history_path.open("rb") as handle:
                        handle.seek(max(0, self.earnings_history_path.stat().st_size - 4096))
                        last = handle.read().decode("utf-8", "replace").splitlines()[-1]
                    previous = json.loads(last)
                    if timestamp <= previous.get("timestamp", 0):
                        return
                    previous_settled = previous.get("settled")
                    # 未成熟余额会回落，累计总额的小幅下降不代表换账户；只有已结算额回退才确认重置。
                    reset = ((account_fingerprint is not None and
                              account_fingerprint != previous.get("account_fingerprint")) or
                             (settled < previous_settled if isinstance(previous_settled, (int, float)) else
                              total < previous.get("total", 0)))
                    if reset:
                        self._append_jsonl(self.earnings_history_path, {**record, "reset": True})
                        return
                self._append_jsonl(self.earnings_history_path, record)
            except (OSError, json.JSONDecodeError, IndexError, TypeError):
                try:
                    self._append_jsonl(self.earnings_history_path, record)
                except OSError as error:
                    print("EARNINGS_HISTORY_ERROR " + str(error), flush=True)

    def tsc_daily_rate(self, now):
        try:
            with self.tsc_history_lock:
                with self.earnings_history_path.open("rb") as handle:
                    handle.seek(max(0, self.earnings_history_path.stat().st_size - 2_000_000))
                    tail = handle.read().decode("utf-8", "replace").splitlines()[-5000:]
                rows = [json.loads(line) for line in tail]
        except (OSError, json.JSONDecodeError):
            rows = []
        last_reset = max((index for index, row in enumerate(rows) if isinstance(row, dict) and row.get("reset")), default=-1)
        rows = [row for row in rows[last_reset + 1:] if isinstance(row, dict)
                and isinstance(row.get("timestamp"), (int, float)) and not isinstance(row.get("timestamp"), bool)
                and math.isfinite(row["timestamp"]) and isinstance(row.get("total"), (int, float))
                and not isinstance(row.get("total"), bool) and math.isfinite(row["total"])]
        rows.sort(key=lambda row: row["timestamp"])
        if not rows:
            return {"coins_per_day": None, "window_seconds": 0, "coverage_seconds": 0}
        newest = rows[-1]
        coverage = max(0, min(PROFIT_WINDOW_SECONDS, now - rows[0]["timestamp"]))
        if now - newest["timestamp"] > 180:
            return {"coins_per_day": None, "window_seconds": 0, "coverage_seconds": coverage, "status": "收益数据过期"}
        cutoff = now - PROFIT_WINDOW_SECONDS
        baseline = next((row for row in reversed(rows) if row["timestamp"] <= cutoff), None)
        if baseline is None:
            return {"coins_per_day": None, "window_seconds": 0, "coverage_seconds": coverage, "status": "24小时样本采集中"}
        elapsed = newest["timestamp"] - baseline["timestamp"]
        delta = newest["total"] - baseline["total"]
        if delta < 0:
            return {"coins_per_day": None, "window_seconds": max(0, elapsed), "coverage_seconds": coverage,
                    "status": "未成熟余额调整后账户净增量为负，收益暂不可用"}
        if elapsed < PROFIT_WINDOW_SECONDS - 180:
            return {"coins_per_day": None, "window_seconds": max(0, elapsed), "coverage_seconds": coverage,
                    "status": "有效收益窗口不足"}
        return {"coins_per_day": delta * PROFIT_WINDOW_SECONDS / elapsed,
                "window_seconds": elapsed, "coverage_seconds": coverage, "status": "正常"}

    @staticmethod
    def _temperature_level(value, warn, critical):
        if value is None:
            return "unavailable"
        if value >= critical:
            return "critical"
        if value >= warn:
            return "warning"
        return "normal"

    def refresh_thermal_sample(self):
        now = time.time()
        gpus = gpu_status()
        alerts = []
        sample_rows = []
        for gpu in gpus:
            index = gpu["index"]
            sensors = (("core", gpu.get("temperature"), GPU_TEMP_WARN_C, GPU_TEMP_CRITICAL_C),
                       ("memory", gpu.get("memory_temperature"), MEMORY_TEMP_WARN_C, MEMORY_TEMP_CRITICAL_C))
            for sensor_name, value, warn, critical in sensors:
                level = self._temperature_level(value, warn, critical)
                key = (index, sensor_name)
                previous = self.thermal_states.get(key)
                self.thermal_states[key] = level
                if sensor_name == "memory" and level == "unavailable":
                    if previous != "unavailable":
                        self._record_thermal_event({"type": "sensor_unavailable", "gpu": index, "sensor": "显存温度",
                                                   "message": "当前 GPU/驱动未提供显存温度读数"})
                    continue
                if level in {"warning", "critical"}:
                    alert = {"gpu_index": index, "sensor": "GPU核心" if sensor_name == "core" else "显存",
                             "severity": level, "celsius": value,
                             "message": f"{gpu.get('display_title', 'GPU '+str(index))} {('核心' if sensor_name == 'core' else '显存')} {value:.0f}°C"}
                    alerts.append(alert)
                if level != previous and (level in {"warning", "critical"} or previous in {"warning", "critical"}):
                    self._record_thermal_event({"type": "temperature_state", "gpu": index,
                                                "sensor": "GPU核心" if sensor_name == "core" else "显存",
                                                "severity": level, "celsius": value,
                                                "threshold_warn_c": warn, "threshold_critical_c": critical,
                                                "message": (f"{gpu.get('display_title', 'GPU '+str(index))} {('核心' if sensor_name == 'core' else '显存')} "
                                                            f"{value:.0f}°C 状态 {level}")})
            sample_rows.append({"gpu": index, "core_c": gpu.get("temperature"),
                                "memory_c": gpu.get("memory_temperature"),
                                "memory_supported": gpu.get("memory_temperature_supported", False),
                                "fan_percent": gpu.get("fan_percent"), "power_w": gpu.get("power")})
        with self.thermal_lock:
            self.latest_gpus = copy.deepcopy(gpus)
            self.latest_thermal_alerts = alerts
            self.thermal_sampled_at = now
        if now - self.last_thermal_log_at >= THERMAL_LOG_INTERVAL:
            self.last_thermal_log_at = now
            self._record_thermal_event({"type": "sample", "gpus": sample_rows})

    def refresh_thermal_forever(self):
        while True:
            try:
                self.refresh_thermal_sample()
            except Exception as error:
                self._record_thermal_event({"type": "sampling_error", "message": str(error)[:240]})
            time.sleep(THERMAL_SAMPLE_INTERVAL)

    def _wait_service_state(self, unit, expected, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = service_status(unit)
            if state == expected:
                return True
            if expected == "inactive" and state == "failed":
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _systemctl_checked(*args):
        try:
            result = run_user_systemctl(*args)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"systemd 命令执行失败：{error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemd 返回失败").strip()[:240]
            raise RuntimeError(detail)
        return result.stdout.strip()

    def _unit_enable_state(self, unit):
        try:
            result = run_user_systemctl("is-enabled", unit)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"无法读取 {unit} 开机状态：{error}") from error
        state = result.stdout.strip()
        if state not in {"enabled", "disabled"}:
            raise RuntimeError(f"{unit} 开机状态无法安全识别：{state or result.stderr.strip()[:120]}")
        return state

    def _restore_enable_states(self, states, units):
        errors = []
        for unit in units:
            desired = states.get(unit)
            if desired not in {"enabled", "disabled"}:
                continue
            try:
                current = self._unit_enable_state(unit)
                if current != desired:
                    self._systemctl_checked(desired, unit)
            except RuntimeError as error:
                print(f"MINER_SWITCH_ROLLBACK_WARNING {unit}: {error}", flush=True)
                errors.append(f"{unit} 开机状态未能恢复")
        return errors

    def switch_project(self, symbol):
        target = next((item for item in self.configs if item["symbol"] == symbol), None)
        if target is None or symbol not in {"TSC", "PRL"}:
            raise ValueError("只允许切换到已配置的 TSC 或 PRL")
        units = [item["service"] for item in self.configs if item["symbol"] in {"TSC", "PRL"}]
        if len(units) != 2 or target["service"] not in units:
            raise ValueError("TSC/PRL 服务配置不完整，拒绝切换")

        with self.switch_lock:
            if (time.time() - self.last_profit_checked_at > 15 or
                    self.last_profit_recommendation != symbol or self.last_profit_current_symbol is None):
                raise RuntimeError("最近一次收益对比未达到提醒阈值或已过期；请刷新大屏后再试")
            states = {item["service"]: service_status(item["service"])
                      for item in self.configs if item["symbol"] in {"TSC", "PRL"}}
            active = [unit for unit, state in states.items() if state == "active"]
            if len(active) != 1:
                raise RuntimeError("当前挖矿服务不是单一 active 状态，拒绝切换并保留现场")
            source = active[0]
            destination = target["service"]
            source_symbol = next((item["symbol"] for item in self.configs if item["service"] == source), None)
            if source_symbol != self.last_profit_current_symbol:
                raise RuntimeError("当前运行币种已变化；请刷新大屏重新比较收益")
            if source == destination:
                return {"status": "already_active", "symbol": symbol, "service": destination}
            if states[destination] not in {"inactive", "failed"}:
                raise RuntimeError(f"目标矿工状态为 {states[destination]}，拒绝切换")
            enabled = {unit: self._unit_enable_state(unit) for unit in units}
            started_at = time.time()
            record = {"timestamp": started_at, "type": "switch_started", "from": source, "to": destination}
            self._append_jsonl(self.operation_log_path, record)
            try:
                self._systemctl_checked("stop", source)
                if not self._wait_service_state(source, "inactive"):
                    raise RuntimeError(f"{source} 停止超时；未启动 {destination}")
                if service_status(destination) == "failed":
                    self._systemctl_checked("stop", destination)
                self._systemctl_checked("start", destination)
                if not self._wait_service_state(destination, "active"):
                    raise RuntimeError(f"{destination} 启动后未进入 active")
                self._systemctl_checked("enable", destination)
                self._systemctl_checked("disable", source)
                if service_status(source) != "inactive" or service_status(destination) != "active":
                    raise RuntimeError("切换后服务状态与预期不符")
                self.project_active_since[symbol] = time.time()
                completed = {"timestamp": time.time(), "type": "switch_completed", "from": source,
                             "to": destination, "symbol": symbol}
                self._append_jsonl(self.operation_log_path, completed)
                return {"status": "switched", "from": source, "to": destination, "symbol": symbol,
                        "completed_at": completed["timestamp"]}
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                rollback_errors = []
                try:
                    if service_status(destination) in {"active", "activating", "failed"}:
                        self._systemctl_checked("stop", destination)
                    if not self._wait_service_state(destination, "inactive"):
                        rollback_errors.append(f"{destination} 未能停止")
                except RuntimeError as rollback_error:
                    rollback_errors.append(str(rollback_error))
                try:
                    if service_status(source) != "active":
                        self._systemctl_checked("start", source)
                    if not self._wait_service_state(source, "active"):
                        rollback_errors.append(f"原矿工 {source} 未能恢复 active")
                except RuntimeError as rollback_error:
                    rollback_errors.append(str(rollback_error))
                rollback_errors.extend(self._restore_enable_states(enabled, units))
                failure = {"timestamp": time.time(), "type": "switch_failed", "from": source,
                           "to": destination, "error": str(error)[:240], "rollback_errors": rollback_errors}
                self._append_jsonl(self.operation_log_path, failure)
                suffix = ("；回退异常：" + "；".join(rollback_errors)) if rollback_errors else "；原矿工已恢复"
                raise RuntimeError(f"切换失败：{error}{suffix}") from error

    def _store_tuning(self, value):
        with self.tune_status_lock:
            self.tuning_cache = copy.deepcopy(value)
            self.tuning_cached_at = time.monotonic()

    def tuning_status(self):
        """gpu-tune status 结果缓存 ≤5 秒；失败结果同样缓存，避免每次轮询都拉起助手。"""
        with self.tune_status_lock:
            if self.tuning_cache is None or time.monotonic() - self.tuning_cached_at >= GPU_TUNE_STATUS_TTL:
                self.tuning_cache = read_tuning_status()
                self.tuning_cached_at = time.monotonic()
            return copy.deepcopy(self.tuning_cache)

    def apply_tuning(self, body):
        """经 root 套接字调用 gpu-tune 执行一次调节；参数错误抛 ValueError，执行失败抛 TuneUnavailable。"""
        argv = tune_helper_argv(body)
        target_key = "group" if body["kind"] == "fan" else "gpu"
        with self.tune_lock:
            started_at = time.time()
            record = {"timestamp": started_at, "type": "gpu_tune", "kind": body["kind"], "action": body["action"],
                      target_key: body.get(target_key), "value": body.get("value"), "argv": argv}
            try:
                try:
                    result = run_gpu_tune(argv, GPU_TUNE_WRITE_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise TuneUnavailable(f"调节命令 {GPU_TUNE_WRITE_TIMEOUT} 秒未返回；请刷新确认实际状态") from error
                except (OSError, subprocess.SubprocessError) as error:
                    raise TuneUnavailable(f"无法执行调节服务：{error}"[:240]) from error
                data = parse_tune_output(result.stdout)
                if data is not None and data.get("ok") is False and result.returncode == 2:
                    raise ValueError(str(data.get("error") or "调节参数被拒绝")[:240])
                if data is None or data.get("ok") is not True or result.returncode != 0:
                    detail = str(data.get("error"))[:200] if data and data.get("error") else _tune_failure_detail(result)
                    raise TuneUnavailable(f"调节失败：{detail}")
                status = data.get("status")
                if isinstance(status, dict):
                    self._store_tuning(status)
                else:
                    with self.tune_status_lock:
                        self.tuning_cache = None
                    status = self.tuning_status()
                outcome = {"status": "ok", "message": str(data.get("message") or "调节已完成")[:240], "tuning": status}
                record.update({"ok": True, "message": outcome["message"]})
                return outcome
            except (ValueError, TuneUnavailable) as error:
                record.update({"ok": False, "error": str(error)[:240]})
                raise
            finally:
                record["duration_s"] = round(time.time() - started_at, 3)
                try:
                    self._append_jsonl(self.tune_log_path, record)
                except OSError as error:
                    # 调节可能已在主机上生效，日志写入失败不改写结果；助手自身另有 /var/log/gpu-tune.log。
                    print("TUNE_LOG_ERROR " + str(error), flush=True)

    def _profit_candidate(self, coin, feeds, now, gpus, services, fx):
        profile = coin.get("profitability")
        if not profile:
            return {"symbol": coin["symbol"], "name": coin["name"], "status": "未配置收益参数",
                    "net_usd_per_day": None, "reason": "缺少功耗配置"}
        price = feeds.get((coin["symbol"], "price"))
        price_config = coin.get("price", {})
        if (not isinstance(price, dict) or now - price.get("fetched_at", 0) > price_config.get("max_age_seconds", 150) or
                ("source_at" in price and now - price["source_at"] > price_config.get("max_source_age_seconds", 600))):
            price = None
        earnings = feeds.get((coin["symbol"], "earnings"))
        earnings_config = coin.get("earnings", {})
        if not isinstance(earnings, dict) or now - earnings.get("fetched_at", 0) > earnings_config.get("max_age_seconds", 180):
            earnings = None
        service = services.get(coin["symbol"])
        gpu_indices = coin.get("gpu_indices", [])
        live_by_index = {gpu["index"]: gpu for gpu in gpus}
        if service == "active":
            selected = [live_by_index.get(index) for index in gpu_indices]
            board_power = sum(gpu["power"] for gpu in selected if gpu is not None)
            power_w = board_power + profile["base_system_w"] if selected and all(selected) else None
            power_basis = "驱动实时板卡功耗 + 估算的主机其余功耗"
        else:
            power_map = profile["gpu_board_w_per_gpu"]
            power_w = sum(float(power_map[str(index)]) for index in gpu_indices) + profile["base_system_w"]
            power_basis = profile["power_basis"]

        daily = None
        yield_source = None
        yield_status = "收益源不可用 / 已过期"
        yield_scale = 1.0
        estimate_type = "observed"
        if coin["symbol"] == "TSC":
            daily_data = self.tsc_daily_rate(now)
            if daily_data.get("coins_per_day") is None:
                yield_status = daily_data.get("status", "24小时样本采集中")
            elif (nonnegative_number(daily_data.get("coins_per_day")) is not None and service == "active" and
                    self.credit_window_matches_gpus(coin["symbol"], now, gpu_indices, gpus,
                                                    self.feed_fingerprints.get((coin["symbol"], "earnings")))):
                daily = daily_data["coins_per_day"]
                yield_status = daily_data.get("status", "正常")
            else:
                yield_status = "近24小时账户结算无法归属当前显卡组合"
            yield_source = "Tiger Pool 累计钱包余额增量（含待成熟）"
            coverage = daily_data.get("coverage_seconds", 0) if daily is not None else 0
        elif coin["symbol"] == "PRL":
            covered = earnings and earnings.get("mode") == "returned_credits" and earnings.get("credit_24h_covered")
            if covered:
                amount = earnings.get("credit_24h")
                if (nonnegative_number(amount) is not None and service == "active" and
                        self.credit_window_matches_gpus(coin["symbol"], now, gpu_indices, gpus,
                                                        self.feed_fingerprints.get((coin["symbol"], "earnings")))):
                    # credit 是账户合计；只有整个窗口都是同一组卡在挖，才可用作该组卡的实记。
                    daily = float(amount)
                    yield_status = "正常；同一组显卡完整近24小时账户 credit"
                    yield_source = "Pearlhash 近24小时奖励 credit"
                    coverage = PROFIT_WINDOW_SECONDS
                else:
                    coverage = 0
                    yield_status = "近24小时账户 credit 无法归属当前显卡组合"
            else:
                coverage = 0
                yield_status = "近24小时奖励记录覆盖不足"
                yield_source = "Pearlhash 近24小时奖励 credit"
        else:
            coverage = 0
            yield_status = "该币种尚未支持日收益口径"

        if service != "active" and daily == 0:
            # 停挖之后流水和余额自然不再增加：入账为 0 说明没有可用的样本，不能当成“去挖它一天挣 0”
            daily, coverage = None, 0
            yield_status = "已停挖，近24小时没有入账，不能当候选产出"
        fx_rate = fx.get("cny_to_usd") if isinstance(fx, dict) and now - fx.get("fetched_at", 0) <= FX_MAX_AGE else None
        price_value = price.get("value") if price else None
        if (daily is None or price_value is None or fx_rate is None or power_w is None):
            missing = []
            if daily is None:
                missing.append(yield_status)
            if price_value is None:
                missing.append("币价不可用 / 已过期")
            if fx_rate is None:
                missing.append("CNY/USD 汇率不可用 / 已过期")
            if power_w is None:
                missing.append("功耗读数不可用")
            return {"symbol": coin["symbol"], "name": coin["name"], "service": service,
                    "status": "数据采集中", "reason": "；".join(missing), "coins_per_day": daily,
                    "price_usd": price_value, "power_w": power_w, "power_basis": power_basis,
                    "yield_source": yield_source, "yield_status": yield_status, "yield_scale": yield_scale,
                    "estimate_type": estimate_type, "coverage_seconds": coverage,
                    "price_fetched_at": price.get("fetched_at") if price else None,
                    "earnings_fetched_at": earnings.get("fetched_at") if earnings else None,
                    "net_usd_per_day": None, "electricity_cny_per_day": None}
        gross = daily * price_value
        electricity_cny = power_w / 1000 * 24 * ELECTRICITY_CNY_PER_KWH
        electricity_usd = electricity_cny * fx_rate
        net = gross - electricity_usd
        return {"symbol": coin["symbol"], "name": coin["name"], "service": service,
                "status": "正常", "coins_per_day": daily, "price_usd": price_value,
                "gross_usd_per_day": gross, "power_w": power_w, "power_basis": power_basis,
                "electricity_cny_per_day": electricity_cny, "electricity_usd_per_day": electricity_usd,
                "net_usd_per_day": net, "fx_cny_to_usd": fx_rate,
                "fx_rate_date": fx.get("rate_date"), "fx_source": fx.get("source"),
                "yield_source": yield_source, "yield_status": yield_status, "yield_scale": yield_scale,
                "estimate_type": estimate_type, "coverage_seconds": coverage,
                "price_fetched_at": price.get("fetched_at"),
                "earnings_fetched_at": earnings.get("fetched_at") if earnings else None}

    def profitability_snapshot(self, now, gpus, services, feeds, fx, configs=None):
        candidates = [self._profit_candidate(coin, feeds, now, gpus, services, fx)
                      for coin in (configs or self.gpu_configs)]
        active_symbols = [symbol for symbol in ("TSC", "PRL") if services.get(symbol) == "active"]
        current_symbol = active_symbols[0] if len(active_symbols) == 1 else None
        current = next((candidate for candidate in candidates if candidate["symbol"] == current_symbol), None)
        target = None
        delta = None
        if current and current.get("net_usd_per_day") is not None:
            alternatives = [candidate for candidate in candidates if candidate is not current and
                            candidate.get("net_usd_per_day") is not None]
            if alternatives:
                best = max(alternatives, key=lambda item: item["net_usd_per_day"])
                delta = best["net_usd_per_day"] - current["net_usd_per_day"]
                if delta >= self.profit_threshold_usd:
                    target = best
        alert_symbol = target["symbol"] if target else None
        self.last_profit_checked_at = now
        self.last_profit_current_symbol = current_symbol
        self.last_profit_recommendation = alert_symbol
        if alert_symbol != self.profit_alert_symbol:
            if alert_symbol is not None or self.profit_alert_symbol is not None:
                event = {"timestamp": now, "type": "profit_alert" if alert_symbol else "profit_alert_cleared",
                         "from": current_symbol, "to": alert_symbol,
                         "delta_usd_per_day": delta, "threshold_usd_per_day": self.profit_threshold_usd}
                try:
                    self._append_jsonl(self.operation_log_path, event)
                except OSError as error:
                    print("MINER_EVENT_LOG_ERROR " + str(error), flush=True)
            self.profit_alert_symbol = alert_symbol
        return {"currency": "USD", "as_of": now, "electricity_cny_per_kwh": ELECTRICITY_CNY_PER_KWH,
                "fx": fx, "threshold_usd_per_day": self.profit_threshold_usd,
                "current_symbol": current_symbol,
                "current_net_usd_per_day": current.get("net_usd_per_day") if current else None,
                "best_alternative_symbol": target["symbol"] if target else None,
                "alternative_delta_usd_per_day": delta,
                "threshold_reached": target is not None,
                "candidates": candidates,
                "formula": "净日利润 USD = 24小时产币量 × 实时参考币价 − (整机估算 W ÷ 1000 × 24 × 0.6 元/kWh × CNY/USD 参考汇率)"}

    @staticmethod
    def _collection(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("data", "items", "results"):
                if isinstance(value.get(key), list):
                    return value[key]
        return None

    @classmethod
    def _balances(cls, value):
        rows = cls._collection(value)
        if rows is None:
            return None
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            currency = row.get("currency") or row.get("code")
            if isinstance(currency, dict):
                currency = currency.get("id") or currency.get("code")
            if not isinstance(currency, str):
                continue
            available = row.get("available")
            if available is None and row.get("balance") is not None and row.get("locked") is not None:
                try:
                    available = str(parse_nonnegative_decimal(row["balance"]) - parse_nonnegative_decimal(row["locked"]))
                except ValueError:
                    continue
            try:
                balance = str(parse_nonnegative_decimal(row.get("balance", available)))
                available = str(parse_nonnegative_decimal(available))
            except ValueError:
                continue
            if parse_nonnegative_decimal(available) > parse_nonnegative_decimal(balance):
                continue
            result.append({"currency": currency.upper(), "balance": balance, "available": available})
        return result

    def _refresh_exchange(self):
        checked_at = time.time()
        data = {"checked_at": checked_at, "public_api": {"state": "unavailable", "http_status": None,
                                                          "checked_at": checked_at}, "pairs": [],
                "account_status": "未接入", "balances": None, "deposits": None,
                "withdrawals": None, "trades": None, "orders": None, "beneficiaries": None, "issues": {}}
        try:
            tickers = self.exchange_client.public_tickers()
            data["public_api"] = {"state": "public_api_ready", "http_status": 200, "checked_at": checked_at}
            data["pairs"] = available_pairs(tickers)
        except ExchangeError as error:
            data["public_api"] = {"state": "upstream_blocked" if error.status == 403 else "unavailable",
                                  "http_status": error.status, "checked_at": checked_at}
            return data
        try:
            member = self.exchange_client.account()
            if not isinstance(member, dict) or not (member.get("uid") or member.get("id")):
                data["account_status"] = "返回结构待核验"
                data["issues"]["account"] = "unrecognized_response"
                return data
            data["account_status"] = "已验证"
        except FileNotFoundError:
            data["account_status"] = "凭据未配置"
            return data
        except (ExchangeError, PermissionError, ValueError) as error:
            data["account_status"] = "认证失败"
            data["issues"]["account"] = error.code if isinstance(error, ExchangeError) else "credentials"
            return data
        for name in ("balances", "deposits", "withdrawals", "trades", "beneficiaries"):
            try:
                value = self.exchange_client.probe_account_collection(name)
                if name == "balances":
                    data[name] = self._balances(value)
                    if data[name] is None:
                        data["issues"][name] = "unrecognized_response"
                else:
                    rows = self._collection(value)
                    if rows is None:
                        data["issues"][name] = "unrecognized_response"
                    else:
                        data[name] = [sanitize_exchange_row(name, item) for item in rows[:10] if isinstance(item, dict)]
            except ExchangeError as error:
                data["issues"][name] = error.code
        try:
            rows = self._collection(self.exchange_client.orders())
            data["orders"] = [sanitize_exchange_row("orders", item) for item in rows[:10] if isinstance(item, dict)] if rows is not None else None
            if rows is None:
                data["issues"]["orders"] = "unrecognized_response"
        except ExchangeError as error:
            data["issues"]["orders"] = error.code
        return data

    def prepare_order(self, market, amount, price):
        quantity, limit = parse_positive_decimal(amount), parse_positive_decimal(price)
        with self.lock:
            exchange = dict(self.exchange or {})
        if time.time() - exchange.get("checked_at", 0) > 60:
            exchange = self._refresh_exchange()
            with self.lock:
                self.exchange = exchange
        if exchange.get("public_api", {}).get("state") != "public_api_ready" or exchange.get("account_status") != "已验证":
            raise ValueError("交易所尚未连通并完成账户认证")
        if market not in exchange.get("pairs", []):
            raise ValueError("交易对未从交易所实时行情核验")
        balances = exchange.get("balances")
        if balances is None:
            raise ValueError("账户可用余额尚未核验")
        base = market[:-4] if market.endswith("usdt") else market[:-3]
        available = next((parse_nonnegative_decimal(b["available"]) for b in balances if b["currency"].lower() == base), None)
        if available is None or quantity > available:
            raise ValueError("可用余额不足或缺失")
        token = secrets.token_urlsafe(32)
        preview = {"market": market, "amount": str(quantity), "price": str(limit),
                   "gross_quote": str(quantity * limit), "expires_at": time.time() + 90}
        with self.lock:
            self.order_previews = {key: value for key, value in self.order_previews.items()
                                   if value["expires_at"] > time.time()}
            self.order_previews[token] = preview
        return {**preview, "preview_token": token, "fee_note": "手续费和实际成交金额以交易所回执为准"}

    def submit_order(self, token):
        with self.lock:
            preview = self.order_previews.pop(token, None)
        if preview is None or preview["expires_at"] < time.time():
            raise ValueError("预览已过期，请重新核对")
        # Never retry a write. A transport failure may mean the exchange received it.
        result = self.exchange_client.limit_sell(preview["market"], preview["amount"], preview["price"])
        if not isinstance(result, dict) or not result.get("id"):
            raise ExchangeError(None, "ambiguous_order_response")
        return {"id": str(result["id"]), "market": result.get("market"), "state": result.get("state")}

    # —— 手动参考价：交易所接口拦截程序访问时，由用户在大屏录入；标明“手动”，24 小时后失效 ——
    def _load_manual_prices(self):
        try:
            data = json.loads(self.manual_prices_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result = {}
        if isinstance(data, dict):
            for symbol, row in data.items():
                if (isinstance(symbol, str) and SYMBOL_RE.fullmatch(symbol) and isinstance(row, dict) and
                        positive_number(row.get("value")) is not None and finite_number(row.get("set_at")) is not None):
                    result[symbol] = {"value": float(row["value"]), "set_at": float(row["set_at"])}
        return result

    def _load_calibrations(self):
        try:
            data = json.loads(self.calibration_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result = {}
        if isinstance(data, dict):
            for symbol, row in data.items():
                ratio = positive_number(row.get("ratio")) if isinstance(row, dict) else None
                stamp = finite_number(row.get("t")) if isinstance(row, dict) else None
                if (isinstance(symbol, str) and SYMBOL_RE.fullmatch(symbol) and ratio is not None and stamp is not None
                        and CALIBRATION_MIN <= ratio <= CALIBRATION_MAX):
                    result[symbol] = {"ratio": float(ratio), "t": float(stamp),
                                      "note": row["note"] if isinstance(row.get("note"), str) else None}
        return result

    def update_calibration(self, symbol, sample, now):
        """在挖时把“近 6 小时实记 ÷ 当下理论值”平滑进该币的校准比例并落盘；离谱的样本不收。"""
        if finite_number(sample) is None or not CALIBRATION_MIN <= sample <= CALIBRATION_MAX:
            return
        with self.calibration_lock:
            previous = self.calibrations.get(symbol)
            ratio = float(sample)
            if previous and 0 <= now - previous["t"] <= CALIBRATION_MAX_AGE:
                weight = min(1.0, (now - previous["t"]) / CALIBRATION_SMOOTH_SECONDS)
                ratio = previous["ratio"] + (sample - previous["ratio"]) * weight
            rows = {**self.calibrations, symbol: {"ratio": ratio, "t": now, "note": None}}
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = self.calibration_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(rows, ensure_ascii=False) + "\n", encoding="utf-8")
                os.chmod(temporary, 0o600)
                temporary.replace(self.calibration_path)
            except OSError as error:
                print("CALIBRATION_WRITE_ERROR " + str(error)[:160], flush=True)
            self.calibrations = rows

    def set_manual_price(self, symbol, value):
        if not isinstance(symbol, str) or symbol not in {coin["symbol"] for coin in self.configs + self.market_configs}:
            raise ValueError("未知币种")
        with self.lock:
            prices = dict(self.manual_prices)
            if value is None:
                prices.pop(symbol, None)
            else:
                number = positive_number(value)
                if number is None or number > 10_000_000:
                    raise ValueError("参考价必须是正数")
                prices[symbol] = {"value": number, "set_at": time.time()}
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.manual_prices_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(prices, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.manual_prices_path)
            self.manual_prices = prices
        self._append_jsonl(self.operation_log_path, {"timestamp": time.time(), "type": "manual_price",
                                                     "symbol": symbol, "value": prices.get(symbol, {}).get("value")})
        return {"symbol": symbol, "manual_price": prices.get(symbol)}

    # —— 历史序列：走势、24 小时产出、涨跌幅、今日电量 ——
    def load_history(self, now=None):
        now = time.time() if now is None else now
        for store in (self.telemetry, self.balances, self.market):
            store.load(now)

    def observed_yield(self, symbol, now, current_rate=None, window=PROFIT_WINDOW_SECONDS, minimum=OBSERVED_MIN_SECONDS,
                       warmup=0):
        """按矿池账户累计值（待付＋已付）在近 24 小时里创出的新高算产出，合计等于首尾对账。

        只在该币运行时记账。累计值超过此前最高水位的部分才算新挖到的：矿池把待付临时扣掉一截、过一两轮又补回，
        或者打款时先扣待付、下一轮才加已付，这中间累计值低于水位，时长照计、补回来的部分不重复计。
        采样中断的那一段整段不算（既不计产出也不计时长），刚开挖前的空窗不会把数字稀释。
        账户对不上的那一段同样整段不算、水位从那一行重新起算：来源摘要改变、已付变少、
        已付字段时有时无，或没有已付字段而待付减少。没有来源摘要时的已付跳增明确标为不可用。
        同源累计值回落多久都保留旧水位；迟到的回补不能算成新收益。来源或已付字段明确异常时才重置。
        有效时长不足 2 小时不出数，不足 24 小时按已有时长外推并如实标明。
        给了当前算力、且它和窗口里的平均算力对不上时（加卡、停卡、调功耗），改按
        “每单位算力记了多少账 × 当前算力”算，不用等窗口滚满 24 小时才跟上。
        window 换成更短的时长就是“近 N 小时实记”，有效时长不足 minimum 同样不出数。
        warmup 是开挖（或采样中断后重新开挖）之后不计入的秒数：按最近一段份额分奖励的矿池，头一两个小时
        入账是爬坡的，记进去会把日产低估一半。
        """
        rows = [row for row in self.balances.snapshot()
                if row.get("s") == symbol and finite_number(row.get("p")) is not None]
        if len(rows) < 2 or now - rows[-1]["t"] > 4 * BALANCE_INTERVAL:
            return None
        cutoff = rows[-1]["t"] - window
        rates = [(row["t"], row["r"][symbol]) for row in self.telemetry.snapshot()
                 if row["t"] >= cutoff and isinstance(row.get("r"), dict) and
                 positive_number(row["r"].get(symbol)) is not None] if current_rate else []
        cursor = 0
        gained = covered = rated_gain = rated_seconds = hash_seconds = 0.0
        uncertain = False
        # 水位从留存的最早一行算起（不只窗口内），窗口起点正好落在回落里也不会把弹回当成新增
        peak = rows[0]["p"] + (rows[0].get("d") or 0.0)
        run_started = rows[0]["t"]
        for previous, current in zip(rows, rows[1:]):
            step = current["t"] - previous["t"]
            if not 0 < step <= 3 * BALANCE_INTERVAL:
                run_started = current["t"]
            paid_before, paid = previous.get("d"), current.get("d")
            total = current["p"] + (paid or 0.0)
            before_id, account_id = previous.get("i"), current.get("i")
            changed_account = bool(before_id and account_id and before_id != account_id)
            if changed_account and current["t"] >= cutoff:
                uncertain = True
            if paid_before is None and paid is None:
                switched = changed_account or current["p"] < previous["p"] - 1e-12
            elif paid_before is None or paid is None or paid < paid_before:
                switched = True
            else:
                switched = changed_account
                if paid > paid_before and not (before_id and account_id) and current["t"] >= cutoff:
                    uncertain = True  # 无法证明是同一账户的打款，不能凭已付增幅猜换账户
            if switched:
                peak = total
            gain = max(0.0, total - peak)
            if total >= peak:
                peak = total
            if (switched or previous["t"] < cutoff or not 0 < step <= 3 * BALANCE_INTERVAL or
                    previous["t"] < run_started + warmup):
                continue
            gained += gain
            covered += step
            while cursor < len(rates) and rates[cursor][0] <= previous["t"]:
                cursor += 1
            end = cursor
            while end < len(rates) and rates[end][0] <= current["t"]:
                end += 1
            if end > cursor:  # 这一段有本机算力采样，才参与“每单位算力记账”的折算
                rated_gain += gain
                rated_seconds += step
                hash_seconds += sum(rate for _, rate in rates[cursor:end]) / (end - cursor) * step
            cursor = end
        if covered < minimum:
            return {"coins_per_day": None, "window_seconds": covered, "full": False}
        if uncertain:
            return {"coins_per_day": None, "window_seconds": covered, "full": False,
                    "status": "账户来源未验证，打款增量暂不能归为挖矿收益"}
        result = {"coins_per_day": gained * PROFIT_WINDOW_SECONDS / covered, "window_seconds": covered,
                  "full": covered >= window - 6 * BALANCE_INTERVAL}
        if hash_seconds > 0 and rated_seconds >= 0.8 * covered:
            window_rate = hash_seconds / rated_seconds
            if abs(current_rate / window_rate - 1) > OBSERVED_RATE_SHIFT:
                result.update(coins_per_day=rated_gain / hash_seconds * current_rate * PROFIT_WINDOW_SECONDS,
                              rate_scale=current_rate / window_rate, window_rate=window_rate)
        return result

    def _held(self, key, condition, now, seconds):
        """条件连续成立满 seconds 才算数；一旦不成立就清零重计。"""
        if not condition:
            self.condition_since.pop(key, None)
            return False
        return now - self.condition_since.setdefault(key, now) >= seconds

    def market_trend(self, symbol, key, current, now, hours=24):
        rows = [row for row in self.market.snapshot() if row.get("s") == symbol]
        return pct_change(current, series_value_at(rows, key, now - hours * 3600, 2 * 3600))

    def running_seconds(self, symbol, now, window=PROFIT_WINDOW_SECONDS):
        """近一个窗口里该币有本机算力采样的时长（遥测每分钟一行）。"""
        return TELEMETRY_INTERVAL * sum(
            1 for row in self.telemetry.snapshot()
            if row["t"] >= now - window and isinstance(row.get("r"), dict) and positive_number(row["r"].get(symbol)) is not None)

    @staticmethod
    def _gpu_uuids(gpus, indices):
        devices = {gpu.get("index"): gpu.get("uuid") for gpu in gpus if isinstance(gpu, dict)}
        uuids = [devices.get(index) for index in indices]
        if not uuids or any(not isinstance(uuid, str) or not uuid.startswith("GPU-") for uuid in uuids):
            return None
        return sorted(uuids) if len(set(uuids)) == len(uuids) else None

    def credit_window_matches_gpus(self, symbol, now, gpu_indices, gpus, source_fingerprint=None):
        """账户收益仅在整段窗口有同一组物理 GPU 的连续本机采样时归为实记。"""
        expected = self._gpu_uuids(gpus, gpu_indices)
        if expected is None:
            return False
        rows = [row for row in self.telemetry.snapshot() if now - PROFIT_WINDOW_SECONDS <= row["t"] <= now]
        if not rows or rows[0]["t"] > now - PROFIT_WINDOW_SECONDS + 2 * TELEMETRY_INTERVAL or (
                rows[-1]["t"] < now - 2 * TELEMETRY_INTERVAL):
            return False
        previous = None
        for row in rows:
            assigned = (row.get("a") or {}).get(symbol)
            if (assigned != expected or
                    (source_fingerprint is not None and (row.get("f") or {}).get(symbol) != source_fingerprint) or
                    positive_number((row.get("r") or {}).get(symbol)) is None or
                    (previous is not None and row["t"] - previous > 2 * TELEMETRY_INTERVAL)):
                return False
            previous = row["t"]
        return True

    def market_median(self, symbol, key, start, end, minimum=3):
        """行情序列里某段时间内某字段的中位数；样本不足返回 None。"""
        values = sorted(row[key] for row in self.market.snapshot()
                        if row.get("s") == symbol and row.get(key) and start <= row["t"] <= end)
        return values[len(values) // 2] if len(values) >= minimum else None

    def energy_today(self, now):
        """按每分钟整机估算功耗积分得到今日用电；采样缺口超过 3 分钟的区间不计入。"""
        local = time.localtime(now)
        midnight = now - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
        rows = [row for row in self.telemetry.snapshot() if row["t"] >= midnight and finite_number(row.get("w")) is not None]
        joules = covered = 0.0
        for previous, current in zip(rows, rows[1:]):
            gap = current["t"] - previous["t"]
            if 0 < gap <= 180:
                joules += (previous["w"] + current["w"]) / 2 * gap
                covered += gap
        kwh = joules / 3_600_000
        return {"kwh": kwh, "cost_cny": kwh * ELECTRICITY_CNY_PER_KWH, "covered_seconds": covered,
                "since": midnight, "elapsed_seconds": now - midnight}

    def history_payload(self, minutes=360, points=180):
        now = time.time()
        rows = [row for row in self.telemetry.snapshot() if row["t"] >= now - minutes * 60]
        step = max(1, math.ceil(len(rows) / max(10, points)))
        rows = rows[::step]
        symbols = sorted({symbol for row in rows for symbol in (row.get("r") or {})})
        gpu_indices = sorted({item[0] for row in rows for item in (row.get("g") or []) if isinstance(item, list) and item})
        gpus = {}
        for index in gpu_indices:
            by_time = [next((item for item in (row.get("g") or []) if item and item[0] == index), None) for row in rows]
            gpus[str(index)] = {"temp": [item[1] if item else None for item in by_time],
                                "memory_temp": [item[2] if item else None for item in by_time],
                                "power": [item[3] if item else None for item in by_time],
                                "fan": [item[4] if item else None for item in by_time]}
        return {"t": [row["t"] for row in rows], "minutes": minutes,
                "rates": {symbol: [(row.get("r") or {}).get(symbol) for row in rows] for symbol in symbols},
                "pool_rates": {symbol: [(row.get("pr") or {}).get(symbol) for row in rows] for symbol in symbols},
                "gpus": gpus, "power": [row.get("w") for row in rows],
                "cpu_rate": [(row.get("c") or [None])[0] for row in rows],
                "cpu_temp": [(row.get("c") or [None, None])[1] for row in rows]}

    def record_history(self, snapshot):
        """后台每分钟调用一次：把当前快照里用得上的数落成时间序列。"""
        now = snapshot["generated_at"]
        if now - self.history_marks["telemetry"] >= TELEMETRY_INTERVAL - 2:
            self.history_marks["telemetry"] = now
            projects = snapshot.get("projects", [])
            cpu = snapshot.get("noid_cpu") or {}
            sample = cpu.get("telemetry") or {}
            assignments = {}
            sources = {}
            for project in projects:
                per_gpu = project.get("local", {}).get("per_gpu") or {}
                if project.get("active") and per_gpu and all(str(index).isdigit() for index in per_gpu):
                    uuids = self._gpu_uuids(snapshot.get("gpus", []), [int(index) for index in per_gpu])
                    if uuids is not None:
                        assignments[project["symbol"]] = uuids
                        kind = "account" if any(c["symbol"] == project["symbol"] and c.get("account")
                                                for c in self.configs) else "earnings"
                        fingerprint = self.feed_fingerprints.get((project["symbol"], kind))
                        if fingerprint:
                            sources[project["symbol"]] = fingerprint
            self.telemetry.append({
                "t": round(now, 1),
                "r": {p["symbol"]: round(p["local"]["total"], 4) for p in projects
                      if p["active"] and p["local"].get("total") is not None},
                "a": assignments,
                "f": sources,
                "pr": {p["symbol"]: round(p["pool"]["hashrate_local_unit"], 4) for p in projects
                       if p["active"] and p["pool"].get("hashrate_local_unit") is not None},
                "g": [[gpu["index"], gpu.get("temperature"), gpu.get("memory_temperature"),
                       round(gpu["power"], 1), gpu.get("fan_percent")] for gpu in snapshot.get("gpus", [])],
                "c": [sample.get("hashrate") if sample.get("status") == "正常" else None, cpu.get("cpu_temperature_c")],
                "w": (snapshot.get("power") or {}).get("estimated_system_w")})
        # 余额与行情：到点后只要这一轮确实记下了东西才开始下一个间隔；刚启动数据源还没回来时，下一分钟接着试
        if now - self.history_marks["balance"] >= BALANCE_INTERVAL - 2:
            for project in snapshot.get("projects", []):
                balance = project.get("balance") or {}
                if (project["active"] and balance.get("recordable") and
                        balance.get("fetched_at", 0) > now - 2 * BALANCE_INTERVAL):
                    symbol = project["symbol"]
                    kind = "account" if any(c["symbol"] == symbol and c.get("account") for c in self.configs) else "earnings"
                    self.balances.append({"t": round(now, 1), "s": symbol,
                                          "p": balance["accruing"], "d": balance.get("paid"),
                                          "i": self.feed_fingerprints.get((symbol, kind))})
                    self.history_marks["balance"] = now
                    economics = project.get("economics") or {}
                    recent, theoretical = economics.get("recent"), economics.get("theoretical_coins_per_day")
                    if (recent and recent.get("coins_per_day") and theoretical and
                            recent["window_seconds"] >= 0.8 * OBSERVED_RECENT_SECONDS):
                        self.update_calibration(project["symbol"], recent["coins_per_day"] / theoretical, now)
        if now - self.history_marks["market"] >= MARKET_INTERVAL - 2:
            for project in snapshot.get("projects", []) + snapshot.get("market_projects", []):
                price = (project.get("price") or {}).get("feed_value")
                network = project.get("network") or {}
                if price is not None or network.get("hashrate_instant") is not None:
                    self.market.append({"t": round(now, 1), "s": project["symbol"], "p": price,
                                        "h": network.get("hashrate_instant"), "y": network.get("yield_index_instant")})
                    self.history_marks["market"] = now

    def record_history_forever(self):
        while True:
            try:
                self.record_history(self.snapshot())
            except Exception as error:
                print("HISTORY_LOOP_ERROR " + str(error)[:200], flush=True)
            time.sleep(TELEMETRY_INTERVAL)

    # —— 项目视图：每个 monitor.json 一行，现场、矿池、行情、估算收益同一结构 ——
    def _feed(self, feeds, coin, kind, now, active):
        value, config = feeds.get((coin["symbol"], kind)), coin.get(kind)
        if not config or not isinstance(value, dict):
            return None
        if now - value.get("fetched_at", 0) > feed_max_age(config, kind, active):
            return None
        if kind == "price" and "source_at" in value and \
                now - value["source_at"] > config.get("max_source_age_seconds", 600):
            return None
        if kind == "network" and "source_at" in value and \
                now - value["source_at"] > config.get("max_source_age_seconds", NETWORK_SOURCE_MAX_AGE):
            return None  # 上游自己的时间戳停了：数据源卡住，不当现值用
        return value

    @staticmethod
    def _cpu_contribution(coin, noid):
        """同币 CPU 矿工的算力（与大屏原有口径一致：当前 PID、正常、≤300 秒、MH/s）。"""
        if coin.get("kind") not in {"cpu", "cpu_gpu"} or not isinstance(noid, dict):
            return None
        sample = noid.get("telemetry") or {}
        pid = noid.get("pid")
        if (noid.get("service") == "active" and strict_int(pid) and pid > 0 and sample.get("pid") == pid and
                sample.get("status") == "正常" and sample.get("unit") == "MH/s" and
                nonnegative_number(sample.get("hashrate")) is not None and
                0 <= (finite_number(sample.get("age_seconds")) or 1e9) <= 300):
            return {"hashrate": float(sample["hashrate"]), "unit": "MH/s", "threads": noid.get("threads"),
                    "accepted": sample.get("accepted"), "stale": sample.get("stale"),
                    "rejected": sample.get("rejected"), "age_seconds": sample.get("age_seconds")}
        return None

    @staticmethod
    def _theoretical_yield(hashrate_base, base_unit, network):
        """本机算力占全网（或占矿池）比例 × 全网（或矿池）日出块，扣矿池费与矿工开发费；单位对不上或缺项返回 None。"""
        if (not network or hashrate_base is None or base_unit != network.get("hashrate_unit") or
                not network.get("block_reward")):
            return None
        fees = (1 - network["pool_fee_percent"] / 100) * (1 - network["extra_fee_percent"] / 100)
        if network.get("difficulty_hashes"):
            blocks_per_day = hashrate_base * PROFIT_WINDOW_SECONDS / network["difficulty_hashes"]
        elif network.get("pool_expected_blocks_24h") and network.get("pool_hashrate_24h"):
            blocks_per_day = hashrate_base / network["pool_hashrate_24h"] * network["pool_expected_blocks_24h"]
        elif network.get("hashrate") and network.get("block_seconds"):
            blocks_per_day = hashrate_base / network["hashrate"] * PROFIT_WINDOW_SECONDS / network["block_seconds"]
        else:
            return None
        return blocks_per_day * network["block_reward"] * fees * network["yield_scale"]

    def _project(self, coin, ctx):
        now, symbol = ctx["now"], coin["symbol"]
        service = "market-only" if coin.get("watch_only") else ctx["services"].get(symbol, "unknown")
        active = service == "active"
        rate = ctx["rates"].get(symbol) if active else None
        price_feed = self._feed(ctx["feeds"], coin, "price", now, active)
        account = self._feed(ctx["feeds"], coin, "account", now, active)
        daily = self._feed(ctx["feeds"], coin, "daily", now, active)
        network = self._feed(ctx["feeds"], coin, "network", now, active)
        legacy_earnings = self._feed(ctx["feeds"], coin, "earnings", now, active)
        legacy_pool = self._feed(ctx["feeds"], coin, "pool", now, active)
        if network and network.get("block_reward") and coin["network"].get("block_reward_path"):
            # 块奖励取的是矿池最近一块的实发数：一个手续费特别高的块会让估算跳三成，取近 15 次读数的中位数
            samples = self.reward_samples.setdefault(symbol, deque(maxlen=15))
            if not samples or samples[-1][0] != network["fetched_at"]:
                samples.append((network["fetched_at"], network["block_reward"]))
            rewards = sorted(value for _, value in samples)
            network = {**network, "block_reward": rewards[len(rewards) // 2]}
        gpu_indices = coin.get("gpu_indices", [])
        live = {gpu["index"]: gpu for gpu in ctx["gpus"]}
        fx = ctx["fx_rate"]
        profile = coin.get("profitability")
        reference = coin.get("reference") or {}

        # 现场算力：GPU 来自矿工采样，同币 CPU 另列，合计只用于展示与全网占比估算
        local = {"status": rate.get("status") if rate else ("未运行" if not active else "等待采样"),
                 "unit": (rate or {}).get("unit") or coin["rate"]["unit"], "gpu": None, "cpu": None, "total": None,
                 "per_gpu": {}, "window": (rate or {}).get("window"), "age_seconds": (rate or {}).get("age_seconds")}
        if rate and rate.get("status") == "正常" and rate.get("total") is not None:
            local.update(gpu=rate["total"], total=rate["total"], per_gpu=dict(rate.get("per_gpu") or {}))
        cpu = self._cpu_contribution(coin, ctx["noid"]) if active or coin.get("kind") == "cpu" else None
        if cpu and local["unit"] == cpu["unit"]:
            local["cpu"] = cpu
            local["total"] = (local["total"] or 0) + cpu["hashrate"]
        scale, base_unit = split_rate_unit(local["unit"])
        local_base = local["total"] * scale if local["total"] is not None and scale else None

        # 报价：手动参考价（用户显式录入）优先于接口报价，两者同时给出便于对照
        manual = self.manual_prices.get(symbol)
        if manual and now - manual["set_at"] > MANUAL_PRICE_MAX_AGE:
            manual = None
        feed_value = price_feed["value"] if price_feed else None
        price = None
        if manual or price_feed:
            price = {"value": manual["value"] if manual else feed_value, "kind": "manual" if manual else "feed",
                     "source": "手动参考价" if manual else price_feed["source"],
                     "source_url": None if manual else price_feed.get("source_url"),
                     "at": manual["set_at"] if manual else (price_feed.get("source_at") or price_feed["fetched_at"]),
                     "feed_value": feed_value, "feed_source": price_feed["source"] if price_feed else None,
                     "change_24h_pct": (price_feed or {}).get("change_24h_pct")}
            if price["change_24h_pct"] is None and feed_value is not None:
                price["change_24h_pct"] = self.market_trend(symbol, "p", feed_value, now)
        price_value = price["value"] if price else None
        # 盘口薄、几分钟能差几倍的币：收益按“近 N 分钟逐次记录的中位价”、现价、盘口买一价里最低的估——
        # 一时拉高不追，已经跌下来的认，卖得出去的价按买一；现价照常显示，手动参考价不动
        minutes = (coin.get("price") or {}).get("smooth_minutes")
        if price and price["kind"] == "feed" and feed_value is not None:
            if minutes:
                recorded = [row["p"] for row in self.market.snapshot()
                            if row.get("s") == symbol and row.get("p") and row["t"] >= now - minutes * 60]
                if len(recorded) >= 2:
                    values = sorted(recorded + [feed_value])
                    median = values[len(values) // 2]
                    price_value = min(median, feed_value)
                    price.update(estimate_value=price_value, median_value=median,
                                 estimate_basis=f"近 {minutes} 分钟中位价与现价取低")
            bid = price_feed.get("bid")
            if bid is not None:
                price["bid"] = bid
                if bid < price_value:
                    price_value = bid
                    price.update(estimate_value=bid, estimate_basis="盘口买一价")

        # 全网算力是折算值、几小时内能差一倍的数据源：算力、理论估算、单位算力收益和它们的 24 小时变化
        # 都改用近 6 小时本机记录的中位数，免得数字和信号跟着当下这一个折算值乱跳
        steady, smoothed = network, False
        if network and network["volatile"]:
            recent = [row["h"] for row in self.market.snapshot()
                      if row.get("s") == symbol and row.get("h") and row["t"] >= now - 6 * 3600]
            if len(recent) >= 3:
                values = sorted(recent + [network["hashrate"]])
                steady, smoothed = {**network, "hashrate": values[len(values) // 2]}, True

        # 全网：算力、相对 24 小时的变化、单位算力日收益指数（判断“好不好挖”的核心）
        net = None
        if network:
            if smoothed:  # 与 24 小时前那 6 小时的中位数比，两头同一口径
                change = pct_change(steady["hashrate"], self.market_median(symbol, "h", now - 30 * 3600, now - 24 * 3600))
                change_basis = "近 6 小时中位数，较 24 小时前同口径"
            else:  # 优先和 24 小时前的本机记录比，与币价、单位算力收益同一个基准；没有记录才用数据源自带的均值
                change = self.market_trend(symbol, "h", network["hashrate"], now)
                change_basis = "较 24 小时前本机记录"
                if change is None:
                    change = pct_change(network["hashrate"], network.get("hashrate_24h"))
                    change_basis = "较数据源 24 小时均值"
            # 矿池自己的算力不可能超过全网；出现这种情况说明该数据源的全网算力是折算值，不拿它算占比
            suspect = bool(network.get("pool_hashrate") and network["pool_hashrate"] > network["hashrate"])
            index = None
            if feed_value is not None and network.get("block_reward") and network.get("difficulty_hashes"):
                index = feed_value * network["block_reward"] * PROFIT_WINDOW_SECONDS / network["difficulty_hashes"]
            elif feed_value is not None and network.get("block_reward") and network.get("block_seconds"):
                index = (feed_value * network["block_reward"] * PROFIT_WINDOW_SECONDS /
                         network["block_seconds"] / network["hashrate"])
            index_instant = index  # 历史里记的是这个当下值
            if index and smoothed:
                # 折算值的币：取近 6 小时逐次记录的指数的中位数（含当下这一次），币价和全网同窗平滑
                recent_index = sorted([row["y"] for row in self.market.snapshot()
                                       if row.get("s") == symbol and row.get("y") and row["t"] >= now - 6 * 3600] + [index_instant])
                index = recent_index[len(recent_index) // 2]
            if not index:
                index_change = None
            elif smoothed:
                index_change = pct_change(index, self.market_median(symbol, "y", now - 30 * 3600, now - 24 * 3600))
            else:
                index_change = self.market_trend(symbol, "y", index, now)
            net = {**{key: network.get(key) for key in (
                "hashrate_unit", "hashrate_24h", "difficulty", "height", "block_reward",
                "block_reward_note", "block_seconds",
                "pool_hashrate", "pool_fee_percent", "extra_fee_percent", "luck_percent", "calibrated", "yield_note",
                "source", "source_url", "fetched_at")},
                "hashrate": steady["hashrate"], "hashrate_instant": network["hashrate"], "smoothed": smoothed,
                "volatile": network["volatile"] or suspect,
                # 折算值还没攒够 6 小时记录时，变化率不可信：不进信号、不触发提醒
                "noisy": (network["volatile"] or suspect) and not smoothed,
                "change_24h_pct": change, "change_basis": change_basis if change is not None else None,
                "pool_share_pct": (network["pool_hashrate"] / network["hashrate"] * 100
                                   if network.get("pool_hashrate") and not (network["volatile"] or suspect) else None),
                "my_share_pct": (local_base / steady["hashrate"] * 100
                                 if local_base is not None and base_unit == network["hashrate_unit"] else None),
                "yield_index": index, "yield_index_instant": index_instant,
                "yield_index_change_24h_pct": index_change}

        # 余额：统一成 待付 / 已付 / 门槛；老的 TSC、PRL 专用解析也并到同一结构
        balance = None
        if account:
            # 配了已付字段却没取到：这一轮不拿它记产出，免得把累计已付当成新挖到的
            complete = account["paid"] is not None or not coin["account"].get("paid_path")
            balance = {"unit": account["unit"], "pending": account["pending"], "paid": account["paid"],
                       "threshold": account["threshold"], "immature": account["immature"],
                       "cumulative": account["pending"] + (account["paid"] or 0) if complete else None,
                       "accruing": account["pending"] + (account["immature"] or 0), "recordable": complete,
                       "source": account["source"], "source_url": account.get("source_url"),
                       "fetched_at": account["fetched_at"], "note": "矿池账户记账，不等于交易所到账"}
        elif legacy_earnings and legacy_earnings.get("mode") == "settled":
            balance = {"unit": legacy_earnings["unit"], "pending": legacy_earnings["balance"],
                       "paid": legacy_earnings["paid"], "threshold": None, "immature": legacy_earnings["immature"],
                       "cumulative": legacy_earnings["settled"] + legacy_earnings["immature"],
                       "accruing": legacy_earnings["balance"] + legacy_earnings["immature"], "recordable": True,
                       "source": legacy_earnings["source"], "source_url": legacy_earnings.get("source_url"),
                       "fetched_at": legacy_earnings["fetched_at"], "note": "已结算含已打款与余额；待成熟另列"}
        elif legacy_earnings and legacy_earnings.get("mode") == "returned_credits":
            balance = {"unit": legacy_earnings["unit"], "pending": legacy_earnings.get("balance"),
                       "paid": legacy_earnings.get("paid"), "threshold": None,
                       "immature": None, "cumulative": None, "credited": legacy_earnings["credit_total"],
                       "credit_count": legacy_earnings["credit_count"], "last_at": legacy_earnings.get("last_at"),
                       "source": legacy_earnings["source"], "source_url": legacy_earnings.get("source_url"),
                       "fetched_at": legacy_earnings["fetched_at"],
                       "note": "按接口返回的流水合计（入账减打款）；流水被截断时不准，也不等于钱包到账"}

        # 矿池侧：池端有效算力、份额质量、最近一次份额
        pool = {"hashrate": None, "hashrate_unit": None, "hashrate_window": None, "hashrate_local_unit": None,
                "ratio": None, "shares": None, "invalid": None, "stale": None, "bad_share_pct": None,
                "last_seen": None, "workers": None, "accepted": None, "rejected": None, "session_stale": None,
                "source": None}
        if account:
            pool.update({key: account.get(key) for key in ("hashrate", "hashrate_unit", "hashrate_window", "shares",
                                                             "invalid", "stale", "last_seen", "workers")})
            pool["source"] = account["source"]
            if account.get("hashrate_base") is not None and scale and account.get("hashrate_base_unit") == base_unit:
                pool["hashrate_local_unit"] = account["hashrate_base"] / scale
                if local_base:
                    pool["ratio"] = account["hashrate_base"] / local_base
            bad = (account.get("invalid") or 0) + (account.get("stale") or 0)
            if account.get("shares") is not None and account["shares"] + bad > 0:
                pool["bad_share_pct"] = bad / (account["shares"] + bad) * 100
        session = (rate or {}).get("pool") or legacy_pool
        if session:
            pool.update(accepted=session.get("accepted"), rejected=session.get("rejected"),
                        session_stale=session.get("stale"))
            pool["source"] = pool["source"] or session.get("source")
            if pool["hashrate"] is None and session.get("effective_hashrate") is not None:
                pool.update(hashrate=session["effective_hashrate"], hashrate_unit=session.get("effective_hashrate_unit"),
                            hashrate_window=session.get("effective_hashrate_window"), workers=session.get("workers"))

        # 功耗与产出
        if profile is None:
            power_w = power_basis = None
        elif active:
            selected = [live.get(index) for index in gpu_indices]
            power_w = (sum(gpu["power"] for gpu in selected) + profile["base_system_w"]
                       if selected and all(selected) else None)
            power_basis = "显卡实时板卡功耗 + 分摊的主机估算功耗"
        else:
            power_w = sum(float(profile["gpu_board_w_per_gpu"][str(index)]) for index in gpu_indices) + profile["base_system_w"]
            power_basis = profile["power_basis"]
        if cpu and power_basis:
            power_basis += "；未计入 CPU 挖矿的增量耗电"

        warmup = (coin.get("account") or {}).get("warmup_minutes", 0) * 60
        observed = (self.observed_yield(symbol, now, local["total"], warmup=warmup)
                    if active and balance and balance.get("recordable") else None)
        if (symbol == "TSC" and legacy_earnings and legacy_earnings.get("mode") == "settled" and
                not self.credit_window_matches_gpus(symbol, now, gpu_indices, ctx["gpus"],
                                                    self.feed_fingerprints.get((symbol, "earnings")))):
            observed = None  # Tiger Pool 是账户合计；窗口混卡或物理卡身份不明时不标为当前卡组实记
        # credit 是账户合计。历史逐分钟记录必须证明整段都由当前这组卡在挖；
        # 单/多卡切换、并行别的币、旧格式缺卡号或采样中断时，不把混合流水冒充该组合实记。
        if (observed is None and active and legacy_earnings and legacy_earnings.get("mode") == "returned_credits" and
                legacy_earnings.get("credit_24h_covered") and nonnegative_number(legacy_earnings.get("credit_24h")) is not None and
                self.credit_window_matches_gpus(symbol, now, gpu_indices, ctx["gpus"],
                                                self.feed_fingerprints.get((symbol, "earnings")))):
            observed = {"coins_per_day": float(legacy_earnings["credit_24h"]), "window_seconds": PROFIT_WINDOW_SECONDS,
                        "full": True, "scale": 1.0}
        # 近 6 小时的实记：全网算力快涨快跌时 24 小时均值是滞后的，换币比较看这个
        recent = (self.observed_yield(symbol, now, local["total"], window=OBSERVED_RECENT_SECONDS,
                                      minimum=0.8 * OBSERVED_RECENT_SECONDS, warmup=warmup)
                  if active and balance and balance.get("recordable") and observed and
                  observed["coins_per_day"] is not None else None)
        if (recent is not None and recent["coins_per_day"] is None and not observed["full"]):
            early = self.observed_yield(symbol, now, local["total"], window=OBSERVED_EARLY_SECONDS,
                                        minimum=0.75 * OBSERVED_EARLY_SECONDS, warmup=warmup)
            recent = early if early and early["coins_per_day"] is not None else recent
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        yesterday = None
        if daily:
            complete = [row for row in daily["days"] if row["day"] < today]
            yesterday = complete[-1] if complete else None
        theoretical = self._theoretical_yield(local_base, base_unit, steady) if active else None
        reference_rate = None
        if not active and reference:
            ref_scale, ref_base = split_rate_unit(reference["unit"])
            if gpu_indices and all(str(index) in reference["hashrate_per_gpu"] for index in gpu_indices):
                ref_total = sum(float(reference["hashrate_per_gpu"][str(index)]) for index in gpu_indices)
                reference_rate = {"total": ref_total, "unit": reference["unit"], "note": reference["note"]}
                theoretical = self._theoretical_yield(ref_total * ref_scale, ref_base, steady)
        calibration = self.calibrations.get(symbol)
        if calibration and not 0 <= now - calibration["t"] <= CALIBRATION_MAX_AGE:
            calibration = None
        if observed and observed["coins_per_day"] is not None:
            coins_per_day = observed["coins_per_day"]
            hours = observed["window_seconds"] / 3600
            basis = "observed" if observed["full"] else "observed_partial"
            basis_label = "矿池账户近 24 小时实际记账" if observed["full"] else f"矿池账户近 {hours:.1f} 小时记账外推"
            basis_short = "账户 24h 实记" if observed["full"] else f"账户近 {hours:.0f}h 外推"
            if observed.get("scale", 1.0) > 1.0:
                basis_label += f" × 双卡外推 ({observed['scale']:.0f}卡)"
                basis_short += f"×{observed['scale']:.0f}卡"
            if observed.get("rate_scale"):
                basis = "observed_partial"
                basis_label = (f"矿池账户近 {hours:.1f} 小时每单位算力的记账 × 当前算力"
                               f"（当前算力是这段时间平均的 {observed['rate_scale']:.2f} 倍）")
                basis_short = f"账户近 {hours:.0f}h·按现算力"
            if not observed["full"] and recent and recent["coins_per_day"] is not None:
                # 挖了还不满一天：拿开挖以来的均值外推，会把已经过去的行情（难度、全网算力）一起摊进来；
                # 日产改按最近一段的实际记账，满一天后回到 24 小时实记
                coins_per_day, hours = recent["coins_per_day"], recent["window_seconds"] / 3600
                basis = "observed_partial"
                basis_label = f"矿池账户近 {hours:.0f} 小时实际记账折一天"
                basis_short = f"账户近 {hours:.0f}h 实记"
                if recent.get("rate_scale"):
                    basis_label += f"，按当前算力（是这段时间平均的 {recent['rate_scale']:.2f} 倍）"
                    basis_short += "·按现算力"
        elif theoretical is not None and calibration:
            # 这个币自己上次实挖时“实记 ÷ 理论”的比例：把矿池运气、份额损耗、口径偏差一并带进来
            coins_per_day, basis = theoretical * calibration["ratio"], "theoretical_calibrated" if active else "reference_calibrated"
            measured = time.strftime("%m-%d", time.localtime(calibration["t"]))
            basis_label = (("按当前全网算力的理论估算" if active else "按历史实测算力的理论估算") +
                           f" × 上次实挖的实记/理论比 {calibration['ratio']:.2f}（{measured} 测得）")
            basis_short = ("理论" if active else "历史算力") + f"×实记比 {calibration['ratio']:.2f}"
        elif theoretical is not None:
            coins_per_day, basis = theoretical, "theoretical" if active else "reference"
            loose = not (network or {}).get("calibrated")
            basis_label = ("按当前全网算力的理论估算" if active else "按历史实测算力的理论估算") + ("（未校准）" if loose else "")
            basis_short = ("理论估算" if active else "历史算力估算") + ("·未校准" if loose else "")
        else:
            coins_per_day, basis, basis_label, basis_short = None, None, None, None
        estimated = basis in {"theoretical", "reference", "theoretical_calibrated", "reference_calibrated"}
        # 矿池自己的算力已经超过按难度折算的全网：全网算力涨得比难度快，难度追平后同样算力的产出按这个比例缩水
        # 两个迹象取更保守的：矿池算力（是滞后的均值）超过全网；实际出块比协议目标快
        lag = None
        if estimated and network:
            factors = []
            if network.get("pool_hashrate") and network["pool_hashrate"] > steady["hashrate"]:
                factors.append(steady["hashrate"] / network["pool_hashrate"])
            if (network.get("target_block_seconds") and network.get("block_seconds") and
                    network["block_seconds"] < network["target_block_seconds"]):
                factors.append(network["block_seconds"] / network["target_block_seconds"])
            if factors and min(factors) < 0.97:
                lag = {"factor": min(factors), "coins_per_day": coins_per_day * min(factors)}
        gross = coins_per_day * price_value if coins_per_day is not None and price_value is not None else None
        electricity_cny = power_w / 1000 * 24 * ELECTRICITY_CNY_PER_KWH if power_w is not None else None
        electricity_usd = electricity_cny * fx if electricity_cny is not None and fx else None
        net_usd = gross - electricity_usd if gross is not None and electricity_usd is not None else None
        missing = [label for label, value in (("产出", coins_per_day), ("币价", price_value), ("汇率", fx),
                                              ("功耗", power_w)) if value is None]
        cards = len(gpu_indices)
        # 上面用的是保守价（近一小时中位价、买一价里低的）；最新成交价更高时另算一份，两头都摆出来
        market_value, at_market = (price["value"] if price else None), None
        if (coins_per_day is not None and price_value is not None and market_value is not None and
                market_value > price_value and electricity_usd is not None):
            market_net = coins_per_day * market_value - electricity_usd
            at_market = {"price_usd": market_value, "gross_usd_per_day": coins_per_day * market_value,
                         "net_usd_per_day": market_net, "net_cny_per_day": market_net / fx if fx else None}
        # 日产按账户实记时是回看：难度几小时翻倍的币，按当前难度的理论值（往后看）和它差出两成以上就并列出来
        at_difficulty = None
        if (basis in {"observed", "observed_partial"} and theoretical and coins_per_day and
                price_value is not None and electricity_usd is not None and
                abs(theoretical / coins_per_day - 1) > 0.2):
            forward_net = theoretical * price_value - electricity_usd
            at_difficulty = {"coins_per_day": theoretical, "gross_usd_per_day": theoretical * price_value,
                             "net_usd_per_day": forward_net, "net_cny_per_day": forward_net / fx if fx else None}
        recent_view = None
        if recent and recent["coins_per_day"] is not None and coins_per_day:
            recent_net = (recent["coins_per_day"] * price_value - electricity_usd
                          if price_value is not None and electricity_usd is not None else None)
            recent_view = {"coins_per_day": recent["coins_per_day"], "window_seconds": recent["window_seconds"],
                           "delta_pct": (recent["coins_per_day"] / coins_per_day - 1) * 100,
                           "net_usd_per_day": recent_net,
                           "net_usd_per_gpu_per_day": recent_net / cards if recent_net is not None and cards else None,
                           "net_cny_per_gpu_per_day": recent_net / fx / cards if recent_net is not None and fx and cards else None}
        # 拿去和别的币比的那个数：在挖的币有近 6 小时实记就用它；按理论估的币若难度滞后，用难度追平后的数；
        # 其余用上面的日净收益
        # 粗估的误差范围（回测：实记比例校准的估算单点偏差一两成）；未校准的理论值给不出范围
        if recent_view and recent_view["net_usd_per_gpu_per_day"] is not None:
            guide_usd, guide_basis = recent_view["net_usd_per_gpu_per_day"], f"近 {recent_view['window_seconds'] / 3600:.0f} 小时实记"
            guide_error = 10 if recent_view["window_seconds"] >= 0.8 * OBSERVED_RECENT_SECONDS else 20
        elif lag and price_value is not None and electricity_usd is not None and cards:
            lag["net_usd_per_day"] = lag["coins_per_day"] * price_value - electricity_usd
            guide_usd, guide_basis = lag["net_usd_per_day"] / cards, basis_short + "·难度追平后"
            guide_error = None
        else:
            guide_usd, guide_basis = (net_usd / cards if net_usd is not None and cards else None), basis_short
            guide_error = (10 if basis in {"observed", "observed_partial"} else
                           15 if basis in {"theoretical_calibrated", "reference_calibrated"} else
                           20 if estimated and (network or {}).get("calibrated") else None)
        economics = {"coins_per_day": coins_per_day, "basis": basis, "basis_label": basis_label,
                     "basis_short": basis_short,
                     "theoretical_coins_per_day": theoretical,
                     "calibration": ({"ratio": calibration["ratio"], "measured_at": calibration["t"],
                                      "note": calibration.get("note")} if calibration else None),
                     "difficulty_lag": lag,
                     "observed": observed, "price_usd": price_value,
                     "price_basis": (price or {}).get("estimate_basis") or "现价",
                     "gross_usd_per_day": gross, "at_market_price": at_market,
                     "at_current_difficulty": at_difficulty,
                     "power_w": power_w, "power_basis": power_basis, "electricity_cny_per_day": electricity_cny,
                     "electricity_usd_per_day": electricity_usd, "net_usd_per_day": net_usd,
                     "net_cny_per_day": net_usd / fx if net_usd is not None and fx else None,
                     "gpu_count": cards,
                     "net_usd_per_gpu_per_day": net_usd / cards if net_usd is not None and cards else None,
                     "net_cny_per_gpu_per_day": net_usd / fx / cards if net_usd is not None and fx and cards else None,
                     "recent": recent_view,
                     "guide_usd_per_gpu_per_day": guide_usd,
                     "guide_cny_per_gpu_per_day": guide_usd / fx if guide_usd is not None and fx else None,
                     "guide_basis": guide_basis, "guide_error_pct": guide_error,
                     "hypothetical": not active,
                     "missing": missing, "calibrated": basis in {"observed", "observed_partial", "theoretical_calibrated",
                                                               "reference_calibrated"} or
                     bool((network or {}).get("calibrated")),
                     "reference_rate": reference_rate, "yesterday": yesterday}
        if balance and balance.get("threshold") and balance.get("pending") is not None:
            balance["progress_pct"] = min(100.0, balance["pending"] / balance["threshold"] * 100)
            remaining = balance["threshold"] - balance["pending"]
            # 停挖的币待付不会再涨，不给到门槛的预计时间
            balance["eta_hours"] = (remaining / coins_per_day * 24
                                    if active and remaining > 0 and coins_per_day and coins_per_day > 0 else None)

        # 好不好挖的一句话结论：全网算力、币价、单位算力收益三样相对 24 小时前的变化
        reasons, score = [], 0
        steady_net = net if net and not net["noisy"] else {}
        net_change = steady_net.get("change_24h_pct")
        if net_change is not None and (net_change >= NETWORK_SURGE_PCT or net_change <= NETWORK_DROP_PCT):
            score += -1 if net_change > 0 else 1
            reasons.append(f"全网算力 {net_change:+.0f}%")
        move = (price or {}).get("change_24h_pct")
        if move is not None and abs(move) >= PRICE_MOVE_PCT:
            score += 1 if move > 0 else -1
            reasons.append(f"币价 {move:+.0f}%")
        index_change = steady_net.get("yield_index_change_24h_pct")
        if index_change is not None and abs(index_change) >= YIELD_INDEX_MOVE_PCT:
            score += 1 if index_change > 0 else -1
            reasons.append(f"单位算力收益 {index_change:+.0f}%")
        signal_level = ("good" if score > 0 else "bad" if score < 0 else "mixed" if reasons else
                        "steady" if price or net else "unknown")
        signal = {"level": signal_level, "reasons": reasons,
                  "label": {"good": "更好挖", "bad": "变难挖", "mixed": "涨跌互抵", "steady": "平稳",
                            "unknown": "数据不足"}[signal_level]}
        feeds_status = []
        for kind in FEED_KINDS:
            if kind not in coin:
                continue
            value = ctx["feeds"].get((symbol, kind))
            fresh_value = self._feed(ctx["feeds"], coin, kind, now, active)
            feeds_status.append({"kind": kind, "label": FEED_LABELS[kind], "source": coin[kind]["source"],
                                 "fetched_at": value.get("fetched_at") if isinstance(value, dict) else None,
                                 "ok": fresh_value is not None,
                                 "error": None if fresh_value is not None else ctx["feed_errors"].get((symbol, kind))})

        board = ctx["leaderboard_tags"].get(coin["whattomine_tag"].upper()) if coin.get("whattomine_tag") else None
        devices = [f"GPU {index}" for index in gpu_indices] + (["CPU"] if cpu else [])
        return {"symbol": symbol, "name": coin["name"], "kind": coin.get("kind", "gpu"), "service": service,
                "active": active, "watch_only": bool(coin.get("watch_only")), "gpu_indices": gpu_indices, "devices": devices,
                "color": coin.get("color"), "algorithm": coin.get("algorithm"), "miner": coin.get("miner"),
                "market_note": coin.get("market_note"), "links": coin.get("links", []), "pools": coin.get("pools", []),
                "local": local, "pool": pool, "price": price, "network": net, "balance": balance,
                "daily": {"unit": daily["unit"], "days": daily["days"][-14:], "timezone": daily["day_timezone"],
                          "source": daily["source"]} if daily else None,
                "economics": economics, "signal": signal, "feeds": feeds_status,
                "leaderboard": {"rank": board["rank"], "index": board["index"]} if board else None,
                "feed_errors": {item["kind"]: item["error"] for item in feeds_status if item["error"]}}

    def _alerts(self, snapshot, projects, totals, now):
        """把值得人看一眼的事按轻重排好：危险 > 注意 > 机会 > 提示。"""
        alerts = []

        def add(level, key, title, detail, symbol=None):
            alerts.append({"id": key, "level": level, "symbol": symbol, "title": title, "detail": detail})

        rig = getattr(self, "rig", None)
        if rig is not None and rig.config_error is None:
            try:
                updates = json.loads((rig.state_dir / "updates.json").read_text())
                if isinstance(updates, dict):
                    for miner_id, row in updates.items():
                        if not isinstance(row, dict):
                            continue
                        if row.get("status") == "available":
                            add("info", "miner-update-" + miner_id, miner_id + " 有新版 " + str(row.get("tag", "")),
                                "到“挖矿控制”查看并升级；升级会短暂停止使用该软件的显卡。")
                        elif row.get("status") == "unavailable" or now - row.get("checked_at", 0) > 86400:
                            add("warning", "miner-update-check-" + miner_id, miner_id + " 版本检查不可用或已过期",
                                "当前矿工继续运行；到“挖矿控制”重新检查官方版本。")
            except (OSError, ValueError, TypeError):
                pass

        notices = getattr(self, "notices", None)
        if notices is not None:
            for symbol, row in notices.snapshot().items():
                unread = row.get("new", [])
                if unread:
                    add("info", "pool-notice-" + symbol, symbol + " 矿池有 " + str(len(unread)) + " 条未读更新",
                        str(unread[0].get("title", "")) + "；到“挖矿控制”的矿池提醒查看来源并确认已读。", symbol)
                if row.get("status") == "error" or (row.get("stale") and row.get("last_success_at")):
                    add("warning", "pool-notice-stale-" + symbol, symbol + " 矿池公告检查失败或已过期",
                        "保留上次公告；当前检查状态见“挖矿控制”，不能解释为没有新公告。", symbol)

        for item in snapshot.get("thermal", {}).get("alerts", []):
            add("critical" if item["severity"] == "critical" else "warning",
                f"thermal-{item['gpu_index']}-{item['sensor']}", item["message"],
                "已到预警线，检查风道与风扇，必要时在“调节”里降功耗。")
        for index in snapshot.get("gpu_conflicts", []):
            add("critical", f"conflict-{index}", f"GPU {index} 被两个矿工同时占用", "核对各矿工的显卡绑定，停掉其中一个。")
        missing = (snapshot.get("power") or {}).get("missing_gpu_indices") or []
        if missing:
            add("critical", "gpu-missing", "显卡掉线：" + "、".join(f"GPU {index}" for index in missing),
                "驱动读不到这张卡，检查供电与转接，必要时重启。")
        active_projects = [project for project in projects if project["active"]]
        if not active_projects:
            add("warning", "idle", "当前没有矿工在跑", "所有已配置项目都处于停止状态。")
        for project in projects:
            symbol = project["symbol"]
            if project["service"] == "failed":
                add("critical", f"failed-{symbol}", f"{symbol} 矿工服务异常退出", "查看该矿工日志后再重启。", symbol)
            if not project["active"]:
                continue
            local, pool, network = project["local"], project["pool"], project["network"] or {}
            if self._held(f"rate-{symbol}", local["status"] != "正常", now, RATE_FAULT_HOLD_SECONDS):
                add("warning", f"rate-{symbol}", f"{symbol} 本机算力采样异常", str(local["status"]), symbol)
            if pool.get("last_seen") and now - pool["last_seen"] > POOL_SILENT_SECONDS:
                add("warning", f"pool-silent-{symbol}", f"{symbol} 矿池 {round((now - pool['last_seen']) / 60)} 分钟没收到份额",
                    "矿工在跑但矿池没有新份额，检查网络与矿池连接。", symbol)
            low = pool.get("ratio") is not None and pool["ratio"] < POOL_LOW_RATIO and local["status"] == "正常"
            if self._held(f"pool-low-{symbol}", low, now, POOL_LOW_HOLD_SECONDS):
                add("warning", f"pool-low-{symbol}", f"{symbol} 池端算力只有本机的 {pool['ratio'] * 100:.0f}%",
                    f"{pool.get('hashrate_window') or '矿池窗口'}口径；持续偏低说明份额在丢，查过期份额与延迟。", symbol)
            if pool.get("bad_share_pct") is not None and pool["bad_share_pct"] > POOL_BAD_SHARE_PCT:
                add("warning", f"shares-{symbol}", f"{symbol} 无效/过期份额 {pool['bad_share_pct']:.1f}%",
                    "超过 3%，多半是到矿池的延迟高或超频不稳。", symbol)
            change = None if network.get("noisy") else network.get("change_24h_pct")
            if change is not None and change >= NETWORK_SURGE_PCT:
                add("warning", f"net-up-{symbol}", f"{symbol} 全网算力 +{change:.0f}%，变难挖了",
                    f"{network.get('change_basis')}；同样算力的产出会按比例下降。", symbol)
            balance = project["balance"] or {}
            if balance.get("progress_pct") is not None and balance["progress_pct"] >= 100:
                add("info", f"payout-{symbol}", f"{symbol} 已到打款门槛",
                    f"待付 {balance['pending']:.6g} {balance['unit']}，等矿池下一批打款。", symbol)
            if (network.get("pool_share_pct") or 0) >= POOL_DOMINANCE_PCT or (
                    network.get("volatile") and (network.get("pool_hashrate") or 0) >= (network.get("hashrate") or 0) / 2 > 0):
                share = "" if network.get("volatile") else f" {network['pool_share_pct']:.0f}%"
                add("info", f"pool-dominant-{symbol}", f"{symbol} 当前矿池算力占全网过半{share}",
                    "单一矿池过半，属中心化风险，留意项目公告。", symbol)
        for project in projects:
            symbol, network, price = project["symbol"], project["network"] or {}, project["price"] or {}
            change = None if network.get("noisy") else network.get("change_24h_pct")
            if change is not None and change <= NETWORK_DROP_PCT:
                add("opportunity", f"net-down-{symbol}", f"{symbol} 全网算力 {change:.0f}%，更好挖了",
                    f"{network.get('change_basis')}；同样算力能分到更多。", symbol)
            index_change = None if network.get("noisy") else network.get("yield_index_change_24h_pct")
            move = price.get("change_24h_pct")
            if move is not None and abs(move) >= PRICE_MOVE_PCT:
                rising = move > 0
                eaten = rising and index_change is not None and index_change < 0  # 涨价被全网算力的上涨吃掉了
                add("info" if eaten else "opportunity" if rising else ("warning" if project["active"] else "info"),
                    f"price-{symbol}", f"{symbol} 币价 24 小时 {'+' if rising else ''}{move:.0f}%",
                    f"但全网算力涨得更多，单位算力收益反而 {index_change:+.0f}%，不算更好挖。" if eaten else
                    "涨幅大时留意全网算力跟涨；小币种盘口薄，实际能卖的价格以交易所为准。" if rising else
                    "跌幅较大，按新价格看一眼是否还覆盖电费。", symbol)
            if index_change is not None and abs(index_change) >= YIELD_INDEX_MOVE_PCT:
                better = index_change > 0
                add("opportunity" if better else ("warning" if project["active"] else "info"), f"index-{symbol}",
                    f"{symbol} 单位算力收益 24 小时 {'+' if better else ''}{index_change:.0f}%",
                    "币价 × 出块奖励 ÷ 全网算力的综合变化。" + ("现在比昨天好挖。" if better else "现在比昨天难挖。"), symbol)
            net_value = project["economics"]["net_usd_per_day"]
            losing = bool(project["active"] and net_value is not None and net_value < 0)
            if self._held(f"loss-{symbol}", losing, now, ECONOMICS_ALERT_HOLD_SECONDS):
                add("warning", f"loss-{symbol}", f"{symbol} 估算在亏电费",
                    f"扣电费后约 ${net_value:.2f}/天（{project['economics']['basis_label']}）。", symbol)
        # 同一张显卡上，停用币种的估算净收益明显高于在挖的币种；两边的数都得是实记或已校准的才比。
        # 在挖的币有近 6 小时实记就拿它比（停用币种是按当下全网估的，24 小时均值在全网快涨时偏高）
        compared = set()
        for current in active_projects:
            mine = current["economics"]["guide_usd_per_gpu_per_day"]
            for other in projects:
                if other["active"] or not set(other["gpu_indices"]) & set(current["gpu_indices"]):
                    continue
                compared.add(f"switch-{current['symbol']}-{other['symbol']}")
                theirs = other["economics"]["guide_usd_per_gpu_per_day"]
                delta = (theirs - mine if mine is not None and theirs is not None and
                         current["economics"]["calibrated"] and other["economics"]["calibrated"] else None)
                # 两边的数各有一两成误差：差额落在误差范围里的不算“更划算”
                noise = (abs(mine) * (current["economics"]["guide_error_pct"] or 0) +
                         abs(theirs) * (other["economics"]["guide_error_pct"] or 0)) / 100 if delta is not None else 0
                key = f"switch-{current['symbol']}-{other['symbol']}"
                if self._held(key, delta is not None and delta >= max(self.profit_threshold_usd, noise), now,
                              ECONOMICS_ALERT_HOLD_SECONDS):
                    add("opportunity", key,
                        f"{other['symbol']} 估算比 {current['symbol']} 每卡多 ${delta:.2f}/天",
                        f"{other['symbol']}：{other['economics']['basis_label']}；{current['symbol']}：{current['economics']['guide_basis']}。"
                        "差额超出两边估算的误差范围，已连续成立 10 分钟；切换前先看该币项目说明。", other["symbol"])
        for stale in [key for key in self.condition_since if key.startswith("switch-") and key not in compared]:
            self.condition_since.pop(stale, None)  # 这一对已经不构成“换”（币种启停变了）：下次重新计时
        board = snapshot.get("leaderboard")
        if board:
            top = board["rows"][0]
            if top["tag"].upper() not in board["ours"]:
                add("info", f"wtm-{top['tag']}", f"WhatToMine 榜首是 {top['name']}（{top['tag']}）",
                    "这是它默认算力口径的相对指数，只当线索；是否值得挖要另查矿工、矿池与流动性。")
        exchange = snapshot.get("exchange") or {}
        if (exchange.get("public_api") or {}).get("state") == "upstream_blocked":
            add("info", "exchange-blocked", "SafeTrade 接口拦截了本机的程序访问",
                "交易所的 Cloudflare 防护对脚本请求返回 403，账户与下单在大屏不可用；行情改用聚合源，卖出请在浏览器操作。")
        system = snapshot.get("system") or {}
        if system.get("reboot_required"):
            add("info", "reboot", "系统更新已装好，等一次重启生效", "重启会中断挖矿几分钟，建议人在机器旁时进行，重启后核对矿工与大屏都自己起来了。")
        if system.get("disk_free_pct") is not None and system["disk_free_pct"] < DISK_FREE_WARN_PCT:
            add("warning", "disk", f"系统盘只剩 {system['disk_free_pct']:.0f}%", "清理模型、日志或回收站。")
        for project in projects:
            for kind, error in project["feed_errors"].items():
                if project["active"] or kind in {"price", "network"}:
                    add("info", f"feed-{project['symbol']}-{kind}", f"{project['symbol']} 的{FEED_LABELS[kind]}数据源暂时取不到",
                        f"{error}；恢复前相关数字留空，不拿旧值顶替。", project["symbol"])
        order = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
        alerts.sort(key=lambda alert: order[alert["level"]])
        with self.alert_lock:
            current = {alert["id"] for alert in alerts}
            for alert in alerts:
                alert["since"] = self.alert_since.setdefault(alert["id"], now)
                self.alert_titles[alert["id"]] = alert["title"]
            for stale in [key for key in self.alert_since if key not in current]:
                del self.alert_since[stale]
            if self.alert_recorded is not None and current != self.alert_recorded:
                try:
                    for alert in alerts:
                        if alert["id"] not in self.alert_recorded:
                            self._append_jsonl(self.alert_log_path, {"timestamp": now, "type": "raised", **alert})
                    for key in self.alert_recorded - current:
                        self._append_jsonl(self.alert_log_path, {"timestamp": now, "type": "cleared", "id": key,
                                                                 "title": self.alert_titles.pop(key, None)})
                except OSError as error:
                    print("ALERT_LOG_ERROR " + str(error)[:160], flush=True)
            self.alert_recorded = current
        return alerts

    def read_alert_log(self, limit=100):
        try:
            with self.alert_log_path.open("rb") as handle:
                handle.seek(max(0, self.alert_log_path.stat().st_size - 256_000))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            lines = []
        records = []
        for line in lines[-max(1, min(int(limit), 200)):]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"file": self.alert_log_path.name, "events": list(reversed(records))}

    def refresh_feeds(self, now=None, services=None):
        check_time = time.time() if now is None else now
        parsers = {"price": parse_price, "pool": parse_pool, "earnings": parse_earnings,
                   "account": parse_account, "daily": parse_daily, "network": parse_network}
        fetched = {}  # 同一轮里相同地址只取一次（多个币种共用一个聚合报价请求、同池的份额与结算接口）
        for coin in self.gpu_configs + self.market_configs:
            # 停矿币种只为行情雷达服务，降到 5 分钟一次，不再按 45 秒去打已停用项目的矿池。
            active = services is None or services.get(coin["symbol"]) == "active"
            for kind in FEED_KINDS:
                config = coin.get(kind)
                if not config:
                    continue
                key = (coin["symbol"], kind)
                interval = max(FEED_INTERVAL, config.get("max_age_seconds", FEED_DEFAULT_MAX_AGE[kind]) // 4)
                if not active and kind != "price":
                    interval = max(interval, INACTIVE_FEED_INTERVAL)
                # 取失败后不等满一个间隔：45 秒后重试，连续失败逐次翻倍，最长不超过正常间隔
                # （开机时代理还没就绪，第一轮常常失败；不该让低频数据源因此空上五分钟）
                failures = self.feed_failures.get(key, 0)
                wait = min(interval, FEED_INTERVAL * 2 ** (failures - 1)) if failures else interval
                if now is None and check_time - self.feed_attempts.get(key, 0) < wait - 1:
                    continue
                self.feed_attempts[key] = check_time
                self.feed_failures[key] = failures + 1  # 先记一次失败，成功后清零
                try:
                    url, _ = feed_url(coin, config)
                    if url is None:
                        self.feed_errors[key] = "收款地址来源不可读"
                        continue
                    fetched_at = time.time() if now is None else check_time
                    if url not in fetched:
                        fetched[url] = fetch_json(url)
                    value = parsers[kind](fetched[url], config, fetched_at)
                    if value is not None:
                        fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest() if kind in {"account", "earnings"} else None
                        with self.lock:
                            self.feeds[key] = value
                            if fingerprint:
                                self.feed_fingerprints[key] = fingerprint
                        self.feed_errors.pop(key, None)
                        self.feed_failures.pop(key, None)
                        if coin["symbol"] == "TSC" and kind == "earnings":
                            self.record_tsc_earnings(value, fingerprint)
                    else:
                        self.feed_errors[key] = "返回结构与配置不符"
                except Exception as error:  # 一个数据源出任何意外都只记在它自己头上，不连累后面的
                    self.feed_errors[key] = type(error).__name__
        if now is None and check_time - self.leaderboard_checked_at >= LEADERBOARD_INTERVAL:
            self.leaderboard_checked_at = check_time
            try:
                value = parse_leaderboard(fetch_json(LEADERBOARD_URL), time.time())
                if value is not None:
                    with self.lock:
                        self.leaderboard = value
            except Exception:
                pass
        if check_time >= self.fx_retry_after and check_time - self.fx_checked_at >= FX_REFRESH_INTERVAL:
            try:
                checked_at = time.time() if now is None else check_time
                value = parse_fx(fetch_json(FX_URL), checked_at)
                if value is not None:
                    with self.lock:
                        self.fx = value
                    self.fx_checked_at = checked_at
                    self.fx_retry_after = 0
                else:
                    self.fx_retry_after = check_time + FX_RETRY_INTERVAL
            except (OSError, ValueError, TimeoutError):
                self.fx_retry_after = check_time + FX_RETRY_INTERVAL

    def refresh_forever(self):
        while True:
            try:
                services = {coin["symbol"]: coin_runtime(coin)[0] for coin in self.gpu_configs}
                self.refresh_feeds(services=services)
            except Exception as error:  # 单个数据源的意外结构不能让整个抓取线程退出
                print("FEED_LOOP_ERROR " + str(error)[:200], flush=True)
            time.sleep(FEED_INTERVAL)

    def refresh_exchange_forever(self):
        while True:
            value = self._refresh_exchange()
            with self.lock:
                self.exchange = value
            time.sleep(EXCHANGE_CHECK_INTERVAL)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        runtime = [coin_runtime(coin) for coin in self.gpu_configs]
        services = {coin["symbol"]: status for status, coin in runtime}
        configs = [coin for _, coin in runtime]
        active = [coin for coin in configs if services[coin["symbol"]] == "active"]
        current = active[0] if len(active) == 1 else None
        active_symbols = [coin["symbol"] for coin in active]
        with self.rate_lock:
            rates = {}
            for coin in active:
                state = self.rate_states.setdefault(coin["symbol"], {})
                rates[coin["symbol"]] = (read_gpu_services_rate(coin, now, state) if coin.get("gpu_services")
                                         else read_rate(coin["rate"], now, state, coin.get("gpu_indices")))
        gpu_owners = {}
        for symbol, rate in rates.items():
            if rate["status"] == "正常":
                for gpu in rate["per_gpu"]:
                    gpu_owners.setdefault(gpu, []).append(symbol)
        gpu_conflicts = sorted(int(gpu) for gpu, owners in gpu_owners.items() if len(owners) > 1)
        with self.lock:
            feeds = dict(self.feeds)
            exchange = dict(self.exchange) if self.exchange else None
            fx = dict(self.fx) if self.fx else None

        def fresh(coin, kind):
            value = feeds.get((coin["symbol"], kind))
            config = coin.get(kind)
            if not config or not value or now - value["fetched_at"] > feed_max_age(
                    config, kind, services.get(coin["symbol"]) == "active"):
                return None
            if "source_at" in value and now - value["source_at"] > config.get("max_source_age_seconds", 600):
                return None
            return value

        gpus = gpu_status()
        thermal_alerts = []
        for gpu in gpus:
            for sensor, value, warn, critical in (
                    ("GPU核心", gpu.get("temperature"), GPU_TEMP_WARN_C, GPU_TEMP_CRITICAL_C),
                    ("显存", gpu.get("memory_temperature"), MEMORY_TEMP_WARN_C, MEMORY_TEMP_CRITICAL_C)):
                level = self._temperature_level(value, warn, critical)
                if level in {"warning", "critical"}:
                    thermal_alerts.append({"gpu_index": gpu["index"], "sensor": sensor,
                                           "severity": level, "celsius": value,
                                           "message": f"{gpu.get('display_title', 'GPU '+str(gpu['index']))} {sensor} {value:.0f}°C"})
        coin_rows = [{"symbol": c["symbol"], "name": c["name"], "service": services[c["symbol"]],
                      "gpu_indices": c.get("gpu_indices", []),
                      "rate": rates.get(c["symbol"]),
                      "pool": (rates.get(c["symbol"], {}).get("pool") or fresh(c, "pool")) if c["symbol"] in rates else None,
                      "pool_source": (rates.get(c["symbol"], {}).get("pool") or c.get("pool", {})).get("source"),
                      "earnings": fresh(c, "earnings"), "earnings_source": c.get("earnings", {}).get("source"),
                      "earnings_source_url": c.get("earnings", {}).get("link"),
                      "price": fresh(c, "price"), "price_source": c.get("price", {}).get("source"),
                      "price_source_url": c.get("price", {}).get("url")} for c in configs]
        hardware = hardware_status()
        noid = None
        if self.noid_config:
            try:
                control = noid_control("status")
                sample = noid_telemetry(control["pid"], now) if control.get("service") == "active" else {"status": "服务未运行"}
                noid = {**control, "telemetry": sample, "gpu_indices": [],
                        "price": fresh(self.noid_config, "price")}
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                noid = {"service": "unknown", "telemetry": {"status": str(error)[:160]},
                        "gpu_indices": [], "price": fresh(self.noid_config, "price")}
            noid["cpu_usage_percent"] = self.cpu_usage()
            cpu_temperatures = [sensor["celsius"] for sensor in hardware.get("temperatures", [])
                                if "coretemp" in sensor.get("chip", "") and
                                "Package" in sensor.get("label", "")]
            noid["cpu_temperature_c"] = cpu_temperatures[0] if cpu_temperatures else None
        profitability = self.profitability_snapshot(now, gpus, services, feeds, fx, configs)
        gpu_board_total_w = sum(gpu["power"] for gpu in gpus) if gpus else None
        expected_indices = getattr(self.rig, "expected_indices", None)
        if not isinstance(expected_indices, (list, tuple, set)):
            expected_indices = EXPECTED_GPU_INDICES
        missing_power_gpus = sorted(set(expected_indices) - {gpu["index"] for gpu in gpus})
        complete_power = gpu_board_total_w is not None and not missing_power_gpus
        result = {
            "generated_at": now,
            "controls_enabled": os.environ.get("MINER_DASHBOARD_CONTROLS") == "1",
            "exchange_enabled": os.environ.get("MINING_MONITOR_EXCHANGE", "0") != "0",
            "coin": " + ".join(active_symbols) if active_symbols else "未挖矿",
            "active_symbol": current["symbol"] if current else None,
            "active_symbols": active_symbols,
            "gpu_conflicts": gpu_conflicts,
            "rgb": rgb_snapshot(),
            "coins": coin_rows,
            "noid_cpu": noid,
            "gpus": gpus,
            "gpu_display_slots": display_slots(gpus),
            "power": {"gpu_board_total_w": round(gpu_board_total_w, 1) if gpu_board_total_w is not None else None,
                      "gpu_count": len(gpus), "wall_w": None, "wall_status": "未接入整机电表",
                      "estimated_system_w": round(gpu_board_total_w + ESTIMATED_HOST_POWER_W, 1) if complete_power else None,
                      "estimated_host_w": ESTIMATED_HOST_POWER_W,
                      "missing_gpu_indices": missing_power_gpus,
                      "estimated_system_basis": "全部显卡实时板卡功耗 + 主机余耗估算 90 W",
                      "estimated_system_status": ("估算值，非电表实测" if complete_power else
                                                  "缺少 " + ", ".join(f"GPU {index}" for index in missing_power_gpus) + " 的功耗读数")},
            "thermal": {"alerts": thermal_alerts, "sampled_at": now,
                        "memory_sensor_available": any(gpu.get("memory_temperature_supported") for gpu in gpus),
                        "memory_sensor_note": ("显存温度来自 gpu-vramtemp.service 读取的 GDDR6X 寄存器（非 NVIDIA 官方接口）"
                                               if any(gpu.get("memory_temperature_source") for gpu in gpus) else
                                               "驱动未提供显存温度；显示 N/A 并记录传感器不可用事件")},
            "profitability": profitability,
            "hardware": hardware,
            "nvidia_fans": nvidia_fan_rpm(),
            "exchange": {**(exchange or {}), "tunnel": "not_required",
                         "trading_enabled": os.environ.get("MINER_DASHBOARD_ALLOW_TRADING") == "1",
                         "trade_status": "已准备" if exchange and exchange.get("pairs") and exchange.get("balances") is not None else "停用",
                         "withdraw_status": "待官方接口与自有钱包核验", "csrf_token": self.csrf_token},
            "rate": rates[current["symbol"]] if current else {"status": "多币并行" if active else "矿工未运行"},
            "pool": fresh(current, "pool") if current else None,
            "pool_label": f"{current['symbol']} 矿池累计份额" if current and "pool" in current else None,
            "tuning": self.tuning_status(),
        }
        if (result["exchange"].get("public_api") or {}).get("state") == "upstream_blocked":
            result["exchange"]["diagnosis"] = ("SafeTrade 在 Cloudflare 上拦截了本机出口的程序请求（返回 403 拦截页），"
                                               "公共行情、账户、下单接口全部不可达；不是隧道或密钥故障。")
        fx_rate = fx.get("cny_to_usd") if isinstance(fx, dict) and now - fx.get("fetched_at", 0) <= FX_MAX_AGE else None
        with self.lock:
            leaderboard = self.leaderboard
        if leaderboard and now - leaderboard["fetched_at"] > LEADERBOARD_MAX_AGE:
            leaderboard = None
        ctx = {"now": now, "services": services, "rates": rates, "feeds": feeds, "gpus": gpus, "noid": noid,
               "fx_rate": fx_rate, "leaderboard_tags": leaderboard["all_tags"] if leaderboard else {},
               "feed_errors": dict(self.feed_errors)}  # 抓取线程会并发改这张表，先拷一份再读
        projects = [self._project(coin, ctx) for coin in configs]
        market_projects = [self._project(coin, ctx) for coin in self.market_configs]
        running = [project for project in projects if project["active"]]
        gross_parts = [project["economics"]["gross_usd_per_day"] for project in running]
        system_w = result["power"]["estimated_system_w"]
        electricity_cny = system_w / 1000 * 24 * ELECTRICITY_CNY_PER_KWH if system_w is not None else None
        electricity_usd = electricity_cny * fx_rate if electricity_cny is not None and fx_rate else None
        complete = bool(running) and all(part is not None for part in gross_parts)
        gross = sum(part for part in gross_parts if part is not None) if any(part is not None for part in gross_parts) else None
        net = gross - electricity_usd if complete and electricity_usd is not None else None
        # 有币的最新成交价高于估算用的保守价时，按成交价再合计一份
        market_net = (sum((project["economics"]["at_market_price"] or project["economics"])["gross_usd_per_day"]
                          for project in running) - electricity_usd
                      if net is not None and any(project["economics"]["at_market_price"] for project in running) else None)
        forward_net = (sum((project["economics"]["at_current_difficulty"] or project["economics"])["gross_usd_per_day"]
                           for project in running) - electricity_usd
                       if net is not None and any(project["economics"]["at_current_difficulty"] for project in running)
                       else None)
        totals = {"gross_usd_per_day": gross, "electricity_cny_per_day": electricity_cny,
                  "electricity_usd_per_day": electricity_usd, "net_usd_per_day": net,
                  "net_usd_per_day_at_difficulty": forward_net,
                  "net_cny_per_day_at_difficulty": forward_net / fx_rate if forward_net is not None and fx_rate else None,
                  "net_cny_per_day": net / fx_rate if net is not None and fx_rate else None,
                  "net_usd_per_day_at_market": market_net,
                  "net_cny_per_day_at_market": market_net / fx_rate if market_net is not None and fx_rate else None,
                  "gpu_count": len({index for project in running for index in project["gpu_indices"]}),
                  "gross_cny_per_day": gross / fx_rate if gross is not None and fx_rate else None,
                  "complete": complete, "fx_cny_to_usd": fx_rate,
                  "missing": [project["symbol"] for project in running
                              if project["economics"]["gross_usd_per_day"] is None],
                  "bases": [f"{project['symbol']} {project['economics']['basis_short']}" for project in running
                            if project["economics"]["basis_short"]],
                  "electricity_cny_per_kwh": ELECTRICITY_CNY_PER_KWH,
                  "energy_today": self.energy_today(now)}
        result.update(ui_version=ui_version(), projects=projects, market_projects=market_projects, totals=totals, system=self.system_status(),
                      leaderboard={**{key: leaderboard[key] for key in ("rows", "total", "source", "source_url", "fetched_at")},
                                   "ours": sorted({coin["whattomine_tag"].upper() for coin in self.gpu_configs
                                                   if coin.get("whattomine_tag")})}
                      if leaderboard else None)
        result["alerts"] = self._alerts(result, projects, totals, now)
        return result

    @staticmethod
    def system_status():
        """主机层面的几项体检数字，读不到的留空。"""
        info = {"reboot_required": REBOOT_REQUIRED_PATH.exists(), "disk_free_pct": None, "disk_free_gb": None,
                "uptime_seconds": None, "load_1m": None, "cpu_threads": os.cpu_count(),
                "memory_available_gb": None, "memory_total_gb": None}
        try:
            usage = shutil.disk_usage("/")
            info.update(disk_free_pct=usage.free / usage.total * 100, disk_free_gb=usage.free / 1e9)
        except OSError:
            pass
        try:
            info["uptime_seconds"] = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
            info["load_1m"] = os.getloadavg()[0]
            memory = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines()[:6]:
                name, _, rest = line.partition(":")
                memory[name] = int(rest.split()[0])
            info.update(memory_total_gb=memory["MemTotal"] / 1e6, memory_available_gb=memory["MemAvailable"] / 1e6)
        except (OSError, ValueError, IndexError, KeyError):
            pass
        return info


class Handler(BaseHTTPRequestHandler):
    monitor = None

    def _json_response(self, status, data):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _peer_is_owner(self):
        """对端是否与本服务同一 UID（经 root 助手改硬件、下单等写操作只给本账户）。"""
        try:
            local = self.connection.getsockname()
        except OSError:
            return False
        return loopback_peer_uid(self.client_address, local) == os.getuid()

    def do_POST(self):
        if os.environ.get("MINER_DASHBOARD_CONTROLS") != "1":
            return self._json_response(403, {"error": "Controls disabled; enable explicitly in your service environment"})
        if self.path == "/api/exchange/order/submit" and os.environ.get("MINER_DASHBOARD_ALLOW_TRADING") != "1":
            return self._json_response(403, {"error": "Exchange trading disabled; read-only by default"})
        if self.path not in {"/api/exchange/order/preview", "/api/exchange/order/submit",
                             "/api/mining/switch", "/api/profit-threshold", "/api/tune",
                             "/api/noid/threads", "/api/manual-price",
                             "/api/rig/assign", "/api/rig/gpu", "/api/rig/check-updates", "/api/rig/upgrade",
                             "/api/rig/notices/check", "/api/rig/notices/read", "/api/rgb"}:
            self.send_error(404)
            return
        if not self._peer_is_owner():
            self._json_response(403, {"error": "仅允许本机监控账户发起写操作"})
            return
        host = self.headers.get("Host", "")
        hostname = host.split(":", 1)[0].lower()
        origin = self.headers.get("Origin", "")
        length_text = self.headers.get("Content-Length", "")
        if (hostname not in {"127.0.0.1", "localhost"} or origin != f"http://{host}" or
                self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json" or
                not length_text.isdecimal() or not 1 <= int(length_text) <= (32768 if self.path == "/api/rig/notices/read" else 4096) or
                self.headers.get("X-CSRF-Token") != self.monitor.csrf_token):
            self._json_response(403, {"error": "请求来源或会话校验失败"})
            return
        try:
            body = json.loads(self.rfile.read(int(length_text)))
            if not isinstance(body, dict):
                raise ValueError("请求体格式无效")
            if self.path == "/api/rig/notices/check":
                response = self.monitor.notices.check_notices()
            elif self.path == "/api/rig/notices/read":
                response = self.monitor.notices.acknowledge_seen(body.get("seen"))
            elif self.path == "/api/rig/gpu":
                if set(body) != {"gpu_uuid", "action"} or not isinstance(body["gpu_uuid"], str) or not isinstance(body["action"], str) or body["action"] not in {"start", "stop"}:
                    raise ValueError("只允许显卡 UUID 与 start/stop 操作")
                response = self.monitor.rig.control_gpu(body["gpu_uuid"], body["action"])
            elif self.path == "/api/rig/assign":
                response = self.monitor.rig.assign(body.get("gpu_uuid"), body.get("symbol"))
            elif self.path == "/api/rig/check-updates":
                response = self.monitor.rig.check_updates()
            elif self.path == "/api/rig/upgrade":
                response = self.monitor.rig.apply_update(body.get("miner_id"), body.get("release_id"))
            elif self.path == "/api/mining/switch":
                rig = getattr(self.monitor, "rig", None)
                if rig is not None and rig.config_error is None:
                    raise RuntimeError("逐卡控制已接管，请到“挖矿控制”按卡操作")
                response = self.monitor.switch_project(body.get("symbol"))
            elif self.path == "/api/rgb":
                response = update_rgb_style(body)
            elif self.path == "/api/tune":
                response = self.monitor.apply_tuning(body)
            elif self.path == "/api/noid/threads":
                value = body.get("threads")
                if not strict_int(value) or not 1 <= value <= 12:
                    raise ValueError("线程数只允许 1–12 的整数")
                with self.monitor.noid_lock:
                    response = noid_control("set-threads", str(value))
            elif self.path == "/api/profit-threshold":
                response = self.monitor.set_profit_threshold(body.get("threshold_usd_per_day"))
            elif self.path == "/api/manual-price":
                response = self.monitor.set_manual_price(body.get("symbol"), body.get("price_usd"))
            elif self.path.endswith("/preview"):
                response = self.monitor.prepare_order(body.get("market"), body.get("amount"), body.get("price"))
            else:
                token = body.get("preview_token")
                if not isinstance(token, str):
                    raise ValueError("缺少有效预览凭据")
                response = self.monitor.submit_order(token)
            self._json_response(200, response)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json_response(400, {"error": str(error)})
        except RuntimeError as error:
            self._json_response(409, {"error": str(error)})
        except TuneUnavailable as error:
            self._json_response(503, {"error": str(error)})
        except OSError:
            self._json_response(503, {"error": "本机操作日志或设置无法写入；未完成请求"})
        except ExchangeError as error:
            self._json_response(503, {"error": "交易所响应异常；如已提交订单，先在交易所核对订单记录，切勿直接重试",
                                      "code": error.code, "http_status": error.status})

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path in {"/", "/index.html"}:
            try:
                payload = HTML_PATH.read_bytes()
            except OSError:
                self.send_error(503)
                return
            mime = "text/html; charset=utf-8"
        elif route.path == "/api/rgb":
            self._json_response(200, rgb_snapshot())
            return
        elif route.path == "/api/rig":
            try:
                snapshot = self.monitor.rig.snapshot()
            except (RuntimeError, OSError, subprocess.SubprocessError):
                snapshot = {"configured": False, "error": "GPU 或矿工状态读取失败，控制暂不可用", "gpus": {}, "updates": {}}
            snapshot["notices"] = self.monitor.notices.snapshot()
            if self._peer_is_owner() and snapshot.get("configured"):
                snapshot["csrf_token"] = self.monitor.csrf_token
            payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode()
            mime = "application/json; charset=utf-8"
        elif route.path == "/api/status":
            snapshot = self.monitor.snapshot()
            if not self._peer_is_owner() and isinstance(snapshot.get("exchange"), dict):
                snapshot["exchange"].pop("csrf_token", None)  # 写凭据只发给本账户的页面
            payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode()
            mime = "application/json; charset=utf-8"
        elif route.path == "/api/thermal-log":
            limit = parse_qs(route.query).get("limit", ["100"])[0]
            if not str(limit).isdecimal() or len(str(limit)) > 3:
                limit = "100"
            payload = json.dumps(self.monitor.read_thermal_log(limit), ensure_ascii=False, allow_nan=False).encode()
            mime = "application/json; charset=utf-8"
        elif route.path == "/api/alert-log":
            payload = json.dumps(self.monitor.read_alert_log(200), ensure_ascii=False, allow_nan=False).encode()
            mime = "application/json; charset=utf-8"
        elif route.path == "/api/history":
            minutes = parse_qs(route.query).get("minutes", ["360"])[0]
            minutes = int(minutes) if str(minutes).isdecimal() and len(str(minutes)) <= 4 else 360
            payload = json.dumps(self.monitor.history_payload(max(10, min(minutes, 2880))),
                                 ensure_ascii=False, allow_nan=False).encode()
            mime = "application/json; charset=utf-8"
        elif route.path in {"/gpu_fans_strip.mp4", "/gpu_fans_strip.jpg"}:
            path = FAN_VIDEO_PATH if route.path.endswith(".mp4") else FAN_POSTER_PATH
            try:
                payload = path.read_bytes()
            except OSError:
                self.send_error(503)
                return
            mime = "video/mp4" if path.suffix == ".mp4" else "image/jpeg"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_string, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    configs = load_configs(args.config_dir)
    if args.check_config:
        print("有效币种配置：" + ", ".join(item["symbol"] for item in configs))
        return
    monitor = Monitor(configs)
    monitor.load_history()
    Handler.monitor = monitor
    threading.Thread(target=monitor.refresh_forever, daemon=True).start()
    threading.Thread(target=monitor.record_history_forever, daemon=True).start()
    if os.environ.get("MINING_MONITOR_EXCHANGE", "0") != "0":
        threading.Thread(target=monitor.refresh_exchange_forever, daemon=True).start()
    threading.Thread(target=monitor.refresh_thermal_forever, daemon=True).start()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
