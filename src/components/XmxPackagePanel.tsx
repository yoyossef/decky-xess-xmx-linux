import { ButtonItem, PanelSectionRow } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { getXmxPackageStatus, isXmxPackageStatus, prepareXmxPackage, type XmxPackageStatus } from "../xmxApi";

interface Props { currentPath: string; selectionDisabled: boolean; onSelect(path: string): void }

/** Global preparation takes no game lock and never selects its result. */
export function XmxPackagePanel({ currentPath, selectionDisabled, onSelect }: Props) {
  const [status, setStatus] = useState<XmxPackageStatus | null>(null);
  const statusRef = useRef<XmxPackageStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [prepared, setPrepared] = useState<XmxPackageStatus["package"]>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const working = useRef(false);
  const poll = useRef<number | undefined>(undefined);
  const generation = useRef(0);
  // Render epoch (see XmxPanel): only a newer render can expire a callback;
  // receipts bump `generation` solely for async request matching.
  const panelEpoch = useRef(0);
  const epoch = ++panelEpoch.current;
  const stopPoll = () => { window.clearTimeout(poll.current); poll.current = undefined; };
  const accept = (value: Awaited<ReturnType<typeof getXmxPackageStatus>>, previousError: string | null = null) => {
    if (!isXmxPackageStatus(value)) throw new Error(typeof value?.error === "string" && value.error || "Invalid package status.");
    generation.current++; // Expire callbacks that displayed an earlier receipt, including while busy.
    statusRef.current = value;
    setStatus(value);
    if (value.package) setPrepared(value.package);
    const reason = value.error || (value.state === "error" ? "Package preparation failed." : value.state === "preparing" ? previousError : null);
    setError(reason);
    if (value.state === "preparing") {
      stopPoll();
      poll.current = window.setTimeout(() => { void inspect(reason); }, 1500);
    }
  };
  // Status is local-only. Poll only an acknowledged running worker, never the
  // release API; an RPC timeout is not evidence that backend work was cancelled.
  const inspect = async (previousError: string | null = null, onlyPreparing = false) => {
    stopPoll();
    const request = ++generation.current;
    try {
      const value = await getXmxPackageStatus();
      if (!mounted.current || request !== generation.current) return;
      if (!onlyPreparing || (isXmxPackageStatus(value) && value.state === "preparing")) accept(value, previousError);
    } catch (exc) {
      if (mounted.current && request === generation.current) {
        statusRef.current = null;
        setStatus(null);
        setError(previousError || String(exc));
      }
    }
  };
  useEffect(() => {
    mounted.current = true;
    void inspect();
    return () => { mounted.current = false; generation.current++; stopPoll(); };
  }, []);
  const update = async () => {
    if (!mounted.current || working.current || statusRef.current?.state === "preparing") return;
    working.current = true;
    stopPoll();
    const request = ++generation.current;
    setBusy(true);
    setError(null);
    try {
      const value = await prepareXmxPackage();
      if (mounted.current && request === generation.current) accept(value);
    } catch (exc) {
      if (mounted.current && request === generation.current) {
        statusRef.current = null;
        setStatus(null);
        setError(String(exc));
        // A duplicate/busy refusal or transport timeout can leave a worker
        // running. Do not let a stale cached ready receipt erase the failure.
        await inspect(String(exc), true);
      }
    } finally {
      working.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const downloading = busy || status?.state === "preparing";
  return <PanelSectionRow>
    <ButtonItem layout="below" disabled={downloading} onClick={() => { void update(); }}>{downloading ? "Downloading…" : "Update package"}</ButtonItem>
    {prepared ? <div>Prepared: {prepared.tag}</div> : null}
    {status?.state === "ready" && status.package && status.package.path !== currentPath ? <ButtonItem layout="below" disabled={downloading || selectionDisabled} onClick={() => {
      if (mounted.current && epoch === panelEpoch.current && !working.current && !selectionDisabled && statusRef.current?.state === "ready" && status.package) onSelect(status.package.path);
    }}>Use for this game</ButtonItem> : null}
    <div>Download only; game setup is separate.</div>
    {error ? <div role="alert">{error}</div> : null}
  </PanelSectionRow>;
}
