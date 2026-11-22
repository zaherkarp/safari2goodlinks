"""Page labels: read what a page says about itself (option 4).

Off by default (triage.page_hints = "off"). When on, collect downloads
each selected tab's page and looks for the labels sites put in their own
HTML for search engines and link previews:

    JSON-LD "@type"          Recipe, Article, Product, VideoObject, ...
    <meta property=og:type>  article, video.movie, product, book, ...
    <link rel=alternate type=application/rss+xml>  the site has a feed
    paragraph text length    rough reading time

The result is a list of soft labels added to the tab's line as tags:

    page:recipe  page:article  page:product  page:video  page:software
    page:event   page:book     page:course   page:qa     page:pdf
    page:feed    read:12min

Labels never move a tab by themselves. They show up in the triage file
to make sorting faster, and a rule can opt in with "page": ["recipe"].

Privacy: this is the one part of the tool that makes network requests.
It requests only pages that were already open in Safari, sends no
cookies (so logged-in pages are seen logged out), and sends no data
beyond a normal GET. Nothing is sent to any third party.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

# Ordinary browser user agent: some sites refuse urllib's default one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
MAX_BYTES = 800_000   # labels live in <head>; no need for whole pages
WORDS_PER_MIN = 230   # typical adult reading speed
MIN_WORDS_FOR_TIME = 400

# schema.org @type -> label. Several types collapse to one label because
# the distinction does not matter for sorting.
SCHEMA_LABELS = {
    "recipe": "recipe",
    "article": "article", "newsarticle": "article", "blogposting": "article",
    "techarticle": "article", "scholarlyarticle": "article", "report": "article",
    "analysisnewsarticle": "article", "opinionnewsarticle": "article",
    "product": "product", "productgroup": "product", "offer": "product",
    "videoobject": "video", "movie": "video", "tvepisode": "video",
    "softwaresourcecode": "software", "softwareapplication": "software",
    "event": "event",
    "book": "book",
    "course": "course",
    "faqpage": "qa", "qapage": "qa", "howto": "qa",
    "podcastepisode": "podcast", "podcastseries": "podcast",
}
OG_LABELS = {"article": "article", "product": "product", "book": "book",
             "video": "video", "music": "audio"}


class _PageParser(HTMLParser):
    """Collect only what the labels need; ignore everything else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og_type = None
        self.has_feed = False
        self.jsonld = []
        self.words = 0
        self._in_jsonld = False
        self._jsonld_buf = []
        self._p_depth = 0
        self._skip_depth = 0  # inside <script>/<style> that is not JSON-LD

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("property", a.get("name", "")).lower() == "og:type":
            self.og_type = a.get("content", "").strip().lower()
        elif tag == "link" and "alternate" in a.get("rel", "").lower() and \
                a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml", "application/feed+json"):
            self.has_feed = True
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_buf = []
            else:
                self._skip_depth += 1
        elif tag == "style":
            self._skip_depth += 1
        elif tag == "p":
            self._p_depth += 1

    def handle_endtag(self, tag):
        if tag == "script":
            if self._in_jsonld:
                self.jsonld.append("".join(self._jsonld_buf))
                self._in_jsonld = False
            elif self._skip_depth:
                self._skip_depth -= 1
        elif tag == "style" and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "p" and self._p_depth:
            self._p_depth -= 1

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
        elif self._p_depth and not self._skip_depth:
            self.words += len(data.split())


def _schema_types(blob):
    """Yield every @type in a JSON-LD blob, walking @graph and nesting."""
    stack = [blob]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            t = node.get("@type")
            for v in (t if isinstance(t, list) else [t]):
                if isinstance(v, str):
                    yield v.rsplit("/", 1)[-1].lower()  # "https://schema.org/Recipe" -> "recipe"
            # Look one level into the obvious containers only. Walking every
            # nested value would pick up e.g. an Organization "publisher" and
            # a Person "author" and drown the page's own type.
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                if key in node:
                    stack.append(node[key])


def labels_from_html(html: str):
    """Pure function: HTML text -> sorted list of label tags."""
    p = _PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # malformed HTML: keep whatever was collected
    labels = set()
    for raw in p.jsonld:
        try:
            blob = json.loads(raw.strip())
        except ValueError:
            continue
        for t in _schema_types(blob):
            if t in SCHEMA_LABELS:
                labels.add(SCHEMA_LABELS[t])
    if p.og_type:
        base = p.og_type.split(".", 1)[0]  # "video.movie" -> "video"
        if base in OG_LABELS:
            labels.add(OG_LABELS[base])
    if p.has_feed:
        labels.add("feed")
    tags = sorted("page:" + x for x in labels)
    if p.words >= MIN_WORDS_FOR_TIME:
        tags.append(f"read:{max(1, round(p.words / WORDS_PER_MIN))}min")
    return tags


_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.I)


def fetch_labels(url: str, timeout: float = 8.0):
    """Download one page and return (labels, error). Never raises."""
    if not url.startswith(("http://", "https://")):
        return [], "not http(s)"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if "pdf" in ctype:
                return ["page:pdf"], None
            if "html" not in ctype and "xml" not in ctype:
                return [], f"not a web page ({ctype or 'no content type'})"
            body = resp.read(MAX_BYTES)
            m = _CHARSET_RE.search(ctype)
            charset = m.group(1) if m else "utf-8"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [], str(getattr(e, "reason", e))
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:  # unknown charset name in the header
        text = body.decode("utf-8", errors="replace")
    return labels_from_html(text), None


def fetch_many(urls, workers: int = 8, timeout: float = 8.0):
    """Fetch labels for many URLs in parallel. Returns {url: (labels, error)}.

    Eight workers keeps a 150-tab batch to well under a minute without
    hammering any one site (tabs are usually spread over many sites).
    """
    urls = list(dict.fromkeys(urls))  # dedupe, keep order
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda u: fetch_labels(u, timeout), urls)
        return dict(zip(urls, results))
