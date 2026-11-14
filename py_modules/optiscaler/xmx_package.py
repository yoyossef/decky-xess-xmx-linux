"""Inert, opt-in upstream package preparation. Never installs or activates games."""
import json
import hashlib
import os
import stat
import zipfile
import re
import io
import time
import urllib.parse
import urllib.request
import ctypes
import fcntl
import shutil
import struct
import uuid
import selectors
import signal
import subprocess
from pathlib import Path

from .xmx import regular_file, snapshot

from .xess_sdk import _https_context


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (req.host == 'api.github.com' or parsed.scheme != 'https'
                or parsed.hostname not in {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError('Redirect is not an official HTTPS release asset')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_cancel(cancel, deadline):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Package preparation cancelled')
    if time.monotonic() >= deadline:
        raise TimeoutError('Package preparation exceeded time budget')


def read_url(url, limit, *, cancel=None, deadline=None):
    """Bounded verified HTTPS, without proxies, cookies, auth or env credentials."""
    deadline = min(deadline or float('inf'), time.monotonic()+180)
    _check_cancel(cancel, deadline)
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {
            'api.github.com', 'github.com', 'raw.githubusercontent.com'}
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('Not an official HTTPS source')
    request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity',
                                                  'User-Agent': 'Decky-XMX-package-preparation'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirects(),
                                         urllib.request.HTTPSHandler(context=_https_context()))
    with opener.open(request, timeout=10) as response:
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('Unexpected download encoding')
        declared = response.headers.get('Content-Length')
        if declared is not None:
            if not declared.isascii() or not declared.isdecimal() or not 0 < int(declared) <= limit:
                raise ValueError('Invalid or oversized Content-Length')
            declared = int(declared)
        output = io.BytesIO()
        while True:
            _check_cancel(cancel, deadline)
            chunk = response.read1(min(1024*1024, limit+1-output.tell()))
            _check_cancel(cancel, deadline)
            if not chunk:
                break
            output.write(chunk)
            if output.tell() > limit:
                raise ValueError('Download exceeds byte budget')
        if not output.tell() or (declared is not None and output.tell() != declared):
            raise ValueError('Truncated or empty download')
        return output.getvalue()

REPO = 'HHHHanasak1/xess-xmx-linux'
LATEST_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
# Release validated against this plugin build. prepare() downloads ONLY this
# tag; bump it (plus tests + release) after validating a new upstream tag.
PINNED_TAG = '20261113'
PINNED_URL = f'https://api.github.com/repos/{REPO}/releases/tags/{PINNED_TAG}'
MAX_ARCHIVE = 128 * 1024 * 1024


def select_release(value):
    """Only the unambiguous official release ZIP with a mandatory API digest."""
    tag = value.get('tag_name')
    if (not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', tag)
            or value.get('draft') is not False or value.get('prerelease') is not False):
        raise ValueError('Invalid stable upstream release metadata')
    name = f'xess-xmx-linux-{tag}.zip'
    assets = value.get('assets')
    if not isinstance(assets, list) or len(assets) > 256:
        raise ValueError('Invalid bounded release assets')
    matches = [a for a in assets if isinstance(a, dict) and a.get('name') == name]
    if len(matches) != 1:
        raise ValueError('Expected exactly one official package ZIP')
    asset = matches[0]
    url = f'https://github.com/{REPO}/releases/download/{tag}/{name}'
    sha = asset.get('digest')
    if (asset.get('browser_download_url') != url or not isinstance(sha, str)
            or not re.fullmatch('sha256:[0-9a-f]{64}', sha)
            or type(asset.get('size')) is not int or not 0 < asset['size'] <= MAX_ARCHIVE):
        raise ValueError('Official ZIP requires a valid API SHA256 digest, URL and bounded size')
    return {'tag': tag, 'url': url, 'size': asset['size'], 'archive_sha256': sha[7:]}


MAX_MEMBERS = 4096
MAX_EXTRACTED = 256 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024


def _fresh_dir(manager, path, mode=0o700):
    with manager._dirfd(path.parent) as parent:
        _write_access(manager, parent)
        os.mkdir(path.name, mode=mode, dir_fd=parent)
        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            owner = manager.home.stat()
            os.fchown(child, owner.st_uid, owner.st_gid)
            os.fchmod(child, mode)
        finally:
            os.close(child)


def extract_archive(manager, data, release, destination, *, cancel=None, deadline=float('inf')):
    """Validate every ZIP entry before writing; no links, aliases or overwrites."""
    if (len(data) != release['size'] or len(data) > MAX_ARCHIVE
            or hashlib.sha256(data).hexdigest() != release['archive_sha256']):
        raise ValueError('Release archive SHA256/size mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError('ZIP member count exceeds limit')
        total, entries, seen = 0, [], {}
        for info in infos:
            name = info.filename
            parts = name.rstrip('/').split('/')
            kind = stat.S_IFMT(info.external_attr >> 16)
            if (info.orig_filename != name or len(name) > 512 or '\\' in name or ':' in name
                    or any(part in ('', '.', '..') or any(ord(c) < 32 for c in part) for part in parts)
                    or parts[0] != 'xess-xmx-linux-'+release['tag']
                    or info.flag_bits & 1 or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or kind == stat.S_IFDIR and not info.is_dir()
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError('Unsafe ZIP member name/type')
            canonical = '/'.join(parts).casefold()
            if canonical in seen:
                raise ValueError('Duplicate/case-ambiguous ZIP entry')
            seen[canonical] = info.is_dir()
            total += info.file_size
            if (info.file_size > MAX_MEMBER or total > MAX_EXTRACTED
                    or info.file_size > max(1, info.compress_size)*1000):
                raise ValueError('ZIP expanded size/ratio exceeds limit')
            if len(parts) > 1:
                entries.append((info, Path(*parts[1:])))
            elif not info.is_dir():
                raise ValueError('ZIP root must be a directory')
        for name in seen:
            if any(seen.get('/'.join(name.split('/')[:i])) is False for i in range(1, len(name.split('/')))):
                raise ValueError('ZIP file/directory collision')
        # Also reject case ambiguity in implicit parent directories.
        spellings = {}
        for info, relative in entries:
            for parent in (relative, *relative.parents):
                spelling = str(parent)
                if spellings.setdefault(spelling.casefold(), spelling) != spelling:
                    raise ValueError('Case-ambiguous ZIP parent')
        _fresh_dir(manager, destination)
        originals = {}
        for info, relative in entries:
            _check_cancel(cancel, deadline)
            target = destination/relative
            if info.is_dir():
                manager._mkdir(target)
                continue
            manager._mkdir(target.parent)
            with archive.open(info) as source:
                payload = source.read(min(MAX_MEMBER, info.file_size)+1)
            if len(payload) != info.file_size:
                raise ValueError('ZIP member size mismatch')
            mode = 0o755 if (info.external_attr >> 16) & 0o111 else 0o644
            manager._atomic(target, payload, mode=mode, expected=None)
            originals[str(relative)] = {'sha256': hashlib.sha256(payload).hexdigest(), 'mode': mode}
        return originals


PROVIDER = 'decky-optiscaler/xmx-package'
RECEIPT = '.decky-xmx-package.json'
STATE_FILE = 'package-preparation.json'


def _write_access(manager, fd):
    owner = manager.home.stat()
    if owner.st_uid == 0:
        raise ValueError('A non-root target user home is required')
    permissions = manager._access_permissions(fd, os.fstat(fd), owner.st_uid, manager._user_groups())
    if not any(bits & 3 == 3 for bits in permissions):
        raise ValueError('Directory is not writable by the target user')


def _guard(manager, *, write=False):
    with manager._dirfd(manager.home) as fd:
        if manager.home.stat().st_uid == 0:
            raise ValueError('A non-root target user home is required')
        if write:
            _write_access(manager, fd)
    # Validate lexical settings components even if its final directory is absent.
    try:
        with manager._dirfd(manager.root):
            pass
    except FileNotFoundError:
        parent = manager.root.parent
        while not parent.exists() and not parent.is_symlink():
            parent = parent.parent
        with manager._dirfd(parent):
            pass


def _json_file(manager, path, limit=16384):
    with regular_file(path, authorize=manager._fd_access) as source:
        data = source.read(limit+1)
    if len(data) > limit:
        raise ValueError('Oversized package ownership metadata')
    return json.loads(data)


def _identity(value):
    if (not isinstance(value, dict) or set(value) != {'tag', 'archive_sha256'}
            or not isinstance(value['tag'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['tag'])
            or not isinstance(value['archive_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', value['archive_sha256'])):
        raise ValueError('Invalid package release identity')


def _state(manager, *, allow_missing_package=False):
    try:
        state = _json_file(manager, manager.root/STATE_FILE)
    except FileNotFoundError:
        return {'ok': True, 'state': 'idle', 'package': None, 'latest': None}
    if (not isinstance(state, dict) or state.get('provider') != PROVIDER or state.get('schema') != 1
            or state.get('state') not in {'idle', 'preparing', 'ready', 'error'}):
        raise ValueError('Unrecognized package preparation ownership record; preserved')
    if state.get('latest') is not None:
        _identity(state['latest'])
    if state.get('error') and (not isinstance(state['error'], str) or len(state['error']) > 2048):
        raise ValueError('Invalid package preparation error record')
    package = state.get('package')
    if package is not None:
        if not isinstance(package, dict) or set(package) != {'tag', 'path', 'archive_sha256', 'runtime_verified'}:
            raise ValueError('Invalid prepared package record')
        _identity({k: package[k] for k in ('tag', 'archive_sha256')})
        if not isinstance(package['path'], str) or package['runtime_verified'] is not False:
            raise ValueError('Invalid prepared package path/runtime claim')
        path = Path(package['path'])
        if (not path.is_absolute() or path.parent != manager.home
                or not re.fullmatch(r'xess-xmx-linux-[A-Za-z0-9_.-]+', path.name)):
            raise ValueError('Prepared package escapes target HOME')
        try:
            with manager._dirfd(path):
                pass
            receipt = _json_file(manager, path/RECEIPT, limit=65536)
        except FileNotFoundError:
            if not allow_missing_package:
                raise
            # Explicit preparation may replace its pointer to a missing
            # directory/receipt, never repair or overwrite the old directory.
            # The caller archives this already-schema-validated record first.
            return {'ok': True, 'state': 'error', 'package': None,
                    'latest': state.get('latest'), 'missing_package_record': state}
        if (receipt.get('provider') != PROVIDER or receipt.get('schema') != 1
                or receipt.get('tag') != package['tag']
                or receipt.get('archive_sha256') != package['archive_sha256']
                or receipt.get('path') != str(path) or receipt.get('runtime_verified') is not False
                or not re.fullmatch('[0-9a-f]{64}', receipt.get('tree_sha256', ''))):
            raise ValueError('Prepared package ownership receipt mismatch')
    result = {'ok': True, 'state': state['state'], 'package': package, 'latest': state.get('latest')}
    if state.get('error'):
        result['error'] = state['error']
    return result


def check_upstream(manager):
    """Visibility only: is there an upstream release newer than the pin?

    Never downloads, never mutates state; any failure degrades to unknown
    so a GitHub outage cannot block the pinned Download."""
    try:
        release = select_release(json.loads(read_url(LATEST_URL, 1024*1024)))
        newer = release['tag'] != PINNED_TAG
        return {'ok': True, 'available': newer, 'latest_tag': release['tag'] if newer else None,
                'pinned': PINNED_TAG, 'error': None}
    except Exception as exc:
        return {'ok': True, 'available': False, 'latest_tag': None, 'pinned': PINNED_TAG,
                'error': str(exc)[:256]}


def get_status(manager):
    """Local-only ownership status, never a network request or runtime claim."""
    try:
        _guard(manager)
        state = _state(manager)
        if state['state'] == 'preparing':
            # A crash leaves its staging tree in place. Do not scavenge it or
            # call it ready. Read-only flock detects a live preparer, not a PID.
            with regular_file(manager.root/'.lock', authorize=manager._fd_access) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {**state, 'pinned': PINNED_TAG}
            return {**state, 'pinned': PINNED_TAG, 'state': 'error',
                    'error': 'Previous package preparation was interrupted; previous package retained'}
        return {**state, 'pinned': PINNED_TAG}
    except Exception as exc:
        return {'ok': True, 'state': 'error', 'package': None, 'latest': None, 'pinned': PINNED_TAG,
                'error': str(exc)[:2048]}


def _save_state(manager, state):
    path = manager.root/STATE_FILE
    manager._atomic(path, json.dumps({'provider': PROVIDER, 'schema': 1, **state}, sort_keys=True).encode(),
                    expected=snapshot(path))


def _tree(manager, package):
    count, size = 0, 0
    for directory, dirs, files in os.walk(package, followlinks=False):
        for name in dirs+files:
            path = Path(directory)/name
            st = path.lstat()
            count += 1
            size += st.st_size
            if count > 8192 or size > 1024*1024*1024:
                raise ValueError('Prepared package exceeds filesystem budget')
            if stat.S_ISLNK(st.st_mode):
                if not path.resolve(strict=True).is_relative_to(package):
                    raise ValueError('Prepared package symlink escapes its boundary')
            elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode) and st.st_nlink == 1):
                raise ValueError('Prepared package contains a special/hard-linked file')
            if not stat.S_ISLNK(st.st_mode) and st.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022):
                raise ValueError('Unsafe prepared package permissions')
            if st.st_uid != manager.home.stat().st_uid:
                raise ValueError('Prepared package is not target-user owned')


def _validate(manager, package, originals):
    _tree(manager, package)
    for relative, expected in originals.items():
        path = package/relative
        with regular_file(path, authorize=manager._fd_access) as source:
            if (hashlib.file_digest(source, 'sha256').hexdigest() != expected['sha256']
                    or stat.S_IMODE(os.fstat(source.fileno()).st_mode) != expected['mode']):
                raise ValueError('Archive original changed during preparation: '+relative)
    result = manager._package(package)
    if not result['valid']:
        raise ValueError('; '.join(result['errors']))
    for relative in ('lib/deps/libSPIRV-Tools.so', 'lib/deps/libdisplay-info.so.3'):
        with regular_file(package/relative, authorize=manager._fd_access) as source:
            header = source.read(64)
        if (len(header) < 64 or header[:7] != b'\x7fELF\x02\x01\x01'
                or struct.unpack_from('<H', header, 18)[0] != 62):
            raise ValueError('Missing/invalid required runtime dependency: '+relative)
    for relative in ('xess/libxess.dll', 'xess/libxess_dx11.dll', 'xess/libxess_fg.dll', 'xess/libxell.dll',
                     'ffx/amd_fidelityfx_loader_dx12.dll', 'ffx/amd_fidelityfx_upscaler_dx12.dll',
                     'ffx/amd_fidelityfx_framegeneration_dx12.dll'):
        with regular_file(package/relative, authorize=manager._fd_access) as source:
            header = source.read(4096)
        offset = struct.unpack_from('<I', header, 60)[0] if len(header) >= 64 else 0
        if (header[:2] != b'MZ' or offset < 64 or offset+26 > len(header)
                or header[offset:offset+4] != b'PE\0\0'
                or struct.unpack_from('<H', header, offset+4)[0] != 0x8664
                or struct.unpack_from('<H', header, offset+24)[0] != 0x20b
                or not struct.unpack_from('<H', header, offset+22)[0] & 0x2000):
            raise ValueError('Missing/invalid prepared PE64 dependency: '+relative)


def _publish(source_fd, source_name, home_fd, target_name):
    """Linux exclusive rename; never replace even an empty foreign directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise RuntimeError('Atomic no-replace publication requires Linux renameat2')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source_name), home_fd, os.fsencode(target_name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    os.fsync(source_fd)
    os.fsync(home_fd)


MAX_WORKSPACE = 1536 * 1024 * 1024
MAX_OUTPUT = 128 * 1024
_LIMIT_EXEC = ('import os,resource,sys; '
               'resource.setrlimit(resource.RLIMIT_FSIZE,(268435456,268435456)); '
               'resource.setrlimit(resource.RLIMIT_CPU,(300,300)); '
               'resource.setrlimit(resource.RLIMIT_NOFILE,(256,256)); '
               'os.execv(sys.argv[1],sys.argv[1:])')


def _workspace_budget(path):
    count, total = 0, 0
    # fwalk pins directory handles and never follows a substituted symlink.
    for _, dirs, files, fd in os.fwalk(path, follow_symlinks=False):
        for name in dirs+files:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue  # A helper legitimately moved/removed its temporary.
            count += 1
            total += st.st_size
            if count > 16384 or total > MAX_WORKSPACE:
                raise RuntimeError('Preparation workspace exceeds filesystem budget')


# System directories mirrored read-only into the offline sandbox.
_SANDBOX_BINDS = ('bin', 'sbin', 'lib', 'lib64')


def _elf_needed(path):
    """DT_NEEDED sonames of an ELF64 file; [] for missing/non-ELF/truncated input."""
    try:
        with open(path, 'rb') as source:
            header = source.read(64)
            if len(header) < 64 or header[:7] != b'\x7fELF\x02\x01\x01':
                return []
            e_phoff, = struct.unpack_from('<Q', header, 32)
            e_phentsize, e_phnum = struct.unpack_from('<HH', header, 54)
            table = source.read(min(e_phentsize*max(e_phnum, 1), 65536))
            loads = []
            dynamic_index = -1
            for index in range(0, len(table)-55, max(e_phentsize, 1)):
                p_type, = struct.unpack_from('<I', table, index)
                if p_type == 2:  # PT_DYNAMIC
                    dynamic_index = index
                elif p_type == 1:  # PT_LOAD
                    offset, vaddr, filesz = struct.unpack_from('<QQQ', table, index+8)[0], struct.unpack_from('<Q', table, index+8+8)[0], struct.unpack_from('<Q', table, index+8+32)[0]
                    loads.append((vaddr, offset, filesz))
            dynamic = dynamic_index >= 0
            if dynamic is None:
                return []
            d_offset, _d_vaddr, _d_paddr, d_filesz = struct.unpack_from('<QQQQ', table, dynamic_index+8)
            source.seek(d_offset)
            entries = source.read(min(d_filesz, 1024*1024))
            strtab = None
            for index in range(0, len(entries)-15, 16):
                d_tag, d_value = struct.unpack_from('<qQ', entries, index)
                if d_tag == 0:
                    break
                if d_tag == 5:
                    strtab = d_value
            if strtab is None:
                return []
            for vaddr, offset, filesz in loads:
                if vaddr <= strtab < vaddr+filesz:
                    source.seek(offset+(strtab-vaddr))
                    strings = source.read(min(filesz-(strtab-vaddr), 1024*1024))
                    break
            else:
                return []
            needed = []
            for index in range(0, len(entries)-15, 16):
                d_tag, d_value = struct.unpack_from('<qQ', entries, index)
                if d_tag == 0:
                    break
                if d_tag == 1 and d_value < len(strings):
                    end = strings.find(b'\0', d_value)
                    if 0 <= end <= len(strings):
                        name = strings[d_value:end].decode('utf-8', 'replace')
                        if name and name not in needed:
                            needed.append(name)
            return needed
    except OSError:
        return []


# Mirrors the upstream session-check dependency set: only what the Steam Linux
# Runtime lacks and the package ships a build of in lib/bundled.
_DEP_NAME = re.compile(r'lib(?:SPIRV-Tools[A-Za-z-]*|display-info)\.so[0-9.]*$')


def _populate_deps(*, manager, package, originals):
    """Stage packagable driver NEEDED deps that the host could not provide.

    session-check copies host libraries into lib/deps and falls back to
    lib/bundled only when it can locate them; a host without the library at
    all (Fedora ships no libdisplay-info.so.3) leaves lib/deps incomplete.
    Copies the verified package-owned bundled build instead. Returns the
    staged relative paths."""
    staged = []
    dependencies = package/'lib/deps'
    sources = [driver for driver in sorted(package.glob('lib/mesa-*/libvulkan_intel.so'))]
    pending = {driver for driver in sources}
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for name in _elf_needed(current):
            if not _DEP_NAME.fullmatch(name) or (dependencies/name).exists():
                continue
            bundled = package/'lib/bundled'/name
            record = originals.get('lib/bundled/'+name)
            if record is None or not bundled.is_file():
                continue
            with regular_file(bundled, authorize=manager._fd_access) as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != record['sha256']:
                    raise ValueError('Bundled dependency does not match the archive original: '+name)
                source.seek(0)
                data = source.read()
            manager._mkdir(dependencies)
            manager._atomic(dependencies/name, data, mode=0o644, expected=None)
            staged.append('lib/deps/'+name)
            pending.add(bundled)  # the staged build may pull further packagable deps (SPIRV-Tools -> display-info)
    # The run path expects every packagable dependency the package ships a build
    # of, not only those a driver build happens to link: an update that changes
    # the Mesa builds must not silently empty lib/deps for the games.
    bundled_dir = package/'lib/bundled'
    if bundled_dir.is_dir():
        for path in sorted(bundled_dir.iterdir()):
            name = path.name
            if not path.is_file() or not _DEP_NAME.fullmatch(name) or (dependencies/name).exists():
                continue
            record = originals.get('lib/bundled/'+name)
            if record is None:
                continue
            with regular_file(path, authorize=manager._fd_access) as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != record['sha256']:
                    raise ValueError('Bundled dependency does not match the archive original: '+name)
                source.seek(0)
                data = source.read()
            manager._mkdir(dependencies)
            manager._atomic(dependencies/name, data, mode=0o644, expected=None)
            staged.append('lib/deps/'+name)
    return sorted(staged)


def _sandbox(manager, staging, command, cancel, deadline, *, allow_failure=False, env=None, extra_args=()):
    """Offline bubblewrap: only fresh work is writable; never fall back."""
    bwrap = shutil.which('bwrap', path='/usr/bin:/bin')
    if not bwrap:
        raise RuntimeError('Package preparation requires bubblewrap (bwrap)')
    owner = manager.home.stat()
    if owner.st_uid == 0 or os.geteuid() not in (0, owner.st_uid):
        raise ValueError('Cannot safely run package preparation as target user')
    for relative in ('work', 'downloads'):
        with manager._dirfd(staging/relative):
            pass
    for relative in ('home', 'tmp', 'cache'):
        manager._mkdir(staging/'work'/relative)
    args = [bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
            '--cap-drop', 'ALL', '--uid', str(owner.st_uid), '--gid', str(owner.st_gid),
            '--ro-bind', '/usr', '/usr']
    for name in _SANDBOX_BINDS:
        path = Path('/')/name
        if path.is_symlink():
            args += ['--symlink', os.readlink(path), str(path)]
        elif path.exists():
            args += ['--ro-bind', str(path), str(path)]
    args += ['--dir', '/etc']
    # No /run, real HOME, D-Bus, devices, Steam, sessions or credentials.
    # Resolved resolv.conf avoids a dangling /run symlink on Fedora systems;
    # helpers nevertheless run with a private, disconnected network namespace.
    for name in ('resolv.conf', 'hosts', 'nsswitch.conf', 'ld.so.cache', 'ssl', 'pki', 'crypto-policies'):
        path = Path('/etc')/name
        if path.exists():
            args += ['--ro-bind', str(path.resolve(strict=True)), str(path)]
    args += ['--proc', '/proc', '--dev', '/dev', '--bind', str(staging/'work'), '/work',
             '--ro-bind', str(staging/'downloads'), '/downloads', '--chdir', '/work']
    args += list(extra_args)
    environment = {'HOME': '/work/home', 'TMPDIR': '/work/tmp', 'XDG_CACHE_HOME': '/work/cache',
                   'PATH': '/work/bin:/usr/bin:/usr/sbin', 'LANG': 'C', 'LC_ALL': 'C'}
    if env:
        environment.update(env)
    for key, value in environment.items():
        args += ['--setenv', key, value]
    args += ['--', '/usr/bin/python3', '-I', '-c', _LIMIT_EXEC, *command]
    _check_cancel(cancel, deadline)
    dropped = {'user': owner.st_uid, 'group': owner.st_gid, 'extra_groups': []} if os.geteuid() == 0 else {}
    deadline = min(deadline, time.monotonic()+300)
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
                               cwd='/', start_new_session=True, **dropped)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                _check_cancel(cancel, deadline)
                _workspace_budget(staging)
                for key, _ in selector.select(.1):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output)+len(chunk) > MAX_OUTPUT:
                        raise RuntimeError('Sandbox output exceeds bounded output budget')
                    output.extend(chunk)
            process.wait(timeout=max(.01, deadline-time.monotonic()))
        _check_cancel(cancel, deadline)
        _workspace_budget(staging)
        text = output.decode('utf-8', 'replace')
        if process.returncode and not (allow_failure and process.returncode == 1):
            raise RuntimeError('Isolated preparation failed: '+text[-4096:])
        return text
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        process.stdout.close()


# Helpers are authenticated by the selected official archive's mandatory API
# SHA256, not a separately shipped list of frozen script or dependency versions.
_HELPERS = ('tools/get-igc.sh', 'tools/get-xess.sh', 'tools/get-ffx.sh', 'tools/session-check.sh')
MAX_DEPENDENCY_DOWNLOADS = 48
MAX_DEPENDENCY_BYTES = 384 * 1024 * 1024
_DEPENDENCY_REPOS = {'intel/intel-graphics-compiler', 'intel/compute-runtime', 'intel/xess'}
_RELEASE_TOKEN = r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}'
_REQUESTS = '.decky-download-requests.jsonl'

# A missing URL is a discovery request, NOT permission for a helper to connect.
# The host validates and fetches it, then retries the still-networkless helper.
# Both the adapter directory and downloaded responses are bound read-only.
_CURL_REPLAY = r'''#!/usr/bin/python3
import json, pathlib, shutil, sys, urllib.parse

def canonical(url):
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=urllib.parse.unquote(parts.path)))
args = sys.argv[1:]
index = json.loads(pathlib.Path('/downloads/index.json').read_text())
if len(args) == 2 and args[0] == '-s':
    url, output = args[1], None
elif len(args) == 4 and args[:2] == ['-sfL', '-o']:
    url, output = args[3], pathlib.Path(args[2]).resolve()
    if not output.is_relative_to('/work'): sys.exit('Helper output escapes fresh work')
else:
    sys.exit('Unsupported helper curl invocation')
if not url: sys.exit('Empty helper URL (metadata not yet available)')
name = index.get(url) or index.get(canonical(url))
if name is None:
    with pathlib.Path('/work/.decky-download-requests.jsonl').open('a') as requests:
        requests.write(json.dumps(url)+'\n')
    sys.exit('Dependency requires official host-side verification')
source = pathlib.Path('/downloads')/name
if output is None:
    sys.stdout.buffer.write(source.read_bytes())
else:
    shutil.copyfile(source, output)
'''


def _canonical_lookup(url):
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=urllib.parse.unquote(parts.path)))


def _dependency_source(url):
    """Narrow origin AND path policy. Never authorize GitHub by hostname alone."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid official dependency URL')
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != parsed.hostname
            or parsed.query or parsed.fragment
            or re.sub(r'%2[bB]', '+', parsed.path) != path
            or any(p in ('.', '..', '') for p in path.split('/')[1:])):
        raise ValueError('Invalid official dependency URL')
    if parsed.hostname == 'api.github.com':
        match = re.fullmatch(r'/repos/([^/]+/[^/]+)/releases/(?:tags/('+_RELEASE_TOKEN+r')|(latest))', path)
        if match and match[1] in _DEPENDENCY_REPOS:
            return {'kind': 'metadata', 'repo': match[1], 'tag': match[2]}
    if parsed.hostname == 'github.com':
        match = re.fullmatch(r'/([^/]+/[^/]+)/releases/download/('+_RELEASE_TOKEN+r')/('+_RELEASE_TOKEN+r')', path)
        if match and match[1] in _DEPENDENCY_REPOS:
            return {'kind': 'asset', 'repo': match[1], 'tag': match[2], 'name': match[3]}
    if (parsed.hostname == 'raw.githubusercontent.com' and re.fullmatch(
            r'/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}'
            r'/Kits/FidelityFX/(?:signedbin/amd_fidelityfx_(?:loader|upscaler|framegeneration)_dx12\.dll|docs/license\.md)',
            parsed.path)):
        return {'kind': 'content'}
    raise ValueError('Unsupported official dependency origin/path')


def _prepare_dependencies(manager, staging, cancel, deadline, originals=None):
    package = staging/'work/package'
    helpers = {}
    for name in _HELPERS:
        with regular_file(package/name, authorize=manager._fd_access) as source:
            data = source.read(1024*1024+1)
        actual = hashlib.sha256(data).hexdigest()
        if (originals is None or name not in originals or len(data) > 1024*1024
                or actual != originals[name]['sha256']):
            raise ValueError('Package helper differs from verified archive: '+name)
        helpers[name] = actual
    for executable in ('bwrap', 'bash', 'ar', 'tar', 'zstd', 'python3', 'sha256sum'):
        if not shutil.which(executable, path='/usr/bin:/bin:/usr/sbin:/sbin'):
            raise RuntimeError('Missing package preparation prerequisite: '+executable)
    downloads = staging/'downloads'
    with manager._dirfd(downloads.parent):
        pass
    if not downloads.exists():
        _fresh_dir(manager, downloads)
    with manager._dirfd(downloads):
        if any(downloads.iterdir()):
            raise ValueError('Dependency download workspace is not fresh')
    _populate_deps(manager=manager, package=package, originals=originals)
    _sandbox(manager, staging, ['/usr/bin/true'], cancel, deadline)
    manager._mkdir(downloads/'bin')
    manager._atomic(downloads/'bin/curl', _CURL_REPLAY.encode(), mode=0o755, expected=None)
    manager._mkdir(staging/'work/bin')
    index, metadata_cache, receipts, total = {}, {}, [], 0

    def fetch(url):
        nonlocal total
        _check_cancel(cancel, deadline)
        source = _dependency_source(url)
        if _canonical_lookup(url) in index:
            return
        expected, declared = None, None
        verification = 'official-https-content'
        if source['kind'] == 'asset':
            metadata_url = f"https://api.github.com/repos/{source['repo']}/releases/tags/{source['tag']}"
            fetch(metadata_url)
            metadata = metadata_cache[_canonical_lookup(metadata_url)]
            matches = [a for a in metadata['assets'] if isinstance(a, dict)
                       and a.get('name') == source['name']]
            if len(matches) != 1:
                raise ValueError('Dependency asset is absent or ambiguous in official release metadata')
            asset = matches[0]
            if (_canonical_lookup(asset.get('browser_download_url', '')) != _canonical_lookup(url)
                    or type(asset.get('size')) is not int or not 0 < asset['size'] <= MAX_ARCHIVE):
                raise ValueError('Dependency asset URL/size differs from official release metadata')
            digest = asset.get('digest')
            if digest is None and source['repo'] in {'intel/intel-graphics-compiler', 'intel/compute-runtime'}:
                # Older Intel assets predate GitHub's digest field. The same
                # official HTTPS release API publishes exact filename checksums.
                body = metadata.get('body', '')
                sums = [match[1] for line in body.splitlines()
                        if (match := re.fullmatch(r'\s*([0-9a-f]{64})[ \t]+\*?'+re.escape(source['name'])+r'[ \t]*', line))] if isinstance(body, str) else []
                if len(sums) != 1:
                    raise ValueError('Official dependency release requires one exact filename SHA256')
                expected, verification = sums[0], 'github-release-body-sha256'
            elif isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                expected, verification = digest[7:], 'github-api-sha256'
            else:
                raise ValueError('Official dependency asset requires a SHA256 digest')
            declared = asset['size']
        if len(receipts) >= MAX_DEPENDENCY_DOWNLOADS:
            raise ValueError('Dependency download count exceeds budget')
        data = read_url(url, 1024*1024 if source['kind'] == 'metadata' else MAX_ARCHIVE,
                        cancel=cancel, deadline=deadline)
        total += len(data)
        if total > MAX_DEPENDENCY_BYTES:
            raise ValueError('Dependency downloads exceed aggregate byte budget')
        actual = hashlib.sha256(data).hexdigest()
        if expected is not None and (actual != expected or len(data) != declared):
            raise ValueError('Official dependency SHA256/size mismatch')
        receipts.append({'url': url, 'size': len(data), 'sha256': actual, 'verification': verification})
        if source['kind'] == 'metadata':
            metadata = json.loads(data)
            if (not isinstance(metadata, dict) or metadata.get('draft') is not False
                    or metadata.get('prerelease') is not False
                    or not isinstance(metadata.get('tag_name'), str)
                    or not re.fullmatch(_RELEASE_TOKEN, metadata['tag_name'])
                    or source['tag'] is not None and metadata['tag_name'] != source['tag']
                    or not isinstance(metadata.get('assets'), list) or len(metadata['assets']) > 256):
                raise ValueError('Invalid official dependency release metadata')
            metadata_cache[_canonical_lookup(url)] = metadata
            # Preserve content; helpers use grep/cut expecting one field per line.
            data = json.dumps(metadata, indent=2).encode()
        filename = 'download-'+str(len(receipts)-1)
        manager._atomic(downloads/filename, data, mode=0o644, expected=None)
        index[url] = index[_canonical_lookup(url)] = filename

    extra = ('--ro-bind', str(downloads/'bin'), '/work/bin')
    request_path = staging/'work'/_REQUESTS
    for name in _HELPERS:
        for attempt in range(MAX_DEPENDENCY_DOWNLOADS+1):
            _check_cancel(cancel, deadline)
            with regular_file(package/name, authorize=manager._fd_access) as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != helpers[name]:
                    raise ValueError('Package helper changed during preparation: '+name)
            manager._atomic(downloads/'index.json', json.dumps(index).encode(), mode=0o644,
                            expected=snapshot(downloads/'index.json'))
            with manager._dirfd(request_path.parent) as work:
                try:
                    os.unlink(request_path.name, dir_fd=work)
                except FileNotFoundError:
                    pass
            failure = None
            try:
                _sandbox(manager, staging, ['/usr/bin/bash', '/work/package/'+name,
                         '--dry-run' if name == _HELPERS[-1] else '/work/package'], cancel, deadline,
                         allow_failure=name == _HELPERS[-1], extra_args=extra,
                         env={'LD_LIBRARY_PATH': '/work/package/lib/deps'})
            except RuntimeError as exc:
                if not str(exc).startswith('Isolated preparation failed:'):
                    raise  # A sandbox output/disk/resource guard is not discovery.
                failure = exc
            try:
                with regular_file(request_path, authorize=manager._fd_access) as source:
                    requests = source.read(65537)
            except FileNotFoundError:
                requests = b''
            if len(requests) > 65536:
                raise ValueError('Dependency request log exceeds budget')
            urls = list(dict.fromkeys(json.loads(line) for line in requests.splitlines()))
            if not urls:
                if failure:
                    raise failure
                break
            for url in urls:
                fetch(url)
        else:
            raise ValueError('Dependency discovery retry budget exceeded')
    return {'helpers': helpers, 'downloads': receipts}


def prepare(manager, *, cancel=None):
    """Explicit prepare only; old versions, ownership and all game state stay put."""
    state = {'ok': True, 'state': 'error', 'package': None, 'latest': None}
    stage = None
    stage_identity = None
    deadline = time.monotonic()+900
    try:
        _guard(manager, write=True)
        with manager._lock():
            state = _state(manager, allow_missing_package=True)
            missing = state.pop('missing_package_record', None)
            if missing is not None:
                history = manager.root/'package-preparation-history'
                manager._mkdir(history)
                manager._atomic(history/(uuid.uuid4().hex+'.json'),
                                json.dumps(missing, sort_keys=True).encode(),
                                mode=0o600, expected=None)
            state = {k: v for k, v in state.items() if k != 'error'}
            try:
                state['state'] = 'preparing'
                _save_state(manager, state)
                _check_cancel(cancel, deadline)
                release = select_release(json.loads(read_url(PINNED_URL, 1024*1024, cancel=cancel, deadline=deadline)))
                state['latest'] = {k: release[k] for k in ('tag', 'archive_sha256')}
                _save_state(manager, state)
                stage = manager.home/('.decky-xmx-stage-'+uuid.uuid4().hex)
                _fresh_dir(manager, stage)
                stage_identity = stage.stat()
                manager._atomic(stage/'owner.json', json.dumps({'provider': PROVIDER, 'id': stage.name}).encode(), expected=None)
                _fresh_dir(manager, stage/'work')
                data = read_url(release['url'], MAX_ARCHIVE, cancel=cancel, deadline=deadline)
                originals = extract_archive(manager, data, release, stage/'work/package', cancel=cancel, deadline=deadline)
                manager._mkdir(stage/'work/package/kernels')
                preparation = _prepare_dependencies(manager, stage, cancel, deadline, originals=originals)
                _check_cancel(cancel, deadline)
                package = stage/'work/package'
                _validate(manager, package, originals)
                target = manager.home/(f"xess-xmx-linux-{release['tag']}-{release['archive_sha256'][:12]}-"+uuid.uuid4().hex[:12])
                receipt = {'provider': PROVIDER, 'schema': 1, 'path': str(target),
                           **state['latest'], 'runtime_verified': False,
                           'preparation': preparation,
                           'tree_sha256': manager._source_hash(package)}
                manager._atomic(package/RECEIPT, json.dumps(receipt, sort_keys=True).encode(), mode=0o644, expected=None)
                with manager._dirfd(package.parent) as source, manager._dirfd(manager.home) as home:
                    _write_access(manager, home)
                    _check_cancel(cancel, deadline)
                    _publish(source, package.name, home, target.name)
                # Do not ever remove the final folder on readback/record failure.
                # It has an explicit receipt and may already have consumers.
                with manager._dirfd(target):
                    pass
                _validate(manager, target, originals)
                if _json_file(manager, target/RECEIPT, limit=65536) != receipt:
                    raise ValueError('Published package ownership readback mismatch')
                state['package'] = {**state['latest'], 'path': str(target), 'runtime_verified': False}
                state['state'] = 'ready'
                _save_state(manager, state)
            except Exception as exc:
                state = {**state, 'state': 'error', 'error': str(exc)[:2048]}
                _save_state(manager, state)
            finally:
                if stage is not None and stage_identity is not None:
                    with manager._dirfd(manager.home) as home:
                        try:
                            actual = os.stat(stage.name, dir_fd=home, follow_symlinks=False)
                        except FileNotFoundError:
                            actual = None
                        if actual and (actual.st_dev, actual.st_ino) == (stage_identity.st_dev, stage_identity.st_ino):
                            shutil.rmtree(stage.name, dir_fd=home)
                            os.fsync(home)
            return {**state, 'pinned': PINNED_TAG}
    except BlockingIOError:
        return get_status(manager)
    except Exception as exc:
        return {**state, 'pinned': PINNED_TAG, 'state': 'error', 'error': str(exc)[:2048]}

