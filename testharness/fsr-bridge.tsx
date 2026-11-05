import assert from "node:assert/strict";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fixtures, calls } from "@decky/api";
import { Router } from "@decky/ui";
import { GameFsrBridge, BridgeGlobalSection } from "../src/components/FsrBridgePanel";

const bare = {
  ok: true, package: { tag: "20261104", path: "/home/player/xess-xmx-linux-20261104" },
  session_conf: "absent",
  hooks: { protons: ["Proton-GE"], hooked: [], plain: ["Proton-GE"], installed: false },
  driver_status: { state: "unknown", line: null },
  apps: ["3357650"], bridges: [], bridge_profiles: {}, names: { "3357650": "PRAGMATA" },
  detected: [], units: {}, journal: [],
};
const hooked = {
  ...bare,
  session_conf: "parked",
  hooks: { protons: ["Proton-GE"], hooked: ["Proton-GE"], plain: [], installed: true },
  driver_status: { state: "ok", line: "ok 2026-11-05 lib drv" },
  units: { "xess-xmx-check.service": "enabled", "xess-xmx-shim.path": "enabled" },
  journal: [{ t: "2026-11-05T00:00:00", step: "install" }, { t: "2026-11-05T00:00:05", step: "park" }],
};
const bridged = {
  ...hooked, bridges: ["3357650"],
  bridge_profiles: { "3357650": { bridge: 1, sdk_latest: false, fg: 0 } },
};

let checks = 0;
async function test(name: string, run: () => void | Promise<void>) {
  const filter = (globalThis as any).process?.env.BRIDGE_TEST;
  if (filter && !name.includes(filter)) return;
  Object.keys(fixtures).forEach(key => delete fixtures[key]);
  Object.assign(fixtures, { get_fsr_bridge_state: bare, install_fsr_bridge: { ok: true },
    uninstall_fsr_bridge: { ok: true } });
  delete (globalThis as any).SteamClient;
  (Router as any).MainRunningApp = undefined;
  calls.length = 0;
  await run();
  checks++;
  console.log(`PASS ${name}`);
}
async function render(node: React.ReactNode) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(node); });
  return { host, root, cleanup: async () => { await act(async () => root.unmount()); host.remove(); } };
}
function button(host: Element, text: string) {
  const found = Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).find(element => element.textContent === text);
  assert.ok(found, `button exists: ${text}`);
  return found;
}
async function press(host: Element, text: string) {
  await act(async () => button(host, text).dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
}
function toggle(host: Element, label: string) {
  const found = host.querySelector<HTMLInputElement>(`input[type=checkbox][aria-label="${label}"]`);
  assert.ok(found, `toggle exists: ${label}`);
  return found;
}
async function flip(host: Element, label: string) {
  await act(async () => toggle(host, label).dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
}
function hasButton(host: Element, text: string) {
  return Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).some(el => el.textContent === text);
}
async function typeValue(host: Element, ariaPrefix: string, value: string) {
  const el = host.querySelector<HTMLInputElement>(`input[aria-label^="${ariaPrefix}"]`);
  assert.ok(el, `field shown: ${ariaPrefix}`);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { setter.call(el, value); el.dispatchEvent(new window.Event("input", { bubbles: true })); });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

// ---- game page surface ----

await test("game tab without hooks offers the install and no toggle", async () => {
  const requests: unknown[][] = [];
  fixtures.get_fsr_bridge_state = (...args: unknown[]) => { requests.push(args); return bare; };
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    assert.deepEqual(requests, [[]], "one state read on mount");
    assert.equal(button(p.host, "Install Proton hooks (advanced)").getAttribute("data-disabled"), null);
    assert.equal(calls.includes("install_fsr_bridge"), false, "no install before the explicit press");
    assert.equal(p.host.querySelector('input[type=checkbox]'), null, "no bridge toggle before the hooks exist");
  } finally { await p.cleanup(); }
});

await test("install from the game page is single-flight and reveals the toggle", async () => {
  const install = deferred<{ ok: boolean }>();
  let installs = 0;
  fixtures.install_fsr_bridge = () => { installs++; return install.promise; };
  fixtures.get_fsr_bridge_state = () => (installs > 0 ? hooked : bare);
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    await press(p.host, "Install Proton hooks (advanced)");
    assert.equal(installs, 1);
    await press(p.host, "Installing hooks…");
    assert.equal(installs, 1, "a running transaction cannot be double-pressed");
    await act(async () => { install.resolve({ ok: true }); await install.promise; });
    assert.equal(toggle(p.host, "FSR→XeSS bridge").checked, false, "hooks on, nothing bridged yet");
  } finally { await p.cleanup(); }
});

await test("install failure keeps the button usable and shows the backend reason", async () => {
  fixtures.install_fsr_bridge = { ok: false, error: "install.sh exited 3: no igdext" };
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    await press(p.host, "Install Proton hooks (advanced)");
    assert.match(p.host.textContent ?? "", /install\.sh exited 3/);
    assert.equal(button(p.host, "Install Proton hooks (advanced)").getAttribute("data-disabled"), null);
  } finally { await p.cleanup(); }
});

await test("game toggle runs the owned transaction and applies options to Steam", async () => {
  let state: typeof bridged = hooked;
  let live = "";
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: async () => live,
    SetAppLaunchOptions: (_id: number, options: string) => { live = options; },
  } };
  const seen: { action?: string; profile?: any; applied?: string } = {};
  const wrapper = "'/state/apps/3357650/launch.sh' %command%";
  fixtures.get_fsr_bridge_state = () => state;
  fixtures.get_xmx_status = () => ({ ok: true, installed: state.bridges.length > 0, legacy: false,
    phase: state.bridges.length > 0 ? "enabled" : "disabled", runtime_verified: false,
    profile: { driver: "default", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false } });
  fixtures.plan_xmx = (_appid: string, _dir: string, options: string, action: string, profile: any) => {
    seen.action = action; seen.profile = profile;
    return { ok: true, plan_id: "plan-1", old_launch_options: options, desired_launch_options: wrapper, operations: [] };
  };
  fixtures.apply_xmx = { ok: true, plan_id: "plan-1", action: "enable", phase: "pending_enable",
    old_launch_options: "", desired_launch_options: wrapper, pending_launch: true };
  fixtures.finish_xmx = (_appid: string, _plan: string, options: string) => {
    seen.applied = options; state = bridged;
    return { ok: true, installed: true, phase: "enabled", pending_launch: false };
  };
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    assert.equal(toggle(p.host, "FSR\u2192XeSS bridge").checked, false);
    await flip(p.host, "FSR\u2192XeSS bridge");
    assert.equal(seen.action, "enable", "a never-managed game is enabled, never configured");
    assert.equal(seen.profile.bridge, 1);
    assert.equal(live, wrapper, "the frontend applied the wrapper to Steam itself");
    assert.equal(seen.applied, wrapper, "finish verified the read-back Steam value");
    assert.equal(toggle(p.host, "FSR\u2192XeSS bridge").checked, true, "the fresh state confirms the bridge");
  } finally { await p.cleanup(); }
});

await test("already-enabled game is configured; edits keep the whole profile", async () => {
  let state: typeof bridged = {
    ...hooked, bridges: ["3357650"],
    bridge_profiles: { "3357650": { bridge: 2, sdk_latest: false, fg: 0 } },
  };
  let live = "'/state/apps/3357650/launch.sh' %command%";
  const wrapper = live;
  const seen: any[] = [];
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: async () => live,
    SetAppLaunchOptions: (_id: number, options: string) => { live = options; },
  } };
  fixtures.get_fsr_bridge_state = () => state;
  fixtures.get_xmx_status = () => ({ ok: true, installed: true, legacy: false, phase: "enabled",
    runtime_verified: false,
    profile: { driver: "26.3", discovery: "auto", diagnostics: false, fg: 0,
      bridge: state.bridge_profiles["3357650"]?.bridge ?? 0,
      sdk_latest: state.bridge_profiles["3357650"]?.sdk_latest ?? false } });
  fixtures.plan_xmx = (_appid: string, _dir: string, options: string, action: string, profile: any) => {
    seen.push({ action, profile });
    return { ok: true, plan_id: "plan-2", old_launch_options: options, desired_launch_options: wrapper, operations: [] };
  };
  fixtures.apply_xmx = (_appid: string, _plan: string) => ({ ok: true, plan_id: "plan-2",
    action: "configure", phase: "pending_configure", old_launch_options: wrapper,
    desired_launch_options: wrapper, pending_launch: true });
  fixtures.finish_xmx = () => {
    const next = seen[seen.length - 1].profile;
    state = { ...state, bridges: next.bridge > 0 ? ["3357650"] : [],
      bridge_profiles: next.bridge > 0 ? { "3357650": { bridge: next.bridge, sdk_latest: next.sdk_latest, fg: 0 } } : {} };
    return { ok: true, installed: true, phase: "enabled", pending_launch: false };
  };
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    assert.equal(toggle(p.host, "FSR\u2192XeSS bridge").checked, true);
    assert.match(p.host.textContent ?? "", /Frame generation runs on XeSS/, "FG-only mode is described");
    await press(p.host, "Bridge details");
    assert.equal(toggle(p.host, "Latest Intel XeSS SDK").checked, false);
    assert.equal(button(p.host, "Frame generation only on XeSS").getAttribute("data-disabled"), "true",
      "the active route button is the current one");
    await flip(p.host, "Latest Intel XeSS SDK");
    assert.equal(seen[0].action, "configure", "an installed game must never be re-enabled");
    assert.equal(seen[0].profile.driver, "26.3", "the untouched driver choice survives the edit");
    assert.equal(seen[0].profile.bridge, 2);
    assert.equal(seen[0].profile.sdk_latest, true, "sdk edit keeps mode and on-state");
    assert.equal(toggle(p.host, "Latest Intel XeSS SDK").checked, true);
    await press(p.host, "Full \u2014 FSR upscaling + frame generation on XeSS");
    assert.equal(seen[1].profile.bridge, 1, "route switch");
    assert.equal(seen[1].profile.sdk_latest, true, "route switch keeps sdk_latest");
    assert.equal(seen[1].profile.driver, "26.3");
  } finally { await p.cleanup(); }
});

await test("a pending game transaction blocks the bridge with a clear reason", async () => {
  fixtures.get_fsr_bridge_state = bridged;
  fixtures.get_xmx_status = { ok: true, installed: true, legacy: false, phase: "pending_enable",
    runtime_verified: false, pending: { plan_id: "x", phase: "pending_enable", action: "enable",
      old_launch_options: "", desired_launch_options: "w", operations: [] } };
  const p = await render(<GameFsrBridge appid="3357650" />);
  try {
    await flip(p.host, "FSR\u2192XeSS bridge");
    assert.match(p.host.textContent ?? "", /already pending/, "the panel points at Setup recovery");
    assert.equal(calls.includes("plan_xmx"), false, "nothing plans over a pending journal");
  } finally { await p.cleanup(); }
});

await test("a game page without an app id explains instead of toggling", async () => {
  fixtures.get_fsr_bridge_state = bridged;
  const p = await render(<GameFsrBridge appid={null} />);
  try {
    assert.match(p.host.textContent ?? "", /no App ID/);
    assert.equal(p.host.querySelector('input[type=checkbox]'), null);
    assert.equal(calls.includes("get_xmx_status"), false);
  } finally { await p.cleanup(); }
});

// ---- settings surface ----

await test("settings section reports scope and never toggles games", async () => {
  fixtures.get_fsr_bridge_state = bridged;
  const p = await render(<BridgeGlobalSection />);
  try {
    assert.match(p.host.textContent ?? "", /installed \(1\/1\) — 1 game bridged/);
    assert.match(p.host.textContent ?? "", /per game only \(session file parked\)/);
    assert.match(p.host.textContent ?? "", /game.s page/);
    assert.equal(p.host.querySelector('input[type=checkbox]'), null, "no game toggles live here anymore");
  } finally { await p.cleanup(); }
});

await test("hooks pointing at a moved package report stale and re-offer install", async () => {
  fixtures.get_fsr_bridge_state = {
    ...bare,
    session_conf: "active",
    hooks: { protons: ["Proton-GE"], hooked: [], plain: [], stale: ["Proton-GE"], installed: false },
  };
  const p = await render(<BridgeGlobalSection />);
  try {
    assert.match(p.host.textContent ?? "", /moved — reinstall/, "stale hooks are named, not called installed");
    assert.equal(button(p.host, "Install Proton hooks (advanced)").getAttribute("data-disabled"), null,
      "the reinstall button is reachable again after a package move");
  } finally { await p.cleanup(); }
});

await test("uninstall needs Details, an arming press and the exact token", async () => {
  const uninstalls: unknown[][] = [];
  let state: typeof bridged = bridged;
  fixtures.get_fsr_bridge_state = () => state;
  fixtures.uninstall_fsr_bridge = (token: string) => { uninstalls.push([token]); state = bare; return { ok: true }; };
  const p = await render(<BridgeGlobalSection />);
  try {
    assert.equal(calls.includes("uninstall_fsr_bridge"), false);
    await press(p.host, "Details");
    await press(p.host, "Uninstall hooks…");
    assert.equal(uninstalls.length, 0, "the first press only arms the confirmation");
    await typeValue(p.host, "Type uninstall-hooks", "uninstall-hook");
    assert.equal(button(p.host, "Confirm: remove upstream hooks").getAttribute("data-disabled"), "true");
    await typeValue(p.host, "Type uninstall-hooks", "uninstall-hooks");
    await press(p.host, "Confirm: remove upstream hooks");
    assert.deepEqual(uninstalls, [["uninstall-hooks"]]);
  } finally { await p.cleanup(); }
});

await test("detected games are reported but never bridged automatically", async () => {
  fixtures.get_fsr_bridge_state = { ...hooked,
    detected: [{ appid: "999999", name: "Other FSR Game", managed: false, marker: "fsr" }] };
  const p = await render(<BridgeGlobalSection />);
  try {
    assert.match(p.host.textContent ?? "", /Detected, never touched \(1\)/);
    assert.match(p.host.textContent ?? "", /Other FSR Game \(fsr\)/);
    assert.equal(calls.some((name: string) => name === "get_xmx_status"), false, "detection never toggles anything");
  } finally { await p.cleanup(); }
});

await test("a malformed state answer shows an error and mounts no controls", async () => {
  fixtures.get_fsr_bridge_state = { ok: true, session_conf: "maybe" };
  const game = await render(<GameFsrBridge appid="3357650" />);
  try {
    assert.match(game.host.textContent ?? "", /Invalid bridge status|bridge status/i);
    assert.equal(game.host.querySelector('input[type=checkbox]'), null);
    assert.equal(hasButton(game.host, "Install Proton hooks (advanced)"), false);
  } finally { await game.cleanup(); }
});

console.log(`fsr-bridge harness: ${checks} checks passed`);
