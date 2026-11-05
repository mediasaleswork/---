import unittest

from partner_monitor.parsing import match_ad, parse_partner_references


class PartnerParsingTests(unittest.TestCase):
    def test_partner_card_and_hidden_entity(self):
        refs = parse_partner_references(
            "Новый партнёр: @Daryo. Пост: https://t.me/s/Daryo/123?single=1",
            ["https://telegram.me/DARYO", "https://t.me/spotuz/456"],
        )
        self.assertEqual([(r.username, r.message_id) for r in refs], [("daryo", None), ("daryo", 123), ("spotuz", 456)])

    def test_private_links_are_identified_without_resolving(self):
        refs = parse_partner_references("https://t.me/+SecretInvite https://t.me/joinchat/OtherInvite https://t.me/c/123456/987")
        self.assertEqual(len(refs), 3)
        self.assertTrue(all(r.private for r in refs))
        self.assertEqual(refs[-1].private_channel_id, 123456)
        self.assertEqual(refs[-1].message_id, 987)

    def test_emails_and_malicious_hosts_not_channels(self):
        text = "mail@partner.com https://t.me.evil.com/daryo https://evil.com/@daryo https://t.me@evil.com/daryo evil.t.me/daryo"
        self.assertEqual(parse_partner_references(text), [])

    def test_reserved_paths_and_invalid_ids(self):
        text = "https://t.me/share/url?url=x https://t.me/proxy?server=x https://t.me/s https://t.me/c/123/0 https://t.me/daryo/-1"
        self.assertEqual(parse_partner_references(text), [])

    def test_bare_links_punctuation_and_order(self):
        refs = parse_partner_references("(t.me/spotuz/42), @Daryo; telegram.me/spotuz/42.")
        self.assertEqual([(r.username, r.message_id) for r in refs], [("spotuz", 42), ("daryo", None)])


class AdvertisementMatchingTests(unittest.TestCase):
    def test_brand_boundaries_case_and_cyrillic(self):
        self.assertTrue(match_ad("Кредит от BRB!", ["brb"], []).matched)
        self.assertFalse(match_ad("BRBank, NOTBRB, brb123", ["brb"], []).matched)
        self.assertTrue(match_ad("БРБ — новый продукт", ["брб"], []).matched)

    def test_uzbek_apostrophes_and_spaces(self):
        self.assertTrue(match_ad("Oʻzbekiston\nbanki", ["O'zbekiston banki"], []).matched)
        self.assertTrue(match_ad("Baraka   xazinasi", ["baraka xazinasi"], []).matched)

    def test_exact_domains_subdomains_and_bare_domains(self):
        for text in ["https://brb.uz/product", "brb.uz", "https://credit.brb.uz/apply"]:
            with self.subTest(text=text):
                self.assertTrue(match_ad(text, [], ["brb.uz"]).matched)
        for text in ["https://evilbrb.uz", "https://brb.uz.evil.com", "foo@brb.uz", "https://notbrb.uz/x", "https://brb.uz@evil.com", "https://evil.com/?next=brb.uz"]:
            with self.subTest(text=text):
                self.assertFalse(match_ad(text, [], ["brb.uz"]).matched)

    def test_empty_config_does_not_match_everything(self):
        self.assertFalse(match_ad("", ["", "  "], [""]).matched)
        self.assertFalse(match_ad("plain text", [], []).matched)


if __name__ == "__main__":
    unittest.main()
