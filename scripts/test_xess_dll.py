#!/usr/bin/env python3
"""Offline real-file DLL transactions. PE fixtures are inert, never executed."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_xmx as fixture
from test_xess_sdk import pe_fixture
from optiscaler import xmx, xess_sdk


class DllTests(unittest.TestCase):
    package: Path
    system32: Path
    setUpBase = fixture.XmxTests.setUp

    def setUp(self):
        self.setUpBase()
        self.target = self.game/'libxess.dll'
        self.original = pe_fixture((1, 2, 3, 4))
        self.candidate = pe_fixture()
        self.target.write_bytes(self.original)
        self.target.chmod(0o750)
        os.setxattr(self.target, 'user.dll-test', b'metadata')
        os.utime(self.target, ns=(1600000000000000000, 1600000000000000001))
        self.old = xmx.snapshot(self.target)
        self.cached = self.manager.root/'sdk-cache'/xess_sdk.SDK_TAG/'libxess.dll'
        self.cached.parent.mkdir(parents=True)
        self.cached.write_bytes(self.candidate)
        pins = patch.object(xess_sdk, 'DLL_SHA256', hashlib.sha256(self.candidate).hexdigest())
        self.sdk_pin = pins
        pins.start()
        self.addCleanup(pins.stop)

    def plan(self, action='enable', options='KEEP=1 %command%', adopt_trial=False):
        result = self.manager.plan_xess_dll('1817230', str(self.target), options, action, adopt_trial)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['old_launch_options'], options)
        self.assertEqual(result['desired_launch_options'], options)
        return result

    def test_retained_dll_preview_preserves_owner_appearing_after_preview(self):
        plan = self.plan()
        app = self.manager.root/'dll-apps/1817230'
        owner = app/'installed.json'
        owner.write_text('{"unexpected":"owner appeared after preview"}')
        before = xmx.snapshot(owner)
        generation = (app/'generation.json').read_bytes()
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(owner), before))
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual((app/'generation.json').read_bytes(), generation)
        self.assertFalse((app/'journal.json').exists())

    def test_pending_dll_enable_preserves_new_owner_on_finish_and_abort(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.pending_owner_conflict(self, plan, 'dll')

    def test_dll_owner_commit_boundary_preserves_late_owner(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_boundary_conflict(self, plan, 'dll')

    def test_dll_owner_abort_delete_boundary_preserves_late_owner(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_boundary_conflict(self, plan, 'dll', 'abort')

    def test_dll_owner_appearing_during_managed_staging_prevents_apply_rename(self):
        fixture.owner_during_managed_stage(self, 'dll')

    def test_dll_owner_commit_death_can_resume_or_abort(self):
        for recovery in ('abort', 'resume'):
            with self.subTest(recovery=recovery):
                plan = self.plan()
                self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
                fixture.owner_commit_death(self, plan, 'dll', recovery)

    def test_dll_owner_delete_death_can_resume_disable(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        plan = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_commit_death(self, plan, 'dll')

    def test_dll_installed_owner_metadata_is_bounded_before_preview_consumption(self):
        fixture.bounded_owner_metadata(self, 'dll')

    def test_dll_disable_revalidates_retained_pending_and_delete_boundary_owners(self):
        enabled = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', enabled['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', enabled['plan_id'], enabled['desired_launch_options'])['ok'])
        plan = self.plan('disable')
        fixture.retained_owner_conflict(self, plan, 'dll')
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.pending_owner_conflict(self, plan, 'dll')
        plan = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_boundary_conflict(self, plan, 'dll')

    def test_dll_trial_adoption_revalidates_retained_pending_and_durable_owners(self):
        manifest, backup = self.trial()
        external = manifest.read_bytes(), backup.read_bytes()
        immediate = xmx.snapshot(self.target)
        plan = self.plan(adopt_trial=True)
        fixture.retained_owner_conflict(self, plan, 'dll')
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.pending_owner_conflict(self, plan, 'dll')
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_commit_death(self, plan, 'dll', 'abort')
        self.assertTrue(xmx.same(xmx.snapshot(self.target), immediate))
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        fixture.owner_commit_death(self, plan, 'dll')
        plan = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual((manifest.read_bytes(), backup.read_bytes()), external)

    def test_unbound_old_dll_preview_is_refused_without_consuming_it(self):
        plan = self.plan()
        app = self.manager.root/'dll-apps/1817230'
        path = app/'plans'/(plan['plan_id']+'.json')
        record = json.loads(path.read_text())
        record.pop('installed_owner')
        path.write_text(json.dumps(record))
        generation = (app/'generation.json').read_bytes()
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertIn('replan', result['error'])
        self.assertEqual((app/'generation.json').read_bytes(), generation)
        self.assertFalse((app/'journal.json').exists())

    def test_dll_owner_change_during_abort_staging_preserves_managed_destination(self):
        fixture.owner_during_restore_stage(self, 'dll')

    def test_dll_owner_change_during_disable_staging_preserves_managed_destination(self):
        fixture.owner_during_restore_stage(self, 'dll', 'finish')

    def test_named_export_reordering_is_an_explicit_experimental_preview_not_a_false_incompatibility(self):
        moved = pe_fixture(names=('xessD3D12Init', 'xessGetVersion'))
        self.target.write_bytes(moved)
        before = xmx.snapshot(self.target)
        result = self.manager.plan_xess_dll('1817230', str(self.target), '')
        self.assertTrue(result['ok'], result)
        compatibility = result['operations'][0]['compatibility']
        self.assertTrue(compatibility['named_exports_preserved'])
        self.assertTrue(compatibility['changed_named_ordinals'])
        self.assertFalse(compatibility['ordinal_imports_verified'])
        self.assertFalse(compatibility['runtime_verified'])
        self.assertIn('ordinal', compatibility['warning'].lower())
        self.assertTrue(xmx.same(xmx.snapshot(self.target), before))
        self.assertFalse((self.manager.root/'dll-apps/1817230/journal.json').exists())

    def test_ordinal_only_function_identity_is_not_inferred_from_an_occupied_slot(self):
        self.target.write_bytes(pe_fixture((1, 2, 3, 4), ordinal_only=True))
        before = xmx.snapshot(self.target)
        for replacement in (pe_fixture(ordinal_only=True),
                            pe_fixture(names=('xessGetVersion', 'xessD3D12Init', 'newAPI'))):
            with self.subTest(replacement=hashlib.sha256(replacement).hexdigest()):
                self.cached.write_bytes(replacement)
                with patch.object(xess_sdk, 'DLL_SHA256', hashlib.sha256(replacement).hexdigest()):
                    result = self.manager.plan_xess_dll('1817230', str(self.target), '')
                self.assertFalse(result['ok'], result)
                self.assertIn('ordinal', result['error'].lower())
                self.assertTrue(xmx.same(xmx.snapshot(self.target), before))

    def test_tampered_persisted_payload_cannot_bypass_the_official_sdk_pin(self):
        import base64
        plan = self.plan()
        path = self.manager.root/'dll-apps/1817230/plans'/(plan['plan_id']+'.json')
        record = json.loads(path.read_text())
        forged = pe_fixture(version=(9, 9, 9, 9))
        record['operations'][0].update(data=base64.b64encode(forged).decode(),
                                        installed_sha256=hashlib.sha256(forged).hexdigest())
        path.write_text(json.dumps(record))
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertFalse((path.parent.parent/'journal.json').exists())

    def test_existing_staging_file_is_not_deleted_by_abort_after_exclusive_create_failure(self):
        plan = self.plan()
        temporary = self.target.parent/('.decky-xmx-'+plan['plan_id']+'-0.tmp')
        temporary.write_bytes(b'')
        before = xmx.snapshot(temporary)
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(temporary), before))
        self.assertTrue((self.manager.root/'dll-apps/1817230/journal.json').exists())

    def test_apply_preserves_an_existing_backup_as_a_conflict(self):
        plan = self.plan()
        backup = self.manager.root/'dll-apps/1817230/backups'/plan['plan_id']/'0'
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b'not this transaction; preserve me')
        before = xmx.snapshot(backup)
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(backup), before))
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertTrue((backup.parents[2]/'journal.json').exists())

    def restart(self):
        self.manager = xmx.Manager(self.home, self.settings, sysfs=self.sysfs, proc=self.root/'proc')

    def kill_at_target_rename(self, operation, after=False):
        pid = os.fork()
        if pid == 0:
            replace = os.replace
            def interrupted(source, destination, *args, **kwargs):
                if Path(destination).name != self.target.name:
                    return replace(source, destination, *args, **kwargs)
                if after:
                    replace(source, destination, *args, **kwargs)
                os._exit(73)
            with patch.object(xmx.os, 'replace', side_effect=interrupted):
                operation()
            os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        self.restart()

    def test_enable_process_death_before_rename_recovers_durable_staging(self):
        plan = self.plan()
        self.kill_at_target_rename(lambda: self.manager.apply_xess_dll('1817230', plan['plan_id']))
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue((self.target.parent/('.decky-xmx-'+plan['plan_id']+'-0.tmp')).exists())
        status = self.manager.get_xess_dll_status('1817230')
        self.assertEqual(status['phase'], 'recovery_required')
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertFalse(list(self.target.parent.glob('.decky-xmx-*.tmp')))

    def test_enable_process_death_after_rename_recovers_immediate_previous_state(self):
        plan = self.plan()
        self.kill_at_target_rename(lambda: self.manager.apply_xess_dll('1817230', plan['plan_id']), after=True)
        self.assertEqual(self.target.read_bytes(), self.candidate)
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))

    def test_restore_process_death_after_rename_resumes_with_exact_metadata_and_default_acl(self):
        fixture.XmxTests.default_acl(self, self.target.parent)
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.kill_at_target_rename(lambda: self.manager.finish_xess_dll(
            '1817230', restore['plan_id'], restore['desired_launch_options']), after=True)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertNotIn('system.posix_acl_access', xmx.snapshot(self.target)['xattrs'])
        self.assertFalse(self.manager.recover_xess_dll(
            '1817230', restore['plan_id'], restore['old_launch_options'])['ok'])
        result = self.manager.recover_xess_dll('1817230', restore['plan_id'],
                                              restore['desired_launch_options'], 'resume')
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.manager.get_xess_dll_status('1817230')['installed'])

    def test_restore_refuses_concurrent_edit_during_staging_and_retains_journal(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        atomic = xmx.Manager._atomic
        def racing(manager, path, data, *args, **kwargs):
            if Path(path) == self.target:
                kwargs['before_replace'] = lambda _: self.target.write_bytes(b'external updater edit')
            return atomic(manager, path, data, *args, **kwargs)
        with patch.object(xmx.Manager, '_atomic', racing):
            result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertEqual(self.target.read_bytes(), b'external updater edit')
        self.assertTrue((self.manager.root/'dll-apps/1817230/journal.json').exists())

    def test_prefix_disable_never_reverts_an_independently_managed_game_dll(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        prefix = fixture.XmxTests.enable(self)
        restore = self.manager.plan_xmx('1817230', '', prefix['desired_launch_options'], 'disable')
        self.assertTrue(restore['ok'], restore)
        self.assertTrue(self.manager.apply_xmx('1817230', restore['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xmx('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertEqual(self.target.read_bytes(), self.candidate)
        self.assertTrue(self.manager.get_xess_dll_status('1817230')['installed'])

    def test_metadata_only_changes_invalidate_the_preview_without_writing_game_files(self):
        for edit in (lambda: self.target.chmod(0o700),
                     lambda: os.setxattr(self.target, 'user.race', b'new'),
                     lambda: os.utime(self.target, ns=(self.old['atime_ns'], self.old['mtime_ns']+1))):
            with self.subTest(edit=edit):
                self.manager._atomic(self.target, self.original, self.old['mode'], self.old)
                plan = self.plan()
                edit()
                changed = xmx.snapshot(self.target)
                result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
                self.assertFalse(result['ok'], result)
                self.assertTrue(xmx.same(xmx.snapshot(self.target), changed))
                self.assertFalse((self.manager.root/'dll-apps/1817230/journal.json').exists())

    def test_corrupt_backup_prevents_restore_and_retains_owned_target_and_journal(self):
        plan = self.plan()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        before = xmx.snapshot(self.target)
        backup = self.manager.root/'dll-apps/1817230/backups'/plan['plan_id']/'0'
        backup.write_bytes(b'corrupt backup')
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), before))
        self.assertTrue((backup.parents[2]/'journal.json').exists())
        self.restart()
        self.assertEqual(self.manager.get_xess_dll_status('1817230')['pending']['plan_id'], plan['plan_id'])

    def test_apply_failure_after_durable_backup_recovers_without_changing_original(self):
        from optiscaler.xess_dll import DllManager
        plan = self.plan()
        atomic = DllManager._atomic
        def fail_after_backup(manager, path, *args, **kwargs):
            result = atomic(manager, path, *args, **kwargs)
            if Path(path).name == '0':
                raise OSError('injected failure after durable backup')
            return result
        with patch.object(DllManager, '_atomic', fail_after_backup):
            result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.restart()
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))

    def test_malformed_restore_timestamps_are_rejected_before_creating_a_journal(self):
        for value in ('not-a-timestamp', True, 1 << 80):
            with self.subTest(value=value):
                plan = self.plan()
                path = self.manager.root/'dll-apps/1817230/plans'/(plan['plan_id']+'.json')
                record = json.loads(path.read_text())
                record['operations'][0]['old']['atime_ns'] = value
                path.write_text(json.dumps(record))
                result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
                self.assertFalse(result['ok'], result)
                self.assertFalse((path.parent.parent/'journal.json').exists())
                self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))

    def test_dll_apply_preserves_each_falsey_unknown_journal_without_consuming_preview(self):
        plan = self.plan()
        journal = self.manager.root/'dll-apps/1817230/journal.json'
        generation = journal.parent/'generation.json'
        initial_generation = generation.read_bytes()
        for unknown in ('null', '{}', '[]', 'false', '0'):
            with self.subTest(unknown=unknown):
                journal.write_text(unknown)
                before = xmx.snapshot(journal)
                result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
                self.assertFalse(result['ok'], result)
                self.assertTrue(xmx.same(xmx.snapshot(journal), before))
                self.assertEqual(generation.read_bytes(), initial_generation)
                self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
                journal.unlink()
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])['ok'])

    def test_unknown_journals_block_both_scopes_without_being_consumed(self):
        prefix = self.manager.plan_xmx('1817230', str(self.package), '')
        dll = self.plan()
        for scope in ('apps', 'dll-apps'):
            journal = self.manager.root/scope/'1817230/journal.json'
            journal.parent.mkdir(parents=True, exist_ok=True)
            for unknown in ('null', '{}', '[]', 'false', '0', '"unknown"',
                            '{"schema":1,"phase":"pending_enable"}', '{bad json', 'dangling-symlink'):
                with self.subTest(scope=scope, unknown=unknown):
                    if unknown == 'dangling-symlink':
                        journal.symlink_to('missing-journal-target')
                    else:
                        journal.write_text(unknown)
                    identity = journal.lstat()
                    before = None if journal.is_symlink() else xmx.snapshot(journal)
                    try:
                        calls = [
                            lambda: self.manager.plan_xmx('1817230', str(self.package), ''),
                            lambda: self.manager.plan_xess_dll('1817230', str(self.target), ''),
                            lambda: self.manager.apply_xmx('1817230', prefix['plan_id']),
                            lambda: self.manager.apply_xess_dll('1817230', dll['plan_id']),
                        ]
                        for call in calls:
                            result = call()
                            self.assertFalse(result['ok'], result)
                        status = (self.manager.get_xmx_status if scope == 'apps' else
                                  self.manager.get_xess_dll_status)('1817230')
                        self.assertFalse(status['ok'], status)
                        recover = self.manager.recover_xmx if scope == 'apps' else self.manager.recover_xess_dll
                        token = prefix['plan_id'] if scope == 'apps' else dll['plan_id']
                        for action in ('abort', 'resume'):
                            result = recover('1817230', token, '', action)
                            self.assertFalse(result['ok'], result)
                        # Status in the unaffected scope remains retryable, not deadlocked.
                        other = (self.manager.get_xess_dll_status if scope == 'apps' else
                                 self.manager.get_xmx_status)('1817230')
                        self.assertTrue(other['ok'], other)
                        if unknown == 'dangling-symlink':
                            self.assertTrue(journal.is_symlink())
                            self.assertEqual(journal.lstat().st_ino, identity.st_ino)
                        else:
                            self.assertTrue(xmx.same(xmx.snapshot(journal), before))
                        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
                        self.assertFalse((self.system32/'ControlLib.dll').exists())
                    finally:
                        journal.unlink(missing_ok=True)
        # Missing really is absence: neither valid preview was consumed by refusals.
        self.assertTrue(self.manager.apply_xess_dll('1817230', dll['plan_id'])['ok'])
        self.assertTrue(self.manager.recover_xess_dll('1817230', dll['plan_id'], dll['old_launch_options'])['ok'])
        self.assertTrue(self.manager.apply_xmx('1817230', prefix['plan_id'])['ok'])
        self.assertTrue(self.manager.recover_xmx('1817230', prefix['plan_id'], '')['ok'])

    def test_unknown_journal_created_during_staging_is_not_overwritten_in_either_scope(self):
        for scope in ('prefix', 'dll'):
            with self.subTest(scope=scope):
                plan = (self.manager.plan_xmx('1817230', str(self.package), '') if scope == 'prefix'
                        else self.plan())
                journal = self.manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230/journal.json'
                atomic = xmx.Manager._atomic
                injected = []
                def race(manager, path, *args, **kwargs):
                    if Path(path) == journal and not injected:
                        def insert(_):
                            journal.write_text('null')
                            injected.append(True)
                        kwargs['before_replace'] = insert
                    return atomic(manager, path, *args, **kwargs)
                with patch.object(xmx.Manager, '_atomic', race):
                    result = (self.manager.apply_xmx if scope == 'prefix' else
                              self.manager.apply_xess_dll)('1817230', plan['plan_id'])
                self.assertTrue(injected)
                self.assertFalse(result['ok'], result)
                self.assertEqual(journal.read_text(), 'null')
                self.assertFalse((self.system32/'ControlLib.dll').exists())
                self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
                journal.unlink()

    def test_unknown_journal_replacement_at_recovery_deletion_is_preserved_in_either_scope(self):
        for scope in ('prefix', 'dll'):
            for operation in ('abort', 'finish'):
                with self.subTest(scope=scope, operation=operation):
                    plan = (self.manager.plan_xmx('1817230', str(self.package), '') if scope == 'prefix'
                            else self.plan(options=''))
                    apply = self.manager.apply_xmx if scope == 'prefix' else self.manager.apply_xess_dll
                    recover = self.manager.recover_xmx if scope == 'prefix' else self.manager.recover_xess_dll
                    finish = self.manager.finish_xmx if scope == 'prefix' else self.manager.finish_xess_dll
                    self.assertTrue(apply('1817230', plan['plan_id'])['ok'])
                    journal = self.manager.root/('apps' if scope == 'prefix' else 'dll-apps')/'1817230/journal.json'
                    saved = [journal.read_bytes()]
                    unlink = xmx.Manager._unlink
                    def race(manager, path, *args, **kwargs):
                        if Path(path) == journal:
                            # Preserve the latest authorized write-ahead state,
                            # including a staged/durable installed-owner commit.
                            saved[0] = journal.read_bytes()
                            journal.write_text('null')
                        return unlink(manager, path, *args, **kwargs)
                    try:
                        with patch.object(xmx.Manager, '_unlink', race):
                            result = (recover('1817230', plan['plan_id'], '') if operation == 'abort' else
                                      finish('1817230', plan['plan_id'], plan['desired_launch_options']))
                        self.assertFalse(result['ok'], result)
                        self.assertEqual(journal.read_text(), 'null')
                    finally:
                        # Restore only our test's known record so subsequent cases remain independent.
                        journal.write_bytes(saved[0])
                        self.assertTrue(recover('1817230', plan['plan_id'], '')['ok'])

    def test_cross_scope_pending_journals_block_planning_and_applying(self):
        prefix = self.manager.plan_xmx('1817230', str(self.package), '')
        dll = self.plan()
        self.assertTrue(self.manager.apply_xmx('1817230', prefix['plan_id'])['ok'])
        self.assertFalse(self.manager.plan_xess_dll('1817230', str(self.target), '')['ok'])
        result = self.manager.apply_xess_dll('1817230', dll['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(self.manager.recover_xmx('1817230', prefix['plan_id'], '')['ok'])
        prefix = self.manager.plan_xmx('1817230', str(self.package), '')
        self.assertTrue(self.manager.apply_xess_dll('1817230', dll['plan_id'])['ok'])
        self.assertFalse(self.manager.plan_xmx('1817230', str(self.package), '')['ok'])
        result = self.manager.apply_xmx('1817230', prefix['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(self.manager.recover_xess_dll('1817230', dll['plan_id'], dll['old_launch_options'])['ok'])
        self.assertTrue(self.manager.apply_xmx('1817230', prefix['plan_id'])['ok'])
        self.assertTrue(self.manager.recover_xmx('1817230', prefix['plan_id'], '')['ok'])

    def trial(self):
        from optiscaler import xess_dll
        self.target.unlink()
        self.target = self.game/'Engine/Plugins/Runtime/Intel/XeSS/Binaries/ThirdParty/Win64/libxess.dll'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.original)
        self.target.chmod(0o755)
        os.setxattr(self.target, 'user.trial', b'preserve')
        self.old = xmx.snapshot(self.target)
        directory = self.home/'.local/state/decky-xess-compat-tests/1817230/sdk-3.0.2'
        directory.mkdir(parents=True)
        backup = directory/'original-libxess.dll'
        self.manager._atomic(backup, self.original, self.old['mode'], self.old)
        manifest = {'appid': '1817230', 'target': str(self.target), 'backup': str(backup),
                    'original_sha256': hashlib.sha256(self.original).hexdigest(),
                    'candidate_sha256': hashlib.sha256(self.candidate).hexdigest(),
                    'sdk_tag': xess_sdk.SDK_TAG, 'sdk_archive_sha256': xess_sdk.SDK_ARCHIVE_SHA256,
                    'original_mode': self.old['mode'], 'original_atime_ns': self.old['atime_ns'],
                    'original_mtime_ns': self.old['mtime_ns'], 'original_xattrs': self.old['xattrs'],
                    'phase': 'installed'}
        path = directory/'manifest.json'
        path.write_text(json.dumps(manifest))
        self.manager._atomic(self.target, self.candidate, self.old['mode'],
                             dict(self.old, sha256=manifest['candidate_sha256']))
        pin = patch.object(xess_dll, 'TRIAL_ORIGINAL_SHA256', manifest['original_sha256'], create=True)
        pin.start()
        self.addCleanup(pin.stop)
        return path, backup

    def test_recognized_adoption_can_record_an_existing_trial_with_moved_named_ordinals(self):
        # This imports ownership of already-present pinned bytes; unlike an
        # update it makes no new API/ordinal compatibility claim or DLL change.
        self.original = pe_fixture((1, 2, 3, 4), names=('xessD3D12Init', 'xessGetVersion'))
        manifest, backup = self.trial()
        before = xmx.snapshot(self.target)
        self.assertTrue(self.manager.get_xess_dll_status('1817230')['trial_available'])
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), before))
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertTrue(manifest.exists())
        self.assertEqual(backup.read_bytes(), self.original)

    def test_trial_restore_metadata_cannot_be_forged_inside_a_persisted_plan(self):
        self.trial()
        plan = self.plan(adopt_trial=True)
        path = self.manager.root/'dll-apps/1817230/plans'/(plan['plan_id']+'.json')
        record = json.loads(path.read_text())
        record['restore_original']['old']['mode'] = 0o700
        record['trial']['original']['mode'] = 0o700
        path.write_text(json.dumps(record))
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse((path.parent.parent/'journal.json').exists())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)

    def test_persisted_adoption_cannot_drop_its_original_restore_provenance(self):
        self.trial()
        plan = self.plan(adopt_trial=True)
        path = self.manager.root/'dll-apps/1817230/plans'/(plan['plan_id']+'.json')
        record = json.loads(path.read_text())
        record.pop('trial')
        record.pop('restore_original')
        path.write_text(json.dumps(record))
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertFalse((path.parent.parent/'journal.json').exists())
        self.assertEqual(self.target.read_bytes(), self.candidate)

    def test_trial_commit_rechecks_original_provenance_after_apply(self):
        self.trial()
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        path = self.manager.root/'dll-apps/1817230/journal.json'
        record = json.loads(path.read_text())
        record['restore_original']['old']['mode'] = 0o700
        record['trial']['original']['mode'] = 0o700
        path.write_text(json.dumps(record))
        result = self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(path.exists())
        self.assertFalse((path.parent/'installed.json').exists())
        self.assertEqual(self.target.read_bytes(), self.candidate)

    def test_trial_adoption_crash_abort_keeps_previous_trial_then_restore_returns_original(self):
        manifest, backup = self.trial()
        external = manifest.read_bytes(), backup.read_bytes()
        immediate = xmx.snapshot(self.target)
        plan = self.plan(adopt_trial=True)
        self.kill_at_target_rename(lambda: self.manager.apply_xess_dll('1817230', plan['plan_id']), after=True)
        result = self.manager.recover_xess_dll('1817230', plan['plan_id'], plan['old_launch_options'])
        self.assertTrue(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(self.target), immediate))
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual((manifest.read_bytes(), backup.read_bytes()), external)

    def test_trial_import_preserves_an_existing_private_original_backup(self):
        self.trial()
        plan = self.plan(adopt_trial=True)
        backup = self.manager.root/'dll-apps/1817230/backups'/plan['plan_id']/'original'
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b'unknown private backup')
        before = xmx.snapshot(backup)
        result = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertFalse(result['ok'], result)
        self.assertTrue(xmx.same(xmx.snapshot(backup), before))
        self.assertEqual(self.target.read_bytes(), self.candidate)

    def test_explicit_recognized_trial_adoption_retains_external_backup_and_original_restore(self):
        manifest, backup = self.trial()
        trial_bytes = manifest.read_bytes(), backup.read_bytes()
        self.assertTrue(self.manager.get_xess_dll_status('1817230')['trial_available'])
        self.assertFalse(self.manager.plan_xess_dll('1817230', str(self.target), '')['ok'])
        plan = self.plan(adopt_trial=True)
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual((manifest.read_bytes(), backup.read_bytes()), trial_bytes)

    def test_plugin_exposes_both_scopes_and_explicit_prepare_off_thread_under_mutation_lock(self):
        import asyncio
        import importlib.util
        import logging
        import sys
        import threading
        import types
        from optiscaler.service import OptiScalerService
        with patch.dict(sys.modules, decky=types.ModuleType('decky')):
            spec = importlib.util.spec_from_file_location('dll_rpc_test', fixture.ROOT/'main.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        plugin = module.Plugin()
        plugin.service = OptiScalerService(fixture.ROOT, self.settings, self.root/'runtime',
                                          self.home, logging.getLogger('dll-rpc'))
        plugin.service.xmx = self.manager
        observations = []
        main_thread = threading.get_ident()
        prepare = xess_sdk.prepare_sdk
        def observed_prepare(manager):
            observations.append((threading.get_ident() != main_thread, plugin.service._mutation_lock.locked()))
            return prepare(manager)
        async def exercise():
            self.assertTrue((await plugin.get_xess_dll_status('1817230'))['ok'])
            self.assertTrue((await plugin.get_xmx_logs('1817230'))['ok'])
            profile_plan = await plugin.plan_xmx('1817230', str(self.package), '', profile={
                'driver': 'default', 'discovery': 'alternate', 'diagnostics': False})
            self.assertTrue(profile_plan['ok'], profile_plan)
            with patch.object(xess_sdk, 'prepare_sdk', side_effect=observed_prepare):
                prepared = await plugin.prepare_xess_sdk()
                self.assertTrue(prepared['ok'], prepared)
                self.assertTrue(prepared['cached'])
            plan = await plugin.plan_xess_dll('1817230', str(self.target), '')
            self.assertTrue(plan['ok'], plan)
            self.assertTrue((await plugin.apply_xess_dll('1817230', plan['plan_id']))['ok'])
            self.assertTrue((await plugin.recover_xess_dll('1817230', plan['plan_id'], ''))['ok'])
            plan = await plugin.plan_xess_dll('1817230', str(self.target), '')
            self.assertTrue((await plugin.apply_xess_dll('1817230', plan['plan_id']))['ok'])
            self.assertTrue((await plugin.finish_xess_dll('1817230', plan['plan_id'], ''))['ok'])
        asyncio.run(exercise())
        self.assertEqual(observations, [(True, True)])

    def test_all_dll_mutation_rpcs_drain_repeated_cancellation_before_unlocking(self):
        import asyncio
        import logging
        import threading
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(fixture.ROOT, self.settings, self.root/'runtime',
                                    self.home, logging.getLogger('dll-cancel'))
        service.xmx = self.manager
        methods = [('prepare_xess_sdk', ()),
                   ('plan_xess_dll', ('1817230', str(self.target), '')),
                   ('apply_xess_dll', ('1817230', 'a'*32)),
                   ('finish_xess_dll', ('1817230', 'a'*32, '')),
                   ('recover_xess_dll', ('1817230', 'a'*32, ''))]
        async def exercise(name, args, worker_raises):
            entered, release, queued_entered = threading.Event(), threading.Event(), threading.Event()
            def slow(*args):
                self.assertTrue(service._mutation_lock.locked())
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test worker release timed out')
                if worker_raises:
                    raise OSError('injected worker failure after cancellation')
                return {'ok': True}
            def queued():
                queued_entered.set()
                return {'ok': True}
            target = xess_sdk if name == 'prepare_xess_sdk' else service.xmx
            attr = 'prepare_sdk' if name == 'prepare_xess_sdk' else name
            with patch.object(target, attr, side_effect=slow):
                task = asyncio.create_task(getattr(service, name)(*args))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                next_task = asyncio.create_task(service._xmx_call(queued, mutation=True))
                try:
                    for _ in range(3):
                        task.cancel()
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        self.assertTrue(service._mutation_lock.locked())
                        self.assertFalse(queued_entered.is_set())
                        self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue((await next_task)['ok'])
                self.assertFalse(service._mutation_lock.locked())
        async def cases():
            for name, args in methods:
                for worker_raises in (False, True):
                    with self.subTest(method=name, worker_raises=worker_raises):
                        await exercise(name, args, worker_raises)
        asyncio.run(cases())

    @unittest.skipUnless(os.environ.get('XESS_SDK_TEST_ARCHIVE') and os.environ.get('XESS_DLL_TEST_ORIGINAL'),
                         'optional real pinned SDK and recognized original integration')
    def test_real_pinned_trial_adoption_and_original_restoration_offline(self):
        from optiscaler import xess_dll
        self.sdk_pin.stop()  # Real pins, not the inert-fixture override.
        self.original = Path(os.environ['XESS_DLL_TEST_ORIGINAL']).read_bytes()
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), xess_dll.TRIAL_ORIGINAL_SHA256)
        self.candidate = xess_sdk._archive_dll(Path(os.environ['XESS_SDK_TEST_ARCHIVE']).read_bytes())
        self.assertEqual(xess_sdk._verify_dll(self.candidate)['version'], '2.0.2.68')
        self.cached.write_bytes(self.candidate)
        manifest, backup = self.trial()
        external = manifest.read_bytes(), backup.read_bytes()
        with patch.object(xess_sdk, '_download_archive', side_effect=AssertionError('must remain offline')):
            plan = self.plan(adopt_trial=True)
            self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
            self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
            self.restart()
            restore = self.plan('disable')
            self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
            self.assertTrue(self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual((manifest.read_bytes(), backup.read_bytes()), external)
        print('REAL DLL TRIAL: original', xess_sdk.inspect_dll(self.target)['version'],
              'candidate', xess_sdk.DLL_VERSION, 'bytes', len(self.candidate))

    @unittest.skipUnless(os.environ.get('XESS_SDK_TEST_ARCHIVE') and os.environ.get('XESS_DLL_TEST_ORIGINAL'),
                         'optional real SDK update/restoration integration')
    def test_real_old_library_update_and_restore_offline(self):
        self.sdk_pin.stop()
        original = Path(os.environ['XESS_DLL_TEST_ORIGINAL']).read_bytes()
        candidate = xess_sdk._archive_dll(Path(os.environ['XESS_SDK_TEST_ARCHIVE']).read_bytes())
        xess_sdk._verify_dll(candidate)
        self.target.write_bytes(original)
        self.old = xmx.snapshot(self.target)
        self.cached.write_bytes(candidate)
        plan = self.plan()
        self.assertTrue(plan['operations'][0]['compatibility']['changed_named_ordinals'])
        self.assertTrue(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        self.assertEqual(xess_sdk.inspect_dll(self.target)['version'], '2.0.2.68')
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])['ok'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertEqual(self.target.read_bytes(), original)

    def test_falsey_existing_dll_owner_blocks_new_backup_lineage(self):
        path = self.manager.root/'dll-apps/1817230/installed.json'
        path.parent.mkdir(parents=True)
        before = xmx.snapshot(self.target)
        for value in (None, {}, [], False, 0, ''):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                self.assertFalse(self.manager.get_xess_dll_status('1817230')['ok'])
                self.assertFalse(self.manager.plan_xess_dll('1817230', str(self.target), '')['ok'])
                self.assertEqual(json.loads(path.read_text()), value)
                self.assertTrue(xmx.same(xmx.snapshot(self.target), before))

    def test_update_and_restore_preserve_metadata_and_require_exact_client_readback(self):
        status = self.manager.get_xess_dll_status('1817230')
        self.assertTrue(status['ok'], status)
        self.assertFalse(status['installed'])
        self.assertTrue(status['sdk']['ready'])
        self.assertEqual(status['candidates'][0]['version'], '1.2.3.4')
        plan = self.plan()
        self.assertEqual(self.target.read_bytes(), self.original)
        applied = self.manager.apply_xess_dll('1817230', plan['plan_id'])
        self.assertTrue(applied['ok'], applied)
        self.assertEqual(applied['action'], 'enable')
        self.assertEqual(applied['phase'], 'pending_enable')
        self.assertEqual(self.target.read_bytes(), self.candidate)
        replacement = xmx.snapshot(self.target)
        for key in ('mode', 'uid', 'gid', 'mtime_ns', 'xattrs'):
            self.assertEqual(replacement[key], self.old[key])
        self.assertFalse(self.manager.finish_xess_dll('1817230', plan['plan_id'], '')['ok'])
        self.assertTrue(self.manager.finish_xess_dll('1817230', plan['plan_id'], plan['desired_launch_options'])['ok'])
        restore = self.plan('disable')
        self.assertTrue(self.manager.apply_xess_dll('1817230', restore['plan_id'])['ok'])
        self.assertEqual(self.target.read_bytes(), self.candidate)
        done = self.manager.finish_xess_dll('1817230', restore['plan_id'], restore['desired_launch_options'])
        self.assertTrue(done['ok'], done)
        self.assertFalse(done['installed'])
        self.assertTrue(xmx.same(xmx.snapshot(self.target), self.old))
        self.assertFalse(self.manager.apply_xess_dll('1817230', plan['plan_id'])['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
