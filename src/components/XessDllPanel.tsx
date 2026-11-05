import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, Router } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { readLaunchOptions } from "../hooks/useRunningGame";
import { XmxController, type XmxState } from "../xmx";
import { applyXessDll, finishXessDll, getXessDllStatus, isXessDllStatus, planXessDll, prepareXessSdk, recoverXessDll, type XessDllStatus } from "../xmxApi";

export interface XessDllInterlock {
  allowed(): boolean;
  canInspect(): boolean;
  acquire(): boolean;
  release(): void;
  reportRecovery(pending: boolean): void;
  clearPreview?: () => void;
}
interface Props { appid: string | null; running: boolean; prefixBlocked: boolean; interlock: XessDllInterlock }
const preStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "12px" } as const;

/** Independent journal: disabling prefix support must never implicitly restore a DLL. */
export function XessDllPanel({ appid, running, prefixBlocked, interlock }: Props) {
  const [status, setStatus] = useState<XessDllStatus | null>(null);
  const statusRef = useRef<XessDllStatus | null>(null);
  const [target, setTarget] = useState("");
  const targetEdited = useRef(false);
  const [loading, setLoading] = useState(true);
  const loadingRef = useRef(true);
  const [error, setError] = useState<string | null>(null);
  const [details, setDetails] = useState(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  const operationRef = useRef(false);
  const [operating, setOperating] = useState(false);
  const adoptTrial = useRef(false);
  const guards = useRef({ running, prefixBlocked });
  guards.current = { running, prefixBlocked };
  const [controller] = useState(() => new XmxController(appid ?? "", {
    readLive: id => readLaunchOptions(Number(id)),
    dispatch: () => false, // DLL plans are forbidden from changing launch options.
    allowed: () => {
      if (guards.current.running || guards.current.prefixBlocked || !interlock.allowed()) return false;
      try { return String(Router.MainRunningApp?.appid ?? "") !== appid; }
      catch { return false; }
    },
    wait: async () => {},
    plan: async (id, path, live, action) => {
      if (action !== "enable" && action !== "disable") throw new Error("Invalid DLL action.");
      const plan = await planXessDll(id, path, live, action, adoptTrial.current);
      if (plan?.ok === true && plan.desired_launch_options !== live) throw new Error("DLL preview attempted to change launch options; nothing was applied.");
      return plan;
    },
    apply: applyXessDll,
    finish: finishXessDll,
    recover: recoverXessDll,
  }));
  const [transaction, setTransaction] = useState<XmxState>({ ...controller.state });
  const reportRecovery = () => interlock.reportRecovery(Boolean(loadingRef.current || !statusRef.current?.ok
    || controller.state.pending || controller.state.needsRefresh
    || controller.state.recoveryBlocked || statusRef.current?.pending || statusRef.current?.phase.startsWith("pending_")
    || statusRef.current?.phase === "recovery_required"));

  const refresh = async (preserveError: string | null = null) => {
    if (!appid || !mounted.current || controller.state.busy) return;
    const request = ++generation.current;
    loadingRef.current = true;
    reportRecovery();
    setLoading(true);
    controller.state.preview = null;
    setTransaction({ ...controller.state });
    setError(preserveError);
    try {
      const value = await getXessDllStatus(appid);
      if (!mounted.current || request !== generation.current) return;
      if (!isXessDllStatus(value)) throw new Error(value?.error || "Invalid or unavailable DLL status. Refresh before making changes.");
      statusRef.current = value;
      setStatus(value);
      const valid = value.candidates.filter(candidate => !candidate.error);
      setTarget(current => targetEdited.current && valid.some(candidate => candidate.path === current) ? current
        : !targetEdited.current && valid.length === 1 ? valid[0].path : "");
      const noJournal = value.pending === null && (value.phase === "enabled" || value.phase === "disabled");
      const pending = value.pending as { action?: unknown; old_launch_options?: unknown; desired_launch_options?: unknown } | null;
      const validPending = pending && (pending.action === "enable" || pending.action === "disable")
        && typeof pending.old_launch_options === "string" && pending.desired_launch_options === pending.old_launch_options;
      controller.restorePending(noJournal ? null : validPending ? pending : undefined);
      setTransaction({ ...controller.state });
      reportRecovery();
    } catch (exc) {
      if (mounted.current && request === generation.current) {
        statusRef.current = null;
        setStatus(null);
        setError(String(exc));
        reportRecovery();
      }
    } finally {
      if (mounted.current && request === generation.current) {
        loadingRef.current = false;
        setLoading(false);
        reportRecovery();
      }
    }
  };
  useEffect(() => {
    mounted.current = true;
    interlock.clearPreview = () => {
      if (!mounted.current) return;
      controller.state.preview = null;
      setTransaction({ ...controller.state });
    };
    if (appid) void refresh();
    else { loadingRef.current = false; setLoading(false); }
    return () => { mounted.current = false; generation.current++; controller.dispose(); interlock.clearPreview = undefined; };
  }, [appid, controller]);

  const run = async (operation: () => Promise<void>) => {
    if (!mounted.current || loadingRef.current || operationRef.current || controller.state.busy
      || guards.current.running || guards.current.prefixBlocked || !statusRef.current?.ok || !interlock.acquire()) return;
    operationRef.current = true;
    setOperating(true);
    setError(null);
    try {
      const promise = operation();
      setTransaction({ ...controller.state });
      await promise;
    } catch (exc) { if (mounted.current) setError(String(exc)); }
    if (mounted.current) {
      setTransaction({ ...controller.state });
      if (controller.state.success || controller.state.needsRefresh) await refresh(controller.state.error);
      reportRecovery();
    }
    operationRef.current = false;
    interlock.release();
    if (mounted.current) setOperating(false);
  };
  const needsRecovery = Boolean(transaction.pending || transaction.needsRefresh || transaction.recoveryBlocked
    || status?.pending || status?.phase.startsWith("pending_") || status?.phase === "recovery_required");
  const blocked = loading || operating || transaction.busy || running || prefixBlocked || !appid || !status?.ok;
  const candidate = status?.candidates.find(item => item.path === target && !item.error);
  const plan = transaction.preview;
  // Live mirror of the selection: a callback retained from an older render
  // (e.g. a Refresh cleared the automatic choice underneath it) plans against
  // the CURRENT selection or refuses loudly, never the stale captured one.
  const targetRef = useRef(target);
  useEffect(() => { targetRef.current = target; });
  const preview = (action: "enable" | "disable", adopt = false) => {
    adoptTrial.current = adopt;
    if (targetRef.current !== target)
      return Promise.reject(new Error("The Game DLL selection changed while the panel updated; nothing was previewed. Retry from the current panel."));
    return controller.preview(target, action);
  };

  const validCandidates = status?.candidates.filter(item => !item.error) ?? [];
  const currentVersion = Boolean(candidate?.version && candidate.version === status?.sdk.version);
  const primaryAction = !status || !validCandidates.length ? null : status.installed ? "Restore original DLL…"
    : status.trial_available ? "Use existing DLL…" : currentVersion ? null : "Update DLL…";
  const cacheNeeded = Boolean(status && validCandidates.length && !status.installed && !status.sdk.ready
    && (status.trial_available || !currentVersion || details));

  return <PanelSection title="XeSS DLL">
    <PanelSectionRow>
      <div>Game DLL: {candidate ? candidate.version || "unknown version" : validCandidates.length ? `${validCandidates.length} candidates — choose one` : status ? "not detected" : "status unavailable"}</div>
      <div>Backup: {status?.installed ? "managed original" : status?.trial_available ? "tested setup available to import" : status ? "not managed" : "unknown"}</div>
      {currentVersion && !status?.installed && !status?.trial_available ? <div>Current version (unmanaged). Version matching is not a hash check.</div> : null}
      <div>Restore separately: disabling XMX keeps this DLL. Stop the game and updater before applying.</div>
      {!status ? <div>Refresh DLL status to retry inspection.</div> : !validCandidates.length ? <div>No supported game DLL found. Check candidate errors in Details, or Refresh DLL status after installing the game.</div> : !candidate ? <div>Choose a Game DLL before previewing.</div> : null}
      {cacheNeeded ? <div>Download SDK to prepare the cache, then preview. No game files are changed by the download.</div> : null}
      {cacheNeeded && status?.sdk.reason ? <div role="alert">{status.sdk.reason}</div> : null}
      {status?.candidates.filter(item => item.error).map(item => <div key={item.path} role="alert">{item.error}</div>)}
      {error || transaction.error ? <div role="alert">{error || transaction.error}</div> : null}
      {transaction.success ? <div>DLL transaction completed with exact launch-option readback. Runtime remains unverified.</div> : null}
    </PanelSectionRow>
    <PanelSectionRow>
      <DropdownItem key={`${target}:${status?.candidates.map(item => item.path).join("|")}`} label="Game DLL"
        selectedOption={target} rgOptions={[{ data: "", label: "Choose a DLL" }, ...validCandidates.map(item => ({ data: item.path, label: validCandidates.length > 1 ? item.path : item.path.split(/[\\/]/).slice(-2).join("/") }))]}
        disabled={blocked || needsRecovery}        onChange={option => {
          if (!mounted.current || loadingRef.current || operationRef.current || guards.current.running || guards.current.prefixBlocked || !interlock.allowed() || needsRecovery) {
            console.warn("[xess-xmx] DLL target change rejected (busy, running, recovery or interlock)");
            setError("DLL change ignored: another operation is active or recovery is pending.");
            return;
          }
          targetEdited.current = true;
          setTarget(option.data);
          controller.state.preview = null;
          controller.state.success = false;
          setTransaction({ ...controller.state });
        }} />
      {cacheNeeded ? <ButtonItem layout="below" disabled={blocked || needsRecovery} onClick={() => {
        if (!blocked && !needsRecovery) void run(async () => {
          const result = await prepareXessSdk();
          if (result?.ok !== true) throw new Error(result?.error || "SDK download failed; no game files were changed.");
          await refresh();
        });
      }}>Download SDK</ButtonItem> : null}
      {!plan && primaryAction ? <ButtonItem layout="below"
        disabled={blocked || needsRecovery || !candidate || (!status?.installed && !status?.sdk.ready)} onClick={() => {
          if (!blocked && !needsRecovery && candidate && status?.installed) void run(() => preview("disable"));
          else if (!blocked && !needsRecovery && candidate && status?.sdk.ready) void run(() => preview("enable", status.trial_available));
        }}>{primaryAction}</ButtonItem> : null}
      <ButtonItem layout="below" disabled={loading || operating || transaction.busy || running || !appid || !interlock.canInspect()} onClick={() => {
        if (mounted.current && !loadingRef.current && !operationRef.current && !guards.current.running && interlock.canInspect()) void refresh();
      }}>Refresh DLL status</ButtonItem>
      <ButtonItem layout="below" onClick={() => setDetails(value => !value)}>{details ? "Hide details" : "Details"}</ButtonItem>
    </PanelSectionRow>
    {plan ? <PanelSectionRow>
      <div>DLL dry-run preview — {plan.action === "disable" ? "Restore original DLL" : adoptTrial.current ? "Use existing DLL" : "Update DLL"}: {candidate?.version || "unknown version"}{plan.action === "disable" ? " → original backup" : ` → ${status?.sdk.version || "pinned version"}`}. No files changed.</div>
      {plan.action === "enable" ? <div>Compatibility test: the game may fail. The original backup is retained for a separate restore.</div> : null}
      <ButtonItem layout="below" disabled={blocked || needsRecovery} onClick={() => { if (!blocked && !needsRecovery) void run(() => controller.confirm(plan.plan_id)); }}>Apply changes</ButtonItem>
    </PanelSectionRow> : null}
    {needsRecovery ? <PanelSectionRow>
      <div role="alert">Pending DLL recovery — do not launch the game. This journal is independent of prefix support.</div>
      <div>Old launch options</div><pre style={preStyle}>{transaction.pending?.old_launch_options ?? "(unavailable — refresh DLL status)"}</pre>
      <div>New launch options (unchanged)</div><pre style={preStyle}>{transaction.pending?.desired_launch_options ?? "(unavailable — refresh DLL status)"}</pre>
      <ButtonItem layout="below" disabled={blocked || !transaction.pendingReady || transaction.needsRefresh} onClick={() => {
        if (!blocked) void run(() => controller.resume());
      }}>Resume pending DLL transaction</ButtonItem>
      <div>{transaction.pendingAbortable ? "To cancel, manually restore the exact original launch options in Steam Properties first, then confirm below. No Steam options will be overwritten." : "Restoration has started or the journal is unconfirmed; abort is not safe. Refresh status and resume only when offered."}</div>
      <ButtonItem layout="below" disabled={blocked || !transaction.pendingAbortable || transaction.needsRefresh} onClick={() => {
        if (!blocked && controller.state.pendingAbortable) void run(() => controller.abort());
      }}>Confirm original options and abort DLL transaction</ButtonItem>
    </PanelSectionRow> : null}
    {details ? <PanelSectionRow>
      <div>{status?.ok ? status.installed ? "Managed DLL update present" : "No managed DLL update" : "DLL status unavailable"}</div>
      <div>Legacy XeSS DLL warning: older library versions may require an explicit update for this game's integration. Architecture must match; DLL presence or replacement is not runtime compatibility proof.</div>
      <div>Experimental DLL replacement: export names are checked, but game imports by ordinal and ABI compatibility are not verified. Named export ordinals may move between SDKs; the game may fail. Confirm only for a reversible compatibility test, with the game and updater stopped.</div>
      <div>Pinned official SDK: {status?.sdk.tag || "unknown"}; library version {status?.sdk.version || "unknown"}; cache {status?.sdk.ready ? "ready" : "not ready"}. Downloading only populates the cache; it does not write game files.</div>
      {status?.sdk.reason && !cacheNeeded ? <div>{status.sdk.reason}</div> : null}
      {status?.candidates.map(item => <div key={item.path}><pre style={preStyle}>{item.path}</pre><div>Library version {item.version || "unknown"}; architecture {item.architecture || "unknown"}</div><pre style={preStyle}>{item.sha256 || "hash unavailable"}</pre></div>)}
      <div>Runtime unverified — shared compiler logs and static library inspection do not establish XeSS XMX rendering.</div>
      {status?.trial_available && !status.installed ? <div>Use existing DLL explicitly imports a recognized tested setup and retains its original library backup. It is separate from prefix-profile import.</div> : null}
      {currentVersion && !status?.installed && !status?.trial_available ? <>
        <div>Matching version does not mean matching hashes. Replace only if needed; the backend checks the exact bytes and may refuse an already-identical unowned DLL.</div>
        {!plan ? <ButtonItem layout="below" disabled={blocked || needsRecovery || !candidate || !status?.sdk.ready} onClick={() => {
          if (!blocked && !needsRecovery && candidate && status?.sdk.ready) void run(() => preview("enable"));
        }}>Replace DLL…</ButtonItem> : null}
      </> : null}
      {plan ? <>
        <div>Old launch options</div><pre style={preStyle}>{plan.old_launch_options || "(empty)"}</pre>
        <div>New launch options (unchanged)</div><pre style={preStyle}>{plan.desired_launch_options || "(empty)"}</pre>
        <div>DLL diffs / operations</div><pre style={preStyle}>{JSON.stringify(plan.operations, null, 2)}</pre>
      </> : null}
    </PanelSectionRow> : null}
  </PanelSection>;
}
