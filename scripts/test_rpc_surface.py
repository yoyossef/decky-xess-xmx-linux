"""Every callable the TypeScript front end invokes must exist on the decky
Plugin object in main.py. The M2 launch proved the gap: methods added to
service.py without a main.py forward surface at runtime as
'Plugin' object has no attribute — invisible to every other suite."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / 'src' / 'xmxApi.ts'
MAIN = ROOT / 'main.py'
SERVICE = ROOT / 'py_modules' / 'optiscaler' / 'service.py'

CALLABLE_RE = re.compile(r'callable<[^;]*?\(\s*"([a-z0-9_]+)"\s*\)', re.DOTALL)
# main.py forwards to the service by string name: self._xmx_rpc("method", ...).
XMX_RPC_RE = re.compile(r'self\._xmx_rpc\(\s*"([a-z0-9_]+)"')
# service.py forwards to the manager either by string: self._xmx_call("method")
# or by direct bound call: self._xmx_call(some_module.some_func, ...).
XMX_CALL_RE = re.compile(r'self\._xmx_call\(\s*"([a-z0-9_]+)"')


def api_callables():
    return sorted(set(CALLABLE_RE.findall(API.read_text(encoding='utf-8'))))


def plugin_methods():
    return set(re.findall(r'^\s+async def ([a-z0-9_]+)\(', MAIN.read_text(encoding='utf-8'), re.M))


def service_methods():
    return set(re.findall(r'^\s+async def ([a-z0-9_]+)\(', SERVICE.read_text(encoding='utf-8'), re.M))


class RpcSurfaceTests(unittest.TestCase):
    def test_api_names_found(self):
        self.assertGreaterEqual(len(api_callables()), 17,
                                'the extractor found too little: check the callable<...> spelling')

    def test_set_fsr_bridge_is_gone_from_both_sides(self):
        # The per-game bridge toggle is now a frontend XmxController transaction
        # (only the frontend can apply Steam launch options). The backend RPC that
        # committed values Steam never received must not silently return.
        text = API.read_text(encoding='utf-8') + MAIN.read_text(encoding='utf-8')
        self.assertNotIn('set_fsr_bridge', text)

    def test_every_callables_name_is_forwarded_by_main(self):
        missing = [name for name in api_callables() if name not in plugin_methods()]
        self.assertEqual(missing, [],
                         'src/xmxApi.ts calls methods main.py never forwards: ' + ', '.join(missing))

    def test_every_xmx_rpc_string_exists_on_the_service(self):
        # The M5.4 badge proved this hop: main.py forwarded the *manager* name
        # ("list_xmx_apps") the service does not expose, getattr raised, the
        # error was swallowed into {ok: false}, and the UI silently read an
        # empty set-up list. Every string main.py hands to _xmx_rpc must be a
        # method the service actually defines.
        targets = set(XMX_RPC_RE.findall(MAIN.read_text(encoding='utf-8')))
        self.assertGreaterEqual(len(targets), 20, 'the extractor found too little')
        missing = sorted(targets - service_methods())
        self.assertEqual(missing, [],
                         'main.py._xmx_rpc forwards names service.py never defines: ' + ', '.join(missing))

    def test_every_xmx_call_string_exists_on_the_manager(self):
        # Same trap one hop deeper: service.py._xmx_call("name") resolves
        # against the XmxManager object at runtime.
        targets = set(XMX_CALL_RE.findall(SERVICE.read_text(encoding='utf-8')))
        self.assertGreaterEqual(len(targets), 15, 'the extractor found too little')
        manager = ROOT / 'py_modules' / 'optiscaler' / 'xmx.py'
        manager_methods = set(re.findall(r'^\s+def ([a-z0-9_]+)\(', manager.read_text(encoding='utf-8'), re.M))
        missing = sorted(targets - manager_methods)
        self.assertEqual(missing, [],
                         'service._xmx_call names methods XmxManager never defines: ' + ', '.join(missing))

    def test_bridge_surface_is_forwarded(self):
        methods = plugin_methods()
        for name in ('get_fsr_bridge_state', 'install_fsr_bridge',
                     'uninstall_fsr_bridge'):
            self.assertIn(name, methods)


if __name__ == '__main__':
    unittest.main(verbosity=2)
