import assert from "node:assert/strict";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fixtures, calls } from "@decky/api";
import { Router } from "@decky/ui";
import { XmxPanel } from "../src/components/XmxPanel";

const manualPath = "/home/player/manual-package";
const preparedPath = "/home/player/.local/share/xmx/packages/v20261104-" + "a".repeat(64);
const prefixStatus = { ok: true, installed: false, legacy: false, phase: "disabled", pending: null, runtime_verified: false,
  package: { valid: true, path: manualPath, errors: [] }, flatpak: { visible: true } };
const dllStatus = { ok: true, installed: false, phase: "disabled", pending: null, trial_available: false,
  sdk: { tag: "v3.0.2", version: "2.0.2.68", ready: true }, candidates: [] };
const idle = { ok: true, state: "idle", package: null, latest: null };
const ready = { ok: true, state: "ready", package: { tag: "v20261104", path: preparedPath, archive_sha256: "a".repeat(64), runtime_verified: false },
  latest: { tag: "v20261104", archive_sha256: "a".repeat(64) } };
let checks = 0;
async function test(name: string, run: () => void | Promise<void>) {
  const filter = (globalThis as any).process?.env.PACKAGE_TEST;
  if (filter && !name.includes(filter)) return;
  Object.keys(fixtures).forEach(key => delete fixtures[key]);
  Object.assign(fixtures, {
    get_pref: (key: string) => ({ key, value: manualPath }), set_pref: { ok: true },
    get_xmx_status: prefixStatus, get_xess_dll_status: dllStatus,
    get_xmx_package_status: idle, prepare_xmx_package: ready,
  });
  (Router as any).MainRunningApp = undefined;
  (globalThis as any).SteamClient = { Apps: {
    GetAppLaunchOptions: () => "before %command%",
    SetAppLaunchOptions: () => { throw new Error("Package preparation/selection must not write Steam options"); },
  } };
  calls.length = 0;
  await run();
  checks++;
  console.log(`PASS ${name}`);
}
async function render(running = false) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(<XmxPanel appid="42" running={running} />); });
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
function input(host: Element) {
  const found = host.querySelector<HTMLInputElement>('input[aria-label="Package folder"]');
  assert.ok(found, "native Package folder exists in Details");
  return found;
}
function noActivation() {
  assert.equal(calls.some(name => /^(plan_|apply_|finish_|recover_)/.test(name)), false, "package work never activates a game configuration");
}

await test("main package row mounts with a local-only status read and one Update package action", async () => {
  const requests: unknown[][] = [];
  fixtures.get_xmx_package_status = (...args: unknown[]) => { requests.push(args); return idle; };
  const p = await render();
  try {
    assert.equal(button(p.host, "Update package").getAttribute("data-disabled"), null);
    assert.deepEqual(requests, [[]], "global local-only status takes no game/path arguments");
    assert.equal(calls.includes("prepare_xmx_package"), false, "no network preparation on mount");
    assert.equal(p.host.querySelector('input[aria-label="Package folder"]'), null, "manual fallback stays in Details");
    assert.equal(p.host.textContent?.includes("Use for this game"), false);
    assert.match(p.host.textContent ?? "", /Download only.*game setup.*separate/i);
    noActivation();
  } finally { await p.cleanup(); }
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

await test("explicit package download is safe while playing and never selects or activates its result", async () => {
  const download = deferred<unknown>();
  const requests: unknown[][] = [];
  fixtures.prepare_xmx_package = (...args: unknown[]) => { requests.push(args); return download.promise; };
  const p = await render(true);
  try {
    await press(p.host, "Details");
    const before = calls.length;
    await press(p.host, "Update package");
    assert.deepEqual(requests, [[]], "backend resolves latest itself without a user URL or destination");
    assert.equal(button(p.host, "Downloading…").getAttribute("data-disabled"), "true");
    await press(p.host, "Downloading…");
    assert.equal(requests.length, 1, "busy preparation cannot be duplicated");
    assert.equal(input(p.host).value, manualPath);
    await act(async () => download.resolve(ready));
    assert.equal(button(p.host, "Update package").getAttribute("data-disabled"), null);
    assert.match(p.host.textContent ?? "", /Prepared: v20261104/);
    assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
    assert.equal(input(p.host).value, manualPath, "download completion never selects the new path");
    assert.deepEqual(calls.slice(before), ["prepare_xmx_package"], "no prefix, preference, DLL or launch-option writes");
    noActivation();
  } finally { await p.cleanup(); }
});

await test("Use for this game explicitly selects the prepared path and refreshes diagnostics without applying", async () => {
  const inspected: unknown[][] = [];
  const saved: unknown[][] = [];
  fixtures.get_xmx_package_status = ready;
  fixtures.get_xmx_status = (...args: unknown[]) => { inspected.push(args); return { ...prefixStatus, package: { valid: true, path: args[1], errors: [] } }; };
  fixtures.set_pref = (...args: unknown[]) => { saved.push(args); return { ok: true }; };
  fixtures.plan_xmx = (...args: unknown[]) => ({ ok: true, plan_id: "old-package-plan", old_launch_options: args[2], desired_launch_options: "after %command%", operations: [] });
  const p = await render();
  try {
    assert.match(p.host.textContent ?? "", /Prepared: v20261104/);
    assert.doesNotMatch(p.host.textContent ?? "", /latest.*checked|up.to.date|XMX verified/i, "cached preparation is not a latest-release check or runtime proof");
    assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), null);
    await press(p.host, "Enable XMX…");
    assert.match(p.host.textContent ?? "", /Dry-run preview/);
    const staleConfirm = queued(p.host, "Apply changes");
    await press(p.host, "Details");
    const before = calls.length;
    await press(p.host, "Use for this game");
    assert.equal(input(p.host).value, preparedPath);
    assert.deepEqual(inspected.at(-1), ["42", preparedPath]);
    assert.deepEqual(saved.at(-1), ["xmx_package_dir", preparedPath]);
    assert.match(p.host.textContent ?? "", /Package validation: valid/);
    assert.doesNotMatch(p.host.textContent ?? "", /Dry-run preview/);
    assert.equal(p.host.textContent?.includes("Use for this game"), false, "same prepared path needs no redundant selection action");
    await act(async () => staleConfirm());
    assert.equal(calls.slice(before).some(name => /^(plan_|apply_|finish_|recover_)/.test(name)), false, "selection and retained old confirmation cannot activate anything");
  } finally { await p.cleanup(); }
});
function queued(host: Element, text: string) {
  const element = button(host, text);
  const key = Object.keys(element).find(name => name.startsWith("__reactProps"))!;
  return (element as any)[key].onClick as () => void;
}

for (const [label, response] of [
  ["relative path", { ...ready, package: { ...ready.package, path: "relative/package" } }],
  ["traversal path", { ...ready, package: { ...ready.package, path: "/home/player/../outside" } }],
  ["root path", { ...ready, package: { ...ready.package, path: "/" } }],
  ["double root", { ...ready, package: { ...ready.package, path: "//home/player/package" } }],
  ["control path", { ...ready, package: { ...ready.package, path: "/home/player/package\u0000" } }],
  ["runtime true", { ...ready, package: { ...ready.package, runtime_verified: true } }],
  ["runtime missing", { ...ready, package: { tag: "tag", path: preparedPath, archive_sha256: "a".repeat(64) } }],
  ["short hash", { ...ready, package: { ...ready.package, archive_sha256: "a".repeat(63) } }],
  ["nonhex hash", { ...ready, package: { ...ready.package, archive_sha256: "g".repeat(64) } }],
  ["empty tag", { ...ready, package: { ...ready.package, tag: "" } }],
  ["invalid latest hash", { ...ready, latest: { tag: "new", archive_sha256: "unverified" } }],
  ["latest missing", { ok: true, state: "ready", package: ready.package }],
  ["latest malformed", { ...ready, latest: [] }],
  ["latest invalid tag", { ...ready, latest: { tag: false, archive_sha256: "b".repeat(64) } }],
  ["invalid error field", { ...ready, error: { message: "unsafe" } }],
  ["unknown state", { ...ready, state: "verified" }],
  ["package missing", { ok: true, state: "ready", latest: null }],
  ["ready without package", { ...ready, package: null }],
  ["array package", { ...ready, package: [] }],
  ["success bit only", { ok: true }],
] as const) {
  await test(`malformed package status rejects ${label} without blocking the manual fallback`, async () => {
    fixtures.get_xmx_package_status = response;
    const p = await render();
    try {
      assert.equal(p.host.textContent?.includes("Use for this game"), false, "unvalidated package cannot be selected");
      assert.match(p.host.textContent ?? "", /Invalid.*package status/i);
      assert.equal(button(p.host, "Update package").getAttribute("data-disabled"), null, "invalid local status can be retried explicitly");
      await press(p.host, "Details");
      assert.equal(input(p.host).value, manualPath);
      assert.equal(input(p.host).disabled, false, "updater failures never join game configuration interlocks");
      noActivation();
    } finally { await p.cleanup(); }
  });
}

for (const kind of ["refusal", "throw", "error state", "malformed"] as const) {
  await test(`package ${kind} preserves its reason, prior prepared info and manual path with explicit retry`, async () => {
    fixtures.get_xmx_package_status = ready;
    fixtures.prepare_xmx_package = () => {
      if (kind === "throw") throw new Error("Connection timed out; preparation may continue");
      return kind === "refusal" ? { ok: false, error: "Archive checksum mismatch; old package kept" }
        : kind === "error state" ? { ...idle, state: "error", error: "Release metadata unavailable; old package kept" }
          : { ...ready, package: { ...ready.package, runtime_verified: true } };
    };
    const p = await render();
    try {
      const staleUse = queued(p.host, "Use for this game");
      await press(p.host, "Details");
      await press(p.host, "Update package");
      assert.match(p.host.textContent ?? "", /Prepared: v20261104/, "a failed attempt must not erase prior prepared info");
      assert.match(p.host.textContent ?? "", kind === "throw" ? /Connection timed out; preparation may continue/
        : kind === "refusal" ? /Archive checksum mismatch; old package kept/ : kind === "error state" ? /Release metadata unavailable; old package kept/ : /Invalid package status/);
      assert.equal(p.host.textContent?.includes("Use for this game"), false, "failure cannot authorize an older cached receipt");
      assert.equal(input(p.host).value, manualPath);
      assert.equal(input(p.host).disabled, false);
      const before = calls.length;
      await act(async () => staleUse());
      assert.deepEqual(calls.slice(before), [], "retained pre-failure Use callback must fail closed");
      fixtures.prepare_xmx_package = ready;
      await press(p.host, "Update package");
      assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), null);
      assert.equal(input(p.host).value, manualPath, "retry success still needs explicit selection");
      assert.doesNotMatch(p.host.textContent ?? "", /checksum mismatch|metadata unavailable|Connection timed out|Invalid package status/);
      noActivation();
    } finally { await p.cleanup(); }
  });
}

async function withPackageClock(run: (clock: { timers: Map<number, () => void>; tick(): Promise<void> }) => Promise<void>) {
  const originalSet = window.setTimeout;
  const originalClear = window.clearTimeout;
  let next = 0;
  const timers = new Map<number, () => void>();
  window.setTimeout = ((fn: () => void) => { timers.set(++next, fn); return next; }) as typeof window.setTimeout;
  window.clearTimeout = id => { timers.delete(id!); };
  try {
    await run({ timers, tick: async () => {
      assert.equal(timers.size, 1, "exactly one bounded local-status poll is scheduled");
      const [id, fn] = timers.entries().next().value!;
      timers.delete(id);
      await act(async () => fn());
    } });
  } finally { window.setTimeout = originalSet; window.clearTimeout = originalClear; }
}
for (const origin of ["mount", "prepare", "RPC timeout", "busy refusal"] as const) {
  await test(`${origin} preparing state polls local status without duplicate downloads or game interlocks`, async () => {
    const preparing = { ...idle, state: "preparing", package: ready.package };
    let reads = 0;
    fixtures.get_xmx_package_status = () => {
      reads++;
      return origin === "mount" || reads > 1 ? preparing : idle;
    };
    fixtures.prepare_xmx_package = () => {
      if (origin === "RPC timeout") throw new Error("Decky RPC timeout; worker still running");
      return origin === "busy refusal" ? { ok: false, error: "Another package preparation is in progress" } : preparing;
    };
    await withPackageClock(async clock => {
      const p = await render();
      try {
        await press(p.host, "Details");
        if (origin !== "mount") await press(p.host, "Update package");
        assert.equal(button(p.host, "Downloading…").getAttribute("data-disabled"), "true");
        const before = calls.filter(name => name === "prepare_xmx_package").length;
        await press(p.host, "Downloading…");
        assert.equal(calls.filter(name => name === "prepare_xmx_package").length, before);
        assert.equal(before, origin === "mount" ? 0 : 1);
        assert.equal(input(p.host).disabled, false, "global preparation cannot lock the manual editor or recovery scopes");
        assert.equal(p.host.textContent?.includes("Use for this game"), false);
        if (origin === "RPC timeout") assert.match(p.host.textContent ?? "", /Decky RPC timeout; worker still running/);
        if (origin === "busy refusal") assert.match(p.host.textContent ?? "", /Another package preparation is in progress/);
        fixtures.get_xmx_package_status = ready;
        await clock.tick();
        assert.equal(button(p.host, "Update package").getAttribute("data-disabled"), null);
        assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), null);
        assert.equal(input(p.host).value, manualPath);
        assert.equal(clock.timers.size, 0, "polling stops when preparation settles");
        noActivation();
      } finally { await p.cleanup(); }
      assert.equal(clock.timers.size, 0, "unmount cancels polling");
    });
  });
}

await test("retained disabled Use callback from a download cannot select the previous receipt after completion", async () => {
  fixtures.get_xmx_package_status = ready;
  const download = deferred<unknown>();
  fixtures.prepare_xmx_package = () => download.promise;
  const newer = { ...ready, package: { ...ready.package, tag: "v-next", path: preparedPath + "-new", archive_sha256: "b".repeat(64) },
    latest: { tag: "v-next", archive_sha256: "b".repeat(64) } };
  const p = await render();
  try {
    await press(p.host, "Details");
    const beforeDownloadUse = queued(p.host, "Use for this game");
    await press(p.host, "Update package");
    assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
    const duringDownloadUse = queued(p.host, "Use for this game");
    await act(async () => download.resolve(newer));
    const before = calls.length;
    await act(async () => { beforeDownloadUse(); duringDownloadUse(); });
    assert.equal(input(p.host).value, manualPath, "neither pre-download nor disabled in-flight callback may select an old receipt");
    assert.deepEqual(calls.slice(before), []);
    assert.match(p.host.textContent ?? "", /Prepared: v-next/);
    await press(p.host, "Use for this game");
    assert.equal(input(p.host).value, newer.package.path, "only the current displayed receipt is selectable");
    noActivation();
  } finally { await p.cleanup(); }
});

await test("Use selection rechecks the live running app even before the polling prop catches up", async () => {
  fixtures.get_xmx_package_status = ready;
  const p = await render();
  try {
    const staleUse = queued(p.host, "Use for this game");
    (Router as any).MainRunningApp = { appid: 42 };
    await act(async () => p.root.render(<XmxPanel appid="42" running={false} />));
    assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
    const before = calls.length;
    await act(async () => staleUse());
    await press(p.host, "Use for this game");
    assert.deepEqual(calls.slice(before), [], "forced current/retained Use must consult live parent guards");
    await press(p.host, "Details");
    assert.equal(input(p.host).value, manualPath);
  } finally { await p.cleanup(); }
});

const pending = { plan_id: "pending", action: "enable", phase: "pending_enable", old_launch_options: "before", desired_launch_options: "after" };
for (const [label, setup] of [
  ["running prop", () => {}],
  ["prefix recovery", () => { fixtures.get_xmx_status = { ...prefixStatus, phase: "pending_enable", pending }; }],
  ["prefix unavailable", () => { fixtures.get_xmx_status = { ok: false, error: "Prefix unavailable" }; }],
  ["prefix missing journal", () => { fixtures.get_xmx_status = { ...prefixStatus, pending: undefined }; }],
  ["legacy prefix", () => { fixtures.get_xmx_status = { ...prefixStatus, legacy: true }; }],
  ["DLL recovery", () => { fixtures.get_xess_dll_status = { ...dllStatus, phase: "pending_enable", pending: { ...pending, desired_launch_options: "before" } }; }],
  ["DLL unavailable", () => { fixtures.get_xess_dll_status = { ok: false, error: "DLL unavailable" }; }],
  ["DLL missing journal", () => { fixtures.get_xess_dll_status = { ...dllStatus, pending: undefined }; }],
] as const) {
  await test(`${label} blocks forced package selection but not independent preparation`, async () => {
    setup();
    fixtures.get_xmx_package_status = ready;
    const p = await render(label === "running prop");
    try {
      assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
      const before = calls.length;
      await press(p.host, "Use for this game");
      assert.deepEqual(calls.slice(before), []);
      assert.equal(button(p.host, "Update package").getAttribute("data-disabled"), null);
      await press(p.host, "Update package");
      assert.deepEqual(calls.slice(before), ["prepare_xmx_package"], "global preparation does not acquire either game lock");
      assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
      await press(p.host, "Details");
      assert.equal(input(p.host).value, manualPath);
      noActivation();
    } finally { await p.cleanup(); }
  });
}
for (const scope of ["prefix", "DLL"] as const) {
  await test(`${scope} operation blocks current and retained Use without deadlocking package downloads`, async () => {
    fixtures.get_xmx_package_status = ready;
    const response = deferred<any>();
    fixtures.get_xmx_logs = () => response.promise;
    fixtures.get_xess_dll_status = { ...dllStatus, sdk: { ...dllStatus.sdk, ready: false },
      candidates: [{ path: "/game/libxess.dll", version: "1.3", architecture: "x86_64", sha256: "a".repeat(64) }] };
    fixtures.prepare_xess_sdk = () => response.promise;
    const p = await render();
    try {
      await press(p.host, "Details");
      const staleUse = queued(p.host, "Use for this game");
      await press(p.host, scope === "prefix" ? "Read logs" : "Download SDK");
      assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), "true");
      const before = calls.length;
      await act(async () => staleUse());
      await press(p.host, "Use for this game");
      assert.deepEqual(calls.slice(before), []);
      await press(p.host, "Update package");
      assert.deepEqual(calls.slice(before), ["prepare_xmx_package"]);
      assert.equal(input(p.host).value, manualPath);
      await act(async () => response.resolve(scope === "prefix" ? { ok: true, runtime_verified: false, logs: [] } : { ok: true }));
      assert.equal(button(p.host, "Use for this game").getAttribute("data-disabled"), null);
      noActivation();
    } finally { await p.cleanup(); }
  });
}
async function enterPackage(host: Element, value: string) {
  await act(async () => {
    const native = input(host);
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(native, value);
    native.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}
for (const edit of ["manual path", "profile"] as const) {
  await test(`retained Use cannot overwrite a subsequent ${edit} edit`, async () => {
    fixtures.get_xmx_package_status = ready;
    const p = await render();
    try {
      await press(p.host, "Details");
      const staleUse = queued(p.host, "Use for this game");
      if (edit === "manual path") await enterPackage(p.host, "/home/player/deliberate-other-package");
      else await act(async () => p.host.querySelector<HTMLInputElement>('input[aria-label="Debug logs"]')!.click());
      const before = calls.length;
      await act(async () => staleUse());
      assert.deepEqual(calls.slice(before), [], "old form-bound callback cannot refresh or save a path");
      assert.equal(input(p.host).value, edit === "manual path" ? "/home/player/deliberate-other-package" : manualPath);
      await press(p.host, "Use for this game");
      assert.equal(input(p.host).value, preparedPath, "fresh explicit selection remains usable");
      noActivation();
    } finally { await p.cleanup(); }
  });
}
for (const stage of ["local status", "download", "local poll"] as const) {
  for (const destination of ["another game", "unmount"] as const) {
    await test(`late ${stage} response after ${destination} cannot select a folder or disturb a new preview`, async () => {
      const response = deferred<any>();
      fixtures.get_xmx_package_status = stage === "local status" ? () => response.promise
        : stage === "local poll" ? { ...idle, state: "preparing" } : ready;
      fixtures.prepare_xmx_package = () => response.promise;
      fixtures.plan_xmx = (...args: unknown[]) => ({ ok: true, plan_id: "new-game-preview", old_launch_options: args[2], desired_launch_options: "new %command%", operations: [] });
      await withPackageClock(async clock => {
        const p = await render();
        let cleaned = false;
        try {
          let staleUse: (() => void) | undefined;
          if (stage === "download") {
            staleUse = queued(p.host, "Use for this game");
            await press(p.host, "Update package");
          }
          if (stage === "local poll") {
            fixtures.get_xmx_package_status = () => response.promise;
            await clock.tick();
          }
          fixtures.get_xmx_package_status = idle;
          if (destination === "another game") {
            await act(async () => p.root.render(<XmxPanel appid="43" running={false} />));
            await press(p.host, "Enable XMX…");
            await press(p.host, "Details");
            assert.match(p.host.textContent ?? "", /Dry-run preview/);
          } else { await p.cleanup(); cleaned = true; }
          const before = calls.length;
          await act(async () => { response.resolve(ready); staleUse?.(); });
          assert.deepEqual(calls.slice(before), [], "late response cannot request old diagnostics, save preferences or mutate a game");
          assert.equal(clock.timers.size, 0);
          if (destination === "another game") {
            assert.equal(input(p.host).value, manualPath);
            assert.match(p.host.textContent ?? "", /Dry-run preview/, "new game's deliberate preview survives the old result");
            assert.doesNotMatch(p.host.textContent ?? "", /Prepared: v20261104/);
            assert.equal(p.host.textContent?.includes("Use for this game"), false);
          }
          assert.equal(calls.some(name => /^(apply_|finish_|recover_)/.test(name)), false);
        } finally { if (!cleaned) await p.cleanup(); }
      });
    });
  }
}

console.log(`XeSS package: ${checks} checks passed`);
