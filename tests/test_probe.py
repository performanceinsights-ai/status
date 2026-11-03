import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import probe  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
PORTAL_HTML = b'<html><script type="module" crossorigin src="/dumptriage/assets/index-abc.js"></script></html>'


def fake(routes):
    """A fetch() that answers from {url: (status, content_type, body, ms) | OSError}."""
    def get(url, data=None, headers=None):
        answer = routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return get


def doc(obj):
    return json.dumps(obj).encode()


class UptimeDays(unittest.TestCase):
    def test_counts_only_the_days_with_samples(self):
        # The history window is 90 days; the figure must not claim days nothing measured (website#2912).
        days = {"2026-10-25": {"c": {"ok": 3, "deg": 0, "out": 1}}, "2026-10-26": {"c": {"ok": 4, "deg": 0, "out": 0}}, "2026-10-24": {"other": {"ok": 9, "deg": 0, "out": 0}}}
        history, uptime, measured = probe.day_summary(days, "c", NOW.date(), 90)
        self.assertEqual((len(history), uptime, measured), (90, 87.5, 2))

    def test_no_samples(self):
        self.assertEqual(probe.day_summary({}, "c", NOW.date(), 90)[1:], (None, 0))


class HttpCheck(unittest.TestCase):
    def test_ok(self):
        r = probe.check_http({"url": "u", "json": {"status": "ok"}}, fake({"u": (200, "application/json", doc({"status": "ok"}), 40)}))
        self.assertEqual((r.state, r.latency_ms), (probe.OPERATIONAL, 40))

    def test_unreachable_is_outage(self):
        self.assertEqual(probe.check_http({"url": "u"}, fake({"u": OSError("boom")})).state, probe.OUTAGE)

    def test_bad_status(self):
        r = probe.check_http({"url": "u"}, fake({"u": (503, "text/plain", b"", 5)}))
        self.assertEqual((r.state, r.detail), (probe.OUTAGE, "Answered HTTP 503"))

    def test_readyz_reporting_database_down(self):
        r = probe.check_http({"url": "u", "json": {"status": "ok", "database": "ok"}}, fake({"u": (200, "application/json", doc({"status": "ok", "database": "unreachable"}), 5)}))
        self.assertEqual(r.state, probe.OUTAGE)

    def test_json_keys_missing(self):
        r = probe.check_http({"url": "u", "json_keys": ["debugger"]}, fake({"u": (200, "application/json", doc({}), 5)}))
        self.assertEqual(r.state, probe.OUTAGE)

    def test_html_where_json_expected(self):
        r = probe.check_http({"url": "u", "json": {"status": "ok"}}, fake({"u": (200, "text/html", b"<html>", 5)}))
        self.assertEqual(r.state, probe.OUTAGE)

    def test_slow_is_degraded(self):
        r = probe.check_http({"url": "u", "degraded_ms": 100}, fake({"u": (200, "text/html", b"", 900)}))
        self.assertEqual(r.state, probe.DEGRADED)

    def test_portal_bundle_present(self):
        routes = {"https://p/dumptriage/": (200, "text/html", PORTAL_HTML, 10),
                  "https://p/dumptriage/assets/index-abc.js": (200, "text/javascript", b"x", 30)}
        r = probe.check_http({"url": "https://p/dumptriage/", "bundle": True}, fake(routes))
        self.assertEqual((r.state, r.latency_ms), (probe.OPERATIONAL, 30))

    def test_portal_bundle_served_as_html_fallback(self):
        routes = {"https://p/dumptriage/": (200, "text/html", PORTAL_HTML, 10),
                  "https://p/dumptriage/assets/index-abc.js": (200, "text/html", b"<html>", 30)}
        self.assertEqual(probe.check_http({"url": "https://p/dumptriage/", "bundle": True}, fake(routes)).state, probe.OUTAGE)

    def test_portal_without_script(self):
        self.assertEqual(probe.check_http({"url": "https://p/", "bundle": True}, fake({"https://p/": (200, "text/html", b"<html>", 1)})).state, probe.OUTAGE)

    def test_portal_script_on_another_host_is_not_followed(self):
        page = b'<script type="module" src="https://evil.example/x.js"></script>'
        self.assertEqual(probe.check_http({"url": "https://p/", "bundle": True}, fake({"https://p/": (200, "text/html", page, 1)})).state, probe.OUTAGE)


def google(*incidents, status=200):
    return fake({probe.GOOGLE_INCIDENTS_URL: (status, "application/json", doc(list(incidents)), 5)})


def incident(products, impact="SERVICE_DISRUPTION", end=None):
    item = {"status_impact": impact, "external_desc": "Elevated errors", "affected_products": [{"title": p} for p in products]}
    if end:
        item["end"] = end
    return item


class GoogleCheck(unittest.TestCase):
    spec = {"products": ["Vertex Gemini API"]}

    def test_no_incident(self):
        self.assertEqual(probe.check_google_cloud(self.spec, google()).state, probe.OPERATIONAL)

    def test_ended_incident_is_ignored(self):
        self.assertEqual(probe.check_google_cloud(self.spec, google(incident(["Vertex Gemini API"], end="2026-10-01T00:00:00+00:00"))).state, probe.OPERATIONAL)

    def test_other_product_is_ignored(self):
        self.assertEqual(probe.check_google_cloud(self.spec, google(incident(["Google Cloud Storage"]))).state, probe.OPERATIONAL)

    def test_ongoing_disruption_and_outage(self):
        r = probe.check_google_cloud(self.spec, google(incident(["Vertex Gemini API"]), incident(["Vertex Gemini API"], "SERVICE_OUTAGE")))
        self.assertEqual((r.state, r.detail), (probe.OUTAGE, "Google reports: Elevated errors"))

    def test_information_only_stays_operational(self):
        self.assertEqual(probe.check_google_cloud(self.spec, google(incident(["Vertex Gemini API"], "SERVICE_INFORMATION"))).state, probe.OPERATIONAL)

    def test_feed_down_is_unknown_not_an_outage(self):
        self.assertEqual(probe.check_google_cloud(self.spec, fake({probe.GOOGLE_INCIDENTS_URL: OSError()})).state, probe.UNKNOWN)
        self.assertEqual(probe.check_google_cloud(self.spec, google(status=500)).state, probe.UNKNOWN)


class UptimeRobotCheck(unittest.TestCase):
    spec = {"api_key_env": "K", "monitors": [1, 2]}

    def answer(self, *statuses):
        body = doc({"stat": "ok", "monitors": [{"status": s} for s in statuses]})
        return fake({probe.UPTIMEROBOT_URL: (200, "application/json", body, 5)})

    def test_without_key_or_monitors_is_left_out(self):
        self.assertIsNone(probe.check_uptimerobot(self.spec, {}))
        self.assertIsNone(probe.check_uptimerobot({"api_key_env": "K", "monitors": []}, {"K": "x"}))

    def test_states(self):
        env = {"K": "secret"}
        self.assertEqual(probe.check_uptimerobot(self.spec, env, self.answer(2, 2)).detail, "2 of 2 external checks passing")
        self.assertEqual(probe.check_uptimerobot(self.spec, env, self.answer(2, 9)).state, probe.OUTAGE)
        self.assertEqual(probe.check_uptimerobot(self.spec, env, self.answer(2, 8)).state, probe.DEGRADED)
        self.assertEqual(probe.check_uptimerobot(self.spec, env, self.answer(0, 1)).state, probe.UNKNOWN)

    def test_api_error_is_unknown(self):
        bad = fake({probe.UPTIMEROBOT_URL: (200, "application/json", doc({"stat": "fail"}), 5)})
        self.assertEqual(probe.check_uptimerobot(self.spec, {"K": "x"}, bad).state, probe.UNKNOWN)

    def test_key_is_not_in_the_result(self):
        r = probe.check_uptimerobot(self.spec, {"K": "topsecret"}, self.answer(9))
        self.assertNotIn("topsecret", json.dumps(vars(r)))

    def ur(self, *monitors):
        return fake({probe.UPTIMEROBOT_URL: (200, "application/json", doc({"stat": "ok", "monitors": list(monitors)}), 5)})

    def test_history_comes_from_uptimerobot(self):
        # website#2915: UptimeRobot saw 25 minutes down on 10-25 that the probe's rounds missed.
        created = int(datetime(2026, 10, 23, 6, tzinfo=timezone.utc).timestamp())
        down = int(datetime(2026, 10, 25, 10, 17, tzinfo=timezone.utc).timestamp())
        ratios = ["100.000"] * 86 + ["100.000", "100.000", "98.264", "100.000"]
        monitor = {"id": 7, "status": 2, "create_datetime": created, "custom_uptime_ranges": "-".join(ratios),
                   "logs": [{"type": 2, "datetime": down + 1500, "duration": 9000}, {"type": 1, "datetime": down, "duration": 1500}]}
        r = probe.check_uptimerobot({"api_key_env": "K", "monitors": [7]}, {"K": "x"}, self.ur(monitor), NOW, 90)
        days = r.history["days"]
        # The creation day (10-23) is left out: UptimeRobot counts its hours before creation as up.
        self.assertEqual([d["s"] for d in days[-4:]], [probe.UNKNOWN, probe.OPERATIONAL, probe.OUTAGE, probe.OPERATIONAL])
        self.assertEqual((days[-2]["u"], r.history["uptime_days"]), (98.26, 3))
        # Weighted by the seconds measured: whole 10-24 and 10-25, 12 h of today.
        self.assertEqual(r.history["uptime"], round((24 * 100 + 24 * 98.264 + 12 * 100) / 60, 2))
        self.assertEqual(r.history["outages"], [{"monitor": 7, "start": datetime.fromtimestamp(down, timezone.utc),
                                                 "end": datetime.fromtimestamp(down + 1500, timezone.utc)}])

    def test_only_status_9_is_an_ongoing_outage(self):
        start = int(NOW.timestamp()) - 600
        log = [{"type": 1, "datetime": start, "duration": 600}]
        for status, ongoing in ((9, True), (8, False)):
            monitor = {"id": 1, "status": status, "create_datetime": 0, "custom_uptime_ranges": "-".join(["100"] * 90), "logs": log}
            r = probe.check_uptimerobot({"api_key_env": "K", "monitors": [1]}, {"K": "x"}, self.ur(monitor), NOW)
            self.assertEqual(r.history["outages"][0]["end"] is None, ongoing)

    def test_paused_or_malformed_monitors_are_skipped(self):
        good = {"id": 1, "status": 2, "create_datetime": 0, "custom_uptime_ranges": "-".join(["99"] * 90), "logs": []}
        paused = {"id": 2, "status": 0, "create_datetime": 0, "custom_uptime_ranges": "-".join(["50"] * 90),
                  "logs": [{"type": 1, "datetime": 100, "duration": 5}]}
        broken = {"id": 3, "status": 2, "custom_uptime_ranges": "x"}
        r = probe.check_uptimerobot({"api_key_env": "K", "monitors": [1, 2, 3]}, {"K": "x"}, self.ur(good, paused, broken), NOW)
        self.assertEqual((r.history["days"][-1]["u"], r.history["outages"]), (99.0, []))
        self.assertIsNone(probe.check_uptimerobot(self.spec, {"K": "x"}, self.ur(broken), NOW).history)

    def test_an_answer_without_history_still_says_the_history_is_the_providers(self):
        r = probe.check_uptimerobot(self.spec, {"K": "x"}, self.answer(2, 2))
        self.assertEqual((r.history, r.provider_history), (None, True))

    def test_asks_for_one_range_per_day_ending_now(self):
        seen = {}
        def get(url, data=None, headers=None):
            seen.update(urllib.parse.parse_qs(data.decode()))
            return 200, "application/json", doc({"stat": "ok", "monitors": [{"status": 2}]}), 5
        probe.check_uptimerobot(self.spec, {"K": "x"}, get, NOW, 90)
        ranges = seen["custom_uptime_ranges"][0].split("-")
        self.assertEqual(len(ranges), 90)
        self.assertEqual(ranges[-1], f"{int(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp())}_{int(NOW.timestamp())}")
        self.assertEqual(seen["logs"], ["1"])


class UptimeRobotBuild(unittest.TestCase):
    config = {"groups": [{"id": "g", "name": "G", "components": [{"id": "ur", "name": "UR", "checks": [{"type": "uptimerobot", "api_key_env": "K", "monitors": [1]}]}]}]}

    def round(self, state, status=2, ratios=None, logs=(), now=NOW, monitor=None):
        monitor = monitor or {"id": 1, "status": status, "create_datetime": 0, "custom_uptime_ranges": "-".join(ratios or ["100"] * 90), "logs": list(logs)}
        get = fake({probe.UPTIMEROBOT_URL: (200, "application/json", doc({"stat": "ok", "monitors": [monitor]}), 5)})
        return probe.build(self.config, {"K": "x"}, state, [], now, get)

    def test_bars_and_incidents_are_uptimerobots(self):
        down = int(NOW.timestamp()) - 7200
        status, state = self.round({}, ratios=["100"] * 89 + ["99.5"], logs=[{"type": 1, "datetime": down, "duration": 300}])
        component = status["groups"][0]["components"][0]
        self.assertEqual((component["state"], component["uptime_days"], component["days"][-1]["s"]), (probe.OPERATIONAL, 90, probe.OUTAGE))
        self.assertEqual([(e["id"], e["end"]) for e in status["incidents"]["recent"]], [("ur-1-2026-10-26T10:00:00+00:00", "2026-10-26T10:05:00+00:00")])

    def test_older_incidents_survive_the_providers_short_log(self):
        down = int(NOW.timestamp()) - 7200
        _, state = self.round({}, logs=[{"type": 1, "datetime": down, "duration": 300}])
        status, state = self.round(state, logs=[])
        self.assertEqual([e["id"] for e in state["events"]], ["ur-1-2026-10-26T10:00:00+00:00"])

    def test_an_ongoing_outage_holds_back_with_the_state(self):
        logs = [{"type": 1, "datetime": int(NOW.timestamp()) - 120, "duration": 120}]
        up = {"prev": {"ur": {"raw": probe.OPERATIONAL, "published": probe.OPERATIONAL}}}
        status, state = self.round(up, status=9, logs=logs)
        self.assertEqual((status["overall"]["state"], status["incidents"]["open"]), (probe.OPERATIONAL, []))
        status, state = self.round(state, status=9, logs=logs)
        self.assertEqual((status["overall"]["state"], len(status["incidents"]["open"])), (probe.OUTAGE, 1))

    def test_a_round_without_history_keeps_the_last_one_and_its_incidents(self):
        down = int(NOW.timestamp()) - 7200
        _, state = self.round({}, ratios=["100"] * 89 + ["99.5"], logs=[{"type": 1, "datetime": down, "duration": 300}])
        status, state = self.round(state, monitor={"id": 1, "status": 2})
        component = status["groups"][0]["components"][0]
        self.assertEqual((component["days"][-1]["u"], len(status["incidents"]["recent"])), (99.5, 1))


class Publish(unittest.TestCase):
    def test_one_bad_answer_is_held_back(self):
        self.assertEqual(probe.publish(probe.OUTAGE, probe.OPERATIONAL, probe.OPERATIONAL), probe.OPERATIONAL)

    def test_two_bad_answers_publish(self):
        self.assertEqual(probe.publish(probe.OUTAGE, probe.OUTAGE, probe.OPERATIONAL), probe.OUTAGE)

    def test_outage_after_degraded_publishes_only_the_confirmed_part(self):
        self.assertEqual(probe.publish(probe.OUTAGE, probe.DEGRADED, probe.OPERATIONAL), probe.DEGRADED)

    def test_blip_does_not_lower_a_published_problem(self):
        self.assertEqual(probe.publish(probe.OUTAGE, probe.OPERATIONAL, probe.DEGRADED), probe.DEGRADED)

    def test_recovery_is_immediate(self):
        self.assertEqual(probe.publish(probe.OPERATIONAL, probe.OUTAGE, probe.OUTAGE), probe.OPERATIONAL)

    def test_first_run_and_unknown(self):
        self.assertEqual(probe.publish(probe.OUTAGE, None, None), probe.OUTAGE)
        self.assertEqual(probe.publish(probe.UNKNOWN, probe.OUTAGE, probe.OUTAGE), probe.UNKNOWN)
        self.assertEqual(probe.publish(probe.OUTAGE, probe.OUTAGE, probe.UNKNOWN), probe.OUTAGE)


CONFIG = {"history_days": 3, "groups": [{"id": "g", "name": "G", "components": [
    {"id": "api", "name": "API", "checks": [{"type": "http", "url": "https://a/healthz"}]},
    {"id": "ur", "name": "UR", "checks": [{"type": "uptimerobot", "api_key_env": "K", "monitors": []}]},
]}]}


def run(routes, state=None, now=NOW, notices=()):
    return probe.build(CONFIG, {}, state or {}, list(notices), now, fake(routes))


class Build(unittest.TestCase):
    up = {"https://a/healthz": (200, "application/json", b"{}", 10)}
    down = {"https://a/healthz": (503, "text/plain", b"", 10)}

    def test_all_up(self):
        status, state = run(self.up)
        self.assertEqual(status["overall"], {"state": "operational", "message": "All systems operational"})
        self.assertEqual([c["id"] for c in status["groups"][0]["components"]], ["api"])  # unconfigured UptimeRobot is left off
        self.assertEqual(status["groups"][0]["components"][0]["uptime"], 100.0)

    def test_outage_needs_two_runs_then_opens_and_closes_an_incident(self):
        status, state = run(self.up)
        status, state = run(self.down, state, NOW + timedelta(minutes=5))
        self.assertEqual(status["overall"]["state"], "operational")  # one bad answer is held back
        status, state = run(self.down, state, NOW + timedelta(minutes=10))
        self.assertEqual(status["overall"]["state"], "outage")
        self.assertEqual(len(status["incidents"]["open"]), 1)
        status, state = run(self.up, state, NOW + timedelta(minutes=15))
        self.assertEqual(status["incidents"]["open"], [])
        self.assertEqual(len(status["incidents"]["recent"]), 1)
        self.assertLess(status["groups"][0]["components"][0]["uptime"], 100)

    def test_first_run_publishes_what_it_sees(self):
        self.assertEqual(run(self.down)[0]["overall"]["state"], "outage")

    def test_history_has_one_entry_per_day_and_is_pruned(self):
        state = {}
        for day in range(5):
            status, state = run(self.up, state, NOW + timedelta(days=day))
        days = status["groups"][0]["components"][0]["days"]
        self.assertEqual(len(days), 3)
        self.assertEqual({d["s"] for d in days}, {"operational"})
        self.assertEqual(len(state["days"]), 3)

    def test_days_without_data_are_unknown(self):
        status, _ = run(self.up)
        self.assertEqual([d["s"] for d in status["groups"][0]["components"][0]["days"]], ["unknown", "unknown", "operational"])

    def test_notices_only_inside_their_window(self):
        notice = {"title": "Maintenance", "body": "b", "level": "maintenance", "from": "2026-10-26T11:00:00+00:00", "until": "2026-10-26T13:00:00+00:00"}
        self.assertEqual(len(run(self.up, notices=[notice])[0]["notices"]), 1)
        self.assertEqual(run(self.up, now=NOW + timedelta(hours=2), notices=[notice])[0]["notices"], [])

    def test_state_and_status_carry_no_secret(self):
        status, state = probe.build(CONFIG, {"K": "topsecret"}, {}, [], NOW, fake(self.up))
        self.assertNotIn("topsecret", json.dumps([status, state]))


class Main(unittest.TestCase):
    def test_writes_status_json_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "c.json")
            with open(config, "w") as handle:
                json.dump({"groups": []}, handle)
            self.assertEqual(probe.main(["--config", config, "--notices", os.path.join(tmp, "none.json"), "--data", os.path.join(tmp, "d"), "--out", os.path.join(tmp, "o")]), 0)
            with open(os.path.join(tmp, "o", "status.json")) as handle:
                self.assertEqual(json.load(handle)["overall"]["state"], "unknown")
            self.assertTrue(os.path.exists(os.path.join(tmp, "d", "state.json")))

    def test_shipped_config_is_valid(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "components.json")) as handle:
            config = json.load(handle)
        for group in config["groups"]:
            for component in group["components"]:
                for spec in component["checks"]:
                    self.assertIn(spec["type"], {"http", "google_cloud", "uptimerobot"})


if __name__ == "__main__":
    unittest.main()
