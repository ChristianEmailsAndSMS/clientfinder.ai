(() => {
  "use strict";
  const { h, api, detail, ago, money, num, clear, debounce } = CF;
  const $ = (id) => document.getElementById(id);
  const PAGE = 30;

  const state = { q: "", posted: "", types: new Set(), platforms: new Set(), tags: new Set(), roles: new Set(), regions: new Set(), kinds: new Set(), location: "", worldwide: true, unspecified: true, remote: "", hasPay: false, payPeriod: "", minPay: "", sort: "newest", offset: 0, total: 0, loading: false };
  let me = null, facets = null, loadToken = 0;
  let saved = new Set();                                   // ids of the jobs this customer bookmarked

  const SORTS = [["newest", "Newest first"], ["oldest", "Oldest first"], ["pay_high", "Highest pay"], ["pay_low", "Lowest pay"]];
  const PERIOD = { hour: "hr", year: "yr", project: "project" };
  // These tags only repeat what the type / remote / pay badges and filters already say, so we do not show them as topics.
  const ATTR_TAGS = new Set(["remote", "contract", "full-time", "has-pay"]);
  const ROLES = [["copywriting", "Copywriting", "copywriter"], ["email-marketing", "Email marketing", "email marketer"],
    ["lifecycle-crm", "Lifecycle / CRM", "lifecycle marketer"], ["funnels-landing-pages", "Funnels & landing pages", "funnel builder"],
    ["creative-strategy", "Creative strategy", "creative strategist"], ["direct-response", "Direct response", "direct response copywriter"],
    ["sms-marketing", "SMS marketing", "sms marketer"], ["growth-marketing", "Growth marketing", "growth marketer"]];
  const ROLE = Object.fromEntries(ROLES.map(([k, l, q]) => [k, { label: l, query: q }]));
  const KIND_HELP = { board: "Real job boards: structured listings with pay and company.", social: "Posts on Reddit, X, LinkedIn: people asking for help, often before a job is formally posted.", web: "Company career pages and other websites." };

  // ---------------------------------------------------------------- boot
  async function boot() {
    const r = await api("GET", "/auth/me");
    if (!r.ok) { location.replace("/login?next=" + encodeURIComponent("/app")); return; }
    me = r.data;
    readUrl(); buildHeader(); buildSort();
    if (me.onboarded && !location.search && !setupOff()) applyPrefs(me.prefs);       // returning customers land on their own setup, unless they cleared it this session
    $("btn-run").addEventListener("click", () => openSearch());
    $("btn-credits").addEventListener("click", openCredits);
    $("btn-filters").addEventListener("click", () => $("filters").classList.add("open"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("q").focus(); }
      if (e.key === "Escape") closeModal();
    });
    const sv = await api("GET", "/account/saved/ids"); if (sv.ok) saved = new Set(sv.data);
    const f = await api("GET", "/jobs/facets");
    facets = f.ok ? f.data : { total: 0, platforms: [], types: [], tags: [], pay_periods: [], kinds: [], regions: [] };
    buildFilters();
    loadJobs(true);
    if (!me.onboarded) openOnboarding(true);
  }

  function buildHeader() {
    const input = h("input", { class: "input", id: "q", type: "search", placeholder: "Search jobs…  ( / )", autocomplete: "off", "aria-label": "Search jobs", value: state.q });
    input.addEventListener("input", debounce(() => { state.q = input.value.trim(); loadJobs(true); }, 280));
    clear($("search-wrap")).append(CF.icon("search", 17), input);
    $("bal").textContent = me.unlimited ? "Unlimited" : money(me.balance_usd);

    const pop = h("div", { class: "pop", hidden: true },
      h("div", { class: "who", text: me.email }),
      me.is_admin ? h("a", { href: "/admin", text: "Admin dashboard" }) : null,
      h("button", { type: "button", text: "My job search setup", onclick: () => { pop.hidden = true; openOnboarding(false); } }),
      h("button", { type: "button", text: "Saved jobs", onclick: () => { pop.hidden = true; openSaved(); } }),
      h("button", { type: "button", text: "Credits & history", onclick: () => { pop.hidden = true; openCredits(); } }),
      h("button", { type: "button", text: "Sign out", onclick: async () => { if (await CF.signOut()) location.replace("/"); else CF.toast("Could not sign you out. Check your connection and try again.", "bad"); } }));
    const btn = h("button", { class: "btn icon", type: "button", "aria-haspopup": "true", "aria-label": "Account menu", text: me.email.slice(0, 1).toUpperCase(), onclick: (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; } });
    clear($("menu")).append(btn, pop);
    document.addEventListener("click", () => { pop.hidden = true; });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) { pop.hidden = true; btn.focus(); } });
  }

  function buildSort() {
    const s = $("sort");
    for (const [v, l] of SORTS) s.appendChild(h("option", { value: v, text: l, selected: v === state.sort }));
    s.addEventListener("change", () => { state.sort = s.value; loadJobs(true); });
  }

  // ---------------------------------------------------------------- filters
  function buildFilters() {
    const root = clear($("filters"));
    const toggle = (set, v, el) => { set.has(v) ? set.delete(v) : set.add(v); if (el) el.classList.toggle("on", set.has(v)); loadJobs(true); };

    root.appendChild(h("div", { class: "row mobile-only" }, h("strong", { text: "Filters" }), h("span", { class: "grow" }),
      h("button", { class: "btn sm", type: "button", text: "Done", onclick: () => root.classList.remove("open") })));

    root.appendChild(section("What you do", h("div", { class: "chips mt12" },
      ROLES.map(([k, l]) => { const b = h("button", { type: "button", class: "chip" + (state.roles.has(k) ? " on" : ""), text: l }); b.addEventListener("click", () => toggle(state.roles, k, b)); return b; }))));

    root.appendChild(section("Posted", seg([["", "Any"], ["1", "24h"], ["7", "7 days"], ["30", "30 days"]], () => state.posted, (v) => { state.posted = v; loadJobs(true); })));
    root.appendChild(section("Remote", seg([["", "Any"], ["true", "Remote"], ["false", "On-site"]], () => state.remote, (v) => { state.remote = v; loadJobs(true); })));

    if (facets.types.length) root.appendChild(section("Job type", h("div", { class: "chips mt12" },
      typeGroups().map((g) => { const on = () => g.values.some((v) => state.types.has(v));
        const b = h("button", { type: "button", class: "chip" + (on() ? " on" : ""), text: `${g.label} ${num(g.count)}` });
        b.addEventListener("click", () => { const was = on(); g.values.forEach((v) => (was ? state.types.delete(v) : state.types.add(v))); b.classList.toggle("on", !was); loadJobs(true); }); return b; }))));

    for (const k of facets.kinds || []) {
      const items = facets.platforms.filter((p) => p.kind === k.value);
      root.appendChild(section(`${k.label} · ${num(k.count)}`,
        h("p", { class: "hint", text: KIND_HELP[k.value] || "" }),
        items.map((p) => check(CF.PLATFORM[p.value] || p.value, p.count, state.platforms.has(p.value), () => toggle(state.platforms, p.value)))));
    }

    const loc = h("input", { class: "input", type: "search", maxlength: "60", placeholder: "City or country, e.g. Berlin", value: state.location, "aria-label": "Location" });
    loc.addEventListener("input", debounce(() => { state.location = loc.value.trim(); loadJobs(true); }, 350));
    const regs = (facets.regions || []).filter((r) => r.count > 0 || state.regions.has(r.value));
    if (regs.length) root.appendChild(section("Location & time zone", loc,
      h("div", { class: "mt12" }, regs.map((r) => check(r.label + (r.offset ? `  (${r.offset})` : ""), regionShown(r), state.regions.has(r.value), () => toggle(state.regions, r.value)))),
      check("Also show worldwide / anywhere jobs", null, state.worldwide, (on) => { state.worldwide = on; buildFilters(); loadJobs(true); }),
      check("Also show posts that do not say where", unspecifiedCount(), state.unspecified, (on) => { state.unspecified = on; buildFilters(); loadJobs(true); }),
      h("p", { class: "hint", text: "Worked out from the location written in each post, so it is approximate. Many posts name no place." })));

    const topics = facets.tags.filter((t) => !ATTR_TAGS.has(t.value));
    if (topics.length) root.appendChild(section("Topics (must have all)", h("div", { class: "chips mt12" },
      topics.map((t) => { const b = h("button", { type: "button", class: "chip" + (state.tags.has(t.value) ? " on" : ""), text: `${t.value} ${num(t.count)}` }); b.addEventListener("click", () => toggle(state.tags, t.value, b)); return b; }))));

    // $40 an hour and $40,000 a year cannot be compared, so a minimum always needs a period. Typing an amount picks the obvious one
    // (1,000 or more means per year, less means per hour) instead of leaving a greyed-out box that silently does nothing.
    const minPay = h("input", { class: "input", type: "number", min: "0", step: "1", placeholder: "Minimum pay, e.g. 50000", value: state.minPay, "aria-label": "Minimum pay" });
    minPay.addEventListener("input", debounce(() => {
      state.minPay = minPay.value;
      if (state.minPay !== "" && !state.payPeriod) { state.payPeriod = Number(state.minPay) >= 1000 ? "year" : "hour"; per.value = state.payPeriod; }
      loadJobs(true);
    }, 350));
    const per = h("select", { class: "input", "aria-label": "Pay period" }, [["", "Any pay period"], ["hour", "Per hour"], ["year", "Per year"], ["project", "Per project"]].map(([v, l]) => h("option", { value: v, text: l, selected: v === state.payPeriod })));
    per.addEventListener("change", () => {
      state.payPeriod = per.value;
      if (!per.value) { state.minPay = ""; minPay.value = ""; }
      loadJobs(true);
    });
    root.appendChild(section("Pay", check("Only jobs that list pay", null, state.hasPay, (on) => { state.hasPay = on; loadJobs(true); }), per, minPay, h("p", { class: "hint", text: "Counts a job when its pay range reaches this amount, so $10k to $50k still shows for a $50k minimum." })));

    root.appendChild(h("div", { class: "fsec" }, h("button", { class: "btn sm w100", type: "button", text: "Clear all filters", onclick: () => { setSetupOff(true); resetFilters(); buildFilters(); loadJobs(true); } })));
  }
  function section(title, ...kids) { return h("div", { class: "fsec" }, h("h4", { text: title }), kids); }
  function check(label, count, on, fn) {
    const input = h("input", { type: "checkbox", checked: on }); input.addEventListener("change", () => fn(input.checked));
    return h("label", { class: "check" }, input, h("span", { text: label }), count != null ? h("span", { class: "ct", text: num(count) }) : null);
  }
  function seg(options, get, set) {
    const wrap = h("div", { class: "seg" });
    const paint = () => { for (const b of wrap.children) b.classList.toggle("on", b.dataset.v === get()); };
    for (const [v, l] of options) wrap.appendChild(h("button", { type: "button", "data-v": v, text: l, onclick: () => { set(v); paint(); } }));
    paint(); return wrap;
  }
  // "part-time" and "part_time" are one filter; each distinct label is one chip that selects every raw value behind it.
  function typeGroups() {
    const by = new Map();
    for (const t of facets.types) { const label = CF.typeLabel(t.value); const g = by.get(label) || { label, values: [], count: 0 }; g.values.push(t.value); g.count += t.count; by.set(label, g); }
    return [...by.values()].sort((a, b) => b.count - a.count);
  }
  // "Clear all filters" must survive a reload: without this the saved setup came straight back and cut 403 jobs to 84.
  // Remembered for this browser tab only; saving the setup again (or signing in fresh) turns the saved setup back on.
  const setupOff = () => { try { return sessionStorage.getItem("cf_setup_off") === "1"; } catch (e) { return false; } };
  const setSetupOff = (on) => { try { on ? sessionStorage.setItem("cf_setup_off", "1") : sessionStorage.removeItem("cf_setup_off"); } catch (e) { /* private mode: it just comes back */ } };
  function resetFilters() {
    Object.assign(state, { posted: "", remote: "", hasPay: false, payPeriod: "", minPay: "", q: "", location: "", worldwide: true, unspecified: true });
    state.types.clear(); state.platforms.clear(); state.tags.clear(); state.roles.clear(); state.regions.clear(); state.kinds.clear();
    const q = $("q"); if (q) q.value = "";
  }

  // ---------------------------------------------------------------- query + URL
  function params(offset) {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    for (const v of state.platforms) p.append("platform", v);
    for (const v of state.types) p.append("type", v);
    for (const v of state.tags) p.append("tag", v);
    for (const v of state.roles) p.append("any_tag", v);
    for (const v of state.regions) p.append("region", v);
    for (const v of state.kinds) p.append("kind", v);
    if (state.regions.size && !state.worldwide) p.set("include_worldwide", "false");
    if (state.regions.size && !state.unspecified) p.set("include_unspecified", "false");
    if (state.location) p.set("location", state.location);
    if (state.remote) p.set("remote", state.remote);
    if (state.hasPay) p.set("has_pay", "true");
    if (state.payPeriod) p.set("pay_period", state.payPeriod);
    if (state.minPay !== "" && state.payPeriod && Number(state.minPay) >= 0) p.set("min_pay", state.minPay);       // pay amounts only mean something per period
    if (state.posted) p.set("posted_within_days", state.posted);
    if (state.sort !== "newest") p.set("sort", state.sort);
    if (offset != null) { p.set("limit", PAGE); p.set("offset", offset); }
    return p;
  }
  function writeUrl() { const s = params().toString(); history.replaceState(null, "", "/app" + (s ? "?" + s : "")); }
  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.q = p.get("q") || ""; state.remote = ["true", "false"].includes(p.get("remote")) ? p.get("remote") : "";
    state.hasPay = p.get("has_pay") === "true"; state.payPeriod = ["hour", "year", "project"].includes(p.get("pay_period")) ? p.get("pay_period") : "";
    state.minPay = state.payPeriod && /^\d+(\.\d+)?$/.test(p.get("min_pay") || "") ? p.get("min_pay") : "";
    state.posted = ["1", "7", "30"].includes(p.get("posted_within_days")) ? p.get("posted_within_days") : "";
    state.sort = SORTS.some(([v]) => v === p.get("sort")) ? p.get("sort") : "newest";
    state.location = (p.get("location") || "").slice(0, 60); state.worldwide = p.get("include_worldwide") !== "false"; state.unspecified = p.get("include_unspecified") !== "false";
    for (const [key, set] of [["platform", state.platforms], ["type", state.types], ["tag", state.tags], ["any_tag", state.roles], ["region", state.regions], ["kind", state.kinds]]) for (const v of p.getAll(key)) if (/^[\w\-]{1,40}$/.test(v)) set.add(v);
  }

  // ---------------------------------------------------------------- results
  async function loadJobs(reset) {
    const token = ++loadToken;
    if (reset) { state.offset = 0; writeUrl(); renderActive(); const l = clear($("list")); for (let i = 0; i < 5; i++) l.appendChild(h("div", { class: "skel h96" })); clear($("more")); }
    state.loading = true;
    const r = await api("GET", "/jobs?" + params(state.offset).toString());
    if (token !== loadToken) return;                       // a newer request replaced this one
    state.loading = false;
    if (r.status === 401) { location.replace("/login?next=" + encodeURIComponent("/app")); return; }
    if (!r.ok) { clear($("list")).appendChild(empty("Could not load jobs", detail(r))); return; }
    state.total = Number(r.headers.get("X-Total-Count") || 0);
    $("count").textContent = `${num(state.total)} ${state.total === 1 ? "job" : "jobs"}`;
    const list = $("list");
    if (reset) clear(list);
    if (!r.data.length && reset) {
      list.appendChild(empty("No jobs match these filters", "Loosen a filter, or run a custom search to look for exactly this.", h("div", { class: "row mt16" },
        h("button", { class: "btn", type: "button", text: "Clear filters", onclick: () => { setSetupOff(true); resetFilters(); buildFilters(); loadJobs(true); } }),
        h("button", { class: "btn primary", type: "button", text: "Run a search", onclick: () => openSearch(state.q) }))));
    }
    for (const j of r.data) list.appendChild(jobCard(j));
    state.offset += r.data.length;
    const more = clear($("more"));
    if (state.offset < state.total && r.data.length) more.appendChild(h("button", { class: "btn", type: "button", text: `Show more (${num(state.total - state.offset)} left)`, onclick: () => loadJobs(false) }));
  }

  function renderActive() {
    const box = clear($("active"));
    const add = (label, fn) => box.appendChild(h("button", { type: "button", class: "chip on", title: "Remove filter", onclick: () => { fn(); buildFilters(); loadJobs(true); } }, label + "  ×"));
    if (state.q) add(`“${state.q}”`, () => { state.q = ""; $("q").value = ""; });
    for (const v of state.platforms) add(CF.PLATFORM[v] || v, () => state.platforms.delete(v));
    for (const v of state.types) add(CF.typeLabel(v), () => state.types.delete(v));
    for (const v of state.tags) add(v, () => state.tags.delete(v));
    for (const v of state.kinds) add(((facets.kinds || []).find((k) => k.value === v) || {}).label || v, () => state.kinds.delete(v));
    for (const v of state.roles) add((ROLE[v] || {}).label || v, () => state.roles.delete(v));
    for (const v of state.regions) add(((facets.regions || []).find((r) => r.value === v) || {}).label || v, () => state.regions.delete(v));
    if (state.location) add(`Location: ${state.location}`, () => { state.location = ""; });
    if (state.remote) add(state.remote === "true" ? "Remote" : "On-site", () => { state.remote = ""; });
    if (state.posted) add(state.posted === "1" ? "Last 24h" : `Last ${state.posted} days`, () => { state.posted = ""; });
    if (state.hasPay) add("Pay listed", () => { state.hasPay = false; });
    if (state.minPay !== "" && state.payPeriod) add(`Pay ≥ ${state.minPay}${state.payPeriod ? "/" + PERIOD[state.payPeriod] : ""}`, () => { state.minPay = ""; });
  }

  function payText(j) {
    const shown = CF.cleanPay(j.pay_text);
    if (shown && !/rate not stated/.test(shown)) return shown;
    if (j.pay_min == null && j.pay_max == null) return shown;
    const k = (n) => (n >= 1000 ? Math.round(n / 100) / 10 + "k" : String(n));
    const range = j.pay_min != null && j.pay_max != null && j.pay_max !== j.pay_min ? `${k(j.pay_min)}–${k(j.pay_max)}` : k(j.pay_max ?? j.pay_min);
    return `$${range}${j.pay_period ? "/" + PERIOD[j.pay_period] : ""}`;
  }
  function empty(title, text, extra) { return h("div", { class: "card empty" }, h("h3", { text: title }), h("p", { text }), extra); }

  // Bookmark toggle; "Saved jobs" in the account menu lists them.
  function saveButton(j, onUnsave) {
    const b = h("button", { class: "btn sm", type: "button", "aria-pressed": String(saved.has(j.id)) });
    const paint = () => { const on = saved.has(j.id); b.textContent = on ? "Saved" : "Save"; b.setAttribute("aria-pressed", String(on)); b.classList.toggle("on", on); b.title = on ? "Remove from your saved jobs" : "Save this job to come back to"; };
    b.addEventListener("click", async () => {
      const want = !saved.has(j.id); b.disabled = true;
      const r = await api(want ? "PUT" : "DELETE", "/account/saved/" + j.id);
      b.disabled = false;
      if (!r.ok) { CF.toast(detail(r), "bad"); if (r.status === 404) saved.delete(j.id); paint(); return; }
      want ? saved.add(j.id) : saved.delete(j.id); paint();
      if (!want && onUnsave) onUnsave();
    });
    paint(); return b;
  }

  async function openSaved() {
    const body = h("div", {}, h("span", { class: "spinner" }));
    modal("Saved jobs", body);
    const r = await api("GET", "/account/saved");
    if (!r.ok) {
      clear(body).append(h("p", { class: "error", text: detail(r) }));
      return;
    }
    saved = new Set(r.data.map((j) => j.id));
    const list = h("div", {});
    const note = () => { if (!list.children.length) clear(list).append(h("p", { class: "muted", text: "Nothing saved yet. Press Save on any job to keep it here." })); };
    for (const j of r.data) { const card = jobCard(j, () => { card.remove(); note(); }); list.appendChild(card); }
    clear(body).append(h("p", { class: "muted", text: `${r.data.length} saved ${r.data.length === 1 ? "job" : "jobs"}, most recently saved first.` }), list);
    note();
  }

  function jobCard(j, onUnsave) {
    const desc = h("div", { class: "desc", hidden: true, text: (j.description || j.raw_snippet || "No description captured.").slice(0, 900) });
    const safe = /^https?:\/\//i.test(j.source_url || "");
    const pay = payText(j);
    const toggle = () => {                                     // the title and the Details button open the same text; both say whether it is open
      desc.hidden = !desc.hidden;
      titleBtn.setAttribute("aria-expanded", String(!desc.hidden));
      detailsBtn.setAttribute("aria-expanded", String(!desc.hidden)); detailsBtn.textContent = desc.hidden ? "Details" : "Hide details";
    };
    const titleBtn = h("button", { class: "title", type: "button", text: j.title, "aria-expanded": "false", onclick: toggle });
    const detailsBtn = h("button", { class: "btn sm", type: "button", "aria-expanded": "false", text: "Details", onclick: toggle });
    return h("article", { class: "card job" },
      h("div", { class: "top" }, titleBtn, h("span", { class: "when", text: ago(j.posted_at || j.first_seen_at) })),
      h("div", { class: "meta" }, CF.platformBadge(j.platform),
        j.company_or_poster ? h("span", { text: j.company_or_poster }) : null,
        j.location ? [j.company_or_poster ? h("span", { class: "sep", text: "·" }) : null, h("span", { text: j.location })] : null),
      h("div", { class: "meta" },
        pay ? h("span", { class: "chip pay", text: pay }) : null,
        j.type ? h("span", { class: "chip type", text: CF.typeLabel(j.type) }) : null,
        j.remote ? h("span", { class: "chip remote", text: "Remote" }) : null,
        (j.tags || []).filter((t) => !ATTR_TAGS.has(t)).slice(0, 4).map((t) => h("span", { class: "chip tag", text: t }))),
      desc,
      h("div", { class: "actions" },
        safe ? h("a", { class: "btn sm primary", href: j.source_url, target: "_blank", rel: "noopener noreferrer nofollow" }, "Open post ", CF.icon("out", 14)) : null,
        detailsBtn,
        h("button", { class: "btn sm", type: "button", title: "First message, cover letter, resume pitch and reply help for this job", onclick: () => openPitch(j) }, "Pitch help"),
        saveButton(j, onUnsave),
        safe ? h("button", { class: "btn sm icon", type: "button", "aria-label": "Copy link", onclick: () => CF.copy(j.source_url) }, CF.icon("copy", 14)) : null));
  }

  // ---------------------------------------------------------------- onboarding
  // What the list returns for a region right now, given the two "also show" switches.
  const unspecifiedCount = () => ((facets.regions || []).find((r) => r.value === "unspecified") || {}).count || 0;
  const regionShown = (r, ww = state.worldwide, un = state.unspecified) => {
    if (r.value === "worldwide" || r.value === "unspecified") return r.count;
    return (ww && r.count_with_worldwide != null ? r.count_with_worldwide : r.count) + (un ? unspecifiedCount() : 0);
  };
  function applyPrefs(p) {
    p = p || {};
    state.roles = new Set(p.roles || []); state.regions = new Set(p.regions || []); state.kinds = new Set(p.kinds || []);
    state.remote = ["true", "false"].includes(p.remote) ? p.remote : "";
  }

  function openOnboarding(first) {
    const kindsAll = (facets.kinds || []).map((k) => k.value);
    const draft = { roles: new Set(me.prefs.roles || []), regions: new Set(me.prefs.regions || []), kinds: new Set(me.prefs.kinds || []), all: !(me.prefs.kinds || []).length, remote: me.prefs.remote || "" };
    let step = 0;
    const tile = (label, sub, on, fn) => { const b = h("button", { type: "button", class: "tile" + (on ? " on" : ""), "aria-pressed": String(on) }, h("strong", { text: label }), sub ? h("small", { text: sub }) : null);
      b.addEventListener("click", () => { const now = fn(); b.classList.toggle("on", now); b.setAttribute("aria-pressed", String(now)); }); return b; };
    const flip = (set, v) => () => { set.has(v) ? set.delete(v) : set.add(v); return set.has(v); };
    const steps = [
      { title: "What kind of work do you do?", sub: "Pick everything you sell. We will show the jobs and posts that need it.",
        body: () => h("div", { class: "tiles" }, ROLES.map(([k, l]) => tile(l, null, draft.roles.has(k), flip(draft.roles, k)))), ok: () => draft.roles.size > 0, why: "Pick at least one so we know what to look for." },
      { title: "Where do you want your clients to be based?", sub: "Pick the time zones you want to work with. Posts that do not say where are still shown. Leave all unselected if clients anywhere are fine.",
        body: () => h("div", {}, h("div", { class: "tiles" }, (facets.regions || []).filter((r) => r.value !== "unspecified" && r.value !== "worldwide").map((r) => tile(r.label, [r.offset, `${num(regionShown(r, true, true))} jobs`].filter(Boolean).join(" · "), draft.regions.has(r.value), flip(draft.regions, r.value)))),
          h("div", { class: "mt16" }, h("div", { class: "hint", text: "On-site or remote?" }), (() => { const wrap = h("div", { class: "seg" }); const paint = () => { for (const b of wrap.children) b.classList.toggle("on", b.dataset.v === draft.remote); };
            for (const [v, l] of [["", "Either"], ["true", "Remote only"], ["false", "On-site"]]) wrap.appendChild(h("button", { type: "button", "data-v": v, text: l, onclick: () => { draft.remote = v; paint(); } })); paint(); return wrap; })())) },
      { title: "Where should we look?", sub: "Choose All of them, or pick just the places you want. Job boards list real openings; social posts are people asking for help, often before a job is formally posted.",
        body: () => h("div", { class: "tiles" },
            tile("All of them", "Search job boards, social media and other websites at once", draft.all, () => { draft.all = true; draft.kinds.clear(); paint(); return true; }),
            (facets.kinds || []).map((k) => tile(k.label, `${num(k.count)} jobs · ${KIND_HELP[k.value]}`, !draft.all && draft.kinds.has(k.value), () => {
              if (draft.all) { draft.all = false; draft.kinds = new Set([k.value]); } else { draft.kinds.has(k.value) ? draft.kinds.delete(k.value) : draft.kinds.add(k.value); }
              if (draft.kinds.size === 0 || draft.kinds.size === kindsAll.length) { draft.all = true; draft.kinds.clear(); }      // none or every place is the same as "All of them"
              paint(); return !draft.all && draft.kinds.has(k.value); }))),
        ok: () => true },
    ];
    const root = clear($("modal-root"));
    const body = h("div", { class: "body" }), foot = h("div", { class: "row end mt20" });
    const dots = h("div", { class: "dots" });
    const m = h("div", { class: "modal wiz", role: "dialog", "aria-modal": "true", "aria-label": "Set up your job search" },
      h("header", {}, h("h2", { text: first ? "Welcome to Clientfinder" : "Your job search setup" }), dots, first ? null : h("button", { class: "btn sm icon", type: "button", "aria-label": "Close", onclick: closeModal }, CF.icon("x", 16))), body);
    const ov = h("div", { class: "overlay" }, m); root.appendChild(ov);

    // "See my matches" means the jobs for the setup just chosen: clear whatever search or filters were on screen, and say so.
    function showMine() {
      const hadSearch = !!(state.q || state.location || state.posted || state.hasPay || state.minPay !== "" || state.types.size || state.platforms.size || state.tags.size);
      setSetupOff(false); resetFilters(); applyPrefs(me.prefs); closeModal(); buildFilters(); loadJobs(true);
      if (hadSearch) CF.toast("Showing jobs for your setup. Your earlier search and filters were cleared.", "good");
    }
    async function save(skip) {
      const prefs = skip ? { roles: [], regions: [], kinds: [], remote: "" } : { roles: [...draft.roles], regions: [...draft.regions], kinds: draft.all ? [] : [...draft.kinds], remote: draft.remote };
      const r = await api("PUT", "/account/prefs", prefs);
      if (!r.ok) { CF.toast(detail(r), "bad"); return false; }
      me.onboarded = true; me.prefs = r.data.prefs; return true;
    }
    function paint() {
      clear(dots).append(...steps.map((_, i) => h("i", { class: i <= step ? "on" : "" })), h("i", { class: step === steps.length ? "on" : "" }));
      clear(body); clear(foot);
      if (step < steps.length) {
        const st = steps[step], err = h("p", { class: "error", hidden: true });
        body.append(h("h3", { class: "wtitle", text: st.title }), h("p", { class: "muted", text: st.sub }), st.body(), err, foot);
        CF.put(foot, first && step === 0 ? h("button", { class: "btn", type: "button", text: "Skip for now", onclick: async () => { if (await save(true)) closeModal(); } }) : step > 0 ? h("button", { class: "btn", type: "button", text: "Back", onclick: () => { step--; paint(); } }) : null,
          h("span", { class: "grow" }),
          h("button", { class: "btn primary", type: "button", text: step === steps.length - 1 ? (first ? "Finish" : "Save and show matches") : "Next",
            onclick: async () => {
              if (st.ok && !st.ok()) { err.textContent = st.why; err.hidden = false; return; }
              if (step === steps.length - 1 && !first) { if (await save(false)) { showMine(); CF.toast("Setup saved", "good"); } return; }      // already set up: no second screen
              step++; paint(); } }));
        return;
      }
      const picked = [...draft.roles].map((r) => (ROLE[r] || {}).label || r).join(", ") || "any role";
      const firstQuery = (ROLE[[...draft.roles][0]] || {}).query || "";           // the service name, e.g. "email marketer"
      body.append(h("h3", { class: "wtitle", text: "You are all set" }), h("p", { class: "muted", text: `Looking for: ${picked}.` }),
        h("div", { class: "sresult" }, h("strong", { text: "Want the freshest posts?" }), h("p", { class: "muted", text: "Our database updates through the day. For something brand new, run your own live search: we search the web right now and add what we find. " + (me.balance_usd > 0 || me.unlimited ? (me.unlimited ? "Your searches are free on this account." : `You have ${money(me.balance_usd)} in credit.`) : "It costs a little credit; browsing is always free.") })),
        foot);
      foot.append(h("button", { class: "btn", type: "button", text: "Show my matches", onclick: async () => { if (await save(false)) showMine(); } }),
        h("span", { class: "grow" }),
        h("button", { class: "btn primary", type: "button", text: "Run a live search now", onclick: async () => { if (await save(false)) { showMine(); openSearch(firstQuery); } } }));
    }
    paint();
  }

  // ---------------------------------------------------------------- modals
  function closeModal() { clear($("modal-root")); }
  function modal(title, ...body) {
    const root = clear($("modal-root"));
    const m = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("header", {}, h("h2", { text: title }), h("button", { class: "btn sm icon", type: "button", "aria-label": "Close", onclick: closeModal }, CF.icon("x", 16))),
      h("div", { class: "body" }, body));
    const ov = h("div", { class: "overlay", onclick: (e) => { if (e.target === ov) closeModal(); } }, m);
    root.appendChild(ov);
    return m;
  }
  async function refreshBalance() { const r = await api("GET", "/auth/me"); if (r.ok) { me = r.data; $("bal").textContent = me.unlimited ? "Unlimited" : money(me.balance_usd); } }

  // ---- credits
  async function openCredits() {
    const body = h("div", {}, h("span", { class: "spinner" }));
    modal("Credits", body);
    const r = await api("GET", "/account/credits?limit=30");
    if (!r.ok) { clear(body).append(h("p", { class: "error", text: detail(r) })); return; }
    const buyErr = h("p", { class: "error", hidden: true });
    const bundleTile = (b) => {
      const kids = [h("strong", { text: "$" + b.usd }), h("small", { text: b.searches ? `about ${num(b.searches)} searches` : "credit for searches and pitch drafts" })];
      if (b.url && /^https:\/\//.test(b.url)) return h("a", { class: "tile bundle", href: b.url, target: "_blank", rel: "noopener noreferrer" }, kids);
      const t = h("button", { class: "tile bundle", type: "button" }, kids);
      t.addEventListener("click", async () => {                         // a fresh checkout link is created for this account on every click
        t.disabled = true; buyErr.hidden = true;
        const c = await api("POST", "/account/checkout", { usd: b.usd });
        t.disabled = false;
        if (c.ok && /^https:\/\//.test(c.data.url || "")) { window.open(c.data.url, "_blank", "noopener,noreferrer"); return; }
        buyErr.textContent = c.ok ? "Could not start checkout." : detail(c); buyErr.hidden = false;
      });
      return t;
    };
    clear(body).append(
      h("div", { class: "bal grad", text: me.unlimited ? "Unlimited" : money(r.data.balance_usd) }),
      h("p", { class: "muted", text: "Credits pay for searches you run yourself. Browsing and filtering the database is always free." }),
      r.data.bundles && r.data.bundles.length ? h("div", { class: "mt16" }, h("strong", { text: "Buy credits" }),
        h("p", { class: "muted", text: "You get credit equal to what you pay, and it never expires. At checkout, use this email so we can match the payment to your account:" }),
        h("div", { class: "row" }, h("strong", { text: me.email }), h("button", { class: "btn sm", type: "button", text: "Copy email", onclick: () => CF.copy(me.email) })),
        h("div", { class: "tiles" }, r.data.bundles.map((b) => bundleTile(b))), buyErr) : "",
      r.data.bundles && r.data.bundles.length ? "" : h("div", { class: "sresult mt16" }, h("strong", { text: "Adding credits" }),
        h("p", { class: "muted", text: r.data.buy_url ? "Pay on the secure checkout page. Credits are added to your account once the payment is confirmed. If they are not there within a day, email christian@emailsandsms.com." : "Card payments are launching soon. Until then, email christian@emailsandsms.com and we will top up your account." }),
        r.data.buy_url && /^https:\/\//.test(r.data.buy_url) ? h("a", { class: "btn primary mt12", href: r.data.buy_url, target: "_blank", rel: "noopener noreferrer", text: "Buy credits" }) : null),
      h("h3", { class: "mt20", text: "History" }),
      r.data.entries.length ? h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["When", "What", "Amount", "Balance"].map((x) => h("th", { text: x })))),
        h("tbody", {}, r.data.entries.map((e) => h("tr", {}, h("td", { text: ago(e.at) }), h("td", { text: e.reason || e.kind }),
          h("td", { class: e.amount_usd < 0 ? "bad" : "good", text: money(e.amount_usd, 4) }), h("td", { text: money(e.balance_after_usd, 4) }))))) : h("p", { class: "muted", text: "No activity yet." }));
  }

  // ---- pitch helper
  const PITCH_MODES = [["first_message", "First message", "The opening DM or email, two versions"], ["cover_letter", "Cover letter", "Short and specific to this job"],
    ["resume", "Resume pitch", "Summary and bullets tailored to this job"], ["examples", "What to send", "Work to link and a quick sample to make"],
    ["reply", "They replied", "Paste or screenshot their reply, get what to say back"]];

  async function openPitch(job) {
    const [cfgR, profR] = await Promise.all([api("GET", "/assist/config"), api("GET", "/account/profile")]);
    if (!cfgR.ok) { CF.toast(detail(cfgR), "bad"); return; }
    const cfg = cfgR.data, prof = profR.ok ? profR.data : { about: "", links: "" };
    const st = { mode: "first_message", image: null };
    const about = h("textarea", { class: "input", rows: "5", maxlength: "6000", placeholder: "Paste your resume or write a few lines: what you do, results you have got (numbers help), tools, rates, niches.", "aria-label": "Your background" }); about.value = prof.about;
    const links = h("input", { class: "input", type: "text", maxlength: "600", placeholder: "Portfolio / website / LinkedIn links", value: prof.links, "aria-label": "Your links" });
    const saved = h("span", { class: "hint" });
    const saveBtn = h("button", { class: "btn sm", type: "button", text: "Save background", onclick: async () => { const r = await api("PUT", "/account/profile", { about: about.value, links: links.value }); saved.textContent = r.ok ? "Saved." : detail(r); } });
    const bg = h("details", { class: "mt12", open: !prof.about },
      h("summary", { text: prof.about ? "Your background (used in every draft)" : "Start here: tell us about you so the drafts sound like you" }), about, links, h("div", { class: "row mt12" }, saveBtn, saved));

    const thread = h("textarea", { class: "input", rows: "5", maxlength: "5000", placeholder: "Paste what they wrote back (or add a screenshot below).", "aria-label": "Their reply" });
    const shot = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp", "aria-label": "Screenshot of the conversation" });
    const shotNote = h("span", { class: "hint" });
    shot.addEventListener("change", () => {
      const f = shot.files[0]; st.image = null; shotNote.textContent = "";
      if (!f) return;
      if (f.size > 3 * 1024 * 1024) { shotNote.textContent = "That image is over 3 MB. Crop it and try again."; shot.value = ""; return; }
      const rd = new FileReader();
      rd.onload = () => { st.image = String(rd.result).split(",")[1] || null; shotNote.textContent = st.image ? "Screenshot attached." : "Could not read that image."; };
      rd.readAsDataURL(f);
    });
    const replyBox = h("div", { class: "mt12", hidden: true }, thread, h("div", { class: "row mt12" }, shot, shotNote),
      h("p", { class: "hint", text: "Your text and screenshot are sent to our AI writer to read. We do not keep the screenshot. Crop out anything private first." }));
    const note = h("input", { class: "input mt12", type: "text", maxlength: "300", placeholder: "Anything else? e.g. keep it shorter, mention my Klaviyo case study", "aria-label": "Extra instruction" });
    const modeHint = h("p", { class: "hint" });
    const seg2 = h("div", { class: "chips mt12" });
    const paint = () => {
      clear(seg2).append(...PITCH_MODES.map(([k, l]) => h("button", { type: "button", class: "chip" + (st.mode === k ? " on" : ""), text: l, onclick: () => { st.mode = k; paint(); } })));
      replyBox.hidden = st.mode !== "reply"; modeHint.textContent = (PITCH_MODES.find((m) => m[0] === st.mode) || [])[2] || "";
    };
    const out = h("div", { class: "mt16", "aria-live": "polite" });
    const broke = !cfg.unlimited && cfg.balance_usd < cfg.price_usd;            // the button would only fail with "not enough credit"
    const go = h("button", { class: "btn primary w100 mt16", type: "button", disabled: !cfg.ready || broke, text: cfg.unlimited ? "Write it" : `Write it · about ${money(cfg.price_usd, 3)}` });
    const noCredit = broke ? h("div", { class: "sresult mt12" }, `You have ${money(cfg.balance_usd)}. A draft costs about ${money(cfg.price_usd, 3)}. `, h("button", { class: "btn sm", type: "button", text: "Add credits", onclick: () => openCredits() })) : null;
    go.addEventListener("click", async () => {
      go.disabled = true; clear(out).append(h("span", { class: "spinner" }), " Writing…");
      const r = await api("POST", "/assist", { mode: st.mode, job_id: job.id, thread: st.mode === "reply" ? thread.value : "", image_b64: st.mode === "reply" ? st.image : null, note: note.value });
      go.disabled = false;
      if (!r.ok) { CF.put(clear(out), h("div", { class: "sresult fail", text: detail(r) }), r.status === 402 ? h("button", { class: "btn sm mt12", type: "button", text: "Add credits", onclick: () => openCredits() }) : null); return; }
      me.balance_usd = r.data.balance_usd; if (!me.unlimited) $("bal").textContent = money(me.balance_usd);
      clear(out).append(h("div", { class: "draft", text: r.data.text }),
        h("div", { class: "row mt12" }, h("button", { class: "btn sm", type: "button", text: "Copy", onclick: () => CF.copy(r.data.text) }),
          h("span", { class: "hint", text: r.data.cost_usd ? `Charged ${money(r.data.cost_usd, 4)}` : "No charge" }),
          h("span", { class: "grow" }), job.source_url && /^https?:\/\//i.test(job.source_url) ? h("a", { class: "btn sm primary", href: job.source_url, target: "_blank", rel: "noopener noreferrer nofollow" }, "Open the post ", CF.icon("out", 14)) : null),
        h("p", { class: "hint", text: "Read it, add your real numbers where you see [brackets], and make it sound like you before you send." }));
    });
    modal("Pitch help",
      h("div", { class: "sresult" }, h("strong", { text: job.title }), h("div", { class: "muted", text: [job.company_or_poster, CF.PLATFORM[job.platform] || job.platform].filter(Boolean).join(" · ") })),
      !cfg.ready ? h("div", { class: "sresult fail mt12", text: cfg.reason }) : null,
      bg, h("div", { class: "hint mt16", text: "What do you need?" }), seg2, modeHint, replyBox, note, go, noCredit,
      h("p", { class: "hint", text: "A written draft costs a fraction of a cent. A live search costs more because it runs about 20 web searches and reads each post." }), out);
    paint();
  }

  // ---- run a search
  const SERVICES = ["copywriting", "copywriter", "email marketing", "email copywriter", "creative strategy", "creative strategist", "funnel builder"];
  const WINDOWS = { d: "Past day", w: "Past week", m: "Past month", m3: "Past 3 months", m6: "Past 6 months", m9: "Past 9 months", y: "Past year" };
  // The customer sells a service; the web search looks for people HIRING for it.
  const asSearch = (service) => {
    const t = service.trim();
    if (/\b(hiring|hire|looking for|need|needs|wanted|seeking)\b/i.test(t)) return t;
    if (!t) return "";
    // Quoted so it does not match the company Builder Funnel.
    const role = /^funnel builder$/i.test(t) ? '"funnel builder"' : t;
    return "hiring " + role;
  };

  async function openSearch(prefill) {
    const cfg = await api("GET", "/searches/config");
    if (!cfg.ok) { CF.toast(detail(cfg), "bad"); return; }
    const c = cfg.data;
    const s = { freshness: "m" };
    const input = h("input", { class: "input", type: "text", maxlength: "100", placeholder: "e.g. email copywriter", value: (prefill || "").replace(/^hiring\s+/i, ""), "aria-label": "The service you sell" });
    const win = h("select", { class: "input", "aria-label": "How far back to look" }, (c.windows || Object.keys(WINDOWS)).map((v) => h("option", { value: v, text: WINDOWS[v] || v, selected: v === s.freshness })));
    win.addEventListener("change", () => { s.freshness = win.value; estimate(); });
    const info = h("div", { class: "muted mt12", "aria-live": "polite" });
    const willSearch = h("div", { class: "hint" });
    const runBtn = h("button", { class: "btn primary w100 mt16", type: "button", disabled: true, text: "Run search" });
    const out = h("div", { id: "search-out" });
    const history = h("div", {});
    const chips = h("div", { class: "chips mt12" }, SERVICES.map((p) => h("button", { type: "button", class: "chip", text: p, onclick: () => { input.value = p; estimate(); input.focus(); } })));

    modal("Find clients who are hiring",
      h("p", { class: "muted", text: "Tell us the service you sell. We search LinkedIn, X, Facebook, Reddit, and remote boards (Dynamite Jobs, Himalayas, We Work Remotely, Remote OK, and more) right now for people hiring for it." }),
      !c.ready ? h("div", { class: "sresult fail mt12", text: c.reason }) : null,
      h("label", { class: "field" }, "What service do you sell?", input), willSearch, chips,
      h("div", { class: "mt16" }, h("div", { class: "hint", text: "How far back should we look?" }), win),
      info, runBtn, out, history);
    input.focus();
    input.addEventListener("input", debounce(estimate, 350));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !runBtn.disabled) runBtn.click(); });

    let quote = null, estSeq = 0;
    async function estimate() {
      const seq = ++estSeq;                                  // only the newest estimate may update the screen
      quote = null; runBtn.disabled = true;
      const q = asSearch(input.value);
      willSearch.textContent = q ? `LinkedIn, X, Facebook, Reddit, remote job boards, and the web, for “${q}”` : "";
      if (input.value.trim().length < 3) {                     // before anything is typed: say the same thing the estimate will say later
        runBtn.textContent = "Run search";
        if (c.unlimited) info.textContent = `Type the service you sell. Your searches are free on this account (${c.left_today} new searches left today).`;
        else if (c.price_usd && c.balance_usd < c.price_usd) clear(info).append(`Type the service you sell. A search costs about ${money(c.price_usd)} and you have ${money(c.balance_usd)}. `, h("button", { class: "btn sm", type: "button", text: "Add credits", onclick: () => openCredits() }));
        else info.textContent = `Type the service you sell. You have ${money(c.balance_usd)} in credits and ${c.left_today} new searches left today.`;
        return;
      }
      const r = await api("POST", "/searches/estimate", { query: q, freshness: s.freshness, site: null });
      if (seq !== estSeq) return;
      if (!r.ok) { info.textContent = detail(r); return; }
      quote = r.data;
      if (!quote.ready) { info.textContent = quote.reason; return; }
      if (quote.cached) { info.textContent = `Free: we searched this recently and have ${quote.cached_results} results ready.`; runBtn.textContent = "Show results (free)"; runBtn.disabled = false; return; }
      runBtn.textContent = `Run search · about ${money(quote.price_usd)}`;
      if (quote.unlimited) { info.textContent = `Your searches are free on this account (no credits used). ${quote.searches_left_today} new searches left in the last-24-hours limit.`; runBtn.textContent = "Run search"; runBtn.disabled = false; return; }
      if (!quote.affordable) {
        clear(info).append(`You have ${money(quote.balance_usd)}. This search needs about ${money(quote.price_usd)}. `, h("button", { class: "btn sm", type: "button", text: "Add credits", onclick: () => openCredits() }));
        return;
      }
      info.textContent = `You have ${money(quote.balance_usd)} in credits. ${quote.searches_left_today} new searches left today.`;
      runBtn.disabled = false;
    }
    runBtn.addEventListener("click", async () => {
      if (input.value.trim().length < 3) return;
      runBtn.disabled = true; const q = asSearch(input.value);
      const r = await api("POST", "/searches", { query: q, freshness: s.freshness, site: null });
      if (!r.ok) { clear(out).append(h("div", { class: "sresult fail", text: detail(r) })); runBtn.disabled = false; return; }
      follow(r.data.id, out, runBtn);
    });
    estimate();
    loadHistory(history, out);
  }

  async function follow(id, out, btn) {
    clear(out).append(h("div", { class: "sresult mt16" }, h("span", { class: "spinner" }), " Searching the web and reading posts. The searches run one after another, so this can take several minutes."));
    for (let i = 0; i < 400; i++) {                        // about 10 minutes: the server gives up on a search after that (STALE_AFTER)
      const r = await api("GET", "/searches/" + id);
      if (!r.ok) { clear(out).append(h("div", { class: "sresult fail", text: detail(r) })); if (btn) btn.disabled = false; return; }
      if (r.data.status === "done" || r.data.status === "failed") { await showResult(r.data, out); if (btn) btn.disabled = false; refreshBalance(); return; }
      await new Promise((res) => setTimeout(res, 1500));
    }
    clear(out).append(h("div", { class: "sresult fail", text: "This is taking longer than expected. Check the search history in a minute; you are only charged if it completes." }));
    if (btn) btn.disabled = false;
  }

  async function showResult(s, out) {
    clear(out);
    if (s.status === "failed") { out.append(h("div", { class: "sresult fail mt16", text: s.error || "The search failed. You were not charged." })); return; }
    const jobs = await api("GET", `/searches/${s.id}/jobs?limit=300`);
    out.append(h("div", { class: "sresult mt16" }, h("strong", { text: s.cached ? "Served from our database (free)" : `Found ${s.results} ${s.results === 1 ? "job" : "jobs"}${s.new_jobs ? `, ${s.new_jobs} new to the database` : ""}` }),
      h("div", { class: "muted", text: s.cached ? "Someone searched this in the last few hours." : `Charged ${money(s.cost_usd, 4)}.` }),
      s.error ? h("div", { class: "muted", text: s.error }) : null));
    const list = h("div", { class: "mt16" });
    if (jobs.ok && jobs.data.length) for (const j of jobs.data) list.appendChild(jobCard(j));
    else list.appendChild(h("p", { class: "muted", text: "No jobs came back for this search. Try different words or a longer time window." }));
    out.append(list);
    loadJobs(true);                                             // new jobs are now in the main list too
    buildFacetsRefresh();
  }
  async function buildFacetsRefresh() { const f = await api("GET", "/jobs/facets"); if (f.ok) { facets = f.data; buildFilters(); } }

  async function loadHistory(box, out) {
    const r = await api("GET", "/searches?limit=6");
    if (!r.ok || !r.data.length) return;
    CF.put(clear(box), h("h3", { class: "mt20", text: "Your recent searches" }),
      r.data.map((s) => h("div", { class: "row mt12" }, h("div", { class: "grow" }, h("div", { text: s.query + (s.site ? `  ·  ${s.site}` : "") }), h("div", { class: "hint", text: `${ago(s.created_at)} · ${s.status === "done" ? s.results + " results" : s.status}${s.cost_usd ? " · " + money(s.cost_usd, 4) : ""}` })),
        s.status === "done" ? h("button", { class: "btn sm", type: "button", text: "View", onclick: () => showResult(s, out) }) : null)));
  }

  boot();
})();
