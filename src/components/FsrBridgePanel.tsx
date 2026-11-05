import { ButtonItem, PanelSection, PanelSectionRow, Router, TextField, ToggleField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { XmxController, type XmxProfile } from "../xmx";
import { readLaunchOptions, setLaunchOptions } from "../hooks/useRunningGame";
import {
  applyXmx, finishXmx, getFsrBridgeState, getXmxStatus, installFsrBridge, isXmxStatus,
  planXmx, recoverXmx, uninstallFsrBridge, type FsrBridgeState,
} from "../xmxApi";

/** Exact-shape check: a malformed RPC answer must never drive the panel. */
export function isFsrBridgeState(value: unknown): value is FsrBridgeState {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const s = value as Partial<FsrBridgeState>;
  return s.ok === true
    && (s.package === null || (!!s.package && typeof s.package.tag === "string" && typeof s.package.path === "string"))
    && ["active", "parked", "absent"].includes(String(s.session_conf))
    && !!s.hooks && Array.isArray(s.hooks.hooked) && typeof s.hooks.installed === "boolean"
    && !!s.driver_status && typeof s.driver_status.state === "string"
    && Array.isArray(s.apps) && Array.isArray(s.bridges) && Array.isArray(s.detected)
    && !!s.bridge_profiles && typeof s.bridge_profiles === "object"
    && !!s.names && typeof s.names === "object" && !!s.units && typeof s.units === "object";
}

const UNINSTALL_TOKEN = "uninstall-hooks";

/** Shared read/mutate plumbing for both bridge surfaces (game tab + settings). */
function useBridgeState() {
  const [state, setState] = useState<FsrBridgeState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const mounted = useRef(true);
  const generation = useRef(0);
  const inspect = async () => {
    const request = ++generation.current;
    try {
      const value = await getFsrBridgeState();
      if (!mounted.current || request !== generation.current) return;
      if (!isFsrBridgeState(value)) throw new Error((value as { error?: string })?.error || "Invalid bridge status.");
      setState(value);
      setError(null);
    } catch (exc) {
      if (mounted.current && request === generation.current) setError(String(exc));
    }
  };
  useEffect(() => {
    mounted.current = true;
    void inspect();
    return () => { mounted.current = false; generation.current++; };
  }, []);
  /** Run one transaction, refresh, then re-assert the backend reason: a
   * successful reload must never swallow why a mutation failed. */
  const mutate = async (kind: string, call: () => Promise<{ ok: boolean; error?: string }>,
                        onDone?: () => void) => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    try {
      const result = await call();
      if (!mounted.current) return;
      await inspect();
      if (!mounted.current) return;
      if (!result.ok) setError(result.error || "The bridge transaction failed; nothing else was changed.");
      else onDone?.();
    } catch (exc) {
      if (mounted.current) setError(String(exc));
    } finally {
      if (mounted.current) setBusy(null);
    }
  };
  /** Same guarantees for multi-step flows that throw instead of returning
   * {ok:false} (the XmxController transaction below). */
  const mutateFlow = async (kind: string, run: () => Promise<void>) => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    let failure: string | null = null;
    try {
      await run();
    } catch (exc) {
      failure = String(exc);
    }
    await inspect();
    if (!mounted.current) return;
    if (failure) setError(failure);
    setBusy(null);
  };
  return { state, setState, error, busy, mounted, inspect, mutate, mutateFlow };
}

const small: React.CSSProperties = { fontSize: "0.8rem", opacity: 0.8, padding: "4px 0" };
const errorStyle: React.CSSProperties = { color: "var(--gpColor-Error, #e05c5c)", fontSize: "0.8rem" };

const DEFAULT_PROFILE: XmxProfile = { driver: "default", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false };

/**
 * Per-game FSR->XeSS bridge: lives on the game page, next to Setup. The
 * upstream install.sh (a global transaction) is offered here too when the
 * hooks are missing, because that is exactly where the user needs it.
 *
 * The profile write goes through XmxController — the same transaction the
 * Setup panel proves working — because only the frontend can apply Steam
 * launch options (SteamClient) and read them back for the backend verify.
 * A backend-orchestrated toggle committed values Steam never received.
 */
export function GameFsrBridge({ appid, running }: Readonly<{ appid: string | null; running?: boolean }>) {
  const { state, error, busy, mounted, inspect, mutate, mutateFlow } = useBridgeState();
  const [details, setDetails] = useState(false);
  const profile = appid ? state?.bridge_profiles[appid] : undefined;
  const on = !!appid && !!profile;
  const mode = profile?.bridge ?? 1;
  const runningRef = useRef(running);
  runningRef.current = running;
  const [controller] = useState(() => new XmxController(appid ?? "", {
    readLive: id => readLaunchOptions(Number(id)),
    dispatch: (id, options) => setLaunchOptions(Number(id), options),
    allowed: () => {
      if (runningRef.current) return false;
      try { return String(Router.MainRunningApp?.appid ?? "") !== appid; }
      catch { return false; }
    },
    wait: () => new Promise(resolve => window.setTimeout(resolve, 150)),
    plan: planXmx,
    apply: applyXmx,
    finish: finishXmx,
    recover: recoverXmx,
  }));
  useEffect(() => () => controller.dispose(), [controller]);
  const send = (nextOn: boolean, nextMode: number, nextSdk: boolean) => mutateFlow(`game:${appid}`, async () => {
    if (!appid || !state?.package) return;
    const status = await getXmxStatus(appid, state.package.path);
    if (!isXmxStatus(status)) throw new Error((status as { error?: string })?.error || "Could not read this game's XMX status.");
    if (status.pending) throw new Error("A transaction is already pending for this game — resolve it in the Setup tab first.");
    if (!status.installed && !nextOn) return; // already off: never write to enable it
    const nextProfile: XmxProfile = {
      ...(status.profile ?? DEFAULT_PROFILE),
      bridge: nextOn ? nextMode : 0,
      sdk_latest: nextOn && nextSdk,
    };
    await controller.preview(state.package.path, status.installed ? "configure" : "enable", nextProfile);
    if (controller.state.error) throw new Error(controller.state.error);
    await controller.confirm();
    if (controller.state.error) throw new Error(controller.state.error);
    if (mounted.current) void inspect();
  });
  return <PanelSection title="FSR bridge (XeSS)">
    <PanelSectionRow>
      <div style={small}>
        Routes this game&apos;s FSR 3.1+ upscaling and frame generation onto Intel XeSS
        (XMX cores). The swap happens only while the game runs and reverts on exit.
      </div>
    </PanelSectionRow>
    {error && <PanelSectionRow><div style={errorStyle}>{error}</div></PanelSectionRow>}
    {!appid && <PanelSectionRow>
      <div style={small}>The bridge works per Steam game; this entry has no App ID.</div>
    </PanelSectionRow>}
    {appid && state && !state.package && <PanelSectionRow>
      <div style={small}>No XMX package yet — prepare it in Settings first.</div>
    </PanelSectionRow>}
    {appid && state?.package && !state.hooks.installed && <PanelSectionRow>
      <ButtonItem layout="below" disabled={busy !== null}
        onClick={() => void mutate("install", installFsrBridge)}>
        {busy === "install" ? "Installing hooks…" : "Install Proton hooks (advanced)"}
      </ButtonItem>
    </PanelSectionRow>}
    {appid && state?.hooks.installed && <PanelSectionRow>
      <ToggleField label="FSR→XeSS bridge"
        checked={on} disabled={busy !== null}
        description={on
          ? mode === 2
            ? "Frame generation runs on XeSS; the game's own FSR upscaler stays"
            : "FSR upscaling and frame generation run on XeSS (reverted on exit)"
          : "Off: the game keeps its own FSR libraries"}
        onChange={(next) => void send(next, mode, profile?.sdk_latest ?? false)} />
    </PanelSectionRow>}
    {appid && state?.hooks.installed && <PanelSectionRow>
      <ButtonItem layout="below" onClick={() => setDetails(!details)}>
        {details ? "Hide bridge details" : "Bridge details"}
      </ButtonItem>
    </PanelSectionRow>}
    {details && appid && state?.hooks.installed && <>
      <PanelSectionRow>
        <div style={{ fontSize: "0.75rem", opacity: 0.7 }}>
          Driver: {state.driver_status.state === "ok" ? "loads for bridged games"
            : state.driver_status.state === "failed" ? "FAILED — games fall back to DP4a"
              : "status written at session start"}
          {" · "}Scope: {state.session_conf === "parked" ? "per game only"
            : state.session_conf === "active" ? "session-wide (install did not finish)" : "not installed"}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={small}>Route:</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy !== null || (on && mode === 1)}
          onClick={() => void send(true, 1, profile?.sdk_latest ?? false)}>
          Full — FSR upscaling + frame generation on XeSS
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy !== null || (on && mode === 2)}
          onClick={() => void send(true, 2, profile?.sdk_latest ?? false)}>
          Frame generation only on XeSS
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField label="Latest Intel XeSS SDK" checked={profile?.sdk_latest ?? false}
          disabled={busy !== null}
          description="Use the package's XeSS libraries instead of the game's own (XESS=latest)"
          onChange={(next) => void send(on, mode, next)} />
      </PanelSectionRow>
    </>}
  </PanelSection>;
}

/**
 * The global half of the bridge, living in the plugin's Settings: install
 * status, the never-adopted detection report, and the double-confirmed
 * uninstall. Per-game control lives on each game page instead.
 */
export function BridgeGlobalSection() {
  const { state, error, busy, mutate } = useBridgeState();
  const [confirming, setConfirming] = useState(false);
  const [token, setToken] = useState("");
  const [details, setDetails] = useState(false);
  const hooks = state?.hooks;
  const bridgedCount = state?.bridges.length ?? 0;
  return <PanelSection title="FSR bridge — Proton hooks (advanced)">
    {error && <PanelSectionRow><div style={errorStyle}>{error}</div></PanelSectionRow>}
    {state && <PanelSectionRow>
      <div style={{ fontSize: "0.8rem" }}>
        Package: {state.package ? state.package.tag : "none — prepare the XMX package first"}<br />
        Proton hooks: {hooks?.installed
          ? `installed (${hooks.hooked.length}/${hooks.protons.length}) — ${bridgedCount} game${bridgedCount === 1 ? "" : "s"} bridged`
          : hooks?.stale?.length
            ? "hooked but the package they load was moved — reinstall below"
            : hooks?.protons?.length ? "not installed" : "no Proton found"}<br />
        Driver scope: {state.session_conf === "parked"
          ? "per game only (session file parked)"
          : state.session_conf === "active"
            ? "session-wide file ACTIVE — install did not finish scoping"
            : "not installed"}
      </div>
    </PanelSectionRow>}
    {state && !hooks?.installed && <PanelSectionRow>
      <ButtonItem layout="below" disabled={!state.package || busy !== null}
        onClick={() => void mutate("install", installFsrBridge)}>
        {busy === "install" ? "Installing hooks…" : "Install Proton hooks (advanced)"}
      </ButtonItem>
    </PanelSectionRow>}
    {hooks?.installed && <PanelSectionRow>
      <div style={small}>Enable the bridge on each game&apos;s page, under “FSR bridge (XeSS)”.</div>
    </PanelSectionRow>}
    {!!state?.detected.length && <PanelSectionRow>
      <div style={{ fontSize: "0.8rem", opacity: 0.8 }}>
        Detected, never touched ({state.detected.length}):{" "}
        {state.detected.map((game) => `${game.name} (${game.marker})`).join(", ")}
      </div>
    </PanelSectionRow>}
    {state && <PanelSectionRow>
      <ButtonItem layout="below" onClick={() => setDetails(!details)}>
        {details ? "Hide details" : "Details"}
      </ButtonItem>
    </PanelSectionRow>}
    {details && state && <>
      <PanelSectionRow>
        <div style={{ fontSize: "0.75rem", opacity: 0.7 }}>
          Protons without hook: {hooks!.plain.length ? hooks!.plain.join(", ") : "none"}<br />
          Driver: {state.driver_status.state}<br />
          Units: {Object.entries(state.units).map(([unit, value]) => `${unit}: ${value}`).join(" · ") || "unknown"}<br />
          Journal: {state.journal.slice(-5).map((entry) => `${entry.t ?? "?"} ${entry.step ?? "?"}`).join(" · ") || "empty"}
        </div>
      </PanelSectionRow>
      {hooks?.installed && <>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy !== null}
            onClick={() => { if (!confirming) setConfirming(true); }}>
            {busy === "uninstall" ? "Removing hooks…" : "Uninstall hooks…"}
          </ButtonItem>
        </PanelSectionRow>
        {confirming && <PanelSectionRow>
          <TextField label={`Type ${UNINSTALL_TOKEN} to confirm`} value={token}
            onChange={(event) => setToken(event.target.value)} />
        </PanelSectionRow>}
        {confirming && <PanelSectionRow>
          <ButtonItem layout="below" disabled={token !== UNINSTALL_TOKEN || busy !== null}
            onClick={() => void mutate("uninstall", () => uninstallFsrBridge(token),
              () => { setConfirming(false); setToken(""); })}>
            {busy === "uninstall" ? "Removing hooks…" : "Confirm: remove upstream hooks"}
          </ButtonItem>
        </PanelSectionRow>}
      </>}
    </>}
  </PanelSection>;
}
