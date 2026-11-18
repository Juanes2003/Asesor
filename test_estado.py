import concurrent.futures
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from estado import DAY, LEASE, Estado


class EstadoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "estado.db"
        self.now = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
        self.store = self.open()

    def open(self, limit=60):
        return Estado(self.path, limit, clock=lambda: self.now)

    def add(self, msg, who="tester", text="¿Hay botas?"):
        return self.store.enqueue(msg, who, text, self.now)

    def test_concurrent_duplicate_committed_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.add, ["same"] * 20))
        self.assertEqual(1, sum(results))
        job = self.store.claim()
        self.assertEqual(job.message_id, "same")
        self.assertIsNone(self.open().claim())

    def test_serial_by_sender_parallel_between_senders(self):
        self.add("a1", "a")
        self.add("a2", "a")
        self.add("b1", "b")
        a = self.store.claim()
        b = self.open().claim()
        self.assertEqual([a.message_id, b.message_id], ["a1", "b1"])
        self.assertTrue(self.store.reserve_send(a, "Necesito la talla."))
        self.assertIsNone(self.store.claim())
        self.store.finish(a, True)
        second = self.store.claim()
        self.assertEqual(second.message_id, "a2")
        self.assertEqual(self.store.history(second), [("¿Hay botas?", "Necesito la talla.")])
        self.assertEqual(self.store.history(b), [])

    def test_lost_worker_can_recover_without_late_duplicate(self):
        self.add("id")
        first = self.store.claim()
        self.now += LEASE + 1
        restarted = self.open()
        second = restarted.claim()
        self.assertFalse(self.store.reserve_send(first, "Respuesta tardía"))
        self.store.abandon(first)
        self.assertTrue(restarted.reserve_send(second, "Respuesta"))
        self.now += LEASE + 1
        self.assertIsNone(self.open().claim())
        self.assertFalse(self.add("id"))
        self.assertFalse(self.store.reserve_send(second, "Respuesta duplicada"))
        self.add("next")
        self.assertEqual(self.store.history(self.store.claim()), [])

    def test_quota_race_across_instances_and_failure(self):
        self.store = self.open(1)
        for i in range(10):
            self.add(str(i), who=str(i))
        jobs = [self.store.claim() for _ in range(10)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            reserved = list(pool.map(lambda job: self.open(1).reserve_send(job, "Hola"), jobs))
        self.assertEqual(sum(reserved), 1)
        self.store.finish(jobs[reserved.index(True)], delivered=False)
        self.add("extra")
        self.assertIsNone(self.open(1).claim())

    def test_month_boundary_and_retention_are_independent(self):
        self.store = self.open(1)
        self.now = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc).timestamp()
        self.add("boundary")
        job = self.store.claim()
        self.assertTrue(self.store.reserve_send(job, "Respuesta"))
        self.store.finish(job, True)
        self.now += 120
        self.add("nextmonth")
        self.assertIsNone(self.open(1).claim())
        self.now += DAY + 1
        self.store.prune()
        with sqlite3.connect(self.path) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(dict(check.execute("SELECT * FROM budget")),
                             {"2026-11": 1, "2026-12": 1})
        self.now = datetime(2027, 1, 4, tzinfo=timezone.utc).timestamp()
        self.add("eligible")
        self.assertTrue(self.store.reserve_send(self.store.claim(), "Respuesta"))

    def test_recheck_service_window_just_before_send(self):
        self.assertFalse(self.store.enqueue("old", "person", "Hola", self.now - DAY))
        self.assertFalse(self.store.enqueue("future", "person", "Hola", self.now + 600))
        self.assertTrue(self.store.enqueue("edge", "person", "Hola", self.now - DAY + 130))
        job = self.store.claim()
        self.now += 20
        self.assertFalse(self.store.reserve_send(job, "Respuesta"))
        self.assertIsNone(self.store.claim())

    def test_pause_and_configuration(self):
        with self.assertRaises(ValueError):
            self.open(1001)
        with self.assertRaises(ValueError):
            self.open(-1)
        self.add("message")
        self.assertIsNone(self.open(0).claim())
        self.assertIsNotNone(self.open(1).claim())


if __name__ == "__main__":
    unittest.main()
