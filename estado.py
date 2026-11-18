"""Cola local y presupuesto del asesor. Una sola base persistente por número."""

import math
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DAY = 24 * 60 * 60
SEND_MARGIN = 120
LEASE = 180
HISTORY_PAIRS = 4


@dataclass(frozen=True)
class Job:
    message_id: str
    sender: str
    question: str
    timestamp: int
    token: str


class Estado:
    def __init__(self, path, monthly_limit=60, *, clock=time.time):
        if type(monthly_limit) is not int or not 0 <= monthly_limit <= 1000:
            raise ValueError("El límite debe ser un entero entre 0 y 1000.")
        if str(path) == ":memory:":
            raise ValueError("Se necesita una base persistente.")
        self.path = Path(path)
        self.monthly_limit = monthly_limit
        self.clock = clock
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # El diario de SQLite hereda los permisos del archivo de la base.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY,
                    message_id TEXT UNIQUE NOT NULL,
                    sender TEXT NOT NULL,
                    question TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    token TEXT,
                    lease_until REAL,
                    reply TEXT
                );
                CREATE INDEX IF NOT EXISTS by_sender ON jobs(sender, seq);
                CREATE INDEX IF NOT EXISTS by_age ON jobs(received_at);
                CREATE TABLE IF NOT EXISTS budget (
                    month TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL CHECK(attempts >= 0)
                );
            """)

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    @staticmethod
    def _months(now):
        # Cerca del cambio de mes se descuentan ambos meses para cubrir el
        # huso horario de facturación y un breve retraso en la entrega.
        margin = 16 * 3600
        return sorted({
            datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")
            for t in (now - margin, now, now + margin)
        })

    def _has_budget(self, db, months):
        if self.monthly_limit == 0:
            return False
        for month in months:
            row = db.execute("SELECT attempts FROM budget WHERE month=?", (month,)).fetchone()
            if row and row["attempts"] >= self.monthly_limit:
                return False
        return True

    @staticmethod
    def _prune(db, now):
        db.execute("DELETE FROM jobs WHERE received_at <= ?", (now - DAY,))
        # Una reserva puede haber producido un envío aunque se haya perdido la
        # respuesta del proveedor. Nunca la devolvemos a la cola.
        db.execute("""
            UPDATE jobs SET status='uncertain', sender='', question='', reply=NULL,
                token=NULL, lease_until=NULL
            WHERE status='sending' AND lease_until <= ?
        """, (now,))
        db.execute("""
            UPDATE jobs SET status='queued', token=NULL, lease_until=NULL
            WHERE status='working' AND lease_until <= ?
        """, (now,))
        db.execute("""
            UPDATE jobs SET status='expired', sender='', question='', reply=NULL,
                token=NULL, lease_until=NULL
            WHERE status IN ('queued','working') AND received_at <= ?
        """, (now - DAY + SEND_MARGIN,))

    def prune(self):
        with self._transaction() as db:
            self._prune(db, self.clock())

    def enqueue(self, message_id, sender, question, timestamp):
        if not all(isinstance(value, str) and value.strip() for value in (message_id, sender, question)):
            return False
        if len(message_id) > 512 or len(sender) > 64 or len(question) > 4096:
            return False
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            return False
        with self._transaction() as db:
            now = self.clock()
            if timestamp <= now - DAY + SEND_MARGIN or timestamp > now + 300:
                return False
            created = db.execute("""
                INSERT INTO jobs (message_id, sender, question, received_at, status)
                VALUES (?, ?, ?, ?, 'queued') ON CONFLICT(message_id) DO NOTHING
            """, (message_id, sender, question, int(min(timestamp, now))))
            return bool(created.rowcount)

    def claim(self):
        with self._transaction() as db:
            now = self.clock()
            self._prune(db, now)
            if not self._has_budget(db, self._months(now)):
                return None
            row = db.execute("""
                SELECT j.* FROM jobs j WHERE j.status='queued' AND NOT EXISTS (
                    SELECT 1 FROM jobs older
                    WHERE older.sender=j.sender AND older.seq<j.seq
                        AND older.status IN ('queued','working','sending')
                ) ORDER BY j.seq LIMIT 1
            """).fetchone()
            if not row:
                return None
            token = secrets.token_hex(16)
            db.execute("UPDATE jobs SET status='working', token=?, lease_until=? WHERE seq=?",
                       (token, now + LEASE, row["seq"]))
            return Job(row["message_id"], row["sender"], row["question"], row["received_at"], token)

    def history(self, job):
        with self._transaction() as db:
            rows = db.execute("""
                SELECT question, reply FROM jobs WHERE sender=? AND status='delivered'
                    AND seq < (SELECT seq FROM jobs WHERE message_id=?)
                    AND received_at > ? ORDER BY seq DESC LIMIT ?
            """, (job.sender, job.message_id, self.clock() - DAY, HISTORY_PAIRS)).fetchall()
            return [(row["question"], row["reply"]) for row in reversed(rows)]

    def reserve_send(self, job, reply):
        """Reserva definitiva justo antes del único intento de POST al proveedor."""
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 4096:
            self.abandon(job)
            return False
        with self._transaction() as db:
            now = self.clock()
            self._prune(db, now)
            owns_job = db.execute("""
                SELECT 1 FROM jobs WHERE message_id=? AND token=? AND status='working'
            """, (job.message_id, job.token)).fetchone()
            if not owns_job:
                return False
            months = self._months(now)
            if not self._has_budget(db, months):
                db.execute("""
                    UPDATE jobs SET status='limited', sender='', question='', token=NULL, lease_until=NULL
                    WHERE message_id=?
                """, (job.message_id,))
                return False
            for month in months:
                db.execute("""
                    INSERT INTO budget (month, attempts) VALUES (?, 1)
                    ON CONFLICT(month) DO UPDATE SET attempts=attempts+1
                """, (month,))
            db.execute("""
                UPDATE jobs SET status='sending', reply=?, lease_until=? WHERE message_id=?
            """, (reply, now + LEASE, job.message_id))
            return True

    def finish(self, job, delivered):
        with self._transaction() as db:
            if delivered:
                db.execute("""
                    UPDATE jobs SET status='delivered', token=NULL, lease_until=NULL
                    WHERE message_id=? AND token=? AND status='sending'
                """, (job.message_id, job.token))
            else:
                db.execute("""
                    UPDATE jobs SET status='uncertain', sender='', question='', reply=NULL,
                        token=NULL, lease_until=NULL
                    WHERE message_id=? AND token=? AND status='sending'
                """, (job.message_id, job.token))

    def abandon(self, job):
        with self._transaction() as db:
            db.execute("""
                UPDATE jobs SET status='abandoned', sender='', question='',
                    token=NULL, lease_until=NULL
                WHERE message_id=? AND token=? AND status='working'
            """, (job.message_id, job.token))
