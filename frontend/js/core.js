import { html, useState, useEffect, useRef } from "/static/vendor/preact-htm.js";
export * from "/static/vendor/preact-htm.js";

// ----------------------------------------------------------------------------- api
export class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}
export async function api(path, { method = "GET", body, raw } = {}) {
  const opt = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined && !raw) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  if (raw) opt.body = body;
  let r;
  try { r = await fetch("/api" + path, opt); }
  catch (e) { window.dispatchEvent(new Event("cf-offline")); throw new ApiError("Can't reach the ClipForge server. It may be asleep.", 0); }
  if (r.status === 401 && !path.startsWith("/auth/")) { window.dispatchEvent(new Event("cf-logout")); }
  const ct = r.headers.get("content-type") || "";
  // A sleeping Codespace answers with a GitHub page or a gateway error instead of our JSON
  if (!ct.includes("json") && (!r.ok || ct.includes("html"))) { window.dispatchEvent(new Event("cf-offline")); throw new ApiError("The ClipForge server is asleep or restarting.", r.status); }
  window.dispatchEvent(new Event("cf-online"));
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : (d && d.message) || (Array.isArray(d) ? d.map(x => x.msg).join("; ") : `Request failed (${r.status})`);
    throw new ApiError(msg, r.status, d);
  }
  return data;
}

// ----------------------------------------------------------------------------- toasts
let pushToast = () => {};
export function toast(msg, err = false) { pushToast({ id: Math.random(), msg, err }); }
export function fail(e) { toast(e.message || String(e), true); }
export function Toasts() {
  const [list, setList] = useState([]);
  useEffect(() => {
    pushToast = t => {
      setList(l => [...l.slice(-3), t]);
      setTimeout(() => setList(l => l.filter(x => x.id !== t.id)), t.err ? 7000 : 3500);
    };
  }, []);
  return html`<div class="toasts" role="status" aria-live="polite">${list.map(t => html`<div key=${t.id} class=${"toast" + (t.err ? " err" : "")}>${t.msg}</div>`)}</div>`;
}

// ----------------------------------------------------------------------------- formatting
export const money = v => v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const int = v => v == null ? "—" : Number(v).toLocaleString("en-US");
export const compact = v => v == null ? "—" : Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
export function clock(s) {
  if (s == null || isNaN(s)) return "—";
  s = Math.max(0, s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = Math.floor(s % 60);
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
}
export const bytes = b => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " KB";
export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
export const dateOnly = iso => iso ? iso.slice(0, 10) : "";
export const nav = path => { location.hash = "#" + path; };
// Form drafts survive a server nap or an accidental refresh. Per-browser only; failures are ignored.
export const draft = {
  get(k) { try { return JSON.parse(localStorage.getItem("cf_draft_" + k) || "null"); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("cf_draft_" + k, JSON.stringify(v)); } catch (e) {} },
  clear(k) { try { localStorage.removeItem("cf_draft_" + k); } catch (e) {} },
};

// ----------------------------------------------------------------------------- hooks
export function useLoad(fn, deps = [], pollMs = 0) {
  const [state, set] = useState({ data: null, error: null, loading: true });
  const alive = useRef(true);
  const reload = async (quiet) => {
    if (!quiet) set(s => ({ ...s, loading: true }));
    try { const data = await fn(); if (alive.current) set({ data, error: null, loading: false }); }
    catch (e) { if (alive.current) set(s => ({ data: quiet ? s.data : null, error: e, loading: false })); }
  };
  useEffect(() => { alive.current = true; reload(); return () => { alive.current = false; }; }, deps);
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(() => { if (!document.hidden) reload(true); }, pollMs);
    return () => clearInterval(t);
  }, [pollMs, ...deps]);
  return { ...state, reload: () => reload(true), set: data => set({ data, error: null, loading: false }) };
}

// ----------------------------------------------------------------------------- small components
const P = {
  home: "M3 11l9-8 9 8M5 10v10h14V10", film: "M4 4h16v16H4zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4",
  scissors: "M6 6m-3 0a3 3 0 106 0 3 3 0 10-6 0M6 18m-3 0a3 3 0 106 0 3 3 0 10-6 0M8.5 8L20 19M8.5 16L20 5",
  target: "M12 12m-9 0a9 9 0 1018 0 9 9 0 10-18 0M12 12m-5 0a5 5 0 1010 0 5 5 0 10-10 0M12 12m-1 0a1 1 0 102 0 1 1 0 10-2 0",
  coins: "M12 3v18M17 7H9.5a2.5 2.5 0 000 5h5a2.5 2.5 0 010 5H6", cog: "M12 12m-3 0a3 3 0 106 0 3 3 0 10-6 0M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  play: "M7 4l13 8-13 8z", pause: "M7 4v16M17 4v16", back: "M15 5l-7 7 7 7", plus: "M12 5v14M5 12h14", x: "M6 6l12 12M18 6L6 18",
  out: "M9 4H5v16h4M16 8l4 4-4 4M20 12H9", up: "M12 19V5M5 12l7-7 7 7", ext: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
};
export const Icon = ({ n }) => html`<svg viewBox="0 0 24 24" aria-hidden="true"><path d=${P[n]} /></svg>`;

export function Loading({ what = "Loading" }) { return html`<div class="empty"><span class="spin"></span> ${what}…</div>`; }
export function ErrorBox({ error, retry }) {
  return html`<div class="empty"><b>Couldn't load this</b>${error.message}${retry && html`<div><button class="btn" onClick=${retry}>Try again</button></div>`}</div>`;
}
export function Field({ label, hint, wide, children }) {
  return html`<label class=${"f" + (wide ? " wide" : "")}><span>${label}</span>${children}${hint && html`<span class="hint">${hint}</span>`}</label>`;
}
export function Modal({ title, onClose, children }) {
  useEffect(() => {
    const k = e => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  return html`<div class="modal" onClick=${e => e.target === e.currentTarget && onClose()}>
    <div class="card" role="dialog" aria-label=${title}>
      <div class="row" style="margin-bottom:14px"><h2 class="grow">${title}</h2>
        <button class="btn ghost sm" onClick=${onClose} aria-label="Close"><${Icon} n="x" /></button></div>
      ${children}</div></div>`;
}
const STATUS = {
  uploading: ["Uploading", ""], uploaded: ["Not analysed", "warn"], analyzing: ["Analysing", "acc"], ready: ["Ready", "ok"], failed: ["Failed", "bad"],
  draft: ["Draft", ""], rendering: ["Rendering", "acc"], rendered: ["Needs review", "warn"], approved: ["Approved", "ok"], rejected: ["Rejected", "bad"],
  lead: ["Lead", ""], active: ["Active", "ok"], paused: ["Paused", "warn"], ended: ["Ended", ""],
  pending: ["Pending", "warn"], paid: ["Paid", "ok"], queued: ["Queued", ""], running: ["Running", "acc"], done: ["Done", "ok"], cancelled: ["Cancelled", ""],
};
export const Status = ({ s }) => { const [l, c] = STATUS[s] || [s, ""]; return html`<span class=${"pill " + c}>${l}</span>`; };
export const Progress = ({ v, money }) => html`<div class=${"bar" + (money ? " money" : "")}><i style=${`width:${Math.max(0, Math.min(100, v))}%`}></i></div>`;

export function JobLine({ job, onChange }) {
  if (!job) return null;
  const act = async (a) => { try { await api(`/jobs/${job.id}/${a}`, { method: "POST" }); onChange && onChange(); } catch (e) { fail(e); } };
  return html`<div class="col" style="gap:5px">
    <div class="row small"><span class="grow trunc">${job.status === "failed" ? html`<span style="color:var(--bad)">${job.error || "Failed"}</span>` : (job.stage || (job.status === "queued" ? "Waiting in queue" : "Working"))}</span>
      ${job.status === "running" && html`<span class="num muted">${Math.round(job.progress * 100)}%</span>`}
      ${job.status === "queued" && html`<button class="btn ghost sm" onClick=${() => act("cancel")}>Cancel</button>`}
      ${job.status === "failed" && html`<button class="btn sm" onClick=${() => act("retry")}>Retry</button>`}</div>
    ${job.status !== "failed" && html`<${Progress} v=${job.status === "queued" ? 0 : job.progress * 100} />`}</div>`;
}

// ----------------------------------------------------------------------------- charts (plain SVG, no library)
export function BarChart({ data, series, fmt = v => v, height = 150 }) {
  // data: [{label, <key>: number}], series: [{key, color, name}]  -> grouped bars
  const W = 600, H = height, pad = { l: 34, r: 6, t: 8, b: 20 };
  const max = Math.max(1e-9, ...data.flatMap(d => series.map(s => d[s.key] || 0)));
  const top = max <= 0 ? 1 : max * 1.12;
  const bw = (W - pad.l - pad.r) / Math.max(1, data.length);
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const every = Math.ceil(data.length / 7);
  return html`<svg class="chart" viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" style=${`height:${H}px`} role="img">
    ${[0, 0.5, 1].map(f => html`<g><line x1=${pad.l} x2=${W - pad.r} y1=${y(top * f)} y2=${y(top * f)} stroke="#262b32" stroke-dasharray=${f ? "2 3" : ""} />
      <text x=${pad.l - 5} y=${y(top * f) + 3} text-anchor="end">${fmt(top * f)}</text></g>`)}
    ${data.map((d, i) => html`<g>
      ${series.map((s, j) => { const v = d[s.key] || 0, w = bw * 0.72 / series.length;
        return html`<rect x=${pad.l + i * bw + bw * 0.14 + j * w} y=${y(v)} width=${Math.max(1, w - 1)} height=${Math.max(0, y(0) - y(v))} rx="1.5" fill=${s.color}><title>${d.label}: ${s.name} ${fmt(v)}</title></rect>`; })}
      ${i % every === 0 && html`<text x=${pad.l + i * bw + bw / 2} y=${H - 6} text-anchor="middle">${d.label}</text>`}</g>`)}
  </svg>`;
}
export const Legend = ({ series }) => html`<div class="legend">${series.map(s => html`<span><i style=${`background:${s.color}`}></i>${s.name}</span>`)}</div>`;
