#!/usr/bin/env python3
"""Probe the service from outside and write status.json for the status page.

Run by .github/workflows/status.yml every few minutes from a GitHub-hosted runner, so a
failure of the hosting provider cannot hide itself. Standard library only.

    python3 probe.py --config components.json --notices notices.json --data data --out public

`--data` holds state.json between runs (the previous raw result of every component, daily
roll-ups for the history bars, and the incident log); the workflow keeps it on the `data`
branch. `--out` receives status.json.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_AGENT = "PerformanceInsights-StatusProbe/1 (+https://status.performanceinsights.ai)"
TIMEOUT = 10
ATTEMPTS = 2

OPERATIONAL, DEGRADED, OUTAGE, UNKNOWN = "operational", "degraded", "outage", "unknown"
_RANK = {OPERATIONAL: 0, DEGRADED: 1, OUTAGE: 2}
_BY_RANK = {rank: state for state, rank in _RANK.items()}

GOOGLE_INCIDENTS_URL = "https://status.cloud.google.com/incidents.json"
UPTIMEROBOT_URL = "https://api.uptimerobot.com/v2/getMonitors"

# UptimeRobot's getMonitors status codes (0 paused and 1 not checked yet say nothing about health).
_UPTIMEROBOT_STATE = {2: OPERATIONAL, 8: DEGRADED, 9: OUTAGE}
_GOOGLE_IMPACT_STATE = {"SERVICE_OUTAGE": OUTAGE, "SERVICE_DISRUPTION": DEGRADED}


class Result:
    """What one check saw: a state, a customer-safe sentence about it, and the time it took."""

    def __init__(self, state, detail="", latency_ms=None):
        self.state, self.detail, self.latency_ms = state, detail, latency_ms


def worst(states):
    ranked = [s for s in states if s in _RANK]
    return max(ranked, key=_RANK.__getitem__) if ranked else UNKNOWN


def fetch(url, timeout=TIMEOUT, data=None, headers=None):
    """GET (or POST with `data`) once per attempt; returns (status, content_type, body, elapsed_ms).
    A transport error or timeout raises OSError after the last attempt. 4xx/5xx are returned, not raised."""
    last = None
    for attempt in range(ATTEMPTS):
        started = time.monotonic()
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(2_000_000)
                status, ctype = response.status, response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            body, status, ctype = err.read(2_000_000), err.code, err.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last = err
            if attempt + 1 < ATTEMPTS:
                time.sleep(1)
            continue
        if status < 500 or attempt + 1 == ATTEMPTS:
            return status, ctype, body, int((time.monotonic() - started) * 1000)
        time.sleep(1)
    raise OSError(str(last))


def _json(body):
    try:
        return json.loads(body)
    except ValueError:
        return None


def check_http(spec, get=fetch):
    url = spec["url"]
    try:
        status, ctype, body, ms = get(url)
    except OSError:
        return Result(OUTAGE, "Not reachable")
    if status != spec.get("expect_status", 200):
        return Result(OUTAGE, f"Answered HTTP {status}", ms)
    if "json" in spec or "json_keys" in spec:
        doc = _json(body)
        if not isinstance(doc, dict):
            return Result(OUTAGE, "Answered something other than JSON", ms)
        if any(doc.get(k) != v for k, v in spec.get("json", {}).items()) or any(k not in doc for k in spec.get("json_keys", [])):
            return Result(OUTAGE, "Reports a problem", ms)
    if spec.get("bundle"):
        bundle = check_bundle(url, body, get)
        if bundle.state != OPERATIONAL:
            return bundle
        ms = max(ms, bundle.latency_ms or 0)
    if ms > spec.get("degraded_ms", 5000):
        return Result(DEGRADED, "Responding slowly", ms)
    return Result(OPERATIONAL, "", ms)


_MODULE_SCRIPT = re.compile(r"""<script[^>]+type=["']module["'][^>]+src=["']([^"']+)["']|<script[^>]+src=["']([^"']+)["'][^>]+type=["']module["']""")


def check_bundle(page_url, html, get):
    """The portal is a single-page app: its HTML can answer 200 while its script does not exist
    (a half-finished deploy), which leaves the customer with a blank page."""
    match = _MODULE_SCRIPT.search(html.decode("utf-8", "replace"))
    if not match:
        return Result(OUTAGE, "The page does not load its application")
    src = urllib.parse.urljoin(page_url, match.group(1) or match.group(2))
    if urllib.parse.urlparse(src).netloc != urllib.parse.urlparse(page_url).netloc:
        return Result(OUTAGE, "The page does not load its application")
    try:
        status, ctype, _, ms = get(src)
    except OSError:
        return Result(OUTAGE, "The application script is not reachable")
    if status != 200 or "html" in ctype.lower():
        return Result(OUTAGE, "The application script is missing")
    return Result(OPERATIONAL, "", ms)


def check_google_cloud(spec, get=fetch, now=None):
    """Google's public incident feed: an incident with no end that lists one of our products.
    The feed being down says nothing about Google's service, so that is UNKNOWN, not an outage."""
    try:
        status, _, body, _ = get(GOOGLE_INCIDENTS_URL)
    except OSError:
        return Result(UNKNOWN, "Google's status feed is not reachable")
    incidents = _json(body)
    if status != 200 or not isinstance(incidents, list):
        return Result(UNKNOWN, "Google's status feed is not readable")
    wanted = set(spec["products"])
    state, summary = OPERATIONAL, ""
    for incident in incidents:
        if incident.get("end"):
            continue
        if wanted.isdisjoint(p.get("title") for p in incident.get("affected_products", [])):
            continue
        incident_state = _GOOGLE_IMPACT_STATE.get(incident.get("status_impact"), OPERATIONAL)
        if _RANK[incident_state] > _RANK[state] or (state == OPERATIONAL and not summary):
            state, summary = incident_state, (incident.get("external_desc") or "")[:200]
    if summary:
        return Result(state, f"Google reports: {summary}")
    return Result(OPERATIONAL, "")


def check_uptimerobot(spec, env, get=fetch):
    """Worst state of the listed monitors. Without a key or any monitor ids there is nothing to
    say, and the component is left off the page (returns None)."""
    key = env.get(spec["api_key_env"], "")
    monitors = [str(m) for m in spec.get("monitors", [])]
    if not key or not monitors:
        return None
    form = urllib.parse.urlencode({"api_key": key, "format": "json", "monitors": "-".join(monitors)}).encode()
    try:
        status, _, body, _ = get(UPTIMEROBOT_URL, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except OSError:
        return Result(UNKNOWN, "UptimeRobot is not reachable")
    doc = _json(body)
    if status != 200 or not isinstance(doc, dict) or doc.get("stat") != "ok":
        return Result(UNKNOWN, "UptimeRobot did not answer")
    states = [_UPTIMEROBOT_STATE.get(m.get("status"), UNKNOWN) for m in doc.get("monitors", [])]
    known = [s for s in states if s != UNKNOWN]
    if not known:
        return Result(UNKNOWN, "No monitor reports a state")
    up = sum(1 for s in known if s == OPERATIONAL)
    return Result(worst(known), f"{up} of {len(known)} external checks passing")


def run_component(component, env, get=fetch):
    """Combine a component's checks: the worst state wins, and the first problem is the detail."""
    results = []
    for spec in component["checks"]:
        kind = spec["type"]
        if kind == "http":
            result = check_http(spec, get)
        elif kind == "google_cloud":
            result = check_google_cloud(spec, get)
        elif kind == "uptimerobot":
            result = check_uptimerobot(spec, env, get)
        else:
            raise ValueError(f"unknown check type {kind!r}")
        if result is not None:
            results.append(result)
    if not results:
        return None
    state = worst([r.state for r in results])
    problem = next((r for r in results if r.state == state and r.detail), None)
    detail = problem.detail if problem else next((r.detail for r in results if r.detail), "")
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return Result(state, detail, max(latencies) if latencies else None)


def publish(raw, prev_raw, prev_published):
    """One bad answer is not an outage: a state worse than the published one is published only once
    the run before it was that bad too. Getting better is published at once. UNKNOWN passes through."""
    if raw == UNKNOWN:
        return UNKNOWN
    if prev_published not in _RANK or _RANK[raw] <= _RANK[prev_published]:
        return raw
    confirmed = min(_RANK[raw], _RANK.get(prev_raw, 0))
    return _BY_RANK[max(confirmed, _RANK[prev_published])]


def roll_up(days, cid, state, day, keep):
    sample = days.setdefault(day, {}).setdefault(cid, {"ok": 0, "deg": 0, "out": 0})
    if state in _RANK:
        sample[{OPERATIONAL: "ok", DEGRADED: "deg", OUTAGE: "out"}[state]] += 1
    for old in sorted(days)[:-keep]:
        del days[old]


def day_summary(days, cid, today, count):
    out, total_ok, total = [], 0, 0
    for offset in range(count - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        sample = days.get(day, {}).get(cid)
        n = sum(sample.values()) if sample else 0
        if not n:
            out.append({"d": day, "s": UNKNOWN})
            continue
        state = OUTAGE if sample["out"] else DEGRADED if sample["deg"] else OPERATIONAL
        # Slow is still up; only an outage counts against uptime.
        out.append({"d": day, "s": state, "u": round(100 * (n - sample["out"]) / n, 2)})
        total, total_ok = total + n, total_ok + n - sample["out"]
    return out, (round(100 * total_ok / total, 2) if total else None)


def track_events(events, cid, name, state, detail, now):
    """Open an incident when a component leaves OPERATIONAL, keep its worst state, close it on recovery."""
    stamp = now.isoformat(timespec="seconds")
    open_event = next((e for e in events if e["component"] == cid and e["end"] is None), None)
    if state in (DEGRADED, OUTAGE):
        if open_event is None:
            events.append({"id": f"{cid}-{stamp}", "component": cid, "name": name, "state": state, "start": stamp, "end": None, "detail": detail})
        elif _RANK[state] > _RANK[open_event["state"]]:
            open_event.update(state=state, detail=detail or open_event["detail"])
    elif state == OPERATIONAL and open_event is not None:
        open_event["end"] = stamp


def active_notices(notices, now):
    out = []
    for notice in notices:
        start = datetime.fromisoformat(notice["from"]) if notice.get("from") else None
        end = datetime.fromisoformat(notice["until"]) if notice.get("until") else None
        if (start is None or start <= now) and (end is None or now < end):
            out.append({k: notice[k] for k in ("title", "body", "level", "from", "until") if k in notice})
    return out


_MESSAGES = {OPERATIONAL: "All systems operational", DEGRADED: "Some systems are degraded", OUTAGE: "Service disruption"}


def build(config, env, state, notices, now, get=fetch):
    """One probe round: returns (status document, new state)."""
    keep = config.get("history_days", 90)
    today = now.date()
    prev = state.get("prev", {})
    days, events = state.get("days", {}), state.get("events", [])
    groups, new_prev, states = [], {}, []
    for group in config["groups"]:
        components = []
        for component in group["components"]:
            result = run_component(component, env, get)
            if result is None:
                continue
            before = prev.get(component["id"], {})
            published = publish(result.state, before.get("raw"), before.get("published"))
            new_prev[component["id"]] = {"raw": result.state, "published": published}
            roll_up(days, component["id"], published, today.isoformat(), keep)
            track_events(events, component["id"], component["name"], published, result.detail, now)
            history, uptime = day_summary(days, component["id"], today, keep)
            states.append(published)
            components.append({
                "id": component["id"], "name": component["name"], "description": component.get("description", ""),
                "state": published, "detail": result.detail if published != OPERATIONAL else "",
                "latency_ms": result.latency_ms, "uptime": uptime, "days": history,
            })
        if components:
            groups.append({"id": group["id"], "name": group["name"], "components": components})
    overall = worst(states)
    cutoff = (now - timedelta(days=keep)).isoformat(timespec="seconds")
    events = [e for e in events if e["end"] is None or e["end"] >= cutoff]
    recent_cutoff = (now - timedelta(days=14)).isoformat(timespec="seconds")
    document = {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "stale_after_minutes": config.get("stale_after_minutes", 30),
        "overall": {"state": overall, "message": _MESSAGES.get(overall, "Status unavailable")},
        "groups": groups,
        "notices": active_notices(notices, now),
        "incidents": {
            "open": [e for e in events if e["end"] is None],
            "recent": sorted((e for e in events if e["end"] and e["end"] >= recent_cutoff), key=lambda e: e["start"], reverse=True)[:20],
        },
    }
    return document, {"prev": new_prev, "days": days, "events": events}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="components.json")
    parser.add_argument("--notices", default="notices.json")
    parser.add_argument("--data", default="data")
    parser.add_argument("--out", default="public")
    args = parser.parse_args(argv)
    os.makedirs(args.data, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    state_path = os.path.join(args.data, "state.json")
    document, state = build(_load(args.config, None), os.environ, _load(state_path, {}), _load(args.notices, []), datetime.now(timezone.utc))
    for path, payload in ((state_path, state), (os.path.join(args.out, "status.json"), document)):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
    print(f"{document['overall']['state']}: " + ", ".join(f"{c['id']}={c['state']}" for g in document["groups"] for c in g["components"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
