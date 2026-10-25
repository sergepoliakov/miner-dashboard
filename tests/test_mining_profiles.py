import contextlib,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from mining_profiles import MiningProfiles,ProfileError,valid_prl,atomic_bytes,encoded


def prl_address(byte=17):
    charset='qpzry9x8gf2tvdw0s3jn54khce6mua7l';values=[1];acc=bits=0
    for x in bytes([byte])*32:
        acc=(acc<<8)|x;bits+=8
        while bits>=5:bits-=5;values.append((acc>>bits)&31)
    if bits:values.append((acc<<(5-bits))&31)
    check=[ord(c)>>5 for c in 'prl']+[0]+[ord(c)&31 for c in 'prl']+values+[0]*6;chk=1
    for v in check:
        top=chk>>25;chk=((chk&0x1ffffff)<<5)^v
        for i,g in enumerate([0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3]):
            if (top>>i)&1:chk^=g
    chk^=0x2bc830a3
    return 'prl1'+''.join(charset[v] for v in values+[chk>>5*(5-i)&31 for i in range(6)])

class Rig:
    def __init__(self,root):
        self.state_dir=root/'rig';self.state_dir.mkdir();(self.state_dir/'desired.json').write_text('{"GPU-1":"STOP"}')
        self.services={'prl0.service':['GPU-0'],'prl1.service':['GPU-1'],'prl2.service':['GPU-2'],'cpu.service':[]}
        self.states={u:('inactive' if u=='prl1.service' else 'active') for u in self.services};self.boot={u:('disabled' if u=='prl1.service' else 'enabled') for u in self.services};self.pids={u:i+100 for i,u in enumerate(self.services)}
        self.gpus={gpu:{'profiles':{'PRL':u}} for u,gs in self.services.items() for gpu in gs};self.cpu_services={'cpu.service'}
        self.calls=[];self.lock=threading.Lock();self.probe=self;self.fail_start_once=False;self.on_stop=None
    def _require_config(self):pass
    @contextlib.contextmanager
    def _locked(self,external_locks=()):
        with self.lock:yield
    def _status(self,u):return self.states[u]
    def _enabled(self,u):return self.boot[u]
    def compute(self):return {g:{u} for u,gs in self.services.items() for g in gs if self.states[u]=='active'}
    def _command(self,action,*units):
        self.calls.append((action,*units))
        if action=='start' and self.fail_start_once:self.fail_start_once=False;raise RuntimeError('start failed')
        for u in units:
            self.states[u]='active' if action=='start' else 'inactive'
            if action=='start':self.pids[u]+=10
        if action=='stop' and self.on_stop:cb=self.on_stop;self.on_stop=None;cb()
    def _wait(self,u,wanted):
        if self.states[u] not in (wanted if isinstance(wanted,set) else {wanted}):raise RuntimeError('unexpected state')

class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.rig=Rig(self.root);self.registry=self.root/'registry.json';self.state=self.root/'state/profiles.json';self.source=self.root/'miner.json'
        self.old=prl_address();self.new=prl_address(34)
        self.initial=dict(address=self.old,worker='old',owner_label='Owner',exchange='wallet')
        atomic_bytes(self.source,encoded({'wallet':self.old,'worker':'old','power_limit':260,'other_coin':'unchanged'}))
        self.provider={'name':'Pearl','pool':'pool.example:9000','services':['prl0.service','prl1.service','prl2.service'],'files':[{'path':str(self.source),'fields':[{'path':['wallet'],'template':'{address}'},{'path':['worker'],'template':'{worker}'}]}],'initial':self.initial}
        self.write_registry();self.controller=MiningProfiles(self.registry,self.state,self.rig)
    def write_registry(self,providers=None):atomic_bytes(self.registry,encoded({'providers':providers or {'PRL':self.provider}}))
    def row(self,symbol='PRL'):return next(p for p in self.controller.snapshot()['profiles'] if p['symbol']==symbol)
    def save(self,**extra):
        b=dict(symbol='PRL',address=self.new,worker='gudong-069',owner_label='Gudong',exchange='safetrade',expected_revision=self.row()['revision']);b.update(extra);return self.controller.save(b)
    def apply(self,**extra):
        r=self.row();b=dict(symbol='PRL',expected_revision=r['revision'],source_version=r['source_version']);b.update(extra);return self.controller.apply(b)
    def test_native_prl_checksum(self):
        self.assertTrue(valid_prl(self.old));self.assertFalse(valid_prl(self.old[:-1]+('q' if self.old[-1]!='q' else 'p')));self.assertFalse(valid_prl('btc1'+self.old[4:]))
    def test_save_is_private_and_never_changes_miners(self):
        before=self.source.read_bytes();self.save();self.assertEqual(before,self.source.read_bytes());self.assertEqual(self.rig.calls,[]);self.assertEqual(self.state.stat().st_mode&0o777,0o600)
        other=MiningProfiles(self.registry,self.state,self.rig);self.assertEqual(other.snapshot()['profiles'][0]['draft']['address'],self.new)
    def test_apply_only_restarts_running_coin_units_and_preserves_stop_power_other_coin(self):
        desired=(self.rig.state_dir/'desired.json').read_bytes();boot=dict(self.rig.boot);cpu=self.rig.pids['cpu.service'];self.save();r=self.apply()
        self.assertEqual(r['restarted_services'],['prl0.service','prl2.service']);self.assertEqual(self.rig.calls,[('stop','prl0.service','prl2.service'),('start','prl0.service','prl2.service')]);self.assertEqual(self.rig.states['prl1.service'],'inactive');self.assertEqual(self.rig.boot,boot);self.assertEqual(self.rig.pids['cpu.service'],cpu);self.assertEqual((self.rig.state_dir/'desired.json').read_bytes(),desired)
        x=json.loads(self.source.read_text());self.assertEqual(x['wallet'],self.new);self.assertEqual(x['power_limit'],260);self.assertEqual(x['other_coin'],'unchanged');self.assertEqual(self.row()['active']['address'],self.new)
    def test_all_stopped_apply_never_starts_any_card(self):
        for u in self.provider['services']:self.rig.states[u]='inactive';self.rig.boot[u]='disabled'
        self.save();self.assertEqual(self.apply()['restarted_services'],[]);self.assertEqual(self.rig.calls,[])
    def test_already_applied_is_idempotent(self):
        r=self.apply();self.assertEqual(r['restarted_services'],[]);self.assertEqual(self.rig.calls,[])
    def test_failed_start_restores_files_and_original_running_choice(self):
        before=self.source.read_bytes();self.save();state=self.state.read_bytes();self.rig.fail_start_once=True
        with self.assertRaisesRegex(ProfileError,'回退'):self.apply()
        self.assertEqual(self.source.read_bytes(),before);self.assertEqual(self.state.read_bytes(),state);self.assertEqual(self.rig.states['prl0.service'],'active');self.assertEqual(self.rig.states['prl1.service'],'inactive')
    def test_stale_revision_or_source_never_stops_miners(self):
        self.save()
        with self.assertRaises(ProfileError):self.apply(expected_revision=0)
        with self.assertRaises(ProfileError):self.apply(source_version='stale')
        self.assertEqual(self.rig.calls,[])
    def test_external_change_while_stopping_is_not_overwritten(self):
        self.save()
        def change():
            x=json.loads(self.source.read_text());x['external']='kept';atomic_bytes(self.source,encoded(x))
        self.rig.on_stop=change
        with self.assertRaises(ProfileError):self.apply()
        self.assertEqual(json.loads(self.source.read_text())['external'],'kept');self.assertEqual(json.loads(self.source.read_text())['wallet'],self.old)
    def test_unsafe_templates_paths_shared_services_are_rejected(self):
        for change in ['binary','constant','shared']:
            p=json.loads(json.dumps(self.provider))
            if change=='binary':p['files'][0]['fields'][0]={'path':['argv',0],'template':'{address}'}
            if change=='constant':p['files'][0]['fields'][0]['template']='fixed'
            if change=='shared':self.write_registry({'PRL':p,'NOID':p})
            else:self.write_registry({'PRL':p})
            with self.assertRaises(ProfileError):self.row()
    def test_symlink_or_world_readable_source_cannot_apply(self):
        self.source.chmod(0o644);self.assertFalse(self.row()['adapter_ready']);self.source.chmod(0o600);real=self.root/'real.json';self.source.rename(real);self.source.symlink_to(real);self.assertFalse(self.row()['adapter_ready'])
    def test_input_injection_or_unknown_fields_never_mutate(self):
        for fields in [{'worker':'x; reboot'},{'address':'bad\naddress'},{'symbol':'UNKNOWN'},{'path':'/etc/passwd'},{'exchange':'other'},{'expected_revision':True}]:
            with self.assertRaises((ValueError,ProfileError)):self.save(**fields)
        self.assertEqual(self.rig.calls,[]);self.assertFalse(self.state.exists())
    def test_other_coins_save_drafts_without_claiming_installed_adapter(self):
        before=self.source.read_bytes()
        for symbol in ['QTC','NOID','CSD','TSC']:
            r=self.save(symbol=symbol,address='DEMO_PUBLIC_ADDRESS',expected_revision=0)['profile'];self.assertFalse(r['adapter_ready']);self.assertIsNone(r['active'])
        self.assertEqual(self.source.read_bytes(),before);self.assertEqual(self.rig.calls,[])
    def test_stats_must_follow_same_address_source(self):
        coins=[{'symbol':'PRL','earnings':{'url':'https://pool.example/private-old-address'}}];self.controller.get_coins=lambda:coins;self.assertFalse(self.row()['adapter_ready'])
        coins[0].update(address_source={'file':str(self.source),'json_path':'wallet'});coins[0]['earnings']['url']='https://pool.example/{address}';self.assertTrue(self.row()['adapter_ready'])
    def test_unmanaged_units_cannot_be_applied(self):
        self.provider['services']=['unmanaged.service'];self.write_registry();self.assertFalse(self.row()['adapter_ready'])

    def test_wrong_coin_or_cpu_unit_cannot_apply(self):
        for unit in ['cpu.service', 'prl0.service']:
            if unit == 'prl0.service': self.rig.gpus['GPU-0']['profiles'] = {'QTC': unit}
            self.provider['services']=[unit];self.write_registry();self.assertFalse(self.row()['adapter_ready'])
            self.save()
            with self.assertRaises(ProfileError): self.apply()
        self.assertEqual(self.rig.calls, [])
    def test_metadata_only_does_not_restart(self):
        self.save(address=self.old,worker='old',owner_label='new owner');result=self.apply()
        self.assertFalse(result['payout_changed']);self.assertEqual(self.rig.calls, [])
    def test_completed_transactions_leave_no_whole_file_backups(self):
        self.save();self.apply();self.assertFalse(self.controller._recovery_dir('PRL').exists())
    def test_interrupted_recovery_blocks_new_apply(self):
        self.controller._recovery_dir('PRL').mkdir(parents=True);self.assertFalse(self.row()['adapter_ready'])
        with self.assertRaises(ProfileError):self.apply()
        self.assertEqual(self.rig.calls, [])

    def test_rollback_does_not_overwrite_concurrent_change_after_our_write(self):
        self.save();original=self.rig._command
        def command(action,*units):
            if action=='start':
                data=json.loads(self.source.read_text());data['external_after_write']='keep'
                atomic_bytes(self.source,encoded(data));raise RuntimeError('start failed after external change')
            return original(action,*units)
        self.rig._command=command
        with self.assertRaisesRegex(ProfileError,'回退不完整'):self.apply()
        self.assertEqual(json.loads(self.source.read_text())['external_after_write'],'keep')
        self.assertTrue(self.controller._recovery_dir('PRL').exists())
        self.assertEqual(self.rig.states['prl0.service'],'inactive')
