/* Carino PACS dashboard front-end — vanilla JS over the REST API. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = async (url, opts) => {
    const res = await fetch(url, opts);
    let body = {};
    try { body = await res.json(); } catch (e) { /* empty */ }
    if (!res.ok) throw new Error(body.error || res.statusText);
    return body;
  };
  const post = (url, data) =>
    api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

  // Full loaded config sections — kept so a Save preserves any key that has no
  // form input (min_free_gb, pending_dir, …); apply_config merges over DEFAULTS,
  // so an omitted key would otherwise silently reset.
  let loadedScp = {}, loadedScu = {}, loadedPrint = {}, loadedRis = {};
  let loadedWeb = { host: "127.0.0.1", port: 8042 };
  let statusTimer = null, logTimer = null;
  let editorUrl = "";                                // DICOM-editor base URL (from status); "" hides ✎ Edit

  /* ── Status polling ──────────────────────────────────────────── */
  function renderStatus(s) {
    const rx = s.receiver, wx = s.watcher, px = s.printer || {}, rs = s.ris || {};

    // This machine's network identity (what remote nodes send to).
    const ni = $("netInfo");
    if (ni) {
      ni.textContent = "";
      // Prefer the full list (multi-homed hosts / device subnets); fall back to
      // the single primary IP for older engines.
      const ips = (s.host_ips && s.host_ips.length) ? s.host_ips : (s.host_ip ? [s.host_ip] : []);
      if (ips.length) {
        ni.classList.remove("offline");
        const v = (t) => { const el = document.createElement("span"); el.className = "v"; el.textContent = t; return el; };
        ni.append(ips.length > 1 ? "Reachable at " : "Your IP is ");
        ips.forEach((ip, i) => { if (i) ni.append(" · "); ni.append(v(ip)); });
        ni.append(" · AE title ", v(rx.aet), " · port ", v(String(rx.port)));
        if (px.running || px.enabled) {
          ni.append(" · print ", v(px.aet), " : ", v(String(px.port)));
        }
      } else {
        ni.classList.add("offline");
        ni.textContent = "You're offline — no network detected";
      }
    }

    editorUrl = (s.editor_url || "").trim();

    // Pending-imports badge on the 📎 button.
    const badge = $("pendingBadge");
    if (badge) {
      const n = s.pending || 0;
      badge.textContent = String(n);
      badge.hidden = n === 0;
    }

    // Stuck-sends badge on the ⚠ button.
    const sBadge = $("stuckBadge");
    if (sBadge) {
      const n = s.stuck || 0;
      sBadge.textContent = String(n);
      sBadge.hidden = n === 0;
    }

    // Open-orders badge on the 📋 button.
    const oBadge = $("ordersBadge");
    if (oBadge) {
      const n = (rs.counts && rs.counts.open) || 0;
      oBadge.textContent = String(n);
      oBadge.hidden = n === 0;
    }

    // Low-disk warning banner (only when the storage volume is below the floor).
    const dw = $("diskWarn");
    if (dw) {
      const d = s.disk || {};
      if (d.low) {
        dw.hidden = false;
        dw.textContent = "⚠ Low disk space — " + (d.free_gb != null ? d.free_gb + " GB" : "?") +
          " free (below the " + d.floor_gb + " GB floor). New incoming studies will be refused until space is freed.";
      } else {
        dw.hidden = true;
      }
    }

    setDot($("rxDot"), rx.running);
    $("rxAet").textContent = rx.aet;
    $("rxAddr").textContent = `${rx.bind}:${rx.port}`;
    $("rxDir").textContent = rx.storage_dir;
    $("rxCount").textContent = rx.received;
    $("rxErr").textContent = rx.errors;
    $("rxTls").textContent = rx.tls ? (rx.tls_mutual ? "TLS (mutual)" : "TLS") : "plaintext";
    setToggle($("rxToggle"), rx.running);

    setDot($("wxDot"), wx.running);
    $("wxDir").textContent = wx.watch_dir;
    $("wxAet").textContent = wx.aet;
    $("wxMode").textContent = wx.on_success;
    $("wxSent").textContent = wx.sent;
    $("wxFailed").textContent = wx.failed;
    $("wxLast").textContent = wx.last_activity || "—";
    setToggle($("wxToggle"), wx.running);

    setDot($("pxDot"), px.running);
    $("pxAet").textContent = px.aet || "—";
    $("pxAddr").textContent = `${px.bind || "0.0.0.0"}:${px.port}`;
    $("pxMode").textContent = (px.color ? "grayscale + color" : "grayscale") +
      " · " + (px.layout === "image" ? "→ image" : "→ PDF");
    $("pxCount").textContent = px.printed || 0;
    $("pxErr").textContent = px.errors || 0;
    $("pxTls").textContent = px.tls ? "TLS" : "plaintext";
    setToggle($("pxToggle"), px.running);

    setDot($("rsDot"), rs.running);
    $("rsAddr").textContent = `${rs.bind || "0.0.0.0"}:${rs.port || "—"}`;
    $("rsMatch").textContent = rs.match_on === "accession_or_patient" ? "accession / patient ID" : "accession";
    $("rsOpen").textContent = (rs.counts && rs.counts.open) || 0;
    $("rsRecv").textContent = rs.received || 0;
    $("rsErr").textContent = rs.errors || 0;
    setToggle($("rsToggle"), rs.running);
  }
  function setToggle(btn, on) {
    btn.dataset.on = String(on);
    btn.textContent = on ? "Stop" : "Start";
  }
  function setDot(el, on) {
    el.classList.toggle("on", on);
    el.classList.toggle("off", !on);
  }
  // Amber activity blink (like an ethernet link/activity LED); only while running.
  function blink(el) {
    if (!el || !el.classList.contains("on")) return;
    el.classList.remove("act");
    void el.offsetWidth;              // restart the CSS animation
    el.classList.add("act");
  }
  async function pollStatus() {
    try { renderStatus(await api("/api/status")); } catch (e) { /* keep last */ }
  }

  /* ── Log polling ─────────────────────────────────────────────── */
  let logSeq = 0, firstLog = true;
  async function pollLog() {
    try {
      const data = await api("/api/log?since=" + logSeq);
      const box = $("log");
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
      let sawStore = false, sawSend = false, sawPrint = false, sawRis = false;
      for (const e of data.entries) {
        logSeq = e.seq;
        if (e.kind === "store") sawStore = true;   // a file was received
        if (e.kind === "send") sawSend = true;      // a file was forwarded
        if (e.kind === "print") sawPrint = true;    // a print job / event
        if (e.kind === "ris") sawRis = true;        // an HL7 order / match event
        const line = document.createElement("div");
        line.className = "line";
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = (e.ts || "").replace("T", " ").replace(/(\+00:00|Z)$/, "");
        const m = document.createElement("span");
        m.className = e.level;
        m.textContent = e.message;
        line.append(t, m);
        box.appendChild(line);
      }
      while (box.childElementCount > 400) box.removeChild(box.firstChild);
      if (atBottom) box.scrollTop = box.scrollHeight;
      if (!firstLog) {                 // don't blink for the backlog on first load
        if (sawStore) blink($("rxDot"));
        if (sawSend) blink($("wxDot"));
        if (sawPrint) blink($("pxDot"));
        if (sawRis) blink($("rsDot"));
      }
      firstLog = false;
    } catch (e) { /* ignore */ }
  }

  /* ── Config load / populate ──────────────────────────────────── */
  async function loadConfig() {
    const c = await api("/api/config");
    loadedScp = c.scp || {};
    loadedScu = c.scu || {};
    loadedPrint = c.print || {};
    loadedRis = c.ris || {};
    loadedWeb = c.web || loadedWeb;
    $("webEditorUrl").value = (c.web && c.web.editor_url) || "";
    $("scpAet").value = c.scp.aet;
    $("scpBind").value = c.scp.bind;
    $("scpPort").value = c.scp.port;
    $("scpDir").value = c.scp.storage_dir;
    $("scpOrganize").checked = !!c.scp.organize;
    $("scpMinFree").value = c.scp.min_free_gb != null ? c.scp.min_free_gb : 2;
    $("scpAllowed").value = (c.scp.allowed_aets || []).join(", ");
    $("scpTls").checked = !!c.scp.tls;
    $("scpTlsCert").value = c.scp.tls_cert || "";
    $("scpTlsKey").value = c.scp.tls_key || "";
    $("scpTlsCa").value = c.scp.tls_ca || "";
    $("scuAet").value = c.scu.aet;
    $("scuDir").value = c.scu.watch_dir;
    $("scuPoll").value = c.scu.poll_interval;
    $("scuMode").value = c.scu.on_success;
    $("scuSent").value = c.scu.sent_dir;
    $("scuTlsVerify").checked = c.scu.tls_verify !== false;
    $("scuTlsCa").value = c.scu.tls_ca || "";
    $("scuTlsCert").value = c.scu.tls_cert || "";
    $("scuTlsKey").value = c.scu.tls_key || "";
    const pr = c.print || {};
    $("prnEnabled").checked = !!pr.enabled;
    $("prnAet").value = pr.aet || "CARINOPRINT";
    $("prnBind").value = pr.bind || "0.0.0.0";
    $("prnPort").value = pr.port != null ? pr.port : 11113;
    $("prnLayout").value = (pr.layout === "image" || pr.layout === "secondary_capture") ? "image" : "pdf";
    $("prnColor").checked = !!pr.color;
    $("prnAllowed").value = (pr.allowed_aets || []).join(", ");
    $("prnTls").checked = !!pr.tls;
    $("prnTlsCert").value = pr.tls_cert || "";
    $("prnTlsKey").value = pr.tls_key || "";
    $("prnTlsCa").value = pr.tls_ca || "";
    const ri = c.ris || {};
    $("risEnabled").checked = !!ri.enabled;
    $("risBind").value = ri.bind || "0.0.0.0";
    $("risPort").value = ri.port != null ? ri.port : 2575;
    $("risDir").value = ri.store_dir || "./ris";
    $("risMatch").value = ri.match_on === "accession_or_patient" ? "accession_or_patient" : "accession";
    $("risAutoClose").checked = ri.auto_close !== false;
    $("risHosts").value = (ri.allowed_hosts || []).join(", ");
    renderDests(c.destinations || []);
    reflowActive();
  }

  function renderDests(list) {
    const body = $("destBody");
    body.innerHTML = "";
    list.forEach(addDestRow);
    if (!list.length) addDestRow({});
  }
  function addDestRow(d) {
    const tpl = $("destRowTpl").content.cloneNode(true);
    const tr = tpl.querySelector("tr");
    tr.querySelector(".d-en").checked = d.enabled !== false;
    tr.querySelector(".d-name").value = d.name || "";
    tr.querySelector(".d-host").value = d.host || "";
    tr.querySelector(".d-port").value = d.port || "";
    tr.querySelector(".d-aet").value = d.aet || "";
    tr.querySelector(".d-tls").checked = !!d.tls;
    tr.querySelector(".del").addEventListener("click", () => tr.remove());
    tr.querySelector(".echo").addEventListener("click", () => echoRow(tr));
    $("destBody").appendChild(tr);
  }
  function collectDests() {
    return [...$("destBody").querySelectorAll("tr")]
      .map((tr) => ({
        enabled: tr.querySelector(".d-en").checked,
        name: tr.querySelector(".d-name").value.trim(),
        host: tr.querySelector(".d-host").value.trim(),
        port: parseInt(tr.querySelector(".d-port").value, 10),
        aet: tr.querySelector(".d-aet").value.trim(),
        tls: tr.querySelector(".d-tls").checked,
      }))
      .filter((d) => d.host && d.aet && d.port);
  }

  function collectConfig() {
    const allowed = $("scpAllowed").value.split(",").map((s) => s.trim()).filter(Boolean);
    // Spread the loaded section first so keys without a form input (min_free_gb,
    // pending_dir, …) survive; the form fields below override the visible ones.
    return {
      scp: {
        ...loadedScp,
        aet: $("scpAet").value.trim(),
        bind: $("scpBind").value.trim() || "0.0.0.0",
        port: parseInt($("scpPort").value, 10),
        storage_dir: $("scpDir").value.trim(),
        organize: $("scpOrganize").checked,
        min_free_gb: parseFloat($("scpMinFree").value) || 0,
        allowed_aets: allowed,
        tls: $("scpTls").checked,
        tls_cert: $("scpTlsCert").value.trim(),
        tls_key: $("scpTlsKey").value.trim(),
        tls_ca: $("scpTlsCa").value.trim(),
      },
      scu: {
        ...loadedScu,
        aet: $("scuAet").value.trim(),
        watch_dir: $("scuDir").value.trim(),
        poll_interval: parseFloat($("scuPoll").value) || 3,
        on_success: $("scuMode").value,
        sent_dir: $("scuSent").value.trim(),
        tls_verify: $("scuTlsVerify").checked,
        tls_ca: $("scuTlsCa").value.trim(),
        tls_cert: $("scuTlsCert").value.trim(),
        tls_key: $("scuTlsKey").value.trim(),
      },
      print: {
        ...loadedPrint,
        enabled: $("prnEnabled").checked,
        aet: $("prnAet").value.trim() || "CARINOPRINT",
        bind: $("prnBind").value.trim() || "0.0.0.0",
        port: parseInt($("prnPort").value, 10),
        layout: $("prnLayout").value,
        color: $("prnColor").checked,
        allowed_aets: $("prnAllowed").value.split(",").map((s) => s.trim()).filter(Boolean),
        tls: $("prnTls").checked,
        tls_cert: $("prnTlsCert").value.trim(),
        tls_key: $("prnTlsKey").value.trim(),
        tls_ca: $("prnTlsCa").value.trim(),
      },
      ris: {
        ...loadedRis,
        enabled: $("risEnabled").checked,
        bind: $("risBind").value.trim() || "0.0.0.0",
        port: parseInt($("risPort").value, 10),
        store_dir: $("risDir").value.trim() || "./ris",
        match_on: $("risMatch").value,
        auto_close: $("risAutoClose").checked,
        allowed_hosts: $("risHosts").value.split(",").map((s) => s.trim()).filter(Boolean),
      },
      destinations: collectDests(),
      web: { ...loadedWeb, editor_url: $("webEditorUrl").value.trim() },
    };
  }

  /* ── Actions ─────────────────────────────────────────────────── */
  async function echoRow(tr) {
    const dest = {
      name: tr.querySelector(".d-name").value.trim(),
      host: tr.querySelector(".d-host").value.trim(),
      port: parseInt(tr.querySelector(".d-port").value, 10),
      aet: tr.querySelector(".d-aet").value.trim(),
    };
    const btn = tr.querySelector(".echo");
    if (!dest.host || !dest.port || !dest.aet) { flashNote("Fill host, port and AE first", false); return; }
    const old = btn.textContent; btn.textContent = "…"; btn.disabled = true;
    try {
      const r = await post("/api/echo", dest);
      flashNote(`${dest.host}: ${r.message}`, r.ok);
    } catch (e) {
      flashNote(`${dest.host}: ${e.message}`, false);
    } finally { btn.textContent = old; btn.disabled = false; }
  }

  function flashNote(msg, ok) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast " + (ok ? "ok" : "bad");
    t.hidden = false;
    clearTimeout(flashNote._t);
    flashNote._t = setTimeout(() => { t.hidden = true; }, 5000);
  }

  async function saveConfig() {
    try {
      await post("/api/config", collectConfig());
      flashNote("Saved.", true);
      pollStatus();
      return true;
    } catch (e) { flashNote(e.message, false); return false; }
  }

  async function toggle(kind, btn) {
    const action = btn.dataset.on === "true" ? "stop" : "start";
    btn.disabled = true;
    try {
      // Persist current edits before starting so workers use them.
      if (action === "start") await post("/api/config", collectConfig()).catch(() => {});
      await post("/api/" + kind, { action });
    } catch (e) { flashNote(e.message, false); }
    finally { btn.disabled = false; pollStatus(); }
  }

  /* ── Drag & drop a folder onto the Receiver / Auto-send cards ──── */
  function droppedFolder(e) {
    // In the desktop app, File.path gives the real absolute path (browsers hide it).
    let isDir = true;
    const items = e.dataTransfer.items;
    if (items && items.length && items[0].webkitGetAsEntry) {
      const entry = items[0].webkitGetAsEntry();
      if (entry) isDir = entry.isDirectory;
    }
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    return { path: f && f.path, isDir: isDir };
  }

  function wireDropZones() {
    // Stop the browser from navigating if a folder is dropped anywhere.
    ["dragover", "drop"].forEach((ev) => window.addEventListener(ev, (e) => e.preventDefault()));
    const zones = [
      { el: $("receiverCard"), input: "scpDir", label: "Storage" },
      { el: $("watcherCard"), input: "scuDir", label: "Watched" },
    ];
    zones.forEach((z) => {
      if (!z.el) return;
      z.el.addEventListener("dragover", (e) => { e.preventDefault(); z.el.classList.add("drop-active"); });
      z.el.addEventListener("dragleave", (e) => { if (!z.el.contains(e.relatedTarget)) z.el.classList.remove("drop-active"); });
      z.el.addEventListener("drop", async (e) => {
        e.preventDefault();
        z.el.classList.remove("drop-active");
        const info = droppedFolder(e);
        if (!info.path) { flashNote("Folder drop needs the desktop app (browsers hide the path).", false); return; }
        if (info.isDir === false) { flashNote("Please drop a folder, not a file.", false); return; }
        $(z.input).value = info.path;
        await saveConfig();
        flashNote(z.label + " folder → " + info.path, true);
      });
    });
  }

  /* ── Kill the whole service ──────────────────────────────────── */
  async function killService() {
    if (!confirm("Shut down Carino PACS?\n\nThe receiver and auto-send stop and the engine process exits.")) return;
    $("killSvc").disabled = true;
    post("/api/shutdown", {}).catch(() => {});   // process may exit before responding
    if (statusTimer) clearInterval(statusTimer);
    if (logTimer) clearInterval(logTimer);
    setDot($("rxDot"), false);
    setDot($("wxDot"), false);
    const ov = document.createElement("div");
    ov.className = "stopped-overlay";
    ov.innerHTML = "<div><h2>Carino PACS has shut down</h2>" +
      "<p>The service stopped. You can close this window, or restart it from your terminal / the desktop app.</p></div>";
    document.body.appendChild(ov);
  }

  /* ── Transaction history ─────────────────────────────────────── */
  let histGroup = "received";

  async function loadHistory() {
    const list = $("histList");
    list.innerHTML = "<div class='hist-empty'>Loading…</div>";
    try {
      const data = await api("/api/studies?group=" + histGroup);
      renderHistory(data.studies || []);
      reflowActive();
    } catch (e) {
      list.innerHTML = "<div class='hist-empty'>Could not load: " + e.message + "</div>";
    }
  }

  function renderHistory(studies) {
    const list = $("histList");
    list.innerHTML = "";
    if (!studies.length) {
      const label = histGroup === "sent" ? "archived" : "received";
      list.innerHTML = "<div class='hist-empty'>No " + label + " studies yet.</div>";
      return;
    }
    studies.forEach((s) => {
      const row = $("histRowTpl").content.cloneNode(true).querySelector(".hist-row");
      row.querySelector(".hist-patient").textContent =
        (s.patient || "(no name)") + (s.patient_id ? "  ·  " + s.patient_id : "");
      const meta = [
        s.study_date || "no date",
        s.study_desc || "(no study description)",
        s.modality,
        s.instances + (s.instances === 1 ? " image" : " images"),
      ].filter(Boolean).join("  ·  ");
      row.querySelector(".hist-meta").textContent = meta;

      const ser = row.querySelector(".hist-series");
      (s.series || []).slice(0, 8).forEach((se) => {
        const chip = document.createElement("span");
        chip.className = "hist-chip";
        chip.textContent = (se.desc || se.modality || "series") + " (" + se.count + ")";
        ser.appendChild(chip);
      });
      if ((s.series || []).length > 8) {
        const more = document.createElement("span");
        more.className = "hist-chip more";
        more.textContent = "+" + (s.series.length - 8) + " more";
        ser.appendChild(more);
      }

      const sendBtn = row.querySelector(".hist-send");
      sendBtn.textContent = histGroup === "sent" ? "Resend" : "Send";
      sendBtn.addEventListener("click", () => histAction("send", s, sendBtn));
      row.querySelector(".hist-attach").addEventListener("click", () => histAttach(s));
      const editBtn = row.querySelector(".hist-edit");
      if (editorUrl) {
        editBtn.hidden = false;
        editBtn.addEventListener("click", () => histEdit(s));
      }
      row.querySelector(".hist-open").addEventListener("click", () => histAction("reveal", s));
      row.querySelector(".hist-del").addEventListener("click", () => histDelete(s));
      list.appendChild(row);
    });
  }

  async function histAction(action, s, btn) {
    const old = btn && btn.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const r = await post("/api/studies/" + action, { group: histGroup, path: s.path });
      flashNote(r.message || "OK", r.ok !== false);
    } catch (e) {
      flashNote(e.message, false);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  async function histDelete(s) {
    if (!confirm("Delete this study from disk?\n\n" + (s.patient || "(no name)") +
                 "\n" + (s.study_desc || "") + "  —  " + s.instances + " image(s)")) return;
    try {
      const r = await post("/api/studies/delete", { group: histGroup, path: s.path });
      flashNote(r.message || "Deleted", r.ok !== false);
      loadHistory();
    } catch (e) { flashNote(e.message, false); }
  }

  async function histDeleteAll() {
    const label = histGroup === "sent" ? "archived" : "received";
    if (!confirm("Delete ALL " + label + " studies?\n\nThis permanently removes every study in the " +
                 label + " folder from disk.")) return;
    try {
      const r = await post("/api/studies/delete-all", { group: histGroup });
      flashNote(r.message || ("Removed " + (r.removed || 0)), r.ok !== false);
      loadHistory();
    } catch (e) { flashNote(e.message, false); }
  }

  // Open a study in DICOM-editor via a postMessage BRIDGE. A remote HTTPS
  // editor cannot fetch our http://localhost API (mixed content — hard-blocked
  // in Safari), so instead WE fetch the DICOM from our own origin (http→http,
  // fine) and hand the bytes to the editor window via postMessage, which is not
  // subject to mixed-content rules. Works in every browser.
  function histEdit(s) {
    if (!editorUrl) return;
    // Resolve relative ("/editor/" = the bundled same-origin editor) or absolute URLs alike.
    let editorAbs, editorOrigin;
    try { const u = new URL(editorUrl, location.origin); editorAbs = u.href; editorOrigin = u.origin; }
    catch (e) { flashNote("Editor URL is not valid", false); return; }
    const manifestUrl = "/api/studies/files?group=" + encodeURIComponent(histGroup) + "&path=" + encodeURIComponent(s.path);
    const sep = editorAbs.includes("#") ? "&" : "#";
    const win = window.open(editorAbs + sep + "carino-bridge", "_blank");   // NOT noopener — we need window.opener on the editor side
    if (!win) { flashNote("Pop-up blocked — allow pop-ups to open the editor", false); return; }
    let done = false;
    async function onMsg(ev) {
      if (ev.source !== win || !ev.data || ev.data.type !== "carino-pacs-ready" || done) return;
      done = true;
      window.removeEventListener("message", onMsg);
      try {
        const man = await api(manifestUrl);                 // same-origin fetch (http→http)
        const entries = man.files || [];
        if (!entries.length) throw new Error(man.message || "no DICOM files in study");
        const files = [];
        for (const e of entries) {
          const r = await fetch(e.url);
          if (r.ok) files.push({ name: e.name, buf: await r.arrayBuffer() });
        }
        if (!files.length) throw new Error("could not read any DICOM file");
        win.postMessage({ type: "carino-pacs-files", files: files }, editorOrigin, files.map((f) => f.buf));
        flashNote("Opened " + files.length + " file(s) in the editor", true);
      } catch (err) {
        flashNote("Editor hand-off failed: " + err.message, false);
      }
    }
    window.addEventListener("message", onMsg);
    setTimeout(() => { if (!done) window.removeEventListener("message", onMsg); }, 60000);
  }

  // Attach a PDF/image to an existing study (inherits its identity, new series).
  function histAttach(s) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".pdf,.jpg,.jpeg,.png,application/pdf,image/*";
    input.addEventListener("change", async () => {
      const f = input.files && input.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("group", histGroup);
      fd.append("path", s.path);
      fd.append("file", f);
      try {
        const res = await fetch("/api/studies/attach", { method: "POST", body: fd });
        let body = {}; try { body = await res.json(); } catch (e) { /* empty */ }
        flashNote(body.message || (res.ok ? "Attached" : "Attach failed"), res.ok && body.ok !== false);
        if (res.ok) loadHistory();
      } catch (e) { flashNote(e.message, false); }
    });
    input.click();
  }

  /* ── Pending imports (non-DICOM review queue) ────────────────── */
  function fmtDate(raw) {
    const s = String(raw || "");
    return (s.length === 8 && /^\d+$/.test(s)) ? s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8) : s;
  }

  function fmtWait(secs) {
    const n = Math.max(0, Number(secs) || 0);
    if (n <= 0) return "due now";
    if (n < 60) return "retry in " + n + "s";
    if (n < 3600) return "retry in " + Math.round(n / 60) + "m";
    return "retry in " + Math.round(n / 3600) + "h";
  }

  async function loadStuck() {
    const list = $("stuckList");
    list.innerHTML = "<div class='hist-empty'>Loading…</div>";
    try {
      const data = await api("/api/stuck");
      renderStuck(data.destinations || []);
      reflowActive();
    } catch (e) {
      list.innerHTML = "<div class='hist-empty'>Could not load: " + e.message + "</div>";
    }
  }

  function renderStuck(dests) {
    const list = $("stuckList");
    list.innerHTML = "";
    if (!dests.length) {
      list.innerHTML = "<div class='hist-empty'>Nothing stuck — every forward is up to date.</div>";
      return;
    }
    dests.forEach((d) => {
      const row = $("stuckRowTpl").content.cloneNode(true).querySelector(".stuck-row");
      row.querySelector(".stuck-dest").textContent = d.name || "(destination)";
      row.querySelector(".stuck-meta").textContent =
        d.instances + (d.instances === 1 ? " instance" : " instances") + " waiting  ·  " +
        d.attempts + (d.attempts === 1 ? " attempt" : " attempts");
      row.querySelector(".stuck-err").textContent = d.last_error ? ("last error: " + d.last_error) : "";
      row.querySelector(".stuck-next").textContent = fmtWait(d.next_in);
      const btn = row.querySelector(".stuck-retry");
      btn.addEventListener("click", () => retryStuck(d.name, btn));
      list.appendChild(row);
    });
  }

  async function retryStuck(dest, btn) {
    const old = btn && btn.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const r = await post("/api/stuck/retry", dest ? { dest } : {});
      flashNote(r.message || "Retrying…", r.ok !== false);
      loadStuck();
      pollStatus();
    } catch (e) {
      flashNote(e.message, false);
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  async function loadPending() {
    const list = $("pendingList");
    list.innerHTML = "<div class='hist-empty'>Loading…</div>";
    try {
      const data = await api("/api/pending");
      renderPending(data.items || []);
      reflowActive();
    } catch (e) {
      list.innerHTML = "<div class='hist-empty'>Could not load: " + e.message + "</div>";
    }
  }

  function renderPending(items) {
    const list = $("pendingList");
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = "<div class='hist-empty'>Nothing waiting for review.</div>";
      return;
    }
    items.forEach((it) => {
      const row = $("pendingRowTpl").content.cloneNode(true).querySelector(".pend-row");
      const kind = row.querySelector(".pend-kind");
      kind.textContent = it.kind === "pdf" ? "PDF" : "IMAGE";
      kind.classList.add(it.kind === "pdf" ? "k-pdf" : "k-img");
      row.querySelector(".pend-file").textContent = it.filename || "(file)";
      row.querySelector(".pend-preview").href = "/api/pending/preview?id=" + encodeURIComponent(it.id);
      row.querySelector(".pf-patient").value = it.patient || "";
      row.querySelector(".pf-pid").value = it.patient_id || "";
      row.querySelector(".pf-acc").value = it.accession || "";
      row.querySelector(".pf-date").value = fmtDate(it.study_date);
      row.querySelector(".pf-sdesc").value = it.study_desc || "";
      row.querySelector(".pf-serdesc").value = it.series_desc || "";
      row.querySelector(".pend-src").textContent = it.source ? ("from " + it.source) : "";
      const appBtn = row.querySelector(".pend-approve");
      appBtn.addEventListener("click", () => approvePending(it.id, row, appBtn));
      row.querySelector(".pend-discard").addEventListener("click", () => discardPending(it.id, it));
      list.appendChild(row);
    });
  }

  async function approvePending(id, row, btn) {
    const edits = {
      id: id,
      patient: row.querySelector(".pf-patient").value.trim(),
      patient_id: row.querySelector(".pf-pid").value.trim(),
      accession: row.querySelector(".pf-acc").value.trim(),
      study_date: row.querySelector(".pf-date").value.trim(),
      study_desc: row.querySelector(".pf-sdesc").value.trim(),
      series_desc: row.querySelector(".pf-serdesc").value.trim(),
    };
    const old = btn.textContent; btn.disabled = true; btn.textContent = "…";
    try {
      const r = await post("/api/pending/approve", edits);
      flashNote(r.message || "Approved", r.ok !== false);
      loadPending();
      pollStatus();
    } catch (e) {
      flashNote(e.message, false);
      btn.disabled = false; btn.textContent = old;
    }
  }

  async function discardPending(id, it) {
    if (!confirm("Discard this file?\n\n" + (it.filename || "") +
                 "\n\nIt is permanently deleted without importing.")) return;
    try {
      const r = await post("/api/pending/discard", { id });
      flashNote(r.message || "Discarded", r.ok !== false);
      loadPending();
      pollStatus();
    } catch (e) { flashNote(e.message, false); }
  }

  /* ── RIS orders (emergency RIS: intake + reconciliation) ─────── */
  let orderStatus = "open";

  async function loadOrders() {
    const list = $("ordersList");
    list.innerHTML = "<div class='hist-empty'>Loading…</div>";
    try {
      const data = await api("/api/ris/orders?status=" + orderStatus);
      renderOrders(data.orders || []);
      $("ordPurge").hidden = orderStatus !== "closed" || !(data.counts && data.counts.closed);
      reflowActive();
    } catch (e) {
      list.innerHTML = "<div class='hist-empty'>Could not load: " + e.message + "</div>";
    }
  }

  function renderOrders(orders) {
    const list = $("ordersList");
    list.innerHTML = "";
    if (!orders.length) {
      list.innerHTML = "<div class='hist-empty'>" +
        (orderStatus === "open" ? "No open orders. Send an ORM over HL7/MLLP or add one above."
                                : "No closed orders yet.") + "</div>";
      return;
    }
    orders.forEach((o) => {
      const row = $("orderRowTpl").content.cloneNode(true).querySelector(".order-row");
      const acc = row.querySelector(".order-acc");
      acc.textContent = o.accession ? ("ACC " + o.accession) : "no accession";
      if (!o.accession) acc.classList.add("order-noacc");
      row.querySelector(".order-patient").textContent = o.patient || o.patient_name || "(no patient)";
      row.querySelector(".hist-meta").textContent = [
        o.patient_id ? "ID " + o.patient_id : "",
        o.modality || "",
        o.study_desc || "(no study description)",
        o.scheduled_dt ? "@ " + o.scheduled_dt : "",
      ].filter(Boolean).join("  ·  ");
      const sub = row.querySelector(".order-sub");
      const bits = ["via " + (o.source || "?"), "queued " + fmtStamp(o.created)];
      if (o.status === "closed") {
        bits.push((o.close_reason === "matched" ? "✓ matched" : "cancelled") + " " + fmtStamp(o.closed));
      }
      if (o.referring) bits.push("ref: " + o.referring);
      sub.textContent = bits.join("  ·  ");
      const cancelBtn = row.querySelector(".order-cancel");
      if (o.status === "closed") cancelBtn.hidden = true;
      else cancelBtn.addEventListener("click", () => orderAction("cancel", o, "Cancel this order? It moves to Closed (kept for the audit trail)."));
      row.querySelector(".order-del").addEventListener("click", () =>
        orderAction("delete", o, "Delete this order permanently? This removes it from the audit trail."));
      list.appendChild(row);
    });
  }

  function fmtStamp(iso) {
    if (!iso) return "";
    return String(iso).replace("T", " ").replace("Z", "");
  }

  async function addOrder(btn) {
    const fields = {
      accession: $("ordAcc").value.trim(),
      patient: $("ordPatient").value.trim(),
      patient_id: $("ordPid").value.trim(),
      modality: $("ordMod").value.trim(),
      study_desc: $("ordDesc").value.trim(),
      scheduled_dt: $("ordWhen").value.trim(),
      referring: $("ordRef").value.trim(),
    };
    if (!fields.accession && !fields.patient && !fields.patient_id) {
      flashNote("An order needs at least an accession, patient name or ID", false);
      return;
    }
    const old = btn.textContent; btn.disabled = true; btn.textContent = "…";
    try {
      const r = await post("/api/ris/orders", fields);
      flashNote(r.message || "Order queued", r.ok !== false);
      if (r.ok !== false) {
        ["ordAcc", "ordPatient", "ordPid", "ordMod", "ordDesc", "ordWhen", "ordRef"].forEach((id) => { $(id).value = ""; });
        orderStatus = "open";
        document.querySelectorAll("#dlgOrders .hist-tab").forEach((t) => t.classList.toggle("active", t.dataset.ostatus === "open"));
        loadOrders();
        pollStatus();
      }
    } catch (e) {
      flashNote(e.message, false);
    } finally { btn.disabled = false; btn.textContent = old; }
  }

  async function orderAction(action, o, confirmMsg) {
    if (confirmMsg && !confirm(confirmMsg + "\n\n" + (o.patient || "(no patient)") +
        (o.accession ? "  ·  ACC " + o.accession : ""))) return;
    try {
      const r = await post("/api/ris/orders/" + action, { id: o.id });
      flashNote(r.message || "Done", r.ok !== false);
      loadOrders();
      pollStatus();
    } catch (e) { flashNote(e.message, false); }
  }

  async function purgeClosedOrders() {
    if (!confirm("Delete ALL closed orders?\n\nThis permanently clears the closed-order audit trail.")) return;
    try {
      const r = await post("/api/ris/orders/purge", {});
      flashNote(r.message || "Purged", r.ok !== false);
      loadOrders();
      pollStatus();
    } catch (e) { flashNote(e.message, false); }
  }

  /* ── Workspace panels: inline tabs that pop out only on overflow ──
     Settings is ALWAYS a popup. History / Destinations / Logs / Pending /
     Stuck render inline in the workspace, and auto-promote to a centered
     popup only when their content is too tall to fit the viewport. Closing
     the popup (✕ / backdrop / Esc) keeps that panel inline (scrolling) for
     the rest of this activation. */
  const INLINE_PANELS = ["dlgHistory", "dlgOrders", "dlgPending", "dlgStuck", "dlgDests", "dlgLogs"];
  const SETTINGS_PANEL = "dlgSettings";
  const loaders = { dlgHistory: loadHistory, dlgOrders: loadOrders, dlgPending: loadPending, dlgStuck: loadStuck };
  let activeInline = "dlgHistory";
  let overlayId = null;
  const dismissed = new Set();

  function highlightTab(id) {
    document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.panel === id));
  }
  function setBackdrop(on) { const b = $("panelBackdrop"); if (b) b.hidden = !on; }

  function openOverlay(id) {
    const p = $(id); if (!p) return;
    p.hidden = false; p.classList.add("as-modal");
    overlayId = id; setBackdrop(true);
  }
  function closeOverlay() {
    if (!overlayId) return;
    const p = $(overlayId);
    if (p) p.classList.remove("as-modal");
    if (overlayId === SETTINGS_PANEL) { if (p) p.hidden = true; }
    else { dismissed.add(overlayId); }   // user prefers inline scroll this time
    overlayId = null; setBackdrop(false);
    highlightTab(activeInline);
  }

  function panelOverflows(id) {
    // "Overflow" = the content is taller than the panel's inline scroll area
    // (the CSS max-height on .modal-body). Measured inline only, so it's
    // independent of viewport quirks and how tall the cards above happen to be.
    const p = $(id); if (!p || p.hidden || p.classList.contains("as-modal")) return false;
    const body = p.querySelector(".modal-body"); if (!body) return false;
    return body.scrollHeight > body.clientHeight + 4;
  }
  function maybeOverflow(id) {
    if (id !== activeInline || overlayId === id || dismissed.has(id)) return;
    if (panelOverflows(id)) openOverlay(id);
  }
  function reflowActive() { maybeOverflow(activeInline); }

  function showInline(id) {
    closeOverlay();
    activeInline = id;
    dismissed.delete(id);
    INLINE_PANELS.forEach((pid) => { const p = $(pid); if (p) { p.classList.remove("as-modal"); p.hidden = pid !== id; } });
    $(SETTINGS_PANEL).hidden = true;
    highlightTab(id);
    if (loaders[id]) loaders[id]();                     // async panels re-check on render
    requestAnimationFrame(() => maybeOverflow(id));     // sync panels (Logs/Dests) check now
  }
  function showSettings() {
    $(SETTINGS_PANEL).hidden = false;
    openOverlay(SETTINGS_PANEL);
    highlightTab(SETTINGS_PANEL);
  }

  /* ── Wire up ─────────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", () => {
    $("killSvc").addEventListener("click", killService);
    $("rxToggle").addEventListener("click", (e) => toggle("receiver", e.target));
    $("wxToggle").addEventListener("click", (e) => toggle("watcher", e.target));
    $("pxToggle").addEventListener("click", (e) => {
      // Starting from the card also flips the "start on launch" flag so it
      // survives a restart (toggle() persists the config before starting).
      if (e.target.dataset.on !== "true") $("prnEnabled").checked = true;
      toggle("printer", e.target);
    });
    $("rsToggle").addEventListener("click", (e) => {
      // Starting from the card also flips the "start on launch" flag (toggle()
      // persists the config before starting, like the printer card).
      if (e.target.dataset.on !== "true") $("risEnabled").checked = true;
      toggle("ris", e.target);
    });
    $("addDest").addEventListener("click", () => addDestRow({ enabled: true }));
    $("saveCfg").addEventListener("click", () => saveConfig().then((ok) => { if (ok) closeOverlay(); }));
    $("saveDests").addEventListener("click", () => saveConfig());
    $("clearLog").addEventListener("click", () => { $("log").innerHTML = ""; });
    wireDropZones();

    // Tab strip: Settings always pops; the rest render inline (pop out on overflow).
    document.querySelectorAll(".tabbtn").forEach((b) =>
      b.addEventListener("click", () => {
        const id = b.dataset.panel;
        if (id === SETTINGS_PANEL) showSettings(); else showInline(id);
      }));
    // Close an open popup: ✕ button, backdrop click, or Escape.
    document.querySelectorAll("[data-demote]").forEach((x) => x.addEventListener("click", closeOverlay));
    $("panelBackdrop").addEventListener("click", closeOverlay);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && overlayId) closeOverlay(); });
    window.addEventListener("resize", reflowActive);

    // History Received/Sent sub-tabs.
    document.querySelectorAll(".hist-tab").forEach((tab) =>
      tab.addEventListener("click", () => {
        document.querySelectorAll(".hist-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        histGroup = tab.dataset.group;
        loadHistory();
      }));
    $("histRefresh").addEventListener("click", loadHistory);
    $("histDeleteAll").addEventListener("click", histDeleteAll);
    // RIS orders: Open/Closed sub-tabs + form + actions.
    document.querySelectorAll("#dlgOrders .hist-tab").forEach((tab) =>
      tab.addEventListener("click", () => {
        document.querySelectorAll("#dlgOrders .hist-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        orderStatus = tab.dataset.ostatus;
        loadOrders();
      }));
    $("ordAdd").addEventListener("click", () => addOrder($("ordAdd")));
    $("ordRefresh").addEventListener("click", loadOrders);
    $("ordPurge").addEventListener("click", purgeClosedOrders);
    $("pendRefresh").addEventListener("click", loadPending);
    $("stuckRefresh").addEventListener("click", loadStuck);
    $("stuckRetryAll").addEventListener("click", () => retryStuck(null, $("stuckRetryAll")));

    loadConfig().catch((e) => flashNote("Load failed: " + e.message, false));
    showInline("dlgHistory");   // default workspace view
    pollStatus();
    pollLog();
    statusTimer = setInterval(pollStatus, 2000);
    logTimer = setInterval(pollLog, 1500);
  });
})();
