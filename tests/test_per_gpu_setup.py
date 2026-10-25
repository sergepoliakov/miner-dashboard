import importlib.util,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import mining_monitor as mm
from per_gpu_miner import build_argv,load_profile,inventory
from rig_control import RigError
ROOT=Path(__file__).resolve().parents[1]
U='GPU-00000000-0000-0000-0000-000000000001'
V='GPU-00000000-0000-0000-0000-000000000002'
S=importlib.util.spec_from_file_location('setup_per_gpu',ROOT/'scripts/setup_per_gpu.py');setup=importlib.util.module_from_spec(S);S.loader.exec_module(setup)

class PerGpuSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.profile=self.root/'profile.json';self.profile.write_text(json.dumps({'selector':'wildrig','argv':[sys.executable,'--algo','pearlhash']}));self.profile.chmod(0o600)
        self.gpus={U:{'index':2,'pci':'00000000:07:00.0'},V:{'index':0,'pci':'00000000:01:00.0'}}
    def test_wildrig_device_uses_pci_not_nvml_index(self):
        output='GPU #0: NVIDIA GPU (busID: 1, arch: sm89)\nGPU #3: NVIDIA GPU (busID: 7, arch: sm89)\n'
        def run(args,**kw):return subprocess.CompletedProcess(args,0,output,'')
        args=build_argv(load_profile(self.profile),U,self.gpus,run)
        self.assertEqual(args[args.index('--gpu-list')+1],'3');self.assertIn('--multiple-instance',args)
    def test_missing_ambiguous_or_unmatched_device_refused(self):
        p=load_profile(self.profile)
        with self.assertRaises(RigError):build_argv(p,V,{U:self.gpus[U]})
        shared={U:self.gpus[U],V:{'index':1,'pci':'00000000:07:01.0'}}
        with self.assertRaises(RigError):build_argv(p,U,shared)
        def run(a,**kw):return subprocess.CompletedProcess(a,0,'GPU #0: Other (busID: 1, arch: sm89)','')
        with self.assertRaises(RigError):build_argv(p,U,self.gpus,run)
    def test_profile_private_and_device_override_rejected(self):
        self.profile.chmod(0o644)
        with self.assertRaises(ValueError):load_profile(self.profile)
        self.profile.chmod(0o600)
        for flag in ('-d','--gpu-list=0','--api-port','--log-file'):
            self.profile.write_text(json.dumps({'selector':'wildrig','argv':[sys.executable,flag,'0']}))
            with self.assertRaises(ValueError):load_profile(self.profile)
    def test_profile_symlink_and_extra_fields_refused(self):
        link=self.root/'link.json';link.symlink_to(self.profile)
        with self.assertRaises(OSError):load_profile(link)
        self.profile.write_text(json.dumps({'selector':'wildrig','argv':[sys.executable],'unexpected':1}))
        with self.assertRaises(ValueError):load_profile(self.profile)
    def test_uuid_selector_passes_single_uuid_without_shell(self):
        self.profile.write_text(json.dumps({'selector':'uuid','argv':[sys.executable,'--device','{device}']}))
        self.assertEqual(build_argv(load_profile(self.profile),U,self.gpus)[-1],U)
    def test_plan_and_install_are_private_and_never_start_miners(self):
        cfg=self.root/'config';state=self.root/'state';units=self.root/'units'
        files,mapping,logs=setup.plan('PRL',self.profile,[U,V],self.gpus,cfg,state,units)
        setup.install(files)
        self.assertEqual(len(mapping),2);self.assertEqual(len(set(mapping.values())),2)
        for p in files:self.assertEqual(p.stat().st_mode & 0o777,0o600)
        coin=json.loads((cfg/'coins.d/PRL.json').read_text());mm.validate_config(coin,Path('test.json'))
        rig=json.loads((cfg/'rig-control.json').read_text())
        self.assertTrue(all(len(cards)==1 for cards in rig['managed_services'].values()))
        with self.assertRaises(ValueError):setup.install(files)
    def test_install_conflict_is_atomic_before_writes(self):
        a=self.root/'a';b=self.root/'b';b.write_text('keep')
        with self.assertRaises(ValueError):setup.install({a:'new',b:'overwrite'})
        self.assertFalse(a.exists());self.assertEqual(b.read_text(),'keep')
    def test_setup_requires_distinct_present_uuids(self):
        with self.assertRaises(ValueError):setup.plan('PRL',self.profile,[U,U],self.gpus)
        with self.assertRaises(ValueError):setup.plan('PRL',self.profile,[V],{U:self.gpus[U]})
    def test_dynamic_uuid_telemetry_survives_index_reordering(self):
        files,mapping,_=setup.plan('PRL',self.profile,[U,V],self.gpus,self.root/'cfg',self.root/'state',self.root/'units')
        coin=json.loads(files[self.root/'cfg/coins.d/PRL.json'])
        moved={U:{'index':5,'pci':'00000000:07:00.0'},V:{'index':1,'pci':'00000000:01:00.0'}}
        with patch('per_gpu_miner.inventory',return_value=moved),patch.object(mm,'service_status',side_effect=lambda name:'active' if name==mapping[U] else 'inactive'):
            status,runtime=mm.coin_runtime(coin)
        self.assertEqual(status,'active');self.assertEqual(runtime['gpu_indices'],[5]);self.assertEqual(runtime['gpu_services']['5'],mapping[U])
        with patch.object(mm,'read_rate',return_value={'status':'正常','total':200,'unit':'TH/s','per_gpu':{'0':200},'age_seconds':1}):
            rate=mm.read_gpu_services_rate(runtime,time.time(),{})
        self.assertEqual(rate['per_gpu'],{'5':200})
    def test_duplicate_uuid_services_rejected(self):
        files,_,_=setup.plan('PRL',self.profile,[U,V],self.gpus,self.root/'cfg',self.root/'state',self.root/'units')
        coin=json.loads(files[self.root/'cfg/coins.d/PRL.json']);coin['gpu_uuid_services'][V]=coin['gpu_uuid_services'][U]
        with self.assertRaises(ValueError):mm.validate_config(coin,Path('bad.json'))
    def test_inventory_duplicate_or_bad_identity_is_rejected(self):
        row=f'0, {U}, 00000000:01:00.0\n'
        with self.assertRaises(ValueError):inventory(lambda *a,**k:subprocess.CompletedProcess(a,0,row+row,''))

    def test_numeric_bus_10_cannot_confuse_pci_0a_with_pci_10(self):
        gpus={U:{'index':0,'pci':'00000000:0a:00.0'},V:{'index':1,'pci':'00000000:10:00.0'}}
        def run(args,**kw):return subprocess.CompletedProcess(args,0,'GPU #4: NVIDIA GPU (busID: 10, arch: sm89)','')
        for target in (U,V):
            with self.assertRaisesRegex(RigError,'Ambiguous'):build_argv(load_profile(self.profile),target,gpus,run)
    def test_explicit_hex_bus_can_select_unique_identity(self):
        gpus={U:{'index':0,'pci':'00000000:0a:00.0'},V:{'index':1,'pci':'00000000:10:00.0'}}
        def run(args,**kw):return subprocess.CompletedProcess(args,0,'GPU #4: NVIDIA GPU (busID: 0x10, arch: sm89)','')
        argv=build_argv(load_profile(self.profile),V,gpus,run)
        self.assertEqual(argv[argv.index('--gpu-list')+1],'4')

    def test_supervisor_refuses_own_unit_on_other_gpu(self):
        from per_gpu_miner import supervise
        from unittest.mock import Mock
        child=Mock();child.poll.return_value=None
        probe=Mock();probe.compute.return_value={V:{'our-miner.service'}}
        with self.assertRaisesRegex(RigError,'different GPU'):
            supervise(child,U,self.gpus[U]['pci'],'our-miner.service',probe,lambda:self.gpus,lambda _:None)
    def test_supervisor_ignores_other_units_and_exits_with_child(self):
        from per_gpu_miner import supervise
        from unittest.mock import Mock
        child=Mock();child.poll.side_effect=[None,0];child.returncode=0
        probe=Mock();probe.compute.return_value={U:{'our-miner.service'},V:{'other-miner.service'}}
        self.assertEqual(supervise(child,U,self.gpus[U]['pci'],'our-miner.service',probe,lambda:self.gpus,lambda _:None),0)
    def test_supervisor_stops_if_identity_or_observability_lost(self):
        from per_gpu_miner import supervise
        from unittest.mock import Mock
        child=Mock();child.poll.return_value=None
        with self.assertRaises(RigError):supervise(child,U,self.gpus[U]['pci'],'ours',Mock(),lambda:{},lambda _:None)
        probe=Mock();probe.compute.side_effect=RigError('unavailable')
        with self.assertRaises(RigError):supervise(child,U,self.gpus[U]['pci'],'ours',probe,lambda:self.gpus,lambda _:None)
    def test_cleanup_signals_only_own_new_session_group(self):
        from per_gpu_miner import terminate_child
        from unittest.mock import Mock
        import signal
        child=Mock();child.poll.return_value=None;child.pid=12345
        with patch('per_gpu_miner.os.killpg') as kill:
            terminate_child(child);kill.assert_called_once_with(12345,signal.SIGTERM)

    def test_single_path_directives_are_not_quoted_and_escape_specifiers(self):
        self.assertEqual(setup.path_value('/tmp/case with spaces/%data'),'/tmp/case with spaces/%%data')
        for bad in ('relative','/tmp/bad\npath','/tmp/bad'+chr(92)):
            with self.assertRaises(ValueError):setup.path_value(bad)
