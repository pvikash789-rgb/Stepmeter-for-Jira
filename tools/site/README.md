# Site build

The public pages in `docs/` are built from these templates.

- `journal-head.html` + `journal-body.html` -> `docs/index.html` (build journal)
- `log-head.html` + `log-body.html` -> `docs/decisions.html` (decision log)
- `docs/brief.html` and `docs/demo.html` are standalone and edited directly.

To rebuild after editing a template:

```bash
python3 tools/site/build.py "Stepmeter for Jira" "https://github.com/pvikash789-rgb/Stepmeter-for-Jira" docs
```

New journal entries go at the top of the `<div class="journal">` block in `journal-body.html`.
New decisions go at the end of the ledger in `log-body.html`, numbered in order after the last one.
