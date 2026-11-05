#!/usr/bin/env python3
"""Offline package-preparation tests. No Steam, game, or live HOME writes."""
import hashlib
import importlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_modules'))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.pkg = importlib.import_module('optiscaler.xmx_package')
        self.temp = tempfile.TemporaryDirectory(prefix='xmx-package-tests-', dir=os.environ['TMPDIR'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/'home'
        self.home.mkdir()
        self.manager = importlib.import_module('optiscaler.xmx').Manager(self.home, self.root/'settings')

    def release(self, tag='next-1', data=b'archive'):
        return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [{
            'name': f'xess-xmx-linux-{tag}.zip', 'size': len(data),
            'digest': 'sha256:'+hashlib.sha256(data).hexdigest(),
            'browser_download_url': f'https://github.com/HHHHanasak1/xess-xmx-linux/releases/download/{tag}/xess-xmx-linux-{tag}.zip'}]}

    def test_status_is_local_only_and_does_not_create_state(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            self.assertEqual(self.pkg.get_status(self.manager), {
                'ok': True, 'state': 'idle', 'package': None, 'latest': None})
        self.assertFalse(self.manager.root.exists())

    def test_latest_selects_the_exact_zip_and_requires_api_digest(self):
        release = self.release()
        release['assets'].append({'name': 'source.zip'})
        selected = self.pkg.select_release(release)
        self.assertEqual(selected['tag'], 'next-1')
        self.assertEqual(selected['archive_sha256'], hashlib.sha256(b'archive').hexdigest())
        for invalid in (None, '', 'sha256:abc', 'md5:'+'a'*64, 'sha256:'+'A'*64):
            release['assets'][0]['digest'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.pkg.select_release(release)


    def test_release_rejects_wrong_urls_duplicates_unstable_and_unbounded_sizes(self):
        for field, bad in [('tag_name', '../evil'), ('draft', True), ('prerelease', True)]:
            value = self.release(); value[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.pkg.select_release(value)
        for field, bad in [('browser_download_url', 'https://evil.test/x.zip'),
                           ('size', 0), ('size', True), ('size', 2**50)]:
            value = self.release(); value['assets'][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                self.pkg.select_release(value)
        value = self.release(); value['assets'] *= 2
        with self.assertRaises(ValueError): self.pkg.select_release(value)

    def test_transport_rejects_redirects_off_official_https_assets(self):
        from urllib.request import Request
        handler = self.pkg.OfficialRedirects()
        for url in ('http://github.com/a', 'https://evil.test/a',
                    'https://github.com@evil.test/a', 'https://github.com:444/a',
                    'https://github.com/another/repo', 'https://api.github.com/anything'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(Request('https://github.com/a'), None, 302, '', {}, url)
        accepted = handler.redirect_request(Request('https://github.com/a'), None, 302, '', {},
                                            'https://release-assets.githubusercontent.com/asset?sig=abc')
        self.assertEqual(accepted.host, 'release-assets.githubusercontent.com')

    def test_transport_budget_truncation_encoding_and_cancellation(self):
        response = io.BytesIO(b'abcdef'); response.headers = {'Content-Length': '6'}
        with patch('urllib.request.OpenerDirector.open', return_value=response) as network:
            self.assertEqual(self.pkg.read_url(self.pkg.LATEST_URL, 12), b'abcdef')
            self.assertNotIn('Authorization', network.call_args.args[0].headers)
        for payload, headers, limit in [(b'abc', {'Content-Length': '6'}, 12),
                                         (b'abcd', {}, 3),
                                         (b'abc', {'Content-Encoding': 'gzip'}, 12),
                                         (b'abc', {'Content-Length': '-1'}, 12)]:
            response = io.BytesIO(payload); response.headers = headers
            with self.subTest(headers=headers, limit=limit), patch('urllib.request.OpenerDirector.open', return_value=response):
                with self.assertRaises(ValueError): self.pkg.read_url(self.pkg.LATEST_URL, limit)
        import threading
        stop = threading.Event(); stop.set()
        with patch('urllib.request.OpenerDirector.open') as network, self.assertRaises(InterruptedError):
            self.pkg.read_url(self.pkg.LATEST_URL, 12, cancel=stop)
        network.assert_not_called()


    def archive(self, entries=None):
        import zipfile
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries or [('xess-xmx-linux-next-1/a', b'bytes')]:
                archive.writestr(name, data)
        return output.getvalue()

    def extract(self, data):
        return self.pkg.extract_archive(self.manager, data, self.pkg.select_release(self.release(data=data)), self.home/'extracted')

    def test_archive_digest_checked_before_any_extraction(self):
        data = self.archive()
        release = self.pkg.select_release(self.release(data=data)); release['archive_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.pkg.extract_archive(self.manager, data, release, self.home/'extracted')
        self.assertFalse((self.home/'extracted').exists())

    def test_archive_extracts_only_fresh_files_and_records_originals(self):
        originals = self.extract(self.archive())
        self.assertEqual((self.home/'extracted/a').read_bytes(), b'bytes')
        self.assertEqual(originals['a']['sha256'], hashlib.sha256(b'bytes').hexdigest())
        with self.assertRaises((ValueError, FileExistsError)):
            self.extract(self.archive())

    def test_archive_rejects_traversal_symlinks_duplicates_and_bombs(self):
        import stat, zipfile, warnings
        link = zipfile.ZipInfo('xess-xmx-linux-next-1/link'); link.external_attr = (stat.S_IFLNK|0o777)<<16
        for entries in [('../escape', b'x'), ('/absolute', b'x'),
                        ('xess-xmx-linux-next-1/a/../../escape', b'x'),
                        ('xess-xmx-linux-next-1/a\\b', b'x'),
                        ('other-package/a', b'x'), (link, b'/etc/passwd')]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.extract(self.archive([entries]))
        for entries in [[('xess-xmx-linux-next-1/a', b'x')]*2,
                        [('xess-xmx-linux-next-1/A', b'x'), ('xess-xmx-linux-next-1/a', b'x')],
                        [('xess-xmx-linux-next-1/a', b'x'), ('xess-xmx-linux-next-1/a/b', b'y')]]:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                data = self.archive(entries)
            with self.assertRaises(ValueError): self.extract(data)
        data = self.archive()
        with patch.object(self.pkg, 'MAX_EXTRACTED', 3), self.assertRaises(ValueError): self.extract(data)
        with patch.object(self.pkg, 'MAX_MEMBERS', 0), self.assertRaises(ValueError): self.extract(data)
        self.assertFalse((self.home/'extracted').exists())


    def package_archive(self):
        import stat, zipfile
        from test_xmx import pe64, elf64
        files = {'igdext64.dll': pe64(), 'ControlLib.dll': pe64(),
                 'lib/libvulkan_intel.so': elf64(), 'lib/mesa-test/libvulkan_intel.so': elf64(),
                 'lib/active': b'test', 'tools/cm-compile.sh': b'#!/bin/sh\n',
                 'xmx-launch.sh': b'#!/bin/sh\n', 'tools/cmk_pack.py': b'# inert',
                 'lib/bundled/libSPIRV-Tools.so': elf64(), 'lib/bundled/libdisplay-info.so.3': elf64()}
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, data in files.items():
                info = zipfile.ZipInfo('xess-xmx-linux-next-1/'+name)
                info.external_attr = (stat.S_IFREG | (0o755 if name.endswith('.sh') else 0o644)) << 16
                archive.writestr(info, data)
        return output.getvalue()

    def fake_dependencies(self, manager, staging, cancel, deadline, **extra):
        from test_xmx import pe64, elf64
        package = staging/'work/package'
        for relative in ('igc/igc/local/lib/libigc.so', 'igc/igc/local/lib/libiga64.so',
                         'igc/neo/lib/x86_64-linux-gnu/libocloc.so', 'igc/neo/bin/ocloc',
                         'lib/deps/libSPIRV-Tools.so', 'lib/deps/libdisplay-info.so.3'):
            path = package/relative; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(elf64()); path.chmod(0o755)
        for relative in ('xess/libxess.dll', 'xess/libxess_dx11.dll', 'xess/libxess_fg.dll', 'xess/libxell.dll',
                         'ffx/amd_fidelityfx_loader_dx12.dll', 'ffx/amd_fidelityfx_upscaler_dx12.dll',
                         'ffx/amd_fidelityfx_framegeneration_dx12.dll'):
            path = package/relative; path.parent.mkdir(exist_ok=True); path.write_bytes(pe64())

    def prepare_fixture(self, dependencies=None):
        data = self.package_archive()
        def network(url, limit, **kwargs):
            return json.dumps(self.release(data=data)).encode() if url == self.pkg.LATEST_URL else data
        with patch.object(self.pkg, 'read_url', side_effect=network), patch.object(
                self.pkg, '_prepare_dependencies', side_effect=dependencies or self.fake_dependencies):
            return self.pkg.prepare(self.manager)

    def test_prepare_publishes_valid_new_package_without_touching_existing_versions(self):
        old = self.home/'xess-xmx-linux'; old.mkdir(); (old/'keep').write_text('running game')
        foreign = self.home/'xess-xmx-linux-next-1-'+Path('unused') if False else self.home/'xess-xmx-linux-next-1'
        foreign.mkdir(); (foreign/'keep').write_text('foreign')
        result = self.prepare_fixture()
        self.assertEqual(result['state'], 'ready', result)
        self.assertTrue(result['ok'])
        package = result['package']; self.assertFalse(package['runtime_verified'])
        target = Path(package['path']); self.assertEqual(target.parent, self.home)
        self.assertNotEqual(target, old); self.assertTrue(self.manager._package(target)['valid'])
        self.assertEqual((old/'keep').read_text(), 'running game')
        self.assertEqual((foreign/'keep').read_text(), 'foreign')
        self.assertEqual(self.pkg.get_status(self.manager), result)
        self.assertFalse(list(self.home.glob('.decky-xmx-stage-*')))
        self.assertEqual(json.loads((target/self.pkg.RECEIPT).read_text())['provider'], self.pkg.PROVIDER)

    def test_failed_or_interrupted_prepare_retains_previous_ready_and_cleans_only_own_staging(self):
        ready = self.prepare_fixture()
        self.assertEqual(ready['state'], 'ready', ready)
        sentinel = self.home/'.decky-xmx-stage-foreign'; sentinel.mkdir(); (sentinel/'keep').write_text('foreign')
        for failure in (RuntimeError('partial dependency failure'), InterruptedError('cancelled')):
            def fail(*args):
                self.fake_dependencies(*args)
                raise failure
            failed = self.prepare_fixture(fail)
            self.assertEqual(failed['state'], 'error', failed)
            self.assertEqual(failed['package'], ready['package'])
            self.assertTrue(Path(ready['package']['path']).exists())
        self.assertEqual(list(self.home.glob('.decky-xmx-stage-*')), [sentinel])
        self.assertEqual((sentinel/'keep').read_text(), 'foreign')

    def test_prepare_rejects_mutated_originals_and_symlink_escape_after_helpers(self):
        def changed(*args):
            self.fake_dependencies(*args)
            (args[1]/'work/package/igdext64.dll').write_bytes(b'changed')
        result = self.prepare_fixture(changed)
        self.assertEqual(result['state'], 'error', result)
        self.assertIn('original', result['error'].lower())
        def escaped(*args):
            self.fake_dependencies(*args)
            (args[1]/'work/package/escape').symlink_to(self.home)
        result = self.prepare_fixture(escaped)
        self.assertEqual(result['state'], 'error', result)
        self.assertFalse(list(self.home.glob('xess-xmx-linux-*')))

    def test_permissions_and_symlink_roots_refused_before_privileged_creation(self):
        self.home.chmod(0o500)
        with patch.object(self.pkg, 'read_url') as network:
            result = self.pkg.prepare(self.manager)
        self.assertEqual(result['state'], 'error', result)
        self.assertFalse(self.manager.root.exists()); network.assert_not_called()
        self.home.chmod(0o700)
        (self.root/'settings').symlink_to(self.home, target_is_directory=True)
        with patch.object(self.pkg, 'read_url') as network:
            result = self.pkg.prepare(self.manager)
        self.assertEqual(result['state'], 'error', result)
        self.assertFalse((self.home/'xess-xmx').exists()); network.assert_not_called()


    def sandbox_stage(self):
        stage = self.home/'sandbox-stage'
        stage.mkdir(); (stage/'work').mkdir(); (stage/'downloads').mkdir()
        return stage

    def test_sandbox_hides_host_home_and_network_and_uses_clean_user_environment(self):
        import time
        stage = self.sandbox_stage()
        (self.home/'secret').write_text('never expose')
        probe = ('import os,pathlib,socket; '
                 'assert os.getuid()!=0; '
                 'assert not pathlib.Path('+repr(str(self.home/'secret'))+').exists(); '
                 'assert os.environ.get("SECRET_TOKEN") is None; '
                 'assert os.environ.get("PYTHONPATH") is None; '
                 'assert os.environ["HOME"]=="/work/home"; '
                 'pathlib.Path("/work/probe").write_text(str(os.getuid())); '
                 'print("isolated")')
        with patch.dict(os.environ, {'SECRET_TOKEN': 'secret', 'PYTHONPATH': '/credential', 'LD_PRELOAD': '/evil.so'}):
            output = self.pkg._sandbox(self.manager, stage, ['/usr/bin/python3', '-I', '-c', probe], None, time.monotonic()+10)
        self.assertIn('isolated', output)
        self.assertEqual((stage/'work/probe').read_text(), str(self.home.stat().st_uid))
        # Offline sandbox: connect to loopback must fail, even if the host has services.
        code = 'import socket; s=socket.socket(); s.settimeout(1); assert s.connect_ex(("127.0.0.1",1))!=0'
        self.pkg._sandbox(self.manager, stage, ['/usr/bin/python3', '-I', '-c', code], None, time.monotonic()+10)

    def test_sandbox_fails_closed_when_unavailable_and_bounds_stdout_disk_and_deadline(self):
        import time
        stage = self.sandbox_stage()
        with patch.object(self.pkg.shutil, 'which', return_value=None), self.assertRaisesRegex(RuntimeError, 'bwrap|bubblewrap'):
            self.pkg._sandbox(self.manager, stage, ['/usr/bin/true'], None, time.monotonic()+10)
        for code, expected in [('print("x"*300000)', 'output'),
                               ('import time; time.sleep(20)', 'time'),
                               ('from pathlib import Path; Path("/work/huge").write_bytes(b"x"*5000); import time; time.sleep(2)', 'budget')]:
            with self.subTest(code=code), patch.object(self.pkg, 'MAX_WORKSPACE', 4096 if 'huge' in code else 1024*1024):
                with self.assertRaises((RuntimeError, TimeoutError)) as error:
                    self.pkg._sandbox(self.manager, stage, ['/usr/bin/python3', '-I', '-c', code], None,
                                      time.monotonic()+(0.3 if 'sleep(20)' in code else 10))
                self.assertIn(expected, str(error.exception).lower())

    def test_sandbox_cancellation_kills_descendants(self):
        import time, threading
        stage = self.sandbox_stage(); cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set); timer.start(); self.addCleanup(timer.cancel)
        command = ['/usr/bin/python3', '-I', '-c',
                   'import subprocess,time; subprocess.Popen(["/usr/bin/python3","-c","import time,pathlib; time.sleep(1); pathlib.Path(\'/work/escaped\').write_text(\'bad\')"]); time.sleep(20)']
        with self.assertRaises(InterruptedError):
            self.pkg._sandbox(self.manager, stage, command, cancel, time.monotonic()+10)
        time.sleep(1.1)
        self.assertFalse((stage/'work/escaped').exists())


    def dependency_helpers(self, stage):
        package = stage/'work/package'; (package/'tools').mkdir(parents=True)
        hashes = {}
        url = 'https://github.com/intel/xess/releases/download/v3.0.2/test.zip'
        scripts = {'tools/get-igc.sh': '#!/bin/bash\nset -eu\ncurl -sfL -o "$1/replayed" '+url+'\n',
                   'tools/get-xess.sh': '#!/bin/bash\nset -eu\ntest -s "$1/replayed"\n',
                   'tools/get-ffx.sh': '#!/bin/bash\nset -eu\ntest -s "$1/replayed"\n',
                   'tools/session-check.sh': '#!/bin/bash\nset -eu\ntest "$1" = --dry-run\nmkdir -p "$XDG_CACHE_HOME/test"\nexit 1\n'}
        for name, script in scripts.items():
            (package/name).write_text(script); hashes[name] = hashlib.sha256(script.encode()).hexdigest()
        return hashes, url

    def test_dependencies_are_pinned_then_replayed_offline_to_pinned_helpers(self):
        import time
        stage = self.sandbox_stage(); hashes, url = self.dependency_helpers(stage)
        payload = b'bounded official dependency fixture'
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(self.pkg, 'METADATA_URLS', ()), patch.object(
                self.pkg, 'DEPENDENCIES', ((url, hashlib.sha256(payload).hexdigest()),)), patch.object(
                self.pkg, 'read_url', return_value=payload) as network:
            self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+20)
        self.assertEqual((stage/'work/package/replayed').read_bytes(), payload)
        self.assertTrue((stage/'work/cache/test').is_dir())
        self.assertEqual(network.call_args.args[0], url)
        self.assertFalse((self.home/'.cache').exists())

    def test_replay_matches_percent_encoded_asset_urls_to_raw_pinned_dependency(self):
        # GitHub release metadata returns '+' in asset paths as '%2B' while the
        # pinned DEPENDENCIES entry is the raw '+' URL; the offline replay must
        # resolve the encoded helper URL to the pinned bytes.
        import time
        stage = self.sandbox_stage()
        package = stage/'work/package'; (package/'tools').mkdir(parents=True)
        raw = ('https://github.com/intel/intel-graphics-compiler/releases/download/'
               'v2.10.10/intel-igc-core-2_2.10.10+18926_amd64.deb')
        encoded = raw.replace('+', '%2B')
        self.assertNotEqual(raw, encoded)
        scripts = {'tools/get-igc.sh': '#!/bin/bash\nset -eu\ncurl -sfL -o "$1/replayed" '+encoded+'\n',
                   'tools/get-xess.sh': '#!/bin/bash\nset -eu\ntest -s "$1/replayed"\n',
                   'tools/get-ffx.sh': '#!/bin/bash\nset -eu\ntest -s "$1/replayed"\n',
                   'tools/session-check.sh': '#!/bin/bash\nset -eu\nexit 1\n'}
        hashes = {}
        for name, script in scripts.items():
            (package/name).write_text(script); hashes[name] = hashlib.sha256(script.encode()).hexdigest()
        payload = b'pinned igc-core fixture'
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(self.pkg, 'METADATA_URLS', ()), patch.object(
                self.pkg, 'DEPENDENCIES', ((raw, hashlib.sha256(payload).hexdigest()),)), patch.object(
                self.pkg, 'read_url', return_value=payload):
            self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+20)
        self.assertEqual((package/'replayed').read_bytes(), payload)

    def test_canonical_lookup_key_decodes_only_the_path_and_stays_injective(self):
        canonical = self.pkg._canonical_lookup
        clean = 'https://github.com/intel/xess/releases/download/v3.0.2/XeSS_SDK_3.0.2.zip'
        self.assertEqual(canonical(clean), clean)
        self.assertEqual(canonical(clean.replace('v3.0.2', '2.10.10%2B18926')),
                         clean.replace('v3.0.2', '2.10.10+18926'))
        # query strings and host are never decoded; a '%' that is not an escape stays verbatim
        self.assertEqual(canonical('https://api.github.com/repos/a/b?sig=%2Bx%2Fy'),
                         'https://api.github.com/repos/a/b?sig=%2Bx%2Fy')
        self.assertEqual(canonical('https://github.com/a/%zz'), 'https://github.com/a/%zz')

    def test_metadata_is_pretty_printed_so_line_parsers_extract_exact_urls(self):
        # GitHub's REST API now answers minified single-line JSON; the audited
        # helpers parse metadata with grep|cut per line. The stored bytes must
        # be pretty-printed and every '"browser_download_url": "<x>"' line must
        # cut out to an exact top-level asset URL, including '+' assets.
        import time
        stage = self.sandbox_stage()
        package = stage/'work/package'; (package/'tools').mkdir(parents=True)
        raw = ('https://github.com/intel/intel-graphics-compiler/releases/download/'
               'v2.10.10/intel-igc-core-2_2.10.10+18926_amd64.deb')
        asset = {'name': 'intel-igc-core-2_2.10.10+18926_amd64.deb', 'browser_download_url': raw}
        metadata = json.dumps({'url': 'https://api.github.com/repos/intel/intel-graphics-compiler/releases/217542229',
                               'html_url': 'https://github.com/intel/intel-graphics-compiler/releases/tag/v2.10.10',
                               'tag_name': 'v2.10.10', 'name': 'IGC 2.10.10', 'draft': False, 'prerelease': False,
                               'assets': [asset, dict(asset, name='intel-igc-core-2_2.10.10+18926_arm64.deb',
                                                      browser_download_url=raw.replace('amd64', 'arm64'))]})
        metadata_url = 'https://api.github.com/repos/intel/intel-graphics-compiler/releases/tags/v2.10.10'
        helper = ('#!/bin/bash\nset -eu\n'
                  'curl -s '+metadata_url+' | grep browser_download_url | cut -d\\" -f4 > "$1/cuturls"\n'
                  'cmp /work/package/metadata-check.expected "$1/cuturls"\n')
        scripts = {'tools/get-igc.sh': helper,
                   'tools/get-xess.sh': '#!/bin/bash\nset -eu\n',
                   'tools/get-ffx.sh': '#!/bin/bash\nset -eu\n',
                   'tools/session-check.sh': '#!/bin/bash\nset -eu\nexit 1\n'}
        hashes = {}
        for name, script in scripts.items():
            (package/name).write_text(script); hashes[name] = hashlib.sha256(script.encode()).hexdigest()
        payload = b'deb-fixture'

        def network(url, limit, **kwargs):
            return metadata.encode() if url == metadata_url else payload
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(
                self.pkg, 'METADATA_URLS', (metadata_url,)), patch.object(
                self.pkg, 'DEPENDENCIES', ((raw, hashlib.sha256(payload).hexdigest()),)), patch.object(
                self.pkg, 'read_url', side_effect=network):
            (package/'metadata-check.expected').write_text(raw+'\n'+raw.replace('amd64', 'arm64')+'\n')
            self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+20)
        index = json.loads((stage/'downloads'/'index.json').read_text())
        stored = (stage/'downloads'/index[metadata_url]).read_text()
        self.assertIn('\n      "browser_download_url": "', stored)
        self.assertEqual(stored.count('"browser_download_url": "'), 2)
        self.assertGreater(stored.count('\n'), 10)

    def test_session_check_step_exposes_package_dependency_dirs_on_ld_library_path(self):
        # The dry-run native probe dlopens the patched Mesa driver; its NEEDED
        # sonames live in the package's lib/deps, which the bwrap environment
        # must expose via LD_LIBRARY_PATH or the load fails with a bare
        # "[Errno 2] ... 'libdisplay-info.so.3'".
        import shutil as _shutil, subprocess, time
        if _shutil.which('gcc') is None:
            self.skipTest('gcc required for the real shared-object probe')
        stage = self.sandbox_stage(); package = stage/'work/package'; (package/'tools').mkdir(parents=True)
        # Unique soname: findable ONLY through LD_LIBRARY_PATH. Device-like
        # sandbox (no /usr bind) so the host cannot accidentally satisfy it.
        probe = 'libxmx-probe-display-info.so.3'
        so = self.root/probe
        subprocess.run(['gcc', '-shared', '-fPIC', '-Wl,-soname,'+probe,
                        '-x', 'c', '-', '-o', str(so)], check=True, capture_output=True, input=b'int xmx_probe(void){return 42;}\n')
        deps = package/'lib/deps'; deps.mkdir(parents=True)
        shutil.copyfile(so, deps/probe)
        scripts = {'tools/get-igc.sh': '#!/bin/bash\nset -eu\n', 'tools/get-xess.sh': '#!/bin/bash\nset -eu\n',
                   'tools/get-ffx.sh': '#!/bin/bash\nset -eu\n',
                   'tools/session-check.sh': ('#!/bin/bash\nset -eu\n'
                                              'python3 -c \'import ctypes;ctypes.CDLL("'+probe+'",mode=ctypes.RTLD_LOCAL)\'\n'
                                              'mkdir -p "$XDG_CACHE_HOME/test"\n')}
        hashes = {}
        for name, script in scripts.items():
            (package/name).write_text(script); hashes[name] = hashlib.sha256(script.encode()).hexdigest()
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(self.pkg, 'METADATA_URLS', ()), patch.object(
                self.pkg, 'DEPENDENCIES', ()), patch.object(self.pkg, 'read_url') as network:
            network.assert_not_called()
            self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+30)
        self.assertTrue((stage/'work/cache/test').is_dir())

    def _shared_object(self, name, needed=None):
        import subprocess
        arguments = ['gcc', '-shared', '-fPIC', '-Wl,-soname,'+name]
        for entry in needed or ():
            arguments += ['-L', str(self.root), '-Wl,--no-as-needed', '-l:'+entry]
        arguments += ['-x', 'c', '-', '-o', str(self.root/name)]
        subprocess.run(arguments, check=True, capture_output=True, input=b'int xmx_probe(void){return 42;}\n')
        return self.root/name

    def test_needed_parser_extracts_only_runtime_dependency_libnames(self):
        import shutil as _shutil
        if _shutil.which('gcc') is None:
            self.skipTest('gcc required to build the fixture driver')
        dep = self._shared_object('libxmx-probe-dep.so')
        driver = self._shared_object('libxmx-probe-driver.so', needed=['libxmx-probe-dep.so'])
        self.assertEqual(self.pkg._elf_needed(driver), ['libxmx-probe-dep.so', 'libc.so.6'])
        plain = self._shared_object('libxmx-probe-plain.so')
        self.assertEqual(self.pkg._elf_needed(plain), ['libc.so.6'])
        self.assertEqual(self.pkg._elf_needed(self.root/'missing.so'), [])

    def test_missing_runtime_dependencies_are_populated_from_bundled(self):
        # A Fedora-style host without libdisplay-info.so.3 leaves session-check
        # unable to fill lib/deps; the package's own lib/bundled copy must be
        # staged so the required-dependency validation and the games' run path
        # both see it.
        import shutil as _shutil, subprocess, time
        if _shutil.which('gcc') is None:
            self.skipTest('gcc required to build the fixture driver')
        stage = self.sandbox_stage(); package = stage/'work/package'
        probe = 'libxmx-probe-display-info.so.3'
        bundled = package/'lib/bundled'; bundled.mkdir(parents=True)
        name = str(probe)
        dep = self._shared_object(name)
        shutil.copyfile(dep, bundled/probe)
        driver = self._shared_object('libvulkan_intel.so', needed=[name])
        mesa = package/'lib/mesa-99.0'; mesa.mkdir(parents=True)
        shutil.copyfile(driver, mesa/'libvulkan_intel.so')
        originals = {'lib/bundled/'+name: {'sha256': hashlib.sha256(dep.read_bytes()).hexdigest(), 'mode': 0o644}}
        probe_pattern = 'lib(?:SPIRV-Tools[A-Za-z-]*|display-info|xmx-probe-display-info)\\.so[0-9.]*$'
        with patch.object(self.pkg, '_DEP_NAME', re.compile(probe_pattern)):
            staged = self.pkg._populate_deps(manager=self.manager, package=package, originals=originals)
        self.assertEqual(staged, ['lib/deps/'+name])
        self.assertEqual((package/'lib/deps'/name).read_bytes(), dep.read_bytes())
        self.assertIn((package/'lib/deps'/name).stat().st_mode & 0o022, (0, 0o020))
        # Idempotent, and a driver without packagable NEEDED entries stages nothing.
        plain_mesa = package/'lib/mesa-88.0'; plain_mesa.mkdir()
        shutil.copyfile(self._shared_object('libxmx-probe-plain2.so'), plain_mesa/'libvulkan_intel.so')
        with patch.object(self.pkg, '_DEP_NAME', re.compile(probe_pattern)):
            self.assertEqual(self.pkg._populate_deps(manager=self.manager, package=package,
                                                     originals=originals), [])

    def test_required_bundled_dependency_is_staged_even_when_the_driver_does_not_link_it(self):
        # The validator requires lib/deps/libdisplay-info.so.3, but the shipped
        # Mesa builds may not link it at all (20261104 does not) and a Fedora
        # host cannot provide it; the package's own bundled build must still
        # land in lib/deps so validation and the games' run path agree.
        import shutil as _shutil
        if _shutil.which('gcc') is None:
            self.skipTest('gcc required to build the fixture driver')
        stage = self.sandbox_stage(); package = stage/'work/package'
        name = 'libdisplay-info.so.3'
        bundled = package/'lib/bundled'; bundled.mkdir(parents=True)
        dep = self._shared_object(name)
        shutil.copyfile(dep, bundled/name)
        mesa = package/'lib/mesa-99.0'; mesa.mkdir(parents=True)
        shutil.copyfile(self._shared_object('libxmx-probe-nodisplay.so'), mesa/'libvulkan_intel.so')
        originals = {'lib/bundled/'+name: {'sha256': hashlib.sha256(dep.read_bytes()).hexdigest(), 'mode': 0o644}}
        staged = self.pkg._populate_deps(manager=self.manager, package=package, originals=originals)
        self.assertIn('lib/deps/'+name, staged)
        self.assertEqual((package/'lib/deps'/name).read_bytes(), dep.read_bytes())

    def test_dependency_hash_or_helper_change_fails_before_execution(self):
        import time
        stage = self.sandbox_stage(); hashes, url = self.dependency_helpers(stage)
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(self.pkg, 'METADATA_URLS', ()), patch.object(
                self.pkg, 'DEPENDENCIES', ((url, '0'*64),)), patch.object(self.pkg, 'read_url', return_value=b'wrong'):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+20)
        self.assertFalse((stage/'work/package/replayed').exists())
        (stage/'work/package/tools/get-igc.sh').write_text('# unreviewed')
        with patch.object(self.pkg, 'HELPER_SHA256', hashes), patch.object(self.pkg, 'read_url') as network:
            with self.assertRaisesRegex(ValueError, 'helper'):
                self.pkg._prepare_dependencies(self.manager, stage, None, time.monotonic()+20)
        network.assert_not_called()

    def test_internal_dependency_symlinks_are_allowed_but_world_writable_files_are_not(self):
        def linked(*args, **kwargs):
            self.fake_dependencies(*args, **kwargs)
            (args[1]/'work/package/igc/igc/local/lib/libigc.so.1').symlink_to('libigc.so')
        result = self.prepare_fixture(linked)
        self.assertEqual(result['state'], 'ready', result)
        def writable(*args, **kwargs):
            self.fake_dependencies(*args, **kwargs)
            (args[1]/'work/package/igc/igc/local/lib/libigc.so').chmod(0o666)
        result = self.prepare_fixture(writable)
        self.assertEqual(result['state'], 'error', result)


    def test_public_rpc_forwarding_and_service_prepare_contract(self):
        import asyncio, importlib.util, logging, types
        from optiscaler.service import OptiScalerService
        decky = types.ModuleType('decky')
        spec = importlib.util.spec_from_file_location('xmx_package_public_plugin', Path(__file__).resolve().parents[1]/'main.py')
        plugin_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'decky': decky}): spec.loader.exec_module(plugin_module)
        service = OptiScalerService(self.root/'plugin', self.root/'settings', self.root/'runtime', self.home, logging.getLogger('test'))
        plugin = plugin_module.Plugin(); plugin.service = service
        async def run():
            local = await plugin.get_xmx_package_status()
            self.assertEqual(local['state'], 'idle')
            with patch.object(self.pkg, 'prepare', return_value={'ok': True, 'state': 'ready'}) as prepare:
                self.assertEqual((await plugin.prepare_xmx_package())['state'], 'ready')
                self.assertIs(prepare.call_args.args[0], service.xmx)
                self.assertIn('cancel', prepare.call_args.kwargs)
        asyncio.run(run())

    def test_public_cancellation_signals_worker_and_holds_lock_through_repeated_cancel(self):
        import asyncio, logging, threading, time
        from optiscaler.service import OptiScalerService
        service = OptiScalerService(self.root/'plugin', self.root/'settings', self.root/'runtime', self.home, logging.getLogger('test'))
        entered = threading.Event(); stopped = threading.Event(); release = threading.Event()
        self.addCleanup(release.set)
        calls = []
        def prepare(manager, *, cancel):
            calls.append(cancel)
            if len(calls) == 1:
                entered.set()
                while not cancel.wait(.02): pass
                stopped.set(); release.wait(5)
            return {'ok': True, 'state': 'error', 'package': None, 'latest': None, 'error': 'cancelled'}
        async def run():
            first = asyncio.create_task(service.prepare_xmx_package())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
            first.cancel(); first.cancel()
            second = asyncio.create_task(service.prepare_xmx_package())
            await asyncio.sleep(.05)
            self.assertEqual(len(calls), 1)
            self.assertFalse(first.done()); self.assertFalse(second.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await first
            self.assertEqual((await second)['state'], 'error')
            self.assertEqual(len(calls), 2)
        with patch.object(self.pkg, 'prepare', side_effect=prepare): asyncio.run(run())


if __name__ == '__main__':
    unittest.main(verbosity=2)
