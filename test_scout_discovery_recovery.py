"""Regression cases from Scout run 35451305910, without network or model calls."""
import re
import unittest
from unittest.mock import patch
import flexfactor_competitors as fc


class ScoutDiscoveryRecoveryTests(unittest.TestCase):
    def research(self, responses, target=5, failed=()):
        calls, searches = [], []
        responses = iter(responses)
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                calls.append(prompt)
                response = next(responses, [])
                if isinstance(response, BaseException):
                    raise response
                return {'competitors': response}
            refs = re.findall(r'EVIDENCE_ID: (web-[a-z0-9]+)', prompt)
            return {'idea_title': 'Numeric input', 'what_it_does': 'Validate numbers',
                    'why_valuable': 'Clear input errors', 'evidence_basis': 'Source page',
                    'purpose_reason': 'Already delivered', 'accept': False, 'evidence_refs': refs[:1]}
        def search(query, **kwargs):
            searches.append(query)
            return [{'url': 'https://' + query.lower() + '.example/docs', 'title': query}], 'fixture', {}
        def fetch(url, title, **kwargs):
            name = url.split('//')[1].split('.')[0]
            if name in failed:
                raise OSError('source unavailable')
            return {'evidence_id': 'web-' + name, 'url': url, 'title': name,
                    'content': name + ' documents numeric operations', 'sha256': 'a'*64}
        with patch.object(fc, 'web_search', side_effect=search), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', side_effect=fetch):
            result = fc.research_competitors(judge, 'Numeric utility', 'Calculate means',
                                            target=target, log=lambda *_: None)
        return result, calls, searches

    @staticmethod
    def names(*numbers):
        return [{'name': f'Calculator{n}', 'kind': 'market', 'search_query': f'Calculator{n}'} for n in numbers]

    def test_partial_discovery_is_replenished_without_repeating_source_searches(self):
        result, calls, searches = self.research([
            self.names(0, 1, 2), self.names(2, 3, 4, 5), self.names(6, 7, 8, 9)], failed=('calculator0', 'calculator1'))
        self.assertEqual(result['verified'], 5)
        self.assertEqual(len(calls), 3)
        self.assertIn('Calculator0', calls[1])
        self.assertIn('ADDITIONAL', calls[1])
        self.assertEqual(len(searches), len(set(searches)))
        self.assertLessEqual(len(searches), 10)
        self.assertEqual(result['discovery_rounds'], 3)

    def test_no_novelty_stops_bounded_research_without_inventing_names(self):
        result, calls, searches = self.research([self.names(0, 1)] * 30)
        self.assertEqual(len(calls), 2)
        self.assertEqual(searches, ['Calculator0', 'Calculator1'])
        self.assertEqual(result['verified'], 2)
        self.assertIn('SHORTFALL', result['coverage_note'])

    def test_replenishment_failure_cannot_be_hidden_by_earlier_names(self):
        for failure in (TimeoutError('discovery timed out'), PermissionError('access denied')):
            with self.subTest(failure=type(failure).__name__):
                result, calls, searches = self.research([self.names(0, 1), failure])
                self.assertEqual(result['verified'], 2)
                self.assertFalse(result['research_complete'])
                self.assertEqual(len(calls), 2)
                self.assertTrue(any(type(failure).__name__ in reason
                                    for reason in result['incomplete_reasons']))

    def test_discovery_round_limit_does_not_grow_with_small_answers(self):
        result, calls, searches = self.research([self.names(n) for n in range(100)], target=25)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(searches), 4)
        self.assertEqual(result['verified'], 4)

    def test_lookalike_owner_cannot_donate_its_license_or_source(self):
        wrong = {'name': 'ClickHouse/ClickHouse', 'url': 'https://github.com/ClickHouse/ClickHouse', 'license': 'Apache-2.0'}
        self.assertIsNone(fc._attributable_repo('click', [wrong]))
        self.assertFalse(fc._name_related('click', wrong))
        canonical = {'name': 'pallets/click', 'url': 'https://github.com/pallets/click', 'license': 'BSD-3-Clause'}
        self.assertEqual(fc._attributable_repo('pallets/click', [wrong, canonical]), canonical)
        self.assertTrue(fc._name_related('click', canonical))

    def test_aliases_of_one_repository_are_not_two_competitors(self):
        def search(query, **kwargs):
            return [{'url': 'https://github.com/tj/commander.js', 'title': 'commander.js'}], 'fixture', {}
        def fetch(url, title, **kwargs):
            return {'evidence_id': 'web-abc123', 'url': url, 'title': title,
                    'content': 'commander.js is a command-line framework', 'sha256': 'a'*64}
        names = [{'name': name, 'search_query': name} for name in ['commander', 'commander.js']]
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                return {'competitors': names}
            return {'idea_title': 'Options', 'what_it_does': 'Parse CLI options',
                    'why_valuable': 'Validate user input', 'evidence_basis': 'Source page',
                    'purpose_reason': 'Already present', 'accept': False, 'evidence_refs': ['web-abc123']}
        with patch.object(fc, 'web_search', side_effect=search), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', side_effect=fetch) as fetched:
            result = fc.research_competitors(judge, 'CLI', 'Calculate means', target=2, log=lambda *_: None)
        self.assertEqual(len(result['competitors']), 1)
        self.assertEqual(fetched.call_count, 1, 'aliases must not spend the source budget twice')



    def test_aliases_do_not_crowd_unique_reward_candidates_out_of_fetch_budget(self):
        aliases = [f'Alias{number}' for number in range(5)]
        fetched = []
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                return {'competitors': [{'name': name, 'search_query': name} for name in aliases]}
            return {'idea_title': 'Inputs', 'what_it_does': 'Validate input',
                    'why_valuable': 'Clear errors', 'evidence_basis': 'Fetched page',
                    'purpose_reason': 'Already present', 'accept': False,
                    'evidence_refs': re.findall(r'EVIDENCE_ID: (web-[a-z0-9]+)', prompt)[:1]}
        def fetch(url, title, **kwargs):
            fetched.append(url)
            return {'evidence_id': 'web-' + str(len(fetched)), 'url': url, 'title': title,
                    'content': ' '.join(aliases) + ' UniqueParser documents numeric input', 'sha256': 'a'*64}
        shared = 'https://github.com/example/shared-parser'
        unique = 'https://github.com/UniqueParser/UniqueParser'
        rr = lambda query: [{'repo': {'fullName': 'UniqueParser/UniqueParser',
                                      'htmlUrl': unique, 'licenseSpdx': 'MIT', 'stars': 0}}]
        with patch.object(fc, 'web_search', return_value=([{'url': shared}], 'fixture', {})), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', side_effect=fetch):
            result = fc.research_competitors(judge, 'CLI', 'Compute means', target=2,
                                            rr_search=rr, log=lambda *_: None)
        self.assertEqual(result['verified'], 2)
        self.assertEqual(sorted(fetched), sorted([shared, unique]))



    def test_qualified_repository_identity_keeps_both_segments_and_punctuation(self):
        for expected, unrelated in [('foo/bar', 'fo/obar'), ('a-b/c', 'a/b-c'), ('a-b/c', 'ab/c')]:
            row = {'name': unrelated, 'url': 'https://github.com/' + unrelated, 'license': 'MIT'}
            with self.subTest(expected=expected, unrelated=unrelated):
                self.assertIsNone(fc._attributable_repo(expected, [row]))
                self.assertFalse(fc._name_related(expected, row))
        row = {'name': 'Foo/Bar', 'license': 'MIT'}
        self.assertEqual(fc._attributable_repo('foo/bar', [row]), row)
        self.assertTrue(fc._name_related('FOO/BAR', row))

    def test_canonical_alias_preserves_repository_metadata_and_source_inspection(self):
        inspected = []
        url = 'https://github.com/tj/commander.js'
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                return {'competitors': [{'name': 'Commander', 'search_query': 'Commander'}]}
            return {'idea_title': 'Options', 'what_it_does': 'Parse options', 'why_valuable': 'Input errors',
                    'evidence_basis': 'Source page', 'purpose_reason': 'Already supported', 'accept': False,
                    'evidence_refs': ['web-fixture']}
        def inspect(row):
            inspected.append(row['name'])
            return {'source_inspection_ok': True, 'license_file_found': True, 'license_families': ['mit'],
                    'source_documents': [{'evidence_id': 'code-fixture', 'content': 'parse()', 'sha256': 'a'*64}]}
        page = dict(evidence_id='web-fixture', url=url, title='commander.js',
                    content='Commander.js parses options.', sha256='a'*64)
        rr = lambda query: [{'repo': {'fullName': 'tj/commander.js', 'htmlUrl': url,
                                      'licenseSpdx': 'MIT', 'stars': 0}}]
        with patch.object(fc, 'web_search', return_value=([page], 'fixture', {})), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', return_value=page) as fetched:
            result = fc.research_competitors(judge, 'CLI', 'Parse options', target=1,
                                            rr_search=rr, source_inspector=inspect, log=lambda *_: None)
        self.assertEqual(inspected, ['tj/commander.js'])
        self.assertEqual(fetched.call_count, 1)
        row = result['competitors'][0]
        self.assertEqual(row['kind'], 'oss')
        self.assertEqual(row['license'], 'MIT')
        self.assertTrue(row['source_inspection_required'])
        self.assertEqual(result['verified'], 1)
        self.assertIn('Commander', row['discovery_aliases'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
