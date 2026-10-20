# audio-data-pipeline
A fault-tolerant ETL pipeline for music data. Features a SQLite-backed queue with lease renewal, concurrent workers, exponential backoff for APIs (iTunes, MusicBrainz, Last.fm), and audio feature extraction (tempo, key, MFCC) using Librosa. Designed for scalability and data integrity.
