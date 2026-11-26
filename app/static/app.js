// Live header refresh + client-side countdowns + album filters. No dependencies.
(function () {
  "use strict";

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec));
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + String(m).padStart(2, "0") + "m";
    if (m) return m + "m " + String(s).padStart(2, "0") + "s";
    return s + "s";
  }

  function tick() {
    var now = Date.now();
    document.querySelectorAll("[data-until]").forEach(function (el) {
      var t = Number(el.dataset.until);
      if (t) el.textContent = fmt((t - now) / 1000);
    });
    document.querySelectorAll("[data-ago]").forEach(function (el) {
      var t = Number(el.dataset.ago);
      if (t) el.textContent = fmt((now - t) / 1000) + " ago";
    });
  }
  setInterval(tick, 1000);

  var hdr = document.getElementById("hdr");
  function refresh() {
    if (!hdr || document.hidden) return;
    fetch(hdr.dataset.src, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html) { hdr.innerHTML = html; tick(); } })
      .catch(function () {});
  }
  setInterval(refresh, 5000);
  document.addEventListener("visibilitychange", refresh);

  // album filters
  var key = "fbdl.filter";
  function applyFilter(f) {
    document.querySelectorAll("table.albums tbody tr").forEach(function (tr) {
      var show = f === "all" ||
        (f === "problem" && tr.dataset.problem === "yes") ||
        (f === tr.dataset.state) ||
        (f === "pending" && tr.dataset.state === "current");
      tr.hidden = !show;
    });
    document.querySelectorAll(".filters button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.filter === f));
    });
    try { localStorage.setItem(key, f); } catch (e) {}
  }
  document.querySelectorAll("[data-filter]").forEach(function (el) {
    el.addEventListener("click", function (ev) { ev.preventDefault(); applyFilter(el.dataset.filter); });
  });
  if (document.querySelector("table.albums")) {
    var saved = "all";
    try { saved = localStorage.getItem(key) || "all"; } catch (e) {}
    applyFilter(saved);
  }
})();

// Copy buttons on command boxes (<pre class="cmd">)
(function () {
  "use strict";
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    return ok;
  }
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; },
        function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }
  document.querySelectorAll("pre.cmd").forEach(function (pre) {
    var box = document.createElement("div");
    box.className = "cmdbox";
    pre.parentNode.insertBefore(box, pre);
    box.appendChild(pre);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copybtn";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy to clipboard");
    btn.addEventListener("click", function () {
      var text = pre.textContent.replace(/\s+$/, "") + (pre.textContent.indexOf("\n") >= 0 ? "\n" : "");
      copy(text).then(function (ok) {
        btn.textContent = ok ? "Copied ✓" : "Select & copy";
        if (!ok) {
          var r = document.createRange(); r.selectNodeContents(pre);
          var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
        }
        setTimeout(function () { btn.textContent = "Copy"; }, 1800);
      });
    });
    box.appendChild(btn);
  });
})();
