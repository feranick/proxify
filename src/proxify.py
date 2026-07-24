#!/usr/bin/env python3
"""
Convert PDF/article URLs into library-proxy (EZproxy) URLs, and optionally
download them.

Set your institution's proxy host once via the LIBPROXY_HOST environment
variable, or per run with --proxy-host. Examples below use libproxy.example.edu.

Two proxy styles are supported:

1. host  (default) -- EZproxy hostname-rewrite style:
       https://pubs.acs.org/doi/10.1021/acsnano.1c07693
       ->
       https://pubs-acs-org.libproxy.example.edu/doi/10.1021/acsnano.1c07693
   The hostname's dots become dashes, then the proxy host is appended.
   Scheme, path, query and fragment are unchanged.

2. login -- login-redirect style (matches the bookmarklet):
       https://pubs.acs.org/doi/10.1021/acsnano.1c07693
       ->
       https://libproxy.example.edu/login?url=https://pubs.acs.org/doi/10.1021/acsnano.1c07693

Downloading:
    Add -d/--download to fetch each proxied link with curl, saved into a folder
    (default "downloads/"). Pass an exported browser cookie file with
    -c/--cookies (Netscape cookie.txt format) so curl authenticates as you.

--------------------------------------------------------------------------
WHAT curl CAN AND CANNOT DO (important)
--------------------------------------------------------------------------
curl can fetch a PDF only when the publisher serves it at a real, direct
URL that returns application/pdf without running JavaScript. That covers,
e.g., Taylor & Francis, Springer /content/pdf/, Nature /articles/X.pdf,
MDPI /pdf, IOP /pdf, J-Stage /_pdf, arXiv, APS.

curl CANNOT retrieve PDFs from publishers that gate the file behind
JavaScript or a bot-check, no matter which cookies you send:
    * Elsevier ScienceDirect / linkinghub.elsevier.com  (retrieve/pii is a
      JS redirect stub, never a PDF)
    * SSRN, ResearchGate
    * Radware/perfdrive "validate" bot-walls
These are detected up front and written to <infile>_needs_browser.<ext>
rather than being retried pointlessly. Fetch those with a headless browser
that reuses the same cookies (see fetch_browser.py).

Wiley is a grey area: /doi/pdf works from some networks and bot-blocks
others. It is attempted, and lands in the failed list if it is blocked.
--------------------------------------------------------------------------
"""

import argparse
import csv
import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit, quote, parse_qsl, urlencode

__version__ = "2026.07.23.4"

# Your institution's EZproxy host. Set it via the LIBPROXY_HOST environment
# variable or the --proxy-host flag; the placeholder below is only a default.
PROXY_SUFFIX = os.environ.get("LIBPROXY_HOST", "libproxy.example.edu")
LOGIN_PREFIX = f"https://{PROXY_SUFFIX}/login?url="
DOI_HOSTS = {"doi.org", "dx.doi.org", "www.doi.org"}


def set_proxy_host(host: str) -> None:
    """Override the proxy host at runtime (updates both proxy styles)."""
    global PROXY_SUFFIX, LOGIN_PREFIX
    if host:
        PROXY_SUFFIX = host.strip().strip("/")
        LOGIN_PREFIX = f"https://{PROXY_SUFFIX}/login?url="
BARE_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# Query parameters that publishers add during consent/cookie redirects and that
# must be stripped from a resolved URL before we try to fetch it. Leaving them
# in makes curl re-request the "cookies not supported" page forever.
JUNK_QUERY_KEYS = {
    "error", "code", "cookieset", "cookies", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content", "wt_mc", "sap-outbound-id",
}

# Publisher hosts whose PDFs are gated behind JavaScript / bot-checks that curl
# cannot pass even with valid cookies. Matched as substrings of the *real*
# (un-proxied) resolved host. These are routed to the needs-browser list.
JS_GATED_HOST_SUBSTR = (
    "sciencedirect.com",
    "linkinghub.elsevier.com",
    "ssrn.com",
    "researchgate.net",
    "perfdrive.com",       # Radware bot-wall interstitial
    "validate.perfdrive",
)


def is_doi_like(url: str) -> bool:
    """True for doi.org links or a bare DOI such as 10.1021/acsnano.1c07693."""
    u = url.strip()
    if BARE_DOI_RE.match(u):
        return True
    host = (urlsplit(u).hostname or "").lower()
    return host in DOI_HOSTS


def is_bare_doi(url: str) -> bool:
    """True for a DOI with no scheme, e.g. 10.1021/acsnano.1c07693."""
    return bool(BARE_DOI_RE.match(url.strip()))


def normalize_doi(url: str) -> str:
    """Turn a bare DOI into a proper https://doi.org/ URL; leave others as-is."""
    u = url.strip()
    return "https://doi.org/" + u if is_bare_doi(u) else u


def clean_resolved_url(url: str) -> str:
    """Strip transient consent/tracking query params from a resolved URL.

    Publisher redirect chains (Springer, Nature, ...) append things like
    ?error=cookies_not_supported&code=<uuid>. Those are artefacts of the
    resolver's own request, not part of the real article URL, so we drop
    them. Any genuinely needed params are preserved.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in JUNK_QUERY_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


def resolve_doi(url: str, timeout: int = 15, max_time: int = 25) -> str:
    """Resolve a DOI to its final publisher URL via public (unproxied) doi.org.

    Uses a throwaway cookie jar so consent/cookie redirects complete cleanly
    instead of dead-ending on a "cookies_not_supported" page, then strips the
    transient query junk that such redirects leave behind. Returns the original
    url unchanged if curl is missing or resolution fails.
    """
    u = normalize_doi(url)
    if shutil.which("curl") is None:
        return u
    jar = None
    try:
        # Per-call temp jar: lets the redirect chain set + resend cookies so the
        # publisher does not bounce us to its cookies-error page.
        fd, jar = tempfile.mkstemp(prefix="proxify_resolve_", suffix=".txt")
        os.close(fd)
        r = subprocess.run(
            ["curl", "-sIL", "--connect-timeout", str(timeout),
             "--max-time", str(max_time),
             "-A", "Mozilla/5.0", "-b", jar, "-c", jar,
             "-o", os.devnull, "-w", "%{url_effective}", u],
            capture_output=True, text=True, timeout=max_time + 10,
        )
        final = (r.stdout or "").strip()
        return clean_resolved_url(final) if final.startswith("http") else u
    except (subprocess.SubprocessError, OSError):
        return u
    finally:
        if jar and os.path.exists(jar):
            try:
                os.remove(jar)
            except OSError:
                pass


def is_arxiv(url: str) -> bool:
    return "arxiv.org" in (urlsplit(url).hostname or "").lower()


def arxiv_pdf_url(url: str) -> str:
    """arxiv.org/abs/<id>  ->  arxiv.org/pdf/<id>  (open access, no proxy needed)."""
    parts = urlsplit(url)
    m = re.search(r"/(?:abs|pdf|format)/(.+)$", parts.path)
    if not m:
        return url
    arxiv_id = m.group(1).rstrip("/")
    arxiv_id = re.sub(r"\.pdf$", "", arxiv_id, flags=re.IGNORECASE)
    return f"https://arxiv.org/pdf/{arxiv_id}"


def is_js_gated(url: str) -> bool:
    """True if the (real) host is known to gate PDFs behind JS/bot-checks."""
    host = (urlsplit(url).hostname or "").lower()
    return any(sub in host for sub in JS_GATED_HOST_SUBSTR)


def proxify(url: str, mode: str = "host", encode: bool = False) -> str:
    url = url.strip()
    if not url or url.startswith("#"):
        return url

    parts = urlsplit(url)
    if not parts.netloc:
        return url  # not a proper URL, leave as-is

    if mode == "login":
        target = quote(url, safe="") if encode else url
        return LOGIN_PREFIX + target

    # host mode: hostname-rewrite
    host = parts.hostname or ""
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.split("@", 1)[0] + "@"
    port = f":{parts.port}" if parts.port else ""

    new_host = host.replace(".", "-") + "." + PROXY_SUFFIX
    new_netloc = f"{userinfo}{new_host}{port}"

    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def guess_pdf_url(url: str) -> str:
    """Best-effort rewrite of a publisher landing/viewer URL to a direct-PDF URL.

    Heuristics per publisher; not guaranteed correct for every article.
    Enabled with -g/--pdf-guess.
    """
    url = url.strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path

    new_path = path
    new_query = parts.query

    # The optional group includes `pdf/` so an already-direct /doi/pdf/ link
    # maps to itself (idempotent) instead of becoming /doi/pdf/pdf/.
    if "acs.org" in host:
        new_path = re.sub(r"/doi/(abs/|full/|epdf/|pdf/)?", "/doi/pdf/", path, count=1)
    elif "science.org" in host:
        new_path = re.sub(r"/doi/(epdf/|full/|abs/|pdf/)?", "/doi/pdf/", path, count=1)
    elif "wiley.com" in host:
        new_path = re.sub(r"/doi/(abs/|full/|epdf/|pdf/)?", "/doi/pdf/", path, count=1)
    elif "tandfonline.com" in host:
        new_path = re.sub(r"/doi/(abs/|full/|epub/|epdf/|pdf/)?", "/doi/pdf/", path, count=1)
    elif "sagepub.com" in host:
        new_path = re.sub(r"/doi/(abs/|full/|epub/|epdf/|pdf/)?", "/doi/pdf/", path, count=1)
    elif "springer.com" in host:
        # /article/<doi> or /chapter/<doi> -> /content/pdf/<doi>.pdf
        if "/article/" in path:
            new_path = path.replace("/article/", "/content/pdf/", 1)
        elif "/chapter/" in path:
            new_path = path.replace("/chapter/", "/content/pdf/", 1)
        if new_path != path and not new_path.endswith(".pdf"):
            new_path += ".pdf"
    elif "nature.com" in host:
        # /articles/<id> -> /articles/<id>.pdf
        if "/articles/" in path and not path.endswith(".pdf"):
            new_path = path.rstrip("/") + ".pdf"
    elif "mdpi.com" in host:
        # /a/b/c/d  ->  /a/b/c/d/pdf
        if not path.rstrip("/").endswith("/pdf"):
            new_path = path.rstrip("/") + "/pdf"
    elif "iopscience.iop.org" in host:
        # /article/<doi>  ->  /article/<doi>/pdf
        if "/article/" in path and not path.rstrip("/").endswith("/pdf"):
            new_path = path.rstrip("/") + "/pdf"
    elif "jstage.jst.go.jp" in host:
        # /article/.../_article/-char/ja  ->  /article/.../_pdf-char/ja
        # /article/.../_article               ->  /article/.../_pdf
        if "-char/" in path:
            new_path = re.sub(r"/_article/-char/", "/_pdf-char/", path, count=1)
        else:
            new_path = re.sub(r"/_article/?$", "/_pdf", path, count=1)

    if new_path == path and new_query == parts.query:
        return url  # no rule matched; leave unchanged
    return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, parts.fragment))


def is_pdf(path: str) -> bool:
    """True if the file starts with the %PDF magic bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _clean_html_text(s: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _meta_content(text: str, key: str, attr: str = "name") -> str:
    """Return the content of <meta {attr}="{key}" content="..."> (attr order
    doesn't matter). Scans each <meta> tag in isolation so it stays linear even
    on huge minified pages (no cross-tag backtracking)."""
    k = key.strip().lower()
    attr_re = re.compile(r'\b%s\s*=\s*["\']([^"\']*)["\']' % re.escape(attr), re.IGNORECASE)
    content_re = re.compile(r'\bcontent\s*=\s*(["\'])(.*?)\1', re.IGNORECASE | re.DOTALL)
    for m in re.finditer(r'<meta\b[^>]*>', text, re.IGNORECASE):
        tag = m.group(0)
        am = attr_re.search(tag)
        if not am or am.group(1).strip().lower() != k:
            continue
        cm = content_re.search(tag)
        if cm:
            return _clean_html_text(cm.group(2))
    return ""


def extract_abstract_text(text: str, min_len: int = 40) -> str:
    """Best-effort abstract extraction from HTML *text*.

    Tries, in order: citation_abstract / dc.description meta tags, JSON-LD
    'abstract'/'description', an element whose class or id contains 'abstract',
    then og:description / description as a last resort. Returns "" if nothing
    plausible (>= min_len chars) is found. Pure stdlib; no network.
    """
    # 1) publisher abstract meta tags
    for key in ("citation_abstract", "dc.description", "dcterms.abstract",
                "dc.Description", "DC.description"):
        val = _meta_content(text, key)
        if len(val) >= min_len:
            return val

    # 2) JSON-LD abstract / description (handles arrays and @graph).
    #    Bounded capture (.{0,500000}?) keeps the regex linear on huge minified
    #    publisher pages instead of backtracking catastrophically.
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.{0,500000}?)</script>',
            text, re.IGNORECASE | re.DOTALL):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        nodes = []
        for c in stack:
            if isinstance(c, dict) and isinstance(c.get("@graph"), list):
                nodes.extend(c["@graph"])
            else:
                nodes.append(c)
        for c in nodes:
            if isinstance(c, dict):
                for key in ("abstract", "description"):
                    v = c.get(key)
                    if isinstance(v, str):
                        val = _clean_html_text(v)
                        if len(val) >= min_len:
                            return val

    # 3) an element whose class/id mentions "abstract" (bounded capture)
    m = re.search(
        r'<(section|div|p)[^>]{0,400}(?:class|id)=["\'][^"\']*abstract[^"\']*["\']'
        r'[^>]{0,400}>(.{0,40000}?)</\1>',
        text, re.IGNORECASE | re.DOTALL)
    if m:
        val = _clean_html_text(m.group(2))
        if len(val) >= min_len:
            return val

    # 4) generic description meta (often boilerplate, so require a bit more)
    for key, attr in (("og:description", "property"), ("description", "name")):
        val = _meta_content(text, key, attr)
        if len(val) >= max(min_len, 60):
            return val

    return ""


def extract_abstract(html_path: str, min_len: int = 40) -> str:
    """Read a saved HTML file and extract its abstract (see
    extract_abstract_text). Returns "" if the file can't be read."""
    try:
        with open(html_path, "rb") as f:
            text = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""
    return extract_abstract_text(text, min_len)


def make_filename(orig_url: str, index: int) -> str:
    """Derive a sensible .pdf filename from the ORIGINAL url."""
    parts = urlsplit(orig_url)
    base = os.path.basename(parts.path.rstrip("/"))
    if not base:
        base = (parts.path.strip("/") or parts.netloc).replace("/", "_")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("_")
    if not base:
        base = f"paper_{index:03d}"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def _slug(text: str) -> str:
    """ASCII-fold, replace whitespace with '_', drop unsafe chars, collapse '_'."""
    t = unicodedata.normalize("NFKD", text or "")
    t = t.encode("ascii", "ignore").decode("ascii")    # drop accents & unicode
    t = re.sub(r"\s+", "_", t.strip())
    t = re.sub(r"[^A-Za-z0-9._-]", "", t)              # keep filename-safe chars
    return re.sub(r"_+", "_", t).strip("_.")


def first_author_lastname(authors: str) -> str:
    """Surname of the first author from an 'authors' field.

    Authors are separated by ';' (or ',' if no ';'); each is given-name-first
    (e.g. 'Eugene A. Olevsky'), so the surname is the last whitespace token of
    the first entry. Returns "" if empty.
    """
    if not authors:
        return ""
    sep = ";" if ";" in authors else ","
    first = authors.split(sep)[0]
    toks = first.replace(",", " ").split()
    return toks[-1] if toks else ""


def filename_from_meta(authors: str, title: str, year: str, max_len: int = 150) -> str:
    """Build a space-free, ASCII-safe base filename (no extension) as
    '<FirstAuthorSurname>_<Title>_<Year>', e.g.
    'Olevsky_Flash_Sintering_2018'. Missing parts are simply omitted.
    """
    surname = first_author_lastname(authors)
    base = _slug(" ".join(p for p in (surname, title) if p))
    y = re.sub(r"[^0-9]", "", str(year or ""))
    if base and y:
        base = f"{base}_{y}"
    elif y:
        base = y
    return base[:max_len].strip("_.")


def filename_from_title(title: str, year: str, max_len: int = 150) -> str:
    """Title+year filename (no author). Kept for callers without author data."""
    return filename_from_meta("", title, year, max_len)


def download(records, outdir: str, cookies, pagedir: str):
    """records: list of dicts with keys orig, proxied, gated.

    Fetch each non-gated link with curl. Real PDFs land in `outdir`. When the
    response is not a PDF (an HTML landing/viewer page), the whole page is saved
    into `pagedir` as <stem>.html — a readable file that carries the abstract
    plus whatever else the page shows. Gated links are skipped (curl cannot
    retrieve them) and reported separately. Returns (failures, needs_browser),
    each a list of (original_doi, filename, title, year, proxied_url, reason).
    """
    if shutil.which("curl") is None:
        raise SystemExit("Error: curl not found on PATH.")
    os.makedirs(outdir, exist_ok=True)

    ok = ok_warn = notpdf = fail = withabs = 0
    failures = []       # (original_doi, filename, title, year, proxied_url, reason)
    needs_browser = []  # (original_doi, filename, title, year, proxied_url, reason)
    used_names = set()  # for de-duplicating filenames
    total = len(records)
    for i, rec in enumerate(records, start=1):
        orig, proxied, gated = rec["orig"], rec["proxied"], rec["gated"]
        title, year = rec.get("title", ""), rec.get("year", "")

        # Compute the filename once (for every row, gated included) so the
        # browser step can reuse the exact same name via the report CSV.
        base = rec.get("name")
        if not base:
            fn = make_filename(orig, i)
            base = fn[:-4] if fn.lower().endswith(".pdf") else fn
        stem, k = base, 2
        while stem.lower() in used_names:   # avoid clobbering same-name papers
            stem = f"{base}_{k}"
            k += 1
        used_names.add(stem.lower())

        if gated:
            needs_browser.append((orig, stem, title, year, proxied,
                                  "JS/bot-gated publisher — needs a browser"))
            print(f"[{i}/{total}] {proxied}\n"
                  f"        SKIP: JS/bot-gated publisher (curl cannot fetch PDF) "
                  f"— added to needs-browser list.")
            continue

        fname = stem + ".pdf"
        dest = os.path.join(outdir, fname)
        cmd = [
            "curl", "-L",
            "--fail",
            "--http1.1",
            "--retry", "3",
            "--retry-connrefused",
            "--connect-timeout", "30",
            "-A", "Mozilla/5.0",
            "-o", dest,
            proxied,
        ]
        if cookies:
            cmd[1:1] = ["-b", cookies, "-c", cookies]

        print(f"[{i}/{total}] {proxied}\n        -> {dest}")
        result = subprocess.run(cmd)
        rc = result.returncode

        file_ok = os.path.exists(dest) and os.path.getsize(dest) > 0

        if file_ok and is_pdf(dest):
            if rc == 0:
                ok += 1
            else:
                ok_warn += 1
                print(f"        OK: complete PDF saved (curl reported exit {rc}, "
                      f"typically an unclean TLS close after full download "
                      f"— file is intact).")
        elif file_ok and not is_pdf(dest):
            notpdf += 1
            # Not a PDF: keep the whole page as a readable .html in pagedir.
            os.makedirs(pagedir, exist_ok=True)
            page_dest = os.path.join(pagedir, stem + ".html")
            try:
                os.replace(dest, page_dest)
                saved_as = page_dest
            except OSError:
                saved_as = dest
            has_abs = bool(extract_abstract(saved_as))
            if has_abs:
                withabs += 1
            abs_note = "with abstract" if has_abs else "no abstract detected"
            failures.append((orig, stem, title, year, proxied,
                             f"not-a-PDF (HTML page saved; {abs_note})"))
            note = "" if rc == 0 else f" (curl exit {rc})"
            print(f"        WARNING: not a PDF{note} — page saved to "
                  f"{pagedir}/{os.path.basename(saved_as)} ({abs_note}).")
        else:
            fail += 1
            failures.append((orig, stem, title, year, proxied, f"download failed (curl exit {rc})"))
            if os.path.exists(dest) and os.path.getsize(dest) == 0:
                try:
                    os.remove(dest)
                except OSError:
                    pass
            print(f"        FAILED: no file downloaded (curl exit {rc}).")

    print(f"\nSummary: {ok + ok_warn} PDF(s) saved"
          + (f" ({ok_warn} completed despite a curl warning)" if ok_warn else "")
          + f" into {outdir}/"
          + f"; {notpdf} HTML page(s) into {pagedir}/"
          + (f" ({withabs} with an abstract)" if withabs else "")
          + f"; {fail} failed"
          + (f"; {len(needs_browser)} skipped (need browser)" if needs_browser else "")
          + ".")
    return failures, needs_browser


def output_root(infile: str) -> str:
    """Folder that holds all output for an input file: the path with its
    extension stripped (keeping any directory). e.g. 'data/list.csv' ->
    'data/list', 'papers.txt' -> 'papers'."""
    root, _ = os.path.splitext(infile)
    return root or infile


def _default_sidecar(infile: str, suffix: str, ext: str | None = None) -> str:
    """<infile>_<suffix>.<ext>. If ext is None, keep the infile's extension."""
    if "." in infile:
        base, in_ext = infile.rsplit(".", 1)
        return f"{base}_{suffix}.{ext or in_ext}"
    return f"{infile}_{suffix}" + (f".{ext}" if ext else "")


def write_report_csv(path: str, rows) -> None:
    """rows: iterable of (original_doi, filename, title, year, proxied_url, reason)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["original_doi", "filename", "title", "year", "proxied_url", "reason"])
        w.writerows(rows)


def looks_like_csv(path: str) -> bool:
    """True if the input is a CSV table (rich access CSV or a report CSV)."""
    if path.lower().endswith(".csv"):
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.readline()
    except OSError:
        return False
    low = head.lower()
    return "," in head and ("doi" in low or "pdf_url" in low or "proxied_url" in low)


def load_csv_items(path: str):
    """Parse a CSV into download items. Understands the rich access CSV
    (doi/title/year/pdf_url/landing_url/doi_url/access_class/unpaywall_is_oa)
    and, for convenience, the report CSVs this script writes (which carry a
    `proxied_url`). Returns a list of dicts:
        {id, title, year, src, is_oa, has_direct_pdf, already_proxied}
    """
    with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        rows = list(csv.DictReader(f))
    items = []
    for r in rows:
        g = lambda k: (r.get(k) or "").strip()
        doi = g("doi") or g("original_doi")
        title, year, authors = g("title"), g("year"), g("authors")
        pdf_url, landing = g("pdf_url"), g("landing_url")
        doi_url, proxied_url = g("doi_url"), g("proxied_url")
        precomputed_name = g("filename")      # present when re-running a report CSV
        access = g("access_class").lower()
        is_oa = (g("unpaywall_is_oa").lower() == "true"
                 or access in {"open_pdf", "oa_landing_only"})

        already_proxied = has_direct_pdf = False
        if proxied_url:                       # re-running one of our report CSVs
            src, already_proxied = proxied_url, True
        elif pdf_url:                         # best case: a direct PDF link
            src, has_direct_pdf = pdf_url, True
        elif landing:
            src = landing
        elif doi_url:
            src = doi_url
        elif doi:
            src = "https://doi.org/" + doi
        else:
            continue                          # nothing usable in this row

        items.append({
            "id": doi or doi_url or src, "title": title, "year": year,
            "authors": authors, "name": precomputed_name,
            "src": src, "is_oa": is_oa, "has_direct_pdf": has_direct_pdf,
            "already_proxied": already_proxied,
        })
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert URLs into library-proxy (EZproxy) URLs.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("infile", nargs="?", default="urls.txt", help="input file (one URL per line)")
    ap.add_argument("outfile", nargs="?", default=None, help="output file (default: <infile>_proxied.<ext>)")
    ap.add_argument("--proxy-host", default=None,
                    help="your EZproxy host (overrides the LIBPROXY_HOST env var), "
                         "e.g. libproxy.example.edu")
    ap.add_argument("-m", "--mode", choices=["host", "login"], default="host",
                    help="proxy style: 'host' (hostname-rewrite, default) or 'login'")
    ap.add_argument("-e", "--encode", action="store_true",
                    help="percent-encode the original URL in login mode")
    ap.add_argument("-d", "--download", action="store_true",
                    help="download each proxied link with curl")
    ap.add_argument("--outroot", default=None,
                    help="parent folder for all output (default: the input filename "
                         "without its extension). Everything below goes inside it.")
    ap.add_argument("-o", "--outdir", default=None,
                    help="directory for real PDFs only (default: <outroot>/downloads)")
    ap.add_argument("--pagedir", "--abstractdir", default=None, dest="pagedir",
                    help="directory for saved HTML pages when no PDF is available "
                         "(default: <outroot>/abstract_failed)")
    ap.add_argument("-c", "--cookies", default=None,
                    help="Netscape cookie.txt file for proxy authentication (used with -d)")
    ap.add_argument("-g", "--pdf-guess", action="store_true",
                    help="rewrite known publisher landing/viewer URLs to direct-PDF URLs "
                         "(ACS, Science, Wiley, T&F, SAGE, Springer, Nature, MDPI, IOP, J-Stage)")
    ap.add_argument("-f", "--failfile", default=None,
                    help="CSV for links that did not yield a PDF (default: <outroot>/failed.csv)")
    ap.add_argument("-b", "--browserfile", default=None,
                    help="CSV for links that need a browser (default: <outroot>/needs_browser.csv)")
    ap.add_argument("-r", "--resolve-doi", action="store_true",
                    help="resolve doi.org links (and bare DOIs) to the final publisher URL first")
    ap.add_argument("-j", "--jobs", type=int, default=8,
                    help="parallel workers for DOI resolution (default: 8)")
    ap.add_argument("-u", "--unresolved-file", default=None,
                    help="file for DOIs that fail to resolve (default: <outroot>/unresolved.txt)")
    ap.add_argument("--no-arxiv-oa", action="store_true",
                    help="do NOT bypass the proxy for arXiv (arXiv is open access; bypass is default)")
    args = ap.parse_args()
    set_proxy_host(args.proxy_host)
    if PROXY_SUFFIX == "libproxy.example.edu":
        print("Warning: proxy host not set — using placeholder 'libproxy.example.edu'. "
              "Set LIBPROXY_HOST or pass --proxy-host for working links.")

    infile = args.infile
    # Everything for this input lives inside one folder named after it.
    root = args.outroot or output_root(infile)
    os.makedirs(root, exist_ok=True)
    outfile = args.outfile or os.path.join(root, "proxied.txt")
    outdir = args.outdir or os.path.join(root, "downloads")
    pagedir = args.pagedir or os.path.join(root, "abstract_failed")

    with open(infile, "r", encoding="utf-8") as f:
        lines = f.readlines()

    def line_url(line: str):
        s = line.strip()
        if not s or s.startswith("#"):
            return None
        return s.split()[0].split("#")[0]

    urls = [line_url(l) for l in lines]
    real = [u for u in urls if u is not None]
    n_bare = sum(1 for u in real if is_bare_doi(u))
    n_doi = sum(1 for u in real if is_doi_like(u))

    # Resolve DOIs (optionally in parallel) into a lookup table.
    resolved_map = {}
    if args.resolve_doi and n_doi:
        doi_urls = sorted({u for u in real if is_doi_like(u)})
        total = len(doi_urls)
        workers = max(1, args.jobs)
        print(f"Resolving {total} DOI(s) with {workers} worker(s) "
              f"(this can take a few minutes)...")
        results_map = {}
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(resolve_doi, u): u for u in doi_urls}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    results_map[src] = fut.result()
                except Exception:
                    results_map[src] = normalize_doi(src)
                done += 1
                if done % 25 == 0 or done == total:
                    print(f"\r  resolved {done}/{total}", end="", flush=True)
        print()
        results = [results_map[u] for u in doi_urls]
        n_changed = 0
        unresolved = []
        for src, res in zip(doi_urls, results):
            resolved_map[src] = res
            if (urlsplit(res).hostname or "").lower() in DOI_HOSTS:
                unresolved.append(src)
            else:
                n_changed += 1
        print(f"Resolved {n_changed}/{len(doi_urls)} DOI(s) to a publisher URL "
              f"({len(unresolved)} could not be resolved).")

        if unresolved:
            unres_file = args.unresolved_file or os.path.join(root, "unresolved.txt")
            with open(unres_file, "w", encoding="utf-8") as f:
                f.write("# DOIs that could not be resolved to a publisher URL\n")
                f.write("# (dead DOI, or the resolver was blocked/offline).\n")
                for src in unresolved:
                    f.write(src + "\n")
            print(f"Wrote {len(unresolved)} unresolved DOI(s) to {unres_file}")
    elif n_bare and not args.resolve_doi:
        print(f"Note: {n_bare} bare DOI(s) detected. Without -r/--resolve-doi they "
              f"are proxied as doi.org links, whose redirect may escape the proxy.")

    def resolved_src(url):
        if args.resolve_doi and is_doi_like(url):
            return resolved_map.get(url, normalize_doi(url))
        return normalize_doi(url)

    def transform(line, url):
        """Return (proxied_line, gated_bool) for output + download routing."""
        if url is None:
            return line, False
        src = resolved_src(url)

        # arXiv is open access; fetch it directly, bypassing the proxy.
        if not args.no_arxiv_oa and is_arxiv(src):
            return arxiv_pdf_url(src), False

        if args.pdf_guess:
            src = guess_pdf_url(src)

        gated = is_js_gated(src)
        return proxify(src, mode=args.mode, encode=args.encode), gated

    transformed = [transform(l, u) for l, u in zip(lines, urls)]
    converted = [t[0] for t in transformed]

    with open(outfile, "w", encoding="utf-8") as f:
        for line in converted:
            f.write(line.rstrip("\n") + "\n")

    print(f"Converted {len(real)} URL(s) [{args.mode} mode]: {infile} -> {outfile}")

    if args.download:
        records = []
        for orig_line, (prox, gated) in zip(lines, transformed):
            o = line_url(orig_line)
            if o is not None:
                records.append({"orig": o, "proxied": prox.strip(), "gated": gated})

        if args.mode == "login" and not args.cookies:
            print("Note: login mode without -c/--cookies will just fetch the login page.")

        failures, needs_browser = download(
            records, outdir, args.cookies, pagedir)

        if failures:
            failfile = args.failfile or os.path.join(root, "failed.csv")
            write_report_csv(failfile, failures)
            print(f"Wrote {len(failures)} failed link(s) to {failfile}")

        if needs_browser:
            browserfile = args.browserfile or os.path.join(root, "needs_browser.csv")
            write_report_csv(browserfile, needs_browser)
            print(f"Wrote {len(needs_browser)} browser-required link(s) to {browserfile}")

        if not failures and not needs_browser:
            print("All links produced a valid PDF.")


if __name__ == "__main__":
    main()
