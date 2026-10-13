# Stepmeter for Jira

Find which Jira and Confluence automation rules will cost the most under Atlassian's per-step billing, and how to rewrite them to cost less.

**Status:** Working prototype. Started 8 October 2026. `phase1/stepmeter.py` reads a site's flows and work item history and writes a waste report. On the test site, it matched the Usage tab's run count for all 7 flows that react to events and accounted for 89 of 97 steps.

**Build journal:** https://pvikash789-rgb.github.io/Stepmeter-for-Jira/

## Why this exists

From 3 December 2026, Atlassian Cloud measures automation usage in steps. Each trigger, condition, action, branch and loop that runs counts as one step, including a trigger that finds nothing to act on. Steps come from a monthly allowance pooled across the organisation, extra usage is billed at $0.50 per 1,000 steps and is on by default, and Enterprise plans move from unlimited automation to an allowance.

Cost now depends on how each rule is built. A daily scheduled rule that loops over 500 work items and edits each one uses at least 15,000 steps a month on its own. Atlassian shows usage by meter and app, and each space's Automation Usage tab ranks flows by estimated steps. What it doesn't show is how many steps went to runs that did nothing, or how to change a flow to use fewer. In the test site, 4 of one flow's 7 steps were spent on runs that stopped at a condition.

## What it will do

Atlassian's Usage tab tells you which flows use the most steps. Stepmeter will tell you how much of that is wasted and how to fix it:

1. Read each automation flow's structure through the Automation Rule Management API.
2. Work out how many steps each run uses and how many go to runs that do nothing.
3. Point to the part of the flow that causes the waste.
4. Suggest a rewrite and estimate the saving before anything is changed.

Secrets inside rules (webhook URLs, headers, tokens) are stripped before processing, and rule data isn't kept.

## Plan

| Phase | What | Status |
|---|---|---|
| 0 | Validate: API access (confirmed), what the product already shows (done), admin interviews | In progress |
| 1 | Cost engine: parse rules, count steps, estimate run frequency, check accuracy | Prototype working |
| 2 | Recommendations and report | Planned |
| 3 | Beta with 5 to 10 admins on their own sites | Planned |
| 4 | Public release, before 3 December 2026 | Planned |

## Repository

- [`docs/`](docs/): the build journal and decision log, published with GitHub Pages
- [`phase0/`](phase0/README.md): test rules, the API spike script and the admin interview guide
- [`phase1/`](phase1/README.md): the working prototype that writes the waste report
- [`tools/site/`](tools/site/README.md): templates for the public pages

## Run the Phase 0 spike

```bash
cp .env.example .env      # add your site, email and API token
python3 phase0/fetch_rules.py
```

Python 3.8 or newer, standard library only. Output goes to `phase0/output/`, which is git-ignored.

## About

Built by Vikash Kumar. I use an AI assistant (Claude) for research and coding. Problem choice, product decisions and trade-offs are mine and are recorded in the [decision log](https://pvikash789-rgb.github.io/Stepmeter-for-Jira/decisions.html).

Not affiliated with or endorsed by Atlassian. Jira and Confluence are trademarks of Atlassian.
