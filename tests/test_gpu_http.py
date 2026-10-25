import http.client,json,os,threading,unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
import mining_monitor as mm
import web_gateway

class GpuHttpTests(unittest.TestCase):
    def setUp(self):
        self.monitor=Mock();self.monitor.csrf_token='synthetic-csrf';self.monitor.rig.control_gpu.return_value={'symbol':'STOP','status':'assigned'}
        self.handler=type('TestHandler',(mm.Handler,),{'monitor':self.monitor,'log_message':lambda *a:None})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),self.handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.cleanup)
    def cleanup(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,body,origin=None,csrf='synthetic-csrf'):
        host='%s:%s'%self.server.server_address
        c=http.client.HTTPConnection(*self.server.server_address)
        c.request('POST','/api/rig/gpu',json.dumps(body).encode(),{'Host':host,'Origin':origin or 'http://'+host,'Content-Type':'application/json','X-CSRF-Token':csrf})
        r=c.getresponse();code=r.status;data=json.loads(r.read());c.close();return code,data
    def test_only_registered_action_payload_reaches_controller(self):
        with patch.dict(os.environ,{'MINER_DASHBOARD_CONTROLS':'1'}),patch.object(self.handler,'_peer_is_owner',return_value=True):
            code,_=self.request({'gpu_uuid':'GPU-demo','action':'stop'});self.assertEqual(code,200)
            self.monitor.rig.control_gpu.assert_called_once_with('GPU-demo','stop')
    def test_unauthorized_or_extra_fields_never_mutate(self):
        with patch.dict(os.environ,{'MINER_DASHBOARD_CONTROLS':'1'}),patch.object(self.handler,'_peer_is_owner',return_value=True):
            self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'stop'},origin='https://invalid.example')[0],403)
            self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'stop'},csrf='wrong')[0],403)
            self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'stop','service':'arbitrary.service'})[0],400)
            self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':[]})[0],400)
            self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'restart'})[0],400)
        self.monitor.rig.control_gpu.assert_not_called()
    def test_default_off_and_other_uid_are_blocked(self):
        with patch.dict(os.environ,{'MINER_DASHBOARD_CONTROLS':'0'}):self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'start'})[0],403)
        with patch.dict(os.environ,{'MINER_DASHBOARD_CONTROLS':'1'}),patch.object(self.handler,'_peer_is_owner',return_value=False):self.assertEqual(self.request({'gpu_uuid':'GPU-demo','action':'start'})[0],403)
        self.monitor.rig.control_gpu.assert_not_called()
    def test_remote_gateway_explicitly_allows_same_control_path(self):self.assertIn('/api/rig/gpu',web_gateway.WRITE)
