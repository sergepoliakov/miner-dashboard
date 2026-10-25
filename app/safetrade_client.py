# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Optional SafeTrade adapter. Credential format follows official API examples.
Account endpoints need live verification for each deployment; no live trading
certification is implied by mock tests. No withdrawal mutation is implemented.
"""

import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import stat
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


HOST = "safe.trade"
BASE = "/api/v2"
SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = int(os.environ.get("SAFETRADE_SOCKS_PORT", "0"))
MAX_RESPONSE = 1_000_000
PAIR_CODES = ("prlusdt", "prlbtc", "tscusdt", "tscbtc")


class ExchangeError(Exception):
    def __init__(self, status, code="unavailable"):
        self.status = status
        self.code = code
        super().__init__(f"SafeTrade {code} ({status})")


def _read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise ExchangeError(None, "tunnel_closed")
        result.extend(chunk)
    return bytes(result)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, timeout=9):
        super().__init__(HOST, timeout=timeout, context=ssl.create_default_context())

    def connect(self):
        raw = socket.create_connection((SOCKS_HOST, SOCKS_PORT), self.timeout)
        try:
            raw.sendall(b"\x05\x01\x00")
            if _read_exact(raw, 2) != b"\x05\x00":
                raise ExchangeError(None, "socks_auth")
            name = HOST.encode("ascii")
            raw.sendall(b"\x05\x01\x00\x03" + bytes((len(name),)) + name + (443).to_bytes(2, "big"))
            answer = _read_exact(raw, 4)
            if answer[:2] != b"\x05\x00":
                raise ExchangeError(None, "socks_denied")
            address_size = {1: 4, 4: 16}.get(answer[3])
            if answer[3] == 3:
                address_size = _read_exact(raw, 1)[0]
            if address_size is None:
                raise ExchangeError(None, "socks_reply")
            _read_exact(raw, address_size + 2)
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
        except BaseException:
            raw.close()
            raise


def parse_positive_decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,12})?", value):
        raise ValueError("金额必须是十进制字符串")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("金额格式无效") from error
    if not number.is_finite() or number <= 0 or number.as_tuple().exponent < -12:
        raise ValueError("金额必须为正，最多 12 位小数")
    return number


def configured_credentials(path):
    path = Path(path)
    file_stat = path.stat()
    if file_stat.st_uid != os.getuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise PermissionError("交易所凭据文件权限不安全")
    data = json.loads(path.read_text())
    key, secret = data.get("key"), data.get("secret")
    if not isinstance(key, str) or not isinstance(secret, str) or not key or not secret:
        raise ValueError("交易所凭据不完整")
    return key, secret


class SafeTradeClient:
    def __init__(self, credentials_path):
        self.credentials_path = Path(credentials_path)
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def _auth_headers(self):
        key, secret = configured_credentials(self.credentials_path)
        with self._nonce_lock:
            self._last_nonce = max(time.time_ns() // 1_000_000, self._last_nonce + 1)
            nonce = str(self._last_nonce)
        signature = hmac.new(secret.encode(), (nonce + key).encode(), hashlib.sha256).hexdigest()
        return {"X-Auth-Apikey": key, "X-Auth-Nonce": nonce,
                "X-Auth-Signature": signature, "Content-Type": "application/json;charset=utf-8"}

    def request(self, method, path, *, data=None, authenticated=False):
        if method not in {"GET", "POST"} or not path.startswith("/") or "?" in path or "//" in path:
            raise ValueError("API 路径或方法无效")
        headers = {"Accept": "application/json", "User-Agent": "GPU-Mining-Monitor/2.0"}
        if authenticated:
            headers.update(self._auth_headers())
        body = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        conn = _SocksHTTPSConnection() if SOCKS_PORT else http.client.HTTPSConnection(HOST, timeout=9, context=ssl.create_default_context())
        try:
            conn.request(method, BASE + path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if response.status not in (200, 201):
                raise ExchangeError(response.status, "blocked" if response.status == 403 else "http_error")
            if len(raw) > MAX_RESPONSE:
                raise ExchangeError(response.status, "too_large")
            if "json" not in response.getheader("Content-Type", "").lower():
                raise ExchangeError(response.status, "non_json")
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError) as error:
                raise ExchangeError(response.status, "invalid_json") from error
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise ExchangeError(None, "transport") from error
        finally:
            conn.close()

    def public_tickers(self):
        return self.request("GET", "/trade/public/tickers")

    def account(self):
        return self.request("GET", "/trade/account/members/me", authenticated=True)

    def orders(self):
        return self.request("GET", "/trade/market/orders", authenticated=True)

    def limit_sell(self, market, amount, price):
        if market not in PAIR_CODES:
            raise ValueError("交易对未获准")
        quantity, limit = parse_positive_decimal(amount), parse_positive_decimal(price)
        return self.request("POST", "/trade/market/orders", authenticated=True,
                            data={"market": market, "side": "sell", "amount": str(quantity),
                                  "type": "limit", "price": str(limit)})

    def probe_account_collection(self, name):
        """Only read-only probes; 404 is not interpreted as empty account data."""
        paths = {
            "balances": ("/trade/account/balances/spot", "/trade/account/balances", "/account/balances"),
            "deposits": ("/trade/account/deposits", "/account/deposits"),
            "withdrawals": ("/trade/account/withdraws", "/account/withdraws"),
            "trades": ("/trade/market/trades", "/market/trades"),
            "beneficiaries": ("/trade/account/beneficiaries",),  # 提币地址簿，只读
        }
        if name not in paths:
            raise ValueError("未知账户集合")
        for path in paths[name]:
            try:
                return self.request("GET", path, authenticated=True)
            except ExchangeError as error:
                if error.status != 404:
                    raise
        raise ExchangeError(404, "endpoint_unverified")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost", timeout=40)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class SafeTradeProxyClient:
    """Monitor-side facade: no credentials or arbitrary API paths."""

    def __init__(self, socket_path="/run/safetrade-api/api.sock"):
        self.socket_path = socket_path

    def _rpc(self, operation, **arguments):
        conn = _UnixConnection(self.socket_path)
        try:
            body = json.dumps({"operation": operation, "arguments": arguments}).encode()
            conn.request("POST", "/rpc", body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raw = response.read(4097)
                try:
                    error = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    error = {}
                raise ExchangeError(error.get("http_status"), error.get("code", "broker_error"))
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ExchangeError(None, "too_large")
            return json.loads(raw)
        except (OSError, http.client.HTTPException) as error:
            raise ExchangeError(None, "broker_unavailable") from error
        finally:
            conn.close()

    def public_tickers(self):
        return self._rpc("public_tickers")

    def account(self):
        return self._rpc("account")

    def orders(self):
        return self._rpc("orders")

    def probe_account_collection(self, name):
        return self._rpc("probe_account_collection", name=name)

    def limit_sell(self, market, amount, price):
        return self._rpc("limit_sell", market=market, amount=amount, price=price)


def available_pairs(tickers):
    """Only explicit pair IDs count. Unknown response shapes yield no trades."""
    source = tickers
    if isinstance(tickers, dict):
        for key in ("data", "tickers", "result"):
            if isinstance(tickers.get(key), (dict, list)):
                source = tickers[key]
                break
    found = set()
    if isinstance(source, dict):
        for key in source:
            if isinstance(key, str):
                found.add(key.lower().replace("/", "").replace("-", ""))
    elif isinstance(source, list):
        for row in source:
            if isinstance(row, dict):
                key = row.get("market") or row.get("id") or row.get("symbol")
                if isinstance(key, str):
                    found.add(key.lower().replace("/", "").replace("-", ""))
    return sorted(found.intersection(PAIR_CODES))
