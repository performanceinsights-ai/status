# DumpTriage status

Source of <https://status.performanceinsights.ai>: the live state of the DumpTriage customer portal, API,
backend, website, AI analysis and external monitoring, with 90 days of history and recent incidents.

`status.json` on the page is the same data in machine-readable form.

## How it works

- `.github/workflows/status.yml` runs `probe.py` every five minutes on a GitHub-hosted runner, then publishes
  `site/` and the fresh `status.json` with GitHub Pages. It is outside the infrastructure it measures, so the page
  stays up when the service does not.
- `components.json` lists what is shown and how each component is checked: HTTP checks of the public endpoints
  (the portal's script bundle is fetched too, so a half-finished deploy shows as down), Google Cloud's public
  incident feed filtered to Vertex AI, and UptimeRobot's monitors.
- A component turns degraded or down only after two runs in a row say so; a recovery shows at once. A third-party
  feed that cannot be read shows "Unknown", never an incident.
- `state.json` (previous results, daily roll-ups, incident log) lives on the single-commit `data` branch.
- The page warns when `status.json` is older than `stale_after_minutes`.

## Changing it

- Add or change a check in `components.json`; `python3 -m unittest discover -s tests` must pass.
- A banner (maintenance, a known issue) goes in `notices.json` with a `from` and `until` time (ISO 8601, UTC); it
  shows only inside that window. `level` is `info`, `maintenance` or `incident`.
- UptimeRobot stays hidden until the repository secret `UPTIMEROBOT_API_KEY` (a read-only key) is set and
  `monitors` in `components.json` lists the monitor ids to show. Only listed monitors appear, so internal monitor
  names never reach the page.
- Everything here is public: probe only public URLs, and put no customer or internal detail in a notice.

Licensed under MIT.
