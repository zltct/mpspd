# MPSPD

MPSPD is a small Python scanner for probing image URLs that follow this shape:

```text
https://images.meupatrocinio.com/<profile_id>/<photo_id>/<photo_number>/
```

It stores found image links instead of downloading the full files. It can run locally, or from a public GitHub fork using GitHub Actions and GitHub Pages.

Use this only for profiles and image URLs you are allowed to access. The scanner does not bypass authentication or permissions.

## How It Works

Start from one known image URL. The scanner probes nearby `photo_id` values while tracking the current `photo_number`.

By default it scans backward:

```text
.../325966/15946361/89/
.../325966/15946360/88/
.../325966/15946359/87/
```

Use `--increment 1` to scan forward instead.

Found links are written to:

- `found_links.jsonl`
- `state.json`
- `index.html`

You can also add links manually in `manual_links.txt`; they will be included in the generated page.

## Run Locally

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run a short scan:

```bash
python mpspd.py scan \
  --seed-url "https://images.meupatrocinio.com/PROFILE_ID/PHOTO_ID/PHOTO_NUMBER/" \
  --output-dir public \
  --increment -1 \
  --concurrency 50 \
  --max-runtime-seconds 300
```

Open `public/index.html` to view results.

## Run In A GitHub Fork

1. Fork this repository.
2. In your fork, go to **Settings -> Actions -> General** and ensure workflows are allowed.
3. Go to **Settings -> Pages**.
4. Select **Deploy from a branch**.
5. Use branch `gh-pages` and folder `/` after the first scan creates that branch.
6. Go to **Settings -> Secrets and variables -> Actions -> Variables**.
7. Add a repository variable:

```text
MPSPD_SEED_URL=https://images.meupatrocinio.com/PROFILE_ID/PHOTO_ID/PHOTO_NUMBER/
```

8. Open the **Actions** tab.
9. Run **MPSPD Scan** manually.

The scanner runs for about 5 hours and 50 minutes by default, publishes `gh-pages`, then starts the next scan. The watchdog workflow runs every 5 minutes and starts a scan if none is queued or running.

## Workflow Inputs

`MPSPD Scan` accepts these manual inputs:

- `seed_url`: starting image URL; used when there is no state or when resetting.
- `increment`: `-1` to scan backward, `1` to scan forward.
- `concurrency`: concurrent HTTP probes. Default: `50`.
- `max_runtime_seconds`: scan duration before publishing and restarting. Default: `21000`.
- `reset_state`: reset from `seed_url`.
- `continue_loop`: start another scan when this run finishes.

Use a lower `max_runtime_seconds`, such as `900` or `1800`, if you want the public page to update every 15-30 minutes instead of after the long run completes.

## Manual Links

Do not edit `index.html` directly. It is generated and will be overwritten.

To add skipped or known links, edit `manual_links.txt` on the `gh-pages` branch. Put one URL per line:

```text
# optional comments are allowed
https://images.meupatrocinio.com/PROFILE_ID/PHOTO_ID/PHOTO_NUMBER/
```

Blank lines and lines starting with `#` are ignored.

## Stop Or Resume

To stop the loop, add a file named `STOP` to the `gh-pages` branch. Remove it to allow future manual or watchdog starts.

To resume from a new URL, run **MPSPD Scan** manually with:

- `seed_url` set to the new URL
- `reset_state=true`
- `increment=-1` or `1`

## Tests

```bash
python -m unittest discover -s tests
```
