"""Private-installation ChatGPT routing through the official Codex client."""
from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from typing import Any

def owner_subscription_only() -> bool:
    return os.environ.get('FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY', '').lower() in ('1', 'true')

def owner_route_allowed(route: Any) -> bool:
    if not owner_subscription_only():
        return True
    if getattr(route, 'cost_class', '') == 'paid-metered':
        return False
    return getattr(route, 'cost_class', '') != 'subscription' or getattr(route, 'api', '') == 'codex-cli'

class OfficialOwnerSubscription:
    def __init__(self, model: str, timeout: float):
        self.model = os.environ.get('FLEXFACTOR_OWNER_CODEX_MODEL') or ('gpt-6-astra' if model in ('', 'auto', 'default', 'codex') else model)
        self.timeout = min(120.0, max(1.0, timeout))
    def complete(self, prompt: str, *, system: str | None = None, max_tokens: int = 4096, timeout: float | None = None) -> str:
        from providers.cli_provider import CliUnavailable, _run_process_tree
        home = os.environ.get('FLEXFACTOR_OWNER_CODEX_HOME', '')
        if not owner_subscription_only() or not Path(home).is_absolute():
            raise CliUnavailable('Owner ChatGPT subscription is not enrolled on this installation')
        node = shutil.which('node')
        if not node:
            raise CliUnavailable('Node is required for the official owner subscription worker')
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'LOCALAPPDATA', 'USERPROFILE', 'HOME'}}
        env['OWNER_AI_CODEX_HOME'] = home
        env['OWNER_AI_CODEX_MODEL'] = self.model
        budget = min(self.timeout, float(timeout or self.timeout))
        payload = {'providers': ['codex'], 'system': system or '', 'prompt': prompt, 'format': 'text',
                   'maxTokens': max(2, min(32000, int(max_tokens))), 'timeoutMs': int(budget * 1000)}
        script = Path(__file__).resolve().parents[1] / 'tools' / 'owner-ai' / 'run-owner.mjs'
        result = _run_process_tree([node, str(script)], input=json.dumps(payload), capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=budget + 10, env=env, shell=False)
        try:
            data = json.loads(result.stdout or '{}')
        except (ValueError, TypeError):
            raise CliUnavailable('Owner subscription returned no valid result') from None
        if result.returncode or data.get('billing_mode') != 'subscription' or data.get('provider') != 'subscription:codex' or data.get('model') != self.model or not data.get('complete') or not str(data.get('raw') or '').strip():
            raise CliUnavailable('Owner ChatGPT subscription unavailable; no metered fallback was used')
        self.last_receipt = {key: data.get(key) for key in ('provider', 'billing_mode', 'model', 'usage')}
        return data['raw']
