#!/usr/bin/env python3
"""Portable, offline tests for the pinned SDK utility; never execute a DLL."""
import hashlib
import importlib
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_modules'))


def pe_fixture(version=(2, 0, 2, 68), names=('xessGetVersion', 'xessD3D12Init'),
               ordinal_only=False):
    """Construct an inert PE32+ with real export and RT_VERSION directories."""
    data = bytearray(0x1000)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 0xf0, 0x2022)
    struct.pack_into('<H', data, 0x98, 0x20b)
    struct.pack_into('<II', data, 0x98 + 32, 0x1000, 0x200)
    struct.pack_into('<II', data, 0x98 + 56, 0x2000, 0x200)
    struct.pack_into('<I', data, 0x98 + 108, 16)
    struct.pack_into('<II', data, 0x98 + 112, 0x1000, 0x200)
    struct.pack_into('<II', data, 0x98 + 128, 0x1400, 0x200)
    struct.pack_into('<8sIIIIIIHHI', data, 0x188, b'.rdata\0\0', 0xe00,
                     0x1000, 0xe00, 0x200, 0, 0, 0, 0, 0x40000040)
    count = len(names) + int(ordinal_only)
    struct.pack_into('<IIHHIIIIIII', data, 0x200, 0, 0, 0, 0, 0x1180,
                     1, count, len(names), 0x1040, 0x1080, 0x10c0)
    for index in range(count):
        struct.pack_into('<I', data, 0x240 + index * 4, 0x1800 + index * 8)
    cursor = 0x300
    for index, (ordinal, name) in enumerate(sorted(enumerate(names), key=lambda row: row[1])):
        struct.pack_into('<I', data, 0x280 + index * 4, cursor + 0xe00)
        struct.pack_into('<H', data, 0x2c0 + index * 2, ordinal)
        raw = name.encode('ascii') + b'\0'
        data[cursor:cursor + len(raw)] = raw
        cursor += len(raw)
    data[0x380:0x38c] = b'libxess.dll\0'
    # Resource tree: type VERSION (16) -> id 1 -> language 1033 -> data.
    for offset in (0x600, 0x618, 0x630):
        struct.pack_into('<IIHHHH', data, offset, 0, 0, 0, 0, 0, 1)
    struct.pack_into('<II', data, 0x610, 16, 0x80000018)
    struct.pack_into('<II', data, 0x628, 1, 0x80000030)
    struct.pack_into('<II', data, 0x640, 1033, 0x48)
    key = 'VS_VERSION_INFO\0'.encode('utf-16le')
    value = struct.pack('<13I', 0xfeef04bd, 0x10000,
                        version[0] << 16 | version[1], version[2] << 16 | version[3],
                        0, 0, 0x3f, 0, 0x40004, 2, 0, 0, 0)
    blob = struct.pack('<HHH', 92, len(value), 0) + key + b'\0\0' + value
    struct.pack_into('<IIII', data, 0x648, 0x1480, len(blob), 0, 0)
    data[0x680:0x680 + len(blob)] = blob
    return bytes(data)


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


class SdkTests(unittest.TestCase):
    def setUp(self):
        self.sdk = importlib.import_module('optiscaler.xess_sdk')
        scratch = os.environ.get('TMPDIR', str(Path.home()/'.hermes/cache/scratch'))
        self.temp = tempfile.TemporaryDirectory(prefix='xess-sdk-tests-', dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def dll(self, data=None, name='libxess.dll'):
        path = self.root/name
        path.write_bytes(pe_fixture() if data is None else data)
        return path

    def manager(self, settings='settings'):
        Manager = importlib.import_module('optiscaler.xmx').Manager
        home = self.root/'home'
        home.mkdir(exist_ok=True)
        return Manager(home, self.root/settings)

    def archive(self, payload=None, entries=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries if entries is not None else [('bin/libxess.dll', pe_fixture() if payload is None else payload)]:
                archive.writestr(name, data)
        return output.getvalue()

    def pins(self, archive, payload=None):
        return patch.multiple(self.sdk,
                              SDK_ARCHIVE_SHA256=hashlib.sha256(archive).hexdigest(),
                              DLL_SHA256=hashlib.sha256(pe_fixture() if payload is None else payload).hexdigest())

    def response(self, data):
        return Response(data)

    def test_explicit_prepare_verifies_pins_and_writes_only_the_sdk_cache(self):
        archive = self.archive()
        manager = self.manager()
        with self.pins(archive), patch('urllib.request.OpenerDirector.open',
                                     return_value=self.response(archive)) as network:
            result = self.sdk.prepare_sdk(manager)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['cached'])
        target = manager.root/'sdk-cache/v3.0.2/libxess.dll'
        self.assertEqual(result['path'], str(target))
        self.assertEqual(result['sdk_tag'], 'v3.0.2')
        self.assertEqual(result['version'], '2.0.2.68')
        self.assertEqual(target.read_bytes(), pe_fixture())
        self.assertEqual(result['sha256'], hashlib.sha256(pe_fixture()).hexdigest())
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()], [target])
        request = network.call_args.args[0]
        self.assertEqual(request.full_url, 'https://github.com/intel/xess/releases/download/v3.0.2/XeSS_SDK_3.0.2.zip')
        self.assertGreater(network.call_args.kwargs['timeout'], 0)

    def test_inspection_reads_embedded_version_architecture_exports_and_digest(self):
        payload = pe_fixture()
        result = self.sdk.inspect_dll(self.dll(payload))
        self.assertEqual(result['version'], '2.0.2.68')
        self.assertEqual(result['architecture'], 'x86_64')
        self.assertEqual(result['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result['exports'], ['xessD3D12Init', 'xessGetVersion'])

    def test_inspection_rejects_non_x64_or_non_dll_images(self):
        for label, offset, fmt, value in (
            ('x86', 0x84, '<H', 0x14c), ('arm64', 0x84, '<H', 0xaa64),
            ('pe32', 0x98, '<H', 0x10b), ('exe', 0x96, '<H', 0x22),
            ('mz', 0, '<2s', b'NO'), ('signature', 0x80, '<4s', b'NOPE'),
        ):
            with self.subTest(label=label):
                data = bytearray(pe_fixture())
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaises(ValueError):
                    self.sdk.inspect_dll(self.dll(data))

    def test_truncated_or_ambiguous_pe_layout_is_rejected(self):
        original = pe_fixture()
        variants = [original[:length] for length in (0, 63, 0x90, 0x188, 0x700, 0xfff)]
        for offset, fmt, value in (
            (0x94, '<H', 8), (0x98 + 108, '<I', 2),
            (0x98 + 60, '<I', 0x180), (0x188 + 16, '<I', 0xf00),
            (0x188 + 20, '<I', 0x100),
        ):
            data = bytearray(original)
            struct.pack_into(fmt, data, offset, value)
            variants.append(bytes(data))
        overlap = bytearray(original)
        struct.pack_into('<H', overlap, 0x86, 2)
        overlap[0x1b0:0x1d8] = original[0x188:0x1b0]
        variants.append(bytes(overlap))
        for index, data in enumerate(variants):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.sdk.inspect_dll(self.dll(data))

    def test_version_resources_are_structured_bounded_not_signature_scans(self):
        for label, offset, fmt, value in (
            ('no type directory', 0x614, '<I', 0x18),
            ('tree outside resource', 0x614, '<I', 0x80000210),
            ('cycle', 0x62c, '<I', 0x80000018),
            ('data is directory', 0x644, '<I', 0x80000048),
            ('short value', 0x682, '<H', 4),
            ('short record', 0x680, '<H', 40),
            ('text version type', 0x684, '<H', 1),
        ):
            with self.subTest(label=label):
                data = bytearray(pe_fixture())
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaises(ValueError):
                    self.sdk.inspect_dll(self.dll(data))

    def test_inspection_accounts_for_ordinal_only_exports_and_forwarders(self):
        data = bytearray(pe_fixture(ordinal_only=True))
        struct.pack_into('<I', data, 0x244, 0x1190)
        data[0x390:0x3a1] = b'other.xessTarget\0'
        result = self.sdk.inspect_dll(self.dll(data))
        self.assertEqual(result['export_ordinals'], {'1': ['xessGetVersion'],
                         '2': ['xessD3D12Init'], '3': []})
        self.assertEqual(result['ordinal_only_exports'], [3])
        self.assertEqual(result['forwarders'], {'2': 'other.xessTarget'})

    def test_export_directory_dll_name_must_be_file_backed_and_terminated(self):
        data = bytearray(pe_fixture())
        struct.pack_into('<I', data, 0x20c, 0x4000)
        with self.assertRaisesRegex(ValueError, 'RVA|export string'):
            self.sdk.inspect_dll(self.dll(data))

    def test_malformed_export_forwarder_is_rejected(self):
        for forwarder in (b'not-a-forwarder', b'.Function', b'other.', b'other.#0',
                          b'other.#65536', b'other.#NaN', b'../evil.Func'):
            with self.subTest(forwarder=forwarder):
                data = bytearray(pe_fixture())
                struct.pack_into('<I', data, 0x244, 0x1190)
                data[0x390:0x390+len(forwarder)+1] = forwarder+b'\0'
                with self.assertRaisesRegex(ValueError, 'forwarder'):
                    self.sdk.inspect_dll(self.dll(data))

    def test_unsorted_export_name_pointer_table_is_rejected(self):
        data = bytearray(pe_fixture())
        rows = []
        for index in range(2):
            address = struct.unpack_from('<I', data, 0x280+index*4)[0]
            ordinal = struct.unpack_from('<H', data, 0x2c0+index*2)[0]
            offset = address-0xe00
            rows.append((bytes(data[offset:data.index(0, offset)]), address, ordinal))
        for index, (_, address, ordinal) in enumerate(sorted(rows, reverse=True)):
            struct.pack_into('<I', data, 0x280+index*4, address)
            struct.pack_into('<H', data, 0x2c0+index*2, ordinal)
        with self.assertRaisesRegex(ValueError, 'sort|order'):
            self.sdk.inspect_dll(self.dll(data))

    def test_export_tables_reject_dangling_ambiguous_and_unbounded_entries(self):
        for label, offset, fmt, value in (
            ('ordinal outside EAT', 0x2c0, '<H', 20),
            ('name points to EAT hole', 0x240, '<I', 0),
            ('duplicate name', 0x284, '<I', 0x1100),
            ('unbounded function count', 0x214, '<I', 70000),
            ('unbounded name count', 0x218, '<I', 70000),
            ('ordinal overflow', 0x210, '<I', 65535),
            ('empty name', 0x280, '<I', 0x11f0),
        ):
            with self.subTest(label=label):
                data = bytearray(pe_fixture())
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaises(ValueError):
                    self.sdk.inspect_dll(self.dll(data))

    def test_export_coverage_is_preliminary_and_never_ignores_ordinals(self):
        old = self.sdk.inspect_dll(self.dll(pe_fixture(ordinal_only=True)))
        retained = self.sdk.compare_exports(old, old)
        self.assertTrue(retained['ok'], retained)
        self.assertTrue(retained['preliminary'])
        self.assertFalse(retained['runtime_verified'])
        self.assertTrue(retained['warnings'])
        new = self.sdk.inspect_dll(self.dll(pe_fixture(names=('xessGetVersion', 'newAPI'))))
        result = self.sdk.compare_exports(old, new)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['missing_exports'], ['xessD3D12Init'])
        self.assertEqual(result['missing_ordinals'], [3])
        self.assertEqual(result['changed_ordinals'], [2])
        moved = self.sdk.inspect_dll(self.dll(pe_fixture(names=('xessD3D12Init', 'xessGetVersion'),
                                                        ordinal_only=True)))
        self.assertFalse(self.sdk.compare_exports(old, moved)['ok'])

    def test_verified_cache_reuse_is_offline_but_tampering_is_never_repaired(self):
        archive = self.archive()
        manager = self.manager()
        target = manager.root/'sdk-cache/v3.0.2/libxess.dll'
        manager._atomic(target, pe_fixture(), mode=0o644, expected=None)
        with self.pins(archive), patch('urllib.request.OpenerDirector.open',
                                     side_effect=AssertionError('must be offline')) as network:
            result = self.sdk.prepare_sdk(manager)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['cached'])
            target.write_bytes(b'tampered')
            result = self.sdk.prepare_sdk(manager)
        self.assertFalse(result['ok'], result)
        self.assertEqual(target.read_bytes(), b'tampered')
        network.assert_not_called()

    def test_ambiguous_or_unsafe_archive_members_are_rejected_without_extraction(self):
        payload = pe_fixture()
        variants = [
            [('bin/libxess.dll', payload), ('elsewhere/libxess.dll', payload)],
            [('bin/libxess.dll', payload), ('../escape.txt', b'no')],
            [('bin/libxess.dll', payload), ('/absolute.txt', b'no')],
            [('bin/libxess.dll', payload), ('BIN/LIBXESS.DLL', payload)],
            [('bin/libxess.dll', payload), ('evil\\\\libxess.dll', payload)],
        ]
        for index, entries in enumerate(variants):
            with self.subTest(index=index):
                archive = self.archive(entries=entries)
                manager = self.manager(str(index))
                with self.pins(archive), patch('urllib.request.OpenerDirector.open',
                                              return_value=self.response(archive)):
                    result = self.sdk.prepare_sdk(manager)
                self.assertFalse(result['ok'], result)
                self.assertFalse((manager.root/'sdk-cache/v3.0.2/libxess.dll').exists())

    def test_dll_file_and_zip_entry_have_explicit_size_bounds(self):
        archive = self.archive()
        manager = self.manager()
        with patch.object(self.sdk, 'MAX_DLL_BYTES', 1024, create=True):
            with self.assertRaisesRegex(ValueError, 'size|large|limit'):
                self.sdk.inspect_dll(self.dll())
            with self.pins(archive), patch('urllib.request.OpenerDirector.open',
                                         return_value=self.response(archive)):
                result = self.sdk.prepare_sdk(manager)
        self.assertFalse(result['ok'], result)
        self.assertFalse((manager.root/'sdk-cache/v3.0.2/libxess.dll').exists())

    def test_download_enforces_declared_and_streamed_size_limits(self):
        for data, headers in (
            (b'x' * 21, {'Content-Length': '21'}), (b'x' * 21, {}),
            (b'x' * 10, {'Content-Length': '20'}),
            (b'x', {'Content-Length': '-1'}),
            (b'x', {'Content-Length': 'NaN'}),
            (b'x', {'Content-Encoding': 'gzip'}),
        ):
            with self.subTest(headers=headers, length=len(data)):
                response = self.response(data)
                response.headers = headers
                with patch.object(self.sdk, 'MAX_ARCHIVE_BYTES', 20, create=True), patch(
                        'urllib.request.OpenerDirector.open', return_value=response):
                    with self.assertRaises(ValueError):
                        self.sdk._download_archive()

    def test_download_has_a_total_time_budget_not_only_socket_timeout(self):
        with patch('urllib.request.OpenerDirector.open', return_value=self.response(b'abc')), patch(
                'time.monotonic', side_effect=[0, 0, 121]):
            with self.assertRaises(TimeoutError):
                self.sdk._download_archive()

    def test_sdk_download_supplies_verified_context_with_system_ca_when_defaults_are_empty(self):
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(context.cert_store_stats()['x509_ca'], 0)
        with patch.dict(os.environ, {}, clear=True), patch('ssl.create_default_context', return_value=context), patch(
                'urllib.request.build_opener', wraps=self.sdk.urllib.request.build_opener) as build, patch(
                'urllib.request.OpenerDirector.open', return_value=self.response(b'x')):
            self.sdk._download_archive()
        handlers = [value for value in build.call_args.args
                    if isinstance(value, self.sdk.urllib.request.HTTPSHandler)]
        self.assertEqual(len(handlers), 1, 'SDK must supply its own verifying HTTPS context')
        self.assertIs(handlers[0]._context, context)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_explicit_ssl_trust_configuration_is_not_expanded(self):
        import ssl
        for variable in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
            with self.subTest(variable=variable):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                with patch.dict(os.environ, {variable: '/administrator/trust'}, clear=True), patch(
                        'ssl.create_default_context', return_value=context), patch(
                        'ssl.SSLContext.load_verify_locations', side_effect=AssertionError('Do not expand explicit trust')):
                    self.assertIs(self.sdk._https_context(), context)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)

    def test_certificate_rejection_is_not_retried_without_verification_or_cached(self):
        import ssl
        manager = self.manager()
        failure = self.sdk.urllib.error.URLError(ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED'))
        with patch('urllib.request.OpenerDirector.open', side_effect=failure) as network, patch(
                'ssl._create_unverified_context', side_effect=AssertionError('Never disable TLS verification')):
            result = self.sdk.prepare_sdk(manager)
        self.assertFalse(result['ok'])
        self.assertIn('CERTIFICATE_VERIFY_FAILED', result['error'])
        self.assertEqual(network.call_count, 1)
        self.assertFalse((manager.root/'sdk-cache/v3.0.2/libxess.dll').exists())

    def test_redirects_remain_https_and_on_official_asset_hosts(self):
        request = self.sdk.urllib.request.Request(self.sdk.SDK_URL)
        handler = self.sdk._OfficialRedirects()
        self.assertLessEqual(handler.max_redirections, 3)
        for url in ('http://github.com/no', 'https://evil.invalid/lib.zip',
                    'file:///etc/passwd', 'https://user:pass@github.com/no',
                    'https://github.com:444/no'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    handler.redirect_request(request, None, 302, '', {}, url)
        url = 'https://release-assets.githubusercontent.com/example?sig=public'
        self.assertEqual(handler.redirect_request(request, None, 302, '', {}, url).full_url, url)
        with patch('urllib.request.build_opener', wraps=self.sdk.urllib.request.build_opener) as build, patch(
                'urllib.request.OpenerDirector.open', return_value=self.response(b'x')):
            self.sdk._download_archive()
        self.assertTrue(any(isinstance(value, self.sdk._OfficialRedirects)
                            for value in build.call_args.args))

    def test_sdk_dll_archive_entry_must_be_regular_not_a_symlink(self):
        info = zipfile.ZipInfo('bin/libxess.dll')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive = self.archive(entries=[(info, pe_fixture())])
        manager = self.manager()
        with self.pins(archive), patch('urllib.request.OpenerDirector.open',
                                     return_value=self.response(archive)):
            result = self.sdk.prepare_sdk(manager)
        self.assertFalse(result['ok'], result)
        self.assertFalse((manager.root/'sdk-cache/v3.0.2/libxess.dll').exists())

    def test_cache_leaf_and_ancestors_reject_symlinks_without_network_or_external_writes(self):
        external = self.root/'external'
        external.mkdir()
        outside = external/'libxess.dll'
        outside.write_bytes(pe_fixture())
        for component in ('sdk-cache', 'sdk-cache/v3.0.2', 'sdk-cache/v3.0.2/libxess.dll'):
            with self.subTest(component=component):
                manager = self.manager(component.replace('/', '-'))
                link = manager.root/component
                link.parent.mkdir(parents=True)
                link.symlink_to(outside if component.endswith('.dll') else external)
                with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network')) as network:
                    result = self.sdk.prepare_sdk(manager)
                self.assertFalse(result['ok'], result)
                network.assert_not_called()
                self.assertTrue(link.is_symlink())
                self.assertEqual(outside.read_bytes(), pe_fixture())
                self.assertEqual(list(external.iterdir()), [outside])

    def test_inspection_rejects_symlinks_and_special_files(self):
        dll = self.dll()
        link = self.root/'alias.dll'
        link.symlink_to(dll)
        fifo = self.root/'fifo.dll'
        os.mkfifo(fifo)
        for path in (link, fifo, self.root):
            with self.subTest(path=path):
                with self.assertRaises((ValueError, OSError)):
                    self.sdk.inspect_dll(path)

    def test_bad_archive_or_payload_is_rejected_before_cache_write(self):
        wrong_arch = bytearray(pe_fixture())
        struct.pack_into('<H', wrong_arch, 0x84, 0x14c)
        for index, (archive, payload, archive_pin, dll_pin) in enumerate((
            (self.archive(), pe_fixture(), '0' * 64, None),
            (b'not a ZIP', pe_fixture(), None, None),
            (self.archive()[:-12], pe_fixture(), None, None),
            (self.archive(), pe_fixture(), None, '0' * 64),
            (self.archive(bytes(wrong_arch)), bytes(wrong_arch), None, None),
            (self.archive(pe_fixture(version=(1, 2, 3, 4))), pe_fixture(version=(1, 2, 3, 4)), None, None),
        )):
            with self.subTest(index=index):
                manager = self.manager(str(index))
                with patch.multiple(self.sdk,
                                    SDK_ARCHIVE_SHA256=archive_pin or hashlib.sha256(archive).hexdigest(),
                                    DLL_SHA256=dll_pin or hashlib.sha256(payload).hexdigest()), patch(
                        'urllib.request.OpenerDirector.open', return_value=self.response(archive)):
                    result = self.sdk.prepare_sdk(manager)
                self.assertFalse(result['ok'], result)
                self.assertFalse((manager.root/'sdk-cache/v3.0.2/libxess.dll').exists())

    @unittest.skipUnless(os.environ.get('XESS_SDK_TEST_ARCHIVE'), 'optional real SDK integration')
    def test_real_pinned_sdk_archive_preparation_and_offline_reuse(self):
        archive = Path(os.environ['XESS_SDK_TEST_ARCHIVE']).read_bytes()
        manager = self.manager()
        with patch.object(self.sdk, '_download_archive', return_value=archive):
            result = self.sdk.prepare_sdk(manager)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['cached'])
        self.assertEqual(result['version'], '2.0.2.68')
        self.assertEqual(result['sha256'], self.sdk.DLL_SHA256)
        self.assertIn('xessGetVersion', result['exports'])
        self.assertIn('xessD3D12Init', result['exports'])
        with patch.object(self.sdk, '_download_archive', side_effect=AssertionError('must be offline')):
            cached = self.sdk.prepare_sdk(manager)
        self.assertTrue(cached['ok'], cached)
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['sha256'], result['sha256'])
        print('REAL SDK:', result['sdk_tag'], result['version'], result['sha256'],
              'exports=', len(result['exports']), 'ordinals=', len(result['export_ordinals']))

    def test_download_checks_deadline_between_single_raw_reads(self):
        class SingleReadResponse(Response):
            def read(self, *args):
                raise AssertionError('read(size) can mask endless slow trickle traffic')
        with patch('urllib.request.OpenerDirector.open', return_value=SingleReadResponse(b'abc')):
            self.assertEqual(self.sdk._download_archive(), b'abc')


if __name__ == '__main__':
    unittest.main(verbosity=2)
