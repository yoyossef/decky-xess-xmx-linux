import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, Router, TextField, ToggleField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { getPref, setPref } from "../api";
import { readLaunchOptions, setLaunchOptions } from "../hooks/useRunningGame";
import { XmxController, type XmxProfile, type XmxState } from "../xmx";
import { applyXmx, finishXmx, getXmxLogs, getXmxStatus, isXmxLogs, isXmxStatus, planXmx, recoverXmx, type XmxLogs, type XmxStatus } from "../xmxApi";
import { NukeSection } from "./NukeSection";
import { XessDllPanel, type XessDllInterlock } from "./XessDllPanel";
import { XmxPackagePanel } from "./XmxPackagePanel";

interface Props { appid: string | null; running: boolean }
const PACKAGE_PREF = "xmx_package_dir";
const preStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "12px" } as const;

/** Keyed state prevents one game's results appearing in the next game's view. */
export function XmxPanel(props: Props) {
  return <XmxPanelSession key={props.appid ?? "unresolved"} {...props} />;
}

/** Native XeSS support is independent of the OptiScaler installation. */
function XmxPanelSession({ appid, running }: Props) {
  const [status, setStatus] = useState<XmxStatus | null>(null);
  const statusRef = useRef<XmxStatus | null>(null);
  const [packageDir, setPackageDir] = useState("");
  const [profile, setProfile] = useState<XmxProfile>({ driver: "default", discovery: "auto", diagnostics: false, fg: 0, bridge: 0, sdk_latest: false });
  const profileEdited = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [preferenceWarning, setPreferenceWarning] = useState<string | null>(null);
  const [guardWarning, setGuardWarning] = useState<string | null>(null);
  const [logs, setLogs] = useState<XmxLogs["logs"] | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const [details, setDetails] = useState(false);
  const mounted = useRef(true);
  const loadingRef = useRef(true);
  const operationRef = useRef(false);
  const scope = useRef({ owner: null as "prefix" | "dll" | null, dllRecovery: true });
  const nukeBusy = useRef(false);
  const [, redrawScope] = useState(0);
  const generation = useRef(0);
  // Render epoch: the LATEST committed render is the truth for callback freshness.
  // Handlers may bump async request counters, but a callback is only ever expired
  // by a newer render — a manual bump without a re-render used to freeze the panel.
  const renderEpoch = useRef(0);
  const epoch = ++renderEpoch.current;
  const runningRef = useRef(running);
  runningRef.current = running;
  const [controller] = useState(() => new XmxController(appid ?? "", {
    readLive: id => readLaunchOptions(Number(id)),
    dispatch: (id, options) => setLaunchOptions(Number(id), options),
    allowed: () => {
      if (runningRef.current || scope.current.owner === "dll" || scope.current.dllRecovery) return false;
      try { return String(Router.MainRunningApp?.appid ?? "") !== appid; }
      catch { return false; }
    },
    wait: () => new Promise(resolve => window.setTimeout(resolve, 150)),
    plan: planXmx,
    apply: applyXmx,
    finish: finishXmx,
    recover: recoverXmx,
  }));
  const [transaction, setTransaction] = useState<XmxState>({ ...controller.state });
  const prefixRecovery = () => Boolean(controller.state.pending || controller.state.recoveryBlocked || controller.state.needsRefresh
    || statusRef.current?.pending || statusRef.current?.phase.startsWith("pending_") || statusRef.current?.phase === "recovery_required");
  const [dllInterlock] = useState<XessDllInterlock>(() => ({
    canInspect: () => mounted.current && !runningRef.current && !controller.state.busy && !scope.current.owner,
    allowed: () => mounted.current && !loadingRef.current && !runningRef.current && !controller.state.busy
      && scope.current.owner !== "prefix" && Boolean(statusRef.current?.ok) && !statusRef.current?.legacy && !prefixRecovery(),
    acquire: () => {
      if (scope.current.owner || !dllInterlock.allowed()) return false;
      scope.current.owner = "dll";
      controller.state.preview = null;
      setTransaction({ ...controller.state });
      redrawScope(value => value + 1);
      return true;
    },
    release: () => {
      if (scope.current.owner === "dll") scope.current.owner = null;
      if (mounted.current) redrawScope(value => value + 1);
    },
    reportRecovery: pending => {
      scope.current.dllRecovery = pending;
      if (!mounted.current) return;
      if (pending) { controller.state.preview = null; setTransaction({ ...controller.state }); }
      redrawScope(value => value + 1);
    },
  }));

  const refresh = async (dir: string, remember = false, transactionError: string | null = null, initializePackage = false) => {
    if (!appid || !mounted.current || controller.state.busy) return;
    const request = ++generation.current;
    controller.state.preview = null;
    dllInterlock.clearPreview?.();
    setTransaction({ ...controller.state });
    loadingRef.current = true;
    setLoading(true);
    setError(transactionError);
    try {
      if (remember) {
        setPreferenceWarning(null);
        try {
          const saved = await setPref(PACKAGE_PREF, dir);
          if (!saved?.ok) throw new Error(saved?.error || "Could not remember package directory.");
        } catch (exc) {
          if (mounted.current && generation.current === request) setPreferenceWarning(`Package directory was not remembered: ${String(exc)}`);
        }
      }
      if (!mounted.current || generation.current !== request) return;
      const value = await getXmxStatus(appid, dir);
      if (mounted.current && generation.current === request) {
        statusRef.current = isXmxStatus(value) ? value : null;
        setStatus(statusRef.current);
        if (isXmxStatus(value)) {
          if (!profileEdited.current && value.profile) setProfile({ ...value.profile, fg: value.profile.fg ?? 0, bridge: value.profile.bridge ?? 0, sdk_latest: value.profile.sdk_latest ?? false });
          if (initializePackage && !dir.trim()) {
            const detected = value.package?.path || value.suggested_package;
            if (typeof detected === "string" && detected.trim()) setPackageDir(detected);
          }
          const noJournal = value.pending === null && (value.phase === "enabled" || value.phase === "disabled");
          // A nuke journal is owned by the Danger-zone flow, never by the
          // regular transaction controller.
          const nukePending = value.pending && typeof value.pending === "object"
            && (value.pending as { action?: string }).action === "nuke";
          controller.restorePending(noJournal || nukePending ? null : value.pending ?? undefined);
          setTransaction({ ...controller.state });
        }
        else setError(value?.error || "Invalid or unavailable XeSS XMX status. Refresh before making changes.");
      }
    } catch (exc) {
      if (mounted.current && generation.current === request) {
        statusRef.current = null;
        setStatus(null);
        setError(String(exc));
      }
    } finally {
      if (mounted.current && generation.current === request) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    mounted.current = true;
    if (appid) void getPref(PACKAGE_PREF, "").then(pref => {
      if (!mounted.current) return;
      const dir = typeof pref?.value === "string" ? pref.value : "";
      setPackageDir(dir);
      void refresh(dir, false, null, true);
    }).catch(exc => {
      if (mounted.current) { loadingRef.current = false; setError(String(exc)); setLoading(false); }
    });
    else { loadingRef.current = false; setLoading(false); }
    return () => { mounted.current = false; generation.current++; controller.dispose(); };
  }, [appid, controller]);

  const run = async (operation: () => Promise<void>) => {
    const blockedBy = editBlockReason(false);
    if (blockedBy) { console.warn(`[xess-xmx] operation rejected: ${blockedBy}`); setGuardWarning(`Operation ignored: ${blockedBy}.`); return; }
    setGuardWarning(null);
    operationRef.current = true;
    scope.current.owner = "prefix";
    dllInterlock.clearPreview?.();
    const promise = operation();
    setTransaction({ ...controller.state });
    try {
      await promise;
      const preview = controller.state.preview;
      if (mounted.current && preview?.action === "adopt_trial" && preview.profile && !profileEdited.current) {
        setProfile({ ...preview.profile, fg: preview.profile.fg ?? 0, bridge: preview.profile.bridge ?? 0, sdk_latest: preview.profile.sdk_latest ?? false });
      }
      if (mounted.current && controller.state.preview) {
        const saved = await setPref(PACKAGE_PREF, packageDir);
        if (mounted.current && !saved?.ok) setError(saved?.error || "Could not remember package directory.");
      }
    } catch (exc) {
      if (mounted.current) setError(String(exc));
    }
    if (mounted.current) {
      setTransaction({ ...controller.state });
      if (controller.state.success || controller.state.needsRefresh) await refresh(packageDir, false, controller.state.error);
      // The page header badges this game's setup state; tell it the record
      // just moved (enable/disable/nuke) so the badge follows without a reload.
      if (controller.state.success)
        window.dispatchEvent(new CustomEvent("xmx-setup-changed", { detail: appid }));
    }
    operationRef.current = false;
    scope.current.owner = null;
    if (mounted.current) redrawScope(value => value + 1);
  };

  const otherScopeBlocked = scope.current.owner === "dll" || scope.current.dllRecovery;
  const editingBlocked = Boolean(scope.current.owner) || otherScopeBlocked;
  const blocked = loading || transaction.busy || running || !appid || !status?.ok || Boolean(status.legacy) || editingBlocked;
  const plan = transaction.preview;
  const pending = transaction.pending;
  const needsRecovery = Boolean(pending || transaction.recoveryBlocked || transaction.needsRefresh || status?.pending || status?.phase?.startsWith("pending_") || status?.phase === "recovery_required");
  const drivers = status?.drivers?.filter(driver => driver.valid) ?? [{ id: "default" as const, label: "Package default", valid: true }];
  const driverValid = drivers.some(driver => driver.id === profile.driver);
  const selectedProfile = status?.profile || status?.drivers || profileEdited.current ? profile : undefined;
  // Live mirror of the form: a callback retained from an older render previews
  // against the CURRENT form or refuses loudly, never a stale captured state.
  const liveForm = useRef({ packageDir, profile, selectedProfile });
  useEffect(() => { liveForm.current = { packageDir, profile, selectedProfile }; });
  const formPreview = (action: "configure" | "enable" | "disable" | "adopt_trial", useProfile?: XmxProfile) => {
    if (liveForm.current.packageDir !== packageDir || liveForm.current.profile !== profile || liveForm.current.selectedProfile !== selectedProfile)
      return Promise.reject(new Error("The form changed while the panel updated; nothing was previewed. Retry from the current panel."));
    return controller.preview(packageDir, action, useProfile);
  };
  // Guards read live refs, never the render-time revision: an incrementing
  // generation with a render that bails out used to silently freeze every
  // handler in this tab (a stale revision is not a real invariant; appid
  // changes remount the whole session and async results match `request`).
  const editBlockReason = (pendingIsBlock = true): string | null => {
    if (!mounted.current) return "the panel was closed";
    if (loadingRef.current) return "the status is still loading — Refresh when it settles";
    if (operationRef.current || controller.state.busy) return "another operation is in progress";
    if (nukeBusy.current) return "the nuke flow is running";
    if (scope.current.owner) return "another section owns this game right now";
    if (scope.current.dllRecovery) return "the DLL recovery flow is active";
    if (runningRef.current) return "stop this game first";
    // A pending transaction blocks form edits, but must NOT block run():
    // Resume pending transaction is the only way out of that state.
    if (pendingIsBlock && prefixRecovery()) return "a pending transaction needs recovery first (see above)";
    if (!statusRef.current?.ok) return "the installation status is unavailable — press Refresh";
    if (statusRef.current.legacy) return "legacy unmanaged installation — changes are blocked";
    return null;
  };
  // The escape hatch must stay reachable when the status cannot be read: a
  // broken prefix or corrupt journals are exactly the states nuke unwinds.
  // So this gate deliberately never consults the status — unlike
  // editBlockReason it only refuses when a nuke would race something.
  const nukeBlockReason = (): string | null => {
    if (!mounted.current) return "the panel was closed";
    if (nukeBusy.current) return "the nuke flow is running";
    if (operationRef.current || controller.state.busy) return "another operation is in progress";
    if (scope.current.owner) return "another section owns this game right now";
    if (scope.current.dllRecovery) return "the DLL recovery flow is active";
    if (runningRef.current) return "stop this game first";
    if (!appid) return "no game selected";
    return null;
  };
  const dropOption = (option: unknown) => String((option as { data?: unknown })?.data ?? option);
  const editProfile = (change: Partial<XmxProfile>) => {
    const blockedBy = editBlockReason();
    if (blockedBy) { console.warn(`[xess-xmx] profile edit rejected: ${blockedBy}`); setGuardWarning(`Change ignored: ${blockedBy}.`); return; }
    setGuardWarning(null);
    profileEdited.current = true;
    setProfile(previous => ({ ...previous, ...change }));
    controller.state.preview = null;
    controller.state.error = null;
    controller.state.success = false;
    setTransaction({ ...controller.state });
  };
  const canSelectPackage = () => {
    if (!appid || editBlockReason()) return false;
    try { return String(Router.MainRunningApp?.appid ?? "") !== appid; }
    catch { return false; }
  };
  const editPackage = (dir: string, inspect = false, sourceEpoch = renderEpoch.current) => {
    const blockedBy = editBlockReason();
    if (blockedBy) { console.warn(`[xess-xmx] package edit rejected: ${blockedBy}`); setGuardWarning(`Package change ignored: ${blockedBy}.`); return; }
    if (sourceEpoch !== renderEpoch.current) { console.warn("[xess-xmx] package callback came from an outdated render"); setGuardWarning("Package action came from an outdated view — use the current panel."); return; }
    if (inspect && !canSelectPackage()) return;
    setGuardWarning(null);
    setPackageDir(dir);
    statusRef.current = statusRef.current ? { ...statusRef.current, package: undefined, flatpak: undefined } : null;
    setStatus(statusRef.current);
    setError(null);
    controller.state.preview = null;
    controller.state.error = null;
    controller.state.success = false;
    dllInterlock.clearPreview?.();
    setTransaction({ ...controller.state });
    if (inspect) void refresh(dir, true);
  };
  const readLogs = async () => {
    if (!appid || !mounted.current || loadingRef.current || operationRef.current || scope.current.owner || runningRef.current) return;
    operationRef.current = true;
    scope.current.owner = "prefix";
    setLogsLoading(true);
    setLogsError(null);
    try {
      const result = await getXmxLogs(appid);
      if (!mounted.current) return;
      if (!isXmxLogs(result)) throw new Error(result?.error || "Invalid or unavailable XeSS logs.");
      // Preserve the backend's bounded startup + tail excerpt intact. Only
      // re-bound oversized/untrusted responses, retaining both ends again.
      setLogs(result.logs.slice(0, 8).map(log => {
        const oversized = log.text.length > 66560;
        return { ...log, text: oversized ? log.text.slice(0, 16384)
          + "\n[... truncated by UI; startup above, tail below ...]\n" + log.text.slice(-49152) : log.text,
          truncated: log.truncated || oversized };
      }));
    } catch (exc) { if (mounted.current) setLogsError(String(exc)); }
    finally {
      operationRef.current = false;
      scope.current.owner = null;
      if (mounted.current) { setLogsLoading(false); redrawScope(value => value + 1); }
    }
  };

  return (
    <><PanelSection title="XeSS XMX">
      <PanelSectionRow>
        <div>{status?.legacy ? "Legacy unmanaged installation — changes blocked. Use the original installer to remove it; this manager will not adopt or migrate it." : status?.ok ? (status.installed ? "Enabled (configured)" : "Not installed") : `Installation status unavailable${status?.error ? `: ${status.error}` : ""} — Refresh to retry, or use the Danger zone below to clear whatever is left.`}</div>
        <div>Runtime unverified — configured does not mean the game is using XMX.</div>
        {!packageDir.trim() ? <div role="alert">Choose an existing package in Details → Package folder, then Refresh.</div>
          : status?.package?.valid === false ? <div role="alert">Package needs attention. Check Details → Package folder, then Refresh.</div> : null}
        {status?.package?.errors?.map((message, index) => <div role="alert" key={index}>{message}</div>)}
        {status?.flatpak?.visible === false ? <div role="alert">{status.flatpak.reason || "Package is not visible to Steam."} Check Details → Flatpak visibility and Steam sandbox access, then Refresh.</div> : null}
        {running ? <div>Stop this game before making changes.</div> : null}
        {error || transaction.error ? <div role="alert">{error || transaction.error}</div> : null}
        {preferenceWarning ? <div role="alert">{preferenceWarning}</div> : null}
        {guardWarning ? <div role="alert">{guardWarning}</div> : null}
        {logsError ? <div role="alert">{logsError}</div> : null}
        {transaction.success ? <div>Launch options verified. Transaction completed; runtime remains unverified.</div> : null}
      </PanelSectionRow>
      <XmxPackagePanel currentPath={packageDir} selectionDisabled={!canSelectPackage()} onSelect={dir => editPackage(dir, true, epoch)} />
      <PanelSectionRow>
        <DropdownItem key={`${profile.driver}:${drivers.map(driver => driver.id + driver.label).join("|")}`}
          label="Driver" selectedOption={profile.driver}
          rgOptions={drivers.map(driver => ({ data: driver.id, label: driver.label }))}
          strDefaultLabel="Selected driver unavailable"
          disabled={blocked || needsRecovery || editingBlocked}
          onChange={option => { const value = dropOption(option); if (drivers.some(driver => driver.id === value)) editProfile({ driver: value as XmxProfile["driver"] }); }} />
        {!driverValid ? <div role="alert">Choose a validated driver before previewing a profile change.</div> : null}
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem key={`fg:${profile.fg}`} label="Frame generation" selectedOption={String(profile.fg)}
          rgOptions={[{ data: "0", label: "Game's choice" }, { data: "2", label: "2x" }, { data: "3", label: "3x" }, { data: "4", label: "4x" },
            ...(profile.fg >= 5 ? [{ data: String(profile.fg), label: profile.fg + "x (experimental)" }] : [])]}
          strDefaultLabel="Game's choice"
          disabled={blocked || needsRecovery || editingBlocked}
          onChange={option => editProfile({ fg: Number(dropOption(option)) })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField label="Intel detection fix" checked={profile.discovery === "alternate"}
          description="Try if the game cannot detect Intel XeSS. Not needed by every game."
          disabled={blocked || needsRecovery || editingBlocked} onChange={checked => editProfile({ discovery: checked ? "alternate" : "auto" })} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField label="Debug logs" checked={profile.diagnostics}
          description="Temporary troubleshooting; adds overhead and records local paths."
          disabled={blocked || needsRecovery || editingBlocked} onChange={checked => editProfile({ diagnostics: checked })} />
      </PanelSectionRow>
      {!plan ? <PanelSectionRow>
        {status?.installed ? <ButtonItem layout="below" disabled={blocked || needsRecovery || !driverValid} onClick={() => {
          if (!blocked && !needsRecovery && status?.installed && driverValid) void run(() => formPreview("configure", profile));
        }}>Save settings…</ButtonItem> : <ButtonItem layout="below" disabled={blocked || needsRecovery || !packageDir.trim() || !driverValid} onClick={() => {
          if (!blocked && !needsRecovery && packageDir.trim() && driverValid) void run(() => formPreview("enable", selectedProfile));
        }}>Enable XMX…</ButtonItem>}
        {status?.installed ? <ButtonItem layout="below" disabled={blocked || needsRecovery} onClick={() => {
          if (!blocked && !needsRecovery && status?.installed) void run(() => formPreview("disable"));
        }}>Disable XMX…</ButtonItem> : null}
      </PanelSectionRow> : null}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={loading || transaction.busy || !appid || running || Boolean(scope.current.owner)} onClick={() => {
          if (mounted.current && !loadingRef.current && !operationRef.current && !scope.current.owner && !controller.state.busy && !runningRef.current) void refresh(packageDir, true);
        }}>Refresh</ButtonItem>
        <ButtonItem layout="below" onClick={() => setDetails(value => !value)}>{details ? "Hide details" : "Details"}</ButtonItem>
      </PanelSectionRow>
      {plan ? <>
        <PanelSectionRow>
          <div>Dry-run preview — {plan.action === "enable" ? "Enable XMX" : plan.action === "disable" ? "Disable XMX" : plan.action === "adopt_trial" ? "Import tested setup" : "Save settings"}. No files changed.</div>
          {plan.profile ? <div>{plan.action === "adopt_trial" ? profileEdited.current ? "Selected promotion profile — may differ from the recognized trial" : "Recognized trial profile" : "Selected per-game profile"}: driver {plan.profile.driver}; frame generation {plan.profile.fg === 0 ? "the game's own" : plan.profile.fg + "x"}; discovery {plan.profile.discovery}; diagnostic logging {plan.profile.diagnostics ? "on" : "off"}.</div>
            : plan.action === "adopt_trial" ? <div>Backend did not return a recognized profile summary. No driver or discovery inference is claimed; refresh with an updated backend to inspect it.</div> : null}
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={blocked} onClick={() => {
            if (!blocked) void run(() => controller.confirm(plan.plan_id));
          }}>Apply changes</ButtonItem>
        </PanelSectionRow>
      </> : null}
      {needsRecovery ? <PanelSectionRow>
        <div role="alert">Pending recovery — no automatic rollback. Do not launch the game until this transaction is resolved.</div>
        <div>Old launch options</div><pre style={preStyle}>{pending ? pending.old_launch_options || "(empty)" : "(unavailable — refresh status before recovery)"}</pre>
        <div>New launch options</div><pre style={preStyle}>{pending ? pending.desired_launch_options || "(empty)" : "(unavailable — refresh status before recovery)"}</pre>
        <ButtonItem layout="below" disabled={blocked || !transaction.pendingReady} onClick={() => {
          if (!blocked) void run(() => controller.resume());
        }}>Resume pending transaction</ButtonItem>
        {transaction.pendingAbortable ? <div>To cancel instead, manually restore the exact old launch options in Steam Properties, then explicitly confirm below. Changed files are never overwritten blindly.</div>
          : <div>{transaction.pendingReady ? "Disable restoration has already started; resume with the new launch options. Abort is no longer safe." : "Refresh status before recovery; do not change launch options using an unconfirmed journal."}</div>}
        <ButtonItem layout="below" disabled={blocked || transaction.needsRefresh || !transaction.pendingAbortable} onClick={() => {
          if (!blocked && controller.state.pendingAbortable) void run(() => controller.abort());
        }}>Confirm original options and abort</ButtonItem>
      </PanelSectionRow> : null}
      {details ? <>
        <PanelSectionRow>
          <TextField label="Package folder" value={packageDir}
            disabled={loading || transaction.busy || running || needsRecovery || editingBlocked}
            onChange={e => editPackage(e.target.value)} />
          <div>Independent of OptiScaler. Prefix support requires native XeSS and an existing package. Game DLL changes are separate.</div>
          <div>GPU static readiness: {status?.gpu?.xmx_capable === true ? "v1-validated device" : status?.gpu?.xmx_capable === false ? "unverified or unsupported device" : "not checked"}</div>
          {status?.gpu?.reason ? <div>{status.gpu.reason}</div> : null}
          <div>Native XeSS candidate: {status?.native_xess?.found === true ? "DLL detected (not compatibility proof)" : status?.native_xess?.found === false ? "not detected" : "not checked"}</div>
          {status?.native_xess?.reason ? <div>{status.native_xess.reason}</div> : null}
          <div>Package validation: {status?.package?.valid === true ? "valid (static checks only)" : status?.package?.valid === false ? "invalid" : "not checked"}</div>
          {status?.package?.path ? <pre style={preStyle}>{status.package.path}</pre> : null}
          <div>Flatpak visibility: {status?.flatpak?.visible === true ? "visible" : status?.flatpak?.visible === false ? "not visible" : "unverified"}</div>
          {status?.flatpak?.reason ? <div>{status.flatpak.reason}</div> : null}
          {status?.drivers?.filter(driver => !driver.valid).map(driver => <div key={driver.id}>{driver.label}: unavailable — {driver.reason || "validation failed"}</div>)}
          <DropdownItem key={`fgx:${profile.fg}`} label="Frame generation multiplier (experimental)" selectedOption={String(profile.fg)}
            rgOptions={[{ data: "0", label: "Game's choice" }, ...[2, 3, 4, 5, 6, 7, 8].map(n => ({ data: String(n), label: n + "x" + (n >= 5 ? " (upstream experimental)" : "") }))]}
            strDefaultLabel="Game's choice"
            disabled={blocked || needsRecovery || editingBlocked}
            onChange={option => editProfile({ fg: Number(dropOption(option)) })} />
          <div>2x-4x is Intel's supported override; 5x-8x is the package's experimental shim. Each step above 4x costs frame rate and latency. Keep a frame limiter on.</div>
          <div>Enabled means configured, not runtime-proven. DLL detection, static checks and shared compiler logs cannot prove this game initialized or rendered with XeSS XMX.</div>
          {plan ? <>
            {plan.selected_driver ? <><div>Pinned driver file (static selection, not runtime proof)</div><pre style={preStyle}>{plan.selected_driver}</pre></> : null}
            <div>Old launch options</div><pre style={preStyle}>{plan.old_launch_options || "(empty)"}</pre>
            <div>New launch options</div><pre style={preStyle}>{plan.desired_launch_options || "(empty)"}</pre>
            <div>Operations</div><pre style={preStyle}>{JSON.stringify(plan.operations, null, 2)}</pre>
          </> : null}
        </PanelSectionRow>
        {status?.installed ? <PanelSectionRow>
          <ButtonItem layout="below" disabled={blocked || needsRecovery || (profileEdited.current && !driverValid)} onClick={() => {
            if (!blocked && !needsRecovery && status?.installed && (!profileEdited.current || driverValid)) void run(() => formPreview("adopt_trial", profileEdited.current ? profile : undefined));
          }}>Import tested setup…</ButtonItem>
          <div>Import recognizes supported manual trial flags from fresh active-client launch options. Unless you edit the choices, it retains the trial's driver, discovery and frame generation multiplier with diagnostic logging off. Original uninstall launch options and file backups are preserved; no runtime verification is implied.</div>
        </PanelSectionRow> : null}
        <PanelSectionRow>
          <div>Diagnostic logs are read only when requested. Log output may contain private paths and game details; review before sharing. A shared compiler log is not proof that this game initialized XeSS XMX. Runtime remains unverified.</div>
          <ButtonItem layout="below" disabled={loading || running || logsLoading || Boolean(scope.current.owner) || !appid} onClick={() => { void readLogs(); }}>Read logs</ButtonItem>
          {logsLoading ? <div>Reading bounded diagnostic logs…</div> : null}
          {logs ? <>
            <div>Bounded view: at most 8 files, up to 65 Ki characters per file. Startup and tail excerpts are preserved when truncated. No log file is changed.</div>
            {logs.map((log, index) => <div key={index}><div>{log.name} — {log.exists ? "present" : "not present"}{log.truncated ? " (truncated)" : ""}</div><pre style={preStyle}>{log.path}</pre><pre style={preStyle}>{log.text || "(no log text)"}</pre></div>)}
            <ButtonItem layout="below" disabled={logsLoading} onClick={() => { setLogs(null); setLogsError(null); }}>Clear log view</ButtonItem>
          </> : null}
        </PanelSectionRow>
      </> : null}
    </PanelSection>
    {appid ? <NukeSection appid={appid} running={running} status={status}
      blocked={() => nukeBlockReason()} busyRef={nukeBusy}
      refresh={() => { void refresh(packageDir, false); }} /> : null}
    <XessDllPanel appid={appid} running={running} prefixBlocked={loading || transaction.busy || running || !appid || !status?.ok || Boolean(status.legacy) || needsRecovery || scope.current.owner === "prefix" || nukeBusy.current} interlock={dllInterlock} />
    </>
  );
}
