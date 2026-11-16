import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_mints as um  # noqa: E402


class NormalizeMintUrlTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(um.normalize_mint_url("https://mint.example.com"), "https://mint.example.com")

    def test_trailing_slash_and_v1_info(self):
        self.assertEqual(um.normalize_mint_url("https://Mint.Example.com/v1/info/"), "https://mint.example.com")
        self.assertEqual(um.normalize_mint_url("https://mint.example.com/"), "https://mint.example.com")

    def test_preserves_path_case(self):
        url = "https://lnbits.example.com/cashu/api/v1/AbCdEf"
        self.assertEqual(um.normalize_mint_url(url), url)

    def test_port(self):
        self.assertEqual(um.normalize_mint_url("https://8333.space:3338"), "https://8333.space:3338")

    def test_rejects_http_and_query(self):
        self.assertIsNone(um.normalize_mint_url("http://mint.example.com"))
        self.assertIsNone(um.normalize_mint_url("https://mint.example.com/?x=1"))
        self.assertIsNone(um.normalize_mint_url("ftp://mint.example.com"))

    def test_adds_scheme(self):
        self.assertEqual(um.normalize_mint_url("mint.example.com"), "https://mint.example.com")


class TestMintFlagTests(unittest.TestCase):
    def test_known_test_mints(self):
        for url in (
            "https://testnut.cashu.space",
            "https://nofee.testnut.cashu.space",
            "https://rugs.cashu.exchange",
            "https://8333.space:3338",
        ):
            self.assertTrue(um.is_test_mint(url), url)

    def test_regular_mint(self):
        self.assertFalse(um.is_test_mint("https://mint.example.com"))

    def test_hint_wins(self):
        self.assertTrue(um.is_test_mint("https://mint.example.com", True))


class ParserTests(unittest.TestCase):
    def test_mintradar(self):
        payload = {
            "mints": [
                {"url": "https://mint.example.com/", "isTest": False},
                {"url": "https://test.example.net", "isTest": True},
                {"url": ""},
                "not-a-dict",
            ]
        }
        items = um.parse_mintradar_payload(payload)
        self.assertEqual([i["url"] for i in items], ["https://mint.example.com", "https://test.example.net"])
        self.assertEqual(items[0]["source"], "mintradar")
        self.assertTrue(items[1]["is_test"])

    def test_cashumints_filters_types(self):
        payload = [
            {"url": "https://mint.example.com", "type": "cashu"},
            {"url": "fedimint:abc", "type": "fedimint"},
            {"url": "https://lnurl.example.com", "type": "lnurl"},
            {"url": "https://mint2.example.com", "type": "cashu"},
        ]
        items = um.parse_cashumints_payload(payload, ["cashu"])
        self.assertEqual([i["url"] for i in items], ["https://mint.example.com", "https://mint2.example.com"])

    def test_nip87_announcement_and_recommendation(self):
        events = [
            {
                "kind": 38172,
                "tags": [
                    ["d", "03" + "a" * 64],
                    ["u", "https://mint.example.com"],
                    ["nuts", "4,5,7"],
                ],
            },
            {
                "kind": 38000,
                "tags": [
                    ["k", "38172"],
                    ["u", "https://mint.example.com", "cashu"],
                    ["u", "https://other.example.com"],
                    ["u", "fed11abc", "fedimint"],
                ],
            },
            {
                "kind": 38000,
                "tags": [["k", "38173"], ["u", "https://wrong.example.com"]],
            },
        ]
        items = {i["url"]: i for i in um.parse_nip87_events(events)}
        self.assertIn("https://mint.example.com", items)
        self.assertTrue(items["https://mint.example.com"]["nip87_announced"])
        self.assertIn("https://other.example.com", items)
        self.assertNotIn("https://wrong.example.com", items)


class DedupeTests(unittest.TestCase):
    def test_same_pubkey_merges_alias_and_provenance(self):
        pubkey = "02" + "b" * 64
        records = [
            {
                "url": "https://a.example.com",
                "status": "online",
                "pubkey": pubkey,
                "discovered_via": ["existing"],
                "first_seen": "2026-01-01T00:00:00Z",
                "is_test": False,
                "nip87_announced": False,
                "nip87_pubkey_match": None,
            },
            {
                "url": "https://b.example.com",
                "status": "online",
                "pubkey": pubkey,
                "discovered_via": ["manual"],
                "first_seen": "2026-02-01T00:00:00Z",
                "is_test": False,
                "nip87_announced": True,
                "nip87_pubkey_match": True,
            },
        ]
        out = um.dedupe_records(records, set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a.example.com")
        self.assertEqual(out[0]["aliases"], ["https://b.example.com"])
        self.assertEqual(out[0]["discovered_via"], ["manual"])
        self.assertTrue(out[0]["nip87_announced"])


class CandidateStabilityTests(unittest.TestCase):
    def test_first_seen_is_preserved(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.json"
            path.write_text(json.dumps([{
                "url": "https://junk.example.com",
                "source": "cashumints",
                "reason": "invalid_url",
                "first_seen": "2026-01-01T00:00:00Z",
            }]), encoding="utf-8")
            original = um.CANDIDATES_JSON
            um.CANDIDATES_JSON = path
            try:
                candidates = um.stabilize_candidates([{
                    "url": "https://junk.example.com",
                    "source": "cashumints",
                    "reason": "invalid_url",
                    "first_seen": "2026-11-16T00:00:00Z",
                }])
            finally:
                um.CANDIDATES_JSON = original
        self.assertEqual(candidates[0]["first_seen"], "2026-01-01T00:00:00Z")


class VersionScoringTests(unittest.TestCase):
    def test_parse_version_strips_slash(self):
        self.assertEqual(um.parse_version("Nutshell/0.21.0/"), ("Nutshell", "0.21.0"))

    def test_version_days_old(self):
        releases = {"nutshell": {"0.21.0": "2026-09-16", "0.22.1": "2026-11-03"}}
        days = um.version_days_old("0.21.0", "Nutshell", releases)
        self.assertIsNotNone(days)
        self.assertEqual(days, (um.datetime(2026, 11, 3, tzinfo=um.timezone.utc) - um.datetime(2026, 9, 16, tzinfo=um.timezone.utc)).days)

    def test_unknown_version(self):
        self.assertIsNone(um.version_days_old("9.9.9", "Nutshell", {"nutshell": {}}))


if __name__ == "__main__":
    unittest.main()
