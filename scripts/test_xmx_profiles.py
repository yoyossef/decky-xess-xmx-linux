#!/usr/bin/env python3
"""Per-game profile transactions using the hardened real-filesystem harness."""
import base64
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_xmx import (XmxTests, elf64, owner_boundary_conflict, owner_commit_death,
                      pending_owner_conflict, retained_owner_conflict, xmx)

DEFAULT = {'driver': 'default', 'discovery': 'auto', 'diagnostics': False}
PROFILE = {'driver': '26.1', 'discovery': 'alternate', 'diagnostics': True}


class ProfileContractTests(unittest.TestCase):
    """Unit contract of the profile dictionary: frame generation multiplier."""
    def test_frame_generation_defaults_to_zero_and_accepts_two_to_eight(self):
        from optiscaler import xmx_profiles as P
        self.assertEqual(P.normalize(None), {**P.DEFAULT, 'fg': 0})
        self.assertEqual(P.normalize(dict(PROFILE))['fg'], 0)
        for fg in (0, 2, 3, 4, 5, 6, 7, 8):
            self.assertEqual(P.normalize({**PROFILE, 'fg': fg})['fg'], fg)

    def test_frame_generation_rejects_non_ints_one_and_out_of_range(self):
        from optiscaler import xmx_profiles as P
        for bad in (True, False, '3', 1, 0.0, 1.5, 9, -1, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                P.normalize({**PROFILE, 'fg': bad})
        with self.assertRaises(ValueError):
            P.normalize({**PROFILE, 'framegen': 3})

    def test_trial_recognition_recognises_frame_generation_prefixes(self):
        from optiscaler import xmx_profiles as P
        managed = 'wrapper %command%'
        profile = P.recognize_trial('XESS_FG=3 '+managed, managed)
        self.assertEqual(profile['fg'], 3)
        profile = P.recognize_trial('XESS_FG=3 IGDEXT_XELL_LOG=1 '+managed, managed)
        self.assertEqual(profile['fg'], 3)
        profile = P.recognize_trial('MESA=26.1 XESS_FG=8 '+managed, managed)
        self.assertEqual(profile['fg'], 8)
        with self.assertRaises(ValueError):
            P.recognize_trial('XESS_FG=1 '+managed, managed)


class ProfileTests(unittest.TestCase):
    # Populated by the shared real-filesystem fixture below.
    root: Path
    home: Path
    game: Path
    package: Path
    prefix: Path
    system32: Path
    settings: Path
    sysfs: Path
    manager: xmx.Manager
    setUp = XmxTests.setUp
    enable = XmxTests.enable
    run_root_namespace = XmxTests.run_root_namespace

    def variant(self, version='26.1.2'):
        path = self.package/'lib'/('mesa-'+version)/'libvulkan_intel.so'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(elf64()+version.encode())
        return path

    def test_profile_owner_commit_death_abort_restores_exact_legacy_owner(self):
        self.variant()
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'preinstall original')
        before = xmx.snapshot(original)
        enabled = self.enable('FOO=bar %command% --keep')
        owner = self.manager._app('1817230')/'installed.json'
        legacy = json.loads(owner.read_text())
        legacy.pop('profile')  # Deployed schema-1 ownership remains readable.
        owner.write_text(json.dumps(legacy, indent=3)+'\n')
        owner.chmod(0o640)
        os.setxattr(owner, 'user.owner', b'previous owner metadata')
        os.utime(owner, ns=(1600000000000000000, 1600000000000000001))
        for action in ('configure', 'adopt_trial'):
            with self.subTest(action=action):
                live = ('MESA=26.1 ' if action == 'adopt_trial' else '')+enabled['desired_launch_options']
                plan = self.manager.plan_xmx('1817230', str(self.package), live, action, PROFILE)
                self.assertTrue(plan['ok'], plan)
                self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
                owner_commit_death(self, plan, recovery='abort')
        disable = self.manager.plan_xmx('1817230', '', enabled['desired_launch_options'], 'disable')
        self.assertTrue(disable['ok'], disable)
        self.assertEqual(disable['desired_launch_options'], enabled['old_launch_options'])
        self.assertTrue(self.manager.apply_xmx('1817230', disable['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', disable['plan_id'], disable['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(original), before))

    def test_all_owned_profile_actions_revalidate_retained_and_pending_owners(self):
        self.variant()
        enabled = self.enable()
        for action in ('configure', 'adopt_trial', 'disable'):
            with self.subTest(action=action):
                live = ('MESA=26.1 ' if action == 'adopt_trial' else '')+enabled['desired_launch_options']
                plan = self.manager.plan_xmx('1817230', str(self.package), live, action, PROFILE)
                self.assertTrue(plan['ok'], plan)
                retained_owner_conflict(self, plan)
                self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
                pending_owner_conflict(self, plan)

    def test_profile_owner_commit_and_rollback_boundaries_preserve_late_external_owner(self):
        self.variant()
        enabled = self.enable()
        for action in ('configure', 'adopt_trial'):
            with self.subTest(action=action):
                live = ('MESA=26.1 ' if action == 'adopt_trial' else '')+enabled['desired_launch_options']
                plan = self.manager.plan_xmx('1817230', str(self.package), live, action, PROFILE)
                self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
                owner_boundary_conflict(self, plan)
                plan = self.manager.plan_xmx('1817230', str(self.package), live, action, PROFILE)
                self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
                owner_commit_death(self, plan, recovery=None)
                owner_boundary_conflict(self, plan, completion='abort')

    def test_configure_commit_keeps_first_uninstall_backups_and_exact_launch(self):
        self.variant()
        original = self.system32/'ControlLib.dll'
        original.write_bytes(b'original shim with metadata')
        original.chmod(0o640)
        os.setxattr(original, 'user.profile', b'first')
        before = xmx.snapshot(original)
        enabled = self.enable('FOO=bar %command% --original')
        app = self.manager._app('1817230')
        baseline = self.manager._load(app/'installed.json')
        plan = self.manager.plan_xmx('1817230', str(self.package),
                                     enabled['desired_launch_options'], 'configure', PROFILE)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['old_launch_options'], plan['desired_launch_options'])
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertEqual(self.manager.get_xmx_status('1817230')['phase'], 'pending_configure')
        self.assertFalse(self.manager.finish_xmx('1817230', plan['plan_id'], 'wrong')['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        installed = self.manager._load(app/'installed.json')
        self.assertEqual(installed['old_launch_options'], baseline['old_launch_options'])
        for old, current in zip(baseline['operations'], installed['operations']):
            self.assertEqual(current['old'], old['old'])
            self.assertEqual(current['backup'], old['backup'])
        disable = self.manager.plan_xmx('1817230', '', plan['desired_launch_options'], 'disable')
        self.assertTrue(disable['ok'], disable)
        self.assertEqual(disable['desired_launch_options'], enabled['old_launch_options'])
        self.assertTrue(self.manager.apply_xmx('1817230', disable['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', disable['plan_id'], disable['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(original), before))

    def test_configure_abort_after_crash_restores_immediate_previous_owner(self):
        self.variant()
        enabled = self.enable()
        app = self.manager._app('1817230')
        baseline = self.manager._load(app/'installed.json')
        before = {i['target']: xmx.snapshot(i['target']) for i in baseline['operations']}
        plan = self.manager.plan_xmx('1817230', str(self.package), enabled['desired_launch_options'], 'configure', PROFILE)
        self.assertTrue(plan['ok'], plan)
        atomic = self.manager._atomic
        def crash(path, *args, **kwargs):
            atomic(path, *args, **kwargs)
            if Path(path) == app/'xess.conf':
                raise OSError('crash after configure rename')
        with patch.object(self.manager, '_atomic', side_effect=crash):
            self.assertFalse(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
        self.assertFalse(fresh.recover_xmx('1817230', plan['plan_id'], 'wrong')['ok'])
        result = fresh.recover_xmx('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['installed'])
        self.assertEqual(fresh._load(app/'installed.json'), baseline)
        for path, expected in before.items():
            self.assertTrue(xmx.same(xmx.snapshot(path), expected), path)

    def test_trial_promotion_normalizes_exact_prefix_then_can_abort_or_uninstall(self):
        self.variant()
        enabled = self.enable()
        app = self.manager._app('1817230')
        baseline = self.manager._load(app/'installed.json')
        trial = ("MESA=26.1 INTC_ALT_DRIVER_EXTENSIONS_PATH='C:\\windows\\system32\\driverstore\\filerepository\\igd_faux.inf_1' "
                 'PROTON_LOG=1 IGDEXT_TRACE=1 ANV_CM_DEBUG=1 '+enabled['desired_launch_options'])
        requested = dict(PROFILE, diagnostics=False)
        for bad in (trial+' --user-edit', 'FOO=1 '+trial, trial.replace('MESA=26.1', 'MESA=26.2'),
                    trial.replace('IGDEXT_TRACE=1', 'IGDEXT_TRACE=2')):
            self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), bad, 'adopt_trial', requested)['ok'])
        plan = self.manager.plan_xmx('1817230', str(self.package), trial, 'adopt_trial', requested)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['desired_launch_options'], enabled['desired_launch_options'])
        self.assertEqual(plan['old_launch_options'], trial)
        applied = self.manager.apply_xmx('1817230', plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.assertEqual(applied['phase'], 'pending_adopt_trial')
        self.assertNotIn('PROTON_LOG=', (app/'xess.conf').read_text())
        self.assertFalse(self.manager.recover_xmx('1817230', plan['plan_id'], enabled['desired_launch_options'])['ok'])
        self.assertTrue(self.manager.recover_xmx('1817230', plan['plan_id'], trial)['ok'])
        self.assertEqual(self.manager._load(app/'installed.json'), baseline)
        plan = self.manager.plan_xmx('1817230', str(self.package), trial, 'adopt_trial', requested)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        installed = self.manager._load(app/'installed.json')
        self.assertEqual(installed['old_launch_options'], '')
        self.assertEqual(installed['profile'], {**requested, 'fg': 0, 'bridge': 0, 'sdk_latest': False})
        disable = self.manager.plan_xmx('1817230', '', plan['desired_launch_options'], 'disable')
        self.assertTrue(self.manager.apply_xmx('1817230', disable['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', disable['plan_id'], '')['ok'])

    def test_deep_native_xess_candidate_is_not_silently_missed(self):
        (self.game/'libxess.dll').unlink()
        deep = self.game.joinpath(*('nested'+str(n) for n in range(10)), 'libxess.dll')
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b'detected candidate')
        self.assertIn(str(deep), self.manager._native_xess('1817230')['files'])

    def test_backup_directory_dll_is_not_a_game_candidate(self):
        (self.game/'libxess.dll').unlink()
        directory = self.game/'decky_optiscaler_backup_files'
        directory.mkdir()
        (directory/'libxess.dll').write_bytes(b'backup only')
        self.assertFalse(self.manager._native_xess('1817230')['found'])

    def test_log_reader_honors_target_user_access_even_if_backend_can_read(self):
        self.run_root_namespace('''
proton = home/'steam-1817230.log'
proton.write_text('PRIVATE ROOT-ONLY LOG')
proton.chmod(0o600)
assert proton.stat().st_uid == 0 and proton.read_text() == 'PRIVATE ROOT-ONLY LOG'
result = manager.get_xmx_logs('1817230')
assert result['ok'], result
entry = result['logs'][0]
assert not entry['exists'] and 'PRIVATE ROOT-ONLY LOG' not in entry['text'], entry
assert 'accessible' in entry['text'], entry
proton.chmod(0o644)
entry = manager.get_xmx_logs('1817230')['logs'][0]
assert entry['exists'] and entry['text'] == 'PRIVATE ROOT-ONLY LOG', entry
''')

    def test_real_process_death_recovers_each_profile_rename_without_losing_owner(self):
        self.variant()
        enabled = self.enable('FOO=bar %command% --keep')
        app = self.manager._app('1817230')
        baseline = self.manager._load(app/'installed.json')
        before = {item['target']: xmx.snapshot(item['target']) for item in baseline['operations']}
        trial = ('MESA=26.1 '+enabled['desired_launch_options'])
        for action in ('configure', 'adopt_trial'):
            live = trial if action == 'adopt_trial' else enabled['desired_launch_options']
            for target in before:
                with self.subTest(action=action, target=target):
                    plan = self.manager.plan_xmx('1817230', str(self.package), live, action, PROFILE)
                    self.assertTrue(plan['ok'], plan)
                    pid = os.fork()
                    if pid == 0:
                        atomic = self.manager._atomic
                        def exit_after_rename(path, *args, **kwargs):
                            atomic(path, *args, **kwargs)
                            if str(path) == target:
                                os._exit(73)
                        self.manager._atomic = exit_after_rename
                        self.manager.apply_xmx('1817230', plan['plan_id'])
                        os._exit(74)
                    _, code = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(code), 73)
                    fresh = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')
                    result = fresh.recover_xmx('1817230', plan['plan_id'], live)
                    self.assertTrue(result['ok'], result)
                    self.assertTrue(result['installed'])
                    self.assertEqual(fresh._load(app/'installed.json'), baseline)
                    for path, expected in before.items():
                        self.assertTrue(xmx.same(xmx.snapshot(path), expected), path)

    def test_falsey_existing_owner_is_not_treated_as_a_clean_install(self):
        enabled = self.enable()
        app = self.manager._app('1817230')
        path = app/'installed.json'
        original = path.read_bytes()
        before = {item['target']: xmx.snapshot(item['target'])
                  for item in json.loads(original)['operations']}
        for value in (None, {}, [], False, 0, ''):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                self.assertFalse(self.manager.get_xmx_status('1817230')['ok'])
                self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), enabled['desired_launch_options'])['ok'])
                self.assertEqual(json.loads(path.read_text()), value)
                for target, expected in before.items():
                    self.assertTrue(xmx.same(xmx.snapshot(target), expected))
        path.write_bytes(original)

    def test_profile_preview_exposes_exact_profile_driver_and_real_diff(self):
        driver = self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '', profile=PROFILE)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan.get('profile'), {**PROFILE, 'fg': 0, 'bridge': 0, 'sdk_latest': False})
        self.assertEqual(plan.get('selected_driver'), str(driver))
        self.assertTrue(all('before_sha256' in item and 'after_sha256' in item
                            for item in plan['operations']))

    def test_xell_latency_log_is_offered_as_low_latency_evidence(self):
        result = self.manager.get_xmx_logs('1817230')
        names = [entry['name'].lower() for entry in result['logs']]
        self.assertTrue(any('xell' in name and 'latency' in name for name in names), names)
        entry = next(entry for entry in result['logs'] if 'xell' in entry['name'].lower())
        self.assertEqual(entry['path'], str(self.prefix/'drive_c/igdext_xell.log'))
        self.assertFalse(entry['exists'])
        (self.prefix/'drive_c/igdext_xell.log').write_text('low latency: on\nauto: waited in 40 of 100 frames\n')
        entry = next(entry for entry in self.manager.get_xmx_logs('1817230')['logs']
                     if 'xell' in entry['name'].lower())
        self.assertTrue(entry['exists'])
        self.assertIn('low latency: on', entry['text'])

    def test_logs_keep_bounded_startup_and_tail_and_refuse_pipes_and_links(self):
        proton = self.home/'steam-1817230.log'
        proton.write_bytes(b'STARTUP\n'+b'x'*180000+b'\nTAIL\n')
        trace = self.prefix/'drive_c/igdext_trace.log'
        os.mkfifo(trace)
        compiler = self.home/'.cache/xess-xmx/compile.log'
        compiler.parent.mkdir(parents=True)
        compiler.symlink_to(proton)
        result = self.manager.get_xmx_logs('1817230')
        self.assertTrue(result['ok'], result)
        self.assertIs(result['runtime_verified'], False)
        self.assertEqual(len(result['logs']), 4)
        log = result['logs'][0]
        self.assertIn('STARTUP', log['text'])
        self.assertIn('TAIL', log['text'])
        self.assertTrue(log['truncated'])
        self.assertLess(len(log['text']), 70000)
        self.assertEqual(result['logs'][1]['text'][:1], '[')
        self.assertIn('shared', result['logs'][2]['name'].lower())
        self.assertNotIn('STARTUP', result['logs'][2]['text'])

    def test_profile_enable_uses_only_owned_config_and_explicit_real_driver(self):
        driver = self.variant()
        before = dict(os.environ)
        plan = self.manager.plan_xmx('1817230', str(self.package), '', profile=PROFILE)
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        done = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(done['ok'], done)
        app = self.manager._app('1817230')
        config = (app/'xess.conf').read_text()
        self.assertIn('export MESA=26.1\n', config)
        self.assertIn("export INTC_ALT_DRIVER_EXTENSIONS_PATH='C:\\windows\\system32\\driverstore\\filerepository\\igd_faux.inf_1'", config)
        for key in ('PROTON_LOG', 'IGDEXT_TRACE', 'ANV_CM_DEBUG'):
            self.assertIn('export '+key+'=1\n', config)
        self.assertEqual(json.loads((app/'intel_icd.json').read_text())['ICD']['library_path'], str(driver))
        status = self.manager.get_xmx_status('1817230')
        self.assertEqual(status['profile'], {**PROFILE, 'fg': 0, 'bridge': 0, 'sdk_latest': False})
        self.assertEqual([v['id'] for v in status['drivers']], ['default', '26.1'])
        self.assertTrue(all(v['valid'] for v in status['drivers']))
        self.assertEqual(dict(os.environ), before)

    def test_frame_generation_reaches_owned_config_and_preview(self):
        self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '', profile={**PROFILE, 'fg': 3})
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['profile']['fg'], 3)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        done = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(done['ok'], done)
        config = (self.manager._app('1817230')/'xess.conf').read_text()
        self.assertIn('export XESS_FG=3\n', config)
        self.assertIn('export IGDEXT_XELL_LOG=1\n', config)
        status = self.manager.get_xmx_status('1817230')
        self.assertTrue(status['ok'], status)
        self.assertEqual(status['profile']['fg'], 3)

    def test_frame_generation_zero_writes_neither_multiplier_nor_log_flag(self):
        driver = self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '', profile=PROFILE)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['profile']['fg'], 0)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        config = (self.manager._app('1817230')/'xess.conf').read_text()
        self.assertNotIn('XESS_FG', config)
        self.assertNotIn('IGDEXT_XELL_LOG', config)

    def test_bridge_full_reaches_owned_config_and_status(self):
        self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '',
                                     profile={**PROFILE, 'bridge': 1})
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['profile']['bridge'], 1)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        done = self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(done['ok'], done)
        config = (self.manager._app('1817230')/'xess.conf').read_text()
        self.assertIn('export FSR=xess\n', config)
        self.assertNotIn('export FSR_FG=xess\n', config)
        self.assertNotIn('export XESS=latest\n', config)
        # With the bridge on, XeLL evidence must flow even at the game's own multiplier.
        self.assertIn('export IGDEXT_XELL_LOG=1\n', config)
        status = self.manager.get_xmx_status('1817230')
        self.assertEqual(status['profile']['bridge'], 1)

    def test_bridge_fg_only_with_sdk_latest(self):
        self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '',
                                     profile={**PROFILE, 'bridge': 2, 'sdk_latest': True})
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        config = (self.manager._app('1817230')/'xess.conf').read_text()
        self.assertIn('export FSR_FG=xess\n', config)
        self.assertIn('export XESS=latest\n', config)
        self.assertNotIn('export FSR=xess\n', config)

    def test_bridge_off_writes_no_route_lines(self):
        self.variant()
        plan = self.manager.plan_xmx('1817230', str(self.package), '', profile=PROFILE)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['profile']['bridge'], 0)
        self.assertTrue(self.manager.apply_xmx('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        config = (self.manager._app('1817230')/'xess.conf').read_text()
        self.assertNotIn('export FSR=', config)
        self.assertNotIn('export FSR_FG=', config)
        self.assertNotIn('export XESS=', config)


class ProfileBridgeContractTests(unittest.TestCase):
    """Unit contract of the FSR->XeSS bridge profile keys (M2)."""
    def test_bridge_keys_default_off(self):
        from optiscaler import xmx_profiles as P
        self.assertEqual(P.DEFAULT['bridge'], 0)
        self.assertIs(P.DEFAULT['sdk_latest'], False)
        normalized = P.normalize(dict(PROFILE))
        self.assertEqual(normalized['bridge'], 0)
        self.assertIs(normalized['sdk_latest'], False)

    def test_bridge_accepts_off_full_and_fg_only(self):
        from optiscaler import xmx_profiles as P
        for bridge in (0, 1, 2):
            self.assertEqual(P.normalize({**PROFILE, 'bridge': bridge})['bridge'], bridge)
        self.assertIs(P.normalize({**PROFILE, 'sdk_latest': True})['sdk_latest'], True)

    def test_bridge_rejects_other_values(self):
        from optiscaler import xmx_profiles as P
        for bad in (True, False, '1', 1.5, 3, -1, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                P.normalize({**PROFILE, 'bridge': bad})
        for bad in ('yes', 1, 0, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                P.normalize({**PROFILE, 'sdk_latest': bad})

    def test_trial_recognition_recognises_bridge_prefixes(self):
        from optiscaler import xmx_profiles as P
        managed = 'wrapper %command%'
        self.assertEqual(P.recognize_trial('FSR=xess '+managed, managed)['bridge'], 1)
        self.assertEqual(P.recognize_trial('FSR_FG=xess '+managed, managed)['bridge'], 2)
        adopted = P.recognize_trial('XESS=latest FSR=xess '+managed, managed)
        self.assertEqual((adopted['bridge'], adopted['sdk_latest']), (1, True))
        adopted = P.recognize_trial('FSR=xess XESS_FG=3 IGDEXT_XELL_LOG=1 '+managed, managed)
        self.assertEqual((adopted['bridge'], adopted['fg']), (1, 3))



if __name__ == '__main__':
    unittest.main(verbosity=2)
