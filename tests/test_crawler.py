import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import crawler
import db


class CrawlerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "tracks.sqlite")
        db.reset_database(self.db_path, confirm=True)

    def tearDown(self):
        crawler.stop_event.clear()
        db.close_connection(self.db_path)
        self.temp_dir.cleanup()

    def test_run_clears_previous_stop_request(self):
        crawler.stop_event.set()

        result = crawler.run(self.db_path, workers=1)

        self.assertEqual(result, 0)
        self.assertFalse(crawler.stop_event.is_set())

    def test_logging_setup_is_idempotent(self):
        crawler._setup_logging("INFO", None)
        first_count = len(crawler.logger.handlers)
        crawler._setup_logging("INFO", None)

        self.assertEqual(first_count, 1)
        self.assertEqual(len(crawler.logger.handlers), 1)

    def test_transient_itunes_failure_is_retried(self):
        item = {"payload": {"title": "Track", "artist": "Artist"}}
        result = {"data": None, "error": "itunes_unavailable"}

        with patch.object(crawler, "analyze_song", return_value=result):
            status = crawler._process_song(item, self.db_path)

        self.assertEqual(status, "retry:itunes_unavailable")

    def test_run_stops_maintenance_thread_when_workers_finish(self):
        db.enqueue(
            item_type="song",
            payload={"title": "Track", "artist": "Artist"},
            dedupe_key="song:artist|track",
            db_path=self.db_path,
        )
        maintenance_stopped = threading.Event()

        def maintenance_loop(_db_path, interval=60):
            crawler.stop_event.wait(5)
            maintenance_stopped.set()

        with (
            patch.object(crawler, "_maintenance_loop", side_effect=maintenance_loop),
            patch.object(crawler, "worker"),
        ):
            result = crawler.run(self.db_path, workers=1)

        self.assertEqual(result, 0)
        self.assertTrue(maintenance_stopped.is_set())
        self.assertTrue(crawler.stop_event.is_set())

    def test_run_rejects_out_of_range_configuration(self):
        with self.assertRaises(ValueError):
            crawler.run(self.db_path, workers=33)
        with self.assertRaises(ValueError):
            crawler.run(self.db_path, lease_seconds=59)

    def test_enqueue_seed_count_excludes_duplicates(self):
        seed = "Track|Artist"

        first = crawler._enqueue_seeds([seed], self.db_path)
        second = crawler._enqueue_seeds([seed], self.db_path)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)


if __name__ == "__main__":
    unittest.main()
