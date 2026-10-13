"""
Stepmeter prototype: read a Jira site's automation flows and write a waste report.

What it does, end to end
  1. Reads every automation flow through the Automation Rule Management API.
  2. Works out the steps each run uses from the flow's structure
     (trigger, conditions, actions, branches, per-item loops).
  3. Spots costly designs: a broad trigger that usually stops at its first check,
     work done before a check, a scheduled search that touches every match,
     loops that cost a step per work item.
  4. Estimates runs in the last 30 days from Jira activity, using ordinary searches
     (for example: work items created, updated, or moved to a status).
     Where a flow starts with a simple label or search check, it also estimates
     how many of those runs would pass the check.
  5. Writes phase1/output/report.html and opens nothing over the network except your
     own Jira site. Secrets inside flows are never written to disk.

Everything it calls on your site is read-only.

Usage (same environment variables as the Phase 0 script):
  python3 phase1/stepmeter.py

Python 3.8+, standard library only.
"""

import base64
import html
import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phase0"))
from fetch_rules import load_env, make_request  # noqa: E402

OUT = Path(__file__).resolve().parent / "output"
DAYS = 30
MAX_ITEMS = 1000  # work items read for history on large sites
# Observed on the test site: a flow with a trigger and one web request used 3 steps per run,
# whether the request succeeded or failed. So a web request counts as 2 steps.
STEP_COST = {"jira.issue.outgoing.webhook": 2}


# ---------------------------------------------------------------- helpers

def walk_strings(node):
    """Yield (key, value) for every string value in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                yield k, v
            else:
                yield from walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_strings(v)


def find_first(node, key_pattern):
    pat = re.compile(key_pattern, re.I)
    for k, v in walk_strings(node):
        if pat.search(k) and v.strip():
            return v.strip()
    return None


def shape(node, depth=0):
    """Keys and types only, no values. Safe to share for debugging."""
    if depth > 6:
        return "..."
    if isinstance(node, dict):
        return {k: shape(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [shape(node[0], depth + 1)] if node else []
    return type(node).__name__


def kind_of(comp):
    c = str(comp.get("component", "")).upper()
    t = str(comp.get("type", "")).lower()
    if "BRANCH" in c or "branch" in t:
        return "branch"
    if "CONDITION" in c or "condition" in t:
        return "condition"
    return "action"


def children_of(comp):
    for key in ("children", "components"):
        if isinstance(comp.get(key), list):
            return comp[key]
    return []


def label_for(comp):
    t = str(comp.get("type", "?"))
    tl = t.lower()
    for key, name in ((":created", "Work item created"), (":updated", "Work item updated (any field)"),
                      ("transition", "Status changed"), ("field.changed", "Field changed"),
                      ("scheduled", "Scheduled search"), ("manual", "Manual trigger"),
                      ("incoming", "Incoming webhook")):
        if str(comp.get("component", "")).upper() == "TRIGGER" or "trigger" in tl or "scheduled" in tl or "field.changed" in tl:
            if key in tl:
                return name
    friendly = {
        "jira.issue.assign": "Assign work item",
        "jira.issue.comment": "Add comment",
        "jira.issue.edit": "Edit fields",
        "jira.issue.transition": "Transition",
        "jira.issue.create": "Create work item",
        "jira.issue.outgoing.webhook": "Send web request",
        "jira.issue.condition": "Field condition",
        "jira.jql.condition": "Search condition",
        "jira.condition.container.block": "If/else",
        "jira.issue.related": "For each related work item",
    }
    return friendly.get(t, t.split(".")[-1].replace("-", " ").replace("_", " "))


# ------------------------------------------------------------ Jira access

class Jira:
    def __init__(self, site, auth, tz_name=None):
        self.site, self.auth = site, auth
        self._projects = {}
        self._issues = {}
        self.tz = timezone.utc
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(tz_name)
            except Exception:
                pass

    def when(self, dt):
        """Format a time for JQL, in the user's own time zone (how Jira reads JQL dates).
        One minute earlier, to be safe; exact filtering happens afterwards."""
        return (dt - timedelta(minutes=1)).astimezone(self.tz).strftime("%Y-%m-%d %H:%M")

    def count(self, jql):
        """Number of work items matching jql, or None if it can't be counted."""
        status, data = make_request(f"https://{self.site}/rest/api/3/search/approximate-count",
                                    self.auth, "POST", {"jql": jql})
        if status == 200 and isinstance(data.get("count"), int):
            return data["count"]
        issues = self.search(jql, fields="id", expand=None, limit=5000)
        return None if issues is None else len(issues)

    def search(self, jql, fields="labels,subtasks,created,status", expand="changelog", limit=MAX_ITEMS):
        out, token = [], None
        while len(out) < limit:
            q = {"jql": jql, "maxResults": "100", "fields": fields}
            if expand:
                q["expand"] = expand
            if token:
                q["nextPageToken"] = token
            status, data = make_request(
                f"https://{self.site}/rest/api/3/search/jql?{urllib.parse.urlencode(q)}", self.auth)
            if status != 200:
                return None if not out else out
            out.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def issues_with_history(self, scope, since):
        """Work items updated since `since`, with their change history. Cached per scope."""
        key = scope
        cached = self._issues.get(key)
        if cached and cached[0] <= since:
            return cached[1]
        jql = jql_and(scope, f'updated >= "{self.when(since)}"') + " ORDER BY updated DESC"
        issues = self.search(jql)
        self._issues[key] = (since, issues or [])
        return issues or []

    def project_key(self, pid):
        if pid not in self._projects:
            status, data = make_request(f"https://{self.site}/rest/api/3/project/{pid}", self.auth)
            self._projects[pid] = data.get("key") if status == 200 else None
        return self._projects[pid]


def scope_clause(jira, summary):
    keys = []
    for ari in summary.get("ruleScopeARIs") or []:
        m = re.search(r"project/(\d+)", str(ari))
        if m:
            k = jira.project_key(m.group(1))
            if k:
                keys.append(k)
    return (f"project in ({', '.join(keys)})", keys) if keys else ("", [])


def jql_and(*parts):
    return " AND ".join(p for p in parts if p)


def parse_time(s):
    """Jira timestamps look like 2026-10-08T21:28:18.123+0530."""
    if not s:
        return None
    if isinstance(s, (int, float)) or re.fullmatch(r"\d{10,13}", str(s)):
        v = float(s)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    s = re.sub(r"\.\d+", "", str(s))
    s = re.sub(r"([+-]\d\d):?(\d\d)$", r"\1\2", s).replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


# ------------------------------------------------------------ events

NOT_UPDATE_FIELDS = {"status", "resolution", "workflow", "resolutiondate"}


def events_for(issue):
    """Human change events on one work item, newest first, each with the labels it had right after."""
    labels_now = set((issue.get("fields") or {}).get("labels") or [])
    histories = ((issue.get("changelog") or {}).get("histories")) or []
    histories = sorted(histories, key=lambda h: h.get("created", ""), reverse=True)
    events, labels = [], set(labels_now)
    for h in histories:
        items = h.get("items") or []
        author = h.get("author") or {}
        by_app = author.get("accountType") == "app"
        events.append({
            "key": issue.get("key"),
            "when": parse_time(h.get("created")),
            "by_app": by_app,
            "fields": {str(i.get("fieldId") or i.get("field") or "").lower() for i in items},
            "status_to": [str(i.get("toString") or "") for i in items if str(i.get("field", "")).lower() == "status"]
                         + [str(i.get("to") or "") for i in items if str(i.get("field", "")).lower() == "status"],
            "labels_after": set(labels),
        })
        for i in items:
            if str(i.get("field", "")).lower() == "labels":
                labels = set(str(i.get("fromString") or "").split())
    return events


def condition_test(comp):
    """Return (test(labels)->bool, description) for a simple labels condition, else (None, reason)."""
    val = comp.get("value") or {}
    field = str(((val.get("selectedField") or {}).get("value")) or val.get("selectedFieldType") or "").lower()
    comparison = str(val.get("comparison") or "").upper()
    cv = (val.get("compareValue") or {}).get("value")
    if field != "labels":
        return None, f"condition on '{field or 'unknown field'}' ({comparison or 'unknown comparison'})"
    wanted = []
    if isinstance(cv, list):
        wanted = [str(x) for x in cv]
    elif isinstance(cv, str):
        s = cv.strip()
        if s.startswith("["):
            try:
                wanted = [str(x) for x in json.loads(s)]
            except ValueError:
                wanted = re.findall(r"[\w.\-]+", s)
        else:
            wanted = [p.strip() for p in re.split(r"[,\s]+", s) if p.strip()]
    wanted = set(wanted)
    if not wanted:
        return None, "labels condition with no readable values"
    negate = "NOT" in comparison or "NONE" in comparison
    if "ALL" in comparison and not negate:
        test = lambda labels: wanted <= labels
    elif comparison in ("IS_EMPTY", "EMPTY"):
        test = lambda labels: not labels
    else:
        test = lambda labels: bool(wanted & labels)
    if negate:
        base = test
        test = lambda labels: not base(labels)
    return test, None


# ---------------------------------------------------------------- analysis

def classify_trigger(trigger):
    t = str(trigger.get("type", "")).lower()
    if "scheduled" in t:
        return "scheduled"
    if ":created" in t:
        return "created"
    if ":updated" in t:
        return "updated"
    if "transition" in t:
        return "transitioned"
    if "field.changed" in t or "field_changed" in t:
        return "field_changed"
    if "manual" in t:
        return "manual"
    if "webhook" in t and "incoming" in t:
        return "incoming_webhook"
    return "other"


def analyse(rule, summary, jira, now):
    body = rule.get("rule", rule)
    name = body.get("name") or summary.get("name", "?")
    state = str(body.get("state") or summary.get("state", "")).upper()
    trigger = body.get("trigger") or {}
    tval = trigger.get("value") or {}
    comps = body.get("components") or []
    tkind = classify_trigger(trigger)
    scope, keys = scope_clause(jira, summary)

    created = parse_time(summary.get("created") or body.get("created"))
    window_start = now - timedelta(days=DAYS)
    since = max(created, window_start) if created else window_start
    days = max(1, round((now - since).total_seconds() / 86400, 1))

    chain = [{"kind": "trigger", "label": label_for(trigger) if trigger.get("type") else "Trigger"}]
    for c in comps:
        item = {"kind": kind_of(c), "label": label_for(c)}
        if item["kind"] == "branch":
            item["children"] = [{"kind": kind_of(ch), "label": label_for(ch)} for ch in children_of(c)]
        chain.append(item)

    res = {"name": name, "enabled": state != "DISABLED", "trigger_kind": tkind, "chain": chain,
           "scope": keys, "since": f"{since.astimezone(jira.tz):%d %b %Y}", "days": days, "findings": [], "notes": [],
           "runs": None, "runs_stopped": None, "est_steps": None, "est_wasted": None, "fix": None}
    if not res["enabled"]:
        res["findings"].append("Turned off, so it uses no steps.")
        res["est_steps"] = res["est_wasted"] = 0
        return res

    first_cond = next((i for i, c in enumerate(comps) if kind_of(c) == "condition"), None)
    actions_before = first_cond is not None and any(kind_of(c) != "condition" for c in comps[:first_cond])
    branch_comps = [c for c in comps if kind_of(c) == "branch"]
    test, why_not = (condition_test(comps[first_cond]) if first_cond is not None else (None, None))

    def cost(c):
        return STEP_COST.get(str(c.get("type", "")), 1)

    def full_steps(n_related=1):
        s = 1
        for c in comps:
            if kind_of(c) == "branch":
                s += 1 + n_related * max(1, sum(cost(ch) for ch in children_of(c)))
            else:
                s += cost(c)
        return s

    stopped_steps = (1 + sum(cost(c) for c in comps[:first_cond]) + 1) if first_cond is not None else None

    # ---- scheduled search
    if tkind == "scheduled":
        q = tval.get("jql") or find_first(tval, r"jql")
        only_changed = bool(tval.get("onlyUpdatedIssues"))
        matches = jira.count(q) if q else None
        runs = max(1, round(days))
        res["runs"] = runs
        if matches is not None:
            per_run = 1 + matches * max(1, sum(cost(c) for c in comps))
            res["est_steps"] = per_run * runs
            res["findings"].append(
                f"Costs one step for the search plus {max(1, len(comps))} per matching work item, every run. "
                f"It matches {matches} items today: about {per_run} steps a run, {per_run * 30:,} a month if it runs daily.")
            if not only_changed:
                res["fix"] = ("Turn on 'only include work items changed since the last run' on the scheduled trigger, "
                              "or narrow the search. Today it acts on every match, every run.")
        res["notes"].append(f"Assumed to run once a day since {res['since']}.")
        return res

    # ---- event triggers: count real events from work item history
    if tkind in ("updated", "field_changed", "transitioned", "created"):
        issues = jira.issues_with_history(scope, since) if tkind != "created" else None
        runs_list = []  # each: (labels_after, issue)
        if tkind == "created":
            created_issues = jira.search(jql_and(scope, f'created >= "{jira.when(since)}"'),
                                         fields="labels,subtasks,created", expand=None) or []
            created_issues = [i for i in created_issues
                              if (parse_time((i.get("fields") or {}).get("created")) or since) >= since]
            runs_list = [(set((i.get("fields") or {}).get("labels") or []), i) for i in created_issues]
            res["notes"].append(f"Runs counted as work items created since {res['since']}.")
        else:
            wanted_fields = {str(f.get("value", "")).lower() for f in (tval.get("fields") or []) if isinstance(f, dict)}
            to_status = {str(s.get("value", "")).lower() for s in (tval.get("toStatus") or []) if isinstance(s, dict)}
            for issue in issues:
                for ev in events_for(issue):
                    if not ev["when"] or ev["when"] < since or ev["by_app"]:
                        continue
                    if tkind == "updated" and not (ev["fields"] - NOT_UPDATE_FIELDS):
                        continue
                    if tkind == "field_changed" and not (ev["fields"] & wanted_fields):
                        continue
                    if tkind == "transitioned":
                        if "status" not in ev["fields"]:
                            continue
                        if to_status and not ({s.lower() for s in ev["status_to"]} & to_status):
                            continue
                    runs_list.append((ev["labels_after"], issue))
            what = {"updated": "edits to work items (not status changes or comments)",
                    "field_changed": f"changes to {', '.join(sorted(wanted_fields)) or 'the chosen fields'}",
                    "transitioned": "status changes" + (f" to {', '.join(sorted(to_status))}" if to_status else "")}[tkind]
            res["notes"].append(f"Runs counted from work item history: {what} made by people since {res['since']}. "
                                "Changes made by automation are skipped, since they don't start other flows by default.")
        runs = len(runs_list)
        res["runs"] = runs

        total = wasted = stopped = 0
        for labels_after, issue in runs_list:
            n_rel = 1
            if branch_comps:
                n_rel = len(((issue.get("fields") or {}).get("subtasks")) or []) or 0
            if test is not None and not test(labels_after):
                total += stopped_steps
                wasted += stopped_steps
                stopped += 1
            else:
                total += full_steps(n_rel) if branch_comps else full_steps()
        res["est_steps"] = total
        if test is not None:
            res["est_wasted"] = wasted
            res["runs_stopped"] = stopped
        elif first_cond is not None:
            res["notes"].append(f"Couldn't evaluate the first check ({why_not}), so every run is counted as a full run.")
        if branch_comps:
            res["notes"].append("Branch cost uses each work item's current number of sub-tasks.")
    else:
        res["notes"].append("Runs can't be counted from work item history for this trigger type.")

    # ---- design findings
    if actions_before:
        msg = "Does work before checking whether it should. Runs that stop at the check still pay for the earlier steps and still change the work item, and the audit log marks them as successful."
        if res["runs_stopped"]:
            msg += f" {res['runs_stopped']} of {res['runs']} runs stopped at the check."
        res["findings"].append(msg)
        res["fix"] = "Move the check above the actions. Runs that stop would cost less and wouldn't change anything."
    elif tkind == "updated" and first_cond == 0:
        msg = "Starts on every edit to any work item, then filters with a check."
        if res["runs_stopped"] is not None:
            msg += f" {res['runs_stopped']} of {res['runs']} runs stopped at the check and did nothing."
        res["findings"].append(msg)
        res["fix"] = ("If the flow only needs to react to one field, trigger on that field changing instead. "
                      "Runs that would stop at the check then never start.")
    if branch_comps:
        res["findings"].append("Contains a branch. Each related work item costs its own steps, so cost grows with the number of sub-tasks or links.")
    if any("webhook" in str(c.get("type", "")).lower() for c in comps):
        res["notes"].append("Sends a web request, which counts as 2 steps per run whether it succeeds or fails. Failures aren't visible from history; check the audit log.")
    if not res["findings"]:
        res["findings"].append("No costly pattern found in how this flow is built.")
    return res


# ------------------------------------------------------------------ report

CSS = """
:root{--bg:#f4f5f2;--surface:#fff;--fg:#1d2320;--muted:#5d6862;--line:#d9ded9;--accent:#2e5c8a;--waste:#a8462f;--waste-soft:#f7e5e0;--ok:#2c6b3f;--ok-soft:#e3f1e6;--bar:#e6e9e6;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#131614;--surface:#1b201d;--fg:#e5e9e6;--muted:#9ca8a1;--line:#2d3530;--accent:#8db8e3;--waste:#ef9a85;--waste-soft:#36201b;--ok:#7fc994;--ok-soft:#18291d;--bar:#2a312d;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.55}
.wrap{max-width:900px;margin:0 auto;padding:28px 16px 64px;display:flex;flex-direction:column;gap:22px}
h1{font-size:1.9rem;margin:0}h2{font-size:1.2rem;margin:0}h3{font-size:1.05rem;margin:0}p{margin:0;max-width:70ch}
.muted{color:var(--muted);font-size:.88rem}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:12px 14px}.tile .n{font-size:1.7rem;font-weight:600;font-variant-numeric:tabular-nums}.tile.w .n{color:var(--waste)}
.flow{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 16px;display:flex;flex-direction:column;gap:10px}.flow.w{border-left:3px solid var(--waste)}
.top{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}.nums{font-family:ui-monospace,Menlo,monospace;font-size:.8rem;color:var(--muted)}
.chain{display:flex;flex-wrap:wrap;gap:6px;font-family:ui-monospace,Menlo,monospace;font-size:.76rem}.chip{border:1px solid var(--line);border-radius:4px;padding:1px 7px}.chip.trigger{border-color:var(--accent);color:var(--accent)}.chip.condition{border-style:dashed}.chip.branch{background:var(--bar)}
ul{margin:0;padding-left:18px}.fix{background:var(--ok-soft);border-radius:4px;padding:8px 10px}.pill{font-size:.72rem;padding:1px 8px;border-radius:99px;background:var(--waste-soft);color:var(--waste);white-space:nowrap}
"""


def esc(s):
    return html.escape(str(s))


def render(site, results):
    enabled = [r for r in results if r["enabled"]]
    known = [r for r in enabled if r["est_steps"] is not None]
    total = sum(r["est_steps"] for r in known)
    wasted = sum(r["est_wasted"] or 0 for r in known)
    flagged = [r for r in enabled if r["fix"]]
    order = sorted(results, key=lambda r: (-(r["est_wasted"] or 0), -(r["est_steps"] or 0), not r["enabled"]))
    tiles = [
        (f"{len(results)}", "flows read"),
        (f"{total:,}", f"steps, since each flow was created (up to {DAYS} days)"),
        (f"{wasted:,}", "steps on runs that stopped at a check", True),
        (f"{len(flagged)}", "flows with a suggested fix"),
    ]
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Stepmeter Report</title><style>{CSS}</style></head><body><div class='wrap'>",
           f"<div class='muted'>Stepmeter prototype · {esc(site)} · {datetime.now():%d %b %Y, %H:%M}</div>",
           "<h1>Automation waste report</h1>",
           "<p>Generated automatically from this site's flows and recent Jira activity. Step counts follow Atlassian's rules: "
           "one step for each trigger, condition, action and branch that runs, and per work item inside branches and scheduled searches. "
           "Runs are counted from each work item's change history since the flow was created (up to 30 days), skipping changes made by automation. They are estimates, not the automation audit log, which has no public API.</p>",
           "<div class='tiles'>" + "".join(f"<div class='tile{' w' if len(t) > 2 else ''}'><div class='n'>{esc(t[0])}</div><div class='muted'>{esc(t[1])}</div></div>" for t in tiles) + "</div>",
           "<h2>Flows</h2>"]
    for r in order:
        chips = []
        for c in r["chain"]:
            chips.append(f"<span class='chip {c['kind']}'>{'if ' if c['kind'] == 'condition' else ''}{esc(c['label'])}</span>")
            for ch in c.get("children", []):
                chips.append(f"<span class='chip'>{esc(ch['label'])}</span>")
        nums = []
        if r["runs"] is not None:
            nums.append(f"{r['runs']:,} run{'' if r['runs'] == 1 else 's'} since {r['since']}")
        if r["est_steps"] is not None:
            nums.append(f"{r['est_steps']:,} steps")
        if r["est_wasted"]:
            nums.append(f"{r['est_wasted']:,} wasted")
        pill = ""
        if r["est_wasted"] and r["est_steps"]:
            pill = f"<span class='pill'>{round(r['est_wasted'] / r['est_steps'] * 100)}% wasted</span>"
        elif not r["enabled"]:
            pill = "<span class='muted'>Off</span>"
        out.append(
            f"<article class='flow{' w' if r['fix'] else ''}'><div class='top'><h3>{esc(r['name'])}</h3>"
            f"<div><span class='nums'>{esc(' · '.join(nums))}</span> {pill}</div></div>"
            f"<div class='chain'>{''.join(chips)}</div>"
            f"<ul>{''.join(f'<li>{esc(f)}</li>' for f in r['findings'])}</ul>"
            + (f"<div class='fix'><strong>Fix:</strong> {esc(r['fix'])}</div>" if r["fix"] else "")
            + (f"<div class='muted'>{esc(' '.join(r['notes']))}</div>" if r["notes"] else "")
            + "</article>")
    out.append("<p class='muted'>Read-only. Flow configuration was processed in memory; secrets were not written to disk.</p></div></body></html>")
    return "".join(out)


# -------------------------------------------------------------------- main

def fetch_rules(site, auth):
    status, info = make_request(f"https://{site}/_edge/tenant_info", None)
    cloud_id = info.get("cloudId")
    if not cloud_id:
        sys.exit(f"Could not read the site's cloudId (HTTP {status}).")
    base = f"https://{site}/gateway/api/automation/public/jira/{cloud_id}/rest/v1"
    summaries, cursor = [], None
    for _ in range(100):
        status, data = make_request(f"{base}/rule/summary", auth, "POST", {"cursor": cursor} if cursor else {})
        if status != 200:
            sys.exit(f"Listing flows failed (HTTP {status}): {data}")
        items = data.get("data") or []
        summaries.extend(items)
        nxt = (data.get("links") or {}).get("next")
        if not nxt or not items:
            break
        cursor = nxt.split("cursor=")[-1] if "cursor=" in nxt else nxt
    rules = []
    for s in summaries:
        uuid = s.get("uuid")
        status, full = make_request(f"{base}/rule/{uuid}", auth)
        if status == 200:
            rules.append((s, full))
        else:
            print(f"  Skipped {s.get('name')}: HTTP {status}")
    return rules


def main():
    site, email, token = load_env()
    auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
    status, me = make_request(f"https://{site}/rest/api/3/myself", auth)
    if status != 200:
        sys.exit(f"Login failed (HTTP {status}). Check ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN.")
    print(f"Logged in as {me.get('displayName')}. Reading flows from {site} ...")
    rules = fetch_rules(site, auth)
    print(f"Read {len(rules)} flows. Estimating activity for the last {DAYS} days ...")
    jira = Jira(site, auth, me.get("timeZone"))
    now = datetime.now(timezone.utc)
    results, shapes = [], []
    for summary, full in rules:
        r = analyse(full, summary, jira, now)
        results.append(r)
        body = full.get("rule", full)
        checks = []
        for c in body.get("components") or []:
            if kind_of(c) == "condition":
                v = c.get("value") or {}
                cv = (v.get("compareValue") or {}).get("value")
                checks.append({"type": c.get("type"), "field": (v.get("selectedField") or {}).get("value"),
                               "comparison": v.get("comparison"), "value_kind": type(cv).__name__,
                               "value_is_json_list": isinstance(cv, str) and cv.strip().startswith("["),
                               "readable": condition_test(c)[0] is not None})
        shapes.append({"name": r["name"], "trigger_type": body.get("trigger", {}).get("type"), "checks": checks,
                       "trigger": shape(body.get("trigger")), "components": [shape(c) for c in body.get("components") or []]})
        est = f"{r['est_steps']} steps" if r["est_steps"] is not None else "steps unknown"
        runs = (f"{r['runs']} run" + ("" if r["runs"] == 1 else "s")) if r["runs"] is not None else ""
        wasted = f"{r['est_wasted']} wasted" if r["est_wasted"] else ""
        print(f"  {r['name'][:30]:30} {runs:>9} {est:>13} {wasted:>11}  {'FIX' if r['fix'] else ''}")

    OUT.mkdir(exist_ok=True)
    (OUT / "report.html").write_text(render(site, results))
    (OUT / "analysis.json").write_text(json.dumps(results, indent=2))
    (OUT / "shapes.json").write_text(json.dumps(shapes, indent=2))
    print(f"\nReport written to {(OUT / 'report.html').relative_to(ROOT)}")
    print("Open it with:  open phase1/output/report.html")


if __name__ == "__main__":
    main()
