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

if __name__=='__main__': unittest.main()

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
