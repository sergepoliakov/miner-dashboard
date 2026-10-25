"""Payout HTTP boundaries and account-lineage regressions, with synthetic data only."""
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
import mining_monitor as mm
from mining_profiles import ProfileError
import web_gateway


class PayoutHttpTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Mock(csrf_token='synthetic-csrf')
        self.monitor.mining_profiles.snapshot.return_value = {'configured': False, 'profiles': []}
        self.monitor.mining_profiles.save.return_value = {'ok': True}
        self.monitor.mining_profiles.apply.return_value = {'ok': True, 'payout_changed': False}
        self.handler = type('Handler', (mm.Handler,), {'monitor': self.monitor, 'log_message': lambda *a: None})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start(); self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, route, method='POST', csrf='synthetic-csrf', origin=None):
        host = '%s:%s' % self.server.server_address
        headers = {'Host': host, 'Origin': origin or 'http://' + host,
                   'Content-Type': 'application/json', 'X-CSRF-Token': csrf}
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request(method, route, None if method == 'GET' else b'{"symbol":"PRL"}', headers)
        response = conn.getresponse(); status = response.status; body = json.loads(response.read()); conn.close()
        return status, body

    def test_defaults_identity_and_csrf_protect_both_writes(self):
        for route in ['/api/mining-profiles/save', '/api/mining-profiles/apply']:
            with patch.dict(os.environ, {'MINER_DASHBOARD_CONTROLS': '0'}):
                self.assertEqual(self.request(route)[0], 403)
            with patch.dict(os.environ, {'MINER_DASHBOARD_CONTROLS': '1'}):
                with patch.object(self.handler, '_peer_is_owner', return_value=False):
                    self.assertEqual(self.request(route)[0], 403)
                with patch.object(self.handler, '_peer_is_owner', return_value=True):
                    self.assertEqual(self.request(route, csrf='wrong')[0], 403)
                    self.assertEqual(self.request(route, origin='https://attacker.invalid')[0], 403)
        self.monitor.mining_profiles.save.assert_not_called(); self.monitor.mining_profiles.apply.assert_not_called()

    def test_authorized_routes_and_explicit_failure_receipt(self):
        with patch.dict(os.environ, {'MINER_DASHBOARD_CONTROLS': '1'}), patch.object(self.handler, '_peer_is_owner', return_value=True):
            self.assertEqual(self.request('/api/mining-profiles/save')[0], 200)
            self.assertEqual(self.request('/api/mining-profiles/apply')[0], 200)
            self.monitor.mining_profiles.apply.side_effect = ProfileError('应用失败；回退不完整，请维护者核对')
            status, body = self.request('/api/mining-profiles/apply')
            self.assertEqual(status, 409); self.assertIn('回退不完整', body['error'])
        self.monitor.mining_profiles.save.assert_called_once_with({'symbol': 'PRL'})

    def test_private_read_and_remote_gateway_paths(self):
        with patch.object(self.handler, '_peer_is_owner', return_value=False):
            self.assertEqual(self.request('/api/mining-profiles', 'GET')[0], 403)
        with patch.object(self.handler, '_peer_is_owner', return_value=True):
            self.assertEqual(self.request('/api/mining-profiles', 'GET')[0], 200)
        self.assertIn('/api/mining-profiles', web_gateway.READ)
        for route in ['/api/mining-profiles/save', '/api/mining-profiles/apply']:
            self.assertIn(route, web_gateway.WRITE)


class AccountLineageTests(unittest.TestCase):
    def setUp(self):
        self.coin = {'symbol': 'PRL', 'earnings': {'url': 'https://pool.example/{address}', 'max_age_seconds': 600}}
        self.monitor = mm.Monitor.__new__(mm.Monitor)
        self.monitor.configs = [self.coin]; self.monitor.feed_fingerprints = {}
        self.url = 'https://pool.example/recipient-B'
        self.fingerprint = hashlib.sha256(self.url.encode()).hexdigest()

    def test_previous_account_cache_cannot_be_used_after_switch(self):
        with patch.object(mm, 'feed_url', return_value=(self.url, None)):
            old = {'fetched_at': 100, 'pending': 99, '_source_fingerprint': 'old-account'}
            self.assertIsNone(self.monitor._feed({('PRL', 'earnings'): old}, self.coin, 'earnings', 101, True))
            old['_source_fingerprint'] = self.fingerprint
            value = self.monitor._feed({('PRL', 'earnings'): old}, self.coin, 'earnings', 101, True)
            self.assertEqual(value['pending'], 99); self.assertNotIn('_source_fingerprint', value)
            self.assertEqual(self.monitor.account_source_fingerprint('PRL', 'earnings'), self.fingerprint)

    def test_previous_account_history_cannot_count_as_new_recipient_income(self):
        self.monitor.balances = Mock()
        self.monitor.balances.snapshot.return_value = [{'s': 'PRL', 't': t, 'p': t / 1000, 'i': 'old-account'} for t in range(100, 3701, 120)]
        self.monitor.telemetry = Mock()
        with patch.object(mm, 'feed_url', return_value=(self.url, None)):
            self.assertIsNone(self.monitor.observed_yield('PRL', 3700))

    def test_inflight_old_account_result_is_discarded(self):
        m = self.monitor; m.gpu_configs = [self.coin]; m.market_configs = []
        m.fx_retry_after = float('inf'); m.feed_attempts = {}; m.feed_failures = {}; m.feed_errors = {}; m.feeds = {}; m.lock = threading.Lock()
        self.coin['earnings']['url'] = 'https://pool.example/{address}'
        with patch.object(mm, 'feed_url', side_effect=[('https://pool.example/recipient-A', None), (self.url, None)]), patch.object(mm, 'fetch_json', return_value={}), patch.object(mm, 'parse_earnings', return_value={'fetched_at': 100}):
            m.refresh_feeds(now=100)
        self.assertEqual(m.feeds, {}); self.assertEqual(m.feed_fingerprints, {})

class GatewayPayoutTimeoutTests(unittest.TestCase):
    def test_slow_apply_has_bounded_gateway_wait_and_explicit_unavailable_response(self):
        handler = type('TimeoutHandler', (web_gateway.Handler,), {'owner': lambda self: True, 'log_message': lambda *a: None})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch.object(web_gateway.OPENER, 'open', side_effect=TimeoutError('synthetic slow upstream')) as call:
                conn = http.client.HTTPConnection(*server.server_address)
                conn.request('POST', '/api/mining-profiles/apply', b'{}',
                             {'Host': web_gateway.HOST, 'Origin': web_gateway.ORIGIN,
                              'Content-Type': 'application/json', 'X-CSRF-Token': 'synthetic-csrf'})
                response = conn.getresponse(); self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.read())['error'], 'monitor_temporarily_unavailable'); conn.close()
                self.assertEqual(call.call_args.kwargs['timeout'], 90)
        finally:
            server.shutdown(); server.server_close(); thread.join()
