from __future__ import annotations

from pathlib import Path
import argparse
import json

from .collection import build_multilingual_player
from .localization import HttpTranslationProvider, RuleTranslationProvider, localize_project
from .models import VideoProject
from .planning import ProjectWorkflow
from .renderer import RenderEngine


def _write(project: VideoProject, output: str) -> None:
    path = project.dump(output)
    print(json.dumps({"project_id": project.id, "status": project.status, "output": str(path)}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avatar-twin", description="Scout public avatar studio")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="turn a prompt/script project into editable scenes")
    plan.add_argument("project")
    plan.add_argument("--output", required=True)
    revise = commands.add_parser("revise", help="apply plan feedback")
    revise.add_argument("project")
    revise.add_argument("feedback")
    revise.add_argument("--output", required=True)
    approve = commands.add_parser("approve", help="explicitly approve a reviewed plan")
    approve.add_argument("project")
    approve.add_argument("--output", required=True)
    render = commands.add_parser("render", help="render an approved project bundle")
    render.add_argument("project")
    render.add_argument("--output-dir", required=True)
    render.add_argument("--asset-root", default=".")
    render.add_argument("--runtime-config", default="", help="local-model or remote-worker runtime JSON")
    render.add_argument("--provider", default="", help="configured provider name; defaults by render mode")
    localize = commands.add_parser("localize", help="create a reviewable language variant")
    localize.add_argument("project")
    localize.add_argument("language")
    localize.add_argument("--output", required=True)
    localize.add_argument("--endpoint", default="")
    localize.add_argument("--api-key", default="")
    localize.add_argument("--allow-demo-rule-translation", action="store_true")
    collection = commands.add_parser("collection", help="bundle rendered previews in a multilingual player")
    collection.add_argument("--variant", action="append", required=True,
                            help="LANGUAGE=PATH_TO_PREVIEW_HTML (repeatable)")
    collection.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workflow = ProjectWorkflow.from_env()
    if args.command == "plan":
        _write(workflow.plan(VideoProject.load(args.project)), args.output)
    elif args.command == "revise":
        _write(workflow.revise(VideoProject.load(args.project), args.feedback), args.output)
    elif args.command == "approve":
        _write(workflow.approve(VideoProject.load(args.project)), args.output)
    elif args.command == "render":
        engine = RenderEngine(
            Path(args.asset_root),
            runtime_config=args.runtime_config or None,
            provider_name=args.provider,
        )
        print(json.dumps(engine.render(VideoProject.load(args.project), args.output_dir), indent=2))
    elif args.command == "localize":
        if args.endpoint:
            provider = HttpTranslationProvider(args.endpoint, args.api_key)
        elif args.allow_demo_rule_translation:
            provider = RuleTranslationProvider()
        else:
            raise SystemExit(
                "localize requires --endpoint; --allow-demo-rule-translation only covers a tiny test vocabulary"
            )
        _write(localize_project(VideoProject.load(args.project), args.language, provider), args.output)
    elif args.command == "collection":
        variants = []
        for value in args.variant:
            if "=" not in value:
                raise SystemExit("--variant must be LANGUAGE=PATH")
            variants.append(tuple(value.split("=", 1)))
        print(json.dumps({"output": str(build_multilingual_player(variants, args.output))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
