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



    def research_external_identities(self, names, urls, rewards=()):
        import hashlib
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                return {'competitors': names}
            return {'idea_title': 'Inputs', 'what_it_does': 'Validate numeric inputs',
                    'why_valuable': 'Clear errors', 'evidence_basis': 'Fetched page',
                    'purpose_reason': 'Already present', 'accept': False,
                    'evidence_refs': re.findall(r'EVIDENCE_ID: (web-[a-z0-9]+)', prompt)[:1]}
        def search(query, **kwargs):
            return [{'url': urls.get(query, 'https://fallback.example/docs'), 'title': 'Product documentation'}], 'fixture', {}
        def fetch(url, title, **kwargs):
            if url == 'https://[':
                raise ValueError('Invalid IPv6 URL')
            return {'evidence_id': 'web-' + hashlib.sha256(url.encode()).hexdigest()[:12],
                    'url': url, 'title': title, 'sha256': 'a'*64,
                    'content': ' '.join(row['name'] for row in names) + ' document numeric operations.'}
        with patch.object(fc, 'web_search', side_effect=search) as searched, \
                patch.object(fc, 'github_repo_search', return_value=[]), \
                patch.object(fc, 'fetch_evidence_document', side_effect=fetch):
            result = fc.research_competitors(judge, 'Numeric utility', 'Calculate means',
                        target=2, rr_search=(lambda query: list(rewards)) if rewards else None,
                        log=lambda *_: None)
        return result, [call.args[0] for call in searched.call_args_list]

    def test_shared_web_pages_and_query_selected_products_are_not_automatic_aliases(self):
        names = [{'name': name, 'search_query': name} for name in ['AlphaTool', 'BetaTool']]
        pairs = [('https://vendor.example/', 'https://vendor.example/'),
                 ('https://vendor.example/comparison', 'https://vendor.example/comparison'),
                 ('https://vendor.example/product?id=alpha', 'https://vendor.example/product?id=beta'),
                 ('https://github.com/', 'https://github.com/')]
        for alpha, beta in pairs:
            with self.subTest(alpha=alpha, beta=beta):
                result, _ = self.research_external_identities(names, {'AlphaTool': alpha, 'BetaTool': beta})
                self.assertEqual(result['verified'], 2)
                self.assertEqual({row['name'] for row in result['competitors']}, {'AlphaTool', 'BetaTool'})

    def test_malformed_reward_url_is_a_named_failure_not_a_gate_crash(self):
        names = [{'name': 'AlphaTool', 'search_query': 'AlphaTool'}]
        rewards = [{'repo': {'fullName': 'Malformed/Metadata', 'htmlUrl': 'https://[', 'licenseSpdx': 'MIT'}}]
        try:
            result, _ = self.research_external_identities(names, {'AlphaTool': 'https://alpha.example/'}, rewards)
        except ValueError as exc:
            self.fail('Malformed external metadata aborted research: ' + str(exc))
        self.assertEqual(result['verified'], 1)
        self.assertTrue(any(key.startswith('canonical-url:') and 'ValueError' in str(reason)
                            for key, reason in result['sources_skipped'].items()))
        self.assertTrue(any(row['name'] == 'AlphaTool' for row in result['competitors']))

    def test_non_ascii_discovery_names_are_retained_even_with_a_shared_latin_query(self):
        names = [{'name': name, 'search_query': 'collaboration'} for name in ['\u98de\u4e66', '\u9489\u9489']]
        result, searches = self.research_external_identities(names, {'collaboration': 'https://vendor.example/docs'})
        self.assertEqual(searches, ['collaboration', 'collaboration'])
        self.assertEqual({row['name'] for row in result['competitors']}, {row['name'] for row in names})
        self.assertEqual(len(result['competitors']), 2)



    def test_qualified_discovery_names_preserve_repository_boundaries(self):
        for first, second in [('foo/bar', 'fo/obar'), ('a-b/c', 'a/b-c'), ('a-b/c', 'ab/c')]:
            with self.subTest(first=first, second=second):
                names = [{'name': name, 'search_query': name} for name in [first, second]]
                result, searches = self.research_external_identities(names, {name: 'https://github.com/' + name for name in [first, second]})
                self.assertEqual(set(searches), {first, second})
                self.assertEqual({row['name'] for row in result['competitors']}, {first, second})

    def test_international_identity_cannot_be_proven_from_its_ascii_fragment(self):
        first, second = 'Calc\u4e00', 'Calc\u4e8c'
        generic = {'url': 'https://calc.example/docs', 'title': 'Calc', 'content': 'Calc handles numbers.'}
        self.assertFalse(fc._document_matches_competitor(first, generic))
        matching = dict(generic, title=first, content=first + ' handles numbers.')
        self.assertTrue(fc._document_matches_competitor(first, matching))
        self.assertFalse(fc._document_matches_competitor(second, matching))

    def test_complete_non_ascii_name_on_a_fetched_page_is_attributable(self):
        name = '\u98de\u4e66'
        self.assertTrue(fc._document_matches_competitor(name, {
            'url': 'https://vendor.example/docs', 'title': name, 'content': name + ' documents its features.'}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
