#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Fixed-phase breathing with a PCI/UUID-bound MSI Lovelace live-color path."""
import ctypes,fcntl,glob,json,os,re,subprocess,tempfile,time,traceback
from pathlib import Path
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor
PERIOD_SEC=7.5
# This file and the profile are installed root-owned; user input is JSON only.
PROFILE=Path('/etc/miner-dashboard/rgb-hardware.json')
def hardware_profile():
    st=PROFILE.stat()
    if PROFILE.is_symlink() or st.st_uid!=0 or st.st_mode & 0o022:raise RuntimeError('Unsafe RGB hardware profile')
    d=json.loads(PROFILE.read_text())
    if not isinstance(d,dict) or not d.get('device_names'):raise RuntimeError('Explicit RGB device names required')
    return d
HARDWARE=hardware_profile()
RGB_STATE=Path(HARDWARE['state_file'])
MSI_TRIO_UUID=HARDWARE.get('msi_trio_uuid')
def read_rgb_style(previous):
    try:
        with RGB_STATE.open('rb') as f:raw=f.read(513)
        if len(raw)>512:raise ValueError('RGB state too large')
        d=json.loads(raw)
        if set(d)!={'version','enabled','color'} or type(d['version']) is not int or d['version']!=1 or type(d['enabled']) is not bool or not isinstance(d['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',d['color']):raise ValueError('Invalid RGB state')
        return d
    except (OSError,ValueError,TypeError):return previous

def record_rgb_status(style):
    fd,name=tempfile.mkstemp(prefix='.miner-rgb-',dir='/run')
    try:
        with os.fdopen(fd,'w') as f:json.dump({**style,'at':time.time(),'pid':os.getpid()},f)
        os.chmod(name,0o644);os.replace(name,'/run/miner-rgb-status.json')
    finally:
        if os.path.exists(name):os.unlink(name)

def gpu_slot(device):
    m=re.search(r'/dev/(i2c-[0-9]+)',device.metadata.location or '')
    if not m:return None
    slots=[p for p in (Path('/sys/class/i2c-dev')/m[1]).resolve().parts if re.fullmatch(r'0000:[0-9a-f]{2}:[0-9a-f]{2}[.][0-7]',p)]
    if not slots:return None
    vendor=Path('/sys/bus/pci/devices')/slots[-1]/'vendor'
    return slots[-1] if vendor.exists() and vendor.read_text().strip()=='0x10de' else None
class SmbusData(ctypes.Union):
    _fields_=[('byte',ctypes.c_uint8),('word',ctypes.c_uint16),('block',ctypes.c_uint8*34)]
class SmbusArgs(ctypes.Structure):
    _fields_=[('read_write',ctypes.c_uint8),('command',ctypes.c_uint8),('size',ctypes.c_uint32),('data',ctypes.POINTER(SmbusData))]
class MsiLovelaceLive:
    """STATIC before RGB; no IDLE reset, per-frame Save, or flash writes."""
    def __init__(self,device,slot):
        pci=Path('/sys/bus/pci/devices')/slot
        values=[(pci/n).read_text().strip() for n in ('vendor','device','subsystem_vendor','subsystem_device')]
        if values!=['0x10de','0x2704','0x1462','0x5111']:raise RuntimeError('MSI PCI identity mismatch')
        bus=subprocess.check_output(['nvidia-smi','-i',MSI_TRIO_UUID,'--query-gpu=pci.bus_id','--format=csv,noheader'],text=True,timeout=5).strip()
        if bus.lower()[-12:]!=slot:raise RuntimeError('MSI UUID mismatch')
        m=re.search(r'/dev/(i2c-[0-9]+)',device.metadata.location or '')
        if not m or slot not in (Path('/sys/class/i2c-dev')/m[1]).resolve().parts:raise RuntimeError('MSI bus mismatch')
        self.fd=os.open('/dev/'+m[1],os.O_RDWR);self.last=None
        try:
            fcntl.ioctl(self.fd,0x0703,0x68)
            for reg,val in ((0x2e,0),(0x2d,0),(0x22,0x13),(0x36,100),(0x38,0)):self.write(reg,val)
        except Exception:self.close();raise
        print('MSI Lovelace live-color owner '+slot+' /dev/'+m[1],flush=True)
    def write(self,reg,value):
        if reg not in (0x2e,0x2d,0x22,0x36,0x38,0x30,0x31,0x32):raise ValueError('Invalid LED register')
        data=SmbusData();data.byte=value
        args=SmbusArgs(0,reg,2,ctypes.pointer(data));fcntl.ioctl(self.fd,0x0720,args)
    def set_color(self,color):
        value=(color.red,color.green,color.blue)
        if value==self.last:return
        self.write(0x22,0x13)
        for reg,v in zip((0x30,0x31,0x32),value):self.write(reg,v)
        self.last=value
    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None
def main():
    style={'version':1,'enabled':False,'color':'#004bff'}
    while True:
        client=None;native={}
        try:
            client=OpenRGBClient('127.0.0.1',6742,name='Miner Dashboard breathing')
            targets=[]
            for d in client.devices:
                if d.name not in HARDWARE['device_names']:continue
                board=d.type.name=='MOTHERBOARD'
                slot=gpu_slot(d) if d.type.name=='GPU' else None
                if not(board or slot):continue
                if 'Direct' not in [m.name for m in d.modes]:continue
                if MSI_TRIO_UUID and d.name=='MSI GeForce RTX 4080 Gaming X Trio':native[d.id]=MsiLovelaceLive(d,slot)
                else:d.set_mode('Direct')
                targets.append(d)
            if not targets:raise RuntimeError('No supported RGB controller')
            print('Breathing targets '+json.dumps([{'name':d.name,'slot':gpu_slot(d),'native_live':d.id in native} for d in targets]),flush=True)
            last_ack=None;last_ack_at=0
            while True:
                phase=(time.time()%PERIOD_SEC)/PERIOD_SEC
                progress=phase*2 if phase<0.5 else (1-phase)*2
                style=read_rgb_style(style)
                base=tuple(int(style['color'][i:i+2],16) for i in (1,3,5))
                scale=0.2+0.8*progress if style['enabled'] else 0
                color=RGBColor(*(round(v*scale) for v in base))
                for d in targets:
                    if d.id in native:native[d.id].set_color(color)
                    else:d.set_color(color,fast=True)
                if style!=last_ack or time.monotonic()-last_ack_at>=5:
                    record_rgb_status(style);last_ack=dict(style);last_ack_at=time.monotonic()
                time.sleep(0.12)
        except Exception as e:
            print('RGB reconnect: '+repr(e),flush=True);traceback.print_exc()
        finally:
            for n in native.values():n.close()
            if client is not None:
                try:client.disconnect()
                except Exception:pass
        time.sleep(3)
if __name__=='__main__':main()
