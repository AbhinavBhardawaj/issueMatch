import unittest
from app.candidates.extractor import CandidateKind, extract_candidate
class ExtractorTests(unittest.TestCase):
    def test_claims_and_approach(self):
        self.assertFalse(extract_candidate("ASSIGN this issue to me!").is_candidate)
        self.assertEqual(extract_candidate("ASSIGN this issue to me!").kind, CandidateKind.CLAIM_ONLY)
        for text in ("Please assign this to me", "I would like to work on this issue", "I will work on this issue", "I want this issue", "I can take this issue", "Can I work on this?"):
            self.assertEqual(extract_candidate(text).kind, CandidateKind.CLAIM_ONLY)
        self.assertTrue(extract_candidate("ASSIGN this issue to me. I'll modify src/a.py.").is_candidate)
        self.assertFalse(extract_candidate("I want to take this up").is_candidate)
        result=extract_candidate("I'd like to work on this. I'll modify src/a.py.")
        self.assertTrue(result.is_candidate); self.assertIn("modify", result.approach_text)
        self.assertTrue(extract_candidate("if token.expired: return 401 else continue validation").is_candidate)
    def test_ordinary_and_empty_ignored(self):
        self.assertFalse(extract_candidate("Thanks!").is_candidate); self.assertFalse(extract_candidate(" ").is_candidate)
