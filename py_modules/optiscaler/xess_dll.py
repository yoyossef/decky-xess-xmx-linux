"""Independent, reversible native DX12 XeSS DLL ownership.

Uses the prefix manager's pinned file operations and global lock, but never
claims prefix ownership and never changes Steam launch options.
"""
import base64
import hashlib
import re
import uuid
from pathlib import Path

from . import steam, xess_sdk
from .xmx import Manager, api, digest, regular_file, same, snapshot

SDK_VERSION = '2.0.2.68'
TRIAL_ORIGINAL_SHA256 = 'd393a7a224ed758959de58160f0f5bb58ef272c3aaff5bc76afda4ab0c16f3cf'
TRIAL_TARGET = 'Engine/Plugins/Runtime/Intel/XeSS/Binaries/ThirdParty/Win64/libxess.dll'


class DllManager(Manager):
    def __init__(self, manager):
        self.__dict__.update(manager.__dict__)

    def _app(self, appid):
        return self.root/'dll-apps'/self._appid(appid)

    @staticmethod
    def _launch(value):
        if not isinstance(value, str) or any(c in value for c in ('\0', '\r', '\n')):
            raise ValueError('Exact single-line active-client launch options required')

    def _target(self, appid, target):
        if not isinstance(target, str) or not Path(target).is_absolute() or '..' in Path(target).parts:
            raise ValueError('Select one exact absolute native XeSS DLL candidate path')
        path = Path(target)
        if path.name.lower() != 'libxess.dll':
            raise ValueError('Only native DX12 libxess.dll can be updated; DX11 is not supported')
        game = steam.find_by_appid(self.home, appid)
        if not game or game.get('source') != 'steam' or game.get('appid') != appid:
            raise ValueError('A matching installed Steam game is required')
        folder = Path(game['path'])
        library = Path(game['library'])
        if not folder.is_relative_to(library/'steamapps/common') or not path.is_relative_to(folder):
            raise ValueError('DLL target escapes the installed Steam game')
        self._no_links(path, Path(path.anchor))
        with self._dirfd(path.parent):
            pass
        self._user_access(path.parent, write=True, execute=True)
        self._user_access(path, read=True)
        current = snapshot(path)
        if not current or current['uid'] != self.home.stat().st_uid:
            raise ValueError('DLL must be an existing regular file owned by the target user')
        self._metadata(current)
        return path

    def _metadata(self, value):
        if any(type(value.get(name)) is not int or not -(1 << 63) <= value[name] < (1 << 63)
               for name in ('atime_ns', 'mtime_ns')):
            raise ValueError('DLL restore timestamps must be bounded integer nanoseconds')
        if (value['uid'] != self.home.stat().st_uid or value['gid'] not in self._user_groups()
                or value['mode'] & ~0o777 or any(not name.startswith('user.') and name not in
                    ('system.posix_acl_access', 'security.selinux') for name in value.get('xattrs', {}))):
            raise ValueError('Privileged DLL restore metadata is not supported')
        for encoded in value.get('xattrs', {}).values():
            base64.b64decode(encoded, validate=True)

    def _sdk(self):
        path = self.root/'sdk-cache'/xess_sdk.SDK_TAG/'libxess.dll'
        info = xess_sdk.inspect_dll(path)
        self._user_access(path, read=True)
        if (info['sha256'] != xess_sdk.DLL_SHA256 or info['version'] != SDK_VERSION or
                info['architecture'] != 'x86_64' or 'xessD3D12Init' not in info['exports']):
            raise ValueError('Prepared SDK DLL does not match pinned official provenance')
        return path, info

    @staticmethod
    def _exports(old, new):
        # The pinned DX12 library also exposes internal INTC_D3D11_* entry
        # points. They do not make it the separate public DX11 XeSS library.
        if 'xessD3D12Init' not in old['exports'] or any(e.startswith('xessD3D11') for e in old['exports']):
            raise ValueError('Target does not expose an unambiguous native DX12 XeSS API')
        coverage = xess_sdk.compare_exports(old, new)
        if coverage['missing_exports']:
            raise ValueError('Candidate DLL lacks required exported names: '+str(coverage['missing_exports']))
        if old['ordinal_only_exports']:
            raise ValueError('Ordinal-only DLL function identity cannot be verified statically')
        # Named imports bind by name; their hint/ordinal positions may move in a
        # new SDK. That is not itself proof of API incompatibility. The caller
        # must expose the remaining ordinal-import/ABI risk in the preview;
        # this deliberately remains an explicit reversible experiment.
        return {'named_exports_preserved': True,
                'changed_named_ordinals': coverage['changed_ordinals'] + coverage['missing_ordinals'],
                'ordinal_imports_verified': False, 'runtime_verified': False,
                'warning': 'Experimental DLL replacement: exported names are preserved, but '
                           'game imports by ordinal, function ABI and rendering compatibility '
                           'are not verified. The game may fail; keep the original backup.'}

    def _trial(self, appid, target=None):
        if appid != '1817230':
            raise ValueError('Only the separately recognized Hi-Fi RUSH trial can be imported')
        directory = self.home/'.local/state/decky-xess-compat-tests/1817230/sdk-3.0.2'
        manifest, backup = directory/'manifest.json', directory/'original-libxess.dll'
        record = self._load(manifest)
        keys = {'appid', 'target', 'backup', 'original_sha256', 'candidate_sha256', 'sdk_tag',
                'sdk_archive_sha256', 'original_mode', 'original_atime_ns', 'original_mtime_ns',
                'original_xattrs', 'phase'}
        if not isinstance(record, dict) or set(record) != keys:
            raise ValueError('Unrecognized trial manifest schema')
        game = steam.find_by_appid(self.home, appid)
        if not game:
            raise ValueError('Trial game not found')
        expected = str(Path(game['path'])/TRIAL_TARGET)
        if (record['appid'] != appid or record['target'] != expected or
                target is not None and str(target) != expected or record['backup'] != str(backup) or
                record['original_sha256'] != TRIAL_ORIGINAL_SHA256 or
                record['candidate_sha256'] != xess_sdk.DLL_SHA256 or
                record['sdk_tag'] != xess_sdk.SDK_TAG or record['phase'] != 'installed' or
                record['sdk_archive_sha256'] != xess_sdk.SDK_ARCHIVE_SHA256 or
                record['original_mode'] != 0o755):
            raise ValueError('Trial provenance or exact target conflict')
        self._target(appid, expected)
        manifest_state, original, current = snapshot(manifest), snapshot(backup), snapshot(expected)
        if not manifest_state or manifest_state['uid'] != self.home.stat().st_uid or not original or not current:
            raise ValueError('Trial manifest, backup or target missing or not user-owned')
        wanted = dict(original, sha256=record['original_sha256'], mode=record['original_mode'],
                      uid=self.home.stat().st_uid, gid=self.home.stat().st_gid,
                      atime_ns=record['original_atime_ns'], mtime_ns=record['original_mtime_ns'],
                      xattrs=record['original_xattrs'])
        self._metadata(wanted)
        if not same(original, wanted) or not same(current, dict(wanted, sha256=xess_sdk.DLL_SHA256)):
            raise ValueError('Trial backup/target hash or metadata conflict')
        old_info = xess_sdk.inspect_dll(backup)
        new_info = xess_sdk.inspect_dll(Path(expected))
        if new_info['version'] != SDK_VERSION:
            raise ValueError('Trial candidate version conflict')
        # Exact pinned provenance above recognizes a separately approved trial
        # whose candidate is already installed. This validates named coverage,
        # not game imports, ABI or rendering compatibility on another title.
        self._exports(old_info, new_info)
        return {'manifest': manifest_state, 'backup': original, 'record': record, 'original': wanted}

    def _check_trial(self, record):
        if record.get('trial'):
            current = self._trial(record['appid'], record['operations'][0]['target'])
            if (current['record'] != record['trial']['record'] or
                    current['original'] != record['trial']['original'] or
                    not same(current['manifest'], record['trial']['manifest']) or
                    not same(current['backup'], record['trial']['backup'])):
                raise ValueError('Trial changed since preview; retaining journal and backups')

    def _validate(self, record, appid):
        app = self._app(appid)
        if (record.get('schema') != 1 or record.get('scope') != 'dll' or
                record.get('appid') != appid or record.get('prefix') != str(self._prefix(appid)) or
                record.get('action') not in ('enable', 'disable') or len(record.get('operations', [])) != 1):
            raise ValueError('Invalid or relocated DLL transaction')
        self._launch(record['old_launch_options'])
        if record['desired_launch_options'] != record['old_launch_options']:
            raise ValueError('DLL transactions must not change launch options')
        item = record['operations'][0]
        target = self._target(appid, item['target'])
        data = base64.b64decode(item['data'], validate=True)
        if (record.get('sdk_tag') != xess_sdk.SDK_TAG or record.get('sdk_sha256') != xess_sdk.DLL_SHA256
                or item['installed_sha256'] != xess_sdk.DLL_SHA256
                or hashlib.sha256(data).hexdigest() != xess_sdk.DLL_SHA256):
            raise ValueError('DLL transaction payload does not match pinned official SDK provenance')
        self._metadata(item['old'])
        if (bool(record.get('trial')) != bool(record.get('restore_original')) or
                item['old']['sha256'] == xess_sdk.DLL_SHA256 and not record.get('trial')):
            raise ValueError('Existing official DLL requires recognized original restore provenance')
        if item['mode'] != item['old']['mode']:
            raise ValueError('DLL transaction permissions mismatch')
        if record.get('created_dirs'):
            raise ValueError('DLL transaction cannot create game directories')
        if item.get('temporary'):
            path = Path(item['temporary'])
            if (path.parent != target.parent or
                    not re.fullmatch(r'\.decky-xmx-[0-9a-f]{32}-0\.tmp', path.name)):
                raise ValueError('DLL staging path is not allowlisted')
            self._no_links(path, Path(path.anchor))
        if item.get('backup'):
            backup = Path(item['backup'])
            relative = backup.relative_to(app/'backups')
            if (len(relative.parts) != 2 or relative.name not in ('0', 'original') or
                    not re.fullmatch('[0-9a-f]{32}', relative.parts[0])):
                raise ValueError('DLL backup path is not allowlisted')
            self._no_links(backup, app)
        if record.get('restore_original'):
            original = record['restore_original']
            self._metadata(original['old'])
            expected_backup = app/'backups'/record['plan_id']/'original'
            if original['backup'] != str(expected_backup):
                raise ValueError('Trial original backup is not allowlisted')
            self._no_links(expected_backup, app)
            if (not record.get('trial') or original['old'] != record['trial']['original']):
                raise ValueError('Trial restore provenance conflict')
        return record

    def _clean_temps(self, journal):
        # A prefix-of-payload test cannot establish ownership of a pre-existing
        # empty/partial file when exclusive staging creation failed. Only remove
        # a complete, durably recorded replacement (or complete restore stage).
        item = journal['operations'][0]
        if not item.get('temporary'):
            return
        temporary = Path(item['temporary'])
        current = snapshot(temporary)
        if current is None:
            return
        if not (item.get('installed') and same(current, item['installed']) or
                journal['phase'] == 'restoring' and same(current, item['old'])):
            raise ValueError(f'Unrecognized DLL staging file; retaining journal and file: {temporary}')
        self._unlink(temporary, expected=current, owner=journal)

    def _matches_owned(self, item, current):
        return bool(item.get('installed')) and same(current, item['installed']) and current['sha256'] == item['installed_sha256']

    @api
    def get_xess_dll_status(self, appid):
        app = self._app(appid)
        installed = self._installed(appid)
        journal = self._journal(appid)
        candidates = []
        for name in self._native_xess(appid)['files']:
            item = {'path': name, 'version': None, 'architecture': None, 'sha256': None}
            try:
                info = xess_sdk.inspect_dll(Path(name))
                item.update({k: info[k] for k in ('version', 'architecture', 'sha256')})
                if Path(name).name.lower() != 'libxess.dll':
                    raise ValueError('DX11 DLL; official replacement is DX12 only')
                self._target(appid, name)
            except (OSError, ValueError) as exc:
                item['error'] = str(exc)
            candidates.append(item)
        sdk = {'tag': xess_sdk.SDK_TAG, 'version': SDK_VERSION, 'ready': False}
        try:
            self._sdk()
            sdk['ready'] = True
        except (OSError, ValueError) as exc:
            sdk['reason'] = str(exc)
        phase = journal['phase'] if journal else 'enabled' if installed else 'disabled'
        if journal and phase not in ('pending_enable', 'pending_disable'):
            phase = 'recovery_required'
        trial_available = False
        if not installed and not journal:
            try:
                self._trial(appid)
                trial_available = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return {'ok': True, 'installed': bool(installed), 'phase': phase,
                'pending': self._pending(journal) if journal else None,
                'candidates': candidates, 'sdk': sdk, 'trial_available': trial_available}

    @api
    def plan_xess_dll(self, appid, target_path, launch_options, action='enable', adopt_trial=False):
        app = self._app(appid)
        self._launch(launch_options)
        if (action not in ('enable', 'disable') or type(adopt_trial) is not bool or
                adopt_trial and action != 'enable'):
            raise ValueError('Unsupported DLL action or trial adoption')
        with self._lock():
            self._scope_clear('dll')
            if self._legacy(appid):
                raise ValueError('Legacy XeSS XMX install detected; no implicit adoption')
            prefix = self._prefix(appid)
            self._stopped(appid, prefix)
            if self._journal(appid) is not None:
                raise ValueError('Pending DLL operation requires recovery')
            target = self._target(appid, target_path)
            installed, owner = self._installed_state(appid)
            token = uuid.uuid4().hex
            compatibility = None
            if action == 'disable':
                if not installed:
                    raise ValueError('DLL update is not installed')
                self._validate(installed, appid)
                if installed['operations'][0]['target'] != str(target):
                    raise ValueError('Restore target conflicts with the owned DLL')
                self._owned(installed['operations'])
                plan = dict(installed, action=action, phase='planned', plan_id=token,
                            generation=self._generation(app), old_launch_options=launch_options,
                            desired_launch_options=launch_options,
                            checks={str(target): snapshot(target)})
            else:
                if installed:
                    raise ValueError('DLL update already installed; restore first')
                candidate, info = self._sdk()
                old_info = xess_sdk.inspect_dll(target)
                compatibility = self._exports(old_info, info)
                old = snapshot(target)
                with regular_file(candidate) as source:
                    data = source.read()
                if hashlib.sha256(data).hexdigest() != info['sha256'] or old['sha256'] != old_info['sha256']:
                    raise ValueError('DLL changed during planning')
                trial = self._trial(appid, target) if adopt_trial else None
                if old['sha256'] == info['sha256'] and not trial:
                    raise ValueError('Official DLL already present without ownership; explicit recognized trial adoption required')
                plan = {'schema': 1, 'scope': 'dll', 'appid': appid, 'prefix': str(prefix),
                        'action': action, 'phase': 'planned', 'plan_id': token,
                        'generation': self._generation(app), 'old_launch_options': launch_options,
                        'desired_launch_options': launch_options, 'sdk_tag': xess_sdk.SDK_TAG,
                        'sdk_sha256': info['sha256'], 'operations': [{
                            'target': str(target), 'old': old, 'mode': old['mode'],
                            'data': base64.b64encode(data).decode(), 'installed_sha256': info['sha256']}]}
                if trial:
                    plan.update(trial=trial, restore_original={'old': trial['original'],
                                'backup': str(app/'backups'/token/'original')})
            self._validate(plan, appid)
            plan['installed_owner'] = owner
            self._save(app/'plans'/(token+'.json'), plan)
            before = plan.get('checks', {}).get(str(target), plan['operations'][0]['old'])
            return {'ok': True, **self._pending(plan), 'operations': [
                {'target': str(target), 'operation': 'restore' if action == 'disable' else 'install',
                 'before_sha256': before['sha256'],
                 'after_sha256': plan['operations'][0]['old']['sha256'] if action == 'disable'
                                  else plan['sdk_sha256'],
                 'compatibility': compatibility}]}

    def _guard(self, appid, prefix):
        self._stopped(appid, Path(prefix))
        if self._legacy(appid):
            raise ValueError('Legacy XeSS XMX install detected; pending state retained')

    @api
    def apply_xess_dll(self, appid, plan_id):
        with self._lock():
            self._scope_clear('dll')
            app = self._app(appid)
            plan = self._record(appid, plan_id)
            self._guard(appid, plan['prefix'])
            self._check_trial(plan)
            if self._journal(appid) is not None or plan['phase'] != 'planned':
                raise ValueError('DLL plan consumed or operation pending')
            if self._generation(app) != plan['generation']:
                raise ValueError('Stale DLL plan generation')
            self._check_owner(plan)
            item = plan['operations'][0]
            expected = plan.get('checks', {}).get(item['target'], item['old'])
            if not same(snapshot(item['target']), expected):
                raise ValueError('Stale DLL target changed')
            if plan['action'] == 'enable':
                _, candidate = self._sdk()
                if candidate['sha256'] != plan['sdk_sha256']:
                    raise ValueError('Stale SDK source')
                self._exports(xess_sdk.inspect_dll(Path(item['target'])), candidate)
            self._check_owner(plan)
            self._save(app/'generation.json', uuid.uuid4().hex)
            plan['phase'] = 'consumed'
            self._save(app/'plans'/(plan_id+'.json'), plan)
            if plan['action'] == 'disable':
                self._owned(plan['operations'])
                plan['phase'] = 'pending_disable'
                self._save(app/'journal.json', plan)
                return {'ok': True, 'pending_launch': True, **self._pending(plan)}
            item['backup'] = str(app/'backups'/plan_id/'0')
            item['temporary'] = str(Path(item['target']).parent/('.decky-xmx-'+plan_id+'-0.tmp'))
            plan['phase'] = 'writing_enable'
            self._save(app/'journal.json', plan)
            if plan.get('trial'):
                self._check_trial(plan)
                with regular_file(plan['trial']['record']['backup']) as source:
                    original = source.read()
                if hashlib.sha256(original).hexdigest() != plan['restore_original']['old']['sha256']:
                    raise ValueError('Trial original changed during import')
                self._atomic(plan['restore_original']['backup'], original, expected=None, owner=plan)
            with regular_file(item['target']) as source:
                original = source.read()
            if hashlib.sha256(original).hexdigest() != item['old']['sha256']:
                raise ValueError('DLL changed while creating backup; recovery required')
            self._atomic(item['backup'], original, expected=None, owner=plan)
            def prepared(installed):
                item['installed'] = installed
                self._save(app/'journal.json', plan)
                self._guard(appid, plan['prefix'])
            metadata = dict(item['old'], sha256=item['installed_sha256'])
            self._atomic(item['target'], base64.b64decode(item['data']), item['mode'], metadata,
                         temporary=item['temporary'], before_replace=prepared, expected=item['old'], owner=plan)
            self._owned(plan['operations'])
            plan['phase'] = 'pending_enable'
            self._check_owner(plan)
            self._save(app/'journal.json', plan)
            return {'ok': True, 'pending_launch': True, **self._pending(plan)}

    @api
    def finish_xess_dll(self, appid, plan_id, launch_options):
        with self._lock():
            app = self._app(appid)
            self._record(appid, plan_id)
            journal = self._journal(appid)
            if not journal or journal['plan_id'] != plan_id:
                raise ValueError('No matching pending DLL operation')
            self._validate(journal, appid)
            self._guard(appid, journal['prefix'])
            if launch_options != journal['desired_launch_options']:
                raise ValueError('Steam launch options do not exactly match the expected value')
            self._check_owner(journal)
            if journal['action'] == 'disable':
                self._restore(journal)
                self._remove_owner(journal)
            else:
                if journal['phase'] != 'pending_enable':
                    raise ValueError('Incomplete DLL operation requires abort recovery')
                self._sdk()
                self._check_trial(journal)
                self._owned(journal['operations'])
                committed = dict(journal, phase='enabled')
                if journal.get('restore_original'):
                    original = journal['restore_original']
                    if digest(original['backup']) != original['old']['sha256']:
                        raise ValueError('Imported original backup changed')
                    committed['operations'] = [dict(journal['operations'][0], **original)]
                    committed.pop('restore_original')
                    committed.pop('trial')
                self._save_owner(journal, committed)
            self._unlink(app/'journal.json', owner=journal)
            return {'ok': True, 'installed': journal['action'] == 'enable',
                    'phase': 'enabled' if journal['action'] == 'enable' else 'disabled', 'pending_launch': False}

    @api
    def recover_xess_dll(self, appid, plan_id, launch_options, action='abort'):
        if action == 'resume':
            return self.finish_xess_dll(appid, plan_id, launch_options)
        if action != 'abort':
            raise ValueError('Recovery action must be abort or resume')
        with self._lock():
            app = self._app(appid)
            self._record(appid, plan_id)
            journal = self._journal(appid)
            if not journal or journal['plan_id'] != plan_id:
                raise ValueError('No matching pending DLL operation')
            self._validate(journal, appid)
            self._guard(appid, journal['prefix'])
            if launch_options != journal['old_launch_options']:
                raise ValueError('Restore and confirm original launch options before abort')
            self._check_owner(journal)
            if journal['action'] == 'disable':
                if journal['phase'] != 'pending_disable':
                    raise ValueError('DLL restoration already started; resume instead')
            else:
                self._restore(journal)
                self._remove_owner(journal)
            self._unlink(app/'journal.json', owner=journal)
            installed = bool(self._installed(appid))
            return {'ok': True, 'installed': installed, 'phase': 'enabled' if installed else 'disabled',
                    'pending_launch': False}
