#!/usr/bin/env python3
"""Publish the uploaded Pages artifact under a build version of its own.

Usage: python3 deploy_pages.py --artifact-id 123

actions/deploy-pages sends the commit SHA as `pages_build_version` and polls the deployment with that same id. `main`
rarely moves, so every round reused one id, and on 2026-11-06 Pages served the 12:25 build for 19 hours while each
deploy reported success (status#6). The version here is <sha>-<run id>-<attempt>, never a repeat. Same calls as the
action (POST /pages/deployments with the artifact id and the job's OIDC token, then poll until `succeed`).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
FAILED = {"deployment_failed", "deployment_content_failed", "deployment_cancelled", "deployment_lost"}


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def request(method, url, token, payload=None):
    """One JSON call; raises HttpError for a 4xx/5xx answer."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode("utf-8", "replace")) from None


def build_version(env):
    return f"{env['GITHUB_SHA']}-{env['GITHUB_RUN_ID']}-{env.get('GITHUB_RUN_ATTEMPT', '1')}"


def oidc_token(env, call=request):
    # Same as core.getIDToken() with no audience: the request URL as given, authorised by the request token.
    return call("GET", env["ACTIONS_ID_TOKEN_REQUEST_URL"], env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])["value"]


def create(env, artifact_id, token, version, call=request):
    return call("POST", f"{API}/repos/{env['GITHUB_REPOSITORY']}/pages/deployments", env["GITHUB_TOKEN"], {
        "artifact_id": artifact_id, "pages_build_version": version, "oidc_token": token})


def deployment_id(created, version):
    return created.get("id") or (created.get("status_url") or "").rstrip("/").rsplit("/", 1)[-1] or version


def wait(env, dep_id, call=request, timeout=300, delay=5, sleep=time.sleep, clock=time.monotonic):
    """Poll until the deployment succeeds; return the last status seen (it is 'succeed' only on success)."""
    deadline = clock() + timeout
    status = None
    errors = 0
    while clock() < deadline:
        sleep(delay)
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
    return status or "timeout"


def deploy(env, artifact_id, call=request, **wait_args):
    """Create the deployment under a unique version and wait for it. Returns (ok, created)."""
    token = oidc_token(env, call)
    version = build_version(env)
    try:
        created = create(env, artifact_id, token, version, call)
    except HttpError as err:
        if err.status not in (400, 422):
            raise
        # Keep the page updating if Pages turns out to refuse a version that is not a bare SHA.
        print(f"::warning::Pages refused build version {version} ({err}); retrying with the commit SHA")
        version = env["GITHUB_SHA"]
        created = create(env, artifact_id, token, version, call)
    dep_id = deployment_id(created, version)
    print(f"Created deployment {dep_id} for build version {version}")
    return wait(env, dep_id, call, **wait_args) == "succeed", created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact-id", required=True, type=int)
    args = parser.parse_args(argv)
    ok, created = deploy(os.environ, args.artifact_id)
    page_url = created.get("page_url") or ""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"page_url={page_url}\n")
    if not ok:
        print("::error::GitHub Pages did not report the deployment as succeeded")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
