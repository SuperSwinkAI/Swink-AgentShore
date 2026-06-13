# Build Pipeline Unification

Status: **Implemented** (branch `build-pipeline-unification`) · Owner: build/release ·
Supersedes the ad-hoc divergence between `scripts/build-macos.sh` and `scripts/build-windows.ps1`.

> Implementation note: the spine package shipped as `scripts/buildkit/` (not `build/`, which
> `.gitignore` swallows) with **flat** modules — `macos.py`, `windows.py`, `phases.py`,
> `verify.py`, `version.py`, `context.py`, `_proc.py`, plus the `_win_signing.ps1` carve-out —
> rather than the `phases/` + `platforms/` subpackages originally sketched below. The behaviour
> matches this design; only the file layout is flatter.

## Goal

One robust, unified, consistent, durable desktop build pipeline across macOS and Windows.
A single cross-platform **Python build spine** owns every phase that isn't genuinely
OS-native; thin per-OS shims keep only the native packaging/signing. Backed by CI compile
coverage, a single version source of truth, a structurally-isolated provisioner, and a
post-build verification gate that makes a "false green" impossible.

## Why (the durability gaps today)

| # | Gap | Evidence |
|---|-----|----------|
| 1 | macOS desktop build has **zero CI coverage**; no job compiles the Rust desktop crate on any OS | `ci.yml` runs only python/dashboard/desktop *unit tests*. This is why #105's provisioner compile error reached a local build. |
| 2 | Provisioner `[[bin]]` lives in the cross-platform app crate → Tauri bundler can fold a **stale** copy into the `.app` (false green) | `desktop/src-tauri/Cargo.toml:15-23`; band-aided with `required-features`. |
| 3 | No clean-build or **artifact verification** gate; runs inherit stale `target/` payloads | neither script verifies the produced payload manifest. |
| 4 | **Version duplicated in 5 files**, parsed with brittle `grep \| sed` | `tauri.conf.json`, `Cargo.toml`, `desktop/package.json`, `dashboard/package.json`, `pyproject.toml`; `build-macos.sh:279`. |
| 5 | Divergent staging logic that isn't *intentionally* divergent | macOS bundles bd + wheel-in-pkg; Windows skips bd (`build-windows.ps1:399`) + ships wheel/uv/provisioner via Inno. |
| 6 | **No toolchain pinning**; uv pinned on Windows only | `build-windows.ps1:107` pins `uv 0.8.11`; `build-macos.sh` pins nothing; no `rust-toolchain.toml`/`.nvmrc`. |
| 7 | `build.rs` bd-staging is an implicit compile side effect (shells out to `bd --version`, scans PATH) | `desktop/src-tauri/build.rs`. |

## Target architecture

```
scripts/
  build-macos.sh          # THIN shim (~25 lines): bootstrap uv, exec spine --target macos "$@"
  build-windows.ps1       # THIN shim (~30 lines): bootstrap uv, exec spine --target windows "$@"
  buildkit/               # the cross-platform spine (uv run python -m scripts.buildkit ...)
                          # (named buildkit, not build/, which .gitignore swallows)
    __main__.py           # arg parse, target dispatch, top-level error/exit handling
    context.py            # BuildContext: paths, version, mode(release/debug), flags
    version.py            # single-source version resolve + drift check / --write
    phases/
      clean.py            # kill processes + remove stale bundle/staging dirs (idempotent)
      dashboard.py        # npm run build + build:lib
      sidecar.py          # bd sidecar staging (macOS bundles; Windows install-time)
      wheel.py            # uv build --wheel into a staging dir
      tauri.py            # npx tauri build (-- --locked); provisioner build (separate crate)
      verify.py           # manifest + signature + embedded-version assertions
    platforms/
      macos.py            # pkgbuild/productbuild component pkgs, notarytool, codesign resolve
      windows.py          # Inno Setup compile, staging payload, signtool orchestration
      _win_signing.ps1    # PRAGMATIC carve-out: Windows cert-store/self-sign ops only
    manifest.py           # expected-payload spec + bundle/installer walker
```

**Why Python for the spine:** `uv` is already a hard build dependency on both OSes, the
team is Python-first, and the cross-platform phases (clean, version, dashboard, wheel,
Tauri invoke, verify) are identical logic that today exists twice. The spine calls native
tools (`pkgbuild`, `productbuild`, `codesign`, `notarytool`, `ISCC`, `signtool`) via
`subprocess`. The **only** code that stays shell-native is the Windows certificate-store /
self-sign logic (`New-SelfSignedCertificate`, `X509Store`), kept as a small `_win_signing.ps1`
helper the spine shells out to — porting that to Python buys nothing.

**Shim contract (unchanged UX):** `scripts/build-macos.sh` with no flags still produces the
signed `.app`/`.dmg`/`.pkg` and reveals it in Finder — preserving the CLAUDE.md "build = this
script" rule and all existing flags (`--skip-dashboard`, `--notarize`, `--no-sign`, …),
which the spine re-implements as argparse options.

## Phase contract

Every phase is a pure function `run(ctx: BuildContext) -> None` that:
- logs a single `==> Phase` banner (parity with today's `log()`),
- is **idempotent** and safe to skip via `ctx.skip_<phase>`,
- raises `BuildError` (non-zero exit, no masking) on failure — never relies on a pipe's exit.

Pipeline order (target-aware): `clean → version.check → dashboard → sidecar → wheel →
tauri → platforms.package → verify`. macOS adds notarize/install/reveal; Windows adds the
Inno compile + signtool sweep.

## Durability fixes (all four in scope)

### A. CI desktop compile gate *(highest value, smallest change — do first)*
Add to `ci.yml` a matrix job on `macos-latest` + `windows-latest`:
```yaml
desktop-compile:
  strategy: { matrix: { os: [macos-latest, windows-latest] } }
  runs-on: ${{ matrix.os }}
  steps:
    - checkout
    - uses: dtolnay/rust-toolchain@stable        # later: read rust-toolchain.toml
    - uses: Swatinem/rust-cache@<pinned-sha>
    - run: cargo build --locked --workspace --manifest-path desktop/src-tauri/Cargo.toml
      env: { AGENTSHORE_SKIP_BD_SIDECAR: "1" }    # don't require bd on the runner
```
This compiles the app crate **and** the provisioner crate on both OSes on every PR —
exactly the gate that would have caught #105. Required check.

### B. Provisioner → its own workspace crate
- Add `[workspace]` to `desktop/src-tauri/Cargo.toml` with members `["." , "provisioner"]`.
- Move `src/bin/agentshore_provisioner/`, `src/bin/agentshore-provisioner.rs`, and
  `install_layout.rs` into `desktop/src-tauri/provisioner/` as crate `agentshore-provisioner`.
- App crate: **delete** the `[[bin]]`, the `provisioner` feature, and `required-features`
  (`Cargo.toml:15-23,62-66`). The app crate now has exactly one binary → Tauri's bundler
  can never fold the provisioner into the `.app` again.
- Keep the `#[cfg(windows)]` / `#[cfg(not(windows))]` path wrappers (already committed in
  `8f81faf`) so the provisioner crate still **compiles** cross-platform under the new
  CI gate; only Windows packaging ships it.
- Build invocation becomes `cargo build -p agentshore-provisioner --release --locked`
  (drops the `--features provisioner` band-aid from `build-windows.ps1:468-470`).

### C. Version single source of truth + drift check
- Canonical: `pyproject.toml [project].version`.
- `scripts/buildkit/version.py` (the 5 mirrors include the new provisioner crate manifest):
  - `read_canonical()` → the one true version.
  - `find_drift()` → list every mirror != canonical; the CLI exits 1 with a `--write` hint on drift.
  - `write()` → rewrite all mirrors from canonical via targeted, formatting-preserving
    line replacement (used when bumping, e.g. `uv run python -m scripts.buildkit version --write`).
- Spine runs the check immediately after `clean`; CI adds a `verify-versions` step
  (`uv run python -m scripts.buildkit version --check`) to `ci.yml`'s python job, and a
  pytest guard (`tests/packaging/test_version_consistency.py`) enforces it on every run.
- Replaces every brittle `grep '"version"' | sed` (`build-macos.sh:279`, `build-windows.ps1:355-359`).

### D. Post-build verification gate
`phases/verify.py`, after packaging, on **both** platforms:
1. **Payload manifest** (`manifest.py`): walk `.app/Contents/MacOS` (macOS) / the Inno
   staged `app/` (Windows); assert the binary set **exactly** matches the expected spec
   (`agentshore-desktop` + bundled `agentshore-bd` on macOS; `agentshore-desktop.exe` on
   Windows). Any extra (e.g. a stray `agentshore-provisioner`) or missing file → `BuildError`.
2. **Signature**: `codesign --verify --deep --strict` (macOS, retained from `build-macos.sh:263`)
   / `signtool verify /pa` (Windows — currently absent).
3. **Embedded version**: read `CFBundleShortVersionString` from `Info.plist` (macOS) /
   exe `FileVersion` (Windows); assert `== version.read_canonical()`.

This is what makes a false green impossible: a stale/extra/mis-versioned artifact fails the build.

## Consistency & reproducibility additions

- **Toolchain pins:** add `rust-toolchain.toml` (pin the stable channel + components) and
  `.nvmrc` (Node 22, matching CI). Assert uv version on macOS too (port `Assert-UvVersion`
  into `version.py`/`context.py`), so both OSes and CI use identical toolchains.
- **Clean guarantee:** `phases/clean.py` removes the prior bundle **and** all staging dirs
  (`pkg-component`, `pkg-scripts`, `windows-installer`, `agentshore-wheel`) every run, so no
  run inherits another's leftovers (root cause of the stale-provisioner false green).
- **bd sidecar:** lift staging out of `build.rs` into `phases/sidecar.py` as an explicit,
  testable step (`build.rs` keeps only the cheap PATH guard); macOS stages+bundles, Windows
  is a documented no-op (install-time provisioning).

## CI coverage summary (after)

| Trigger | Job | Today | After |
|---------|-----|-------|-------|
| PR / push | python, dashboard, desktop unit tests | ✅ | ✅ + `verify-versions` |
| PR / push | **desktop crate compile (mac+win)** | ❌ | ✅ (fix A — catches #105-class) |
| PR touching `desktop/`,`packaging/`,`scripts/buildkit/` | unsigned packaging **smoke build** (spine end-to-end, no sign/notarize) | ❌ | ✅ (path-filtered) |
| `v*` tag | Windows installer | ✅ unsigned | ✅ via spine + verify gate |
| `v*` tag | **macOS `.pkg`** (signed+notarized) | ❌ | ✅ via spine (needs signing secrets) |
| `v*` tag | PyPI publish | ✅ | ✅ (unchanged) |

## Rollout (incremental, value-first — each step independently shippable)

1. ✅ **CI desktop compile gate** (fix A) + toolchain pins (`99b1464`).
2. ✅ **Version single-source + drift check** (fix C) + `verify-versions` CI step + pytest guard (`1b48e02`).
3. ✅ **Provisioner separate crate** (fix B); dropped the `required-features` band-aid (`400a8d1`).
4. ✅ **Python spine + macOS port**; `build-macos.sh` → shim. Validated by a real `--no-sign --no-pkg` build (`cb1df74`).
5. ✅ **Verification gate** (fix D) wired into the macOS spine path (`a17e27c`, exercised in step 4).
6. ✅ **Windows port** onto the spine; `build-windows.ps1` → shim; `_win_signing.ps1` carve-out (`8c1046e`).
7. ◻️ **packaging smoke** — partial: a `windows-spine` CI job validates the Windows spine imports/parses
   + `.ps1` syntax on `windows-latest` (the full signed Inno build stays tag-triggered). A macOS PR
   packaging-smoke and the **bd-sidecar phase extraction from `build.rs`** remain optional follow-ups.

Steps 1–3 (pure durability) landed before the spine refactor; all four selected durability fixes
(A/B/C/D) plus the full macOS+Windows port are done. Step 7's remainder is optional polish.

## Risks / open questions

- **Windows signing in Python:** cert-store + self-sign stays as `_win_signing.ps1` (pragmatic
  carve-out). Acceptable, or insist on a pure-Python signing path?
- **macOS CI signing secrets:** the tag-time signed+notarized macOS `.pkg` job needs Developer
  ID cert + notary creds in GH secrets. Until provisioned, that job runs unsigned (parity with
  today's Windows `-NoSign` tag job).
- **Cargo workspace move (fix B):** can't validate the Windows provisioner build from macOS;
  relies on the new CI compile gate (fix A) landing first — hence the rollout order.
- **Spine bootstrap:** the shims must locate `uv` before handing off; minimal bash/ps1 bootstrap retained.
```
