import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from sales_agent.config import Config
from sales_agent.research import validate, Deferred
from sales_agent.rules import RuleResearcher
from sales_agent.runner import cycle, due
from sales_agent.sheets import Sheets, LEAD_HEADERS, SCRIPT_HEADERS, lead_row
from sales_agent.sources import parse_feed, parse_telegram, relevant
from sales_agent.store import Store

NOW = datetime(2026,11,9,5,tzinfo=timezone.utc)
TEXT = 'Компания TestBrand открыла новый магазин в Ташкенте и планирует расширить присутствие в Узбекистане.'


def candidate(sid):
    return dict(source_id=sid, company='TestBrand', evidence_quote=TEXT, sector='Ритейл',
        event_kind='открытие',service='Telegram и СМИ',hypothesis='Может быть полезен локальный охват',
        priority='A',rationale='Новая точка требует знакомства с аудиторией',confidence='высокая')


class SalesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'sales.db')
        self.cfg=Config(data_dir=self.root,batch_size=1,max_leads=1)
        self.store.add_source(dict(url='https://t.me/spotuz/123',title=TEXT,text=TEXT,
            published=NOW.isoformat(),origin='test',links=[]))
        self.source=self.store.pending((NOW-timedelta(days=1)).isoformat())[0]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_exact_evidence_required_and_only_source_url_is_used(self):
        good=candidate(self.source['id'])
        good['source_url']='https://fake.example'
        lead=validate(good,{self.source['id']:self.source},NOW)
        self.assertEqual(lead['source_url'],self.source['url'])
        self.assertIn(TEXT,lead['scripts']['first'])
        good['evidence_quote']=TEXT+' Гарантия 500 клиентов.'
        self.assertIsNone(validate(good,{self.source['id']:self.source},NOW))

    def test_unknown_company_source_or_service_rejected(self):
        for field,value in [('company','Another Brand'),('source_id','unknown'),('service','Наружные баннеры')]:
            data=candidate(self.source['id']); data[field]=value
            self.assertIsNone(validate(data,{self.source['id']:self.source},NOW))

    def test_durable_dedup_and_budget_survive_restart(self):
        lead=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.assertTrue(self.store.add_lead(lead))
        self.assertTrue(self.store.reserve_request('2026-11-09',1))
        self.store.close(); self.store=Store(self.root/'sales.db')
        self.assertFalse(self.store.add_lead(dict(lead,company='testbrand')))
        self.assertFalse(self.store.reserve_request('2026-11-09',1))
        self.assertTrue(self.store.reserve_request('2026-11-10',1))

    def test_model_failure_does_not_consume_source_queue(self):
        class Fail:
            def classify(self,*args): raise Deferred('quota')
        result=cycle(self.cfg,self.store,now=NOW,researcher=Fail(),collect_sources=False)
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(len(self.store.pending('2026')),1)
        self.assertFalse(self.store.leads())

    def test_success_is_idempotent_and_stale_sources_ignored(self):
        class Good:
            def classify(_,batch,now): return [candidate(batch[0]['id'])]
        first=cycle(self.cfg,self.store,now=NOW,researcher=Good(),collect_sources=False)
        second=cycle(self.cfg,self.store,now=NOW,researcher=Good(),collect_sources=False)
        self.assertEqual([first['new_leads'],second['new_leads']],[1,0])
        self.assertEqual(len(self.store.leads()),1)

    def test_monitoring_interval_survives_restart(self):
        self.assertTrue(due(self.store,self.cfg,NOW))
        self.store.set_state('last_researched',NOW.isoformat())
        self.assertFalse(due(self.store,self.cfg,NOW))
        self.assertTrue(due(self.store,self.cfg,NOW+timedelta(minutes=15)))

    def test_rss_uses_real_dates_and_html_text(self):
        rss='<rss><channel><item><title>TestBrand открыла магазин</title><link>https://news.example/1</link><pubDate>Mon, 09 Nov 2026 05:00:00 GMT</pubDate><description>&lt;p&gt;Новый магазин&lt;/p&gt;</description></item><item><title>Без даты</title><link>https://news.example/2</link></item></channel></rss>'
        rows=parse_feed(rss,'test')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['published'],NOW.isoformat())
        self.assertNotIn('<p>',rows[0]['text'])

    def test_telegram_only_configured_public_post_and_real_date(self):
        html='<div class="tgme_widget_message" data-post="spotuz/123"><div class="tgme_widget_message_text">'+TEXT+'</div><time datetime="2026-11-09T05:00:00+00:00"></time></div>'
        rows=parse_telegram(html,'spotuz')
        self.assertEqual(rows[0]['url'],'https://t.me/spotuz/123')
        self.assertFalse(parse_telegram(html,'other_channel'))
        self.assertTrue(relevant(rows[0]))

    def test_post_date_uses_current_publication_not_forwarded_date(self):
        html='<div class="tgme_widget_message" data-post="spotuz/123"><div class="tgme_widget_message_forwarded_from"><time datetime="2026-11-01T00:00:00Z"></time></div><div class="tgme_widget_message_text">'+TEXT+'</div><a class="tgme_widget_message_date"><time datetime="2026-11-09T22:15:00Z"></time></a></div>'
        row=parse_telegram(html,'spotuz')[0]
        self.assertEqual(row['published'],'2026-11-09T22:15:00+00:00')
        lead=validate(candidate(self.source['id']),{self.source['id']:dict(self.source,published=row['published'])},NOW)
        self.store.add_lead(lead)
        displayed=datetime(1899,12,30)+timedelta(days=lead_row(lead)[5])
        self.assertEqual(displayed.strftime('%Y-%m-%d %H:%M'),'2026-11-10 03:15')

    def test_manual_edits_and_sorted_rows_preserved_and_formula_text_raw(self):
        lead=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.store.add_lead(lead)
        class Fake(Sheets):
            def __init__(self): self.writes=[]
            def verify(self): pass
            def read(_,name,bounds):
                if name=='Лиды': return [LEAD_HEADERS,[lead['id'],'Edited by owner']]
                return [SCRIPT_HEADERS,[lead['id'],'Edited script']]
            def api(self,method,path='',**kwargs):
                self.writes.append(kwargs['json']); return {}
        sheets=Fake()
        self.assertEqual(sheets.sync(self.store),0)
        ranges=[v['range'] for v in sheets.writes[0]['data']]
        self.assertFalse(any('Лиды' in r or 'Скрипты' in r for r in ranges))
        self.assertEqual(sheets.writes[0]['valueInputOption'],'RAW')

    def test_real_default_pipeline_works_without_any_model_or_network_post(self):
        with patch.dict('os.environ',{},clear=True),patch('requests.post',side_effect=AssertionError('No model calls allowed')):
            result=cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['new_leads'],1)
        self.assertEqual(self.store.leads()[0]['company'],'TestBrand')

    def test_migration_rechecks_old_sources_once_without_duplicate_crm(self):
        self.store.processed([self.source['id']])
        with patch('requests.post',side_effect=AssertionError('No model calls allowed')):
            result=cycle(self.cfg,self.store,now=NOW,collect_sources=False)
            repeat=cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        self.assertEqual(result['new_leads'],1)
        self.assertEqual(repeat['new_leads'],0)
        self.assertEqual(self.store.state('research_engine'),'rules-1')

    def test_noncommercial_news_does_not_block_the_persistent_queue(self):
        self.store.add_source(dict(url='https://news.example/exchange',title='Курсы валют',
            text='Опубликованы текущие курсы валют. Справочные данные банков о стоимости доллара и евро за сегодняшний день.',
            published=NOW.isoformat(),origin='news',links=[]))
        cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        self.assertFalse(self.store.pending('2026'))

    def test_unknown_brand_and_uzbek_brand_use_literal_evidence(self):
        for body,name in [
            ('Asterium запускает новый продукт для предпринимателей Узбекистана с удобными финансовыми инструментами.','Asterium'),
            ('Orzu kompaniyasi yangi do‘konini ishga tushirdi, unda Toshkent aholisi uchun keng mahsulot tanlovi mavjud.','Orzu')]:
            row=dict(self.source,text=body,title=body)
            found=RuleResearcher().classify([row],NOW)
            self.assertTrue(found,body)
            lead=validate(found[0],{row['id']:row},NOW)
            self.assertIsNotNone(lead,found)
            self.assertEqual(lead['company'],name)
            self.assertIn(lead['evidence_quote'],body)

    def test_politics_competitors_and_distant_brand_footer_are_not_leads(self):
        bodies=[
            'Президент Узбекистана посетил завод NURA Electric. Обсуждены различные вопросы экономики страны.',
            'Рекламное агентство TEAMS запускает новый сервис продвижения для предпринимателей Узбекистана.',
            'Открылся новый магазин в городе. '+('Нейтральная городская новость. '*20)+' Компания TestBrand сообщает свой телефон.']
        for body in bodies:
            row=dict(self.source,text=body,title=body[:100])
            self.assertFalse(RuleResearcher().classify([row],NOW),body)

    def test_workbook_identity_read_precedes_writes_and_refuses_wrong_headers(self):
        class Fake(Sheets):
            def __init__(self): self.calls=[]
            def api(self,method,path='',**kwargs):
                self.calls.append(method)
                return {'sheets':[{'properties':{'title':n}} for n in ['Лиды','Скрипты','Источники','Работа агента']]}
            def read(self,name,bounds): return [['Wrong workbook']]
        sheet=Fake()
        with self.assertRaises(ValueError): sheet.verify()
        self.assertEqual(sheet.calls,['GET'])


if __name__=='__main__': unittest.main()
