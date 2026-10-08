// Shared helpers. Plain JS, no dependencies, and NO innerHTML anywhere: every value from the server is inserted as text.
(() => {
  "use strict";
  const CF = (window.CF = {});

  CF.h = function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.appendChild(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
    }
    return el;
  };
  const h = CF.h;
  CF.clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

  // tiny icon set (stroke icons, built with createElementNS so no HTML parsing)
  const ICONS = {
    search: "M11 19a8 8 0 1 1 5.3-14M21 21l-4.3-4.3",
    lock: "M6 11V8a6 6 0 1 1 12 0v3M5 11h14v10H5z",
    out: "M7 17L17 7M8 7h9v9",
    x: "M6 6l12 12M18 6L6 18",
    filter: "M3 5h18M6 12h12M10 19h4",
    copy: "M9 9h11v11H9zM5 15V5h10",
    check: "M5 12l5 5L20 7",
    bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
  };
  CF.icon = (name, size = 16) => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", size); svg.setAttribute("height", size);
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(ns, "path"); p.setAttribute("d", ICONS[name]); svg.appendChild(p);
    return svg;
  };

  CF.api = async function api(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "clientfinder" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let res, data = null;
    try { res = await fetch(path, opts); } catch (e) { return { ok: false, status: 0, data: { detail: "Network error. Check your connection." }, headers: new Headers() }; }
    try { data = await res.json(); } catch (e) { /* empty body */ }
    return { ok: res.ok, status: res.status, data, headers: res.headers };
  };
  CF.detail = (r) => {
    const d = r.data && r.data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => x.msg).join(" ");
    return r.status === 429 ? "Too many requests. Try again in a moment." : `Something went wrong (${r.status || "offline"}).`;
  };

  CF.ago = (iso) => {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return "just now";
    const m = s / 60; if (m < 60) return `${Math.round(m)}m ago`;
    const hr = m / 60; if (hr < 36) return `${Math.round(hr)}h ago`;
    const d = hr / 24; if (d < 60) return `${Math.round(d)}d ago`;
    return `${Math.round(d / 30)}mo ago`;
  };
  CF.money = (n, max = 2) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: max });
  CF.num = (n) => Number(n || 0).toLocaleString();

  CF.PLATFORM = { remoteok: "RemoteOK", weworkremotely: "We Work Remotely", remotive: "Remotive", greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby",
    problogger: "ProBlogger", reddit: "Reddit", linkedin: "LinkedIn", twitter: "X / Twitter", indeed: "Indeed", upwork: "Upwork", web: "Web", mediabistro: "Mediabistro" };
  CF.TYPE = { full_time: "Full-time", contract: "Contract", hourly: "Hourly", fixed: "Fixed price", social_post: "Social post", part_time: "Part-time" };
  CF.platformBadge = (p) => h("span", { class: "plat " + String(p).replace(/[^a-z]/g, ""), text: CF.PLATFORM[p] || p });

  CF.toast = (msg, kind) => {
    let box = document.getElementById("toasts");
    if (!box) { box = h("div", { id: "toasts", role: "status", "aria-live": "polite" }); document.body.appendChild(box); }
    const t = h("div", { class: "toast " + (kind || ""), text: msg });
    box.appendChild(t);
    setTimeout(() => t.remove(), kind === "bad" ? 7000 : 4000);
  };

  // Only same-site relative paths are allowed as a post-login destination (no open redirects).
  CF.safeNext = (raw, fallback) => (typeof raw === "string" && /^\/(?![\/\\])[A-Za-z0-9\-_\/?=&#.%]*$/.test(raw) ? raw : fallback);

  CF.debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  CF.copy = async (text) => { try { await navigator.clipboard.writeText(text); CF.toast("Copied", "good"); } catch (e) { CF.toast("Could not copy", "bad"); } };
})();
