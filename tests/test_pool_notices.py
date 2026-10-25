# SPDX-License-Identifier: GPL-3.0-only
import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pool_notices import NoticeChecker, NoticeError, _entries, main, SOURCES


class PoolNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_qtc_empty_baseline_then_new_announcement_and_failure(self):
        now = [1000]
        announcement = [""]
        fail = [False]
        def fetch(url, limit):
            if fail[0]:
                raise OSError("offline")
            return json.dumps({"announcement": announcement[0]}).encode()
        checker = NoticeChecker(self.state, ["QTC"], fetcher=fetch, now=lambda: now[0])
        baseline = checker.check_notices()["QTC"]
        self.assertEqual(baseline["status"], "baseline")
        self.assertEqual(baseline["new"], [])
        announcement[0] = "维护通知 <script>secret=exposed</script> 钱包 0x" + "a" * 40
        now[0] += 901
        added = checker.check_notices()["QTC"]
        self.assertEqual(added["status"], "ok")
        self.assertEqual(len(added["new"]), 1)
        self.assertNotIn("exposed", added["new"][0]["text"])
        self.assertNotIn("0x" + "a" * 40, added["new"][0]["text"])
        self.assertEqual(added["new"][0]["discovered_at"], now[0])
        now[0] += 901
        repeat = checker.check_notices()["QTC"]
        self.assertEqual(len(repeat["new"]), 1)
        self.assertEqual(repeat["new"][0]["discovered_at"], added["new"][0]["discovered_at"])
        fail[0] = True
        now[0] += 901
        stale = checker.check_notices()["QTC"]
        self.assertEqual(stale["status"], "error")
        self.assertEqual(len(stale["new"]), 1)
        self.assertEqual(stale["last_success_at"], added["last_success_at"] + 901)
        self.assertEqual(checker.acknowledge_all()["QTC"]["new"], [])
        now[0] += 37 * 3600
        self.assertTrue(checker.snapshot()["QTC"]["stale"])

    def test_tsc_uses_only_announcements_section_and_revision(self):
        html = b'''<html><script>token=bad</script><section class="page announcements-page" data-page="announcements">
        <article class="announcement-item"><time datetime="2026-10-17">17</time>
        <div class="announcement-content"><p class="announcement-kind">\xe7\x9f\xbf\xe6\xb1\xa0\xe5\x85\xac\xe5\x91\x8a</p>
        <h3>244 TSC \xe6\x95\x85\xe9\x9a\x9c\xe8\xa1\xa5\xe5\x81\xbf\xe5\xb7\xb2\xe5\x8f\x91\xe6\x94\xbe</h3>
        <p>\xe5\xbb\xba\xe8\xae\xae 27B \xe7\x9f\xbf\xe5\xb7\xa5\xe5\x88\x87\xe5\x9b\x9e 8B \xe6\xa8\xa1\xe5\x9e\x8b\xe3\x80\x82</p>
        <a href="#announcement-detail" data-announcement-open="20261017">\xe6\x9f\xa5\xe7\x9c\x8b</a></div></article></section>
        <article class="announcement-item"><h3>Outside</h3></article><div>123456 GH/s</div></html>'''
        rows = _entries(html, SOURCES["TSC"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2026-10-17")
        self.assertEqual(rows[0]["item_id"], "20261017")
        self.assertNotIn("Outside", json.dumps(rows))
        self.assertNotIn("123456", json.dumps(rows))
        now = [1000]
        content = [html]
        checker = NoticeChecker(self.state, ["TSC"], fetcher=lambda url, limit: content[0], now=lambda: now[0])
        self.assertEqual(checker.check_notices()["TSC"]["new"], [])
        content[0] = html.replace(b"27B", b"27B revised")
        now[0] += 901
        self.assertEqual(len(checker.check_notices()["TSC"]["new"]), 1)

    def test_unknown_source_rejected(self):
        with self.assertRaises(NoticeError):
            NoticeChecker(self.state, ["https://evil.example/"])

    def test_cli_snapshot_never_fetches_and_check_summarizes_errors(self):
        output = io.StringIO()
        with patch("pool_notices._fetch", side_effect=AssertionError("network called")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(self.state)]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(set(summary), set(SOURCES))
        self.assertEqual(summary["QTC"], {"status": "unchecked", "unread_count": 0, "stale": True})
        output = io.StringIO()
        with patch.object(NoticeChecker, "check_notices", return_value={
                "QTC": {"status": "error", "new": [{"text": "secret body"}], "stale": True}}):
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(self.state), "--check"]), 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"QTC": {"status": "error", "unread_count": 1, "stale": True}})
        self.assertNotIn("secret body", output.getvalue())

    def test_missing_qtc_field_and_missing_tsc_section_keep_cache(self):
        for key, valid, invalid in (("QTC", b'{"announcement":"published"}',
                                     b'{"miner_version":"6.4.0"}'),
                                    ("TSC", b'<section class="page announcements-page" data-page="announcements"><article class="announcement-item"><h3>published</h3></article></section>',
                                     b"<html><body>challenge</body></html>")):
            with self.subTest(source=key):
                now = [1000]
                payload = [valid]
                checker = NoticeChecker(self.state / key, [key],
                                        fetcher=lambda url, limit: payload[0], now=lambda: now[0])
                old = checker.check_notices()[key]
                self.assertEqual(old["status"], "baseline")
                payload[0] = invalid
                now[0] += 901
                failed = checker.check_notices()[key]
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["entries"], old["entries"])
                self.assertEqual(failed["baseline_at"], old["baseline_at"])

    def test_qtc_edit_after_display_limit_is_unread(self):
        now = [1000]
        notice = ["notice " * 80 + "old"]
        checker = NoticeChecker(self.state, ["QTC"],
                                fetcher=lambda url, limit: json.dumps({"announcement": notice[0]}).encode(),
                                now=lambda: now[0])
        self.assertEqual(checker.check_notices()["QTC"]["new"], [])
        notice[0] = "notice " * 80 + "new"
        now[0] += 901
        result = checker.check_notices()["QTC"]
        self.assertEqual(len(result["new"]), 1)
        self.assertLessEqual(len(result["new"][0]["text"]), 500)

    def test_noid_official_json_old_items_are_baseline(self):
        sample = [{"id": "2", "title": "NOID 最低发款临时调整为 50 NOID",
                   "body": "【简体中文】NOID 最低发款额已由 10 临时调整为 50。",
                   "level": "update", "created_at": "2026-10-05T17:54:25.352Z"},
                  {"id": "1", "title": "重要：请务必升级至 INVminer 0.1.53+",
                   "body": "旧版本说明。", "created_at": "2026-08-29T02:01:21.960Z"}]
        now = [1000]
        checker = NoticeChecker(self.state, ["NOID"],
                                fetcher=lambda url, limit: json.dumps(sample).encode(), now=lambda: now[0])
        initial = checker.check_notices()["NOID"]
        self.assertEqual(initial["status"], "baseline")
        self.assertEqual(len(initial["entries"]), 2)
        self.assertEqual(initial["new"], [])
        self.assertEqual(initial["entries"][0]["published_at"], "2026-10-05T17:54:25.352Z")
        sample.insert(0, {"id": "3", "title": "新公告", "body": "今日维护", "created_at": "2026-10-21T00:00:00Z"})
        now[0] += 901
        changed = checker.check_notices()["NOID"]
        self.assertEqual([row["item_id"] for row in changed["new"]], ["3"])
        sample.pop(0)
        now[0] += 901
        restarted = NoticeChecker(self.state, ["NOID"],
                                  fetcher=lambda url, limit: json.dumps(sample).encode(), now=lambda: now[0])
        self.assertEqual([row["item_id"] for row in restarted.check_notices()["NOID"]["new"]], ["3"])
        sample.insert(0, {"id": "3", "title": "新公告", "body": "今日维护", "created_at": "2026-10-21T00:00:00Z"})
        now[0] += 901
        self.assertEqual([row["item_id"] for row in restarted.check_notices()["NOID"]["new"]], ["3"])
        self.assertEqual(restarted.acknowledge_all()["NOID"]["new"], [])
        now[0] += 901
        self.assertEqual(restarted.check_notices()["NOID"]["new"], [])

    def test_acknowledge_seen_preserves_notice_arriving_after_display(self):
        now = [1000]
        rows = []
        checker = NoticeChecker(self.state, ["NOID"],
                                fetcher=lambda url, limit: json.dumps(rows).encode(), now=lambda: now[0])
        self.assertEqual(checker.check_notices()["NOID"]["new"], [])
        rows.append({"id": "a", "title": "A", "body": "first", "created_at": "2026-10-21T00:00:00Z"})
        now[0] += 901
        displayed = checker.check_notices()["NOID"]["new"]
        self.assertEqual(len(displayed), 1)
        rows.append({"id": "b", "title": "B", "body": "later", "created_at": "2026-10-21T00:01:00Z"})
        now[0] += 901
        self.assertEqual(len(checker.check_notices()["NOID"]["new"]), 2)
        remaining = checker.acknowledge_seen({"NOID": [displayed[0]["id"]]})["NOID"]["new"]
        self.assertEqual([entry["item_id"] for entry in remaining], ["b"])
        self.assertEqual(checker.acknowledge_seen({})["NOID"]["new"], remaining)

    def test_acknowledge_seen_rejects_unknown_source_and_invalid_ids(self):
        checker = NoticeChecker(self.state, ["NOID"])
        for seen in ({"OTHER": []}, {"NOID": ["abc"]}, {"NOID": ["a" * 64] * 301},
                     {"NOID": "a" * 64}, None):
            with self.subTest(seen=str(seen)[:40]), self.assertRaises(NoticeError):
                checker.acknowledge_seen(seen)


if __name__ == "__main__":
    unittest.main()
