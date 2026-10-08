(() => {
  "use strict";
  const { h, api, detail, ago, money, num, clear, debounce } = CF;
  const $ = (id) => document.getElementById(id);
  const PAGE = 30;

  const state = { q: "", posted: "", types: new Set(), platforms: new Set(), tags: new Set(), remote: "", hasPay: false, payPeriod: "", minPay: "", sort: "newest", offset: 0, total: 0, loading: false };
  let me = null, facets = null, loadToken = 0;

  const SORTS = [["newest", "Newest first"], ["oldest", "Oldest first"], ["pay_high", "Highest pay"], ["pay_low", "Lowest pay"]];
  const PERIOD = { hour: "hr", year: "yr", project: "project" };
  // These tags only repeat what the type / remote / pay badges and filters already say, so we do not show them as topics.
  const ATTR_TAGS = new Set(["remote", "contract", "full-time", "has-pay"]);

  // ---------------------------------------------------------------- boot
  async function boot() {
    const r = await api("GET", "/auth/me");
    if (!r.ok) { location.replace("/login?next=" + encodeURIComponent("/app")); return; }
    me = r.data;
    buildHeader(); readUrl(); buildSort();
    $("btn-run").addEventListener("click", () => openSearch());
    $("btn-credits").addEventListener("click", openCredits);
    $("btn-filters").addEventListener("click", () => $("filters").classList.add("open"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("q").focus(); }
      if (e.key === "Escape") closeModal();
    });
    const f = await api("GET", "/jobs/facets");
    facets = f.ok ? f.data : { total: 0, platforms: [], types: [], tags: [], pay_periods: [] };
    buildFilters();
    loadJobs(true);
  }

  function buildHeader() {
    const input = h("input", { class: "input", id: "q", type: "search", placeholder: "Search jobs…  ( / )", autocomplete: "off", "aria-label": "Search jobs", value: state.q });
    input.addEventListener("input", debounce(() => { state.q = input.value.trim(); loadJobs(true); }, 280));
    clear($("search-wrap")).append(CF.icon("search", 17), input);
    $("bal").textContent = money(me.balance_usd);

    const pop = h("div", { class: "pop", hidden: true },
      h("div", { class: "who", text: me.email }),
      me.is_admin ? h("a", { href: "/admin", text: "Admin dashboard" }) : null,
      h("button", { type: "button", text: "Credits & history", onclick: () => { pop.hidden = true; openCredits(); } }),
      h("button", { type: "button", text: "Sign out", onclick: async () => { await api("POST", "/auth/logout"); location.replace("/"); } }));
    const btn = h("button", { class: "btn icon", type: "button", "aria-haspopup": "true", "aria-label": "Account menu", text: me.email.slice(0, 1).toUpperCase(), onclick: (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; } });
    clear($("menu")).append(btn, pop);
    document.addEventListener("click", () => { pop.hidden = true; });
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

    root.appendChild(section("Posted", seg([["", "Any"], ["1", "24h"], ["7", "7 days"], ["30", "30 days"]], () => state.posted, (v) => { state.posted = v; loadJobs(true); })));
    root.appendChild(section("Remote", seg([["", "Any"], ["true", "Remote"], ["false", "On-site"]], () => state.remote, (v) => { state.remote = v; loadJobs(true); })));

    if (facets.types.length) root.appendChild(section("Job type", h("div", { class: "chips mt12" },
      facets.types.map((t) => { const b = h("button", { type: "button", class: "chip" + (state.types.has(t.value) ? " on" : ""), text: `${CF.TYPE[t.value] || t.value} ${num(t.count)}` }); b.addEventListener("click", () => toggle(state.types, t.value, b)); return b; }))));

    if (facets.platforms.length) root.appendChild(section("Platform", facets.platforms.map((p) => check(CF.PLATFORM[p.value] || p.value, p.count, state.platforms.has(p.value), () => toggle(state.platforms, p.value)))));

    const topics = facets.tags.filter((t) => !ATTR_TAGS.has(t.value));
    if (topics.length) root.appendChild(section("Topics (must have all)", h("div", { class: "chips mt12" },
      topics.map((t) => { const b = h("button", { type: "button", class: "chip" + (state.tags.has(t.value) ? " on" : ""), text: `${t.value} ${num(t.count)}` }); b.addEventListener("click", () => toggle(state.tags, t.value, b)); return b; }))));

    const minPay = h("input", { class: "input", type: "number", min: "0", step: "1", placeholder: "Minimum pay", value: state.minPay, "aria-label": "Minimum pay" });
    minPay.addEventListener("input", debounce(() => { state.minPay = minPay.value; loadJobs(true); }, 350));
    const per = h("select", { class: "input", "aria-label": "Pay period" }, [["", "Any pay period"], ["hour", "Per hour"], ["year", "Per year"], ["project", "Per project"]].map(([v, l]) => h("option", { value: v, text: l, selected: v === state.payPeriod })));
    per.addEventListener("change", () => { state.payPeriod = per.value; loadJobs(true); });
    root.appendChild(section("Pay", check("Only jobs that list pay", null, state.hasPay, (on) => { state.hasPay = on; loadJobs(true); }), per, minPay));

    root.appendChild(h("div", { class: "fsec" }, h("button", { class: "btn sm w100", type: "button", text: "Clear all filters", onclick: () => { resetFilters(); buildFilters(); loadJobs(true); } })));
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
  function resetFilters() {
    Object.assign(state, { posted: "", remote: "", hasPay: false, payPeriod: "", minPay: "", q: "" });
    state.types.clear(); state.platforms.clear(); state.tags.clear();
    const q = $("q"); if (q) q.value = "";
  }

  // ---------------------------------------------------------------- query + URL
  function params(offset) {
    const p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    for (const v of state.platforms) p.append("platform", v);
    for (const v of state.types) p.append("type", v);
    for (const v of state.tags) p.append("tag", v);
    if (state.remote) p.set("remote", state.remote);
    if (state.hasPay) p.set("has_pay", "true");
    if (state.payPeriod) p.set("pay_period", state.payPeriod);
    if (state.minPay !== "" && Number(state.minPay) >= 0) p.set("min_pay", state.minPay);
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
    state.minPay = /^\d+(\.\d+)?$/.test(p.get("min_pay") || "") ? p.get("min_pay") : "";
    state.posted = ["1", "7", "30"].includes(p.get("posted_within_days")) ? p.get("posted_within_days") : "";
    state.sort = SORTS.some(([v]) => v === p.get("sort")) ? p.get("sort") : "newest";
    for (const [key, set] of [["platform", state.platforms], ["type", state.types], ["tag", state.tags]]) for (const v of p.getAll(key)) if (/^[\w\-]{1,40}$/.test(v)) set.add(v);
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
        h("button", { class: "btn", type: "button", text: "Clear filters", onclick: () => { resetFilters(); buildFilters(); loadJobs(true); } }),
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
    for (const v of state.types) add(CF.TYPE[v] || v, () => state.types.delete(v));
    for (const v of state.tags) add(v, () => state.tags.delete(v));
    if (state.remote) add(state.remote === "true" ? "Remote" : "On-site", () => { state.remote = ""; });
    if (state.posted) add(state.posted === "1" ? "Last 24h" : `Last ${state.posted} days`, () => { state.posted = ""; });
    if (state.hasPay) add("Pay listed", () => { state.hasPay = false; });
    if (state.minPay !== "") add(`Pay ≥ ${state.minPay}${state.payPeriod ? "/" + PERIOD[state.payPeriod] : ""}`, () => { state.minPay = ""; });
  }

  function payText(j) {
    if (j.pay_text && j.pay_text !== "unspecified") return j.pay_text;
    if (j.pay_min == null && j.pay_max == null) return "";
    const k = (n) => (n >= 1000 ? Math.round(n / 100) / 10 + "k" : String(n));
    const range = j.pay_min != null && j.pay_max != null && j.pay_max !== j.pay_min ? `${k(j.pay_min)}–${k(j.pay_max)}` : k(j.pay_max ?? j.pay_min);
    return `$${range}${j.pay_period ? "/" + PERIOD[j.pay_period] : ""}`;
  }
  function empty(title, text, extra) { return h("div", { class: "card empty" }, h("h3", { text: title }), h("p", { text }), extra); }

  function jobCard(j) {
    const desc = h("div", { class: "desc", hidden: true, text: (j.description || j.raw_snippet || "No description captured.").slice(0, 900) });
    const safe = /^https?:\/\//i.test(j.source_url || "");
    const pay = payText(j);
    const titleBtn = h("button", { class: "title", type: "button", text: j.title, "aria-expanded": "false", onclick: () => { desc.hidden = !desc.hidden; titleBtn.setAttribute("aria-expanded", String(!desc.hidden)); } });
    return h("article", { class: "card job" },
      h("div", { class: "top" }, titleBtn, h("span", { class: "when", text: ago(j.posted_at || j.first_seen_at) })),
      h("div", { class: "meta" }, CF.platformBadge(j.platform),
        j.company_or_poster ? h("span", { text: j.company_or_poster }) : null,
        j.location ? [h("span", { class: "sep", text: "·" }), h("span", { text: j.location })] : null),
      h("div", { class: "meta" },
        pay ? h("span", { class: "chip pay", text: pay }) : null,
        j.type && CF.TYPE[j.type] ? h("span", { class: "chip type", text: CF.TYPE[j.type] }) : null,
        j.remote ? h("span", { class: "chip remote", text: "Remote" }) : null,
        (j.tags || []).filter((t) => !ATTR_TAGS.has(t)).slice(0, 4).map((t) => h("span", { class: "chip tag", text: t }))),
      desc,
      h("div", { class: "actions" },
        safe ? h("a", { class: "btn sm primary", href: j.source_url, target: "_blank", rel: "noopener noreferrer nofollow" }, "Open post ", CF.icon("out", 14)) : null,
        h("button", { class: "btn sm", type: "button", onclick: () => { desc.hidden = !desc.hidden; titleBtn.setAttribute("aria-expanded", String(!desc.hidden)); } }, desc.hidden ? "Details" : "Hide details"),
        safe ? h("button", { class: "btn sm icon", type: "button", "aria-label": "Copy link", onclick: () => CF.copy(j.source_url) }, CF.icon("copy", 14)) : null));
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
  async function refreshBalance() { const r = await api("GET", "/auth/me"); if (r.ok) { me = r.data; $("bal").textContent = money(me.balance_usd); } }

  // ---- credits
  async function openCredits() {
    const body = h("div", {}, h("span", { class: "spinner" }));
    modal("Credits", body);
    const r = await api("GET", "/account/credits?limit=30");
    if (!r.ok) { clear(body).append(h("p", { class: "error", text: detail(r) })); return; }
    clear(body).append(
      h("div", { class: "bal grad", text: money(r.data.balance_usd) }),
      h("p", { class: "muted", text: "Credits pay for searches you run yourself. Browsing and filtering the database is always free." }),
      h("div", { class: "sresult mt16" }, h("strong", { text: "Adding credits" }), h("p", { class: "muted", text: "Card payments are launching soon. Until then, email christian@emailsandsms.com and we will top up your account." })),
      h("h3", { class: "mt20", text: "History" }),
      r.data.entries.length ? h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["When", "What", "Amount", "Balance"].map((x) => h("th", { text: x })))),
        h("tbody", {}, r.data.entries.map((e) => h("tr", {}, h("td", { text: ago(e.at) }), h("td", { text: e.reason || e.kind }),
          h("td", { class: e.amount_usd < 0 ? "bad" : "good", text: money(e.amount_usd, 4) }), h("td", { text: money(e.balance_after_usd, 4) }))))) : h("p", { class: "muted", text: "No activity yet." }));
  }

  // ---- run a search
  const PRESETS = ["hiring email copywriter", "need a funnel builder", "looking for a copywriter", "hiring creative strategist", "klaviyo specialist wanted", "landing page designer needed"];
  const SITE_LABEL = { "twitter.com": "Twitter", "x.com": "X", "reddit.com": "Reddit", "linkedin.com/posts": "LinkedIn posts", "indeed.com": "Indeed", "upwork.com": "Upwork" };

  async function openSearch(prefill) {
    const cfg = await api("GET", "/searches/config");
    if (!cfg.ok) { CF.toast(detail(cfg), "bad"); return; }
    const c = cfg.data;
    const s = { freshness: "w", site: "" };
    const input = h("input", { class: "input", type: "text", maxlength: "120", placeholder: "e.g. hiring email copywriter klaviyo", value: prefill || "", "aria-label": "What to search for" });
    const site = h("select", { class: "input", "aria-label": "Where to look" }, h("option", { value: "", text: "Anywhere on the web" }), c.sites.map((x) => h("option", { value: x, text: SITE_LABEL[x] || x })));
    site.addEventListener("change", () => { s.site = site.value; estimate(); });
    const windowSeg = seg([["d", "Past day"], ["w", "Past week"], ["m", "Past month"]], () => s.freshness, (v) => { s.freshness = v; estimate(); });
    const info = h("div", { class: "muted mt12", "aria-live": "polite" });
    const runBtn = h("button", { class: "btn primary w100 mt16", type: "button", disabled: true, text: "Run search" });
    const out = h("div", { id: "search-out" });
    const history = h("div", {});
    const chips = h("div", { class: "chips mt12" }, PRESETS.map((p) => h("button", { type: "button", class: "chip", text: p, onclick: () => { input.value = p; estimate(); input.focus(); } })));

    modal("Run your own search",
      h("p", { class: "muted", text: "Tell us what you want. We search the web, read each post and add the jobs to the database." }),
      !c.ready ? h("div", { class: "sresult fail mt12", text: c.reason }) : null,
      h("label", { class: "field" }, "What are you looking for?", input), chips,
      h("div", { class: "row end mt16" }, h("div", {}, h("div", { class: "hint", text: "Time window" }), windowSeg), h("div", { class: "grow" }, h("div", { class: "hint", text: "Where" }), site)),
      info, runBtn, out, history);
    input.focus();
    input.addEventListener("input", debounce(estimate, 350));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !runBtn.disabled) runBtn.click(); });

    let quote = null;
    async function estimate() {
      quote = null; runBtn.disabled = true;
      const q = input.value.trim();
      if (q.length < 3) { info.textContent = `Type at least 3 characters. You have ${c.left_today} new searches left today.`; runBtn.textContent = "Run search"; return; }
      const r = await api("POST", "/searches/estimate", { query: q, freshness: s.freshness, site: s.site || null });
      if (input.value.trim() !== q) return;
      if (!r.ok) { info.textContent = detail(r); return; }
      quote = r.data;
      if (!quote.ready) { info.textContent = quote.reason; return; }
      if (quote.cached) { info.textContent = `Free: we searched this recently and have ${quote.cached_results} results ready.`; runBtn.textContent = "Show results (free)"; runBtn.disabled = false; return; }
      runBtn.textContent = `Run search · about ${money(quote.price_usd)}`;
      if (!quote.affordable) { info.textContent = `You have ${money(quote.balance_usd)}. This search needs about ${money(quote.price_usd)}. Add credits to run it.`; return; }
      info.textContent = `You have ${money(quote.balance_usd)} in credits. ${quote.searches_left_today} new searches left today.`;
      runBtn.disabled = false;
    }
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true; const q = input.value.trim();
      const r = await api("POST", "/searches", { query: q, freshness: s.freshness, site: s.site || null });
      if (!r.ok) { clear(out).append(h("div", { class: "sresult fail", text: detail(r) })); runBtn.disabled = false; return; }
      follow(r.data.id, out, runBtn);
    });
    estimate();
    loadHistory(history, out);
  }

  async function follow(id, out, btn) {
    clear(out).append(h("div", { class: "sresult mt16" }, h("span", { class: "spinner" }), " Searching the web and reading posts. This takes about a minute…"));
    for (let i = 0; i < 120; i++) {
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
    const jobs = await api("GET", `/searches/${s.id}/jobs?limit=50`);
    out.append(h("div", { class: "sresult mt16" }, h("strong", { text: s.cached ? "Served from our database (free)" : `Found ${s.results} ${s.results === 1 ? "job" : "jobs"}${s.new_jobs ? `, ${s.new_jobs} new to the database` : ""}` }),
      h("div", { class: "muted", text: s.cached ? "Someone searched this in the last few hours." : `Charged ${money(s.cost_usd, 4)}.` })));
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
    clear(box).append(h("h3", { class: "mt20", text: "Your recent searches" }),
      r.data.map((s) => h("div", { class: "row mt12" }, h("div", { class: "grow" }, h("div", { text: s.query + (s.site ? `  ·  ${s.site}` : "") }), h("div", { class: "hint", text: `${ago(s.created_at)} · ${s.status === "done" ? s.results + " results" : s.status}${s.cost_usd ? " · " + money(s.cost_usd, 4) : ""}` })),
        s.status === "done" ? h("button", { class: "btn sm", type: "button", text: "View", onclick: () => showResult(s, out) }) : null)));
  }

  boot();
})();
