#!/usr/bin/env python3
"""Offline native XeSS XMX tests; fixtures never execute package binaries."""
import asyncio
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_modules'))
from optiscaler import xmx

ROOT = Path(__file__).resolve().parents[1]


def pe64():
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 0xf0, 0x2000)
    struct.pack_into('<H', data, 0x98, 0x20b)
    return bytes(data)


def elf64():
    data = bytearray(128)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, 3, 62, 1)
    return bytes(data)


def retained_owner_conflict(case, plan, scope='prefix'):
    manager = case.manager
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    owner = app/'installed.json'
    prior = owner.read_bytes() if owner.exists() else None
    prior_state = xmx.snapshot(owner)
    generation = (app/'generation.json').read_bytes()
    targets = {item['target']: xmx.snapshot(item['target']) for item in plan['operations']}
    for change in ('unknown', 'metadata') if prior else ('unknown',):
        with case.subTest(change=change):
            if change == 'unknown':
                owner.write_text('{"unexpected":"owner changed after preview"}')
            else:
                os.setxattr(owner, 'user.external-owner', b'changed after preview')
            changed = xmx.snapshot(owner)
            apply = manager.apply_xmx if scope == 'prefix' else manager.apply_xess_dll
            result = apply('1817230', plan['plan_id'])
            case.assertFalse(result['ok'], result)
            case.assertEqual((app/'generation.json').read_bytes(), generation)
            case.assertFalse((app/'journal.json').exists())
            case.assertTrue(xmx.same(xmx.snapshot(owner), changed))
            for target, expected in targets.items():
                case.assertTrue(xmx.same(xmx.snapshot(target), expected))
            if prior is None:
                owner.unlink()
            else:
                manager._atomic(owner, prior, prior_state['mode'], prior_state)


def pending_owner_conflict(case, plan, scope='prefix'):
    """Both completions must leave an unexpected owner and managed files alone."""
    manager = case.manager
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    finish = manager.finish_xmx if scope == 'prefix' else manager.finish_xess_dll
    abort = manager.recover_xmx if scope == 'prefix' else manager.recover_xess_dll
    owner = app/'installed.json'
    saved = owner.read_bytes() if owner.exists() else None
    saved_state = xmx.snapshot(owner)
    targets = {item['target']: xmx.snapshot(item['target']) for item in plan['operations']}
    journal = xmx.snapshot(app/'journal.json')
    for change in ('unknown', 'metadata', 'removed') if saved else ('unknown', 'same-plan-uncommitted'):
        with case.subTest(change=change):
            if change == 'unknown':
                owner.write_text('{"unexpected":"owner appeared while pending"}')
            elif change == 'same-plan-uncommitted':
                forged = dict(manager._load(app/'journal.json'), phase='enabled', action='enable')
                owner.write_text(json.dumps(forged, sort_keys=True))
            elif change == 'metadata':
                os.setxattr(owner, 'user.external-owner', b'changed while pending')
            else:
                owner.unlink()
            changed = xmx.snapshot(owner)
            for call, options in ((finish, plan['desired_launch_options']), (abort, plan['old_launch_options'])):
                result = call('1817230', plan['plan_id'], options)
                case.assertFalse(result['ok'], result)
                case.assertTrue(xmx.same(xmx.snapshot(owner), changed))
                case.assertTrue(xmx.same(xmx.snapshot(app/'journal.json'), journal))
                for path, expected in targets.items():
                    case.assertTrue(xmx.same(xmx.snapshot(path), expected), path)
            # Only the test removes its injected owner, never the transaction.
            if saved is None:
                owner.unlink(missing_ok=True)
            else:
                manager._atomic(owner, saved, saved_state['mode'], saved_state)
    result = abort('1817230', plan['plan_id'], plan['old_launch_options'])
    case.assertTrue(result['ok'], result)


def owner_boundary_conflict(case, plan, scope='prefix', completion='finish'):
    manager = case.manager
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    owner = app/'installed.json'
    previous = owner.read_bytes() if owner.exists() else None
    previous_state = xmx.snapshot(owner)
    injected = []
    def insert():
        owner.write_text('{"unexpected":"owner changed at mutation boundary"}')
        injected.append(xmx.snapshot(owner))
    atomic, unlink = xmx.Manager._atomic, xmx.Manager._unlink
    def stage(instance, path, *args, **kwargs):
        if Path(path) == owner:
            prepared = kwargs.get('before_replace')
            def race(state):
                if prepared:
                    prepared(state)
                insert()
            kwargs['before_replace'] = race
        return atomic(instance, path, *args, **kwargs)
    def delete(instance, path, *args, **kwargs):
        if Path(path) == owner:
            insert()
        return unlink(instance, path, *args, **kwargs)
    finish = manager.finish_xmx if scope == 'prefix' else manager.finish_xess_dll
    abort = manager.recover_xmx if scope == 'prefix' else manager.recover_xess_dll
    with patch.object(xmx.Manager, '_atomic', stage), patch.object(xmx.Manager, '_unlink', delete):
        result = (finish('1817230', plan['plan_id'], plan['desired_launch_options']) if completion == 'finish'
                  else abort('1817230', plan['plan_id'], plan['old_launch_options']))
    case.assertTrue(injected, 'owner boundary was not exercised')
    case.assertFalse(result['ok'], result)
    case.assertTrue(xmx.same(xmx.snapshot(owner), injected[-1]))
    case.assertTrue((app/'journal.json').exists())
    if previous is None:
        owner.unlink()
    else:
        manager._atomic(owner, previous, previous_state['mode'], previous_state)
    result = (finish('1817230', plan['plan_id'], plan['desired_launch_options'])
              if manager._load(app/'journal.json')['action'] == 'disable'
              else abort('1817230', plan['plan_id'], plan['old_launch_options']))
    case.assertTrue(result['ok'], result)


def owner_during_managed_stage(case, scope='prefix'):
    manager = case.manager
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    plan = (manager.plan_xmx('1817230', str(case.package), '') if scope == 'prefix' else case.plan())
    apply = manager.apply_xmx if scope == 'prefix' else manager.apply_xess_dll
    target = Path(plan['operations'][0]['target'])
    before = xmx.snapshot(target)
    owner = app/'installed.json'
    atomic = xmx.Manager._atomic
    injected = []
    def stage(instance, path, *args, **kwargs):
        if Path(path) == target:
            prepared = kwargs.get('before_replace')
            def race(state):
                if prepared:
                    prepared(state)
                owner.write_text('{"unexpected":"owner appeared during managed staging"}')
                injected.append(xmx.snapshot(owner))
            kwargs['before_replace'] = race
        return atomic(instance, path, *args, **kwargs)
    with patch.object(xmx.Manager, '_atomic', stage):
        result = apply('1817230', plan['plan_id'])
    case.assertTrue(injected)
    case.assertFalse(result['ok'], result)
    case.assertTrue(xmx.same(xmx.snapshot(owner), injected[-1]))
    case.assertTrue(xmx.same(xmx.snapshot(target), before))
    case.assertTrue((app/'journal.json').exists())
    owner.unlink()
    abort = manager.recover_xmx if scope == 'prefix' else manager.recover_xess_dll
    case.assertTrue(abort('1817230', plan['plan_id'], plan['old_launch_options'])['ok'])


def owner_commit_death(case, plan, scope='prefix', recovery: str | None = 'resume'):
    manager = case.manager
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    owner = app/'installed.json'
    prior = owner.read_bytes() if owner.exists() else None
    prior_state = xmx.snapshot(owner)
    journal = manager._load(app/'journal.json')
    pid = os.fork()
    if pid == 0:
        unlink = xmx.Manager._unlink
        def die(instance, path, *args, **kwargs):
            if Path(path) == app/'journal.json':
                os._exit(73)
            return unlink(instance, path, *args, **kwargs)
        with patch.object(xmx.Manager, '_unlink', die):
            finish = manager.finish_xmx if scope == 'prefix' else manager.finish_xess_dll
            finish('1817230', plan['plan_id'], plan['desired_launch_options'])
        os._exit(74)
    _, status = os.waitpid(pid, 0)
    case.assertEqual(os.waitstatus_to_exitcode(status), 73)
    durable = xmx.snapshot(owner)
    case.manager = xmx.Manager(case.home, case.settings, sysfs=case.sysfs, proc=case.root/'proc')
    if recovery is None:
        return
    recover = case.manager.recover_xmx if scope == 'prefix' else case.manager.recover_xess_dll
    options = plan['desired_launch_options'] if recovery == 'resume' else plan['old_launch_options']
    result = recover('1817230', plan['plan_id'], options, recovery)
    case.assertTrue(result['ok'], result)
    case.assertFalse((app/'journal.json').exists())
    if recovery == 'resume':
        case.assertTrue(xmx.same(xmx.snapshot(owner), durable))
    else:
        case.assertEqual(owner.read_bytes() if owner.exists() else None, prior)
        case.assertTrue(xmx.same(xmx.snapshot(owner), prior_state))
        for item in journal['operations']:
            case.assertTrue(xmx.same(xmx.snapshot(item['target']), item['old']), item['target'])


def bounded_owner_metadata(case, scope='prefix'):
    manager = case.manager
    if scope == 'prefix':
        enabled = case.enable()
        plan = manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
    else:
        enabled = case.plan()
        case.assertTrue(manager.apply_xess_dll('1817230', enabled['plan_id'])['ok'])
        case.assertTrue(manager.finish_xess_dll('1817230', enabled['plan_id'], enabled['desired_launch_options'])['ok'])
        plan = case.plan('disable')
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    path = app/'plans'/(plan['plan_id']+'.json')
    saved = path.read_bytes()
    generation = (app/'generation.json').read_bytes()
    owner = xmx.snapshot(app/'installed.json')
    targets = {item['target']: xmx.snapshot(item['target']) for item in plan['operations']}
    for value in (True, 'timestamp', 1 << 80):
        with case.subTest(scope=scope, value=value):
            record = json.loads(saved)
            record['installed_owner']['atime_ns'] = value
            path.write_text(json.dumps(record))
            apply = manager.apply_xmx if scope == 'prefix' else manager.apply_xess_dll
            result = apply('1817230', plan['plan_id'])
            case.assertFalse(result['ok'], result)
            case.assertEqual((app/'generation.json').read_bytes(), generation)
            case.assertFalse((app/'journal.json').exists())
            case.assertTrue(xmx.same(xmx.snapshot(app/'installed.json'), owner))
            for target, expected in targets.items():
                case.assertTrue(xmx.same(xmx.snapshot(target), expected))
    path.write_bytes(saved)


def owner_during_restore_stage(case, scope='prefix', completion='abort'):
    manager = case.manager
    if scope == 'prefix':
        target = case.system32/'ControlLib.dll'
        target.write_bytes(b'preinstall shim')
        plan = manager.plan_xmx('1817230', str(case.package), '')
        apply, finish, abort = manager.apply_xmx, manager.finish_xmx, manager.recover_xmx
    else:
        target = case.target
        plan = case.plan()
        apply, finish, abort = manager.apply_xess_dll, manager.finish_xess_dll, manager.recover_xess_dll
    case.assertTrue(apply('1817230', plan['plan_id'])['ok'])
    if completion == 'finish':
        case.assertTrue(finish('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        plan = (manager.plan_xmx('1817230', '', plan['desired_launch_options'], 'disable')
                if scope == 'prefix' else case.plan('disable'))
        case.assertTrue(apply('1817230', plan['plan_id'])['ok'])
    app = manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230'
    owner = app/'installed.json'
    prior = owner.read_bytes() if owner.exists() else None
    prior_state, before = xmx.snapshot(owner), xmx.snapshot(target)
    atomic = xmx.Manager._atomic
    injected = []
    def stage(instance, path, *args, **kwargs):
        if Path(path) == target:
            prepared = kwargs.get('before_replace')
            def race(state):
                if prepared:
                    prepared(state)
                owner.write_text('{"unexpected":"owner changed during restore staging"}')
                injected.append(xmx.snapshot(owner))
            kwargs['before_replace'] = race
        return atomic(instance, path, *args, **kwargs)
    operation = finish if completion == 'finish' else abort
    options = plan['desired_launch_options'] if completion == 'finish' else plan['old_launch_options']
    with patch.object(xmx.Manager, '_atomic', stage):
        result = operation('1817230', plan['plan_id'], options)
    case.assertTrue(injected)
    case.assertFalse(result['ok'], result)
    case.assertTrue(xmx.same(xmx.snapshot(owner), injected[-1]))
    case.assertTrue(xmx.same(xmx.snapshot(target), before))
    case.assertTrue((app/'journal.json').exists())
    if prior is None:
        owner.unlink()
    else:
        manager._atomic(owner, prior, prior_state['mode'], prior_state)
    case.assertTrue(operation('1817230', plan['plan_id'], options)['ok'])


class XmxTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR', str(Path.home()/'.hermes/cache/scratch'))
        Path(scratch).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='xmx-tests-', dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/'home'
        self.home.mkdir()
        self.library = self.home/'.local/share/Steam'
        self.apps = self.library/'steamapps'
        self.prefix = self.apps/'compatdata/1817230/pfx'
        self.system32 = self.prefix/'drive_c/windows/system32'
        self.system32.mkdir(parents=True)
        self.apps.joinpath('appmanifest_1817230.acf').write_text(
            '"AppState" { "appid" "1817230" "installdir" "HiFi" }')
        self.game = self.apps/'common/HiFi'
        self.game.mkdir(parents=True)
        (self.game/'libxess.dll').write_bytes(pe64())
        self.package = self.home/'xess-xmx-linux'
        files = {
            'igdext64.dll': pe64(), 'ControlLib.dll': pe64(),
            'lib/libvulkan_intel.so': elf64(),
            'lib/mesa-test/libvulkan_intel.so': elf64(),
            'igc/igc/local/lib/libigc.so.2': elf64(),
            'igc/igc/local/lib/libigdfcl.so.2': elf64(),
            'igc/igc/local/lib/libiga64.so.2': elf64(),
            'igc/igc/local/lib/libopencl-clang2.so.15': elf64(),
            'igc/neo/lib/x86_64-linux-gnu/libocloc.so': elf64(),
            'igc/neo/bin/ocloc-test': elf64(),
            'tools/cm-compile.sh': b'#!/bin/bash\npython3 "$HERE/cmk_pack.py"\n',
            'tools/cmk_pack.py': b'# fixture packer\n',
            'xmx-launch.sh': b'#!/bin/bash\n[ -f "$XMX_CONF" ] && . "$XMX_CONF"\nexec "$@"\n',
        }
        for rel, data in files.items():
            p = self.package/rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(0o755 if rel.endswith('.sh') or '/bin/' in rel else 0o644)
        (self.package/'lib/active').symlink_to('mesa-test')
        (self.package/'lib/bundled').mkdir()
        (self.package/'kernels').mkdir()
        self.sysfs = self.root/'drm'
        card = self.sysfs/'card1/device'
        card.mkdir(parents=True)
        (card/'vendor').write_text('0x8086\n')
        (card/'device').write_text('0xb080\n')
        (self.root/'proc').mkdir()
        self.settings = self.root/'settings'
        self.manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')

    def run_root_namespace(self, body):
        """Real UID-0 backend, UID-1000 target and controlled scratch ancestors."""
        import subprocess
        import textwrap
        bootstrap = '''
import os
import struct
from pathlib import Path
from unittest.mock import patch
from optiscaler import xmx, xess_dll
root = Path(os.environ['XMX_TEST_ROOT'])
root.chmod(0o755)
os.chroot(root)
os.chdir('/')
home = Path('/home')
for directory, _, files in os.walk(home):
    os.chown(directory, 1000, 1000)
    for name in files:
        os.chown(Path(directory)/name, 1000, 1000, follow_symlinks=False)
manager = xmx.Manager(home, '/settings')
assert os.geteuid() == 0 and home.stat().st_uid == 1000
'''
        cleanup = '''
finally:
    for directory, _, files in os.walk('/'):
        os.chown(directory, 0, 0)
        for name in files:
            os.chown(Path(directory)/name, 0, 0, follow_symlinks=False)
'''
        code = bootstrap+'\ntry:\n'+textwrap.indent(textwrap.dedent(body), '    ')+cleanup
        environment = dict(os.environ, XMX_TEST_ROOT=str(self.root),
                           PYTHONPATH=str(ROOT/'py_modules'), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(['unshare', '--user', '--map-auto', '--map-root-user',
                                 sys.executable, '-c', code], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_list_xmx_apps_reports_only_real_records(self):
        apps = self.manager.root/'apps'
        apps.mkdir(parents=True, exist_ok=True)
        (apps/'3357650').mkdir()
        (apps/'3357650'/'installed.json').write_text('{}')
        (apps/'3132990').mkdir()          # configured but the record is corrupt:
        (apps/'3132990'/'installed.json').write_text('{corrupt')  # still installed
        (apps/'292030').mkdir()           # a plan left over is not a setup
        (apps/'0bogus').mkdir()
        (apps/'0bogus'/'installed.json').write_text('{}')
        self.assertEqual(self.manager.list_xmx_apps(), {'apps': ['3132990', '3357650']})

    def test_list_xmx_apps_without_any_state_is_empty(self):
        self.assertEqual(self.manager.list_xmx_apps(), {'apps': []})

    def test_logs_regular_file_swap_requires_exact_fd_authorization_in_uid0_namespace(self):
        self.run_root_namespace('''
log = home/'steam-1817230.log'
log.write_text('readable original')
os.chown(log, 1000, 1000)
log.chmod(0o644)
replacement = home/'root-only-dummy.log'
replacement.write_text('ROOT-ONLY-DUMMY-MUST-NOT-LEAK')
replacement.chmod(0o600)
assert replacement.stat().st_uid == 0
manager._user_access(log, read=True)
opening, swapped = os.open, []
def race(path, flags, *args, **kwargs):
    if Path(path).name == log.name and not swapped:
        os.replace(replacement, log)
        swapped.append(True)
    return opening(path, flags, *args, **kwargs)
with patch.object(xmx.os, 'open', side_effect=race):
    result = manager.get_xmx_logs('1817230')
assert swapped and result['ok'], result
entry = result['logs'][0]
assert not entry['exists'] and 'ROOT-ONLY-DUMMY' not in entry['text'], entry
assert 'Unavailable' in entry['text'], entry
''')

    def test_logs_directory_swap_requires_traversed_fd_authorization_in_uid0_namespace(self):
        self.run_root_namespace('''
parent = home/'.cache'
parent.mkdir()
os.chown(parent, 1000, 1000)
directory = parent/'xess-xmx'
directory.mkdir()
os.chown(directory, 1000, 1000)
(directory/'compile.log').write_text('readable original')
replacement = parent/'root-only-directory'
replacement.mkdir(mode=0o700)
(replacement/'compile.log').write_text('ROOT-DIRECTORY-DUMMY-MUST-NOT-LEAK')
(replacement/'compile.log').chmod(0o644)
manager._user_access(directory/'compile.log', read=True)
opening, swapped = os.open, []
def race(path, flags, *args, **kwargs):
    if Path(path).name == directory.name and flags & os.O_DIRECTORY and not swapped:
        directory.rename(parent/'previous-directory')
        replacement.rename(directory)
        swapped.append(True)
    return opening(path, flags, *args, **kwargs)
with patch.object(xmx.os, 'open', side_effect=race):
    result = manager.get_xmx_logs('1817230')
assert swapped and result['ok'], result
entry = result['logs'][2]
assert not entry['exists'] and 'ROOT-DIRECTORY-DUMMY' not in entry['text'], entry
assert 'Unavailable' in entry['text'], entry
''')

    def test_log_descriptor_file_and_directory_acls_match_kernel_uid1000_access(self):
        self.run_root_namespace('''
def acl(path, named_user, named_group, group, mask, other):
    entries = [(1, 7, 0xffffffff)]
    if named_user is not None:
        entries.append((2, named_user, 1000))
    entries.append((4, group, 0xffffffff))
    if named_group is not None:
        entries.append((8, named_group, 1000))
    entries.extend([(16, mask, 0xffffffff), (32, other, 0xffffffff)])
    os.setxattr(path, 'system.posix_acl_access', struct.pack('<I', 2)+b''.join(
        struct.pack('<HHI', *entry) for entry in entries))
def kernel_can_read(path):
    pid = os.fork()
    if pid == 0:
        os.setgroups([1000])
        os.setgid(1000)
        os.setuid(1000)
        try:
            with open(path, 'rb') as source:
                source.read(1)
        except PermissionError:
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    result = os.waitstatus_to_exitcode(status)
    assert result in (0, 1), result
    return result == 0
parent = home/'.cache/xess-xmx'
parent.mkdir(parents=True)
log = parent/'compile.log'
log.write_text('ACL-CONTROLLED-DUMMY')
for check_directory in (False, True):
    checked = parent if check_directory else log
    needed = 1 if check_directory else 4
    without_needed = 4 if check_directory else 1
    # Named user takes precedence over groups/other; mask limits named users
    # and matched owning/named groups; matching denied groups cannot use other.
    cases = [
        (0, 7, 7, 7, 7, 0, False),
        (needed, None, 0, needed, 0, 0, True),
        (needed, 7, 7, without_needed, 7, 0, False),
        (None, needed, 0, needed, 0, 0, True),
        (None, 0, 0, 7, 7, 0, False),
        (None, needed, 0, without_needed, 7, 0, False),
        (None, None, needed, needed, 0, 1000, True),
        (None, None, needed, without_needed, 7, 1000, False),
    ]
    for named_user, named_group, group, mask, other, gid, allowed in cases:
        for path in (parent, log):
            try:
                os.removexattr(path, 'system.posix_acl_access')
            except OSError:
                pass
            os.chown(path, 0, 0)
            path.chmod(0o755 if path.is_dir() else 0o644)
        os.chown(checked, 0, gid)
        acl(checked, named_user, named_group, group, mask, other)
        kernel_allowed = kernel_can_read(log)
        assert kernel_allowed == allowed, (check_directory, named_user, named_group, group, mask, other, gid, allowed,
            checked.stat(), list(struct.iter_unpack('<HHI', os.getxattr(checked, 'system.posix_acl_access')[4:])),
            manager._access_permissions(checked, checked.stat(), 1000, [1000]))
        entry = manager.get_xmx_logs('1817230')['logs'][2]
        assert entry['exists'] == allowed, (check_directory, named_user, named_group, group, mask, other, gid, entry)
        if allowed:
            assert entry['text'] == 'ACL-CONTROLLED-DUMMY', entry
        else:
            assert 'ACL-CONTROLLED-DUMMY' not in entry['text'] and 'Unavailable' in entry['text'], entry
''')

    def test_unreadable_journal_fd_blocks_status_and_mutation_in_uid0_namespace(self):
        self.run_root_namespace('''
settings = Path('/settings')
settings.mkdir()
os.chown(settings, 1000, 1000)
manager = xmx.Manager(home, settings, sysfs='/drm', proc='/proc')
plan = manager.plan_xmx('1817230', str(home/'xess-xmx-linux'), '')
assert plan['ok'], plan
for scope in ('apps', 'dll-apps'):
    app = manager.root/scope/'1817230'
    manager._mkdir(app)
    journal = app/'journal.json'
    journal.write_text('null')
    journal.chmod(0o600)
    assert journal.stat().st_uid == 0
    before = xmx.snapshot(journal)
    status = (manager.get_xmx_status if scope == 'apps' else manager.get_xess_dll_status)('1817230')
    assert not status['ok'] and 'accessible' in status['error'], status
    result = manager.plan_xmx('1817230', str(home/'xess-xmx-linux'), '')
    assert not result['ok'], result
    result = manager.apply_xmx('1817230', plan['plan_id'])
    assert not result['ok'], result
    assert xmx.same(xmx.snapshot(journal), before)
    journal.unlink()
assert manager.apply_xmx('1817230', plan['plan_id'])['ok']
assert manager.recover_xmx('1817230', plan['plan_id'], '')['ok']
''')

    def test_retained_preview_preserves_owner_appearing_after_preview(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        app = self.manager._app('1817230')
        owner = app/'installed.json'
        owner.write_text('{"unexpected":"owner appeared after preview"}')
        before = xmx.snapshot(owner)
        generation = (app/'generation.json').read_bytes()
        targets = {item['target']: xmx.snapshot(item['target']) for item in plan['operations']}
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(owner), before))
        self.assertEqual((app/'generation.json').read_bytes(), generation)
        self.assertFalse((app/'journal.json').exists())
        for path, expected in targets.items():
            self.assertTrue(xmx.same(xmx.snapshot(path), expected), path)

    def test_pending_enable_preserves_new_owner_on_finish_and_abort(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        pending_owner_conflict(self, plan)

    def test_owner_commit_boundary_preserves_late_owner(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owner_boundary_conflict(self, plan)

    def test_owner_abort_delete_boundary_preserves_late_owner(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owner_boundary_conflict(self, plan, completion='abort')

    def test_owner_appearing_during_managed_staging_prevents_apply_rename(self):
        owner_during_managed_stage(self)

    def test_owner_commit_death_can_abort_to_preinstall_state(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owner_commit_death(self, plan, recovery='abort')

    def test_owner_delete_death_can_resume_disable(self):
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owner_commit_death(self, plan)

    def test_installed_owner_metadata_is_bounded_before_preview_consumption(self):
        bounded_owner_metadata(self)

    def test_owner_disable_delete_boundary_preserves_late_owner(self):
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owner_boundary_conflict(self, plan)

    def test_installed_owner_parse_and_snapshot_share_the_authorized_descriptor(self):
        self.enable()
        owner = self.manager._app('1817230')/'installed.json'
        before = xmx.snapshot(owner)
        previous = json.loads(owner.read_text())
        original = xmx.regular_file
        swapped = []
        @contextmanager
        def race(path, **kwargs):
            with original(path, **kwargs) as source:
                if Path(path) == owner and not swapped:
                    self.assertEqual(kwargs.get('authorize'), self.manager._fd_access)
                    replacement = owner.with_name('new-owner')
                    replacement.write_text('{"unexpected":"new pathname, not the open owner"}')
                    replacement.replace(owner)
                    swapped.append(True)
                yield source
        with patch.object(xmx, 'regular_file', race):
            record, identity = self.manager._installed_state('1817230')
        self.assertTrue(swapped)
        self.assertEqual(record, previous)
        self.assertTrue(xmx.same(identity, before))
        self.assertIn('unexpected', json.loads(owner.read_text()))

    def test_unbound_old_preview_is_refused_without_consuming_it(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        app = self.manager._app('1817230')
        path = app/'plans'/(plan['plan_id']+'.json')
        record = json.loads(path.read_text())
        record.pop('installed_owner')
        path.write_text(json.dumps(record))
        generation = (app/'generation.json').read_bytes()
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertIn('replan', result['error'])
        self.assertEqual((app/'generation.json').read_bytes(), generation)
        self.assertFalse((app/'journal.json').exists())

    def test_owner_change_during_abort_staging_preserves_managed_destination(self):
        owner_during_restore_stage(self)

    def test_owner_change_during_disable_staging_preserves_managed_destination(self):
        owner_during_restore_stage(self, completion='finish')

    def test_status_is_static_and_never_claims_runtime_verification(self):
        result = self.manager.get_xmx_status('1817230')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['installed'])
        self.assertFalse(result['legacy'])
        self.assertEqual(result['phase'], 'disabled')
        self.assertIs(result['runtime_verified'], False)
        self.assertEqual(result['suggested_package'], str(self.package))
        self.assertTrue(result['package']['valid'], result)
        self.assertTrue(result['gpu']['xmx_capable'])
        self.assertIsNone(result['flatpak']['visible'])

    def test_enable_requires_exact_client_confirmation_before_commit(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        applied = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.assertTrue(applied['pending_launch'])
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), pe64())
        status = self.manager.get_xmx_status('1817230')
        self.assertEqual(status['phase'], 'pending_enable')
        self.assertFalse(status['installed'])
        self.assertEqual(status['pending']['plan_id'], plan['plan_id'])
        failed = self.manager.finish_xmx('1817230', plan['plan_id'], '')
        self.assertFalse(failed['ok'])
        result = self.manager.finish_xmx('1817230', plan['plan_id'],
                                         applied['desired_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.manager.get_xmx_status('1817230')['installed'])
        self.assertFalse(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        app = self.settings/'xess-xmx/apps/1817230'
        self.assertIn('ANV_CM_HELPER=', (app/'xess.conf').read_text())
        self.assertIn('export XMX_CONF=', (app/'launch.sh').read_text())
        self.assertEqual(json.loads((app/'intel_icd.json').read_text())['ICD']['library_path'],
                         str(self.package/'lib/libvulkan_intel.so'))

    def test_xess_extra_exports_survive_enable_and_configure(self):
        app = self.manager._app('1817230')
        app.mkdir(parents=True, exist_ok=True)
        (app/'xess.extra').write_text('# user extras win last\n'
                                      'export WINEDLLOVERRIDES="cabinet=n,b"\n'
                                      'export FSR4_XMX_NO_DIALOG=1\n')
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        applied = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.manager.finish_xmx('1817230', plan['plan_id'], applied['desired_launch_options'])
        conf = (app/'xess.conf').read_text()
        self.assertIn('export WINEDLLOVERRIDES="cabinet=n,b"', conf)
        self.assertIn('export FSR4_XMX_NO_DIALOG=1', conf)
        # A later configure (driver change) must re-render but keep the extras.
        plan = self.manager.plan_xmx('1817230', str(self.package),
                                     applied['desired_launch_options'],
                                     action='configure',
                                     profile={'driver': 'default', 'discovery': 'alternate',
                                              'diagnostics': False, 'fg': 0, 'bridge': 0,
                                              'sdk_latest': False})
        applied = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.manager.finish_xmx('1817230', plan['plan_id'], applied['desired_launch_options'])
        conf = (app/'xess.conf').read_text()
        self.assertIn('INTC_ALT_DRIVER_EXTENSIONS_PATH', conf)
        self.assertIn('export WINEDLLOVERRIDES="cabinet=n,b"', conf)

    def test_invalid_xess_extra_line_blocks_the_plan(self):
        app = self.manager._app('1817230')
        app.mkdir(parents=True, exist_ok=True)
        (app/'xess.extra').write_text('rm -rf /\n')
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertIn('xess.extra', result['error'])

    def test_official_reduced_igc_package_is_valid_without_opencl_frontends(self):
        self.assertTrue(self.manager._package(self.package)['valid'])
        for relative in ('igc/igc/local/lib/libigdfcl.so.2',
                         'igc/igc/local/lib/libopencl-clang2.so.15'):
            (self.package/relative).unlink()
        package = self.manager._package(self.package)
        self.assertTrue(package['valid'], package)
        self.assertTrue(package['compiler_validated'], package)
        self.assertEqual(package['errors'], [])
        self.assertFalse(package['runtime_verified'])

    def test_reduced_igc_package_still_requires_each_core_dependency(self):
        for relative in ('igc/igc/local/lib/libigdfcl.so.2',
                         'igc/igc/local/lib/libopencl-clang2.so.15'):
            (self.package/relative).unlink()
        for relative, glob in (
                ('igc/igc/local/lib/libigc.so.2', 'igc/igc/local/lib/libigc.so*'),
                ('igc/igc/local/lib/libiga64.so.2', 'igc/igc/local/lib/libiga64.so*'),
                ('igc/neo/lib/x86_64-linux-gnu/libocloc.so',
                 'igc/neo/lib/x86_64-linux-gnu/libocloc.so*'),
                ('igc/neo/bin/ocloc-test', 'igc/neo/bin/ocloc*')):
            with self.subTest(relative=relative):
                path = self.package/relative
                original, mode = path.read_bytes(), path.stat().st_mode & 0o777
                path.unlink()
                try:
                    package = self.manager._package(self.package)
                    self.assertFalse(package['valid'], package)
                    self.assertFalse(package['compiler_validated'], package)
                    self.assertEqual(package['errors'], ['Missing compiler dependency: '+glob])
                finally:
                    path.write_bytes(original)
                    path.chmod(mode)
                self.assertTrue(self.manager._package(self.package)['valid'])

    def test_package_validation_rejects_incomplete_or_wrong_architecture_payloads(self):
        cases = [
            ('ControlLib.dll', b'MZ fake DLL'),
            ('igdext64.dll', pe64()[:0x98]+b'\x0b\x01'+pe64()[0x9a:]),
            ('lib/libvulkan_intel.so', b'not an ELF'),
            ('lib/mesa-test/libvulkan_intel.so', b'not a real selected driver'),
            ('tools/cmk_pack.py', b''),
            ('igc/igc/local/lib/libigc.so.2', b'placeholder'),
            ('igc/neo/lib/x86_64-linux-gnu/libocloc.so', b''),
            ('igc/neo/bin/ocloc-test', b'#!/bin/sh\nexit 0\n'),
        ]
        for relative, bad in cases:
            with self.subTest(relative=relative):
                path = self.package/relative
                original = path.read_bytes()
                path.write_bytes(bad)
                status = self.manager.get_xmx_status('1817230', str(self.package))
                self.assertFalse(status['package']['valid'], status)
                self.assertTrue(status['package']['errors'])
                result = self.manager.plan_xmx('1817230', str(self.package), '')
                self.assertFalse(result['ok'], result)
                path.write_bytes(original)
        executable = self.package/'tools/cm-compile.sh'
        executable.chmod(0o644)
        self.assertFalse(self.manager.get_xmx_status('1817230')['package']['valid'])

    def enable(self, options=''):
        plan = self.manager.plan_xmx('1817230', str(self.package), options)
        self.assertTrue(plan['ok'], plan)
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(result['ok'], result)
        result = self.manager.finish_xmx('1817230', plan['plan_id'],
                                         plan['desired_launch_options'])
        self.assertTrue(result['ok'], result)
        return plan

    def test_prefix_rebuild_removing_created_files_can_be_re_enabled_not_deadlocked(self):
        enabled = self.enable()
        driver_dir = self.system32/'driverstore/filerepository/igd_faux.inf_1'
        driver = driver_dir/'igdext64.dll'
        self.assertTrue(driver.is_file())
        # Proton or Steam verifies/rebuilds the prefix and wipes our created tree.
        import shutil as _shutil
        _shutil.rmtree(self.system32/'driverstore')
        (self.system32/'ControlLib.dll').unlink()
        # Re-using the setup must not deadlock in an owned-file conflict...
        plan = self.manager.plan_xmx('1817230', str(self.package),
                                     enabled['desired_launch_options'], 'configure')
        self.assertTrue(plan['ok'], plan)
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(result['ok'], result)
        result = self.manager.finish_xmx('1817230', plan['plan_id'],
                                         plan['desired_launch_options'])
        self.assertTrue(result['ok'], result)
        # ...and the files really come back.
        self.assertTrue(driver.is_file())
        self.assertTrue((self.system32/'ControlLib.dll').is_file())
        self.assertTrue(self.manager.get_xmx_status('1817230')['installed'])

    def test_third_party_file_at_created_path_still_blocks_configure(self):
        enabled = self.enable()
        driver = self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll'
        driver.write_bytes(b'someone else replaced our file')
        plan = self.manager.plan_xmx('1817230', str(self.package),
                                     enabled['desired_launch_options'], 'configure')
        self.assertFalse(plan['ok'], plan)
        self.assertIn('Owned file conflict', plan['error'])

    def test_disable_defers_removal_until_confirmation_then_restores_metadata(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original exact binary\0\xff')
        original.chmod(0o640)
        os.utime(original, ns=(1600000000000000000, 1600000000000000001))
        os.setxattr(original, 'user.xmx-test', b'original metadata')
        before = original.stat()
        enabled = self.enable('env FOO="a b" %command% --original')
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['desired_launch_options'], enabled['old_launch_options'])
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(original.read_bytes(), pe64())
        self.assertFalse(self.manager.finish_xmx('1817230', plan['plan_id'], 'wrong')['ok'])
        result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(result['ok'], result)
        after = original.stat()
        self.assertEqual(original.read_bytes(), b'original exact binary\0\xff')
        self.assertEqual(after.st_mode, before.st_mode)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(os.getxattr(original, 'user.xmx-test'), b'original metadata')
        self.assertFalse((self.system32/'driverstore').exists())
        self.assertFalse(self.manager.get_xmx_status('1817230')['installed'])

    def default_acl(self, directory):
        entries = [(1, 7, 0xffffffff), (2, 4, self.home.stat().st_uid+1),
                   (4, 5, 0xffffffff), (16, 5, 0xffffffff), (32, 5, 0xffffffff)]
        acl = struct.pack('<I', 2)+b''.join(struct.pack('<HHI', *entry) for entry in entries)
        os.setxattr(directory, 'system.posix_acl_default', acl)

    def test_disable_removes_inherited_access_acl_absent_from_original(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original without access ACL')
        target.chmod(0o640)
        before = xmx.snapshot(target)
        self.assertEqual(before['xattrs'], {})
        self.default_acl(self.system32)
        enabled = self.enable()
        self.assertIn('system.posix_acl_access', xmx.snapshot(target)['xattrs'])
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(target), before), xmx.snapshot(target))

    def test_default_acl_restore_survives_process_death_after_rename(self):
        target = self.system32/'ControlLib.dll'
        for action in ('disable', 'abort'):
            with self.subTest(action=action):
                if 'system.posix_acl_default' in os.listxattr(self.system32):
                    os.removexattr(self.system32, 'system.posix_acl_default')
                target.unlink(missing_ok=True)
                target.write_bytes(b'original for default ACL crash recovery')
                target.chmod(0o640)
                before = xmx.snapshot(target)
                self.assertEqual(before['xattrs'], {})
                self.default_acl(self.system32)
                enabled = self.manager.plan_xmx('1817230', str(self.package), '')
                self.assertTrue(enabled['ok'], enabled)
                self.assertTrue(self.manager.apply_xmx('1817230', enabled['plan_id'])['ok'])
                if action == 'disable':
                    self.assertTrue(self.manager.finish_xmx('1817230', enabled['plan_id'],
                                                            enabled['desired_launch_options'])['ok'])
                    plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
                    self.assertTrue(plan['ok'], plan)
                    self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
                else:
                    plan = enabled
                pid = os.fork()
                if pid == 0:
                    replace = os.replace
                    def die_after_restore(source, destination, *args, **kwargs):
                        result = replace(source, destination, *args, **kwargs)
                        if Path(destination).name == target.name:
                            os._exit(73)
                        return result
                    with patch.object(xmx.os, 'replace', side_effect=die_after_restore):
                        if action == 'disable':
                            self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
                        else:
                            self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
                    os._exit(74)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 73)
                self.assertTrue(xmx.same(xmx.snapshot(target), before), xmx.snapshot(target))
                self.manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
                self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])
                if action == 'disable':
                    result = self.manager.recover_xmx('1817230', plan['plan_id'],
                                                      plan['desired_launch_options'], 'resume')
                else:
                    result = self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
                self.assertTrue(result['ok'], result)
                self.assertTrue(xmx.same(xmx.snapshot(target), before))
                self.assertIsNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_crashed_enable_is_recoverable_after_restart_only_with_original_options(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original')
        original.chmod(0o640)
        plan = self.manager.plan_xmx('1817230', str(self.package), 'CUSTOM=1 %command%')
        atomic = self.manager._atomic
        def crash(path, *args, **kwargs):
            if Path(path) == original:
                raise OSError('simulated power loss before second prefix write')
            return atomic(path, *args, **kwargs)
        with patch.object(self.manager, '_atomic', side_effect=crash):
            self.assertFalse(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        status = self.manager.get_xmx_status('1817230')
        self.assertEqual(status['phase'], 'recovery_required')
        self.assertEqual(status['pending']['plan_id'], plan['plan_id'])
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])
        self.assertFalse(self.manager.recover_xmx('1817230', plan['plan_id'], 'wrong')['ok'])
        result = self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(original.read_bytes(), b'original')
        self.assertEqual(original.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.system32/'driverstore').exists())
        self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'disabled')
        self.assertFalse(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])

    def test_plan_refuses_changed_package_source_without_prefix_writes(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        (self.package/'tools/cmk_pack.py').write_text('# changed after consent\n')
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertIn('source', result['error'].lower())
        self.assertFalse(self.manager.apply_xmx('12345', plan['plan_id'])['ok'])

    def test_competing_plans_are_invalidated_even_after_abort_returns_original_files(self):
        first = self.manager.plan_xmx('1817230', str(self.package), '')
        stale = self.manager.plan_xmx('1817230', str(self.package), 'OTHER=1 %command%')
        self.assertTrue(self.manager.apply_xmx('1817230', first['plan_id'])['ok'])
        self.assertTrue(self.manager.recover_xmx('1817230', first['plan_id'], '')['ok'])
        result = self.manager.apply_xmx('1817230', stale['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_enable_over_our_own_stale_wrapper_never_double_wraps(self):
        # After the per-game record was cleared, Steam can still hold exactly
        # the wrapper we would write. A fresh enable must reuse that string;
        # wrapping it again produced a broken "launch.sh launch.sh %command%"
        # desired (on-device, after the Pragmata reset).
        first = self.manager.plan_xmx('1817230', str(self.package), '')
        wrapper = first['desired_launch_options']
        second = self.manager.plan_xmx('1817230', str(self.package), wrapper)
        self.assertTrue(second['ok'], second)
        self.assertEqual(second['desired_launch_options'], wrapper)
        self.assertEqual(second['old_launch_options'], wrapper)

    def test_symlinked_prefix_target_and_state_paths_are_refused(self):
        outside = self.home/'outside'
        outside.mkdir()
        (outside/'ControlLib.dll').write_bytes(b'untouched')
        self.system32.rmdir()
        self.system32.symlink_to(outside, target_is_directory=True)
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertEqual((outside/'ControlLib.dll').read_bytes(), b'untouched')
        self.system32.unlink()
        self.system32.mkdir()
        other_settings = self.root/'other-settings'
        other_settings.mkdir()
        (other_settings/'xess-xmx').symlink_to(outside, target_is_directory=True)
        other = xmx.Manager(self.home, other_settings, sysfs=self.sysfs, proc=self.root/'proc')
        result = other.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertFalse((outside/'apps').exists())

    def test_settings_boundary_and_ancestor_symlinks_are_not_adopted(self):
        outside = self.root/'outside-settings'
        outside.mkdir()
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                linked = self.root/('linked-parent' if ancestor else 'linked-settings')
                linked.symlink_to(outside, target_is_directory=True)
                settings = linked/'nested-settings' if ancestor else linked
                manager = xmx.Manager(self.home, settings, sysfs=self.sysfs, proc=self.root/'proc')
                result = manager.plan_xmx('1817230', str(self.package), '')
                self.assertFalse(result['ok'], result)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_settings_creation_requires_target_user_write_in_real_root_namespace(self):
        import subprocess
        code = '''
import os
from pathlib import Path
from optiscaler import xmx, xess_dll
root = Path(os.environ['XMX_TEST_ROOT'])
# Chroot only into this scratch fixture so all ancestors are controlled: the
# denial below really is the nearest existing parent, not the host's 0700 home.
root.chmod(0o755)
os.chroot(root)
os.chdir('/')
root = Path('/')
home = root/'namespace-home'
home.mkdir()
os.chown(home, 1000, 1000)
parent = root/'namespace-settings-parent'
parent.mkdir(mode=0o755)
assert os.geteuid() == 0 and parent.stat().st_uid == 0
# UID 0 can create here; the selected UID 1000 cannot. No fake stat/access.
probe = parent/'root-can-create'
probe.mkdir()
probe.rmdir()
manager = xmx.Manager(home, parent/'settings')
try:
    for acl_denial in (False, True):
        if acl_denial:
            import struct
            entries = [(1, 7, 0xffffffff), (2, 0, 1000), (4, 7, 0xffffffff),
                       (16, 7, 0xffffffff), (32, 7, 0xffffffff)]
            acl = struct.pack('<I', 2)+b''.join(struct.pack('<HHI', *entry) for entry in entries)
            os.setxattr(parent, 'system.posix_acl_access', acl)
            assert parent.stat().st_mode & 0o777 == 0o777  # Mode mask alone permits write.
        try:
            with manager._lock():
                pass
        except (OSError, ValueError):
            pass
        assert not (parent/'settings').exists(), 'root created settings without target-user write permission'
    os.removexattr(parent, 'system.posix_acl_access')
    parent.chmod(0o777)
    # A writable parent succeeds for the same real UID-0 backend and UID-1000 home.
    with manager._lock():
        assert manager.root.stat().st_uid == 1000
        assert (manager.root/'.lock').stat().st_uid == 1000
finally:
    # Return namespace-owned fixtures to the caller, including on regression.
    for tree in (home, parent):
        for directory, _, files in os.walk(tree):
            os.chown(directory, 0, 0)
            for name in files:
                os.chown(Path(directory)/name, 0, 0)
'''
        environment = dict(os.environ, XMX_TEST_ROOT=str(self.root),
                           PYTHONPATH=str(ROOT/'py_modules'), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(['unshare', '--user', '--map-auto', '--map-root-user',
                                 sys.executable, '-c', code], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_explicit_home_alias_is_bound_without_resolving_mutable_settings(self):
        alias = self.root/'home-alias'
        alias.symlink_to(self.home, target_is_directory=True)
        settings = alias/'plugin-settings'
        manager = xmx.Manager(alias, settings, sysfs=self.sysfs, proc=self.root/'proc')
        self.assertEqual(manager.root, self.home/'plugin-settings/xess-xmx')
        plan = manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        self.assertTrue((manager.root/'apps/1817230/plans'/(plan['plan_id']+'.json')).is_file())

    def test_dangling_prefix_journal_blocks_apply_without_replacing_the_link(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        journal = self.manager._app('1817230')/'journal.json'
        journal.symlink_to('missing-target')
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(journal.is_symlink())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_symlinked_pending_journal_is_refused_without_consuming_it(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        journal = self.settings/'xess-xmx/apps/1817230/journal.json'
        saved = journal.with_name('saved-journal.json')
        journal.rename(saved)
        journal.symlink_to(saved.name)
        self.assertFalse(self.manager.get_xmx_status('1817230')['ok'])
        self.assertFalse(self.manager.finish_xmx('1817230', plan['plan_id'],
                                                plan['desired_launch_options'])['ok'])
        self.assertTrue(journal.is_symlink())
        self.assertTrue(saved.is_file())
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), pe64())

    def test_legacy_manifest_blocks_without_modifying_it(self):
        legacy = self.home/'.local/state/xess-xmx-anatase/1817230/state.json'
        legacy.parent.mkdir(parents=True)
        content = b'{"launch":{"appid":"1817230"},"files":[]}'
        legacy.write_bytes(content)
        status = self.manager.get_xmx_status('1817230')
        self.assertTrue(status['legacy'], status)
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertEqual(legacy.read_bytes(), content)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_new_legacy_manifest_blocks_finish_and_abort_without_touching_pending_files(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        legacy = self.home/'.local/state/xess-xmx-anatase/1817230/state.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"appid":"1817230"}')
        self.assertFalse(self.manager.finish_xmx('1817230', plan['plan_id'],
                                                plan['desired_launch_options'])['ok'])
        self.assertFalse(self.manager.recover_xmx('1817230', plan['plan_id'], '')['ok'])
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), pe64())
        self.assertEqual(legacy.read_text(), '{"appid":"1817230"}')
        self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_unknown_intel_device_is_not_assumed_xmx_capable_and_connectors_are_ignored(self):
        (self.sysfs/'card1/device/device').write_text('0x1234\n')
        connector = self.sysfs/'card1-HDMI-A-1/device'
        connector.mkdir(parents=True)
        (connector/'vendor').write_text('0x8086')
        (connector/'device').write_text('0xb080')
        status = self.manager.get_xmx_status('1817230')
        self.assertFalse(status['gpu']['xmx_capable'])
        self.assertEqual(len(status['gpu']['devices']), 1)
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertIn('GPU', result['error'])

    def test_service_and_plugin_expose_guarded_off_thread_rpc_contract(self):
        import importlib.util
        import threading
        import types
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(ROOT, self.settings, self.root/'runtime',
                                    self.home, logging.getLogger('xmx-tests'))
        self.assertEqual(service.xmx.root, self.settings/'xess-xmx')
        service.xmx = self.manager
        main_thread = threading.get_ident()
        original_plan = self.manager.plan_xmx
        observations = []
        def observed_plan(*args):
            observations.append((threading.get_ident() != main_thread, service._mutation_lock.locked()))
            return original_plan(*args)
        async def exercise():
            with patch.object(self.manager, 'plan_xmx', side_effect=observed_plan):
                plan = await service.plan_xmx('1817230', str(self.package), '')
            self.assertTrue(plan['ok'], plan)
            applied = await service.apply_xmx('1817230', plan['plan_id'])
            self.assertTrue(applied['ok'], applied)
            self.assertTrue((await service.finish_xmx('1817230', plan['plan_id'],
                                                      plan['desired_launch_options']))['ok'])
            self.assertFalse((await service.plan_xmx('../bad', '', ''))['ok'])
            with patch.object(self.manager, 'get_xmx_status', side_effect=OSError('probe failed')):
                self.assertEqual(await service.get_xmx_status('1817230'),
                                 {'ok': False, 'error': 'probe failed'})
        asyncio.run(exercise())
        self.assertEqual(observations, [(True, True)])
        stub = types.ModuleType('decky')
        with patch.dict(sys.modules, decky=stub):
            spec = importlib.util.spec_from_file_location('xmx_test_main', ROOT/'main.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        plugin = module.Plugin()
        plugin.service = service
        for name in ('get_xmx_status', 'plan_xmx', 'apply_xmx', 'finish_xmx', 'recover_xmx'):
            self.assertTrue(callable(getattr(plugin, name, None)), name)
        self.assertTrue(asyncio.run(plugin.get_xmx_status('1817230'))['ok'])

    def test_plugin_rpc_roundtrip_for_abort_enable_and_disable(self):
        import importlib.util
        import types
        from optiscaler.service import OptiScalerService
        with patch.dict(sys.modules, decky=types.ModuleType('decky')):
            spec = importlib.util.spec_from_file_location('xmx_test_rpc_roundtrip', ROOT/'main.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        plugin = module.Plugin()
        plugin.service = OptiScalerService(ROOT, self.settings, self.root/'runtime',
                                          self.home, logging.getLogger('xmx-tests'))
        plugin.service.xmx = self.manager
        async def exercise():
            plan = await plugin.plan_xmx('1817230', str(self.package), '')
            self.assertTrue(plan['ok'], plan)
            self.assertTrue((await plugin.apply_xmx('1817230', plan['plan_id']))['ok'])
            self.assertTrue((await plugin.recover_xmx('1817230', plan['plan_id'], ''))['ok'])
            self.assertEqual((await plugin.get_xmx_status('1817230'))['phase'], 'disabled')
            plan = await plugin.plan_xmx('1817230', str(self.package), '')
            self.assertTrue((await plugin.apply_xmx('1817230', plan['plan_id']))['ok'])
            self.assertTrue((await plugin.finish_xmx('1817230', plan['plan_id'],
                                                     plan['desired_launch_options']))['ok'])
            disable = await plugin.plan_xmx('1817230', '', plan['desired_launch_options'], 'disable')
            self.assertTrue(disable['ok'], disable)
            self.assertEqual(disable['desired_launch_options'], '')
            self.assertTrue((await plugin.apply_xmx('1817230', disable['plan_id']))['ok'])
            self.assertTrue((await plugin.recover_xmx('1817230', disable['plan_id'], '', 'resume'))['ok'])
            self.assertEqual((await plugin.get_xmx_status('1817230'))['phase'], 'disabled')
        asyncio.run(exercise())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_mutation_rpc_cancellation_keeps_lock_until_worker_finishes(self):
        import threading
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(ROOT, self.settings, self.root/'runtime',
                                    self.home, logging.getLogger('xmx-tests'))
        entered, release = threading.Event(), threading.Event()
        def slow_plan(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test worker was not released')
            return {'ok': True}
        async def exercise():
            with patch.object(service.xmx, 'plan_xmx', side_effect=slow_plan):
                task = asyncio.create_task(service.plan_xmx('1817230', str(self.package), ''))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                try:
                    self.assertTrue(service._mutation_lock.locked(),
                                    'RPC cancellation released the lock while its worker was still mutating')
                    task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertTrue(service._mutation_lock.locked(),
                                    'Repeated cancellation released a still-running worker')
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertFalse(service._mutation_lock.locked())
        asyncio.run(exercise())

    def test_tampered_persisted_plan_cannot_write_outside_allowlisted_targets(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        record_path = self.settings/'xess-xmx/apps/1817230/plans'/(plan['plan_id']+'.json')
        record = json.loads(record_path.read_text())
        outside = self.home/'do-not-write.dll'
        record['operations'][0]['target'] = str(outside)
        record_path.write_text(json.dumps(record))
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse(outside.exists())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_package_diagnostics_refuse_escaping_links_and_special_tree_entries(self):
        outside = self.home/'outside-compiler'
        outside.mkdir()
        link = self.package/'lib/bundled/escape'
        link.symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.manager.get_xmx_status('1817230')['package']['valid'])
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])
        link.unlink()
        os.mkfifo(link)
        self.assertFalse(self.manager.get_xmx_status('1817230')['package']['valid'])
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])

    def test_package_refuses_source_symlink_swapped_at_open(self):
        target = self.package/'ControlLib.dll'
        outside = self.home/'outside-package.dll'
        outside.write_bytes(pe64())
        original_open, original_path_open = os.open, Path.open
        swapped = False
        def swap(path):
            nonlocal swapped
            if not swapped and Path(path).name == target.name:
                swapped = True
                target.unlink()
                target.symlink_to(outside)
        def open_fd(path, *args, **kwargs):
            swap(path)
            return original_open(path, *args, **kwargs)
        def open_path(path, *args, **kwargs):
            swap(path)
            return original_path_open(path, *args, **kwargs)
        with patch.object(xmx.os, 'open', side_effect=open_fd), patch.object(Path, 'open', open_path):
            package = self.manager._package(str(self.package))
        self.assertTrue(swapped)
        self.assertFalse(package['valid'], package)

    def test_plan_cannot_persist_bytes_read_through_a_transient_source_symlink(self):
        target = self.package/'ControlLib.dll'
        original = target.read_bytes()
        outside = self.home/'outside-source.dll'
        outside.write_bytes(b'private bytes outside the selected package')
        fd_open, path_open = os.open, Path.open
        opens = 0
        def selected(path):
            nonlocal opens
            if Path(path).name == target.name:
                opens += 1
                return opens == 2  # First read is the static header validation.
            return False
        def transient_open(opener, path, *args, **kwargs):
            if not selected(path):
                return opener(path, *args, **kwargs)
            target.unlink()
            target.symlink_to(outside)
            try:
                return opener(path, *args, **kwargs)
            finally:
                target.unlink()
                with path_open(target, 'wb') as stream:
                    stream.write(original)
        with patch.object(xmx.os, 'open', side_effect=lambda p, *a, **kw:
                          transient_open(fd_open, p, *a, **kw)), patch.object(Path, 'open',
                          lambda p, *a, **kw: transient_open(path_open, p, *a, **kw)):
            result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertGreaterEqual(opens, 2)
        self.assertFalse(result['ok'], result)
        self.assertFalse(list(self.settings.rglob('plans/*.json')))

    def test_package_change_between_payload_read_and_plan_hash_is_refused(self):
        read = self.manager._source_bytes
        def change_after_read(package, relative):
            data = read(package, relative)
            if relative == 'ControlLib.dll':
                (package/relative).write_bytes(pe64()+b'new package revision')
            return data
        with patch.object(self.manager, '_source_bytes', side_effect=change_after_read):
            result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertFalse(list(self.settings.rglob('plans/*.json')))
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_package_helper_must_be_executable_by_target_user_not_just_anyone(self):
        helper = self.package/'tools/cm-compile.sh'
        helper.chmod(0o654)  # Group execute does not grant the owning user execute.
        status = self.manager.get_xmx_status('1817230')
        self.assertFalse(status['package']['valid'], status)
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])

    def test_ambiguous_or_non_command_placeholders_are_refused_without_prefix_writes(self):
        for options in ('"%command%" --flag', "'%command%'", 'PREFIX="%command%"',
                        '%command%.exe', 'echo ignored # %command%', '\\%command%',
                        'env %command% %command%', '%command%\r--flag'):
            with self.subTest(options=options):
                result = self.manager.plan_xmx('1817230', str(self.package), options)
                self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_placeholder_requires_a_real_shell_word_start(self):
        for options in ('CUSTOM=one\\ %command%', 'CUSTOM=one\\\t%command%',
                        'CUSTOM=one\u00a0%command%', 'CUSTOM=one\v%command%',
                        'CUSTOM=one\f%command%', '" "%command%', "''%command%"):
            with self.subTest(options=options):
                result = self.manager.plan_xmx('1817230', str(self.package), options)
                self.assertFalse(result['ok'], result)
                self.assertIn('standalone, unquoted', result['error'])
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertFalse(list(self.settings.rglob('plans/*.json')))

    def test_placeholder_requires_a_real_shell_word_end(self):
        for suffix in ('\u00a0--flag', '\v--flag', '\f--flag',
                       '\\ --flag', '\\\t--flag', '" "--flag'):
            with self.subTest(suffix=suffix):
                result = self.manager.plan_xmx('1817230', str(self.package), '%command%'+suffix)
                self.assertFalse(result['ok'], result)
                self.assertIn('standalone, unquoted', result['error'])
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertFalse(list(self.settings.rglob('plans/*.json')))

    def test_shell_comment_and_backtick_placeholder_regressions_are_refused(self):
        for options in ('printf harmless;# %command%', 'printf %s ` %command% `',
                        'printf %s ` "%command%" `', "printf %s ` '%command%' `",
                        'printf harmless|# %command%', 'printf harmless&# %command%',
                        'printf harmless; %command%', '$(printf %command%)',
                        'FOO="$(printf ignored)" %command%', '%command% # trailing comment',
                        '%command% --flag `printf ignored`'):
            with self.subTest(options=options):
                result = self.manager.plan_xmx('1817230', str(self.package), options)
                self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertFalse(list(self.settings.rglob('plans/*.json')))

    def test_launch_composition_preserves_env_assignments_and_arguments(self):
        import shlex
        import subprocess
        old = (' \t CUSTOM="two words" CUSTOM_ESCAPED=one\\ two '
               'CUSTOM_SLASH=one\\\\\tWINEDLLOVERRIDES="dinput8=n,b;dxgi=n,b" \t%command%\t--original  ')
        plan = self.manager.plan_xmx('1817230', str(self.package), old)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['old_launch_options'], old)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        command = shlex.join([sys.executable, '-c',
            'import os,sys,json; print(json.dumps([os.getenv("CUSTOM"),os.getenv("CUSTOM_ESCAPED"),'
            'os.getenv("CUSTOM_SLASH"),os.getenv("WINEDLLOVERRIDES"),sys.argv[1:]]))'])
        shell_command = plan['desired_launch_options'].replace('%command%', command)
        result = subprocess.run(['/bin/sh', '-c', shell_command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ['two words', 'one two', 'one\\', 'dinput8=n,b;dxgi=n,b', ['--original']])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'],
                                                plan['desired_launch_options'])['ok'])
        disable = self.manager.plan_xmx('1817230', '', plan['desired_launch_options'], 'disable')
        self.assertTrue(disable['ok'], disable)
        self.assertEqual(disable['desired_launch_options'], old)
        self.assertTrue(self.manager.apply_xmx('1817230', disable['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', disable['plan_id'], old)['ok'])

    def proc_fixture(self, pid, name, argv, cgroup, *, ppid=1, uid=None, exe=None):
        """Kernel-visible identities captured read-only on OXP3 (no fake environ)."""
        process = self.root/'proc'/str(pid)
        process.mkdir(exist_ok=True)
        uid = self.home.stat().st_uid if uid is None else uid
        (process/'status').write_text(
            f'Name:\t{name}\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        (process/'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in argv)+b'\0')
        (process/'cgroup').write_text('0::'+cgroup+'\n')
        (process/'environ').write_bytes(b'')
        (process/'exe').symlink_to(exe or '/unreadable-exe')
        return process

    @contextmanager
    def denied_environments(self, *processes):
        read, readlink = Path.read_bytes, Path.readlink
        denied = {process/'environ' for process in processes}
        def guarded(path):
            if path in denied:
                raise PermissionError('OXP3 undumpable process')
            return read(path)
        def guarded_link(path):
            target = readlink(path)
            if target == Path('/unreadable-exe'):
                raise PermissionError('OXP3 undumpable executable link')
            return target
        with patch.object(Path, 'read_bytes', guarded), patch.object(Path, 'readlink', guarded_link):
            yield

    def test_undumpable_backend_recognizes_its_actual_proc_directory(self):
        process = Path('/proc')/str(os.getpid())
        original_iterdir = Path.iterdir
        self.manager.proc = Path('/proc')
        def only_backend(path):
            return iter([process]) if path == Path('/proc') else original_iterdir(path)
        with patch.object(Path, 'iterdir', only_backend), self.denied_environments(process):
            self.manager._stopped('1817230', self.prefix)

    def test_same_pid_number_in_another_proc_tree_is_not_a_self_exemption(self):
        process = self.proc_fixture(os.getpid(), 'Decky OptiScale',
            ['Decky OptiScaler (main.py)'], '/system.slice/plugin_loader.service')
        with self.denied_environments(process):
            with self.assertRaisesRegex(ValueError, 'host identity unproven'):
                self.manager._stopped('1817230', self.prefix)

    def test_oxp3_undumpable_decky_helpers_do_not_block_planning(self):
        cgroup = '/system.slice/plugin_loader.service'
        loader = str(self.home/'homebrew/services/PluginLoader')
        self.proc_fixture(1611, 'PluginLoader', [loader, '-B', '-S', '-I', '-c',
            'import sys; from multiprocessing.forkserver import main; main(...)', loader],
            cgroup, ppid=1584, uid=0)
        denied = []
        for pid, title, directory in ((1613, 'TabMaster', 'TabMaster'),
                (1619, 'CSS Loader', 'SDH-CssLoader'),
                (1625, 'SteamGridDB', 'decky-steamgriddb')):
            script = self.home/'homebrew/plugins'/directory/'main.py'
            denied.append(self.proc_fixture(pid, title, [f'{title} ({script})', '', ''],
                                             cgroup, ppid=1611))
        with self.denied_environments(*denied):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_oxp3_undumpable_user_manager_is_identified_by_root_systemd_parent(self):
        self.proc_fixture(1, 'systemd', ['/usr/lib/systemd/systemd', '--switched-root', '--system'],
                          '/init.scope', ppid=0, uid=0)
        uid = self.home.stat().st_uid
        process = self.proc_fixture(1715, 'systemd', ['/usr/lib/systemd/systemd', '--user'],
            f'/user.slice/user-{uid}.slice/user@{uid}.service/init.scope')
        with self.denied_environments(process):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)

    def test_oxp3_undumpable_sd_pam_requires_identified_user_manager_ancestry(self):
        uid = self.home.stat().st_uid
        container = '/system.slice/system-spaces.slice/spaces@arch.service/payload'
        self.proc_fixture(2268, 'systemd', ['/usr/lib/systemd/systemd'],
                          container+'/init.scope', ppid=2264, uid=0)
        group = container+f'/user.slice/user-{uid}.slice/user@{uid}.service/init.scope'
        self.proc_fixture(2971, 'systemd', ['/usr/lib/systemd/systemd', '--user'],
                          group, ppid=2268, exe='/usr/lib/systemd/systemd')
        process = self.proc_fixture(2980, '(sd-pam)', ['(sd-pam)'], group, ppid=2971)
        with self.denied_environments(process):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)

    def test_oxp3_container_login_sd_pam_requires_verified_sleep_and_root_init(self):
        uid = self.home.stat().st_uid
        container = '/system.slice/system-spaces.slice/spaces@arch.service/payload'
        self.proc_fixture(2268, 'systemd', ['/usr/lib/systemd/systemd'],
                          container+'/init.scope', ppid=2264, uid=0)
        group = container+f'/user.slice/user-{uid}.slice/session-c5.scope'
        self.proc_fixture(2930, 'sleep', ['/usr/bin/sleep', 'infinity'], group,
                          ppid=2268, exe='/usr/bin/sleep')
        process = self.proc_fixture(2995, '(sd-pam)', ['(sd-pam)'], group, ppid=2930)
        with self.denied_environments(process):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)

    def test_oxp3_undumpable_gamescope_session_is_not_a_game_launcher(self):
        uid = self.home.stat().st_uid
        self.proc_fixture(1, 'systemd', ['/usr/lib/systemd/systemd', '--system'],
                          '/init.scope', ppid=0, uid=0)
        user = f'/user.slice/user-{uid}.slice/user@{uid}.service'
        self.proc_fixture(1715, 'systemd', ['/usr/lib/systemd/systemd', '--user'], user+'/init.scope')
        group = user+'/session.slice/anatase-gamemode.service'
        self.proc_fixture(1781, 'anatase-gamemod', ['/bin/bash', '/usr/libexec/anatase-gamemode'],
                          group, ppid=1715, exe='/usr/bin/bash')
        argv = ['gamescope', '--backend', 'drm', '--generate-drm-mode', 'fixed',
                '--xwayland-count', '2', '--default-touch-mode', '4', '--hide-cursor-delay',
                '3000', '--max-scale', '2', '--fade-out-duration', '200', '--cursor-scale-height',
                '720', '-e', '-R', f'/run/user/{uid}/gamescope.uOLILVF/startup.socket',
                '-T', f'/run/user/{uid}/gamescope.uOLILVF/stats.pipe', '-O', '*,eDP-1']
        process = self.proc_fixture(1806, 'gamescope-wl', argv, group, ppid=1781)
        with self.denied_environments(process):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        # Even a session-owned gamescope with a child command is not exempt.
        (process/'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in
                                                 argv+['--', 'wine', 'game.exe'])+b'\0')
        with self.denied_environments(process):
            refused = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(refused['ok'], refused)

    def test_oxp3_undumpable_ssh_session_requires_root_postauth_and_listener(self):
        username = xmx.pwd.getpwuid(self.home.stat().st_uid).pw_name
        self.proc_fixture(1531, 'sshd', ['sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups'],
                          '/system.slice/sshd.service', uid=0)
        group = f'/user.slice/user-{self.home.stat().st_uid}.slice/session-29.scope'
        self.proc_fixture(11096, 'sshd-session', [f'sshd-session: {username} [postauth]', ''],
                          group, ppid=1531, uid=0)
        process = self.proc_fixture(11100, 'sshd-session', [f'sshd-session: {username}@notty', '', ''],
                                   group, ppid=11096)
        with self.denied_environments(process):
            plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)

    def test_running_game_refuses_mutation_even_if_it_started_after_planning(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        proc = self.root/'proc/123'
        proc.mkdir(parents=True)
        (proc/'environ').write_bytes(b'SteamAppId=1817230\0')
        self.manager.proc = self.root/'proc'
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertIn('running', result['error'].lower())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_changed_owned_permissions_refuse_commit_and_abort_without_deleting_user_changes(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        owned = self.system32/'ControlLib.dll'
        owned.chmod(0o600)
        result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertFalse(result['ok'], result)
        result = self.manager.recover_xmx('1817230', plan['plan_id'], '')
        self.assertFalse(result['ok'], result)
        self.assertTrue(owned.exists())
        self.assertEqual(owned.stat().st_mode & 0o777, 0o600)
        self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_owned_xattr_edits_block_commit_and_abort_without_losing_changes(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        target = self.system32/'ControlLib.dll'
        os.setxattr(target, 'user.external', b'user metadata after apply')
        self.assertFalse(self.manager.finish_xmx('1817230', plan['plan_id'],
                                                plan['desired_launch_options'])['ok'])
        self.assertFalse(self.manager.recover_xmx('1817230', plan['plan_id'], '')['ok'])
        self.assertEqual(os.getxattr(target, 'user.external'), b'user metadata after apply')
        self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_stale_metadata_change_is_not_silently_overwritten(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original')
        os.setxattr(target, 'user.example', b'old')
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        os.setxattr(target, 'user.example', b'changed by user after planning')
        result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertEqual(os.getxattr(target, 'user.example'), b'changed by user after planning')
        self.assertEqual(target.read_bytes(), b'original')

    def test_status_keeps_selected_package_after_restart_without_frontend_resupplying_path(self):
        moved = self.home/'custom package with spaces'
        self.package.rename(moved)
        self.package = moved
        self.enable()
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        status = fresh.get_xmx_status('1817230')
        self.assertEqual(status['package']['path'], str(moved))
        self.assertTrue(status['package']['valid'], status)

    def test_digest_refuses_symlink_and_special_files(self):
        original = self.home/'digest-original'
        original.write_bytes(b'no link following')
        linked = self.home/'digest-link'
        linked.symlink_to(original)
        with self.assertRaises((OSError, ValueError)):
            xmx.digest(linked)
        linked.unlink()
        os.mkfifo(linked)
        with self.assertRaises((OSError, ValueError)):
            xmx.digest(linked)

    def test_snapshot_refuses_leaf_symlink_swapped_at_open(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original regular file')
        secret = self.home/'outside-snapshot'
        secret.write_bytes(b'must not read through a swapped symlink')
        original_open, original_path_open = os.open, Path.open
        swapped = False
        def swap(path):
            nonlocal swapped
            if not swapped and Path(path).name == target.name:
                swapped = True
                target.unlink()
                target.symlink_to(secret)
        def open_fd(path, *args, **kwargs):
            swap(path)
            return original_open(path, *args, **kwargs)
        def open_path(path, *args, **kwargs):
            swap(path)
            return original_path_open(path, *args, **kwargs)
        with patch.object(xmx.os, 'open', side_effect=open_fd), patch.object(Path, 'open', open_path):
            with self.assertRaises((OSError, ValueError)):
                xmx.snapshot(target)
        self.assertTrue(swapped)
        self.assertEqual(secret.read_bytes(), b'must not read through a swapped symlink')

    def test_atomic_write_cannot_follow_parent_swapped_for_symlink_mid_write(self):
        target = self.system32/'ControlLib.dll'
        outside = self.home/'outside-race'
        outside.mkdir()
        kept = self.system32.with_name('system32-kept')
        original_open = os.open
        swapped = False
        def swap(path, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path).name.startswith('.ControlLib.dll.'):
                swapped = True
                self.system32.rename(kept)
                self.system32.symlink_to(outside, target_is_directory=True)
            return original_open(path, *args, **kwargs)
        try:
            with patch.object(xmx.os, 'open', side_effect=swap):
                self.manager._atomic(target, b'new owned DLL', 0o644)
        except (OSError, ValueError):
            pass  # Refusal is also safe; writing through the replacement is not.
        self.assertTrue(swapped)
        self.assertFalse((outside/'ControlLib.dll').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_disable_unlink_cannot_follow_parent_swapped_for_symlink(self):
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        outside = self.home/'outside-unlink'
        outside.mkdir()
        sentinel = outside/'ControlLib.dll'
        sentinel.write_bytes(b'user unrelated data')
        kept = self.system32.with_name('system32-kept')
        original_unlink = os.unlink
        swapped = False
        def swap(path, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path).name == 'ControlLib.dll':
                swapped = True
                self.system32.rename(kept)
                self.system32.symlink_to(outside, target_is_directory=True)
            return original_unlink(path, *args, **kwargs)
        with patch.object(xmx.os, 'unlink', side_effect=swap):
            self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(swapped)
        self.assertTrue(sentinel.is_file())
        self.assertEqual(sentinel.read_bytes(), b'user unrelated data')

    def test_enable_rechecks_same_target_after_staging_fsync(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original before same-target race')
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        sync = os.fsync
        edited = False
        def edit_staged_destination(fd):
            nonlocal edited
            path = Path(os.readlink('/proc/self/fd/'+str(fd)))
            if not edited and path.name == '.decky-xmx-'+plan['plan_id']+'-1.tmp':
                edited = True
                target.write_bytes(b'user edit while this same target was staged')
            return sync(fd)
        with patch.object(xmx.os, 'fsync', side_effect=edit_staged_destination):
            result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(edited)
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'user edit while this same target was staged')
        app = self.settings/'xess-xmx/apps/1817230'
        self.assertTrue((app/'journal.json').is_file())
        self.assertEqual((app/'backups'/plan['plan_id']/'1').read_bytes(),
                         b'original before same-target race')

    def restore_staging_edit(self, action):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original before restoration staging')
        enabled = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(enabled['ok'], enabled)
        self.assertTrue(self.manager.apply_xmx('1817230', enabled['plan_id'])['ok'])
        if action == 'disable':
            self.assertTrue(self.manager.finish_xmx('1817230', enabled['plan_id'],
                                                    enabled['desired_launch_options'])['ok'])
            plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
            self.assertTrue(plan['ok'], plan)
            self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        else:
            plan = enabled
        sync = os.fsync
        edited = False
        def edit_staged_destination(fd):
            nonlocal edited
            path = Path(os.readlink('/proc/self/fd/'+str(fd)))
            if not edited and path.name == '.decky-xmx-'+enabled['plan_id']+'-1.tmp':
                edited = True
                target.write_bytes(b'user edit while restoring this same target')
            return sync(fd)
        with patch.object(xmx.os, 'fsync', side_effect=edit_staged_destination):
            if action == 'disable':
                result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
            else:
                result = self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(edited)
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'user edit while restoring this same target')
        app = self.settings/'xess-xmx/apps/1817230'
        self.assertTrue((app/'journal.json').is_file())
        self.assertEqual((app/'backups'/enabled['plan_id']/'1').read_bytes(),
                         b'original before restoration staging')
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        self.assertEqual(fresh.get_xmx_status('1817230')['phase'], 'recovery_required')

    def test_disable_rechecks_same_target_after_staging_fsync(self):
        self.restore_staging_edit('disable')

    def test_abort_rechecks_same_target_after_staging_fsync(self):
        self.restore_staging_edit('abort')

    def test_abort_rechecks_same_target_at_unlink_entry(self):
        target = self.system32/'ControlLib.dll'
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        unlink = self.manager._unlink
        edited = False
        def edit_before_pinned_unlink(path, *args, **kwargs):
            nonlocal edited
            if Path(path) == target:
                edited = True
                target.write_bytes(b'user edit immediately before pinned deletion')
            return unlink(path, *args, **kwargs)
        with patch.object(self.manager, '_unlink', side_effect=edit_before_pinned_unlink):
            result = self.manager.recover_xmx('1817230', plan['plan_id'], '')
        self.assertTrue(edited)
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'user edit immediately before pinned deletion')
        self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_mid_apply_external_edit_is_not_overwritten_by_a_later_operation(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original before plan')
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        original_atomic = self.manager._atomic
        def edit_after_first(path, *args, **kwargs):
            result = original_atomic(path, *args, **kwargs)
            if Path(path).name == 'igdext64.dll':
                target.write_bytes(b'user edit during apply')
            return result
        with patch.object(self.manager, '_atomic', side_effect=edit_after_first):
            result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'user edit during apply')
        self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'recovery_required')

    def test_abort_does_not_adopt_matching_bytes_written_to_an_unstarted_target(self):
        target = self.system32/'ControlLib.dll'
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        atomic = self.manager._atomic
        def external_edit(path, *args, **kwargs):
            result = atomic(path, *args, **kwargs)
            if Path(path).name == 'igdext64.dll':
                target.write_bytes(pe64())  # Same bytes/mode, but this operation never started.
                target.chmod(0o644)
            return result
        with patch.object(self.manager, '_atomic', side_effect=external_edit):
            result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        result = self.manager.recover_xmx('1817230', plan['plan_id'], '')
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), pe64())
        self.assertIsNotNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_mid_restore_external_edit_is_not_overwritten_by_a_later_operation(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original before enable')
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        unlink = self.manager._unlink
        def edit_after_first(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if Path(path).name == 'intel_icd.json':
                target.write_bytes(b'user edit during restore')
            return result
        with patch.object(self.manager, '_unlink', side_effect=edit_after_first):
            result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'user edit during restore')
        self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'recovery_required')

    def test_plugin_rpc_unavailable_service_returns_error_instead_of_raising(self):
        import importlib.util
        import types
        stub = types.ModuleType('decky')
        with patch.dict(sys.modules, decky=stub):
            spec = importlib.util.spec_from_file_location('xmx_test_unavailable', ROOT/'main.py')
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        plugin = module.Plugin()
        result = asyncio.run(plugin.get_xmx_status('1817230'))
        self.assertFalse(result['ok'])
        self.assertIsInstance(result['error'], str)

    def test_root_home_is_not_accepted_as_target_user_for_prefix_writes(self):
        from optiscaler import steam
        manager = xmx.Manager('/usr', self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        with patch.object(steam, 'library_folders', return_value=[self.library]):
            with self.assertRaisesRegex(ValueError, 'user|owner|root'):
                manager._prefix('1817230')

    def test_enable_requires_native_game_xess_not_optiscaler_translation(self):
        (self.game/'libxess.dll').unlink()
        status = self.manager.get_xmx_status('1817230')
        self.assertFalse(status['native_xess']['found'])
        self.assertFalse(status['native_xess']['compatibility_verified'])
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertIn('native', result['error'].lower())

    def test_hardlinked_lock_is_refused_without_chmodding_the_other_name(self):
        victim = self.home/'unrelated-user-file'
        victim.write_bytes(b'keep bytes and permissions')
        victim.chmod(0o644)
        self.manager.root.mkdir(parents=True)
        os.link(victim, self.manager.root/'.lock')
        result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)
        self.assertEqual(victim.read_bytes(), b'keep bytes and permissions')
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_lock_is_explicitly_owned_by_target_user_for_root_backend_compatibility(self):
        observed = []
        chown = os.fchown
        def record(fd, uid, gid):
            observed.append((os.readlink('/proc/self/fd/'+str(fd)), uid, gid))
            return chown(fd, uid, gid)
        with patch.object(xmx.os, 'fchown', side_effect=record):
            with self.manager._lock():
                pass
        self.assertIn((str(self.settings/'xess-xmx/.lock'), self.home.stat().st_uid,
                       self.home.stat().st_gid), observed)

    def test_disable_process_death_before_restore_rename_recovers_recorded_original_stage(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original with exact metadata for staged restore')
        target.chmod(0o640)
        os.setxattr(target, 'user.restore-stage', b'original')
        old = xmx.snapshot(target)
        enabled = self.enable()
        self.default_acl(self.system32)
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        pid = os.fork()
        if pid == 0:
            replace = os.replace
            def die_before_rename(source, destination, *args, **kwargs):
                if Path(destination).name == target.name:
                    os._exit(73)
                return replace(source, destination, *args, **kwargs)
            with patch.object(xmx.os, 'replace', side_effect=die_before_rename):
                self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        record = self.manager._load(self.manager._app('1817230')/'journal.json')
        stage = next(item['temporary'] for item in record['operations'] if item['target'] == str(target))
        self.assertTrue(xmx.same(xmx.snapshot(stage), old))
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        result = fresh.recover_xmx('1817230', plan['plan_id'], plan['desired_launch_options'], 'resume')
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(target), old))
        self.assertFalse(Path(stage).exists())
        self.assertEqual(fresh.get_xmx_status('1817230')['phase'], 'disabled')

    def test_real_process_death_before_atomic_rename_leaves_recoverable_staging_file(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        pid = os.fork()
        if pid == 0:
            rename = os.replace
            def die_before_rename(source, destination, *args, **kwargs):
                if Path(destination).name == 'igdext64.dll':
                    os._exit(73)
                return rename(source, destination, *args, **kwargs)
            with patch.object(xmx.os, 'replace', side_effect=die_before_rename):
                self.manager.apply_xmx('1817230', plan['plan_id'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        self.assertEqual(manager.get_xmx_status('1817230')['phase'], 'recovery_required')
        result = manager.recover_xmx('1817230', plan['plan_id'], '')
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.system32/'driverstore').exists())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_apply_rechecks_target_user_package_access_before_prefix_writes(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        access = self.manager._user_access
        def revoked(path, **kwargs):
            if Path(path) == self.package/'tools/cm-compile.sh':
                raise ValueError('package access revoked for target user')
            return access(path, **kwargs)
        with patch.object(self.manager, '_user_access', side_effect=revoked):
            result = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertIsNone(self.manager.get_xmx_status('1817230')['pending'])

    def test_finish_rechecks_target_user_package_access_before_commit(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        access = self.manager._user_access
        def revoked(path, **kwargs):
            if Path(path) == self.package/'tools/cm-compile.sh':
                raise ValueError('package access revoked while awaiting confirmation')
            return access(path, **kwargs)
        with patch.object(self.manager, '_user_access', side_effect=revoked):
            result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertFalse(self.manager.get_xmx_status('1817230')['installed'])
        self.assertTrue(self.manager.recover_xmx('1817230', plan['plan_id'], '')['ok'])

    def test_crash_after_rename_recovers_files_with_inherited_xattrs(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        pid = os.fork()
        if pid == 0:
            sync, replace = os.fsync, os.replace
            def inherited(fd):
                path = Path(os.readlink('/proc/self/fd/'+str(fd)))
                if path.name.startswith('.decky-xmx-'):
                    os.setxattr(fd, 'user.inherited', b'fixture for inherited ACL/SELinux metadata')
                return sync(fd)
            def die_after_rename(source, destination, *args, **kwargs):
                result = replace(source, destination, *args, **kwargs)
                if Path(destination).name == 'igdext64.dll':
                    os._exit(73)
                return result
            with patch.object(xmx.os, 'fsync', side_effect=inherited), \
                    patch.object(xmx.os, 'replace', side_effect=die_after_rename):
                self.manager.apply_xmx('1817230', plan['plan_id'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        result = fresh.recover_xmx('1817230', plan['plan_id'], '')
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.system32/'driverstore').exists())
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_restart_abort_recovers_after_each_managed_enable_rename(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original for crash matrix')
        original.chmod(0o640)
        os.setxattr(original, 'user.matrix', b'preserve')
        before = xmx.snapshot(original)
        for name in ('igdext64.dll', 'ControlLib.dll', 'launch.sh', 'xess.conf', 'intel_icd.json'):
            with self.subTest(after_rename=name):
                plan = self.manager.plan_xmx('1817230', str(self.package), 'ORIGINAL=1 %command%')
                self.assertTrue(plan['ok'], plan)
                pid = os.fork()
                if pid == 0:
                    replace = os.replace
                    def die(source, destination, *args, **kwargs):
                        result = replace(source, destination, *args, **kwargs)
                        if Path(destination).name == name:
                            os._exit(73)
                        return result
                    with patch.object(xmx.os, 'replace', side_effect=die):
                        self.manager.apply_xmx('1817230', plan['plan_id'])
                    os._exit(74)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 73)
                self.manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
                result = self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
                self.assertTrue(result['ok'], result)
                self.assertTrue(xmx.same(xmx.snapshot(original), before))
                self.assertFalse((self.system32/'driverstore').exists())
                self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'disabled')

    def test_restart_resumes_enable_commit_after_installed_record_is_durable(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        pid = os.fork()
        if pid == 0:
            unlink = self.manager._unlink
            def die_before_journal_unlink(path, *args, **kwargs):
                if Path(path).name == 'journal.json':
                    os._exit(73)
                return unlink(path, *args, **kwargs)
            with patch.object(self.manager, '_unlink', side_effect=die_before_journal_unlink):
                self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        status = fresh.get_xmx_status('1817230')
        self.assertTrue(status['installed'])
        self.assertIsNotNone(status['pending'])
        result = fresh.recover_xmx('1817230', plan['plan_id'], plan['desired_launch_options'], 'resume')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(fresh.get_xmx_status('1817230')['pending'])
        self.assertTrue(fresh.get_xmx_status('1817230')['installed'])

    def test_package_change_while_waiting_for_client_confirmation_blocks_commit(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        (self.package/'tools/cmk_pack.py').write_text('# edited while waiting\n')
        result = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertFalse(self.manager.get_xmx_status('1817230')['installed'])
        self.assertTrue(self.manager.recover_xmx('1817230', plan['plan_id'], '')['ok'])

    def test_target_user_access_honors_named_acl_denial_even_when_mode_mask_allows_read(self):
        path = self.package/'tools/cmk_pack.py'
        uid = self.home.stat().st_uid
        entries = [(1, 6, 0xffffffff), (2, 0, uid), (4, 4, 0xffffffff),
                   (16, 4, 0xffffffff), (32, 4, 0xffffffff)]
        acl = struct.pack('<I', 2)+b''.join(struct.pack('<HHI', *entry) for entry in entries)
        os.setxattr(path, 'system.posix_acl_access', acl)
        stat_path = Path.stat
        def root_owned(current, *args, **kwargs):
            result = stat_path(current, *args, **kwargs)
            if current == path:
                values = list(result)
                values[4] = 0  # Model a root-owned package read by a root Decky backend.
                return os.stat_result(values)
            return result
        with patch.object(Path, 'stat', root_owned):
            with self.assertRaisesRegex(ValueError, 'accessible|ACL'):
                self.manager._user_access(path, read=True)

    def test_existing_destination_directories_require_target_user_traversal(self):
        directory = self.system32/'driverstore/filerepository/igd_faux.inf_1'
        directory.mkdir(parents=True)
        access = self.manager._user_access
        def inaccessible(path, **kwargs):
            if Path(path) == directory:
                raise ValueError('destination is root-readable but not traversable by target user')
            return access(path, **kwargs)
        with patch.object(self.manager, '_user_access', side_effect=inaccessible):
            result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertFalse((directory/'igdext64.dll').exists())

    def test_root_backend_refuses_settings_not_traversable_by_target_user(self):
        access = self.manager._user_access
        def inaccessible_state(path, **kwargs):
            if Path(path).is_relative_to(self.settings):
                raise ValueError('settings ancestor not accessible by target user')
            return access(path, **kwargs)
        with patch.object(self.manager, '_user_access', side_effect=inaccessible_state):
            result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_real_process_death_during_disable_is_resumable_not_abortable(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original before enable')
        original.chmod(0o640)
        enabled = self.enable('BEFORE=1 %command%')
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        pid = os.fork()
        if pid == 0:
            rename = os.replace
            def die_before_restore(source, destination, *args, **kwargs):
                if Path(destination).name == 'ControlLib.dll':
                    os._exit(73)
                return rename(source, destination, *args, **kwargs)
            with patch.object(xmx.os, 'replace', side_effect=die_before_restore):
                self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        self.assertEqual(manager.get_xmx_status('1817230')['phase'], 'recovery_required')
        self.assertFalse(manager.recover_xmx('1817230', plan['plan_id'],
                                            plan['old_launch_options'])['ok'])
        result = manager.recover_xmx('1817230', plan['plan_id'], plan['desired_launch_options'], 'resume')
        self.assertTrue(result['ok'], result)
        self.assertEqual(original.read_bytes(), b'original before enable')
        self.assertEqual(original.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.system32/'driverstore').exists())
        self.assertEqual(manager.get_xmx_status('1817230')['phase'], 'disabled')

    def test_cancelling_pending_disable_leaves_enable_files_intact(self):
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        result = self.manager.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), pe64())
        self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'enabled')

    def test_selected_driver_cannot_be_a_hardlink_to_the_dispatcher(self):
        driver = self.package/'lib/mesa-test/libvulkan_intel.so'
        driver.unlink()
        os.link(self.package/'lib/libvulkan_intel.so', driver)
        package = self.manager.get_xmx_status('1817230')['package']
        self.assertFalse(package['valid'], package)
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])

    def test_selector_file_layout_and_internal_symlinks_are_accepted(self):
        (self.package/'lib/active').unlink()
        (self.package/'lib/mesa-test').rename(self.package/'lib/mesa-26.3.0-rc2')
        (self.package/'lib/active').write_text('26.3.0-rc2\n')
        status = self.manager.get_xmx_status('1817230')
        self.assertTrue(status['package']['valid'], status)
        self.assertTrue(status['package']['compiler_validated'])

    def test_tampered_restore_metadata_cannot_request_privileged_bits_groups_or_capabilities(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original')
        enabled = self.enable()
        manifest = self.settings/'xess-xmx/apps/1817230/installed.json'
        before = manifest.read_text()
        unsafe = [('mode', 0o4644), ('gid', 2147483647),
                  ('xattrs', {'security.capability': 'AQAAAA=='})]
        for key, value in unsafe:
            with self.subTest(key=key):
                record = json.loads(before)
                next(item['old'] for item in record['operations'] if item['old'])[key] = value
                manifest.write_text(json.dumps(record))
                result = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
                self.assertFalse(result['ok'], result)
                self.assertEqual(original.read_bytes(), pe64())
        manifest.write_text(before)
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(plan['ok'], plan)

    def test_prefix_backup_creation_preserves_preexisting_and_late_conflicts(self):
        target = self.system32/'ControlLib.dll'
        target.write_bytes(b'original user DLL')
        target.chmod(0o640)
        os.setxattr(target, 'user.backup-regression', b'preserve')
        old = xmx.snapshot(target)
        for during_staging in (False, True):
            with self.subTest(during_staging=during_staging):
                plan = self.manager.plan_xmx('1817230', str(self.package), '')
                self.assertTrue(plan['ok'], plan)
                backup = self.manager._app('1817230')/'backups'/plan['plan_id']/'1'
                before = []
                def obstruction(_=None):
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(b'unknown existing backup; do not overwrite')
                    before.append(xmx.snapshot(backup))
                if not during_staging:
                    obstruction()
                atomic = self.manager._atomic
                def race(path, *args, **kwargs):
                    if during_staging and Path(path) == backup:
                        kwargs['before_replace'] = obstruction
                    return atomic(path, *args, **kwargs)
                try:
                    with patch.object(self.manager, '_atomic', side_effect=race):
                        result = self.manager.apply_xmx('1817230', plan['plan_id'])
                    self.assertFalse(result['ok'], result)
                    self.assertEqual(len(before), 1)
                    self.assertTrue(xmx.same(xmx.snapshot(backup), before[0]))
                    self.assertTrue(xmx.same(xmx.snapshot(target), old))
                    self.assertTrue((self.manager._app('1817230')/'journal.json').exists())
                finally:
                    result = self.manager.recover_xmx('1817230', plan['plan_id'], '')
                    self.assertTrue(result['ok'], result)

    def test_corrupt_original_backup_blocks_disable_without_removing_owned_files(self):
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original')
        enabled = self.enable()
        record = json.loads((self.settings/'xess-xmx/apps/1817230/installed.json').read_text())
        backup = next(item['backup'] for item in record['operations'] if item['old'])
        Path(backup).write_bytes(b'corrupt backup')
        result = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertFalse(result['ok'], result)
        self.assertEqual(original.read_bytes(), pe64())
        self.assertEqual(Path(backup).read_bytes(), b'corrupt backup')

    def test_invalid_identifiers_and_busy_lock_fail_closed(self):
        for bad in ('', '0', '001', '../1817230', '1817230/..', ' 1817230', 1817230, True):
            with self.subTest(appid=bad):
                self.assertFalse(self.manager.get_xmx_status(bad)['ok'])
                self.assertFalse(self.manager.plan_xmx(bad, str(self.package), '')['ok'])
        with self.manager._lock():
            result = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertFalse(result['ok'], result)
        self.assertFalse((self.system32/'ControlLib.dll').exists())

    def test_backend_never_changes_steam_vdf_or_native_game_dll(self):
        config = self.library/'userdata/1000/config/localconfig.vdf'
        config.parent.mkdir(parents=True)
        vdf = b'// exact user-owned Steam config\n"LaunchOptions" "DO NOT CHANGE"\n'
        config.write_bytes(vdf)
        dll = (self.game/'libxess.dll').read_bytes()
        enabled = self.enable()
        plan = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], '')['ok'])
        self.assertEqual(config.read_bytes(), vdf)
        self.assertEqual((self.game/'libxess.dll').read_bytes(), dll)

    def test_abort_preserves_unowned_empty_partial_and_complete_prefix_staging_files(self):
        for content in (b'', pe64()[:31], pe64()):
            with self.subTest(bytes=len(content)):
                plan = self.manager.plan_xmx('1817230', str(self.package), '')
                self.assertTrue(plan['ok'], plan)
                temporary = self.system32/('.decky-xmx-'+plan['plan_id']+'-1.tmp')
                temporary.write_bytes(content)
                before = xmx.snapshot(temporary)
                journal = self.manager._app('1817230')/'journal.json'
                try:
                    result = self.manager.apply_xmx('1817230', plan['plan_id'])
                    self.assertFalse(result['ok'], result)
                    managed = {p: xmx.snapshot(p) for p in
                               (self.system32/'ControlLib.dll',
                                self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll')}
                    fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
                    result = fresh.recover_xmx('1817230', plan['plan_id'], '')
                    self.assertFalse(result['ok'], result)
                    self.assertTrue(xmx.same(xmx.snapshot(temporary), before))
                    self.assertTrue(journal.exists())
                    for target, expected in managed.items():
                        self.assertTrue(xmx.same(xmx.snapshot(target), expected))
                finally:
                    # Removing the test-owned obstruction restores legitimate crash recovery.
                    temporary.unlink(missing_ok=True)
                    if journal.exists():
                        result = self.manager.recover_xmx('1817230', plan['plan_id'], '')
                        self.assertTrue(result['ok'], result)

    def test_prefix_staging_cleanup_preserves_edits_made_after_preflight(self):
        plan = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(plan['ok'], plan)
        pid = os.fork()
        if pid == 0:
            replace = os.replace
            def die_before_rename(source, destination, *args, **kwargs):
                if Path(destination).name == 'ControlLib.dll':
                    os._exit(73)
                return replace(source, destination, *args, **kwargs)
            with patch.object(xmx.os, 'replace', side_effect=die_before_rename):
                self.manager.apply_xmx('1817230', plan['plan_id'])
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        temporary = self.system32/('.decky-xmx-'+plan['plan_id']+'-1.tmp')
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        journal = fresh._app('1817230')/'journal.json'
        unlink = fresh._unlink
        raced = []
        def edit_before_unlink(path, *args, **kwargs):
            if Path(path) == temporary:
                temporary.write_bytes(b'user edit after staging cleanup preflight')
                raced.append(True)
            return unlink(path, *args, **kwargs)
        with patch.object(fresh, '_unlink', side_effect=edit_before_unlink):
            result = fresh.recover_xmx('1817230', plan['plan_id'], '')
        self.assertTrue(raced)
        self.assertFalse(result['ok'], result)
        self.assertEqual(temporary.read_bytes(), b'user edit after staging cleanup preflight')
        self.assertTrue(journal.exists())
        temporary.unlink()
        self.assertTrue(fresh.recover_xmx('1817230', plan['plan_id'], '')['ok'])

    def test_existing_staging_conflict_is_never_deleted_on_exclusive_open_failure(self):
        target = self.system32/'ControlLib.dll'
        temporary = self.system32/'.decky-xmx-conflicting.tmp'
        temporary.write_bytes(b'user file at conflicting temporary name')
        with self.assertRaises(FileExistsError):
            self.manager._atomic(target, b'new', temporary=temporary)
        self.assertTrue(temporary.exists())
        self.assertEqual(temporary.read_bytes(), b'user file at conflicting temporary name')
        self.assertFalse(target.exists())

    def test_plan_is_durable_read_only_and_preserves_options_verbatim(self):
        options = 'MANGOHUD=1 %command% --flag "a b"'
        result = self.manager.plan_xmx('1817230', str(self.package), options)
        self.assertTrue(result['ok'], result)
        self.assertRegex(result['plan_id'], r'^[0-9a-f]{32}$')
        self.assertEqual(result['old_launch_options'], options)
        self.assertTrue(result['desired_launch_options'].startswith('MANGOHUD=1 '))
        self.assertTrue(result['desired_launch_options'].endswith('%command% --flag \"a b\"'))
        self.assertEqual(len(result['operations']), 5)
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        plans = list(self.settings.rglob(result['plan_id']+'.json'))
        self.assertEqual(len(plans), 1)
        record = json.loads(plans[0].read_text())
        self.assertEqual(record['appid'], '1817230')
        self.assertEqual(record['phase'], 'planned')


if __name__ == '__main__':
    unittest.main(verbosity=2)
