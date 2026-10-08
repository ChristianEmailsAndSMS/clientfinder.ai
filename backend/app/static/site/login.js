(() => {
  "use strict";
  const { api, detail, safeNext } = CF;
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const next = safeNext(params.get("next"), "/app");
  let setupToken = null;

  function go() { location.replace(next); }

  // already signed in? skip the form
  api("GET", "/auth/me").then((r) => { if (r.ok) go(); });
  api("GET", "/auth/status").then((r) => {
    if (r.ok && !r.data.auth_configured) {
      const n = $("notice"); n.hidden = false;
      n.textContent = "Sign-in is not switched on yet: the server is missing JWT_SECRET (see docs/SECURITY.md).";
    }
  });

  function tab(which) {
    const up = which === "up";
    $("tab-in").classList.toggle("on", !up); $("tab-up").classList.toggle("on", up);
    $("form-in").hidden = up; $("form-up").hidden = !up;
    (up ? $("form-up") : $("form-in")).elements.email.focus();
    history.replaceState(null, "", up ? "#signup" : location.pathname + location.search);
  }
  $("tab-in").addEventListener("click", () => tab("in"));
  $("tab-up").addEventListener("click", () => tab("up"));
  if (location.hash === "#signup") tab("up");

  for (const b of document.querySelectorAll("[data-show]")) {
    b.addEventListener("click", () => {
      const input = b.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show";
    });
  }

  function busy(btn, on, label) { btn.disabled = on; btn.textContent = on ? "One moment…" : label; }

  $("form-up").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, err = $("err-up"), btn = $("btn-up");
    err.textContent = ""; busy(btn, true);
    const r = await api("POST", "/auth/signup", { email: f.elements.email.value, password: f.elements.password.value });
    if (r.ok) { go(); return; }
    busy(btn, false, "Create my account");
    err.textContent = CF.detail(r);
    if (r.status === 409) { $("form-in").elements.email.value = f.elements.email.value; }
  });

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
