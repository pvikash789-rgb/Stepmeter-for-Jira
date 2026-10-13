"""Assemble the public pages. Usage: python3 build.py "<Product name>" "<repo url>" <out_dir>"""
import sys
from pathlib import Path

SRC = Path(__file__).parent  # templates live next to this script
name, repo, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)

naming = f"""<article class="entry"><div class="id">10<span>8 Oct</span></div><div class="body">
      <div class="meta"><span class="tag">Naming</span></div>
      <h3>Call it {name}</h3>
      <dl><dt>Why</dt><dd>"Step" is the unit Atlassian uses to measure automation usage, so the name says what the tool is about. Atlassian's brand guidelines for Marketplace vendors ask that app names avoid the word "Atlassian" and place the product name after a preposition, as in "for Jira".</dd></dl>
    </div></article>"""

HEAD = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'

def nav(other, label):
    return (f'<nav style="max-width:860px;margin:0 auto;padding:16px 16px 0;font-family:var(--mono);font-size:.8rem">'
            f'<a href="{other}">{label} &rarr;</a> &middot; <a href="{repo}">Code on GitHub</a></nav>\n')

def fill(s):
    return s.replace("{{NAME}}", name).replace("{{REPO}}", repo).replace("{{NAMING}}", naming)

jhead = (SRC / "journal-head.html").read_text().replace("<title>Stepmeter Build Journal</title>", f"<title>{name.split(' for ')[0]} Build Journal</title>")
lhead = (SRC / "log-head.html").read_text().replace("<title>Vikash PM Decision Log</title>", f"<title>{name.split(' for ')[0]} Decision Log</title>")

(out / "index.html").write_text(HEAD + jhead + "</head>\n<body>\n" + nav("decisions.html", "Decision log") + fill((SRC / "journal-body.html").read_text()) + "</body>\n</html>\n")
(out / "decisions.html").write_text(HEAD + lhead + "</head>\n<body>\n" + nav("index.html", "Build journal") + fill((SRC / "log-body.html").read_text()) + "</body>\n</html>\n")
print("built", out)
