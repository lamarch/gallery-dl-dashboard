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
