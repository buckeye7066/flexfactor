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
        answer = {'benefit_score': 70, 'how_it_helps': 'Typed numeric validation',
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

    def exercise_scout(self, *, verified=1, fail=True):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            Path(directory, 'app.py').write_text('print(1)\n', encoding='utf-8')
            args = types.SimpleNamespace(program=directory, repo_rewards_url='https://rr.example',
                auto_start=False, max_cost=1, apply=False, apply_tier='adopt', competitor_count=1,
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
            answer = {'benefit_score': 10, 'how_it_helps': 'Not relevant to this numeric CLI',
                      'integration_note': 'No adoption needed', 'risks': []}
            calls = stack.enter_context(patch.object(ff, '_judge',
                side_effect=TimeoutError('timed out') if fail else None, return_value=answer))
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
