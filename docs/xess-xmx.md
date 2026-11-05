# Native-game XeSS XMX

This backend is independent of OptiScaler. Prefix support uses a user-selected,
already extracted `xess-xmx-linux` package. It never edits Steam VDF, migrates
legacy installs, or changes system Mesa/Proton files. The separate, explicit
XeSS DLL manager below can download a pinned official SDK into plugin settings
and update one selected game DLL after a preview. Prefix disable and game-DLL
restore are independent transactions.

## Per-game compatibility profiles

`plan_xmx` accepts `profile = {driver, discovery, diagnostics}`:

- `driver`: `default`, `26.1`, or `26.3`. Explicit variants are offered only when
  statically validated in the selected package; ambiguity is refused. Explicit
  selection pins the real driver in the per-game ICD and sets `MESA` in the
  per-game config. The package's global `lib/active` is never changed.
- `fg`: integer `0`, `2..8` — XeSS frame generation multiplier override
  (`XESS_FG` + `IGDEXT_XELL_LOG=1` in the per-game config; the prefix ControlLib
  answers the game). `0` leaves the game's own choice. `5..8` is the upstream
  package's experimental shim, exposed only in Details. Records stored before
  frame generation normalize to `fg: 0`; trial import recognizes an exact
  `XESS_FG=<n>` prefix (with or without the log flag) and keeps the multiplier.
  Low latency (XeLL) is the upstream default while frame generation runs; the
  panel's logs read `drive_c/igdext_xell.log` as evidence, never as runtime proof.
- `bridge`: `0` off, `1` = `FSR=xess` (the game's FSR 3.1+ upscaling and frame
  generation run on XeSS), `2` = `FSR_FG=xess` (only frame generation on XeSS,
  the game's own FSR upscaler stays). `sdk_latest`: adds `XESS=latest`, Intel's
  SDK libraries instead of the game's older ones. The upstream Proton hook reads
  these from the game's environment at each start and swaps the FSR DLLs into
  the package's XeSS-backed ones only while the game runs (reverted on exit).
  Requires the hooks transaction: dedicated **FSR Bridge** tab — install runs
  upstream `install.sh` as the desktop user, then parks its session-wide driver
  file (`.decky-fsr-bridge`) so only toggled games load the patched driver.
  Uninstall needs the typed token `uninstall-hooks` and refuses while any game
  is bridged. Games detected but not managed are reported, never adopted.
- `discovery`: `auto` or `alternate`. Alternate sets the exact per-game
  `INTC_ALT_DRIVER_EXTENSIONS_PATH` DriverStore path without registry edits.
- `diagnostics`: strict boolean; when enabled the app config adds `PROTON_LOG=1`,
  `IGDEXT_TRACE=1`, and `ANV_CM_DEBUG=1`. Off by default. Existing user launch
  customizations remain preserved; the plugin does not clear unrelated user flags.

An enabled app can preview `action='configure'` with its exact managed launch
string. `action='adopt_trial'` explicitly promotes a recognized temporary prefix
of Mesa/discovery/diagnostic assignments, removes those temporary flags from the
Steam launch string and records the selected profile. Unexplained edits are
refused. If no profile is supplied, trial recognition retains its driver and
discovery, with diagnostic verbosity disabled. Preview returns the exact profile,
selected driver and before/after SHA-256 for each prefix/config file operation.

Configure/promotion use the same journal/readback protocol as enable. Their
pending phases are `pending_configure` and `pending_adopt_trial`. Abort restores
the immediately previous installed files/record and launch string. Commit retains
the **first installation's** backups and original launch options for eventual
disable; repeated profile changes must not replace these originals with the
previous managed config.

`get_xmx_status` adds `profile` and `drivers` (IDs, labels, validation and reasons).
These are configuration/static checks, never current-game rendering proof.

## Independent game XeSS DLL update/restore

- `prepare_xess_sdk()` explicitly prepares only the plugin-owned cache, with
  pinned official Intel release `v3.0.2`, archive SHA-256 and DLL SHA-256. The
  library's embedded version is **2.0.2.68**, not the SDK tag. No automatic
  game update or latest-release tracking is performed.
- `get_xess_dll_status(appid)` lists native DLL candidates, architecture, embedded
  version and hashes, SDK cache readiness, independent ownership/pending state,
  and a recognized prior Hi-Fi RUSH trial when available.
- `plan_xess_dll(appid, target_path, launch_options, action='enable', adopt_trial=False)`
  requires an explicit absolute candidate target, an installed Steam game and a
  stopped existing prefix. Replacement supports only PE64 x86-64 **DX12
  `libxess.dll`**. `libxess_dx11.dll` is detected but not overwritten with the DX12
  library. Static export checks are preliminary, not ABI/runtime certification.
- `apply_xess_dll`, `finish_xess_dll`, and `recover_xess_dll` use their own journal
  under `xess-xmx/dll-apps/<appid>/`. They reuse pinned file operations, safe
  metadata restoration, global mutation lock and exact client readback. Their
  old/new launch strings are identical: DLL operations never dispatch Steam changes.
- `action='disable'` restores the separately backed-up original DLL only after
  target/backup verification and exact unchanged Steam-option confirmation.
  Updates by Steam or the user are conflicts, not permission to overwrite them.
- Explicit import of the separately approved Hi-Fi RUSH trial validates its exact
  manifest, source hashes, target and original metadata. Abort returns the
  immediate pre-import DLL; eventual restore returns the pre-trial original.
  The external trial backup is retained. Other legacy installer state is not adopted.

Pending operations in either scope block starting work in the other. Disabling
prefix XMX **does not restore the game DLL**; restoring a game DLL does not disable
prefix XMX. Keep the game and other writers stopped throughout both kinds of
transaction. No SDK DLL is bundled in the plugin artifact.

## Diagnostic log view

`get_xmx_logs(appid)` reads only the default per-app Proton log, the prefix IGDEXT
trace and shared compiler log. It refuses links/special files, checks target-user
read permission, bounds bytes and retains startup plus tail. The frontend further
bounds the rendered view. Reads are on request, not polling; hiding text does not
delete logs. App switches cannot reuse old app results. Logs can contain private
paths/game details. Appended shim sessions and shared cached compiler successes
are **not** automatically assigned to the current game or converted into a
runtime-verified badge.

## Frontend RPC contract (original prefix protocol)

Every RPC returns `{ok: boolean, error?: string, ...}`; exceptions are translated
into `ok: false`. App IDs are canonical positive decimal strings.

* `get_xmx_status(appid, package_dir='')`: returns `installed`, `legacy`,
  `phase` (`disabled`, `enabled`, `pending_enable`, `pending_disable`, or
  `recovery_required`), `runtime_verified: false`, `suggested_package`, `package`
  diagnostics, `gpu`, `native_xess`, `flatpak`, and `pending` (object or null).
  `installed` means a committed manager record, not runtime compatibility. Without
  an explicit package path, diagnostics use the pending/installed package path
  before the suggested default.
* `plan_xmx(appid, package_dir, launch_options, action='enable')`: launch options
  MUST be the exact active-client string (not a VDF fallback). Returns `plan_id`,
  `old_launch_options`, `desired_launch_options`, and `operations`. A persisted
  single-use plan binds the app, prefix, original options, file/source hashes,
  and installation generation. Disable passes `action='disable'`; package_dir
  is ignored for disable. No prefix files are changed by planning.
* `apply_xmx(appid, plan_id)`: revalidates the plan, consumes its token, writes a
  durable write-ahead journal, then installs enable files. Returns
  `desired_launch_options`, `old_launch_options`, `pending_launch: true`.
  Disable only preflights and journals here: it DOES NOT remove any files yet.
* `finish_xmx(appid, plan_id, launch_options)`: pass the exact string re-read
  from the active Steam client AFTER setting the apply result's desired options.
  A mismatch fails without committing/removing files. Enable commits only after
  exact confirmation; disable restores owned files only after confirmation.
* `recover_xmx(appid, plan_id, launch_options, action='abort')`: explicit recovery
  after reload/crash or a failed client-options update. First set the pending
  record's `old_launch_options` in Steam and re-read it exactly, then call with
  `action='abort'` to roll back file changes (or cancel a pending disable).
  `action='resume'` is equivalent to `finish_xmx` and requires the desired string;
  a partially-written enable must be aborted, not resumed. Once disable restoration
  has started (`pending.phase == 'restoring'`), abort is refused: re-confirm the
  desired restored options and resume instead. Conflicts fail closed and preserve
  the journal/backups. Recovery never edits Steam itself.

UI sequence: read active options → plan → present operations → apply → set active
options → read active options back → finish. Re-read immediately before apply;
if it differs from plan.old_launch_options, do not apply (create a fresh plan).
The backend cannot independently observe active Steam client options. Never use
an offline VDF read as successful client confirmation. On reload inspect pending
before allowing another plan. `pending` includes `plan_id`, `action`,
`old_launch_options`, `desired_launch_options`, and `phase`.

Legacy `~/.local/state/xess-xmx-anatase/*/state.json` is detected and blocks this
manager for the corresponding app; it is never adopted, modified or removed.
This refusal is rechecked at apply, finish and abort, not just during planning.
If a legacy manifest appears while pending, the journal and managed files remain
untouched for explicit conflict resolution.
`runtime_verified: false` is intentional: static package/GPU checks and sandbox
visibility are not proof that a game successfully executed XMX kernels.

## Exact response details

Plan `operations` is a list of `{target: absolute_path, operation: 'install' |
'restore'}`. Enable lists five files; disable lists the corresponding restores.
Apply also returns `plan_id`, `action`, and `phase` (`pending_enable` or
`pending_disable`). Finish/recover success returns `installed`, `phase`, and
`pending_launch: false`. A finished/aborted token cannot be applied again.
`pending.phase` preserves journal detail (`writing_enable`, `pending_enable`,
`pending_disable`, or `restoring`); the outer status maps interrupted writes or
restores to `recovery_required`. On **any apply/finish/recover error**, fetch status
before another attempt; the journal may already be pending. There is no implicit
backend retry that edits launch options.

Enable inserts its wrapper at the single `%command%` placeholder, keeping leading
environment assignments and existing arguments intact. With no placeholder, old
options remain arguments after a new wrapped `%command%`. A placeholder must be
one standalone, unquoted shell word. Only unquoted, unescaped ASCII space or tab
separates words; escaped blanks and nonbreaking spaces do not. Quoted, escaped,
concatenated or commented-out placeholders are refused, as are multiple
placeholders, NUL, LF and CR. The v1
scanner rejects shell comments, all backticks (even quoted literals), unquoted
control operators (`; | & < > ( )`), active `$()`/`${}` substitutions, and unbalanced
quotes/trailing escapes across the **whole** string. It does not implement or
repair general shell syntax. Ordinary environment assignments and quoted values
such as `WINEDLLOVERRIDES="dinput8=n,b;dxgi=n,b"` remain supported; quoted semicolons
are not treated as control operators. Original options are retained
verbatim for restoration, including an explicitly empty string.

## Durable state and ownership

All manager state lives under `<settings_dir>/xess-xmx/`, never runtime_dir:

```
.lock
apps/<appid>/
  generation.json
  plans/<plan_id>.json
  journal.json          # only while pending/recovery is needed
  installed.json        # only after commit, removed after successful disable
  backups/<enable_plan_id>/<file_index>
  launch.sh
  xess.conf
  intel_icd.json
```

The last three files are enable assets and are removed/restored on disable.
Plans and backups are retained as durable recovery/audit evidence; do not delete
settings while enabled or pending. Disable before uninstalling the plugin.

Only these prefix DLL targets are managed:

```
<pfx>/drive_c/windows/system32/driverstore/filerepository/igd_faux.inf_1/igdext64.dll
<pfx>/drive_c/windows/system32/ControlLib.dll
```

The wrapper sets `XMX_CONF` to this app's config and invokes the selected package's
`xmx-launch.sh`. Config sets `VK_DRIVER_FILES` to the per-app ICD,
`VKD3D_DISABLE_EXTENSIONS=VK_EXT_descriptor_buffer`, `ANV_CM_KERNEL_DIR` and
`ANV_CM_HELPER` to package paths, and `force_vk_vendor=0`. The ICD points to the
absolute package dispatcher, with file format `1.0.1`, API `1.4.348`, and 64-bit
library architecture. No user's global Vulkan config is modified.

The service serializes mutations with `_mutation_lock` and runs blocking work via
`asyncio.to_thread`; the manager also uses a nonblocking cross-process flock.
Cancelling an RPC does not stop its filesystem thread, so the service drains that
worker before releasing the mutation lock (including repeated cancellation).
The lock itself must be a single-link regular file, not a hard link to another
user file whose permissions a root backend could otherwise change.
Every plan binds source-tree hashes, original file hashes/metadata and a durable
installation generation. Applying one plan invalidates competing previews even
if the operation is later aborted. File snapshots record bytes, mode, UID/GID,
atime/mtime and xattrs. Restoration checks bytes, mode, UID/GID, mtime and the exact
xattr set, removing unwanted inherited staging attributes (including default-ACL
access entries) before replacement. If the filesystem does not allow an attribute
to be removed/restored, restoration fails with its journal/backups retained, not
with a false success. Recorded atime is reapplied, but verification or other reads
can advance it; atime is not a conflict/progress discriminator or an exact final
atime guarantee. Ordinary user-owned metadata is restorable; set-ID/sticky
bits, groups the target user does not belong to, capabilities and trusted/unknown
xattr namespaces are refused before applying. Supported xattr namespaces are
`user.*`, POSIX access ACLs and SELinux labels. Directory creation is journaled and
only newly-created, still-empty directories are removed. Backups and the
write-ahead journal are fsynced before prefix replacement; directory entries are
fsynced too. Each replacement's exact hash/UID/GID/mode/mtime/xattrs are captured
from its staging FD and durably journaled **before rename**, including inherited
ACL/SELinux attributes. Commit, disable and abort check that metadata, not just
bytes and executable bits. A target without a durable replacement snapshot is
never adopted merely because a later external file has the same payload bytes.
Recovery infers original/installed progress from snapshots so a crash between
rename and the next phase update is not mistaken for a completed transaction.
Restore preflights the whole set, then rechecks each file and original backup.
Enable and restore perform an expected-state destination check through the pinned
parent FD immediately before replacement, **after staging, fsync and callbacks**;
deletes likewise check through their pinned parent immediately before unlink.
Edits observed at these checks are retained as conflicts, with the original
backup (when one existed) and journal, even when earlier operations succeeded.
Prefix staging filenames are also
journaled and cleaned only when their ownership/content match
the recorded operation. Unknown/changed staging files are retained as conflicts.

Mutable paths are allowlisted against the currently discovered Steam prefix;
symlinked mutable components, relocated prefixes, ambiguous duplicate prefixes,
foreign/root-owned library/prefix roots, edited files, and corrupt backups fail
closed. Atomic writes and deletes pin directory FDs with `O_NOFOLLOW` rather than
following a parent symlink swapped after validation. File snapshots, digests,
state/plan reads, package header checks and copied package DLLs also pin no-follow
FDs; permitted internal source symlinks are resolved and confined to the selected
package before opening. Source hashes bracket payload reads so a package revision
change cannot silently bind different DLL bytes to a plan. Additional Steam
libraries come from `steam.library_folders(home)`; there are no fixed user home paths.
The configured settings path is **not** resolved through symlinks and adopted as
a new writable boundary: settings and ancestor symlinks are refused during pinned
traversal. Only the explicitly supplied home path is canonicalized as a trusted
home alias; settings lexically beneath that home are bound beneath its canonical
home. Mutable descendants still cannot contain symlinks.
New state/prefix files, directories and the lock are explicitly owned by the
non-root target home user, even when Decky's backend is root. Pinned traversal
checks target-user search permission, and checks write/search at the nearest
existing parent **before creating** a missing directory, as well as write/search
on the file-creation parent. Inaccessible paths are refused, not chmodded globally.
Package architecture/dependency and
target-user access checks run again before enable apply and commit, so root's
access does not conceal permission revocation since preview. Static access checks
include POSIX named-user/group ACL entries and their masks, not only mode bits.
They do not certify SELinux policy decisions or sandbox access. Every mutation
phase also checks the target user's process environments for the app/prefix and
refuses a running game or an unreadable environment that prevents establishing
it stopped.
These checks and the manager lock are not a filesystem transaction or atomic
compare-and-swap against another writer. Ordinary Linux rename/unlink do not bind
the destination's snapshot to the syscall: a hostile/concurrent write after the
last check can still be overwritten/deleted, and there is no displaced-version
capture for that final window. Journaled backups preserve the pre-install
original, not every concurrent revision. The lock only coordinates cooperating
manager instances. Keep the game **stopped**, and stop other prefix/settings
writers, throughout planning/apply/confirmation/recovery. A game can also start
immediately after a process check; the backend cannot prevent that.

## Static readiness is not a successful runtime

* Require a detected native `libxess.dll` or `libxess_dx11.dll` in the installed
  Steam game. This is a candidate only: DLL presence does not prove native-game
  support or distinguish every third-party modification. No game DLL is changed.
* The conservative v1 GPU policy accepts Intel vendor `0x8086`, device `0xb080`.
  Other Intel IDs are **unverified**, not assumed XMX-capable from vendor alone.
  Only full `card[0-9]+` sysfs entries are scanned, not connector names.
* Package diagnostics require PE64 x86-64 shim/ControlLib DLLs, ELF64 x86-64
  dispatcher **and a separate selected Mesa driver** (not a hard link to the
  dispatcher), executable target-user helpers, nonempty `cmk_pack.py`,
  kernels/bundled directories, and actual ELF
  compiler dependencies (`libigc`, `libigdfcl`, `libiga64`, `libopencl-clang`,
  `libocloc`, and executable `ocloc*` in the package's IGC/NEO trees). A script
  filename or tiny dispatcher by itself is not a complete package. Internal
  package symlinks are supported; escaping source paths and special files anywhere
  in the selected tree are refused in diagnostics as well as planning. `lib/active`
  may be an internal directory symlink or a version-selector file such as
  `26.3.0-rc2` selecting `lib/mesa-26.3.0-rc2/`.
* These are static architecture/dependency checks, not dlopen, compiler execution,
  package authenticity checks, or game compatibility certification. No package
  executable is ever run by the backend. Use a trusted user-selected package.
* `flatpak.visible` is deliberately `null`, with a reason: this backend does not
  run a root-owned `flatpak` probe and cannot certify the target user's sandbox
  visibility of package, prefix and settings paths. No Flatpak permissions are
  changed. Visibility and the actual game-driver/CM-helper runtime need separate
  user-context verification. No runtime XMX proof is inferred from static files,
  logs being absent, or merely setting launch options.
* Legacy manifests are read only for blocking detection. A malformed legacy record
  blocks conservatively rather than being treated as evidence that no install
  exists. There is no adoption, migration, FSR4 setup or translation-layer install.

## Offline verification

Run `python3 scripts/test_xmx.py`; fixtures are created only under `$TMPDIR`
(defaulting to the Hermes scratch directory). The suite uses fake PE/ELF fixtures
for validation, exercises real prefix/state filesystem transactions, checks
service/main RPC forwarding and error handling, actually executes the generated
shell wrapper with a harmless local Python command, and forks child processes
that exit abruptly before atomic renames, after each of the five managed enable
renames, with inherited staging xattrs, during disable, after restoration rename
under a real parent default ACL (disable resume and enable abort), and after the enable
commit record becomes durable. Restarted managers exercise abort/resume, and the
suite covers configured-settings/ancestor symlinks, explicit home aliases,
transient source/target symlinks, same-target staging-time edits in enable,
disable and abort, guarded deletion, shell comment/backtick regressions,
metadata/backup conflicts, package/access revocation, legacy refusal and RPC cancellation.
It does not launch Steam, a game, a compiler, or the real package. Permission tests
include a real UID-0 **user-namespace** subprocess chrooted only into its scratch
fixture: UID-1000 target-user mode/ACL write denial is checked before settings
creation, then a writable parent verifies target ownership. This requires Linux
`unshare`, subordinate UID/GID mappings and user namespaces. Other permission
tests model root-only visibility explicitly. Namespace-root checks are not a
host-root Decky runtime test, Flatpak visibility proof or successful XMX execution.
