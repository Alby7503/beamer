import json
import unittest

from core import updates


def release(tag, **extra):
    return dict({"tag_name": tag, "html_url": f"https://github.com/kalkman-code/beamer/releases/tag/{tag}",
                 "draft": False, "prerelease": False}, **extra)


class NewerReleaseTests(unittest.TestCase):
    def test_every_newer_stable_patch_is_offered(self):
        self.assertIsNotNone(updates.newer_release("1.4.0", release("v1.4.1", body="Small fixes.")))
        self.assertIsNotNone(updates.newer_release("1.4.0", release("v1.4.1")))
        self.assertIsNotNone(updates.newer_release("1.4.0", release("v1.4.1", body="Fixes a crash.\n<!-- announce -->")))

    def test_a_quiet_patch_still_offers_the_minor_version_someone_is_behind(self):
        self.assertIsNotNone(updates.newer_release("1.4.2", release("v1.5.1", body="Small fixes.")))
        self.assertIsNotNone(updates.newer_release("1.9.0", release("v2.0.1")))

    def test_a_newer_tag_is_offered_with_its_page(self):
        self.assertEqual(updates.newer_release("1.3.3", release("v1.4.0")),
                         ("1.4.0", "https://github.com/kalkman-code/beamer/releases/tag/v1.4.0"))

    def test_the_same_or_an_older_tag_is_not(self):
        self.assertIsNone(updates.newer_release("1.4.0", release("v1.4.0")))
        self.assertIsNone(updates.newer_release("1.4.0", release("v1.3.9")))

    def test_versions_compare_as_numbers(self):
        self.assertIsNotNone(updates.newer_release("1.9.0", release("v1.10.0")))
        self.assertIsNotNone(updates.newer_release("1.4", release("v1.4.1")))
        self.assertIsNotNone(updates.newer_release("1.5.9", release("v1.5.10")))
        self.assertIsNone(updates.newer_release("1.5.10", release("v1.5.9")))

    def test_drafts_prereleases_and_odd_tags_never_count(self):
        self.assertIsNone(updates.newer_release("1.3.3", release("v2.0.0", prerelease=True)))
        self.assertIsNone(updates.newer_release("1.3.3", release("v2.0.0", draft=True)))
        self.assertIsNone(updates.newer_release("1.3.3", release("v2.0.0-beta")))
        self.assertIsNone(updates.newer_release("1.3.3", {"message": "rate limited"}))
        self.assertIsNone(updates.newer_release("1.3.3", ["not", "a", "dict"]))

    def test_a_dev_build_is_never_told_to_update(self):
        self.assertIsNone(updates.newer_release("dev", release("v9.0.0")))

    def test_a_page_outside_the_project_is_replaced_by_the_releases_page(self):
        found = updates.newer_release("1.3.3", release("v1.4.0", html_url="https://example.com/beamer.exe"))
        self.assertEqual(found, ("1.4.0", updates.RELEASES_PAGE))


class BetaTests(unittest.TestCase):
    def test_a_beta_reads_its_own_version(self):
        self.assertIsNotNone(updates.parse_version("1.5.0-beta.1"))
        self.assertIsNone(updates.parse_version("1.5.0-beta"))
        self.assertIsNone(updates.parse_version("1.5.0-rc.1"))

    def test_the_public_release_is_offered_to_a_beta_without_an_announce(self):
        self.assertEqual(updates.newer_release("1.5.0-beta.1", release("v1.5.0"))[0], "1.5.0")
        self.assertEqual(updates.newer_release("1.5.0-beta.1", release("v1.5.1"))[0], "1.5.1")
        self.assertEqual(updates.newer_release("1.5.0-beta.3", release("v1.6.0"))[0], "1.6.0")

    def test_an_older_release_is_not_offered_to_a_beta(self):
        self.assertIsNone(updates.newer_release("1.5.0-beta.1", release("v1.4.3")))
        self.assertIsNone(updates.newer_release("1.5.0-beta.1", release("v1.4.9", body="<!-- announce -->")))

    def test_a_beta_is_never_offered_to_anyone(self):
        self.assertIsNone(updates.newer_release("1.4.3", release("v1.5.0-beta.2")))
        self.assertIsNone(updates.newer_release("1.4.3", release("v1.5.0-beta.2", prerelease=True)))
        self.assertIsNone(updates.newer_release("1.5.0-beta.1", release("v1.5.0-beta.2")))
        self.assertIsNone(updates.newer_release("1.5.0-beta.1", release("v1.5.0-beta.2", prerelease=True)))
        self.assertIsNone(updates.newer_release("1.4.3", release("v1.5.0", prerelease=True)))

    def test_a_beta_checks_for_updates_like_any_install(self):
        results = []
        checker = updates.Checker("1.5.0-beta.1", lambda: True, results.append,
                                  fetcher=lambda: json.dumps(release("v1.5.0")).encode("utf-8"),
                                  logger=updates.logging.getLogger("test-updates"))
        checker.check_once()
        self.assertEqual([r[0] for r in results], ["1.5.0"])


class CheckerTests(unittest.TestCase):
    def test_switching_off_during_a_request_discards_its_answer(self):
        enabled = True
        results = []

        def fetcher():
            nonlocal enabled
            enabled = False
            return json.dumps(release("v1.6.0")).encode()

        checker = updates.Checker("1.5.9", lambda: enabled, results.append, fetcher=fetcher)
        checker.check_once()
        self.assertEqual(results, [])
        self.assertIsNone(checker.latest)

    def make(self, payload=None, enabled=True, error=None):
        results = []

        def fetcher():
            if error:
                raise error
            return json.dumps(payload).encode("utf-8")

        checker = updates.Checker("1.3.3", lambda: enabled, results.append, fetcher=fetcher,
                                  logger=updates.logging.getLogger("test-updates"))
        return checker, results

    def test_reports_a_newer_release(self):
        checker, results = self.make(release("v1.4.0"))
        checker.check_once()
        self.assertEqual(results, [("1.4.0", "https://github.com/kalkman-code/beamer/releases/tag/v1.4.0")])

    def test_reports_none_when_current(self):
        checker, results = self.make(release("v1.3.3"))
        checker.check_once()
        self.assertEqual(results, [None])

    def test_switched_off_it_asks_nothing(self):
        checker, results = self.make(error=AssertionError("fetched while switched off"), enabled=False)
        checker.check_once()
        self.assertEqual(results, [])

    def test_a_failed_check_keeps_the_last_answer(self):
        checker, results = self.make(release("v1.4.0"))
        checker.check_once()
        checker.fetcher = lambda: (_ for _ in ()).throw(OSError("offline"))
        checker.check_once()
        self.assertEqual(checker.latest[0], "1.4.0")
        self.assertEqual(len(results), 1)


class NoticeTests(unittest.TestCase):
    def test_dismissal_survives_a_new_check_but_not_a_newer_version(self):
        found = ("1.5.10", "https://github.com/kalkman-code/beamer/releases/tag/v1.5.10")
        self.assertEqual(updates.visible_notice(found, True, ""), found)
        self.assertIsNone(updates.visible_notice(found, True, "1.5.10"))
        newer = ("1.5.11", updates.RELEASES_PAGE)
        self.assertEqual(updates.visible_notice(newer, True, "1.5.10"), newer)

    def test_off_setting_hides_a_cached_result_and_none_stays_hidden(self):
        self.assertIsNone(updates.visible_notice(("1.6.0", updates.RELEASES_PAGE), False, ""))
        self.assertIsNone(updates.visible_notice(None, True, ""))


class PortableCheckerTests(unittest.TestCase):
    NAME = "Beamer-Portable-1.5.10-windows-x64.zip"
    URL = "https://github.com/kalkman-code/beamer/releases/download/v1.5.10/" + NAME

    def make(self, assets, portable=True):
        payload = release("v1.5.10", assets=assets)
        results = []
        checker = updates.Checker("1.5.9", lambda: True, results.append,
                                  fetcher=lambda: json.dumps(payload).encode(), portable=portable)
        checker.check_once()
        return checker, results

    def test_portable_download_keeps_the_release_page_for_whats_new(self):
        checker, results = self.make([{"name": self.NAME, "browser_download_url": self.URL}])
        page = "https://github.com/kalkman-code/beamer/releases/tag/v1.5.10"
        self.assertEqual(checker.download_url, self.URL)
        self.assertEqual(checker.latest, ("1.5.10", page))
        self.assertEqual(results, [checker.latest])

    def test_missing_portable_zip_never_offers_the_installer(self):
        for assets in (None, [], {}, [None, "asset"],
                       [{"name": "Beamer-Setup-1.5.10.exe", "browser_download_url": self.URL}]):
            with self.subTest(assets=assets):
                checker, results = self.make(assets)
                self.assertEqual(checker.latest[0], "1.5.10")
                self.assertIsNone(checker.download_url)
                self.assertEqual(results, [checker.latest])

    def test_untrusted_or_wrong_release_assets_are_rejected(self):
        for url in (self.URL.replace("github.com", "github.com.evil.invalid"),
                    self.URL.replace("https:", "http:"),
                    self.URL.replace("/beamer/", "/another-project/"),
                    self.URL.replace("/v1.5.10/", "/v1.5.9/"),
                    self.URL + "?source=untrusted", self.URL + "#fragment",
                    self.URL.replace(self.NAME, "Beamer-Setup-1.5.10.exe"),
                    "https://evil.invalid/" + self.NAME):
            with self.subTest(url=url):
                checker, _ = self.make([{"name": self.NAME, "browser_download_url": url}])
                self.assertIsNone(checker.download_url)

    def test_installed_checker_keeps_its_existing_release_page_behaviour(self):
        checker, results = self.make([{"name": self.NAME, "browser_download_url": self.URL}],
                                     portable=False)
        self.assertIsNone(checker.download_url)
        self.assertEqual(results, [checker.latest])

    def test_new_answer_clears_an_asset_from_the_previous_answer(self):
        checker, _ = self.make([{"name": self.NAME, "browser_download_url": self.URL}])
        checker.fetcher = lambda: json.dumps(release("v1.5.11", assets=[])).encode()
        checker.check_once()
        self.assertEqual(checker.latest[0], "1.5.11")
        self.assertIsNone(checker.download_url)
        checker.fetcher = lambda: json.dumps(release("v1.5.9")).encode()
        checker.check_once()
        self.assertIsNone(checker.latest)
        self.assertIsNone(checker.download_url)

    def test_failed_request_keeps_matching_cached_page_and_asset(self):
        checker, _ = self.make([{"name": self.NAME, "browser_download_url": self.URL}])
        previous = checker.latest
        checker.fetcher = lambda: (_ for _ in ()).throw(OSError("synthetic offline"))
        checker.check_once()
        self.assertEqual(checker.latest, previous)
        self.assertEqual(checker.download_url, self.URL)


if __name__ == "__main__":
    unittest.main()
