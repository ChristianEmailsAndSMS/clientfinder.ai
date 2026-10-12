(() => {
  "use strict";
  const { h, api, ago, num, clear } = CF;
  const SOURCES = ["remoteok", "weworkremotely", "remotive", "greenhouse", "lever", "ashby", "problogger"];

  document.getElementById("lock-ico").appendChild(CF.icon("lock", 16));
  const chips = document.getElementById("source-chips");
  for (const s of SOURCES) chips.appendChild(h("span", { class: "chip", text: CF.PLATFORM[s] }));

  api("GET", "/public/stats").then((r) => {
    if (!r.ok) return;
    const d = r.data;
    document.getElementById("live-dot").classList.toggle("live", d.live);
    document.getElementById("live-text").textContent = d.live
      ? `Live: updated ${ago(d.last_update)}`
      : "Updating the database";
    const box = clear(document.getElementById("stats"));
    for (const [n, l] of [[num(d.jobs_total), "jobs in the database"], [num(d.jobs_new_24h), "added in the last 24 hours"], [num(d.sources), "platforms covered"]])
      box.appendChild(h("div", { class: "card s" }, h("div", { class: "n grad", text: n }), h("div", { class: "l", text: l })));
  });

  api("GET", "/public/preview").then((r) => {
    const box = clear(document.getElementById("preview"));
    if (!r.ok || !r.data.length) { box.appendChild(h("div", { class: "prow" }, h("div", { class: "muted", text: "The first jobs will appear here shortly." }))); return; }
    const openSignup = () => { document.getElementById("tab-up").click(); document.getElementById("auth-panel").scrollIntoView({ behavior: "smooth", block: "center" }); };
    for (const j of r.data) {
      box.appendChild(h("div", { class: "prow", role: "link", tabindex: "0", title: "Create a free account to open this post", onclick: openSignup,
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSignup(); } } },
        h("div", {}, h("div", { class: "t", text: j.title }),
          h("div", { class: "m" }, CF.platformBadge(j.platform),
            j.type && CF.TYPE[j.type] ? h("span", { class: "chip type", text: CF.TYPE[j.type] }) : null,
            j.remote ? h("span", { class: "chip remote", text: "Remote" }) : null,
            CF.cleanPay(j.pay) ? h("span", { class: "chip pay", text: CF.cleanPay(j.pay) }) : null)),
        h("div", { class: "when", text: ago(j.seen_at) })));
    }
  });

  (CF.publicConfig || api("GET", "/public/config")).then((r) => {
    if (!r.ok) return;
    if (r.data.signup_bonus_usd > 0) {                                      // only while the welcome credit is really on
      const li = document.querySelector(".ticks li:nth-child(2)");
      if (li) li.textContent = r.data.signup_bonus_usd >= r.data.search_price_usd ? "Run your own search for the exact role you sell: your first one is free" : "Run your own search for the exact role you sell: free credit to start";
    }
    const line = document.getElementById("price-line");
    line.textContent = `About ${CF.money(r.data.search_price_usd)} per search, billed from credits`;
    if (!r.data.live_search) {
      const b = document.getElementById("mode-badge");
      b.textContent = "Switching on soon"; b.className = "badge soon";
    }
  });
})();
