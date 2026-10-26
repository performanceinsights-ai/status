import json
import os
import sys
import tempfile
import unittest
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
