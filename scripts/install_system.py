#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Opt-in Linux helpers from a reviewed root-owned staging tree; no miner start."""
import argparse, grp, json, os, pwd, re, stat, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DEST=Path('/opt/miner-dashboard')
APP_FILES=('runtime_paths.py','gpu_display.py','gpu_tune.py','mining_monitor.html','mining_monitor.py','noid_cpu_control.py','noid_cpu_update.py','pool_notices.py','rgb_control.py','rgb_daemon.py','rig_control.py','safetrade_api_broker.py','safetrade_client.py','web_gateway.py')

def safe_parents(path):
    for p in (path,*path.parents):
        if not os.path.lexists(p):continue
        st=p.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
            raise ValueError('Unsafe root-owned path: '+str(p))

def source_snapshot():
    # Root-owned staging prevents same-user replacement between checks and reads.
    safe_parents(ROOT);safe_parents(ROOT/'app');safe_parents(Path(__file__))
    data={}
    for name in APP_FILES:
        p=ROOT/'app'/name;safe_parents(p)
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size>1_000_000:raise ValueError('Unexpected source file')
            data[name]=f.read()
    if {p.name for p in (ROOT/'app').iterdir() if p.name!='__pycache__'}!=set(APP_FILES):
        raise ValueError('Unexpected source entries; stage only the release app files')
    return data

def preflight(targets):
    for p in targets:
        safe_parents(p.parent)
        if os.path.lexists(p):raise ValueError('Refusing existing destination: '+str(p))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--user',required=True);p.add_argument('--tuning',action='store_true');p.add_argument('--exchange',action='store_true');p.add_argument('--rgb',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:p.error('Requires root; follow the root staging instructions in docs/INSTALL.md')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',a.user):p.error('Invalid user')
    account=pwd.getpwnam(a.user)
    if account.pw_uid==0:p.error('Dashboard user must be non-root')
    if not any((a.tuning,a.exchange,a.rgb)):p.error('Select at least one helper')
    try:source=source_snapshot()
    except (OSError,ValueError) as e:p.error(str(e))
    units=Path('/etc/systemd/system');cfg=Path('/etc/miner-dashboard');group=grp.getgrgid(account.pw_gid).gr_name
    files={}
    if a.tuning:
        files[cfg/'hardware.json']=(json.dumps({'allowed_users':[a.user],'hwmon_chip':'','fan_groups':{}},indent=2)+'\n',0o644)
        files[Path('/usr/local/sbin/gpu-tune')]=(source['gpu_tune.py'],0o755)
        files[units/'gpu-tune.socket']=(f'[Unit]\nDescription=Miner Dashboard narrow hardware socket\n[Socket]\nListenStream=/run/gpu-tune.sock\nSocketUser={a.user}\nSocketMode=0600\nAccept=yes\n[Install]\nWantedBy=sockets.target\n',0o644)
        files[units/'gpu-tune@.service']=('[Unit]\nDescription=Miner Dashboard single hardware request\n[Service]\nExecStart=/usr/bin/python3 -I /usr/local/sbin/gpu-tune serve\nStandardInput=socket\nUser=root\nTimeoutStopSec=15\nUMask=0077\n',0o644)
        files[units/'miner-dashboard-tuning-restore.service']=(f'[Unit]\nDescription=Restore saved UUID power and clock limits\nAfter=nvidia-persistenced.service\nBefore=user@{account.pw_uid}.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I /usr/local/sbin/gpu-tune apply\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n',0o644)
    if a.exchange:
        files[Path('/etc/tmpfiles.d/miner-dashboard-exchange.conf')]=(f'd /run/safetrade-api 0750 safetrade-api {group} -\n',0o644)
        files[units/'miner-dashboard-exchange.service']=(f'[Unit]\nDescription=Optional SafeTrade credential-isolated broker (read only)\nAfter=network-online.target\n[Service]\nUser=safetrade-api\nGroup={group}\nExecStart=/usr/bin/python3 {DEST}/app/safetrade_api_broker.py\nEnvironment=MINER_DASHBOARD_GROUP={group}\nEnvironment=MINER_DASHBOARD_ALLOW_TRADING=0\nRestart=on-failure\nRestartSec=10\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nReadWritePaths=/run/safetrade-api\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n',0o644)
    if a.rgb:
        files[cfg/'rgb-hardware.json']=(json.dumps({'state_file':str(Path(account.pw_dir)/'.config/miner-dashboard/rgb.json'),'device_names':[],'msi_trio_uuid':None},indent=2)+'\n',0o644)
        files[units/'miner-dashboard-rgb.service']=(f'[Unit]\nDescription=Opt-in fixed-phase RGB breathing\nAfter=openrgb.service\n[Service]\nUser=root\nExecStart={DEST}/rgb-venv/bin/python3 -I {DEST}/app/rgb_daemon.py\nRestart=on-failure\nRestartSec=5\nNoNewPrivileges=yes\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n',0o644)
    try:preflight([DEST,*files])
    except (OSError,ValueError) as e:p.error(str(e))
    created=[];dirs=[]
    def mkdir(path,mode=0o755):
        if path.exists():safe_parents(path);return
        mkdir(path.parent);path.mkdir(mode=mode);dirs.append(path)
    def write(path,value,mode):
        mkdir(path.parent);safe_parents(path.parent)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode);created.append(path)
        with os.fdopen(fd,'wb') as f:
            f.write(value.encode() if isinstance(value,str) else value);os.fchmod(f.fileno(),mode);f.flush();os.fsync(f.fileno())
    try:
        mkdir(DEST)
        for name,value in source.items():write(DEST/'app'/name,value,0o644)
        for path,(value,mode) in files.items():write(path,value,mode)
        if a.exchange:
            try:broker=pwd.getpwnam('safetrade-api')
            except KeyError:
                subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin','safetrade-api'],check=True);broker=pwd.getpwnam('safetrade-api')
            # Never change an existing credential store or follow its symlinks.
            store=Path('/var/lib/safetrade-api')
            if os.path.lexists(store):
                st=store.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid!=broker.pw_uid or st.st_mode & 0o077:raise ValueError('Unsafe existing credential store')
            else:store.mkdir(mode=0o700);os.chown(store,broker.pw_uid,broker.pw_gid)
            subprocess.run(['systemd-tmpfiles','--create','/etc/tmpfiles.d/miner-dashboard-exchange.conf'],check=True)
        subprocess.run(['systemctl','daemon-reload'],check=True)
    except BaseException:
        for path in reversed(created):path.unlink()
        for path in reversed(dirs):
            try:path.rmdir()
            except OSError:pass
        raise
    print('Root-owned helpers installed. No services enabled. Review profiles and enable explicitly. A failed exchange provisioning may leave an unused system account/store; no credentials are overwritten.')
if __name__=='__main__':main()
