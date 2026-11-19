# Klubbdigest – Kristiansand & Oslo

A weekly digest of dance and club nights in Kristiansand and Oslo — house, tech house, techno, disco and the groovy in-between.

The public site is a static GitHub Pages site:

**https://reikaas.github.io/club-digest/**

People can subscribe by email (Buttondown) or follow [the RSS feed](https://reikaas.github.io/club-digest/feed.xml). Every email includes a one-click unsubscribe link.

## Subscribe form

The signup form on the site posts to Buttondown's embed endpoint. The username lives in **one place**, `_config.yml`:

```yaml
buttondown_username: "BUTTONDOWN_USERNAME"
```

Replace `BUTTONDOWN_USERNAME` with the Buttondown username once the account exists, then rebuild:

```bash
python3 scripts/publish_issue.py --rebuild
```

Do not put API keys in this repository. The send key belongs in the `BUTTONDOWN_API_KEY` environment variable only. See `.env.example`.

## Publishing an issue

Write the issue as Markdown with YAML frontmatter, or as JSON.

Markdown (`issues/YYYY-MM-DD.md`):

```markdown
---
title: "Klubbdigest, 25 November 2026"
date: 2026-11-25
summary: "One or two sentences for the archive and RSS."
subject: "Klubbdigest – 25 November 2026"
---

Introductory paragraph.

## This weekend

**Friday 27 November — The Villa**
[RANDOMER (UK) & Fochs Koshka (Live)](https://www.thevilla.no/event-details/randomer-uk-fochs-koshka-live)
```

JSON (same fields; `body` or `body_markdown` holds the Markdown body, without frontmatter):

```json
{
  "date": "2026-11-25",
  "title": "Klubbdigest, 25 November 2026",
  "summary": "One or two sentences for the archive and RSS.",
  "subject": "Klubbdigest – 25 November 2026",
  "body": "Introductory paragraph.\n"
}
```

Then render the issue HTML, rebuild the homepage, archive and RSS feed, commit and push:

```bash
python3 scripts/publish_issue.py issues/2026-11-25.md
python3 scripts/publish_issue.py path/to/issue.json
python3 scripts/publish_issue.py --date 2026-11-25
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--no-commit` | Write files only |
| `--no-push` | Commit locally, do not push |
| `--rebuild` | Rebuild every page from `issues/*.md` |
| `--send` | Also send the issue through the Buttondown API |
| `-m, --message` | Custom commit message |

`--send` requires `BUTTONDOWN_API_KEY`. It `POST`s to `https://api.buttondown.com/v1/emails` with header `Authorization: Token $BUTTONDOWN_API_KEY` and a JSON body of `subject`, Markdown `body`, and `status: "about_to_send"`. If Buttondown's API version 2026-04-01 asks for first-send confirmation, the script retries once with `X-Buttondown-Live-Dangerously: true`. Nothing is sent unless you pass `--send`.

The script has no third-party Python dependencies.

## Local layout

```
_config.yml                 # site settings, including Buttondown username
assets/style.css
assets/favicon.svg
issues/YYYY-MM-DD.md        # source
issues/YYYY-MM-DD.html      # generated
index.html                  # generated (latest issue + subscribe form)
archive.html                # generated
feed.xml                    # generated RSS
404.html                    # generated
scripts/publish_issue.py
```

GitHub Pages is served from the `main` branch, site root.
