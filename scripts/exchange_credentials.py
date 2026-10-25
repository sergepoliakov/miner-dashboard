#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Interactive isolated credential setup; never accepts a key on command line."""
import getpass, json, os, pwd
from pathlib import Path
if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Run with sudo after installing the exchange helper')
    owner=pwd.getpwnam('safetrade-api');path=Path('/var/lib/safetrade-api/credentials.json')
    key=getpass.getpass('SafeTrade API key (hidden): ');secret=getpass.getpass('SafeTrade API secret (hidden): ')
    if not key or not secret:raise SystemExit('Both values required')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'key':key,'secret':secret},f);f.write('\n');os.fchown(f.fileno(),owner.pw_uid,owner.pw_gid)
    print('Private credential file created; values are not logged. Trading remains disabled.')
