from __future__ import annotations

from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, unquote, urlparse
import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid

from .jobs import JobStore, RenderQueue
from .library import AvatarLibrary, TemplateStore, VoiceCatalog
from .live import LiveSessionStore, live_provider_from_env
from .localization import HttpTranslationProvider, localize_project
from .models import AvatarProfile, BrandKit, ValidationError, VideoProject
from .planning import ProjectWorkflow
from .renderer import RenderEngine
from .studio import BrandKitStore, StudioProjectStore


CAPABILITIES = {
    "prompt_to_editable_plan": True,
    "review_and_approval": True,
    "voices": {
        "catalog_filters": ["language", "locale", "accent", "style", "gender"],
        "controls": ["voice_id", "emotion", "rate", "pitch_semitones"],
        "providers": ["operator HTTP TTS", "Piper", "Coqui XTTS-v2 consented clone"],
    },
    "avatars": ["photo", "digital_twin", "stylized", "animal", "reusable looks"],
    "backgrounds": ["brand color", "color", "image", "looped video", "generated image", "transparent"],
    "composition": ["per-scene layouts", "brand logo/accent", "captions", "background music"],
    "studio": ["saved projects", "revision history", "scene review", "brand kits"],
    "live_avatar": ["knowledge context", "voice selection", "bidirectional WebRTC session"],
    "templates": ["voice", "background", "brand", "aspect", "resolution", "scene styles"],
    "localization": ["translation endpoint", "reviewable localized project", "voice/lip-sync rerender"],
    "real_asset_uploads": [
        "avatar_image", "narration_audio", "voice_sample", "driving_performance_video",
        "background_image", "background_video", "brand_logo", "background_music",
    ],
    "avatar_engines": ["Wan2.2 Animate", "Wan2.2 S2V", "SadTalker", "MuseTalk", "LivePortrait"],
    "execution": ["local model checkout", "HTTPS remote GPU worker"],
    "outputs": ["validated MP4/WebM/MOV", "SRT captions", "project/timeline JSON", "hash/command receipts"],
    "consent_required_for": ["photo avatar", "digital twin", "voice clone"],
    "performance_transfer": {
        "driving_video_drives_motion": True,
        "supplied_audio_is_authoritative": True,
    },
    "false_fallbacks_removed": ["procedural cartoon", "timing tone", "static-video success", "metadata-only render"],
}


def _legacy_studio_html() -> str:
    return r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Independent Avatar Studio</title>
<style>*{box-sizing:border-box}body{margin:0;background:#090b10;color:#f6f7fb;font:15px system-ui,sans-serif}main{width:min(1180px,94vw);margin:34px auto 80px}.eyebrow{color:#47e6c1;font-weight:800;letter-spacing:.11em;text-transform:uppercase}h1{font-size:clamp(38px,7vw,76px);line-height:.92;margin:.22em 0}.lede{color:#b7bfcc;max-width:850px;font-size:18px}.card{background:#141824;border:1px solid #2c3345;border-radius:22px;padding:24px;margin-top:24px;box-shadow:0 24px 80px #0007}h2{margin:.2em 0 1em}label{display:block;font-weight:750;margin:12px 0 6px}textarea,input,select{width:100%;background:#0b0e16;border:1px solid #343d53;border-radius:11px;color:white;padding:11px;font:inherit}textarea{min-height:120px;resize:vertical}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}.upload{border:1px dashed #4a5570;padding:12px;border-radius:13px}.upload small,.fine{display:block;color:#929daf;margin-top:6px;font-size:13px}button{margin-top:20px;border:0;border-radius:999px;background:#6d5dfc;color:#fff;padding:14px 24px;font-weight:850;font-size:16px;cursor:pointer}button:disabled{opacity:.45}.status{margin-top:18px;padding:14px;border-radius:12px;background:#0b0e16;white-space:pre-wrap;line-height:1.55}a{color:#47e6c1}.consent{display:flex;align-items:center;gap:10px}.consent input{width:auto}.bad{color:#ff8d8d}.good{color:#47e6c1}@media(max-width:800px){.grid,.grid3{grid-template-columns:1fr}}</style></head>
<body><main><div class="eyebrow">Independent public-behavior implementation</div><h1>Avatar Video<br>Studio</h1><p class="lede">Create a consented presenter, choose a language/voice/accent, compose real backgrounds and branding, then produce a model-backed video with an auditable receipt.</p>
<section class="card"><div id="runtime" class="status">Checking model runtime…</div><h2>Story and identity</h2><label>Title</label><input id="title" value="Avatar studio presentation"><label>Script / creative direction</label><textarea id="script">Create a polished presenter video with natural speech, motion, captions, and brand styling.</textarea>
<div class="grid"><div class="upload"><label>Avatar image (required)</label><input id="avatar" type="file" accept="image/*"><small>Photo and digital-twin identities require consent.</small></div><div class="upload"><label>Driving performance video</label><input id="driving" type="file" accept="video/*"><small>Optional for talking head; required for performance transfer.</small></div></div>
<label>Consent subject</label><input id="subject" placeholder="Name of the person depicted/heard"><label class="consent"><input id="consent" type="checkbox"> I confirm this person authorized avatar-video use and, if provided, voice cloning.</label></section>
<section class="card"><h2>Voice, accent, and language</h2><div class="grid3"><div><label>Voice ID</label><input id="voiceId" list="voiceOptions" value="default"><datalist id="voiceOptions"></datalist><label>Provider</label><select id="voiceProvider"><option value="auto">Auto</option><option value="http">Configured HTTP TTS</option><option value="xtts_v2">XTTS-v2 clone</option><option value="piper">Piper local</option></select></div><div><label>Language</label><input id="language" value="en"><label>Locale</label><input id="locale" value="en-US"></div><div><label>Accent</label><input id="accent" value="neutral"><label>Style / delivery</label><input id="voiceStyle" value="conversational"></div><div><label>Emotion</label><input id="emotion" value="neutral"><label>Speaking rate (0.5–2)</label><input id="rate" type="number" min="0.5" max="2" step="0.05" value="1"></div><div><label>Pitch semitones (-12–12)</label><input id="pitch" type="number" min="-12" max="12" step="0.5" value="0"><label>Gender descriptor</label><input id="gender" value="unspecified"></div><div class="upload"><label>Reference voice sample</label><input id="voiceSample" type="file" accept="audio/*"><small>Enables consented XTTS voice cloning.</small></div><div class="upload"><label>Recorded narration/master audio</label><input id="audio" type="file" accept="audio/*"><small>When supplied, this is authoritative and TTS is skipped.</small></div><div class="upload"><label>Background music</label><input id="music" type="file" accept="audio/*"><label>Music volume</label><input id="musicVolume" type="number" min="0" max="1" step="0.01" value="0.12"></div></div></section>
<section class="card"><h2>Background, brand, and output</h2><div class="grid3"><div><label>Background kind</label><select id="backgroundKind"><option value="brand_color">Brand color</option><option value="color">Custom color</option><option value="image">Image</option><option value="video">Looped video</option><option value="generated">Generated image</option><option value="transparent">Transparent</option></select><label>Color</label><input id="backgroundColor" type="color" value="#10131a"></div><div class="upload"><label>Background image/video</label><input id="backgroundAsset" type="file" accept="image/*,video/*"><small>Choose a matching background kind.</small></div><div><label>Generated-background prompt</label><input id="backgroundPrompt" placeholder="Modern music studio, soft depth of field"><label>Fit</label><select id="backgroundFit"><option>cover</option><option>contain</option><option>stretch</option></select></div><div><label>Blur</label><input id="backgroundBlur" type="number" min="0" max="100" value="0"><label>Chroma key (optional)</label><input id="keyColor" placeholder="#00ff00"></div><div><label>Primary brand color</label><input id="primaryColor" type="color" value="#6c5ce7"><label>Brand background</label><input id="brandBackground" type="color" value="#10131a"></div><div class="upload"><label>Brand logo</label><input id="logo" type="file" accept="image/*"><small>Placed at top right; primary color becomes the accent bar.</small></div><div><label>Aspect</label><select id="aspect"><option>16:9</option><option>9:16</option><option>1:1</option><option>4:5</option></select><label>Resolution</label><select id="resolution"><option>480p</option><option selected>720p</option><option>1080p</option><option>4k</option></select></div><div><label>Format</label><select id="format"><option>mp4</option><option>webm</option><option>mov</option></select><label>Avatar provider override</label><input id="provider" placeholder="Use configured default"></div></div>
<button id="generate">Plan, approve, and render</button><div class="status" id="status">Waiting for inputs.</div><p class="fine">Production completion requires a real configured avatar engine and a decoded, moving output. Model weights can remain on a GPU worker; the branch contains the application and orchestration.</p></section></main>
<script>
const q=s=>document.querySelector(s),status=q('#status'),button=q('#generate');let voiceMap={};
async function json(path,options={}){const r=await fetch(path,options),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function post(path,data){return json(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)})}
async function upload(input,kind){if(!input.files.length)return '';const f=input.files[0];status.textContent='Uploading '+kind+'…';const r=await fetch('/api/assets?kind='+encodeURIComponent(kind),{method:'POST',headers:{'x-filename':f.name,'content-type':f.type||'application/octet-stream'},body:f});const j=await r.json();if(!r.ok)throw new Error(j.error||'upload failed');return j.path}
async function poll(id){for(;;){const j=await json('/api/jobs/'+id);status.textContent=`Job ${id}: ${j.state} (${Math.round(j.progress*100)}%)`;if(j.state==='completed'){status.innerHTML=`<span class="good">Completed and validated.</span> <a href="/outputs/${id}/preview.html" target="_blank">Play video</a> · <a href="/outputs/${id}/artifact.json" target="_blank">Inspect receipt</a>`;return}if(j.state==='failed')throw new Error(j.error);await new Promise(r=>setTimeout(r,900))}}
async function runtime(){try{const h=await json('/health');q('#runtime').innerHTML=h.runtime.ready?'<span class="good">Model runtime ready.</span> '+JSON.stringify(h.runtime.provider):'<span class="bad">Model runtime not ready.</span> '+(h.runtime.reason||JSON.stringify(h.runtime.providers||h.runtime))}catch(e){q('#runtime').textContent='Runtime check failed: '+e.message}}
async function loadVoices(){try{const data=await json('/api/voices');for(const v of data.voices||[]){voiceMap[v.id]=v;const o=document.createElement('option');o.value=v.id;o.label=[v.name,v.locale,v.accent,v.style].filter(Boolean).join(' · ');q('#voiceOptions').append(o)}}catch(e){}}
q('#voiceId').onchange=()=>{const v=voiceMap[q('#voiceId').value];if(!v)return;for(const [id,key] of [['voiceProvider','provider'],['language','language'],['locale','locale'],['accent','accent'],['voiceStyle','style'],['emotion','emotion'],['gender','gender'],['rate','rate'],['pitch','pitch_semitones']])if(v[key]!==undefined)q('#'+id).value=v[key]};
button.onclick=async()=>{button.disabled=true;try{if(!q('#avatar').files.length)throw new Error('Avatar image is required.');if(!q('#consent').checked||!q('#subject').value.trim())throw new Error('Recorded consent and subject name are required.');const kind=q('#backgroundKind').value,asset=q('#backgroundAsset');if((kind==='image'||kind==='video')&&!asset.files.length)throw new Error(kind+' background requires a file.');if(kind==='transparent'&&q('#format').value==='mp4')throw new Error('Transparent output requires webm or mov.');const avatarPath=await upload(q('#avatar'),'avatar_image'),audioPath=await upload(q('#audio'),'audio'),drivingPath=await upload(q('#driving'),'driving_video'),voiceSample=await upload(q('#voiceSample'),'voice_sample'),musicPath=await upload(q('#music'),'background_music'),logoPath=await upload(q('#logo'),'logo');let backgroundPath='';if(asset.files.length)backgroundPath=await upload(asset,kind==='video'?'background_video':'background_image');const consent={granted:true,subject_name:q('#subject').value.trim(),recorded_at:new Date().toISOString(),evidence_reference:'studio affirmative checkbox',permitted_uses:['avatar_video','voice_clone']};let p={title:q('#title').value,prompt:q('#script').value,script:q('#script').value,target_duration_s:30,aspect_ratio:q('#aspect').value,language:q('#language').value,output_format:q('#format').value,output_resolution:q('#resolution').value,avatar:{kind:'photo',image_path:avatarPath,style:drivingPath?'full_body':'talking_head',consent},voice:{id:q('#voiceId').value,provider:q('#voiceProvider').value,language:q('#language').value,locale:q('#locale').value,accent:q('#accent').value,style:q('#voiceStyle').value,emotion:q('#emotion').value,gender:q('#gender').value,rate:Number(q('#rate').value),pitch_semitones:Number(q('#pitch').value),sample_path:voiceSample,consent},background:{kind,color:q('#backgroundColor').value,path:backgroundPath,prompt:q('#backgroundPrompt').value,fit:q('#backgroundFit').value,blur:Number(q('#backgroundBlur').value),key_color:q('#keyColor').value},brand:{name:'Avatar Studio',primary_color:q('#primaryColor').value,secondary_color:'#00cec9',background_color:q('#brandBackground').value,text_color:'#ffffff',logo_path:logoPath},narration_audio_path:audioPath,background_music_path:musicPath,background_music_volume:Number(q('#musicVolume').value),metadata:drivingPath?{driving_video_path:drivingPath}:{}};status.textContent='Planning editable scenes…';let planned=await post('/api/plan',{project:p});let approved=await post('/api/approve',{project:planned});let job=await post('/api/jobs',{project:approved,provider:q('#provider').value.trim()});await poll(job.id)}catch(e){status.textContent='Error: '+e.message}finally{button.disabled=false}};runtime();
</script></body></html>'''.replace(";runtime();", ";loadVoices();runtime();")


def _studio_html() -> str:
    return r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scout Avatar Studio</title><style>
:root{color-scheme:dark;--bg:#0a0c12;--panel:#151925;--line:#2b3347;--muted:#98a2b8;--accent:#7868ff;--good:#4ee2bd;--bad:#ff9292}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f6f7fb;font:15px/1.45 Inter,system-ui,sans-serif}.app{display:grid;grid-template-columns:270px minmax(0,1fr);min-height:100vh}.sidebar{border-right:1px solid var(--line);padding:22px;background:#0e111a;position:sticky;top:0;height:100vh;overflow:auto}.brand{font-weight:900;font-size:20px}.brand small{display:block;color:var(--muted);font-size:11px;letter-spacing:.12em;text-transform:uppercase}.sidebar button,.project{width:100%;text-align:left;margin:10px 0 0}.project{border:1px solid var(--line);background:#141925;color:white;border-radius:10px;padding:10px;cursor:pointer}.project b,.project span{display:block}.project span{color:var(--muted);font-size:12px}.main{max-width:1280px;width:100%;padding:28px 34px 70px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}.top h1{font-size:42px;line-height:1;margin:0}.runtime{max-width:520px}.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:18px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}label{display:block;font-weight:750;margin:9px 0 5px}input,textarea,select{width:100%;border:1px solid #39435c;border-radius:9px;background:#0d1018;color:white;padding:10px;font:inherit}textarea{min-height:96px;resize:vertical}.toolbar{display:flex;flex-wrap:wrap;gap:10px;position:sticky;bottom:0;background:#0a0c12e8;padding:14px 0;backdrop-filter:blur(12px);z-index:3}button{border:1px solid #4c5774;border-radius:999px;background:#242b3e;color:white;padding:10px 16px;font-weight:800;cursor:pointer}button.primary{background:var(--accent);border-color:var(--accent)}button:disabled{opacity:.45}.status{white-space:pre-wrap;background:#0c0f17;border:1px solid var(--line);padding:12px;border-radius:10px;color:var(--muted)}.good{color:var(--good)}.bad{color:var(--bad)}.scene{border:1px solid #3a445d;border-radius:13px;padding:14px;margin-top:12px}.scene-head{display:flex;justify-content:space-between;gap:12px;align-items:center}.scene-head b{font-size:17px}.fine{color:var(--muted);font-size:12px}.hidden{display:none}a{color:var(--good)}@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.grid,.grid3{grid-template-columns:1fr}.main{padding:22px}.top{display:block}}
</style></head><body><div class="app"><aside class="sidebar"><div class="brand">Scout Avatar Studio<small>Independent URL twin</small></div><button id="newProject">+ New project</button><button id="refreshProjects">Refresh projects</button><h3>Projects</h3><div id="projectList" class="fine">Loading…</div><h3>Templates</h3><select id="templateSelect"><option value="">Choose template</option></select><button id="applyTemplate">Apply template</button><button id="saveTemplate">Save current as template</button><h3>Brand kits</h3><select id="brandSelect"><option value="">Choose brand kit</option></select><button id="applyBrand">Apply brand kit</button><button id="saveBrand">Save current brand kit</button></aside>
<main class="main"><div class="top"><div><div class="fine">PUBLIC-BEHAVIOR IMPLEMENTATION</div><h1>Video Studio</h1><p class="fine">Plan, edit every scene, approve, render, translate, and retain version history.</p></div><div id="runtime" class="status runtime">Checking production runtime…</div></div>
<section class="card"><h2>Project and creative brief</h2><div class="grid"><div><label>Title</label><input id="title" value="Avatar studio presentation"></div><div><label>Target duration (seconds)</label><input id="duration" type="number" min="1" max="1800" value="30"></div></div><label>Script or creative direction</label><textarea id="script">Create a polished presenter video with natural speech, motion, captions, and brand styling.</textarea><label>Revision feedback</label><textarea id="feedback" placeholder="Change scene two to a close-up, shorten the opening, make the delivery warmer…"></textarea></section>
<section class="card"><h2>Avatar and consent</h2><div class="grid3"><div><label>Avatar type</label><select id="avatarKind"><option value="photo">Photo avatar</option><option value="digital_twin">Digital twin</option><option value="stylized">Stylized avatar</option><option value="animal">Animal avatar</option></select><label>Avatar style</label><select id="avatarStyle"><option value="talking_head">Talking head</option><option value="full_body">Full body</option></select></div><div><label>Avatar photo</label><input id="avatar" type="file" accept="image/*"><span id="avatarSaved" class="fine"></span></div><div><label>Driving or training video</label><input id="driving" type="file" accept="video/*"><span id="drivingSaved" class="fine"></span></div><div><label>Consent subject</label><input id="subject" placeholder="Person depicted or heard"></div><div><label>Consent reference</label><input id="consentRef" placeholder="Recorded consent file or internal record"></div><div><label><input id="consent" type="checkbox" style="width:auto"> Authorized for avatar and voice-clone use</label></div></div></section>
<section class="card"><h2>Voice, language, and delivery</h2><div class="grid3"><div><label>Voice</label><input id="voiceId" list="voiceOptions" value="default"><datalist id="voiceOptions"></datalist><label>Engine</label><select id="voiceProvider"><option value="auto">Best configured</option><option value="http">HTTP TTS</option><option value="xtts_v2">XTTS-v2 clone</option><option value="piper">Piper local</option></select></div><div><label>Language</label><input id="language" value="en"><label>Locale</label><input id="locale" value="en-US"></div><div><label>Accent</label><input id="accent" value="neutral"><label>Delivery style</label><input id="voiceStyle" value="conversational"></div><div><label>Emotion</label><input id="emotion" value="neutral"><label>Gender descriptor</label><input id="gender" value="unspecified"></div><div><label>Rate (0.5–2)</label><input id="rate" type="number" min="0.5" max="2" step="0.05" value="1"><label>Pitch semitones</label><input id="pitch" type="number" min="-12" max="12" step="0.5" value="0"></div><div><label>Voice direction</label><textarea id="voiceDirection" placeholder="Warm, precise, pause after headings"></textarea></div><div><label>Voice-clone sample</label><input id="voiceSample" type="file" accept="audio/*"><span id="voiceSampleSaved" class="fine"></span></div><div><label>Recorded narration</label><input id="audio" type="file" accept="audio/*"><span id="audioSaved" class="fine"></span></div><div><label>Background music</label><input id="music" type="file" accept="audio/*"><label>Music volume</label><input id="musicVolume" type="number" min="0" max="1" step="0.01" value="0.12"></div></div></section>
<section class="card"><h2>Background, brand, captions, and export</h2><div class="grid3"><div><label>Background</label><select id="backgroundKind"><option value="brand_color">Brand color</option><option value="color">Color</option><option value="image">Image</option><option value="video">Looped video</option><option value="generated">Generated image</option><option value="transparent">Transparent</option></select><label>Color</label><input id="backgroundColor" type="color" value="#10131a"></div><div><label>Background asset</label><input id="backgroundAsset" type="file" accept="image/*,video/*"><span id="backgroundSaved" class="fine"></span><label>Generation prompt</label><input id="backgroundPrompt"></div><div><label>Fit</label><select id="backgroundFit"><option>cover</option><option>contain</option><option>stretch</option></select><label>Blur</label><input id="backgroundBlur" type="number" min="0" max="100" value="0"></div><div><label>Brand name</label><input id="brandName" value="Avatar Studio"><label>Primary color</label><input id="primaryColor" type="color" value="#6c5ce7"></div><div><label>Brand background</label><input id="brandBackground" type="color" value="#10131a"><label>Font family</label><input id="fontFamily" value="system-ui"></div><div><label>Logo</label><input id="logo" type="file" accept="image/*"><span id="logoSaved" class="fine"></span></div><div><label>Brand glossary (JSON)</label><textarea id="glossary">{}</textarea></div><div><label>Aspect</label><select id="aspect"><option>16:9</option><option>9:16</option><option>1:1</option><option>4:5</option></select><label>Resolution</label><select id="resolution"><option>480p</option><option selected>720p</option><option>1080p</option><option>4k</option></select></div><div><label>Format</label><select id="format"><option>mp4</option><option>webm</option><option>mov</option></select><label><input id="captions" type="checkbox" checked style="width:auto"> Stylized captions</label><label>Avatar provider override</label><input id="provider"></div></div></section>
<section class="card"><div class="scene-head"><div><h2>Editable scenes</h2><div class="fine">Review scripts, timing, layout, captions, motion, and expression before approval.</div></div><button id="addScene">+ Scene</button></div><div id="scenes" class="fine">Plan the project to create scenes.</div></section>
<section class="card"><h2>Live conversational avatar</h2><div class="grid3"><div><label>Live avatar ID</label><input id="liveAvatarId" placeholder="Enrolled avatar ID"></div><div><label>Voice ID</label><input id="liveVoiceId"></div><div><label>Language</label><input id="liveLanguage" value="en"></div></div><label>Knowledge and behavior context</label><textarea id="liveContext" placeholder="What the avatar knows, how it should respond, and its tone"></textarea><button id="startLive">Start bidirectional session</button></section>
<section class="card"><h2>Localization</h2><div class="grid"><div><label>Target language</label><input id="targetLanguage" placeholder="es, fr, de, ja…"></div><div><label>Target voice ID (optional)</label><input id="targetVoice"></div></div><button id="localize">Create reviewable localized project</button></section>
<div class="toolbar"><button id="plan" class="primary">1. Plan scenes</button><button id="revise">2. Revise plan</button><button id="save">Save revision</button><button id="approve">3. Approve and render</button></div><div id="status" class="status">Waiting for a project.</div>
</main></div><script>
const q=s=>document.querySelector(s), status=q('#status'); let project=null, revision=null, voiceMap={};
const assets={avatar:'',driving:'',voiceSample:'',audio:'',music:'',background:'',logo:''};
async function json(path,options={}){const r=await fetch(path,options),j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function post(path,data){return json(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)})}
async function upload(id,kind,key){const input=q('#'+id);if(!input.files.length)return assets[key]||'';const f=input.files[0];status.textContent='Uploading '+kind+'…';const r=await fetch('/api/assets?kind='+encodeURIComponent(kind),{method:'POST',headers:{'x-filename':f.name,'content-type':f.type||'application/octet-stream'},body:f});const j=await r.json();if(!r.ok)throw new Error(j.error||'upload failed');assets[key]=j.path;q('#'+key+'Saved')&&(q('#'+key+'Saved').textContent=j.path);return j.path}
function consent(){return{granted:q('#consent').checked,subject_name:q('#subject').value.trim(),recorded_at:new Date().toISOString(),evidence_reference:q('#consentRef').value.trim()||'studio affirmative authorization',permitted_uses:['avatar_video','voice_clone']}}
async function baseProject(){if(!q('#consent').checked||!q('#subject').value.trim())throw new Error('Avatar/voice authorization and subject are required.');const avatar=await upload('avatar','avatar_image','avatar'),driving=await upload('driving','driving_video','driving');if(!avatar)throw new Error('Avatar image is required.');const voiceSample=await upload('voiceSample','voice_sample','voiceSample'),audio=await upload('audio','audio','audio'),music=await upload('music','background_music','music'),logo=await upload('logo','logo','logo');const kind=q('#backgroundKind').value,bg=await upload('backgroundAsset',kind==='video'?'background_video':'background_image','background');if((kind==='image'||kind==='video')&&!bg)throw new Error(kind+' background requires an asset.');let glossary={};try{glossary=JSON.parse(q('#glossary').value||'{}')}catch(e){throw new Error('Brand glossary must be valid JSON.')}const c=consent();return{...(project||{}),title:q('#title').value,prompt:q('#script').value,script:q('#script').value,target_duration_s:Number(q('#duration').value),aspect_ratio:q('#aspect').value,language:q('#language').value,source_language:(project&&project.source_language)||q('#language').value,output_format:q('#format').value,output_resolution:q('#resolution').value,captions_enabled:q('#captions').checked,avatar:{kind:q('#avatarKind').value,image_path:avatar,style:q('#avatarStyle').value,consent:c},voice:{id:q('#voiceId').value,provider:q('#voiceProvider').value,language:q('#language').value,locale:q('#locale').value,accent:q('#accent').value,style:q('#voiceStyle').value,emotion:q('#emotion').value,gender:q('#gender').value,rate:Number(q('#rate').value),pitch_semitones:Number(q('#pitch').value),sample_path:voiceSample,consent:c,traits:{direction:q('#voiceDirection').value}},background:{kind,color:q('#backgroundColor').value,path:bg,prompt:q('#backgroundPrompt').value,fit:q('#backgroundFit').value,blur:Number(q('#backgroundBlur').value)},brand:{name:q('#brandName').value,primary_color:q('#primaryColor').value,secondary_color:'#00cec9',background_color:q('#brandBackground').value,text_color:'#ffffff',font_family:q('#fontFamily').value,logo_path:logo,glossary},narration_audio_path:audio,background_music_path:music,background_music_volume:Number(q('#musicVolume').value),metadata:driving?{...((project&&project.metadata)||{}),driving_video_path:driving}:((project&&project.metadata)||{})}}
function sceneCard(s,i){const d=document.createElement('div');d.className='scene';d.dataset.index=i;d.innerHTML='<div class="scene-head"><b></b><button class="remove">Remove</button></div><div class="grid3"><div><label>Script</label><textarea class="sceneScript"></textarea></div><div><label>Caption</label><textarea class="sceneCaption"></textarea></div><div><label>Duration</label><input class="sceneDuration" type="number" min="0.2" step="0.1"></div><div><label>Layout</label><select class="sceneLayout"><option>presenter_left</option><option>presenter_right</option><option>center</option><option>full_bleed</option><option>performance</option></select></div><div><label>Expression</label><input class="sceneExpression"></div><div><label>Gesture / motion direction</label><input class="sceneGesture"></div><div><label>Title / headline</label><input class="sceneTitle"></div><div><label>Title position</label><select class="sceneTitlePosition"><option>top</option><option>center</option><option>bottom</option></select></div><div><label>Title color / size</label><div class="grid"><input class="sceneTitleColor" type="color"><input class="sceneTitleSize" type="number" min="12" max="160"></div></div><div><label>Media asset path</label><input class="sceneMedia" placeholder="Uploaded image or video path"></div><div><label>Media position</label><select class="sceneMediaPosition"><option>right</option><option>left</option><option>center</option><option>full_bleed</option></select></div><div><label>Media scale</label><input class="sceneMediaScale" type="number" min="0.1" max="1" step="0.05"></div></div>';d.querySelector('b').textContent='Scene '+(i+1);d.querySelector('.sceneScript').value=s.script||'';d.querySelector('.sceneCaption').value=s.caption||s.script||'';d.querySelector('.sceneDuration').value=s.duration_s||3;d.querySelector('.sceneLayout').value=s.layout||'presenter_left';d.querySelector('.sceneExpression').value=s.expression||'warm';d.querySelector('.sceneGesture').value=s.gesture||'natural';d.querySelector('.sceneTitle').value=s.title_text||'';d.querySelector('.sceneTitlePosition').value=s.title_position||'top';d.querySelector('.sceneTitleColor').value=s.title_color||'#ffffff';d.querySelector('.sceneTitleSize').value=s.title_size||42;d.querySelector('.sceneMedia').value=s.media_path||'';d.querySelector('.sceneMediaPosition').value=s.media_position||'right';d.querySelector('.sceneMediaScale').value=s.media_scale||0.36;d.querySelector('.remove').onclick=()=>{d.remove();collectScenes();renderScenes(project.scenes)};return d}
function renderScenes(rows){const root=q('#scenes');root.innerHTML='';(rows||[]).forEach((s,i)=>root.append(sceneCard(s,i)));if(!(rows||[]).length)root.textContent='No scenes yet.'}
function collectScenes(){if(!project)project={};let cursor=0;project.scenes=[...document.querySelectorAll('.scene')].map((d,i)=>{const duration=Number(d.querySelector('.sceneDuration').value);const s={id:(project.scenes&&project.scenes[i]&&project.scenes[i].id)||'scene_'+i,index:i,start_s:cursor,duration_s:duration,script:d.querySelector('.sceneScript').value,caption:d.querySelector('.sceneCaption').value,layout:d.querySelector('.sceneLayout').value,expression:d.querySelector('.sceneExpression').value,gesture:d.querySelector('.sceneGesture').value,title_text:d.querySelector('.sceneTitle').value,title_position:d.querySelector('.sceneTitlePosition').value,title_color:d.querySelector('.sceneTitleColor').value,title_size:Number(d.querySelector('.sceneTitleSize').value),media_path:d.querySelector('.sceneMedia').value,media_position:d.querySelector('.sceneMediaPosition').value,media_scale:Number(d.querySelector('.sceneMediaScale').value),visual:'presenter',transition:'crossfade'};cursor+=duration;return s});return project.scenes}
function fill(p){project=p;revision=null;q('#title').value=p.title||'';q('#script').value=p.script||p.prompt||'';q('#duration').value=p.target_duration_s||30;q('#aspect').value=p.aspect_ratio||'16:9';q('#language').value=p.language||'en';q('#format').value=p.output_format||'mp4';q('#resolution').value=p.output_resolution||'720p';q('#captions').checked=p.captions_enabled!==false;for(const [key,val] of Object.entries({avatar:p.avatar&&p.avatar.image_path,driving:p.metadata&&p.metadata.driving_video_path,voiceSample:p.voice&&p.voice.sample_path,audio:p.narration_audio_path,music:p.background_music_path,background:p.background&&p.background.path,logo:p.brand&&p.brand.logo_path})){assets[key]=val||'';q('#'+key+'Saved')&&(q('#'+key+'Saved').textContent=assets[key])}const c=(p.avatar&&p.avatar.consent)||{};q('#subject').value=c.subject_name||'';q('#consentRef').value=c.evidence_reference||'';q('#consent').checked=!!c.granted;const v=p.voice||{};for(const [id,key] of [['voiceId','id'],['voiceProvider','provider'],['locale','locale'],['accent','accent'],['voiceStyle','style'],['emotion','emotion'],['gender','gender'],['rate','rate'],['pitch','pitch_semitones']])if(v[key]!==undefined)q('#'+id).value=v[key];q('#voiceDirection').value=(v.traits&&v.traits.direction)||'';const b=p.background||{};for(const [id,key] of [['backgroundKind','kind'],['backgroundColor','color'],['backgroundPrompt','prompt'],['backgroundFit','fit'],['backgroundBlur','blur']])if(b[key]!==undefined)q('#'+id).value=b[key];const k=p.brand||{};for(const [id,key] of [['brandName','name'],['primaryColor','primary_color'],['brandBackground','background_color'],['fontFamily','font_family']])if(k[key]!==undefined)q('#'+id).value=k[key];q('#glossary').value=JSON.stringify(k.glossary||{},null,2);renderScenes(p.scenes||[])}
async function save(){if(!project)throw new Error('Plan or open a project first.');collectScenes();project={...(await baseProject()),scenes:project.scenes,status:project.status||'draft'};const body={project};if(revision!==null)body.expected_revision=revision;const r=await post('/api/projects',body);project=r.project;revision=r.revision;status.textContent='Saved project revision '+revision;await loadProjects();return r}
async function loadProjects(){const rows=await json('/api/projects');const root=q('#projectList');root.innerHTML='';rows.forEach(r=>{const b=document.createElement('button');b.className='project';const title=document.createElement('b'),meta=document.createElement('span');title.textContent=r.title;meta.textContent=`${r.status} · r${r.revision} · ${r.language} · ${r.aspect_ratio}`;b.append(title,meta);b.onclick=async()=>{const rec=await json('/api/projects/'+encodeURIComponent(r.id));fill(rec.project);revision=rec.revision;status.textContent='Opened '+r.title+' revision '+revision};root.append(b)});if(!rows.length)root.textContent='No saved projects.'}
async function loadLibraries(){const [templates,brands]=await Promise.all([json('/api/templates'),json('/api/brand-kits')]);q('#templateSelect').innerHTML='<option value="">Choose template</option>';templates.forEach(t=>q('#templateSelect').add(new Option(t.name||t.id,t.id)));q('#brandSelect').innerHTML='<option value="">Choose brand kit</option>';brands.forEach(b=>q('#brandSelect').add(new Option(b.name||b.id,b.id)))}
async function poll(id){for(;;){const j=await json('/api/jobs/'+id);status.textContent=`Render ${id}: ${j.state} (${Math.round(j.progress*100)}%)`;if(j.state==='completed'){status.innerHTML=`<span class="good">Completed and validated.</span> <a href="/outputs/${id}/preview.html" target="_blank">Play</a> · <a href="/outputs/${id}/artifact.json" target="_blank">Receipt</a>`;return}if(j.state==='failed')throw new Error(j.error);await new Promise(r=>setTimeout(r,900))}}
async function guarded(fn){try{await fn()}catch(e){status.textContent='Error: '+e.message;throw e}}
q('#plan').onclick=()=>guarded(async()=>{project=await post('/api/plan',{project:await baseProject()});revision=null;renderScenes(project.scenes);status.textContent='Plan ready. Review every scene before approval.'});q('#revise').onclick=()=>guarded(async()=>{if(!project)throw new Error('Plan first.');collectScenes();project=await post('/api/revise',{project,feedback:q('#feedback').value});renderScenes(project.scenes);status.textContent='Revision ready for review.'});q('#save').onclick=()=>guarded(save);q('#approve').onclick=()=>guarded(async()=>{if(!project)throw new Error('Plan first.');collectScenes();project={...(await baseProject()),scenes:project.scenes,status:project.status};project=await post('/api/approve',{project});await save();const job=await post('/api/jobs',{project,provider:q('#provider').value.trim()});await poll(job.id)});q('#addScene').onclick=()=>{if(!project)project={scenes:[]};collectScenes();project.scenes.push({index:project.scenes.length,start_s:project.scenes.reduce((n,s)=>n+Number(s.duration_s||0),0),duration_s:3,script:'New scene',caption:'New scene',layout:'presenter_left',expression:'warm',gesture:'natural'});renderScenes(project.scenes)};q('#startLive').onclick=()=>guarded(async()=>{const session=await post('/api/live/sessions',{avatar_id:q('#liveAvatarId').value,context:q('#liveContext').value,voice_id:q('#liveVoiceId').value,language:q('#liveLanguage').value});status.innerHTML='<span class="good">Live session ready.</span> Opening secure room…';window.open(session.join_url,'_blank','noopener')});q('#localize').onclick=()=>guarded(async()=>{if(!project)throw new Error('Plan first.');collectScenes();project=await post('/api/localize',{project,language:q('#targetLanguage').value,voice_id:q('#targetVoice').value});revision=null;fill(project);status.textContent='Localized project created. Review before rendering.'});q('#saveTemplate').onclick=()=>guarded(async()=>{if(!project)throw new Error('Plan first.');const id=prompt('Template ID');if(!id)return;await post('/api/templates',{template_id:id,name:q('#title').value,project});await loadLibraries()});q('#applyTemplate').onclick=()=>guarded(async()=>{if(!project)project=await baseProject();project=await post('/api/templates/apply',{template_id:q('#templateSelect').value,project});fill(project)});q('#saveBrand').onclick=()=>guarded(async()=>{const id=prompt('Brand kit ID');if(!id)return;const p=await baseProject();await post('/api/brand-kits',{brand_id:id,brand:p.brand});await loadLibraries()});q('#applyBrand').onclick=()=>guarded(async()=>{if(!project)project=await baseProject();project=await post('/api/brand-kits/apply',{brand_id:q('#brandSelect').value,project});fill(project)});q('#newProject').onclick=()=>{project=null;revision=null;Object.keys(assets).forEach(k=>assets[k]='');renderScenes([]);status.textContent='New unsaved project.'};q('#refreshProjects').onclick=loadProjects;
async function startup(){try{const h=await json('/health');q('#runtime').innerHTML=h.runtime.ready?'<span class="good">Production renderer ready.</span> '+JSON.stringify(h.runtime.provider):'<span class="bad">Renderer configuration required.</span> '+(h.runtime.reason||JSON.stringify(h.runtime.providers||h.runtime));const voices=await json('/api/voices');(voices.voices||[]).forEach(v=>{voiceMap[v.id]=v;q('#voiceOptions').append(new Option([v.name,v.locale,v.accent,v.style].filter(Boolean).join(' · '),v.id))});await Promise.all([loadProjects(),loadLibraries()])}catch(e){status.textContent='Startup error: '+e.message}}startup();
</script></body></html>'''


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ppm"}
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_UPLOAD_KINDS = {
    "avatar_image": _IMAGE_EXTENSIONS,
    "background_image": _IMAGE_EXTENSIONS,
    "logo": _IMAGE_EXTENSIONS,
    "audio": _AUDIO_EXTENSIONS,
    "voice_sample": _AUDIO_EXTENSIONS,
    "background_music": _AUDIO_EXTENSIONS,
    "driving_video": _VIDEO_EXTENSIONS,
    "background_video": _VIDEO_EXTENSIONS,
    "scene_media": _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS,
}


@dataclass
class AvatarStudioApp:
    workspace: Path
    asset_root: Path
    runtime_config: str | Path | None = None
    provider_name: str = ""
    voice_catalog_path: str | Path | None = None
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024
    allow_test_backends: bool = False

    def __post_init__(self) -> None:
        self.workspace = self.workspace.resolve()
        self.asset_root = self.asset_root.resolve()
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.uploads = self.asset_root / ".avatar_twin_uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        selected_catalog = str(self.voice_catalog_path or os.environ.get("AVATAR_TWIN_VOICE_CATALOG") or "").strip()
        self.voice_catalog = VoiceCatalog.load(selected_catalog) if selected_catalog else None
        self.templates = TemplateStore(self.workspace / "templates")
        self.avatars = AvatarLibrary(self.workspace / "avatars", self.asset_root)
        self.projects = StudioProjectStore(self.workspace / "projects")
        self.brand_kits = BrandKitStore(self.workspace / "brand-kits")
        self.live_sessions = LiveSessionStore(self.workspace / "live-sessions")
        self.store = JobStore(self.workspace)
        self.workflow = ProjectWorkflow.from_env()
        self.renderer = RenderEngine(
            self.asset_root,
            runtime_config=self.runtime_config,
            provider_name=self.provider_name,
            allow_test_backends=self.allow_test_backends,
        )
        self.queue = RenderQueue(self.store, self.renderer)

    def close(self) -> None:
        self.queue.close()

    def upload(self, stream: BinaryIO, *, length: int, filename: str, kind: str) -> dict[str, Any]:
        if kind not in _UPLOAD_KINDS:
            raise ValidationError("unsupported asset kind")
        if not 0 < length <= self.max_upload_bytes:
            raise ValidationError("asset upload is empty or exceeds the configured limit")
        cleaned = _SAFE_FILENAME.sub("-", Path(filename).name).strip(".-") or "asset"
        suffix = Path(cleaned).suffix.lower()
        if suffix not in _UPLOAD_KINDS[kind]:
            raise ValidationError(f"unsupported {kind} extension: {suffix or '(none)'}")
        destination = self.uploads / f"{kind}-{uuid.uuid4().hex}{suffix}"
        digest = hashlib.sha256()
        remaining = length
        try:
            with destination.open("xb") as output:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValidationError("asset upload ended before Content-Length")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {
            "kind": kind,
            "path": destination.relative_to(self.asset_root).as_posix(),
            "bytes": length,
            "sha256": digest.hexdigest(),
        }

    def get(self, path: str, query: dict[str, list[str]]) -> dict[str, Any] | list[dict[str, Any]]:
        if path == "/api/voices":
            if not self.voice_catalog:
                return {"configured": False, "voices": []}
            filters = {name: str((query.get(name) or [""])[0])
                       for name in ("language", "locale", "accent", "style", "gender")}
            return {
                "configured": True,
                "voices": [asdict(item) for item in self.voice_catalog.search(**filters)],
            }
        if path == "/api/templates":
            return self.templates.list()
        if path == "/api/avatars":
            return self.avatars.list()
        if path == "/api/projects":
            include_archived = str((query.get("include_archived") or [""])[0]).lower() in {
                "1", "true", "yes",
            }
            return self.projects.list(include_archived=include_archived)
        if path == "/api/brand-kits":
            return self.brand_kits.list()
        if path == "/api/live/sessions":
            return self.live_sessions.list()
        project_match = re.fullmatch(r"/api/projects/([A-Za-z0-9._-]{1,96})", path)
        if project_match:
            revision_raw = str((query.get("revision") or [""])[0]).strip()
            revision = int(revision_raw) if revision_raw else None
            return self.projects.get(project_match.group(1), revision=revision)
        revisions_match = re.fullmatch(
            r"/api/projects/([A-Za-z0-9._-]{1,96})/revisions", path)
        if revisions_match:
            return self.projects.revisions(revisions_match.group(1))
        brand_match = re.fullmatch(r"/api/brand-kits/([A-Za-z0-9._-]{1,96})", path)
        if brand_match:
            return asdict(self.brand_kits.get(brand_match.group(1)))
        raise KeyError(path)

    @staticmethod
    def _project(body: dict[str, Any]) -> VideoProject:
        raw_project = body.get("project")
        if not isinstance(raw_project, dict):
            raise ValidationError("request requires a project object")
        return VideoProject.from_dict(raw_project)

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == "/api/avatars":
            raw_avatar = body.get("avatar")
            if not isinstance(raw_avatar, dict):
                raise ValidationError("request requires an avatar object")
            return 201, self.avatars.register(
                AvatarProfile.from_dict(raw_avatar),
                look_name=str(body.get("look_name") or "default"),
            )
        if path == "/api/brand-kits":
            raw_brand = body.get("brand")
            if not isinstance(raw_brand, dict):
                raise ValidationError("request requires a brand object")
            return 201, self.brand_kits.save(
                str(body.get("brand_id") or ""), BrandKit.from_dict(raw_brand))
        if path == "/api/live/sessions":
            session = live_provider_from_env().create(
                avatar_id=str(body.get("avatar_id") or ""),
                context=str(body.get("context") or ""),
                voice_id=str(body.get("voice_id") or ""),
                language=str(body.get("language") or "en"),
            )
            self.live_sessions.save(session)
            return 201, session.to_dict()
        if path == "/api/live/sessions/end":
            response = live_provider_from_env().end(
                str(body.get("provider_session_id") or ""))
            return 200, {"ended": True, "provider": response}
        restore_match = re.fullmatch(
            r"/api/projects/([A-Za-z0-9._-]{1,96})/restore", path)
        if restore_match:
            return 200, self.projects.restore(
                restore_match.group(1), int(body.get("revision", 0)))
        archive_match = re.fullmatch(
            r"/api/projects/([A-Za-z0-9._-]{1,96})/archive", path)
        if archive_match:
            expected = body.get("expected_revision")
            return 200, self.projects.archive(
                archive_match.group(1),
                expected_revision=int(expected) if expected is not None else None)
        project = self._project(body)
        if path == "/api/projects":
            expected = body.get("expected_revision")
            return 201, self.projects.save(
                project, expected_revision=int(expected) if expected is not None else None)
        if path == "/api/brand-kits/apply":
            project.brand = self.brand_kits.get(str(body.get("brand_id") or ""))
            project.touch()
            project.validate()
            return 200, project.to_dict()
        if path == "/api/plan":
            return 200, self.workflow.plan(project).to_dict()
        if path == "/api/revise":
            return 200, self.workflow.revise(project, str(body.get("feedback") or "")).to_dict()
        if path == "/api/approve":
            return 200, self.workflow.approve(project).to_dict()
        if path == "/api/templates":
            return 201, self.templates.save(
                str(body.get("template_id") or ""), project, name=str(body.get("name") or ""),
            )
        if path == "/api/templates/apply":
            return 200, self.templates.apply(str(body.get("template_id") or ""), project).to_dict()
        if path == "/api/localize":
            endpoint = os.environ.get("AVATAR_TWIN_TRANSLATION_URL", "").strip()
            if not endpoint:
                raise ValidationError("AVATAR_TWIN_TRANSLATION_URL is not configured")
            provider = HttpTranslationProvider(endpoint, os.environ.get("AVATAR_TWIN_TRANSLATION_KEY", ""))
            return 200, localize_project(project, str(body.get("language") or ""), provider).to_dict()
        if path == "/api/jobs":
            requested_provider = str(body.get("provider") or "").strip()
            if requested_provider:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requested_provider):
                    raise ValidationError("provider name contains unsafe characters")
                project.metadata["runtime_provider"] = requested_provider
            return 202, self.queue.submit(project).to_dict()
        raise KeyError(path)


def make_handler(app: AvatarStudioApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AvatarTwin/0.3"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("invalid Content-Length") from exc
            if not 0 < length <= 2_000_000:
                raise ValidationError("JSON body must be between 1 byte and 2 MB")
            if self.headers.get_content_type() != "application/json":
                raise ValidationError("Content-Type must be application/json")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValidationError("JSON body must be an object")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    data = _studio_html().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/health":
                    self._json(200, {
                        "status": "ok", "runtime": app.renderer.readiness(),
                        "services": {
                            "voice_catalog": bool(app.voice_catalog),
                            "tts": bool(os.environ.get("AVATAR_TWIN_TTS_URL") or os.environ.get("AVATAR_TWIN_PIPER_MODEL") or os.environ.get("AVATAR_TWIN_XTTS_SPEAKER_WAV")),
                            "translation": bool(os.environ.get("AVATAR_TWIN_TRANSLATION_URL")),
                            "generated_backgrounds": bool(os.environ.get("AVATAR_TWIN_BACKGROUND_URL")),
                            "video_agent": bool(os.environ.get("AVATAR_TWIN_AGENT_URL")),
                            "live_avatar": bool(os.environ.get("AVATAR_TWIN_LIVE_URL")),
                        },
                    })
                elif path == "/api/capabilities":
                    self._json(200, {**CAPABILITIES, "runtime": app.renderer.readiness()})
                elif (path in {"/api/voices", "/api/templates", "/api/avatars",
                               "/api/projects", "/api/brand-kits", "/api/live/sessions"}
                      or path.startswith("/api/projects/")
                      or path.startswith("/api/brand-kits/")):
                    self._json(200, app.get(path, parse_qs(parsed.query)))
                elif path == "/api/jobs":
                    self._json(200, [record.to_dict() for record in app.store.list()])
                elif path.startswith("/api/jobs/"):
                    self._json(200, app.store.get(path.rsplit("/", 1)[-1]).to_dict())
                elif path.startswith("/outputs/"):
                    self._serve_output(path)
                else:
                    self._json(404, {"error": "not found"})
            except KeyError:
                self._json(404, {"error": "not found"})
            except (ValidationError, ValueError) as exc:
                self._json(400, {"error": str(exc)})

        def _serve_output(self, request_path: str) -> None:
            relative = Path(unquote(request_path.removeprefix("/outputs/")))
            candidate = (app.store.outputs / relative).resolve()
            if not candidate.is_relative_to(app.store.outputs) or not candidate.is_file():
                self._json(404, {"error": "not found"})
                return
            if candidate.suffix.lower() not in {
                ".html", ".json", ".srt", ".mp4", ".webm", ".mov", ".wav", ".mp3", ".m4a", ".png", ".jpg", ".webp",
            }:
                self._json(403, {"error": "file type is not downloadable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(candidate.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with candidate.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/assets":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError as exc:
                        raise ValidationError("invalid Content-Length") from exc
                    kind = str((parse_qs(parsed.query).get("kind") or [""])[0])
                    filename = self.headers.get("X-Filename", "asset")
                    self._json(201, app.upload(self.rfile, length=length, filename=filename, kind=kind))
                    return
                status, payload = app.post(path, self._body())
                self._json(status, payload)
            except KeyError:
                self._json(404, {"error": "not found"})
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
            except (ValidationError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the independent public avatar-video studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default=".avatar_twin")
    parser.add_argument("--asset-root", default=".")
    parser.add_argument("--runtime-config", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--voice-catalog", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = AvatarStudioApp(
        Path(args.workspace), Path(args.asset_root),
        runtime_config=args.runtime_config or None,
        provider_name=args.provider,
        voice_catalog_path=args.voice_catalog or None,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Independent Avatar Video Studio: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
