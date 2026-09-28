"""Снимок SQLite, безопасный при работающем боте (sqlite backup API)."""
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
