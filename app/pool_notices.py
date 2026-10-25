# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Read-only polling of locally allowlisted official pool notices.

Polling is explicit; snapshot reads only a local cache. A first successful poll
establishes a baseline and never creates a new-notice event.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import urllib.request
import xml.etree.ElementTree as ET


MAX_RESPONSE = 512 * 1024
MAX_TEXT = 500
MAX_ITEMS = 30
MAX_UNREAD = 100
MAX_SEEN = 1000
STALE_AFTER = 3600
MIN_POLL_INTERVAL = 15 * 60

# Sites and fields are code-owned. The dashboard can select only these ids.
SOURCES = {
    "QTC": {"url": "https://www.quanpool.com/api/terms", "kind": "json_announcement",
            "field": "announcement", "label": "QuanPool 官方公告"},
    "TSC": {"url": "https://tsc-miner.tiger-pool.com/", "kind": "tsc_html",
            "display_url": "https://tsc-miner.tiger-pool.com/#/announcements",
            "label": "TSC Tiger Pool 官方公告"},
    "NOID": {"url": "https://noid.innovlab.cc/api/coins/parano1d/announcements", "kind": "noid_json",
             "label": "NOID 官方矿池公告"},
}


class NoticeError(RuntimeError):
    pass


class _Text(HTMLParser):
    def __init__(self, target_id=None):
        super().__init__(convert_charrefs=True)
        self.target_id = target_id
        self.depth = 0
        self.blocked = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.blocked += 1
        if self.target_id and not self.depth and dict(attrs).get("id") == self.target_id:
            self.depth = 1
        elif self.depth:
            self.depth += 1
        if self.depth and tag in {"br", "p", "li", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.blocked:
            self.blocked -= 1
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.blocked and (self.target_id is None or self.depth):
            self.parts.append(data)


class _TscAnnouncements(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.section = 0
        self.mode = None
        self.current = None
        self.rows = []
        self.blocked = 0
        self.found_section = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag in {"script", "style", "noscript", "svg"}:
            self.blocked += 1
        if tag == "section" and {"page", "announcements-page"} <= classes and attrs.get("data-page") == "announcements":
            self.section += 1
            self.found_section = True
        elif self.section and tag == "article" and "announcement-item" in classes and not self.depth:
            self.depth = 1
            self.current = {"title": [], "body": [], "published_at": None, "item_id": None}
        elif self.depth:
            self.depth += 1
        if self.depth and self.current is not None:
            if tag == "time":
                self.current["published_at"] = attrs.get("datetime")
            if tag == "h3":
                self.mode = "title"
            elif tag == "p" and "announcement-kind" not in classes:
                self.mode = "body"
            if tag == "a" and attrs.get("data-announcement-open"):
                self.current["item_id"] = attrs["data-announcement-open"]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.blocked:
            self.blocked -= 1
        if tag in {"h3", "p"}:
            self.mode = None
        if self.depth:
            self.depth -= 1
            if not self.depth and self.current is not None:
                row = self.current
                self.rows.append({"title": _clean(" ".join(row["title"]))[:120],
                                  "text": _clean(" ".join(row["body"])),
                                  "published_at": _clean(row["published_at"]) if row["published_at"] else None,
                                  "item_id": _clean(row["item_id"]) if row["item_id"] else None,
                                  "revision": hashlib.sha256(_clean(" ".join(row["title"] + row["body"]), None).encode()).hexdigest()})
                self.current = None
        elif tag == "section" and self.section:
            self.section -= 1

    def handle_data(self, data):
        if not self.blocked and self.current is not None and self.mode in {"title", "body"}:
            self.current[self.mode].append(data)


def _clean(value, limit=MAX_TEXT):
    parser = _Text()
    parser.feed(str(value))
    text = " ".join(unescape("".join(parser.parts)).split())
    text = re.sub(r"(?i)\b(password|token|secret|private[_ -]?key|api[_ -]?key)\s*[:=]\s*\S+",
                  r"\1=[redacted]", text)
    text = re.sub(r"\b(?:0x[0-9a-fA-F]{40,}|[A-Za-z0-9]{28,})\b", "[redacted address]", text)
    return text[:limit] if limit is not None else text


def _fetch(url, limit=MAX_RESPONSE):
    request = urllib.request.Request(url, headers={"Accept": "application/json, application/rss+xml, application/atom+xml, text/html",
                                                   "User-Agent": "miner-maintenance/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        actual = urlsplit(response.url)
        wanted = urlsplit(url)
        if actual.scheme != "https" or actual.hostname != wanted.hostname:
            raise NoticeError("Official source redirected to another host")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise NoticeError("Official source response exceeds limit")
    return data


def _entries(payload, spec):
    kind = spec["kind"]
    if kind == "json_announcement":
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise NoticeError("Invalid official announcement data")
        if spec["field"] not in document:
            raise NoticeError("Official announcement field missing")
        value = document.get(spec["field"])
        if value is None or value == "":
            return []
        if not isinstance(value, str):
            raise NoticeError("Invalid official announcement field")
        content = _clean(value)
        return [{"title": content[:120], "text": content, "published_at": None,
                 "revision": hashlib.sha256(_clean(value, None).encode()).hexdigest()}] if content else []
    if kind == "tsc_html":
        parser = _TscAnnouncements()
        parser.feed(payload.decode("utf-8"))
        if not parser.found_section:
            raise NoticeError("Official announcement section missing")
        return [row for row in parser.rows[:MAX_ITEMS] if row["title"] or row["text"]]
    if kind == "noid_json":
        document = json.loads(payload)
        if not isinstance(document, list):
            raise NoticeError("Invalid NOID announcement data")
        rows = []
        for item in document[:MAX_ITEMS]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), (str, int)) or
                    not isinstance(item.get("title"), str) or not isinstance(item.get("body"), str)):
                raise NoticeError("Invalid NOID announcement item")
            rows.append({"item_id": str(item["id"])[:80], "title": _clean(item["title"])[:120],
                         "text": _clean(item["body"]),
                         "published_at": _clean(item["created_at"]) if isinstance(item.get("created_at"), str) else None,
                         "revision": hashlib.sha256((item["title"] + "\n" + item["body"]).encode()).hexdigest()})
        return rows
    if kind in {"rss", "atom"}:
        root = ET.fromstring(payload)
        item_tag = "item" if kind == "rss" else "{http://www.w3.org/2005/Atom}entry"
        result = []
        for node in root.iter(item_tag):
            if len(result) >= MAX_ITEMS:
                break
            if kind == "rss":
                title = node.findtext("title") or ""
                body = node.findtext("description") or ""
                published = node.findtext("pubDate")
            else:
                ns = "{http://www.w3.org/2005/Atom}"
                title = node.findtext(ns + "title") or ""
                body = node.findtext(ns + "summary") or node.findtext(ns + "content") or ""
                published = node.findtext(ns + "published") or node.findtext(ns + "updated")
            clean_title, clean_body = _clean(title), _clean(body)
            if clean_title or clean_body:
                result.append({"title": clean_title[:120], "text": clean_body, "published_at": _clean(published) if published else None})
        return result
    if kind == "html_watch":
        parser = _Text(spec["element_id"])
        parser.feed(payload.decode("utf-8"))
        content = _clean(" ".join(parser.parts))
        if not content:
            raise NoticeError("Official watch section unavailable")
        return [{"title": "官方网站指定区域文本变化", "text": content, "published_at": None}]
    raise NoticeError("Unsupported official source")


def _identity(row):
    return hashlib.sha256(json.dumps([row.get("item_id"), row["title"], row["text"], row["published_at"],
                                      row.get("revision")],
                                     ensure_ascii=False).encode()).hexdigest()


class NoticeChecker:
    def __init__(self, state_dir, source_ids=None, fetcher=None, now=None):
        self.state_dir = Path(state_dir)
        self.source_ids = tuple(source_ids) if source_ids is not None else tuple(SOURCES)
        if any(source not in SOURCES for source in self.source_ids) or len(set(self.source_ids)) != len(self.source_ids):
            raise NoticeError("Unknown official notice source")
        self.fetcher = fetcher or _fetch
        self.now = now or time.time

    def snapshot(self):
        try:
            cached = json.loads((self.state_dir / "pool-notices.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if not isinstance(cached, dict):
            cached = {}
        current = int(self.now())
        result = {}
        for source in self.source_ids:
            row = cached.get(source, {})
            row = row if isinstance(row, dict) else {}
            last_success = row.get("last_success_at")
            stale = not isinstance(last_success, int) or current - last_success > STALE_AFTER
            result[source] = {"source": SOURCES[source]["label"],
                              "url": SOURCES[source].get("display_url", SOURCES[source]["url"]),
                              "kind": SOURCES[source]["kind"], "status": row.get("status", "unchecked"),
                              "checked_at": row.get("checked_at"), "last_success_at": last_success,
                              "baseline_at": row.get("baseline_at"), "stale": stale,
                              "new": row.get("new", []), "entries": row.get("entries", []),
                              "overflow": bool(row.get("overflow", False)),
                              "error": row.get("error")}
        return result

    @contextlib.contextmanager
    def _locked(self):
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state_dir / "pool-notices.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def check_notices(self):
        with self._locked():
            return self._check_locked()

    def _check_locked(self):
        previous = self.snapshot()
        current = int(self.now())
        try:
            raw_cache = json.loads((self.state_dir / "pool-notices.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_cache = {}
        if not isinstance(raw_cache, dict):
            raw_cache = {}
        result = {}
        for source in self.source_ids:
            spec = SOURCES[source]
            old = previous[source]
            old_raw = raw_cache.get(source, {})
            if not isinstance(old_raw, dict):
                old_raw = {}
            if isinstance(old.get("checked_at"), int) and current - old["checked_at"] < MIN_POLL_INTERVAL:
                result[source] = old_raw
                continue
            try:
                payload = self.fetcher(spec["url"], MAX_RESPONSE)
                if len(payload) > MAX_RESPONSE:
                    raise NoticeError("Official source response exceeds limit")
                rows = _entries(payload, spec)
                prior_entries = {row["id"]: row for row in old.get("entries", [])
                                 if isinstance(row, dict) and "id" in row}
                for row in rows:
                    row["id"] = _identity(row)
                    row["source"] = spec["label"]
                    row["url"] = spec.get("display_url", spec["url"])
                    row["discovered_at"] = prior_entries.get(row["id"], {}).get("discovered_at", current)
                old_ids = set(old_raw.get("seen", []))
                baseline = old.get("baseline_at")
                arriving = [] if baseline is None else [row for row in rows if row["id"] not in old_ids]
                unread = list(old.get("new", [])) + arriving
                seen = list(dict.fromkeys([row["id"] for row in rows] + old_raw.get("seen", [])))
                result[source] = {"status": "baseline" if baseline is None else "ok", "checked_at": current,
                                  "last_success_at": current, "baseline_at": current if baseline is None else baseline,
                                  "new": unread[-MAX_UNREAD:], "entries": rows, "seen": seen[:MAX_SEEN],
                                  "overflow": bool(old_raw.get("overflow")) or len(unread) > MAX_UNREAD or len(seen) > MAX_SEEN,
                                  "error": None}
            except Exception as exc:
                reason = str(exc) if isinstance(exc, NoticeError) else "Official source fetch or parse failed"
                result[source] = {"status": "error", "checked_at": current,
                                  "last_success_at": old.get("last_success_at"),
                                  "baseline_at": old.get("baseline_at"), "new": old.get("new", []),
                                  "entries": old.get("entries", []), "seen": old_raw.get("seen", []),
                                  "overflow": bool(old_raw.get("overflow")),
                                  "error": reason[:160]}
        self._save(result)
        return self.snapshot()

    def acknowledge_all(self):
        """Clear only currently known unread items, under the polling lock."""
        with self._locked():
            try:
                cached = json.loads((self.state_dir / "pool-notices.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
            if not isinstance(cached, dict):
                cached = {}
            for source in self.source_ids:
                if isinstance(cached.get(source), dict):
                    cached[source]["new"] = []
                    cached[source]["overflow"] = False
            self._save(cached)
        return self.snapshot()

    def acknowledge_seen(self, seen):
        """Clear only unread ids the caller actually displayed."""
        if (not isinstance(seen, dict) or any(source not in self.source_ids for source in seen) or
                any(not isinstance(ids, list) or any(not isinstance(item_id, str) or
                    not re.fullmatch(r"[0-9a-f]{64}", item_id) for item_id in ids)
                    for ids in seen.values()) or
                sum(len(ids) for ids in seen.values()) > 300):
            raise NoticeError("Invalid displayed notice ids")
        with self._locked():
            try:
                cached = json.loads((self.state_dir / "pool-notices.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
            if not isinstance(cached, dict):
                cached = {}
            for source, ids in seen.items():
                row = cached.get(source)
                if isinstance(row, dict):
                    acknowledged = set(ids)
                    row["new"] = [entry for entry in row.get("new", [])
                                  if isinstance(entry, dict) and entry.get("id") not in acknowledged]
            self._save(cached)
        return self.snapshot()

    def _save(self, result):
        fd, temporary = tempfile.mkstemp(prefix=".pool-notices-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_dir / "pool-notices.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def check_notices(state_dir):
    return NoticeChecker(state_dir).check_notices()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read locally cached official pool notices")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--check", action="store_true", help="Fetch official sources before showing status")
    args = parser.parse_args(argv)
    try:
        checker = NoticeChecker(args.state_dir)
        rows = checker.check_notices() if args.check else checker.snapshot()
        summary = {source: {"status": row["status"], "unread_count": len(row["new"]),
                            "stale": row["stale"]} for source, row in rows.items()}
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1 if any(row["status"] == "error" for row in rows.values()) else 0
    except (NoticeError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:160]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
