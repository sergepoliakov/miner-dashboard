#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Local SafeTrade API broker with a narrow operation allowlist.

Runs as the dedicated safetrade-api user. The dashboard has access to the Unix
socket, never to the API credential file. No withdrawal operation exists here.
"""

import grp
import json
import os
import socketserver
from decimal import Decimal
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from safetrade_client import (ExchangeError, SafeTradeClient, available_pairs,
                              parse_positive_decimal)


SOCKET_PATH = Path("/run/safetrade-api/api.sock")
CREDENTIALS_PATH = Path("/var/lib/safetrade-api/credentials.json")
CLIENT = SafeTradeClient(CREDENTIALS_PATH)


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "items", "results"):
            if isinstance(value.get(key), list):
                return value[key]
    return None


def _balance_available(value, currency):
    rows = _rows(value)
    if rows is None:
        raise ExchangeError(None, "balance_unverified")
    for row in rows:
        if not isinstance(row, dict):
            continue
        unit = row.get("currency") or row.get("code")
        if isinstance(unit, dict):
            unit = unit.get("id") or unit.get("code")
        if not isinstance(unit, str) or unit.lower() != currency:
            continue
        available = row.get("available")
        if available is None and row.get("balance") is not None and row.get("locked") is not None:
            try:
                available = Decimal(str(row["balance"])) - Decimal(str(row["locked"]))
            except Exception as error:
                raise ExchangeError(None, "balance_unverified") from error
        try:
            amount = Decimal(str(available))
        except Exception as error:
            raise ExchangeError(None, "balance_unverified") from error
        if amount.is_finite() and amount >= 0:
            return amount
    raise ExchangeError(None, "balance_unverified")


def dispatch(operation, arguments, client=CLIENT):
    if not isinstance(arguments, dict):
        raise ValueError("invalid_arguments")
    if operation == "public_tickers" and not arguments:
        return client.public_tickers()
    if operation == "account" and not arguments:
        return client.account()
    if operation == "orders" and not arguments:
        return client.orders()
    if operation == "probe_account_collection" and set(arguments) == {"name"}:
        return client.probe_account_collection(arguments["name"])
    if operation == "limit_sell" and os.environ.get("MINER_DASHBOARD_ALLOW_TRADING") != "1":
        raise ExchangeError(None, "trading_disabled")
    if operation == "limit_sell" and set(arguments) == {"market", "amount", "price"}:
        market, amount, price = arguments["market"], arguments["amount"], arguments["price"]
        quantity = parse_positive_decimal(amount)
        parse_positive_decimal(price)
        if market not in available_pairs(client.public_tickers()):
            raise ExchangeError(None, "pair_unverified")
        base = market[:-4] if market.endswith("usdt") else market[:-3]
        if quantity > _balance_available(client.probe_account_collection("balances"), base):
            raise ExchangeError(None, "insufficient_balance")
        return client.limit_sell(market, amount, price)
    raise ValueError("operation_not_allowed")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = self.headers.get("Content-Length", "")
        if self.path != "/rpc" or self.headers.get("Content-Type") != "application/json" or not length.isdecimal() or not 1 <= int(length) <= 4096:
            self._reply(400, {"code": "bad_request"})
            return
        try:
            request = json.loads(self.rfile.read(int(length)))
            result = dispatch(request.get("operation"), request.get("arguments"))
            self._reply(200, result)
        except (ValueError, TypeError, AttributeError):
            self._reply(400, {"code": "bad_request"})
        except FileNotFoundError:
            self._reply(503, {"code": "credentials_missing"})
        except (PermissionError, OSError):
            self._reply(503, {"code": "unavailable"})
        except ExchangeError as error:
            self._reply(503, {"code": error.code, "http_status": error.status})

    def _reply(self, status, data):
        payload = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    with Server(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o660)
        os.chown(SOCKET_PATH, os.getuid(), grp.getgrnam(os.environ.get("MINER_DASHBOARD_GROUP", "miner-dashboard")).gr_gid)
        server.serve_forever()


if __name__ == "__main__":
    main()
