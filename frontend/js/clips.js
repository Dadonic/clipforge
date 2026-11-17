import { html, useState, api, fail, toast, useLoad, Loading, ErrorBox, Status, JobLine, clock, nav, ago } from "./core.js";

const FILTERS = [["review", "To review", "draft,rendering,rendered"], ["approved", "Approved", "approved"], ["rejected", "Rejected", "rejected"], ["all", "All", ""]];

export function Clips() {
  const [filter, setFilter] = useState("review");
  const [sel, setSel] = useState(new Set());
  const [assign, setAssign] = useState("");
  const st = FILTERS.find(f => f[0] === filter)[2];
  const { data, error, loading, reload } = useLoad(() => Promise.all([api("/clips" + (st ? "?status=" + st : "")), api("/campaigns")]), [filter], 3000);
  if (loading && !data) return html`<${Loading} />`;
  if (error && !data) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const [clips, campaigns] = data;
  const ids = [...sel].filter(i => clips.some(c => c.id === i));
  const toggle = id => { const n = new Set(sel); n.has(id) ? n.delete(id) : n.add(id); setSel(n); };
  const all = () => setSel(ids.length === clips.length ? new Set() : new Set(clips.map(c => c.id)));
  const bulk = async (action, extra = {}) => {
    if (action === "delete" && !confirm(`Delete ${ids.length} clip${ids.length === 1 ? "" : "s"}?`)) return;
    try {
      const r = await api("/clips/bulk", { method: "POST", body: { ids, action, ...extra } });
      const verb = { render: "queued for render", approve: "approved", reject: "rejected", delete: "deleted", assign: "assigned" }[action];
      if (r.done.length) toast(`${r.done.length} clip${r.done.length === 1 ? "" : "s"} ${verb}`);
      if (r.skipped.length) toast(`${r.skipped.length} skipped. First: “${r.skipped[0].title}” — ${r.skipped[0].why}`, true);
      setSel(new Set()); reload();
    } catch (e) { fail(e); }
  };
  const rendered = ids.filter(i => clips.find(c => c.id === i).has_output);
  return html`
  <div class="head"><div class="grow"><h1>Review queue</h1><p>Nothing leaves here without your approval and a passing compliance check.</p></div>
    <div class="seg">${FILTERS.map(([k, l]) => html`<button class=${filter === k ? "on" : ""} onClick=${() => { setFilter(k); setSel(new Set()); }}>${l}</button>`)}</div></div>

  ${ids.length > 0 && html`<div class="card row wrap" style="margin-bottom:12px;position:sticky;top:8px;z-index:6;border-color:var(--accent)">
    <b>${ids.length} selected</b>
    <button class="btn sm" onClick=${() => bulk("render")}>Render</button>
    <button class="btn sm good" onClick=${() => bulk("approve")}>Approve</button>
    <button class="btn sm" onClick=${() => bulk("reject")}>Reject</button>
    <a class=${"btn sm" + (rendered.length ? "" : " hide")} href=${`/api/export.zip?ids=${rendered.join(",")}`}>Export ${rendered.length} as ZIP</a>
    ${campaigns.length > 0 && html`<select style="width:auto;max-width:180px" value=${assign} onChange=${e => { if (e.target.value) bulk("assign", { campaign_id: +e.target.value }); setAssign(""); }}><option value="">Assign to campaign…</option>${campaigns.map(c => html`<option value=${c.id}>${c.name}</option>`)}</select>`}
    <button class="btn sm ghost danger right" onClick=${() => bulk("delete")}>Delete</button></div>`}

  <div class="card flush">
    ${clips.length ? html`<div class="item" style="padding:9px 16px"><input type="checkbox" checked=${ids.length === clips.length} onChange=${all} aria-label="Select all" /><span class="small muted">Select all</span></div>
      ${clips.map(c => html`<div class="item" key=${c.id}>
        <input type="checkbox" checked=${sel.has(c.id)} onChange=${() => toggle(c.id)} aria-label=${"Select " + c.title} />
        <div class="grow click" style="cursor:pointer" onClick=${() => nav("/studio/" + c.id)}>
          <div class="row wrap" style="gap:6px"><b class="trunc" style="max-width:100%">${c.title}</b><${Status} s=${c.status} />
            ${c.needs_render && c.status !== "rendering" && html`<span class="pill warn">${c.has_output ? "Edits not rendered" : "Not rendered"}</span>`}
            ${(c.flags || []).some(f => f.severity === "review") && !c.flags_cleared && html`<span class="pill warn">Flag to review</span>`}
            ${c.compliance && !c.compliance.ok && html`<span class="pill bad">${c.compliance.fails} rule${c.compliance.fails === 1 ? "" : "s"} failing</span>`}
            ${c.submissions > 0 && html`<span class="pill est">Posted ×${c.submissions}</span>`}</div>
          <div class="small muted">${c.duration.toFixed(0)}s · ${c.campaign_name || "No campaign"} · from ${c.video_title} at ${clock(c.start)} · ${ago(c.created_at)}</div>
          ${c.job && html`<div style="margin-top:6px;max-width:380px"><${JobLine} job=${c.job} onChange=${reload} /></div>`}</div>
        <button class="btn sm" onClick=${() => nav("/studio/" + c.id)}>Open</button></div>`)}`
      : html`<div class="empty"><b>${filter === "review" ? "Nothing to review" : "No clips here"}</b>${filter === "review" ? "Open a recording and turn its ranked highlights into clips." : ""}<div><a class="btn" href="#/library">Go to recordings</a></div></div>`}
  </div>`;
}
