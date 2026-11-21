import { html, useState, useEffect, useRef, api, fail, toast, useLoad, Loading, ErrorBox, Field, Status, JobLine, Progress, Icon, clock, bytes, ago, nav, money } from "./core.js";

// ----------------------------------------------------------------------------- upload (chunked, resumable)
async function uploadFile(file, meta, onProgress, after) {
  const v = await api("/videos", { method: "POST", body: { ...meta, filename: file.name, size: file.size } });
  const step = v.chunk_size;
  for (let off = 0; off < file.size; off += step) {
    const blob = file.slice(off, off + step);
    let tries = 0;
    for (;;) {
      try { await api(`/videos/${v.id}/chunk?offset=${off}`, { method: "PUT", body: blob, raw: true }); break; }
      catch (e) { if (++tries >= 4 || (e.status && e.status < 500)) throw e; await new Promise(r => setTimeout(r, 1500 * tries)); }
    }
    onProgress(Math.min(1, (off + blob.size) / file.size));
  }
  return api(`/videos/${v.id}/complete`, { method: "POST", body: after || {} });
}

function AddRecording({ campaigns, onDone, user }) {
  const us = (user && user.settings) || {};
  const [auto, setAuto] = useState({ analyze: true, clips: us.auto_clips ?? 10 });
  const [mode, setMode] = useState("file");
  const [f, setF] = useState({ title: "", streamer: "", campaign_id: "", authorized: false, source_note: "", url: "" });
  const [file, setFile] = useState(null);
  const [prog, setProg] = useState(null);
  const up = k => e => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const meta = () => ({ title: f.title, streamer: f.streamer, campaign_id: f.campaign_id ? +f.campaign_id : null, authorized: f.authorized, source_note: f.source_note });
  const go = async () => {
    try {
      if (mode === "file") {
        if (!file) return toast("Choose a video file first", true);
        setProg(0);
        const v = await uploadFile(file, meta(), setProg, { analyze: auto.analyze, auto_clips: auto.analyze ? +auto.clips || 0 : 0 });
        toast(auto.analyze ? "Upload finished. Analysis started." : "Upload finished"); onDone(v);
      } else {
        const v = await api("/videos/import-url", { method: "POST", body: { ...meta(), url: f.url } });
        toast("Import started"); onDone(v);
      }
    } catch (e) { fail(e); } finally { setProg(null); }
  };
  const busy = prog !== null;
  return html`<div class="card" style="margin-bottom:14px"><div class="row" style="margin-bottom:12px"><h2 class="grow">Add a recording</h2>
      <div class="seg"><button class=${mode === "file" ? "on" : ""} onClick=${() => setMode("file")}>Upload file</button><button class=${mode === "url" ? "on" : ""} onClick=${() => setMode("url")}>Direct file link</button></div></div>
    <div class="fields">
      ${mode === "file"
        ? html`<${Field} wide label="Video file (MP4, MOV, MKV, WebM)" hint=${file ? `${bytes(file.size)} · uploads in 8 MB pieces and resumes if the connection drops` : "Long streams are fine; the file is never loaded into browser memory."}>
            <input type="file" accept=".mp4,.mov,.mkv,.webm,video/*" disabled=${busy} onChange=${e => { const x = e.target.files[0]; setFile(x); if (x && !f.title) setF({ ...f, title: x.name.replace(/\.[^.]+$/, "") }); }} /><//>`
        : html`<${Field} wide label="Link to the video file" hint="Must point straight at a file you were given access to (for example a download link from the streamer). Pages on Twitch, Kick or YouTube are not ripped."><input placeholder="https://…/stream.mp4" value=${f.url} onInput=${up("url")} /><//>`}
      <${Field} label="Title"><input value=${f.title} onInput=${up("title")} /><//>
      <${Field} label="Streamer"><input value=${f.streamer} onInput=${up("streamer")} placeholder="Name used for credit and titles" /><//>
      <${Field} label="Campaign (optional)"><select value=${f.campaign_id} onChange=${up("campaign_id")}><option value="">None yet</option>${campaigns.map(c => html`<option value=${c.id}>${c.name}</option>`)}</select><//>
      <${Field} label="Where the footage came from" hint="For your own records"><input value=${f.source_note} onInput=${up("source_note")} placeholder="e.g. campaign's official VOD folder" /><//>
      ${mode === "file" && html`<div class="wide row wrap"><label class="f inline"><input type="checkbox" checked=${auto.analyze} onChange=${e => setAuto({ ...auto, analyze: e.target.checked })} /><span>Analyse as soon as it uploads, then make the top</span></label>
        <input type="number" min="0" max="30" style="width:70px" value=${auto.clips} disabled=${!auto.analyze} onInput=${e => setAuto({ ...auto, clips: e.target.value })} aria-label="Number of clips to make automatically" /><span class="small">clips automatically (0 = just rank them)</span></div>`}
      <label class="f inline wide"><input type="checkbox" checked=${f.authorized} onChange=${up("authorized")} />
        <span>I'm allowed to use this footage (campaign-provided or permitted by the streamer). It is not leaked or pre-release material.</span></label>
    </div>
    ${busy && html`<div style="margin-top:12px"><div class="row small" style="margin-bottom:4px"><span class="grow">Uploading… keep this tab open</span><span class="num">${Math.round(prog * 100)}%</span></div><${Progress} v=${prog * 100} /></div>`}
    <div class="row" style="margin-top:14px"><button class="btn primary" disabled=${busy || !f.authorized} onClick=${go}>${mode === "file" ? "Upload" : "Import"}</button>
      ${!f.authorized && html`<span class="hint">Tick the authorisation box to continue.</span>`}</div></div>`;
}

function RecordStream({ campaigns, user, sys, onDone }) {
  const us = (user && user.settings) || {};
  const [f, setF] = useState({ url: "", minutes: 30, mode: "browser", campaign_id: "", auto_clips: us.auto_clips ?? 5, authorized: false });
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState(null);
  const up = k => e => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const load = async () => { try { setSt(await api("/recorder/status")); } catch (e) { /* server restarting */ } };
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, []);
  useEffect(() => {
    if (f.mode !== "browser" || !/^https?:\/\//.test(f.url)) { setCheck(null); return; }
    const t = setTimeout(async () => { try { setCheck(await api("/recorder/check", { method: "POST", body: { url: f.url } })); } catch (e) { setCheck(null); } }, 600);
    return () => clearTimeout(t);
  }, [f.url, f.mode]);
  if (!sys || !sys.recorder || !sys.recorder.available) return null;
  const rec = st && ["starting", "recording", "stopping"].includes(st.status);
  const start = async () => {
    setBusy(true);
    try { setSt(await api("/recorder/start", { method: "POST", body: { ...f, minutes: +f.minutes, auto_clips: +f.auto_clips || 0, campaign_id: f.campaign_id ? +f.campaign_id : null } })); toast("Recording started"); }
    catch (e) { fail(e); } finally { setBusy(false); load(); }
  };
  const stop = async () => { setBusy(true); try { await api("/recorder/stop", { method: "POST" }); toast("Recording saved. It will appear below and start analysing."); setTimeout(onDone, 4000); } catch (e) { fail(e); } finally { setBusy(false); load(); } };
  const left = st && st.status === "recording" && st.minutes ? Math.max(0, st.minutes * 60 - (st.elapsed || 0)) : null;
  return html`<div class="card" style="margin-bottom:14px"><div class="row" style="margin-bottom:12px"><h2 class="grow">Record a stream</h2>
      ${rec ? html`<span class="pill" style="background:#d33;color:#fff">● ${st.status === "recording" ? "Recording " + clock(st.elapsed || 0) : st.status}</span>` : st && st.last_file ? html`<span class="small muted">Last recording saved</span>` : ""}</div>
    ${rec ? html`<p class="small">${st.mode === "browser" ? "OBS is playing and recording the stream in the background. Nothing on your screen is captured." : "OBS is recording the Edge window. Keep the stream full screen in Edge."}
        ${left != null ? html` Stops by itself in ${clock(left)}.` : ""} When it stops, the recording is added below${st.status === "recording" ? " and analysed automatically" : ""}.</p>
        <div class="row"><button class="btn danger" disabled=${busy || st.status !== "recording"} onClick=${stop}>Stop and save now</button></div>`
    : html`<div class="fields">
      <${Field} label="What to record"><select value=${f.mode} onChange=${up("mode")}><option value="browser">A link (OBS plays it, recommended)</option><option value="edge">My Edge window (stream open full screen)</option></select><//>
      ${f.mode === "browser" && html`<${Field} wide label="Stream or video link" hint=${check && check.embeddable === false ? "This video can't be played outside YouTube. Open it in Edge, press F, and choose “My Edge window”." : check && check.youtube_id ? "YouTube link: OBS will play it full frame without the player controls. Add &t=1h20m to start a replay at a point." : "A YouTube live or replay link works best."}><input placeholder="https://www.youtube.com/watch?v=…" value=${f.url} onInput=${up("url")} /><//>`}
      <${Field} label="Minutes to record"><input type="number" min="1" max="240" value=${f.minutes} onInput=${up("minutes")} /><//>
      <${Field} label="Campaign"><select value=${f.campaign_id} onChange=${up("campaign_id")}><option value="">None</option>${campaigns.map(c => html`<option value=${c.id}>${c.name}</option>`)}</select><//>
      <${Field} label="Clips to make when done"><input type="number" min="0" max="30" value=${f.auto_clips} onInput=${up("auto_clips")} /><//>
      <label class="f inline wide"><input type="checkbox" checked=${f.authorized} onChange=${up("authorized")} /><span>I'm allowed to clip this stream (it belongs to the campaign's streamer).</span></label></div>
      <div class="row" style="margin-top:14px"><button class="btn primary" disabled=${busy || !f.authorized || (f.mode === "browser" && !f.url) || (check && check.embeddable === false)} onClick=${start}>${busy ? "Starting OBS…" : "Start recording"}</button>
        ${st && st.status === "error" && html`<span class="hint" style="color:var(--bad,#e66)">${st.error}</span>`}</div>`}</div>`;
}

export function Library({ user }) {
  const { data, error, loading, reload } = useLoad(() => Promise.all([api("/videos"), api("/campaigns"), api("/system")]), [], 4000);
  if (loading && !data) return html`<${Loading} />`;
  if (error && !data) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const [videos, campaigns, sys] = data;
  return html`<div class="head"><div class="grow"><h1>Recordings</h1><p>Upload a stream, run the analysis, then cut the moments it ranks highest.</p></div></div>
    <${RecordStream} campaigns=${campaigns} user=${user} sys=${sys} onDone=${reload} />
    <${AddRecording} campaigns=${campaigns} user=${user} onDone=${v => v.status === "uploaded" || v.status === "analyzing" ? nav("/video/" + v.id) : reload()} />
    <div class="card flush">${videos.length ? videos.map(v => html`<div class="item click" onClick=${() => nav("/video/" + v.id)}>
        <div class="grow"><div class="row"><b class="trunc">${v.title}</b><${Status} s=${v.status} /></div>
          <div class="small muted">${v.duration ? clock(v.duration) : "—"} · ${v.width ? `${v.width}×${v.height}` : "—"} · ${bytes(v.size)}${v.streamer ? " · " + v.streamer : ""} · ${ago(v.created_at)}</div>
          ${v.job && ["queued", "running", "failed"].includes(v.job.status) && html`<div style="margin-top:6px;max-width:420px"><${JobLine} job=${v.job} onChange=${reload} /></div>`}</div>
        <div class="small muted num" style="text-align:right;white-space:nowrap">${v.highlights} highlights<br />${v.clips} clips</div></div>`)
      : html`<div class="empty"><b>No recordings yet</b>Upload your first stream above. A 2-hour 1080p VOD is typically 3-6 GB.</div>`}</div>`;
}

// ----------------------------------------------------------------------------- video detail
function Timeline({ vid, duration, highlights, playhead, onSeek }) {
  const { data } = useLoad(() => api(`/videos/${vid}/timeline`).catch(() => null), [vid, highlights.length]);
  if (!data || !data.excite.length) return null;
  const n = data.excite.length;
  const path = arr => arr.map((v, i) => `${i ? "L" : "M"}${(i / (n - 1 || 1) * 1000).toFixed(1)},${(70 - v * 64).toFixed(1)}`).join("");
  const click = e => { const r = e.currentTarget.getBoundingClientRect(); onSeek((e.clientX - r.left) / r.width * duration); };
  return html`<div><div class="tl" onClick=${click} title="Click to jump">
      <svg viewBox="0 0 1000 74" preserveAspectRatio="none"><path d=${path(data.excite) + "L1000,74L0,74Z"} fill="rgba(255,106,43,.18)" stroke="#ff6a2b" stroke-width="1" vector-effect="non-scaling-stroke" />
        ${data.chat.length > 0 && html`<path d=${path(data.chat)} fill="none" stroke="#8ea0ff" stroke-width="1" vector-effect="non-scaling-stroke" />`}</svg>
      ${(data.offstream || []).map(o => html`<div class="off" style=${`left:${o.s / duration * 100}%;width:${Math.max(0.3, (o.e - o.s) / duration * 100)}%`} title=${o.reason === "ad_or_break" ? "Ad break or off-stream screen: never used in clips" : "Player controls on screen: never used in clips"}></div>`)}
      ${highlights.map(h => html`<div class="rng" style=${`left:${h.start / duration * 100}%;width:${Math.max(0.3, (h.end - h.start) / duration * 100)}%`} title=${`#${h.rank} ${h.title}`}></div>`)}
      <div class="ph" style=${`left:${playhead / duration * 100}%`}></div></div>
    <div class="legend" style="margin-top:6px"><span><i style="background:#ff6a2b"></i>Audio excitement vs. stream baseline</span>${data.chat.length > 0 && html`<span><i style="background:#8ea0ff"></i>Chat activity</span>`}<span><i style="background:rgba(255,106,43,.3);border:1px solid #ff6a2b"></i>Suggested highlights</span>${(data.offstream || []).length > 0 && html`<span><i style="background:rgba(160,165,180,.45)"></i>Ads / player controls (skipped)</span>`}</div></div>`;
}

const FLAG_NAMES = { possible_spoiler: "Possible story spoiler", graphic_violence: "Possible graphic violence", strong_language: "Strong language", possible_cinematic: "Possible cutscene", off_stream: "Ad break or off-stream screen", player_controls: "Player controls on screen" };
export const Flags = ({ flags }) => (flags || []).map(f => html`<div class="flag"><b>${FLAG_NAMES[f.code] || f.code}.</b> ${f.detail}</div>`);

function HighlightCard({ h, cats, onPreview, onClip, onDismiss, busy }) {
  const [open, setOpen] = useState(false);
  const s = h.signals || {};
  return html`<div class="hl">
    <div class="rank"><div class="score num">${Math.round(h.engagement_score)}</div><small>score · #${h.rank}</small></div>
    <div><div class="row wrap"><b class="grow" style="min-width:180px">${h.title}</b>
        <span class="small num muted">${clock(h.start)} – ${clock(h.end)} · ${Math.round(h.end - h.start)}s</span></div>
      <div class="meta"><span class="pill acc">${cats[h.category] || "Notable moment"}</span>
        <span class="pill">Viral potential ${Math.round(h.viral_potential)}</span><span class="pill">Confidence ${Math.round(h.confidence * 100)}%</span>
        ${s.laugh > 0.35 && html`<span class="pill">Laughter</span>`}${s.scream > 0.35 && html`<span class="pill">Shout</span>`}${s.chat > 0.4 && html`<span class="pill est">Chat spike</span>`}
        <span class="pill">${h.scored_by === "heuristic" ? "Signal scoring" : "LLM + signals"}</span></div>
      <p class="muted small">${h.description}</p>
      <p class="small" style="margin-top:4px">${h.reason}</p>
      ${s.hook_text && html`<p class="small" style="margin-top:4px"><span class="dim">Hook text:</span> ${s.hook_text}</p>`}
      <${Flags} flags=${h.flags} />
      ${open && html`<blockquote>${h.transcript || "No speech detected in this window."}</blockquote>
        <p class="small muted" style="margin-top:6px">${h.hook}</p>`}
      <div class="row wrap" style="margin-top:10px">
        <button class="btn sm" onClick=${() => onPreview(h)}><${Icon} n="play" />Preview</button>
        <button class="btn sm primary" disabled=${busy} onClick=${() => onClip(h)}>Make clip</button>
        <button class="btn sm ghost" onClick=${() => setOpen(!open)}>${open ? "Hide" : "Show"} transcript</button>
        <button class="btn sm ghost right" onClick=${() => onDismiss(h)}>Dismiss</button></div></div></div>`;
}

export function VideoDetail({ id, sys, user }) {
  const { data, error, loading, reload } = useLoad(() => Promise.all([api(`/videos/${id}`), api(`/videos/${id}/highlights`), api("/campaigns")]), [id], 3000);
  const us = user.settings || {};
  const [opt, setOpt] = useState({ min_duration: us.min_duration || 15, max_duration: us.max_duration || 60, max_highlights: us.max_highlights || 40, use_llm: us.use_llm !== false, voice_isolation: !!us.voice_isolation });
  const [busy, setBusy] = useState(false);
  const [t, setT] = useState(0);
  const [mark, setMark] = useState({ a: null, b: null });
  const [topN, setTopN] = useState(5);
  const vref = useRef(null), stopAt = useRef(null);
  useEffect(() => { if (data && data[0].analysis_settings && data[0].analysis_settings.min_duration) setOpt(o => ({ ...o, ...Object.fromEntries(Object.entries(data[0].analysis_settings).filter(([, v]) => v != null)) })); }, [data && data[0].status]);
  if (loading && !data) return html`<${Loading} />`;
  if (error && !data) return html`<${ErrorBox} error=${error} retry=${reload} />`;
  const [v, hs, campaigns] = data;
  const shown = hs.filter(h => h.status === "suggested");
  const cats = sys ? sys.profile.categories : {};
  const working = v.job && ["queued", "running"].includes(v.job.status);
  const seek = (s, e) => { const el = vref.current; if (!el) return; el.currentTime = s; stopAt.current = e ?? null; if (e != null) el.play().catch(() => {}); };
  const onTime = () => { const el = vref.current; setT(el.currentTime); if (stopAt.current != null && el.currentTime >= stopAt.current) { el.pause(); stopAt.current = null; } };
  const run = async () => { setBusy(true); try { await api(`/videos/${id}/analyze`, { method: "POST", body: opt }); toast("Analysis queued"); reload(); } catch (e) { fail(e); } finally { setBusy(false); } };
  const clip = async h => { setBusy(true); try { const c = await api(`/highlights/${h.id}/clip`, { method: "POST", body: { render: true } }); nav("/studio/" + c.id); } catch (e) { fail(e); setBusy(false); } };
  const clipTop = async n => { setBusy(true); try { const r = await api(`/videos/${id}/clips/top`, { method: "POST", body: { count: n } }); toast(`${r.created} clips created and rendering`); nav("/clips"); } catch (e) { fail(e); setBusy(false); } };
  const dismiss = async h => { try { await api(`/highlights/${h.id}`, { method: "PATCH", body: { status: "dismissed" } }); reload(); } catch (e) { fail(e); } };
  const manual = async () => { try { const c = await api(`/videos/${id}/clips`, { method: "POST", body: { start: mark.a, end: mark.b } }); nav("/studio/" + c.id); } catch (e) { fail(e); } };
  const chat = async e => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
    try { const r = await api(`/videos/${id}/chat`, { method: "POST", body: fd, raw: true }); toast(`${r.messages} chat messages loaded. Run the analysis again to use them.`); reload(); } catch (x) { fail(x); } e.target.value = ""; };
  const patch = async body => { try { await api(`/videos/${id}`, { method: "PATCH", body }); reload(); } catch (e) { fail(e); } };
  const del = async () => { if (!confirm(`Delete “${v.title}”, its clips and all cached analysis? This can't be undone.`)) return; try { await api(`/videos/${id}`, { method: "DELETE" }); nav("/library"); } catch (e) { fail(e); } };
  const num = k => e => setOpt({ ...opt, [k]: +e.target.value });
  const tp = sys && sys.transcription.providers.find(p => p.available);
  return html`
  <div class="head"><a class="btn ghost sm" href="#/library" aria-label="Back to recordings"><${Icon} n="back" /></a>
    <div class="grow"><h1 class="trunc">${v.title}</h1>
      <p>${clock(v.duration)} · ${v.width}×${v.height} · ${v.vcodec}${v.acodec ? "/" + v.acodec : " (no audio)"} · ${bytes(v.size)} <${Status} s=${v.status} /></p></div>
    <button class="btn danger sm" onClick=${del}>Delete</button></div>

  <div class="grid g23" style="margin-bottom:14px">
    <div class="card">
      ${v.has_proxy ? html`<video ref=${vref} class="player" src=${`/api/videos/${id}/stream`} controls playsinline preload="metadata" onTimeUpdate=${onTime}></video>
        <div style="margin-top:10px"><${Timeline} vid=${id} duration=${v.duration} highlights=${shown} playhead=${t} onSeek=${s => seek(s)} /></div>
        <div class="row wrap" style="margin-top:10px"><span class="small muted">Cut your own:</span>
          <button class="btn sm" onClick=${() => setMark({ ...mark, a: +t.toFixed(1) })}>Set start ${mark.a != null ? clock(mark.a) : ""}</button>
          <button class="btn sm" onClick=${() => setMark({ ...mark, b: +t.toFixed(1) })}>Set end ${mark.b != null ? clock(mark.b) : ""}</button>
          <button class="btn sm primary" disabled=${mark.a == null || mark.b == null || mark.b - mark.a < 1} onClick=${manual}>Make clip${mark.a != null && mark.b != null && mark.b > mark.a ? ` (${Math.round(mark.b - mark.a)}s)` : ""}</button></div>`
        : html`<div class="empty"><b>Preview not built yet</b>The browser preview and the highlight list are created when you run the analysis.</div>`}
    </div>
    <div class="card col"><h2>Analysis</h2>
      ${working && html`<${JobLine} job=${v.job} onChange=${reload} />`}
      ${v.job && v.job.status === "failed" && html`<${JobLine} job=${v.job} onChange=${reload} />`}
      ${v.error && v.status !== "failed" && html`<div class="note warn">${v.error}</div>`}
      <div class="fields">
        <${Field} label="Shortest clip (s)"><input type="number" min="5" max="180" value=${opt.min_duration} onInput=${num("min_duration")} /><//>
        <${Field} label="Longest clip (s)"><input type="number" min="5" max="180" value=${opt.max_duration} onInput=${num("max_duration")} /><//>
        <${Field} label="Max highlights"><input type="number" min="1" max="100" value=${opt.max_highlights} onInput=${num("max_highlights")} /><//>
        <label class="f inline" style="align-self:end;padding-bottom:6px"><input type="checkbox" checked=${opt.use_llm && sys && sys.semantic.llm_configured} disabled=${!sys || !sys.semantic.llm_configured} onChange=${e => setOpt({ ...opt, use_llm: e.target.checked })} /><span>LLM re-rank</span></label>
        <label class="f inline" style="align-self:end;padding-bottom:6px" title="Separates the streamer's voice from game sound before transcription. Clearer captions on loud gameplay; adds roughly the recording's length to the analysis time."><input type="checkbox" checked=${opt.voice_isolation} onChange=${e => setOpt({ ...opt, voice_isolation: e.target.checked })} /><span>Clean voice (slower)</span></label>
      </div>
      <button class="btn primary" disabled=${busy || working || v.status === "uploading"} onClick=${run}>${v.status === "ready" ? "Re-run analysis" : "Analyse recording"}</button>
      <div class="hint">Speech: ${v.transcribe_provider || (tp ? tp.name + " (local, free)" : "no provider available")} · Ranking: ${v.semantic_provider || (sys && sys.semantic.llm_configured ? "LLM + signals" : "signal scoring (no LLM key set)")}
        ${v.cost_estimate ? ` · Est. cost ${money(v.cost_estimate.usd)}` : ""}${v.cost && v.cost.total_usd != null ? ` · Last run cost ${money(v.cost.total_usd)}` : ""}.
        Re-runs reuse the cached transcript and audio analysis, so changing durations is fast and free.</div>
      <hr style="border:0;border-top:1px solid var(--line);width:100%;margin:4px 0" />
      <${Field} label="Streamer"><input value=${v.streamer} onChange=${e => patch({ streamer: e.target.value })} /><//>
      <${Field} label="Campaign"><select value=${v.campaign_id || ""} onChange=${e => patch(e.target.value ? { campaign_id: +e.target.value } : { clear_campaign: true })}><option value="">None</option>${campaigns.map(c => html`<option value=${c.id}>${c.name}</option>`)}</select><//>
      <${Field} label=${"Chat log (optional)" + (v.has_chat ? " — loaded" : "")} hint="Twitch chat JSON export, CSV, or lines like [1:02:33] user: message. Adds chat-spike detection."><input type="file" accept=".txt,.json,.csv,.log" onChange=${chat} /><//>
      <div class="hint">Facecam: ${v.facecam ? `${v.facecam_source === "auto" ? "detected automatically" : "set by you"} — adjust it in the clip editor.` : "none detected. You can draw it in the clip editor."}</div>
    </div>
  </div>

  <div class="card flush">
    <div class="row wrap" style="padding:14px 16px;border-bottom:1px solid var(--line)"><h2 class="grow">Ranked highlights <span class="muted" style="font-weight:400">${shown.length}</span></h2>
      ${shown.length > 0 && html`<span class="small muted">Make the top</span><select style="width:auto" value=${topN} onChange=${e => setTopN(+e.target.value)} aria-label="How many clips">${[3, 5, 10, 15, 20].filter(n => n <= Math.max(3, shown.length)).map(n => html`<option value=${n}>${Math.min(n, shown.length)}</option>`)}</select>
        <button class="btn sm primary" disabled=${busy} onClick=${() => clipTop(topN)}>Make clips</button>`}</div>
    ${shown.length ? shown.map(h => html`<${HighlightCard} key=${h.id} h=${h} cats=${cats} busy=${busy} onPreview=${x => { seek(x.start, x.end); vref.current && vref.current.scrollIntoView({ block: "nearest", behavior: "smooth" }); }} onClip=${clip} onDismiss=${dismiss} />`)
      : html`<div class="empty">${v.status === "ready" ? html`<b>No strong moments found</b>The stream had no stretch that stood out on audio, speech or chat. Try a lower minimum duration, add a chat log, or cut manually with the player above.`
        : working ? html`<b>Analysis in progress</b>Highlights appear here when it finishes. You can leave this page.` : html`<b>Not analysed yet</b>Run the analysis to get a ranked list of moments.`}</div>`}
  </div>`;
}
