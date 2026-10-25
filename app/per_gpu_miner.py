#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Launch a trusted local miner for one exact GPU UUID; never use a shell."""
import argparse, csv, json, os, re, stat, subprocess, signal, time
from pathlib import Path
from rig_control import HostProbe, RigError

UUID = re.compile(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')

def inventory(runner=subprocess.run):
    r = runner(['nvidia-smi','--query-gpu=index,uuid,pci.bus_id','--format=csv,noheader'],capture_output=True,text=True,timeout=10,check=True)
    result={};indices=set()
    for row in csv.reader(r.stdout.splitlines(),skipinitialspace=True):
        if len(row)!=3:raise ValueError('Malformed NVIDIA inventory')
        index,uuid,pci=(x.strip() for x in row)
        if not index.isdecimal() or not UUID.fullmatch(uuid) or not re.fullmatch(r'[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]',pci):raise ValueError('Malformed GPU identity')
        if uuid in result or int(index) in indices:raise ValueError('Duplicate GPU identity')
        result[uuid]={'index':int(index),'pci':pci.lower()};indices.add(int(index))
    return result

def load_profile(path):
    # Wallets/API-free miner connection data remain in a private local file.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o077 or st.st_size>32768:raise ValueError('Miner profile must be an owned private regular file')
        data=json.load(f)
    if not isinstance(data,dict) or set(data)-{'argv','selector','cwd'}:raise ValueError('Invalid miner profile fields')
    argv=data.get('argv');selector=data.get('selector')
    if not isinstance(argv,list) or not 1<=len(argv)<=128 or any(not isinstance(x,str) or not x or len(x)>4096 or any(ord(c)<32 for c in x) for x in argv):raise ValueError('Invalid miner argv')
    if not Path(argv[0]).is_absolute() or selector not in {'wildrig','uuid'}:raise ValueError('Absolute executable and supported selector required')
    if 'cwd' in data and (not isinstance(data['cwd'],str) or not Path(data['cwd']).is_absolute() or any(ord(c)<32 for c in data['cwd'])):raise ValueError('Absolute working directory required')
    if selector=='uuid' and argv.count('{device}')!=1:raise ValueError('UUID selector requires one standalone {device} argument')
    if selector=='wildrig':
        reserved=('--gpu-list','--opencl-platforms','--api-port','--log-file')
        if any(x in {'-d','-l','{device}'} or x.startswith(('-d','-l')) and len(x)>2 or any(x==key or x.startswith(key+'=') for key in reserved) for x in argv[1:]):raise ValueError('WildRig device, API and log options are managed per GPU')
    return data

def build_argv(profile,uuid,gpus,runner=subprocess.run):
    if uuid not in gpus:raise RigError('Configured GPU is absent')
    argv=list(profile['argv'])
    # Freeze the executable symlink for enumeration and exec of this process.
    argv[0]=str(Path(argv[0]).resolve(strict=True))
    if profile['selector']=='uuid':return [uuid if x=='{device}' else x for x in argv]
    bus=int(gpus[uuid]['pci'].split(':')[-2],16)
    if sum(int(row['pci'].split(':')[-2],16)==bus for row in gpus.values())!=1:raise RigError('Ambiguous PCI bus; no miner started')
    r=runner([argv[0],'--opencl-platforms','nvidia','--print-devices'],capture_output=True,text=True,timeout=20,check=True)
    rows=re.findall(r'GPU\s*#(\d+):[^\n]*?\(busID:\s*([0-9a-fA-Fx]+)\s*,',ANSI.sub('',r.stdout+'\n'+r.stderr))
    candidates=[]
    known_buses={int(row['pci'].split(':')[-2],16) for row in gpus.values()}
    for number,value in rows:
        # WildRig does not document the radix. A numeric token such as 10 may
        # name PCI 0a (decimal) or PCI 10 (hex): refuse if both exist.
        if value.lower().startswith('0x') or re.search('[a-fA-F]',value):
            possible={int(value,16)}
        else:
            possible={int(value,10),int(value,16)}
        matches=possible & known_buses
        if len(matches)!=1:raise RigError('Ambiguous miner bus notation; no GPU selected')
        if next(iter(matches))==bus:candidates.append(number)
    if len(candidates)!=1:raise RigError('Miner PCI identity could not be uniquely matched')
    argv+=['--opencl-platforms','nvidia','--gpu-list',candidates[0]]
    if '--multiple-instance' not in argv:argv.append('--multiple-instance')
    return argv

def supervise(child,uuid,pci,owner,probe=None,inventory_reader=inventory,sleep=time.sleep):
    """Verify the running unit's GPU use, including starts made at system boot."""
    probe=probe or HostProbe()
    while child.poll() is None:
        current=inventory_reader()
        if uuid not in current or current[uuid]['pci']!=pci:raise RigError('GPU identity changed while miner was running')
        compute=probe.compute()
        if any(gpu!=uuid and owner in owners for gpu,owners in compute.items()):
            raise RigError('Miner unit used a different GPU')
        sleep(1)
    return child.returncode

def terminate_child(child):
    # Each child is started in a new session; never signal another unit/group.
    if child.poll() is not None:return
    os.killpg(child.pid,signal.SIGTERM)
    try:child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',type=Path,required=True);p.add_argument('--uuid',required=True);a=p.parse_args()
    if not UUID.fullmatch(a.uuid):p.error('Invalid GPU UUID')
    child=None
    try:
        profile=load_profile(a.profile);gpus=inventory()
        argv=build_argv(profile,a.uuid,gpus)
        if inventory()!=gpus:raise RigError('GPU inventory changed during startup')
        owners=HostProbe._cgroup_owners(Path('/proc/self/cgroup').read_text())-{'unknown'}
        if len(owners)!=1:raise RigError('Launch this helper through its dedicated systemd unit')
        owner=next(iter(owners))
        if HostProbe().compute().get(a.uuid):raise RigError('GPU occupied; refusing a duplicate miner')
        child=subprocess.Popen(argv,cwd=profile.get('cwd'),start_new_session=True)
        result=supervise(child,a.uuid,gpus[a.uuid]['pci'],owner)
        raise SystemExit(result)
    except (OSError,ValueError,RigError,subprocess.SubprocessError):
        # Never dump private argv, wallet, pool URL, or exception output into logs.
        raise SystemExit('Per-GPU miner stopped: verify private profile, GPU identity and occupancy')
    finally:
        if child is not None:terminate_child(child)
if __name__=='__main__':main()
