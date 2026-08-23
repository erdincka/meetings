"""A tiny static corpus service.

`search_corpus` exists so a persona can cite something from outside the company.
Pointing it at the live internet would make the demo non-hermetic -- results
would drift, runs would not be reproducible, and it would need egress to
arbitrary hosts, which is precisely what the sandbox network policy is designed
to prevent.

So this serves a fixed, small set of pseudo-industry documents over plain HTTP.
Ranking is deliberately naive: term overlap, no embeddings. The point is to give
agents citable external material with stable provenance, not to be a search
engine.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CORPUS = json.loads((Path(__file__).parent / "corpus.json").read_text())
STOPWORDS = frozenset(
    "a an and are as at be by for from has how in is it its of on or that the to was were what "
    "when where which who will with".split()
)


def _terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS and len(t) > 2}


def search(query: str, top_k: int = 3) -> list[dict[str, object]]:
    wanted = _terms(query)
    if not wanted:
        return []

    scored = []
    for doc in CORPUS:
        haystack = _terms(f"{doc['title']} {doc['text']}")
        overlap = len(wanted & haystack)
        if overlap:
            scored.append((overlap, doc))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "id": doc["id"],
            "title": doc["title"],
            "source": doc["source"],
            "date": doc["date"],
            "excerpt": doc["text"][:600],
            "score": score,
        }
        for score, doc in scored[:top_k]
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._reply(200, {"status": "ok", "documents": len(CORPUS)})
            return
        if parsed.path != "/search":
            self._reply(404, {"error": "not found"})
            return

        params = parse_qs(parsed.query)
        query = (params.get("q") or [""])[0]
        try:
            top_k = min(int((params.get("top_k") or ["3"])[0]), 10)
        except ValueError:
            top_k = 3
        self._reply(200, {"results": search(query, top_k)})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("corpus: " + fmt % args + "\n")


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
