# Scout Avatar Video Studio

This retained Program Scout branch contains an independently written, launchable avatar-video application derived from HeyGen's publicly documented behavior. It belongs only to Program Scout's URL-specific branch and is not integrated into any target application.

The branch does not contain HeyGen source, private APIs, credentials, media, or model weights. It reaches the same classes of outcomes through original orchestration around documented open models and operator-selected services. Keeping large weights and GPU execution outside Git lets every scouted URL keep a permanent branch without making the Scout repository or a desktop machine carry every model.

## Executable capability surface

| Area | What this branch actually executes |
|---|---|
| Video agent | A configured HTTP video agent turns one brief into strictly validated, editable scenes. The built-in deterministic planner remains available and identifies itself as a baseline. Plans require review and approval. |
| Full studio | Saved projects, optimistic revision history, restore/archive, scene-by-scene script/timing/layout/expression/gesture editing, scene headlines and media overlays, and separate plan/revise/save/approve/render states. |
| Avatars | Photo/digital-twin enrollment with consent and immutable source hashes; reusable identities and looks; talking-head and performance-transfer adapters for Wan2.2 Animate/S2V, SadTalker, MuseTalk, and LivePortrait. |
| Voices and accents | Filterable voice catalogs with language, locale, accent, style, gender, emotion, rate, and pitch; operator HTTP TTS; local Piper; consented multilingual Coqui XTTS-v2 voice cloning. HTTP providers receive every voice control. Local voices receive real FFmpeg rate/pitch processing. |
| Backgrounds | Brand/custom colors, uploaded images, looped videos, generated images through a configured image service, blur/fit controls, chroma-key removal, and WebM/MOV transparency. |
| Composition | Per-scene presenter-left/right/center/full-bleed placement, logo overlay, brand accent, captions, background-music mixing, aspect ratios, 480p/720p/1080p/4K, and MP4/WebM/MOV output. |
| Templates and brand | Durable templates reuse voice, accent, background, brand, aspect, resolution, and scene layouts. Brand logos/colors and glossary rules change executable output or localization. |
| Localization/dubbing | A configured translator creates a reviewable target-language project, changes the voice language, clears source narration, and re-runs speech/avatar generation. A multilingual player can collect rendered variants. |
| Live avatar | A configured HTTPS live-avatar engine creates a real bidirectional WebRTC session from an enrolled avatar, selected voice/language, and knowledge/behavior context; room URLs are returned once and never persisted. |
| Verification | A job succeeds only after FFprobe finds real video/audio, duration is in tolerance, sampled frames contain meaningful motion, and command/artifact hashes are written. Static, cartoon, timing-tone, and metadata-only fallbacks cannot complete production jobs. |

## Launch the program

Requirements are Python 3.11+ and FFmpeg/FFprobe. Install this branch and copy the runtime template:

```bash
python -m pip install -e .
cp runtime.example.json runtime.local.json
```

Edit `runtime.local.json` to point at installed model repositories/checkpoints or at your HTTPS GPU worker. Then launch:

```bash
avatar-twin-server \
  --host 127.0.0.1 \
  --port 8765 \
  --workspace .avatar-twin \
  --asset-root . \
  --runtime-config runtime.local.json \
  --voice-catalog voices.example.json
```

Open `http://127.0.0.1:8765`. A later desktop icon only needs to run that command and open the URL. The program itself is already the launch target.

The studio exposes these discovery/library routes:

- `GET /health`, `GET /api/capabilities`
- `GET /api/voices?language=en&accent=British&style=news`
- `GET|POST /api/projects`, project revision history/restore/archive routes
- `GET|POST /api/templates`, `/api/avatars`, and `/api/brand-kits`
- `POST /api/plan`, `/api/revise`, `/api/approve`, `/api/localize`
- `GET|POST /api/live/sessions` and `POST /api/live/sessions/end`
- `POST /api/assets`, `/api/jobs`
- `GET /api/jobs/<id>` and streamed `/outputs/<job>/<artifact>`

## Runtime choices

Local model adapters execute the upstream CLIs with explicit argument vectors and no shell. Every command receives a receipt with argv, timestamps, exit code, log paths, and log hashes. `runtime.example.json` includes all supported providers.

For machines without enough VRAM, use `remote_worker`. The application streams uploads/downloads and commits no weights or generated videos. The worker protocol is in [REMOTE_WORKER_CONTRACT.md](REMOTE_WORKER_CONTRACT.md).

Optional services are configured through environment variables:

| Variable | Purpose |
|---|---|
| `AVATAR_TWIN_TTS_URL`, `AVATAR_TWIN_TTS_KEY` | Provider-neutral WAV TTS with voice/accent/style controls |
| `AVATAR_TWIN_PIPER_MODEL`, `AVATAR_TWIN_PIPER_CONFIG` | Local Piper voice |
| `AVATAR_TWIN_XTTS_SPEAKER_WAV` | Default consented XTTS-v2 speaker sample |
| `AVATAR_TWIN_TRANSLATION_URL`, `AVATAR_TWIN_TRANSLATION_KEY` | General translation/dubbing |
| `AVATAR_TWIN_BACKGROUND_URL`, `AVATAR_TWIN_BACKGROUND_KEY` | Generated background images |
| `AVATAR_TWIN_AGENT_URL`, `AVATAR_TWIN_AGENT_KEY` | Structured one-prompt video planning |
| `AVATAR_TWIN_VOICE_CATALOG` | Voice catalog JSON |
| `AVATAR_TWIN_WORKER_TOKEN` | Remote GPU worker bearer token |
| `AVATAR_TWIN_LIVE_URL`, `AVATAR_TWIN_LIVE_KEY` | Bidirectional live-avatar session provider |

## Reuse from another program

```python
from avatar_twin import ProjectWorkflow, RenderEngine, StudioProjectStore, VideoProject

project = ProjectWorkflow.from_env().plan(VideoProject.from_dict(project_data))
revision = StudioProjectStore(".avatar-twin/projects").save(project)
project = ProjectWorkflow.from_env().approve(project)  # after user review
artifact = RenderEngine(
    allowed_asset_root=asset_root,
    runtime_config="runtime.local.json",
).render(project, output_dir)
```

Exact reusable symbols and their contracts are recorded in `REUSE_CONTRACT.json`; a later, separately approved target integration can consume them without changing this Scout branch's ownership.

## Verification

```bash
python -m unittest discover -s tests -p 'test*.py' -v
```

Tests use explicit `test_only` FFmpeg fixtures to exercise the complete pipeline without downloading weights. `RuntimeConfig` rejects those providers in production. The real model adapters and readiness checks remain the only production completion path.

See [PUBLIC_BEHAVIOR_CONTRACT.md](PUBLIC_BEHAVIOR_CONTRACT.md) for the evidence-to-code accounting and [RUNTIME_CONTRACT.json](RUNTIME_CONTRACT.json) for executable routes.
