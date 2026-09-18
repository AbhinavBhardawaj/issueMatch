import unittest
from app.github.events import MalformedGitHubEvent, normalize_issue_comment_created

def payload(): 
    return {"action":"created", "repository":{"name":"repo","owner":{"login":"org"},"default_branch":"main"}, "issue":{"number":4,"title":"T","body":None}, "comment":{"id":9,"body":"hello","user":{"login":"u","id":2}}, "installation":{"id":3}}

class EventTests(unittest.TestCase):
    def test_normalizes(self): 
        self.assertEqual(normalize_issue_comment_created(payload(), "x").repository_owner, "org")

    def test_malformed(self):
        with self.assertRaises(MalformedGitHubEvent): normalize_issue_comment_created({"action":"created"}, "x")
