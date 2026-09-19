"""Keep invalid idea evidence inside the existing provider routing boundary."""
import copy
import os
import tempfile
import unittest
from unittest.mock import Mock
import flexfactor as ff
import flexfactor_competitors as fc
import flexfactor_rotation as rotation


class ScoutIdeaRoutingTests(unittest.TestCase):
    def test_invalid_reference_tries_another_route_in_the_same_call(self):
        self.assertTrue(hasattr(ff, '_scout_idea_call'))
        good = dict(idea_title='Exact input validation', what_it_does='Reject invalid ports',
                    why_valuable='Safe CLI input', evidence_basis='The fetched source',
                    purpose_reason='Match the documented input contract', accept=False,
                    evidence_refs=['web-fixture'])
        bad = dict(good, evidence_refs=['EVIDENCE_ID_1'])
        visited = []
        with tempfile.TemporaryDirectory() as directory:
            routes = [rotation.Route.from_json(dict(id=n, backend='fixture', model=n,
                pool=n, api='ollama', tier=rotation.STRONG, cost_class=rotation.LOCAL_UNLIMITED))
                for n in ('bad', 'good')]
            store = rotation.StateStore(os.path.join(directory, 'routing.json'))
            store.update(lambda state: state['pools'].update({'good': {'calls': 1, 'last_used_at': 1.0}}))
            def factory(route):
                backend = Mock()
                def structured(*args, **kwargs):
                    visited.append(route.id)
                    return copy.deepcopy(bad if route.id == 'bad' else good)
                backend.structured.side_effect = structured
                return backend
            provider = rotation.RotatingProvider(rotation.Rotator(rotation.Catalog(routes), store=store),
                                                 factory, tier=rotation.STRONG)
            def validate(data):
                result, reason = fc._normalize_idea(data, 'fixture', valid_evidence_refs={'web-fixture'})
                if reason:
                    raise ValueError(reason)
                return result
            result = ff._scout_idea_call(provider, fc.IDEA_SYSTEM, 'provided source', fc.IDEA_SCHEMA,
                                        validator=validate)
        self.assertEqual(result, good)
        self.assertEqual(visited, ['bad', 'good'])

    def test_pipeline_validator_rejects_forged_ids_before_accepting_an_answer(self):
        from unittest.mock import patch
        validated = []
        good = dict(idea_title='Input validation', what_it_does='Reject invalid numbers',
                    why_valuable='Useful error messages', evidence_basis='Fetched documentation',
                    purpose_reason='Already delivered', accept=False, evidence_refs=['web-fixture'])
        def judge(system, prompt, schema):
            self.assertIs(schema, fc.DISCOVERY_SCHEMA)
            return {'competitors': [{'name': 'Calculator', 'search_query': 'Calculator'}]}
        def author(system, prompt, schema, *, validator):
            validated.append(prompt)
            for bad in (dict(good, evidence_refs=['web-forged']), dict(good, accept='false'),
                        dict(good, evidence_refs='web-fixture'), {}):
                with self.assertRaises(ValueError):
                    validator(bad)
            return validator(good)
        doc = {'evidence_id': 'web-fixture', 'url': 'https://calculator.example/docs',
               'title': 'Calculator', 'content': 'Calculator validates numeric inputs.', 'sha256': 'a'*64}
        with patch.object(fc, 'web_search', return_value=([doc], 'fixture', {})), \
             patch.object(fc, 'github_repo_search', return_value=[]), \
             patch.object(fc, 'fetch_evidence_document', return_value=doc):
            result = fc.research_competitors(judge, 'numeric CLI', 'Compute a mean', target=1,
                                            author_validated=author, log=lambda *_: None)
        self.assertEqual(len(validated), 1)
        self.assertTrue(result['research_complete'])
        self.assertEqual(result['verified'], 1)
        self.assertIn('EXACT EVIDENCE IDENTIFIERS', validated[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
