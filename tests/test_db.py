import tempfile
import unittest
from pathlib import Path

import db


class DatabasePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "tracks.sqlite")
        db.reset_database(self.db_path, confirm=True)

    def tearDown(self):
        db.close_connection(self.db_path)
        self.temp_dir.cleanup()

    def _result(self, genre, duration, status="verified", confidence=0.95):
        return {
            "data": {
                "title": "Track",
                "artist": "Artist",
                "genre": genre,
                "duration_sec": duration,
                "identity": {
                    "canonical_id": "local:test",
                    "canonical_id_type": "local_fingerprint",
                    "resolution_status": status,
                    "resolution_confidence": confidence,
                },
                "provenance": {"analyzer_version": "test"},
                "features": None,
                "tags": [],
            },
            "ok": status == "verified",
            "requires_review": status == "ambiguous",
            "completeness": {"basic_metadata": True},
        }

    def test_partial_analysis_preserves_previous_values_and_creates_history(self):
        first_id = db.insert_from_analysis(
            self._result("Rock", 200.0), self.db_path
        )
        second_id = db.insert_from_analysis(
            self._result(None, None, status="ambiguous", confidence=0.4),
            self.db_path,
        )

        song = db.get_song(first_id, self.db_path)
        connection = db.get_connection(self.db_path)
        versions = connection.execute(
            "SELECT COUNT(*) FROM song_analysis_versions WHERE song_id = ?",
            (first_id,),
        ).fetchone()[0]

        self.assertEqual(first_id, second_id)
        self.assertEqual(song["genre"], "Rock")
        self.assertEqual(song["duration_sec"], 200.0)
        self.assertEqual(versions, 2)

    def test_partial_analysis_preserves_text_when_new_text_is_empty(self):
        first = self._result("Rock", 200.0)
        first["data"]["title"] = "Original title"
        first["data"]["artist"] = "Original artist"
        song_id = db.insert_from_analysis(first, self.db_path)

        second = self._result(None, None, status="ambiguous", confidence=0.4)
        second["data"]["title"] = ""
        second["data"]["artist"] = ""
        db.insert_from_analysis(second, self.db_path)

        song = db.get_song(song_id, self.db_path)
        self.assertEqual(song["title"], "Original title")
        self.assertEqual(song["artist_display"], "Original artist")

    def test_empty_analysis_is_not_persisted(self):
        self.assertIsNone(db.insert_from_analysis({"data": None}, self.db_path))
        self.assertEqual(db.count_songs(self.db_path), 0)

    def test_expired_work_reaches_failed_after_attempt_limit(self):
        queue_id = db.enqueue(
            item_type="song",
            payload={"title": "Track", "artist": "Artist"},
            dedupe_key="song:artist|track",
            db_path=self.db_path,
        )
        item = db.pop_next("worker", lease_seconds=-1, db_path=self.db_path)

        self.assertEqual(item["id"], queue_id)
        self.assertEqual(db.requeue_expired(self.db_path, max_attempts=1), 1)
        self.assertEqual(db.queue_stats(self.db_path)["failed"], 1)

    def test_enqueue_completed_dedupe_returns_none(self):
        first = db.enqueue(
            item_type="artist",
            payload={"mbid": "artist-1"},
            dedupe_key="artist:artist-1",
            db_path=self.db_path,
        )
        item = db.pop_next("worker", db_path=self.db_path)
        db.mark_done(item["id"], worker_id="worker", db_path=self.db_path)

        duplicate = db.enqueue(
            item_type="artist",
            payload={"mbid": "artist-1"},
            dedupe_key="artist:artist-1",
            dedupe_completed=True,
            db_path=self.db_path,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(db.queue_stats(self.db_path)["done"], 1)


if __name__ == "__main__":
    unittest.main()
