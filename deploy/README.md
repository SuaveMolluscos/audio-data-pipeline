# Debian deployment

These files are templates for a Debian 13 host. Review paths and resource limits before enabling the service.
The application version is recorded in `VERSION` so every deployment can be identified.

## Install

```sh
sudo useradd --system --home /opt/music-crawler --shell /usr/sbin/nologin musiccrawler
sudo mkdir -p /opt/music-crawler /var/lib/music-crawler /var/log/music-crawler /etc/music-crawler
sudo mkdir -p /var/cache/music-crawler/numba
sudo chown -R musiccrawler:musiccrawler /opt/music-crawler /var/cache/music-crawler /var/lib/music-crawler /var/log/music-crawler
sudo -u musiccrawler git clone https://github.com/SuaveMolluscos/audio-data-pipeline.git /opt/music-crawler
sudo -u musiccrawler python3 -m venv /opt/music-crawler/.venv
sudo -u musiccrawler /opt/music-crawler/.venv/bin/pip install -r /opt/music-crawler/requirements.txt
sudo cp /opt/music-crawler/deploy/music-crawler.env.example /etc/music-crawler/music-crawler.env
sudo chmod 600 /etc/music-crawler/music-crawler.env
sudoedit /etc/music-crawler/music-crawler.env
```

For production, check out a reviewed release or commit instead of deploying an unpinned branch.

Install `ffmpeg` with the Debian package manager if audio analysis is required.

## Enable services

```sh
sudo cp /opt/music-crawler/deploy/music-crawler.service /etc/systemd/system/
sudo cp /opt/music-crawler/deploy/music-crawler-backup.service /etc/systemd/system/
sudo cp /opt/music-crawler/deploy/music-crawler-backup.timer /etc/systemd/system/
sudo cp /opt/music-crawler/deploy/runtime.env.example /etc/music-crawler/runtime.env
sudo chmod 600 /etc/music-crawler/runtime.env
sudo chmod 750 /opt/music-crawler/deploy/backup-db.sh
sudo chmod 750 /opt/music-crawler/deploy/preflight.sh
sudo -u musiccrawler /opt/music-crawler/.venv/bin/python /opt/music-crawler/seeds.py --db /var/lib/music-crawler/tracks.db enqueue --categories pop rock
sudo systemctl daemon-reload
sudo systemctl enable --now music-crawler.service
sudo systemctl enable --now music-crawler-backup.timer
sudo systemctl status music-crawler.service
journalctl -u music-crawler.service -f
```

Before starting or after updating the code, run the preflight check:

```sh
sudo -u musiccrawler /opt/music-crawler/deploy/preflight.sh
```

It compiles the code, runs the tests, checks SQLite integrity, and verifies the systemd unit. Keep the previous code and a database backup until this check and a short real crawl have completed.

## Monitor over SSH

Run the interactive monitor in a separate SSH session:

```sh
/opt/music-crawler/.venv/bin/python /opt/music-crawler/monitor.py \
	--db /var/lib/music-crawler/tracks.db
```

The monitor reads the database and systemd logs. Closing the monitor or the SSH session does not stop the crawler.
It includes live logs, recent logs, queue activity, table counts, recent songs, service configuration, systemd status, health checks, disk usage, backups, failed jobs, and a confirmed restart action.
The current menu options include live monitoring, logs, database, complete song details with audio/features/history, health, failures, backups, changing workers/lease, adding seeds, listing/editing/prioritizing/canceling pending tasks, restarting the service, exporting a consistent SQLite copy, and exporting songs to CSV. The keep-alive service processes new seeds automatically.
For a compact check from scripts or cron, use `monitor.py --summary --no-color`.

Before a long run, enqueue a small category and verify the database, logs, API access, and backup restore procedure. Keep at least one backup outside the old PC.
