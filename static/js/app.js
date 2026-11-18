/* BrandBridge AI – shared UI behaviour. No secrets or API keys live in the browser. */
(function () {
  "use strict";

  // Toasts from server-side flash messages
  document.querySelectorAll(".toast[data-autoshow]").forEach(function (el) {
    new bootstrap.Toast(el, { delay: 6000 }).show();
  });

  // Mobile sidebar
  var sidebar = document.getElementById("bb-sidebar");
  var toggle = document.getElementById("bb-toggle");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () { sidebar.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (sidebar.classList.contains("open") && !sidebar.contains(e.target) && e.target !== toggle && !toggle.contains(e.target)) {
        sidebar.classList.remove("open");
      }
    });
  }

  // Loading overlay for slow forms (checking runs analysis on the server)
  var overlay = document.getElementById("bb-loading");
  document.querySelectorAll("form[data-loading]").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity()) { return; }
      var msg = overlay && overlay.querySelector(".msg");
      if (msg) { msg.textContent = form.getAttribute("data-loading"); }
      if (overlay) { overlay.classList.add("show"); }
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; });
    });
  });

  // Confirmation prompts
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { e.preventDefault(); e.stopImmediatePropagation(); }
    });
  });

  // Character counter
  document.querySelectorAll("textarea[data-counter]").forEach(function (ta) {
    var out = document.getElementById(ta.getAttribute("data-counter"));
    function upd() { if (out) { out.textContent = ta.value.length.toLocaleString() + " characters"; } }
    ta.addEventListener("input", upd); upd();
  });

  // Gauge fill
  document.querySelectorAll(".gauge[data-score]").forEach(function (g) {
    var s = parseInt(g.getAttribute("data-score"), 10) || 0;
    g.style.setProperty("--p", s);
    g.style.setProperty("--c", s >= 85 ? "#16A34A" : s >= 60 ? "#F59E0B" : "#DC2626");
  });

  // Charts (data always fetched from the database-backed endpoint)
  var PALETTE = ["#2563EB", "#0EA5E9", "#F59E0B", "#10B981", "#8B5CF6", "#EF4444", "#64748B"];
  window.BB = window.BB || {};
  window.BB.renderCharts = function (s) {
    if (typeof Chart === "undefined") { return; }
    function mk(id, cfg) {
      var el = document.getElementById(id);
      if (!el) { return; }
      cfg.options = Object.assign({ responsive: true, maintainAspectRatio: false }, cfg.options || {});
      new Chart(el, cfg);
    }
    var empty = function (obj) { return Object.values(obj).every(function (v) { return !v; }); };
    var show = function (id, hasData) {
      var el = document.getElementById(id);
      var ph = document.getElementById(id + "-empty");
      if (el && ph) { el.parentElement.style.display = hasData ? "" : "none"; ph.style.display = hasData ? "none" : ""; }
    };
    var st = s.status, labels = { checked: "Checked", pending_review: "Pending review", changes_requested: "Changes requested", approved: "Approved", rejected: "Rejected" };
    show("chartStatus", !empty(st));
    if (!empty(st)) {
      mk("chartStatus", { type: "doughnut", data: { labels: Object.keys(st).map(function (k) { return labels[k]; }),
        datasets: [{ data: Object.values(st), backgroundColor: ["#93C5FD", "#FBBF24", "#FB923C", "#22C55E", "#EF4444"] }] },
        options: { plugins: { legend: { position: "bottom" } }, cutout: "62%" } });
    }
    show("chartSeverity", !empty(s.severity));
    if (!empty(s.severity)) {
      mk("chartSeverity", { type: "bar", data: { labels: Object.keys(s.severity), datasets: [{ label: "Issues", data: Object.values(s.severity),
        backgroundColor: ["#DC2626", "#F59E0B", "#3B82F6"], borderRadius: 8 }] },
        options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } } });
    }
    show("chartCategories", !empty(s.categories));
    if (!empty(s.categories)) {
      mk("chartCategories", { type: "bar", data: { labels: Object.keys(s.categories), datasets: [{ label: "Issues", data: Object.values(s.categories),
        backgroundColor: PALETTE[0], borderRadius: 8 }] },
        options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } } });
    }
    var m = s.monthly, anyM = m.submissions.some(Boolean) || m.reviews.some(Boolean);
    show("chartMonthly", anyM);
    if (anyM) {
      mk("chartMonthly", { type: "bar", data: { labels: m.labels, datasets: [
        { label: "Submissions", data: m.submissions, backgroundColor: "#2563EB", borderRadius: 6 },
        { label: "Review decisions", data: m.reviews, backgroundColor: "#10B981", borderRadius: 6 }] },
        options: { plugins: { legend: { position: "bottom" } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } } });
    }
    show("chartTrend", s.score_trend.values.length > 0);
    if (s.score_trend.values.length) {
      mk("chartTrend", { type: "line", data: { labels: s.score_trend.labels, datasets: [{ label: "Average compliance score", data: s.score_trend.values,
        borderColor: "#2563EB", backgroundColor: "rgba(37,99,235,.12)", fill: true, tension: .3, pointRadius: 4 }] },
        options: { plugins: { legend: { display: false } }, scales: { y: { min: 0, max: 100 } } } });
    }
  };

  var dataUrl = document.body.getAttribute("data-analytics-url");
  if (dataUrl) {
    fetch(dataUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) { throw new Error("HTTP " + r.status); } return r.json(); })
      .then(window.BB.renderCharts)
      .catch(function () {
        document.querySelectorAll(".chart-box").forEach(function (b) {
          b.innerHTML = '<div class="empty"><i class="bi bi-exclamation-triangle"></i>Charts could not be loaded. Please refresh.</div>';
        });
      });
  }
})();
