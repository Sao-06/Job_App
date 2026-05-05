import sqlite3
import hashlib
import os

DB_PATH = 'jobs.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            job_hash TEXT UNIQUE,
            co TEXT,
            role TEXT,
            loc TEXT,
            url TEXT,
            skills TEXT,
            score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def generate_job_hash(co, role, loc):
    h = hashlib.sha256()
    h.update(f"{co.lower()}|{role.lower()}|{loc.lower()}".encode('utf-8'))
    return h.hexdigest()

def upsert_job(co, role, loc, url, skills, score):
    job_hash = generate_job_hash(co, role, loc)
    conn = get_db()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO jobs (id, job_hash, co, role, loc, url, skills, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (job_hash, job_hash, co, role, loc, url, skills, score))
        conn.commit()
    finally:
        conn.close()

init_db()
