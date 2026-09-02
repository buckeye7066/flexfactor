from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

from .models import VideoProject


_WORD = re.compile(r"[\w’'-]+|[^\w\s]", re.UNICODE)
_VISEME_BY_CHAR = {
    "a": "AI", "i": "AI", "y": "AI",
    "e": "E",
    "o": "O",
    "u": "U",
    "f": "FV", "v": "FV",
    "l": "L",
    "m": "MBP", "b": "MBP", "p": "MBP",
    "w": "WQ", "q": "WQ",
    "r": "R",
}


def _word_tokens(text: str) -> list[str]:
    return [token for token in _WORD.findall(text) if any(ch.isalnum() for ch in token)]


def _word_visemes(word: str) -> list[str]:
    lowered = word.lower()
    values: list[str] = []
    index = 0
    while index < len(lowered):
        pair = lowered[index:index + 2]
        if pair in {"th", "dh"}:
            value = "TH"
            index += 2
        elif pair in {"ch", "sh", "zh", "jh"}:
            value = "CH"
            index += 2
        else:
            char = lowered[index]
            value = _VISEME_BY_CHAR.get(char, "CDGKNRSTXZ" if char.isalpha() else "rest")
            index += 1
        if value != "rest" and (not values or values[-1] != value):
            values.append(value)
    return values or ["rest"]


def _caption_chunks(words: list[dict[str, Any]], max_words: int = 8) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for offset in range(0, len(words), max_words):
        chunk = words[offset:offset + max_words]
        if chunk:
            output.append({
                "start_s": chunk[0]["start_s"],
                "end_s": chunk[-1]["end_s"],
                "text": " ".join(item["text"] for item in chunk),
                "scene_index": chunk[0]["scene_index"],
            })
    return output


@dataclass(slots=True)
class TimelineCompiler:
    fps: int = 24

    def compile(self, project: VideoProject) -> dict[str, Any]:
        project.validate()
        words: list[dict[str, Any]] = []
        visemes: list[dict[str, Any]] = []
        motions: list[dict[str, Any]] = []
        scene_records: list[dict[str, Any]] = []

        for scene in project.scenes:
            tokens = _word_tokens(scene.script)
            speaking_span = scene.duration_s * 0.88
            weights = [max(1.0, len(token) ** 0.65) for token in tokens]
            cursor = scene.start_s + scene.duration_s * 0.05
            for token, weight in zip(tokens, weights):
                duration = speaking_span * weight / sum(weights) if weights else speaking_span
                start = cursor
                end = min(scene.end_s, cursor + duration)
                word = {
                    "text": token,
                    "start_s": round(start, 4),
                    "end_s": round(end, 4),
                    "scene_index": scene.index,
                }
                words.append(word)
                shapes = _word_visemes(token)
                shape_span = max(0.02, (end - start) / len(shapes))
                for position, shape in enumerate(shapes):
                    visemes.append({
                        "start_s": round(start + position * shape_span, 4),
                        "end_s": round(min(end, start + (position + 1) * shape_span), 4),
                        "shape": shape,
                        "word": token,
                        "scene_index": scene.index,
                    })
                cursor = end

            # A deterministic performance grammar: blinks, expression, head movement,
            # and hand emphasis. External performance authority remains separate.
            motions.append({
                "start_s": scene.start_s,
                "end_s": scene.end_s,
                "kind": "expression",
                "value": scene.expression,
                "strength": 0.7,
                "scene_index": scene.index,
            })
            if scene.gesture != "none":
                motions.append({
                    "start_s": round(scene.start_s + scene.duration_s * 0.32, 4),
                    "end_s": round(scene.start_s + scene.duration_s * 0.62, 4),
                    "kind": "gesture",
                    "value": scene.gesture,
                    "strength": 0.85 if scene.gesture == "emphasis" else 0.5,
                    "scene_index": scene.index,
                })
            blink = scene.start_s + 1.4
            while blink < scene.end_s - 0.2:
                motions.append({
                    "start_s": round(blink, 4),
                    "end_s": round(blink + 0.12, 4),
                    "kind": "blink",
                    "value": "closed",
                    "strength": 1.0,
                    "scene_index": scene.index,
                })
                blink += 2.6
            if scene.performance:
                motions.append({
                    "start_s": scene.start_s,
                    "end_s": scene.end_s,
                    "kind": "performance_authority",
                    "value": scene.performance,
                    "strength": 1.0,
                    "scene_index": scene.index,
                })
            scene_records.append({
                "id": scene.id,
                "index": scene.index,
                "start_s": scene.start_s,
                "end_s": scene.end_s,
                "layout": scene.layout,
                "visual": scene.visual,
                "transition": scene.transition,
                "camera": scene.camera,
            })

        captions = _caption_chunks(words) if project.captions_enabled else []
        return {
            "schema_version": "1.0",
            "project_id": project.id,
            "duration_s": round(project.duration_s, 4),
            "fps": self.fps,
            "aspect_ratio": project.aspect_ratio,
            "dimensions": list(project.dimensions),
            "language": project.language,
            "scenes": scene_records,
            "words": words,
            "visemes": visemes,
            "motions": sorted(motions, key=lambda item: (item["start_s"], item["kind"])),
            "captions": captions,
        }


def active_event(events: list[dict[str, Any]], time_s: float, default: Any = None) -> Any:
    for event in events:
        if event["start_s"] <= time_s < event["end_s"]:
            return event
    return default
