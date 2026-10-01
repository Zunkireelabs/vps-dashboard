"""Offline tests: no server, no network. Run:  python3 -m unittest discover -s tests"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent
os.environ["DASH_CONFIG"] = str(HERE / "fixtures" / "config.toml")
sys.path.insert(0, str(HERE.parent))

import build  # noqa: E402
from history import History  # noqa: E402


def http(code=200, secs=0.3, err="", body="ok"):
    return dict(code=code, secs=secs, err=err, body=body)


class Classify(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(build.classify_http(http(), 90)[0], "green")

    def test_nothing_answering(self):
        self.assertEqual(build.classify_http(http(404, body=build.TRAEFIK_404), 90)[0], "red")

    def test_app_404_is_only_a_warning(self):
        self.assertEqual(build.classify_http(http(404, body="nope"), 90)[0], "amber")

    def test_server_error_and_no_answer(self):
        self.assertEqual(build.classify_http(http(502), 90)[0], "red")
        self.assertEqual(build.classify_http(http(0), 90)[0], "red")
        self.assertEqual(build.classify_http(http(0, err="timed out"), 90)[0], "red")

    def test_certificate_soon_and_slow(self):
        self.assertEqual(build.classify_http(http(), 5)[0], "amber")
        level, why = build.classify_http(http(secs=6), 90)
        self.assertEqual(level, "green")
        self.assertIn("slow", why)


class FastProbe(unittest.TestCase):
    def setUp(self):
        self.real = build.curl

    def tearDown(self):
        build.curl = self.real

    def test_down_only_after_two_failures_in_a_row(self):
        build.curl = lambda h, r=None: http(0, None, "timed out")
        ok, upd, n = build.fast_probe("a.example.com", {"reach": True}, 0)
        self.assertEqual((ok, upd, n), (False, None, 1))            # first failure: remembered, not announced
        ok, upd, n = build.fast_probe("a.example.com", {"reach": True}, n)
        self.assertEqual((ok, upd["level"], n), (False, "red", 2))  # second: now it is Down

    def test_recovery_resets_the_count(self):
        build.curl = lambda h, r=None: http()
        ok, upd, n = build.fast_probe("a.example.com", {"reach": True}, 2)
        self.assertEqual((ok, upd["level"], n), (True, "green", 0))

    def test_unreachable_domain_counts_as_down_without_probing(self):
        def boom(*a, **k):
            raise AssertionError("must not probe a host visitors cannot reach")
        build.curl = boom
        self.assertEqual(build.fast_probe("a.example.com", {"reach": False}, 0), (False, None, 0))


class HistoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(Path(self.tmp.name) / "h.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_newest_first(self):
        self.h.add_event(100, "status", "a", "A", "prod", "green", "red")
        self.h.add_event(200, "status", "a", "A", "prod", "red", "green", "was Down for 2 min")
        self.assertEqual([e["ts"] for e in self.h.recent_events(5)], [200, 100])

    def test_uptime(self):
        now = time.time()
        for ok in (True, True, False):
            self.h.probe("a.example.com", now, ok)
        pct, hours = self.h.uptime("a.example.com")
        self.assertAlmostEqual(pct, 66.67, places=1)
        self.assertIsNone(self.h.uptime("never-seen.example.com"))

    def test_levels_roundtrip(self):
        self.h.save_levels({"a": dict(level="red", since=5.0, known=True, name="A", env="prod")})
        self.assertEqual(self.h.levels()["a"]["level"], "red")

    def test_prune_terminates_under_a_tiny_size_cap(self):
        self.h.max_mb = 0.0001
        self.h.add_samples(time.time(), [("a", 1.0, 1.0, 0)], dict(mem_used=1, mem_total=2, swap_used=0, disk_pct=50, disk_free_gb=5, load1=0.1))
        self.h.prune()   # must return, not loop forever


class WatchLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = History(Path(self.tmp.name) / "h.db")

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self):
        return [e["kind"] for e in self.h.recent_events(10)][::-1]

    def test_needs_several_checks_in_a_row_then_recovers(self):
        for _ in range(2):
            build.watch(self.h, 1, "cpu:a", "a", "A", "prod", True, False, "high", "ok", need=3)
        self.assertEqual(self.kinds(), [])                           # 2 of 3 — not yet
        build.watch(self.h, 1, "cpu:a", "a", "A", "prod", True, False, "high", "ok", need=3)
        build.watch(self.h, 1, "cpu:a", "a", "A", "prod", False, True, "high", "ok", need=3)
        self.assertEqual(self.kinds(), ["warn", "ok"])

    def test_first_run_is_quiet(self):
        build.watch(self.h, 1, "x", "a", "A", "prod", True, False, "bad", "ok", quiet=True)
        self.assertEqual(self.kinds(), [])
        build.watch(self.h, 2, "x", "a", "A", "prod", False, True, "bad", "back to normal")
        self.assertEqual(self.kinds(), ["ok"])                       # but its recovery is still reported


class Ports(unittest.TestCase):
    def test_unexplained_port_is_flagged(self):
        inv = dict(routes=[dict(file="r", backends=["http://172.18.0.1:9090"])],
                   containers=[dict(name="proxy", published=[80, 443])],
                   svcs={}, ports=[dict(port=p, host="0.0.0.0", owner="") for p in (22, 80, 9090, 9999)])
        what = {p["port"]: p["what"] for p in build.classify_ports(inv)}
        self.assertTrue(what[22] and what[80] and what[9090])
        self.assertEqual(what[9999], "")


class Misc(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(build.dur(20), "under a minute")
        self.assertEqual(build.dur(300), "5 min")
        self.assertEqual(build.dur(3 * 3600 + 120), "3 h 2 min")

    def test_missing_config_gives_a_helpful_message(self):
        env = {k: v for k, v in os.environ.items() if k != "DASH_CONFIG"}
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(HERE.parent)!r}); import config"],
                               cwd=d, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No config found", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
