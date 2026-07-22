# Security Policy

## Scope

- Translation is performed via the Google Gemini API. Your `GEMINI_API_KEY`
  is read from a local `.env` file (see `.env.example`) — it is never
  committed, logged, or sent anywhere except directly to Google's API.
- The optional SSH/WARP tunnel forwards traffic to a remote host you control;
  no credentials for it are stored in this repo.
- Downloaded manga pages and translated output are cached locally
  (`cache.db`, `output/`) and are not uploaded anywhere by this project.

## Content and copyright

Chapters are fetched from MangaDex and translated for personal use. The
underlying manga content remains under its original publisher's copyright —
**this tool is for personal/local use only, not for redistributing or
publishing translated chapters commercially.** Use at your own risk with
respect to MangaDex's and the original publisher's terms.

## Reporting a vulnerability

If you find a security issue, please open a GitHub issue or contact
[@Aleksandr_NFA](https://github.com/Sanexxxx777).

## Supported versions

Latest `main` only.
