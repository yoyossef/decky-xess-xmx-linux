"""Pinned official XeSS SDK preparation and inert, pure-Python PE inspection.

Inspection never loads DLLs. SDK release and embedded DLL version are distinct.
"""
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import ssl
import struct
import time
import urllib.parse
import urllib.request
import zipfile

SDK_TAG = 'v3.0.2'
SDK_ARCHIVE_SHA256 = '88b8a373f30e33f3558a77a93e634f11b8132fc3047ea1a8edeead32b8471990'
DLL_SHA256 = '251659dd84a3e84de67c886a4186e01f3eca49b00641906fe38bb6b807e5d5b7'


class _PE:
    def __init__(self, data):
        self.data = data
        pe = self.unpack('<I', 0x3c)[0]
        if self.take(0, 2) != b'MZ' or pe < 64 or self.take(pe, 4) != b'PE\0\0':
            raise ValueError('Invalid DOS/PE signature')
        machine, sections, _, _, _, optional_size, flags = self.unpack('<HHIIIHH', pe + 4)
        optional = pe + 24
        if machine != 0x8664 or self.unpack('<H', optional)[0] != 0x20b:
            raise ValueError('Only x86_64 PE32+ DLLs are supported')
        if not flags & 0x2000:
            raise ValueError('PE image is not a DLL')
        directory_count = self.unpack('<I', optional + 108)[0]
        if not 1 <= sections <= 96 or not 3 <= directory_count <= 16:
            raise ValueError('Invalid PE section or directory count')
        if optional_size < 112 + directory_count * 8:
            raise ValueError('Truncated PE optional header')
        self.take(optional, optional_size + sections * 40)
        self.headers = self.unpack('<I', optional + 60)[0]
        self.image_size = self.unpack('<I', optional + 56)[0]
        if not optional + optional_size + sections * 40 <= self.headers <= len(data):
            raise ValueError('Invalid PE header extent')
        if self.image_size <= self.headers:
            raise ValueError('Invalid PE image extent')
        self.directories = optional + 112
        self.sections = []
        for index in range(sections):
            offset = optional + optional_size + index * 40
            virtual_size, rva, raw_size, raw = self.unpack('<IIII', offset + 8)
            extent = max(virtual_size, raw_size)
            if rva < self.headers or rva + extent > self.image_size:
                raise ValueError('Invalid PE virtual section extent')
            if raw_size:
                if raw < self.headers:
                    raise ValueError('PE raw section overlaps headers')
                self.take(raw, raw_size)
            for other_rva, other_size, other_raw, other_raw_size in self.sections:
                if extent and other_size and rva < other_rva + other_size and other_rva < rva + extent:
                    raise ValueError('Ambiguous overlapping PE virtual sections')
                if raw_size and other_raw_size and raw < other_raw + other_raw_size and other_raw < raw + raw_size:
                    raise ValueError('Ambiguous overlapping PE raw sections')
            self.sections.append((rva, extent, raw, raw_size))

    def take(self, offset, size):
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ValueError('Truncated PE structure')
        return self.data[offset:offset + size]

    def unpack(self, fmt, offset):
        return struct.unpack(fmt, self.take(offset, struct.calcsize(fmt)))

    def rva(self, address, size):
        for start, virtual_size, raw, raw_size in self.sections:
            relative = address - start
            if 0 <= relative and relative + size <= min(virtual_size, raw_size):
                self.take(raw + relative, size)
                return raw + relative
        raise ValueError('PE RVA is not backed by file data')

    def directory(self, index):
        address, size = self.unpack('<II', self.directories + index * 8)
        return self.rva(address, size), address, size

    def string(self, address, limit=4096):
        start = self.rva(address, 1)
        end = self.data.find(b'\0', start, start + limit)
        if end < 0 or end == start:
            raise ValueError('Empty or unterminated PE export string')
        self.rva(address, end - start + 1)
        try:
            return self.data[start:end].decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError('Non-ASCII PE export string') from exc

    def exports(self):
        offset, export_rva, size = self.directory(0)
        if size < 40:
            raise ValueError('Truncated export directory')
        _, _, _, _, dll_name, base, count, names_count, functions, names, ordinals = self.unpack(
            '<IIHHIIIIIII', offset)
        self.string(dll_name)
        if not 1 <= count <= 65536 or names_count > 65536 or base + count > 65536:
            raise ValueError('Invalid or excessive PE export count/ordinal range')
        functions = self.rva(functions, count * 4)
        table, forwarders = {}, {}
        for index in range(count):
            address = self.unpack('<I', functions + index * 4)[0]
            if not address:
                continue
            ordinal = str(base + index)
            table[ordinal] = []
            if export_rva <= address < export_rva + size:
                value = self.string(address, export_rva + size - address)
                module, separator, symbol = value.rpartition('.')
                if (not separator or not re.fullmatch(r'[A-Za-z0-9_.-]+', module)
                        or not re.fullmatch(r'[!-~]+', symbol)
                        or symbol.startswith('#') and (not symbol[1:].isdecimal()
                            or not 1 <= int(symbol[1:]) <= 65535)):
                    raise ValueError('Invalid PE export forwarder')
                forwarders[ordinal] = value
            else:
                self.rva(address, 1)
        if names_count:
            names, ordinals = self.rva(names, names_count * 4), self.rva(ordinals, names_count * 2)
        result = set()
        last_name = None
        for index in range(names_count):
            address = self.unpack('<I', names + index * 4)[0]
            ordinal = str(base + self.unpack('<H', ordinals + index * 2)[0])
            name = self.string(address)
            if last_name is not None and name < last_name:
                raise ValueError('PE export name table is not lexically sorted')
            last_name = name
            if ordinal not in table or name in result:
                raise ValueError('Dangling or ambiguous PE export name/ordinal')
            table[ordinal].append(name)
            result.add(name)
        return {'exports': sorted(result), 'export_ordinals': table,
                'ordinal_only_exports': [int(ordinal) for ordinal, values in table.items() if not values],
                'forwarders': forwarders}

    def version(self):
        root, _, size = self.directory(2)
        visited = set()
        budget = 4096

        def resource(relative, length):
            if relative < 0 or relative + length > size:
                raise ValueError('Resource structure escapes resource directory')
            return root + relative

        def entries(target):
            nonlocal budget
            if not target & 0x80000000:
                raise ValueError('Expected a PE resource directory')
            relative = target & 0x7fffffff
            if relative in visited:
                raise ValueError('Cyclic or aliased PE resource directory')
            visited.add(relative)
            offset = resource(relative, 16)
            named, ids = self.unpack('<HH', offset + 12)
            count = named + ids
            budget -= count
            if budget < 0:
                raise ValueError('Too many PE resource entries')
            resource(relative + 16, count * 8)
            result, seen = [], set()
            for index in range(count):
                name, child = self.unpack('<II', offset + 16 + index * 8)
                if bool(name & 0x80000000) != (index < named) or name in seen:
                    raise ValueError('Ambiguous PE resource entries')
                seen.add(name)
                if name & 0x80000000:
                    string = name & 0x7fffffff
                    length = self.unpack('<H', resource(string, 2))[0]
                    resource(string + 2, length * 2)
                result.append((name, child))
            return result

        versions = []
        for resource_id, target in entries(0x80000000):
            if resource_id != 16:
                continue
            for _, child in entries(target):
                for _, leaf in entries(child):
                    if leaf & 0x80000000:
                        raise ValueError('Expected PE resource data entry')
                    address, length, _, _ = self.unpack('<IIII', resource(leaf, 16))
                    if length < 92 or length > 1024 * 1024:
                        raise ValueError('Invalid version resource size')
                    blob = self.take(self.rva(address, length), length)
                    record_size, value_size, kind = struct.unpack('<HHH', blob[:6])
                    if not 92 <= record_size <= length or value_size != 52 or kind != 0:
                        raise ValueError('Invalid fixed version record extent/type')
                    if blob[6:38] != 'VS_VERSION_INFO\0'.encode('utf-16le'):
                        raise ValueError('Invalid version resource key')
                    signature, structure_version, high, low = struct.unpack('<IIII', blob[40:56])
                    if signature != 0xfeef04bd or structure_version != 0x10000:
                        raise ValueError('Invalid fixed version signature')
                    versions.append(f'{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}')
        if not versions or len(set(versions)) != 1:
            raise ValueError('Missing or ambiguous embedded file version')
        return versions[0]


def compare_exports(previous, candidate):
    """Check static export coverage only, never ABI or runtime compatibility.

    Pass dictionaries returned by inspect_dll. Every old occupied ordinal is
    checked, including ordinal-only entries and the name-to-ordinal association.
    Function signatures, ordinal-only identity and forwarded dependencies cannot
    be established by PE table inspection; a passing result is preliminary.
    """
    missing = sorted(set(previous['exports']) - set(candidate['exports']))
    old, new = previous['export_ordinals'], candidate['export_ordinals']
    missing_ordinals = sorted(int(value) for value in old.keys() - new.keys())
    changed = sorted(int(value) for value in old.keys() & new.keys()
                     if not set(old[value]).issubset(new[value]))
    return {'ok': not (missing or missing_ordinals or changed),
            'missing_exports': missing, 'missing_ordinals': missing_ordinals,
            'changed_ordinals': changed, 'preliminary': True, 'runtime_verified': False,
            'warnings': ['Export coverage does not prove ABI, ordinal-only function identity, '
                         'forwarded dependency availability, or runtime compatibility.']}


def _inspect(data):
    pe = _PE(data)
    return {'version': pe.version(), 'architecture': 'x86_64',
            'sha256': hashlib.sha256(data).hexdigest(), **pe.exports()}


def inspect_dll(path):
    """Return inert DLL metadata; malformed or inaccessible files raise errors."""
    # Lazy import allows Manager itself to import this utility without a cycle.
    from .xmx import regular_file
    with regular_file(Path(path)) as source:
        if not 0 < os.fstat(source.fileno()).st_size <= MAX_DLL_BYTES:
            raise ValueError('DLL size exceeds inspection limit or is empty')
        data = source.read(MAX_DLL_BYTES + 1)
        if len(data) > MAX_DLL_BYTES:
            raise ValueError('DLL size exceeds inspection limit')
        return _inspect(data)


SDK_URL = 'https://github.com/intel/xess/releases/download/v3.0.2/XeSS_SDK_3.0.2.zip'
DLL_VERSION = '2.0.2.68'
_DLL_ENTRY = 'bin/libxess.dll'
MAX_DLL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if (url.scheme != 'https' or url.hostname not in {
                'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                or url.username is not None or url.password is not None or url.port not in (None, 443)):
            raise ValueError('SDK redirect is not an official HTTPS asset URL')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _https_context():
    context = ssl.create_default_context()
    # Decky's frozen Python/OpenSSL may not find the host distribution's CA
    # bundle. Add a system-owned trust store explicitly, without weakening TLS.
    # An explicit administrator SSL_CERT_* selection must remain authoritative.
    if 'SSL_CERT_FILE' not in os.environ and 'SSL_CERT_DIR' not in os.environ:
        for bundle in ('/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
                       '/etc/ssl/certs/ca-certificates.crt', '/etc/ssl/cert.pem',
                       '/etc/pki/tls/certs/ca-bundle.crt'):
            if Path(bundle).is_file():
                context.load_verify_locations(cafile=bundle)
                break
    return context


def _download_archive():
    # Check the 120s body budget between single raw reads. The socket timeout
    # bounds read inactivity to 10s; HTTPS setup/header parsing still uses the
    # transport's inactivity timeout, not a hard whole-request deadline.
    deadline = time.monotonic() + 120
    request = urllib.request.Request(SDK_URL, headers={'Accept-Encoding': 'identity'})
    with urllib.request.build_opener(_OfficialRedirects(), urllib.request.HTTPSHandler(
            context=_https_context())).open(request, timeout=10) as response:
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('Unexpected SDK download content encoding')
        declared = response.headers.get('Content-Length')
        if declared is not None:
            if not declared.isascii() or not declared.isdecimal():
                raise ValueError('Invalid SDK download Content-Length')
            declared = int(declared)
            if not 0 < declared <= MAX_ARCHIVE_BYTES:
                raise ValueError('SDK archive download size exceeds limit or is empty')
        output = io.BytesIO()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('SDK download exceeded total time budget')
            # HTTPResponse.read(size) may perform many raw reads while a peer
            # trickles bytes below the socket timeout. read1 performs at most
            # one buffered/raw read so the total budget is checked between them.
            chunk = response.read1(min(_CHUNK_BYTES, MAX_ARCHIVE_BYTES + 1 - output.tell()))
            if time.monotonic() >= deadline:
                raise TimeoutError('SDK download exceeded total time budget')
            if not chunk:
                break
            output.write(chunk)
            if output.tell() > MAX_ARCHIVE_BYTES:
                raise ValueError('SDK archive download size exceeds limit')
        if not output.tell() or (declared is not None and output.tell() != declared):
            raise ValueError('Truncated or empty SDK archive download')
        return output.getvalue()


def _archive_dll(data):
    if hashlib.sha256(data).hexdigest() != SDK_ARCHIVE_SHA256:
        raise ValueError('Pinned SDK archive SHA256 mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        matches, seen = [], set()
        for info in archive.infolist():
            name = info.filename
            parts = name.rstrip('/').split('/')
            if (info.orig_filename != name or '\\' in name or ':' in name or
                    any(part in ('', '.', '..') for part in parts)):
                raise ValueError('Unsafe SDK archive member name')
            if name.casefold() in seen:
                raise ValueError('Duplicate or case-ambiguous SDK archive member')
            seen.add(name.casefold())
            if parts[-1].casefold() == 'libxess.dll':
                matches.append(info)
        if len(matches) != 1 or matches[0].filename != _DLL_ENTRY:
            raise ValueError('SDK archive must contain exactly the intended bin/libxess.dll')
        info = matches[0]
        kind = stat.S_IFMT(info.external_attr >> 16)
        if info.is_dir() or kind not in (0, stat.S_IFREG) or info.flag_bits & 1:
            raise ValueError('SDK DLL entry must be an unencrypted regular file')
        if not 0 < info.file_size <= MAX_DLL_BYTES:
            raise ValueError('SDK DLL entry size exceeds limit or is empty')
        with archive.open(info) as source:
            payload = source.read(MAX_DLL_BYTES + 1)
        if len(payload) != info.file_size or len(payload) > MAX_DLL_BYTES:
            raise ValueError('SDK DLL entry size mismatch or exceeds limit')
        return payload


def _verify_dll(data):
    if hashlib.sha256(data).hexdigest() != DLL_SHA256:
        raise ValueError('Pinned libxess.dll SHA256 mismatch')
    result = _inspect(data)
    if result['version'] != DLL_VERSION:
        raise ValueError('Pinned libxess.dll embedded version mismatch')
    return result


def prepare_sdk(manager):
    """Explicitly prepare only the pinned cache; caller holds manager._lock.

    No game or prefix is read or changed. All writes use Manager's target-user
    and pinned-directory guards. Errors are returned without adopting bad files.
    """
    target = Path(manager.root)/'sdk-cache'/SDK_TAG/'libxess.dll'
    result = {'ok': False, 'path': str(target), 'sdk_tag': SDK_TAG,
              'version': None, 'sha256': None, 'cached': False}
    try:
        manager._mkdir(target.parent)
        try:
            inspected = inspect_dll(target)
        except FileNotFoundError:
            pass
        else:
            manager._user_access(target, read=True)
            if inspected['sha256'] != DLL_SHA256 or inspected['version'] != DLL_VERSION:
                raise ValueError('Pinned libxess.dll cache SHA256/version mismatch')
            return {**result, **inspected, 'ok': True, 'cached': True}
        data = _archive_dll(_download_archive())
        _verify_dll(data)
        manager._atomic(target, data, mode=0o644, expected=None)
        manager._user_access(target, read=True)
        inspected = inspect_dll(target)
        if inspected['sha256'] != DLL_SHA256:
            raise ValueError('Pinned libxess.dll cache changed after write')
        return {**result, **inspected, 'ok': True}
    except Exception as exc:
        return {**result, 'error': str(exc)}
