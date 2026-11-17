import { html, api, useLoad, Loading, ErrorBox, money, int, compact, ago, BarChart, Legend, JobLine, Status, Progress } from "./core.js";

const Stat = ({ k, v, s, cls = "" }) => html`<div class=${"stat " + cls}><div class="k">${k}</div><div class="v num">${v}</div>${s && html`<div class="s">${s}</div>`}</div>`;

export function Dashboard() {
  const { data: d, error, loading, reload } = useLoad(() => api("/dashboard"), [], 4000);
  if (loading && !d) return html`<${Loading} />`;
  if (error && !d) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const e = d.earnings;
  const fresh = d.videos_total === 0;
  const eSeries = [{ key: "confirmed", color: "#3ddc97", name: "Confirmed (paid)" }, { key: "estimated", color: "#8ea0ff", name: "Estimated (from approved views)" }];
  const hasMoney = e.series.some(x => x.confirmed || x.estimated);
  const hasProd = d.production.some(x => x.clips);
  return html`
  <div class="head"><div class="grow"><h1>Command center</h1><p>Everything here is computed from your own recordings, clips and submissions.</p></div>
    <a class="btn primary" href="#/library">Add a recording</a></div>

  ${fresh && html`<div class="card" style="margin-bottom:14px"><h2>Get your first clips out</h2>
    <ol class="muted" style="margin:0;padding-left:18px;line-height:1.9">
      <li><a href="#/campaigns">Add the campaign</a> you joined on Whop (rate, rules, hashtags).</li>
      <li><a href="#/library">Upload a stream recording</a> you are allowed to use and run the analysis.</li>
      <li>Turn the top-ranked moments into vertical clips, review them, approve.</li>
      <li>Post them, then record each link under <a href="#/earnings">Earnings</a> to track views and payouts.</li></ol></div>`}

  <div class="grid g4" style="margin-bottom:12px">
    <${Stat} cls="money" k="Confirmed earnings" v=${money(e.confirmed_total)} s=${`${e.paid_count} paid submission${e.paid_count === 1 ? "" : "s"}`} />
    <${Stat} cls="est" k="Estimated earnings" v=${money(e.estimated_total)} s="Approved views × campaign rate. Not money yet." />
    <${Stat} k="Approved campaign views" v=${compact(e.approved_views)} s=${`${compact(e.total_views)} total views recorded`} />
    <${Stat} k="Avg per 1,000 approved views" v=${e.avg_per_1k_estimated == null ? "—" : money(e.avg_per_1k_estimated)} s=${e.avg_per_1k_confirmed != null ? `${money(e.avg_per_1k_confirmed)} on paid clips` : "No paid clips yet"} />
  </div>
  <div class="grid g4" style="margin-bottom:14px">
    <${Stat} k="Recordings processed" v=${int(d.videos_processed)} s=${`${d.videos_total} uploaded`} />
    <${Stat} k="Clips generated" v=${int(d.clips_generated)} s=${`${d.clips_rejected} rejected`} />
    <${Stat} k="Awaiting review" v=${int(d.clips_awaiting_review)} s=${d.clips_awaiting_review ? html`<a href="#/clips">Open review queue</a>` : "Nothing waiting"} />
    <${Stat} k="Approved clips" v=${int(d.clips_approved)} s=${`${d.active_campaigns} active campaign${d.active_campaigns === 1 ? "" : "s"}`} />
  </div>

  <div class="grid g2" style="margin-bottom:14px">
    <div class="card"><div class="row" style="margin-bottom:8px"><h2 class="grow">Earnings, last 30 days</h2></div>
      ${hasMoney ? html`<${BarChart} data=${e.series.map(x => ({ ...x, label: x.date.slice(5) }))} series=${eSeries} fmt=${v => "$" + (v >= 100 ? Math.round(v) : v.toFixed(1))} /><${Legend} series=${eSeries} />`
        : html`<div class="empty"><b>No earnings recorded yet</b>Record a submission and update its approved views to see this fill in.</div>`}</div>
    <div class="card"><div class="row" style="margin-bottom:8px"><h2 class="grow">Clips produced, last 14 days</h2></div>
      ${hasProd ? html`<${BarChart} data=${d.production.map(x => ({ ...x, label: x.date.slice(5) }))} series=${[{ key: "clips", color: "#ff6a2b", name: "Clips" }]} fmt=${v => Math.round(v)} />`
        : html`<div class="empty"><b>No clips yet</b>Clips appear here on the day you create them.</div>`}</div>
  </div>

  <div class="grid g23" style="margin-bottom:14px">
    <div class="card flush"><div style="padding:16px 16px 4px"><h2>Campaign performance</h2></div>
      ${e.per_campaign.length ? html`<div class="scrollx"><table class="tbl"><thead><tr><th>Campaign</th><th class="n">Rate / 1k</th><th class="n">Approved views</th><th class="n">Estimated</th><th class="n">Confirmed</th><th>Budget used</th></tr></thead>
        <tbody>${e.per_campaign.map(c => html`<tr><td><a href="#/campaigns" style="text-decoration:none;font-weight:500">${c.name}</a> <${Status} s=${c.status} /></td>
          <td class="n">${money(c.payout_per_1k)}</td><td class="n">${int(c.approved_views)}</td><td class="n" style="color:var(--est)">${money(c.estimated)}</td>
          <td class="n" style="color:var(--money)">${money(c.confirmed)}</td>
          <td style="min-width:110px">${c.budget_used_pct == null ? html`<span class="dim small">No budget set</span>` : html`<div class="row"><div class="grow"><${Progress} v=${c.budget_used_pct} /></div><span class="small num muted">${c.budget_used_pct}%</span></div>`}</td></tr>`)}</tbody></table></div>`
        : html`<div class="empty"><b>No campaigns yet</b><a class="btn" href="#/campaigns">Add a campaign</a></div>`}</div>
    <div class="card"><h2>Revenue opportunities</h2>
      ${d.opportunities.length ? html`<div class="col">${d.opportunities.slice(0, 5).map(o => html`<div>
          <div class="row"><b class="grow trunc">${o.name}</b><span class="pill est">${money(o.payout_per_1k)}/1k</span></div>
          <div class="small muted">${o.approved_unposted > 0 ? html`<span style="color:var(--money)">${o.approved_unposted} approved clip${o.approved_unposted === 1 ? "" : "s"} not posted yet</span>` : "No approved clips waiting"}
            ${o.in_review > 0 ? ` · ${o.in_review} in review` : ""}${o.days_left != null ? ` · ${o.days_left < 0 ? "deadline passed" : o.days_left + " d left"}` : ""}</div></div>`)}</div>`
        : html`<p class="muted small">Active campaigns show here with the clips you have ready to post for each.</p>`}</div>
  </div>

  <div class="grid g3">
    <div class="card"><h2>Processing</h2>
      ${d.jobs.length || d.failed_jobs.length ? html`<div class="col">${[...d.jobs, ...d.failed_jobs].map(j => html`<div><div class="small trunc" style="font-weight:500;margin-bottom:3px">${j.label}</div><${JobLine} job=${j} onChange=${reload} /></div>`)}</div>`
        : html`<p class="muted small">No jobs running.</p>`}</div>
    <div class="card"><h2>Top clips by approved views</h2>
      ${e.top_clips.length ? html`<div class="col">${e.top_clips.map(c => html`<a class="row" style="text-decoration:none" href=${"#/studio/" + c.id}><span class="grow trunc">${c.title}</span><span class="num muted small">${compact(c.approved_views)}</span><span class="num small" style="color:var(--est)">${money(c.estimated)}</span></a>`)}</div>`
        : html`<p class="muted small">Ranks your posted clips once views are recorded.</p>`}</div>
    <div class="card"><h2>Recent activity</h2>
      ${d.activity.length ? html`<div class="col" style="gap:7px">${d.activity.map(a => html`<div class="row small"><span class="grow">${a.message}</span><span class="dim" style="white-space:nowrap">${ago(a.created_at)}</span></div>`)}</div>`
        : html`<p class="muted small">Uploads, analyses, renders and approvals are logged here.</p>`}</div>
  </div>`;
}
