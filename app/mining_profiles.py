# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Private per-coin payout drafts and explicit, rollback-safe miner configuration updates.

The browser supplies addresses, never paths, commands, credentials or service names.
Only operator-registered JSON fields and managed user services may be changed.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

COINS = {'PRL': 'Pearl', 'QTC': 'Quantus', 'NOID': 'NOID', 'CSD': 'Chain Solution', 'TSC': 'TensorCash'}
FIELDS = {'address', 'worker', 'owner_label', 'exchange'}
LEAVES = {'wallet', 'address', 'user', 'worker', 'wallet_address', 'mining_address', 'worker_name'}


class ProfileError(RuntimeError):
    pass


def private_bytes(path, optional=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if optional:
            return None
        raise ProfileError('矿工配置文件不存在') from None
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 1_000_000:
            raise ProfileError('配置文件必须由当前矿工用户持有，且仅本人可访问')
        return f.read()


def atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.payout-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def valid_prl(address):
    """Validate native PRL v0/v1 witness addresses, including Bech32/Bech32m checksum."""
    if address != address.lower() or not 14 <= len(address) <= 90 or not address.startswith('prl1'):
        return False
    alphabet = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
    try:
        data = [alphabet.index(c) for c in address[4:]]
    except ValueError:
        return False
    values = [ord(c) >> 5 for c in 'prl'] + [0] + [ord(c) & 31 for c in 'prl'] + data
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ value
        for i, generator in enumerate([0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]):
            if (top >> i) & 1:
                chk ^= generator
    if len(data) < 7 or data[0] not in (0, 1) or chk != (1 if data[0] == 0 else 0x2bc830a3):
        return False
    acc = bits = 0
    program = []
    for value in data[1:-6]:
        acc = (acc << 5) | value
        bits += 5
        if bits >= 8:
            bits -= 8
            program.append((acc >> bits) & 255)
    if bits >= 5 or ((acc << (8 - bits)) & 255):
        return False
    return len(program) in (20, 32) if data[0] == 0 else len(program) == 32


class MiningProfiles:
    def __init__(self, registry_path, state_path, rig, coins=None):
        self.registry_path, self.state_path = Path(registry_path), Path(state_path)
        self.get_rig = rig if callable(rig) else lambda: rig
        self.get_coins = coins

    def _registry(self):
        raw = private_bytes(self.registry_path, optional=True)
        if raw is None:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'providers'} or not isinstance(value['providers'], dict):
                raise ValueError()
            bound_fields, bound_units = {}, set()
            for symbol, p in value['providers'].items():
                if not re.fullmatch(r'[A-Z0-9]{2,12}', symbol) or not isinstance(p, dict) or set(p) - {'name', 'pool', 'services', 'files', 'initial', 'address_regex', 'external_locks'}:
                    raise ValueError()
                locks = p.get('external_locks', [])
                if not isinstance(locks, list) or any(not isinstance(v, str) or not Path(v).is_absolute() for v in locks):
                    raise ValueError()
                if any(k in p and (not isinstance(p[k], str) or len(p[k]) > 256 or any(ord(c) < 32 for c in p[k])) for k in ['name', 'pool']) or '@' in p.get('pool', ''):
                    raise ValueError()
                if not isinstance(p.get('services'), list) or not p['services'] or any(not isinstance(u, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9@_.:-]*\.service', u) for u in p['services']) or len(set(p['services'])) != len(p['services']) or bound_units & set(p['services']):
                    raise ValueError()
                bound_units.update(p['services'])
                if not isinstance(p.get('files'), list) or not 1 <= len(p['files']) <= 16:
                    raise ValueError()
                seen = set()
                payout_fields = 0
                for target in p['files']:
                    if not isinstance(target, dict) or set(target) != {'path', 'fields'} or not Path(target['path']).is_absolute():
                        raise ValueError()
                    canonical = Path(target['path']).resolve()
                    if canonical in seen or canonical in {self.registry_path.resolve(), self.state_path.resolve()}:
                        raise ValueError()
                    seen.add(canonical)
                    if not isinstance(target['fields'], list) or not 1 <= len(target['fields']) <= 16:
                        raise ValueError()
                    paths = set()
                    for field in target['fields']:
                        if not isinstance(field, dict) or set(field) != {'path', 'template'}:
                            raise ValueError()
                        path = field['path']
                        if not isinstance(path, list) or not 1 <= len(path) <= 12 or any(isinstance(k, bool) or not isinstance(k, (int, str)) or isinstance(k, int) and k < 0 for k in path) or tuple(path) in paths:
                            raise ValueError()
                        paths.add(tuple(path))
                        previous = bound_fields.setdefault(canonical, [])
                        if any(tuple(path[:len(other)]) == other or tuple(other[:len(path)]) == tuple(path) for other in previous):
                            raise ValueError()
                        previous.append(tuple(path))
                        if isinstance(path[-1], int):
                            if len(path) < 2 or path[-2] != 'argv' or not 1 <= path[-1] < 128:
                                raise ValueError()
                        elif path[-1].lower() not in LEAVES:
                            raise ValueError()
                        template = field['template']
                        if template not in {'{address}', '{worker}', '{mining_user}'}:
                            raise ValueError()
                        if isinstance(path[-1], str) and (path[-1].lower() in {'worker', 'worker_name'}) != (template == '{worker}'):
                            raise ValueError()
                        if template in {'{address}', '{mining_user}'}: payout_fields += 1
                if not payout_fields:
                    raise ValueError()
                if 'address_regex' in p:
                    re.compile(p['address_regex'])
                if 'initial' in p:
                    self._binding(symbol, p['initial'], p)
            return value['providers']
        except (ValueError, TypeError, KeyError, re.error):
            raise ProfileError('收款适配器配置无效，需由本机维护者核对') from None

    def _state(self):
        raw = private_bytes(self.state_path, optional=True)
        if raw is None:
            return {'version': 1, 'profiles': {}}
        try:
            data = json.loads(raw)
            if data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
                raise ValueError()
            return data
        except (ValueError, AttributeError):
            raise ProfileError('收款设置状态不可读取，请先核对备份') from None

    def _binding(self, symbol, body, provider):
        if set(body) != FIELDS or any(not isinstance(v, str) or any(ord(c) < 32 for c in v) for v in body.values()):
            raise ValueError('只允许公开收款地址、矿工名、使用者与地址来源')
        b = {k: v.strip() for k, v in body.items()}
        if b['address'] and (len(b['address']) > 256 or not re.fullmatch(provider.get('address_regex', r'[A-Za-z0-9][A-Za-z0-9:_-]{7,255}'), b['address'])):
            raise ValueError('收款地址格式不正确')
        if symbol == 'PRL' and b['address'] and not valid_prl(b['address']):
            raise ValueError('PRL 地址的网络前缀或校验和不正确')
        if b['worker'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', b['worker']):
            raise ValueError('矿工名限1–32位字母、数字、下划线或短横线')
        if len(b['owner_label']) > 64 or b['exchange'] not in {'wallet', 'safetrade'}:
            raise ValueError('使用者或地址来源无效')
        return b

    @staticmethod
    def _value(binding, template):
        return template.format(**binding, mining_user=binding['address'] + ('.' + binding['worker'] if binding['worker'] else ''))

    def _sources(self, provider):
        sources = []
        digest = hashlib.sha256(encoded(provider))
        for target in provider['files']:
            path = Path(target['path'])
            raw = private_bytes(path)
            try:
                data = json.loads(raw)
                for field in target['fields']:
                    node = data
                    for key in field['path']:
                        if isinstance(node, list) != isinstance(key, int):
                            raise ValueError()
                        node = node[key]
                    if not isinstance(node, str):
                        raise ValueError()
                    if isinstance(field['path'][-1], int):
                        argv = data
                        for key in field['path'][:-1]: argv = argv[key]
                        previous = argv[field['path'][-1] - 1]
                        if previous not in {'--user', '-u', '--wallet', '--address', '--username', '--worker', '--worker-name', '--rig-id'}:
                            raise ValueError()
                        if (previous in {'--worker', '--worker-name', '--rig-id'}) != (field['template'] == '{worker}'):
                            raise ValueError()
            except (ValueError, KeyError, IndexError, TypeError):
                raise ProfileError('已登记的矿工收款字段与源文件不匹配') from None
            digest.update(str(path).encode()); digest.update(raw)
            sources.append((target, raw, data))
        return sources, digest.hexdigest()

    def _coin_services(self, symbol, rig):
        """A managed service is usable only when its actual profile belongs to this coin."""
        allowed = set()
        for unit, cards in rig.services.items():
            if cards and all(getattr(rig, 'gpus', {}).get(gpu, {}).get('profiles', {}).get(symbol) == unit
                             and set(k for k, v in rig.gpus[gpu]['profiles'].items() if v == unit) == {symbol}
                             for gpu in cards):
                allowed.add(unit)
        if self.get_coins is not None:
            for coin in self.get_coins():
                if coin.get('symbol') == symbol and coin.get('kind') in {'cpu', 'cpu_gpu'}:
                    unit = coin.get('service')
                    if unit in getattr(rig, 'cpu_services', set()): allowed.add(unit)
        return allowed

    def _recovery_dir(self, symbol):
        return self.state_path.parent / ('recovery-' + symbol)

    @staticmethod
    def _clear_backup(backup):
        for path in backup.iterdir(): path.unlink()
        backup.rmdir()

    def _row(self, symbol, state, registry):
        p = registry.get(symbol, {})
        saved = state['profiles'].get(symbol, {})
        if not isinstance(saved, dict) or set(saved) - {'active', 'draft', 'revision'} or isinstance(saved.get('revision', 0), bool) or not isinstance(saved.get('revision', 0), int) or saved.get('revision', 0) < 0:
            raise ProfileError('私有收款状态格式无效，请核对备份')
        initial = p.get('initial')
        active = saved.get('active', initial)
        draft = saved.get('draft') or active or dict(address='', worker='', owner_label='', exchange='wallet')
        try:
            active = self._binding(symbol, active, p) if active is not None else None
            draft = self._binding(symbol, draft, p)
        except (ValueError, TypeError):
            raise ProfileError('私有收款字段格式无效，请核对备份') from None
        ready = False; version = ''; error = None
        if p:
            try:
                rig = self.get_rig(); rig._require_config()
                if not self._coin_services(symbol, rig) or set(p['services']) != self._coin_services(symbol, rig):
                    raise ProfileError('矿工服务与该币种的完整登记不匹配，当前只能保存草稿')
                if self._recovery_dir(symbol).exists():
                    raise ProfileError('上次应用尚未完整恢复，请维护者核对私有 recovery 备份')
                sources, version = self._sources(p)
                if self.get_coins is not None:
                    coin = next((c for c in self.get_coins() if c.get('symbol') == symbol), {})
                    feeds = [coin[k] for k in ['earnings', 'account', 'daily'] if isinstance(coin.get(k), dict) and coin[k].get('url')]
                    if feeds:
                        source = coin.get('address_source') or {}
                        matched = any(Path(source.get('file', '/nonexistent')).resolve() == Path(t['path']).resolve() and source.get('json_path') == '.'.join(map(str, f['path'])) and (f['template'] == '{address}' or f['template'] == '{mining_user}' and source.get('split') == '.') for t in p['files'] for f in t['fields'])
                        if not matched or any('{address}' not in f['url'] for f in feeds):
                            raise ProfileError('矿池统计尚未绑定同一收款源')
                ready = True
                if active:
                    active = self._binding(symbol, active, p)
                    for target, _, data in sources:
                        for field in target['fields']:
                            node = data
                            for key in field['path']: node = node[key]
                            if node != self._value(active, field['template']):
                                active = None; error = '源配置已变化，无法确认当前收款设置；请重新核对'
                                break
                        if active is None: break
            except ProfileError as exc:
                ready = False; active = None; error = str(exc)
            except (OSError, ValueError, RuntimeError):
                ready = False; active = None
                error = '矿工适配器或源文件尚未就绪，当前只能保存草稿'
        else:
            error = '尚未配置此币种的矿工适配器；保存草稿不会改动矿工'
        return dict(symbol=symbol, name=p.get('name', COINS.get(symbol, symbol)), pool=p.get('pool', '尚未配置'), adapter_ready=ready,
                    active=active, draft=draft, revision=saved.get('revision', 0), source_version=version, error=error)

    def snapshot(self):
        with self.get_rig()._locked():
            registry, state = self._registry(), self._state()
            return {'configured': bool(registry), 'profiles': [self._row(s, state, registry) for s in dict.fromkeys([*COINS, *registry])]}

    @staticmethod
    def _revision(body, current):
        value = body.get('expected_revision')
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('缺少有效的配置版本')
        if value != current:
            raise ProfileError('设置已被其他页面更改，请重新读取后再操作')

    def save(self, body):
        if not isinstance(body, dict) or set(body) != FIELDS | {'symbol', 'expected_revision'}:
            raise ValueError('收款设置字段不正确')
        with self.get_rig()._locked():
            registry, state = self._registry(), self._state(); symbol = body['symbol']
            if not isinstance(symbol, str) or symbol not in {*COINS, *registry}:
                raise ValueError('未知币种')
            row = self._row(symbol, state, registry); self._revision(body, row['revision'])
            binding = self._binding(symbol, {k: body[k] for k in FIELDS}, registry.get(symbol, {}))
            saved = state['profiles'].setdefault(symbol, {})
            saved.update(draft=binding, revision=row['revision'] + 1)
            atomic_bytes(self.state_path, encoded(state))
            return {'ok': True, 'profile': self._row(symbol, state, registry), 'message': '草稿已保存；当前矿工和收款地址尚未更改'}

    def apply(self, body):
        if not isinstance(body, dict) or set(body) != {'symbol', 'expected_revision', 'source_version'}:
            raise ValueError('应用设置字段不正确')
        rig = self.get_rig()
        initial = self._registry()
        symbol = body['symbol']
        if not isinstance(symbol, str) or symbol not in initial:
            raise ProfileError('此币种尚未配置可应用的矿工适配器')
        locks = set(initial[symbol].get('external_locks', []))
        for adapter in getattr(rig, 'miners', {}).values():
            if isinstance(adapter, dict) and set(adapter.get('services', [])) & set(initial[symbol]['services']):
                locks.update(adapter.get('external_locks', []))
        with rig._locked(external_locks=tuple(sorted(locks))):
            registry, state = self._registry(), self._state(); symbol = body['symbol']
            if not isinstance(symbol, str) or symbol not in registry:
                raise ProfileError('此币种尚未配置可应用的矿工适配器')
            if encoded(initial[symbol]) != encoded(registry[symbol]):
                raise ProfileError('矿工适配器已变化，请重新读取')
            row = self._row(symbol, state, registry); self._revision(body, row['revision'])
            if not row['adapter_ready'] or not row['source_version'] or body['source_version'] != row['source_version']:
                raise ProfileError('源配置尚未就绪或已更改，请重新读取后核对')
            p = registry[symbol]; binding = self._binding(symbol, row['draft'], p)
            if not binding['address']:
                raise ValueError('应用前必须填写收款地址')
            if binding['worker'] and not any('{worker}' in f['template'] or '{mining_user}' in f['template'] for t in p['files'] for f in t['fields']):
                raise ValueError('此矿工适配器不支持矿工名，请将矿工名留空')
            if binding == row['active']:
                return {'ok': True, 'profile': row, 'message': '当前收款设置已经生效，无需重启矿工', 'restarted_services': [], 'payout_changed': False}
            sources, version = self._sources(p)
            if version != body['source_version']: raise ProfileError('源配置已更改，请重新读取')
            updates = []
            for target, raw, data in sources:
                updated = copy.deepcopy(data)
                for field in target['fields']:
                    node = updated
                    for key in field['path'][:-1]: node = node[key]
                    node[field['path'][-1]] = self._value(binding, field['template'])
                if updated != data: updates.append((Path(target['path']), raw, encoded(updated)))
            payout_changed = row['active'] is None or row['active']['address'] != binding['address']
            if not updates:
                state['profiles'].setdefault(symbol, {}).update(active=binding, draft=binding, revision=row['revision'] + 1)
                atomic_bytes(self.state_path, encoded(state))
                return {'ok': True, 'profile': self._row(symbol, state, registry), 'message': '身份备注已更新；矿工参数未变化，无需重启', 'restarted_services': [], 'payout_changed': payout_changed}
            states = {u: rig._status(u) for u in rig.services}
            if any(states[u] not in {'active', 'inactive', 'failed'} for u in p['services']):
                raise ProfileError('矿工正在切换状态，请稍后重试')
            running = [u for u in p['services'] if states[u] == 'active']
            boot = {u: rig._enabled(u) for u in p['services']}
            compute = rig.probe.compute()
            for unit in running:
                if any(unit in owners and gpu not in rig.services[unit] for gpu, owners in compute.items()):
                    raise ProfileError('矿工实际占用与登记显卡不一致，应用已拦截')
                for gpu in rig.services[unit]:
                    if compute.get(gpu, set()) - set(running):
                        raise ProfileError('显卡还有其他计算任务，应用已拦截')
            old_state = private_bytes(self.state_path, optional=True)
            desired_path = rig.state_dir / 'desired.json'
            desired = desired_path.read_bytes() if desired_path.exists() else None
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = self._recovery_dir(symbol)
            backup.mkdir(mode=0o700)
            atomic_bytes(backup / 'manifest.json', encoded({'symbol': symbol, 'files': [str(path) for path, _, _ in updates], 'running': running, 'old_state_exists': old_state is not None, 'phase': 'prepared'}))
            for i, (_, raw, _) in enumerate(updates): atomic_bytes(backup / f'{i}.json', raw)
            if old_state is not None: atomic_bytes(backup / 'state.json', old_state)
            written = []; state_written = False
            try:
                if running:
                    rig._command('stop', *running)
                    for u in running: rig._wait(u, {'inactive', 'failed'})
                    after_stop = rig.probe.compute()
                    if any(set(running) & owners for owners in after_stop.values()):
                        raise ProfileError('矿工停止后仍占用显卡')
                    if any(after_stop.get(gpu) for unit in running for gpu in rig.services[unit]):
                        raise ProfileError('显卡已被其他计算任务占用')
                if self._sources(p)[1] != version:
                    raise ProfileError('停止期间源配置发生变化')
                for path, old_raw, raw in updates:
                    if private_bytes(path) != old_raw: raise ProfileError('写入前源配置发生变化')
                    atomic_bytes(path, raw); written.append((path, raw))
                state['profiles'].setdefault(symbol, {}).update(active=binding, draft=binding, revision=row['revision'] + 1)
                atomic_bytes(self.state_path, encoded(state))
                state_written = True
                if running:
                    rig._command('start', *running)
                    for u in running: rig._wait(u, 'active')
                    if any(unit in owners and gpu not in rig.services[unit] for unit in running for gpu, owners in rig.probe.compute().items()):
                        raise ProfileError('启动后实际占用与登记显卡不一致')
                if any(rig._enabled(u) != boot[u] for u in boot) or any(rig._status(u) != v for u, v in states.items() if u not in p['services']) or (desired_path.read_bytes() if desired_path.exists() else None) != desired:
                    raise ProfileError('其他服务或开机选择发生变化')
            except Exception:
                restored = True
                try:
                    if running: rig._command('stop', *running)
                    for path, ours in reversed(written):
                        if private_bytes(path) != ours:
                            restored = False  # 不能用回退覆盖事务之后的独立修改
                            continue
                        index = next(i for i, (target, _, _) in enumerate(updates) if target == path)
                        atomic_bytes(path, private_bytes(backup / f'{index}.json'))
                    if state_written:
                        if old_state is None: self.state_path.unlink(missing_ok=True)
                        else: atomic_bytes(self.state_path, private_bytes(backup / 'state.json'))
                    if running:
                        owners = rig.probe.compute()
                        safe = [u for u in running if restored and all(not (owners.get(gpu, set()) - {u}) for gpu in rig.services[u])]
                        if len(safe) != len(running): restored = False
                        if safe:
                            rig._command('start', *safe)
                            for u in safe: rig._wait(u, 'active')
                    if any(rig._enabled(u) != boot[u] for u in boot) or (desired_path.read_bytes() if desired_path.exists() else None) != desired:
                        restored = False
                except Exception:
                    restored = False
                if restored: self._clear_backup(backup)
                raise ProfileError('应用失败；' + ('本次写入已回退，原运行选择已恢复' if restored else '回退不完整，请维护者核对')) from None
            self._clear_backup(backup)
            return {'ok': True, 'profile': self._row(symbol, state, registry), 'message': '收款配置已应用；请继续核对算力与矿池新份额', 'restarted_services': running, 'payout_changed': payout_changed}
