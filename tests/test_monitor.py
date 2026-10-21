import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from io import StringIO
from contextlib import redirect_stdout

import db
from monitor import MonitorConfig, ServiceMonitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "tracks.sqlite")
        db.reset_database(self.db_path, confirm=True)

    def tearDown(self):
        db.close_connection(self.db_path)
        self.temp_dir.cleanup()

    def test_snapshot_reads_database_state(self):
        db.enqueue(
            item_type="song",
            payload={"title": "Track", "artist": "Artist"},
            dedupe_key="song:artist|track",
            db_path=self.db_path,
        )
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))

        snapshot = monitor.snapshot()

        self.assertEqual(snapshot["stats"]["queue"]["pending"], 1)
        self.assertEqual(snapshot["recent_queue"][0]["item_type"], "song")

    def test_database_report_includes_core_tables(self):
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))

        report = monitor.database_report()

        self.assertIn("songs", report)
        self.assertIn("song_analysis_versions", report)
        self.assertIn("queue", report)

    def test_configuration_report_does_not_expose_api_key(self):
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))

        report = monitor.configuration_report()

        self.assertIn("Last.fm API key", report)
        self.assertNotIn("secret", report)

    def test_configuration_report_reads_systemd_environment_file(self):
        env_path = Path(self.temp_dir.name) / "service.env"
        env_path.write_text(
            "LASTFM_API_KEY=secret\n"
            "ANALYZER_LOG_FILE=/var/log/music-crawler/analyzer.log\n",
            encoding="utf-8",
        )
        monitor = ServiceMonitor(
            MonitorConfig(db_path=self.db_path, env_file=str(env_path))
        )

        report = monitor.configuration_report()

        self.assertIn("Last.fm API key     : configurada", report)
        self.assertIn("analyzer.log", report)
        self.assertNotIn("secret", report)

    def test_health_and_backup_reports_are_readable(self):
        backup_dir = Path(self.temp_dir.name) / "backups"
        backup_dir.mkdir()
        (backup_dir / "tracks-20260921T010000Z.sqlite").touch()
        monitor = ServiceMonitor(
            MonitorConfig(db_path=self.db_path, backup_dir=str(backup_dir))
        )

        health = monitor.health_report()
        backups = monitor.backup_report()
        failures = monitor.failed_report()

        self.assertIn("Base de datos", health)
        self.assertIn("tracks-20260921T010000Z.sqlite", backups)
        self.assertIn("No hay trabajos fallidos", failures)

    def test_runtime_configuration_and_queue_report(self):
        runtime_path = Path(self.temp_dir.name) / "runtime.env"
        runtime_path.write_text(
            "CRAWLER_WORKERS=4\nCRAWLER_LEASE_SECONDS=900\n",
            encoding="utf-8",
        )
        db.enqueue(
            item_type="song",
            payload={"title": "Track", "artist": "Artist"},
            priority=12.0,
            dedupe_key="song:artist|track",
            db_path=self.db_path,
        )
        monitor = ServiceMonitor(
            MonitorConfig(
                db_path=self.db_path,
                runtime_env_file=str(runtime_path),
            )
        )

        runtime = monitor.runtime_configuration()
        queue = monitor.queue_report()

        self.assertEqual(runtime, {"workers": 4, "lease_seconds": 900})
        self.assertIn("Track - Artist", queue)

    def test_invalid_runtime_configuration_uses_defaults(self):
        runtime_path = Path(self.temp_dir.name) / "runtime.env"
        runtime_path.write_text(
            "CRAWLER_WORKERS=invalid\nCRAWLER_LEASE_SECONDS=invalid\n",
            encoding="utf-8",
        )
        monitor = ServiceMonitor(
            MonitorConfig(
                db_path=self.db_path,
                runtime_env_file=str(runtime_path),
            )
        )

        self.assertEqual(
            monitor.runtime_configuration(),
            {"workers": 1, "lease_seconds": 600},
        )

    def test_export_database_and_csv(self):
        db.insert_song(
            {
                "title": "Track",
                "artist": "Artist",
                "identity": {
                    "canonical_id": "local:export",
                    "canonical_id_type": "local_fingerprint",
                    "resolution_status": "verified",
                },
            },
            self.db_path,
        )
        export_dir = Path(self.temp_dir.name) / "exports"
        sqlite_path = export_dir / "export.sqlite"
        csv_path = export_dir / "songs.csv"
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))

        with patch("builtins.input", side_effect=[str(sqlite_path), ""]):
            monitor.export_database()
        with patch("builtins.input", side_effect=[str(csv_path), ""]):
            monitor.export_songs_csv()

        exported = db.get_connection(str(sqlite_path))
        self.assertEqual(
            exported.execute("SELECT COUNT(*) FROM songs").fetchone()[0], 1
        )
        self.assertIn("Track", csv_path.read_text(encoding="utf-8-sig"))
        db.close_connection(str(sqlite_path))

    def test_summary_is_machine_readable(self):
        monitor = ServiceMonitor(
            MonitorConfig(db_path=self.db_path, use_color=False)
        )

        summary = monitor.summary()

        self.assertIn("service=", summary)
        self.assertIn("songs=0", summary)
        self.assertIn("pending=0", summary)

    def test_song_detail_shows_metadata_features_and_history(self):
        result = {
            "data": {
                "title": "Track",
                "artist": "Artist",
                "features": {
                    "method": "librosa",
                    "statistics": {"tempo_bpm": {"mean": 120.0}},
                },
                "identity": {
                    "canonical_id": "local:detail",
                    "canonical_id_type": "local_fingerprint",
                    "resolution_status": "verified",
                    "resolution_confidence": 0.99,
                },
                "provenance": {"analyzer_version": "test"},
                "tags": [{"name": "rock", "weight": 80}],
            },
            "ok": True,
            "requires_review": False,
            "completeness": {"audio_features": True},
        }
        song_id = db.insert_from_analysis(result, self.db_path)
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))
        output = StringIO()

        with patch("builtins.input", side_effect=[str(song_id), ""]):
            with redirect_stdout(output):
                monitor.song_detail()

        rendered = output.getvalue()
        self.assertIn("librosa", rendered)
        self.assertIn("tempo_bpm", rendered)
        self.assertIn("HISTORIAL DE ANALISIS", rendered)

    def test_edit_pending_seed_rejects_non_song_items(self):
        queue_id = db.enqueue(
            item_type="artist",
            payload={"mbid": "artist-1"},
            dedupe_key="artist:artist-1",
            db_path=self.db_path,
        )
        monitor = ServiceMonitor(MonitorConfig(db_path=self.db_path))
        output = StringIO()

        with patch("builtins.input", side_effect=[str(queue_id), ""]), redirect_stdout(output):
            monitor.edit_pending_seed()

        row = db.get_connection(self.db_path).execute(
            "SELECT item_type, payload_json FROM queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        self.assertEqual(row["item_type"], "artist")
        self.assertEqual(row["payload_json"], '{"mbid": "artist-1"}')
        self.assertIn("tipo canción", output.getvalue())


if __name__ == "__main__":
    unittest.main()
