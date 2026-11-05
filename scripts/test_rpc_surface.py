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

CALLABLE_RE = re.compile(r'callable<[^;]*?\(\s*"([a-z0-9_]+)"\s*\)', re.DOTALL)


def api_callables():
    return sorted(set(CALLABLE_RE.findall(API.read_text(encoding='utf-8'))))


def plugin_methods():
    return set(re.findall(r'^\s+async def ([a-z0-9_]+)\(', MAIN.read_text(encoding='utf-8'), re.M))


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

    def test_bridge_surface_is_forwarded(self):
        methods = plugin_methods()
        for name in ('get_fsr_bridge_state', 'install_fsr_bridge',
                     'uninstall_fsr_bridge'):
            self.assertIn(name, methods)


if __name__ == '__main__':
    unittest.main(verbosity=2)
