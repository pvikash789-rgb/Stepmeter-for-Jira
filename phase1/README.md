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
4. Counts runs from each work item's change history since the flow was created (up to 30 days): edits for "work item updated" triggers, changes to the chosen field for "field changed" triggers, status changes for "transitioned" triggers, and new work items for "created" triggers. Changes made by automation are skipped, because they don't start other flows by default. For flows whose first check is on labels, it uses the labels each work item had at the time to work out which runs passed the check and which stopped.
5. Writes `phase1/output/report.html`, plus `analysis.json` and `shapes.json` (structure only, no values, safe to share for debugging). The output folder is git-ignored.

## Limits

- Run counts come from work item history, not the automation audit log, which has no public API. They can differ when a flow's trigger has extra filters, or when a flow is set to be started by other flows.
- Scheduled flows are assumed to run once a day. Manual and incoming-webhook triggers can't be counted from history.
- On large sites, history is read for up to 1,000 recently updated work items.
- Failed runs (for example a web request that errors) can't be detected from structure alone.
