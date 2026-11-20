"""News mentions must not create or refresh CRM leads without an ad label."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sales_agent.config import Config
from sales_agent.research import validate
from sales_agent.rules import RuleResearcher, is_advertisement
from sales_agent.runner import cycle
from sales_agent.sources import parse_feed, parse_telegram, relevant
from sales_agent.store import Store
from test_sales import NOW, candidate


NEWS = 'Компания TestBrand открыла новый магазин в Ташкенте и планирует расширить присутствие в Узбекистане.'


def source(text, url='https://t.me/example/1', **extra):
    return dict(id='fixture', url=url, title=text[:180], text=text,
                origin='Telegram @example', published=NOW.isoformat(), links='[]', **extra)


class AdsOnlyTests(unittest.TestCase):
    def test_company_news_is_rejected_with_or_without_a_known_brand(self):
        for text in [NEWS, 'TestBrand запустил новый финансовый продукт для бизнеса.',
                     'Компания TestBrand заключила партнёрство и привлекла инвестиции.',
                     'TestBrand рассказал, как развивается рынок рекламы в Узбекистане.',
                     'Банк TestBrand запустил программу. Ему помогла телевизионная реклама.']:
            row = source(text)
            with self.subTest(text=text):
                self.assertFalse(relevant(row))
                self.assertEqual(RuleResearcher().classify([row], NOW), [])
                self.assertEqual(RuleResearcher(['TestBrand']).classify([row], NOW), [])

    def test_validation_rejects_unmarked_source_even_if_classified_as_an_ad(self):
        row = source(NEWS)
        data = dict(candidate(row['id']), evidence_quote=NEWS, event_kind='рекламное размещение')
        self.assertIsNone(validate(data, {row['id']:row}, NOW))

    def test_article_company_metadata_cannot_bypass_the_marker(self):
        row = source(NEWS, article_company='TestBrand', article_url='https://kapital.uz/article/')
        self.assertEqual(RuleResearcher().classify([row], NOW), [])

    def test_supported_explicit_labels_still_produce_ads(self):
        for label in ['Реклама', 'На правах рекламы', '(на правах рекламы)',
                      '(реклама).', '#реклама', 'Reklama', '#reklama', '(advertisement)', '#ad']:
            row = source(NEWS + ' ' + label)
            with self.subTest(label=label):
                self.assertTrue(relevant(row))
                found = RuleResearcher().classify([row], NOW)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['event_kind'], 'рекламное размещение')
                self.assertIsNotNone(validate(found[0], {row['id']:row}, NOW))

    def test_full_post_marker_is_checked_before_text_is_shortened(self):
        row = source(NEWS + ' Условия предложения.' * 400 + ' Реклама')
        self.assertGreater(len(row['text']), 5000)
        self.assertFalse(is_advertisement(row['text'][:5000]))
        self.assertTrue(relevant(row))
        found = RuleResearcher().classify([row], NOW)
        self.assertEqual(len(found), 1)
        self.assertIsNotNone(validate(found[0], {row['id']:row}, NOW))

    def test_rss_news_also_requires_a_label(self):
        for suffix, expected in [('', False), (' (на правах рекламы)', True)]:
            rss = ('<rss><channel><item><title>TestBrand открыла магазин</title>'
                   '<link>https://news.example/article</link>'
                   f'<pubDate>{NOW.isoformat()}</pubDate><description>{NEWS}{suffix}</description>'
                   '</item></channel></rss>')
            rows = parse_feed(rss, 'news')
            self.assertEqual(relevant(rows[0]), expected)

    def test_telegram_line_break_keeps_a_standalone_footer_label(self):
        body = NEWS.rstrip('.')
        html = ('<div class="tgme_widget_message" data-post="example/1">'
                f'<time datetime="{NOW.isoformat()}"></time>'
                f'<div class="tgme_widget_message_text">{body}<br>Реклама</div></div>')
        self.assertTrue(relevant(parse_telegram(html, 'example')[0]))
        self.assertTrue(is_advertisement('Реклама: ' + body))

    def test_news_does_not_create_or_refresh_a_company_but_a_marked_edit_does(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'sales.sqlite3')
            cfg = Config(data_dir=Path(folder), feeds=[], max_leads=10)
            row = source(NEWS)
            row.pop('id'); row['links']=[]
            try:
                store.add_source(row)
                result = cycle(cfg, store, now=NOW, collect_sources=False)
                self.assertEqual(result['new_leads'], 0)
                self.assertEqual(store.publications(), [])
                # Editing that very post to add a genuine label makes it eligible.
                store.add_source(dict(row, text=NEWS+' Реклама'))
                result = cycle(cfg, store, now=NOW, collect_sources=False)
                self.assertEqual(result['new_leads'], 1)
                ident = store.leads()[0]['id']
                # Newer ordinary news cannot replace an existing ad in the CRM.
                store.add_source(dict(row, url='https://t.me/example/2',
                                      published=(NOW+timedelta(minutes=1)).isoformat()))
                result = cycle(cfg, store, now=NOW+timedelta(minutes=2), collect_sources=False)
                self.assertEqual(result['new_publications'], 0)
                self.assertEqual(store.leads()[0]['id'], ident)
                self.assertEqual(store.leads()[0]['source_url'], row['url'])
                self.assertEqual(len(store.publications()), 1)
            finally:
                store.close()
