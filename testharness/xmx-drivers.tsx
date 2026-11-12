import assert from "node:assert/strict";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fixtures, calls } from "@decky/api";
import { Router } from "@decky/ui";
import { isXmxPlan, isXmxProfile } from "../src/xmx";
import { isXmxStatus } from "../src/xmxApi";
import { XmxPanel } from "../src/components/XmxPanel";

const profile = { driver: "default", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false };
const status = { ok: true, installed: false, legacy: false, phase: "disabled", pending: null, runtime_verified: false,
  package: { valid: true, path: "/home/player/package", errors: [] }, profile,
  drivers: [{ id: "default", label: "Upstream default", valid: true }] };
let checks = 0;
function check(name: string, run: () => void) { run(); checks++; console.log(`PASS ${name}`); }
for (const driver of ["default", "26.1", "27.0", "27.1.2", "27.0-devel", "stock-26.3", "stock-27.1.2-rc1"]) {
  check(`safe dynamic driver ${driver} validates in profiles, status and plans`, () => {
    const selected = { ...profile, driver };
    assert.equal(isXmxProfile(selected), true);
    assert.equal(isXmxStatus({ ...status, profile: selected, drivers: [{ id: driver, label: driver, valid: true }] }), true);
    assert.equal(isXmxPlan({ ok: true, plan_id: "dynamic", old_launch_options: "", desired_launch_options: "%command%",
      operations: [], profile: selected }), true);
  });
}
for (const driver of ["", "stock", "stock-default", "26", "../../26.3", "/tmp/26.3", "stock-../26.3", "stock-26.3/../../bad",
  "26.3;touch", "26.3\n", "26.3\u0000", " 26.3", "26.3 ", "27.0-$(id)", "12345.0", "27.0-" + "a".repeat(33), null, 26.3]) {
  check(`unsafe driver ${JSON.stringify(driver)} is rejected before any choice is offered`, () => {
    assert.equal(isXmxProfile({ ...profile, driver }), false);
    assert.equal(isXmxStatus({ ...status, drivers: [{ id: driver, label: "unsafe", valid: true }] }), false);
    assert.equal(isXmxPlan({ ok: true, plan_id: "unsafe", old_launch_options: "", desired_launch_options: "%command%",
      operations: [], profile: { ...profile, driver } }), false);
  });
}

Object.assign(fixtures, {
  get_pref: (key: string) => ({ key, value: "/home/player/package" }), set_pref: { ok: true },
  get_xmx_package_status: { ok: true, state: "idle", package: null, latest: null, pinned: "v20261104" },
  check_xmx_package_upstream: { ok: true, available: false, latest_tag: null, pinned: "v20261104", error: null },
  get_xmx_status: { ...status, drivers: [...status.drivers,
    { id: "27.0", label: "Patched Mesa 27.0", valid: true },
    { id: "stock-27.0", label: "Stock Mesa 27.0 + layer (explicit)", valid: true }] },
  get_xess_dll_status: { ok: true, installed: false, phase: "disabled", pending: null, trial_available: false,
    sdk: { tag: "sdk", version: "1", ready: false }, candidates: [] },
});
(Router as any).MainRunningApp = undefined;
(globalThis as any).SteamClient = { Apps: {
  GetAppLaunchOptions: () => "before %command%",
  SetAppLaunchOptions: () => { throw new Error("A driver preview must not change launch options"); },
} };
const planned: unknown[][] = [];
fixtures.plan_xmx = (...args: unknown[]) => {
  planned.push(args);
  return { ok: true, plan_id: "selected-driver", old_launch_options: args[2], desired_launch_options: "after %command%",
    operations: [], profile: args[4] };
};
const host = document.createElement("div");
document.body.appendChild(host);
const root = createRoot(host);
try {
  await act(async () => root.render(<XmxPanel appid="42" running={false} />));
  const control = () => host.querySelector<HTMLSelectElement>('select[aria-label="Driver"]')!;
  assert.equal(control().disabled, false);
  assert.equal(control().value, "default", "stock+layer is never automatically selected");
  assert.equal(Array.from(control().options).some(option => option.value === "stock-27.0"), true);
  await act(async () => {
    control().value = "stock-27.0";
    control().dispatchEvent(new window.Event("change", { bubbles: true }));
  });
  assert.equal(control().value, "stock-27.0");
  const enable = Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).find(button => button.textContent === "Enable XMX…")!;
  await act(async () => enable.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  assert.equal((planned.at(-1)?.[4] as any)?.driver, "stock-27.0");
  assert.match(host.textContent ?? "", /Dry-run preview/);
  assert.equal(calls.some((name: string) => /^(apply_|finish_|recover_)/.test(name)), false);
  checks++;
  console.log("PASS dynamic driver dropdown preserves default and previews only an explicit stock+layer selection");
} finally {
  await act(async () => root.unmount());
  host.remove();
}
console.log(`XeSS drivers: ${checks} checks passed`);
