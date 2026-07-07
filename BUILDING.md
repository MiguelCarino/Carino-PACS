# Running, building & releasing Carino PACS

Three things, in order of commitment:

1. **[Run it](#1-run-it)** — from source, no build step (needs Python).
2. **[Build a standalone installer](#2-build-a-standalone-installer)** — one OS at a time, on your own machine.
3. **[Release with GitHub Actions](#3-release-with-github-actions)** — all three OSes automatically, via tags.

---

## 1. Run it

Needs **Python 3.8+** (and, for the desktop app, **Node 18+**). Nothing is compiled.

### The DICOM server (CLI / dashboard)

**Linux / macOS**
```bash
cd Carino-PACS
./setup.sh            # once — creates .venv and installs deps
./run.sh serve        # dashboard → http://127.0.0.1:8042
```

**Windows (PowerShell)**
```powershell
cd Carino-PACS
.\setup.ps1           # once
.\run.ps1 serve
```

Headless variants (any OS, via `run.sh` / `run.ps1`):
```
run.sh receive        # storage SCP only
run.sh send           # folder auto-forward only
run.sh echo --host 10.0.0.5 --port 104 --aet REMOTE
```

### The desktop app (tray + window), dev mode
Runs against the `.venv` you just created — no build needed.
```bash
cd Carino-PACS/desktop
npm install
npm start
```
Tray icon → click to open the window. Closing hides to tray; **Quit** from the
tray menu stops everything.

---

## 2. Build a standalone installer

This produces an installer that **bundles Python** — the target machine needs
nothing preinstalled. You build **on the OS you want the installer for** (you
cannot build a Windows `.exe` on Linux — see [why](#why-one-os-at-a-time)).

Prerequisites on the build machine: **Python 3.8+** and **Node 18+**.

```bash
# from the repo root — freeze the Python engine into a single binary
python -m pip install pyinstaller
python -m PyInstaller packaging/pacs-engine.spec --distpath desktop/engine --workpath build/pyi

# then build the OS-native installer
cd desktop
npm install
npm run dist
```

Output appears in **`desktop/dist/`**:

| You built on… | You get |
|---|---|
| Linux   | `Carino PACS-1.0.0.AppImage` |
| macOS   | `Carino PACS-1.0.0.dmg` (+ `.zip`) |
| Windows | `Carino PACS Setup 1.0.0.exe` |

In a packaged build the engine is the frozen `pacs-engine` binary inside the
app, and its config + received/outgoing/sent folders live in the per-user data
directory (e.g. `~/.config/Carino PACS` on Linux, `%APPDATA%\Carino PACS` on
Windows), **not** the install folder.

### Why one OS at a time
PyInstaller and electron-builder are **not cross-compilers** — each emits a
binary for the OS it runs on. To get all three from one place, use the CI
workflow below (a Linux, a macOS and a Windows runner build in parallel).

---

## 3. Release with GitHub Actions

The repo ships `.github/workflows/desktop-build.yml`, which builds **all three
OSes** and uploads the installers. But it only runs once the code is on GitHub.

### 3a. One-time: put the project on GitHub

```bash
cd Carino-PACS
git init
git add -A
git commit -m "Carino PACS initial commit"
git branch -M main
```

Create an **empty** repository on github.com (no README/licence), then:

```bash
git remote add origin https://github.com/<your-user>/Carino-PACS.git
git push -u origin main
```

> `.gitignore` already excludes `.venv/`, `node_modules/`, `dist/`, the frozen
> `engine/`, and local runtime data, so `git add -A` only commits source.

### 3b. Trigger a build — two ways

**A) Manually (no tag)** — good for testing.
GitHub → **Actions** tab → **Build desktop app** → **Run workflow**.

**B) By tag** — the normal way to cut a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow watches for tags matching `v*`, so pushing one starts a build.

### 3c. Get the installers
Open **Actions → the run that just started**. When the three jobs finish, the
**Artifacts** panel at the bottom holds:

- `carinopacs-ubuntu-latest`  → the `.AppImage`
- `carinopacs-macos-latest`   → the `.dmg` / `.zip`
- `carinopacs-windows-latest` → the `.exe`

Download and distribute them. (Artifacts live on the run for ~90 days; see
[Attach to a Release](#3e-optional-attach-installers-to-a-github-release) to
publish them permanently against the tag.)

### 3d. What a "tag" actually is
A git **tag** is just a named, permanent pointer to one commit — a release
marker like `v1.0.0`. The workflow is configured to fire whenever you push a
tag starting with `v`. So the mental model is:

```
edit code → commit → push to main        (nothing builds)
git tag v1.2.0 → git push origin v1.2.0   (⇢ builds all 3 OSes)
```

Bump the number each release (`v1.0.0`, `v1.0.1`, `v1.1.0`, …). Use the manual
"Run workflow" button when you just want to test the build without tagging.

### 3e. (Optional) Attach installers to a GitHub Release
Right now the installers sit on the Actions run. If you'd rather have them
attached to the tag on the **Releases** page (permanent, public download URLs),
that's a small addition to the workflow — ask and it'll be wired in.

---

## Website (GitHub Pages)

The `docs/` folder is a ready-to-publish landing page (Carino navbar, plain-language
explanation, per-platform download buttons, a section for technical users).

Enable it once: **GitHub → Settings → Pages → Build and deployment → Source:
“Deploy from a branch” → Branch: `main` / folder: `/docs`**. Your site appears at
`https://<your-user>.github.io/Carino-PACS/`.

The download buttons call the GitHub API for your **latest release** and auto-fill
the correct `.exe` / `.dmg` / `.AppImage` links (and detect the visitor's OS). Until
you cut a release they simply point at the Releases page — so publish a `v*` tag
(section 3) and the buttons light up automatically, no edits needed.

> The page loads its own local `carino-navbar.js` + `carino-clock.js` (no CDN). If
> you set a custom domain later, add a `docs/CNAME` file with the hostname.

## Signing (optional — removes the "unknown developer" warnings)

Builds are **unsigned by default**: they run, but the first launch shows a
Gatekeeper (macOS) / SmartScreen (Windows) warning. To sign automatically, add
these under **GitHub → Settings → Secrets and variables → Actions**:

| Secret(s) | For |
|---|---|
| `CSC_LINK`, `CSC_KEY_PASSWORD` | code-signing cert (`.p12`, base64) — macOS & Windows |
| `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` | macOS notarization |

The next tag/manual build picks them up with **no code change**; leave them
unset and builds stay unsigned. Alternatives: deploy unsigned to managed
machines via MDM / Group Policy, or use Azure Trusted Signing on Windows.
