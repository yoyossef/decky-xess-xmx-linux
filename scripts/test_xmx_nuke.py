"""M3: red nuke button — escape-hatch uninstall that works in broken states."""
import json
import os
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_xmx import XmxTests  # noqa: E402

APPID = '1817230'


class XmxNukeTests(XmxTests):
    def nuke_app_dir(self):
        return self.manager.root/'apps'/APPID

    def plan(self, package=None):
        return self.manager.nuke_plan_xmx(APPID, str(package if package is not None else self.package))

    # ---- happy paths ----------------------------------------------------
    def test_nuke_removes_pure_creations_and_finishes_on_readback(self):
        enabled = self.enable()
        self.assertTrue((self.system32/'ControlLib.dll').exists())
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        actions = {Path(f['target']).name: f['action'] for f in plan['files']}
        self.assertEqual(actions['ControlLib.dll'], 'remove')
        self.assertEqual(actions['igdext64.dll'], 'remove')
        self.assertEqual(plan['desired_launch_options'], '')
        applied = self.manager.nuke_apply_xmx(APPID, plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertFalse((self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll').exists())
        finished = self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])
        self.assertTrue(finished['ok'], finished)
        self.assertFalse(self.nuke_app_dir().exists())
        backups = list((self.manager.root/'nuke-backups').glob('nuke-1817230-*.tar.gz'))
        self.assertEqual(len(backups), 1, 'expected one tar backup')
        with tarfile.open(backups[0]) as tf:
            self.assertTrue(any('installed.json' in member for member in tf.getnames()))
        status = self.manager.get_xmx_status(APPID)
        self.assertTrue(status['ok'])
        self.assertFalse(status['installed'])
        self.assertEqual(status['phase'], 'disabled')

    def test_nuke_restores_replaced_originals(self):
        (self.system32/'ControlLib.dll').write_bytes(b'preinstall shim')
        self.enable()
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(),
                         (self.package/'ControlLib.dll').read_bytes())
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        actions = {Path(f['target']).name: f['action'] for f in plan['files']}
        self.assertEqual(actions['ControlLib.dll'], 'restore')
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), b'preinstall shim')
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])['ok'])
        self.assertFalse(self.nuke_app_dir().exists())

    # ---- broken-state escape hatches -------------------------------------
    def test_nuke_leaves_externally_modified_file_but_still_completes(self):
        self.enable()
        owned = self.system32/'ControlLib.dll'
        owned.write_bytes(b'tampered by someone else')
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        actions = {Path(f['target']).name: f['action'] for f in plan['files']}
        self.assertEqual(actions['ControlLib.dll'], 'leave')
        self.assertTrue(any('ControlLib' in w for w in plan['warnings']), plan)
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertEqual(owned.read_bytes(), b'tampered by someone else',
                         'nuke must never delete files it cannot prove are ours')
        self.assertFalse((self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll').exists())
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])['ok'])

    def test_nuke_with_corrupt_journal_falls_back_to_disk_scan(self):
        enabled = self.enable()
        (self.nuke_app_dir()/'installed.json').write_text('{"truncat')
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(plan['warnings'], 'disk-scan mode must disclose itself')
        actions = {Path(f['target']): f['action'] for f in plan['files']}
        self.assertEqual(actions[self.system32/'ControlLib.dll'], 'remove')
        self.assertEqual(actions[self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll'], 'remove')
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])['ok'])
        status = self.manager.get_xmx_status(APPID)
        self.assertFalse(status['installed'])

    def test_nuke_sweeps_pending_transaction_from_broken_state(self):
        plan = self.manager.plan_xmx(APPID, str(self.package), '')
        self.assertTrue(self.manager.apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertTrue((self.nuke_app_dir()/'journal.json').exists())
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(plan['warnings'])
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])['ok'])
        status = self.manager.get_xmx_status(APPID)
        self.assertFalse(status['installed'])
        self.assertEqual(status['phase'], 'disabled')

    # ---- legacy ------------------------------------------------------------
    def _make_legacy(self, old_options='INTEL_JAY=all'):
        legacy = self.home/'.local/state/xess-xmx-anatase/abc123'
        igdext = self.system32/'driverstore/filerepository/igd_faux.inf_1/igdext64.dll'
        igdext.parent.mkdir(parents=True, exist_ok=True)
        igdext.write_bytes((self.package/'igdext64.dll').read_bytes())
        control = self.system32/'ControlLib.dll'
        control.write_bytes((self.package/'ControlLib.dll').read_bytes())
        wrapper = legacy/'launch.sh'
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text('#!/bin/sh\nexec "$@"\n')
        (legacy/'state.json').write_text(json.dumps({
            'prefix': str(self.prefix),
            'files': [
                {'target': str(igdext), 'existed': False, 'backup': str(legacy/'backups/0'),
                 'old_sha256': None,
                 'installed_sha256': (self.package/'igdext64.dll').read_bytes() and
                 __import__('hashlib').sha256((self.package/'igdext64.dll').read_bytes()).hexdigest()},
                {'target': str(control), 'existed': False, 'backup': str(legacy/'backups/1'),
                 'old_sha256': None,
                 'installed_sha256': __import__('hashlib').sha256(
                     (self.package/'ControlLib.dll').read_bytes()).hexdigest()},
            ],
            'wrapper': str(wrapper),
            'created_dirs': [str(igdext.parent), str(igdext.parent.parent), str(igdext.parent.parent.parent)],
            'launch': {'config': 'x', 'appid': APPID, 'old': old_options,
                       'installed': f'{old_options} {wrapper} %command%',
                       'backup': str(legacy/'localconfig.vdf.before-xmx')},
        }))
        return legacy

    def test_nuke_handles_legacy_install_and_parks_legacy_dir(self):
        legacy = self._make_legacy()
        self.assertTrue(self.manager.get_xmx_status(APPID)['legacy'])
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(plan['legacy'])
        self.assertEqual(plan['desired_launch_options'], 'INTEL_JAY=all')
        actions = {Path(f['target']).name: f['action'] for f in plan['files']}
        self.assertEqual(actions['ControlLib.dll'], 'remove')
        self.assertEqual(actions['igdext64.dll'], 'remove')
        self.assertEqual(actions['launch.sh'], 'remove')
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertFalse((self.system32/'ControlLib.dll').exists())
        self.assertFalse(legacy.exists())
        parked = list((self.manager.root/'nuke-backups').glob('legacy-abc123-*'))
        self.assertEqual(len(parked), 1)
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], 'INTEL_JAY=all')['ok'])
        status = self.manager.get_xmx_status(APPID)
        self.assertFalse(status['legacy'])

    # ---- guards ------------------------------------------------------------
    def test_nuke_apply_refuses_game_started_after_planning(self):
        self.enable()
        plan = self.plan()
        proc = self.root/'proc/456'
        proc.mkdir(parents=True)
        (proc/'environ').write_bytes(b'SteamAppId=1817230\0')
        result = self.manager.nuke_apply_xmx(APPID, plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertIn('running', result['error'].lower())
        self.assertTrue((self.system32/'ControlLib.dll').exists())

    def test_nuke_finish_refuses_wrong_readback_then_accepts_correct(self):
        enabled = self.enable()
        plan = self.plan()
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        refused = self.manager.nuke_finish_xmx(APPID, plan['plan_id'], 'some other string %command%')
        self.assertFalse(refused['ok'], refused)
        self.assertTrue((self.nuke_app_dir()/'journal.json').exists())
        self.assertTrue(self.manager.nuke_finish_xmx(
            APPID, plan['plan_id'], plan['desired_launch_options'])['ok'])

    def test_nuke_status_stays_available_when_installed_record_is_unreadable_json(self):
        self.enable()
        (self.nuke_app_dir()/'installed.json').write_text('}{ not json')
        status = self.manager.get_xmx_status(APPID)
        self.assertTrue(status['ok'], status)
        self.assertEqual(status['phase'], 'recovery_required')
        self.assertTrue(status['state_corrupt'])
        self.assertTrue(status['corruption_reasons'])
        # The escape hatch still functions from this state.
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        self.assertTrue(plan['warnings'])

    def test_nuke_clean_game_is_a_no_op_plan(self):
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['files'], [])
        self.assertEqual(plan['desired_launch_options'], '')
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertTrue(self.manager.nuke_finish_xmx(APPID, plan['plan_id'], '')['ok'])

    def test_nuke_disk_scan_refuses_files_that_match_nothing_ours(self):
        # Foreign ControlLib.dll that is neither journal-verified nor package bytes.
        (self.system32/'ControlLib.dll').write_bytes(b'foreign vendor binary')
        plan = self.plan()
        self.assertTrue(plan['ok'], plan)
        actions = {Path(f['target']).name: f['action'] for f in plan['files']}
        self.assertIn(actions.get('ControlLib.dll'), (None, 'leave'),
                      'foreign files are never removed, nor offered for removal')
        self.assertTrue(self.manager.nuke_apply_xmx(APPID, plan['plan_id'])['ok'])
        self.assertEqual((self.system32/'ControlLib.dll').read_bytes(), b'foreign vendor binary')


def load_tests(loader, standard_tests, pattern):
    # Subclassing XmxTests re-collects its inherited tests; keep only nuke tests.
    def flatten(suite):
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                yield from flatten(test)
            else:
                yield test
    return unittest.TestSuite(
        test for test in flatten(standard_tests)
        if test.id().rsplit('.', 1)[-1].startswith('test_nuke'))


if __name__ == '__main__':
    unittest.main()
