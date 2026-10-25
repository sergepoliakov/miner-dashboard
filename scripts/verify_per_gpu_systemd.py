#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Disposable CI: real user units, supervisor/miner children, synthetic NVIDIA data."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from rig_control import RigController,HostProbe
from setup_per_gpu import plan,install

def run(args,timeout=15):return subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=False)
def ctl(*args):return subprocess.check_output(['systemctl','--user',*args],text=True).strip()

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise SystemExit('Disposable GitHub Actions VM only')
    uuids=[f'GPU-00000000-0000-0000-0000-{i+1:012d}' for i in range(3)]
    with tempfile.TemporaryDirectory(prefix='per-gpu-integration-') as temp:
        root=Path(temp)/'case with spaces';root.mkdir()
        bin_dir=root/'bin';bin_dir.mkdir();records=root/'records';records.mkdir()
        # Fixtures deliberately implement only the NVIDIA fields used by our code.
        smi='''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['MINER_CI_GPU_DIR'])
uuids=['GPU-00000000-0000-0000-0000-'+str(i+1).zfill(12) for i in range(3)]
query=next((x for x in sys.argv if x.startswith('--query-')),'')
if query=='--query-gpu=uuid':print('\\n'.join(uuids))
elif query=='--query-gpu=index,uuid':print('\\n'.join(f'{i}, {u}' for i,u in enumerate(uuids)))
elif query=='--query-gpu=index,uuid,pci.bus_id':print('\\n'.join(f'{i}, {u}, 00000000:0{i+1}:00.0' for i,u in enumerate(uuids)))
elif query=='--query-compute-apps=gpu_uuid,pid':
 for p in sorted(root.glob('*.json')):
  try:
   row=json.loads(p.read_text());pid=int(row['pid']);stat=Path('/proc')/str(pid)/'stat'
   if stat.exists() and stat.read_text().rsplit(')',1)[1].split()[0]!='Z':print(str(row['uuid'])+', '+str(pid))
  except (OSError,ValueError,KeyError):pass
else:sys.exit(2)
'''
        miner='''#!/usr/bin/python3
import json,os,sys,time
from pathlib import Path
uuid=sys.argv[1];path=Path(os.environ['MINER_CI_GPU_DIR'])/(uuid+'.json')
path.write_text(json.dumps({'uuid':uuid,'pid':os.getpid()}))
while True:time.sleep(1)
'''
        (bin_dir/'nvidia-smi').write_text(smi);(bin_dir/'nvidia-smi').chmod(0o755)
        (bin_dir/'fake-miner').write_text(miner);(bin_dir/'fake-miner').chmod(0o755)
        profile=root/'profile.json';profile.write_text(json.dumps({'selector':'uuid','argv':[str(bin_dir/'fake-miner'),'{device}']}));profile.chmod(0o600)
        directory=Path.home()/'.config/systemd/user';directory.mkdir(parents=True,exist_ok=True)
        files,mapping,logs=plan('PRL',profile,uuids,{u:{'index':i,'pci':f'00000000:0{i+1}:00.0'} for i,u in enumerate(uuids)},root/'config',root/'state',directory)
        units=list(mapping.values());unit_paths=[directory/unit for unit in units]
        if any(os.path.lexists(p) for p in files):raise SystemExit('CI destination already exists; refusing overwrite')
        fixture_path=str(bin_dir)+':/usr/bin:/bin'
        for path in unit_paths:
            files[path]=files[path].replace('[Install]',f'Environment="PATH={fixture_path}"\nEnvironment="MINER_CI_GPU_DIR={records}"\n[Install]')
        logs.mkdir(parents=True)
        install(files)
        previous_path=os.environ.get('PATH','');previous_records=os.environ.get('MINER_CI_GPU_DIR')
        os.environ['PATH']=fixture_path;os.environ['MINER_CI_GPU_DIR']=str(records)
        probe=HostProbe()
        # Check the main dashboard installer as well: same path-directive class.
        import install_user
        install_user.ROOT=root
        user_unit=root/'ci-dashboard.service'
        user_unit.write_text(install_user.unit('mining_monitor.py','CI syntax only','Environment=MINING_MONITOR_ALLOW_EMPTY_COINS=1'))
        def child_pid(uuid):return str(json.loads((records/(uuid+'.json')).read_text())['pid'])
        def ready(uuids_to_check):
            for _ in range(100):
                compute=probe.compute()
                if all(compute.get(u)=={mapping[u]} for u in uuids_to_check):return
                time.sleep(.1)
            raise RuntimeError('CI miner child did not become observable')
        try:
            subprocess.run(['systemd-analyze','--user','verify',*[str(p) for p in unit_paths],str(user_unit)],check=True)
            ctl('daemon-reload')
            config=root/'config/rig-control.json';state=root/'state/control'
            rig=RigController(config,state,runner=run,probe=probe)
            for uuid in uuids:rig.control_gpu(uuid,'start')
            ready(uuids)
            before={unit:ctl('show',unit,'-p','MainPID','--value') for unit in units}
            children={u:child_pid(u) for u in uuids}
            assert all(int(pid)>0 for pid in before.values())
            rig.control_gpu(uuids[1],'stop')
            assert ctl('show',mapping[uuids[1]],'-p','MainPID','--value')=='0'
            assert run(['systemctl','--user','is-enabled',mapping[uuids[1]]]).stdout.strip()=='disabled'
            assert uuids[1] not in probe.compute()
            for uuid in (uuids[0],uuids[2]):
                assert ctl('show',mapping[uuid],'-p','MainPID','--value')==before[mapping[uuid]]
                assert child_pid(uuid)==children[uuid]
            reloaded=RigController(config,state,runner=run,probe=probe)
            assert reloaded.snapshot()['gpus'][uuids[1]]['desired']=='STOP'
            reloaded.control_gpu(uuids[1],'start');ready(uuids)
            assert child_pid(uuids[1])!=children[uuids[1]]
            for uuid in (uuids[0],uuids[2]):
                assert ctl('show',mapping[uuid],'-p','MainPID','--value')==before[mapping[uuid]]
                assert child_pid(uuid)==children[uuid]
            assert ctl('is-enabled',mapping[uuids[1]])=='enabled'
            print('Real systemd units + per-GPU supervisors/children: selected GPU stop/resume persisted; other supervisor AND child PIDs unchanged. NVIDIA inventory is synthetic; no hardware/pool claim.')
        finally:
            for unit in units:run(['systemctl','--user','disable','--now',unit])
            for path in unit_paths:path.unlink()
            ctl('daemon-reload')
            os.environ['PATH']=previous_path
            if previous_records is None:os.environ.pop('MINER_CI_GPU_DIR',None)
            else:os.environ['MINER_CI_GPU_DIR']=previous_records
if __name__=='__main__':main()
