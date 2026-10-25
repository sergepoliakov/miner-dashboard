# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import hmac
import json
import os
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import mining_monitor
import safetrade_api_broker
from safetrade_client import ExchangeError, SafeTradeClient, SafeTradeProxyClient, available_pairs, configured_credentials, parse_positive_decimal


class SafeTradeClientTests(unittest.TestCase):
    def test_signature_matches_official_hmac_shape_and_never_exposes_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"key": "test-key", "secret": "test-secret"}))
            os.chmod(path, 0o600)
            client = SafeTradeClient(path)
            headers = client._auth_headers()
            expected = hmac.new(b"test-secret", (headers["X-Auth-Nonce"] + "test-key").encode(), hashlib.sha256).hexdigest()
            self.assertEqual(headers["X-Auth-Signature"], expected)
            self.assertNotIn("test-secret", repr(headers))
            self.assertGreater(int(client._auth_headers()["X-Auth-Nonce"]), int(headers["X-Auth-Nonce"]))

    def test_secret_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"key": "a", "secret": "b"}))
            os.chmod(path, 0o644)
            with self.assertRaises(PermissionError):
                configured_credentials(path)

    def test_unknown_pairs_and_invalid_amount_fail_closed(self):
        self.assertEqual(available_pairs({"data": [{"market": "PRL/USDT"}, {"market": "BTC/USDT"}]}), ["prlusdt"])
        self.assertEqual(available_pairs({"unexpected": {"price": "1"}}), [])
        for amount in ("0", "-1", "NaN", "1e999", "1.1234567890123"):
            with self.assertRaises(ValueError):
                parse_positive_decimal(amount)

    def test_public_block_does_not_read_credentials(self):
        rig = mining_monitor.Monitor([])
        with patch.object(rig.exchange_client, "public_tickers", side_effect=ExchangeError(403, "blocked")), \
             patch.object(rig.exchange_client, "account", side_effect=AssertionError("credentials read")):
            result = rig._refresh_exchange()
        self.assertEqual(result["public_api"]["state"], "upstream_blocked")
        self.assertEqual(result["account_status"], "未接入")

    def test_order_requires_verified_pair_balance_and_is_one_shot(self):
        rig = mining_monitor.Monitor([])
        rig.exchange = {"checked_at": time.time(), "public_api": {"state": "public_api_ready"},
                        "account_status": "已验证", "pairs": ["prlusdt"],
                        "balances": [{"currency": "PRL", "balance": "3", "available": "2"}]}
        with self.assertRaisesRegex(ValueError, "余额不足"):
            rig.prepare_order("prlusdt", "2.1", "1.2")
        with self.assertRaisesRegex(ValueError, "交易对"):
            rig.prepare_order("tscbtc", "1", "1.2")
        preview = rig.prepare_order("prlusdt", "1.5", "1.2")
        self.assertEqual(preview["gross_quote"], "1.80")
        with patch.object(rig.exchange_client, "limit_sell", return_value={"id": 123, "state": "wait"}) as submit:
            self.assertEqual(rig.submit_order(preview["preview_token"])["id"], "123")
            with self.assertRaisesRegex(ValueError, "过期"):
                rig.submit_order(preview["preview_token"])
            submit.assert_called_once_with("prlusdt", "1.5", "1.2")

    def test_balances_without_available_or_locked_are_not_tradeable(self):
        self.assertEqual(mining_monitor.Monitor._balances([{"currency": "PRL", "balance": "5"}]), [])
        self.assertEqual(mining_monitor.Monitor._balances([{"currency": "PRL", "balance": "5", "locked": "2"}]),
                         [{"currency": "PRL", "balance": "5", "available": "3"}])

    def test_account_probe_requires_real_member_and_balance_shape(self):
        rig = mining_monitor.Monitor([])
        with patch.object(rig.exchange_client, "public_tickers", return_value={"data": {"prlusdt": {"last": "1"}}}), \
             patch.object(rig.exchange_client, "account", return_value={"uid": "sample"}), \
             patch.object(rig.exchange_client, "probe_account_collection", side_effect=lambda name: {
                 "balances": [{"currency": "PRL", "balance": "3", "locked": "1"}],
                 "deposits": [{"currency": "PRL", "amount": "1", "state": "accepted", "address": "must-not-leak"}],
                 "withdrawals": [], "trades": [],
                 "beneficiaries": [{"id": 1, "currency": "usdt", "name": "冷钱包", "state": "active",
                                    "blockchain_key": "tron", "data": {"address": "must-not-leak"}}]}[name]), \
             patch.object(rig.exchange_client, "orders", return_value=[]):
            result = rig._refresh_exchange()
        self.assertEqual(result["pairs"], ["prlusdt"])
        self.assertEqual(result["balances"][0]["available"], "2")
        self.assertEqual(result["account_status"], "已验证")
        self.assertNotIn("address", result["deposits"][0])
        self.assertEqual(result["beneficiaries"], [{"id": "1", "currency": "usdt", "name": "冷钱包",
                                                    "state": "active", "blockchain_key": "tron"}])   # 地址不进输出

        with patch.object(rig.exchange_client, "public_tickers", return_value={"prlusdt": {}}), \
             patch.object(rig.exchange_client, "account", return_value={"error": "unauthorized"}), \
             patch.object(rig.exchange_client, "probe_account_collection", side_effect=AssertionError("must not probe")):
            result = rig._refresh_exchange()
        self.assertEqual(result["account_status"], "返回结构待核验")
        self.assertIsNone(result["balances"])

    @patch.dict(os.environ, {"MINER_DASHBOARD_ALLOW_TRADING":"1"})
    def test_broker_refuses_withdrawal_and_rechecks_pair_and_balance(self):
        class FakeClient:
            calls = 0
            def public_tickers(self):
                return {"data": {"prlusdt": {}}}
            def probe_account_collection(self, name):
                return [{"currency": "prl", "balance": "2", "locked": "1"}]
            def limit_sell(self, market, amount, price):
                self.calls += 1
                return {"id": "test-order"}
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "operation_not_allowed"):
            safetrade_api_broker.dispatch("withdraw", {"address": "other"}, client)
        with self.assertRaises(ExchangeError):
            safetrade_api_broker.dispatch("limit_sell", {"market": "tscbtc", "amount": "0.5", "price": "1"}, client)
        with self.assertRaises(ExchangeError):
            safetrade_api_broker.dispatch("limit_sell", {"market": "prlusdt", "amount": "1.5", "price": "1"}, client)
        self.assertEqual(client.calls, 0)
        self.assertEqual(safetrade_api_broker.dispatch("limit_sell", {"market": "prlusdt", "amount": "0.5", "price": "1"}, client)["id"], "test-order")
        self.assertEqual(client.calls, 1)

    def test_monitor_broker_unix_socket_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "api.sock")
            try:
                server = safetrade_api_broker.Server(socket_path, safetrade_api_broker.Handler)
            except PermissionError:
                self.skipTest("当前沙箱禁止创建 Unix socket；矿机现场另行验证")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            with patch.object(safetrade_api_broker, "dispatch", side_effect=lambda op, args: {"prlusdt": {}} if op == "public_tickers" else (_ for _ in ()).throw(ValueError("denied"))):
                worker.start()
                try:
                    proxy = SafeTradeProxyClient(socket_path)
                    self.assertEqual(proxy.public_tickers(), {"prlusdt": {}})
                    with self.assertRaises(ExchangeError):
                        proxy._rpc("withdraw", address="arbitrary")
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
