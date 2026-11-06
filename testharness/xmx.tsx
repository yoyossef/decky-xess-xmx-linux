import assert from "node:assert/strict";
import { XmxController, type XmxApply } from "../src/xmx";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fixtures, calls as rpcCalls } from "@decky/api";
import { XmxPanel } from "../src/components/XmxPanel";
import { Router } from "@decky/ui";
import { readLaunchOptions, setLaunchOptions } from "../src/hooks/useRunningGame";

async function renderPanel(props: { appid: string | null; running: boolean } = { appid: "42", running: false }) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(<XmxPanel {...props} />); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); });
  return { host, root, cleanup: async () => { await act(async () => root.unmount()); host.remove(); } };
}

const statusFixture = {
  ok: true, installed: false, legacy: false, phase: "disabled", pending: null, runtime_verified: false,
  suggested_package: "", package: { valid: false, errors: ["Select an existing package"] },
  flatpak: { visibility: "unknown" },
};
Object.assign(fixtures, {
  get_pref: (key: string, fallback: unknown) => ({ key, value: fallback }),
  set_pref: { ok: true },
  get_xmx_package_status: { ok: true, state: "idle", package: null, latest: null },
  get_xmx_status: statusFixture,
  // Unknown/error DLL state must fail closed, so unrelated tests supply an
  // authoritative empty DLL journal rather than accidentally relying on it.
  get_xess_dll_status: { ok: true, installed: false, phase: "disabled", pending: null, trial_available: false,
    sdk: { tag: "v3.0.2", version: "2.0.2.68", ready: false }, candidates: [] },
});

let checks = 0;
async function test(name: string, run: () => void | Promise<void>) {
  const filter = (globalThis as any).process?.env.XMX_TEST;
  if (filter && !name.includes(filter)) return;
  await run();
  checks++;
  console.log(`PASS ${name}`);
}

function pendingResponse(overrides: Record<string, unknown> = {}): XmxApply {
  return { ok: true, pending_launch: true, plan_id: "plan-1", action: "enable", phase: "pending_enable",
    old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%", ...overrides };
}

function rig(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  let live: string | null = "gamemoderun %command%";
  let plannedOptions = live;
  let plannedAction = "enable";
  const dependencies = {
    readLive: async () => { calls.push("read"); return live; },
    dispatch: (_id: string, options: string) => { calls.push("dispatch"); live = options; return true; },
    allowed: () => true,
    wait: async () => {},
    plan: async (_id: string, _dir: string, old: string, _action: string) => {
      calls.push("plan");
      plannedOptions = old;
      plannedAction = _action;
      return { ok: true, plan_id: "plan-1", old_launch_options: old,
        desired_launch_options: "XMX=1 gamemoderun %command%", operations: ["stage owned files"] };
    },
    apply: async () => { calls.push("apply"); return pendingResponse({ old_launch_options: plannedOptions, action: plannedAction, phase: `pending_${plannedAction}` }); },
    finish: async () => { calls.push("finish"); return { ok: true }; },
    ...overrides,
  };
  const controller = new XmxController("42", dependencies);
  return { controller, calls, dependencies, setLive: (value: string | null) => { live = value; } };
}

await test("unavailable active-client options block preview before backend planning", async () => {
  const r = rig();
  r.setLive(null);
  await r.controller.preview("/existing/package", "enable");
  assert.match(r.controller.state.error ?? "", /live|active.*client/i);
  assert.deepEqual(r.calls, ["read"]);
  assert.equal(r.controller.state.preview, null);
});

await test("preview shows exact old/new options and operations without applying", async () => {
  const r = rig();
  await r.controller.preview("/existing/package", "enable");
  assert.deepEqual(r.calls, ["read", "plan"]);
  assert.equal(r.controller.state.preview?.old_launch_options, "gamemoderun %command%");
  assert.equal(r.controller.state.preview?.desired_launch_options, "XMX=1 gamemoderun %command%");
  assert.deepEqual(r.controller.state.preview?.operations, ["stage owned files"]);
  assert.equal(r.controller.state.busy, false);
});

await test("changed live options invalidate preview before apply", async () => {
  const r = rig();
  await r.controller.preview("/existing/package", "enable");
  r.setLive("new user options %command%");
  await r.controller.confirm();
  assert.match(r.controller.state.error ?? "", /changed|preview/i);
  assert.equal(r.controller.state.preview, null);
  assert.equal(r.calls.includes("apply"), false);
  assert.equal(r.calls.includes("dispatch"), false);
});

await test("explicit confirm applies, dispatches then finishes only exact readback", async () => {
  const r = rig();
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.deepEqual(r.calls, ["read", "plan", "read", "apply", "read", "dispatch", "read", "finish"]);
  assert.equal(r.controller.state.pending, null);
  assert.equal(r.controller.state.preview, null);
  assert.equal(r.controller.state.success, true);
});

await test("a true setter return without effect retries boundedly and never finishes", async () => {
  const r = rig({ dispatch: () => true });
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.equal(r.calls.filter(c => c === "read").length, 8); // preview + pre-apply + post-apply + five readbacks
  assert.equal(r.calls.includes("finish"), false);
  assert.ok(r.controller.state.pending);
  assert.equal(r.controller.state.success, false);
  assert.match(r.controller.state.error ?? "", /verified|readback/i);
});

await test("finish failure preserves pending recovery and resumes without rewriting verified options", async () => {
  const r = rig();
  let finishes = 0;
  r.dependencies.finish = async () => { r.calls.push("finish"); return ++finishes > 1 ? { ok: true } : { ok: false, error: "journal unavailable" }; };
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.ok(r.controller.state.pending);
  assert.equal(r.controller.state.success, false);
  assert.match(r.controller.state.error ?? "", /journal unavailable/);
  r.controller.restorePending({ plan_id: "plan-1", action: "enable", phase: "pending_enable", old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%" });
  await r.controller.resume();
  assert.equal(r.calls.filter(c => c === "apply").length, 1);
  assert.equal(r.calls.filter(c => c === "dispatch").length, 1);
  assert.equal(r.calls.filter(c => c === "finish").length, 2);
  assert.equal(r.controller.state.pending, null);
  assert.equal(r.controller.state.success, true);
});

await test("navigation during backend apply prevents stale Steam writes and leaves recovery pending", async () => {
  let release!: (value: unknown) => void;
  const r = rig({ apply: () => new Promise(resolve => { release = resolve; }) });
  await r.controller.preview("/existing/package", "enable");
  const applying = r.controller.confirm();
  await Promise.resolve();
  r.controller.dispose();
  release(pendingResponse());
  await applying;
  assert.equal(r.calls.includes("dispatch"), false);
  assert.equal(r.calls.includes("finish"), false);
  assert.ok(r.controller.state.pending);
});

await test("running game blocks every transaction entry point", async () => {
  let allowed = false;
  const r = rig({ allowed: () => allowed });
  await r.controller.preview("/existing/package", "enable");
  assert.deepEqual(r.calls, []);
  assert.match(r.controller.state.error ?? "", /running|stopped/i);
  allowed = true;
  await r.controller.preview("/existing/package", "enable");
  allowed = false;
  await r.controller.confirm();
  assert.equal(r.calls.includes("apply"), false);
});

for (const boundary of ["preview-read", "plan", "confirm-read", "launch-read", "readback", "wait"]) {
  await test(`game switch at ${boundary} stops the stale continuation`, async () => {
    let reads = 0;
    const r = rig();
    const originalRead = r.dependencies.readLive;
    r.dependencies.readLive = async () => {
      const value = await originalRead();
      reads++;
      const cutoff = { "preview-read": 1, "confirm-read": 2, "launch-read": 3, "readback": 4 }[boundary];
      if (reads === cutoff) r.controller.dispose();
      return value;
    };
    if (boundary === "plan") {
      const originalPlan = r.dependencies.plan;
      r.dependencies.plan = async (...args) => { const p = await originalPlan(...args); r.controller.dispose(); return p; };
    }
    if (boundary === "wait") {
      r.dependencies.dispatch = () => true;
      r.dependencies.wait = async () => { r.controller.dispose(); };
    }
    await r.controller.preview("/existing/package", "enable");
    await r.controller.confirm();
    assert.equal(r.calls.includes("finish"), false);
    if (["preview-read", "plan", "confirm-read"].includes(boundary)) assert.equal(r.calls.includes("apply"), false);
    if (boundary === "launch-read") assert.equal(r.calls.includes("dispatch"), false);
  });
}

await test("busy and pending transactions cannot be replaced or applied twice", async () => {
  const r = rig();
  await r.controller.preview("/existing/package", "enable");
  const first = r.controller.confirm();
  const second = r.controller.confirm();
  await Promise.all([first, second]);
  assert.equal(r.calls.filter(c => c === "apply").length, 1);
  const pending = rig({ dispatch: () => false });
  await pending.controller.preview("/existing/package", "enable");
  await pending.controller.confirm();
  const callsBefore = pending.calls.length;
  await pending.controller.preview("/somewhere/else", "disable");
  assert.equal(pending.calls.length, callsBefore);
  assert.ok(pending.controller.state.pending);
});

for (const response of [
  { ok: false, error: "legacy unmanaged install" },
  { ok: true, plan_id: "bad", old_launch_options: "different account", desired_launch_options: "x", operations: [] },
  { ok: true, plan_id: "", old_launch_options: "gamemoderun %command%", desired_launch_options: "x", operations: [] },
]) {
  await test("invalid or refused backend plans never become confirmable", async () => {
    const r = rig({ plan: async () => response });
    await r.controller.preview("/existing/package", "enable");
    assert.equal(r.controller.state.preview, null);
    assert.ok(r.controller.state.error);
    await r.controller.confirm();
    assert.equal(r.calls.includes("apply"), false);
  });
}

for (const response of [
  { ok: true, phase: "pending_launch", desired_launch_options: "surprise different options" },
  { ok: true, phase: "unknown", desired_launch_options: "XMX=1 gamemoderun %command%" },
]) {
  await test("unconfirmed apply response leaves non-resumable pending recovery, never Steam writes", async () => {
    const r = rig({ apply: async () => response });
    await r.controller.preview("/existing/package", "enable");
    await r.controller.confirm();
    await r.controller.resume();
    assert.ok(r.controller.state.pending);
    assert.equal(r.controller.state.pendingReady, false);
    assert.equal(r.calls.includes("dispatch"), false);
    assert.equal(r.calls.includes("finish"), false);
  });
}

await test("independent panel distinguishes managed install, package, Flatpak, and unverified runtime", async () => {
  const p = await renderPanel();
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /XeSS XMX/);
  assert.match(p.host.textContent ?? "", /not installed/i);
  assert.match(p.host.textContent ?? "", /Package validation/);
  assert.match(p.host.textContent ?? "", /Flatpak visibility/);
  assert.match(p.host.textContent ?? "", /Runtime unverified/);
  assert.ok(p.host.querySelector('[data-label="Package folder"]'));
  assert.equal(rpcCalls.includes("prepare_payload"), false);
  await p.cleanup();
});

function fieldProps(host: Element, label: string) {
  const element = host.querySelector(`[data-label="${label}"]`)!;
  const key = Object.keys(element).find(k => k.startsWith("__reactFiber"))!;
  let fiber = (element as any)[key];
  while (fiber.return) fiber = fiber.return;
  const find = (node: any): any => {
    if (!node) return undefined;
    if (node.memoizedProps?.label === label && node.memoizedProps?.onChange) return node.memoizedProps;
    return find(node.child) ?? find(node.sibling);
  };
  return find(fiber.stateNode.current);
}
function packageInput(host: Element) {
  const input = host.querySelector<HTMLInputElement>('input[aria-label="Package folder"]');
  if (!input) throw new Error("native package input is not rendered");
  return input;
}
async function enterPackage(host: Element, value: string) {
  const input = packageInput(host);
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function press(host: Element, text: string) {
  const button = Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === text);
  assert.ok(button, `button exists: ${text}`);
  await act(async () => { button.dispatchEvent(new window.MouseEvent("click", { bubbles: true })); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); });
}

await test("first-visit hydration fills the native input and previews the detected package without typing", async () => {
  const preference = deferred<{ key: string; value: string }>();
  const status = deferred<unknown>();
  const requests: unknown[][] = [];
  const planned: unknown[][] = [];
  const saved: unknown[][] = [];
  const previous = { get_pref: fixtures.get_pref, get_xmx_status: fixtures.get_xmx_status, plan_xmx: fixtures.plan_xmx, set_pref: fixtures.set_pref };
  const steam = (globalThis as any).SteamClient;
  fixtures.get_pref = () => preference.promise;
  fixtures.get_xmx_status = (...args: unknown[]) => { requests.push(args); return status.promise; };
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "detected-plan", old_launch_options: args[2], desired_launch_options: "after %command%", operations: [] };
  };
  fixtures.set_pref = (...args: unknown[]) => { saved.push(args); return { ok: true }; };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await openDetails(p.host);
  try {
    assert.equal(packageInput(p.host).value, "");
    assert.equal(packageInput(p.host).disabled, true);
    assert.deepEqual(requests, []);
    await act(async () => preference.resolve({ key: "xmx_package_dir", value: "" }));
    assert.deepEqual(requests, [["42", ""]]);
    assert.equal(packageInput(p.host).value, "");
    assert.equal(packageInput(p.host).disabled, true);
    await act(async () => status.resolve({ ...statusFixture, suggested_package: "/suggested/package", package: { path: "/detected/actual package", valid: true, errors: [] } }));
    assert.ok(Array.from(p.host.querySelectorAll("pre")).some(e => e.textContent === "/detected/actual package"), "diagnostics already display the detected path");
    assert.equal(packageInput(p.host).value, "/detected/actual package", "displayed input must hydrate, not only its React prop");
    assert.equal(fieldProps(p.host, "Package folder").value, packageInput(p.host).value);
    const button = Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Enable XMX…")!;
    assert.equal(button.getAttribute("data-disabled"), null);
    await press(p.host, "Enable XMX…");
    assert.deepEqual(planned, [["42", "/detected/actual package", "before %command%", "enable"]]);
    assert.deepEqual(saved, [["xmx_package_dir", "/detected/actual package"]]);
    assert.match(p.host.textContent ?? "", /Dry-run preview/);
    assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
  } finally {
    await p.cleanup();
    Object.assign(fixtures, previous);
    (globalThis as any).SteamClient = steam;
  }
});

await test("first-visit hydration respects an asynchronously remembered package over the suggestion", async () => {
  const preference = deferred<{ key: string; value: string }>();
  const requests: unknown[][] = [];
  fixtures.get_pref = () => preference.promise;
  fixtures.get_xmx_status = (...args: unknown[]) => {
    requests.push(args);
    return { ...statusFixture, suggested_package: "/suggested/package", package: { path: "/remembered/package", valid: true, errors: [] } };
  };
  const p = await renderPanel();
  await openDetails(p.host);
  try {
    assert.equal(packageInput(p.host).value, "");
    assert.equal(packageInput(p.host).disabled, true);
    await act(async () => preference.resolve({ key: "xmx_package_dir", value: "/remembered/package" }));
    assert.equal(packageInput(p.host).value, "/remembered/package");
    assert.equal(fieldProps(p.host, "Package folder").value, packageInput(p.host).value);
    assert.deepEqual(requests, [["42", "/remembered/package"]]);
  } finally {
    await p.cleanup();
    fixtures.get_pref = (key: string, fallback: unknown) => ({ key, value: fallback });
    fixtures.get_xmx_status = statusFixture;
  }
});

for (const suggested of ["/suggested/package", ""]) {
  await test(`first-visit hydration ${suggested ? "uses the suggestion when diagnostic path is absent" : "keeps an undetected package blank and preview blocked"}`, async () => {
    fixtures.get_pref = (key: string, fallback: unknown) => ({ key, value: fallback });
    fixtures.get_xmx_status = { ...statusFixture, suggested_package: suggested };
    const p = await renderPanel();
    assert.deepEqual(buttonLabels(panelSection(p.host)), ["Update package", "Enable XMX…", "Refresh", "Details"]);
    assert.match(panelSection(p.host).textContent ?? "", /Details.*Package folder.*Refresh/);
    assert.equal(p.host.querySelector('input[aria-label="Package folder"]'), null);
    await openDetails(p.host);
    try {
      assert.equal(packageInput(p.host).value, suggested);
      assert.equal(fieldProps(p.host, "Package folder").value, packageInput(p.host).value);
      assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Enable XMX…")?.getAttribute("data-disabled"), suggested ? null : "true");
    } finally {
      await p.cleanup();
      fixtures.get_xmx_status = statusFixture;
    }
  });
}

await test("clearing the package deliberately is not undone by validation fallback diagnostics", async () => {
  fixtures.get_pref = (key: string) => ({ key, value: "/chosen/package" });
  fixtures.get_xmx_status = { ...statusFixture, suggested_package: "/detected/package", package: { path: "/detected/package", valid: true, errors: [] } };
  const p = await renderPanel();
  await openDetails(p.host);
  assert.equal(packageInput(p.host).value, "/chosen/package");
  await enterPackage(p.host, "");
  assert.equal(fieldProps(p.host, "Package folder").value, "");
  await press(p.host, "Refresh");
  assert.equal(packageInput(p.host).value, "");
  assert.equal(fieldProps(p.host, "Package folder").value, "");
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Enable XMX…")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
  fixtures.get_xmx_status = statusFixture;
});

await test("package directory loads and is remembered through existing preferences without default home paths", async () => {
  const requests: unknown[][] = [];
  const saved: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/user selected/package" });
  fixtures.set_pref = (...args: unknown[]) => { saved.push(args); return { ok: true }; };
  fixtures.get_xmx_status = (...args: unknown[]) => { requests.push(args); return statusFixture; };
  const p = await renderPanel();
  await openDetails(p.host);
  assert.equal(fieldProps(p.host, "Package folder").value, "/user selected/package");
  assert.deepEqual(requests.at(-1), ["42", "/user selected/package"]);
  await act(async () => { fieldProps(p.host, "Package folder").onChange({ target: { value: "/new/package" } }); });
  await press(p.host, "Refresh");
  assert.equal(saved.at(-1)?.[1], "/new/package");
  assert.deepEqual(requests.at(-1), ["42", "/new/package"]);
  await p.cleanup();
});

await test("UI requires preview and explicit confirmation, uses only Steam active-client options", async () => {
  let live = "user wrapper %command%";
  const requests: unknown[][] = [];
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: async () => live,
    SetAppLaunchOptions: (_id: number, options: string) => { live = options; },
  } };
  fixtures.get_xmx_status = { ...statusFixture, package: { valid: true, errors: [] } };
  fixtures.plan_xmx = (...args: unknown[]) => {
    requests.push(args);
    return { ok: true, plan_id: "ui-plan", old_launch_options: args[2], desired_launch_options: "XMX=1 user wrapper %command%", operations: [{ operation: "stage", path: "/owned/file" }] };
  };
  fixtures.apply_xmx = pendingResponse({ plan_id: "ui-plan", old_launch_options: "user wrapper %command%", desired_launch_options: "XMX=1 user wrapper %command%" });
  fixtures.finish_xmx = { ok: true };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await openDetails(p.host);
  await press(p.host, "Enable XMX…");
  assert.deepEqual(requests.at(-1), ["42", "/user selected/package", "user wrapper %command%", "enable"]);
  assert.match(p.host.textContent ?? "", /Old launch options/);
  assert.match(p.host.textContent ?? "", /New launch options/);
  assert.match(p.host.textContent ?? "", /\/owned\/file/);
  assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
  await press(p.host, "Apply changes");
  assert.ok(rpcCalls.slice(before).includes("finish_xmx"));
  assert.match(p.host.textContent ?? "", /Launch options verified/);
  assert.equal(rpcCalls.slice(before).includes("get_launch_options"), false);
  await p.cleanup();
});

await test("documented backend pending_enable apply response is accepted without weakening option checks", async () => {
  const r = rig({ apply: async () => pendingResponse() });
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.equal(r.controller.state.success, true);
});

await test("persisted pending record is restored after remount and resumes only its exact desired options", async () => {
  const r = rig();
  r.controller.restorePending({ plan_id: "persisted", action: "enable", phase: "pending_enable", old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%" });
  assert.ok(r.controller.state.pending);
  assert.equal(r.controller.state.pendingReady, true);
  r.setLive("another user edit");
  await r.controller.resume();
  assert.equal(r.calls.includes("dispatch"), false);
  assert.equal(r.calls.includes("finish"), false);
  assert.ok(r.controller.state.pending);
  r.setLive("XMX=1 gamemoderun %command%");
  await r.controller.resume();
  assert.deepEqual(r.calls, ["read", "read", "finish"]);
  assert.equal(r.controller.state.pending, null);
});

await test("pending recovery survives remount and blocks new previews until explicit safe resume", async () => {
  const pending = { plan_id: "after-reload", action: "enable", phase: "pending_enable", old_launch_options: "before %command%", desired_launch_options: "after %command%" };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "after %command%" } };
  fixtures.get_xmx_status = { ...statusFixture, phase: "pending_enable", pending };
  const finishes: unknown[][] = [];
  fixtures.finish_xmx = (...args: unknown[]) => { finishes.push(args); fixtures.get_xmx_status = { ...statusFixture, installed: true, phase: "enabled", pending: null }; return { ok: true }; };
  const before = rpcCalls.length;
  let p = await renderPanel();
  assert.match(p.host.textContent ?? "", /Pending recovery/);
  await p.cleanup();
  p = await renderPanel();
  await press(p.host, "Enable XMX…");
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await press(p.host, "Resume pending transaction");
  assert.deepEqual(finishes, [["42", "after-reload", "after %command%"]]);
  assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
  assert.doesNotMatch(p.host.textContent ?? "", /Pending recovery/);
  await p.cleanup();
});

await test("legacy unmanaged installation is identified and blocks all mutations", async () => {
  fixtures.get_xmx_status = { ...statusFixture, installed: true, legacy: true };
  const before = rpcCalls.length;
  const p = await renderPanel();
  assert.match(p.host.textContent ?? "", /Legacy unmanaged.*blocked/i);
  assert.equal(buttonLabels(panelSection(p.host)).includes("Enable XMX…"), false, "installed games cannot be enabled a second time");
  await press(p.host, "Save settings…");
  await press(p.host, "Disable XMX…");
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await p.cleanup();
});

await test("GameDetail Setup contains the XeSS panel with and without OptiScaler", async () => {
  const { GameDetail } = await import("../src/components/GameDetail");
  fixtures.get_xmx_status = statusFixture;
  fixtures.get_auto_plan = { plan: { available: false, enabled: false }, recommendation: null };
  fixtures.get_live_status = null;
  fixtures.get_monitor = { state: {}, problems: [], recent: [], configured: {} };
  fixtures.read_config = { ok: true, values: {} };
  window.localStorage.setItem("decky-optiscaler:advanced", "1");
  for (const installed of [false, true]) {
    fixtures.get_game = {
      path: "/games/native", name: "Native game", target: "/games/native", candidates: [],
      install: { installed, path: "/games/native", filename: installed ? "dxgi.dll" : null, backed_up: [], candidates: [], extra_proxies: [], fsr4: { ready: false, files: {} } },
      gpu: { names: [], name: "test", vendor: "amd", generation: "test" }, writable: true,
      fsr4_sources: [], ini_info: { present: false },
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(<GameDetail appid="42" gameName="Native game" gamePath="/games/native" status={null} runningGame={null} onBack={() => {}} />));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    assert.match(host.querySelector('[data-tab="install"]')?.textContent ?? "", /XeSS XMX/);
    await act(async () => root.unmount());
    host.remove();
  }
});

await test("partial enable abort is explicit and requires exact original live options without writing them", async () => {
  const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
  r.controller.restorePending({ plan_id: "partial", action: "enable", phase: "writing_enable", old_launch_options: "before %command%", desired_launch_options: "after %command%" });
  assert.equal(r.controller.state.pendingReady, false);
  r.setLive("unrelated edit");
  await r.controller.abort();
  assert.equal(r.calls.includes("recover"), false);
  assert.ok(r.controller.state.pending);
  r.setLive("before %command%");
  await r.controller.abort();
  assert.deepEqual(r.calls, ["read", "read", "recover"]);
  assert.equal(r.controller.state.pending, null);
});

await test("partial backend write offers explicit abort after manual restoration, not unsafe resume", async () => {
  const pending = { plan_id: "interrupted", action: "enable", phase: "writing_enable", old_launch_options: "before %command%", desired_launch_options: "after %command%" };
  fixtures.get_xmx_status = { ...statusFixture, phase: "recovery_required", pending };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  const recovered: unknown[][] = [];
  fixtures.recover_xmx = (...args: unknown[]) => { recovered.push(args); fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null }; return { ok: true }; };
  const p = await renderPanel();
  assert.match(p.host.textContent ?? "", /restore.*old launch options.*Steam Properties/i);
  assert.equal(p.host.querySelector('input[aria-label="Package folder"]'), null, "required manual restoration and cancel stay on the default surface");
  assert.ok(buttonLabels(panelSection(p.host)).includes("Confirm original options and abort"));
  const resume = Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Resume pending transaction")!;
  assert.equal(resume.getAttribute("data-disabled"), "true");
  await press(p.host, "Confirm original options and abort");
  assert.deepEqual(recovered, [["42", "interrupted", "before %command%", "abort"]]);
  assert.doesNotMatch(p.host.textContent ?? "", /Pending recovery/);
  await p.cleanup();
});

await test("preview remembers a newly entered existing package even without separate validation", async () => {
  fixtures.get_xmx_status = { ...statusFixture, package: { valid: true, errors: [] }, pending: null };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  fixtures.plan_xmx = (_id: string, _dir: string, old: string) => ({ ok: true, plan_id: "remembered-plan", old_launch_options: old, desired_launch_options: "after %command%", operations: [] });
  const saved: unknown[][] = [];
  fixtures.set_pref = (...args: unknown[]) => { saved.push(args); return { ok: true }; };
  const p = await renderPanel();
  await openDetails(p.host);
  await act(async () => fieldProps(p.host, "Package folder").onChange({ target: { value: "/direct/manual/package" } }));
  await press(p.host, "Enable XMX…");
  assert.deepEqual(saved.at(-1), ["xmx_package_dir", "/direct/manual/package"]);
  await p.cleanup();
});

await test("explicit backend no-journal readback clears uncertain apply failure without any rollback", async () => {
  const r = rig({ apply: async () => { throw new Error("response lost"); } });
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.ok(r.controller.state.pending);
  r.controller.restorePending(null);
  assert.equal(r.controller.state.pending, null);
  assert.equal(r.controller.state.error, null);
  assert.equal(r.calls.includes("dispatch"), false);
  assert.equal(r.calls.includes("finish"), false);
});

await test("automatic status refresh resolves confirmed absent journals without claiming apply success", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  fixtures.apply_xmx = async () => { throw new Error("response lost"); };
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  await press(p.host, "Apply changes");
  assert.doesNotMatch(p.host.textContent ?? "", /Pending recovery/);
  assert.match(p.host.textContent ?? "", /response lost/);
  assert.doesNotMatch(p.host.textContent ?? "", /Transaction completed/);
  await p.cleanup();
});

await test("validation and Flatpak visibility have explicit independent labels, not runtime-success claims", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", package: { valid: false, errors: ["Missing package dependency"] }, flatpak: { visible: false, reason: "Selected package is outside sandbox access" } };
  const p = await renderPanel();
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /Package validation: invalid/i);
  assert.match(p.host.textContent ?? "", /Missing package dependency/);
  assert.match(p.host.textContent ?? "", /Flatpak visibility: not visible/i);
  assert.match(p.host.textContent ?? "", /outside sandbox access/);
  assert.match(p.host.textContent ?? "", /Runtime unverified/);
  await p.cleanup();
});

await test("busy UI ignores queued edits and a game switch cannot dispatch the old game's options", async () => {
  let release!: (value: unknown) => void;
  const writes: unknown[][] = [];
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%", SetAppLaunchOptions: (...args: unknown[]) => writes.push(args) } };
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  fixtures.apply_xmx = () => new Promise(resolve => { release = resolve; });
  const before = rpcCalls.length;
  const p = await renderPanel();
  await openDetails(p.host);
  await press(p.host, "Enable XMX…");
  await press(p.host, "Apply changes");
  const field = fieldProps(p.host, "Package folder");
  assert.equal(field.disabled, true);
  await act(async () => field.onChange({ target: { value: "/queued/edit" } }));
  assert.equal(fieldProps(p.host, "Package folder").value, "/user selected/package");
  await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
  await act(async () => { release({ ok: true, pending_launch: true, phase: "pending_enable", desired_launch_options: "after %command%" }); });
  assert.deepEqual(writes, []);
  assert.equal(rpcCalls.slice(before).includes("finish_xmx"), false);
  assert.doesNotMatch(p.host.textContent ?? "", /Pending recovery/);
  await p.cleanup();
});

await test("backend status refusal is shown as unavailable, never not-installed or a render crash", async () => {
  fixtures.get_xmx_status = { ok: false, error: "No unique existing Steam prefix" };
  const p = await renderPanel();
  assert.match(p.host.textContent ?? "", /Installation status unavailable/);
  assert.match(p.host.textContent ?? "", /No unique existing Steam prefix/);
  assert.doesNotMatch(p.host.textContent ?? "", /Not installed/);
  await p.cleanup();
});

await test("malformed pending recovery never invents empty original launch options", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "recovery_required", pending: { plan_id: "incomplete" } };
  const p = await renderPanel();
  assert.match(p.host.textContent ?? "", /Pending recovery/);
  assert.match(p.host.textContent ?? "", /unavailable.*refresh/i);
  assert.doesNotMatch(p.host.textContent ?? "", /\(empty\)/);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Resume pending transaction")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
});

await test("malformed recovery status invalidates older resumable state and blocks new plans", async () => {
  const r = rig();
  r.controller.restorePending({ plan_id: "old", action: "enable", phase: "pending_enable", old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%" });
  r.controller.state.success = true;
  r.controller.restorePending({ plan_id: "new-but-incomplete" });
  assert.equal(r.controller.state.success, false);
  await r.controller.resume();
  await r.controller.preview("/existing/package", "enable");
  assert.deepEqual(r.calls, []);
  assert.equal(r.controller.state.pendingReady, false);
});

await test("interrupted disable restoration resumes with exact desired options, unlike partial enable", async () => {
  const r = rig();
  r.controller.restorePending({ plan_id: "restoring-disable", action: "disable", phase: "restoring", old_launch_options: "XMX=1 gamemoderun %command%", desired_launch_options: "gamemoderun %command%" });
  assert.equal(r.controller.state.pendingReady, true);
  await r.controller.resume();
  assert.deepEqual(r.calls, ["read", "finish"]);
  assert.equal(r.controller.state.success, true);
});

await test("game starting between polling updates is rechecked live before backend planning", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => { (Router as any).MainRunningApp = { appid: 42 }; return "before %command%"; } } };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  assert.match(p.host.textContent ?? "", /game must be stopped/i);
  (Router as any).MainRunningApp = undefined;
  await p.cleanup();
});

// Regression checks for the coordinator's already-established safety rules.
await test("empty live options are a known value, not permission to use an offline fallback", async () => {
  const r = rig();
  r.setLive("");
  await r.controller.preview("/existing/package", "enable");
  assert.equal(r.controller.state.preview?.old_launch_options, "");
  await r.controller.confirm();
  assert.equal(r.controller.state.success, true);
});

await test("launch-options race during backend apply keeps pending and never overwrites the user edit", async () => {
  const r = rig();
  r.dependencies.apply = async () => { r.setLive("new user edit"); return pendingResponse(); };
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.ok(r.controller.state.pending);
  assert.equal(r.calls.includes("dispatch"), false);
  assert.equal(r.calls.includes("finish"), false);
});

await test("false dispatch keeps pending without finishing or automatically rolling back", async () => {
  const r = rig({ dispatch: () => false, recover: async () => { throw new Error("Automatic rollback forbidden"); } });
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.ok(r.controller.state.pending);
  assert.equal(r.calls.includes("finish"), false);
  assert.match(r.controller.state.error ?? "", /Steam rejected/);
});

await test("delayed exact live readback can finish within the bounded retries", async () => {
  let waits = 0;
  const r = rig({ dispatch: () => true });
  r.dependencies.wait = async () => { if (++waits === 2) r.setLive("XMX=1 gamemoderun %command%"); };
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.equal(waits, 2);
  assert.equal(r.controller.state.success, true);
  assert.equal(r.calls.filter(c => c === "finish").length, 1);
});

await test("readback differing by whitespace is not exact confirmation", async () => {
  const r = rig();
  r.dependencies.dispatch = (_id: string, options: string) => { r.setLive(options + " "); return true; };
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  assert.ok(r.controller.state.pending);
  assert.equal(r.calls.includes("finish"), false);
});

await test("unavailable Steam getters never call the backend VDF fallback or plan", async () => {
  (globalThis as any).SteamClient = undefined;
  (globalThis as any).appDetailsStore = undefined;
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  assert.match(p.host.textContent ?? "", /Live active-client launch options are unavailable/);
  assert.equal(rpcCalls.slice(before).includes("get_launch_options"), false);
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await p.cleanup();
});

await test("appDetailsStore is an active-client source when the direct getter is absent", async () => {
  (globalThis as any).SteamClient = { Apps: {} };
  (globalThis as any).appDetailsStore = { GetAppDetails: () => ({ unAppID: 42, strLaunchOptions: "active store %command%" }) };
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /active store %command%/);
  await p.cleanup();
  (globalThis as any).appDetailsStore = undefined;
});

await test("running prop disables every setup action and queued field edits", async () => {
  const p = await renderPanel({ appid: "42", running: true });
  await openDetails(p.host);
  const before = rpcCalls.length;
  assert.equal(fieldProps(p.host, "Package folder").disabled, true);
  await press(p.host, "Enable XMX…");
  assert.equal(buttonLabels(panelSection(p.host)).includes("Disable XMX…"), false, "no disable action without an installation");
  await press(p.host, "Refresh");
  assert.equal(rpcCalls.length, before);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Update package")?.getAttribute("data-disabled"), null, "package-only global preparation remains independent while playing");
  assert.ok(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).filter(e => !["Details", "Hide details", "Update package"].includes(e.textContent ?? "")).every(e => e.getAttribute("data-disabled") === "true"), "all game setup/inspection actions stay disabled; read-only expansion and independent preparation remain accessible");
  await p.cleanup();
});

await test("backend finish errors require fresh status before any recovery retry", async () => {
  const r = rig({ finish: async () => { r.calls.push("finish"); return { ok: false, error: "disable restoration interrupted" }; } });
  await r.controller.preview("/existing/package", "enable");
  await r.controller.confirm();
  const before = r.calls.length;
  await r.controller.resume();
  await r.controller.abort();
  assert.equal(r.calls.length, before, "no recovery calls until backend pending phase has been refreshed");
});

await test("UI automatically refreshes journal state after a backend apply error before offering recovery", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  fixtures.plan_xmx = (_id: string, _dir: string, old: string) => ({ ok: true, plan_id: "partial-write", old_launch_options: old, desired_launch_options: "after %command%", operations: [] });
  fixtures.apply_xmx = () => {
    fixtures.get_xmx_status = { ...statusFixture, phase: "recovery_required", pending: { plan_id: "partial-write", action: "enable", phase: "writing_enable", old_launch_options: "before %command%", desired_launch_options: "after %command%" } };
    return { ok: false, error: "disk full after first write" };
  };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  await press(p.host, "Apply changes");
  const calls = rpcCalls.slice(before);
  assert.ok(calls.lastIndexOf("get_xmx_status") > calls.indexOf("apply_xmx"), "read journal after uncertain mutation");
  assert.match(p.host.textContent ?? "", /Pending recovery/);
  assert.match(p.host.textContent ?? "", /disk full after first write/);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Resume pending transaction")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
});

await test("disable restoration already started cannot offer or execute abort", async () => {
  const pending = { plan_id: "restoring", action: "disable", phase: "restoring", old_launch_options: "enabled %command%", desired_launch_options: "original %command%" };
  const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
  r.controller.restorePending(pending);
  r.setLive(pending.old_launch_options);
  await r.controller.abort();
  assert.deepEqual(r.calls, [], "must not ask user to restore the wrong options for an irreversible disable");
  fixtures.get_xmx_status = { ...statusFixture, phase: "recovery_required", pending };
  const p = await renderPanel();
  assert.match(p.host.textContent ?? "", /restoration.*started.*resume/i);
  assert.doesNotMatch(p.host.textContent ?? "", /manually restore the exact old/i);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Confirm original options and abort")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
});

await test("failed status refresh never leaves stale managed status authorizing new previews", async () => {
  fixtures.get_xmx_status = { ...statusFixture, installed: true, phase: "enabled", pending: null };
  const p = await renderPanel();
  fixtures.get_xmx_status = async () => { throw new Error("status connection lost"); };
  await press(p.host, "Refresh");
  assert.match(p.host.textContent ?? "", /Installation status unavailable/);
  assert.doesNotMatch(p.host.textContent ?? "", /Enabled \(configured\)/);
  const before = rpcCalls.length;
  assert.equal(buttonLabels(panelSection(p.host)).includes("Disable XMX…"), false, "unavailable status cannot authorize disable");
  await press(p.host, "Enable XMX…");
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await p.cleanup();
});

await test("only the documented action-bound apply response can authorize a Steam write", async () => {
  for (const wrong of [
    { plan_id: "different-token" }, { action: "disable" }, { phase: "pending_disable" },
    { old_launch_options: "different active account" }, { pending_launch: false }, { phase: "pending_launch" },
  ]) {
    const r = rig({ apply: async () => pendingResponse(wrong) });
    await r.controller.preview("/existing/package", "enable");
    await r.controller.confirm();
    assert.equal(r.calls.includes("dispatch"), false, JSON.stringify(wrong));
    assert.equal(r.calls.includes("finish"), false);
    assert.ok(r.controller.state.pending);
    assert.equal(r.controller.state.pendingReady, false);
  }
});

await test("missing or malformed refreshed journals invalidate older recovery permissions", async () => {
  for (const value of [undefined, false, "unavailable", 7]) {
    const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
    r.controller.restorePending({ plan_id: "old", action: "enable", phase: "pending_enable", old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%" });
    r.controller.restorePending(value);
    await r.controller.resume();
    await r.controller.abort();
    await r.controller.preview("/existing/package", "enable");
    assert.deepEqual(r.calls, [], String(value));
  }
  fixtures.get_xmx_status = { ...statusFixture, phase: "pending_enable", pending: { plan_id: "stale", action: "enable", phase: "pending_enable", old_launch_options: "before %command%", desired_launch_options: "after %command%" } };
  const p = await renderPanel();
  fixtures.get_xmx_status = { ...statusFixture, phase: "recovery_required", pending: null };
  await press(p.host, "Refresh");
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Resume pending transaction")?.getAttribute("data-disabled"), "true");
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Confirm original options and abort")?.getAttribute("data-disabled"), "true");
  assert.doesNotMatch(p.host.textContent ?? "", /before %command%/);
  await p.cleanup();
});

await test("malformed success status is unavailable rather than falsely not-installed or crashing", async () => {
  for (const response of [
    { ok: true }, { ...statusFixture, phase: 42 }, { ...statusFixture, installed: undefined },
    { ...statusFixture, package: { valid: false, errors: "not an array" } },
  ]) {
    fixtures.get_xmx_status = response;
    const p = await renderPanel();
    assert.match(p.host.textContent ?? "", /Installation status unavailable/);
    assert.doesNotMatch(p.host.textContent ?? "", /Not installed/);
    const before = rpcCalls.length;
    await press(p.host, "Enable XMX…");
    assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
    await p.cleanup();
  }
});

await test("queued preview cannot race an in-flight package/status validation", async () => {
  fixtures.get_xmx_status = { ...statusFixture, phase: "disabled", pending: null };
  const p = await renderPanel();
  let release!: (value: unknown) => void;
  fixtures.get_xmx_status = () => new Promise(resolve => { release = resolve; });
  const button = (label: string) => Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === label)!;
  const before = rpcCalls.length;
  await act(async () => {
    button("Refresh").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    button("Enable XMX…").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await act(async () => { release({ ...statusFixture, phase: "disabled", pending: null }); });
  await p.cleanup();
});

await test("interrupted enable abort can continue its restoration but cannot resume enabling", async () => {
  const pending = { plan_id: "aborting-enable", action: "enable", phase: "restoring", old_launch_options: "before %command%", desired_launch_options: "after %command%" };
  const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
  r.controller.restorePending(pending);
  await r.controller.resume();
  assert.deepEqual(r.calls, []);
  r.setLive("before %command%");
  await r.controller.abort();
  assert.deepEqual(r.calls, ["read", "recover"]);
  assert.equal(r.controller.state.pending, null);
});

await test("a missing journal even in stable status visibly blocks all new setup actions", async () => {
  fixtures.get_xmx_status = { ...statusFixture, pending: undefined };
  const p = await renderPanel();
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /Pending recovery/);
  assert.equal(fieldProps(p.host, "Package folder").disabled, true);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Enable XMX…")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
});

await test("editing a selected package invalidates both its preview and cached package diagnostics", async () => {
  fixtures.get_xmx_status = { ...statusFixture, package: { valid: true, path: "/validated/package", errors: [] } };
  fixtures.plan_xmx = (_id: string, _dir: string, old: string) => ({ ok: true, plan_id: "edit-plan", old_launch_options: old, desired_launch_options: "after %command%", operations: [] });
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  const p = await renderPanel();
  await openDetails(p.host);
  await press(p.host, "Enable XMX…");
  assert.match(p.host.textContent ?? "", /Dry-run preview/);
  await enterPackage(p.host, "/different/unvalidated/package");
  assert.equal(fieldProps(p.host, "Package folder").value, packageInput(p.host).value);
  assert.doesNotMatch(p.host.textContent ?? "", /Dry-run preview|\/validated\/package/);
  assert.match(p.host.textContent ?? "", /Package validation: not checked/);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).some(e => e.textContent === "Apply changes"), false);
  await p.cleanup();
});

await test("disable previews without a package and commits only an exactly empty active-client readback", async () => {
  let live = "enabled wrapper %command%";
  const planned: unknown[][] = [];
  const finished: unknown[][] = [];
  const writes: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "" });
  fixtures.get_xmx_status = { ...statusFixture, installed: true, phase: "enabled" };
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "disable-empty", old_launch_options: live, desired_launch_options: "", operations: [{ target: "/owned/ControlLib.dll", operation: "restore" }] };
  };
  fixtures.apply_xmx = pendingResponse({ plan_id: "disable-empty", action: "disable", phase: "pending_disable", old_launch_options: live, desired_launch_options: "" });
  fixtures.finish_xmx = (...args: unknown[]) => { finished.push(args); fixtures.get_xmx_status = { ...statusFixture }; return { ok: true }; };
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: async () => live,
    SetAppLaunchOptions: (...args: unknown[]) => { writes.push(args); live = args[1] as string; },
  } };
  const p = await renderPanel();
  await openDetails(p.host);
  await press(p.host, "Disable XMX…");
  assert.deepEqual(planned, [["42", "", "enabled wrapper %command%", "disable"]]);
  assert.deepEqual(writes, []);
  assert.match(p.host.textContent ?? "", /restore/);
  await press(p.host, "Apply changes");
  assert.deepEqual(writes, [[42, ""]]);
  assert.deepEqual(finished, [["42", "disable-empty", ""]]);
  assert.match(p.host.textContent ?? "", /Not installed/);
  assert.match(p.host.textContent ?? "", /Runtime unverified/);
  await p.cleanup();
});

await test("unmount while awaiting exact readback never sends a stale finish", async () => {
  let reads = 0;
  let release!: (value: string) => void;
  fixtures.get_pref = (key: string) => ({ key, value: "/existing/package" });
  fixtures.get_xmx_status = statusFixture;
  fixtures.plan_xmx = (_id: string, _dir: string, old: string) => ({ ok: true, plan_id: "unmount-read", old_launch_options: old, desired_launch_options: "after %command%", operations: [] });
  fixtures.apply_xmx = pendingResponse({ plan_id: "unmount-read", old_launch_options: "before %command%", desired_launch_options: "after %command%" });
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: async () => ++reads === 4 ? new Promise<string>(resolve => { release = resolve; }) : "before %command%",
    SetAppLaunchOptions: () => {},
  } };
  const before = rpcCalls.length;
  const p = await renderPanel();
  await press(p.host, "Enable XMX…");
  await press(p.host, "Apply changes");
  assert.equal(reads, 4);
  await p.cleanup();
  await act(async () => release("after %command%"));
  assert.equal(rpcCalls.slice(before).includes("finish_xmx"), false);
});

await test("late status from a previous game cannot replace the current game's independent panel", async () => {
  let release!: (value: unknown) => void;
  const previousPref = fixtures.get_pref;
  fixtures.get_pref = (key: string, fallback: unknown) => ({ key, value: fallback });
  fixtures.get_xmx_status = (id: string) => id === "42" ? new Promise(resolve => { release = resolve; }) : { ...statusFixture, installed: true, phase: "enabled", package: { valid: true, path: "/current-game/package", errors: [] } };
  const p = await renderPanel();
  await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
  await act(async () => release({ ...statusFixture, legacy: true, phase: "recovery_required", pending: { plan_id: "old-game" }, package: { valid: true, path: "/old-game/package", errors: [] } }));
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /Enabled \(configured\)/);
  assert.doesNotMatch(p.host.textContent ?? "", /Legacy unmanaged|Pending recovery|\/old-game\/package/);
  assert.equal(packageInput(p.host).value, "/current-game/package");
  assert.equal(fieldProps(p.host, "Package folder").value, packageInput(p.host).value);
  await p.cleanup();
  fixtures.get_pref = previousPref;
});

await test("finish failure refreshes the disable journal's irreversible restoration phase", async () => {
  const pending = { plan_id: "disable-recovery", action: "disable", phase: "pending_disable", old_launch_options: "enabled %command%", desired_launch_options: "before %command%" };
  fixtures.get_xmx_status = { ...statusFixture, installed: true, phase: "pending_disable", pending };
  fixtures.finish_xmx = () => { fixtures.get_xmx_status = { ...statusFixture, installed: true, phase: "recovery_required", pending: { ...pending, phase: "restoring" } }; return { ok: false, error: "restore interrupted" }; };
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "before %command%" } };
  const p = await renderPanel();
  const before = rpcCalls.length;
  await press(p.host, "Resume pending transaction");
  assert.deepEqual(rpcCalls.slice(before), ["finish_xmx", "get_xmx_status"]);
  assert.match(p.host.textContent ?? "", /restore interrupted/);
  assert.match(p.host.textContent ?? "", /restoration.*started.*resume/i);
  assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Confirm original options and abort")?.getAttribute("data-disabled"), "true");
  await p.cleanup();
});

await test("static GPU and native-game diagnostics remain separate from installed and runtime status", async () => {
  fixtures.get_xmx_status = { ...statusFixture, gpu: { xmx_capable: true, reason: "Only the validated PCI device is accepted" }, native_xess: { found: true, reason: "DLL presence is only a candidate" } };
  const p = await renderPanel();
  await openDetails(p.host);
  assert.match(p.host.textContent ?? "", /GPU static readiness: v1-validated device/);
  assert.match(p.host.textContent ?? "", /Only the validated PCI device/);
  assert.match(p.host.textContent ?? "", /Native XeSS candidate: DLL detected/);
  assert.match(p.host.textContent ?? "", /DLL presence is only a candidate/);
  assert.match(p.host.textContent ?? "", /Not installed/);
  assert.match(p.host.textContent ?? "", /Runtime unverified/);
  await p.cleanup();
});

await test("a queued old handler cannot plan after the latest status became unavailable", async () => {
  fixtures.get_xmx_status = statusFixture;
  const p = await renderPanel();
  const button = Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Enable XMX…")!;
  const propsKey = Object.keys(button).find(key => key.startsWith("__reactProps"))!;
  const queued = (button as any)[propsKey].onClick;
  fixtures.get_xmx_status = { ok: false, error: "Cannot safely inspect this prefix" };
  await press(p.host, "Refresh");
  const before = rpcCalls.length;
  await act(async () => queued());
  assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
  await p.cleanup();
});

await test("launch reader hydrates cold details through the active client's async subscription, including empty options", async () => {
  const steam = (globalThis as any).SteamClient;
  const store = (globalThis as any).appDetailsStore;
  let cached: unknown;
  let subscriptions = 0;
  let unsubscriptions = 0;
  const active = new Set<unknown>();
  const before = rpcCalls.length;
  (globalThis as any).appDetailsStore = { GetAppDetails: () => cached };
  (globalThis as any).SteamClient = { Apps: {
    RegisterForAppDetails: (appid: number, callback: (details: unknown) => void) => {
      assert.equal(appid, 1817230);
      subscriptions++;
      active.add(callback);
      queueMicrotask(() => {
        cached = { unAppID: appid, strLaunchOptions: "" };
        callback(cached);
      });
      return { unregister: () => { unsubscriptions++; active.delete(callback); } };
    },
    GetLaunchOptionsForApp: () => { throw new Error("Executable choices are not custom launch options"); },
  } };
  try {
    assert.equal(cached, undefined, "Steam's details store starts cold");
    assert.equal(await readLaunchOptions(1817230), "", "an explicit empty raw string is known, not unavailable");
    assert.equal(subscriptions, 1);
    assert.equal(unsubscriptions, 1);
    assert.equal(active.size, 0);
    assert.deepEqual(rpcCalls.slice(before), [], "no backend/VDF read");
  } finally {
    (globalThis as any).SteamClient = steam;
    (globalThis as any).appDetailsStore = store;
  }
});

async function withLaunchClient(apps: Record<string, unknown>, run: () => Promise<void>, cached?: unknown) {
  const steam = (globalThis as any).SteamClient;
  const store = (globalThis as any).appDetailsStore;
  (globalThis as any).SteamClient = { Apps: apps };
  (globalThis as any).appDetailsStore = { GetAppDetails: () => cached };
  try { await run(); } finally {
    (globalThis as any).SteamClient = steam;
    (globalThis as any).appDetailsStore = store;
  }
}

await test("launch reader cleans synchronous and repeated callbacks exactly once", async () => {
  let unsubscriptions = 0;
  let callback!: (details: unknown) => void;
  await withLaunchClient({
    RegisterForAppDetails: (appid: number, receive: typeof callback) => {
      callback = receive;
      receive({ unAppID: appid, strLaunchOptions: "sync %command%" });
      receive({ unAppID: appid, strLaunchOptions: "must not replace the first response" });
      return { unregister: () => {
        unsubscriptions++;
        receive({ unAppID: appid, strLaunchOptions: "reentrant cleanup" });
      } };
    },
  }, async () => {
    assert.equal(await readLaunchOptions(42), "sync %command%");
    assert.equal(unsubscriptions, 1, "the handle only exists after a synchronous callback returns");
    callback({ unAppID: 42, strLaunchOptions: "late" });
    assert.equal(unsubscriptions, 1);
  });
});

await test("launch reader waits for exact numeric unAppID identity, ignoring other or missing IDs", async () => {
  let unsubscriptions = 0;
  await withLaunchClient({
    RegisterForAppDetails: (_appid: number, receive: (details: unknown) => void) => {
      queueMicrotask(() => {
        for (const unAppID of [43, "42", undefined, null, NaN]) {
          receive({ unAppID, strLaunchOptions: "foreign options" });
          assert.equal(unsubscriptions, 0, `must keep waiting after ${String(unAppID)}`);
        }
        receive({ unAppID: 42, strLaunchOptions: "  exact raw %command%  " });
      });
      return { unregister: () => { unsubscriptions++; } };
    },
  }, async () => {
    assert.equal(await readLaunchOptions(42), "  exact raw %command%  ");
    assert.equal(unsubscriptions, 1);
  });
});

async function withLaunchClock(run: (clock: {
  timers: Map<number, { fire: () => void; delay: number }>;
  expire: () => void;
}) => Promise<void>) {
  const set = window.setTimeout;
  const clear = window.clearTimeout;
  let next = 0;
  const timers = new Map<number, { fire: () => void; delay: number }>();
  window.setTimeout = ((fire: () => void, delay: number) => {
    timers.set(++next, { fire, delay });
    return next;
  }) as typeof window.setTimeout;
  window.clearTimeout = (id: number | undefined) => { timers.delete(id!); };
  try {
    await run({ timers, expire: () => {
      const [id, timer] = timers.entries().next().value!;
      timers.delete(id);
      timer.fire();
    } });
  } finally {
    window.setTimeout = set;
    window.clearTimeout = clear;
  }
}

await test("launch reader bounds an unanswered subscription and cleans timeout and late callbacks without cached fallback", async () => {
  let unsubscriptions = 0;
  let receive!: (details: unknown) => void;
  const before = rpcCalls.length;
  await withLaunchClient({
    RegisterForAppDetails: (_appid: number, callback: typeof receive) => {
      receive = callback;
      return { unregister: () => { unsubscriptions++; } };
    },
  }, async () => withLaunchClock(async clock => {
    const reading = readLaunchOptions(42);
    assert.equal(clock.timers.size, 1, "a silent Steam client must not leave the read pending forever");
    const delay = clock.timers.values().next().value!.delay;
    assert.ok(delay > 0 && delay <= 5000, "the read has a short finite deadline");
    assert.equal(unsubscriptions, 0);
    clock.expire();
    assert.equal(await reading, null, "unavailable is not empty or stale cached options");
    assert.equal(unsubscriptions, 1);
    assert.equal(clock.timers.size, 0);
    receive({ unAppID: 42, strLaunchOptions: "too late" });
    assert.equal(unsubscriptions, 1);
    assert.deepEqual(rpcCalls.slice(before), []);
  }), { unAppID: 42, strLaunchOptions: "stale store value" });
});

await test("launch reader waits for a raw string instead of coercing missing or other API data", async () => {
  let unsubscriptions = 0;
  const malformed = [undefined, null, 7, false, ["executable choice"], { strLaunchOptions: "nested" }, new String("boxed")];
  await withLaunchClient({
    RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
      queueMicrotask(() => {
        for (const strLaunchOptions of malformed) {
          receive({ unAppID: appid, strLaunchOptions });
          assert.equal(unsubscriptions, 0, "a partial or malformed callback is not a known string");
        }
        receive({ unAppID: appid, strLaunchOptions: "" });
      });
      return { unregister: () => { unsubscriptions++; } };
    },
  }, async () => {
    assert.equal(await readLaunchOptions(42), "");
    assert.equal(unsubscriptions, 1);
  });
});

await test("launch reader returns unavailable and clears its deadline when registration throws, without cached fallback", async () => {
  let registrations = 0;
  await withLaunchClient({
    RegisterForAppDetails: () => { registrations++; throw new Error("Steam registration failed"); },
  }, async () => withLaunchClock(async clock => {
    assert.equal(await readLaunchOptions(42), null);
    assert.equal(registrations, 1);
    assert.equal(clock.timers.size, 0, "registration failure must cancel its deadline");
  }), { unAppID: 42, strLaunchOptions: "do not trust this cache" });
});

await test("launch reader releases the subscription and timer when callback access throws", async () => {
  let receive!: (details: unknown) => void;
  let unsubscriptions = 0;
  await withLaunchClient({
    RegisterForAppDetails: (_appid: number, callback: typeof receive) => {
      receive = callback;
      return { unregister: () => { unsubscriptions++; } };
    },
  }, async () => withLaunchClock(async clock => {
    const reading = readLaunchOptions(42);
    assert.doesNotThrow(() => receive({ unAppID: 42, get strLaunchOptions() { throw new Error("Steam details unavailable"); } }));
    assert.equal(await reading, null);
    assert.equal(unsubscriptions, 1);
    assert.equal(clock.timers.size, 0);
    receive({ unAppID: 42, strLaunchOptions: "late" });
    assert.equal(unsubscriptions, 1);
  }));
});

await test("launch reader attempts a throwing unsubscribe only once and still settles its bounded read", async () => {
  let receive!: (details: unknown) => void;
  let unsubscriptions = 0;
  await withLaunchClient({
    RegisterForAppDetails: (_appid: number, callback: typeof receive) => {
      receive = callback;
      return { unregister: () => { unsubscriptions++; throw new Error("client cleanup failed"); } };
    },
  }, async () => withLaunchClock(async clock => {
    const reading = readLaunchOptions(42);
    let outcome: unknown = "pending";
    void reading.then(value => { outcome = value; });
    assert.doesNotThrow(() => receive({ unAppID: 42, strLaunchOptions: "raw %command%" }));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.notEqual(outcome, "pending", "a cleanup exception cannot strand the read after its timer was cleared");
    assert.equal(await reading, "raw %command%");
    assert.equal(unsubscriptions, 1);
    assert.equal(clock.timers.size, 0);
    receive({ unAppID: 42, strLaunchOptions: "late" });
    assert.equal(unsubscriptions, 1);
  }));
});

for (const [label, details] of [
  ["missing launch-option field", { unAppID: 42 }],
  ["wrong app", { unAppID: 43, strLaunchOptions: "foreign" }],
  ["string app ID", { unAppID: "42", strLaunchOptions: "foreign" }],
  ["executable choices", [{ unAppID: 42, strLaunchOptions: "not a custom-options response" }]],
  ["no details", null],
] as const) {
  await test(`launch reader times out ${label} as unavailable and unsubscribes once`, async () => {
    let unsubscriptions = 0;
    let receive!: (details: unknown) => void;
    await withLaunchClient({
      RegisterForAppDetails: (_appid: number, callback: typeof receive) => {
        receive = callback;
        return { unregister: () => { unsubscriptions++; } };
      },
    }, async () => withLaunchClock(async clock => {
      const reading = readLaunchOptions(42);
      receive(details);
      assert.equal(clock.timers.size, 1);
      assert.equal(unsubscriptions, 0);
      clock.expire();
      assert.equal(await reading, null);
      assert.equal(unsubscriptions, 1);
      assert.equal(clock.timers.size, 0);
    }));
  });
}

for (const synchronous of [false, true]) {
  await test(`launch reader cancels its deadline after ${synchronous ? "synchronous" : "asynchronous"} success`, async () => {
    let unsubscriptions = 0;
    await withLaunchClient({
      RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
        const reply = () => receive({ unAppID: appid, strLaunchOptions: "" });
        if (synchronous) reply(); else queueMicrotask(reply);
        return { unregister: () => { unsubscriptions++; } };
      },
    }, async () => withLaunchClock(async clock => {
      assert.equal(await readLaunchOptions(42), "");
      assert.equal(unsubscriptions, 1);
      assert.equal(clock.timers.size, 0);
    }));
  });
}

for (const options of ["", "  direct %command%  "]) {
  await test(`launch reader preserves a client's direct getter (${JSON.stringify(options)}) ahead of subscription and cache`, async () => {
    const requests: number[] = [];
    await withLaunchClient({
      GetAppLaunchOptions: async (appid: number) => { requests.push(appid); return options; },
      RegisterForAppDetails: () => { throw new Error("unneeded subscription"); },
      GetLaunchOptionsForApp: () => { throw new Error("executable choices are not launch options"); },
    }, async () => withLaunchClock(async clock => {
      assert.equal(await readLaunchOptions(42), options);
      assert.deepEqual(requests, [42]);
      assert.equal(clock.timers.size, 0);
    }), { unAppID: 42, strLaunchOptions: "stale cache" });
  });
}

for (const kind of ["throws", "array", "object", "number"] as const) {
  await test(`launch reader uses active details when the direct getter ${kind}, never coercing API data`, async () => {
    let subscriptions = 0;
    let unsubscriptions = 0;
    await withLaunchClient({
      GetAppLaunchOptions: async () => {
        if (kind === "throws") throw new Error("getter unavailable on this client");
        return { array: ["executable choices"], object: { strLaunchOptions: "nested" }, number: 7 }[kind];
      },
      RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
        subscriptions++;
        queueMicrotask(() => receive({ unAppID: appid, strLaunchOptions: "active callback %command%" }));
        return { unregister: () => { unsubscriptions++; } };
      },
    }, async () => {
      assert.equal(await readLaunchOptions(42), "active callback %command%");
      assert.equal(subscriptions, 1);
      assert.equal(unsubscriptions, 1);
    });
  });
}

await test("launch reader takes fresh post-write subscriptions despite a stale cached store before transaction finish", async () => {
  let live = "before %command%";
  const reads: string[] = [];
  const writes: unknown[][] = [];
  let unsubscriptions = 0;
  const active = new Set<unknown>();
  await withLaunchClient({
    RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
      assert.equal(appid, 42);
      assert.equal(active.size, 0, "each transaction read cleans up before the next");
      active.add(receive);
      reads.push(live);
      queueMicrotask(() => receive({ unAppID: appid, strLaunchOptions: live }));
      return { unregister: () => { unsubscriptions++; active.delete(receive); } };
    },
    SetAppLaunchOptions: (...args: unknown[]) => { writes.push(args); live = args[1] as string; },
    GetLaunchOptionsForApp: () => { throw new Error("not a custom-options reader"); },
  }, async () => {
    const r = rig({
      readLive: async () => { r.calls.push("read"); return readLaunchOptions(42); },
      dispatch: (_id: string, value: string) => { r.calls.push("dispatch"); return setLaunchOptions(42, value); },
      finish: async () => {
        r.calls.push("finish");
        assert.equal(reads.at(-1), "XMX=1 gamemoderun %command%", "finish follows a fresh exact readback");
        return { ok: true };
      },
    });
    await r.controller.preview("/existing/package", "enable");
    assert.equal(r.controller.state.preview?.old_launch_options, "before %command%");
    assert.deepEqual(writes, [], "Preview is read-only");
    await r.controller.confirm();
    assert.deepEqual(reads, ["before %command%", "before %command%", "before %command%", "XMX=1 gamemoderun %command%"]);
    assert.deepEqual(writes, [[42, "XMX=1 gamemoderun %command%"]]);
    assert.equal(unsubscriptions, reads.length);
    assert.equal(active.size, 0);
    assert.equal(r.controller.state.success, true);
  }, { unAppID: 42, strLaunchOptions: "stale desired value, not the active account" });
});

await test("launch reader rejects cached details for wrong, string or missing app identity when subscription is absent", async () => {
  for (const unAppID of [43, "42", undefined]) {
    await withLaunchClient({}, async () => {
      assert.equal(await readLaunchOptions(42), null, `untrusted cached identity: ${String(unAppID)}`);
    }, { unAppID, strLaunchOptions: "must not use another app's settings" });
  }
});

await test("launch reader never lets stale desired cache confirm an ineffective active-client setter", async () => {
  let subscriptions = 0;
  let unsubscriptions = 0;
  const writes: unknown[][] = [];
  await withLaunchClient({
    RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
      subscriptions++;
      queueMicrotask(() => receive({ unAppID: appid, strLaunchOptions: "before %command%" }));
      return { unregister: () => { unsubscriptions++; } };
    },
    SetAppLaunchOptions: (...args: unknown[]) => { writes.push(args); },
  }, async () => {
    const r = rig({
      readLive: async () => { r.calls.push("read"); return readLaunchOptions(42); },
      dispatch: (_id: string, value: string) => { r.calls.push("dispatch"); return setLaunchOptions(42, value); },
    });
    await r.controller.preview("/existing/package", "enable");
    await r.controller.confirm();
    assert.equal(subscriptions, 8, "preview, pre-apply, pre-write and five exact readbacks stay fresh");
    assert.equal(unsubscriptions, subscriptions);
    assert.deepEqual(writes, [[42, "XMX=1 gamemoderun %command%"]]);
    assert.equal(r.calls.includes("finish"), false);
    assert.ok(r.controller.state.pending);
    assert.equal(r.controller.state.success, false);
    assert.match(r.controller.state.error ?? "", /verified|readback/i);
  }, { unAppID: 42, strLaunchOptions: "XMX=1 gamemoderun %command%" });
});

await test("cold-cache Enable XMX… uses the subscribed active-client empty string without backend VDF fallback or writes", async () => {
  const previous = { get_pref: fixtures.get_pref, get_xmx_status: fixtures.get_xmx_status, plan_xmx: fixtures.plan_xmx };
  const planned: unknown[][] = [];
  let subscriptions = 0;
  let unsubscriptions = 0;
  fixtures.get_pref = (key: string) => ({ key, value: "/existing/package" });
  fixtures.get_xmx_status = statusFixture;
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "cold-preview", old_launch_options: args[2], desired_launch_options: "after %command%", operations: [] };
  };
  try {
    await withLaunchClient({
      RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
        assert.equal(appid, 1817230);
        subscriptions++;
        queueMicrotask(() => receive({ unAppID: appid, strLaunchOptions: "" }));
        return { unregister: () => { unsubscriptions++; } };
      },
      GetLaunchOptionsForApp: () => { throw new Error("executable-choice API is forbidden"); },
      SetAppLaunchOptions: () => { throw new Error("Preview must not write Steam options"); },
    }, async () => {
      const before = rpcCalls.length;
      const p = await renderPanel({ appid: "1817230", running: false });
      try {
        await press(p.host, "Enable XMX…");
        assert.deepEqual(planned, [["1817230", "/existing/package", "", "enable"]]);
        assert.match(p.host.textContent ?? "", /Dry-run preview/);
        assert.doesNotMatch(p.host.textContent ?? "", /launch options are unavailable/);
        assert.equal(subscriptions, 1);
        assert.equal(unsubscriptions, 1);
        assert.equal(rpcCalls.slice(before).includes("get_launch_options"), false);
        assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
      } finally { await p.cleanup(); }
    });
  } finally { Object.assign(fixtures, previous); }
});

await test("launch reader never calls executable-choice GetLaunchOptionsForApp even when it is the only Apps getter", async () => {
  let executableChoiceReads = 0;
  await withLaunchClient({
    GetLaunchOptionsForApp: () => { executableChoiceReads++; return [{ strLaunchOptions: "not raw custom options" }]; },
  }, async () => {
    assert.equal(await readLaunchOptions(42), null);
    assert.equal(executableChoiceReads, 0);
  });
});

await test("launch reader retains exact cached strings only for clients without a subscription API", async () => {
  for (const strLaunchOptions of ["", "  cached %command%  ", undefined, ["not raw"], 7]) {
    await withLaunchClient({}, async () => {
      assert.equal(await readLaunchOptions(42), typeof strLaunchOptions === "string" ? strLaunchOptions : null);
    }, { unAppID: 42, strLaunchOptions });
  }
});

await test("profile configure preview forwards explicit per-game choices without applying", async () => {
  const planned: unknown[][] = [];
  const profile = { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false };
  const r = rig({ plan: async (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "profile-plan", old_launch_options: args[2], desired_launch_options: "profile %command%", operations: ["update profile"] };
  } });
  await (r.controller.preview as any)("/package", "configure", profile);
  assert.deepEqual(planned, [["42", "/package", "gamemoderun %command%", "configure", profile]]);
  assert.equal(r.controller.state.preview?.action, "configure");
  assert.equal(r.calls.includes("apply"), false);
});

const defaultProfile = { driver: "default", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false };
const profileStatus = { ...statusFixture, installed: true, phase: "enabled", profile: defaultProfile,
  drivers: [ { id: "default", label: "Package default", valid: true }, { id: "26.1", label: "26.1 (validated)", valid: true }, { id: "26.3", label: "26.3", valid: false, reason: "Missing driver files" } ] };
function nativeControl<T extends Element>(host: Element, label: string): T {
  const element = host.querySelector<T>(`[aria-label="${label}"]`);
  assert.ok(element, `native control exists: ${label}`);
  return element;
}
async function choose(host: Element, label: string, value: string) {
  await act(async () => {
    const select = nativeControl<HTMLSelectElement>(host, label);
    select.value = value;
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
}
async function toggle(host: Element, label: string) {
  await act(async () => nativeControl<HTMLInputElement>(host, label).click());
}
await test("native profile choices are read-only until update preview and edits invalidate the preview", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.plan_xmx = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "choices", old_launch_options: args[2], desired_launch_options: "new %command%", operations: ["profile diff"] }; };
  const before = rpcCalls.length;
  await withLaunchClient({ GetAppLaunchOptions: () => "old %command%" }, async () => {
    const p = await renderPanel();
    await openDetails(p.host);
    try {
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "default");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, false);
      assert.doesNotMatch(nativeControl<HTMLSelectElement>(p.host, "Driver").textContent ?? "", /26.3/);
      assert.match(p.host.textContent ?? "", /Missing driver files/);
      await choose(p.host, "Driver", "26.1");
      await toggle(p.host, "Intel detection fix");
      await toggle(p.host, "Debug logs");
      assert.deepEqual(planned, []);
      await press(p.host, "Save settings…");
      assert.deepEqual(planned, [["42", "/package", "old %command%", "configure", { driver: "26.1", discovery: "alternate", diagnostics: true, fg: 0, bridge: 0, sdk_latest: false }]]);
      assert.match(p.host.textContent ?? "", /profile diff/);
      await toggle(p.host, "Debug logs");
      assert.doesNotMatch(p.host.textContent ?? "", /Dry-run preview/);
      await press(p.host, "Import tested setup…");
      assert.equal(planned.at(-1)?.[3], "adopt_trial");
      assert.deepEqual(planned.at(-1)?.[4], { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false });
      assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("frame generation defaults to the game's own, reaches the profile and reads back in the preview", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    const profile = (args[4] ?? defaultProfile) as Record<string, unknown>;
    return { ok: true, plan_id: "fg", old_launch_options: args[2], desired_launch_options: "new %command%",
      profile, operations: ["fg diff"] };
  };
  await withLaunchClient({ GetAppLaunchOptions: () => "old %command%" }, async () => {
    const p = await renderPanel();
    try {
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Frame generation").value, "0");
      assert.doesNotMatch(nativeControl<HTMLSelectElement>(p.host, "Frame generation").textContent ?? "", /experimental/,
        "experimental multipliers stay hidden until selected");
      await choose(p.host, "Frame generation", "3");
      await press(p.host, "Save settings…");
      assert.equal((planned.at(-1)![4] as any).fg, 3);
      assert.match(p.host.textContent ?? "", /frame generation 3x/i);
      await press(p.host, "Details");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Frame generation multiplier (experimental)").value, "3");
      await choose(p.host, "Frame generation multiplier (experimental)", "7");
      await press(p.host, "Save settings…");
      assert.equal((planned.at(-1)![4] as any).fg, 7);
      assert.match(p.host.textContent ?? "", /frame generation 7x/i);
      assert.match(nativeControl<HTMLSelectElement>(p.host, "Frame generation").textContent ?? "", /7x \(experimental\)/,
        "the compact control names the chosen experimental multiplier");
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("a stored frame generation multiplier hydrates the compact control and disables cleanly", async () => {
  const old = { ...fixtures };
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = { ...profileStatus, profile: { ...defaultProfile, fg: 4 } };
  await withLaunchClient({ GetAppLaunchOptions: () => "old %command%" }, async () => {
    const p = await renderPanel();
    try {
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Frame generation").value, "4");
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("delayed profile hydration is game-scoped and validation preserves edits but expires previews", async () => {
  const old = { ...fixtures };
  const delayed = deferred<unknown>();
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = () => delayed.promise;
  fixtures.plan_xmx = (...args: unknown[]) => ({ ok: true, plan_id: "hydrate", old_launch_options: args[2], desired_launch_options: "updated %command%", operations: [] });
  await withLaunchClient({ GetAppLaunchOptions: () => "old %command%" }, async () => {
    const p = await renderPanel();
    try {
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").disabled, true);
      await act(async () => delayed.resolve({ ...profileStatus, profile: { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false } }));
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "26.1");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Intel detection fix").checked, true);
      await choose(p.host, "Driver", "default");
      await press(p.host, "Save settings…");
      assert.match(p.host.textContent ?? "", /Dry-run preview/);
      const refresh = deferred<unknown>();
      fixtures.get_xmx_status = () => refresh.promise;
      await press(p.host, "Refresh");
      assert.doesNotMatch(p.host.textContent ?? "", /Dry-run preview/, "status refresh invalidates the old preview immediately");
      await act(async () => refresh.resolve({ ...profileStatus, profile: { driver: "26.1", discovery: "alternate", diagnostics: true, fg: 0, bridge: 0, sdk_latest: false } }));
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "default", "late stored choices cannot clobber intentional edits");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, false);
      fixtures.get_xmx_status = { ...profileStatus, profile: defaultProfile };
      await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "default");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Intel detection fix").checked, false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

for (const action of ["configure", "adopt_trial"] as const) {
  await test(`${action} recovery accepts only action-bound pending or writing phases and exact launch readback`, async () => {
    const record = { plan_id: "plan-1", action, phase: `pending_${action}`, old_launch_options: "gamemoderun %command%", desired_launch_options: "XMX=1 gamemoderun %command%" };
    const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
    r.controller.restorePending(record);
    assert.equal(r.controller.state.pendingReady, true);
    assert.equal(r.controller.state.pendingAbortable, true);
    await r.controller.resume();
    assert.equal(r.controller.state.success, true);
    assert.equal(r.calls.includes("apply"), false);
    const partial = rig({ recover: async () => ({ ok: true }) });
    partial.controller.restorePending({ ...record, phase: `writing_${action}` });
    assert.equal(partial.controller.state.pendingReady, false);
    assert.equal(partial.controller.state.pendingAbortable, true);
    await partial.controller.abort();
    assert.equal(partial.controller.state.success, true);
    const malformed = rig();
    malformed.controller.restorePending({ ...record, phase: "pending_enable" });
    assert.equal(malformed.controller.state.recoveryBlocked, true);
    assert.equal(malformed.controller.state.pendingAbortable, false);
  });
}

const dllFixture = { ok: true, installed: false, phase: "disabled", pending: null, trial_available: false,
  sdk: { tag: "v3.0.2", version: "2.0.2.68", ready: true },
  candidates: [{ path: "/game/a/libxess.dll", version: "1.3.0", architecture: "x86_64", sha256: "a".repeat(64) },
    { path: "/game/b/libxess.dll", version: "1.2.0", architecture: "x86_64", sha256: "b".repeat(64) }] };
await test("independent DLL update selects an explicit target, caches the pinned SDK, previews and confirms without Steam writes", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  const applied: unknown[][] = [];
  const finished: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, sdk: { ...dllFixture.sdk, ready: false, reason: "Not downloaded" } };
  fixtures.prepare_xess_sdk = () => { fixtures.get_xess_dll_status = dllFixture; return { ok: true }; };
  fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "dll-update", old_launch_options: args[2], desired_launch_options: args[2], operations: [{ before: "1.2.0", after: "2.0.2.68", target: args[1] }] }; };
  fixtures.apply_xess_dll = (...args: unknown[]) => { applied.push(args); return { ok: true, pending_launch: true, plan_id: "dll-update", action: "enable", phase: "pending_enable", old_launch_options: "raw %command%", desired_launch_options: "raw %command%" }; };
  fixtures.finish_xess_dll = (...args: unknown[]) => { finished.push(args); fixtures.get_xess_dll_status = { ...dllFixture, installed: true, phase: "enabled" }; return { ok: true }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "raw %command%", SetAppLaunchOptions: () => { throw new Error("DLL update must not change Steam options"); } }, async () => {
    const before = rpcCalls.length;
    const p = await renderPanel();
    await openDetails(p.host, "dll");
    try {
      assert.match(p.host.textContent ?? "", /XeSS DLL/);
      assert.match(p.host.textContent ?? "", /1.3.0.*x86_64/);
      assert.match(p.host.textContent ?? "", /legacy.*DLL|DLL.*legacy/i);
      assert.match(p.host.textContent ?? "", /experimental DLL replacement.*ordinal.*may fail/i);
      assert.match(p.host.textContent ?? "", /Restore separately.*disabling XMX keeps this DLL/i);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "", "never guess among several DLLs");
      await press(p.host, "Update DLL…");
      assert.deepEqual(planned, []);
      await choose(p.host, "Game DLL", "/game/b/libxess.dll");
      assert.equal(dllFixture.candidates.find(candidate => candidate.path === nativeControl<HTMLSelectElement>(p.host, "Game DLL").value)?.architecture, "x86_64", "success fixture must select an architecture supported by the real backend");
      await press(p.host, "Download SDK");
      assert.equal(rpcCalls.slice(before).filter(name => name === "prepare_xess_sdk").length, 1);
      assert.deepEqual(applied, []);
      await press(p.host, "Update DLL…");
      assert.deepEqual(planned, [["42", "/game/b/libxess.dll", "raw %command%", "enable", false]]);
      assert.match(p.host.textContent ?? "", /DLL dry-run preview/);
      assert.match(p.host.textContent ?? "", /2.0.2.68/);
      assert.deepEqual(applied, []);
      await press(p.host, "Apply changes");
      assert.deepEqual(applied, [["42", "dll-update"]]);
      assert.deepEqual(finished, [["42", "dll-update", "raw %command%"]]);
      assert.match(p.host.textContent ?? "", /Managed DLL update present/);
      assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("unsupported x86 DLL error candidate is visible but cannot be selected or previewed", async () => {
  const old = { ...fixtures };
  const unsupported = { path: "/game/x86/libxess.dll", version: null, architecture: null, sha256: null,
    error: "Only x86_64 PE32+ DLLs are supported" };
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, candidates: [...dllFixture.candidates, unsupported] };
  const p = await renderPanel();
  assert.match(panelSection(p.host, "dll").textContent ?? "", /Only x86_64 PE32\+ DLLs are supported/, "candidate errors remain visible with Details closed");
  await openDetails(p.host, "dll");
  try {
    assert.match(p.host.textContent ?? "", /Only x86_64 PE32\+ DLLs are supported/);
    assert.match(p.host.textContent ?? "", /\/game\/x86\/libxess.dll/);
    const select = nativeControl<HTMLSelectElement>(p.host, "Game DLL");
    assert.equal(Array.from(select.options).some(option => option.value === unsupported.path), false, "backend-refused candidates must not be offered as selectable targets");
    const before = rpcCalls.length;
    await act(async () => fieldProps(p.host, "Game DLL").onChange({ data: unsupported.path }));
    await press(p.host, "Update DLL…");
    assert.equal(buttonLabels(panelSection(p.host, "dll")).includes("Use existing DLL…"), false, "unrecognized trials are never offered for import");
    assert.equal(rpcCalls.slice(before).includes("plan_xess_dll"), false, "even a forced invalid selection cannot plan a DLL transaction");
    assert.doesNotMatch(p.host.textContent ?? "", /DLL dry-run preview/);
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

await test("DLL trial adoption and restore stay explicit and failed restore finish resumes its independent journal", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  let applies = 0;
  let finishes = 0;
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, trial_available: true };
  fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "dll-restore", old_launch_options: args[2], desired_launch_options: args[2], operations: [args[4] ? "adopt existing trial; retain original backup" : "restore original library"] }; };
  const pending = { plan_id: "dll-restore", action: "disable", phase: "restoring", old_launch_options: "same", desired_launch_options: "same" };
  fixtures.apply_xess_dll = () => { applies++; return { ok: true, pending_launch: true, ...pending, phase: "pending_disable" }; };
  fixtures.finish_xess_dll = () => {
    finishes++;
    if (finishes === 1) { fixtures.get_xess_dll_status = { ...dllFixture, installed: true, phase: "recovery_required", pending }; return { ok: false, error: "DLL restore interrupted" }; }
    fixtures.get_xess_dll_status = dllFixture;
    return { ok: true };
  };
  await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
    const p = await renderPanel();
    await openDetails(p.host, "dll");
    try {
      await choose(p.host, "Game DLL", "/game/a/libxess.dll");
      await press(p.host, "Use existing DLL…");
      assert.equal(planned.at(-1)?.[4], true);
      assert.match(p.host.textContent ?? "", /adopt existing trial/);
      await choose(p.host, "Game DLL", "/game/b/libxess.dll");
      assert.doesNotMatch(p.host.textContent ?? "", /DLL dry-run preview/);
      fixtures.get_xess_dll_status = { ...dllFixture, installed: true, phase: "enabled" };
      await press(p.host, "Refresh DLL status");
      await press(p.host, "Restore original DLL…");
      assert.deepEqual(planned.at(-1), ["42", "/game/b/libxess.dll", "same", "disable", false]);
      assert.equal(applies, 0);
      await press(p.host, "Apply changes");
      assert.match(p.host.textContent ?? "", /Pending DLL recovery/);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true);
      assert.equal(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === "Confirm original options and abort DLL transaction")?.getAttribute("data-disabled"), "true");
      await press(p.host, "Resume pending DLL transaction");
      assert.equal(applies, 1);
      assert.equal(finishes, 2);
      assert.match(p.host.textContent ?? "", /No managed DLL update/);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("DLL download and pending update interlock prefix edits and recovery abort never rolls back implicitly", async () => {
  const old = { ...fixtures };
  const download = deferred<{ ok: boolean }>();
  const recovered: unknown[][] = [];
  let prefixPlans = 0;
  let sdkCalls = 0;
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, sdk: { ...dllFixture.sdk, ready: false } };
  fixtures.prepare_xess_sdk = () => { sdkCalls++; return download.promise.then(result => { fixtures.get_xess_dll_status = dllFixture; return result; }); };
  fixtures.plan_xmx = (...args: unknown[]) => { prefixPlans++; return { ok: true, plan_id: "prefix", old_launch_options: args[2], desired_launch_options: "profile", operations: [] }; };
  fixtures.plan_xess_dll = (...args: unknown[]) => ({ ok: true, plan_id: "dll-partial", old_launch_options: args[2], desired_launch_options: args[2], operations: ["write DLL"] });
  const pending = { plan_id: "dll-partial", action: "enable", phase: "writing_enable", old_launch_options: "same", desired_launch_options: "same" };
  fixtures.apply_xess_dll = () => { fixtures.get_xess_dll_status = { ...dllFixture, phase: "recovery_required", pending }; return { ok: false, error: "DLL write interrupted" }; };
  fixtures.recover_xess_dll = (...args: unknown[]) => { recovered.push(args); fixtures.get_xess_dll_status = dllFixture; return { ok: true }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
    const p = await renderPanel();
    await openDetails(p.host);
    try {
      await press(p.host, "Save settings…");
      assert.match(p.host.textContent ?? "", /Dry-run preview/);
      const queuedProfile = fieldProps(p.host, "Debug logs").onChange;
      await choose(p.host, "Game DLL", "/game/a/libxess.dll");
      await press(p.host, "Download SDK");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true);
      assert.equal(packageInput(p.host).disabled, true);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true);
      assert.doesNotMatch(p.host.textContent ?? "", /Dry-run preview/);
      await act(async () => queuedProfile(true));
      await press(p.host, "Save settings…");
      await press(p.host, "Download SDK");
      assert.equal(prefixPlans, 1);
      assert.equal(sdkCalls, 1);
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, false);
      await act(async () => download.resolve({ ok: true }));
      await press(p.host, "Update DLL…");
      await press(p.host, "Apply changes");
      assert.match(p.host.textContent ?? "", /DLL write interrupted/);
      assert.match(panelSection(p.host, "dll").textContent ?? "", /manually restore the exact original launch options.*Steam Properties.*confirm below/i);
      assert.equal(buttonLabels(panelSection(p.host, "dll")).includes("Hide details"), false, "DLL recovery controls and cancellation instructions do not depend on expansion");
      assert.equal(packageInput(p.host).disabled, true);
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true);
      await press(p.host, "Save settings…");
      assert.equal(prefixPlans, 1);
      assert.deepEqual(recovered, [], "failed apply cannot automatically abort");
      await press(p.host, "Confirm original options and abort DLL transaction");
      assert.deepEqual(recovered, [["42", "dll-partial", "same", "abort"]]);
      assert.equal(packageInput(p.host).disabled, false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("bounded XeSS diagnostic logs are fetched only on request and late logs cannot cross games", async () => {
  const old = { ...fixtures };
  const pendingLogs = deferred<unknown>();
  const requests: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  fixtures.get_xmx_logs = (...args: unknown[]) => { requests.push(args); return pendingLogs.promise; };
  const p = await renderPanel();
  await openDetails(p.host);
  try {
    assert.deepEqual(requests, []);
    assert.match(p.host.textContent ?? "", /overhead.*local paths|local paths.*overhead/i);
    await press(p.host, "Read logs");
    assert.deepEqual(requests, [["42"]]);
    assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true);
    await act(async () => pendingLogs.resolve({ ok: true, runtime_verified: false, logs: [{ name: "Shared compiler log", path: "/cache/compiler.log", exists: true, text: "compiler initialized; not game proof", truncated: true }, { name: "Game log", path: "/game/xess.log", exists: false, text: "", truncated: false }] }));
    assert.match(p.host.textContent ?? "", /compiler initialized; not game proof/);
    assert.match(p.host.textContent ?? "", /truncated/i);
    assert.match(p.host.textContent ?? "", /shared compiler log.*not proof/i);
    assert.match(p.host.textContent ?? "", /not present/);
    await press(p.host, "Clear log view");
    assert.doesNotMatch(p.host.textContent ?? "", /compiler initialized; not game proof/);
    const late = deferred<unknown>();
    fixtures.get_xmx_logs = () => late.promise;
    await press(p.host, "Read logs");
    await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
    await act(async () => late.resolve({ ok: true, runtime_verified: false, logs: [{ name: "Foreign game", path: "/foreign", exists: true, text: "private old game log", truncated: false }] }));
    assert.doesNotMatch(p.host.textContent ?? "", /private old game log/);
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

await test("bounded log display preserves backend startup and tail rather than keeping only its suffix", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  const backendText = "STARTUP: XeSS initialization failure\n" + "h".repeat(16384)
    + "\n[... truncated; startup above, tail below ...]\n" + "t".repeat(49152) + "\nLATEST: process exit";
  fixtures.get_xmx_logs = { ok: true, runtime_verified: false,
    logs: [{ name: "Proton", path: "/steam-42.log", exists: true, text: backendText, truncated: true }] };
  const p = await renderPanel();
  await openDetails(p.host);
  try {
    await press(p.host, "Read logs");
    assert.match(p.host.textContent ?? "", /STARTUP: XeSS initialization failure/);
    assert.match(p.host.textContent ?? "", /LATEST: process exit/);
    assert.match(p.host.textContent ?? "", /startup above, tail below/);
    assert.doesNotMatch(p.host.textContent ?? "", /last 32 KiB/);
    const rendered = Array.from(p.host.querySelectorAll("pre")).find(element => element.textContent?.includes("STARTUP:"))!;
    assert.ok(rendered.textContent!.length <= 66560, "the browser still enforces a finite per-file text budget");
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

await test("unknown and failed DLL inspection interlock prefix changes until an explicit successful status retry", async () => {
  const old = { ...fixtures };
  const initial = deferred<unknown>();
  const retry = deferred<unknown>();
  const planned: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = () => initial.promise;
  fixtures.plan_xmx = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "prefix", old_launch_options: args[2], desired_launch_options: "new", operations: [] }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "old" }, async () => {
    const p = await renderPanel();
    try {
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true, "DLL journal is not known yet");
      await press(p.host, "Save settings…");
      assert.deepEqual(planned, []);
      await act(async () => initial.resolve({ ok: false, error: "DLL journal unreadable" }));
      assert.match(p.host.textContent ?? "", /DLL journal unreadable/);
      await press(p.host, "Save settings…");
      assert.deepEqual(planned, [], "RPC failure is not an absent DLL journal");
      fixtures.get_xess_dll_status = () => retry.promise;
      await press(p.host, "Refresh DLL status");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true);
      await act(async () => retry.resolve(dllFixture));
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, false);
      await press(p.host, "Save settings…");
      assert.equal(planned.length, 1);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

function queuedPress(host: Element, text: string) {
  const button = Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).find(e => e.textContent === text);
  assert.ok(button, `button exists: ${text}`);
  const key = Object.keys(button).find(name => name.startsWith("__reactProps"))!;
  return (button as any)[key].onClick as () => void;
}

await test("retained profile callbacks edit live state but can never confirm a replacement preview", async () => {
  const old = { ...fixtures };
  let plans = 0;
  const applied: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  fixtures.plan_xmx = (...args: unknown[]) => ({ ok: true, plan_id: `profile-${++plans}`, old_launch_options: args[2], desired_launch_options: "new", operations: [] });
  fixtures.apply_xmx = (...args: unknown[]) => { applied.push(args); return { ok: false, error: "not expected" }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "old" }, async () => {
    const p = await renderPanel();
    try {
      const oldPreview = queuedPress(p.host, "Save settings…");
      const oldDiagnostics = fieldProps(p.host, "Debug logs").onChange;
      await choose(p.host, "Driver", "26.1");
      // M2.7: no revision gate. A retained preview button refuses LOUDLY when its
      // captured form no longer matches the live one; retained field callbacks
      // edit live state; applying replaced previews stays blocked by plan tokens.
      await act(async () => { oldPreview(); });
      assert.equal(plans, 0, "a stale captured form cannot plan");
      assert.match(p.host.textContent ?? "", /form changed while the panel updated/i);
      await act(async () => { oldDiagnostics(true); });
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, true, "a retained field callback edits live state");
      await press(p.host, "Save settings…");
      const oldConfirm = queuedPress(p.host, "Apply changes");
      await toggle(p.host, "Debug logs");
      await press(p.host, "Save settings…");
      assert.equal(plans, 2);
      await act(async () => oldConfirm());
      assert.deepEqual(applied, [], "a queued confirmation may not apply a different preview token (plan id is bound at dialog render)");
      assert.match(p.host.textContent ?? "", /Dry-run preview/);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("retained DLL callbacks preview the captured target but can never confirm a replacement token", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  const applied: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  fixtures.plan_xess_dll = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: `dll-${planned.length}`, old_launch_options: args[2], desired_launch_options: args[2], operations: [] };
  };
  fixtures.apply_xess_dll = (...args: unknown[]) => { applied.push(args); return { ok: false, error: "not expected" }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
    const p = await renderPanel();
    try {
      await choose(p.host, "Game DLL", "/game/a/libxess.dll");
      const stalePreview = queuedPress(p.host, "Update DLL…");
      const staleTarget = fieldProps(p.host, "Game DLL").onChange;
      await choose(p.host, "Game DLL", "/game/b/libxess.dll");
      // M2.7: no revision gate; a stale preview button refuses loudly because its
      // captured target differs from the live selection, and applying replaced
      // previews stays blocked by the plan-token check below.
      await act(async () => { stalePreview(); });
      assert.deepEqual(planned, [], "a stale captured DLL target cannot plan");
      assert.match(p.host.textContent ?? "", /selection changed while the panel updated/i);
      await act(async () => { staleTarget({ data: "/game/a/libxess.dll" }); });
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "/game/a/libxess.dll", "a retained field callback edits live state");
      await press(p.host, "Update DLL…");
      const staleConfirm = queuedPress(p.host, "Apply changes");
      await choose(p.host, "Game DLL", "/game/a/libxess.dll");
      await press(p.host, "Update DLL…");
      await act(async () => staleConfirm());
      assert.deepEqual(applied, []);
      assert.match(p.host.textContent ?? "", /DLL dry-run preview/);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("a rejected field change surfaces the reason instead of dying silently", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = { ok: false, error: "Prefix journal unavailable" };
  fixtures.get_xess_dll_status = dllFixture;
  const p = await renderPanel();
  try {
    const edit = fieldProps(p.host, "Debug logs").onChange;
    await act(async () => { edit(true); });
    assert.match(p.host.textContent ?? "", /ignored: the installation status is unavailable/i);
  } finally { await p.cleanup(); }
  Object.assign(fixtures, old);
});

await test("Driver onChange accepts a raw option value as device Dropdowns send it", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "raw-option", old_launch_options: args[2], desired_launch_options: "managed", profile: args[4], operations: [] };
  };
  await withLaunchClient({ GetAppLaunchOptions: () => "managed" }, async () => {
    const p = await renderPanel();
    try {
      await act(async () => { fieldProps(p.host, "Driver").onChange("26.1"); });
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "26.1", "raw string option coerced through dropOption");
      await press(p.host, "Save settings…");
      assert.equal(planned.length, 1);
      assert.equal((planned[0][4] as { driver?: string }).driver, "26.1");
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("unedited trial promotion uses backend recognition and displays the effective profile and pinned driver before confirmation", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  const recognized = { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false };
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = dllFixture;
  const trial = "MESA=26.1 INTC_ALT_DRIVER_EXTENSIONS_PATH='C:\\windows\\system32\\driverstore\\filerepository\\igd_faux.inf_1' PROTON_LOG=1 IGDEXT_TRACE=1 ANV_CM_DEBUG=1 wrapper %command%";
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "recognized-trial", old_launch_options: args[2], desired_launch_options: "wrapper %command%",
      profile: args[4] ?? recognized, selected_driver: "/package/lib/mesa-26.1/libvulkan_intel.so",
      operations: [{ target: "/prefix/config", before_sha256: "a".repeat(64), after_sha256: "b".repeat(64) }] };
  };
  await withLaunchClient({ GetAppLaunchOptions: () => trial }, async () => {
    const p = await renderPanel();
    await openDetails(p.host);
    try {
      const before = rpcCalls.length;
      await press(p.host, "Import tested setup…");
      assert.deepEqual(planned, [["42", "/package", trial, "adopt_trial"]], "do not silently override the recognized trial with the stored default profile");
      assert.match(p.host.textContent ?? "", /Recognized trial profile/);
      assert.match(p.host.textContent ?? "", /26.1.*alternate.*diagnostic logging off/i);
      assert.match(p.host.textContent ?? "", /\/package\/lib\/mesa-26.1\/libvulkan_intel.so/);
      assert.match(p.host.textContent ?? "", /original.*uninstall.*launch options.*preserv/i);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "26.1");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Intel detection fix").checked, true);
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, false);
      assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
      await toggle(p.host, "Debug logs");
      await press(p.host, "Import tested setup…");
      assert.deepEqual(planned.at(-1)?.[4], { ...recognized, diagnostics: true }, "an explicit edit overrides inferred choices");
      assert.match(p.host.textContent ?? "", /Selected promotion profile.*may differ from.*trial/i);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("malformed profile summaries and nonabsolute selected drivers never become confirmable", async () => {
  for (const summary of [
    { profile: { driver: "26.1", discovery: "alternate" } },
    { profile: { driver: "26.1", discovery: "alternate", diagnostics: "false" } },
    { profile: { driver: "26.9", discovery: "alternate", diagnostics: false } },
    { profile: { ...defaultProfile, unexpected: "arbitrary object" } },
    { profile: [defaultProfile] }, { profile: null },
    { selected_driver: { path: "/driver" } }, { selected_driver: "relative/driver.so" },
  ]) {
    const r = rig({ plan: async (_id: string, _dir: string, live: string) => ({ ok: true, plan_id: "bad-summary", old_launch_options: live,
      desired_launch_options: "new", operations: [], ...summary }) });
    await r.controller.preview("/package", "adopt_trial");
    assert.equal(r.controller.state.preview, null, JSON.stringify(summary));
    await r.controller.confirm();
    assert.equal(r.calls.includes("apply"), false);
  }
});

for (const action of ["configure", "adopt_trial"] as const) {
  await test(`${action} interrupted abort can continue restoring the prior profile but never resume the new one`, async () => {
    const r = rig({ recover: async () => { r.calls.push("recover"); return { ok: true }; } });
    r.controller.restorePending({ plan_id: "profile-abort", action, phase: "restoring", old_launch_options: "manual trial %command%", desired_launch_options: "managed %command%" });
    assert.equal(r.controller.state.pendingReady, false);
    assert.equal(r.controller.state.pendingAbortable, true);
    await r.controller.resume();
    assert.deepEqual(r.calls, []);
    r.setLive("manual trial %command%");
    await r.controller.abort();
    assert.deepEqual(r.calls, ["read", "recover"]);
    assert.equal(r.controller.state.success, true);
  });
}

await test("simultaneous unavailable scopes remain mutation-blocked but their read-only status retries cannot deadlock", async () => {
  const old = { ...fixtures };
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = { ok: false, error: "Prefix status disconnected" };
  fixtures.get_xess_dll_status = { ok: false, error: "DLL status disconnected" };
  const p = await renderPanel();
  try {
    const before = rpcCalls.length;
    assert.equal(buttonLabels(panelSection(p.host)).includes("Save settings…"), false);
    await press(p.host, "Enable XMX…");
    assert.equal(buttonLabels(panelSection(p.host, "dll")).includes("Download SDK"), false, "unknown cache readiness never offers a write");
    assert.deepEqual(rpcCalls.slice(before), []);
    fixtures.get_xmx_status = profileStatus;
    await press(p.host, "Refresh");
    assert.ok(rpcCalls.slice(before).includes("get_xmx_status"), "a read-only status retry must not require the other scope's recovery to be clear");
    assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true, "DLL state is still unknown");
    fixtures.get_xess_dll_status = dllFixture;
    await press(p.host, "Refresh DLL status");
    assert.ok(rpcCalls.slice(before).includes("get_xess_dll_status"));
    assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, false);
    assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, false);
    assert.equal(rpcCalls.slice(before).some(name => /^(apply_|finish_|recover_|plan_)/.test(name)), false);
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

for (const failure of ["refused", "rejected", "throws"] as const) {
  await test(`${failure} preference save cannot prevent prefix status recovery after initial inspection failure`, async () => {
    const old = { ...fixtures };
    const retry = deferred<unknown>();
    const requests: unknown[][] = [];
    const saved: unknown[][] = [];
    fixtures.get_pref = (key: string) => ({ key, value: "/package" });
    fixtures.get_xmx_status = (...args: unknown[]) => { requests.push(args); return { ok: false, error: "Initial prefix inspection failed" }; };
    fixtures.get_xess_dll_status = { ...dllFixture, sdk: { ...dllFixture.sdk, ready: false } };
    fixtures.set_pref = (...args: unknown[]) => {
      saved.push(args);
      if (failure === "refused") return { ok: false, error: "Preference storage unavailable" };
      if (failure === "rejected") return Promise.reject(new Error("Preference storage unavailable"));
      throw new Error("Preference storage unavailable");
    };
    const p = await renderPanel();
    try {
      assert.match(p.host.textContent ?? "", /Initial prefix inspection failed/);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true);
      fixtures.get_xmx_status = (...args: unknown[]) => { requests.push(args); return retry.promise; };
      const before = rpcCalls.length;
      await press(p.host, "Refresh");
      assert.deepEqual(saved, [["xmx_package_dir", "/package"]]);
      assert.deepEqual(requests, [["42", "/package"], ["42", "/package"]], "preference failure must not skip the read-only status retry");
      assert.match(p.host.textContent ?? "", /Preference storage unavailable/);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true, "no authorization before the fresh inspection completes");
      await press(p.host, "Download SDK");
      assert.equal(buttonLabels(panelSection(p.host)).includes("Save settings…"), false);
      await press(p.host, "Enable XMX…");
      assert.equal(rpcCalls.slice(before).some((name: string) => /^(prepare_|plan_|apply_|finish_|recover_)/.test(name)), false);
      await act(async () => retry.resolve(profileStatus));
      assert.match(p.host.textContent ?? "", /Enabled \(configured\)/);
      assert.doesNotMatch(p.host.textContent ?? "", /Initial prefix inspection failed/);
      assert.ok(Array.from(p.host.querySelectorAll('[role="alert"]')).some(element => element.textContent?.includes("Preference storage unavailable")), "successful status must retain a visible preference warning");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, false, "only fresh authoritative status clears the unknown prefix interlock");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, false);
      assert.equal(requests.length, 2, "exactly one inspection per explicit refresh");
      assert.equal(rpcCalls.slice(before).some((name: string) => /^(prepare_|plan_|apply_|finish_|recover_)/.test(name)), false);
    } finally { await p.cleanup(); Object.assign(fixtures, old); }
  });
}

for (const result of ["pending", "error"] as const) {
  await test(`preference warning cannot authorize stale recovery or bypass ${result} prefix interlocks`, async () => {
    const old = { ...fixtures };
    const retry = deferred<unknown>();
    const pending = { plan_id: "prefix-pending", action: "configure", phase: "pending_configure", old_launch_options: "old", desired_launch_options: "new" };
    fixtures.get_pref = (key: string) => ({ key, value: "/package" });
    fixtures.get_xmx_status = { ...profileStatus, phase: "pending_configure", pending };
    fixtures.get_xess_dll_status = { ...dllFixture, sdk: { ...dllFixture.sdk, ready: false } };
    fixtures.set_pref = { ok: false, error: "Preference storage unavailable" };
    const p = await renderPanel();
    try {
      const staleResume = queuedPress(p.host, "Resume pending transaction");
      fixtures.get_xmx_status = () => retry.promise;
      const before = rpcCalls.length;
      await press(p.host, "Refresh");
      assert.equal(rpcCalls.slice(before).filter((name: string) => name === "get_xmx_status").length, 1);
      await act(async () => staleResume());
      assert.equal(rpcCalls.slice(before).some((name: string) => /^(plan_|apply_|finish_|recover_)/.test(name)), false, "cached pending journal cannot authorize recovery during validation");
      await act(async () => retry.resolve(result === "pending" ? { ...profileStatus, phase: "pending_configure", pending }
        : { ok: false, error: "Fresh prefix inspection failed" }));
      assert.match(p.host.textContent ?? "", /Preference storage unavailable/);
      if (result === "error") {
        assert.match(p.host.textContent ?? "", /Fresh prefix inspection failed/);
        assert.match(p.host.textContent ?? "", /Installation status unavailable/);
        await press(p.host, "Resume pending transaction");
        await press(p.host, "Confirm original options and abort");
      }
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true);
      await press(p.host, "Download SDK");
      await press(p.host, result === "pending" ? "Save settings…" : "Enable XMX…");
      assert.equal(rpcCalls.slice(before).some((name: string) => /^(prepare_|plan_|apply_|finish_|recover_)/.test(name)), false, "pending/error inspection is not an absent journal");
      assert.equal(rpcCalls.slice(before).filter((name: string) => name === "get_xmx_status").length, 1);
    } finally { await p.cleanup(); Object.assign(fixtures, old); }
  });
}

await test("trial recognition is not blocked by an unavailable stored driver and invalidates callbacks from before inference", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = { ...profileStatus, profile: { driver: "26.3", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false } };
  fixtures.get_xess_dll_status = dllFixture;
  fixtures.plan_xmx = (...args: unknown[]) => {
    planned.push(args);
    return { ok: true, plan_id: "recovered-selector", old_launch_options: args[2], desired_launch_options: "managed",
      profile: { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false }, selected_driver: "/package/26.1/driver.so", operations: [] };
  };
  await withLaunchClient({ GetAppLaunchOptions: () => "manual trial" }, async () => {
    const p = await renderPanel();
    await openDetails(p.host);
    try {
      const beforeInference = fieldProps(p.host, "Debug logs").onChange;
      await press(p.host, "Import tested setup…");
      assert.equal(planned.length, 1, "backend recognition, not the old stored selector, chooses the unedited trial profile");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Driver").value, "26.1");
      assert.match(p.host.textContent ?? "", /Recognized trial profile/);
      await act(async () => beforeInference(true));
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, true, "field callbacks stay live after the import re-rendered the form");
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

for (const [label, prefixStatus] of [
  ["unknown", { ...profileStatus, phase: "future_unknown_phase" }],
  ["error", { ok: false, error: "Prefix journal unavailable" }],
  ["pending", { ...profileStatus, phase: "pending_configure", pending: { plan_id: "prefix-pending", action: "configure", phase: "pending_configure", old_launch_options: "old", desired_launch_options: "new" } }],
  ["missing journal", { ...profileStatus, pending: undefined }],
] as const) {
  await test(`${label} prefix state interlocks independent DLL mutations without requiring prefix installation`, async () => {
    const old = { ...fixtures };
    fixtures.get_xmx_status = prefixStatus;
    fixtures.get_xess_dll_status = { ...dllFixture, trial_available: true, sdk: { ...dllFixture.sdk, ready: false } };
    const p = await renderPanel();
    try {
      const before = rpcCalls.length;
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, true);
      await press(p.host, "Download SDK");
      await press(p.host, "Use existing DLL…");
      assert.equal(rpcCalls.slice(before).includes("prepare_xess_sdk"), false);
      assert.equal(rpcCalls.slice(before).includes("plan_xess_dll"), false);
      fixtures.get_xmx_status = statusFixture;
      await press(p.host, "Refresh");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").disabled, false, "known empty prefix journal authorizes independent DLL setup even without support installed");
    } finally { await p.cleanup(); Object.assign(fixtures, old); }
  });
}

for (const [label, dllStatus] of [
  ["unknown", { ...dllFixture, phase: "future_unknown_phase" }],
  ["error", { ok: false, error: "DLL journal unavailable" }],
  ["pending", { ...dllFixture, phase: "pending_enable", pending: { plan_id: "dll-pending", action: "enable", phase: "pending_enable", old_launch_options: "same", desired_launch_options: "same" } }],
  ["missing journal", { ...dllFixture, pending: undefined }],
  ["malformed journal", { ...dllFixture, phase: "recovery_required", pending: { plan_id: "invalid" } }],
] as const) {
  await test(`${label} DLL state interlocks profile mutations until an explicit authoritative status retry`, async () => {
    const old = { ...fixtures };
    fixtures.get_pref = (key: string) => ({ key, value: "/package" });
    fixtures.get_xmx_status = profileStatus;
    fixtures.get_xess_dll_status = dllStatus;
    const p = await renderPanel();
    try {
      const before = rpcCalls.length;
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, true);
      await press(p.host, "Save settings…");
      await press(p.host, "Disable XMX…");
      assert.equal(rpcCalls.slice(before).includes("plan_xmx"), false);
      fixtures.get_xess_dll_status = dllFixture;
      await press(p.host, "Refresh DLL status");
      assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").disabled, false);
    } finally { await p.cleanup(); Object.assign(fixtures, old); }
  });
}

await test("DLL adoption and restore do not require installed prefix support and never dispatch Steam options", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  const finished: unknown[][] = [];
  let subscriptions = 0;
  let unsubscriptions = 0;
  let action = "enable";
  fixtures.get_xmx_status = { ...statusFixture, gpu: { xmx_capable: false }, native_xess: { found: true } };
  fixtures.get_xess_dll_status = { ...dllFixture, trial_available: true };
  fixtures.plan_xess_dll = (...args: unknown[]) => {
    planned.push(args);
    action = args[3] as string;
    return { ok: true, plan_id: "independent-dll", old_launch_options: args[2], desired_launch_options: args[2], operations: [args[4] ? "adopt recognized trial" : "restore original DLL"] };
  };
  fixtures.apply_xess_dll = () => ({ ok: true, pending_launch: true, plan_id: "independent-dll", action, phase: `pending_${action}`, old_launch_options: "  exact raw %command%  ", desired_launch_options: "  exact raw %command%  " });
  fixtures.finish_xess_dll = (...args: unknown[]) => {
    finished.push(args);
    fixtures.get_xess_dll_status = { ...dllFixture, installed: action === "enable", phase: action === "enable" ? "enabled" : "disabled" };
    return { ok: true };
  };
  await withLaunchClient({
    RegisterForAppDetails: (appid: number, receive: (details: unknown) => void) => {
      subscriptions++;
      queueMicrotask(() => receive({ unAppID: appid, strLaunchOptions: "  exact raw %command%  " }));
      return { unregister: () => { unsubscriptions++; } };
    },
    SetAppLaunchOptions: () => { throw new Error("DLL scope must never dispatch Steam options"); },
  }, async () => {
    const before = rpcCalls.length;
    const p = await renderPanel();
    await openDetails(p.host, "dll");
    try {
      assert.match(p.host.textContent ?? "", /Pinned official SDK: v3.0.2; library version 2.0.2.68/);
      await choose(p.host, "Game DLL", "/game/a/libxess.dll");
      await press(p.host, "Use existing DLL…");
      assert.equal(planned.at(-1)?.[4], true);
      assert.deepEqual(finished, []);
      await press(p.host, "Apply changes");
      await press(p.host, "Restore original DLL…");
      assert.deepEqual(planned.at(-1), ["42", "/game/a/libxess.dll", "  exact raw %command%  ", "disable", false]);
      await press(p.host, "Apply changes");
      assert.equal(finished.length, 2);
      assert.deepEqual(finished.at(-1), ["42", "independent-dll", "  exact raw %command%  "]);
      assert.equal(subscriptions, 6, "both plans read cold live options, then re-read before apply and finish");
      assert.equal(unsubscriptions, subscriptions);
      assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
      assert.equal(rpcCalls.slice(before).includes("get_launch_options"), false);
      assert.match(p.host.textContent ?? "", /No managed DLL update/);
      assert.match(p.host.textContent ?? "", /Runtime unverified/);
    } finally { await p.cleanup(); }
  }, { unAppID: 42, strLaunchOptions: "untrusted stale cache" });
  Object.assign(fixtures, old);
});

for (const stage of ["plan", "apply", "sdk", "status"] as const) {
  await test(`DLL ${stage} response cannot cross a game switch or trigger stale continuation`, async () => {
    const old = { ...fixtures };
    const response = deferred<any>();
    const statusRequests: string[] = [];
    fixtures.get_xmx_status = profileStatus;
    fixtures.get_xess_dll_status = (appid: string) => { statusRequests.push(appid); return stage === "status" ? appid === "42" ? response.promise
      : { ...dllFixture, candidates: [{ ...dllFixture.candidates[0], path: "/other-game/libxess.dll" }] }
      : stage === "sdk" ? { ...dllFixture, sdk: { ...dllFixture.sdk, ready: false } } : dllFixture; };
    fixtures.plan_xess_dll = (...args: unknown[]) => stage === "plan" ? response.promise : { ok: true, plan_id: "old-dll", old_launch_options: args[2], desired_launch_options: args[2], operations: [] };
    fixtures.apply_xess_dll = () => response.promise;
    fixtures.prepare_xess_sdk = () => response.promise;
    await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
      const before = rpcCalls.length;
      const p = await renderPanel();
      try {
        if (stage !== "status") await choose(p.host, "Game DLL", "/game/a/libxess.dll");
        if (stage === "sdk") await press(p.host, "Download SDK");
        if (stage === "plan" || stage === "apply") await press(p.host, "Update DLL…");
        if (stage === "apply") await press(p.host, "Apply changes");
        await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
        await act(async () => response.resolve(stage === "status" ? { ...dllFixture, phase: "recovery_required", pending: { plan_id: "foreign" } }
          : stage === "plan" ? { ok: true, plan_id: "old-dll", old_launch_options: "same", desired_launch_options: "same", operations: ["OLD GAME PREVIEW"] }
          : { ok: true, pending_launch: true, plan_id: "old-dll", action: "enable", phase: "pending_enable", old_launch_options: "same", desired_launch_options: "same" }));
        assert.doesNotMatch(p.host.textContent ?? "", /OLD GAME PREVIEW|Pending DLL recovery/);
        if (stage === "status") assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "/other-game/libxess.dll", "late inspection cannot replace the new game's sole selected candidate");
        assert.equal(rpcCalls.slice(before).includes("finish_xess_dll"), false);
        assert.equal(statusRequests.filter(id => id === "42").length, 1, "a late SDK response cannot refresh the old game");
      } finally { await p.cleanup(); }
    });
    Object.assign(fixtures, old);
  });
}

await test("unavailable profile status refuses even programmatic disabled-control callbacks", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = { ok: false, error: "Cannot inspect the prefix journal" };
  fixtures.get_xess_dll_status = dllFixture;
  const p = await renderPanel();
  try {
    await act(async () => fieldProps(p.host, "Debug logs").onChange(true));
    assert.equal(nativeControl<HTMLInputElement>(p.host, "Debug logs").checked, false);
    await act(async () => fieldProps(p.host, "Intel detection fix").onChange(true));
    assert.equal(nativeControl<HTMLInputElement>(p.host, "Intel detection fix").checked, false);
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

function panelSection(host: Element, scope: "prefix" | "dll" = "prefix") {
  const title = scope === "prefix" ? "XeSS XMX" : "XeSS DLL";
  const section = Array.from(host.querySelectorAll('[data-mock="PanelSection"]')).find(element => element.querySelector('[data-title]')?.textContent?.startsWith(title));
  assert.ok(section, `${scope} section exists`);
  return section!;
}
async function openDetails(host: Element, scope: "prefix" | "dll" = "prefix") {
  const section = panelSection(host, scope);
  if (Array.from(section.querySelectorAll('[data-mock="ButtonItem"]')).some(button => button.textContent === "Details")) await press(section, "Details");
}
function buttonLabels(host: Element) {
  return Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).map(button => button.textContent);
}

await test("compact prefix defaults expose only settings and relevant preview actions with controller-accessible audit details", async () => {
  const old = { ...fixtures };
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = { ...profileStatus, package: { valid: true, path: "/package", errors: [] } };
  fixtures.plan_xmx = (...args: unknown[]) => ({ ok: true, plan_id: "compact", old_launch_options: args[2], desired_launch_options: "new %command%",
    profile: { driver: "26.1", discovery: "alternate", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false }, selected_driver: "/package/driver.so",
    operations: [{ target: "/prefix/private", before_sha256: "a".repeat(64), after_sha256: "b".repeat(64) }] });
  await withLaunchClient({ GetAppLaunchOptions: () => "old %command%" }, async () => {
    const p = await renderPanel();
    try {
      const section = panelSection(p.host);
      assert.deepEqual(buttonLabels(section), ["Update package", "Save settings…", "Disable XMX…", "Refresh", "Details"]);
      for (const label of ["Driver", "Intel detection fix", "Debug logs"]) assert.ok(section.querySelector(`[aria-label="${label}"]`));
      assert.match(section.textContent ?? "", /Enabled.*configured/);
      assert.match(section.textContent ?? "", /Runtime unverified/);
      assert.equal(section.querySelectorAll("pre").length, 0);
      assert.equal(section.querySelector('input[aria-label="Package folder"]'), null);
      assert.doesNotMatch(section.textContent ?? "", /GPU static readiness|Flatpak visibility|Import tested setup|Read logs/);
      const before = rpcCalls.length;
      await press(section, "Save settings…");
      assert.match(section.textContent ?? "", /Save settings.*26.1/);
      assert.equal(section.querySelectorAll("pre").length, 0, "preview is concise until audit details are requested");
      assert.equal(buttonLabels(section).filter(label => label === "Apply changes").length, 1);
      assert.equal(rpcCalls.slice(before).includes("apply_xmx"), false);
      await openDetails(p.host);
      assert.ok(section.querySelector('input[aria-label="Package folder"]'));
      assert.match(section.textContent ?? "", /GPU static readiness|Flatpak visibility/);
      assert.match(section.textContent ?? "", /old %command%/);
      assert.match(section.textContent ?? "", /new %command%/);
      assert.match(section.textContent ?? "", /\/prefix\/private/);
      assert.match(section.textContent ?? "", new RegExp("a".repeat(64)));
      assert.match(section.textContent ?? "", /\/package\/driver.so/);
      assert.ok(buttonLabels(section).includes("Import tested setup…"));
      assert.ok(buttonLabels(section).includes("Read logs"));
      await press(section, "Hide details");
      assert.equal(section.querySelectorAll("pre").length, 0);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("compact prefix readiness failures stay visible and actionable without expanding diagnostics", async () => {
  const old = { ...fixtures };
  fixtures.get_pref = (key: string) => ({ key, value: "/package" });
  fixtures.get_xmx_status = { ...profileStatus, package: { valid: true, path: "/package", errors: [] },
    flatpak: { visible: false, reason: "Package is outside Steam sandbox access" } };
  const p = await renderPanel();
  try {
    const section = panelSection(p.host);
    assert.equal(section.querySelector('input[aria-label="Package folder"]'), null);
    assert.match(section.textContent ?? "", /outside Steam sandbox access/);
    assert.match(section.textContent ?? "", /Details.*Flatpak visibility.*Refresh/);
    assert.ok(Array.from(section.querySelectorAll('[role="alert"]')).some(element => element.textContent?.includes("sandbox")));
    await openDetails(p.host);
    assert.match(section.textContent ?? "", /Flatpak visibility: not visible/);
    await press(section, "Hide details");
    assert.match(section.textContent ?? "", /outside Steam sandbox access/);
    assert.ok(buttonLabels(section).includes("Refresh"));
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

for (const kind of ["update", "trial", "managed", "current"] as const) {
  await test(`compact DLL ${kind} surface offers only its relevant action and keeps audit data in Details`, async () => {
    const old = { ...fixtures };
    fixtures.get_xmx_status = profileStatus;
    fixtures.get_xess_dll_status = { ...dllFixture, installed: kind === "managed", phase: kind === "managed" ? "enabled" : "disabled",
      sdk: { ...dllFixture.sdk, ready: kind !== "managed" },
      trial_available: kind === "trial", candidates: [{ ...dllFixture.candidates[0], version: kind === "update" ? "1.3.0" : dllFixture.sdk.version }] };
    const planned: unknown[][] = [];
    fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "compact-dll", old_launch_options: args[2], desired_launch_options: args[2],
      operations: [{ target: args[1], before_sha256: "a".repeat(64), after_sha256: "b".repeat(64) }] }; };
    await withLaunchClient({ GetAppLaunchOptions: () => "unchanged %command%" }, async () => {
      const p = await renderPanel();
      try {
        const section = Array.from(p.host.querySelectorAll('[data-mock="PanelSection"]')).at(-1)!;
        const primary = kind === "managed" ? "Restore original DLL…" : kind === "trial" ? "Use existing DLL…" : kind === "update" ? "Update DLL…" : null;
        assert.deepEqual(buttonLabels(section), [...(primary ? [primary] : []), "Refresh DLL status", "Details"]);
        assert.match(section.textContent ?? "", /Game DLL:.*(1.3.0|2.0.2.68)/);
        assert.match(section.textContent ?? "", /Backup:/);
        assert.match(section.textContent ?? "", /disabling XMX.*DLL/i);
        assert.equal(section.querySelectorAll("pre").length, 0);
        assert.doesNotMatch(section.textContent ?? "", /\/game\/a\/libxess.dll|aaaaaaaaaaaaaaaa/);
        const before = rpcCalls.length;
        if (primary) {
          await press(section, primary);
          assert.deepEqual(planned, [["42", "/game/a/libxess.dll", "unchanged %command%", kind === "managed" ? "disable" : "enable", kind === "trial"]]);
          assert.equal(buttonLabels(section).filter(label => label === "Apply changes").length, 1);
          assert.equal(section.querySelectorAll("pre").length, 0);
          assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
        } else {
          assert.match(section.textContent ?? "", /Current version.*unmanaged/i);
          assert.doesNotMatch(section.textContent ?? "", /hash.identical|hash.verified/i);
        }
        await openDetails(p.host, "dll");
        assert.match(section.textContent ?? "", /\/game\/a\/libxess.dll/);
        assert.match(section.textContent ?? "", new RegExp("a".repeat(64)));
        assert.match(section.textContent ?? "", /Pinned official SDK/);
        if (primary) assert.match(section.textContent ?? "", /unchanged %command%/);
        if (kind === "current") {
          assert.match(section.textContent ?? "", /version.*not.*hash|hash.*not.*version/i);
          await press(section, "Replace DLL…");
          assert.equal(planned.length, 1, "advanced replacement is an explicit preview, not an automatic refusal-prone default");
          assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
        }
        assert.equal(buttonLabels(section).includes("Download SDK"), false, "ready cache never offers a redundant download");
      } finally { await p.cleanup(); }
    });
    Object.assign(fixtures, old);
  });
}

await test("fresh single valid DLL is preselected for preview only and intentional clearing survives refresh", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, trial_available: false, candidates: [dllFixture.candidates[0],
    { path: "/game/unsupported.dll", version: null, architecture: null, sha256: null, error: "Unsupported DLL" }] };
  fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "single", old_launch_options: args[2], desired_launch_options: args[2], operations: [] }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "same", SetAppLaunchOptions: () => { throw new Error("Selection cannot write Steam options"); } }, async () => {
    const before = rpcCalls.length;
    const p = await renderPanel();
    try {
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "/game/a/libxess.dll");
      assert.deepEqual(planned, []);
      assert.equal(rpcCalls.slice(before).some((name: string) => /^(apply_|finish_|recover_)/.test(name)), false);
      await press(p.host, "Update DLL…");
      assert.deepEqual(planned, [["42", "/game/a/libxess.dll", "same", "enable", false]]);
      assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
      await choose(p.host, "Game DLL", "");
      await press(p.host, "Refresh DLL status");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "", "an explicit clear is not first-visit hydration");
      await press(p.host, "Update DLL…");
      assert.equal(planned.length, 1);
      await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "/game/a/libxess.dll", "a new game gets independent first-visit selection");
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

await test("DLL refresh clears automatic selection when a sole candidate becomes ambiguous", async () => {
  const old = { ...fixtures };
  const planned: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, candidates: [dllFixture.candidates[0]] };
  fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "chosen-dll", old_launch_options: args[2], desired_launch_options: args[2], operations: [] }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
    const before = rpcCalls.length;
    const p = await renderPanel();
    try {
      const section = panelSection(p.host, "dll");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "/game/a/libxess.dll");
      const stalePreview = queuedPress(section, "Update DLL…");
      fixtures.get_xess_dll_status = dllFixture;
      await press(section, "Refresh DLL status");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "", "an automatic choice is not deliberate selection among multiple candidates");
      assert.equal(Array.from(section.querySelectorAll('[data-mock="ButtonItem"]')).find(button => button.textContent === "Update DLL…")?.getAttribute("data-disabled"), "true");
      assert.doesNotMatch(section.textContent ?? "", /DLL dry-run preview/);
      await press(section, "Update DLL…");
      await act(async () => stalePreview());
      assert.deepEqual(planned, []);
      assert.equal(rpcCalls.slice(before).includes("plan_xess_dll"), false, "refresh cannot authorize a target the user did not choose");
      await choose(p.host, "Game DLL", "/game/b/libxess.dll");
      await press(section, "Update DLL…");
      assert.deepEqual(planned, [["42", "/game/b/libxess.dll", "same", "enable", false]]);
      assert.match(section.textContent ?? "", /DLL dry-run preview/);
      assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

for (const choice of ["/game/a/libxess.dll", ""]) {
  await test(`DLL refresh preserves ${choice ? "a valid explicit selection" : "an intentional clear"} when candidates become multiple`, async () => {
    const old = { ...fixtures };
    const planned: unknown[][] = [];
    fixtures.get_xmx_status = profileStatus;
    fixtures.get_xess_dll_status = { ...dllFixture, candidates: [dllFixture.candidates[0]] };
    fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "deliberate-dll", old_launch_options: args[2], desired_launch_options: args[2], operations: [] }; };
    await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
      const p = await renderPanel();
      try {
        await choose(p.host, "Game DLL", choice);
        fixtures.get_xess_dll_status = dllFixture;
        await press(p.host, "Refresh DLL status");
        assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, choice);
        assert.deepEqual(planned, [], "retaining deliberate input is read-only");
        fixtures.get_xess_dll_status = { ...dllFixture, candidates: [dllFixture.candidates[0]] };
        await press(p.host, "Refresh DLL status");
        assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, choice, "a later sole candidate must not undo an intentional clear");
        await press(p.host, "Update DLL…");
        assert.deepEqual(planned, choice ? [["42", choice, "same", "enable", false]] : []);
      } finally { await p.cleanup(); }
    });
    Object.assign(fixtures, old);
  });
}

await test("DLL selector distinguishes multiple candidates sharing the same path suffix", async () => {
  const old = { ...fixtures };
  const paths = ["/game/a/Win64/libxess.dll", "/game/b/Win64/libxess.dll"];
  const planned: unknown[][] = [];
  fixtures.get_xmx_status = profileStatus;
  fixtures.get_xess_dll_status = { ...dllFixture, candidates: dllFixture.candidates.map((candidate, index) => ({ ...candidate, path: paths[index] })) };
  fixtures.plan_xess_dll = (...args: unknown[]) => { planned.push(args); return { ok: true, plan_id: "distinct-dll", old_launch_options: args[2], desired_launch_options: args[2], operations: [] }; };
  await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
    const before = rpcCalls.length;
    const p = await renderPanel();
    try {
      const options = Array.from(nativeControl<HTMLSelectElement>(p.host, "Game DLL").options).filter(option => option.value);
      assert.deepEqual(options.map(option => option.value), paths, "candidate data paths remain exact");
      assert.deepEqual(options.map(option => option.textContent), paths, "multiple candidates show full paths, not indistinguishable suffixes");
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, "");
      assert.deepEqual(planned, []);
      await choose(p.host, "Game DLL", paths[1]);
      assert.equal(nativeControl<HTMLSelectElement>(p.host, "Game DLL").value, paths[1]);
      await press(p.host, "Update DLL…");
      assert.deepEqual(planned, [["42", paths[1], "same", "enable", false]]);
      assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
    } finally { await p.cleanup(); }
  });
  Object.assign(fixtures, old);
});

for (const malicious of [false, true]) {
  await test(`DLL preview ${malicious ? "rejects successful Steam-option changes" : "preserves backend refusal without desired options"}`, async () => {
    const old = { ...fixtures };
    fixtures.get_xmx_status = profileStatus;
    fixtures.get_xess_dll_status = dllFixture;
    fixtures.plan_xess_dll = (...args: unknown[]) => malicious
      ? { ok: true, plan_id: "malicious", old_launch_options: args[2], desired_launch_options: "changed Steam options", operations: [] }
      : { ok: false, error: "Original DLL backup conflicts; preserve files and inspect backup." };
    await withLaunchClient({ GetAppLaunchOptions: () => "same" }, async () => {
      const p = await renderPanel();
      try {
        const before = rpcCalls.length;
        await choose(p.host, "Game DLL", "/game/a/libxess.dll");
        await press(p.host, "Update DLL…");
        assert.match(p.host.textContent ?? "", malicious ? /DLL preview attempted to change launch options/ : /Original DLL backup conflicts; preserve files and inspect backup/);
        assert.doesNotMatch(p.host.textContent ?? "", /DLL dry-run preview/);
        assert.equal(rpcCalls.slice(before).includes("apply_xess_dll"), false);
        if (!malicious) assert.doesNotMatch(p.host.textContent ?? "", /attempted to change launch options/);
      } finally { await p.cleanup(); }
    });
    Object.assign(fixtures, old);
  });
}

await test("nuke stays reachable when the installation status is unavailable", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = { ok: false, error: "Exactly one existing Steam prefix is required" };
  fixtures.nuke_plan_xmx = { ok: true, plan_id: "b".repeat(32), legacy: false, files: [
    { target: "/state/apps/42/xess.conf", action: "remove" }], warnings: [], desired_launch_options: "" };
  const p = await renderPanel();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.doesNotMatch(p.host.textContent ?? "", /Nuke blocked/);
    assert.match(p.host.textContent ?? "", /delete \/state\/apps\/42\/xess\.conf/);
    assert.ok(Array.from(p.host.querySelectorAll('[data-mock="ButtonItem"]'))
      .some(el => el.textContent === "Apply nuke (1 file)"), "apply button shown");
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

await test("nuke stays reachable while a pending transaction needs recovery", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = { ...statusFixture, pending: { action: "enable", plan_id: "c".repeat(32),
    phase: "pending_enable", old_launch_options: "", desired_launch_options: "XMX=1 %command%" } };
  fixtures.nuke_plan_xmx = { ok: true, plan_id: "d".repeat(32), legacy: false, files: [], warnings: [
    "A pending transaction was found; nuke supersedes it."], desired_launch_options: "" };
  const p = await renderPanel();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.doesNotMatch(p.host.textContent ?? "", /Nuke blocked/);
    assert.match(p.host.textContent ?? "", /nuke supersedes/);
  } finally { await p.cleanup(); Object.assign(fixtures, old); }
});

await test("nuke still refuses while the game is running", async () => {
  const old = { ...fixtures };
  fixtures.get_xmx_status = { ok: false, error: "Exactly one existing Steam prefix is required" };
  (Router as any).MainRunningApp = { appid: 42 };
  const p = await renderPanel();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /Nuke blocked: stop this game first/);
  } finally { (Router as any).MainRunningApp = undefined; await p.cleanup(); Object.assign(fixtures, old); }
});

console.log(`XeSS XMX: ${checks} checks passed`);
