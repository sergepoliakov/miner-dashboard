# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""RGB color/enable state only; breathing shape and period cannot be changed."""
import fcntl,json,os,re,tempfile,time
from pathlib import Path
from runtime_paths import CONFIG_DIR
STATE=CONFIG_DIR/'rgb.json'
APPLIED=Path('/run/miner-rgb-status.json')
DEFAULT={'version':1,'enabled':True,'color':'#004bff'}
def validate(d,version=True):
    expected={'version','enabled','color'} if version else {'enabled','color'}
    if not isinstance(d,dict) or set(d)!=expected:raise ValueError('RGB只允许修改颜色和开关')
    if version and (type(d['version']) is not int or d['version']!=1):raise ValueError('RGB配置版本无效')
    if type(d['enabled']) is not bool:raise ValueError('灯效开关必须为布尔值')
    if not isinstance(d['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',d['color']):raise ValueError('颜色必须为六位十六进制色值')
    return {'version':1,'enabled':d['enabled'],'color':d['color'].lower()}
def read_record():
    if not STATE.exists():return dict(DEFAULT),0
    with STATE.open('rb') as f:
        raw=f.read(513);updated_at=os.fstat(f.fileno()).st_mtime
    if len(raw)>512:raise ValueError('RGB配置过大')
    return validate(json.loads(raw)),updated_at
def read_style():
    return read_record()[0]
def snapshot():
    try:d,updated_at=read_record()
    except (OSError,ValueError,TypeError):return {'ok':False,'error':'RGB设置无法读取'}
    applied=False
    try:
        a=json.loads(APPLIED.read_text())
        applied=a.get('enabled')==d['enabled'] and a.get('color')==d['color'] and 0<=time.time()-a.get('at',0)<20
    except (OSError,ValueError,TypeError):pass
    return {**d,'updated_at':updated_at,'ok':True,'effect':'breathing','period_seconds':7.5,'applied':applied}
def update_style(body):
    d=validate(body,version=False)
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (STATE.parent/'.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        fd,tmp=tempfile.mkstemp(prefix='.rgb-',dir=STATE.parent)
        try:
            with os.fdopen(fd,'w') as f:json.dump(d,f);f.write('\n');f.flush();os.fsync(f.fileno())
            os.chmod(tmp,0o600);os.replace(tmp,STATE)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    return {**snapshot(),'saved':True}
