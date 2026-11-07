import os
import sys
import unittest

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
        ok, created = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertTrue(ok)
        self.assertEqual(created["page_url"], "https://p/")
        post = [c for c in fake.calls if c[0] == "POST"][0]
        self.assertEqual(post[1], "https://api.github.com/repos/o/r/pages/deployments")
        self.assertEqual(post[2], "gh-token")
        self.assertEqual(post[3], {"artifact_id": 7, "pages_build_version": "abc123-99-2", "oidc_token": "jwt"})
        oidc = fake.calls[0]
        self.assertEqual((oidc[1], oidc[2]), ("https://oidc/x?a=1", "req"))

    def test_polls_the_deployment_it_created(self):
        fake = Fake({"id": "dep-9"}, ["queued", "deployment_in_progress", "succeed"])
        ok, _ = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertTrue(ok)
        gets = [c[1] for c in fake.calls if c[0] == "GET" and "pages/deployments" in c[1]]
        self.assertEqual(gets, ["https://api.github.com/repos/o/r/pages/deployments/dep-9"] * 3)

    def test_failed_status_is_not_ok(self):
        ok, _ = deploy_pages.deploy(ENV, 7, Fake({"id": "d"}, ["deployment_failed"]), **NOWAIT)
        self.assertFalse(ok)

    def test_refused_version_retries_with_the_plain_sha(self):
        fake = Fake([deploy_pages.HttpError(422, "bad version"), {"id": "abc123"}])
        ok, _ = deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertTrue(ok)
        posts = [c[3]["pages_build_version"] for c in fake.calls if c[0] == "POST"]
        self.assertEqual(posts, ["abc123-99-2", "abc123"])

    def test_other_errors_are_not_retried(self):
        fake = Fake([deploy_pages.HttpError(403, "no pages: write")])
        with self.assertRaises(deploy_pages.HttpError):
            deploy_pages.deploy(ENV, 7, fake, **NOWAIT)
        self.assertEqual(len([c for c in fake.calls if c[0] == "POST"]), 1)


class Wait(unittest.TestCase):

    def test_times_out_with_last_status(self):
        ticks = iter(range(0, 1000, 100))
        status = deploy_pages.wait(ENV, "d", Fake({}, ["queued"]), timeout=250, sleep=lambda _: None, clock=lambda: next(ticks))
        self.assertEqual(status, "queued")

    def test_gives_up_after_ten_status_errors(self):
        err = deploy_pages.HttpError(500, "boom")
        status = deploy_pages.wait(ENV, "d", Fake({}, [err]), **NOWAIT)
        self.assertTrue(status.startswith("error:"))

    def test_an_error_between_answers_is_survived(self):
        status = deploy_pages.wait(ENV, "d", Fake({}, [deploy_pages.HttpError(502, "x"), "succeed"]), **NOWAIT)
        self.assertEqual(status, "succeed")


if __name__ == "__main__":
    unittest.main()
