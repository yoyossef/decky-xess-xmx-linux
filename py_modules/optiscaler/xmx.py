"""Native-game XeSS XMX management (no OptiScaler or Steam-config writes)."""
from pathlib import Path
from contextlib import contextmanager
from functools import wraps
from typing import Any
import base64
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import struct
import tarfile
import time
import uuid

from . import steam, xmx_profiles

_UNGUARDED = object()
_MISSING = object()


def api(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
    return call


def digest(path):
    with regular_file(path) as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


@contextmanager
def regular_file(path, *, authorize=None):
    """Pin a no-follow regular file; optionally authorize each exact open FD."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Absolute canonical file required')
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            if authorize:
                authorize(directory, execute=True)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        if authorize:
            authorize(directory, execute=True)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError(f'Not a regular file: {path}')
            if authorize:
                authorize(source.fileno(), read=True)
            yield source
    finally:
        os.close(directory)


def _file_snapshot(source):
    fd = source.fileno()
    st = os.fstat(fd)
    return {'sha256': hashlib.file_digest(source, 'sha256').hexdigest(),
            'mode': stat.S_IMODE(st.st_mode), 'uid': st.st_uid, 'gid': st.st_gid,
            'mtime_ns': st.st_mtime_ns, 'atime_ns': st.st_atime_ns,
            'xattrs': {name: base64.b64encode(os.getxattr(fd, name)).decode()
                       for name in os.listxattr(fd)}}


def snapshot(path):
    try:
        with regular_file(path) as source:
            return _file_snapshot(source)
    except FileNotFoundError:
        return None


def same(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    return all(actual[key] == expected[key]
               for key in ('sha256', 'mode', 'uid', 'gid', 'mtime_ns', 'xattrs'))


def _snapshot_at(directory, name):
    """Snapshot the destination in the same pinned directory used by rename."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'Not a regular destination: {name}')
        return _file_snapshot(source)


class Manager:
    def __init__(self, home, settings_dir, *, sysfs='/sys/class/drm', proc='/proc'):
        home = Path(home).absolute()
        self.home = home.resolve()
        settings = Path(settings_dir).absolute()
        # Only the supplied home is a trusted canonical alias. Never resolve
        # mutable settings components into an unrelated writable boundary.
        if settings.is_relative_to(home):
            settings = self.home/settings.relative_to(home)
        self.root = settings / 'xess-xmx'
        self.sysfs = Path(sysfs)
        self.proc = Path(proc)

    def _appid(self, appid):
        if not isinstance(appid, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', appid):
            raise ValueError('App ID must be a canonical positive decimal string')
        return appid

    def _app(self, appid):
        return self.root/'apps'/self._appid(appid)

    def _no_links(self, path, anchor):
        path, anchor = Path(path), Path(anchor)
        if not path.is_relative_to(anchor):
            raise ValueError(f'Path escapes its allowed root: {path}')
        current = anchor
        for part in path.relative_to(anchor).parts:
            current = current/part
            if current.is_symlink():
                raise ValueError(f'Symlink in mutable path refused: {current}')

    @contextmanager
    def _dirfd(self, path, create=False):
        """Pin each directory with O_NOFOLLOW; no check/open symlink window."""
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Absolute canonical directory required')
        user = self.home.stat()
        groups = self._user_groups()
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def accessible(directory, write=False):
            needed = 1 | (2 if write else 0)
            permissions = self._access_permissions(directory, os.fstat(directory), user.st_uid, groups)
            if not any(bits & needed == needed for bits in permissions):
                raise ValueError(f'Directory is not accessible by target user {user.st_uid}: {path}')

        try:
            for part in path.parts[1:]:
                accessible(fd)
                made = False
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    # Check the pinned nearest existing parent before UID 0
                    # could create anything the target user cannot create.
                    accessible(fd, write=True)
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    made = True
                if made:
                    os.fchown(child, user.st_uid, user.st_gid)
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                    os.fsync(fd)
                os.close(fd)
                fd = child
            accessible(fd, write=create)
            yield fd
        finally:
            os.close(fd)

    def _mkdir(self, path):
        with self._dirfd(path, create=True):
            pass

    def _atomic(self, path, data, mode=0o600, metadata=None, temporary=None, before_replace=None,
                expected=_UNGUARDED, owner=None):
        if owner is not None:
            self._check_owner(owner)
        path = Path(path)
        with self._dirfd(path.parent, create=True) as parent:
            tmp = Path(temporary).name if temporary else '.'+path.name+'.'+uuid.uuid4().hex
            if temporary and Path(temporary).parent != path.parent:
                raise ValueError('Temporary file must be in the target directory')
            created = False
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                             mode, dir_fd=parent)
                created = True
                with os.fdopen(fd, 'w+b') as stream:
                    os.fchown(stream.fileno(), self.home.stat().st_uid, self.home.stat().st_gid)
                    os.fchmod(stream.fileno(), mode)
                    stream.write(data)
                    stream.flush()
                    if metadata:
                        os.fchown(stream.fileno(), metadata['uid'], metadata['gid'])
                        os.fchmod(stream.fileno(), metadata['mode'])
                        attributes = metadata.get('xattrs', {})
                        for name in os.listxattr(stream.fileno()):
                            if name not in attributes:
                                os.removexattr(stream.fileno(), name)
                        for name, value in attributes.items():
                            os.setxattr(stream.fileno(), name, base64.b64decode(value))
                        os.utime(stream.fileno(), ns=(metadata['atime_ns'], metadata['mtime_ns']))
                        stream.seek(0)
                        if not same(_file_snapshot(stream), metadata):
                            raise ValueError(f'Restored staging metadata conflict: {path}')
                    os.fsync(stream.fileno())
                    if before_replace:
                        stream.seek(0)
                        before_replace(_file_snapshot(stream))
                # Final expected-state guard, after staging, fsync and callbacks.
                # This is not an atomic compare-and-swap against hostile writers.
                if owner is not None:
                    self._check_owner(owner)
                if expected is not _UNGUARDED and not same(_snapshot_at(parent, path.name), expected):
                    raise ValueError(f'Destination changed before replacement; recovery required: {path}')
                os.replace(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    if created:
                        os.unlink(tmp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _sync(self, path):
        with self._dirfd(path) as fd:
            os.fsync(fd)

    def _unlink(self, path, missing_ok=False, expected=_UNGUARDED, owner=None):
        path = Path(path)
        if path.name == 'journal.json' and expected is _UNGUARDED:
            _, expected = self._journal_state(path.parent.name)
        with self._dirfd(path.parent) as fd:
            if owner is not None:
                self._check_owner(owner)
            if expected is not _UNGUARDED and not same(_snapshot_at(fd, path.name), expected):
                raise ValueError(f'Destination changed before deletion; recovery required: {path}')
            try:
                os.unlink(path.name, dir_fd=fd)
            except FileNotFoundError:
                if not missing_ok:
                    raise
            os.fsync(fd)

    def _save(self, path, value):
        path = Path(path)
        expected = _UNGUARDED
        if path.name == 'journal.json':
            current, expected = self._journal_state(path.parent.name)
            if current is not None and current['plan_id'] != value['plan_id']:
                raise ValueError('Different pending journal; retaining it for recovery')
        self._atomic(path, json.dumps(value, sort_keys=True).encode(), expected=expected)

    @staticmethod
    def _load(path, *, missing=None) -> Any:
        try:
            with regular_file(path) as source:
                return json.load(source)
        except FileNotFoundError:
            return missing

    def _installed(self, appid) -> dict[str, Any] | None:
        record, _ = self._installed_state(appid)
        return record

    def _installed_state(self, appid) -> tuple[dict[str, Any] | None, dict | None]:
        # An existing null/empty/malformed owner is not a clean installation.
        # Never start a new backup lineage over an unrecognized prior record.
        try:
            with regular_file(self._app(appid)/'installed.json', authorize=self._fd_access) as source:
                identity = _file_snapshot(source)
                source.seek(0)
                record = json.load(source)
        except FileNotFoundError:
            return None, None
        if not isinstance(record, dict) or not record:
            raise ValueError('Unknown installed ownership record; preserve it for recovery')
        self._validate(record, appid)
        if record.get('phase') != 'enabled' or record.get('action') != 'enable':
            raise ValueError('Invalid installed ownership phase; preserve it for recovery')
        return record, identity

    @staticmethod
    def _owner_snapshot(value):
        if value is None:
            return
        keys = {'sha256', 'mode', 'uid', 'gid', 'mtime_ns', 'atime_ns', 'xattrs'}
        if (not isinstance(value, dict) or set(value) != keys or
                not isinstance(value['sha256'], str) or
                not re.fullmatch('[0-9a-f]{64}', value['sha256']) or
                any(type(value[key]) is not int or not 0 <= value[key] < (1 << 32)
                    for key in ('uid', 'gid')) or
                type(value['mode']) is not int or not 0 <= value['mode'] <= 0o7777 or
                any(type(value[key]) is not int or not -(1 << 63) <= value[key] < (1 << 63)
                    for key in ('mtime_ns', 'atime_ns')) or
                not isinstance(value['xattrs'], dict) or len(value['xattrs']) > 256):
            raise ValueError('Invalid bounded installed-owner snapshot; retaining recovery state')
        size = 0
        for name, encoded in value['xattrs'].items():
            if (not isinstance(name, str) or not 1 <= len(name.encode()) <= 255 or '\0' in name or
                    not isinstance(encoded, str) or len(encoded) > 87384):
                raise ValueError('Invalid installed-owner xattrs; retaining recovery state')
            size += len(name.encode())+len(base64.b64decode(encoded, validate=True))
        if size > 65536:
            raise ValueError('Oversized installed-owner xattrs; retaining recovery state')

    def _check_owner(self, record):
        if 'installed_owner' not in record:
            raise ValueError('Unbound installed ownership; preserve recovery state and replan')
        self._owner_snapshot(record['installed_owner'])
        if (record['installed_owner'] is None) != (record['action'] == 'enable'):
            raise ValueError('Invalid action-bound installed ownership; retaining recovery state')
        if 'owner_commit' in record:
            self._owner_snapshot(record['owner_commit'])
            if (record['owner_commit'] is None or record['action'] == 'disable' or
                    record['phase'] not in ('pending_'+record['action'], 'restoring')):
                raise ValueError('Invalid staged installed-owner commit; retaining recovery state')
        if 'owner_removed' in record and (record['owner_removed'] is not True or
                record['phase'] != 'restoring' or record['action'] not in ('enable', 'disable')):
            raise ValueError('Invalid staged installed-owner removal; retaining recovery state')
        _, current = self._installed_state(record['appid'])
        if not (same(current, record['installed_owner']) or
                record.get('owner_commit') is not None and same(current, record['owner_commit']) or
                record.get('owner_removed') is True and current is None):
            raise ValueError('Installed ownership changed; preserve it and recovery state')
        return current

    def _save_owner(self, journal, value):
        expected = self._check_owner(journal)
        if journal.get('owner_commit') is not None and same(expected, journal['owner_commit']):
            return
        # Installed records keep no growing transaction/owner history. The
        # pending journal alone carries bounded before/commit snapshots.
        value = {key: item for key, item in value.items()
                 if key not in ('installed_owner', 'owner_commit', 'owner_removed')}
        def prepared(state):
            journal['owner_commit'] = state
            self._save(self._app(journal['appid'])/'journal.json', journal)
        self._atomic(self._app(journal['appid'])/'installed.json',
                     json.dumps(value, sort_keys=True).encode(), expected=expected,
                     before_replace=prepared, owner=journal)

    def _remove_owner(self, journal):
        expected = self._check_owner(journal)
        journal['owner_removed'] = True
        self._save(self._app(journal['appid'])/'journal.json', journal)
        self._unlink(self._app(journal['appid'])/'installed.json', missing_ok=True, expected=expected)

    def _owner_backup(self, journal):
        return self._app(journal['appid'])/'backups'/journal['plan_id']/'owner'

    def _backup_owner(self, journal):
        # Preserve the exact previous serialization, not a newly reconstructed
        # record. Authorization, snapshot and bytes all refer to the same FD.
        self._check_owner(journal)
        with regular_file(self._app(journal['appid'])/'installed.json', authorize=self._fd_access) as source:
            state = _file_snapshot(source)
            source.seek(0)
            data = source.read()
        if (not same(state, journal['installed_owner']) or
                hashlib.sha256(data).hexdigest() != state['sha256']):
            raise ValueError('Previous installed owner changed while backing up; recovery required')
        self._atomic(self._owner_backup(journal), data, expected=None, owner=journal)

    def _restore_owner(self, journal):
        expected = self._check_owner(journal)
        original = journal['installed_owner']
        if same(expected, original):
            return  # Normally abort never needs to rewrite the prior owner.
        if (not original or original['uid'] != self.home.stat().st_uid or
                original['gid'] not in self._user_groups() or original['mode'] & ~0o777 or
                any(not name.startswith('user.') and name not in
                    ('system.posix_acl_access', 'security.selinux') for name in original['xattrs'])):
            raise ValueError('Unsafe previous installed-owner restore metadata; retaining recovery state')
        with regular_file(self._owner_backup(journal), authorize=self._fd_access) as source:
            data = source.read()
        if hashlib.sha256(data).hexdigest() != original['sha256']:
            raise ValueError('Previous installed-owner backup conflict; retaining recovery state')
        # Both destinations remain recognized across a crash before/after this
        # rollback rename: the original preview snapshot and our staged commit.
        self._atomic(self._app(journal['appid'])/'installed.json', data,
                     original['mode'], original, expected=expected, owner=journal)

    def _journal(self, appid) -> dict | None:
        record, _ = self._journal_state(appid)
        return record

    def _journal_state(self, appid) -> tuple[dict | None, dict | None]:
        # JSON null, unreadable/special files and dangling links are not absence.
        # Parse and snapshot the same pinned descriptor so the final mutation
        # guard cannot accidentally bless a different pathname replacement.
        try:
            with regular_file(self._app(appid)/'journal.json', authorize=self._fd_access) as source:
                identity = _file_snapshot(source)
                source.seek(0)
                record = json.load(source)
        except FileNotFoundError:
            return None, None
        if not isinstance(record, dict):
            raise ValueError('Unknown persisted journal; retaining it for manual recovery')
        action = record['action']
        if action == 'nuke':
            # Escape-hatch journal: outside the strict transaction schema on
            # purpose, so it stays readable when the normal journal is broken.
            if (record.get('schema') != 1 or record.get('appid') != appid or
                    record.get('phase') != 'pending_nuke' or
                    not isinstance(record.get('plan_id'), str) or
                    not re.fullmatch(r'[0-9a-f]{32}', record['plan_id']) or
                    any(not isinstance(record.get(key), str) or
                        any(char in record[key] for char in ('\0', '\r', '\n'))
                        for key in ('old_launch_options', 'desired_launch_options'))):
                raise ValueError('Invalid persisted nuke journal; retaining it for manual recovery')
            return record, identity
        self._validate(record, appid)
        phases = {'restoring', 'pending_'+action}
        if action != 'disable':
            phases.add('writing_'+action)
        if (record.get('phase') not in phases or
                any(not isinstance(record.get(key), str) or
                    not re.fullmatch(r'[0-9a-f]{32}', record[key])
                    for key in ('plan_id', 'generation')) or
                any(not isinstance(record.get(key), str) or
                    any(char in record[key] for char in ('\0', '\r', '\n'))
                    for key in ('old_launch_options', 'desired_launch_options'))):
            raise ValueError('Invalid persisted journal; retaining it for manual recovery')
        return record, identity

    @contextmanager
    def _lock(self):
        if self.home.stat().st_uid == 0:
            raise ValueError('A non-root target user home is required')
        with self._dirfd(self.root, create=True) as directory:
            fd = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                identity = os.fstat(fd)
                if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
                    raise ValueError('Lock must be a single-link regular file')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fchown(fd, self.home.stat().st_uid, self.home.stat().st_gid)
                os.fchmod(fd, 0o600)
                self._user_access(self.root, read=True, execute=True)
                yield
            finally:
                os.close(fd)

    def _scope_clear(self, scope):
        other = self.root/('dll-apps' if scope == 'prefix' else 'apps')
        self._no_links(other, self.root.parent)
        if other.exists():
            for app in other.iterdir():
                self._no_links(app, other)
                if self._load(app/'journal.json', missing=_MISSING) is not _MISSING:
                    raise ValueError('Other XeSS scope has a pending operation; recover it first')

    def _prefix(self, appid):
        uid = self.home.stat().st_uid
        if uid == 0 or self.home == Path('/'):
            raise ValueError('A non-root target user home is required')
        candidates = []
        for library in steam.library_folders(self.home):
            prefix = library/'steamapps/compatdata'/appid/'pfx'
            if prefix.is_dir():
                candidates.append(prefix)
        if len(candidates) != 1:
            raise ValueError('Exactly one existing Steam prefix is required')
        prefix = candidates[0]
        library = prefix.parents[3]
        self._no_links(prefix/'drive_c/windows/system32', library)
        for directory in (library, library/'steamapps', prefix.parent.parent, prefix.parent,
                          prefix, prefix/'drive_c', prefix/'drive_c/windows',
                          prefix/'drive_c/windows/system32'):
            if directory.stat().st_uid != uid:
                raise ValueError(f'Steam prefix/library owner is not the target user: {directory}')
        if not (prefix/'drive_c/windows/system32').is_dir():
            raise ValueError('An existing Windows system32 prefix directory is required')
        return prefix

    def _targets(self, appid, prefix):
        system = prefix/'drive_c/windows/system32'
        app = self._app(appid)
        self._no_links(app, self.root.parent)
        targets = [system/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll',
                   system/'ControlLib.dll', app/'launch.sh', app/'xess.conf', app/'intel_icd.json']
        for target in targets:
            parent = target.parent
            while not parent.exists():
                parent = parent.parent
            self._user_access(parent, execute=True)
        return targets

    @api
    def plan_xmx(self, appid, package_dir, launch_options, action='enable', profile=None):
        app = self._app(appid)
        profile_supplied = profile is not None
        profile = xmx_profiles.normalize(profile)
        if not isinstance(launch_options, str) or any(c in launch_options for c in ('\0', '\n', '\r')):
            raise ValueError('Exact single-line active-client launch options required')
        if action not in ('enable', 'disable', 'configure', 'adopt_trial'):
            raise ValueError('Unsupported action')
        with self._lock():
            self._scope_clear('prefix')
            if self._legacy(appid):
                raise ValueError('Legacy XeSS XMX install detected; no adoption or migration is supported')
            prefix = self._prefix(appid)
            self._stopped(appid, prefix)
            if self._journal(appid) is not None:
                raise ValueError('A pending operation requires recovery first')
            if action == 'disable':
                return self._plan_disable(appid, prefix, launch_options)
            previous, owner = self._installed_state(appid)
            if action in ('configure', 'adopt_trial'):
                if not previous:
                    raise ValueError('Not enabled; refusing to configure or adopt trial')
                if action == 'adopt_trial':
                    trial_profile = xmx_profiles.recognize_trial(launch_options, previous['desired_launch_options'])
                    if not profile_supplied:
                        profile = trial_profile
                elif launch_options != previous['desired_launch_options']:
                    raise ValueError('Not enabled or launch options conflict; refusing to configure')
                self._validate(previous, appid)
                self._owned(previous['operations'])
            elif previous:
                raise ValueError('Already enabled; disable first')
            if not self._native_xess(appid)['found']:
                raise ValueError('No native game XeSS DLL found; translation/OptiScaler installation is outside v1')
            if not self._gpu()['xmx_capable']:
                raise ValueError('No GPU with v1-validated XMX device capability (Intel 0xb080)')
            diagnostics = self._package(package_dir)
            if not diagnostics['valid']:
                raise ValueError('; '.join(diagnostics['errors']))
            package = Path(diagnostics['path'])
            targets = self._targets(appid, prefix)
            wrapper = ('#!/bin/sh\nexport XMX_CONF='+shlex.quote(str(targets[3]))+
                       '\nexec '+shlex.quote(str(package/'xmx-launch.sh'))+' "$@"\n')
            config, driver = xmx_profiles.render(self, package, targets, profile, diagnostics)
            # User-maintained per-game exports (e.g. WINEDLLOVERRIDES for the
            # FSR4-XMX plugin) survive every Save; invalid lines fail the plan.
            config = config + xmx_profiles.extra(Path(targets[3]).parent / xmx_profiles.EXTRA_NAME)
            icd = json.dumps({'file_format_version': '1.0.1', 'ICD': {
                'api_version': '1.4.348', 'library_arch': '64',
                'library_path': str(driver)}})
            source_hash = self._source_hash(package)
            data = [self._source_bytes(package, 'igdext64.dll'),
                    self._source_bytes(package, 'ControlLib.dll'),
                    wrapper.encode(), config.encode(), icd.encode()]
            if self._source_hash(package) != source_hash:
                raise ValueError('Package source changed while preparing the plan')
            operations = [{'target': str(path), 'old': snapshot(path),
                           'data': base64.b64encode(content).decode(),
                           'installed_sha256': hashlib.sha256(content).hexdigest(),
                           'mode': 0o755 if i == 2 else 0o644}
                          for i, (path, content) in enumerate(zip(targets, data))]
            token = uuid.uuid4().hex
            wrapped = shlex.quote(str(targets[2]))+' %command%'
            if launch_options.count('%command%') > 1:
                raise ValueError('Ambiguous launch options: multiple %command% placeholders')
            position = launch_options.find('%command%')
            quote, escaped, word_start = '', False, True
            for i, char in enumerate(launch_options):
                if char == '`':
                    raise ValueError('Backticks are not supported in v1 launch options')
                if i == position and (quote or escaped or not word_start):
                    raise ValueError('A standalone, unquoted %command% placeholder is required')
                if escaped:
                    escaped = False
                elif char == '\\' and quote != "'":
                    escaped = True
                elif char == '$' and quote != "'" and launch_options[i+1:i+2] in ('(', '{'):
                    raise ValueError('Shell substitutions are not supported in v1 launch options')
                elif quote:
                    if char == quote:
                        quote = ''
                elif char in ("'", '"'):
                    quote = char
                elif char in ';|&<>()':
                    raise ValueError('Shell control operators are not supported in v1 launch options')
                elif char == '#' and word_start:
                    raise ValueError('Shell comments are not supported in v1 launch options')
                elif char in ' \t':
                    word_start = True
                    continue
                word_start = False
            if quote or escaped:
                raise ValueError('Unbalanced or trailing shell quoting is not supported')
            if position >= 0:
                end = position+len('%command%')
                if end < len(launch_options) and launch_options[end] not in ' \t':
                    raise ValueError('A standalone, unquoted %command% placeholder is required')
            desired = (launch_options.replace('%command%', wrapped) if '%command%' in launch_options
                       else wrapped+(' '+launch_options if launch_options else ''))
            # The live field may already be exactly our wrapper (per-game state
            # cleared while Steam kept the options): reuse it — wrapping a
            # wrapper is corruption, never a valid install.
            if launch_options == wrapped:
                desired = wrapped
            plan = {'schema': 1, 'appid': appid, 'plan_id': token, 'phase': 'planned',
                    'action': action, 'prefix': str(prefix), 'package_dir': str(package),
                    'source_hash': source_hash, 'generation': self._generation(app), 'profile': profile,
                    'old_launch_options': launch_options, 'desired_launch_options': desired,
                    'operations': operations, 'installed_owner': owner}
            if previous:
                plan.update(previous=previous, desired_launch_options=previous['desired_launch_options'])
            self._validate(plan, appid)
            self._save(app/'plans'/(token+'.json'), plan)
            return {'ok': True, **{key: plan[key] for key in (
                'plan_id', 'old_launch_options', 'desired_launch_options')},
                'profile': dict(profile), 'selected_driver': str(driver),
                'operations': [{'target': item['target'], 'operation': 'install',
                                'before_sha256': item['old']['sha256'] if item['old'] else None,
                                'after_sha256': item['installed_sha256']}
                               for item in operations]}

    def _plan_disable(self, appid, prefix, launch_options):
        app = self._app(appid)
        installed, owner = self._installed_state(appid)
        if not installed or launch_options != installed['desired_launch_options']:
            raise ValueError('Not enabled or launch options conflict; refusing to restore')
        self._validate(installed, appid)
        self._owned(installed['operations'])
        plan = dict(installed, generation=self._generation(app), action='disable',
                    installed_owner=owner,
                    phase='planned', plan_id=uuid.uuid4().hex,
                    old_launch_options=launch_options,
                    desired_launch_options=installed['old_launch_options'],
                    checks={item['target']: snapshot(item['target'])
                            for item in installed['operations']})
        self._save(app/'plans'/(plan['plan_id']+'.json'), plan)
        return {'ok': True, **{key: plan[key] for key in (
            'plan_id', 'old_launch_options', 'desired_launch_options')},
            'operations': [{'target': item['target'], 'operation': 'restore'}
                           for item in plan['operations']]}

    def _matches_owned(self, item, current):
        user = self.home.stat()
        if (not current or current['sha256'] != item['installed_sha256'] or
                current['mode'] != item['mode'] or current['uid'] != user.st_uid or
                current['gid'] != user.st_gid):
            return False
        # Every managed replacement is durably snapshotted before rename.
        # Matching bytes alone cannot prove that an unstarted target is ours.
        return bool(item.get('installed')) and same(current, item['installed'])

    def _owned(self, operations):
        for item in operations:
            current = snapshot(item['target'])
            if not self._matches_owned(item, current):
                raise ValueError(f'Owned file conflict: {item["target"]}')
            if item.get('backup') and (not Path(item['backup']).is_file() or
                                      digest(item['backup']) != item['old']['sha256']):
                raise ValueError(f'Backup conflict: {item["backup"]}')

    def _clean_temps(self, journal):
        candidates = []
        for item in journal['operations']:
            if not item.get('temporary'):
                continue
            temporary = Path(item['temporary'])
            current = snapshot(temporary)
            if current is None:
                continue
            # Exclusive creation can fail on an unrelated empty/partial (or
            # byte-identical) file. Only complete durable snapshots prove ours.
            if not (item.get('installed') and same(current, item['installed']) or
                    journal['phase'] == 'restoring' and same(current, item['old'])):
                raise ValueError(f'Staging file conflict; retaining it: {temporary}')
            candidates.append((temporary, current))
        for temporary, checked in candidates:
            self._unlink(temporary, expected=checked, owner=journal)

    def _restore(self, journal):
        self._check_owner(journal)
        self._clean_temps(journal)
        needs_restore = []
        # Infer progress from hashes, not a post-write flag that a crash can lose.
        # Preflight every file/backup before restoring even one of them.
        for item in journal['operations']:
            current = snapshot(item['target'])
            if same(current, item['old']):
                continue
            if not self._matches_owned(item, current):
                raise ValueError(f'Recovery conflict: {item["target"]}')
            if item['old'] and (not item.get('backup') or
                                not Path(item['backup']).is_file() or
                                digest(item['backup']) != item['old']['sha256']):
                raise ValueError(f'Backup conflict: {item.get("backup")}')
            needs_restore.append((item, current))
        journal['phase'] = 'restoring'
        app = self._app(journal['appid'])
        self._save(app/'journal.json', journal)
        for item, checked in reversed(needs_restore):
            target = Path(item['target'])
            if not same(snapshot(target), checked):
                raise ValueError(f'Recovery conflict during restore: {target}')
            if item['old']:
                original = Path(item['backup']).read_bytes()
                if hashlib.sha256(original).hexdigest() != item['old']['sha256']:
                    raise ValueError(f'Backup conflict during restore: {item["backup"]}')
                self._atomic(target, original, item['old']['mode'], item['old'],
                             temporary=item.get('temporary'), expected=checked, owner=journal)
            else:
                self._unlink(target, expected=checked, owner=journal)
        for directory in sorted(journal.get('created_dirs', []), key=len, reverse=True):
            path = Path(directory)
            if path.is_dir() and not any(path.iterdir()):
                with self._dirfd(path.parent) as fd:
                    self._check_owner(journal)
                    os.rmdir(path.name, dir_fd=fd)
                    os.fsync(fd)

    @staticmethod
    def _source_bytes(package, relative):
        resolved = (package/relative).resolve(strict=True)
        if not resolved.is_relative_to(package):
            raise ValueError(f'Package source escapes package: {relative}')
        with regular_file(resolved) as source:
            return source.read()

    @staticmethod
    def _source_hash(package, *, hash_files=True):
        records = []
        for path in sorted(package.rglob('*')):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(package):
                raise ValueError(f'Package source escapes package: {path}')
            st = path.lstat()
            if not (path.is_symlink() or stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise ValueError(f'Special package source refused: {path}')
            records.append([str(path.relative_to(package)), st.st_mode, st.st_uid, st.st_gid,
                            os.readlink(path) if path.is_symlink() else
                            digest(path) if hash_files and path.is_file() else 'directory'])
        return hashlib.sha256(json.dumps(records).encode()).hexdigest()

    def _user_groups(self):
        user = self.home.stat()
        try:
            return os.getgrouplist(pwd.getpwuid(user.st_uid).pw_name, user.st_gid)
        except KeyError:
            return [user.st_gid]

    @staticmethod
    def _access_permissions(path, st, uid, groups):
        if st.st_uid == uid:
            return [(st.st_mode >> 6) & 7]
        try:
            acl = os.getxattr(path, 'system.posix_acl_access')
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            return [(st.st_mode >> (3 if st.st_gid in groups else 0)) & 7]
        if len(acl) < 4 or (len(acl)-4) % 8 or struct.unpack_from('<I', acl)[0] != 2:
            raise ValueError(f'Unsupported access ACL: {path}')
        entries = list(struct.iter_unpack('<HHI', acl[4:]))
        if any(tag not in (1, 2, 4, 8, 16, 32) or perms & ~7 for tag, perms, _ in entries):
            raise ValueError(f'Unsupported access ACL entry: {path}')
        mask = next((perms for tag, perms, _ in entries if tag == 16), 7)
        named = [perms & mask for tag, perms, ident in entries if tag == 2 and ident == uid]
        if named:
            return named
        matched = [perms & mask for tag, perms, ident in entries
                   if (tag == 4 and st.st_gid in groups) or (tag == 8 and ident in groups)]
        if matched:
            return matched
        other = [perms for tag, perms, _ in entries if tag == 32]
        if not other:
            raise ValueError(f'Incomplete access ACL: {path}')
        return other

    def _fd_access(self, fd, *, read=False, execute=False):
        """Authorize inode metadata/ACLs, never a path that can name a new inode."""
        user = self.home.stat()
        permissions = self._access_permissions(fd, os.fstat(fd), user.st_uid, self._user_groups())
        needed = (4 if read else 0) | (1 if execute else 0)
        if not any(bits & needed == needed for bits in permissions):
            raise ValueError(f'Open file/directory is not accessible by target user {user.st_uid}')

    def _user_access(self, path, *, read=False, execute=False, write=False):
        path = Path(path).resolve(strict=True)
        user = self.home.stat()
        groups = self._user_groups()
        current = path
        while True:
            st = current.stat()
            permissions = self._access_permissions(current, st, user.st_uid, groups)
            needed = (4 if read and current == path else 0) | (2 if write and current == path else 0)
            needed |= 1 if current.is_dir() or (execute and current == path) else 0
            if not any(bits & needed == needed for bits in permissions):
                raise ValueError(f'Path is not accessible by target user {user.st_uid}: {current}')
            if current == current.parent:
                break
            current = current.parent

    def _package(self, package_dir):
        result = {'path': str(package_dir), 'valid': False, 'errors': [],
                  'runtime_verified': False, 'compiler_validated': False}
        try:
            package = Path(package_dir).resolve(strict=True)
            result['path'] = str(package)
            if not package.is_dir():
                raise ValueError('Select an existing extracted package directory')

            def file(relative, kind=None, executable=False):
                path = package/relative
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(package) or not resolved.is_file():
                    raise ValueError(f'Package file escapes package or is not regular: {relative}')
                if resolved.stat().st_size == 0:
                    raise ValueError(f'Empty package file: {relative}')
                self._user_access(resolved, read=True, execute=executable)
                with regular_file(resolved) as source:
                    header = source.read(4096)
                if kind == 'pe':
                    offset = struct.unpack_from('<I', header, 0x3c)[0] if len(header) >= 64 else 0
                    if (header[:2] != b'MZ' or offset < 64 or offset+26 > len(header) or
                            header[offset:offset+4] != b'PE\0\0' or
                            struct.unpack_from('<H', header, offset+4)[0] != 0x8664 or
                            struct.unpack_from('<H', header, offset+24)[0] != 0x20b or
                            not struct.unpack_from('<H', header, offset+22)[0] & 0x2000):
                        raise ValueError(f'Expected a PE64 x86-64 DLL: {relative}')
                if kind == 'elf':
                    if (len(header) < 64 or header[:7] != b'\x7fELF\x02\x01\x01' or
                            struct.unpack_from('<H', header, 18)[0] != 62 or
                            struct.unpack_from('<H', header, 16)[0] not in (2, 3)):
                        raise ValueError(f'Expected an ELF64 x86-64 binary: {relative}')
                return resolved

            file('igdext64.dll', 'pe')
            file('ControlLib.dll', 'pe')
            dispatcher = file('lib/libvulkan_intel.so', 'elf')
            active = package/'lib/active'
            if active.is_dir():
                selected = active/'libvulkan_intel.so'
            else:
                selection = file('lib/active').read_text().strip()
                if not re.fullmatch(r'[a-zA-Z0-9_.-]+', selection) or selection in ('.', '..'):
                    raise ValueError('Invalid active Mesa driver selection')
                selected = package/'lib'/('mesa-'+selection)/'libvulkan_intel.so'
            driver = file(selected.relative_to(package), 'elf')
            if driver.samefile(dispatcher):
                raise ValueError('Dispatcher is not a real selected Mesa driver')
            result['selected_driver'] = str(driver)
            file('xmx-launch.sh', executable=True)
            file('tools/cm-compile.sh', executable=True)
            file('tools/cmk_pack.py')
            for relative in ('kernels', 'lib/bundled'):
                directory = (package/relative).resolve(strict=True)
                if not directory.is_relative_to(package) or not directory.is_dir():
                    raise ValueError(f'Missing package directory: {relative}')
                self._user_access(directory, read=True, write=relative == 'kernels')
            for glob in ('igc/igc/local/lib/libigc.so*', 'igc/igc/local/lib/libiga64.so*',
                         'igc/neo/lib/x86_64-linux-gnu/libocloc.so*', 'igc/neo/bin/ocloc*'):
                matches = sorted(package.glob(glob))
                if not matches:
                    raise ValueError(f'Missing compiler dependency: {glob}')
                for path in matches:
                    file(path.relative_to(package), 'elf', '/bin/' in str(path))
            self._source_hash(package, hash_files=False)
            result['compiler_validated'] = True
            result['valid'] = True
        except (OSError, ValueError, struct.error) as exc:
            result['errors'].append(str(exc))
        return result

    def _generation(self, app):
        generation = self._load(app/'generation.json')
        if generation is None:
            generation = uuid.uuid4().hex
            self._save(app/'generation.json', generation)
        return generation

    def _validate(self, record, appid):
        app = self._app(appid)
        prefix = self._prefix(appid)
        if (record.get('schema') != 1 or record.get('appid') != appid or
                record.get('prefix') != str(prefix) or record.get('action') not in
                ('enable', 'disable', 'configure', 'adopt_trial')):
            raise ValueError('Invalid or relocated persisted transaction')
        xmx_profiles.normalize(record.get('profile'))
        if record['action'] in ('configure', 'adopt_trial'):
            previous = record.get('previous')
            if not previous or previous.get('action') != 'enable' or previous.get('previous'):
                raise ValueError('Missing configure rollback ownership')
            self._validate(previous, appid)
        targets = self._targets(appid, prefix)
        if [item['target'] for item in record['operations']] != [str(p) for p in targets]:
            raise ValueError('Persisted transaction targets are not allowlisted')
        allowed_dirs = set()
        for target in targets:
            self._no_links(target, Path(target.anchor))
            parent = target.parent
            anchor = prefix if target.is_relative_to(prefix) else app
            while parent.is_relative_to(anchor):
                allowed_dirs.add(str(parent))
                if parent == anchor:
                    break
                parent = parent.parent
        if any(path not in allowed_dirs for path in record.get('created_dirs', [])):
            raise ValueError('Persisted transaction directory is not allowlisted')
        for i, item in enumerate(record['operations']):
            data = base64.b64decode(item['data'], validate=True)
            if hashlib.sha256(data).hexdigest() != item['installed_sha256']:
                raise ValueError('Persisted transaction payload hash mismatch')
            if item['mode'] != (0o755 if i == 2 else 0o644):
                raise ValueError('Persisted transaction permissions mismatch')
            if item['old']:
                original = item['old']
                if original['uid'] != self.home.stat().st_uid:
                    raise ValueError('Refusing to restore a file not owned by the target user')
                if (original['gid'] not in self._user_groups() or original['mode'] & ~0o777 or
                        any(not name.startswith('user.') and name not in
                            ('system.posix_acl_access', 'security.selinux')
                            for name in original.get('xattrs', {}))):
                    raise ValueError('Privileged restore metadata is not supported')
            if item.get('temporary'):
                temporary = Path(item['temporary'])
                if (temporary.parent != Path(item['target']).parent or
                        not re.fullmatch(r'\.decky-xmx-[0-9a-f]{32}-'+str(i)+r'\.tmp', temporary.name)):
                    raise ValueError('Persisted staging path is not allowlisted')
                self._no_links(temporary, Path(temporary.anchor))
            if item.get('backup'):
                backup = Path(item['backup'])
                relative = backup.relative_to(app/'backups')
                if (len(relative.parts) != 2 or relative.name != str(i) or
                        not re.fullmatch(r'[0-9a-f]{32}', relative.parts[0])):
                    raise ValueError('Persisted backup path is not allowlisted')
                self._no_links(backup, app)
        return record

    def _record(self, appid, token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
            raise ValueError('Invalid plan token')
        record = self._load(self._app(appid)/'plans'/(token+'.json'))
        if not record or record['appid'] != appid or record['plan_id'] != token:
            raise ValueError('Unknown or wrong-app plan token')
        return self._validate(record, appid)

    @staticmethod
    def _pending(record):
        return {key: record[key] for key in ('plan_id', 'action', 'old_launch_options',
                                            'desired_launch_options', 'phase')}

    @api
    def apply_xmx(self, appid, plan_id):
        with self._lock():
            self._scope_clear('prefix')
            app = self._app(appid)
            plan = self._record(appid, plan_id)
            self._stopped(appid, Path(plan['prefix']))
            if self._legacy(appid):
                raise ValueError('Legacy XeSS XMX install detected')
            if plan['phase'] != 'planned' or self._journal(appid) is not None:
                raise ValueError('Plan already consumed or another operation is pending')
            if self._generation(app) != plan['generation']:
                raise ValueError('Stale plan: installation generation changed')
            self._check_owner(plan)
            if plan['action'] != 'disable' and not self._gpu()['xmx_capable']:
                raise ValueError('GPU capability changed since planning')
            if plan['action'] != 'disable':
                diagnostics = self._package(plan['package_dir'])
                if not diagnostics['valid']:
                    raise ValueError('; '.join(diagnostics['errors']))
                if self._source_hash(Path(plan['package_dir'])) != plan['source_hash']:
                    raise ValueError('Stale plan: package source changed')
            for item in plan['operations']:
                expected = plan.get('checks', {}).get(item['target'], item['old'])
                if not same(snapshot(item['target']), expected):
                    raise ValueError('Stale plan: target file changed')
            self._check_owner(plan)
            self._save(app/'generation.json', uuid.uuid4().hex)
            plan['phase'] = 'consumed'
            self._save(app/'plans'/(plan_id+'.json'), plan)
            if plan['action'] == 'disable':
                self._owned(plan['operations'])
                plan['phase'] = 'pending_disable'
                self._save(app/'journal.json', plan)
                return {'ok': True, 'pending_launch': True, **self._pending(plan)}
            plan['phase'] = 'writing_'+plan['action']
            plan['created_dirs'] = []
            for i, item in enumerate(plan['operations']):
                item['backup'] = str(app/'backups'/plan_id/str(i)) if item['old'] else None
                item['temporary'] = str(Path(item['target']).parent/('.decky-xmx-'+plan_id+'-'+str(i)+'.tmp'))
                parent = Path(item['target']).parent
                while not parent.exists():
                    if str(parent) not in plan['created_dirs']:
                        plan['created_dirs'].append(str(parent))
                    parent = parent.parent
            self._save(app/'journal.json', plan)
            if plan['action'] in ('configure', 'adopt_trial'):
                self._backup_owner(plan)
            for item in plan['operations']:
                if not same(snapshot(item['target']), item['old']):
                    raise ValueError('Target changed during apply; recovery required')
                if item['backup']:
                    original = Path(item['target']).read_bytes()
                    if hashlib.sha256(original).hexdigest() != item['old']['sha256']:
                        raise ValueError('Target changed while making backup; recovery required')
                    self._atomic(item['backup'], original, expected=None, owner=plan)
                def prepared(installed):
                    # Persist exact replacement metadata (including inherited
                    # ACL/SELinux xattrs) before the rename can become visible.
                    item['installed'] = installed
                    self._save(app/'journal.json', plan)
                self._atomic(item['target'], base64.b64decode(item['data']), item['mode'],
                             temporary=item['temporary'], before_replace=prepared, expected=item['old'], owner=plan)
            self._validate(plan, appid)
            self._owned(plan['operations'])
            plan['phase'] = 'pending_'+plan['action']
            self._check_owner(plan)
            self._save(app/'journal.json', plan)
            return {'ok': True, 'pending_launch': True, **self._pending(plan)}

    @api
    def finish_xmx(self, appid, plan_id, launch_options):
        with self._lock():
            app = self._app(appid)
            record = self._record(appid, plan_id)
            journal = self._journal(appid)
            if not journal or journal['plan_id'] != record['plan_id']:
                raise ValueError('No matching pending operation')
            self._validate(journal, appid)
            self._stopped(appid, Path(journal['prefix']))
            if self._legacy(appid):
                raise ValueError('Legacy XeSS XMX install detected; pending state retained')
            if launch_options != journal['desired_launch_options']:
                raise ValueError('Steam launch options do not exactly match the expected value')
            self._check_owner(journal)
            if journal['action'] == 'disable':
                self._restore(journal)
                self._remove_owner(journal)
                self._unlink(app/'journal.json', owner=journal)
                self._sync(app)
                return {'ok': True, 'installed': False, 'phase': 'disabled', 'pending_launch': False}
            if journal['phase'] != 'pending_'+journal['action']:
                raise ValueError('Incomplete file operation requires recovery')
            diagnostics = self._package(journal['package_dir'])
            if not diagnostics['valid']:
                raise ValueError('; '.join(diagnostics['errors']))
            if self._source_hash(Path(journal['package_dir'])) != journal['source_hash']:
                raise ValueError('Package source changed while awaiting client confirmation')
            self._owned(journal['operations'])
            committed = dict(journal, phase='enabled', action='enable')
            if journal['action'] in ('configure', 'adopt_trial'):
                previous = journal['previous']
                committed.pop('previous')
                committed['old_launch_options'] = previous['old_launch_options']
                committed['created_dirs'] = previous.get('created_dirs', [])
                committed['operations'] = [dict(item, old=original['old'], backup=original.get('backup'))
                                           for item, original in zip(journal['operations'], previous['operations'])]
            self._save_owner(journal, committed)
            self._unlink(app/'journal.json', owner=journal)
            self._sync(app)
            return {'ok': True, 'installed': True, 'phase': 'enabled', 'pending_launch': False}

    # ---- Emergency nuke: escape hatch that works in corrupted states ------

    def _nuke_json(self, path):
        # Returns (record|None, corrupt). Unreadable state is the reason this
        # hatch exists; it must never raise here.
        try:
            record = json.loads(Path(path).read_text())
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError):
            return None, True
        return (record, False) if isinstance(record, dict) else (None, True)

    def _nuke_allowlist(self, package_dir):
        shas = set()
        if not package_dir:
            return shas
        try:
            diagnostics = self._package(package_dir)
            if not diagnostics.get('valid'):
                return shas
            package = Path(diagnostics['path'])
            for name in ('igdext64.dll', 'ControlLib.dll'):
                shas.add(hashlib.sha256(self._source_bytes(package, name)).hexdigest())
        except Exception:
            return set()
        return shas

    def _nuke_classify_journal(self, record, allow, warnings):
        files = []
        for item in record.get('operations', []):
            try:
                target = Path(str(item.get('target', '')))
            except TypeError:
                continue
            if not target.exists():
                continue
            try:
                current = digest(target)
            except Exception:
                files.append({'target': str(target), 'action': 'leave',
                              'reason': 'not a regular readable file'})
                warnings.append(f'Cannot verify {target}; left in place.')
                continue
            old, backup = item.get('old'), item.get('backup')
            if isinstance(old, dict) and backup:
                try:
                    if digest(Path(backup)) == old.get('sha256'):
                        files.append({'target': str(target), 'action': 'restore',
                                      'backup': str(backup), 'mode': old.get('mode', 0o644) & 0o777})
                        continue
                except Exception:
                    pass
                warnings.append(f'Backup for {target} is missing or mismatched; left in place.')
                files.append({'target': str(target), 'action': 'leave', 'reason': 'backup mismatch'})
            elif current == item.get('installed_sha256') or current in allow:
                files.append({'target': str(target), 'action': 'remove'})
            else:
                warnings.append(f'{target} no longer matches our installed bytes; left in place.')
                files.append({'target': str(target), 'action': 'leave', 'reason': 'content changed'})
        return files

    def _nuke_disk_scan(self, appid, prefix, allow, warnings):
        files = []
        for target in self._targets(appid, prefix):
            if not target.exists():
                continue
            entry = {'target': str(target), 'action': 'leave', 'reason': 'not provably ours'}
            try:
                if target.name in ('igdext64.dll', 'ControlLib.dll'):
                    if digest(target) in allow:
                        entry = {'target': str(target), 'action': 'remove'}
                    else:
                        continue  # foreign file: do not even list it
                elif target.name == 'launch.sh':
                    text = target.read_text(errors='ignore')
                    if 'XMX_CONF=' in text and 'xmx-launch.sh' in text:
                        entry = {'target': str(target), 'action': 'remove'}
                elif target.name == 'xess.conf':
                    if 'VK_DRIVER_FILES=' in target.read_text(errors='ignore'):
                        entry = {'target': str(target), 'action': 'remove'}
                elif target.name == 'intel_icd.json':
                    if 'libvulkan_intel' in target.read_text(errors='ignore'):
                        entry = {'target': str(target), 'action': 'remove'}
            except OSError:
                warnings.append(f'Cannot read {target}; left in place.')
            if entry['action'] == 'leave':
                warnings.append(f'{target} is not provably ours; left in place.')
            files.append(entry)
        return files

    def _nuke_legacy(self, appid):
        manifests = self.home/'.local/state/xess-xmx-anatase'
        attributed = []
        for path in manifests.glob('*/state.json'):
            record, corrupt = self._nuke_json(path)
            if corrupt or record is None:
                # An unparseable legacy record cannot be excluded (same rule as _legacy).
                attributed.append((path.parent, None))
                continue
            needle = '/compatdata/'+appid+'/'
            if (path.parent.name == appid or
                    str(record.get('launch', {}).get('appid', record.get('appid', ''))) == appid or
                    any(needle in str(item.get('target', '')) for item in record.get('files', []))):
                attributed.append((path.parent, record))
        return attributed

    def _nuke_classify_legacy(self, record, allow, warnings):
        files = []
        for item in record.get('files', []):
            try:
                target = Path(str(item.get('target', '')))
            except TypeError:
                continue
            if not target.exists():
                continue
            try:
                current = digest(target)
            except Exception:
                warnings.append(f'Cannot verify {target}; left in place.')
                continue
            if item.get('existed') and item.get('backup'):
                try:
                    if digest(Path(str(item['backup']))) == item.get('old_sha256'):
                        files.append({'target': str(target), 'action': 'restore',
                                      'backup': str(item['backup']), 'mode': 0o644})
                        continue
                except Exception:
                    pass
                warnings.append(f'Legacy backup for {target} is missing or mismatched; left in place.')
            elif not item.get('existed') and (current == item.get('installed_sha256') or current in allow):
                files.append({'target': str(target), 'action': 'remove'})
                continue
            warnings.append(f'{target} does not match legacy install evidence; left in place.')
        wrapper = record.get('wrapper')
        if wrapper and Path(str(wrapper)).is_file():
            files.append({'target': str(wrapper), 'action': 'remove',
                          'reason': 'inside parked legacy directory'})
        return files

    @api
    def nuke_plan_xmx(self, appid, package_dir=''):
        app = self._app(appid)
        with self._lock():
            prefix = self._prefix(appid)
            self._stopped(appid, prefix)
            warnings, files, created = [], [], []
            allow = self._nuke_allowlist(package_dir)
            owner, owner_corrupt = self._nuke_json(app/'installed.json')
            journal, journal_corrupt = self._nuke_json(app/'journal.json')
            legacy_dirs = self._nuke_legacy(appid)
            desired = None
            seen = set()

            def add(entries):
                for entry in entries:
                    if entry['target'] not in seen:
                        seen.add(entry['target'])
                        files.append(entry)

            if owner:
                add(self._nuke_classify_journal(owner, allow, warnings))
                created += [str(d) for d in owner.get('created_dirs', []) if str(d)]
                desired = owner.get('old_launch_options')
            if journal:
                add(self._nuke_classify_journal(journal, allow, warnings))
                created += [str(d) for d in journal.get('created_dirs', []) if str(d)]
                warnings.insert(0, 'A pending transaction was found; nuke supersedes it.')
                oldest = journal
                while isinstance(oldest.get('previous'), dict):
                    oldest = oldest['previous']
                desired = desired or oldest.get('old_launch_options')
            if owner_corrupt or journal_corrupt or not (owner or journal):
                warnings.insert(0, 'Install journal missing or unreadable; disk-scan mode '
                                   'removes only provably-ours files.')
                add(self._nuke_disk_scan(appid, prefix, allow, warnings))
            legacy_paths = []
            for directory, record in legacy_dirs:
                legacy_paths.append(str(directory))
                if record is None:
                    warnings.append(f'Legacy manifest {directory} is unreadable; it will be parked as-is.')
                    continue
                add(self._nuke_classify_legacy(record, allow, warnings))
                created += [str(d) for d in record.get('created_dirs', []) if str(d)]
                if desired is None:
                    desired = str(record.get('launch', {}).get('old', '') or '')
            if desired is None:
                desired = ''
            if not allow:
                warnings.append('No readable package supplied; prefix DLLs can only be '
                                'removed when the install journal proves them ours.')
            plan_id = uuid.uuid4().hex
            plan = {'schema': 1, 'appid': appid, 'plan_id': plan_id, 'action': 'nuke',
                    'phase': 'planned', 'prefix': str(prefix), 'files': files,
                    'created_dirs': created, 'legacy_dirs': legacy_paths,
                    'old_launch_options': desired, 'desired_launch_options': desired,
                    'legacy': bool(legacy_paths), 'warnings': warnings}
            self._save(app/'nuke-plans'/(plan_id+'.json'), plan)
            return {'ok': True, **plan}

    @api
    def nuke_apply_xmx(self, appid, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r'[0-9a-f]{32}', plan_id):
            raise ValueError('Invalid nuke plan token')
        with self._lock():
            app = self._app(appid)
            plan, corrupt = self._nuke_json(app/'nuke-plans'/(plan_id+'.json'))
            if corrupt or not plan or plan.get('action') != 'nuke' or plan.get('appid') != appid \
                    or plan.get('plan_id') != plan_id:
                raise ValueError('Unknown or wrong-app nuke plan token')
            if plan.get('prefix') != str(self._prefix(appid)):
                raise ValueError('Stale nuke plan: prefix path changed')
            self._stopped(appid, Path(plan['prefix']))
            backups = self.root/'nuke-backups'
            backups.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            tar_path = backups/f'nuke-{appid}-{stamp}.tar.gz'
            with tarfile.open(tar_path, 'w:gz') as archive:
                if app.is_dir():
                    archive.add(app, arcname='apps/'+appid)
                for directory in plan.get('legacy_dirs', []):
                    if Path(directory).is_dir():
                        archive.add(directory, arcname='legacy/'+Path(directory).name)
            journal = {'schema': 1, 'appid': appid, 'prefix': plan['prefix'],
                       'action': 'nuke', 'phase': 'pending_nuke', 'plan_id': plan_id,
                       'old_launch_options': plan['old_launch_options'],
                       'desired_launch_options': plan['desired_launch_options'],
                       'backup': str(tar_path)}
            try:
                self._save(app/'journal.json', journal)
            except ValueError:
                # Corrupted pending journal: it is inside the tar; overwrite directly.
                self._atomic(app/'journal.json',
                             (json.dumps(journal, sort_keys=True)+'\n').encode(), 0o600)
            removed, restored, left = [], [], []
            for entry in plan['files']:
                target = Path(entry['target'])
                try:
                    if entry['action'] == 'restore':
                        data = Path(entry['backup']).read_bytes()
                        self._atomic(target, data, entry.get('mode', 0o644))
                        restored.append(entry['target'])
                    elif entry['action'] == 'remove':
                        if target.exists():
                            self._unlink(target, missing_ok=True)
                        removed.append(entry['target'])
                    else:
                        left.append(entry['target'])
                except Exception as exc:
                    left.append(entry['target'])
                    warnings = plan.get('warnings', [])
                    warnings.append(f'Failed on {target}: {exc}')
            for directory in sorted(plan.get('created_dirs', []),
                                    key=lambda p: len(Path(p).parts), reverse=True):
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
            for directory in plan.get('legacy_dirs', []):
                path = Path(directory)
                if path.is_dir():
                    shutil.move(str(path), str(backups/('legacy-'+path.name+'-'+stamp)))
            self._save(app/'nuke-plans'/(plan_id+'.json'), dict(plan, phase='applied',
                                                                backup=str(tar_path)))
            self._sync(app)
            return {'ok': True, 'pending_launch': True, 'plan_id': plan_id,
                    'backup': str(tar_path), 'removed': removed, 'restored': restored,
                    'left': left, 'warnings': plan.get('warnings', []),
                    'desired_launch_options': plan['desired_launch_options']}

    @api
    def nuke_finish_xmx(self, appid, plan_id, launch_options):
        with self._lock():
            app = self._app(appid)
            journal = self._journal(appid)
            if not journal or journal.get('action') != 'nuke' or journal.get('plan_id') != plan_id:
                raise ValueError('No matching pending nuke operation')
            if not isinstance(launch_options, str) or launch_options != journal['desired_launch_options']:
                raise ValueError('Steam launch options do not exactly match the expected value')
            self._nuke_purge(app)
            return {'ok': True, 'installed': False, 'phase': 'disabled', 'pending_launch': False}

    def _nuke_purge(self, directory):
        directory = Path(directory)
        if not directory.is_relative_to(self.root/'apps') or not directory.is_dir():
            return
        for current, dirs, names in os.walk(directory, topdown=False):
            base = Path(current)
            for name in names:
                path = base/name
                try:
                    path.unlink()
                except OSError:
                    pass
            for name in dirs:
                try:
                    (base/name).rmdir()
                except OSError:
                    pass
        try:
            directory.rmdir()
        except OSError:
            pass

    @api
    def recover_xmx(self, appid, plan_id, launch_options, action='abort'):
        if action == 'resume':
            return self.finish_xmx(appid, plan_id, launch_options)
        if action != 'abort':
            raise ValueError('Recovery action must be abort or resume')
        with self._lock():
            app = self._app(appid)
            self._record(appid, plan_id)
            journal = self._journal(appid)
            if not journal or journal['plan_id'] != plan_id:
                raise ValueError('No matching pending operation')
            self._validate(journal, appid)
            self._stopped(appid, Path(journal['prefix']))
            if self._legacy(appid):
                raise ValueError('Legacy XeSS XMX install detected; pending state retained')
            if launch_options != journal['old_launch_options']:
                raise ValueError('Restore and confirm the original launch options before abort')
            self._check_owner(journal)
            if journal['action'] == 'disable':
                if journal['phase'] != 'pending_disable':
                    raise ValueError('Disable restoration already started; resume with desired options')
            else:
                self._restore(journal)
                if journal['action'] in ('configure', 'adopt_trial'):
                    self._restore_owner(journal)
                else:
                    self._remove_owner(journal)
            self._unlink(app/'journal.json', owner=journal)
            self._sync(app)
            installed = (app/'installed.json').exists()
            return {'ok': True, 'installed': installed,
                    'phase': 'enabled' if installed else 'disabled', 'pending_launch': False}

    def _native_xess(self, appid):
        result = {'found': False, 'files': [], 'compatibility_verified': False,
                  'reason': 'DLL presence is only a candidate, not proof of game or XMX compatibility.'}
        game = steam.find_by_appid(self.home, appid)
        if not game or game.get('source') != 'steam' or game.get('appid') != appid:
            return result
        folder = Path(game['path']).resolve()
        if not folder.is_relative_to(Path(game['library']).resolve()/'steamapps/common'):
            return result
        for directory, children, files in os.walk(folder, followlinks=False):
            path = Path(directory)
            children[:] = [name for name in children
                           if not (path/name).is_symlink() and name.casefold() not in
                           ('decky_optiscaler_backup_files', '.decky_optiscaler_backup_files',
                            'backup', 'backups')]
            for name in files:
                dll = path/name
                if name.lower() in ('libxess.dll', 'libxess_dx11.dll') and not dll.is_symlink():
                    result['files'].append(str(dll))
        result['found'] = bool(result['files'])
        return result

    def _process_identity(self, entry):
        """Read independent proc evidence; a comm/title alone is never sufficient."""
        status = dict(line.split(':', 1) for line in (entry/'status').read_text().splitlines()
                      if ':' in line)
        uid = tuple(int(value) for value in status['Uid'].split())
        if len(uid) != 4:
            raise ValueError('Incomplete process UID identity')
        command = (entry/'cmdline').read_bytes()
        if not command.endswith(b'\0'):
            raise ValueError('Incomplete process command line')
        # setproctitle leaves NUL padding after the one title argument.
        args = [os.fsdecode(arg) for arg in command.rstrip(b'\0').split(b'\0')]
        group = (entry/'cgroup').read_text().strip()
        if not group.startswith('0::/') or '\n' in group:
            raise ValueError('Unsupported process cgroup layout')
        try:
            executable = str((entry/'exe').readlink())
        except PermissionError:
            executable = None
        return {'name': status['Name'].strip(), 'ppid': str(int(status['PPid'])),
                'uids': uid, 'args': args, 'group': group[3:], 'exe': executable}

    def _user_manager(self, process):
        uid = self.home.stat().st_uid
        suffix = f'/user.slice/user-{uid}.slice/user@{uid}.service/init.scope'
        if (process['name'] != 'systemd' or process['uids'] != (uid,)*4
                or process['args'] != ['/usr/lib/systemd/systemd', '--user']
                or process['exe'] not in (None, '/usr/lib/systemd/systemd')
                or not process['group'].endswith(suffix)):
            return False
        parent = self._process_identity(self.proc/process['ppid'])
        return (parent['uids'] == (0,)*4 and parent['name'] == 'systemd'
                and parent['args'][0] == '/usr/lib/systemd/systemd'
                and '--user' not in parent['args']
                and parent['exe'] in (None, '/usr/lib/systemd/systemd')
                and parent['group'] == process['group'][:-len(suffix)]+'/init.scope')

    def _known_host_process(self, entry):
        """Narrow exceptions for independently identified undumpable host helpers."""
        try:
            process = self._process_identity(entry)
            if process['uids'] != (self.home.stat().st_uid,)*4:
                return False
            parent = self._process_identity(self.proc/process['ppid'])
            plugins = {'TabMaster': 'TabMaster', 'CSS Loader': 'SDH-CssLoader',
                       'SteamGridDB': 'decky-steamgriddb'}
            plugin = plugins.get(process['name'])
            loader = str(self.home/'homebrew/services/PluginLoader')
            if plugin:
                title = f"{process['name']} ({self.home/'homebrew/plugins'/plugin/'main.py'})"
                return (process['args'] == [title] and process['exe'] in (None, loader)
                        and process['group'] == parent['group'] == '/system.slice/plugin_loader.service'
                        and parent['uids'] == (0,)*4 and parent['name'] == 'PluginLoader'
                        and parent['exe'] in (None, loader)
                        and parent['args'][:5] == [loader, '-B', '-S', '-I', '-c']
                        and len(parent['args']) == 7 and parent['args'][-1] == loader
                        and parent['args'][5].startswith(
                            'import sys; from multiprocessing.forkserver import main; main('))
            if self._user_manager(process):
                return True
            if (process['name'] == '(sd-pam)' and process['args'] == ['(sd-pam)']
                    and process['exe'] in (None, '/usr/lib/systemd/systemd')
                    and process['group'] == parent['group']):
                if self._user_manager(parent):
                    return True
                # Anatase system-spaces login PAM child: sleep is the session leader.
                session = re.fullmatch(r'(/system\.slice/system-spaces\.slice/'
                    r'spaces@[^/]+\.service/payload)/user\.slice/user-'+
                    str(self.home.stat().st_uid)+r'\.slice/session-c[0-9]+\.scope', process['group'])
                if (session and parent['uids'] == process['uids'] and parent['name'] == 'sleep'
                        and parent['args'] == ['/usr/bin/sleep', 'infinity']
                        and parent['exe'] == '/usr/bin/sleep'):
                    init = self._process_identity(self.proc/parent['ppid'])
                    return (init['uids'] == (0,)*4 and init['name'] == 'systemd'
                            and init['args'] == ['/usr/lib/systemd/systemd']
                            and init['exe'] in (None, '/usr/lib/systemd/systemd')
                            and init['group'] == session[1]+'/init.scope')
            if process['name'] == 'gamescope-wl':
                uid = self.home.stat().st_uid
                args = process['args']
                fixed = ['--backend', 'drm', '--generate-drm-mode', 'fixed', '--xwayland-count', '2',
                         '--default-touch-mode', '4', '--hide-cursor-delay', '3000', '--max-scale', '2',
                         '--fade-out-duration', '200', '--cursor-scale-height', '720', '-e', '-R']
                if (len(args) == 24 and args[0] in ('gamescope', '/usr/bin/gamescope')
                        and args[1:19] == fixed and args[20] == '-T' and args[22] == '-O'
                        and re.fullmatch(r'/run/user/'+str(uid)+r'/gamescope\.[A-Za-z0-9]+/startup.socket', args[19])
                        and args[21] == args[19].rsplit('/', 1)[0]+'/stats.pipe'
                        and re.fullmatch(r'[A-Za-z0-9*,.-]+', args[23])):
                    pass
                else:
                    return False
                return (process['exe'] in (None, '/usr/bin/gamescope')
                        and process['group'] == parent['group'] ==
                            f'/user.slice/user-{uid}.slice/user@{uid}.service/session.slice/anatase-gamemode.service'
                        and parent['uids'] == process['uids'] and parent['name'] == 'anatase-gamemod'
                        and parent['args'] == ['/bin/bash', '/usr/libexec/anatase-gamemode']
                        and parent['exe'] == '/usr/bin/bash'
                        and self._user_manager(self._process_identity(self.proc/parent['ppid'])))
            if process['name'] == 'sshd-session':
                username = pwd.getpwuid(self.home.stat().st_uid).pw_name
                if not (len(process['args']) == 1 and re.fullmatch(
                        'sshd-session: '+re.escape(username)+r'@(?:notty|pts/[0-9]+)', process['args'][0])
                        and process['exe'] in (None, '/usr/libexec/openssh/sshd-session', '/usr/lib/ssh/sshd-session')
                        and re.fullmatch(r'/user.slice/user-'+str(self.home.stat().st_uid)+
                                         r'.slice/session-[0-9]+.scope', process['group'])
                        and parent['group'] == process['group'] and parent['uids'] == (0,)*4
                        and parent['name'] == 'sshd-session'
                        and parent['args'] == [f'sshd-session: {username} [postauth]']
                        and parent['exe'] in (None, '/usr/libexec/openssh/sshd-session', '/usr/lib/ssh/sshd-session')):
                    return False
                listener = self._process_identity(self.proc/parent['ppid'])
                return (listener['uids'] == (0,)*4 and listener['name'] == 'sshd'
                        and listener['group'] == '/system.slice/sshd.service'
                        and listener['exe'] in (None, '/usr/sbin/sshd', '/usr/bin/sshd')
                        and len(listener['args']) == 1 and re.fullmatch(
                            r'sshd: /usr/sbin/sshd -D \[listener\] [0-9]+ of [0-9]+-[0-9]+ startups',
                            listener['args'][0]))
            return False
        except (OSError, ValueError, KeyError):
            return False

    def _stopped(self, appid, prefix):
        for entry in self.proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != self.home.stat().st_uid:
                    continue
                # Decky may drop privileges and make its own environ unreadable.
                # Kernel proc-directory identity proves self, unlike a PID/title.
                if entry.samefile('/proc/self'):
                    continue
                environment = dict(field.split(b'=', 1) for field in
                                   (entry/'environ').read_bytes().split(b'\0') if b'=' in field)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                if self._known_host_process(entry):
                    continue
                raise ValueError('Cannot establish that the game is stopped: '
                                 f'process {entry.name} environment unreadable and host identity unproven') from exc
            if any(environment.get(key) == appid.encode() for key in (b'SteamAppId', b'SteamGameId')):
                raise ValueError('Game is running; stop it before changing its prefix')
            for key, expected in ((b'WINEPREFIX', prefix), (b'STEAM_COMPAT_DATA_PATH', prefix.parent)):
                value = environment.get(key)
                if value and Path(os.fsdecode(value)).resolve() == expected:
                    raise ValueError('Game prefix is running; stop it before changing files')

    def _legacy(self, appid):
        manifests = self.home/'.local/state/xess-xmx-anatase'
        for path in manifests.glob('*/state.json'):
            if path.parent.name == appid:
                return True
            try:
                record = self._load(path)
                if str(record.get('launch', {}).get('appid', record.get('appid', ''))) == appid:
                    return True
                needle = '/compatdata/'+appid+'/'
                if any(needle in str(item.get('target', '')) for item in record.get('files', [])):
                    return True
            except (OSError, ValueError, AttributeError, TypeError):
                # An unparseable legacy record is not safe evidence of no install.
                return True
        return False

    def _gpu(self):
        devices = []
        for card in sorted(self.sysfs.glob('card*')):
            if not re.fullmatch(r'card[0-9]+', card.name):
                continue
            try:
                vendor = (card/'device/vendor').read_text().strip().lower()
                device = (card/'device/device').read_text().strip().lower()
            except OSError:
                continue
            devices.append({'card': card.name, 'vendor': vendor, 'device': device,
                            'xmx_capable': vendor == '0x8086' and device == '0xb080'})
        return {'devices': devices, 'xmx_capable': any(d['xmx_capable'] for d in devices),
                'reason': 'Only Intel PCI 0xb080 is validated for this v1; other Intel devices are unverified.'}

    def _dll_manager(self):
        from .xess_dll import DllManager
        return DllManager(self)

    @api
    def get_xess_dll_status(self, appid):
        return self._dll_manager().get_xess_dll_status(appid)

    @api
    def plan_xess_dll(self, appid, target_path, launch_options, action='enable', adopt_trial=False):
        return self._dll_manager().plan_xess_dll(appid, target_path, launch_options, action, adopt_trial)

    @api
    def apply_xess_dll(self, appid, plan_id):
        return self._dll_manager().apply_xess_dll(appid, plan_id)

    @api
    def finish_xess_dll(self, appid, plan_id, launch_options):
        return self._dll_manager().finish_xess_dll(appid, plan_id, launch_options)

    @api
    def recover_xess_dll(self, appid, plan_id, launch_options, action='abort'):
        return self._dll_manager().recover_xess_dll(appid, plan_id, launch_options, action)

    @api
    def get_xmx_logs(self, appid):
        appid = self._appid(appid)
        prefix = self._prefix(appid)
        paths = [('Proton (per-app default path)', self.home/('steam-'+appid+'.log')),
                 ('IGDEXT trace (prefix; may contain earlier sessions)', prefix/'drive_c/igdext_trace.log'),
                 ('Compiler (shared; not per-game execution proof)', self.home/'.cache/xess-xmx/compile.log'),
                 ('XELL low latency (prefix; frame generation evidence)', prefix/'drive_c/igdext_xell.log')]
        logs = []
        for name, path in paths:
            entry = {'name': name, 'path': str(path), 'exists': False, 'text': '', 'truncated': False}
            try:
                with regular_file(path, authorize=self._fd_access) as source:
                    size = os.fstat(source.fileno()).st_size
                    head = source.read(min(size, 16384))
                    if size > 65536:
                        source.seek(size-49152)
                        head += b'\n[... truncated; startup above, tail below ...]\n'+source.read(49152)
                        entry['truncated'] = True
                    else:
                        head += source.read(max(0, size-len(head)))
                    entry.update(exists=True, text=head.decode('utf-8', errors='replace'))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                entry['text'] = '[Unavailable: '+str(exc)+']'
            logs.append(entry)
        return {'ok': True, 'logs': logs, 'runtime_verified': False}

    @api
    def list_xmx_apps(self):
        # The library list's "Set up" filter: app ids that carry an
        # ownership record, i.e. games this plugin has actually configured.
        # An unreadable record still counts — something is installed there
        # and the game page (status/nuke) is where it gets explained.
        found = []
        try:
            entries = list((self.root / "apps").iterdir())
        except OSError:
            return {"apps": []}
        for entry in entries:
            try:
                if entry.is_dir() and (entry / "installed.json").is_file() \
                        and re.fullmatch(r"[1-9][0-9]{0,9}", entry.name):
                    found.append(entry.name)
            except OSError:
                continue
        return {"apps": sorted(found)}

    @api
    def get_xmx_status(self, appid, package_dir=''):
        app = self._app(appid)
        # The red-button escape hatch must stay reachable when the installed
        # ownership record is unreadable JSON (disk-scan covers the files).
        # JOURNALS stay strict: atomic fsync'd writes make journal corruption
        # effectively impossible, and two long-standing contracts require an
        # unknown journal to block status ('block both scopes without being
        # consumed'). Access guards never become tolerated corruption.
        corrupt = []
        try:
            installed = self._installed(appid)
        except (OSError, ValueError) as exc:
            if ('not accessible by target user' in str(exc)
                    or not (isinstance(exc, json.JSONDecodeError)
                            or 'invalid JSON' in str(exc)
                            or 'expected a JSON object' in str(exc))):
                raise
            installed = None
            corrupt.append(f'Installed ownership record unreadable: {exc}')
        journal = self._journal(appid)
        phase = (journal['phase'] if journal else
                 'recovery_required' if corrupt else 'enabled' if installed else 'disabled')
        if journal and phase not in ('pending_enable', 'pending_disable', 'pending_configure',
                                     'pending_adopt_trial', 'pending_nuke'):
            phase = 'recovery_required'
        recorded_package = (journal or installed or {}).get('package_dir')
        package = Path(package_dir or recorded_package or self.home/'xess-xmx-linux')
        return {'ok': True, 'installed': bool(installed), 'legacy': bool(self._legacy(appid)), 'phase': phase,
                'state_corrupt': bool(corrupt),
                'corruption_reasons': corrupt,
                'runtime_verified': False, 'suggested_package': str(self.home/'xess-xmx-linux'),
                'package': self._package(package),
                'profile': xmx_profiles.normalize((installed or {}).get('profile')),
                'drivers': [{k: v for k, v in item.items() if k != 'path'} for item in
                            xmx_profiles.drivers(self, package, self._package(package))],
                'gpu': self._gpu(), 'native_xess': self._native_xess(appid),
                'flatpak': {'visible': None, 'runtime_verified': False,
                            'reason': 'Sandbox visibility has not been verified as the target user.'},
                'pending': self._pending(journal) if journal else None}
