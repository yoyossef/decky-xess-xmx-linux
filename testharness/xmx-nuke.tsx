import assert from "node:assert/strict";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fixtures, calls } from "@decky/api";
import { Router } from "@decky/ui";
import { NukeSection } from "../src/components/NukeSection";
import type { XmxStatus } from "../src/xmxApi";

const baseStatus = {
  ok: true, installed: true, legacy: false, phase: "enabled", runtime_verified: false,
  pending: null,
} as unknown as XmxStatus;
const plan = {
  ok: true, plan_id: "a".repeat(32), legacy: false,
  files: [
    { target: "/prefix/system32/ControlLib.dll", action: "restore" },
    { target: "/state/apps/42/launch.sh", action: "remove" },
    { target: "/prefix/system32/someone-elses.dll", action: "leave", reason: "content changed" },
  ],
  warnings: ["Backup for X was verified."],
  desired_launch_options: "INTEL_JAY=all",
};

let checks = 0;
async function test(name: string, run: () => void | Promise<void>) {
  Object.keys(fixtures).forEach(key => delete fixtures[key]);
  Object.assign(fixtures, { nuke_plan_xmx: plan, nuke_apply_xmx: { ok: true }, nuke_finish_xmx: { ok: true } });
  (globalThis as any).SteamClient = undefined;
  (Router as any).MainRunningApp = undefined;
  calls.length = 0;
  await run();
  checks++;
  console.log(`PASS ${name}`);
}
async function render(props: Partial<{ running: boolean; blocked: () => string | null }> = {}) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const refreshed: string[] = [];
  await act(async () => {
    root.render(<NukeSection appid="42" running={props.running ?? false} status={baseStatus}
      blocked={props.blocked ?? (() => null)} busyRef={{ current: false }}
      refresh={() => refreshed.push("refresh")} />);
  });
  return { host, refreshed, cleanup: async () => { await act(async () => root.unmount()); host.remove(); } };
}
function button(host: Element, text: string) {
  const found = Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).find(element => element.textContent === text);
  assert.ok(found, `button exists: ${text} — have: ${Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).map(e => JSON.stringify(e.textContent)).join(", ")}`);
  return found;
}
async function press(host: Element, text: string) {
  await act(async () => button(host, text).dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
}
function hasButton(host: Element, text: string) {
  return Array.from(host.querySelectorAll('[data-mock="ButtonItem"]')).some(el => el.textContent === text);
}

await test("arm shows a preview with actions and warnings, nothing dispatched yet", async () => {
  const p = await render();
  try {
    assert.ok(hasButton(p.host, "Nuke this game's XMX install…"));
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /delete \/state\/apps\/42\/launch\.sh/);
    assert.match(p.host.textContent ?? "", /restore \/prefix\/system32\/ControlLib\.dll/);
    assert.match(p.host.textContent ?? "", /leave \/prefix\/system32\/someone-elses\.dll \(content changed\)/);
    assert.ok(hasButton(p.host, "Apply nuke (3 files)"));
    assert.equal(calls.includes("nuke_apply_xmx"), false, "preview applies nothing");
  } finally { await p.cleanup(); }
});

await test("cancel returns to idle without applying", async () => {
  const p = await render();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    await press(p.host, "Cancel");
    assert.ok(hasButton(p.host, "Nuke this game's XMX install…"));
    assert.equal(calls.includes("nuke_apply_xmx"), false);
  } finally { await p.cleanup(); }
});

await test("apply dispatches launch options, verifies readback, finishes and refreshes", async () => {
  let live = "wrapper %command%";
  const writes: unknown[][] = [];
  (globalThis as any).SteamClient = {
    Apps: {
      GetAppLaunchOptions: async () => live,
      SetAppLaunchOptions: (...args: unknown[]) => { writes.push(args); live = String(args[1]); },
    },
  };
  const p = await render();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    await press(p.host, "Apply nuke (3 files)");
    assert.deepEqual(writes, [[42, "INTEL_JAY=all"]]);
    assert.equal(calls.includes("nuke_finish_xmx"), true);
    assert.match(p.host.textContent ?? "", /Nuke complete/);
    assert.deepEqual(p.refreshed, ["refresh"]);
    assert.equal(live, "INTEL_JAY=all");
  } finally { await p.cleanup(); }
});

await test("a Steam readback mismatch stops before finish", async () => {
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => "stale %command%" } };
  const p = await render();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    await press(p.host, "Apply nuke (3 files)");
    assert.equal(calls.includes("nuke_finish_xmx"), false, "finish requires exact readback");
    assert.match(p.host.textContent ?? "", /Steam returned "stale %command%"/);
  } finally { await p.cleanup(); }
});

await test("pending nuke journal offers Finish without re-arming", async () => {
  let live = "INTEL_JAY=all";
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => live } };
  fixtures.nuke_plan_xmx = plan;
  const status = {
    ...baseStatus, phase: "pending_nuke",
    pending: { plan_id: plan.plan_id, action: "nuke", phase: "pending_nuke",
               old_launch_options: "INTEL_JAY=all", desired_launch_options: "INTEL_JAY=all" },
  } as unknown as XmxStatus;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const refreshed: string[] = [];
  await act(async () => {
    root.render(<NukeSection appid="42" running={false} status={status} blocked={() => null}
      busyRef={{ current: false }} refresh={() => refreshed.push("refresh")} />);
  });
  try {
    assert.ok(hasButton(host, "Finish nuke"));
    assert.equal(hasButton(host, "Nuke this game's XMX install…"), false);
    await press(host, "Finish nuke");
    assert.equal(calls.includes("nuke_finish_xmx"), true);
    assert.match(host.textContent ?? "", /Nuke complete/);
  } finally { await act(async () => root.unmount()); host.remove(); void live; }
});

await test("running game refuses to arm or resume", async () => {
  const p = await render({ running: true });
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /Nuke blocked: stop this game first/);
    assert.equal(calls.includes("nuke_plan_xmx"), false);
  } finally { await p.cleanup(); }
});

await test("parent interlock blocks the nuke flow", async () => {
  const p = await render({ blocked: () => "another section owns this game right now" });
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /Nuke blocked: another section owns this game right now/);
    assert.equal(calls.includes("nuke_plan_xmx"), false);
  } finally { await p.cleanup(); }
});

await test("preview failure surfaces the backend error", async () => {
  fixtures.nuke_plan_xmx = { ok: false, error: "Exactly one existing Steam prefix is required" };
  const p = await render();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /Exactly one existing Steam prefix is required/);
    assert.equal(hasButton(p.host, "Apply nuke (0 files)"), false);
  } finally { await p.cleanup(); }
});

await test("empty plan states nothing installed but still offers a safe apply", async () => {
  fixtures.nuke_plan_xmx = { ok: true, plan_id: "b".repeat(32), files: [], warnings: [], desired_launch_options: "" };
  let live = "";
  (globalThis as any).SteamClient = { Apps: { GetAppLaunchOptions: async () => live } };
  const p = await render();
  try {
    await press(p.host, "Nuke this game's XMX install…");
    assert.match(p.host.textContent ?? "", /Nothing was found installed for this game\./);
    await press(p.host, "Apply nuke (0 files)");
    assert.equal(calls.includes("nuke_finish_xmx"), true);
  } finally { await p.cleanup(); }
});

console.log(`xmx-nuke harness: ${checks} checks passed`);
