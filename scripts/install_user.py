#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Install user services; no sudo, downloads, miner start or hardware writes."""
import argparse, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from runtime_paths import CONFIG_DIR

def unit(app,description,environment):
    # systemd quoting, including literal percent escapes.
    def q(value):return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    root_path=str(ROOT)
    if not root_path.startswith('/') or any(ord(c)<32 for c in root_path) or '\\' in root_path:
        raise ValueError('Unsupported systemd working directory')
    def argv(value): return q(value).replace('$','$$')
    return f'''[Unit]
Description={description}
After=network-online.target

[Service]
Type=simple
ExecStart={argv(sys.executable)} {argv(ROOT/'app'/app)}
WorkingDirectory={root_path.replace('%','%%')}
Environment={q('MINER_DASHBOARD_CONFIG='+str(CONFIG_DIR))}
{environment}
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
KillMode=control-group
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=default.target
'''
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--enable-controls',action='store_true');p.add_argument('--web-only',action='store_true',help='Add only the gateway to an existing core installation');p.add_argument('--host',help='HTTPS host for authenticated remote gateway, e.g. miner.example.com');a=p.parse_args()
    if os.geteuid()==0:p.error('Run as the ordinary mining user, not root')
    if a.web_only and not a.host:p.error('--web-only requires --host')
    if a.host:
        import re
        if not re.fullmatch(r'[A-Za-z0-9.-]+',a.host):p.error('Host only, without scheme, port or slash')
        if not (CONFIG_DIR/'web-auth.json').exists():p.error('Run configure.py web-auth first')
    d=Path.home()/'.config/systemd/user';d.mkdir(parents=True,exist_ok=True)
    env='Environment=MINING_MONITOR_ALLOW_EMPTY_COINS=1\nEnvironment=MINING_MONITOR_EXCHANGE=0\nEnvironment=MINER_DASHBOARD_CONTROLS='+str(int(a.enable_controls))
    units={} if a.web_only else {'miner-dashboard.service':unit('mining_monitor.py','Miner Dashboard local monitor',env)}
    if a.host:units['miner-dashboard-web.service']=unit('web_gateway.py','Miner Dashboard authenticated HTTPS upstream','Environment=MINER_DASHBOARD_HOST='+a.host)
    # Do not clobber an existing unit or silently change an existing deployment.
    if any((d/name).exists() for name in units):p.error('Unit exists. Review/edit it explicitly before reinstalling')
    for name,body in units.items():(d/name).write_text(body)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    for name in units:subprocess.run(['systemctl','--user','enable','--now',name],check=True)
    print('Services installed. For unattended boot: sudo loginctl enable-linger "$USER". HTTPS proxy setup is separate.')
if __name__=='__main__':main()
