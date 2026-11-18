import { html, render, useState, useEffect, api, fail, Toasts, Icon, Loading, Field } from "./core.js";
import { Dashboard } from "./dashboard.js";
import { Library, VideoDetail } from "./library.js";
import { Studio } from "./studio.js";
import { Clips } from "./clips.js";
import { Campaigns } from "./campaigns.js";
import { Earnings } from "./earnings.js";
import { Settings } from "./settings.js";

const NAV = [
  ["/", "Command center", "home"], ["/library", "Recordings", "film"], ["/clips", "Review queue", "scissors"],
  ["/campaigns", "Campaigns", "target"], ["/earnings", "Earnings", "coins"], ["/settings", "Settings", "cog"],
];
const SHORT = { "/": "Home", "/library": "Recordings", "/clips": "Review", "/campaigns": "Campaigns", "/earnings": "Earnings", "/settings": "Settings" };

function useRoute() {
  const get = () => (location.hash.replace(/^#/, "") || "/");
  const [r, set] = useState(get());
  useEffect(() => { const h = () => { set(get()); window.scrollTo(0, 0); }; window.addEventListener("hashchange", h); return () => window.removeEventListener("hashchange", h); }, []);
  return r;
}

function Auth({ onAuth }) {
  const [mode, setMode] = useState("login");
  const [f, setF] = useState({ email: "", password: "", name: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async e => {
    e.preventDefault(); setBusy(true); setErr("");
    try { onAuth(await api("/auth/" + mode, { method: "POST", body: f })); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const up = k => e => setF({ ...f, [k]: e.target.value });
  return html`<div class="auth"><form class="card col" style="gap:14px" onSubmit=${submit}>
    <div class="brand" style="padding:0"><i>F</i>ClipForge AI</div>
    <div><h1>${mode === "login" ? "Sign in" : "Create your account"}</h1>
      <p class="muted" style="margin-top:4px">Turn long streams into approved, paid short-form clips.</p></div>
    ${mode === "signup" && html`<${Field} label="Name"><input value=${f.name} onInput=${up("name")} autocomplete="name" /><//>`}
    <${Field} label="Email"><input type="email" required value=${f.email} onInput=${up("email")} autocomplete="email" /><//>
    <${Field} label="Password" hint=${mode === "signup" ? "At least 8 characters" : ""}>
      <input type="password" required minlength=${mode === "signup" ? 8 : 1} value=${f.password} onInput=${up("password")} autocomplete=${mode === "login" ? "current-password" : "new-password"} /><//>
    ${err && html`<div class="note warn" role="alert">${err}</div>`}
    <button class="btn primary block" disabled=${busy}>${busy ? html`<span class="spin"></span>` : ""} ${mode === "login" ? "Sign in" : "Create account"}</button>
    <button type="button" class="btn ghost block" onClick=${() => { setMode(mode === "login" ? "signup" : "login"); setErr(""); }}>
      ${mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}</button>
  </form></div>`;
}

function App() {
  const [user, setUser] = useState(undefined);
  const route = useRoute();
  const [sys, setSys] = useState(null);
  const [active, setActive] = useState(0);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    const off = () => setOffline(true), on = () => setOffline(false);
    window.addEventListener("cf-offline", off); window.addEventListener("cf-online", on);
    return () => { window.removeEventListener("cf-offline", off); window.removeEventListener("cf-online", on); };
  }, []);
  useEffect(() => {
    api("/auth/me").then(setUser).catch(() => setUser(null));
    const out = () => setUser(null);
    window.addEventListener("cf-logout", out);
    return () => window.removeEventListener("cf-logout", out);
  }, []);
  useEffect(() => {
    if (!user) return;
    api("/system").then(setSys).catch(fail);
    const tick = () => { if (!document.hidden) api("/jobs").then(j => setActive(j.filter(x => x.status === "queued" || x.status === "running").length)).catch(() => {}); };
    tick(); const t = setInterval(tick, 5000); return () => clearInterval(t);
  }, [user && user.id]);

  if (user === undefined) return html`<${Loading} what="Starting" />`;
  if (!user) return html`<${Auth} onAuth=${setUser} /><${Toasts} />`;

  const seg = route.split("/").filter(Boolean);
  const base = "/" + (seg[0] || "");
  const top = base === "/video" ? "/library" : base === "/studio" ? "/clips" : base;
  const ctx = { user, setUser, sys };
  let view;
  if (base === "/") view = html`<${Dashboard} ...${ctx} />`;
  else if (base === "/library") view = html`<${Library} ...${ctx} />`;
  else if (base === "/video") view = html`<${VideoDetail} key=${seg[1]} id=${+seg[1]} ...${ctx} />`;
  else if (base === "/studio") view = html`<${Studio} key=${seg[1]} id=${+seg[1]} ...${ctx} />`;
  else if (base === "/clips") view = html`<${Clips} ...${ctx} />`;
  else if (base === "/campaigns") view = html`<${Campaigns} ...${ctx} />`;
  else if (base === "/earnings") view = html`<${Earnings} ...${ctx} />`;
  else if (base === "/settings") view = html`<${Settings} ...${ctx} />`;
  else view = html`<div class="empty"><b>Page not found</b><a class="btn" href="#/">Back to the command center</a></div>`;

  const logout = async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); setUser(null); };
  return html`<div class="shell">
    <nav class="rail" aria-label="Main">
      <div class="brand"><i>F</i>ClipForge AI</div>
      ${NAV.map(([p, l, ic]) => html`<a class=${"nav" + (top === p ? " on" : "")} href=${"#" + p}><${Icon} n=${ic} />${l}
        ${p === "/" && active > 0 && html`<span class="pill acc"><span class="spin" style="width:9px;height:9px;border-width:1.5px"></span>${active}</span>`}</a>`)}
      <div class="foot"><div class="trunc" style="font-weight:500">${user.name || user.email}</div>
        <button class="nav" style="padding:6px 0;margin-top:2px" onClick=${logout}><${Icon} n="out" />Sign out</button></div>
    </nav>
    <main>${offline && html`<div class="note warn" role="alert" style="margin-bottom:14px"><b>ClipForge's server is asleep.</b> If you run it on GitHub Codespaces, open the Codespace tab, restart it, wait a minute, then
      <button class="btn sm" style="margin-left:6px" onClick=${() => location.reload()}>Reload</button><div class="small" style="margin-top:4px">Your account, campaigns and clips are safe. A campaign you were typing is kept as a draft.</div></div>`}${view}</main>
    <nav class="tabbar" aria-label="Main">
      ${NAV.map(([p, l, ic]) => html`<a class=${top === p ? "on" : ""} href=${"#" + p}><${Icon} n=${ic} />${SHORT[p]}</a>`)}
    </nav>
    <${Toasts} />
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
