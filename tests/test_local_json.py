"""Narrow framing recovery never fills or rewrites model semantics."""
import unittest
from splice.local_intent import _decode_json


class LocalJsonTests(unittest.TestCase):
    def test_records_exactly_one_redundant_closing_brace(self):
        corrections = []
        self.assertEqual(_decode_json('{"intent":{},"unresolved_constraints":[]}}', corrections),
                         {"intent": {}, "unresolved_constraints": []})
        self.assertEqual(corrections, ['removed_one_redundant_closing_brace'])

    def test_rejects_other_trailing_or_ambiguous_content(self):
        for text in ('{}{}', '{} extra', '{"a":1,"a":2}', '{"a":NaN}', '{} }}', '{"a":'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _decode_json(text)

    def test_braces_inside_strings_are_preserved(self):
        corrections = []
        self.assertEqual(_decode_json('{"value":"{}"}', corrections), {"value": '{}'})
        self.assertEqual(corrections, [])
