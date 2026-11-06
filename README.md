# Decky XeSS XMX

A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for **Intel Arc
handhelds**: real Intel **XeSS super resolution and frame generation running on the XMX matrix
units** inside Proton games — per game, from Game Mode, with undo. On top of it, an
**FSR → XeSS bridge** (a game that only speaks FSR 3.1+ ends up on XeSS on XMX, multi-frame
generation included), and the full [OptiScaler](https://github.com/optiscaler/OptiScaler)
manager parked behind an explicit button for the games XeSS XMX doesn't serve.

> ## ⚠️ Experimental — and, honestly, sloppy
>
> This plugin is **unofficial, experimental, and held together with visible duct tape**. It
> patches driver files inside your **Proton prefixes**, rewrites **Steam launch options**, and
> hooks **Proton installs** — the sort of thing that occasionally eats things. **Prefix
> deletion or corruption is to be expected**; it is part of how this works, not a surprise.
>
> **Use at your own risk. We are not responsible for lost saves, broken prefixes, ruined
> settings, or anything else this does to your machine.** Back your saves up; a cloud sync of
> a broken state is still broken. The plugin keeps backups and journals for its own writes and
> refuses to touch files it didn't create — that limits the damage, it doesn't remove the risk.
>
> Not affiliated with, endorsed by, or supported by Intel, AMD, Valve, or any project credited
> below. Report issues [here](https://github.com/yoyossef/decky-xess-xmx-linux/issues) — never
> to those projects' maintainers.

## What it does

### XeSS on XMX, per game

- **Native XeSS SR + frame generation on Intel XMX** through the
  [xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux) package — the patched Mesa
  ANV driver, the `igdext64.dll` shim, `ControlLib.dll` and the CM-kernel toolchain —
  installed, verified and **scoped to the games you pick** instead of applied system-wide.
- **Transactional everything.** Every change is previewed file-by-file (add / replace /
  restore, with hashes), applied, and finished as one step. Interrupted operations keep a
  **recovery journal** and the panel tells you exactly how to resume or abort.
- **Per-game Mesa variant** (e.g. 26.1.2 vs the newer 26.3 release candidate), alternate Intel
  extension discovery, optional diagnostic logs — chosen per game, visible as a version badge
  in the library.
- **It knows its own files.** Everything the plugin creates is tracked; anything it didn't
  create is never overwritten or deleted. A game whose prefix got rebuilt underneath the
  plugin re-enables cleanly instead of deadlocking.
- **Launch options are borrowed, not owned.** Whatever the game's launch options were before
  are recorded and offered back on disable — restore exactly, remove only what we added, or
  leave alone.
- **A nuke that works when nothing else does.** The Danger-zone nuke restores the prefix to its
  pre-plugin state and the launch options to what they were — *especially* when the install
  state is corrupted, which is exactly when you need it. It always tars a backup first, and it
  never deletes a foreign file, only leaves it.
- Existing script-managed XeSS installs (`install.sh`-style, system-wide) are detected and
  **blocked**, not migrated silently. The initial GPU allowlist is Intel PCI `0xb080` (OXP3,
  Arc B390); other devices are unverified, not assumed supported merely because they are Intel.

### FSR → XeSS bridge

- One-time, **upstream** Proton hooks (the xess-xmx-linux `install.sh`, run unmodified against
  SHA-pinned helpers), then per game: `FSR=xess` makes the game's FSR 3.1+ upscaling run on
  XeSS on XMX; `FSR_FG=xess` adds XeSS frame generation.
- **Reports, never adopts.** FSR-capable games are detected and listed; none gets bridged
  without an explicit, previewed request.
- Stale-hook detection with an in-panel repair: move the package and the plugin notices your
  games silently stopped being bridged — and says so instead of letting you debug it.

### XeSS library DLL

- Cache a pinned official Intel XeSS SDK, swap **one** selected game's x86-64 DX12
  `libxess.dll`, or restore the original. SDK tag and embedded library version shown
  separately. Swap and restore are independent, individually previewed transactions.

### Steam library integration

- All Steam libraries (SD card included), non-Steam shortcuts, custom folders; the right
  executable is scored out of the candidates (Unreal's `Binaries/Win64`, Cyberpunk's
  `bin/x64`) with an override when the game disagrees.
- The **Set up** filter tracks the games XeSS XMX is configured for. Each one wears badges in
  the list — **XMX** (blue), the **Mesa build** (red), **FSR 4 Bridge** (red, when live) — and
  the game page header reflects the same state.
- An **“XeSS XMX Settings”** entry in the Steam library context menu opens the game's page
  directly.
- Enable, disable or nuke anywhere refreshes badges and lists **live**, no page reload.

### Diagnostics

- Per-game log panel; the running backend, GPU and Proton version are read out rather than
  assumed; status banners show the real backend error instead of a shrug.

### OptiScaler manager (hidden on purpose)

This fork began as [Decky OptiScaler](https://github.com/danielcamilo1/decky-optiscaler) by
danielcamilo, and its complete manager is still in there — **behind an explicit OptiScaler
button** on each game's Setup tab, because it serves the games XeSS XMX doesn't. OptiScaler
v0.9.4 is bundled, so setup works offline and every game gets the same build:

- **Wiki-driven setup.** Your game is matched against the OptiScaler
  [Compatibility List](https://github.com/optiscaler/OptiScaler/wiki/Compatibility-List)
  (bundled copy + cached refresh), and setup becomes a checklist that says which wiki field
  each line came from before it writes anything.
- **Basic or Advanced settings.** A handful of gamepad-friendly controls, or all 288 settings
  across 34 sections generated from the reference INI's comments; edits preserve every comment.
- **Live in-game control.** A bundled ASI plugin lets the Quick Access panel change frame
  generation, the upscaler, the FidelityFX FG version and the FSR version mid-game, next to
  both frame rates — the overlay's powers, from the QAM.
- **FSR 4.1.1b on the original Steam Deck**, file backup/restore, [OptiPatcher](https://github.com/optiscaler/OptiPatcher)
  bundled. See [docs/steam-deck-fsr4.md](docs/steam-deck-fsr4.md) and the upstream project's
  docs; that contract is unchanged.

## Requirements

- **Decky Loader** on a native Steam install — the Flatpak Steam is not supported.
- An **Intel GPU with XMX** on the `xe`/`i915` kernel driver: Xe3 (validated on the ONEXPLAYER
  3, Arc B390), Xe2 (user-validated on Arc B580), Xe-HPG (user-validated on Arc A310). Meteor
  Lake has no XMX units; other Intel GPUs are unverified, not assumed working.
- An extracted [xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux) release package,
  which **you** download. The plugin never downloads the package or any driver — it validates
  the one you point it at against pinned SHA256s, then installs it per game.
- One of `7z`, `7zz`, `7za`, `7zr` or `bsdtar` for the bundled OptiScaler release (SteamOS has
  both p7zip and bsdtar; it runs once, on first install).
- Games on their **DX12 renderer** — XeSS/FSR is selected in each game's own graphics settings.

## Installing the plugin

1. Grab the plugin zip from this repository's [releases](https://github.com/yoyossef/decky-xess-xmx-linux/releases)
   (or build it, below — `pnpm build && python3 scripts/package.py` writes
   `out/Decky XeSS XMX.zip`).
2. Either:
   - In Game Mode, open Decky's settings, turn on **Developer mode**, and use
     **Install Plugin from URL** with the zip's download link; or
   - unpack the zip into `~/homebrew/plugins/` (folder name `Decky XeSS XMX`) and restart
     Decky Loader.
3. Download and extract an xess-xmx-linux release somewhere it can stay — then point the
   plugin at it (next section). Nothing on this list happens automatically, on purpose.

## Setting a game up

1. Open the game in the plugin (Games tab, or **“XeSS XMX Settings”** in its library context
   menu).
2. On **Setup**, point the XeSS XMX panel at your extracted `xess-xmx-linux` package — it is
   verified against pinned checksums before anything else happens.
3. Pick the Mesa variant for this game, review the **preview** — every file, every launch
   option, before it is written — and confirm.
4. Launch the game, select **XeSS** on its DX12 renderer. The first launch compiles the game's
   kernels once (a minute or two); after that they come from `~/.cache/xess-xmx`.
5. Game without XeSS? Install the **FSR bridge** once from the Setup tab, then toggle the
   bridge on that game and pick FSR in the game.

**Disable removes exactly what enable added** — files and launch options — and the **nuke** is
there for when state goes sideways. Disable XeSS XMX per game *before* removing the plugin or
deleting its settings: the per-game launch wrapper and recovery state live in the plugin
settings directory.

## Screenshots

<!-- TODO(yous): Quick Access panel over a running game — XMX active, frame rate visible -->
![XeSS XMX over a running game](assets/quick-access.jpg)

<!-- TODO(yous): the Games list with the Set up filter and the XMX / Mesa / FSR 4 Bridge badges -->
![Set up filter with badges](assets/setup-list.jpg)

<!-- TODO(yous): a game's Setup tab with the XeSS XMX panel and the file-by-file preview -->
![Setup and preview](assets/setup-preview.jpg)

<!-- TODO(yous): the library context menu with the "XeSS XMX Settings" entry -->
![Library context menu](assets/context-menu.jpg)

<!-- TODO(yous): the FSR bridge panel — hooks installed, one game bridged -->
![FSR 4 Bridge](assets/fsr-bridge.jpg)

<!-- TODO(yous): OptiScaler manager behind its button (advanced settings / live control) -->
![OptiScaler manager](assets/optiscaler.jpg)

## Building

```sh
pnpm install
python3 scripts/generate_schema.py   # option metadata, from the reference INI (OptiScaler half)
python3 scripts/fetch_compat_seed.py # refresh the bundled compatibility list
./asi/build.sh                       # live-control ASI -> bin/ (zig cc; no MSVC needed)
pnpm build
python3 scripts/package.py           # -> out/Decky XeSS XMX.zip
```

Tests — the shipping chain, RPC hops included, is covered headlessly:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' # backend, bridge, nuke, RPC surface
python3 scripts/selftest.py                            # OptiScaler backend, synthetic library
HARNESS=run.tsx node testharness/build-and-run.mjs     # the whole UI in headless React
HARNESS=all-options.tsx node testharness/build-and-run.mjs
```

The transaction/recovery/RPC contract in detail: [docs/xess-xmx.md](docs/xess-xmx.md).

## Support

This plugin is free and open source and will stay that way. The OptiScaler management half it
grew out of is maintained by danielcamilo — [his Ko-fi](https://ko-fi.com/danielcamilo) is
where thanks for that work belong.

## Credits

All of the actual magic belongs to other people. This plugin only orchestrates their work from
the Decky UI:

- [xess-xmx-linux](https://github.com/HHHHanasak1/xess-xmx-linux) (MIT) — the reason XeSS runs
  on XMX under Proton at all: the `igdext64.dll` shim (built on Intel's open-source
  [dxvk-igdext](https://github.com/GameTechDev/dxvk-igdext)), the patched Mesa ANV driver that
  compiles XeSS' own CM kernels with Intel's IGC, `ControlLib.dll`, the FSR→XeSS loader shims,
  the Proton hooks, and the Mesa 26.3 “Jay” patches by @kas5852. The shims and drivers are
  their files, run **unmodified** — this plugin validates and scopes them.
- [Decky OptiScaler](https://github.com/danielcamilo1/decky-optiscaler) by danielcamilo — the
  plugin this fork is built on, and the author of everything behind the OptiScaler button.
- [OptiScaler](https://github.com/optiscaler/OptiScaler), its
  [wiki](https://github.com/optiscaler/OptiScaler/wiki) contributors, and
  [OptiPatcher](https://github.com/optiscaler/OptiPatcher).
- **nitec** and the OptiScaler/XMX modding community — the FSR 4-on-XMX INT8 work that made
  “FSR 4 on Intel matrix cores” a thing.
- [ClawDeck](https://www.getclawdeck.com/) (MIT) — per-game AI-upscale and XeSS
  frame-generation UX on Windows handhelds; an inspiration for parts of this plugin's setup
  flow, not a code source.
- [Intel](https://github.com/intel/xess) for the XeSS SDK and
  [AMD](https://gpuopen.com/fidelityfx/) for the FidelityFX loader and providers — releases
  bundled or fetched by the upstream package, each under their own licenses.
- [wimlib](https://wimlib.net/) (LGPL) — handles the Windows imaging formats the driver
  toolchain touches.
- [Decky Framegen](https://github.com/xXJSONDeruloXx/Decky-Framegen) — reference for how an
  upscaler gets deployed into a game folder on Linux.
- [Decky LSFG-VK](https://github.com/xXJSONDeruloXx/decky-lsfg-vk) — reference for the
  configuration-driven plugin UI.
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader), and SteamGridDB's
  context-menu patch as the reference the library shortcut follows.

## AI disclosure

This fork was built with heavy use of AI agents (Claude, Hermes). Every design decision,
review, and testing run was human, and everything shipped here was run on an ONEXPLAYER 3
(Arc B390) — which is also how we know it's experimental.
