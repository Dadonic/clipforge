import { html, useState, api, fail, toast, useLoad, Loading, ErrorBox, Field, Status, Modal, Icon, Progress, money, int, dateOnly } from "./core.js";

const BLANK = { name: "", streamer: "", whop_url: "", instructions: "", payout_per_1k: 1, max_budget: "", max_payout_per_clip: "", min_duration: 15, max_duration: 60,
  required_hashtags: "", required_cta: "", approved_platforms: [], prohibited: "", submission_instructions: "", deadline: "", status: "active",
  require_watermark: false, watermark_text: "", require_attribution: true, notes: "", preset_layout: "", preset_caption: "" };

function toForm(c) {
  return { ...BLANK, ...c, max_budget: c.max_budget ?? "", max_payout_per_clip: c.max_payout_per_clip ?? "", required_hashtags: (c.required_hashtags || []).join(" "),
    prohibited: (c.prohibited || []).join(", "), deadline: dateOnly(c.deadline), preset_layout: (c.preset || {}).layout || "", preset_caption: (c.preset || {}).caption_style || "" };
}
function toBody(f) {
  const n = v => v === "" || v == null ? null : +v;
  return { name: f.name, streamer: f.streamer, whop_url: f.whop_url, instructions: f.instructions, payout_per_1k: +f.payout_per_1k || 0, max_budget: n(f.max_budget),
    max_payout_per_clip: n(f.max_payout_per_clip), min_duration: +f.min_duration || 0, max_duration: +f.max_duration || 60,
    required_hashtags: f.required_hashtags.split(/[\s,]+/).filter(Boolean), required_cta: f.required_cta, approved_platforms: f.approved_platforms,
    prohibited: f.prohibited.split(",").map(s => s.trim()).filter(Boolean), submission_instructions: f.submission_instructions, deadline: f.deadline || null,
    status: f.status, require_watermark: f.require_watermark, watermark_text: f.watermark_text, require_attribution: f.require_attribution, notes: f.notes,
    preset: { ...(f.preset_layout ? { layout: f.preset_layout } : {}), ...(f.preset_caption ? { caption_style: f.preset_caption } : {}) } };
}

function CampaignForm({ initial, sys, onClose }) {
  const [f, setF] = useState(toForm(initial || {}));
  const [busy, setBusy] = useState(false);
  const up = k => e => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const plat = p => setF({ ...f, approved_platforms: f.approved_platforms.includes(p) ? f.approved_platforms.filter(x => x !== p) : [...f.approved_platforms, p] });
  const save = async () => { setBusy(true); try { await api(initial ? `/campaigns/${initial.id}` : "/campaigns", { method: initial ? "PATCH" : "POST", body: toBody(f) }); toast("Campaign saved"); onClose(true); } catch (e) { fail(e); } finally { setBusy(false); } };
  return html`<${Modal} title=${initial ? "Edit campaign" : "Add a campaign"} onClose=${() => onClose(false)}>
    <p class="muted small" style="margin-bottom:12px">Copy the details from the campaign's page on Whop. These rules drive the compliance check and the earnings maths.</p>
    <div class="fields">
      <${Field} label="Campaign name"><input value=${f.name} onInput=${up("name")} /><//>
      <${Field} label="Streamer / brand"><input value=${f.streamer} onInput=${up("streamer")} /><//>
      <${Field} wide label="Whop campaign link"><input value=${f.whop_url} onInput=${up("whop_url")} placeholder="https://whop.com/…" /><//>
      <${Field} label="Payout per 1,000 views ($)"><input type="number" step="0.01" min="0" value=${f.payout_per_1k} onInput=${up("payout_per_1k")} /><//>
      <${Field} label="Status"><select value=${f.status} onChange=${up("status")}><option value="lead">Lead (not joined yet)</option><option value="active">Active</option><option value="paused">Paused</option><option value="ended">Ended</option></select><//>
      <${Field} label="Total campaign budget ($)" hint="Optional"><input type="number" min="0" value=${f.max_budget} onInput=${up("max_budget")} /><//>
      <${Field} label="Max payout per clip ($)" hint="Optional; caps the estimate"><input type="number" min="0" value=${f.max_payout_per_clip} onInput=${up("max_payout_per_clip")} /><//>
      <${Field} label="Min clip length (s)"><input type="number" min="0" value=${f.min_duration} onInput=${up("min_duration")} /><//>
      <${Field} label="Max clip length (s)"><input type="number" min="1" value=${f.max_duration} onInput=${up("max_duration")} /><//>
      <${Field} label="Required hashtags" hint="Separated by spaces"><input value=${f.required_hashtags} onInput=${up("required_hashtags")} placeholder="#gta6 #streamer" /><//>
      <${Field} label="Required caption / CTA text"><input value=${f.required_cta} onInput=${up("required_cta")} placeholder="e.g. Watch live on kick.com/name" /><//>
      <${Field} wide label="Approved platforms" hint="Leave all off if the campaign allows any platform">
        <div class="row wrap">${sys.platforms.map(p => html`<label class="f inline"><input type="checkbox" checked=${f.approved_platforms.includes(p)} onChange=${() => plat(p)} /><span>${p.replace(/_/g, " ")}</span></label>`)}</div><//>
      <${Field} wide label="Prohibited words or topics" hint="Comma separated. Checked against the title, description and transcript."><input value=${f.prohibited} onInput=${up("prohibited")} /><//>
      <label class="f inline"><input type="checkbox" checked=${f.require_watermark} onChange=${up("require_watermark")} /><span>Watermark required</span></label>
      <${Field} label="Watermark text"><input value=${f.watermark_text} onInput=${up("watermark_text")} disabled=${!f.require_watermark} /><//>
      <label class="f inline wide"><input type="checkbox" checked=${f.require_attribution} onChange=${up("require_attribution")} /><span>Streamer must be credited in the title, description, hashtags or watermark</span></label>
      <${Field} label="Deadline"><input type="date" value=${f.deadline} onInput=${up("deadline")} /><//>
      <${Field} label="Default layout for this campaign"><select value=${f.preset_layout} onChange=${up("preset_layout")}><option value="">Use my default</option>${Object.entries(sys.layouts).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
      <${Field} label="Default caption style"><select value=${f.preset_caption} onChange=${up("preset_caption")}><option value="">Use my default</option>${Object.entries(sys.caption_styles).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
      <${Field} wide label="Campaign instructions (paste from Whop)"><textarea value=${f.instructions} onInput=${up("instructions")}></textarea><//>
      <${Field} wide label="How to submit"><textarea style="min-height:50px" value=${f.submission_instructions} onInput=${up("submission_instructions")}></textarea><//>
      <${Field} wide label="Your notes"><textarea style="min-height:50px" value=${f.notes} onInput=${up("notes")}></textarea><//>
    </div>
    <div class="row" style="margin-top:14px"><button class="btn primary right" disabled=${busy || !f.name.trim()} onClick=${save}>Save campaign</button></div><//>`;
}

export function Campaigns({ sys }) {
  const { data, error, loading, reload } = useLoad(() => api("/campaigns"), []);
  const [form, setForm] = useState(null);   // null | "new" | campaign
  if ((loading && !data) || !sys) return html`<${Loading} />`;
  if (error && !data) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const del = async c => { if (!confirm(`Delete “${c.name}”?`)) return; try { await api(`/campaigns/${c.id}`, { method: "DELETE" }); reload(); } catch (e) { fail(e); } };
  const setStatus = async (c, status) => { try { await api(`/campaigns/${c.id}`, { method: "PATCH", body: { status } }); reload(); } catch (e) { fail(e); } };
  const leads = data.filter(c => c.status === "lead"), rest = data.filter(c => c.status !== "lead");
  const Card = c => html`<div class="card col" key=${c.id}>
    <div class="row wrap"><h2 class="grow trunc">${c.name}</h2><${Status} s=${c.status} /><span class="pill est">${money(c.payout_per_1k)} / 1k views</span></div>
    <div class="small muted">${c.streamer || "No streamer set"} · ${c.min_duration}–${c.max_duration}s${c.required_hashtags.length ? " · " + c.required_hashtags.join(" ") : ""}${c.deadline ? " · ends " + dateOnly(c.deadline) : ""}</div>
    ${c.status !== "lead" && html`<div class="grid g4" style="gap:8px">
      ${[["Clips", int(c.stats.clips)], ["Approved views", int(c.stats.approved_views)], ["Estimated", money(c.stats.estimated)], ["Confirmed", money(c.stats.confirmed)]].map(([k, v]) => html`<div><div class="tiny dim">${k}</div><div class="num" style="font-weight:600">${v}</div></div>`)}</div>
      ${c.stats.budget_used_pct != null && html`<div><div class="row tiny dim" style="margin-bottom:3px"><span class="grow">Your estimated share of the ${money(c.max_budget)} budget</span><span class="num">${c.stats.budget_used_pct}%</span></div><${Progress} v=${c.stats.budget_used_pct} /></div>`}`}
    ${c.notes && html`<p class="small muted">${c.notes}</p>`}
    <div class="row wrap">${c.whop_url && html`<a class="btn sm" href=${c.whop_url} target="_blank" rel="noopener">Open on Whop <${Icon} n="ext" /></a>`}
      <button class="btn sm" onClick=${() => setForm(c)}>Edit rules</button>
      ${c.status === "lead" && html`<button class="btn sm primary" onClick=${() => setStatus(c, "active")}>I joined — mark active</button>`}
      ${c.status === "active" && html`<button class="btn sm ghost" onClick=${() => setStatus(c, "ended")}>Mark ended</button>`}
      <button class="btn sm ghost danger right" onClick=${() => del(c)}>Delete</button></div></div>`;
  return html`
  <div class="head"><div class="grow"><h1>Campaigns</h1><p>The Whop Content Rewards campaigns you clip for, with their rules and payout rates.</p></div>
    <button class="btn primary" onClick=${() => setForm("new")}><${Icon} n="plus" />Add campaign</button></div>
  <div class="note" style="margin-bottom:14px"><b style="color:var(--ink)">How this connects to Whop:</b> ${sys.whop.detail} Find campaigns on <a href="https://whop.com" target="_blank" rel="noopener">Whop</a>, then save the ones worth doing here.</div>
  ${data.length === 0 && html`<div class="card"><div class="empty"><b>No campaigns yet</b>Add the campaign you joined, or save a few as leads while you compare rates and rules.<div><button class="btn primary" onClick=${() => setForm("new")}>Add your first campaign</button></div></div></div>`}
  ${rest.length > 0 && html`<div class="grid g2" style="margin-bottom:18px">${rest.map(Card)}</div>`}
  ${leads.length > 0 && html`<h2 style="margin-bottom:10px">Leads to evaluate</h2><div class="grid g2">${leads.map(Card)}</div>`}
  ${form && html`<${CampaignForm} initial=${form === "new" ? null : form} sys=${sys} onClose=${ok => { setForm(null); ok && reload(); }} />`}`;
}
