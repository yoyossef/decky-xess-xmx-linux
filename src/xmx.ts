/** Active-client-only transaction coordinator. No backend VDF fallback belongs here. */
export type XmxAction = "enable" | "disable" | "configure" | "adopt_trial";
export interface XmxProfile {
  driver: "default" | "26.1" | "26.3";
  discovery: "auto" | "alternate";
  diagnostics: boolean;
  // 0 = the game's own choice; 2-4 Intel's override range; 5-8 upstream experimental.
  fg: number;
  // FSR->XeSS bridge: 0 off, 1 FSR=xess (upscaling + FG on XeSS), 2 FSR_FG=xess (FG only).
  bridge: number;
  // XESS=latest: Intel's SDK libraries instead of the game's older ones.
  sdk_latest: boolean;
}
export function isXmxProfile(value: unknown): value is XmxProfile {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const p = value as XmxProfile;
  return Object.keys(p).length === 6 && ["default", "26.1", "26.3"].includes(p.driver)
    && ["auto", "alternate"].includes(p.discovery) && typeof p.diagnostics === "boolean"
    && Number.isInteger(p.fg) && [0, 2, 3, 4, 5, 6, 7, 8].includes(p.fg)
    && Number.isInteger(p.bridge) && [0, 1, 2].includes(p.bridge)
    && typeof p.sdk_latest === "boolean";
}
export interface XmxResult {
  ok: boolean;
  error?: string;
}
export interface XmxPlan extends XmxResult {
  plan_id: string;
  old_launch_options: string;
  desired_launch_options: string;
  operations: unknown[];
  /** Optional for older backends; only a complete validated summary is shown. */
  profile?: XmxProfile;
  selected_driver?: string;
}
export function isXmxPlan(value: unknown): value is XmxPlan {
  if (!value || typeof value !== "object") return false;
  const p = value as Partial<XmxPlan>;
  return p.ok === true && typeof p.plan_id === "string" && p.plan_id.length > 0
    && typeof p.old_launch_options === "string" && typeof p.desired_launch_options === "string"
    && Array.isArray(p.operations)
    && (p.profile === undefined || isXmxProfile(p.profile))
    && (p.selected_driver === undefined || (typeof p.selected_driver === "string"
      && p.selected_driver.startsWith("/") && !p.selected_driver.includes("\0")));
}

export interface XmxApply extends XmxResult {
  plan_id?: string;
  action?: XmxAction;
  old_launch_options?: string;
  pending_launch?: boolean;
  phase?: string;
  desired_launch_options?: string;
}
export interface XmxDependencies {
  readLive(appid: string): Promise<string | null>;
  dispatch(appid: string, options: string): boolean;
  allowed(): boolean;
  wait(): Promise<void>;
  plan(appid: string, packageDir: string, options: string, action: XmxAction, profile?: XmxProfile): Promise<XmxPlan>;
  apply(appid: string, planId: string): Promise<XmxApply>;
  finish(appid: string, planId: string, options: string): Promise<XmxResult>;
  recover?(appid: string, planId: string, options: string, action: "abort"): Promise<XmxResult>;
}
export interface XmxState {
  busy: boolean;
  error: string | null;
  preview: (XmxPlan & { action: XmxAction }) | null;
  pending: XmxPlan | null;
  pendingReady: boolean;
  pendingAbortable: boolean;
  needsRefresh: boolean;
  recoveryBlocked: boolean;
  success: boolean;
}

export class XmxController {
  state: XmxState = {
    busy: false, error: null, preview: null, pending: null,
    pendingReady: false, pendingAbortable: false, needsRefresh: false,
    recoveryBlocked: false, success: false,
  };

  private active = true;

  constructor(readonly appid: string, private readonly dependencies: XmxDependencies) {}

  /** Supply only a successful, fresh backend status.pending (explicit null means no journal). */
  restorePending(value: unknown) {
    if (!this.active || this.state.busy) return;
    this.state.pendingAbortable = false;
    if (value === null) {
      this.state.needsRefresh = false;
      if (this.state.pending || this.state.recoveryBlocked) {
        this.state.recoveryBlocked = false;
        this.state.pending = null;
        this.state.pendingReady = false;
        this.state.error = null;
      }
      return;
    }
    const record = (value && typeof value === "object" ? value : {}) as Partial<XmxPlan> & { phase?: string; action?: string };
    const plan = { ...record, ok: true, operations: record.operations ?? [] };
    const profileChange = record.action === "configure" || record.action === "adopt_trial";
    const profilePending = profileChange && record.phase === `pending_${record.action}`;
    const profileWriting = profileChange && record.phase === `writing_${record.action}`;
    const profileRestoring = profileChange && record.phase === "restoring";
    const knownPhase = (record.action === "enable" && (record.phase === "writing_enable" || record.phase === "pending_enable" || record.phase === "restoring"))
      || (record.action === "disable" && (record.phase === "pending_disable" || record.phase === "restoring"))
      || profilePending || profileWriting || profileRestoring;
    if (!isXmxPlan(plan) || !knownPhase) {
      this.state.recoveryBlocked = true;
      this.state.preview = null;
      this.state.pending = null;
      this.state.pendingReady = false;
      this.state.success = false;
      this.state.error = "Invalid pending recovery record. Do not launch the game or create a new plan.";
      return;
    }
    this.state.recoveryBlocked = false;
    this.state.needsRefresh = false;
    this.state.preview = null;
    this.state.pending = plan;
    this.state.pendingReady = (record.action === "enable" && record.phase === "pending_enable")
      || (record.action === "disable" && (record.phase === "pending_disable" || record.phase === "restoring")) || profilePending;
    this.state.pendingAbortable = record.action === "enable"
      || (record.action === "disable" && record.phase === "pending_disable") || profilePending || profileWriting || profileRestoring;
    this.state.success = false;
  }

  dispose() { this.active = false; }

  private guard() {
    if (!this.active) throw new Error("View changed; reopen this game's Setup to recover any pending transaction.");
    if (!this.dependencies.allowed()) throw new Error("The game must be stopped before changing XeSS XMX.");
  }

  /** A failed RPC may already have changed journal phase. Never retry stale recovery. */
  private async mutate<T extends XmxResult>(operation: () => Promise<T>): Promise<T> {
    try {
      const result = await operation();
      if (result?.ok !== true) throw new Error(result?.error || "Backend transaction failed; refresh pending recovery.");
      return result;
    } catch (error) {
      this.state.needsRefresh = true;
      this.state.pendingReady = false;
      this.state.pendingAbortable = false;
      throw error;
    }
  }

  async confirm(expectedPlanId?: string) {
    const plan = this.state.preview;
    if (!plan || (expectedPlanId !== undefined && expectedPlanId !== plan.plan_id)
      || this.state.busy || this.state.pending || this.state.needsRefresh || this.state.recoveryBlocked) return;
    this.state.busy = true;
    this.state.error = null;
    try {
      this.guard();
      const options = await this.dependencies.readLive(this.appid);
      this.guard();
      if (options !== plan.old_launch_options) {
        this.state.preview = null;
        throw new Error("Live launch options changed or are unavailable. Create a new preview.");
      }
      this.state.pending = plan;
      this.state.preview = null;
      this.guard();
      const applied = await this.mutate(() => this.dependencies.apply(this.appid, plan.plan_id));
      this.guard();
      const matches = applied.pending_launch === true && applied.phase === `pending_${plan.action}`
        && applied.action === plan.action && applied.plan_id === plan.plan_id
        && applied.old_launch_options === plan.old_launch_options
        && applied.desired_launch_options === plan.desired_launch_options;
      if (!matches) {
        this.state.needsRefresh = true;
        throw new Error("Apply did not confirm the previewed options. Refresh pending recovery before continuing.");
      }
      this.state.pendingReady = true;
      this.state.pendingAbortable = true;
      await this.completeLaunch(plan);
    } catch (error) {
      this.state.error = String(error);
    } finally {
      this.state.busy = false;
    }
  }

  async resume() {
    if (this.state.busy || this.state.needsRefresh || !this.state.pending || !this.state.pendingReady) return;
    this.state.busy = true;
    this.state.error = null;
    try {
      await this.completeLaunch(this.state.pending);
    } catch (error) {
      this.state.error = String(error);
    } finally {
      this.state.busy = false;
    }
  }

  /** Never restores Steam options blindly: the user must first restore the exact original string. */
  async abort() {
    const plan = this.state.pending;
    if (this.state.busy || this.state.needsRefresh || !this.state.pendingAbortable || !plan || !this.dependencies.recover) return;
    this.state.busy = true;
    this.state.error = null;
    try {
      this.guard();
      const options = await this.dependencies.readLive(this.appid);
      this.guard();
      if (options !== plan.old_launch_options) throw new Error("Restore the exact old launch options in Steam Properties before confirming abort. Nothing was overwritten.");
      await this.mutate(() => this.dependencies.recover!(this.appid, plan.plan_id, options, "abort"));
      this.guard();
      this.state.pending = null;
      this.state.pendingReady = false;
      this.state.pendingAbortable = false;
      this.state.success = true;
    } catch (error) {
      this.state.error = String(error);
    } finally {
      this.state.busy = false;
    }
  }

  private async completeLaunch(plan: XmxPlan) {
    this.guard();
    const current = await this.dependencies.readLive(this.appid);
    this.guard();
    if (current !== plan.old_launch_options && current !== plan.desired_launch_options) {
      throw new Error("Live launch options changed or are unavailable; pending recovery required. No options were overwritten.");
    }
    let readback: string | null = current;
    if (current !== plan.desired_launch_options) {
      if (!this.dependencies.dispatch(this.appid, plan.desired_launch_options)) throw new Error("Steam rejected launch options; pending recovery required.");
      for (let attempt = 0; attempt < 5; attempt++) {
        if (attempt > 0) await this.dependencies.wait();
        this.guard();
        readback = await this.dependencies.readLive(this.appid);
        this.guard();
        if (readback === plan.desired_launch_options) break;
      }
    }
    if (readback !== plan.desired_launch_options) throw new Error("Launch options were not verified; pending recovery required.");
    this.guard();
    await this.mutate(() => this.dependencies.finish(this.appid, plan.plan_id, readback!));
    this.guard();
    this.state.pending = null;
    this.state.pendingReady = false;
    this.state.pendingAbortable = false;
    this.state.success = true;
  }

  async preview(packageDir: string, action: XmxAction, profile?: XmxProfile) {
    if (this.state.busy || this.state.pending || this.state.recoveryBlocked) return;
    this.state.busy = true;
    this.state.error = null;
    this.state.preview = null;
    this.state.success = false;
    try {
      this.guard();
      const options = await this.dependencies.readLive(this.appid);
      this.guard();
      if (options === null) throw new Error("Live active-client launch options are unavailable. No changes were made.");
      const plan = await (profile === undefined
        ? this.dependencies.plan(this.appid, packageDir, options, action)
        : this.dependencies.plan(this.appid, packageDir, options, action, { ...profile }));
      this.guard();
      if (!isXmxPlan(plan) || plan.old_launch_options !== options) {
        throw new Error(plan?.error || "Invalid preview or different live launch options. Nothing was applied.");
      }
      this.state.preview = { ...plan, action };
    } catch (error) {
      this.state.error = String(error);
    } finally {
      this.state.busy = false;
    }
  }
}
