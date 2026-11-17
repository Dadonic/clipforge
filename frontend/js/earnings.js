import { html, useState, api, fail, toast, useLoad, Loading, ErrorBox, Field, Modal, Icon, Progress, BarChart, Legend, money, int, compact, dateOnly } from "./core.js";

function AddSubmission({ sys, onClose }) {
  const { data } = useLoad(() => Promise.all([api("/clips?status=approved"), api("/campaigns")]), []);
  const [f, setF] = useState({ clip_id: "", platform: "tiktok", url: "", views: 0, approved_views: 0 });
  if (!data) return html`<${Modal} title="Record a submission" onClose=${() => onClose(false)}><${Loading} /><//>`;
  const [clips] = data;
  const save = async () => { try { await api("/submissions", { method: "POST", body: { ...f, clip_id: +f.clip_id, views: +f.views, approved_views: +f.approved_views } }); toast("Submission recorded"); onClose(true); } catch (e) { fail(e); } };
  return html`<${Modal} title="Record a submission" onClose=${() => onClose(false)}>
    ${clips.length === 0 ? html`<div class="empty"><b>No approved clips yet</b>Only approved clips can be recorded as posted.<div><a class="btn" href="#/clips">Open review queue</a></div></div>` : html`
    <div class="fields">
      <${Field} wide label="Approved clip"><select value=${f.clip_id} onChange=${e => setF({ ...f, clip_id: e.target.value })}><option value="">Choose…</option>${clips.map(c => html`<option value=${c.id}>${c.title} (${c.campaign_name || "no campaign"})</option>`)}</select><//>
      <${Field} label="Platform"><select value=${f.platform} onChange=${e => setF({ ...f, platform: e.target.value })}>${sys.platforms.map(p => html`<option value=${p}>${p.replace(/_/g, " ")}</option>`)}</select><//>
      <${Field} label="Link to the posted clip"><input value=${f.url} onInput=${e => setF({ ...f, url: e.target.value })} placeholder="https://…" /><//>
      <${Field} label="Views"><input type="number" min="0" value=${f.views} onInput=${e => setF({ ...f, views: e.target.value })} /><//>
      <${Field} label="Approved views"><input type="number" min="0" value=${f.approved_views} onInput=${e => setF({ ...f, approved_views: e.target.value })} /><//>
    </div><div class="row" style="margin-top:14px"><button class="btn primary right" disabled=${!f.clip_id || !f.url} onClick=${save}>Save</button></div>`}<//>`;
}

function Row({ s, onSaved }) {
  const [e, setE] = useState(null);
  const cur = e || s;
  const set = (k, v) => setE({ ...cur, [k]: v });
  const save = async () => {
    try { await api(`/submissions/${s.id}`, { method: "PATCH", body: { views: +cur.views, approved_views: +cur.approved_views, confirmed_earnings: +cur.confirmed_earnings, payment_status: cur.payment_status, payment_date: dateOnly(cur.payment_date) || null } });
      setE(null); toast("Updated"); onSaved(); } catch (x) { fail(x); }
  };
  const del = async () => { if (!confirm("Delete this submission and its history?")) return; try { await api(`/submissions/${s.id}`, { method: "DELETE" }); onSaved(); } catch (x) { fail(x); } };
  return html`<tr>
    <td style="max-width:220px"><div class="trunc" style="font-weight:500">${s.clip_title || "(clip deleted)"}</div><div class="small muted trunc">${s.campaign_name || "No campaign"} · ${s.platform.replace(/_/g, " ")} · <a href=${s.url} target="_blank" rel="noopener">link</a></div></td>
    <td class="n"><input type="number" min="0" style="width:92px;text-align:right" value=${cur.views} onInput=${x => set("views", x.target.value)} aria-label="Views" /></td>
    <td class="n"><input type="number" min="0" style="width:92px;text-align:right" value=${cur.approved_views} onInput=${x => set("approved_views", x.target.value)} aria-label="Approved views" /></td>
    <td class="n small muted">${money(s.payout_per_1k)}</td>
    <td class="n" style="color:var(--est)">${money(s.estimated_earnings)}</td>
    <td class="n"><input type="number" min="0" step="0.01" style="width:84px;text-align:right" value=${cur.confirmed_earnings} onInput=${x => set("confirmed_earnings", x.target.value)} aria-label="Confirmed earnings" /></td>
    <td><select style="width:104px" value=${cur.payment_status} onChange=${x => set("payment_status", x.target.value)} aria-label="Payment status">${["pending", "approved", "paid", "rejected"].map(p => html`<option value=${p}>${p}</option>`)}</select></td>
    <td><input type="date" style="width:138px" value=${dateOnly(cur.payment_date)} onInput=${x => set("payment_date", x.target.value)} aria-label="Payment date" /></td>
    <td style="white-space:nowrap">${e && html`<button class="btn sm primary" onClick=${save}>Save</button> `}<button class="btn sm ghost danger" onClick=${del} aria-label="Delete submission"><${Icon} n="x" /></button></td></tr>`;
}

export function Earnings({ sys }) {
  const { data, error, loading, reload } = useLoad(() => Promise.all([api("/earnings"), api("/submissions")]), []);
  const [add, setAdd] = useState(false);
  const [imp, setImp] = useState(null);
  if ((loading && !data) || !sys) return html`<${Loading} />`;
  if (error && !data) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const [e, subs] = data;
  const csv = async ev => { const f = ev.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
    try { const r = await api("/submissions/import-csv", { method: "POST", body: fd, raw: true }); setImp(r); reload(); } catch (x) { fail(x); } ev.target.value = ""; };
  const series = [{ key: "confirmed", color: "#3ddc97", name: "Confirmed (paid)" }, { key: "estimated", color: "#8ea0ff", name: "Estimated growth" }];
  const W = ({ k, w }) => html`<div class="stat"><div class="k">${k}</div><div class="row" style="gap:14px;margin-top:2px"><div><div class="num" style="font-weight:700;font-size:17px;color:var(--money)">${money(w.confirmed)}</div><div class="tiny dim">confirmed</div></div>
    <div><div class="num" style="font-weight:700;font-size:17px;color:var(--est)">${money(w.estimated)}</div><div class="tiny dim">estimated</div></div></div></div>`;
  return html`
  <div class="head"><div class="grow"><h1>Earnings</h1><p><span style="color:var(--est)">Estimated</span> = approved views ÷ 1,000 × campaign rate. <span style="color:var(--money)">Confirmed</span> = what you marked as actually paid.</p></div>
    <label class="btn" style="cursor:pointer">Import CSV<input type="file" accept=".csv" class="hide" onChange=${csv} /></label>
    <button class="btn primary" onClick=${() => setAdd(true)}><${Icon} n="plus" />Record submission</button></div>

  <div class="grid g4" style="margin-bottom:12px">
    <div class="stat money"><div class="k">Confirmed, all time</div><div class="v num">${money(e.confirmed_total)}</div><div class="s">${e.paid_count} paid</div></div>
    <div class="stat est"><div class="k">Estimated, all time</div><div class="v num">${money(e.estimated_total)}</div><div class="s">Not guaranteed until paid</div></div>
    <div class="stat"><div class="k">Pending payouts (estimated)</div><div class="v num">${money(e.pending_estimated)}</div><div class="s">Submissions not yet paid or rejected</div></div>
    <div class="stat"><div class="k">Approved views</div><div class="v num">${compact(e.approved_views)}</div><div class="s">${compact(e.total_views)} total across ${e.submissions} submission${e.submissions === 1 ? "" : "s"}</div></div>
  </div>
  <div class="grid g3" style="margin-bottom:14px"><${W} k="Today" w=${e.today} /><${W} k="Last 7 days" w=${e.week} /><${W} k="Last 30 days" w=${e.month} /></div>

  ${imp && html`<div class="note" style="margin-bottom:14px"><b style="color:var(--ink)">CSV import:</b> ${imp.updated} submission${imp.updated === 1 ? "" : "s"} updated.
    ${imp.unmatched.length > 0 && html` ${imp.unmatched.length} row${imp.unmatched.length === 1 ? "" : "s"} skipped because the link isn't recorded here yet (first: ${imp.unmatched[0]}). Record those submissions, then import again.`}
    ${imp.errors.length > 0 && html` Problems: ${imp.errors.slice(0, 3).join(" ")}`} <button class="btn sm ghost" onClick=${() => setImp(null)}>Dismiss</button></div>`}

  <div class="grid g2" style="margin-bottom:14px">
    <div class="card"><h2>Last 30 days</h2>${e.series.some(x => x.confirmed || x.estimated) ? html`<${BarChart} data=${e.series.map(x => ({ ...x, label: x.date.slice(5) }))} series=${series} fmt=${v => "$" + (v >= 100 ? Math.round(v) : v.toFixed(1))} /><${Legend} series=${series} />`
      : html`<div class="empty">Nothing recorded in this period yet.</div>`}</div>
    <div class="card flush"><div style="padding:16px 16px 4px"><h2>By campaign</h2></div>
      ${e.per_campaign.length ? html`<div class="scrollx"><table class="tbl"><thead><tr><th>Campaign</th><th class="n">Subs</th><th class="n">Estimated</th><th class="n">Confirmed</th><th>Budget</th></tr></thead><tbody>
        ${e.per_campaign.map(c => html`<tr><td class="trunc" style="max-width:160px">${c.name}</td><td class="n">${c.submissions}</td><td class="n" style="color:var(--est)">${money(c.estimated)}</td><td class="n" style="color:var(--money)">${money(c.confirmed)}</td>
          <td style="min-width:90px">${c.budget_used_pct == null ? html`<span class="dim small">—</span>` : html`<div class="row"><div class="grow"><${Progress} v=${c.budget_used_pct} /></div><span class="small num muted">${c.budget_used_pct}%</span></div>`}</td></tr>`)}</tbody></table></div>`
        : html`<div class="empty">No campaigns with activity yet.</div>`}</div>
  </div>

  <div class="card flush"><div class="row" style="padding:14px 16px;border-bottom:1px solid var(--line)"><h2 class="grow">Submissions</h2><span class="hint">Edit numbers inline as the campaign reports them. CSV import matches rows by post link.</span></div>
    ${subs.length ? html`<div class="scrollx"><table class="tbl"><thead><tr><th>Clip</th><th class="n">Views</th><th class="n">Approved views</th><th class="n">Rate/1k</th><th class="n">Estimated</th><th class="n">Confirmed $</th><th>Payment</th><th>Paid on</th><th></th></tr></thead>
        <tbody>${subs.map(s => html`<${Row} key=${s.id + ":" + s.views + ":" + s.approved_views + ":" + s.payment_status + ":" + s.confirmed_earnings} s=${s} onSaved=${reload} />`)}</tbody></table></div>`
      : html`<div class="empty"><b>No submissions recorded</b>After you post an approved clip and submit it on the campaign page, record the link here. Earnings are never assumed.<div><button class="btn primary" onClick=${() => setAdd(true)}>Record a submission</button></div></div>`}</div>
  ${add && html`<${AddSubmission} sys=${sys} onClose=${ok => { setAdd(false); ok && reload(); }} />`}`;
}
