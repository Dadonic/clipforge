import { html, useState, api, fail, toast, Loading, Field } from "./core.js";

export function Settings({ user, setUser, sys }) {
  const s0 = user.settings || {};
  const [name, setName] = useState(user.name || "");
  const [s, setS] = useState({ default_layout: s0.default_layout || "", default_caption_style: s0.default_caption_style || "", quality: s0.quality || "standard",
    watermark: s0.watermark || "", min_duration: s0.min_duration || 15, max_duration: s0.max_duration || 60, max_highlights: s0.max_highlights || 40 });
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  if (!sys) return html`<${Loading} />`;
  const up = k => e => setS({ ...s, [k]: e.target.type === "number" ? +e.target.value : e.target.value });
  const save = async () => { try { setUser(await api("/auth/me", { method: "PATCH", body: { name, settings: s } })); toast("Settings saved"); } catch (e) { fail(e); } };
  const savePw = async () => { try { await api("/auth/me", { method: "PATCH", body: pw }); setPw({ current_password: "", new_password: "" }); toast("Password changed"); } catch (e) { fail(e); } };
  const tr = sys.transcription, pol = sys.profile.policy;
  const Dot = ({ ok }) => html`<span class=${"pill " + (ok ? "ok" : "")}>${ok ? "Available" : "Not set up"}</span>`;
  return html`
  <div class="head"><div class="grow"><h1>Settings</h1><p>Your defaults, and what this server is actually wired up to.</p></div></div>
  <div class="grid g2">
    <div class="col">
      <div class="card col"><h2>Clip defaults</h2>
        <div class="fields">
          <${Field} label="Layout for new clips"><select value=${s.default_layout} onChange=${up("default_layout")}><option value="">Automatic (facecam split if found)</option>${Object.entries(sys.layouts).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
          <${Field} label="Caption style"><select value=${s.default_caption_style} onChange=${up("default_caption_style")}><option value="">Pop</option>${Object.entries(sys.caption_styles).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>
          <${Field} label="Render quality" hint="Draft is fastest; high is slowest and largest"><select value=${s.quality} onChange=${up("quality")}>${sys.quality.map(q => html`<option value=${q}>${q}</option>`)}</select><//>
          <${Field} label="Default watermark"><input value=${s.watermark} onInput=${up("watermark")} placeholder="Optional" /><//>
          <${Field} label="Shortest clip (s)"><input type="number" min="5" max="180" value=${s.min_duration} onInput=${up("min_duration")} /><//>
          <${Field} label="Longest clip (s)"><input type="number" min="5" max="180" value=${s.max_duration} onInput=${up("max_duration")} /><//>
        </div></div>
      <div class="card col"><h2>Account</h2>
        <div class="fields"><${Field} label="Name"><input value=${name} onInput=${e => setName(e.target.value)} /><//><${Field} label="Email"><input value=${user.email} disabled /><//></div>
        <div class="row"><button class="btn primary" onClick=${save}>Save settings</button></div>
        <hr style="border:0;border-top:1px solid var(--line);width:100%" />
        <div class="fields"><${Field} label="Current password"><input type="password" value=${pw.current_password} onInput=${e => setPw({ ...pw, current_password: e.target.value })} autocomplete="current-password" /><//>
          <${Field} label="New password"><input type="password" value=${pw.new_password} onInput=${e => setPw({ ...pw, new_password: e.target.value })} autocomplete="new-password" /><//></div>
        <div class="row"><button class="btn" disabled=${!pw.current_password || pw.new_password.length < 8} onClick=${savePw}>Change password</button></div></div>
    </div>
    <div class="col">
      <div class="card col"><h2>AI services on this server</h2>
        <p class="hint">Configured with environment variables in the server's .env file. Keys are never shown here.</p>
        <h3>Speech-to-text <span class="muted" style="font-weight:400">(mode: ${tr.selected})</span></h3>
        ${tr.providers.map(p => html`<div class="row small"><span class="grow"><b>${p.name}</b>${p.detail ? html`<span class="muted"> — ${p.detail}</span>` : ""}</span><${Dot} ok=${p.available} /></div>`)}
        <h3 style="margin-top:6px">Highlight ranking</h3>
        <div class="row small"><span class="grow"><b>Signal scoring</b><span class="muted"> — audio, speech, scene and chat signals. Always on, free.</span></span><${Dot} ok=${true} /></div>
        <div class="row small"><span class="grow"><b>LLM re-rank</b><span class="muted"> — ${sys.semantic.llm_configured ? `${sys.semantic.llm_model} at ${sys.semantic.llm_base_url}` : "set LLM_API_KEY (any OpenAI-compatible endpoint) to enable"}</span></span><${Dot} ok=${sys.semantic.llm_configured} /></div>
        <h3 style="margin-top:6px">Storage</h3>
        <div class="row small"><span class="grow">${sys.storage.backend === "s3" ? `Rendered clips are copied to bucket “${sys.storage.bucket}”` : "Local disk on the server"}</span><${Dot} ok=${true} /></div>
        <h3 style="margin-top:6px">Whop</h3><p class="small muted">${sys.whop.detail}</p></div>
      <div class="card col"><h2>${sys.profile.name} content rules</h2>
        <p class="hint">These lists live in backend/app/profiles/gta6.json and can be edited there. Flags never auto-reject; they hold a clip until you review it.</p>
        ${[["Spoiler triggers", pol.spoiler_keywords], ["Graphic-violence triggers", pol.violence_keywords], ["Strong language", pol.strong_language]].map(([k, list]) => html`<div><h3>${k}</h3><p class="small muted">${list.join(", ")}</p></div>`)}
        <p class="small muted">Violence in the first ${pol.violence_early_seconds}s of a clip is called out separately. Possible cutscenes are flagged when there are ${pol.cinematic_min_scene_cuts}+ camera cuts with continuous dialogue and no streamer reaction.</p>
        <div><h3>Moment categories</h3><p class="small muted">${Object.values(sys.profile.categories).join(" · ")}</p></div></div>
    </div>
  </div>`;
}
