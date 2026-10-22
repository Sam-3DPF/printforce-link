---
title: "A green PyInstaller build is not a working binary — smoke the published artifact"
module: printforce-link
date: 2026-07-16
last_updated: 2026-09-29
category: build-errors
problem_type: build_error
component: tooling
severity: high
symptoms:
  - "Frozen --onedir binary exits immediately with ModuleNotFoundError: No module named 'bridge'"
  - "The PyInstaller build step and the whole release workflow go green, but the published artifact is unrunnable"
  - "A local build worked while the CI build produced a broken binary from the same command"
root_cause: config_error
resolution_type: config_change
tags:
  - pyinstaller
  - frozen-binary
  - packaging
  - ci-release
  - smoke-test
  - printforce-link
related_components:
  - github-actions
  - bridge
---

# A green PyInstaller build is not a working binary — smoke the published artifact

## Problem

The first `v0.1.0` release of PrintForce Link (the downloadable Bambu bridge agent, repo `Sam-3DPF/printforce-link`) built successfully on every platform, published a GitHub Release, and passed its checksum — yet the binary **crashed the instant it launched** with `ModuleNotFoundError: No module named 'bridge'`. The build being "green" told us nothing about whether the app runs.

## Symptoms

- `pyinstaller --onedir ... packaging/entry.py` succeeds; the release job publishes the archive + `SHA256SUMS`.
- Running the extracted binary immediately prints:
  `ModuleNotFoundError: No module named 'bridge'` from `entry.py` (`from bridge.app import main`).
- Confusingly, a *local* build of the same command produced a working binary, so the failure looked environment-specific.

## What Didn't Work

- **Trusting the CI status.** Two of three build legs were green and had uploaded artifacts. "Build succeeded" was treated as "binary works" — it isn't. PyInstaller's `Analysis` failing to find a module is a *runtime* `ModuleNotFoundError`, not a build-time error.
- **Trusting the local build.** The local build happened to succeed because of a path difference (the interpreter/cwd made `bridge` importable during analysis). That success masked the real gap and nearly shipped a broken binary.

## Solution

PyInstaller resolves imports relative to the **entry script's** directory. The entry script is `packaging/entry.py`, so that `packaging/` directory is on the analysis path — but the `bridge` package lives at the **repo root** (`bridge/`), so it was never discovered or bundled. `entry.py` imports it lazily (`from bridge.app import main` inside `run()`), which compounded the miss.

Fix the build command to put the repo root on the analysis path and force-collect the package:

```yaml
# .github/workflows/release.yml
run: >
  pyinstaller --onedir --noconfirm --name printforce-link
  --paths .                       # repo root, where bridge/ lives, on the analysis path
  --collect-submodules bridge     # force every bridge.* module in (entry imports it lazily)
  --collect-submodules paho
  --hidden-import paho.mqtt.client
  --collect-data certifi
  packaging/entry.py
```

Then **verify by running the published artifact**, not the build log — download it via the exact URL the installer uses (`/releases/latest/download/...`), verify the checksum, extract into the real install layout, and run the binary:

```
2026-07-16 ... INFO  bridge.app: Loaded Config(dpf_base_url='https://dev.3dprintforce.com', printers=0, cloud_token=***)
2026-07-16 ... ERROR bridge.app: no cloud credential: ... (expected — no pair token supplied)
```

Reaching the app's own startup logic (config loaded, pairing gate hit) proves every import resolved. The broken build failed this exact smoke; the fixed one passes it.

## Why This Works

`--paths .` adds the repo root to the module search path PyInstaller's static analysis uses, so `bridge` becomes discoverable. `--collect-submodules bridge` guarantees the whole package tree is bundled even though the only import is deferred inside a function (static analysis of lazy imports is less reliable). A frozen app has no `site-packages` fallback at runtime, so anything not bundled at build time is simply absent — surfacing only when the code path that imports it runs.

## Prevention

- **A successful build is not a passing test. Smoke-run the actual published artifact** — the bytes a user downloads — before declaring a release good. Extract it into the real install layout and confirm it reaches its own startup logic. This one check is what caught the broken release.
- **CI now smoke-runs the build** (the `release.yml` "Smoke version" steps check `printforce-link --version` against the tag). That catches a missing `bridge` package. It does not replace a smoke of the published artifact: `--version` returns before `bridge.app` and its third-party imports load.
- **Frozen-build import failures are runtime, not build-time.** Never infer "the binary runs" from "the build was green."
- **When the entry script lives in a subdirectory** (`packaging/entry.py`) but the app package is at the repo root, always pass `--paths .` and `--collect-submodules <pkg>`. A lazy/deferred import of the package makes the miss more likely.
- **Distrust a local build that disagrees with CI.** Reproduce the CI invocation exactly (same working directory, same flags) rather than assuming the environment is the difference.
