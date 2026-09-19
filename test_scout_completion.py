"""Real Scout timeout recovery and truthful result regression tests."""
import io
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch
import flexfactor as ff
import flexfactor_purpose as fp

class ScoutBenefitRecoveryTests(unittest.TestCase):
    def test_transient_timeout_retries_once_without_fabricating_a_skip(self):
        self.assertTrue(hasattr(ff, '_judge_scout_benefit'))
        answer = {'benefit_score': 70, 'verdict': 'consider', 'how_it_helps': 'Typed numeric validation',
                  'integration_note': 'Wrap CLI arguments', 'risks': ['One dependency']}
        with patch.object(ff, '_judge', side_effect=[TimeoutError('timed out'), answer]) as call:
            self.assertEqual(ff._judge_scout_benefit(object(), 'candidate'), answer)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all(c.kwargs['max_tokens'] <= 2048 for c in call.call_args_list))

    def test_repeated_timeouts_remain_incomplete_not_successful_rejections(self):
        self.assertTrue(hasattr(ff, '_judge_scout_benefit'))
        with patch.object(ff, '_judge', side_effect=TimeoutError('timed out')) as call:
            with self.assertRaises(TimeoutError): ff._judge_scout_benefit(object(), 'candidate')
        self.assertEqual(call.call_count, 2)

    def test_budget_failure_is_not_retried(self):
        self.assertTrue(hasattr(ff, '_judge_scout_benefit'))
        with patch.object(ff, '_judge', side_effect=ff.BudgetExceededError('budget')) as call:
            with self.assertRaises(ff.BudgetExceededError): ff._judge_scout_benefit(object(), 'candidate')
        self.assertEqual(call.call_count, 1)



class ScoutEvaluationReceiptTests(unittest.TestCase):
    def failed_evaluation(self):
        return {'repo': {'fullName': 'example/typed-parser', 'htmlUrl': 'https://github.com/example/typed-parser'},
                'need': 'Typed validation', 'recommendation': 'SKIP', 'evaluation_complete': False,
                'benefit': {'benefit_score': 0, 'how_it_helps': 'judging failed: timed out'},
                'evaluation_error': 'TimeoutError: timed out'}

    def test_console_does_not_call_unreviewed_candidate_unnecessary(self):
        output = io.StringIO()
        with redirect_stdout(output):
            ff._print_scout_report('calculator', {}, [self.failed_evaluation()])
        self.assertIn('INCOMPLETE', output.getvalue())
        self.assertNotIn('1 candidate(s) evaluated and found unnecessary', output.getvalue())

    def test_markdown_separates_incomplete_evaluations(self):
        with tempfile.TemporaryDirectory() as directory:
            report = ff._write_scout_report(directory, 'calculator', {}, [self.failed_evaluation()])
            text = Path(report).read_text(encoding='utf-8')
        self.assertIn('## Incomplete evaluations', text)
        self.assertNotIn('## Evaluated but unnecessary', text)
        self.assertIn('timed out', text)

    def exercise_scout(self, *, verified=1, fail=True, apply=False, qualify=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            Path(directory, 'app.py').write_text('print(1)\n', encoding='utf-8')
            args = types.SimpleNamespace(program=directory, repo_rewards_url='https://rr.example',
                auto_start=False, max_cost=1, apply=apply, apply_tier='adopt', competitor_count=1,
                allow_remote_program_context=True)
            contract = fp.PurposeContract(name='calculator', purpose='Calculate means', authored=True)
            profile = {'name': 'calculator', 'summary': 'Calculate arithmetic means', 'stack': ['Python'],
                       'goals': ['Correct inputs'], 'opportunities': [{'need': 'Typed validation',
                       'url_search_query': 'numeric validation', 'repo_search_query': 'typed parser'}]}
            competitor_module = types.SimpleNamespace(
                research_competitors=lambda *a, **kw: {'verified': verified, 'target': 1, 'competitors': [],
                    'coverage_note': 'complete', 'research_complete': True}, report_lines=lambda *_: [])
            row = {'repo': {'fullName': 'example/typed-parser', 'htmlUrl': 'https://github.com/example/typed-parser'},
                   'finalScore': 80, 'safety': {'verdict': 'pass'}}
            replacements = {'resolve_repo_rewards_url': ('https://rr.example', 'fixture'),
                'resolve_program_input': ('calculator', 'local source'), 'resolve_project_dir': directory,
                '_model_args_for_repository': types.SimpleNamespace(_source_local_only=False),
                '_best_available_provider': object(),
                '_ensure_program_understanding': (contract, 'authored', True, 'fixture'),
                '_scout_program_profile': (profile, ''), '_competitors_module': competitor_module,
                'repo_rewards_search': [row]}
            for name, value in replacements.items():
                stack.enter_context(patch.object(ff, name, return_value=value))
            answer = {'benefit_score': 10, 'verdict': 'skip', 'how_it_helps': 'Not relevant to this numeric CLI',
                      'integration_note': 'No adoption needed', 'risks': []}
            calls = stack.enter_context(patch.object(ff, '_judge',
                side_effect=TimeoutError('timed out') if fail else None, return_value=answer))
            if qualify:
                stack.enter_context(patch.object(ff, '_qualifies_for_apply', return_value=True))
                stack.enter_context(patch.object(ff, '_apply_phase', side_effect=AssertionError(
                    'incomplete research must not reach apply')))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            code = ff._run_scout_impl(args)
            self.assertTrue(Path(directory, '_scout_report.json').exists())
            import json
            report = json.loads(Path(directory, '_scout_report.json').read_text(encoding='utf-8'))
            return code, report, calls.call_count

    def test_scout_returns_failure_after_saving_a_timed_out_candidate(self):
        code, report, count = self.exercise_scout()
        self.assertEqual(code, 2)
        self.assertEqual(count, 2)
        self.assertEqual(report['recommendations'][0]['evaluation_status'], 'incomplete')
        self.assertFalse(report['completion']['complete'])

    def test_source_shortfall_is_not_success_even_when_all_candidates_were_judged(self):
        code, report, count = self.exercise_scout(verified=0, fail=False)
        self.assertEqual(code, 2)
        self.assertEqual(count, 1)
        self.assertFalse(report['completion']['complete'])
        self.assertEqual(report['recommendations'][0]['evaluation_status'], 'complete')

    def test_full_source_and_evaluation_coverage_succeeds(self):
        code, report, count = self.exercise_scout(fail=False)
        self.assertEqual(code, 0)
        self.assertEqual(count, 1)
        self.assertTrue(report['completion']['complete'])




class ScoutCompletionGateTests(unittest.TestCase):
    def test_all_requested_sources_and_evaluations_are_required(self):
        self.assertTrue(hasattr(ff, '_scout_completion_status'))
        valid = {'verified': 25, 'research_complete': True}
        evaluations = [{'evaluation_complete': True}]
        self.assertTrue(ff._scout_completion_status(valid, evaluations, 25)['complete'])
        for research, rows in [({'verified': 17, 'research_complete': True}, evaluations),
                               ({'verified': 25, 'research_complete': False}, evaluations),
                               (valid, [{'evaluation_complete': False}]),
                               ({}, evaluations), (valid, [{}])]:
            with self.subTest(research=research, evaluations=rows):
                status = ff._scout_completion_status(research, rows, 25)
                self.assertFalse(status['complete'])
                self.assertTrue(status['incomplete_reasons'])
        self.assertTrue(ff._scout_completion_status(valid, [], 25)['complete'])

    def test_timeout_recovery_does_not_retry_a_nested_budget_refusal(self):
        wrapped = RuntimeError('all routes failed')
        refusal = ff.BudgetExceededError('budget exhausted')
        refusal.__cause__ = TimeoutError('earlier network timeout')
        wrapped.__cause__ = refusal
        with patch.object(ff, '_judge', side_effect=wrapped) as calls:
            with self.assertRaises(RuntimeError):
                ff._judge_scout_benefit(object(), 'candidate')
        self.assertEqual(calls.call_count, 1)


    def test_timeout_wrapper_cannot_hide_a_spending_policy_refusal(self):
        timeout = TimeoutError('provider wrapper timed out')
        timeout.__cause__ = ff.BudgetExceededError('policy must win')
        with patch.object(ff, '_judge', side_effect=timeout) as calls:
            with self.assertRaises(TimeoutError):
                ff._judge_scout_benefit(object(), 'candidate')
        self.assertEqual(calls.call_count, 1)


class ScoutJudgmentSchemaTests(unittest.TestCase):
    def test_missing_or_invalid_judgment_cannot_be_counted_as_complete(self):
        valid = {'benefit_score': 10, 'verdict': 'skip', 'how_it_helps': 'Already implemented',
                 'integration_note': '', 'risks': []}
        invalid = [{}, [], dict(valid, benefit_score=True), dict(valid, benefit_score=101),
                   dict(valid, verdict='unknown'), dict(valid, how_it_helps=''),
                   dict(valid, risks='none')]
        for field in valid:
            row = dict(valid)
            del row[field]
            invalid.append(row)
        for row in invalid:
            with self.subTest(row=row), patch.object(ff, '_judge', return_value=row):
                with self.assertRaises(ff.StructuredOutputShapeError):
                    ff._judge_scout_benefit(object(), 'candidate')

    def test_malformed_judgment_descends_the_existing_ladder_in_one_call(self):
        import os
        import flexfactor_rotation as rotation
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            routes = [rotation.Route.from_json({'id': n, 'backend': 'fixture', 'model': n,
                'pool': n, 'api': 'ollama', 'tier': rotation.LIGHT,
                'cost_class': rotation.LOCAL_UNLIMITED}) for n in ('bad', 'good')]
            store = rotation.StateStore(os.path.join(directory, 'routing.json'))
            store.update(lambda state: state['pools'].update({'good': {'calls': 1, 'last_used_at': 1.0}}))
            visited = []
            valid = {'benefit_score': 10, 'verdict': 'skip', 'how_it_helps': 'Already implemented',
                     'integration_note': '', 'risks': []}
            def factory(route):
                backend = Mock()
                def structured(*args, **kwargs):
                    visited.append(route.id)
                    return {} if route.id == 'bad' else dict(valid)
                backend.structured.side_effect = structured
                return backend
            provider = rotation.RotatingProvider(rotation.Rotator(rotation.Catalog(routes), store=store),
                                                  factory, tier=rotation.LIGHT)
            with patch.object(provider, 'structured_validated', wraps=provider.structured_validated) as routed:
                self.assertEqual(ff._judge_scout_benefit(provider, 'candidate'), valid)
            self.assertEqual(routed.call_count, 1)
            self.assertEqual(visited, ['bad', 'good'])


class ScoutReviewRegressions(unittest.TestCase):
    def test_runtime_and_sdk_timeouts_receive_the_bounded_retry(self):
        import httpx, openai, anthropic
        request = httpx.Request('GET', 'https://fixture.invalid')
        errors = [ff.StreamDeadlineError('stream timed out'),
                  openai.APITimeoutError(request=request),
                  anthropic.APITimeoutError(request=request),
                  httpx.ReadTimeout('read timeout', request=request)]
        valid = {'benefit_score': 10, 'verdict': 'skip', 'how_it_helps': 'Already implemented',
                 'integration_note': '', 'risks': []}
        for cause in errors:
            wrapped = RuntimeError('route failed')
            wrapped.__cause__ = cause
            with self.subTest(kind=type(cause).__name__), patch.object(
                    ff, '_judge', side_effect=[wrapped, valid]) as calls:
                self.assertEqual(ff._judge_scout_benefit(object(), 'candidate'), valid)
                self.assertEqual(calls.call_count, 2)

    def test_still_running_abandoned_worker_is_not_duplicated(self):
        error = ff._AbandonedCallTimeout('underlying worker is still running')
        with patch.object(ff, '_judge', side_effect=error) as calls:
            with self.assertRaises(ff._AbandonedCallTimeout):
                ff._judge_scout_benefit(object(), 'candidate')
        self.assertEqual(calls.call_count, 1)

    def test_incomplete_proposal_never_says_judged_unnecessary(self):
        row = ScoutEvaluationReceiptTests().failed_evaluation()
        report = ff._scout_contract.build_scout_structured_report('calculator', {}, [row])
        self.assertIn('INCOMPLETE', report['recommendations'][0]['rejection_reason'])
        self.assertIsNone(report['recommendations'][0]['benefit']['score'])
        self.assertIn('INCOMPLETE', report['proposals'][0]['rejection_reason'])
        self.assertEqual(report['proposals'][0]['evaluation_status'], 'incomplete')

    def test_truncated_adopt_prefix_cannot_authorize_an_evaluation(self):
        row = {'benefit_score': 90, 'verdict': 'adopt', 'how_it_helps': 'New numeric validation',
               'integration_note': 'Import parser', 'risks': []}
        partial = ff._ff_partial.attach_partial_meta(row, ff._ff_partial.PartialSalvageEvidence())
        with patch.object(ff, '_judge', return_value=partial):
            with self.assertRaises(ff.StructuredOutputShapeError):
                ff._judge_scout_benefit(object(), 'candidate')

    def test_incomplete_research_takes_precedence_over_apply_no_op(self):
        code, report, _ = ScoutEvaluationReceiptTests().exercise_scout(
            verified=0, fail=False, apply=True, qualify=True)
        self.assertEqual(code, 2)
        self.assertFalse(report['completion']['complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
