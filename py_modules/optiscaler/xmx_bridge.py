"""FSR->XeSS bridge: the upstream Proton hooks, scoped to games we manage.

The xess-xmx-linux package routes a game's FSR 3.1+ upscaling/frame generation
onto XeSS (``FSR=xess`` / ``FSR_FG=xess``) through the Proton hook its
``install.sh`` writes next to every Proton (``user_settings.py`` runs
``tools/proton-hook.py`` at every game start, and the hook only acts when those
variables are in the game's environment). Our per-game wrapper (xmx.py) exports
them for bridged games only, so the driver reach stays per game.

install.sh is run UNCHANGED and as the desktop user (never root, upstream's own
requirement): it drops a session-wide ``~/.config/environment.d/50-xess-xmx.conf``
as it goes, which would push the patched driver into gamescope and every game.
Right after a verified install this module moves that file aside (parked, put
back by uninstall) and clears the same variables from the running user session,
so only games whose wrapper exports the variables ever see the patched driver.

The hook side is upstream's file; uninstall runs upstream's ``uninstall.sh`` and
this module only removes its own parked file and snapshot. Every step lands in
``bridge-journal.jsonl`` as evidence.
"""
import json
import logging
import os
import pwd
import subprocess
import time
from pathlib import Path

log = logging.getLogger('optiscaler.xmx_bridge')

ENV_CONF = '.config/environment.d/50-xess-xmx.conf'
PARKED_SUFFIX = '.decky-fsr-bridge'
BRIDGE_ENV = 'bridge.env'
JOURNAL = 'bridge-journal.jsonl'
DRIVER_STATUS = '.cache/xess-xmx/driver-status'
HOOK_MARK = 'xess-xmx proton hook'
UNITS = ('xess-xmx-check.service', 'xess-xmx-shim.path')
#: the panel must echo this exact token to unlock the destructive uninstall
CONFIRM = 'uninstall-hooks'


class BridgeError(ValueError):
    pass


def _home_user(home: Path) -> str:
    return pwd.getpwuid(home.stat().st_uid).pw_name


def user_command(username, argv, home):
    """Wrap argv so it runs as the desktop user with a sane, minimal environment.

    Decky plugin processes run AS the desktop user (verified on the oxp3:
    the plugin python processes carry the login uid), where runuser is
    impossible: "may not be used by non-root users". So:
      - same uid as the home owner  -> run directly, env -i kept for hygiene
        (the service process environment must never leak into the session);
      - root                        -> runuser down to the user, as designed;
      - any other uid               -> refuse loudly, never guess.

    ``env -i`` on purpose in both paths. XDG_RUNTIME_DIR and the session bus
    are added only when they exist, so systemctl --user can reach the running
    graphical session.
    """
    uid = Path(home).stat().st_uid
    runtime = Path('/run/user')/str(uid)
    env = ['PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=' + str(home),
           'USER=' + username, 'LOGNAME=' + username, 'SHELL=/bin/sh']
    if runtime.is_dir():
        env.append('XDG_RUNTIME_DIR=' + str(runtime))
        bus = runtime/'bus'
        if bus.exists():
            env.append('DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(bus))
    core = ['/usr/bin/env', '-i', *env, *[str(a) for a in argv]]
    euid = os.geteuid()
    if euid == uid:
        return core
    if euid == 0:
        return ['runuser', '-u', username, '--', *core]
    raise BridgeError(
        'This service runs as uid %d and cannot act as %s (uid %d); '
        'the FSR bridge needs the desktop user or root' % (euid, username, uid))


def _default_run(manager, argv, cwd=None, timeout=None):
    username = _home_user(manager.home)
    try:
        command = user_command(username, argv, manager.home)
    except BridgeError as exc:
        # A state() read must degrade to "unknown", never explode the panel.
        return {'rc': 126, 'out': str(exc)}
    proc = subprocess.run(command, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)
    return {'rc': proc.returncode, 'out': proc.stdout + proc.stderr}


def _journal(manager, step, **detail):
    entry = {'t': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'step': step}
    entry.update(detail)
    path = manager.root/JOURNAL
    manager.root.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _journal_tail(manager, limit=20):
    path = manager.root/JOURNAL
    if not path.is_file():
        return []
    lines = path.read_text(encoding='utf-8').splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({'step': 'unreadable', 'raw': line[:200]})
    return out


def _package(manager):
    """The prepared, receipt-verified package from the package pipeline, or None."""
    from . import xmx_package
    try:
        status = xmx_package.get_status(manager)
    except Exception as exc:  # an unrecognized ownership record must not crash the panel
        raise BridgeError('Package preparation state unreadable: ' + str(exc))
    package = status.get('package') if status.get('state') == 'ready' else None
    return package


def _library_roots(manager):
    """Steam library roots: the default one plus whatever libraryfolders.vdf adds."""
    from . import steam
    roots = [manager.home/'.local/share/Steam']
    try:
        for path in steam.library_folders(manager.home):
            candidate = Path(path)
            if candidate not in roots:
                roots.append(candidate)
    except Exception:
        pass
    return roots


def _protons(manager):
    """Folders install.sh considers a Proton: Valve's and the user's, every library."""
    found = []
    for root in _library_roots(manager):
        for folder in (sorted((root/'steamapps/common').glob('Proton*'))
                       + sorted((root/'compatibilitytools.d').glob('*'))):
            if (folder/'proton').is_file():
                found.append(folder)
    return found


def _hooks(manager):
    protons = _protons(manager)
    hooked, plain = [], []
    for folder in protons:
        try:
            text = (folder/'user_settings.py').read_text(encoding='utf-8', errors='replace')
        except OSError:
            text = ''
        (hooked if HOOK_MARK in text else plain).append(folder.name)
    return {'protons': [p.name for p in protons], 'hooked': hooked, 'plain': plain,
            'installed': bool(hooked)}


def _driver_status(manager):
    try:
        line = (manager.home/DRIVER_STATUS).read_text(encoding='utf-8').splitlines()[0]
    except (OSError, IndexError):
        return {'state': 'unknown', 'line': None}
    if line.startswith('ok '):
        return {'state': 'ok', 'line': line}
    if line.startswith('FAILED'):
        return {'state': 'failed', 'line': line}
    return {'state': 'unknown', 'line': line}


def _session_conf_state(manager):
    conf = manager.home/ENV_CONF
    if Path(str(conf) + PARKED_SUFFIX).is_file():
        return 'parked'
    return 'active' if conf.is_file() else 'absent'


def _parse_conf(text):
    env = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
            value = value[1:-1]
        if key.replace('_', '').isalnum() and not key[0].isdigit():
            env[key] = value
    return env


def _bridges(manager):
    out = []
    apps = manager.root/'apps'
    if apps.is_dir():
        for folder in sorted(apps.iterdir()):
            conf = folder/'xess.conf'
            try:
                text = conf.read_text(encoding='utf-8')
            except OSError:
                continue
            if 'export FSR=' in text or 'export FSR_FG=' in text:
                out.append(folder.name)
    return out


def _bridge_profiles(manager):
    """Per-bridged-game view of what the wrapper actually exports: the game
    page shows and edits exactly this, never a guess."""
    out = {}
    apps = manager.root/'apps'
    if apps.is_dir():
        for folder in sorted(apps.iterdir()):
            conf = folder/'xess.conf'
            try:
                text = conf.read_text(encoding='utf-8')
            except OSError:
                continue
            if 'export FSR=' not in text and 'export FSR_FG=' not in text:
                continue
            fg = 0
            if 'export XESS_FG=' in text:
                try:
                    fg = int(text.split('export XESS_FG=', 1)[1].split('\n', 1)[0].strip())
                except ValueError:
                    fg = 0
            out[folder.name] = {
                'bridge': 1 if 'export FSR=xess' in text else 2,
                'sdk_latest': 'export XESS=latest' in text,
                'fg': fg,
            }
    return out


#: detection walk: depth below the game folder, directory budget per state(),
#: and the panel cap — a report, never a scan that can pin the service.
DETECT_DEPTH = 3
DETECT_BUDGET = 20000
DETECT_LIMIT = 60


def _detect(manager, managed):
    """Installed games that ship XeSS/OptiScaler/FSR loaders but are not ours.

    Report only, never adopted: toggling one of these is an explicit user act
    through the per-game flow (Q7: no silent adoption)."""
    from . import steam
    out, budget = [], DETECT_BUDGET
    for root in _library_roots(manager):
        try:
            games = steam.steam_games(root)
        except Exception:
            continue
        for game in games:
            if game['appid'] in managed:
                continue
            top = Path(game['path'])
            depth = len(top.parts)
            xess = fsr = False
            for current, dirs, files in os.walk(top):
                budget -= 1
                if budget <= 0:
                    break
                if Path(current).parts[:depth] != top.parts:
                    continue
                if len(Path(current).parts) - depth >= DETECT_DEPTH:
                    dirs[:] = []
                low = [name.lower() for name in files]
                if 'optiscaler.ini' in low or any(n.startswith('libxess') and n.endswith('.dll') for n in low):
                    xess = True
                if any(n.startswith('amd_fidelityfx') and n.endswith('.dll') for n in low):
                    fsr = True
                if xess:
                    break
            if xess or fsr:
                out.append({'appid': game['appid'], 'name': game['name'], 'managed': False,
                            'marker': 'xess' if xess else 'fsr'})
            if budget <= 0 or len(out) >= DETECT_LIMIT:
                return out
    return out


def _names(manager):
    """appid -> display name for every installed Steam game (labels only)."""
    from . import steam
    names = {}
    for root in _library_roots(manager):
        try:
            for game in steam.steam_games(root):
                names.setdefault(game['appid'], game['name'])
        except Exception:
            continue
    return names


def state(manager, *, run=None):
    run = run or (lambda argv, cwd=None, timeout=None: _default_run(manager, argv, cwd, timeout))
    apps = sorted(p.name for p in (manager.root/'apps').iterdir() if p.is_dir()) \
        if (manager.root/'apps').is_dir() else []
    names = _names(manager)
    result = {'ok': True, 'package': None, 'session_conf': _session_conf_state(manager),
              'hooks': _hooks(manager), 'driver_status': _driver_status(manager),
              'apps': apps, 'bridges': _bridges(manager),
              'bridge_profiles': _bridge_profiles(manager),
              'names': {app: names.get(app, 'App ' + app) for app in apps},
              'detected': _detect(manager, set(apps)),
              'units': {}, 'journal': _journal_tail(manager)}
    try:
        package = _package(manager)
    except BridgeError as exc:
        result['package_error'] = str(exc)
        package = None
    if package:
        result['package'] = {'tag': package['tag'], 'path': package['path']}
    for unit in UNITS:
        try:
            checked = run(['systemctl', '--user', 'is-enabled', unit], timeout=20)
            result['units'][unit] = (checked.get('out') or '').strip().splitlines()[-1] \
                if (checked.get('out') or '').strip() else ('enabled' if checked['rc'] == 0 else 'disabled')
        except Exception:
            result['units'][unit] = 'unknown'
    return result


def install(manager, *, run=None):
    """One explicit transaction: upstream install.sh (as user) -> verify -> park."""
    run = run or (lambda argv, cwd=None, timeout=None: _default_run(manager, argv, cwd, timeout))
    try:
        package = _package(manager)
    except BridgeError as exc:
        return {'ok': False, 'error': str(exc)}
    if not package:
        return {'ok': False, 'error': 'No prepared XMX package: update the package first'}
    pkg_path = Path(package['path'])
    script = pkg_path/'install.sh'
    if not script.is_file():
        return {'ok': False, 'error': 'install.sh missing from the prepared package'}
    result = run(['bash', str(script)], cwd=pkg_path, timeout=900)
    if result['rc'] != 0:
        tail = (result.get('out') or '').strip().splitlines()[-3:]
        _journal(manager, 'install-failed', rc=result['rc'], tail=tail)
        return {'ok': False, 'error': 'install.sh exited %d: %s' % (result['rc'], ' / '.join(tail) or 'no output')}
    hooks = _hooks(manager)
    conf = manager.home/ENV_CONF
    if not hooks['installed'] or _session_conf_state(manager) != 'active':
        _journal(manager, 'install-unverified', hooks=hooks['hooked'], conf=_session_conf_state(manager))
        return {'ok': False, 'error': 'install.sh ran but the Proton hook or the session file was not '
                                      'found (writable Protons only); nothing was scoped', 'hooks': hooks}
    env = _parse_conf(conf.read_text(encoding='utf-8'))
    if 'VK_DRIVER_FILES' not in env:
        _journal(manager, 'install-unverified', reason='session file without VK_DRIVER_FILES')
        return {'ok': False, 'error': 'The session file has no VK_DRIVER_FILES; leaving it active'}
    snapshot = manager.root/BRIDGE_ENV
    manager.root.mkdir(parents=True, exist_ok=True)
    uid = manager.home.stat().st_uid
    gid = manager.home.stat().st_gid
    tmp = snapshot.with_name(snapshot.name + '.tmp')
    tmp.write_text('# XMX session variables, parked by the FSR bridge (applied per game)\n'
                   + ''.join('%s=%s\n' % (key, value) for key, value in env.items()), encoding='utf-8')
    os.chown(tmp, uid, gid)
    tmp.replace(snapshot)
    os.replace(conf, Path(str(conf) + PARKED_SUFFIX))
    _journal(manager, 'install', tag=package['tag'], keys=sorted(env))
    cleared = run(['systemctl', '--user', 'unset-environment', *sorted(env)], timeout=30)
    _journal(manager, 'park', cleared_rc=cleared['rc'],
             note='' if cleared['rc'] == 0 else 'a reboot clears the session variables')
    return {'ok': True, 'tag': package['tag'], 'hooks': hooks,
            'session_conf': 'parked', 'out': (result.get('out') or '')[-2000:]}


def uninstall(manager, confirm, *, run=None):
    """Undo: refuse while any game is bridged, then upstream uninstall.sh."""
    run = run or (lambda argv, cwd=None, timeout=None: _default_run(manager, argv, cwd, timeout))
    if confirm != CONFIRM:
        return {'ok': False, 'error': 'Type %s to confirm; nothing was changed' % CONFIRM}
    active = _bridges(manager)
    if active:
        return {'ok': False, 'error': 'Turn the bridge off first for: ' + ', '.join(active)}
    try:
        package = _package(manager)
    except BridgeError:
        package = None
    script = Path(package['path'])/'uninstall.sh' if package else None
    if script and script.is_file():
        result = run(['bash', str(script)], cwd=script.parent, timeout=900)
        if result['rc'] != 0:
            tail = (result.get('out') or '').strip().splitlines()[-3:]
            _journal(manager, 'uninstall-failed', rc=result['rc'], tail=tail)
            return {'ok': False, 'error': 'uninstall.sh exited %d: %s' % (result['rc'], ' / '.join(tail) or 'no output')}
    else:
        # the package folder is gone: still drop our own parked traces
        result = {'rc': 0, 'out': 'package folder gone; removed the bridge traces only'}
    parked = Path(str(manager.home/ENV_CONF) + PARKED_SUFFIX)
    if parked.is_file():
        parked.unlink()
    snapshot = manager.root/BRIDGE_ENV
    if snapshot.exists():
        snapshot.unlink()
    _journal(manager, 'uninstall', note='upstream script + parked traces')
    return {'ok': True, 'out': (result.get('out') or '')[-2000:]}
