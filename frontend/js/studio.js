import { html, useState, useEffect, useRef, api, fail, toast, Loading, ErrorBox, Field, Status, JobLine, Icon, clock, bytes, nav, Modal } from "./core.js";
import { Flags } from "./library.js";

const hexA = (hex, a) => { const h = hex.replace("#", ""); return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`; };
const loadedFonts = {};
function ensureFont(f) {
  if (!f || loadedFonts[f.label]) return;
  loadedFonts[f.label] = true;
  new FontFace("cf-" + f.label, `url(/fonts/${encodeURIComponent(f.file)})`).load().then(x => document.fonts.add(x)).catch(() => {});
}

/* Draws the same panels + caption cues the renderer will burn in. Canvas is 540x960 (half of 1080x1920). */
function drawFrame(ctx, video, clip, small) {
  const S = 0.5, W = 540, H = 960;
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
  const lay = clip.panels;
  if (!lay || !video.videoWidth) return;
  const k = video.videoWidth / lay.source.w, ky = video.videoHeight / lay.source.h;
  if (lay.blur_bg) {      // cheap blur that also works on iOS Safari: downscale hard, then upscale
    const sc = Math.max(27 / video.videoWidth, 48 / video.videoHeight), sw = 27 / sc, sh = 48 / sc;
    small.drawImage(video, (video.videoWidth - sw) / 2, (video.videoHeight - sh) / 2, sw, sh, 0, 0, 27, 48);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    ctx.drawImage(small.canvas, 0, 0, 27, 48, 0, 0, W, H);
    ctx.fillStyle = "rgba(0,0,0,.45)"; ctx.fillRect(0, 0, W, H);
  }
  for (const p of lay.panels) ctx.drawImage(video, p.src.x * k, p.src.y * ky, p.src.w * k, p.src.h * ky, p.dst.x * S, p.dst.y * S, p.dst.w * S, p.dst.h * S);
  const cp = clip.caption_resolved, t = video.currentTime - clip.start;
  if (clip.watermark_text) {
    ctx.font = `600 ${22}px "cf-${cp.font}", Inter, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(255,255,255,.65)"; ctx.strokeStyle = "rgba(0,0,0,.55)"; ctx.lineWidth = 2;
    const y = clip.watermark_pos === "bottom" ? H - 56 : clip.watermark_pos === "middle" ? H / 2 : 56;
    ctx.strokeText(clip.watermark_text, W / 2, y); ctx.fillText(clip.watermark_text, W / 2, y);
  }
  if (cp.hook_text && t >= 0 && t < (cp.hook_seconds || 3)) {
    const hs = 32; ctx.font = `700 ${hs}px "cf-${cp.font}", Inter, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    let ls = [cp.hook_text.slice(0, 90)];
    if (ls[0].length > 24) { const x = ls[0], mid = x.length / 2; let cut = -1, bd = 1e9; for (let i = 0; i < x.length; i++) if (x[i] === " " && Math.abs(i - mid) < bd) { bd = Math.abs(i - mid); cut = i; } if (cut > 0) ls = [x.slice(0, cut), x.slice(cut + 1)]; }
    const hy = H * (cp.hook_y || 0.16), lh = hs * 1.25, bw = Math.min(W - 20, Math.max(...ls.map(l => ctx.measureText(l).width)) + 28);
    ctx.fillStyle = "#fff"; ctx.fillRect((W - bw) / 2, hy - lh * ls.length / 2 - 8, bw, lh * ls.length + 16);
    ctx.fillStyle = "#000"; ls.forEach((l, i) => ctx.fillText(l, W / 2, hy + (i - (ls.length - 1) / 2) * lh, W - 40));
  }
  const cdur = clip.end - clip.start;
  if (cp.cta_text && t >= cdur - (cp.cta_seconds || 3) && t <= cdur + 0.2) {
    const hs = 28; ctx.font = `700 ${hs}px "cf-${cp.font}", Inter, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    let ls = [cp.cta_text.slice(0, 80)];
    if (ls[0].length > 26) { const x = ls[0], mid = x.length / 2; let cut = -1, bd = 1e9; for (let i = 0; i < x.length; i++) if (x[i] === " " && Math.abs(i - mid) < bd) { bd = Math.abs(i - mid); cut = i; } if (cut > 0) ls = [x.slice(0, cut), x.slice(cut + 1)]; }
    const hy = H * (cp.cta_y || 0.84), lh = hs * 1.25, bw = Math.min(W - 20, Math.max(...ls.map(l => ctx.measureText(l).width)) + 26);
    ctx.fillStyle = "#FFE600"; ctx.fillRect((W - bw) / 2, hy - lh * ls.length / 2 - 7, bw, lh * ls.length + 14);
    ctx.fillStyle = "#000"; ls.forEach((l, i) => ctx.fillText(l, W / 2, hy + (i - (ls.length - 1) / 2) * lh, W - 40));
  }
  if (!cp.enabled) return;
  const cue = clip.cues.find(c => t >= c.s && t < c.e);
  if (!cue) return;
  let size = cp.size * S;
  const font = z => `700 ${z}px "cf-${cp.font}", Inter, sans-serif`;
  ctx.font = font(size); ctx.textBaseline = "middle"; ctx.textAlign = "left"; ctx.lineJoin = "round";
  const gap = size * 0.28;
  let widths = cue.words.map(w => ctx.measureText(w.w).width), total = widths.reduce((a, b) => a + b, 0) + gap * (cue.words.length - 1);
  if (total > W - 40) { size *= (W - 40) / total; ctx.font = font(size); widths = cue.words.map(w => ctx.measureText(w.w).width); total = widths.reduce((a, b) => a + b, 0) + size * 0.28 * (cue.words.length - 1); }
  const y = H * cp.y; let x = (W - total) / 2;
  if (cp.bg) { ctx.fillStyle = "rgba(0,0,0,.7)"; ctx.fillRect(x - 10, y - size * 0.7, total + 20, size * 1.4); }
  let active = cue.words.findIndex((w, i) => t < (cue.words[i + 1] ? cue.words[i + 1].s : cue.e));
  if (active < 0) active = cue.words.length - 1;
  cue.words.forEach((w, i) => {
    let col = cp.color;
    if (cp.style === "karaoke") col = i < active || (i === active && t >= w.s) ? cp.accent : cp.color;
    else if (cp.style === "clean" || cp.style === "oneword") col = w.kw ? cp.keyword_color : cp.color;
    else col = i === active ? cp.accent : (w.kw ? cp.keyword_color : cp.color);
    if (!cp.bg && cp.outline > 0) { ctx.strokeStyle = cp.outline_color; ctx.lineWidth = cp.outline * S * 2; ctx.strokeText(w.w, x, y); }
    ctx.fillStyle = col; ctx.fillText(w.w, x, y);
    x += widths[i] + size * 0.28;
  });
}

function Preview({ clip, fonts, vref, onTime }) {
  const cref = useRef(null), small = useRef(null), clipRef = useRef(clip);
  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(clip.start);
  clipRef.current = clip;
  useEffect(() => { ensureFont(fonts.find(f => f.label === clip.caption_resolved.font)); }, [clip.caption_resolved.font]);
  useEffect(() => {
    const v = vref.current, ctx = cref.current.getContext("2d");
    small.current = Object.assign(document.createElement("canvas"), { width: 27, height: 48 }).getContext("2d");
    let raf, last = -1;
    const loop = () => {
      const c = clipRef.current;
      if (!v.paused && v.currentTime >= c.end) { v.pause(); v.currentTime = c.start; }
      drawFrame(ctx, v, c, small.current);
      if (Math.abs(v.currentTime - last) > 0.05) { last = v.currentTime; setT(last); onTime && onTime(last); }
      raf = requestAnimationFrame(loop);
    };
    const play = () => setPlaying(true), pause = () => setPlaying(false);
    v.addEventListener("play", play); v.addEventListener("pause", pause);
    v.addEventListener("loadedmetadata", () => { v.currentTime = clipRef.current.start; }, { once: true });
    raf = requestAnimationFrame(loop);
    return () => { cancelAnimationFrame(raf); v.removeEventListener("play", play); v.removeEventListener("pause", pause); v.pause(); };
  }, []);
  const toggle = () => { const v = vref.current; if (v.paused) { if (v.currentTime < clip.start || v.currentTime >= clip.end - 0.05) v.currentTime = clip.start; v.play().catch(fail); } else v.pause(); };
  return html`<div>
    <canvas ref=${cref} class="pv" width="540" height="960" onClick=${toggle} aria-label="Clip preview"></canvas>
    <video ref=${vref} class="srcvid" src=${`/api/videos/${clip.video_id}/stream`} playsinline preload="auto"></video>
    <div class="transport"><button class="btn sm" onClick=${toggle} aria-label=${playing ? "Pause" : "Play"}><${Icon} n=${playing ? "pause" : "play"} /></button>
      <input type="range" min=${clip.start} max=${clip.end} step="0.05" value=${Math.min(Math.max(t, clip.start), clip.end)} onInput=${e => { vref.current.currentTime = +e.target.value; }} aria-label="Scrub" />
      <span class="small num muted" style="white-space:nowrap">${clock(Math.max(0, t - clip.start))} / ${clock(clip.end - clip.start)}</span></div>
    <p class="hint center" style="margin-top:4px">Live preview of crop and captions. The rendered file is the source of truth.</p></div>`;
}

/* Source-frame overview: drag to draw the facecam rectangle. */
function Overview({ vref, rect, onRect, tick }) {
  const ref = useRef(null), drag = useRef(null);
  useEffect(() => {
    const c = ref.current, v = vref.current; if (!c || !v || !v.videoWidth) return;
    c.width = 480; c.height = Math.round(480 * v.videoHeight / v.videoWidth);
    const ctx = c.getContext("2d"); ctx.drawImage(v, 0, 0, c.width, c.height);
    const r = drag.current && drag.current.r || rect;
    if (r) { ctx.fillStyle = "rgba(255,106,43,.22)"; ctx.strokeStyle = "#ff6a2b"; ctx.lineWidth = 2; ctx.fillRect(r.x * c.width, r.y * c.height, r.w * c.width, r.h * c.height); ctx.strokeRect(r.x * c.width, r.y * c.height, r.w * c.width, r.h * c.height); }
  });
  const pos = e => { const b = ref.current.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)), Math.min(1, Math.max(0, (e.clientY - b.top) / b.height))]; };
  const [, force] = useState(0);
  const down = e => { e.currentTarget.setPointerCapture(e.pointerId); const [x, y] = pos(e); drag.current = { x, y, r: null }; };
  const move = e => { if (!drag.current) return; const [x, y] = pos(e), d = drag.current; d.r = { x: Math.min(x, d.x), y: Math.min(y, d.y), w: Math.abs(x - d.x), h: Math.abs(y - d.y) }; force(n => n + 1); };
  const upp = () => { const d = drag.current; drag.current = null; if (d && d.r && d.r.w > 0.03 && d.r.h > 0.03) onRect(d.r); else force(n => n + 1); };
  return html`<canvas ref=${ref} class="overview" data-tick=${tick} onPointerDown=${down} onPointerMove=${move} onPointerUp=${upp} aria-label="Source frame. Drag to mark the facecam."></canvas>`;
}

function SubmissionForm({ clip, sys, campaigns, onClose }) {
  const camp = campaigns.find(c => c.id === clip.campaign_id);
  const plats = camp && camp.approved_platforms && camp.approved_platforms.length ? camp.approved_platforms : sys.platforms;
  const [f, setF] = useState({ platform: plats[0], url: "", views: 0, approved_views: 0 });
  const save = async () => { try { await api("/submissions", { method: "POST", body: { ...f, clip_id: clip.id, views: +f.views, approved_views: +f.approved_views } }); toast("Submission recorded"); onClose(true); } catch (e) { fail(e); } };
  return html`<${Modal} title="Record a submission" onClose=${() => onClose(false)}>
    <p class="muted small" style="margin-bottom:12px">ClipForge doesn't post or submit for you. Post the clip, submit the link on the campaign page, then record it here so views and earnings can be tracked.</p>
    ${camp && camp.submission_instructions && html`<div class="note" style="margin-bottom:12px"><b>Campaign submission steps:</b> ${camp.submission_instructions}</div>`}
    <div class="fields">
      <${Field} label="Platform"><select value=${f.platform} onChange=${e => setF({ ...f, platform: e.target.value })}>${plats.map(p => html`<option value=${p}>${p.replace(/_/g, " ")}</option>`)}</select><//>
      <${Field} label="Link to the posted clip"><input value=${f.url} onInput=${e => setF({ ...f, url: e.target.value })} placeholder="https://…" /><//>
      <${Field} label="Views so far"><input type="number" min="0" value=${f.views} onInput=${e => setF({ ...f, views: e.target.value })} /><//>
      <${Field} label="Approved views so far" hint="As shown by the campaign, not your own count"><input type="number" min="0" value=${f.approved_views} onInput=${e => setF({ ...f, approved_views: e.target.value })} /><//>
    </div>
    <div class="row" style="margin-top:14px">${camp && camp.whop_url && html`<a class="btn" href=${camp.whop_url} target="_blank" rel="noopener">Open campaign page <${Icon} n="ext" /></a>`}
      <button class="btn primary right" onClick=${save}>Save submission</button></div><//>`;
}

const TABS = [["cut", "Cut"], ["layout", "Layout"], ["captions", "Captions"], ["details", "Details"], ["assist", "Assistant"], ["check", "Compliance"], ["post", "Post"]];

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast("Copied"); }
  catch (e) { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); toast("Copied"); } catch (x) { toast("Select the text and copy it manually", true); } a.remove(); }
}

/* Everything needed to post an approved clip: ready text per platform, the file, and where to record the link. */
function PostKit({ clip, onRecord }) {
  const [kit, setKit] = useState(null);
  useEffect(() => { api(`/clips/${clip.id}/posting`).then(setKit).catch(fail); }, [clip.id, clip.title, clip.description, (clip.hashtags || []).join(" "), clip.campaign_id, clip.status, clip.submissions]);
  if (!kit) return html`<${Loading} />`;
  return html`<div class="col">
    ${!kit.approved && html`<div class="note warn">Approve the clip first. The text below is ready, but only approved clips can be recorded as posted.</div>`}
    <div class="row wrap">${clip.has_output && html`<a class="btn primary" href=${`/api/clips/${clip.id}/file?download=1`}>1. Download MP4</a>`}
      ${kit.campaign_url && html`<a class="btn" href=${kit.campaign_url} target="_blank" rel="noopener">Open campaign page <${Icon} n="ext" /></a>`}</div>
    <h3>2. Post it</h3>
    ${kit.platforms.map(p => html`<div class="card" style="background:var(--bg2)"><div class="row" style="margin-bottom:8px"><b class="grow">${p.name}</b>${p.posted && html`<span class="pill ok">Recorded</span>`}
        ${p.upload_url && html`<a class="btn sm" href=${p.upload_url} target="_blank" rel="noopener">Open upload <${Icon} n="ext" /></a>`}</div>
      ${p.fields.map(f => html`<div style="margin-bottom:8px"><div class="row"><span class="tiny dim grow">${f.label} \u00b7 ${f.text.length} characters</span><button class="btn sm" onClick=${() => copyText(f.text)}>Copy</button></div>
        <div class="small" style="margin-top:3px;word-break:break-word">${f.text}</div></div>`)}</div>`)}
    <h3>3. Record the link here${kit.campaign_name ? ", and submit it on the campaign page" : ""}</h3>
    ${kit.rules.length > 0 && html`<div class="note">${kit.rules.map(r => html`<div>\u2022 ${r}</div>`)}</div>`}
    <button class="btn" disabled=${!kit.approved} onClick=${onRecord}>Record a posted link${clip.submissions ? ` (${clip.submissions} recorded)` : ""}</button>
    <p class="hint">ClipForge does not post for you. TikTok, Instagram and YouTube only allow automatic posting from apps they have reviewed and approved.</p></div>`;
}

export function Studio({ id, sys }) {
  const [clip, setClip] = useState(null);
  const [err, setErr] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [tab, setTab] = useState("cut");
  const [view, setView] = useState("preview");
  const [saving, setSaving] = useState(false);
  const [sug, setSug] = useState(null);
  const [subOpen, setSubOpen] = useState(false);
  const [now, setNow] = useState(0);
  const vref = useRef(null), timer = useRef(null), pending = useRef({}), seq = useRef(0);

  const load = () => api(`/clips/${id}`).then(setClip).catch(setErr);
  useEffect(() => { load(); api("/campaigns").then(setCampaigns).catch(() => {}); return () => clearTimeout(timer.current); }, [id]);
  // poll while a render job is active
  useEffect(() => {
    if (!clip || !clip.job || !["queued", "running"].includes(clip.job.status)) return;
    const t = setInterval(() => api(`/clips/${id}`).then(c => { if (!Object.keys(pending.current).length) setClip(c); }).catch(() => {}), 1500);
    return () => clearInterval(t);
  }, [clip && clip.job && clip.job.status, clip && clip.job && clip.job.id]);

  if (err) return html`<${ErrorBox} error=${err} />`;
  if (!clip || !sys) return html`<${Loading} />`;

  const flush = async () => {
    const body = pending.current; pending.current = {};
    if (!Object.keys(body).length) return;
    const my = ++seq.current; setSaving(true);
    try { const c = await api(`/clips/${id}`, { method: "PATCH", body }); if (my === seq.current && !Object.keys(pending.current).length) setClip(c); }
    catch (e) { fail(e); load(); } finally { if (my === seq.current) setSaving(false); }
  };
  /* Optimistic edit + debounced save. The server answers with fresh panels/cues so the preview stays exact. */
  const edit = (patch, delay = 350) => {
    setClip(c => ({ ...c, ...patch }));
    pending.current = { ...pending.current, ...patch };
    clearTimeout(timer.current); timer.current = setTimeout(flush, delay);
  };
  const lp = clip.layout_params || {}, cp = clip.caption_resolved, v = clip.video;
  const setLP = (k, val) => edit({ layout_params: { ...lp, [k]: val } });
  const setCP = (k, val) => edit({ caption_params: { ...(clip.caption_params || {}), [k]: val } });
  const act = async (path, okMsg) => { clearTimeout(timer.current); await flush(); try { const c = await api(`/clips/${id}/${path}`, { method: "POST", body: {} }); if (c.id) setClip(c); okMsg && toast(okMsg); return c; } catch (e) { if (e.detail && e.detail.report) { setClip(c => ({ ...c, compliance: e.detail.report })); setTab("check"); } fail(e); } };
  const check = async () => { clearTimeout(timer.current); await flush(); try { const r = await api(`/clips/${id}/check`, { method: "POST" }); setClip(c => ({ ...c, compliance: r })); } catch (e) { fail(e); } };
  const suggest = async () => { clearTimeout(timer.current); await flush(); try { setSug(await api(`/clips/${id}/suggest`)); } catch (e) { fail(e); } };
  const del = async () => { if (!confirm("Delete this clip?")) return; try { await api(`/clips/${id}`, { method: "DELETE" }); nav("/clips"); } catch (e) { fail(e); } };
  const nudge = (k, d) => edit({ [k]: Math.max(0, +(clip[k] + d).toFixed(2)) });
  const rendering = clip.job && ["queued", "running"].includes(clip.job.status);
  const dur = clip.end - clip.start;
  const camp = campaigns.find(c => c.id === clip.campaign_id);
  const reviewFlags = (clip.flags || []).filter(f => f.severity === "review");
  const cam = lp.facecam || v.facecam;
  const usesCam = ["split", "facecam_focus"].includes(clip.layout);
  const words = clip.words || [];
  const editWord = (i, w) => { const n = words.slice(); if (w.trim() === "") n.splice(i, 1); else n[i] = { ...n[i], w }; edit({ words: n }, 700); };

  const panel = {
    cut: () => html`<div class="col">
      <div class="fields">
        ${["start", "end"].map(k => html`<${Field} label=${(k === "start" ? "Start" : "End") + " (seconds in recording)"}>
          <div class="row" style="gap:4px"><button class="btn sm" onClick=${() => nudge(k, -0.5)}>−0.5</button>
            <input type="number" step="0.1" min="0" value=${clip[k]} onChange=${e => edit({ [k]: +e.target.value })} />
            <button class="btn sm" onClick=${() => nudge(k, 0.5)}>+0.5</button></div><//>`)}
      </div>
      <div class="row wrap"><button class="btn sm" onClick=${() => edit({ start: +now.toFixed(2) })}>Start at playhead</button><button class="btn sm" onClick=${() => edit({ end: +now.toFixed(2) })}>End at playhead</button>
        <span class="small muted right num">${dur.toFixed(1)}s${camp ? ` · campaign allows ${camp.min_duration}–${camp.max_duration}s` : ""}</span></div>
      <div class="fields">
        <${Field} label=${`Volume ${clip.audio_gain_db > 0 ? "+" : ""}${clip.audio_gain_db} dB`}><input type="range" min="-12" max="12" step="1" value=${clip.audio_gain_db} onInput=${e => edit({ audio_gain_db: +e.target.value })} /><//>
        <label class="f inline" style="align-self:end;padding-bottom:4px"><input type="checkbox" checked=${clip.normalize_audio} onChange=${e => edit({ normalize_audio: e.target.checked })} /><span>Normalise loudness for social (−14 LUFS)</span></label>
        <${Field} label="Render quality"><select value=${clip.quality} onChange=${e => edit({ quality: e.target.value })}>${sys.quality.map(q => html`<option value=${q}>${q}</option>`)}</select><//>
      </div></div>`,
    layout: () => html`<div class="col">
      <${Field} label="Layout"><select value=${clip.layout} onChange=${e => edit({ layout: e.target.value })}>${Object.entries(sys.layouts).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
      <div class="fields">
        ${clip.layout !== "custom" && html`<${Field} label="Horizontal focus"><input type="range" min="0" max="1" step="0.01" value=${lp.focus_x ?? 0.5} onInput=${e => setLP("focus_x", +e.target.value)} /><//>`}
        ${["stacked", "fullscreen", "split", "facecam_focus"].includes(clip.layout) && html`<${Field} label="Vertical focus"><input type="range" min="0" max="1" step="0.01" value=${lp.focus_y ?? 0.5} onInput=${e => setLP("focus_y", +e.target.value)} /><//>`}
        ${clip.layout !== "custom" && html`<${Field} label=${`Zoom ${(lp.zoom ?? 1).toFixed(2)}×`}><input type="range" min="1" max="2.5" step="0.05" value=${lp.zoom ?? 1} onInput=${e => setLP("zoom", +e.target.value)} /><//>`}
        ${clip.layout === "blurfit" && html`<${Field} label="Vertical position"><input type="range" min="0" max="1" step="0.01" value=${lp.offset_y ?? 0.42} onInput=${e => setLP("offset_y", +e.target.value)} /><//>`}
        ${["split", "facecam_focus", "custom"].includes(clip.layout) && html`<${Field} label="Top panel height"><input type="range" min="0.15" max="0.8" step="0.01" value=${lp.ratio ?? (clip.layout === "facecam_focus" ? 0.55 : 0.34)} onInput=${e => setLP("ratio", +e.target.value)} /><//>`}
      </div>
      ${usesCam && html`<div class="col" style="gap:6px"><h3>Facecam area</h3>
        <p class="hint">${cam ? "Drag on the frame to redraw it." : "No facecam detected. Drag on the frame to mark where the webcam is."}</p>
        <${Overview} vref=${vref} rect=${cam} tick=${Math.floor(now)} onRect=${r => setLP("facecam", r)} />
        <div class="row wrap">${lp.facecam && html`<button class="btn sm" onClick=${async () => { try { await api(`/videos/${v.id}`, { method: "PATCH", body: { facecam: lp.facecam } }); toast("Saved as the facecam for every clip from this recording"); } catch (e) { fail(e); } }}>Use for all clips from this recording</button>
          <button class="btn sm ghost" onClick=${() => { const n = { ...lp }; delete n.facecam; edit({ layout_params: n }); }}>Reset</button>`}</div></div>`}
      ${clip.layout === "custom" && html`<div class="col" style="gap:6px"><h3>Top region</h3><${Overview} vref=${vref} rect=${lp.top || cam} tick=${Math.floor(now)} onRect=${r => setLP("top", r)} />
        <h3>Bottom region</h3><${Overview} vref=${vref} rect=${lp.bottom} tick=${Math.floor(now)} onRect=${r => setLP("bottom", r)} /><p class="hint">Drag on each frame to choose what fills that half.</p></div>`}
      <div class="fields"><${Field} label="Watermark / credit text"><input value=${clip.watermark_text} onInput=${e => edit({ watermark_text: e.target.value }, 600)} placeholder="e.g. @streamer on Kick" /><//>
        <${Field} label="Watermark position"><select value=${clip.watermark_pos} onChange=${e => edit({ watermark_pos: e.target.value })}><option value="top">Top</option><option value="middle">Middle</option><option value="bottom">Bottom</option></select><//></div></div>`,
    captions: () => html`<div class="col">
      <div class="fields"><${Field} wide label="Hook text (shown big at the top for the first seconds)" hint="One short line that makes someone stop scrolling. Describe what really happens in the clip.">
          <input maxlength="90" value=${cp.hook_text || ""} onInput=${e => setCP("hook_text", e.target.value)} placeholder="e.g. He did NOT expect the NPC to do this" /><//>
        <${Field} label=${`Hook stays for ${cp.hook_seconds || 3}s`}><input type="range" min="1" max="8" step="0.5" value=${cp.hook_seconds || 3} onInput=${e => setCP("hook_seconds", +e.target.value)} /><//>
        <${Field} label="Hook position"><input type="range" min="0.06" max="0.5" step="0.01" value=${cp.hook_y || 0.16} onInput=${e => setCP("hook_y", +e.target.value)} /><//>
        <${Field} wide label="End-card text (last seconds of the clip)" hint="Where viewers should go next. Filled in from the campaign if it sets one.">
          <input maxlength="80" value=${cp.cta_text || ""} onInput=${e => setCP("cta_text", e.target.value)} placeholder="e.g. Subscribe to @Tomovids on YouTube" /><//>
        <${Field} label=${`End card shows for ${cp.cta_seconds || 3}s`}><input type="range" min="1" max="8" step="0.5" value=${cp.cta_seconds || 3} onInput=${e => setCP("cta_seconds", +e.target.value)} /><//></div>
      <label class="f inline"><input type="checkbox" checked=${cp.enabled} onChange=${e => setCP("enabled", e.target.checked)} /><span>Burn captions into the video</span></label>
      <div class="fields">
        <${Field} label="Style"><select value=${clip.caption_style} onChange=${e => edit({ caption_style: e.target.value, caption_params: { ...(clip.caption_params || {}), size: null, max_words: null, uppercase: null, bg: null } })}>${Object.entries(sys.caption_styles).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
        <${Field} label="Font" hint="Drop .ttf/.otf files into the server's fonts folder to add more"><select value=${cp.font} onChange=${e => setCP("font", e.target.value)}>${sys.fonts.map(f => html`<option value=${f.label}>${f.label}</option>`)}</select><//>
        <${Field} label=${`Size ${cp.size}px`}><input type="range" min="40" max="160" step="2" value=${cp.size} onInput=${e => setCP("size", +e.target.value)} /><//>
        <${Field} label="Vertical position"><input type="range" min="0.08" max="0.92" step="0.01" value=${cp.y} onInput=${e => setCP("y", +e.target.value)} /><//>
        <${Field} label=${`Outline ${cp.outline}px`}><input type="range" min="0" max="14" step="1" value=${cp.outline} onInput=${e => setCP("outline", +e.target.value)} /><//>
        <${Field} label="Words per line"><input type="number" min="1" max="8" value=${cp.max_words} onChange=${e => setCP("max_words", Math.max(1, +e.target.value))} /><//>
      </div>
      <div class="row wrap">
        ${[["color", "Text"], ["accent", "Active word"], ["keyword_color", "Keywords"], ["outline_color", "Outline"]].map(([k, l]) => html`<label class="f" style="flex:none"><span>${l}</span><input type="color" value=${cp[k]} onInput=${e => setCP(k, e.target.value)} /></label>`)}
        <label class="f inline"><input type="checkbox" checked=${cp.uppercase} onChange=${e => setCP("uppercase", e.target.checked)} /><span>UPPERCASE</span></label>
        <label class="f inline"><input type="checkbox" checked=${cp.bg} onChange=${e => setCP("bg", e.target.checked)} /><span>Background box</span></label>
        <label class="f inline"><input type="checkbox" checked=${cp.highlight_keywords} onChange=${e => setCP("highlight_keywords", e.target.checked)} /><span>Highlight keywords</span></label></div>
      <div><div class="row" style="margin-bottom:6px"><h3 class="grow">Transcript <span class="muted" style="font-weight:400">${words.length} words</span></h3>
          <button class="btn sm ghost" onClick=${() => confirm("Discard your caption edits and reload the original transcript for this range?") && act("recaption", "Captions reloaded from the transcript")}>Regenerate</button></div>
        ${words.length ? html`<div class="words">${words.map((w, i) => html`<input key=${i + ":" + w.s} class=${now >= w.s && now < w.e ? "now" : ""} value=${w.w} size=${Math.max(2, w.w.length)} onChange=${e => editWord(i, e.target.value)} onFocus=${() => { if (vref.current) vref.current.currentTime = Math.max(clip.start, w.s - 0.1); }} aria-label="Caption word" />`)}</div>
          <p class="hint" style="margin-top:4px">Click a word to fix it; clear it to remove it. Timing stays attached to each word.</p>`
          : html`<div class="note">No speech was transcribed in this range, so there is nothing to caption.</div>`}</div></div>`,
    details: () => html`<div class="col">
      <${Field} label="Title"><input value=${clip.title} onInput=${e => edit({ title: e.target.value }, 600)} /><//>
      <${Field} label="Description / caption for the post"><textarea value=${clip.description} onInput=${e => edit({ description: e.target.value }, 600)}></textarea><//>
      <${Field} label="Hashtags" hint="Separated by spaces"><input value=${(clip.hashtags || []).join(" ")} onChange=${e => edit({ hashtags: e.target.value.split(/[\s,]+/).filter(Boolean) })} /><//>
      <${Field} label="Campaign"><select value=${clip.campaign_id || ""} onChange=${e => edit(e.target.value ? { campaign_id: +e.target.value } : { clear_campaign: true, campaign_id: null })}><option value="">None</option>${campaigns.map(c => html`<option value=${c.id}>${c.name}</option>`)}</select><//>
      <p class="hint">Changing the title, description, hashtags or campaign never forces a re-render.</p></div>`,
    assist: () => html`<div class="col">
      ${!sug ? html`<p class="muted small">Suggestions come from this clip's transcript and audio measurements. Nothing is invented.</p><button class="btn" onClick=${suggest}>Analyse this clip</button>`
        : html`<div><h3 style="margin-bottom:6px">Title options</h3><div class="col" style="gap:6px">${sug.titles.map(t => html`<div class="row"><span class="grow small">${t}</span><button class="btn sm" onClick=${() => edit({ title: t })}>Use</button></div>`)}</div></div>
          <div><h3 style="margin-bottom:4px">Opening hook</h3><p class="small muted">${sug.hook}</p></div>
          ${sug.tips.length > 0 && html`<div><h3 style="margin-bottom:6px">Edit notes</h3><div class="col" style="gap:6px">${sug.tips.map(t => html`<div class="note">${t.text}</div>`)}</div>
            ${(sug.trim.start != null || sug.trim.end != null) && html`<button class="btn sm" style="margin-top:8px" onClick=${() => { edit({ ...(sug.trim.start != null ? { start: sug.trim.start } : {}), ...(sug.trim.end != null ? { end: sug.trim.end } : {}) }); setSug(null); }}>Apply suggested trim</button>`}</div>`}
          <div class="row wrap"><span class="small grow">Caption style: <b>${sys.caption_styles[sug.caption_style.value]}</b> <span class="muted">(${sug.caption_style.why})</span></span><button class="btn sm" onClick=${() => edit({ caption_style: sug.caption_style.value })}>Use</button></div>
          <div class="row wrap"><span class="small grow">Layout: <b>${sys.layouts[sug.layout.value]}</b> <span class="muted">(${sug.layout.why})</span></span><button class="btn sm" onClick=${() => edit({ layout: sug.layout.value })}>Use</button></div>
          <div class="row wrap"><span class="small grow trunc">Hashtags: ${sug.hashtags.join(" ")}</span><button class="btn sm" onClick=${() => edit({ hashtags: sug.hashtags })}>Use</button></div>
          <div class="row wrap"><span class="small grow">Post description</span><button class="btn sm" onClick=${() => edit({ description: sug.description })}>Use</button></div>
          <button class="btn sm ghost" onClick=${suggest}>Refresh</button>`}</div>`,
    check: () => html`<div class="col">
      ${reviewFlags.length > 0 && html`<div><${Flags} flags=${clip.flags} />
        <label class="f inline" style="margin-top:10px"><input type="checkbox" checked=${clip.flags_cleared} onChange=${e => edit({ flags_cleared: e.target.checked }, 0)} /><span>I watched this clip and these flags are fine to publish</span></label></div>`}
      ${clip.compliance ? html`<div>${clip.compliance.checks.map(c => html`<div class=${"chk " + c.status}><i>${c.status === "pass" ? "✓" : c.status === "fail" ? "×" : "!"}</i><div><div>${c.label}</div>${c.detail && html`<div class="small muted">${c.detail}</div>`}</div></div>`)}</div>
          <div class=${"note" + (clip.compliance.ok ? "" : " warn")}>${clip.compliance.ok ? "All measurable rules pass. The campaign owner and the platform still make the final call." : `${clip.compliance.fails} rule${clip.compliance.fails === 1 ? "" : "s"} failing. Fix them, then check again.`}</div>`
        : html`<p class="muted small">${camp ? `Checks this clip against “${camp.name}” rules plus format and content flags.` : "No campaign assigned: only format and content flags are checked."}</p>`}
      <button class="btn" onClick=${check}>Run compliance check</button></div>`,
    post: () => html`<${PostKit} clip=${clip} onRecord=${() => setSubOpen(true)} />`,
  };

  return html`
  <div class="head"><a class="btn ghost sm" href="#/clips" aria-label="Back to review queue"><${Icon} n="back" /></a>
    <div class="grow"><h1 class="trunc">${clip.title || "Untitled clip"}</h1>
      <p><a href=${"#/video/" + clip.video_id}>${clip.video_title}</a> · ${clock(clip.start)}–${clock(clip.end)} <${Status} s=${clip.status} />
        ${clip.needs_render && clip.status !== "rendering" && html` <span class="pill warn">${clip.has_output ? "Edits not rendered" : "Not rendered"}</span>`}
        ${saving && html` <span class="dim small">Saving…</span>`}</p></div>
    <button class="btn ghost danger sm" onClick=${del}>Delete</button></div>

  <div class="studio">
    <div class="stage">
      <div class="seg" style="margin-bottom:8px;width:100%"><button class=${view === "preview" ? "on" : ""} style="flex:1" onClick=${() => setView("preview")}>Live preview</button>
        <button class=${view === "output" ? "on" : ""} style="flex:1" disabled=${!clip.has_output} onClick=${() => { vref.current && vref.current.pause(); setView("output"); }}>Rendered file</button></div>
      <div class=${view === "preview" ? "" : "hide"}><${Preview} clip=${clip} fonts=${sys.fonts} vref=${vref} onTime=${setNow} /></div>
      ${view === "output" && clip.has_output && html`<video class="out" key=${clip.rendered_hash} src=${`/api/clips/${id}/file?h=${clip.rendered_hash}`} controls playsinline></video>
        <p class="hint center" style="margin-top:4px">${clip.out_meta.width}×${clip.out_meta.height} · ${clip.out_meta.vcodec}/${clip.out_meta.acodec} · ${bytes(clip.out_meta.size)}${clip.needs_render ? " · older than your latest edits" : ""}</p>`}
      ${clip.job && html`<div style="margin-top:10px"><${JobLine} job=${clip.job} onChange=${load} /></div>`}
    </div>
    <div>
      <div class="seg" style="margin-bottom:12px" role="tablist">${TABS.map(([k, l]) => html`<button role="tab" aria-selected=${tab === k} class=${tab === k ? "on" : ""} onClick=${() => setTab(k)}>${l}${k === "check" && clip.compliance && !clip.compliance.ok ? " •" : ""}</button>`)}</div>
      <div class="card">${panel[tab]()}</div>
      <div class="sticky-actions">
        <button class="btn primary" disabled=${rendering || !clip.needs_render} onClick=${() => act("render", "Render queued")}>${rendering ? "Rendering…" : clip.has_output ? "Re-render" : "Render"}</button>
        ${clip.status !== "approved" ? html`<button class="btn good" disabled=${rendering || clip.needs_render} title=${clip.needs_render ? "Render first" : ""} onClick=${() => act("approve", "Clip approved")}>Approve</button>`
          : html`<button class="btn" onClick=${() => setSubOpen(true)}>Record submission${clip.submissions ? ` (${clip.submissions})` : ""}</button>`}
        ${clip.status !== "rejected" && html`<button class="btn" onClick=${() => act("reject", "Clip rejected")}>Reject</button>`}
        ${clip.has_output && html`<a class="btn" href=${`/api/clips/${id}/file?download=1`}>Download MP4</a>`}
      </div>
    </div>
  </div>
  ${subOpen && html`<${SubmissionForm} clip=${clip} sys=${sys} campaigns=${campaigns} onClose=${ok => { setSubOpen(false); ok && load(); }} />`}`;
}
