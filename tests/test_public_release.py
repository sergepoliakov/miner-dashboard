import http.client, importlib.util, json, os, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch, Mock
from http.server import ThreadingHTTPServer
import gpu_display, gpu_tune, mining_monitor as mm, rgb_control as rgb, safetrade_api_broker as broker, web_gateway as gateway
from safetrade_client import ExchangeError
ROOT=Path(__file__).resolve().parents[1]
U='GPU-00000000-0000-0000-0000-000000000001'
class ReleaseTests(unittest.TestCase):
    def test_trading_default_blocks_before_any_client_call(self):
        client=Mock()
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ExchangeError,'trading_disabled'):
            broker.dispatch('limit_sell',{'market':'prlusdt','amount':'1','price':'1'},client)
        self.assertEqual(client.mock_calls,[])
    def test_rgb_only_color_and_switch_persist_private(self):
        with tempfile.TemporaryDirectory() as d,patch.object(rgb,'STATE',Path(d)/'rgb.json'):
            value=rgb.update_style({'enabled':False,'color':'#AA22FF'})
            self.assertFalse(value['enabled']);self.assertEqual(value['color'],'#aa22ff')
            self.assertEqual(rgb.STATE.stat().st_mode & 0o777,0o600)
            self.assertEqual(value['period_seconds'],7.5)
            for bad in ({'enabled':True,'color':'#000000','mode':'static'},{'enabled':1,'color':'#000000'},{'enabled':True,'color':'red'}):
                with self.assertRaises(ValueError):rgb.update_style(bad)
    def test_display_uuid_survives_gpu_index_change(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'layout.json';p.write_text(json.dumps({'version':1,'slots':[{'number':3,'name':'DEMO','uuid':U}]}))
            for index in (0,4):
                result=gpu_display.decorate_gpus([{'index':index,'uuid':U,'name':'NVIDIA GPU'}],p)[0]
                self.assertEqual(result['display_title'],'GPU 3（DEMO）')
    def test_no_implicit_fan_mapping_or_allowed_user(self):
        if not Path('/etc/miner-dashboard/hardware.json').exists():
            self.assertEqual(gpu_tune.FAN_GROUPS,{})
            self.assertEqual(gpu_tune.SERVE_ALLOWED_USERS,())
    def test_examples_validate(self):
        coin=json.loads((ROOT/'examples/PRL.example.json').read_text());mm.validate_config(coin,Path('PRL.example.json'))
        from rig_control import RigController
        with tempfile.TemporaryDirectory() as d:
            r=RigController(ROOT/'examples/rig-control.example.json',Path(d));self.assertIsNone(r.config_error)
    def test_unconfigured_noid_gpu_adapter_fails_cleanly(self):
        c=json.loads((ROOT/'examples/PRL.example.json').read_text());c.update(symbol='NOID',kind='cpu_gpu',gpu_indices=[0])
        with self.assertRaises(ValueError):mm.validate_config(c,Path('bad.json'))
    def test_monitor_default_read_only_http(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),mm.Handler);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            with patch.dict(os.environ,{'MINER_DASHBOARD_CONTROLS':'0'}):
                conn=http.client.HTTPConnection(*server.server_address);conn.request('POST','/api/tune',b'{}',{'Content-Type':'application/json'});r=conn.getresponse();self.assertEqual(r.status,403);r.read();conn.close()
        finally:server.shutdown();server.server_close();t.join()
    def test_gateway_session_tamper_and_restart_persistence(self):
        cfg={'username':'demo','session_key':'00'*32}
        with tempfile.TemporaryDirectory() as d,patch.object(gateway,'SESSION_FILE',Path(d)/'session.sqlite3'):
            token=gateway.issue_session(cfg);cookie=gateway.COOKIE+'='+token
            self.assertTrue(gateway.valid_session(cookie,cfg));self.assertFalse(gateway.valid_session(cookie+'x',cfg))
            self.assertFalse(gateway.valid_session(cookie,cfg,time.time()+gateway.TTL+1))
    def test_gateway_anon_and_bad_origin_fail(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),gateway.Handler);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            with patch.object(gateway,'settings',return_value={'session_key':'00'*32,'username':'demo'}):
                for method,path,headers,expected in [('GET','/api/status',{'Host':gateway.HOST},401),('POST','/api/rgb',{'Host':gateway.HOST,'Origin':'https://attacker.invalid','Content-Type':'application/json'},403),('GET','/',{'Host':'invalid.example'},400)]:
                    conn=http.client.HTTPConnection(*server.server_address);conn.request(method,path,b'{}' if method=='POST' else None,headers);r=conn.getresponse();self.assertEqual(r.status,expected);r.read();conn.close()
        finally:server.shutdown();server.server_close();t.join()

class InstallerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('installer',ROOT/'scripts/install_system.py')
        cls.installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.installer)
    def test_existing_destination_refused_without_write(self):
        with tempfile.TemporaryDirectory() as d,patch.object(self.installer,'safe_parents'):
            p=Path(d)/'existing';p.write_text('original')
            with self.assertRaisesRegex(ValueError,'Refusing existing'):self.installer.preflight([p])
            self.assertEqual(p.read_text(),'original')
    def test_dangling_destination_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d,patch.object(self.installer,'safe_parents'):
            p=Path(d)/'link';p.symlink_to(Path(d)/'not-there')
            with self.assertRaises(ValueError):self.installer.preflight([p])
    def test_source_symlink_refused_before_read(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'app').mkdir();secret=root/'not-release';secret.write_text('sensitive-fixture')
            (root/'app/runtime_paths.py').symlink_to(secret)
            # Directory-ownership check is mocked only so NOFOLLOW can be tested as a non-root user.
            with patch.object(self.installer,'ROOT',root),patch.object(self.installer,'safe_parents'):
                with self.assertRaises(OSError):self.installer.source_snapshot()

if __name__=='__main__':unittest.main()
