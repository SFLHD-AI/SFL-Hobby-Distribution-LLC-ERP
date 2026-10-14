"""
Nightly database backup. Add to cron on the server:
  0 2 * * *  cd /srv/sfl-erp && docker compose exec -T web python manage.py backup_db

Keeps the last N backups (default 14) in BACKUP_DIR (default ./backups).
Works for both Postgres (pg_dump) and SQLite (file copy).
"""
import gzip
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Dump the database to a timestamped, gzipped backup file."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=os.environ.get("BACKUP_DIR", "backups"))
        parser.add_argument("--keep", type=int, default=14)

    def handle(self, *args, **options):
        backup_dir = Path(options["dir"])
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db = settings.DATABASES["default"]

        if "postgresql" in db["ENGINE"]:
            out = backup_dir / f"sfl_erp_{stamp}.sql.gz"
            env = os.environ.copy()
            env["PGPASSWORD"] = db["PASSWORD"]
            cmd = ["pg_dump", "-h", db["HOST"], "-p", str(db["PORT"] or 5432),
                   "-U", db["USER"], db["NAME"]]
            try:
                with gzip.open(out, "wb") as f:
                    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, check=True)
                    f.write(proc.stdout)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                raise CommandError(f"pg_dump failed: {e}") from e
        else:
            src = Path(db["NAME"])
            out = backup_dir / f"sfl_erp_{stamp}.sqlite3.gz"
            with open(src, "rb") as fin, gzip.open(out, "wb") as fout:
                shutil.copyfileobj(fin, fout)

        self.stdout.write(self.style.SUCCESS(f"Backup written: {out}"))

        backups = sorted(backup_dir.glob("sfl_erp_*.gz"))
        excess = len(backups) - options["keep"]
        for old in backups[:max(excess, 0)]:
            old.unlink()
            self.stdout.write(f"pruned old backup: {old.name}")
