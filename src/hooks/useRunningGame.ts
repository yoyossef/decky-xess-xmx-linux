import { Router } from "@decky/ui";
import { useEffect, useState } from "react";
import type { RunningGame } from "../types";

const POLL_MS = 4000;

function fromOverview(overview: unknown): RunningGame | null {
  if (!overview) return null;
  const app = overview as { appid?: number; display_name?: string; gameid?: string };
  if (typeof app.appid !== "number") return null;
  return {
    appid: app.appid,
    name: app.display_name || `App ${app.appid}`,
    gameid: app.gameid || String(app.appid),
  };
}

function currentApp(): RunningGame | null {
  try {
    return fromOverview(Router.MainRunningApp);
  } catch {
    return null;
  }
}

function describeAppId(appid: number): RunningGame {
  try {
    const overview = fromOverview(appStore?.GetAppOverviewByAppID?.(appid));
    if (overview) return overview;
  } catch {
    /* non-Steam shortcuts have no overview */
  }
  return { appid, name: `App ${appid}`, gameid: String(appid) };
}

/**
 * The game Steam currently has running.
 *
 * Lifetime notifications only fire on change, and this panel is usually opened
 * while a game is already up, so the current app is read on mount and polled as
 * a safety net.
 */
export function useRunningGame(): RunningGame | null {
  const [game, setGame] = useState<RunningGame | null>(() => currentApp());

  useEffect(() => {
    let registration: Unregisterable | undefined;
    try {
      registration = SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications(
        (notification) => {
          if (notification.bRunning) {
            setGame(describeAppId(notification.unAppID));
          } else {
            setGame((current) =>
              current && current.appid === notification.unAppID ? currentApp() : current
            );
          }
        }
      );
    } catch {
      /* not running inside the Steam client */
    }

    const poll = window.setInterval(() => {
      const running = currentApp();
      setGame((current) => {
        if (running?.appid === current?.appid) return current;
        return running;
      });
    }, POLL_MS);

    return () => {
      registration?.unregister();
      window.clearInterval(poll);
    };
  }, []);

  return game;
}

export function restartGame(game: RunningGame) {
  try {
    SteamClient.Apps.TerminateApp(game.gameid, false);
    window.setTimeout(() => {
      try {
        SteamClient.Apps.RunGame(game.gameid, "", -1, 100);
      } catch {
        /* the user can relaunch manually */
      }
    }, 2500);
    return true;
  } catch {
    return false;
  }
}

/** Exact active-client custom launch options, or null when unavailable. */
export async function readLaunchOptions(appid: number): Promise<string | null> {
  let apps: {
    GetAppLaunchOptions?: (id: number) => Promise<unknown>;
    RegisterForAppDetails?: (
      id: number, callback: (details: { unAppID?: unknown; strLaunchOptions?: unknown }) => void
    ) => Unregisterable;
  } | undefined;
  try {
    apps = SteamClient?.Apps as unknown as typeof apps;
    if (apps?.GetAppLaunchOptions) {
      const value = await apps.GetAppLaunchOptions(appid);
      if (typeof value === "string") return value;
    }
  } catch {
    /* fall through to app details */
  }
  if (apps?.RegisterForAppDetails) {
    // Subscribe for every read, including post-write verification: the store can
    // be cold or stale. A failed subscription must not fall back to that cache.
    return new Promise((resolve) => {
      let registration: Unregisterable | undefined;
      let settled = false;
      const cleanup = () => {
        const current = registration;
        registration = undefined;
        try {
          current?.unregister();
        } catch {
          /* A client cleanup error must not strand the read or cause a retry. */
        }
      };
      const finish = (value: string | null) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        cleanup();
        resolve(value);
      };
      const timeout = window.setTimeout(() => finish(null), 2000);
      try {
        registration = apps.RegisterForAppDetails!(appid, (details) => {
          if (settled) return;
          try {
            if (typeof details?.unAppID !== "number" || details.unAppID !== appid
              || typeof details.strLaunchOptions !== "string") return;
            finish(details.strLaunchOptions);
          } catch {
            finish(null);
          }
        });
        // A synchronous callback runs before the registration handle is returned.
        if (settled) cleanup();
      } catch {
        finish(null);
      }
    });
  }
  try {
    // The store the Steam library's own Properties dialog reads its launch
    // options box out of, which is present on builds that have no getter.
    const details = (
      globalThis as unknown as {
        appDetailsStore?: { GetAppDetails?: (id: number) => { unAppID?: unknown; strLaunchOptions?: unknown } | null };
      }
    ).appDetailsStore?.GetAppDetails?.(appid);
    if (details?.unAppID === appid && typeof details.unAppID === "number"
      && typeof details.strLaunchOptions === "string") return details.strLaunchOptions;
  } catch {
    /* The active client cannot report options; callers must handle null. */
  }
  return null;
}

/**
 * When Steam last ran this game, as a unix time, or null if it will not say.
 *
 * `rt_last_played` is on Steam's own app overview rather than anything decky
 * declares, so it is read through a cast and every caller has to cope with
 * null: a library scanned from a custom folder has no overview at all.
 */
export function lastPlayed(appid: string | null): number | null {
  if (!appid) return null;
  try {
    const overview = appStore?.GetAppOverviewByAppID?.(Number(appid)) as unknown as
      | { rt_last_played?: number }
      | undefined;
    const value = overview?.rt_last_played;
    return typeof value === "number" && value > 0 ? value : null;
  } catch {
    return null;
  }
}

export function setLaunchOptions(appid: number, options: string) {
  try {
    SteamClient.Apps.SetAppLaunchOptions(appid, options);
    return true;
  } catch {
    return false;
  }
}
