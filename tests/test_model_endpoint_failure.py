"""Recognising Hermes core's endpoint failure message (adapter.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _adapter_test_support import HERMES_AGENT_AVAILABLE, SKIP_REASON, load_mixin_module  # noqa: E402


@unittest.skipUnless(HERMES_AGENT_AVAILABLE, SKIP_REASON)
class TestClassifyModelEndpointFailure(unittest.TestCase):
    def setUp(self):
        self.adapter = load_mixin_module("adapter")

    def test_auth_failure(self):
        text = "API call failed after 3 retries: PermissionDeniedError: Error code: 403"
        self.assertEqual(self.adapter.classify_model_endpoint_failure(text), "auth")

    def test_unavailable_failure(self):
        text = "API call failed after 3 retries: APIConnectionError: Connection error."
        self.assertEqual(self.adapter.classify_model_endpoint_failure(text), "unavailable")

    def test_normal_answers_are_left_alone(self):
        for text in ("Hi! How can I help?", "A 403 error means that...", ""):
            self.assertIsNone(self.adapter.classify_model_endpoint_failure(text))

    def test_texts_name_the_model_provider(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from user_texts import user_text

        keys = self.adapter.MODEL_ENDPOINT_FAILURE_KEYS
        self.assertIn("model provider rejected the request (authentication error)",
                      user_text(keys["auth"]))
        self.assertIn("model provider is not reachable", user_text(keys["unavailable"]))


if __name__ == "__main__":
    unittest.main()
