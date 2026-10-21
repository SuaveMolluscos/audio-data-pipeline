import unittest
from contextlib import redirect_stdout
from io import StringIO
import sys
from unittest.mock import patch

import analyzer


class AnalyzerIdentityTests(unittest.TestCase):
    def test_title_only_match_requires_manual_review(self):
        candidate = {
            "title": "Imagine",
            "artist": "Different Artist",
            "duration_sec": 180.0,
        }

        selected, match = analyzer.resolve_itunes_identity(
            [candidate], "Imagine", None
        )

        self.assertIs(selected, candidate)
        self.assertEqual(match.status, "ambiguous")
        self.assertIn("artist_not_provided", match.evidence)

    def test_tied_verified_candidates_require_manual_review(self):
        candidates = [
            {
                "title": "Imagine",
                "artist": "John Lennon",
                "duration_sec": 180.0,
            },
            {
                "title": "Imagine",
                "artist": "John Lennon",
                "duration_sec": 180.0,
            },
        ]

        selected, match = analyzer.resolve_itunes_identity(
            candidates, "Imagine", "John Lennon"
        )

        self.assertIs(selected, candidates[0])
        self.assertEqual(match.status, "ambiguous")
        self.assertIn("candidates_tied", match.evidence)

    def test_itunes_transport_failure_is_distinct_from_no_results(self):
        with patch.object(analyzer, "http_get_json", return_value=None):
            self.assertIsNone(analyzer.search_itunes("Imagine"))

        with patch.object(analyzer, "http_get_json", return_value={"results": []}):
            self.assertEqual(analyzer.search_itunes("Imagine"), [])

    def test_analysis_returns_error_when_itunes_is_unavailable(self):
        with patch.object(analyzer, "search_itunes", return_value=None):
            result = analyzer.analyze_song(title="Imagine", artist="John Lennon")

        self.assertEqual(result["error"], "itunes_unavailable")
        self.assertIsNone(result["data"])

    def test_cli_exposes_help_without_starting_network_requests(self):
        output = StringIO()
        with (
            patch.object(sys, "argv", ["analyzer.py", "--help"]),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            analyzer.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("query", output.getvalue())


if __name__ == "__main__":
    unittest.main()
