import { callable } from "@decky/api";
import type { XmxAction, XmxApply, XmxPlan, XmxProfile, XmxResult } from "./xmx";
import { isXmxProfile } from "./xmx";
export { isXmxProfile } from "./xmx";

/** Kept separate from OptiScaler's APIs: no payload install or VDF fallback. */
export interface XmxStatus extends XmxResult {
  installed: boolean;
  legacy: boolean | Record<string, unknown>;
  phase: string;
  runtime_verified: false;
  suggested_package?: string;
  package?: { valid?: boolean; path?: string; errors?: string[]; compiler_validated?: boolean; runtime_verified?: false };
  flatpak?: { visible?: boolean | null; reason?: string; runtime_verified?: false };
  gpu?: { xmx_capable?: boolean; reason?: string };
  native_xess?: { found?: boolean; reason?: string; compatibility_verified?: false };
  pending?: unknown;
  profile?: XmxProfile;
  drivers?: { id: XmxProfile["driver"]; label: string; valid: boolean; reason?: string }[];
}


/** Validate the fields the panel renders; an RPC success bit alone is not status. */
export function isXmxStatus(value: unknown): value is XmxStatus {
  if (!value || typeof value !== "object") return false;
  const status = value as Partial<XmxStatus>;
  if (status.ok !== true || typeof status.installed !== "boolean"
    || !(typeof status.legacy === "boolean" || (status.legacy && typeof status.legacy === "object" && !Array.isArray(status.legacy)))
    || !["enabled", "disabled", "pending_enable", "pending_disable", "pending_configure",
         "pending_adopt_trial", "pending_nuke", "recovery_required"].includes(status.phase ?? "")
    || status.runtime_verified !== false) return false;
  if (status.profile !== undefined && !isXmxProfile(status.profile)) return false;
  if (status.drivers !== undefined && (!Array.isArray(status.drivers) || status.drivers.some(d => !d
    || !["default", "26.1", "26.3"].includes(d.id) || typeof d.label !== "string" || typeof d.valid !== "boolean"
    || (d.reason !== undefined && typeof d.reason !== "string")))) return false;
  const p = status.package;
  if (p !== undefined && (!p || typeof p !== "object" || Array.isArray(p)
    || (p.valid !== undefined && typeof p.valid !== "boolean")
    || (p.path !== undefined && typeof p.path !== "string")
    || (p.errors !== undefined && (!Array.isArray(p.errors) || p.errors.some(error => typeof error !== "string"))))) return false;
  const f = status.flatpak;
  for (const diagnostic of [status.gpu, status.native_xess]) {
    if (diagnostic !== undefined && (!diagnostic || typeof diagnostic !== "object" || Array.isArray(diagnostic)
      || (diagnostic.reason !== undefined && typeof diagnostic.reason !== "string"))) return false;
  }
  if (status.gpu?.xmx_capable !== undefined && typeof status.gpu.xmx_capable !== "boolean") return false;
  if (status.native_xess?.found !== undefined && typeof status.native_xess.found !== "boolean") return false;
  return f === undefined || Boolean(f && typeof f === "object" && !Array.isArray(f)
    && (f.visible === undefined || f.visible === null || typeof f.visible === "boolean")
    && (f.reason === undefined || typeof f.reason === "string"));
}

export const getXmxStatus = callable<[appid: string, packageDir?: string], XmxStatus | XmxResult>("get_xmx_status");
export interface XmxSetupApps {
  apps: string[];
}
export const getXmxSetupApps = callable<[], XmxSetupApps | XmxResult>("get_xmx_setup_apps");
export const planXmx = callable<[appid: string, packageDir: string, launchOptions: string, action?: XmxAction, profile?: XmxProfile], XmxPlan>("plan_xmx");
export const applyXmx = callable<[appid: string, planId: string], XmxApply>("apply_xmx");
export const finishXmx = callable<[appid: string, planId: string, launchOptions: string], XmxResult>("finish_xmx");
export const recoverXmx = callable<[appid: string, planId: string, launchOptions: string, action: "abort"], XmxResult>("recover_xmx");

export interface XmxNukeFile { target: string; action: "remove" | "restore" | "leave"; reason?: string }
export interface XmxNukePlan extends XmxResult {
  plan_id?: string;
  files?: unknown;
  warnings?: unknown;
  desired_launch_options?: string;
  legacy?: boolean;
}
export interface XmxNukeApply extends XmxResult {
  removed?: unknown;
  restored?: unknown;
  left?: unknown;
  backup?: string;
}
export const nukePlanXmx = callable<[appid: string, packageDir?: string], XmxNukePlan>("nuke_plan_xmx");
export const nukeApplyXmx = callable<[appid: string, planId: string], XmxNukeApply>("nuke_apply_xmx");
export const nukeFinishXmx = callable<[appid: string, planId: string, launchOptions: string], XmxResult>("nuke_finish_xmx");

export interface XessDllStatus extends XmxResult {
  installed: boolean;
  phase: string;
  pending: unknown;
  candidates: { path: string; version: string | null; architecture: string | null; sha256: string | null; error?: string }[];
  sdk: { tag: string; version: string; ready: boolean; reason?: string };
  trial_available: boolean;
}
export function isXessDllStatus(value: unknown): value is XessDllStatus {
  if (!value || typeof value !== "object") return false;
  const s = value as XessDllStatus;
  return s.ok === true && typeof s.installed === "boolean"
    && ["enabled", "disabled", "pending_enable", "pending_disable", "recovery_required"].includes(s.phase)
    && typeof s.trial_available === "boolean" && Array.isArray(s.candidates)
    && s.candidates.every(c => c && typeof c.path === "string" && [c.version, c.architecture, c.sha256].every(v => v === null || typeof v === "string")
      && (c.error === undefined || typeof c.error === "string"))
    && Boolean(s.sdk && typeof s.sdk.tag === "string" && typeof s.sdk.version === "string" && typeof s.sdk.ready === "boolean"
      && (s.sdk.reason === undefined || typeof s.sdk.reason === "string"));
}
export const getXessDllStatus = callable<[appid: string], XessDllStatus | XmxResult>("get_xess_dll_status");
export const prepareXessSdk = callable<[], XmxResult>("prepare_xess_sdk");
export const planXessDll = callable<[appid: string, targetPath: string, launchOptions: string, action?: "enable" | "disable", adoptTrial?: boolean], XmxPlan>("plan_xess_dll");
export const applyXessDll = callable<[appid: string, planId: string], XmxApply>("apply_xess_dll");
export const finishXessDll = callable<[appid: string, planId: string, launchOptions: string], XmxResult>("finish_xess_dll");
export const recoverXessDll = callable<[appid: string, planId: string, launchOptions: string, action: "abort"], XmxResult>("recover_xess_dll");

export interface XmxLogs extends XmxResult {
  runtime_verified: false;
  logs: { name: string; path: string; exists: boolean; text: string; truncated: boolean }[];
}
export function isXmxLogs(value: unknown): value is XmxLogs {
  if (!value || typeof value !== "object") return false;
  const result = value as XmxLogs;
  return result.ok === true && result.runtime_verified === false && Array.isArray(result.logs)
    && result.logs.every(log => log && typeof log.name === "string" && typeof log.path === "string"
      && typeof log.exists === "boolean" && typeof log.text === "string" && typeof log.truncated === "boolean");
}
export const getXmxLogs = callable<[appid: string], XmxLogs | XmxResult>("get_xmx_logs");

/** Global package preparation is separate from per-game configuration. */
export interface XmxPreparedPackage {
  tag: string;
  path: string;
  archive_sha256: string;
  runtime_verified: false;
}
export interface XmxPackageStatus {
  ok: true;
  state: "idle" | "preparing" | "ready" | "error";
  package: XmxPreparedPackage | null;
  latest: { tag: string; archive_sha256: string } | null;
  error?: string;
}
export function isXmxPackageStatus(value: unknown): value is XmxPackageStatus {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const status = value as Partial<XmxPackageStatus>;
  const tag = (v: unknown) => typeof v === "string" && v.trim().length > 0 && !/[\x00-\x1f\x7f]/.test(v);
  const hash = (v: unknown) => typeof v === "string" && /^[a-fA-F0-9]{64}$/.test(v);
  const path = (v: unknown) => typeof v === "string" && v.startsWith("/") && !v.startsWith("//") && v.length > 1
    && !/[\x00-\x1f\x7f]/.test(v) && !v.split("/").some(part => part === "." || part === "..");
  const pkg = status.package;
  const latest = status.latest;
  return status.ok === true && ["idle", "preparing", "ready", "error"].includes(status.state ?? "")
    && (status.error === undefined || typeof status.error === "string")
    && (pkg === null || Boolean(pkg && typeof pkg === "object" && !Array.isArray(pkg)
      && tag(pkg.tag) && hash(pkg.archive_sha256) && path(pkg.path) && pkg.runtime_verified === false))
    && (latest === null || Boolean(latest && typeof latest === "object" && !Array.isArray(latest)
      && tag(latest.tag) && hash(latest.archive_sha256)))
    && (status.state !== "ready" || Boolean(pkg));
}
export const getXmxPackageStatus = callable<[], XmxPackageStatus | XmxResult>("get_xmx_package_status");
export const prepareXmxPackage = callable<[], XmxPackageStatus | XmxResult>("prepare_xmx_package");

// ---- FSR->XeSS bridge (dedicated panel) ------------------------------------
export interface FsrBridgeDetected {
  appid: string;
  name: string;
  managed: boolean;
  marker: "xess" | "fsr";
}
export interface FsrBridgeState {
  ok: boolean;
  package: { tag: string; path: string } | null;
  package_error?: string;
  session_conf: "active" | "parked" | "absent";
  hooks: { protons: string[]; hooked: string[]; plain: string[]; stale?: string[]; installed: boolean };
  driver_status: { state: string; line: string | null };
  apps: string[];
  bridges: string[];
  bridge_profiles: Record<string, { bridge: number; sdk_latest: boolean; fg: number }>;
  names: Record<string, string>;
  detected: FsrBridgeDetected[];
  units: Record<string, string>;
  journal: { t?: string; step?: string; [key: string]: unknown }[];
  error?: string;
}
export const getFsrBridgeState = callable<[], FsrBridgeState | XmxResult>("get_fsr_bridge_state");
export const installFsrBridge = callable<[], XmxResult>("install_fsr_bridge");
export const uninstallFsrBridge = callable<[confirm: string], XmxResult>("uninstall_fsr_bridge");
