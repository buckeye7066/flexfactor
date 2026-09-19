import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flexfactor_rotation as R
from providers import cli_provider as cp

class OwnerSubscriptionPolicyTests(unittest.TestCase):
    def test_owner_mode_excludes_metered_even_when_call_allows_paid(self):
        routes = [R.Route(id=n, backend=n, backend_label=n, model=n, wire_model=n,
            api=api, base_url='http://unused', pool=n, cost_class=cost, tier=R.FRONTIER)
            for n,api,cost in [('api','openai',R.PAID_METERED),('codex','codex-cli',R.SUBSCRIPTION),('free','ollama',R.LOCAL_UNLIMITED)]]
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY':'1'}):
            rotator=R.Rotator(R.Catalog(routes),R.StateStore(os.path.join(root,'state.json')))
            first=rotator.next_route(allow_paid=True,paid_first=True,now=100)
            self.assertEqual(first.route.id,'codex')
            rotator.report(first.route,'quota_exhausted',retry_after_seconds=3600,now=100)
            self.assertEqual(rotator.next_route(allow_paid=True,paid_first=True,now=101).route.id,'free')
    def test_codex_requires_chatgpt_authentication(self):
        self.assertIn('forced_login_method=chatgpt',cp._argv_for('codex-cli','codex',None))
    def test_enrolled_owner_does_not_load_oauth_file_for_direct_http(self):
        from providers import chatgpt_subscription as direct
        with mock.patch.dict(os.environ,{'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY':'1','FLEXFACTOR_OWNER_CODEX_HOME':tempfile.gettempdir()}),mock.patch.object(direct,'load_exportable_oauth') as load:
            client=cp.build_subscription_client('codex-cli','gpt-6-astra','codex',30)
            self.assertIsNotNone(client)
            load.assert_not_called()


class OwnerInvocationContractTests(unittest.TestCase):
    def test_subscription_error_cannot_be_returned_as_a_completed_result(self):
        from providers.owner_subscription import OfficialOwnerSubscription
        import subprocess
        with mock.patch.dict(os.environ,{'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY':'1','FLEXFACTOR_OWNER_CODEX_HOME':tempfile.gettempdir()}), mock.patch.object(cp,'_run_process_tree',return_value=subprocess.CompletedProcess([],0,'{"billing_mode":"paid_api","raw":"wrong"}')), mock.patch('shutil.which',return_value='node'):
            with self.assertRaises(cp.CliUnavailable):
                OfficialOwnerSubscription('gpt-6-astra',30).complete('test')
    def test_strict_metered_pin_cannot_escape_owner_policy(self):
        route=R.Route(id='api',backend='api',backend_label='API',model='model',wire_model='model',api='openai',base_url='http://unused',pool='api',cost_class=R.PAID_METERED,tier=R.FRONTIER)
        with tempfile.TemporaryDirectory() as root,mock.patch.dict(os.environ,{'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY':'1'}):
            rotator=R.Rotator(R.Catalog([route]),R.StateStore(os.path.join(root,'state.json')))
            with self.assertRaises(R.PinUnavailable):rotator.next_route(allow_paid=True,pin='api')

class OwnerWorkerDeliveryTests(unittest.TestCase):
    def test_managed_session_cannot_launch_nested_owner_inference(self):
        from providers.owner_subscription import OfficialOwnerSubscription
        with mock.patch.dict(os.environ,{'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY':'1','FLEXFACTOR_OWNER_CODEX_HOME':tempfile.gettempdir(),'CODEX_THREAD_ID':'managed-fixture'}), mock.patch.object(cp,'_run_process_tree') as run:
            with self.assertRaises(cp.CliUnavailable):
                OfficialOwnerSubscription('gpt-6-astra',30).complete('fixture')
            run.assert_not_called()
    def test_installed_provider_declares_and_delivers_worker_resources(self):
        import tomllib
        import providers.owner_subscription as owner
        root=Path(__file__).resolve().parents[1]
        config=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf8'))
        self.assertIn('owner_ai/*.mjs',config['tool']['setuptools']['package-data'].get('providers',[]))
        for name in ['run-owner.mjs','officialCli.mjs','codexAppServer.mjs']:
            self.assertTrue((Path(owner.__file__).resolve().parent/'owner_ai'/name).is_file(),name)
    def test_planner_observes_owner_worker_output_and_input_limits(self):
        import flexfactor as ff
        from providers.owner_subscription import OfficialOwnerSubscription
        subscription=OfficialOwnerSubscription('gpt-6-astra',30)
        provider=cp.CliProvider('codex-cli','codex','codex',subscription=subscription)
        self.assertLessEqual(ff._provider_output_ceiling(provider),32000)
        self.assertFalse(ff._whole_file_is_plausible(provider,'x'*250000))

class OwnerProtocolTests(unittest.TestCase):
    def test_actual_model_authentication_and_cancellation_protocol(self):
        import subprocess
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run(['node','--test',str(root/'tests/owner_subscription_protocol.mjs')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout[-4000:]+result.stderr[-1000:])

class OwnerMergeBlockerTests(unittest.TestCase):
    def test_fallback_catalog_includes_the_enrolled_owner_codex_route(self):
        import flexfactor as ff
        with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '1',
                'FLEXFACTOR_OWNER_CODEX_HOME': tempfile.gettempdir(),
                'FLEXFACTOR_OWNER_CODEX_MODEL': 'owner-model'}), \
                mock.patch.object(ff, '_provider_free_routed', return_value=False):
            routes = ff._builtin_route_catalog(R)
            owner = [r for r in routes if r.api == 'codex-cli']
            self.assertEqual(len(owner), 1)
            self.assertEqual(owner[0].wire_model, 'owner-model')
            self.assertEqual(owner[0].cost_class, R.SUBSCRIPTION)
            with tempfile.TemporaryDirectory() as root:
                rotator = R.Rotator(R.Catalog(routes), R.StateStore(os.path.join(root, 'state.json')))
                self.assertEqual(rotator.next_route(allow_paid=True, paid_first=True, now=100).route.api, 'codex-cli')

    def test_rotating_owner_planner_preserves_worker_limits(self):
        import flexfactor as ff
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            store = R.StateStore(os.path.join(root, 'state.json'))
            with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '1'}), \
                    mock.patch.object(R, 'load_catalog', return_value=None), \
                    mock.patch.object(R, 'StateStore', return_value=store), \
                    mock.patch.object(ff, '_provider_free_routed', return_value=False), \
                    mock.patch.object(ff, '_hydrate_route_credentials', return_value=[]), \
                    mock.patch.object(ff, '_route_unusable_reason', return_value=''):
                provider = ff._build_rotating_provider(SimpleNamespace(max_cost=0), None, 'best', quiet=True)
                self.assertEqual(ff._provider_output_ceiling(provider), 32000)
                self.assertFalse(ff._whole_file_is_plausible(provider, 'x' * 100000))

    def test_unenrolled_fallback_catalog_does_not_add_an_owner_route(self):
        import flexfactor as ff
        with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '0'}), \
                mock.patch.object(ff, '_provider_free_routed', return_value=False):
            self.assertFalse(any(r.api == 'codex-cli' for r in ff._builtin_route_catalog(R)))

    def test_owner_mode_cannot_reuse_paid_rescue_clients_or_keys(self):
        import flexfactor as ff
        provider = object.__new__(ff.AnthropicProvider)
        provider._paid_client_obj = mock.Mock()
        provider._oai_rescue = mock.Mock()
        with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '1',
                'FLEXFACTOR_FALLBACK_ANTHROPIC_KEY': 'fixture-anthropic',
                'FLEXFACTOR_FALLBACK_OPENAI_KEY': 'fixture-openai'}):
            self.assertEqual(ff._fallback_anthropic_key(), '')
            self.assertEqual(ff._fallback_openai_key(), '')
            self.assertIsNone(provider._paid_client())
            self.assertIsNone(provider._openai_rescue_provider())
            original = RuntimeError('free route unavailable')
            with mock.patch.object(ff, '_stream_with_deadline') as stream:
                with self.assertRaises(RuntimeError) as raised:
                    provider._paid_message({}, original)
                self.assertIs(raised.exception, original)
                stream.assert_not_called()
            self.assertEqual(os.environ['FLEXFACTOR_FALLBACK_ANTHROPIC_KEY'], 'fixture-anthropic')

    def test_nonowner_rescue_policy_is_unchanged(self):
        import flexfactor as ff
        with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '0',
                'FLEXFACTOR_FALLBACK_ANTHROPIC_KEY': 'fixture-anthropic',
                'FLEXFACTOR_FALLBACK_OPENAI_KEY': 'fixture-openai'}):
            self.assertEqual(ff._fallback_anthropic_key(), 'fixture-anthropic')
            self.assertEqual(ff._fallback_openai_key(), 'fixture-openai')

    def test_fixed_owner_calls_gate_payloads_before_subscription_execution(self):
        import flexfactor as ff
        for provider_name in ('openai', 'anthropic'):
            for method in ('complete', 'grade', 'structured'):
                with self.subTest(provider=provider_name, method=method):
                    subscription = mock.Mock(model='owner-model')
                    subscription.complete.return_value = '{"score": 90, "summary": "fixture"}'
                    with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '1'}), \
                            mock.patch.object(cp, 'build_subscription_client', return_value=subscription), \
                            mock.patch.object(ff, '_egress_gate', side_effect=RuntimeError('blocked fixture')) as guard:
                        provider = ff.make_provider(provider_name, 'owner-model')
                        with self.assertRaisesRegex(RuntimeError, 'blocked fixture'):
                            getattr(provider, method)('source fixture')
                        guard.assert_called_once()
                        subscription.complete.assert_not_called()

    def test_fixed_owner_redaction_is_the_payload_sent_to_subscription(self):
        import flexfactor as ff
        subscription = mock.Mock(model='owner-model')
        subscription.complete.return_value = 'complete fixture'
        with mock.patch.dict(os.environ, {'FLEXFACTOR_OWNER_SUBSCRIPTION_ONLY': '1'}), \
                mock.patch.object(cp, 'build_subscription_client', return_value=subscription), \
                mock.patch.object(ff, '_egress_gate', return_value='redacted fixture'):
            provider = ff.make_provider('openai', 'owner-model')
            self.assertEqual(provider.complete('source fixture'), 'complete fixture')
            self.assertEqual(subscription.complete.call_args.args[0], 'redacted fixture')

if __name__=='__main__': unittest.main()
