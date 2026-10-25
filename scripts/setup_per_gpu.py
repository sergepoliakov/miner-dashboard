#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Plan/install private single-GPU user services, without starting or stopping any miner."""
import argparse, hashlib, json, os, re, subprocess, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from runtime_paths import CONFIG_DIR, STATE_DIR
from per_gpu_miner import inventory, load_profile, UUID
ROOT=Path(__file__).resolve().parents[1]

def quote(value):
    if any(ord(c)<32 for c in str(value)):raise ValueError('Control character in unit field')
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'

def plan(symbol,profile_path,selected,gpus,config_dir=CONFIG_DIR,state_dir=STATE_DIR,unit_dir=None):
    if not re.fullmatch(r'[A-Z0-9]{2,12}',symbol):raise ValueError('Invalid symbol')
    if not selected or len(set(selected))!=len(selected) or any(not UUID.fullmatch(u) or u not in gpus for u in selected):raise ValueError('Choose unique present GPU UUIDs')
    profile_path=Path(profile_path).resolve();profile=load_profile(profile_path)
    unit_dir=Path(unit_dir) if unit_dir else Path.home()/'.config/systemd/user'
    logs=Path(state_dir)/'miner-logs';files={};units={};rig={'gpus':{},'managed_services':{},'miners':{}}
    rate={'log':str(logs/'unused.log'),'unit':'TH/s','gpu_regex':r'GPU\s*#?(?P<gpu>\d+).*?(?P<value>[\d.]+)\s*TH/s','total_regex':r'Total.*?(?P<value>[\d.]+)\s*TH/s','table_start_regex':'Speed','max_age_seconds':120,'log_by_uuid':{}}
    for uuid in selected:
        short=hashlib.sha256(uuid.encode()).hexdigest()[:12];name=f'miner-{symbol.lower()}-{short}.service';log=logs/(name+'.log');units[uuid]=name
        cwd=profile.get('cwd',str(Path(profile['argv'][0]).parent))
        body=f'''[Unit]
Description=Miner Dashboard single GPU {symbol} {short}
After=network-online.target
[Service]
Type=simple
ExecStart={quote(sys.executable)} {quote(ROOT/'app/per_gpu_miner.py')} --profile {quote(profile_path)} --uuid {quote(uuid)}
WorkingDirectory={quote(cwd)}
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=20
NoNewPrivileges=yes
UMask=0077
StandardOutput=append:{quote(log)}
StandardError=append:{quote(log)}
[Install]
WantedBy=default.target
'''
        files[unit_dir/name]=body
        rig['gpus'][uuid]={'label':f'GPU {gpus[uuid]["index"]}','profiles':{symbol:name}}
        rig['managed_services'][name]=[uuid];rate['log_by_uuid'][uuid]=str(log)
    rate['log']=next(iter(rate['log_by_uuid'].values()))
    coin={'symbol':symbol,'name':symbol,'service':next(iter(units.values())),'gpu_indices':[gpus[u]['index'] for u in selected],'gpu_uuid_services':units,'rate':rate}
    from mining_monitor import validate_config
    validate_config(coin,Path('generated.json'))
    files[Path(config_dir)/'rig-control.json']=json.dumps(rig,indent=2)+'\n'
    files[Path(config_dir)/'coins.d'/(symbol+'.json')]=json.dumps(coin,indent=2)+'\n'
    return files,units,logs

def install(files):
    # No override and no shell. Interrupted writes are rolled back from this call.
    if any(os.path.lexists(p) for p in files):raise ValueError('Destination exists; back up and move old private mappings before installing')
    created=[]
    try:
        for p,body in files.items():
            p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);created.append(p)
            with os.fdopen(fd,'w') as f:f.write(body);f.flush();os.fsync(f.fileno())
    except BaseException:
        for p in reversed(created):p.unlink()
        raise

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbol',default='PRL');p.add_argument('--profile',type=Path,required=True);p.add_argument('--uuid',action='append',required=True);p.add_argument('--write',action='store_true');a=p.parse_args()
    if os.geteuid()==0:p.error('Use the ordinary mining user')
    try:
        files,units,logs=plan(a.symbol,a.profile,a.uuid,inventory())
        print('Per-GPU units: '+', '.join(units.values()))
        if not a.write:print('Plan only. Review docs/PER_GPU.md, then use --write. No miner changed.');return
        logs.mkdir(mode=0o700,parents=True,exist_ok=True)
        install(files);subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        print('Private files installed. No miners started/enabled/stopped. Use the dashboard after enabling controls; disable legacy shared miners first.')
    except (OSError,ValueError,subprocess.SubprocessError):p.exit(1,'Per-GPU setup failed; check private paths, profile and existing destinations. No miner service was changed.\n')
if __name__=='__main__':main()
