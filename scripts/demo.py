#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Synthetic, loopback-only demo. No miner, GPU, wallet or exchange is touched."""
import argparse, json, math, os, sys, tempfile, time, subprocess, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# Set all paths BEFORE importing app modules. A demo never reads a real profile.
PRIVATE = tempfile.TemporaryDirectory(prefix='miner-dashboard-demo-')
os.environ['MINER_DASHBOARD_CONFIG'] = PRIVATE.name + '/config'
os.environ['MINER_DASHBOARD_STATE'] = PRIVATE.name + '/state'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
import mining_monitor as mm

def uuid(i): return 'GPU-00000000-0000-0000-0000-' + str(i+1).zfill(12)
def unit(i): return f'demo-gpu-{i}.service'
class SimulatedHost:
    def __init__(self):
        self.states={unit(i):'active' for i in range(4)}
        self.boot={unit(i):'enabled' for i in range(4)}
    def gpus(self):return {uuid(i) for i in range(4)}
    def gpu_indices(self):return {uuid(i):i for i in range(4)}
    def compute(self):return {uuid(i):{unit(i)} for i in range(4) if self.states[unit(i)]=='active'}
    def run(self,args,timeout=20):
        action,name=args[2:4]
        if action=='is-active':return subprocess.CompletedProcess(args,0,self.states[name]+'\n','')
        if action=='is-enabled':return subprocess.CompletedProcess(args,0,self.boot[name]+'\n','')
        if action in {'start','stop'}:
            for unit_name in args[3:]:self.states[unit_name]='active' if action=='start' else 'inactive'
        elif action in {'enable','disable'}:
            for unit_name in args[3:]:self.boot[unit_name]='enabled' if action=='enable' else 'disabled'
        else:raise ValueError('Unsupported simulated command')
        return subprocess.CompletedProcess(args,0,'','')
HOST=SimulatedHost()
DEMO_LOCK=threading.RLock()
def running():return [i for i in range(4) if HOST.states[unit(i)]=='active']
STYLE={'version':1,'enabled':True,'color':'#004bff','ok':True,'effect':'breathing','period_seconds':7.5,'applied':False,'demo':True}
def cards():
    return [dict(index=i,uuid=uuid(i),name='NVIDIA GeForce RTX 4080',display_number=i+1,
                 display_name='DEMO '+chr(65+i),display_title=f'GPU {i+1}（DEMO {chr(65+i)}）',
                 display_model='RTX 4080 · 合成数据',temperature=[62,65,60,63][i],
                 memory_temperature=None,memory_temperature_supported=False,fan_percent=55+i*3,
                 power=round(259+i*2+math.sin(time.time()/15+i),1) if i in running() else 12,power_limit=280,
                 utilization=100 if i in running() else 0,clock=2550+i*15,core_clock=2550+i*15,memory_used=1024,memory_total=16384,
                 pci_bus_id=f'00000000:0{i+1}:00.0') for i in range(4)]
COIN={'symbol':'PRL','name':'Pearl · DEMO','service':unit(0),'gpu_indices':list(range(4)),'gpu_services':{str(i):unit(i) for i in range(4)},
      'rate':{'log':'/tmp/demo-miner.log','unit':'TH/s','gpu_regex':r'GPU (?P<gpu>\d+): (?P<value>[\d.]+)',
              'total_regex':r'Total: (?P<value>[\d.]+)','table_start_regex':'Speed','log_by_gpu':{str(i):'/tmp/demo-gpu-'+str(i)+'.log' for i in range(4)}},'color':'#86a9f2'}
mm.gpu_status=cards
mm.hardware_status=lambda:{'fans':[],'temperatures':[]}
mm.nvidia_fan_rpm=lambda:[]
mm.rgb_snapshot=lambda:dict(STYLE)
mm.display_slots=lambda gs:[dict(g,present=True,pending=False) for g in gs]
mm.service_status=lambda name:HOST.states.get(name,'inactive')
mm.read_gpu_services_rate=lambda *a,**k:{'status':'正常','total':sum(200+i for i in running()),'unit':'TH/s','per_gpu':{str(i):200+i for i in running()},'age_seconds':2,'window':'DEMO 60 秒合成采样'}
monitor=mm.Monitor([COIN])
from rig_control import RigController
RIG=Path(PRIVATE.name)/'rig.json'
RIG.write_text(json.dumps({'gpus':{uuid(i):{'label':f'GPU {i+1} DEMO','profiles':{'PRL':unit(i)}} for i in range(4)},'managed_services':{unit(i):[uuid(i)] for i in range(4)},'miners':{}}))
monitor.rig=RigController(RIG,Path(PRIVATE.name)/'control',runner=HOST.run,probe=HOST)
PAYOUT_SOURCE=Path(PRIVATE.name)/'simulated-miner.json'
PAYOUT_SOURCE.write_text(json.dumps({'wallet':'DEMO_NOT_CONNECTED','worker':'rig-01','power_limit':280}));PAYOUT_SOURCE.chmod(0o600)
PAYOUT_REGISTRY=Path(PRIVATE.name)/'payout-registry.json'
PAYOUT_REGISTRY.write_text(json.dumps({'providers':{'PRL':{'name':'Pearl','pool':'Pearlhash · DEMO（不连接）','services':[unit(i) for i in range(4)],'files':[{'path':str(PAYOUT_SOURCE),'fields':[{'path':['wallet'],'template':'{address}'},{'path':['worker'],'template':'{worker}'}]}]}}}));PAYOUT_REGISTRY.chmod(0o600)
from mining_profiles import MiningProfiles, ProfileError
monitor.mining_profiles=MiningProfiles(PAYOUT_REGISTRY,Path(PRIVATE.name)/'payout-state/profiles.json',lambda:monitor.rig)
monitor.tuning_status=lambda:{'ok':True,'gpus':[dict(index=g['index'],uuid=g['uuid'],clock={'current_mhz':g['clock'],'hw_max_mhz':3000,'locked_max_mhz':None,'step_mhz':50},power={'draw_w':g['power'],'limit_w':280,'default_w':320,'min_w':150,'max_w':320,'step_w':10}) for g in cards()],'fans':[],'errors':[]}
monitor.system_status=lambda:{'uptime_seconds':86400,'load_1m':1.8,'cpu_threads':16,'memory_total_gb':32,'memory_available_gb':24,'disk_free_gb':380,'disk_free_pct':76,'reboot_required':False}

def snapshot():
    now=time.time(); d=monitor.snapshot(now)
    p=d['projects'][0]
    p.update(price={'value':1.25,'kind':'feed','source':'DEMO 合成报价','at':now,'change_24h_pct':2.4},
             balance={'pending':0.45,'paid':12.0,'threshold':1.0,'accruing':0.45,'unit':'PRL','symbol':'PRL','progress_pct':45},
             pool={'hashrate_local_unit':805,'hashrate_window':'DEMO 10 分钟','ratio':.996,'accepted':1234,'rejected':0,'last_seen':now-8,'source':'DEMO'},
             signal={'level':'good','label':'平稳','reasons':['DEMO：采样正常']})
    count=len(running());ratio=count/4
    watts=d['power']['estimated_system_w'] or 0
    gross=10*ratio;electricity_cny=watts/1000*24*0.6;net=gross-electricity_cny*0.14
    p['economics'].update(coins_per_day=8*ratio,gross_usd_per_day=gross,net_usd_per_day=net,
                         electricity_usd_per_day=electricity_cny*0.14,basis_short='DEMO 合成估算',basis_label='演示数值；不代表实际收益',calibrated=False)
    d['totals'].update(gross_usd_per_day=gross,net_usd_per_day=net,net_cny_per_day=net/0.14,
                       gross_cny_per_day=gross/0.14,electricity_cny_per_day=electricity_cny,gpu_count=count,fx_cny_to_usd=.14,bases=['DEMO 合成数据'])
    p['network']={'hashrate':52e18,'hashrate_unit':'H/s','change_24h_pct':-3.2,'change_basis':'DEMO 较24小时前','yield_index':1.4e-14,'yield_index_change_24h_pct':5.5,'block_reward':2000,'block_seconds':160,'my_share_pct':0.00155,'source':'DEMO 合成全网','fetched_at':now,'noisy':False,'smoothed':False,'volatile':False}
    p['economics'].update(power_w=watts,net_cny_per_gpu_per_day=net/0.14/count if count else None,guide_cny_per_gpu_per_day=net/0.14/count if count else None,guide_usd_per_gpu_per_day=net/count if count else None,guide_basis='DEMO 合成值',missing=[])
    d['exchange_enabled']=True
    d['exchange']={'public_api':{'state':'unavailable'},'diagnosis':'DEMO：未连接交易所；所有账户数据均为空，禁止真实下单。','trade_status':'停用','csrf_token':'demo-only','pairs':[],'trading_enabled':False}
    d['alerts']=[{'id':'demo','level':'info','title':'DEMO 合成数据','detail':'无显卡/钱包/交易所连接','since':now}]
    d['controls_enabled']=True  # Only isolated fake-host GPU/payout mutations are enabled.
    d['demo']=True
    return d

def history():
    t=[time.time()-60*(89-i) for i in range(90)]
    return {'t':t,'minutes':90,'rates':{'PRL':[806+2*math.sin(i/5) for i in range(90)]},'pool_rates':{'PRL':[805]*90},
            'gpus':{str(g['index']):{'temp':[g['temperature']+math.sin(i/9) for i in range(90)],'power':[g['power']+math.sin(i/7) for i in range(90)],'memory_temp':[None]*90,'fan':[g['fan_percent']]*90} for g in cards()},'power':[1135]*90,'cpu_rate':[],'cpu_temp':[]}

class Handler(BaseHTTPRequestHandler):
    def reply(self,value,code=200,mime='application/json'):
        b=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def do_GET(self):
        path=self.path.split('?')[0]
        if path=='/':
            b=mm.HTML_PATH.read_text().replace('<body>','<body><div style="padding:9px;text-align:center;background:#233b62;color:white;font:14px system-ui">DEMO · 合成数据 · 逐卡启停仅操作模拟状态 · 不连接显卡、矿池或交易所</div>')
            return self.reply(b.encode(),mime='text/html; charset=utf-8')
        if path=='/api/status':return self.reply(snapshot())
        if path=='/api/history':return self.reply(history())
        if path=='/api/rig':return self.reply({**monitor.rig.snapshot(),'csrf_token':'demo-only','notices':{}})
        if path=='/api/mining-profiles':return self.reply({**monitor.mining_profiles.snapshot(),'controls_enabled':True})
        if path=='/api/rgb':return self.reply(STYLE)
        if path in ('/api/alert-log','/api/thermal-log'):return self.reply({'events':[],'entries':[]})
        return self.reply({'error':'demo_not_found'},404)
    def do_POST(self):
        # Only the isolated fake host can change. Hardware/trading mutations remain forbidden.
        if self.path not in {'/api/rig/gpu','/api/mining-profiles/save','/api/mining-profiles/apply'}:return self.reply({'error':'DEMO 禁止真实硬件、交易与其他写操作'},403)
        length=self.headers.get('Content-Length','')
        if (self.headers.get('Origin')!='http://'+self.headers.get('Host','') or self.headers.get('X-CSRF-Token')!='demo-only'):
            return self.reply({'error':'DEMO 来源或会话校验失败'},403)
        if not length.isdecimal() or not 1<=int(length)<=4096 or self.headers.get('Content-Type')!='application/json':return self.reply({'error':'Bad body'},400)
        try:
            body=json.loads(self.rfile.read(int(length)))
            with DEMO_LOCK:
                if self.path=='/api/rig/gpu':
                    if not isinstance(body,dict) or set(body)!={'gpu_uuid','action'} or not all(isinstance(v,str) for v in body.values()):raise ValueError('Invalid control request')
                    result=monitor.rig.control_gpu(body['gpu_uuid'],body['action'])
                elif self.path.endswith('/save'):result=monitor.mining_profiles.save(body)
                else:result=monitor.mining_profiles.apply(body)
            return self.reply({**result,'demo':True})
        except ProfileError as error:return self.reply({'error':str(error)},409)
        except (ValueError,RuntimeError) as error:return self.reply({'error':str(error) if self.path.startswith('/api/mining-profiles/') else '无效的模拟显卡操作'},400)
    def log_message(self,*a):pass

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=18091);args=parser.parse_args()
    print(f'DEMO http://127.0.0.1:{args.port} — synthetic data only',flush=True)
    with ThreadingHTTPServer(('127.0.0.1',args.port),Handler) as server:server.serve_forever()
if __name__=='__main__':main()
