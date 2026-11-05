#!/usr/bin/env python3
"""FSR->XeSS bridge transactions (M2) against fake homes and an injected runner.

The injected runner plays the part of the desktop user's shell: it records every
argv and replays the fake install.sh/uninstall.sh as bash. systemctl never runs.
"""
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_modules'))
from optiscaler import xmx, xmx_bridge, xmx_profiles

TAG = 'TESTTAG'
PKG_NAME = 'xess-xmx-linux-' + TAG
ICD_LINE = ('VK_DRIVER_FILES=/fake/run/intel_icd.json:/usr/share/vulkan/icd.d/radeon_icd.x86_64.json\n')

INSTALL_SH = r'''#!/bin/bash
set -u
mkdir -p "$HOME/.config/environment.d" "$HOME/.cache/xess-xmx"
{
echo "VK_DRIVER_FILES=/fake/run/intel_icd.json:/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"
echo "VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer,VK_KHR_present_wait2"
echo "ANV_CM_KERNEL_DIR=/fake/kernels"
echo "ANV_CM_HELPER=/fake/tools/cm-compile.sh"
echo "force_vk_vendor=0"
} > "$HOME/.config/environment.d/50-xess-xmx.conf"
for p in "$HOME"/.local/share/Steam/steamapps/common/Proton*/; do
  [ -f "$p/proton" ] || continue
  echo "# xess-xmx proton hook - written by install.sh" > "$p/user_settings.py"
done
echo "ok $(date -Iseconds) /fake/lib " > "$HOME/.cache/xess-xmx/driver-status"
echo installed
'''

UNINSTALL_SH = r'''#!/bin/bash
set -u
rm -f "$HOME/.config/environment.d/50-xess-xmx.conf"
for p in "$HOME"/.local/share/Steam/steamapps/common/Proton*/; do rm -f "$p/user_settings.py"; done
echo restored
'''


def prepared_package(home: Path, state_root: Path):
    package = home/PKG_NAME
    (package/'tools').mkdir(parents=True)
    (package/'install.sh').write_text(INSTALL_SH)
    (package/'uninstall.sh').write_text(UNINSTALL_SH)
    sha = lambda value: '0'*64
    receipt = {'provider': 'decky-optiscaler/xmx-package', 'schema': 1, 'tag': TAG,
               'archive_sha256': sha(0), 'path': str(package), 'runtime_verified': False,
               'tree_sha256': sha(1)}
    (package/'.decky-xmx-package.json').write_text(json.dumps(receipt))
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root/'package-preparation.json').write_text(json.dumps(
        {'provider': 'decky-optiscaler/xmx-package', 'schema': 1, 'state': 'ready',
         'package': {'tag': TAG, 'path': str(package), 'archive_sha256': sha(0),
                     'runtime_verified': False}, 'latest': None}))
    return package


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)/'home'
        self.home.mkdir()
        self.proton = self.home/'.local/share/Steam/steamapps/common/Proton-GE9'
        self.proton.mkdir(parents=True)
        (self.proton/'proton').write_text('#!/bin/sh\n')
        self.calls = []

        def run(argv, cwd=None, timeout=None):
            argv = [str(a) for a in argv]
            self.calls.append((argv, str(cwd) if cwd else None, timeout))
            if argv[:2] == ['systemctl', '--user']:
                if 'is-enabled' in argv:
                    return {'rc': 0, 'out': 'enabled\n'}
                return {'rc': 0, 'out': ''}
            script = Path(argv[-1])
            if script.name == 'install.sh':
                env = {**os.environ, 'HOME': str(self.home)}
                done = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
                return {'rc': done.returncode, 'out': done.stdout + done.stderr}
            if script.name == 'uninstall.sh':
                env = {**os.environ, 'HOME': str(self.home)}
                done = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
                return {'rc': done.returncode, 'out': done.stdout + done.stderr}
            raise AssertionError('unexpected command: %r' % argv)
        self.run = run
        self.manager = xmx.Manager(self.home, self.home/'.settings')

    def tearDown(self):
        self._tmp.cleanup()

    def journal(self):
        path = self.manager.root/xmx_bridge.JOURNAL
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # ---- install transaction -------------------------------------------------
    def test_install_refuses_without_prepared_package(self):
        result = xmx_bridge.install(self.manager, run=self.run)
        self.assertFalse(result['ok'])
        self.assertIn('package', result['error'].lower())
        self.assertEqual(self.calls, [])

    def test_install_runs_script_parks_conf_and_saves_env(self):
        package = prepared_package(self.home, self.manager.root)
        result = xmx_bridge.install(self.manager, run=self.run)
        self.assertTrue(result['ok'], result)
        # the package's own install.sh, run from its folder, never as root
        self.assertEqual(self.calls[0][0][-2:], ['bash', str(package/'install.sh')])
        self.assertEqual(self.calls[0][1], str(package))
        self.assertEqual(self.calls[0][2], 900)
        conf = self.home/'.config/environment.d/50-xess-xmx.conf'
        self.assertFalse(conf.exists(), 'the session file must not stay active')
        parked = Path(str(conf) + xmx_bridge.PARKED_SUFFIX)
        self.assertTrue(parked.is_file())
        env = (self.manager.root/'bridge.env').read_text()
        self.assertIn('VK_DRIVER_FILES=/fake/run/intel_icd.json', env)
        unset = [call for call in self.calls if 'unset-environment' in call[0]]
        self.assertEqual(len(unset), 1)
        for key in ('VK_DRIVER_FILES', 'VKD3D_DISABLE_EXTENSIONS', 'ANV_CM_KERNEL_DIR',
                    'ANV_CM_HELPER', 'force_vk_vendor'):
            self.assertIn(key, unset[0][0])
        steps = [entry['step'] for entry in self.journal()]
        self.assertIn('install', steps)
        self.assertIn('park', steps)

    def test_install_aborts_when_script_fails(self):
        package = prepared_package(self.home, self.manager.root)
        (package/'install.sh').write_text(INSTALL_SH + '\nexit 7\n')
        result = xmx_bridge.install(self.manager, run=self.run)
        self.assertFalse(result['ok'])
        conf = self.home/'.config/environment.d/50-xess-xmx.conf'
        # the script wrote the file but failed afterwards: nothing may be moved
        self.assertTrue(conf.exists())
        self.assertFalse(Path(str(conf) + xmx_bridge.PARKED_SUFFIX).exists())
        self.assertFalse((self.manager.root/'bridge.env').exists())
        self.assertEqual([e['step'] for e in self.journal()], ['install-failed'])

    def test_install_refuses_when_hook_missing(self):
        package = prepared_package(self.home, self.manager.root)
        (package/'install.sh').write_text(
            INSTALL_SH.replace('xess-xmx proton hook', 'someone elses settings'))
        result = xmx_bridge.install(self.manager, run=self.run)
        self.assertFalse(result['ok'])
        self.assertIn('hook', result['error'])
        conf = self.home/'.config/environment.d/50-xess-xmx.conf'
        self.assertTrue(conf.exists())
        self.assertFalse(Path(str(conf) + xmx_bridge.PARKED_SUFFIX).exists())

    # ---- state ---------------------------------------------------------------
    def test_state_reports_parked_hooks_and_driver_status(self):
        prepared_package(self.home, self.manager.root)
        self.assertTrue(xmx_bridge.install(self.manager, run=self.run)['ok'])
        state = xmx_bridge.state(self.manager, run=self.run)
        self.assertEqual(state['package']['tag'], TAG)
        self.assertEqual(state['session_conf'], 'parked')
        self.assertTrue(state['hooks']['installed'], state['hooks'])
        self.assertEqual(state['driver_status']['state'], 'ok')
        self.assertEqual(state['units']['xess-xmx-check.service'], 'enabled')

    def test_state_without_package(self):
        state = xmx_bridge.state(self.manager, run=self.run)
        self.assertIsNone(state['package'])
        self.assertFalse(state['hooks']['installed'])
        self.assertEqual(state['session_conf'], 'absent')

    # ---- per-game bridges and uninstall gate ---------------------------------
    def bridge_config(self, appid='1817230', line='export FSR=xess\n'):
        app = self.manager.root/'apps'/self.manager._appid(appid)
        app.mkdir(parents=True, exist_ok=True)
        (app/'xess.conf').write_text(line)
        return app

    def test_state_lists_active_bridges(self):
        prepared_package(self.home, self.manager.root)
        xmx_bridge.install(self.manager, run=self.run)
        self.bridge_config('1817230')
        self.bridge_config('3357650', 'export FSR_FG=xess\n')
        state = xmx_bridge.state(self.manager, run=self.run)
        self.assertEqual(sorted(state['bridges']), ['1817230', '3357650'])

    def test_state_bridge_profiles_expose_mode_and_sdk_per_game(self):
        prepared_package(self.home, self.manager.root)
        xmx_bridge.install(self.manager, run=self.run)
        self.bridge_config('1817230')
        self.bridge_config('3357650', 'export FSR_FG=xess\nexport XESS=latest\n')
        state = xmx_bridge.state(self.manager, run=self.run)
        self.assertEqual(state['bridge_profiles']['1817230'],
                         {'bridge': 1, 'sdk_latest': False, 'fg': 0})
        self.assertEqual(state['bridge_profiles']['3357650'],
                         {'bridge': 2, 'sdk_latest': True, 'fg': 0})

    def test_uninstall_requires_confirmation(self):
        result = xmx_bridge.uninstall(self.manager, 'yes', run=self.run)
        self.assertFalse(result['ok'])
        self.assertEqual(self.calls, [])

    def test_uninstall_refuses_while_a_bridge_is_active(self):
        prepared_package(self.home, self.manager.root)
        xmx_bridge.install(self.manager, run=self.run)
        self.bridge_config('1817230')
        result = xmx_bridge.uninstall(self.manager, xmx_bridge.CONFIRM, run=self.run)
        self.assertFalse(result['ok'])
        self.assertIn('1817230', result['error'])

    def test_uninstall_runs_script_and_clears_traces(self):
        package = prepared_package(self.home, self.manager.root)
        xmx_bridge.install(self.manager, run=self.run)
        parked = Path(str(self.home/'.config/environment.d/50-xess-xmx.conf') + xmx_bridge.PARKED_SUFFIX)
        self.assertTrue(parked.exists())
        result = xmx_bridge.uninstall(self.manager, xmx_bridge.CONFIRM, run=self.run)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.calls[-1][0][-2:], ['bash', str(package/'uninstall.sh')])
        self.assertFalse(parked.exists())
        self.assertFalse((self.manager.root/'bridge.env').exists())
        self.assertEqual(self.journal()[-1]['step'], 'uninstall')

    # ---- non-adoption report -----------------------------------------------------
    def _fake_game(self, appid, name, files):
        steamapps = self.home/'.local/share/Steam/steamapps'
        steamapps.mkdir(parents=True, exist_ok=True)
        (steamapps/('appmanifest_%s.acf' % appid)).write_text(
            '"appinfo" {\n  "appid" "%s"\n  "name" "%s"\n  "installdir" "%s"\n}\n' % (appid, name, name))
        game = steamapps/'common'/name
        for rel in files:
            target = game/rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'x')
        return game

    def test_state_reports_apps_and_unmanaged_games_never_touched(self):
        prepared_package(self.home, self.manager.root)
        self.bridge_config('1817230', 'export VK_DRIVER_FILES=/fake\n')  # managed, no bridge line
        self._fake_game('1817230', 'Managed Game', ['game.exe'])
        self._fake_game('3357650', 'Pragmata', ['pragmata.exe', 'amd_fidelityfx_loader_dx12.dll'])
        self._fake_game('999999', 'Xess Game', ['libxess.dll'])
        self._fake_game('888888', 'Plain Game', ['game.exe'])
        state = xmx_bridge.state(self.manager, run=self.run)
        self.assertEqual(state['apps'], ['1817230'])
        self.assertEqual(state['names']['1817230'], 'Managed Game')
        detected = {entry['appid']: entry for entry in state['detected']}
        self.assertEqual(detected['3357650']['marker'], 'fsr')
        self.assertEqual(detected['999999']['marker'], 'xess')
        self.assertNotIn('888888', detected)
        self.assertTrue(all(not entry['managed'] for entry in state['detected']))

    # ---- service-level toggle ------------------------------------------------




    def test_default_executor_drops_privileges_to_the_home_user(self):
        name = pwd.getpwuid(os.stat(self.home).st_uid).pw_name
        argv = xmx_bridge.user_command(name, ['true'], self.home)
        if os.geteuid() == 0:
            self.assertEqual(argv[:4], ['runuser', '-u', name, '--'])
        else:
            # Decky service plugins run AS the desktop user; runuser is then
            # impossible ("may not be used by non-root users") and pointless.
            self.assertEqual(argv[:2], ['/usr/bin/env', '-i'])
        joined = ' '.join(argv)
        self.assertIn('HOME=' + str(self.home), joined)
        self.assertNotIn('PATH=/root', joined)

    def test_user_command_from_root_uses_runuser(self):
        real = os.geteuid
        os.geteuid = lambda: 0
        try:
            argv = xmx_bridge.user_command('player', ['true'], self.home)
        finally:
            os.geteuid = real
        self.assertEqual(argv[:4], ['runuser', '-u', 'player', '--'])

    def test_user_command_from_a_foreign_user_refuses(self):
        name = pwd.getpwuid(os.stat(self.home).st_uid).pw_name
        if os.geteuid() in (0, os.stat(self.home).st_uid):
            self.skipTest('this uid legitimately owns the command path')
        with self.assertRaises(xmx_bridge.BridgeError):
            xmx_bridge.user_command('someone-else', ['true'], self.home)


if __name__ == '__main__':
    unittest.main(verbosity=2)
