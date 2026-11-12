"""Strict per-game XeSS profile rendering; no session/global mutations."""
from pathlib import Path
import json
import re
import shlex
import struct

DEFAULT = {'driver': 'default', 'discovery': 'auto', 'diagnostics': False, 'fg': 0,
           'bridge': 0, 'sdk_latest': False}
#: 0 = the game's own choice; 2-4 is Intel's supported override range; 5-8 is the
#: package's experimental shim trick (see upstream launch-options.md).
FG_CHOICES = (0, 2, 3, 4, 5, 6, 7, 8)
#: FSR->XeSS bridge (upstream proton hook reads these from the game's environment):
#: 0 = off, 1 = FSR=xess (upscaling and frame generation on XeSS), 2 = FSR_FG=xess
#: (only frame generation on XeSS, the game's own FSR upscaler stays). sdk_latest
#: adds XESS=latest, Intel's SDK libraries in place of the game's older ones.
BRIDGE_CHOICES = (0, 1, 2)
ALTERNATE = r'C:\windows\system32\driverstore\filerepository\igd_faux.inf_1'


def driver_id(value):
    """Version-family selector, never a filesystem path or an executable token."""
    return isinstance(value, str) and (value == 'default' or
        re.fullmatch(r'(?:stock-)?[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?(?:-[A-Za-z0-9_.-]{1,32})?', value) is not None)


def normalize(profile):
    if profile is None:
        return dict(DEFAULT)
    # Stored records from before frame generation carry only the old three keys;
    # a missing fg means the game's own choice, unknown keys stay refused.
    if not isinstance(profile, dict) or not set(profile) <= set(DEFAULT):
        raise ValueError('Invalid profile: driver, discovery, diagnostics and integer fg required')
    profile = {**DEFAULT, **profile}
    if (not driver_id(profile['driver']) or
            profile['discovery'] not in ('auto', 'alternate') or
            type(profile['diagnostics']) is not bool or
            type(profile['fg']) is not int or profile['fg'] not in FG_CHOICES or
            type(profile['bridge']) is not int or profile['bridge'] not in BRIDGE_CHOICES or
            type(profile['sdk_latest']) is not bool):
        raise ValueError('Invalid profile: driver, discovery, diagnostics, fg, bridge and sdk_latest required')
    return profile


def drivers(manager, package, diagnostics):
    result = [{'id': 'default', 'label': 'Upstream default', 'valid': diagnostics['valid']}]
    if not diagnostics['valid']:
        result[0]['reason'] = '; '.join(diagnostics['errors'])
        return result
    package = Path(package).resolve(strict=True)
    selectors = set()
    for candidate in (package/'lib').glob('mesa-*'):
        match = re.fullmatch(r'mesa-((?:stock-)?[0-9]{1,4}\.[0-9]{1,4})(?:\.[0-9]{1,4})?(?:-[A-Za-z0-9_.-]{1,32})?', candidate.name)
        if match:
            selectors.add(match[1])
    for selector in sorted(selectors):
        matches = sorted(p for p in (package/'lib').glob('mesa-'+selector+'*')
                         if p.name == 'mesa-'+selector or p.name.startswith('mesa-'+selector+'.')
                         or p.name.startswith('mesa-'+selector+'-'))
        if not matches:
            continue
        entry = {'id': selector, 'label': 'Mesa '+selector, 'valid': False}
        try:
            # Do not guess the dispatcher's version precedence. A single real
            # variant is required; the per-game ICD pins that exact ELF so a
            # missing/changed variant cannot silently fall back to upstream.
            if len(matches) != 1:
                raise ValueError('Ambiguous Mesa selector: multiple matching variants')
            relative = (matches[0]/'libvulkan_intel.so').relative_to(package)
            data = manager._source_bytes(package, relative)
            if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01' or
                    struct.unpack_from('<H', data, 18)[0] != 62 or
                    struct.unpack_from('<H', data, 16)[0] not in (2, 3)):
                raise ValueError('Selected Mesa variant is not an ELF64 x86-64 driver')
            path = (package/relative).resolve(strict=True)
            manager._user_access(path, read=True)
            if path.samefile(package/'lib/libvulkan_intel.so'):
                raise ValueError('Mesa selector points to dispatcher instead of real driver')
            if selector.startswith('stock-'):
                # Stock ANV alone does not implement the kernel-splicing path.
                layer_env(manager, package)
                entry['label'] = 'Mesa '+selector[6:]+' (stock + XMX layer, Xe3 only)'
            entry.update(valid=True, path=str(path))
        except (OSError, ValueError, struct.error) as exc:
            entry['reason'] = str(exc)
        result.append(entry)
    return result


def layer_env(manager, package):
    """Validate the optional modern layer before a stock-driver profile uses it."""
    path = package/'layer'
    binary = path/'libVkLayer_xess_xmx.so'
    data = manager._source_bytes(package, binary.relative_to(package))
    if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01' or
            struct.unpack_from('<H', data, 18)[0] != 62 or
            struct.unpack_from('<H', data, 16)[0] not in (2, 3)):
        raise ValueError('XMX layer is not an ELF64 x86-64 library')
    manifest = json.loads(manager._source_bytes(package, 'layer/VkLayer_xess_xmx.json'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('layer'), dict):
        raise ValueError('Unsupported XMX layer manifest')
    layer = manifest['layer']
    library = layer.get('library_path')
    if (layer.get('name') != 'VK_LAYER_XESS_xmx' or not isinstance(library, str)
            or Path(library).is_absolute() or '..' in Path(library).parts
            or (path/library).resolve(strict=True) != binary.resolve(strict=True)):
        raise ValueError('Unsupported XMX layer manifest')
    manager._user_access(binary, read=True)
    return {'VK_ADD_LAYER_PATH': path, 'VK_LOADER_LAYERS_ENABLE': 'VK_LAYER_XESS_xmx',
            'XMX_LAYER_HELPER': package/'tools/cm-compile.sh', 'XMX_IGC_DIR': package/'igc'}


def recognize_trial(observed, managed):
    selected = re.match(r'^MESA=([^ ]+) ', observed)
    selectors = ['default', '26.1', '26.3']
    if selected and driver_id(selected[1]) and selected[1] not in selectors:
        selectors.append(selected[1])
    for driver in selectors:
        for alternate in (False, True):
            for diagnostics in (False, True):
                for fg in FG_CHOICES:
                    for bridge in BRIDGE_CHOICES:
                        for sdk_latest in (False, True):
                            base = []
                            if driver != 'default':
                                base.append('MESA='+driver)
                            if alternate:
                                base.append('INTC_ALT_DRIVER_EXTENSIONS_PATH='+shlex.quote(ALTERNATE))
                            if diagnostics:
                                base.extend(('PROTON_LOG=1', 'IGDEXT_TRACE=1', 'ANV_CM_DEBUG=1'))
                            if sdk_latest:
                                base.append('XESS=latest')
                            # The upstream hook reads the route from the environment; only
                            # one of the two FSR switches makes sense in one trial.
                            if bridge == 1:
                                base.append('FSR=xess')
                            elif bridge == 2:
                                base.append('FSR_FG=xess')
                            candidates = []
                            if fg:
                                # A manual trial may or may not have carried the log flag;
                                # adoption always re-renders it, since the panel shows its evidence.
                                candidates.append([*base, 'XESS_FG=%d' % fg, 'IGDEXT_XELL_LOG=1'])
                                candidates.append([*base, 'XESS_FG=%d' % fg])
                            elif bridge:
                                # A bridged game may run at the game's own multiplier; the
                                # XeLL flag may or may not have been set by hand.
                                candidates.append([*base, 'IGDEXT_XELL_LOG=1'])
                                candidates.append(base)
                            elif base:
                                candidates.append(base)
                            for flags in candidates:
                                if observed == ' '.join(flags)+' '+managed:
                                    return {'driver': driver, 'discovery': 'alternate' if alternate else 'auto',
                                            'diagnostics': False, 'fg': fg, 'bridge': bridge,
                                            'sdk_latest': sdk_latest}
    raise ValueError('Trial launch options conflict: only exact supported temporary prefixes can be adopted')


EXTRA_NAME = 'xess.extra'


def extra(path):
    """Verbatim env lines from the per-game extras file (appended last, so
    they win over the rendered profile). Only exports, comments and blanks."""
    path = Path(path)
    if not path.exists():
        return ''
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        entry = line.strip()
        if not entry or entry.startswith('#'):
            continue
        if not entry.startswith('export ') or '=' not in entry:
            raise ValueError('xess.extra lines must be "export NAME=value", a comment or blank: ' + entry[:60])
        name = entry[len('export '):].split('=', 1)[0]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('xess.extra defines an invalid variable name: ' + name[:60])
        out.append(entry)
    return ('\n'.join(out) + '\n') if out else ''


def render(manager, package, targets, profile, diagnostics):
    profile = normalize(profile)
    library = package/'lib/libvulkan_intel.so'
    if profile['driver'] != 'default':
        selected = next((v for v in drivers(manager, package, diagnostics)
                         if v['id'] == profile['driver']), None)
        if not selected or not selected['valid']:
            raise ValueError('Requested Mesa driver unavailable: '+profile['driver']+
                             ('; '+selected.get('reason', '') if selected else ''))
        library = Path(selected['path'])
    env = {'VK_DRIVER_FILES': targets[4],
           'VKD3D_DISABLE_EXTENSIONS': 'VK_EXT_descriptor_buffer,VK_KHR_present_wait2',
           'ANV_CM_KERNEL_DIR': package/'kernels',
           'ANV_CM_HELPER': package/'tools/cm-compile.sh', 'force_vk_vendor': '0'}
    if profile['driver'] != 'default':
        env['MESA'] = profile['driver']
    if (profile['driver'].startswith('stock-') or
            profile['driver'] == 'default' and
            Path(diagnostics.get('selected_driver', '')).parent.name.startswith('mesa-stock-')):
        if not any(device.get('xmx_capable') and str(device.get('device', '')).lower().startswith(('0xb08', '0xfd8'))
                   for device in manager._gpu().get('devices', [])):
            raise ValueError('The stock driver XMX layer currently supports Xe3 GPUs only')
        env.update(layer_env(manager, package))
    if profile['discovery'] == 'alternate':
        env['INTC_ALT_DRIVER_EXTENSIONS_PATH'] = ALTERNATE
    if profile['diagnostics']:
        env.update(PROTON_LOG='1', IGDEXT_TRACE='1', ANV_CM_DEBUG='1')
    if profile['fg']:
        # ControlLib.dll in the prefix answers the multiplier from the game's
        # environment; the XeLL log is the panel's low-latency evidence.
        env.update(XESS_FG=str(profile['fg']), IGDEXT_XELL_LOG='1')
    if profile['bridge'] == 1:
        # The upstream Proton hook turns the game's FSR 3.1+ DLLs into the
        # package's XeSS-backed ones while the game runs (reverted on exit).
        env['FSR'] = 'xess'
    elif profile['bridge'] == 2:
        env['FSR_FG'] = 'xess'
    if profile['sdk_latest']:
        env['XESS'] = 'latest'
    if profile['bridge'] and not profile['fg']:
        # With the bridge on, XeSS frame generation may run at the game's own
        # multiplier; the XeLL log still carries the low-latency evidence.
        env['IGDEXT_XELL_LOG'] = '1'
    rendered = '\n'.join('export '+key+'='+shlex.quote(str(value)) for key, value in env.items())+'\n'
    if 'VK_ADD_LAYER_PATH' in env:
        # VK_LAYER_PATH overrides VK_ADD_LAYER_PATH in the loader. Prepend our
        # layer to an inherited explicit path, but do not create an override
        # when absent (that would hide the system's default layer locations).
        rendered += ('if [ -n "${VK_LAYER_PATH:-}" ]; then export VK_LAYER_PATH='+
                     shlex.quote(str(package/'layer'))+':"$VK_LAYER_PATH"; '
                     'elif [ "${VK_LAYER_PATH+x}" = x ]; then unset VK_LAYER_PATH; fi\n')
    return rendered, library
