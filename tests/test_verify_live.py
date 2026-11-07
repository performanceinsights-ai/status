import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import verify_live  # noqa: E402

BUILT = "2026-11-07T07:44:27+00:00"


def page(stamp):
    return json.dumps({"generated_at": stamp}).encode()


def sequence(*answers):
    """A read_live() that returns each answer in turn (an Exception is raised) and then repeats the last."""
    items = list(answers)

    def read():
        answer = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return read


class IsLive(unittest.TestCase):

    def test_same_build_is_live(self):
        self.assertTrue(verify_live.is_live(BUILT, BUILT))

    def test_later_build_is_live(self):
        # A later round that finished first must not fail this one.
        self.assertTrue(verify_live.is_live(BUILT, "2026-11-07T07:49:30+00:00"))

    def test_older_build_is_not(self):
        self.assertFalse(verify_live.is_live(BUILT, "2026-11-06T12:22:48+00:00"))


class WaitFor(unittest.TestCase):

    def test_returns_at_once_when_live(self):
        slept = []
        seen = verify_live.wait_for(BUILT, sequence(page(BUILT)), sleep=slept.append)
        self.assertEqual(seen, BUILT)
        self.assertEqual(slept, [])

    def test_waits_for_the_new_build(self):
        slept = []
        stale = page("2026-11-06T12:22:48+00:00")
        seen = verify_live.wait_for(BUILT, sequence(stale, stale, page(BUILT)), delay=10, sleep=slept.append)
        self.assertEqual(seen, BUILT)
        self.assertEqual(slept, [10, 10])

    def test_frozen_page_gives_up_with_the_stale_stamp(self):
        stale = "2026-11-06T12:22:48+00:00"
        slept = []
        seen = verify_live.wait_for(BUILT, sequence(page(stale)), attempts=3, sleep=slept.append)
        self.assertEqual(seen, stale)
        self.assertFalse(verify_live.is_live(BUILT, seen))
        self.assertEqual(len(slept), 2)

    def test_unreadable_page_is_retried_then_reported_as_none(self):
        seen = verify_live.wait_for(BUILT, sequence(OSError("down"), b"not json", b"{}"), attempts=3, sleep=lambda _: None)
        self.assertIsNone(seen)

    def test_wrong_shaped_json_is_not_a_crash(self):
        for body in (b"[]", b'{"generated_at": null}', b'{"generated_at": 5}', b"null"):
            self.assertIsNone(verify_live.wait_for(BUILT, sequence(body), attempts=2, sleep=lambda _: None), body)


class Main(unittest.TestCase):

    def test_a_relative_or_empty_url_is_an_error_not_a_request(self):
        for url in ("/status.json", "status.json", ""):
            self.assertEqual(verify_live.main(["--built", __file__, "--url", url]), 1)


if __name__ == "__main__":
    unittest.main()
