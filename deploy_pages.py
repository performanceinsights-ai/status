#!/usr/bin/env python3
"""Publish the uploaded Pages artifact under a build version of its own.

Usage: python3 deploy_pages.py --artifact-id 123

actions/deploy-pages sends the commit SHA as `pages_build_version` and polls the deployment with that same id. `main`
rarely moves, so every round reused one id, and on 2026-11-06 Pages served the 12:25 build for 19 hours while each
deploy reported success (status#6). The version here is <sha>-<run id>-<attempt>, never a repeat. Same calls as the
action (POST /pages/deployments with the artifact id and the job's OIDC token, then poll until `succeed`, cancel the
deployment when the job is cancelled or times out).
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
FAILED = {"deployment_failed", "deployment_content_failed", "deployment_cancelled", "deployment_lost"}


class HttpError(Exception):
    """A 4xx/5xx answer, or (status 0) a call that got no usable answer: network error, timeout, bad JSON."""

    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def request(method, url, token, payload=None):
    """One JSON call; raises HttpError for an error answer or an unusable one."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode("utf-8", "replace")) from None
    except (OSError, ValueError) as err:  # URLError, timeouts, resets, truncated or non-JSON bodies
        raise HttpError(0, f"{type(err).__name__}: {err}") from None


def build_version(env):
    return f"{env['GITHUB_SHA']}-{env['GITHUB_RUN_ID']}-{env.get('GITHUB_RUN_ATTEMPT', '1')}"


def oidc_token(env, call=request):
    # Same as core.getIDToken() with no audience: the request URL as given, authorised by the request token.
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        if not env.get(name):
            raise RuntimeError('Ensure the job has permission "id-token: write" (no OIDC token is available).')
    return call("GET", env["ACTIONS_ID_TOKEN_REQUEST_URL"], env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])["value"]


def create(env, artifact_id, token, version, call=request):
    return call("POST", f"{API}/repos/{env['GITHUB_REPOSITORY']}/pages/deployments", env["GITHUB_TOKEN"], {
        "artifact_id": artifact_id, "pages_build_version": version, "oidc_token": token})


def refused_version(err):
    """Pages said the build version itself is not acceptable (not: a deployment in progress, a bad artifact or token)."""
    return err.status in (400, 422) and "version" in err.body.lower()


def create_hint(err):
    if err.status == 403:
        return ' Ensure GITHUB_TOKEN has permission "pages: write".'
    if err.status == 404:
        return " Ensure GitHub Pages has been enabled for the repository."
    if err.status >= 500 or err.status == 0:
        return " Is githubstatus.com reporting a Pages outage? The next round tries again."
    return ""


def deployment_id(created, version):
    return created.get("id") or (created.get("status_url") or "").rstrip("/").rsplit("/", 1)[-1] or version


def wait(env, dep_id, call=request, timeout=300, delay=5, sleep=None, clock=time.monotonic):
    """Poll until the deployment succeeds or fails; return the last status ('succeed' only on success)."""
    deadline = clock() + timeout
    status = None
    errors = 0
    while clock() < deadline:
        (sleep or time.sleep)(delay)
        try:
            status = call("GET", f"{API}/repos/{env['GITHUB_REPOSITORY']}/pages/deployments/{dep_id}", env["GITHUB_TOKEN"]).get("status")
            errors = 0
        except HttpError as err:
            errors += 1
            print(f"::warning::status check failed ({err})")
            if errors >= 10:
                return f"error: {err}"
            continue
        print(f"status: {status}")
        if status == "succeed" or status in FAILED:
            return status
    return f"timeout (last status {status})"


def cancel(env, dep_id, call=request):
    try:
        call("POST", f"{API}/repos/{env['GITHUB_REPOSITORY']}/pages/deployments/{dep_id}/cancel", env["GITHUB_TOKEN"])
        print(f"Canceled deployment {dep_id}")
    except HttpError as err:
        print(f"::warning::could not cancel deployment {dep_id} ({err})")


def deploy(env, artifact_id, call=request, on_created=lambda created: None, **wait_args):
    """Create the deployment under a unique version and wait for it. Returns (outcome, created); outcome is 'succeed' or why not."""
    token = oidc_token(env, call)
    version = build_version(env)
    try:
        created = create(env, artifact_id, token, version, call)
    except HttpError as err:
        if not refused_version(err):
            raise
        # Keep the page updating if Pages refuses a version that is not a bare SHA.
        print(f"::warning::Pages refused build version {version} ({err}); retrying with the commit SHA")
        version = env["GITHUB_SHA"]
        created = create(env, artifact_id, token, version, call)
    dep_id = deployment_id(created, version)
    print(f"Created deployment {dep_id} for build version {version}")
    on_created(created)
    outcome = None
    try:
        outcome = wait(env, dep_id, call, **wait_args)
    finally:
        # An interrupt (job cancelled or timed out) or a poll that gave up must not leave the deployment running, or
        # the next round's create is refused while it is in progress.
        if outcome != "succeed" and outcome not in FAILED:
            cancel(env, dep_id, call)
    return outcome, created


def _interrupted(signum, frame):
    raise SystemExit(128 + signum)


def write_output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"{name}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact-id", required=True, type=int)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)

    def announce(created):
        # As the action does, before polling, so the environment URL is known even when the deployment then fails.
        if created.get("page_url"):
            write_output("page_url", created["page_url"])
        else:
            print("::warning::Pages returned no page_url")

    try:
        outcome, _ = deploy(os.environ, args.artifact_id, request, on_created=announce)
    except KeyError as err:
        print(f"::error::Missing environment variable {err}")
        return 1
    except RuntimeError as err:
        print(f"::error::{err}")
        return 1
    except HttpError as err:
        print(f"::error::Creating the Pages deployment failed: {err}.{create_hint(err)}")
        return 1
    if outcome != "succeed":
        print(f"::error::GitHub Pages did not report the deployment as succeeded: {outcome}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
