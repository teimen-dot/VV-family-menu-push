#!/usr/bin/env python3
"""Safe SQLite and photo backup utility for the single-instance deployment."""

import argparse
import glob
import os
import shlex
import sqlite3
import subprocess
import tarfile
from datetime import datetime

from db import DB_PATH
from runtime_config import photo_dir

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _timestamp(now=None):
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def quick_check(path):
    conn = sqlite3.connect(os.path.abspath(path))
    try:
        conn.execute("PRAGMA query_only = ON")
        return conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()


def backup_database(source, backup_dir, now=None):
    os.makedirs(backup_dir, exist_ok=True)
    destination = os.path.join(backup_dir, f"family_menu_{_timestamp(now)}.db")
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    if quick_check(destination) != "ok":
        raise RuntimeError("backup quick_check failed")
    return destination


def restore_database(source_backup, destination):
    if os.path.exists(destination):
        raise FileExistsError("restore destination already exists")
    source_conn = sqlite3.connect(os.path.abspath(source_backup))
    source_conn.execute("PRAGMA query_only = ON")
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    if quick_check(destination) != "ok":
        raise RuntimeError("restored database quick_check failed")
    return destination


def backup_photos(source_dir, backup_dir, now=None):
    if not os.path.isdir(source_dir):
        raise FileNotFoundError("photo directory does not exist")
    os.makedirs(backup_dir, exist_ok=True)
    destination = os.path.join(backup_dir, f"photos_{_timestamp(now)}.tar.gz")
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(source_dir, arcname="photos", recursive=True)
    return destination


def apply_retention(backup_dir, pattern, keep):
    files = sorted(glob.glob(os.path.join(backup_dir, pattern)), key=os.path.getmtime, reverse=True)
    for old in files[max(1, keep):]:
        os.remove(old)


def remote_copy(artifact):
    if os.environ.get("BACKUP_REMOTE_ENABLED", "false").lower() != "true":
        return False
    template = os.environ.get("BACKUP_REMOTE_COMMAND", "").strip()
    if not template or "{artifact}" not in template:
        raise RuntimeError("BACKUP_REMOTE_COMMAND must contain {artifact}")
    command = [part.replace("{artifact}", artifact) for part in shlex.split(template)]
    subprocess.run(command, check=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("db", "photos", "all"), nargs="?", default="db")
    args = parser.parse_args()
    backup_dir = os.environ.get("BACKUP_DIR", "/opt/family-menu/backups")
    retention = int(os.environ.get("BACKUP_RETENTION", "14"))
    artifacts = []
    if args.mode in ("db", "all"):
        artifacts.append(backup_database(DB_PATH, backup_dir))
        apply_retention(backup_dir, "family_menu_*.db", retention)
    if args.mode in ("photos", "all"):
        artifacts.append(backup_photos(photo_dir(BASE_DIR), backup_dir))
        apply_retention(backup_dir, "photos_*.tar.gz", retention)
    for artifact in artifacts:
        remote_copy(artifact)
        print(f"[OK] {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
