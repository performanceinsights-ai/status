import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import deploy_pages  # noqa: E402

ENV = {"GITHUB_SHA": "abc123", "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY": "o/r",
       "GITHUB_TOKEN": "gh-token", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc/x?a=1", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}
NOWAIT = {"sleep": lambda _: None}


class Fake:
    """Records calls; answers POST from `created` (a dict, or a list of dicts/HttpErrors) and GET status from `statuses`."""

    def __init__(self, created, statuses=("succeed",)):
        self.created = list(created) if isinstance(created, list) else [created]
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, token, payload))
        if url == ENV["ACTIONS_ID_TOKEN_REQUEST_URL"]:
            return {"value": "jwt"}
        if url.endswith("/cancel"):
            return {}
        if method == "POST":
            answer = self.created.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        answer = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(answer, Exception):
            raise answer
        return {"status": answer}


class Version(unittest.TestCase):

    def test_includes_run_and_attempt(self):
        self.assertEqual(deploy_pages.build_version(ENV), "abc123-99-2")

    def test_attempt_defaults_to_one(self):
        env = {k: v for k, v in ENV.items() if k != "GITHUB_RUN_ATTEMPT"}
        self.assertEqual(deploy_pages.build_version(env), "abc123-99-1")

    def test_two_rounds_on_one_commit_differ(self):
        self.assertNotEqual(deploy_pages.build_version(ENV), deploy_pages.build_version({**ENV, "GITHUB_RUN_ID": "100"}))


class DeploymentId(unittest.TestCase):

    def test_prefers_id(self):
        self.assertEqual(deploy_pages.deployment_id({"id": "d1", "status_url": "https://x/y/z"}, "v"), "d1")

    def test_falls_back_to_status_url_then_version(self):
        self.assertEqual(deploy_pages.deployment_id({"status_url": "https://x/pages/deployments/d2"}, "v"), "d2")
        self.assertEqual(deploy_pages.deployment_id({}, "v"), "v")


class Deploy(unittest.TestCase):

    def test_posts_unique_version_with_artifact_and_token_then_succeeds(self):
        fake = Fake({"id": "abc123-99-2", "page_url": "https://p/"})
        outcome = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(outcome, "succeed")
        post = [c for c in fake.calls if c[0] == "POST"][0]
        self.assertEqual(post[1], "https://api.github.com/repos/o/r/pages/deployments")
        self.assertEqual(post[2], "gh-token")
        self.assertEqual(post[3], {"artifact_id": 7, "pages_build_version": "abc123-99-2", "oidc_token": "jwt"})
        oidc = fake.calls[0]
        self.assertEqual((oidc[1], oidc[2]), ("https://oidc/x?a=1", "req"))

    def test_polls_the_deployment_it_created(self):
        fake = Fake({"id": "dep-9"}, ["queued", "deployment_in_progress", "succeed"])
        outcome = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(outcome, "succeed")
        gets = [c[1] for c in fake.calls if c[0] == "GET" and "pages/deployments" in c[1]]
        self.assertEqual(gets, ["https://api.github.com/repos/o/r/pages/deployments/dep-9"] * 3)

    def test_failed_status_is_not_ok(self):
        fake = Fake({"id": "d"}, ["deployment_failed"])
        outcome = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(outcome, "deployment_failed")
        self.assertEqual([c for c in fake.calls if c[1].endswith("/cancel")], [])  # already final, nothing to cancel

    def test_refused_version_retries_with_the_plain_sha(self):
        fake = Fake([deploy_pages.HttpError(422, "pages_build_version is invalid"), {"id": "abc123"}])
        outcome = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(outcome, "succeed")
        posts = [c[3]["pages_build_version"] for c in fake.calls if c[0] == "POST"]
        self.assertEqual(posts, ["abc123-99-2", "abc123"])

    def test_other_errors_are_not_retried(self):
        fake = Fake([deploy_pages.HttpError(403, "no pages: write")])
        with self.assertRaises(deploy_pages.HttpError):
            deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(len([c for c in fake.calls if c[0] == "POST"]), 1)

    def test_other_400_or_422_is_not_taken_for_a_refused_version(self):
        for err in (deploy_pages.HttpError(400, "a deployment is already in progress"), deploy_pages.HttpError(422, "invalid artifact")):
            fake = Fake([err])
            with self.assertRaises(deploy_pages.HttpError):
                deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
            self.assertEqual(len([c for c in fake.calls if c[0] == "POST"]), 1)

    def test_a_poll_that_gives_up_cancels_the_deployment(self):
        fake = Fake({"id": "dep-9"}, [deploy_pages.HttpError(0, "URLError: down")])
        outcome = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertTrue(outcome.startswith("error:"))
        self.assertEqual([c[1] for c in fake.calls if c[1].endswith("/cancel")],
                         ["https://api.github.com/repos/o/r/pages/deployments/dep-9/cancel"])

    def test_an_interrupt_cancels_the_deployment_and_propagates(self):
        fake = Fake({"id": "dep-9"})

        def interrupted(_):
            raise SystemExit(143)
        with self.assertRaises(SystemExit):
            deploy_pages.deploy(ENV, 7, fake, sleep=interrupted)
        self.assertEqual(len([c for c in fake.calls if c[1].endswith("/cancel")]), 1)

    def test_reports_the_created_deployment_before_polling(self):
        seen = []
        deploy_pages.deploy(ENV, 7, Fake({"id": "d", "page_url": "https://p/"}), on_created=seen.append, **NOWAIT)
        self.assertEqual(seen, [{"id": "d", "page_url": "https://p/"}])

    def test_missing_oidc_variables_say_what_to_fix(self):
        env = {k: v for k, v in ENV.items() if k != "ACTIONS_ID_TOKEN_REQUEST_URL"}
        with self.assertRaisesRegex(RuntimeError, "id-token: write"):
            deploy_pages.oidc_token(env, Fake({}))


class Wait(unittest.TestCase):

    def test_times_out_with_last_status(self):
        ticks = iter(range(0, 1000, 100))
        status = deploy_pages.wait(ENV, "d", Fake({}, ["queued"]), timeout=250, sleep=lambda _: None, clock=lambda: next(ticks))
        self.assertEqual(status, "timeout (last status queued)")

    def test_gives_up_after_ten_status_errors(self):
        err = deploy_pages.HttpError(500, "boom")
        status = deploy_pages.wait(ENV, "d", Fake({}, [err]), **NOWAIT)
        self.assertTrue(status.startswith("error:"))

    def test_an_error_between_answers_is_survived(self):
        status = deploy_pages.wait(ENV, "d", Fake({}, [deploy_pages.HttpError(502, "x"), "succeed"]), **NOWAIT)
        self.assertEqual(status, "succeed")


class Request(unittest.TestCase):

    def test_sends_auth_and_json_and_parses_the_answer(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id": 1}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            answer = deploy_pages.request("POST", "https://api/x", "tok", {"a": 1})
        sent = urlopen.call_args[0][0]
        self.assertEqual((answer, sent.get_method(), sent.data), ({"id": 1}, "POST", b'{"a": 1}'))
        self.assertEqual(sent.get_header("Authorization"), "Bearer tok")

    def test_http_error_keeps_status_and_body(self):
        err = urllib.error.HTTPError("https://api/x", 422, "x", {}, io.BytesIO(b"bad pages_build_version"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(deploy_pages.HttpError) as caught:
                deploy_pages.request("POST", "https://api/x", "tok", {})
        self.assertEqual((caught.exception.status, caught.exception.body), (422, "bad pages_build_version"))
        self.assertTrue(deploy_pages.refused_version(caught.exception))

    def test_network_failures_and_bad_bodies_become_status_zero(self):
        for failure in (urllib.error.URLError("down"), TimeoutError("slow"), ConnectionResetError("reset")):
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(deploy_pages.HttpError) as caught:
                    deploy_pages.request("GET", "https://api/x", "tok")
            self.assertEqual(caught.exception.status, 0)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>"
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(deploy_pages.HttpError):
                deploy_pages.request("GET", "https://api/x", "tok")


class Main(unittest.TestCase):

    def run_main(self, fake, env=ENV):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "output")
            full = {**env, "GITHUB_OUTPUT": out}
            with mock.patch.dict(os.environ, full, clear=True), mock.patch.object(deploy_pages, "request", fake), \
                    mock.patch("time.sleep"), mock.patch("signal.signal"):
                code = deploy_pages.main(["--artifact-id", "7"])
            return code, (open(out).read() if os.path.exists(out) else "")

    def test_success_writes_the_page_url_output(self):
        code, output = self.run_main(Fake({"id": "d", "page_url": "https://p.example/"}))
        self.assertEqual((code, output), (0, "page_url=https://p.example/\n"))

    def test_no_page_url_writes_no_output(self):
        code, output = self.run_main(Fake({"id": "d"}))
        self.assertEqual((code, output), (0, ""))

    def test_failure_exits_one(self):
        code, _ = self.run_main(Fake({"id": "d", "page_url": "https://p/"}, ["deployment_failed"]))
        self.assertEqual(code, 1)

    def test_create_errors_exit_one_instead_of_a_traceback(self):
        code, _ = self.run_main(Fake([deploy_pages.HttpError(403, "forbidden")]))
        self.assertEqual(code, 1)
        self.assertIn("pages: write", deploy_pages.create_hint(deploy_pages.HttpError(403, "")))

    def test_missing_variable_exits_one(self):
        code, _ = self.run_main(Fake({"id": "d"}), {k: v for k, v in ENV.items() if k != "GITHUB_SHA"})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
