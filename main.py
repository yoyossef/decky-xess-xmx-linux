"""Decky XeSS XMX — XeSS on Intel XMX, FSR bridging and OptiScaler, per game."""

import os
import sys

import decky

sys.path.append(os.path.join(os.path.dirname(__file__), "py_modules"))

from optiscaler.service import OptiScalerService  # noqa: E402


class Plugin:
    service: OptiScalerService = None

    async def _main(self):
        decky.logger.info("Decky XeSS XMX starting")
        self.service = OptiScalerService(
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            settings_dir=decky.DECKY_PLUGIN_SETTINGS_DIR,
            runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
            home=decky.HOME,
            logger=decky.logger,
        )
        decky.logger.info("payload status: %s", self.service.payload.status())
        # Start the compatibility list refreshing now rather than when a game
        # is first opened: it is served from cache either way, so the only
        # thing waiting costs is how out of date the first answer is.
        self.service.prime_wiki()

    async def _unload(self):
        decky.logger.info("Decky XeSS XMX unloading")

    async def _uninstall(self):
        decky.logger.info("Decky XeSS XMX uninstalled")

    # -- status ----------------------------------------------------------
    async def get_status(self) -> dict:
        return await self.service.get_status()

    async def prepare_payload(self, force: bool = False) -> dict:
        try:
            root = await self.service.ensure_payload(force)
            return {"ok": True, "path": root}
        except Exception as exc:
            decky.logger.exception("payload extraction failed")
            return {"ok": False, "error": str(exc)}

    # -- libraries -------------------------------------------------------
    async def list_libraries(self) -> list:
        return await self.service.list_libraries()

    async def add_custom_library(self, path: str, name: str = None) -> dict:
        return await self.service.add_custom_library(path, name)

    async def remove_custom_library(self, path: str) -> dict:
        return await self.service.remove_custom_library(path)

    async def browse(self, path: str = None) -> dict:
        return await self.service.browse(path)

    # -- games -----------------------------------------------------------
    async def list_all_games(self) -> list:
        return await self.service.list_all_games()

    async def list_games(self, library_path: str, source: str = "steam") -> list:
        return await self.service.list_games(library_path, source)

    async def get_game(self, game_path: str, name: str = None) -> dict:
        return await self.service.get_game(game_path, name)

    async def find_running_game(self, appid: str, shortcut: dict = None) -> dict:
        return await self.service.find_running_game(appid, shortcut)

    async def _xmx_rpc(self, method, *args):
        try:
            if self.service is None:
                raise RuntimeError("XeSS XMX service is not initialized")
            return await getattr(self.service, method)(*args)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def get_xmx_status(self, appid: str, package_dir: str = "") -> dict:
        return await self._xmx_rpc("get_xmx_status", appid, package_dir)

    async def get_xmx_setup_apps(self) -> dict:
        return await self._xmx_rpc("get_xmx_setup_apps")

    async def plan_xmx(self, appid: str, package_dir: str, launch_options: str,
                       action: str = "enable", profile: dict | None = None) -> dict:
        return await self._xmx_rpc("plan_xmx", appid, package_dir, launch_options, action, profile)

    async def get_xmx_logs(self, appid: str) -> dict:
        return await self._xmx_rpc("get_xmx_logs", appid)

    async def prepare_xess_sdk(self) -> dict:
        return await self._xmx_rpc("prepare_xess_sdk")

    async def get_xmx_package_status(self) -> dict:
        return await self._xmx_rpc("get_xmx_package_status")

    async def prepare_xmx_package(self) -> dict:
        return await self._xmx_rpc("prepare_xmx_package")

    async def get_fsr_bridge_state(self) -> dict:
        return await self._xmx_rpc("get_fsr_bridge_state")

    async def install_fsr_bridge(self) -> dict:
        return await self._xmx_rpc("install_fsr_bridge")

    async def uninstall_fsr_bridge(self, confirm: str) -> dict:
        return await self._xmx_rpc("uninstall_fsr_bridge", confirm)


    async def get_xess_dll_status(self, appid: str) -> dict:
        return await self._xmx_rpc("get_xess_dll_status", appid)

    async def plan_xess_dll(self, appid: str, target_path: str, launch_options: str,
                            action: str = "enable", adopt_trial: bool = False) -> dict:
        return await self._xmx_rpc("plan_xess_dll", appid, target_path, launch_options, action, adopt_trial)

    async def apply_xess_dll(self, appid: str, plan_id: str) -> dict:
        return await self._xmx_rpc("apply_xess_dll", appid, plan_id)

    async def finish_xess_dll(self, appid: str, plan_id: str, launch_options: str) -> dict:
        return await self._xmx_rpc("finish_xess_dll", appid, plan_id, launch_options)

    async def recover_xess_dll(self, appid: str, plan_id: str, launch_options: str,
                               action: str = "abort") -> dict:
        return await self._xmx_rpc("recover_xess_dll", appid, plan_id, launch_options, action)

    async def apply_xmx(self, appid: str, plan_id: str) -> dict:
        return await self._xmx_rpc("apply_xmx", appid, plan_id)

    async def finish_xmx(self, appid: str, plan_id: str, launch_options: str) -> dict:
        return await self._xmx_rpc("finish_xmx", appid, plan_id, launch_options)

    async def recover_xmx(self, appid: str, plan_id: str, launch_options: str,
                          action: str = "abort") -> dict:
        return await self._xmx_rpc("recover_xmx", appid, plan_id, launch_options, action)

    async def nuke_plan_xmx(self, appid: str, package_dir: str = "") -> dict:
        return await self._xmx_rpc("nuke_plan_xmx", appid, package_dir)

    async def nuke_apply_xmx(self, appid: str, plan_id: str) -> dict:
        return await self._xmx_rpc("nuke_apply_xmx", appid, plan_id)

    async def nuke_finish_xmx(self, appid: str, plan_id: str, launch_options: str) -> dict:
        return await self._xmx_rpc("nuke_finish_xmx", appid, plan_id, launch_options)

    async def get_fsr4_info(self, target_dir: str) -> dict:
        return await self.service.get_fsr4_info(target_dir)

    async def import_fsr4_files(self, target_dir: str, source_dir: str) -> dict:
        return await self.service.import_fsr4_files(target_dir, source_dir)

    async def set_fsr4_build(self, target_dir: str, build_id: str) -> dict:
        return await self.service.set_fsr4_build(target_dir, build_id)

    async def restore_fsr4_build(self, target_dir: str) -> dict:
        return await self.service.restore_fsr4_build(target_dir)

    async def get_launch_options(self, appid: str) -> dict:
        return await self.service.get_launch_options(appid)

    async def record_launch_options(self, target_dir: str, options: str = None,
                                    appid: str = None) -> dict:
        return await self.service.record_launch_options(target_dir, options, appid)

    async def get_pref(self, key: str, default=None) -> dict:
        return await self.service.get_pref(key, default)

    async def set_pref(self, key: str, value=None) -> dict:
        return await self.service.set_pref(key, value)

    async def set_game_target(self, game_path: str, target_dir: str) -> dict:
        return await self.service.set_game_target(game_path, target_dir)

    # -- wiki ------------------------------------------------------------
    async def get_recommendation(self, name: str, extra_names: list = None,
                                 force: bool = False, game_path: str = None) -> dict:
        return await self.service.get_recommendation(name, extra_names, force, game_path)

    async def search_wiki(self, query: str, limit: int = 30) -> dict:
        return await self.service.search_wiki(query, limit)

    async def set_wiki_entry(self, game_path: str, entry_name: str) -> dict:
        return await self.service.set_wiki_entry(game_path, entry_name)

    async def verify_install(self, target_dir: str) -> dict:
        return await self.service.verify_install(target_dir)

    async def refresh_wiki(self) -> dict:
        return await self.service.refresh_wiki()

    async def wiki_status(self, force: bool = False) -> dict:
        return await self.service.wiki_status(force)

    # -- automatic set-up ------------------------------------------------
    async def get_auto_plan(self, name: str, extra_names: list = None,
                            force: bool = False, game_path: str = None) -> dict:
        return await self.service.get_auto_plan(name, extra_names, force, game_path)

    async def set_auto_mode(self, game_path: str, enabled: bool) -> dict:
        return await self.service.set_auto_mode(game_path, enabled)

    async def auto_install(self, target_dir: str, game_path: str = None,
                           name: str = None, extra_names: list = None) -> dict:
        return await self.service.auto_install(target_dir, game_path, name, extra_names)

    async def apply_auto_settings(self, target_dir: str, game_path: str = None,
                                  name: str = None, extra_names: list = None) -> dict:
        return await self.service.apply_auto_settings(target_dir, game_path, name, extra_names)

    # -- install ---------------------------------------------------------
    async def install(self, target_dir: str, filename: str = "dxgi.dll",
                      preserve_ini: bool = True, optipatcher: bool = False) -> dict:
        return await self.service.install(target_dir, filename, preserve_ini, optipatcher)

    async def get_reframework_status(self, target_dir: str, game_path: str = None,
                                     name: str = None, extra_names: list = None) -> dict:
        return await self.service.get_reframework_status(
            target_dir, game_path, name, extra_names)

    async def install_reframework(self, target_dir: str, game_path: str = None,
                                  name: str = None, extra_names: list = None) -> dict:
        return await self.service.install_reframework(
            target_dir, game_path, name, extra_names)

    async def remove_reframework(self, target_dir: str) -> dict:
        return await self.service.remove_reframework(target_dir)

    async def get_optipatcher_status(self, target_dir: str) -> dict:
        return await self.service.get_optipatcher_status(target_dir)

    async def install_optipatcher(self, target_dir: str, enabled: bool = True) -> dict:
        return await self.service.install_optipatcher(target_dir, enabled)

    async def uninstall(self, target_dir: str, remove_ini: bool = True) -> dict:
        return await self.service.uninstall(target_dir, remove_ini)

    # -- configuration ---------------------------------------------------
    async def read_config(self, target_dir: str) -> dict:
        return await self.service.read_config(target_dir)

    async def write_config(self, target_dir: str, changes: list) -> dict:
        return await self.service.write_config(target_dir, changes)

    async def reset_config(self, target_dir: str) -> dict:
        return await self.service.reset_config(target_dir)

    # -- monitoring ------------------------------------------------------
    async def get_live_status(self, target_dir: str) -> dict:
        return await self.service.get_live_status(target_dir)

    async def install_live(self, target_dir: str) -> dict:
        return await self.service.install_live(target_dir)

    async def switch_upscaler(self, target_dir: str, code: str) -> dict:
        return await self.service.switch_upscaler(target_dir, code)

    async def get_live_log(self, target_dir: str, lines: int = 40) -> dict:
        return await self.service.get_live_log(target_dir, lines)

    async def get_monitor(self, target_dir: str) -> dict:
        return await self.service.get_monitor(target_dir)

    async def clear_log(self, target_dir: str) -> dict:
        return await self.service.clear_log(target_dir)

    async def set_logging(self, target_dir: str, enabled: bool) -> dict:
        return await self.service.set_logging(target_dir, enabled)
