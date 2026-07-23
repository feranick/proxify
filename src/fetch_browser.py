#!/usr/bin/env python3
"""
Headless-browser companion to proxify.py for the JS/bot-gated publishers.

proxify.py fetches PDFs with curl, which cannot pass the JavaScript /
bot-checks used by Elsevier ScienceDirect, Wiley, SSRN, ResearchGate, etc. It
writes those links to <infile>_needs_browser.csv. THIS script picks that file
up, drives a real Chromium via Playwright (reusing your exported cookies so you
stay logged in through the library proxy), lets the page's JavaScript run, finds
the real PDF, and downloads it. When no PDF can be had it still saves the
rendered landing page and extracts the abstract — mirroring proxify.py's folders.

    downloads/         real PDFs (verified %PDF magic bytes)
    landing_pages/     rendered HTML when no PDF was obtainable
    abstract_failed/   abstract .txt extracted from those pages

--------------------------------------------------------------------------
SETUP (one time):
    pip install playwright
    playwright install chromium
--------------------------------------------------------------------------

Set your institution's proxy host via the LIBPROXY_HOST environment variable or
the --proxy-host flag (only needed to route relative PDF links that resolve to a
bare publisher domain).

Usage:
    # Typical: consume the list proxify.py produced
    python3 fetch_browser.py simple_test_dois_needs_browser.csv -c cookies.txt

    # A plain list of URLs (one per line) works too
    python3 fetch_browser.py urls.txt -c cookies.txt

    # See the browser, slow down, or test a subset
    python3 fetch_browser.py needs.csv -c cookies.txt --headful --delay 2 --limit 5

Input may be either the CSV proxify.py / proxify_csv.py writes (uses the
`proxied_url` column) or a plain text file with one URL per line. A results CSV
(<infile>_browser_results.csv) records the outcome for every link.
"""

import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import urlsplit, urljoin

# Reuse the tested helpers from the sibling script (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import proxify as pm
except Exception as e:  # pragma: no cover - only if the file is missing
    print(f"Error: could not import proxify.py from the same folder: {e}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Cookie jar (Netscape cookies.txt) -> Playwright cookie dicts
# --------------------------------------------------------------------------
def parse_netscape_cookies(path: str):
    """Parse a Netscape/libcurl cookies.txt into Playwright add_cookies() dicts.

    Handles the `#HttpOnly_` line prefix that curl uses to flag HttpOnly
    cookies. Session cookies (expires == 0) are emitted with expires -1.
    """
    cookies = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            http_only = False
            if raw.startswith("#HttpOnly_"):
                http_only = True
                raw = raw[len("#HttpOnly_"):]
            elif raw.startswith("#"):
                continue  # real comment
            parts = raw.split("\t")
            if len(parts) != 7:
                continue
            domain, include_sub, cpath, secure, expires, name, value = parts
            try:
                exp = int(expires)
            except ValueError:
                exp = 0
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": cpath or "/",
                "expires": float(exp) if exp > 0 else -1.0,
                "httpOnly": http_only,
                "secure": secure.upper() == "TRUE",
                "sameSite": "Lax",
            })
    return cookies


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------
def read_targets(path: str):
    """Return list of dicts: {original, url, title, year}.

    If `path` is a CSV proxify.py / proxify_csv.py writes (has a
    `proxied_url` header), use original_doi + proxied_url and carry title/year
    so downloaded files can be named consistently. Otherwise treat every
    non-comment line as a URL (first whitespace/tab token).
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.readline()
    if "proxied_url" in head:
        rows = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for r in csv.DictReader(f):
                url = (r.get("proxied_url") or "").strip()
                if url:
                    rows.append({
                        "original": (r.get("original_doi") or url).strip(),
                        "url": url,
                        "title": (r.get("title") or "").strip(),
                        "year": (r.get("year") or "").strip(),
                        "name": (r.get("filename") or "").strip(),
                    })
        return rows
    # plain list
    targets = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            url = s.split()[0].split("\t")[0]
            targets.append({"original": url, "url": url, "title": "", "year": "", "name": ""})
    return targets


def resolve_input(infile: str) -> str:
    """Find the actual needs_browser CSV to read.

    Accepts, for convenience, any of:
      * the file itself           (access_all_papers/needs_browser.csv)
      * its output folder         (access_all_papers/  or  access_all_papers)
      * the original input name    (access_all_papers.csv)
    and returns the folder's needs_browser.csv when one exists. Otherwise
    returns the path unchanged.
    """
    if os.path.isdir(infile):
        cand = os.path.join(infile, "needs_browser.csv")
        return cand if os.path.exists(cand) else infile
    if os.path.basename(infile) != "needs_browser.csv":
        cand = os.path.join(pm.output_root(infile), "needs_browser.csv")
        if os.path.exists(cand):
            return cand
    return infile


def normalize_nav_url(url: str) -> str:
    """Rewrite known flaky redirect stubs to the page they resolve to.

    Elsevier's `linkinghub.elsevier.com/retrieve/pii/<PII>` is only a redirect
    to `sciencedirect.com/science/article/pii/<PII>`; under automation that hop
    often hangs, so we jump straight to ScienceDirect (which loads and carries
    the abstract). Works for both proxied and bare forms.
    """
    m = re.search(r"linkinghub[.-]elsevier[.-]com(\.[^/]*)?/retrieve/pii/([^/?#]+)", url)
    if m:
        suffix, pii = m.group(1), m.group(2)   # suffix e.g. '.libproxy.mit.edu' (proxied)
        host = ("www-sciencedirect-com" + suffix) if suffix else "www.sciencedirect.com"
        return f"https://{host}/science/article/pii/{pii}"
    return url


# --------------------------------------------------------------------------
# PDF-link discovery on a rendered page
# --------------------------------------------------------------------------
def _abs_and_proxied(candidate: str, page_url: str) -> str:
    """Resolve a candidate href against the page, then route through the proxy
    if it points at a bare (un-proxied) publisher domain."""
    absu = urljoin(page_url, candidate)
    host = (urlsplit(absu).hostname or "").lower()
    if host and not host.endswith(pm.PROXY_SUFFIX):
        return pm.proxify(absu, mode="host")
    return absu


def discover_pdf_url(html: str, page_url: str):
    """Find the most likely direct-PDF URL on a rendered publisher page.

    Priority: <meta name="citation_pdf_url"> (almost universal among
    publishers), then anchors whose href ends in .pdf or contains /pdf.
    Returns an absolute, proxy-routed URL or None.
    """
    # 1) citation_pdf_url meta tag (either attribute order)
    for pat in (
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]*content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]*name=["\']citation_pdf_url["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            return _abs_and_proxied(m.group(1).strip(), page_url)

    # 2) anchors that look like a PDF
    for m in re.finditer(r'<a[^>]+href=["\'](.*?)["\']', html, re.IGNORECASE | re.DOTALL):
        href = m.group(1).strip()
        low = href.lower()
        if low.endswith(".pdf") or "/pdf" in low or "pdfft" in low or "/epdf" in low:
            return _abs_and_proxied(href, page_url)

    # 3) the page itself may already be a PDF endpoint
    if page_url.lower().endswith(".pdf") or "/pdf" in page_url.lower():
        return page_url
    return None


def landing_fallback_url(original: str, current_url: str):
    """A landing-page URL to try when the direct PDF attempt fails.

    Prefers the article's DOI page (routed through the proxy), which — unlike a
    PDF endpoint's viewer/challenge shell — actually contains the abstract, and
    often a working `citation_pdf_url`. Returns None if we have no DOI to fall
    back on, or if it would just re-fetch the page we're already on.
    """
    if pm.is_bare_doi(original) or pm.is_doi_like(original):
        fb = pm.proxify(pm.normalize_doi(original), mode="host")
        return fb if fb != current_url else None
    return None


def safe_name(original: str, url: str, index: int) -> str:
    """Stable base filename (no extension) from the DOI/original, else the URL."""
    cand = original if original and original != url else url
    cand = re.sub(r"^https?://", "", cand)
    cand = re.sub(r"[^A-Za-z0-9._-]", "_", cand).strip("_")
    cand = re.sub(r"_+", "_", cand)
    if not cand:
        cand = f"paper_{index:03d}"
    return cand[:120]


def abstract_text(html: str) -> str:
    """Extract an abstract from HTML text, tolerant of an older proxify.py
    that only has the file-based extract_abstract()."""
    fn = getattr(pm, "extract_abstract_text", None)
    if fn is not None:
        return fn(html)
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        return pm.extract_abstract(p)
    except Exception:
        return ""
    finally:
        try:
            os.remove(p)
        except OSError:
            pass


def safe_content(page):
    """page.content() that tolerates a page still navigating (client-side
    redirects, bot-wall bounces). Retries briefly, then falls back to the DOM."""
    for _ in range(3):
        try:
            return page.content()
        except Exception:
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
    try:
        return page.evaluate("() => document.documentElement.outerHTML")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Main browser loop
# --------------------------------------------------------------------------
def run(targets, cookies_path, outdir, htmldir, abstractdir,
        headful, delay, timeout_ms, nav_wait, settle_ms, block_resources):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed.\n"
              "  pip install playwright\n"
              "  playwright install chromium")
        sys.exit(1)

    os.makedirs(outdir, exist_ok=True)
    cookies = parse_netscape_cookies(cookies_path) if cookies_path else []
    print(f"Loaded {len(cookies)} cookie(s) from {cookies_path}" if cookies_path
          else "No cookie file given (you will likely hit paywalls).")

    results = []   # (original, url, status, saved_path)
    pdfs = abstracts = html_only = failed = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
            accept_downloads=True,
        )
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                print(f"Warning: some cookies were rejected ({e}); continuing.")

        # Big speedup: we only need the HTML (for the citation_pdf_url meta / PDF
        # links), then we fetch the PDF via the request API. Aborting images,
        # media, fonts and stylesheets avoids downloading megabytes of page
        # chrome per article. JavaScript still runs (needed for bot-checks).
        if block_resources:
            context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in
                    {"image", "media", "font", "stylesheet"}
                    else route.continue_()
                ),
            )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def fetch_once(target_url):
            """Navigate to target_url and try for a PDF.
            Returns (pdf_bytes_or_None, rendered_html)."""
            target_url = normalize_nav_url(target_url)   # skip flaky redirect stubs
            page.goto(target_url, wait_until=nav_wait, timeout=timeout_ms)
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            # If we still landed on a redirect stub, wait briefly for the bounce.
            if "linkinghub" in (page.url or ""):
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
            html = safe_content(page)
            pdf_url = discover_pdf_url(html, page.url)
            body = None
            if pdf_url:
                try:
                    resp = context.request.get(pdf_url, timeout=min(timeout_ms, 30000))
                    if resp.ok:
                        body = resp.body()
                except Exception:
                    body = None
            if not (body and body[:5].startswith(b"%PDF")):
                body = None
            return body, html

        total = len(targets)
        used_names = set()
        for i, t in enumerate(targets, start=1):
            original, url = t["original"], t["url"]
            print(f"[{i}/{total}] {url}")
            # Reuse the exact filename proxify computed (from the report CSV);
            # else derive from title+year; else from the URL.
            stem = t.get("name") \
                or pm.filename_from_title(t.get("title", ""), t.get("year", "")) \
                or safe_name(original, url, i)
            base, k = stem, 2
            while stem.lower() in used_names:   # avoid clobbering same-title papers
                stem = f"{base}_{k}"
                k += 1
            used_names.add(stem.lower())
            status = "failed"
            saved = ""
            try:
                # First attempt: the URL we were given (often a direct PDF link).
                body, html = fetch_once(url)
                abs_src = url

                # If that yielded no PDF *and* this page has no abstract (e.g. it
                # was a PDF endpoint's viewer/challenge shell), fall back to the
                # article's DOI landing page — which has the abstract, and often
                # a citation_pdf_url that works when the direct link didn't.
                if not body and not abstract_text(html):
                    fb = landing_fallback_url(original, page.url)
                    if fb:
                        print(f"        no PDF/abstract here — retrying via landing page")
                        try:
                            body2, html2 = fetch_once(fb)
                            if body2:
                                body = body2
                            if html2:
                                html, abs_src = html2, fb
                        except Exception as e:
                            print(f"        (landing fallback failed: {type(e).__name__})")

                if body:
                    dest = os.path.join(outdir, stem + ".pdf")
                    with open(dest, "wb") as fh:
                        fh.write(body)
                    pdfs += 1
                    status, saved = "pdf", dest
                    print(f"        OK: PDF saved -> {dest}")
                else:
                    # No PDF: save rendered HTML, try to extract an abstract.
                    os.makedirs(htmldir, exist_ok=True)
                    html_dest = os.path.join(htmldir, stem + ".html")
                    with open(html_dest, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    abstract = abstract_text(html)
                    if abstract:
                        os.makedirs(abstractdir, exist_ok=True)
                        abs_dest = os.path.join(abstractdir, stem + ".txt")
                        with open(abs_dest, "w", encoding="utf-8") as af:
                            af.write(f"# DOI/original: {original}\n"
                                     f"# Source URL:   {abs_src}\n\n{abstract}\n")
                        abstracts += 1
                        status, saved = "abstract", abs_dest
                        print(f"        no PDF — abstract saved -> {abs_dest}")
                    else:
                        html_only += 1
                        status, saved = "html_only", html_dest
                        print(f"        no PDF, no abstract — HTML saved -> {html_dest}")
            except Exception as e:
                failed += 1
                status = f"failed: {type(e).__name__}"
                print(f"        FAILED: {e}")

            results.append((original, url, status, saved))
            if delay:
                time.sleep(delay)

        context.close()
        browser.close()

    print(f"\nSummary: {pdfs} PDF(s), {abstracts} abstract(s), "
          f"{html_only} HTML-only, {failed} failed (of {len(targets)}).")
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Fetch JS/bot-gated PDFs with a headless browser, reusing cookies.")
    ap.add_argument("infile",
                    help="the needs_browser.csv, or its output folder, or the original "
                         "input name (the folder's needs_browser.csv is found "
                         "automatically); also accepts a plain URL list")
    ap.add_argument("-c", "--cookies", default=None,
                    help="Netscape cookies.txt for the proxy session")
    ap.add_argument("--proxy-host", default=None,
                    help="your EZproxy host (overrides LIBPROXY_HOST), e.g. libproxy.example.edu")
    ap.add_argument("--outroot", default=None,
                    help="parent folder for all output. Default: the folder the input "
                         "file is in (so it converges with proxify output), else the "
                         "input name without extension.")
    ap.add_argument("-o", "--outdir", default=None, help="PDF output dir (default: <outroot>/downloads)")
    ap.add_argument("--htmldir", default=None,
                    help="HTML landing-page dir (default: <outroot>/landing_pages)")
    ap.add_argument("--abstractdir", default=None,
                    help="abstract dir (default: <outroot>/abstract_failed)")
    ap.add_argument("--headful", action="store_true",
                    help="show the browser window (helps with some bot-checks)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds to wait between links (default: 0.3)")
    ap.add_argument("--timeout", type=int, default=30000,
                    help="per-navigation timeout in ms (default: 30000)")
    ap.add_argument("--settle", type=int, default=1200,
                    help="ms to wait after load for JS rendering (default: 1200; "
                         "0 disables). Replaces the slow 'networkidle' wait.")
    ap.add_argument("--nav-wait", default="domcontentloaded",
                    choices=["load", "domcontentloaded", "commit"],
                    help="Playwright wait_until for goto (default: domcontentloaded)")
    ap.add_argument("--full-resources", action="store_true",
                    help="load images/CSS/fonts too (slower; default blocks them "
                         "since only the HTML is needed)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N links (0 = all)")
    ap.add_argument("--results", default=None,
                    help="results CSV path (default: <outroot>/browser_results.csv)")
    args = ap.parse_args()
    pm.set_proxy_host(args.proxy_host)

    # Accept the folder / original CSV and locate needs_browser.csv inside it.
    infile = resolve_input(args.infile)
    if infile != args.infile:
        print(f"Using {infile}")

    # Output goes into the input file's folder if it has one (so PDFs converge
    # with what proxify produced), otherwise a folder named after the input.
    indir = os.path.dirname(infile)
    root = args.outroot or indir or pm.output_root(os.path.basename(infile))
    os.makedirs(root, exist_ok=True)
    outdir = args.outdir or os.path.join(root, "downloads")
    htmldir = args.htmldir or os.path.join(root, "landing_pages")
    abstractdir = args.abstractdir or os.path.join(root, "abstract_failed")
    results_path = args.results or os.path.join(root, "browser_results.csv")

    targets = read_targets(infile)
    if args.limit > 0:
        targets = targets[:args.limit]
    if not targets:
        print("No targets found in input.")
        return
    print(f"{len(targets)} link(s) to fetch via browser.")

    results = run(targets, args.cookies, outdir, htmldir, abstractdir,
                  args.headful, args.delay, args.timeout, args.nav_wait,
                  args.settle, not args.full_resources)

    with open(results_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["original_doi", "url", "status", "saved_path"])
        w.writerows(results)
    print(f"Wrote results to {results_path}")


if __name__ == "__main__":
    main()
