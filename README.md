# Carino PACS

A small, cross-platform **DICOM store-only PACS**. It does two things:

<img width="1160" height="868" alt="image" src="https://github.com/user-attachments/assets/9e30ba10-e34b-42f0-a97c-96eaf5a67ddb" />


- **Receive** — a Storage SCP that accepts `C-STORE` (and `C-ECHO`) and files incoming studies to disk, optionally organized by Patient / Study / Series.
- **Auto-send** — watches a folder and automatically forwards every new `.dcm` to **N** remote DICOM nodes via `C-STORE`, retrying each host until it accepts.

Everything is driven by one `config.json` and can be run head-less from the CLI or through a local web dashboard (Carino-workshop styled). No query/retrieve, no worklist — just store and forward.

Built on [`pynetdicom`](https://github.com/pydicom/pynetdicom) + [`pydicom`](https://github.com/pydicom/pydicom), so it runs identically on **Windows, macOS, and Linux (Debian & Fedora)**.

---

## Quick start

### macOS / Linux
```bash
./setup.sh          # creates .venv and installs dependencies
./run.sh init       # writes config.json and creates the folders
./run.sh serve      # dashboard at http://127.0.0.1:8042
```

### Windows (PowerShell)
```powershell
.\setup.ps1
.\run.ps1 init
.\run.ps1 serve
```

> Requires **Python 3.8+**. On Debian/Ubuntu also install `python3-venv`
> (`sudo apt install python3-venv`); Fedora and macOS ship what's needed.

Open the dashboard, add your remote node(s), point the watch folder at wherever
your `.dcm` files land, and hit **Start** on Receiver and/or Auto-send.

---

## Desktop app (tray agent)

> **Full run / build / release walkthrough:** see **[BUILDING.md](BUILDING.md)**.

`desktop/` is an optional Electron wrapper that runs Carino PACS as a **background
tray agent**: it launches the Python engine (`pacs serve --receive --watch`),
sits in the system tray / menu bar, and opens the dashboard in a native window
when you click it. Closing the window hides it back to the tray — the engine
keeps receiving and forwarding.

```bash
cd desktop
npm install
npm start          # runs against the project's .venv
```

- **Left-click the tray icon** → open the window. **Close** → hide to tray.
  **Tray menu → Quit** → stop the engine and exit.
- **Tray menu → Start at login** registers it as a real login-time agent
  (Windows/macOS; on Linux use an autostart `.desktop` entry or a systemd user unit).
- If the DICOM port is busy or a TLS cert is bad, the window still opens and the
  error shows in the Activity log — the agent never dies silently.

> Run `./setup.sh` (or `setup.ps1`) in the project root first so the `.venv`
> exists. `npm run dist` (electron-builder) scaffolds installers, but a fully
> standalone build also needs the Python engine bundled (e.g. PyInstaller into
> `extraResources`) — the dev `npm start` path needs none of that.
>
> Linux note: tray icons need an AppIndicator-capable tray (GNOME needs the
> AppIndicator extension); Windows and macOS work out of the box.

### Building standalone installers

`npm start` needs Python + the venv present. To produce a **self-contained**
installer that bundles the engine (no Python needed on the target):

```bash
# 1. freeze the DICOM engine (from the repo root)
pip install pyinstaller
pyinstaller packaging/pacs-engine.spec --distpath desktop/engine --workpath build/pyi

# 2. build the installer for the current OS
cd desktop && npm install && npm run dist
```

Output lands in `desktop/dist/` — `.AppImage` (Linux), `.dmg`/`.zip` (macOS),
`.exe` (Windows). In a packaged build the engine is the frozen `pacs-engine`
binary under the app's resources, and config + data live in the per-user data
dir (`app.getPath('userData')`), not the install folder.

**No cross-compilation:** each OS must be built on that OS. The bundled workflow
`.github/workflows/desktop-build.yml` does all three on a runner matrix — trigger
it from the Actions tab or push a `v*` tag, then download the three artifacts.

### Code signing (optional, activates via secrets)

Builds are **unsigned by default** — they run, but show a Gatekeeper (macOS) /
SmartScreen (Windows) warning the first time. To sign + notarize, add these repo
secrets and the *same* workflow produces signed builds with no code changes:

| Secret(s) | Purpose |
|---|---|
| `CSC_LINK`, `CSC_KEY_PASSWORD` | code-signing cert (.p12, base64) — macOS & Windows |
| `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` | macOS notarization |

When the secrets are absent the build stays unsigned (the `afterSign` hook skips
notarization gracefully). Other options: deploy unsigned to **managed machines**
via MDM / Group Policy and trust it centrally (no public cert needed), or use
**Azure Trusted Signing** as a low-cost cloud signer on Windows.

## CLI

The dashboard is optional — every function has a head-less command:

```bash
./run.sh serve [--receive] [--watch] [--host H] [--port P]
                     # web dashboard; flags also auto-start the workers
./run.sh receive [--port 11112] [--aet CARINOPACS] [--out ./received]
                     # Storage SCP only, runs until Ctrl+C
./run.sh send [--watch-dir ./outgoing]
                     # folder watcher / auto-forward only, runs until Ctrl+C
./run.sh echo --name "Example PACS"
./run.sh echo --host 10.0.0.5 --port 104 --aet REMOTEPACS
                     # connectivity test
./run.sh init        # scaffold config.json + folders
```

All commands accept `-c / --config <path>` (default `config.json`).

---

## Configuration (`config.json`)

```jsonc
{
  "scp": {
    "aet": "CARINOPACS",       // this server's AE title
    "bind": "0.0.0.0",         // interface to listen on
    "port": 11112,             // DICOM listen port
    "storage_dir": "./received",
    "organize": true,          // file under PatientID/StudyUID/SeriesUID/
    "allowed_aets": [],        // whitelist of calling AE titles ([] = accept any)
    "tls": false,              // serve DICOM over TLS
    "tls_cert": "",            // server certificate (PEM)
    "tls_key": "",             // server private key (PEM)
    "tls_ca": ""               // if set: require + verify client certs (mutual TLS)
  },
  "scu": {
    "aet": "CARINOSCU",        // AE title used when sending
    "watch_dir": "./outgoing", // folder to watch for new .dcm files
    "poll_interval": 3,        // seconds between scans
    "on_success": "keep",      // keep | move | delete (after all hosts accept)
    "sent_dir": "./sent",      // archive folder when on_success = "move"
    "tls_verify": true,        // verify each TLS node's certificate
    "tls_ca": "",              // CA bundle to verify against ("" = system store)
    "tls_cert": "",            // our client cert for mutual TLS (optional)
    "tls_key": ""              // our client private key (optional)
  },
  "destinations": [
    { "name": "Remote PACS", "host": "10.0.0.5", "port": 104, "aet": "REMOTEPACS", "enabled": true, "tls": false }
  ],
  "web": { "host": "127.0.0.1", "port": 8042 },
  "logs_dir": "./logs"         // dated log files (one per day) live here
}
```

By default everything lives in **`~/CarinoPACS/`** — the config file, the
`received` / `outgoing` / `sent` folders, and the `logs` folder (one
`YYYY-MM-DD.log` per day). Relative paths are resolved against the config file's
own directory and `~` is expanded, so the defaults land there automatically; set
absolute paths to put them elsewhere.

In the desktop app / dashboard you can also **drag a folder from your file
manager onto the Receiver or Auto-send card** to set its location.

---

## How the auto-send behaves

- A file is only sent once it is **stable** (size unchanged between two scans and
  non-zero) — so half-written files are never forwarded.
- Each file is tracked **per destination**; it counts as done only when **every
  enabled** destination has accepted it. Failed hosts are retried on the next scan.
- Send progress is persisted (`.carinopacs_state.json` next to your config), so a
  restart doesn't re-forward everything.
- Files are detected by the DICOM `DICM` marker, so extension-less DICOM works too.
- Compressed objects (JPEG / JPEG-LS / JPEG2000 / RLE) are forwarded **as-is** —
  there is no transcoding, so a destination that refuses the object's transfer
  syntax is reported as a failure rather than silently altering data.

---

## TLS (DICOM over TLS)

Both sides can use TLS, toggled independently.

- **Receiver:** tick **Serve DICOM over TLS** and provide a certificate + private key (PEM).
  Add a **Client-cert CA** to also require + verify client certificates (mutual TLS).
- **Sending:** tick the **TLS** box on a destination row. The client-side material lives in
  **Auto-send settings**: **Verify remote TLS certificate** (turn off for self-signed/testing),
  an optional **Trusted CA bundle** (blank = system trust store), and an optional client
  certificate + key for mutual TLS.

Generate a self-signed cert for testing:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout server.key -out server.crt \
  -subj "/CN=your-host" -addext "subjectAltName=IP:10.0.0.5"
```

- TLS uses the **same port** — a plaintext peer cannot talk to a TLS receiver (and vice-versa).
- With verification **on**, the remote cert must be valid for the host/IP you dial (matching
  SAN); otherwise supply the CA or turn verification off.
- TLS encrypts + authenticates the *transport*, not the DICOM *application* — combine it with
  `allowed_aets` and/or mutual TLS for real access control.

## Notes & caveats

- **Ports:** DICOM's registered port is **104**, which is privileged on
  Linux/macOS (needs root). The default here is **11112** to avoid that; set your
  remote nodes accordingly, or run the receiver elevated if you must use 104.
- **Firewall:** allow inbound TCP on the receiver port on the host machine.
- **Security:** the dashboard has **no authentication** and defaults to
  `127.0.0.1`. Keep it on localhost (or behind your own VPN/reverse proxy) — do
  not expose it to untrusted networks. `allowed_aets` gives you basic
  calling-AE filtering on the receiver, but DICOM itself is unauthenticated.
- Scope is deliberately **store + forward only** (no C-FIND / C-MOVE / MWL).

---

Part of the [carino.systems](https://carino.systems/) workshop.
