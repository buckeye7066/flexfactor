from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import json
import math
import os
import re

from .models import Scene, ValidationError, VideoProject, stable_id


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_EMPHASIS = {"important", "must", "new", "best", "now", "discover", "create", "play"}


def _sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    parts = [part.strip() for part in _SENTENCE.split(clean) if part.strip()]
    return parts or [clean]


def _visual_for(text: str, index: int) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("piano", "guitar", "violin", "music", "perform")):
        return "performance"
    if any(word in lowered for word in ("number", "percent", "result", "growth")):
        return "motion_graphic"
    if any(word in lowered for word in ("show", "screen", "demo", "app")):
        return "product_demo"
    return ("presenter", "b_roll", "presenter_detail")[index % 3]


def _expression_for(text: str) -> str:
    lowered = text.lower()
    if "!" in text or any(word in lowered for word in _EMPHASIS):
        return "enthusiastic"
    if "?" in text:
        return "curious"
    if any(word in lowered for word in ("risk", "cannot", "never", "protect", "important")):
        return "serious"
    return "warm"


@dataclass(slots=True)
class VideoPlanner:
    min_scene_s: float = 2.0
    max_scene_s: float = 15.0

    def plan(self, source: VideoProject) -> VideoProject:
        project = source.clone()
        project.validate()
        if project.scenes:
            project.status = "awaiting_review"
            project.touch()
            return project

        text = project.script.strip() or project.prompt.strip()
        parts = _sentences(text)
        if not parts:
            raise ValidationError("planner received no speakable text")

        # Split long thoughts so each scene stays readable and independently editable.
        chunks: list[str] = []
        for part in parts:
            words = part.split()
            while len(words) > 32:
                chunks.append(" ".join(words[:28]) + ".")
                words = words[28:]
            if words:
                chunks.append(" ".join(words))

        weights = [max(1, len(re.findall(r"\b\w+\b", chunk))) for chunk in chunks]
        natural_duration = sum(max(self.min_scene_s, weight / 2.45 + 0.8) for weight in weights)
        target = max(float(project.target_duration_s), self.min_scene_s * len(chunks))
        total = max(min(target, self.max_scene_s * len(chunks)), min(natural_duration, target))
        raw = [total * weight / sum(weights) for weight in weights]
        durations = [min(self.max_scene_s, max(self.min_scene_s, value)) for value in raw]
        scale = total / sum(durations)
        durations = [max(self.min_scene_s, value * scale) for value in durations]

        scenes: list[Scene] = []
        cursor = 0.0
        for index, (chunk, duration) in enumerate(zip(chunks, durations)):
            duration = round(duration, 3)
            layout = "presenter_left" if index % 2 == 0 else "presenter_right"
            visual = _visual_for(chunk, index)
            if visual == "performance":
                layout = "performance"
            scene = Scene(
                id=stable_id("scene", project.id, index, chunk),
                index=index,
                start_s=round(cursor, 3),
                duration_s=duration,
                script=chunk,
                visual=visual,
                layout=layout,
                transition="fade" if index == 0 else ("wipe" if index % 3 == 1 else "crossfade"),
                expression=_expression_for(chunk),
                gesture="emphasis" if _expression_for(chunk) == "enthusiastic" else "natural",
                caption=chunk,
            )
            scenes.append(scene)
            cursor += duration

        # Recompute starts once so rounding cannot introduce gaps.
        cursor = 0.0
        for scene in scenes:
            scene.start_s = round(cursor, 3)
            cursor = scene.end_s
        project.scenes = scenes
        project.target_duration_s = round(cursor, 3)
        project.status = "awaiting_review"
        project.metadata["planner"] = {
            "kind": "deterministic_public_behavior_baseline",
            "scene_count": len(scenes),
            "words": sum(weights),
        }
        project.touch()
        project.validate()
        return project

    def revise(self, source: VideoProject, feedback: str) -> VideoProject:
        if source.status not in {"planned", "awaiting_review", "approved"}:
            raise ValidationError("only planned projects can be revised")
        project = source.clone()
        note = feedback.strip()
        lowered = note.lower()
        if not note:
            raise ValidationError("revision feedback cannot be empty")
        if "portrait" in lowered or "vertical" in lowered:
            project.aspect_ratio = "9:16"
        elif "square" in lowered:
            project.aspect_ratio = "1:1"
        elif "landscape" in lowered or "widescreen" in lowered:
            project.aspect_ratio = "16:9"
        if "captions off" in lowered or "no captions" in lowered:
            project.captions_enabled = False
        if "captions on" in lowered or "add captions" in lowered:
            project.captions_enabled = True
        if "more energetic" in lowered or "enthusiastic" in lowered:
            for scene in project.scenes:
                scene.expression = "enthusiastic"
                scene.gesture = "emphasis"
        if "calm" in lowered:
            for scene in project.scenes:
                scene.expression = "calm"
                scene.gesture = "subtle"
        speed = 1.0
        if "shorter" in lowered or "faster" in lowered:
            speed = 0.82
        elif "longer" in lowered or "slower" in lowered:
            speed = 1.18
        if not math.isclose(speed, 1.0):
            cursor = 0.0
            for scene in project.scenes:
                scene.start_s = round(cursor, 3)
                scene.duration_s = round(max(self.min_scene_s, scene.duration_s * speed), 3)
                cursor = scene.end_s
            project.target_duration_s = round(cursor, 3)
        project.metadata.setdefault("revision_history", []).append(note)
        project.status = "awaiting_review"
        project.touch()
        project.validate()
        return project


@dataclass(slots=True)
class HttpVideoAgentPlanner:
    """Provider-neutral one-prompt video agent with a strict editable-scene contract."""

    endpoint: str
    api_key: str = ""
    timeout_s: float = 180.0
    max_response_bytes: int = 2 * 1024 * 1024

    def _request(self, action: str, project: VideoProject, feedback: str = "") -> dict:
        parsed = urlsplit(self.endpoint)
        loopback = (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValidationError("video-agent endpoint must use HTTPS (HTTP is allowed for loopback)")
        contract = {
            "title": "string",
            "scenes": [{
                "duration_s": "number 1..60",
                "script": "spoken text",
                "visual": "presenter|b_roll|product_demo|motion_graphic|performance",
                "layout": "presenter_left|presenter_right|center|full_bleed|performance",
                "transition": "fade|crossfade|wipe|cut",
                "expression": "short descriptor",
                "gesture": "short descriptor",
                "caption": "caption text",
                "camera": "JSON object",
            }],
        }
        payload = json.dumps({
            "action": action,
            "creative_brief": project.prompt or project.script,
            "current_project": project.to_dict(),
            "feedback": feedback,
            "required_response": contract,
            "constraints": {
                "target_duration_s": project.target_duration_s,
                "aspect_ratio": project.aspect_ratio,
                "language": project.language,
                "editable": True,
                "no_asset_paths": True,
            },
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=self.timeout_s) as response:
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise ValidationError("video-agent response exceeded the configured byte limit")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("video agent returned invalid JSON") from exc
        if isinstance(value, dict) and isinstance(value.get("project"), dict):
            value = value["project"]
        if not isinstance(value, dict):
            raise ValidationError("video agent response must be an object")
        return value

    @staticmethod
    def _apply(source: VideoProject, response: dict, action: str) -> VideoProject:
        rows = response.get("scenes")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("video agent returned no editable scenes")
        project = source.clone()
        if str(response.get("title") or "").strip():
            project.title = str(response["title"]).strip()[:240]
        scenes: list[Scene] = []
        cursor = 0.0
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise ValidationError("video agent returned a non-object scene")
            script = str(raw.get("script") or raw.get("caption") or "").strip()
            duration = float(raw.get("duration_s", 0.0))
            if not script or not 1.0 <= duration <= 60.0:
                raise ValidationError("video-agent scenes require script and duration_s between 1 and 60")
            # Asset paths are intentionally never accepted from an AI planner.
            scene = Scene.from_dict({
                **raw,
                "id": stable_id("scene", project.id, index, script),
                "index": index,
                "start_s": round(cursor, 3),
                "duration_s": round(duration, 3),
                "script": script,
                "media_path": "",
            })
            scene.validate()
            scenes.append(scene)
            cursor = scene.end_s
        if cursor > 1800:
            raise ValidationError("video-agent plan exceeds the 30-minute project limit")
        project.scenes = scenes
        project.target_duration_s = round(cursor, 3)
        project.status = "awaiting_review"
        project.metadata["planner"] = {
            "kind": "configured_http_video_agent",
            "action": action,
            "scene_count": len(scenes),
        }
        project.touch()
        project.validate()
        return project

    def plan(self, source: VideoProject) -> VideoProject:
        source.validate()
        return self._apply(source, self._request("plan", source), "plan")

    def revise(self, source: VideoProject, feedback: str) -> VideoProject:
        if source.status not in {"planned", "awaiting_review", "approved"}:
            raise ValidationError("only planned projects can be revised")
        if not feedback.strip():
            raise ValidationError("revision feedback cannot be empty")
        revised = self._apply(source, self._request("revise", source, feedback.strip()), "revise")
        revised.metadata.setdefault("revision_history", []).append(feedback.strip())
        return revised

@dataclass(slots=True)
class ProjectWorkflow:
    planner: VideoPlanner | HttpVideoAgentPlanner = field(default_factory=VideoPlanner)

    @classmethod
    def from_env(cls) -> "ProjectWorkflow":
        endpoint = os.environ.get("AVATAR_TWIN_AGENT_URL", "").strip()
        if not endpoint:
            return cls()
        return cls(HttpVideoAgentPlanner(
            endpoint=endpoint,
            api_key=os.environ.get("AVATAR_TWIN_AGENT_KEY", "").strip(),
            timeout_s=float(os.environ.get("AVATAR_TWIN_AGENT_TIMEOUT_S", "180")),
        ))

    def plan(self, project: VideoProject) -> VideoProject:
        return self.planner.plan(project)

    def revise(self, project: VideoProject, feedback: str) -> VideoProject:
        return self.planner.revise(project, feedback)

    def approve(self, source: VideoProject) -> VideoProject:
        if source.status not in {"planned", "awaiting_review"}:
            raise ValidationError("only a reviewed plan can be approved")
        project = source.clone()
        project.status = "approved"
        project.metadata["approval"] = {"explicit": True, "method": "local_workflow"}
        project.touch()
        project.validate(require_approved=True)
        return project
