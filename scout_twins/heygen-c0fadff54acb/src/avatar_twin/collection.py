from __future__ import annotations

from pathlib import Path
from typing import Iterable
import html
import json
import re
import shutil

from .models import ValidationError


_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def build_multilingual_player(variants: Iterable[tuple[str, str | Path]], output: str | Path) -> Path:
    """Bundle self-contained previews behind a language selector."""
    destination = Path(output).resolve()
    variant_dir = destination.parent / f"{destination.stem}-variants"
    variant_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen: set[str] = set()
    for language, preview_value in variants:
        if not _LANGUAGE.fullmatch(language) or language in seen:
            raise ValidationError(f"invalid or duplicate language variant: {language}")
        preview = Path(preview_value).resolve()
        if not preview.is_file() or preview.suffix.lower() != ".html":
            raise ValidationError(f"language preview is not an HTML file: {preview_value}")
        seen.add(language)
        copied = variant_dir / f"{language}.html"
        shutil.copyfile(preview, copied)
        records.append({"language": language, "src": copied.relative_to(destination.parent).as_posix()})
    if not records:
        raise ValidationError("multilingual player needs at least one variant")
    payload = json.dumps(records, separators=(",", ":")).replace("</", "<\\/")
    options = "".join(
        f'<option value="{html.escape(item["language"])}">{html.escape(item["language"])}</option>'
        for item in records
    )
    document = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Multilingual avatar video</title>
<style>html,body{{height:100%;margin:0;background:#090b10;color:white;font-family:system-ui}}main{{height:100%;display:grid;grid-template-rows:auto 1fr}}header{{display:flex;gap:14px;align-items:center;padding:12px 18px;background:#151927}}select{{background:#0d1019;color:white;border:1px solid #434a66;border-radius:10px;padding:8px 12px}}iframe{{width:100%;height:100%;border:0}}</style></head><body><main><header><strong>Language</strong><select id="language">{options}</select></header><iframe id="player" title="Localized avatar video"></iframe></main><script>const variants={payload},select=document.querySelector('#language'),player=document.querySelector('#player');function load(){{player.src=variants.find(v=>v.language===select.value).src}}select.onchange=load;select.value=variants[0].language;load();</script></body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination

