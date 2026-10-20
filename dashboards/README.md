# Dashboard exports

Put an Azure Data Explorer dashboard export here (ADX web UI: dashboard → **File → Download dashboard to file**),
e.g. `dashboards/finops.json`, and set `DASHBOARD_FILE` to it. Its tiles become the `run_dashboard_query` tool.

`*.json` here is git-ignored: exports can hold internal names (subscriptions, projects). The Docker image copies
this folder, so a file present at build time is deployed (Bicep `dashboardFile=/app/dashboards/<name>.json`).

Check what the app will use before deploying:

    PYTHONPATH=src python -m finops_nl.dashboard dashboards/finops.json

Tiles the query guard rejects (system tables, other databases, entities outside `ALLOWED_ENTITIES`, over the
length limit) are skipped and listed with the reason.

Then check the tiles actually run on your hub, and exclude the ones that don't (missing tags/columns):

    DASHBOARD_FILE=dashboards/finops.json ADX_CLUSTER_URI=https://... \
      python scripts/check_queries.py --only dashboard --write-exclude

This writes `dashboards/finops.exclude.json` (git-ignored, copied into the image with the export).
