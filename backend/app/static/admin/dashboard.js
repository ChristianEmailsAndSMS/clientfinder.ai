// Clientfinder admin dashboard. Plain JS, no dependencies, no innerHTML: every value is inserted as text.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);

  // ---------- helpers ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
    }
    return el;
  }
  const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  const usd = (n) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : "never");

  async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "clientfinder" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let res, data = null;
    try { res = await fetch(path, opts); } catch (e) { return { ok: false, status: 0, data: { detail: "Network error" } }; }
    try { data = await res.json(); } catch (e) { /* empty body */ }
    return { ok: res.ok, status: res.status, data };
  }
  const detail = (r) => (r.data && typeof r.data.detail === "string" ? r.data.detail
    : r.data && Array.isArray(r.data.detail) ? r.data.detail.map((d) => d.msg).join("; ") : `Error ${r.status}`);

  function view(name) {
    $("loading").hidden = true;
    $("v-dash").hidden = name !== "dash";
  }
  let flashTimer;
  function flash(msg, bad) {
    const el = $("flash");
    el.textContent = msg; el.hidden = false; el.className = "flash " + (bad ? "bad" : "good");
    clearTimeout(flashTimer); flashTimer = setTimeout(() => { el.hidden = true; }, 6000);
  }
  function table(heads, rows) {
    return h("div", { class: "wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, heads.map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: heads.length, class: "muted" }, "Nothing here.")))));
  }

  // ---------- boot ----------
  // Sign-in lives on /login. Anyone who is not a signed-in admin is sent there and returned here afterwards.
  async function boot() {
    const me = await api("GET", "/auth/me");
    if (!me.ok || !me.data.is_admin) { location.replace("/login?next=" + encodeURIComponent("/admin")); return; }
    enterDashboard(me.data);
  }
  $("btn-logout").addEventListener("click", async () => { await api("POST", "/auth/logout"); location.replace("/login"); });
  $("btn-logout-all").addEventListener("click", async () => { await api("POST", "/auth/logout-all"); location.replace("/login"); });

  // ---------- dashboard shell ----------
  const renderers = { overview: renderOverview, sources: renderSources, failed: renderFailed, users: renderUsers, security: renderSecurity };
  function enterDashboard(me) {
    $("who").textContent = me.email;
    view("dash");
    openTab("overview");
  }
  function openTab(name) {
    for (const b of $("tabs").querySelectorAll("button")) b.classList.toggle("on", b.dataset.tab === name);
    for (const t of document.querySelectorAll(".tab")) t.hidden = t.id !== "tab-" + name;
    renderers[name]();
  }
  $("tabs").addEventListener("click", (e) => { const t = e.target.dataset && e.target.dataset.tab; if (t) openTab(t); });
  async function guarded(method, path, body) {
    const r = await api(method, path, body);
    if (r.status === 401) { flash("Your session ended. Please sign in again.", true); setTimeout(() => location.replace("/login?next=/admin"), 1200); }
    return r;
  }

  // ---------- overview ----------
  async function renderOverview() {
    const box = clear($("tab-overview"));
    const r = await guarded("GET", "/admin/overview");
    if (!r.ok) { box.appendChild(h("p", { class: "error" }, detail(r))); return; }
    const o = r.data;
    const stat = (n, l, cls) => h("div", { class: "stat" }, h("div", { class: "n " + (cls || "") }, n), h("div", { class: "l" }, l));
    box.appendChild(h("div", { class: "cards" },
      stat(o.jobs.total, "jobs in the database"),
      stat(o.jobs.new_24h, "new in the last 24h"),
      stat(o.customers.count, "customers"),
      stat(usd(o.customers.credit_liability_usd), "credit balances owed to customers"),
      stat(`${o.search_budget.used} / ${o.search_budget.budget}`, "Google searches this month"),
      stat(o.failed_urls_pending, "failed URLs waiting", o.failed_urls_pending ? "warn" : "good"),
      stat(o.backups.status, "backups", o.backups.status === "ok" ? "good" : "bad"),
    ));
    const problems = [];
    if (o.sources.failing.length) problems.push("Failing sources: " + o.sources.failing.join(", "));
    if (o.sources.stale.length) problems.push("No recent run: " + o.sources.stale.join(", "));
    if (o.backups.status !== "ok") problems.push("Backups are " + o.backups.status);
    box.appendChild(h("h2", {}, "Needs attention"));
    box.appendChild(problems.length ? h("ul", {}, problems.map((p) => h("li", { class: "bad" }, p))) : h("p", { class: "good" }, "Nothing flagged."));
  }

  // ---------- sources ----------
  async function renderSources() {
    const box = clear($("tab-sources"));
    const r = await guarded("GET", "/admin/sources");
    if (!r.ok) { box.appendChild(h("p", { class: "error" }, detail(r))); return; }
    const rows = r.data.map((s) => h("tr", {},
      h("td", {}, s.key), h("td", {}, s.kind), h("td", {}, s.total_jobs),
      h("td", {}, s.last_run ? h("span", { class: s.last_run.status === "ok" ? "good" : "bad" }, s.last_run.status) : "never"),
      h("td", {}, s.last_run ? when(s.last_run.at) : ""),
      h("td", {}, `${s.runs_24h} (${s.failed_24h} failed)`),
      h("td", {}, h("button", { class: "small", disabled: s.running, onclick: async () => {
        const x = await guarded("POST", `/admin/sources/${encodeURIComponent(s.key)}/run`);
        flash(x.ok ? `Started ${s.key}. Refresh in a minute.` : detail(x), !x.ok);
        setTimeout(renderSources, 1500);
      } }, s.running ? "Running…" : "Run now"))));
    box.appendChild(table(["Source", "Type", "Jobs", "Last status", "Last run", "Runs (24h)", ""], rows));
  }

  // ---------- failed URLs ----------
  async function renderFailed() {
    const box = clear($("tab-failed"));
    const r = await guarded("GET", "/admin/failed-urls?status=pending&limit=100");
    if (!r.ok) { box.appendChild(h("p", { class: "error" }, detail(r))); return; }
    const c = r.data.counts || {};
    box.appendChild(h("div", { class: "row" },
      h("p", { class: "muted" }, `Pending: ${c.pending || 0} · Dead (gave up after 5 tries): ${c.dead || 0} · Resolved: ${c.resolved || 0}`),
      h("button", { disabled: !(c.pending > 0), onclick: async () => {
        const x = await guarded("POST", "/admin/failed-urls/requeue", {});
        flash(x.ok ? "Requeue started. It uses fetch + Claude tokens but no search credits." : detail(x), !x.ok);
        setTimeout(renderFailed, 2000);
      } }, "Requeue all pending")));
    box.appendChild(table(["Platform", "URL", "Error", "Tries", "Last failed", ""], r.data.items.map((f) => h("tr", {},
      h("td", {}, f.platform), h("td", {}, f.url), h("td", {}, f.error || ""), h("td", {}, f.attempts), h("td", {}, when(f.last_failed_at)),
      h("td", {}, h("button", { class: "small", onclick: async () => {
        const x = await guarded("POST", "/admin/failed-urls/requeue", { ids: [f.id] });
        flash(x.ok ? "Retry started." : detail(x), !x.ok); setTimeout(renderFailed, 2000);
      } }, "Retry"))))));
  }

  // ---------- customers & credits ----------
  async function renderUsers(query) {
    const box = clear($("tab-users"));
    const search = h("input", { type: "search", placeholder: "Search by email", value: query || "" });
    box.appendChild(h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); renderUsers(search.value); } },
      search, h("button", { type: "submit" }, "Search")));
    const r = await guarded("GET", "/admin/users?limit=100" + (query ? "&q=" + encodeURIComponent(query) : ""));
    if (!r.ok) { box.appendChild(h("p", { class: "error" }, detail(r))); return; }
    box.appendChild(table(["ID", "Email", "Balance", "Status", "Last sign-in", ""], r.data.map((u) => h("tr", {},
      h("td", {}, u.id), h("td", {}, u.email + (u.is_admin ? " (admin)" : "")), h("td", {}, usd(u.balance_usd)),
      h("td", {}, u.is_active ? (u.locked ? h("span", { class: "warn" }, "locked") : "active") : h("span", { class: "bad" }, "suspended")),
      h("td", {}, when(u.last_login_at)),
      h("td", {}, h("button", { class: "small", onclick: () => manageUser(u.id) }, "Manage"))))));
    box.appendChild(h("div", { id: "user-panel" }));
  }
  async function manageUser(id) {
    const panel = clear($("user-panel"));
    const r = await guarded("GET", `/admin/users/${id}/ledger?limit=50`);
    if (!r.ok) { panel.appendChild(h("p", { class: "error" }, detail(r))); return; }
    const u = r.data.user;
    const amount = h("input", { name: "amount", inputmode: "decimal", placeholder: "10.00", required: true });
    const kind = h("select", { name: "kind" }, h("option", { value: "grant" }, "Add credit"), h("option", { value: "revoke" }, "Remove credit"), h("option", { value: "refund" }, "Refund"));
    const reason = h("input", { name: "reason", placeholder: "Why (required, kept in the audit log)", required: true, minlength: 3 });
    const act = async (path, body, msg) => { const x = await guarded("POST", path, body); flash(x.ok ? msg : detail(x), !x.ok); if (x.ok) manageUser(id); };
    panel.appendChild(h("h2", {}, `${u.email}: balance ${usd(u.balance_usd)}`));
    panel.appendChild(h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); act(`/admin/users/${id}/credits`, { amount_usd: amount.value, kind: kind.value, reason: reason.value }, "Credit updated."); } },
      h("label", {}, "Amount (USD)", amount), h("label", {}, "Action", kind), h("label", {}, "Reason", reason), h("button", { type: "submit" }, "Apply")));
    panel.appendChild(h("div", { class: "row" },
      h("button", { class: "ghost", onclick: () => act(`/admin/users/${id}/active`, { active: !u.is_active }, u.is_active ? "Account suspended." : "Account re-enabled.") }, u.is_active ? "Suspend account" : "Re-enable account"),
      h("button", { class: "ghost", onclick: () => act(`/admin/users/${id}/unlock`, {}, "Unlocked.") }, "Clear lockout"),
      h("button", { class: "ghost", onclick: () => act(`/admin/users/${id}/logout-everywhere`, {}, "All sessions ended.") }, "Sign out everywhere")));
    panel.appendChild(h("h2", {}, "Credit history"));
    panel.appendChild(table(["When", "Type", "Amount", "Balance after", "Reason"], r.data.entries.map((x) => h("tr", {},
      h("td", {}, when(x.at)), h("td", {}, x.kind), h("td", { class: x.amount_usd < 0 ? "bad" : "good" }, usd(x.amount_usd)),
      h("td", {}, usd(x.balance_after_usd)), h("td", {}, x.reason)))));
    panel.scrollIntoView({ behavior: "smooth" });
  }

  // ---------- security log ----------
  async function renderSecurity() {
    const box = clear($("tab-security"));
    const r = await guarded("GET", "/admin/security/events?limit=200");
    if (!r.ok) { box.appendChild(h("p", { class: "error" }, detail(r))); return; }
    box.appendChild(h("p", { class: "muted" }, "Sign-ins, failures, lockouts and every admin action. Repeated login_fail or setup_fail from one address means someone is guessing."));
    box.appendChild(table(["When", "Event", "Email", "IP", "Detail"], r.data.map((e) => h("tr", {},
      h("td", {}, when(e.at)), h("td", { class: /fail|lock/.test(e.event) ? "bad" : "" }, e.event),
      h("td", {}, e.email || ""), h("td", {}, e.ip || ""), h("td", {}, e.detail || "")))));
  }

  boot();
})();
