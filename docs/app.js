/* Carino PACS landing page — detect the visitor's OS, highlight their download,
   and wire per-platform links to the latest GitHub release (fallback: Releases). */
(function () {
  "use strict";

  var REPO = "MiguelCarino/Carino-PACS";
  var RELEASES = "https://github.com/" + REPO + "/releases";

  var cards = {
    windows: document.querySelector('.dl-btn[data-os="windows"]'),
    macos: document.querySelector('.dl-btn[data-os="macos"]'),
    linux: document.querySelector('.dl-btn[data-os="linux"]'),
  };

  // Works even before JS / before any release exists.
  Object.keys(cards).forEach(function (k) { if (cards[k]) cards[k].href = RELEASES; });

  function detectOS() {
    var s = ((navigator.userAgentData && navigator.userAgentData.platform) ||
      navigator.platform || navigator.userAgent || "").toLowerCase();
    if (s.indexOf("win") >= 0) return "windows";
    if (s.indexOf("mac") >= 0 || s.indexOf("darwin") >= 0 || s.indexOf("iphone") >= 0 || s.indexOf("ipad") >= 0) return "macos";
    if (s.indexOf("linux") >= 0 || s.indexOf("x11") >= 0 || s.indexOf("android") >= 0) return "linux";
    return null;
  }

  var os = detectOS();
  if (os && cards[os]) cards[os].classList.add("recommended");

  function matchOS(name) {
    var n = name.toLowerCase();
    if (n.slice(-4) === ".exe") return "windows";
    if (n.slice(-4) === ".dmg") return "macos";
    if (n.slice(-9) === ".appimage") return "linux";
    return null;
  }

  function setDownload(k, url, name) {
    var btn = cards[k];
    if (!btn) return;
    btn.href = url;
    btn.title = name;
  }

  var vEl = document.getElementById("version");
  fetch("https://api.github.com/repos/" + REPO + "/releases/latest",
    { headers: { Accept: "application/vnd.github+json" } })
    .then(function (res) { if (!res.ok) throw new Error(String(res.status)); return res.json(); })
    .then(function (rel) {
      var found = 0;
      (rel.assets || []).forEach(function (a) {
        var k = matchOS(a.name);
        if (k) { setDownload(k, a.browser_download_url, a.name); found++; }
      });
      if (vEl) {
        vEl.innerHTML = found
          ? 'Latest release: <strong>' + rel.tag_name + '</strong> · <a href="' + RELEASES + '">all versions &amp; notes</a>'
          : 'Latest release <strong>' + rel.tag_name + '</strong> has no installers yet — <a href="' + RELEASES + '">see releases</a>.';
      }
    })
    .catch(function () {
      if (vEl) vEl.innerHTML = 'No public release yet. <a href="' + RELEASES + '">Check releases</a>, or build it from source below.';
    });
})();
