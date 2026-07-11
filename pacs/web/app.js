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

  let loadedWeb = { host: "127.0.0.1", port: 8042 }; // preserved across saves
  let statusTimer = null, logTimer = null;
  let editorUrl = "";                                // DICOM-editor base URL (from status); "" hides ✎ Edit

  /* ── Status polling ──────────────────────────────────────────── */
  function renderStatus(s) {
    const rx = s.receiver, wx = s.watcher;

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
      let sawStore = false, sawSend = false;
      for (const e of data.entries) {
        logSeq = e.seq;
        if (e.kind === "store") sawStore = true;   // a file was received
        if (e.kind === "send") sawSend = true;      // a file was forwarded
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
      }
      firstLog = false;
    } catch (e) { /* ignore */ }
  }

  /* ── Config load / populate ──────────────────────────────────── */
  async function loadConfig() {
    const c = await api("/api/config");
    loadedWeb = c.web || loadedWeb;
    $("scpAet").value = c.scp.aet;
    $("scpBind").value = c.scp.bind;
    $("scpPort").value = c.scp.port;
    $("scpDir").value = c.scp.storage_dir;
    $("scpOrganize").checked = !!c.scp.organize;
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
    renderDests(c.destinations || []);
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
    return {
      scp: {
        aet: $("scpAet").value.trim(),
        bind: $("scpBind").value.trim() || "0.0.0.0",
        port: parseInt($("scpPort").value, 10),
        storage_dir: $("scpDir").value.trim(),
        organize: $("scpOrganize").checked,
        allowed_aets: allowed,
        tls: $("scpTls").checked,
        tls_cert: $("scpTlsCert").value.trim(),
        tls_key: $("scpTlsKey").value.trim(),
        tls_ca: $("scpTlsCa").value.trim(),
      },
      scu: {
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
      destinations: collectDests(),
      web: loadedWeb,
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
    // A modal <dialog> renders in the browser "top layer", above every normal
    // element (even z-index:max). Re-parent the toast into the open dialog so
    // notifications show ON TOP of the popup instead of hidden behind it.
    const openDlgs = document.querySelectorAll("dialog[open]");
    const host = openDlgs.length ? openDlgs[openDlgs.length - 1] : document.body;
    if (t.parentNode !== host) host.appendChild(t);
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
    document.querySelectorAll(".modal").forEach((d) => { if (d.open) d.close(); });
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

  // Open a study in DICOM-editor via deep-link. We hand the editor a manifest
  // URL (absolute, this dashboard's origin); it fetches each DICOM and loads it.
  function histEdit(s) {
    if (!editorUrl) return;
    const manifest = location.origin + "/api/studies/files?group=" +
      encodeURIComponent(histGroup) + "&path=" + encodeURIComponent(s.path);
    const sep = editorUrl.includes("#") ? "&" : "#";
    window.open(editorUrl + sep + "load=" + encodeURIComponent(manifest), "_blank", "noopener");
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

  async function loadPending() {
    const list = $("pendingList");
    list.innerHTML = "<div class='hist-empty'>Loading…</div>";
    try {
      const data = await api("/api/pending");
      renderPending(data.items || []);
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

  /* ── Wire up ─────────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", () => {
    $("killSvc").addEventListener("click", killService);
    $("rxToggle").addEventListener("click", (e) => toggle("receiver", e.target));
    $("wxToggle").addEventListener("click", (e) => toggle("watcher", e.target));
    $("addDest").addEventListener("click", () => addDestRow({ enabled: true }));
    $("saveCfg").addEventListener("click", () => saveConfig().then((ok) => { if (ok) $("dlgSettings").close(); }));
    $("saveDests").addEventListener("click", () => saveConfig().then((ok) => { if (ok) $("dlgDests").close(); }));
    $("clearLog").addEventListener("click", () => { $("log").innerHTML = ""; });
    wireDropZones();

    // Popup windows: buttons → native <dialog> modals.
    const openMap = { openSettings: "dlgSettings", openDests: "dlgDests", openLogs: "dlgLogs" };
    Object.keys(openMap).forEach((b) =>
      $(b).addEventListener("click", () => { const d = $(openMap[b]); if (d && !d.open) d.showModal(); }));
    document.querySelectorAll(".modal").forEach((dlg) => {
      dlg.querySelectorAll("[data-close]").forEach((x) => x.addEventListener("click", () => dlg.close()));
      dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // click backdrop to close
      // when a modal closes, return the toast to <body> so it isn't stuck hidden inside it
      dlg.addEventListener("close", () => document.body.appendChild($("toast")));
    });

    // History popup: open + load, tab switching, refresh, delete-all.
    $("openHistory").addEventListener("click", () => {
      const d = $("dlgHistory"); if (d && !d.open) d.showModal();
      loadHistory();
    });
    document.querySelectorAll(".hist-tab").forEach((tab) =>
      tab.addEventListener("click", () => {
        document.querySelectorAll(".hist-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        histGroup = tab.dataset.group;
        loadHistory();
      }));
    $("histRefresh").addEventListener("click", loadHistory);
    $("histDeleteAll").addEventListener("click", histDeleteAll);

    // Pending-imports popup: open + load, refresh.
    $("openPending").addEventListener("click", () => {
      const d = $("dlgPending"); if (d && !d.open) d.showModal();
      loadPending();
    });
    $("pendRefresh").addEventListener("click", loadPending);

    loadConfig().catch((e) => flashNote("Load failed: " + e.message, false));
    pollStatus();
    pollLog();
    statusTimer = setInterval(pollStatus, 2000);
    logTimer = setInterval(pollLog, 1500);
  });
})();
