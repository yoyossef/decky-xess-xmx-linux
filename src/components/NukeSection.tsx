import { ButtonItem, PanelSection, PanelSectionRow, Router } from "@decky/ui";
import { useRef, useState } from "react";
import { readLaunchOptions, setLaunchOptions } from "../hooks/useRunningGame";
import { nukeApplyXmx, nukeFinishXmx, nukePlanXmx, type XmxNukePlan, type XmxStatus } from "../xmxApi";

interface Props {
  appid: string;
  running: boolean;
  status: XmxStatus | null;
  /** Parent interlock: returns a human reason when another flow owns this game. */
  blocked: () => string | null;
  /** Set while the nuke flow runs so the rest of the panel stands down. */
  busyRef: { current: boolean };
  refresh: () => void;
}

const preStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "12px" } as const;
const dangerStyle = { color: "var(--gpColor-Red, #e2574c)" } as const;
type FileEntry = { target: string; action: string; reason?: string };

/** Escape hatch: tear a game's XMX install down even when journals are broken. */
export function NukeSection({ appid, running, status, blocked, busyRef, refresh }: Props) {
  const [phase, setPhase] = useState<"idle" | "armed" | "preview" | "done">("idle");
  const [plan, setPlan] = useState<XmxNukePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const busyOp = useRef(false);
  const epoch = useRef(0);
  mounted.current = true;
  const resume = (() => {
    if (status?.phase !== "pending_nuke" || !status.pending || typeof status.pending !== "object") return null;
    const value = status.pending as Record<string, unknown>;
    return typeof value.plan_id === "string" && typeof value.desired_launch_options === "string"
      ? { plan_id: value.plan_id, desired_launch_options: value.desired_launch_options } : null;
  })();

  const usable = (): string | null => {
    if (busyOp.current) return "a nuke step is already running";
    const reason = blocked();
    if (reason) return reason;
    if (running) return "stop this game first";
    try { if (String(Router.MainRunningApp?.appid ?? "") === appid) return "stop this game first"; } catch { /* probe best-effort */ }
    return null;
  };
  const fail = (request: number, message: unknown) => {
    if (!mounted.current || epoch.current !== request) return;
    setError(String(message));
    setBusy(false); busyOp.current = false; busyRef.current = false;
  };
  const settle = (request: number, done: string) => {
    if (!mounted.current || epoch.current !== request) return;
    setNote(done); setBusy(false); busyOp.current = false; busyRef.current = false;
  };

  const finishNuke = async (request: number, planId: string, desired: string) => {
    setLaunchOptions(Number(appid), desired);
    const readback = await readLaunchOptions(Number(appid));
    if (readback !== desired)
      throw new Error(`Steam returned ${JSON.stringify(readback)} instead of ${JSON.stringify(desired)}. `
        + "Fix the launch options to match, then press Finish nuke.");
    const finished = await nukeFinishXmx(appid, planId, desired);
    if (!finished?.ok) throw new Error(finished?.error || "Nuke finish failed.");
    settle(request, "Nuke complete: state cleared and launch options restored. Refresh to re-detect.");
    refresh();
  };

  const resumeNuke = async () => {
    if (!resume) return;
    const reason = usable();
    if (reason) { setError(`Nuke resume blocked: ${reason}.`); return; }
    const request = ++epoch.current;
    setError(null); setBusy(true); busyOp.current = true; busyRef.current = true;
    try { await finishNuke(request, resume.plan_id, resume.desired_launch_options); }
    catch (exc) { fail(request, `Nuke resume failed: ${String(exc)}`); }
  };

  const arm = async () => {
    const reason = usable();
    if (reason) { setError(`Nuke blocked: ${reason}.`); return; }
    const request = ++epoch.current;
    setError(null); setBusy(true); busyOp.current = true; busyRef.current = true;
    try {
      const value = await nukePlanXmx(appid);
      if (!value?.ok || typeof value.plan_id !== "string") throw new Error(value?.error || "Nuke preview failed.");
      if (!mounted.current || epoch.current !== request) return;
      setPlan(value); setPhase("preview");
      setBusy(false); busyOp.current = false; busyRef.current = false;
    } catch (exc) { fail(request, exc); }
  };

  const apply = async () => {
    if (!plan || typeof plan.plan_id !== "string") { setError("No valid nuke preview to apply."); return; }
    const planId = plan.plan_id;
    const desired = typeof plan.desired_launch_options === "string" ? plan.desired_launch_options : "";
    const reason = usable();
    if (reason) { setError(`Nuke blocked: ${reason}.`); return; }
    const request = ++epoch.current;
    setError(null); setBusy(true); busyOp.current = true; busyRef.current = true;
    try {
      const applied = await nukeApplyXmx(appid, planId);
      if (!applied?.ok) throw new Error(applied?.error || "Nuke apply failed.");
      await finishNuke(request, planId, desired);
      setPhase("done");
    } catch (exc) { fail(request, `Files may be half-removed; press Finish nuke after fixing the cause. ${String(exc)}`); }
  };

  const cancel = () => {
    if (busyOp.current) return;
    epoch.current++;
    setPhase("idle"); setPlan(null); setError(null);
  };

  const listed = (Array.isArray(plan?.files) ? plan!.files : []) as FileEntry[];
  const warnings: string[] = Array.isArray(plan?.warnings)
    ? (plan!.warnings as unknown[]).filter((w): w is string => typeof w === "string") : [];

  return <PanelSection title="Danger zone">
    <PanelSectionRow>
      <div style={preStyle}>
        Removes this game&apos;s XeSS XMX files (originals restored when the journal proves a backup),
        parks any legacy install, restores the previous launch options, and clears the plugin&apos;s state
        for this game. Works even when the install is in a broken state; a tar backup is taken first.
      </div>
    </PanelSectionRow>
    {resume ? <>
      <PanelSectionRow>
        <div role="alert">A nuke was applied but not finished: set Steam launch options back to
          {` ${JSON.stringify(resume.desired_launch_options)} `}if needed, then finish it.</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || running} onClick={() => void resumeNuke()}>
          <span style={dangerStyle}>Finish nuke</span>
        </ButtonItem>
      </PanelSectionRow>
    </> : null}
    {phase === "idle" && !resume ? <PanelSectionRow>
      <ButtonItem layout="below" disabled={busy || running} onClick={() => void arm()}>
        <span style={dangerStyle}>Nuke this game&apos;s XMX install…</span>
      </ButtonItem>
    </PanelSectionRow> : null}
    {phase === "preview" && plan ? <>
      <PanelSectionRow>
        <div style={preStyle}>
          {listed.length ? listed.map(item =>
            `${item.action === "remove" ? "delete" : item.action} ${item.target}${item.reason ? ` (${item.reason})` : ""}`
          ).join("\n") : "Nothing was found installed for this game."}
        </div>
      </PanelSectionRow>
      {warnings.map((message, index) => <PanelSectionRow key={index}><div role="alert" style={preStyle}>{message}</div></PanelSectionRow>)}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || running} onClick={() => void apply()}>
          <span style={dangerStyle}>Apply nuke ({listed.length} file{listed.length === 1 ? "" : "s"})</span>
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={cancel}>Cancel</ButtonItem></PanelSectionRow>
    </> : null}
    {note ? <PanelSectionRow><div style={preStyle}>{note}</div></PanelSectionRow> : null}
    {error ? <PanelSectionRow><div role="alert" style={preStyle}>{error}</div></PanelSectionRow> : null}
  </PanelSection>;
}
