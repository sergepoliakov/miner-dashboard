#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Create private config, discover GPU slots, or bind an existing miner unit."""
import argparse, getpass, hashlib, json, os, re, secrets, subprocess, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from runtime_paths import CONFIG_DIR, STATE_DIR

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')

def discover():
    result=subprocess.run(['nvidia-smi','--query-gpu=index,uuid,name','--format=csv,noheader'],capture_output=True,text=True,check=True,timeout=10)
    import csv
    return [(int(i),u.strip(),n.strip()) for i,u,n in csv.reader(result.stdout.splitlines(),skipinitialspace=True)]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','discover','bind','web-auth'])
    p.add_argument('--symbol',default='PRL');p.add_argument('--service');p.add_argument('--log',type=Path)
    p.add_argument('--uuid',action='append');p.add_argument('--username',default='admin')
    a=p.parse_args()
    CONFIG_DIR.mkdir(mode=0o700,parents=True,exist_ok=True);STATE_DIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    (CONFIG_DIR/'coins.d').mkdir(mode=0o700,exist_ok=True)
    if a.action=='init':print('Config directory created outside the Git clone. No miner started.');return
    if a.action=='web-auth':
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}',a.username):p.error('Invalid username')
        password=getpass.getpass('New dashboard password (at least 12 characters): ')
        if len(password)<12 or password!=getpass.getpass('Repeat password: '):p.error('Password too short or does not match')
        salt=secrets.token_bytes(32);rounds=600000
        save(CONFIG_DIR/'web-auth.json',{'username':a.username,'salt':salt.hex(),'iterations':rounds,'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,rounds).hex(),'session_key':secrets.token_hex(32)})
        print('Private password hash saved. No default password is included.');return
    gpus=discover()
    if a.action=='discover':
        save(CONFIG_DIR/'gpu-display.json',{'version':1,'slots':[{'number':i+1,'uuid':u,'name':n} for i,(_,u,n) in enumerate(gpus)]})
        print('Private UUID layout saved. Edit names/order in gpu-display.json.');return
    if not a.uuid or not a.service or not a.log or not a.log.is_absolute():p.error('bind requires --uuid (repeat per card), --service, absolute --log')
    if not re.fullmatch(r'[A-Z0-9]{2,12}',a.symbol) or not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',a.service):p.error('Invalid symbol/service')
    wanted=set(a.uuid);live={u:(i,n) for i,u,n in gpus}
    if len(wanted)!=len(a.uuid) or not wanted.issubset(live):p.error('UUID missing, repeated, or not present')
    service=subprocess.run(['systemctl','--user','show',a.service,'-p','LoadState','--value'],capture_output=True,text=True,check=True,timeout=10)
    if service.stdout.strip()!='loaded':p.error('Miner service must already exist')
    # One safe initial profile. Existing binding is never silently overwritten.
    cfg={'gpus':{u:{'label':live[u][1],'index':live[u][0],'profiles':{a.symbol:a.service}} for u in a.uuid},'managed_services':{a.service:a.uuid},'miners':{}}
    coin={'symbol':a.symbol,'name':a.symbol,'service':a.service,'gpu_indices':[live[u][0] for u in a.uuid],
          'rate':{'log':str(a.log),'unit':'TH/s','gpu_regex':r'GPU\s*#?(?P<gpu>\d+).*?(?P<value>[\d.]+)\s*TH/s','total_regex':r'Total.*?(?P<value>[\d.]+)\s*TH/s','table_start_regex':r'(?:GPU|Total|Speed)','max_age_seconds':120}}
    from mining_monitor import validate_config
    validate_config(coin,Path('generated.json'))
    for path in (CONFIG_DIR/'rig-control.json',CONFIG_DIR/'coins.d'/f'{a.symbol}.json'):
        if path.exists():p.error('Binding already exists; edit private configuration explicitly')
    save(CONFIG_DIR/'rig-control.json',cfg);save(CONFIG_DIR/'coins.d'/f'{a.symbol}.json',coin)
    print('Private mapping saved. Confirm regex/unit against YOUR miner log; no service started.')
if __name__=='__main__':main()
