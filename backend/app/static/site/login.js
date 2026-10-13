// Sign up / sign in. Used by BOTH the home page (panel inside the hero) and /login (standalone), so clientfinder.ai is all you need.
(() => {
  "use strict";
  const { h, api, detail, safeNext, clear } = CF;
  const $ = (id) => document.getElementById(id);
  const onHome = document.body.dataset.page === "home";
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next"), "/app");
  let setupToken = null;

  // The owner signing in with no destination of their own lands on the dashboard, the same place the 2FA enrolment sends them.
  const go = (me) => location.replace(me && me.is_admin && next === "/app" ? "/admin" : next);
  // An error line is hidden (and silent for screen readers) until it has something to say.
  const say = (el, msg) => { el.textContent = msg || ""; el.hidden = !msg; if (msg) el.setAttribute("role", "alert"); else el.removeAttribute("role"); };
  CF.publicConfig = CF.publicConfig || api("GET", "/public/config");        // home.js reads the same answer

  // ---------- already signed in? ----------
  // /auth/status says whether there is a session, so a visitor never triggers a 401 from /auth/me (it shows up red in the console).
  const status = api("GET", "/auth/status");
  status.then((r) => (r.ok && r.data.signed_in ? api("GET", "/auth/me") : { ok: false })).then((r) => {
    if (!r.ok) return;
    if (!onHome) { go(r.data); return; }                       // /login: skip the form
    const ni = document.getElementById("nav-signin"), nu = document.getElementById("nav-signup");     // header: one "Open the app" entry instead of sign-in / sign-up
    if (ni && nu) { nu.replaceWith(h("a", { class: "btn sm primary", href: "/app", text: "Open the app" })); ni.remove(); }   // new node: the old one carries the #signup click handler
    signedInHome = true;                                  // the tabs are gone below: sign-up / sign-in links now lead to the app
    const panel = clear($("auth-panel"));                 // home: replace the form with a welcome panel
    panel.append(h("div", { class: "welcome" },
      h("h2", { class: "authtitle", text: "Welcome back" }),
      h("div", { class: "muted who", text: r.data.email }),
      h("a", { class: "btn primary lg", href: "/app", text: "Open the app" }),
      r.data.is_admin ? h("a", { class: "btn", href: "/admin", text: "Admin dashboard" }) : null,
      h("button", { class: "btn", type: "button", text: "Sign out", onclick: async () => { if (await CF.signOut()) location.reload(); else CF.toast("Could not sign you out. Check your connection and try again.", "bad"); } })));
  });

  // The back button can restore this page from the browser's back/forward cache with the old "Welcome back" panel (or form) frozen in it,
  // e.g. after signing out in /app. A restored page re-checks the session by loading afresh.
  window.addEventListener("pageshow", (e) => { if (e.persisted) location.reload(); });

  status.then((r) => {
    if (r.ok && !r.data.auth_configured && $("notice")) {
      const n = $("notice"); n.hidden = false;
      n.textContent = "Sign-in is not switched on yet: the server is missing JWT_SECRET (see docs/SECURITY.md).";
    }
  });

  // ---------- tabs ----------
  let signedInHome = false;
  function tab(which, focus) {
    if (signedInHome || !$("tab-in")) { if (signedInHome) location.assign("/app"); return; }   // signed in: no form to switch, go to the app
    const up = which === "up";
    $("tab-in").classList.toggle("on", !up); $("tab-up").classList.toggle("on", up);
    $("tab-in").setAttribute("aria-selected", String(!up)); $("tab-up").setAttribute("aria-selected", String(up));
    $("form-in").hidden = up; $("form-up").hidden = !up;
    say($("err-in"), ""); say($("err-up"), ""); $("forgot-note").hidden = true;
    if ($("auth-title")) $("auth-title").textContent = up ? "Create your free account" : "Welcome back";
    if ($("auth-sub")) $("auth-sub").textContent = up ? signupLine : "Sign in to pick up where you left off.";
    if (focus) (up ? $("form-up") : $("form-in")).elements.email.focus({ preventScroll: false });
  }
  let signupLine = "No card needed. Browsing the database is free.";
  CF.publicConfig.then((r) => {                                               // advertise the welcome credit only while it is really on
    if (!r.ok || !(r.data.signup_bonus_usd > 0)) return;
    const b = r.data.signup_bonus_usd;
    signupLine = `No card needed. ${b >= r.data.search_price_usd ? "Your first live search is free" : `You get $${b.toFixed(2)} in free credit`}, and browsing is always free.`;
    if (!$("form-up").hidden && $("auth-sub")) $("auth-sub").textContent = signupLine;
  });
  $("tab-in").addEventListener("click", () => { tab("in", true); setHash("#signin"); });
  $("tab-up").addEventListener("click", () => { tab("up", true); setHash("#signup"); });
  function setHash(hash) { history.replaceState(null, "", location.pathname + location.search + hash); }

  // Buttons and links elsewhere on the page ("Create account", "Sign in", "Create free account") point at #signup / #signin.
  function fromHash(focus) {
    if (location.hash === "#signup") { tab("up", focus); if (onHome && focus) $("auth-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
    else if (location.hash === "#signin") { tab("in", focus); if (onHome && focus) $("auth-panel").scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
  window.addEventListener("hashchange", () => fromHash(true));
  // A link to the hash the page is already on fires no hashchange (the tabs set the hash themselves), so handle clicks directly.
  document.querySelectorAll('a[href="#signin"], a[href="#signup"]').forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const hash = a.getAttribute("href");
    setHash(hash);
    fromHash(true);
  }));
  if (onHome) { tab(location.hash === "#signin" ? "in" : "up", false); }      // visitors land on "Create account"
  else { tab(location.hash === "#signup" ? "up" : "in", true); }               // /login lands on "Sign in"

  for (const b of document.querySelectorAll("[data-show]")) {
    b.addEventListener("click", () => {
      const input = b.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show"; b.setAttribute("aria-pressed", String(show));
    });
  }

  $("btn-forgot").addEventListener("click", () => { $("forgot-note").hidden = !$("forgot-note").hidden; });

  // The site's own messages instead of the browser's "Please fill out this field" bubble.
  function missing(f, needPassword = true) {
    if (!f.elements.email.value.trim()) { f.elements.email.focus(); return "Enter your email address."; }
    if (!f.elements.email.value.includes("@")) { f.elements.email.focus(); return "Enter a valid email address."; }
    if (needPassword && !f.elements.password.value) { f.elements.password.focus(); return "Enter your password."; }
    return "";
  }

  function busy(btn, on, label) { btn.disabled = on; btn.textContent = on ? "One moment…" : label; }

  // ---------- create account ----------
  $("form-up").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, err = $("err-up"), btn = $("btn-up");
    say(err, missing(f) || (f.elements.password.value.length < 12 ? "Your password needs at least 12 characters. A few random words works well." : ""));
    if (!err.hidden) return;
    busy(btn, true);
    const r = await api("POST", "/auth/signup", { email: f.elements.email.value, password: f.elements.password.value });
    if (r.ok) {
      if (r.data.welcome_note) { try { sessionStorage.setItem("cf_welcome_note", r.data.welcome_note); } catch (x) { /* storage blocked: the note is optional */ } }
      go(r.data); return;
    }
    busy(btn, false, "Create free account");
    say(err, detail(r));
    if (r.status === 409) { $("form-in").elements.email.value = f.elements.email.value; }
  });

  // ---------- sign in ----------
  $("form-in").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, err = $("err-in"), btn = $("btn-in");
    say(err, missing(f));
    if (!err.hidden) return;
    busy(btn, true);
    const r = await api("POST", "/auth/login", { email: f.elements.email.value, password: f.elements.password.value, totp_code: f.elements.totp_code.value || null });
    if (r.ok) { go(r.data); return; }
    busy(btn, false, "Sign in");
    const d = r.data && r.data.detail;
    if (r.status === 401 && d === "totp_required") {
      $("row-code").hidden = false; f.elements.totp_code.focus(); say(err, "Enter the 6-digit code from your authenticator app."); return;
    }
    if (r.status === 401 && d === "totp_setup_required") { startEnrol(r.data.setup_token); return; }
    say(err, detail(r));
  });

  // ---------- admin: first sign-in turns on 2FA ----------
  // Built here, only when an admin account needs it, so the public pages carry none of this markup.
  function enrolScreen(qr, key) {
    const err = h("p", { class: "error", hidden: true });
    const code = h("input", { class: "input", name: "totp_code", inputmode: "numeric", required: "", autocomplete: "one-time-code", maxlength: "7" });
    const form = h("form", { autocomplete: "off" }, h("label", { class: "field" }, "6-digit code", code), err, h("button", { class: "btn primary", type: "submit", text: "Turn on and continue" }));
    const box = h("div", { id: "v-2fa" },
      h("h2", { class: "authtitle", text: "Turn on two-factor sign-in" }),
      h("p", { class: "muted", text: "Admin accounts need a second step. Scan this with an authenticator app (1Password, Authy, Google Authenticator), then enter the 6-digit code it shows." }),
      h("img", { src: qr, alt: "Two-factor QR code", width: "200", height: "200" }),
      h("p", { class: "hint", text: "Cannot scan? Enter this key by hand:" }), h("div", { class: "keybox", text: key }), form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault(); say(err, "");
      const r = await api("POST", "/auth/2fa/confirm", { setup_token: setupToken, totp_code: code.value });
      if (r.ok) { location.replace(next === "/app" ? "/admin" : next); return; }
      say(err, detail(r));
      if (r.status === 401) { box.remove(); $("v-main").hidden = false; say($("err-in"), detail(r)); }
    });
    return { box, code };
  }
  async function startEnrol(token) {
    setupToken = token;
    const r = await api("POST", "/auth/2fa/start", { setup_token: token });
    if (!r.ok) { say($("err-in"), detail(r)); return; }
    const { box, code } = enrolScreen(r.data.qr, r.data.totp_secret);
    $("v-main").hidden = true; $("v-main").parentElement.appendChild(box);
    code.focus();
  }
})();
