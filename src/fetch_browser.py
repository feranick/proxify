#!/usr/bin/env python3
"""
Headless-browser companion to proxify.py for the JS/bot-gated publishers.

proxify.py lists the JS/bot-gated links (Elsevier ScienceDirect, Wiley, SSRN,
ResearchGate, etc.) in needs_browser.csv. THIS script picks that file up and
gets the PDF, or failing that the full readable HTML page.

Curl-first: each link is tried with curl first (fast, no browser); only links
that curl can't crack fall back to a real Chromium via Playwright (reusing your
cookies). If every link is handled by curl, the browser never launches.

    downloads/         real PDFs (verified %PDF magic bytes)
    abstract_failed/   when no PDF: the full HTML page as <stem>.html, plus its
                       companion graphical-abstract image as <stem>.<img>

--------------------------------------------------------------------------
SETUP (one time; only needed if any link falls back to the browser):
    pip install playwright
    python3 -m playwright install chromium
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
import shutil
import subprocess
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
    to ScienceDirect; under automation that hop often hangs, so we jump straight
    to the ScienceDirect article page. We target `/science/article/abs/pii/<PII>`
    — the exact page the redirect resolves to — so ScienceDirect doesn't have to
    do its own /pii/ -> /abs/ redirect on top. Works proxied and bare.
    """
    m = re.search(r"linkinghub[.-]elsevier[.-]com(\.[^/]*)?/retrieve/pii/([^/?#]+)", url)
    if m:
        suffix, pii = m.group(1), m.group(2)   # suffix e.g. '.libproxy.example.edu' (proxied)
        host = ("www-sciencedirect-com" + suffix) if suffix else "www.sciencedirect.com"
        return f"https://{host}/science/article/abs/pii/{pii}"
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


_IMG_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/tiff": ".tif",
    "image/svg+xml": ".svg",
}


def discover_image_url(html: str, page_url: str):
    """Find a companion graphical-abstract / thumbnail image on a landing page.

    Priority: an <img> inside a 'graphical'/'abstract' figure, then the
    `og:image` meta, then `<link rel="image_src">`. Returns an absolute,
    proxy-routed URL or None.
    """
    m = re.search(
        r'<(figure|div)[^>]*(?:class|id)=["\'][^"\']*(?:graphical|abstract)[^"\']*["\']'
        r'[^>]*>(.*?)</\1>', html, re.IGNORECASE | re.DOTALL)
    if m:
        im = re.search(r'<img[^>]+(?:data-src|src)=["\'](.*?)["\']',
                       m.group(2), re.IGNORECASE | re.DOTALL)
        if im and im.group(1).strip():
            return _abs_and_proxied(im.group(1).strip(), page_url)
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]*property=["\']og:image["\']',
    ):
        mm = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if mm and mm.group(1).strip():
            return _abs_and_proxied(mm.group(1).strip(), page_url)
    mm = re.search(r'<link[^>]+rel=["\']image_src["\'][^>]*href=["\'](.*?)["\']',
                   html, re.IGNORECASE | re.DOTALL)
    if mm and mm.group(1).strip():
        return _abs_and_proxied(mm.group(1).strip(), page_url)
    return None


def image_ext(content_type: str, url: str) -> str:
    """Pick a file extension from the response content-type, else the URL."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _IMG_EXT:
        return _IMG_EXT[ct]
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|tif|tiff|svg)(?:[?#]|$)", url, re.IGNORECASE)
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg").replace("tiff", "tif")
    return ".jpg"


def looks_like_image(data: bytes, content_type: str) -> bool:
    """True if bytes look like a real image (magic bytes) or a declared image type."""
    if content_type.split(";")[0].strip().lower() in _IMG_EXT:
        return True
    return (
        data[:3] == b"\xff\xd8\xff"                      # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"              # PNG
        or data[:6] in (b"GIF87a", b"GIF89a")           # GIF
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")  # WEBP
        or data[:4] in (b"II*\x00", b"MM\x00*")         # TIFF
    )


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


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def curl_bytes(url: str, cookies_path, timeout_s: int = 30) -> bytes:
    """Fetch a URL with curl (following redirects, reusing cookies). Returns
    the raw bytes, or b'' on any error. Bounded by timeout_s."""
    if shutil.which("curl") is None:
        return b""
    cmd = ["curl", "-sL", "--http1.1", "--connect-timeout", "15",
           "--max-time", str(timeout_s), "-A", UA]
    if cookies_path:
        cmd += ["-b", cookies_path, "-c", cookies_path]
    cmd += [url]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s + 15).stdout or b""
    except Exception:
        return b""


def html_is_usable(html: str) -> bool:
    """True if the HTML looks like a real article page (has an abstract or
    citation_* metadata) rather than a bot-wall/challenge/login stub."""
    if not html or len(html) < 200:
        return False
    if abstract_text(html):
        return True
    return any(k in html for k in ("citation_pdf_url", "citation_title", "citation_doi"))


def save_page(pagedir: str, stem: str, html: str, page_url: str,
              original: str, src_url: str, fetch_bytes):
    """Save the full HTML page as <stem>.html in pagedir (a readable file with
    the abstract plus whatever else the page shows), and the companion
    graphical-abstract image as <stem>.<ext> if one is found. `fetch_bytes(url)`
    returns (bytes, content_type). Returns (page_path, image_path_or_None)."""
    os.makedirs(pagedir, exist_ok=True)
    page_path = os.path.join(pagedir, stem + ".html")
    header = (f"<!-- saved by fetch_browser: DOI/original={original}; "
              f"source={src_url} -->\n")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(header + (html or ""))
    img_path = None
    img_url = discover_image_url(html or "", page_url)
    if img_url:
        data, ct = fetch_bytes(img_url)
        if data and looks_like_image(data, ct):
            img_path = os.path.join(pagedir, stem + image_ext(ct, img_url))
            with open(img_path, "wb") as f:
                f.write(data)
    return page_path, img_path


# --------------------------------------------------------------------------
# Main loop: curl-first, browser only for links curl can't crack
# --------------------------------------------------------------------------
def run(targets, cookies_path, outdir, pagedir, headful, delay, timeout_ms,
        nav_wait, settle_ms, block_resources, curl_first, pdf_timeout_ms,
        curl_timeout_ms):
    os.makedirs(outdir, exist_ok=True)
    results = []                                   # (original, url, status, saved_path)
    counts = {"pdf": 0, "abstract": 0, "html": 0, "failed": 0}
    used_names = set()
    total = len(targets)

    def make_stem(t, i):
        stem = (t.get("name")
                or pm.filename_from_title(t.get("title", ""), t.get("year", ""))
                or safe_name(t["original"], t["url"], i))
        base, k = stem, 2
        while stem.lower() in used_names:          # avoid clobbering same-name papers
            stem = f"{base}_{k}"
            k += 1
        used_names.add(stem.lower())
        return stem

    curl_secs = max(1, curl_timeout_ms // 1000)    # page fetch: bail fast on a stall
    pdf_secs = max(1, pdf_timeout_ms // 1000)
    curl_fetch = lambda u: (curl_bytes(u, cookies_path, pdf_secs), "")

    # ---- Phase 1: curl-first (no browser) ----
    browser_queue = []                             # (t, stem)
    if curl_first and shutil.which("curl"):
        for i, t in enumerate(targets, start=1):
            original, url = t["original"], t["url"]
            stem = make_stem(t, i)
            nav = normalize_nav_url(url)
            print(f"[curl {i}/{total}] {nav}")
            data = curl_bytes(nav, cookies_path, curl_secs)
            body = data if data.startswith(b"%PDF") else None
            html = "" if body else data.decode("utf-8", "ignore")
            if not body and html:                  # page may link to a PDF curl can grab
                pdf_url = discover_pdf_url(html, nav)
                if pdf_url:
                    pdata = curl_bytes(pdf_url, cookies_path, pdf_secs)
                    if pdata.startswith(b"%PDF"):
                        body = pdata
            if body:
                dest = os.path.join(outdir, stem + ".pdf")
                with open(dest, "wb") as f:
                    f.write(body)
                counts["pdf"] += 1
                results.append((original, url, "pdf", dest))
                print(f"        OK: PDF via curl -> {dest}")
            elif html and html_is_usable(html):
                pp, ip = save_page(pagedir, stem, html, nav, original, nav, curl_fetch)
                had = bool(abstract_text(html))
                counts["abstract" if had else "html"] += 1
                results.append((original, url, "abstract" if had else "html", pp))
                print(f"        page via curl -> {pp}" + (" (+image)" if ip else ""))
            else:
                browser_queue.append((t, stem))
                print("        curl blocked/empty -> queued for browser")
            if delay:
                time.sleep(delay)
    else:
        browser_queue = [(t, make_stem(t, i)) for i, t in enumerate(targets, start=1)]

    # ---- Phase 2: browser, only for what curl couldn't get ----
    if browser_queue:
        _run_browser(browser_queue, cookies_path, outdir, pagedir, headful, delay,
                     timeout_ms, nav_wait, settle_ms, block_resources, pdf_timeout_ms,
                     results, counts)
    else:
        print("\nAll links handled without the browser.")

    print(f"\nSummary: {counts['pdf']} PDF(s), {counts['abstract']} page(s) with abstract, "
          f"{counts['html']} page(s) without abstract, {counts['failed']} failed "
          f"(of {total}).")
    return results


def _run_browser(queue, cookies_path, outdir, pagedir, headful, delay, timeout_ms,
                 nav_wait, settle_ms, block_resources, pdf_timeout_ms, results, counts):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed (needed for the blocked links).\n"
              "  pip install playwright\n  python3 -m playwright install chromium")
        for t, _stem in queue:
            counts["failed"] += 1
            results.append((t["original"], t["url"], "failed: playwright-missing", ""))
        return

    cookies = parse_netscape_cookies(cookies_path) if cookies_path else []
    print(f"\nLaunching browser for {len(queue)} link(s) curl couldn't fetch "
          f"(loaded {len(cookies)} cookie(s)).")
    pdf_timeout = min(pdf_timeout_ms, 30000)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(user_agent=UA, accept_downloads=True)
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as e:
                print(f"Warning: some cookies were rejected ({e}); continuing.")
        # Only the HTML is needed to find the PDF/abstract; abort heavy assets.
        if block_resources:
            context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in {"image", "media", "font", "stylesheet"}
                    else route.continue_()
                ),
            )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def br_fetch(u):
            """(bytes, content_type) via the browser's authenticated request API,
            hard-bounded so a stalled PDF endpoint can't wedge the run."""
            try:
                r = context.request.get(u, timeout=pdf_timeout)
                if r.ok:
                    return (r.body(), r.headers.get("content-type", ""))
            except Exception:
                pass
            return (b"", "")

        def fetch_once(target_url):
            nav = normalize_nav_url(target_url)
            if nav != target_url:
                print(f"        -> {nav}")
            try:
                page.goto(nav, wait_until=nav_wait, timeout=timeout_ms)
            except Exception as e:
                print(f"        (navigation didn't fully settle: {type(e).__name__}; "
                      f"using partial page)")
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            if "linkinghub" in (page.url or ""):
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
            html = safe_content(page)
            body = None
            pdf_url = discover_pdf_url(html, page.url)
            if pdf_url:
                data, _ = br_fetch(pdf_url)
                if data.startswith(b"%PDF"):
                    body = data
            return body, html

        total = len(queue)
        for j, (t, stem) in enumerate(queue, start=1):
            original, url = t["original"], t["url"]
            print(f"[browser {j}/{total}] {url}")
            try:
                body, html = fetch_once(url)
                src = url
                # If no PDF and no abstract here, retry via the DOI landing page.
                if not body and not abstract_text(html):
                    fb = landing_fallback_url(original, page.url)
                    if fb:
                        print("        no PDF/abstract here — retrying via landing page")
                        try:
                            body2, html2 = fetch_once(fb)
                            if body2:
                                body = body2
                            if html2:
                                html, src = html2, fb
                        except Exception as e:
                            print(f"        (landing fallback failed: {type(e).__name__})")
                if body:
                    dest = os.path.join(outdir, stem + ".pdf")
                    with open(dest, "wb") as fh:
                        fh.write(body)
                    counts["pdf"] += 1
                    results.append((original, url, "pdf", dest))
                    print(f"        OK: PDF saved -> {dest}")
                else:
                    pp, ip = save_page(pagedir, stem, html, page.url, original, src, br_fetch)
                    had = bool(abstract_text(html))
                    counts["abstract" if had else "html"] += 1
                    results.append((original, url, "abstract" if had else "html", pp))
                    print(f"        no PDF — page saved -> {pp}" + (" (+image)" if ip else ""))
            except Exception as e:
                counts["failed"] += 1
                results.append((original, url, f"failed: {type(e).__name__}", ""))
                print(f"        FAILED: {e}")
            if delay:
                time.sleep(delay)

        context.close()
        browser.close()


def main():
    ap = argparse.ArgumentParser(
        description="Fetch gated PDFs/abstracts: curl-first, browser only when blocked.")
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
    ap.add_argument("--pagedir", "--abstractdir", default=None, dest="pagedir",
                    help="saved-HTML-page dir when no PDF (default: <outroot>/abstract_failed)")
    ap.add_argument("--no-curl-first", dest="curl_first", action="store_false",
                    help="skip the curl pass; use the browser for every link")
    ap.add_argument("--pdf-timeout", type=int, default=15000,
                    help="ms cap on each PDF-endpoint fetch (default: 15000) so a "
                         "stalled publisher can't wedge the run")
    ap.add_argument("--curl-timeout", type=int, default=20000,
                    help="ms cap on each curl page fetch (default: 20000); a stalled "
                         "page bails to the browser rather than hanging")
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
    pagedir = args.pagedir or os.path.join(root, "abstract_failed")
    results_path = args.results or os.path.join(root, "browser_results.csv")

    targets = read_targets(infile)
    if args.limit > 0:
        targets = targets[:args.limit]
    if not targets:
        print("No targets found in input.")
        return
    print(f"{len(targets)} link(s) to fetch.")

    results = run(targets, args.cookies, outdir, pagedir,
                  args.headful, args.delay, args.timeout, args.nav_wait,
                  args.settle, not args.full_resources, args.curl_first,
                  args.pdf_timeout, args.curl_timeout)

    with open(results_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["original_doi", "url", "status", "saved_path"])
        w.writerows(results)
    print(f"Wrote results to {results_path}")


if __name__ == "__main__":
    main()
