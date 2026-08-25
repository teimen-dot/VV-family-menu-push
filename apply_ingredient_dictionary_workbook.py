#!/usr/bin/env python3
"""Explicit owner-curated ingredient workbook migration. Never runs at startup."""

import argparse
import json

from db import get_db, log_event
from ingredient_dictionary import apply_authoritative_workbook, parse_authoritative_workbook


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook")
    args = parser.parse_args()
    with open(args.workbook, "rb") as handle:
        rows, operations = parse_authoritative_workbook(handle.read())
    conn = get_db()
    try:
        report = apply_authoritative_workbook(conn, rows, operations)
    finally:
        conn.close()
    log_event("ingredient_dictionary_authoritative_import", "ingredient", "dictionary", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
