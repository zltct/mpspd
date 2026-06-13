# MPSPD cloud runner

This repo now supports a long-running public GitHub Actions scanner that publishes
plain static results to GitHub Pages.

## Local verification

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Tiny live smoke test:

```powershell
.\.venv\Scripts\python.exe .\mpspd.py scan `
  --seed-url https://images.meupatrocinio.com/325966/23670390/99/ `
  --output-dir .\smoke-output `
  --max-candidates 1 `
  --concurrency 1 `
  --max-runtime-seconds 10 `
  --retries 0 `
  --reset
```

## New GitHub account setup

Install Git for Windows and GitHub CLI if they are not available:

- https://git-scm.com/download/win
- https://cli.github.com/

Authenticate the new account:

```powershell
gh auth login
gh auth status
gh api user --jq .login
```

The final command must print the new account username, not the main account.

## Repository setup

Create a public repository under the new account. Either:

- fork `leonheart-squall/mpspd`; or
- create a fresh public repository and push this working tree there.

If this is the first cloud run and `gh-pages/state.json` does not exist yet,
provide a seed URL manually when running `MPSPD Scan`, or set a repository
variable:

- name: `MPSPD_SEED_URL`
- value: an existing image URL such as `https://images.meupatrocinio.com/325966/23670390/99/`

The default scan direction is descending: `--increment -1`. A seed like
`https://images.meupatrocinio.com/325966/15946361/89/` will search toward
photo number `88`, then `87`, and so on. Use `--increment 1` only when scanning
forward.

## GitHub Pages

After the first successful `MPSPD Scan` run, enable Pages:

- Settings -> Pages
- Source: Deploy from a branch
- Branch: `gh-pages`
- Folder: `/`

The generated files are:

- `index.html`
- `state.json`
- `found_links.jsonl`

## Running continuously

Start `MPSPD Scan` once from the Actions tab. It runs for about 5h50m, publishes
results, and dispatches another scan when it succeeds.

`MPSPD Watchdog` runs every 5 minutes. It only dispatches a scan when no scan is
queued or in progress.

To stop the loop, add a file named `STOP` to the `gh-pages` branch. Remove that
file to allow future manual/watchdog starts.
