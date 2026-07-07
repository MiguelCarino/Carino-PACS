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

  /* ── Status polling ──────────────────────────────────────────── */
  function renderStatus(s) {
    const rx = s.receiver, wx = s.watcher;
    $("rxDot").className = "dot " + (rx.running ? "on" : "off");
    $("rxAet").textContent = rx.aet;
    $("rxAddr").textContent = `${rx.bind}:${rx.port}`;
    $("rxDir").textContent = rx.storage_dir;
    $("rxCount").textContent = rx.received;
    $("rxErr").textContent = rx.errors;
    $("rxTls").textContent = rx.tls ? (rx.tls_mutual ? "TLS (mutual)" : "TLS") : "plaintext";
    setToggle($("rxToggle"), rx.running);

    $("wxDot").className = "dot " + (wx.running ? "on" : "off");
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
  async function pollStatus() {
    try { renderStatus(await api("/api/status")); } catch (e) { /* keep last */ }
  }

  /* ── Log polling ─────────────────────────────────────────────── */
  let logSeq = 0;
  async function pollLog() {
    try {
      const data = await api("/api/log?since=" + logSeq);
      const box = $("log");
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
      for (const e of data.entries) {
        logSeq = e.seq;
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
    const n = $("saveNote");
    n.textContent = msg;
    n.className = "save-note " + (ok ? "ok" : "bad");
    clearTimeout(flashNote._t);
    flashNote._t = setTimeout(() => { n.textContent = ""; n.className = "save-note"; }, 6000);
  }

  async function saveConfig() {
    try {
      await post("/api/config", collectConfig());
      flashNote("Saved.", true);
      pollStatus();
    } catch (e) { flashNote(e.message, false); }
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

  /* ── Wire up ─────────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", () => {
    $("rxToggle").addEventListener("click", (e) => toggle("receiver", e.target));
    $("wxToggle").addEventListener("click", (e) => toggle("watcher", e.target));
    $("addDest").addEventListener("click", () => addDestRow({ enabled: true }));
    $("saveCfg").addEventListener("click", saveConfig);
    $("clearLog").addEventListener("click", () => { $("log").innerHTML = ""; });
    wireDropZones();

    loadConfig().catch((e) => flashNote("Load failed: " + e.message, false));
    pollStatus();
    pollLog();
    setInterval(pollStatus, 2000);
    setInterval(pollLog, 1500);
  });
})();
