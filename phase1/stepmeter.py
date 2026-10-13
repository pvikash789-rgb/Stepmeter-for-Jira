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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phase0"))
from fetch_rules import load_env, make_request  # noqa: E402

OUT = Path(__file__).resolve().parent / "output"
DAYS = 30


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
    def __init__(self, site, auth):
        self.site, self.auth = site, auth
        self._projects = {}

    def count(self, jql):
        """Number of work items matching jql, or None if it can't be counted."""
        status, data = make_request(f"https://{self.site}/rest/api/3/search/approximate-count",
                                    self.auth, "POST", {"jql": jql})
        if status == 200 and isinstance(data.get("count"), int):
            return data["count"]
        # Fallback: page through ids (fine for small sites).
        total, token = 0, None
        for _ in range(50):
            q = {"jql": jql, "maxResults": "100", "fields": "id"}
            if token:
                q["nextPageToken"] = token
            status, data = make_request(
                f"https://{self.site}/rest/api/3/search/jql?{urllib.parse.urlencode(q)}", self.auth)
            if status != 200:
                return None
            total += len(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token:
                return total
        return total

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


def condition_jql(comp):
    """Turn a simple condition into JQL, or return None if we can't."""
    t = str(comp.get("type", "")).lower()
    val = comp.get("value") or {}
    if "jql" in t:
        q = find_first(val, r"jql|query")
        return q
    pairs = list(walk_strings(val))
    if any(v.lower() == "labels" for _, v in pairs):
        skip_keys = re.compile(r"type|comparison|operator|compare$|field|kind|mode", re.I)
        negate = any(re.search(r"not|none", v, re.I) for k, v in pairs if re.search(r"comparison|operator", k, re.I))
        labels = []
        for k, v in pairs:
            if skip_keys.search(k) or v.lower() == "labels" or v.startswith(("{", "$")):
                continue
            for part in v.split(","):
                part = part.strip()
                if re.fullmatch(r"[\w.\-]+", part) and not re.fullmatch(r"[A-Z_]+|\d+|customfield_\d+", part):
                    labels.append(part)
        if labels:
            clause = f"labels in ({', '.join(json.dumps(l) for l in dict.fromkeys(labels))})"
            return f"NOT ({clause})" if negate else clause
    return None


def analyse(rule, summary, jira):
    body = rule.get("rule", rule)
    name = body.get("name") or summary.get("name", "?")
    state = str(body.get("state") or summary.get("state", "")).upper()
    trigger = body.get("trigger") or {}
    comps = body.get("components") or []
    tkind = classify_trigger(trigger)
    scope, keys = scope_clause(jira, summary)

    chain = [{"kind": "trigger", "label": label_for(trigger) if trigger.get("type") else "Trigger",
              "type": trigger.get("type", "")}]
    for c in comps:
        item = {"kind": kind_of(c), "label": label_for(c), "type": c.get("type", "")}
        if item["kind"] == "branch":
            item["children"] = [{"kind": kind_of(ch), "label": label_for(ch)} for ch in children_of(c)]
        chain.append(item)

    res = {"name": name, "enabled": state != "DISABLED", "trigger_kind": tkind, "chain": chain,
           "scope": keys, "findings": [], "notes": [], "runs": None, "steps_per_full_run": None,
           "steps_per_stopped_run": None, "est_steps": None, "est_wasted": None, "fix": None}
    if not res["enabled"]:
        res["findings"].append("Turned off, so it uses no steps.")
        res["est_steps"] = res["est_wasted"] = 0
        return res

    first_cond = next((i for i, c in enumerate(comps) if kind_of(c) == "condition"), None)
    actions_before = first_cond is not None and any(kind_of(c) != "condition" for c in comps[:first_cond])
    branch_comps = [c for c in comps if kind_of(c) == "branch"]

    # Steps for one full run (branches counted with one item each; adjusted below).
    def full_steps(per_item=1):
        s = 1
        for c in comps:
            if kind_of(c) == "branch":
                s += 1 + per_item * max(1, len(children_of(c)))
            else:
                s += 1
        return s

    res["steps_per_full_run"] = full_steps()
    if first_cond is not None:
        res["steps_per_stopped_run"] = 1 + first_cond + 1

    # Runs in the last 30 days, estimated from Jira activity.
    period = f"-{DAYS}d"
    runs = None
    if tkind == "created":
        runs = jira.count(jql_and(scope, f"created >= {period}"))
        res["notes"].append(f"Runs estimated as work items created in the last {DAYS} days.")
    elif tkind == "updated":
        runs = jira.count(jql_and(scope, f"updated >= {period}"))
        res["notes"].append(f"Runs estimated as work items updated in the last {DAYS} days. "
                            "Each item can be updated many times, so this is a lower bound.")
    elif tkind == "transitioned":
        to_status = find_first(trigger.get("value") or {}, r"^(to|toStatus|name|value)$")
        clause = f'status CHANGED TO "{to_status}" AFTER {period}' if to_status and len(to_status) < 40 else f"status CHANGED AFTER {period}"
        runs = jira.count(jql_and(scope, clause))
        res["notes"].append(f"Runs estimated as work items whose status changed in the last {DAYS} days.")
    elif tkind == "scheduled":
        runs = DAYS
        res["notes"].append("Assumed to run once a day.")
    else:
        res["notes"].append("Run frequency can't be estimated from Jira activity for this trigger type.")
    res["runs"] = runs

    # Scheduled search: one step per matching work item per run.
    if tkind == "scheduled":
        q = find_first(trigger.get("value") or {}, r"jql")
        matches = jira.count(q) if q else None
        if matches is not None:
            per_run = 1 + matches * max(1, len(comps))
            res["steps_per_full_run"] = per_run
            res["est_steps"] = per_run * DAYS
            res["findings"].append(f"Costs one step for the search plus {max(1, len(comps))} per matching work item, every run. "
                                   f"It matches {matches} items today, so about {per_run} steps a run and {per_run * DAYS:,} a month.")
            res["fix"] = ("Only act on items that changed since the last run (an option on the scheduled trigger), "
                          "or narrow the search. Steps grow with every item the search matches.")
        else:
            res["findings"].append("A scheduled search that pays a step per matching work item, but its search couldn't be read.")
        res["est_wasted"] = None
        return res

    # Branches: cost per related item.
    if branch_comps:
        res["findings"].append("Contains a branch. Every related work item the branch touches costs its own steps, "
                               "so the flow gets more expensive as items have more children or links.")

    # Broad trigger followed by a filter, or work before the check.
    pass_runs = None
    if first_cond is not None and runs:
        cq = condition_jql(comps[first_cond])
        if cq and tkind in ("updated", "created"):
            window = "updated" if tkind == "updated" else "created"
            pass_runs = jira.count(jql_and(scope, f"{window} >= {period}", cq))
        if cq is None:
            res["notes"].append("The first condition is too complex to turn into a search, so the pass rate isn't estimated.")

    if runs is not None and res["est_steps"] is None:
        full, stopped = res["steps_per_full_run"], res["steps_per_stopped_run"]
        if pass_runs is not None:
            stop_runs = max(0, runs - pass_runs)
            res["est_steps"] = pass_runs * full + stop_runs * stopped
            res["est_wasted"] = stop_runs * stopped
        else:
            res["est_steps"] = runs * full

    if actions_before:
        res["findings"].append("Does work before checking whether it should. Runs that stop at the check still pay for "
                               "the earlier steps and still change the work item, and the audit log marks them as successful.")
        res["fix"] = "Move the check above the actions. Runs that stop would cost less and wouldn't change anything."
    elif tkind == "updated" and first_cond == 0:
        msg = "Starts on every update to any work item, then filters with a check."
        if pass_runs is not None and runs:
            msg += f" About {runs - pass_runs} of {runs} estimated runs would stop at the check and do nothing."
        res["findings"].append(msg)
        res["fix"] = ("If the flow only needs to react to one field, trigger on that field changing instead. "
                      "Runs that would stop at the check then never start.")
    if tkind == "updated" and first_cond is None and not comps:
        res["findings"].append("Starts on every update but does nothing.")

    if any("webhook" in str(c.get("type", "")).lower() for c in comps):
        res["notes"].append("Sends a web request. Failed requests still use steps; check the audit log for errors.")

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
        (f"{total:,}", f"estimated steps, last {DAYS} days"),
        (f"{wasted:,}", "estimated steps on runs that stop at a check", True),
        (f"{len(flagged)}", "flows with a suggested fix"),
    ]
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Stepmeter Report</title><style>{CSS}</style></head><body><div class='wrap'>",
           f"<div class='muted'>Stepmeter prototype · {esc(site)} · {datetime.now():%d %b %Y, %H:%M}</div>",
           "<h1>Automation waste report</h1>",
           "<p>Generated automatically from this site's flows and recent Jira activity. Step counts follow Atlassian's rules: "
           "one step for each trigger, condition, action and branch that runs, and per work item inside branches and scheduled searches. "
           "Run counts are estimates from the last 30 days of activity, not from the automation audit log.</p>",
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
            nums.append(f"~{r['runs']:,} runs")
        if r["est_steps"] is not None:
            nums.append(f"~{r['est_steps']:,} steps")
        if r["est_wasted"]:
            nums.append(f"~{r['est_wasted']:,} wasted")
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
    jira = Jira(site, auth)
    results, shapes = [], []
    for summary, full in rules:
        r = analyse(full, summary, jira)
        results.append(r)
        body = full.get("rule", full)
        shapes.append({"name": r["name"], "trigger": shape(body.get("trigger")), "components": shape(body.get("components"))})
        est = f"~{r['est_steps']} steps" if r["est_steps"] is not None else "steps unknown"
        print(f"  {r['name'][:34]:34} {est:>16}  {'FIX' if r['fix'] else ''}")

    OUT.mkdir(exist_ok=True)
    (OUT / "report.html").write_text(render(site, results))
    (OUT / "analysis.json").write_text(json.dumps(results, indent=2))
    (OUT / "shapes.json").write_text(json.dumps(shapes, indent=2))
    print(f"\nReport written to {(OUT / 'report.html').relative_to(ROOT)}")
    print("Open it with:  open phase1/output/report.html")


if __name__ == "__main__":
    main()
