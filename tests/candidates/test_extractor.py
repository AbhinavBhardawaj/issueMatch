import unittest
from app.candidates.extractor import extract_candidate
class ExtractorTests(unittest.TestCase):
    def test_claims_and_approach(self):
        self.assertTrue(extract_candidate("ASSIGN this issue to me!").is_candidate)
        self.assertTrue(extract_candidate("I want to take this up").is_candidate)
        result=extract_candidate("I'd like to work on this. I'll modify src/a.py.")
        self.assertTrue(result.is_candidate); self.assertIn("modify", result.approach_text)
    def test_ordinary_and_empty_ignored(self):
        self.assertFalse(extract_candidate("Thanks!").is_candidate); self.assertFalse(extract_candidate(" ").is_candidate)
