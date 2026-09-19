"""Verify Scout's bounded candidate allowance covers failed source checks."""
import re
import unittest
from unittest.mock import patch
import flexfactor_competitors as fc


class EvidenceCandidateBudgetTests(unittest.TestCase):
    def test_evidence_overflow_is_named_before_the_final_verified_selection(self):
        names = [f'Calculator{number}' for number in range(10)]
        discovered, fetched = [], []
        def judge(system, prompt, schema):
            if schema is fc.DISCOVERY_SCHEMA:
                limit = int(re.search(r'Name up to (\d+)', prompt).group(1))
                discovered.append(limit)
                return {'competitors': [{'name': n, 'search_query': n} for n in names[:limit]]}
            refs = re.findall(r'EVIDENCE_ID: (web-[a-z0-9]+)', prompt)
            return {'idea_title': 'Input errors', 'what_it_does': 'Reject invalid input',
                    'why_valuable': 'Clear feedback', 'evidence_basis': 'Fetched documentation',
                    'purpose_reason': 'Already supported', 'accept': False, 'evidence_refs': refs[:1]}
        def search(query, **kwargs):
            return [{'url': f'https://{query.lower()}.example/docs', 'title': query}], 'fixture', {}
        def fetch(url, title, **kwargs):
            name = url.split('//')[1].split('.')[0]
            fetched.append(name)
            if int(name.replace('calculator', '')) < 5: raise OSError('source unavailable')
            return {'evidence_id': 'web-'+name, 'url': url, 'title': name,
                    'content': name+' documents numeric inputs', 'sha256': 'a'*64}
        with patch.object(fc, 'web_search', side_effect=search), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', side_effect=fetch):
            result = fc.research_competitors(judge, 'numeric utility', 'Calculate means',
                                            target=5, log=lambda *_: None)
        self.assertEqual(result['verified'], 5)
        self.assertEqual(discovered, [10])
        self.assertEqual(len(fetched), 10)
        self.assertEqual(len(set(fetched)), 10)
        self.assertTrue(all(c['evidence_status'] == 'verified' for c in result['competitors']))
        self.assertTrue(all('Calculator' in c['name'] for c in result['competitors']))
        self.assertGreaterEqual(len(result['sources_skipped']), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
