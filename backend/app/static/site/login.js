// Sign up / sign in. Used by BOTH the home page (panel inside the hero) and /login (standalone), so clientfinder.ai is all you need.
(() => {
  "use strict";
  const { h, api, detail, safeNext, clear } = CF;
  const $ = (id) => document.getElementById(id);
  const onHome = document.body.dataset.page === "home";
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next"), "/app");
  let setupToken = null;

  const go = () => location.replace(next);

  // ---------- already signed in? ----------
  api("GET", "/auth/me").then((r) => {
    if (!r.ok) return;
    if (!onHome) { go(); return; }                       // /login: skip the form
    const panel = clear($("auth-panel"));                 // home: replace the form with a welcome panel
    panel.append(h("div", { class: "welcome" },
      h("h2", { class: "authtitle", text: "Welcome back" }),
      h("div", { class: "muted who", text: r.data.email }),
      h("a", { class: "btn primary lg", href: "/app", text: "Open the app" }),
      r.data.is_admin ? h("a", { class: "btn", href: "/admin", text: "Admin dashboard" }) : null,
      h("button", { class: "btn", type: "button", text: "Sign out", onclick: async () => { await api("POST", "/auth/logout"); location.reload(); } })));
  });

  api("GET", "/auth/status").then((r) => {
    if (r.ok && !r.data.auth_configured && $("notice")) {
      const n = $("notice"); n.hidden = false;
      n.textContent = "Sign-in is not switched on yet: the server is missing JWT_SECRET (see docs/SECURITY.md).";
    }
  });

  // ---------- tabs ----------
  function tab(which, focus) {
    const up = which === "up";
    $("tab-in").classList.toggle("on", !up); $("tab-up").classList.toggle("on", up);
    $("form-in").hidden = up; $("form-up").hidden = !up;
    if ($("auth-title")) $("auth-title").textContent = up ? "Create your free account" : "Welcome back";
    if (focus) (up ? $("form-up") : $("form-in")).elements.email.focus({ preventScroll: false });
  }
  $("tab-in").addEventListener("click", () => { tab("in", true); setHash("#signin"); });
  $("tab-up").addEventListener("click", () => { tab("up", true); setHash("#signup"); });
  function setHash(hash) { history.replaceState(null, "", location.pathname + location.search + hash); }

  // Buttons and links elsewhere on the page ("Create account", "Sign in", "Create free account") point at #signup / #signin.
  function fromHash(focus) {
    if (location.hash === "#signup") { tab("up", focus); if (onHome && focus) $("auth-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
    else if (location.hash === "#signin") { tab("in", focus); if (onHome && focus) $("auth-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
  window.addEventListener("hashchange", () => fromHash(true));
  if (onHome) { tab(location.hash === "#signin" ? "in" : "up", false); }      // visitors land on "Create account"
  else { tab(location.hash === "#signup" ? "up" : "in", true); }               // /login lands on "Sign in"

  for (const b of document.querySelectorAll("[data-show]")) {
    b.addEventListener("click", () => {
      const input = b.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show";
    });
  }

  function busy(btn, on, label) { btn.disabled = on; btn.textContent = on ? "One moment…" : label; }

  // ---------- create account ----------
  $("form-up").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, err = $("err-up"), btn = $("btn-up");
    err.textContent = ""; busy(btn, true);
    const r = await api("POST", "/auth/signup", { email: f.elements.email.value, password: f.elements.password.value });
    if (r.ok) { go(); return; }
    busy(btn, false, "Create my account");
    err.textContent = detail(r);
    if (r.status === 409) { $("form-in").elements.email.value = f.elements.email.value; }
  });

  // ---------- sign in ----------
  $("form-in").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, err = $("err-in"), btn = $("btn-in");
    err.textContent = ""; busy(btn, true);
    const r = await api("POST", "/auth/login", { email: f.elements.email.value, password: f.elements.password.value, totp_code: f.elements.totp_code.value || null });
    if (r.ok) { go(); return; }
    busy(btn, false, "Sign in");
    const d = r.data && r.data.detail;
    if (r.status === 401 && d === "totp_required") {
      $("row-code").hidden = false; f.elements.totp_code.focus(); err.textContent = "Enter the 6-digit code from your authenticator app."; return;
    }
    if (r.status === 401 && d === "totp_setup_required") { startEnrol(r.data.setup_token); return; }
    err.textContent = detail(r);
  });

  // ---------- admin: first sign-in turns on 2FA ----------
  async function startEnrol(token) {
    setupToken = token;
    const r = await api("POST", "/auth/2fa/start", { setup_token: token });
    if (!r.ok) { $("err-in").textContent = detail(r); return; }
    $("qr").src = r.data.qr; $("totp-key").textContent = r.data.totp_secret;
    $("v-main").hidden = true; $("v-2fa").hidden = false;
    $("form-2fa").elements.totp_code.focus();
  }
  $("form-2fa").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("err-2fa"); err.textContent = "";
    const r = await api("POST", "/auth/2fa/confirm", { setup_token: setupToken, totp_code: e.target.elements.totp_code.value });
    if (r.ok) { location.replace(next === "/app" ? "/admin" : next); return; }
    err.textContent = detail(r);
    if (r.status === 401) { $("v-2fa").hidden = true; $("v-main").hidden = false; $("err-in").textContent = detail(r); }
  });
})();
