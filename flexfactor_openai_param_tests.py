"""max_tokens -> max_completion_tokens repair (run ledger iplay-20260823 #22/#117).

Newer api.openai.com models reject `max_tokens` with a 400 that names the
replacement. _chat_create must swap and retry INSIDE the same attempt (the
call's one paid round buys an answer, not parameter discovery), learn the
model, and leave every other error untouched. Runs offline.
"""

from __future__ import annotations

import unittest

import flexfactor as F

ERR = ("Error code: 400 - {'error': {'message': \"Unsupported parameter: "
       "'max_tokens' is not supported with this model. Use "
       "'max_completion_tokens' instead.\", 'type': 'invalid_request_error', "
       "'param': 'max_tokens', 'code': 'unsupported_parameter'}}")


class _Completions:
    def __init__(self, reject_max_tokens: bool):
        self.reject = reject_max_tokens
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.reject and "max_tokens" in kwargs:
            raise RuntimeError(ERR)
        return {"served": True}


class _Client:
    def __init__(self, reject_max_tokens: bool = True):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(reject_max_tokens)


class ChatCreateRepair(unittest.TestCase):
    def setUp(self):
        F._NEEDS_MAX_COMPLETION_TOKENS.clear()

    def test_named_400_swaps_and_retries_in_same_attempt(self):
        c = _Client()
        out = F._chat_create(c, model="chat-latest", max_tokens=4000, messages=[])
        self.assertEqual(out, {"served": True})
        calls = c.chat.completions.calls
        self.assertEqual(len(calls), 2)                      # one swap retry, same attempt
        self.assertIn("max_tokens", calls[0])
        self.assertNotIn("max_tokens", calls[1])
        self.assertEqual(calls[1]["max_completion_tokens"], 4000)
        self.assertIn("chat-latest", F._NEEDS_MAX_COMPLETION_TOKENS)

    def test_learned_model_swaps_before_the_first_call(self):
        F._NEEDS_MAX_COMPLETION_TOKENS.add("chat-latest")
        c = _Client()
        F._chat_create(c, model="chat-latest", max_tokens=4000, messages=[])
        calls = c.chat.completions.calls
        self.assertEqual(len(calls), 1)                      # no wasted round at all
        self.assertEqual(calls[0]["max_completion_tokens"], 4000)

    def test_unrelated_errors_are_not_retried(self):
        class _Boom(_Completions):
            def create(self, **kwargs):
                self.calls.append(dict(kwargs))
                raise RuntimeError("Error code: 429 - rate limited")
        c = _Client()
        c.chat.completions = _Boom(False)
        with self.assertRaises(RuntimeError):
            F._chat_create(c, model="gpt-4.1", max_tokens=100, messages=[])
        self.assertEqual(len(c.chat.completions.calls), 1)
        self.assertNotIn("gpt-4.1", F._NEEDS_MAX_COMPLETION_TOKENS)

    def test_models_that_accept_max_tokens_are_untouched(self):
        c = _Client(reject_max_tokens=False)
        F._chat_create(c, model="gpt-4.1", max_tokens=100, messages=[])
        self.assertEqual(c.chat.completions.calls[0]["max_tokens"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
