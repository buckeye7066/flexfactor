"""Bounded live reproduction of mobile purpose inference, without target writes."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import flexfactor as ff
import flexfactor_rotation as rotation
from flexfactor_egress import redact_text
from providers import cli_provider as cp


def main():
    binary = cp.cli_binary_for('copilot-cli')
    if not binary:
        raise RuntimeError('The production CLI is not installed')
    prompt = ('Return JSON only: {"purpose":"Compute arithmetic means",'
              '"primary_users":["CLI users"],"core_journeys":["Compute the mean"],'
              '"acceptance_criteria":["mean(2,4) is 3"],"evidence_refs":["README.md:3"]}. '
              'Do not use tools.')
    begin = time.monotonic()
    completed = cp._run_process_tree(
        cp._argv_for('copilot-cli', binary, None, 'auto') + ['--output-format', 'json'],
        input=prompt, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=60, env=cp._recursion_guard_env('copilot-cli'), shell=False)
    models, types = [], []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        types.append(event.get('type'))
        data = event.get('data') or {}
        model = data.get('chosenModel') or data.get('model')
        if isinstance(model, str) and model not in models:
            models.append(model[:100])
    clean, _ = redact_text(completed.stderr[-1000:])
    print(json.dumps({'phase': 'raw-auto-observation', 'exit_code': completed.returncode,
                      'seconds': round(time.monotonic()-begin, 2), 'models': models,
                      'events': types, 'error_tail': clean}), flush=True)
    if completed.returncode:
        raise RuntimeError('The existing scoped workflow credential did not serve inference')

    def failed(route, error):
        safe, _ = redact_text(str(error))
        print(json.dumps({'phase': 'purpose-route-failure', 'route': route.id,
                          'type': type(error).__name__, 'detail': safe[:1000]}), flush=True)

    def factory(route):
        return cp.CliProvider('copilot-cli', route.wire_model, binary,
                              timeout=45, subscription=None)

    routes = [route for route in ff._builtin_route_catalog(rotation)
              if route.api == 'copilot-cli']
    with tempfile.TemporaryDirectory() as directory:
        provider = rotation.RotatingProvider(
            rotation.Rotator(rotation.Catalog(routes),
                store=rotation.StateStore(str(Path(directory) / 'routing.json'))),
            factory, tier=rotation.STRONG, paid_first=True, allow_paid=True,
            on_error=failed)
        key = os.path.normcase(os.path.abspath(directory))
        ff._PURPOSE_EVIDENCE_CACHE[key] = {'sources': [{
            'kind': 'readme', 'confidence': 'high', 'path_or_ref': 'README.md:3',
            'excerpt': 'Tiny CLI to compute the arithmetic mean of supplied numbers.'}]}
        started = time.monotonic()
        contract, error = ff._infer_purpose_contract(provider, 'tinystats', directory)
        print(json.dumps({'phase': 'production-purpose-inference',
            'seconds': round(time.monotonic()-started, 2), 'model': provider.model,
            'contract_present': contract is not None,
            'error': redact_text(str(error or ''))[0][:1500]}), flush=True)
        if contract is None:
            raise RuntimeError('The production purpose inference did not complete')

    # Reproduce the real gather, source policy and factory on the exact public
    # baseline. No refactor function is invoked and no target changes are made.
    from types import SimpleNamespace
    import faulthandler
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'target'
        git = ff._run(['git', 'clone', '--quiet',
            'https://github.com/buckeye7066/flexfactor-demo-tinystats.git', str(target)],
            directory, timeout=45)
        if git.returncode != 0:
            raise RuntimeError('The public diagnostic fixture could not be cloned')
        checkout = ff._git(['checkout', '--detach',
            'b41ee8171171811870b4dd46d40f3fe37364826f'], str(target))
        if checkout.returncode != 0:
            raise RuntimeError('The exact failed baseline could not be selected')
        args = SimpleNamespace(max_cost=1)
        real = ff._best_available_provider(ff._model_args_for_repository(args, str(target)), ff.CostMeter(1))
        original_error = real._on_error
        def report(route, error):
            failed(route, error)
            if original_error:
                original_error(route, error)
        real._on_error = report
        faulthandler.dump_traceback_later(90, repeat=True)
        started = time.monotonic()
        name, context = ff._gather_from_folder(str(target))
        print(json.dumps({'phase': 'real-gather', 'context_chars': len(context)}), flush=True)
        contract, confidence, authorized, error = ff._ensure_program_understanding(
            real, name, str(target), context_blob=context,
            explicit_goal='Correct mean to divide by the number of values; use a fresh default bucket in append_item; make parse_port enforce 1-65535 and raise ValueError on invalid input. Preserve the documented APIs and do not weaken validation or tests.')
        faulthandler.cancel_dump_traceback_later()
        print(json.dumps({'phase': 'exact-refactor-purpose',
            'seconds': round(time.monotonic()-started, 2), 'model': real.model,
            'contract_present': contract is not None, 'confidence': confidence,
            'error': redact_text(str(error or ''))[0][:1200]}), flush=True)
        if contract is None:
            raise RuntimeError('The exact refactor baseline did not establish purpose')


if __name__ == '__main__':
    main()
