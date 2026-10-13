# Phase 1: automated waste report (prototype)

One command reads a Jira site's automation flows and writes a waste report.

```bash
python3 phase1/stepmeter.py
open phase1/output/report.html
```

It uses the same three environment variables as the Phase 0 script (`ATLASSIAN_SITE`, `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`). Python 3.8 or newer, standard library only. Everything it calls on your site is read-only.

## What it does

1. Reads every flow through the Automation Rule Management API.
2. Works out the steps per run from each flow's structure, including one step per work item inside branches and scheduled searches.
3. Flags costly designs: a broad "any update" trigger followed by a check, work done before a check, scheduled searches that touch every match, branches that cost per related item.
4. Estimates runs in the last 30 days from ordinary Jira searches (work items created, updated or moved to a status). For flows that start with a simple label or search check, it also estimates how many runs pass the check.
5. Writes `phase1/output/report.html`, plus `analysis.json` and `shapes.json` (structure only, no values, safe to share for debugging). The output folder is git-ignored.

## Limits

- Run counts are estimates from Jira activity, not from the automation audit log, which has no public API. "Work items updated" counts items, not update events, so it's a lower bound.
- Flows triggered by field changes, manual runs or incoming webhooks can't be estimated this way yet.
- Failed runs (for example a web request that errors) can't be detected from structure alone.
