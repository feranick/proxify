# Library-proxy PDF toolkit

Fetch paper PDFs through a library proxy (**EZproxy**) using your authenticated session
cookies. Three scripts, sharing one core:

| Script | Use when your input is… | Notes |
|--------|-------------------------|-------|
| **`proxify.py`** | a **.txt** list of URLs or DOIs (one per line) | The original workflow; resolves DOIs, proxies, downloads with `curl`. |
| **`proxify_csv.py`** | a **metadata CSV** (one paper per row, with `pdf_url`/`landing_url`/`title`/…) | Faster: uses the columns to skip resolution, fetch open-access PDFs directly, and name files by title+year. |
| **`fetch_browser.py`** | the `needs_browser.csv` either produces | Headless browser for JS/bot-gated publishers `curl` can't crack. |

All three write into the **same three folders** and produce compatible sidecar
CSVs, so you can chain them. Pick `proxify.py` **or** `proxify_csv.py`
depending on your input, then run `fetch_browser.py` on whatever gets routed to
the browser. See [proxify_csv.py](#csv-input-proxify_csvpy) and
[Fetching gated PDFs with a browser](#fetching-gated-pdfs-with-a-browser-fetch_browserpy).

---

# proxify.py

Convert a list of paper/article URLs (or DOIs) into library-proxy (EZproxy) URLs, and
optionally download the PDFs with `curl`.

## What it does

Given a text file with one URL (or DOI) per line, the script rewrites each URL
so it routes through `libproxy.example.edu`, writing the results to a new text file.
It supports two proxy styles and can also fetch each link. Downloads are sorted
into three folders:

| Folder | Contents |
|--------|----------|
| `downloads/` | Real PDFs only (verified by the `%PDF` magic bytes) |
| `landing_pages/` | HTML landing/viewer pages when no PDF could be obtained |
| `abstract_failed/` | Abstract extracted (as `.txt`) from those landing pages |

## Requirements

- Python 3 (standard library only — no packages to install)
- `curl` on your `PATH` (only needed for the `--download` option)
- Playwright + Chromium — **only** for the `fetch_browser.py` companion
  ([setup below](#setting-up-playwright-one-time))

## Set your proxy host

The scripts don't hard-code any institution. Point them at your library's
EZproxy host once via an environment variable:

```bash
export LIBPROXY_HOST=libproxy.your-university.edu
```

or per run with `--proxy-host libproxy.your-university.edu` (works on all three
scripts). If unset, the placeholder `libproxy.example.edu` is used and a warning
is printed — links won't resolve until you set your real host. The examples
below use `libproxy.example.edu`.

## Input format

A plain text file, one URL per line. Blank lines are kept, and lines starting
with `#` are treated as comments and passed through unchanged. Bare DOIs and
`doi.org` links are both supported (see [DOI links](#doi-links--r--resolve-doi)).

```
https://pubs.acs.org/doi/10.1021/acsnano.1c07693
https://www.nature.com/articles/s41586-020-2649-2.pdf
10.1111/jace.15314
```

## Proxy styles (`-m/--mode`)

### `host` (default) — hostname rewrite

Dots in the hostname become dashes, then `.libproxy.example.edu` is appended. The
path, query, and fragment are left unchanged.

```
https://pubs.acs.org/doi/10.1021/acsnano.1c07693
->
https://pubs-acs-org.libproxy.example.edu/doi/10.1021/acsnano.1c07693
```

### `login` — login redirect

The original URL is prepended with `https://libproxy.example.edu/login?url=`
(matches the browser bookmarklet form).

```
https://pubs.acs.org/doi/10.1021/acsnano.1c07693
->
https://libproxy.example.edu/login?url=https://pubs.acs.org/doi/10.1021/acsnano.1c07693
```

Add `-e/--encode` to percent-encode the original URL in the query string.
This is safer when a URL contains its own query parameters (an unencoded `&`
would otherwise break the target):

```
https://libproxy.example.edu/login?url=https%3A%2F%2Fpubs.acs.org%2Fdoi%2F10.1021%2Facsnano.1c07693
```

## Options

| Flag | Description |
|------|-------------|
| `infile` | Input file, one URL/DOI per line (default: `urls.txt`) |
| `outfile` | Output file (default: `<infile>_proxied.<ext>`) |
| `-m`, `--mode` | Proxy style: `host` (default) or `login` |
| `-e`, `--encode` | Percent-encode the original URL in `login` mode |
| `-d`, `--download` | Download each proxied link with `curl` |
| `-o`, `--outdir` | Folder for **real PDFs only** (default: `downloads`) |
| `--htmldir` | Folder for HTML landing pages (default: `landing_pages`) |
| `--abstractdir` | Folder for extracted abstracts (default: `abstract_failed`) |
| `-c`, `--cookies` | Netscape `cookies.txt` file for proxy authentication (with `-d`) |
| `-g`, `--pdf-guess` | Rewrite known landing/viewer URLs to direct-PDF URLs before proxying |
| `-f`, `--failfile` | CSV for links that didn't yield a PDF (default: `<infile>_failed.csv`) |
| `-b`, `--browserfile` | CSV for JS/bot-gated links needing the browser (default: `<infile>_needs_browser.csv`) |
| `-r`, `--resolve-doi` | Resolve `doi.org` links / bare DOIs to the publisher URL before proxying |
| `-j`, `--jobs` | Parallel workers for DOI resolution (default: 8) |
| `-u`, `--unresolved-file` | File for DOIs that fail to resolve (default: `<infile>_unresolved.<ext>`) |
| `--no-arxiv-oa` | Do **not** bypass the proxy for arXiv (default: fetch arXiv open-access, direct) |
| `-h`, `--help` | Show help |

## Usage

```bash
# Host rewrite (default) -> urls_proxied.txt
python3 proxify.py urls.txt

# Explicit output file
python3 proxify.py urls.txt proxied.txt

# Login-redirect form (optionally URL-encoded with -e)
python3 proxify.py -m login urls.txt

# Convert and download the PDFs into downloads/
python3 proxify.py -d urls.txt

# Download with authentication cookies into a custom folder
python3 proxify.py -d -c cookies.txt -o papers urls.txt

# Recommended for a DOI list: resolve, guess direct-PDF, download
python3 proxify.py -r -g -j 40 -d -c cookies.txt simple_test_dois.txt
```

## Downloading (`-d/--download`)

When `-d` is set, each proxied link is fetched with `curl`:

- follows redirects (`-L`) so the proxy login bounce is handled
- forces HTTP/1.1 (`--http1.1`) to avoid the common `curl: (56) ... unexpected
  eof while reading` error some publisher servers throw over HTTP/2
- retries failed transfers and sets a browser-like User-Agent (some publishers
  block curl's default agent)
- saves **real PDFs** into `--outdir` (default `downloads/`), one PDF per link

Filenames are derived from the **original** URL's path, e.g.
`https://.../acsnano.1c07693` becomes `acsnano.1c07693.pdf`.

Links to JS/bot-gated publishers that `curl` cannot fetch are **skipped** before
download and routed to the browser list — see
[Browser-required links](#browser-required-links).

### Success is judged by the file, not curl's exit code

Some publisher servers send the entire PDF and then close the TLS connection
without a clean `close_notify`. `curl` reports this as
`curl: (56) ... unexpected eof while reading` and exits non-zero **even though
the file is complete and valid**. To avoid false failures, the script inspects
what actually landed on disk after every download and classifies each link as:

- **PDF saved** — file exists and begins with `%PDF`, saved in `downloads/`. If
  `curl` also returned an error (e.g. exit 56), it's still counted as saved,
  with a note explaining the file is intact.
- **non-PDF page** — file exists but isn't a PDF (usually an HTML
  landing/viewer page). It is **moved to `landing_pages/`** (not left in
  `downloads/`), and the script tries to extract an abstract from it.
- **failed** — nothing usable was downloaded; any empty stub file is removed.

The run ends with a summary line, e.g.
`Summary: 3 PDF(s) saved into downloads/; 43 landing page(s) into landing_pages/ (30 abstract(s) into abstract_failed/); 14 failed; ...`

### Abstract extraction

When a link yields an HTML landing page instead of a PDF, the script makes a
best-effort attempt to pull the article abstract out of it and save it as a
`.txt` in `abstract_failed/`. It looks, in order, for: `citation_abstract` /
`dc.description` meta tags → JSON-LD `abstract`/`description` → an element whose
class or id contains `abstract` → `og:description` as a last resort. If nothing
plausible is found, no `.txt` is written and the reason column notes
`no abstract found`. Each abstract file is prefixed with the original DOI/URL
and the source URL for traceability.

### Failed-links file (CSV)

Any link that did **not** produce a valid PDF — both hard failures and non-PDF
landing pages — is written to a CSV (default `<infile>_failed.csv`, override
with `-f`) with columns `original_doi`, `title`, `year`, `proxied_url`,
`reason` (`title`/`year` are blank for plain-text input, populated from a CSV):

```csv
original_doi,title,year,proxied_url,reason
10.1111/jace.15314,Continuous flash sintering,2017,https://ceramics-onlinelibrary-wiley-com.libproxy.example.edu/doi/pdf/10.1111/jace.15314,not-a-PDF (HTML landing/viewer page); abstract -> Continuous_flash_sintering_2017.txt
10.2139/ssrn.3630430,,,https://www-ssrn-com.libproxy.example.edu/abstract=3630430,download failed (curl exit 22)
```

`proxied_url` is the exact URL `curl` attempted (after resolution and PDF
guessing), so you can tell at a glance whether a failure came from a bad guessed
URL or a genuinely gated publisher.

### Browser-required links

Publishers whose PDFs are gated behind JavaScript or bot-checks
(Elsevier ScienceDirect / `linkinghub`, SSRN, ResearchGate, Radware/perfdrive
bot-walls) are detected up front and written to `<infile>_needs_browser.csv`
(same three columns) **instead of being retried pointlessly with curl**. Feed
that file to `fetch_browser.py` — see
[Fetching gated PDFs with a browser](#fetching-gated-pdfs-with-a-browser-fetch_browserpy).
This file is only written if there are gated links in the run.

## Direct-PDF guessing (`-g/--pdf-guess`)

Many publisher URLs point to an article **landing** or **viewer** page rather
than the PDF itself (e.g. `/doi/10.x`, `/doi/epdf/10.x`). Downloading those
yields HTML, not a PDF. With `-g`, the script rewrites common patterns to their
direct-PDF form *before* proxying:

| Publisher | From | To |
|-----------|------|----|
| ACS | `/doi/10.x` | `/doi/pdf/10.x` |
| Science | `/doi/epdf/10.x` | `/doi/pdf/10.x` |
| Wiley | `/doi/abs/10.x` | `/doi/pdf/10.x` |
| Taylor & Francis | `/doi/abs/10.x` | `/doi/pdf/10.x` |
| SAGE | `/doi/abs/10.x` | `/doi/pdf/10.x` |
| Springer | `/article/<doi>` or `/chapter/<doi>` | `/content/pdf/<doi>.pdf` |
| Nature | `/articles/<id>` | `/articles/<id>.pdf` |
| MDPI | `/a/b/c/d` | `/a/b/c/d/pdf` |
| IOP | `/article/<doi>` | `/article/<doi>/pdf` |
| J-Stage | `/…/_article[/-char/ja]` | `/…/_pdf[-char/ja]` |

These are **heuristics** — they cover the common cases but aren't guaranteed for
every article or publisher. URLs that don't match any rule are passed through
unchanged.

### arXiv is fetched open-access

arXiv is free, so arXiv links (`arxiv.org/abs/<id>`) are rewritten to
`arxiv.org/pdf/<id>` and fetched **directly, bypassing the proxy** entirely.
Pass `--no-arxiv-oa` to disable this and route arXiv through libproxy like
everything else.

## DOI links (`-r/--resolve-doi`)

A DOI URL like `https://doi.org/10.1021/acsnano.1c07693` (or a bare DOI such as
`10.1021/acsnano.1c07693`) redirects to the publisher's **real** domain. If you
just proxy `doi.org` and let curl follow the redirect, the redirect target is
the un-proxied publisher URL — so you can lose proxy access and hit a paywall.

With `-r`, each DOI is resolved **first** against public (unproxied) `doi.org`
to obtain the final publisher URL, and the proxy (and `-g`, if set) is then
applied to that real URL. The processing order per link is:

```
DOI  --resolve-->  publisher landing URL  --pdf-guess (opt)-->  direct PDF  --proxify-->  libproxy URL
```

Resolution runs with its **own temporary cookie jar** so publisher consent
redirects (Springer, Nature, …) complete cleanly instead of dead-ending on a
`?error=cookies_not_supported` page; any such transient query junk is stripped
from the resolved URL. Recommended for DOI-heavy lists:

```bash
python3 proxify.py -r -g -d -c cookies.txt urls.txt
```

Notes:
- Resolution uses `curl` and needs network access; bare DOIs are auto-prefixed
  with `https://doi.org/`.
- If resolution fails (offline, DOI not found), the DOI is kept as a `doi.org`
  link so nothing is lost.
- Non-DOI URLs are untouched by this option.

### Input files that are just DOI numbers

A file containing one bare DOI per line (no `http`, no `doi.org`) is fully
supported — e.g.:

```
10.1111/jace.15314
10.1038/s41524-020-00359-7
10.1016/j.jeurceramsoc.2018.08.048
```

Each bare DOI is automatically turned into `https://doi.org/<doi>`. In plain
host mode that becomes `https://doi-org.libproxy.example.edu/<doi>`; with `-r` it is
resolved to the real publisher URL first (recommended, since the `doi.org`
redirect can otherwise escape the proxy). The script prints a reminder when it
sees bare DOIs without `-r`.

### Parallel resolution (`-j/--jobs`)

Resolving thousands of DOIs one at a time is slow, so `-r` resolves them
concurrently — 8 workers by default, tunable with `-j`. Resolution results are
de-duplicated and cached per run. (Downloads themselves remain sequential to
respect publisher rate limits and the shared cookie jar.)

A large list still takes a while: the run prints `Resolving N DOI(s)...` and
then a live `resolved X/N` counter that updates as it goes. Each DOI is capped
(~25s max) so one slow server can't stall the batch. **The proxied output file
is written only after resolution finishes** — until then it's normal to see just
the counter advancing. Raising `-j` (e.g. `-j 40`) speeds up a big list.

### Unresolved-DOI file

With `-r`, any DOI that never leaves `doi.org` — a dead/invalid DOI, or one the
resolver couldn't reach — is written to a separate file (default
`<infile>_unresolved.<ext>`, override with `-u`), one bare DOI per line, so you
can investigate or re-run just those.

## The three problem files at a glance

A full run can produce up to three "problem" lists:

| File | When | Contains |
|------|------|----------|
| `<infile>_unresolved.<ext>` | with `-r` | DOIs that never resolved past `doi.org` |
| `<infile>_failed.csv` | with `-d` | links that downloaded but weren't a valid PDF, or failed entirely |
| `<infile>_needs_browser.csv` | with `-d` | JS/bot-gated links for the browser step |

If a stage has no problems, its file is simply not written.

### Authentication

The proxy requires you to be authenticated. For downloads to return real PDFs
rather than a login page, provide an exported browser cookie file:

1. Log in to libproxy in your browser.
2. Export your cookies to a Netscape-format `cookies.txt` (e.g. via a
   "Get cookies.txt" browser extension).
3. Pass it with `-c cookies.txt`.

Use `host` mode for downloading. `login` mode without cookies will only fetch
the login page — the script prints a warning if you try this. Cookies expire —
if downloads start returning login pages, re-export a fresh `cookies.txt`.

---

# CSV input (`proxify_csv.py`)

When your input is a metadata CSV (one paper per row) rather than a plain list,
use `proxify_csv.py`. It shares all of `proxify.py`'s machinery — proxying,
`-g` PDF-guessing, `curl` download, abstract extraction, JS-gated routing, the
three output folders, and the `_failed.csv` / `_needs_browser.csv` sidecars — but
uses the CSV columns to work smarter and faster.

## Why it's faster

- **Skips DOI resolution** for any row that already has a `pdf_url` or
  `landing_url` (most of them), instead of hitting `doi.org` for every entry.
- **Fetches open-access PDFs directly, without the proxy.** Rows that are open
  access (`unpaywall_is_oa` true, or `access_class` of `open_pdf` /
  `oa_landing_only`) *and* have a direct `pdf_url` are downloaded straight from
  the publisher — the proxy is only used for closed / `needs_library` rows.
- **Names files by title + year**, e.g. `Flash_sintering_of_ceramics_2019.pdf`
  (spaces removed, accents folded to ASCII; duplicate title+year gets `_2`).

## Expected columns

A header row is required. These columns are used; extras are ignored and missing
ones are tolerated:

```
doi, title, year, unpaywall_is_oa, oa_status, pdf_url, landing_url,
doi_url, access_class
```

Per row, the URL to fetch is chosen in this order: `pdf_url` → `landing_url` →
`doi_url` → `https://doi.org/<doi>`. A row with only a DOI is resolved when `-r`
is given (same as the txt workflow).

## Usage

```bash
# Straightforward: download everything the CSV points to
python3 proxify_csv.py access_all_papers.csv -d -c cookies.txt

# Recommended: also resolve DOI-only rows and guess direct-PDF URLs
python3 proxify_csv.py access_all_papers.csv -r -g -j 40 -d -c cookies.txt
```

On startup it reports how it will route the rows, e.g.
`Loaded 1481 row(s): 564 open-access PDF(s) fetched directly, 596 row(s) are
DOI-only (use -r to resolve them).`

## Options

Same flags as `proxify.py` (`-d`, `-o`, `--htmldir`, `--abstractdir`, `-c`,
`-g`, `-f`, `-b`, `-r`, `-j`, `-u`, `--no-arxiv-oa`, `-m`), minus the
text-list-specific `-e`. The proxied URL list is written to
`<infile>_proxied.txt`.

## Re-running a report CSV

Because the `_failed.csv` and `_needs_browser.csv` files carry a `proxied_url`
column (plus `title`/`year`), you can feed either back into `proxify_csv.py` to
retry, or into `fetch_browser.py` for the browser step — filenames stay
consistent across all of them.

---

# Fetching gated PDFs with a browser (`fetch_browser.py`)

`curl` cannot pass the JavaScript / bot-checks used by Elsevier ScienceDirect,
Wiley, SSRN, ResearchGate, and similar sites — no matter which cookies it sends.
`proxify.py` lists those links in `<infile>_needs_browser.csv`.
`fetch_browser.py` picks that file up, drives a real Chromium via Playwright
(reusing your cookies so you stay logged in through the proxy), lets the page's
JavaScript run, finds the real PDF, and downloads it.

**Papers come first.** For each link it renders the page, locates the PDF (via
the `citation_pdf_url` meta tag that most publishers emit, then PDF anchors),
and downloads the actual PDF into `downloads/`. Only when no PDF is obtainable
does it save the rendered HTML to `landing_pages/` and the abstract to
`abstract_failed/` — the same three folders as the main script, so output
converges. Run both from the same directory.

**DOI landing fallback.** When the given link is a *direct PDF* URL and the
fetch fails, the page in the browser is the PDF endpoint's viewer/challenge
shell, which has no abstract. In that case — if the input row carries a DOI —
the script navigates to the DOI landing page (through the proxy) and retries
there: that page holds the real abstract, and sometimes a `citation_pdf_url`
that works even though the direct link didn't. The abstract file records which
URL it actually came from.

## Setting up Playwright (one time)

Playwright is only needed for this step. Install the package and its Chromium
build:

```bash
pip install playwright
python3 -m playwright install chromium
```

Notes:

- `python3 -m playwright install chromium` downloads a private copy of Chromium
  (~150 MB) into Playwright's own cache; it does **not** touch your normal
  Chrome install.
- On Linux you may also need the system libraries Chromium depends on:
  ```bash
  python3 -m playwright install-deps chromium
  ```
- If `pip install playwright` is blocked on a managed/system Python, use a
  virtual environment:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate        # Windows: .venv\Scripts\activate
  pip install playwright
  python3 -m playwright install chromium
  ```
- Verify:
  ```bash
  python3 -c "from playwright.sync_api import sync_playwright; print('playwright OK')"
  ```

## Usage

```bash
# First run: watch the browser, do a few, clear any one-time CAPTCHA
python3 fetch_browser.py simple_test_dois_needs_browser.csv -c cookies.txt --headful --limit 3

# Then let it run the whole list
python3 fetch_browser.py simple_test_dois_needs_browser.csv -c cookies.txt
```

Input may be either the `_needs_browser.csv` from `proxify.py` (it reads the
`proxied_url` column) or a plain text file with one URL per line. A results CSV
(`<infile>_browser_results.csv`) records the outcome of every link with status
`pdf` / `abstract` / `html_only` / `failed`.

## Options

| Flag | Description |
|------|-------------|
| `infile` | `needs_browser` CSV, or a plain URL list |
| `-c`, `--cookies` | Netscape `cookies.txt` for the proxy session |
| `-o`, `--outdir` | PDF output dir (default: `downloads`) |
| `--htmldir` | HTML landing-page dir (default: `landing_pages`) |
| `--abstractdir` | Abstract dir (default: `abstract_failed`) |
| `--headful` | Show the browser window (helps past some bot-checks) |
| `--delay` | Seconds between links (default: 0.3) |
| `--timeout` | Per-navigation timeout in ms (default: 30000) |
| `--settle` | ms to wait after load for JS rendering (default: 1200; 0 disables) |
| `--nav-wait` | `goto` wait condition: `load`, `domcontentloaded` (default), `commit` |
| `--full-resources` | Load images/CSS/fonts too (slower; blocked by default) |
| `--limit` | Only process the first N links (0 = all) |
| `--results` | Results CSV path (default: `<infile>_browser_results.csv`) |

### If it's still slow

By default the browser now blocks images/CSS/fonts (only the HTML is needed) and
skips the old `networkidle` wait, which were the main time sinks. A stuck page
is capped by `--timeout` (30 s). To go faster still: lower `--settle` (e.g.
`--settle 500`) or `--timeout`, and keep the default resource blocking on. If a
particular publisher needs its scripts/styles to reveal the PDF link, add
`--full-resources` for that run.

## Two honest expectations

- **Run `--headful` the first time.** Some sites (SSRN, ResearchGate especially)
  present a Cloudflare/CAPTCHA challenge you may need to clear once by hand; a
  visible window lets you do that.
- **The browser removes the bot barrier, not the paywall.** A PDF only downloads
  if your library subscription actually entitles you to that article. If the library doesn't
  subscribe to the title, there's no PDF to get.

## Troubleshooting

- **Playwright `Executable doesn't exist`** — you installed the pip package but
  not the browser; run `python3 -m playwright install chromium`.
- **Everything returns a login page** — your proxy session expired; re-export
  `cookies.txt`.
- **`error=cookies_not_supported` in a URL** — stale cookies; re-export.
  (`proxify.py -r` already strips these from resolved URLs.)

## Notes

- No content is modified beyond the hostname/prefix — DOIs and paths are
  preserved exactly.
- Non-URL lines (blanks, comments) are passed through so you can annotate your
  list.
- Download failures (paywall, expired cookies, network) are reported per link,
  and both scripts continue with the rest of the list.
