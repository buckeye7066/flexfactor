# Public behavior contract

This is a clean-room behavioral contract based on first-party public pages and documentation reviewed on 2026-09-02. “Implemented” means this branch contains an executable route and validates a concrete artifact; it does not mean the independent models produce pixel-identical output or copy HeyGen's infrastructure/catalog.

| Public capability | First-party evidence | Independent implementation | Status |
|---|---|---|---|
| One prompt can create a video plan with script, visuals, voice, pacing, and captions | [Video Agent](https://developers.heygen.com/docs/video-agent), [Video Agent Academy](https://www.heygen.com/academy/video-agent) | `HttpVideoAgentPlanner` structured contract, deterministic baseline, editable review/approval workflow | Implemented; creative quality follows configured agent |
| A studio provides editable scenes, media, text, music, stylized captions, and templates | [New AI Studio overview](https://help.heygen.com/en/articles/11049655-overview-our-new-ai-studio) | Local studio UI with saved/versioned projects; separate plan, revise, save, approve, and render states; scene scripts, timing, layouts, expression, motion, title overlays, supporting media, global music, captions, and templates | Implemented |
| Avatar videos can be generated from text/audio and an avatar image | [Digital Twin generation](https://developers.heygen.com/generate-avatar-video), [Developer products](https://developers.heygen.com/) | Wan2.2 S2V, SadTalker, and MuseTalk adapters; actual audio/video probing and motion tests | Implemented with separately installed open models |
| A real performance can drive a reusable avatar identity/look | [Create Avatar](https://developers.heygen.com/docs/create-avatar), [Avatar Looks](https://developers.heygen.com/docs/avatar-looks) | Wan2.2 Animate and LivePortrait adapters; consented `AvatarLibrary` | Implemented with configured model runtime |
| Voice catalogs expose languages, accents, styles, and other attributes | [Voices overview](https://developers.heygen.com/docs/voices/overview), [Getting started with voices](https://help.heygen.com/en/articles/9834925-how-to-get-started-with-voices) | `VoiceCatalog` filters language/locale/accent/style/gender; studio/API fields flow into TTS | Implemented; no vendor catalog copied |
| Voice engines and delivery controls apply consistently across project scenes | [Advanced Voice Settings](https://help.heygen.com/en/articles/16139180-advance-voice-settings) | Provider selection plus project-wide locale, accent, style, emotion, delivery direction, rate, and pitch; configured providers receive those controls | Implemented; available engines follow operator configuration |
| Users can synthesize and clone voices | [AI voice generator](https://www.heygen.com/tool/ai-voice-generator), [AI voice cloning](https://www.heygen.com/tool/ai-voice-cloning) | HTTP WAV TTS, Piper, consented Coqui XTTS-v2; real rate/pitch filters; clone consent | Implemented with configured voices/models |
| Videos support backgrounds, layouts, branding, and transparent output | [HeyGen product](https://www.heygen.com/), [Transparent background](https://developers.heygen.com/transparent-background-videos), [Brand Kit](https://www.heygen.com/academy/brand-kit) | FFmpeg image/video/color/generated/transparent composition, per-scene layouts, logo/accent, chroma key | Implemented; generated images require configured service |
| Reusable templates preserve presentation choices | [Templates](https://www.heygen.com/academy/templates) | `TemplateStore` saves/applies voice, accent, background, brand, aspect, resolution, and scene styles | Implemented |
| Brand kits and glossaries control visual identity, pronunciation, and translation terms | [On Brand](https://developers.heygen.com/docs/on-brand), [Brand Glossary](https://developers.heygen.com/docs/brand-glossary) | Durable `BrandKitStore`; logo/colors/font alter composition and glossary rules flow through planning/localization | Implemented |
| Existing videos can be translated/dubbed into other languages | [HeyGen product](https://www.heygen.com/) | General translation endpoint, review state, target-language voice rerender, captions, multilingual collection | Implemented workflow; provider controls language quality |
| Cinematic/advanced avatar generation is available | [Cinematic Avatar](https://developers.heygen.com/cinematic-avatar), [Hyperframes](https://developers.heygen.com/hyperframes) | Provider-neutral remote GPU worker and command adapter can expose operator-selected cinematic models; artifacts receive the same validation | Partial; model-specific fidelity is external |
| Developer access includes APIs and CLI workflows | [Developer portal](https://developers.heygen.com/), [CLI](https://developers.heygen.com/cli) | JSON HTTP studio API and `avatar-twin` CLI for plan, revise, approve, render, localize, and multilingual collection | Implemented |
| Creation jobs are asynchronous and report state/results | [Developer portal](https://developers.heygen.com/) | Persistent `JobStore`, bounded worker queue, status route, streamed result artifacts | Implemented |
| A live avatar can conduct low-latency conversations using a knowledge context | [LiveAvatar overview](https://help.heygen.com/en/articles/10035615-how-to-get-started-with-liveavatar) | Provider-neutral HTTPS adapter creates bidirectional WebRTC sessions from avatar, voice, language, and knowledge/behavior context; sensitive join URLs are not persisted | Implemented with a configured live-avatar engine |
| Real-person avatar and cloned-voice use requires authorization | [Create Avatar](https://developers.heygen.com/docs/create-avatar) | Separate required consent scopes for `avatar_video` and `voice_clone`; enrolled identity hash cannot change silently | Implemented |

## Completion rule

A production job completes only if:

1. the project has explicit approval and required consent;
2. a non-test model backend reports ready and returns a new video artifact;
3. FFprobe finds a decodable video with dimensions, frames, and expected duration;
4. sampled frames show meaningful temporal visual change;
5. final assembly contains authoritative speech/master audio;
6. performance-transfer jobs preserve configured driving-video and authoritative-audio hashes;
7. the artifact manifest records provider, model, input/output hashes, probes, composition choices, and command/worker receipts.

Unavailable weights, TTS, translation, generation, consent, or runtime configuration produces an error. A static clip, procedural drawing, timing tone, metadata record, or browser-only preview is never reported as a completed production equivalent.
