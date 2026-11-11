import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from sales_agent.config import Config
from sales_agent.research import validate, Deferred
from sales_agent.rules import RuleResearcher
from sales_agent.runner import cycle, due
from sales_agent.sheets import Sheets, LEAD_HEADERS, SCRIPT_HEADERS, lead_row, script_row_moves
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
        self.assertEqual(self.store.state('research_engine'),'rules-2')

    def test_marked_brand_ads_are_candidates_without_launch_keywords(self):
        examples=[
            ('Три года с Haval: бренд объединяет владельцев и любителей комфортных автомобилей по всему Узбекистану (реклама).','Haval'),
            ('Как Semurg Insurance организовала страховые выплаты и обслуживание своих клиентов с помощью платформы EIOS (реклама).','Semurg Insurance'),
            ('HUMO спорт, технологиялар ва замонавий санъатни бирлаштирди. Махсус ҳудудда тўлов ечимлари намойиш этилди. Реклама','HUMO'),
            ('Wuling Bardosh — qulay shartlarda avtomobil xarid qiling. Boshlang‘ich to‘lov va muddatlar haqida batafsil. Reklama','Wuling'),
            ('Central Asian University talabalar uchun xalqaro tibbiyot musobaqasini tashkil qildi, jamoalar ishtirok etdi. Reklama','Central Asian University')]
        for text,company in examples:
            source=dict(self.source,text=text,title=text[:180])
            self.assertTrue(relevant(source))
            found=RuleResearcher().classify([source],NOW)
            self.assertEqual(len(found),1,text)
            lead=validate(found[0],{source['id']:source},NOW)
            self.assertIsNotNone(lead,text)
            self.assertEqual(lead['company'],company)
            self.assertEqual(lead['event_kind'],'рекламное размещение')

    def test_anonymous_ads_and_news_about_advertising_are_not_invented_companies(self):
        from sales_agent.rules import is_advertisement
        text='Нужны деньги? Мы поможем оформить кредит под залог имущества. Подробности по телефону. #реклама'
        self.assertFalse(RuleResearcher().classify([dict(self.source,text=text,title=text)],NOW))
        self.assertFalse(is_advertisement('Реклама в Telegram становится популярной: компания рассказала о тенденциях рынка.'))

    def test_paid_material_marker_with_link_is_recognized(self):
        text='Octobank получил награду Visa за развитие цифровых платежей в Узбекистане. Награда стала признанием достижений банка. https://kapital.uz/octobank-poluchil-nagradu/ (на правах рекламы)'
        source=dict(self.source,text=text,title=text[:180])
        self.assertTrue(relevant(source))
        found=RuleResearcher().classify([source],NOW)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['company'],'Octobank')
        self.assertIsNotNone(validate(found[0],{source['id']:source},NOW))

    def test_slogans_and_parts_of_product_names_do_not_become_companies(self):
        examples=[
            ('BIR KUN. KATTA IMKONIYATLAR. FOYDALI XARIDLAR! Kutgan xaridingizni amalga oshiring: MEDIAPARK’da keng turdagi texnikalar va katta chegirmalar! #реклама','MEDIAPARK'),
            ('Ваш iPhone это ваша карта Mastercard. Загрузите карту Mastercard в Apple Wallet и оплачивайте ей покупки. На правах рекламы','Mastercard'),
            ('Қаланыздағы саудаңызды payme go арқалы TezQR менен төлең ҳәм кешбек алың. Қосымшада payme go ашың. #реклама','payme')]
        for text,company in examples:
            source=dict(self.source,text=text,title=text[:180])
            found=RuleResearcher().classify([source],NOW)
            self.assertEqual(len(found),1,text)
            self.assertEqual(found[0]['company'],company)
        text='Регистрация на Copernicus Olympiad открыта. Участники получат приглашение на Global Round в США. На правах рекламы'
        self.assertFalse(RuleResearcher().classify([dict(self.source,text=text,title=text)],NOW))

    def test_advertiser_is_not_a_location_product_or_research_title(self):
        examples=[
            ('2 KUNDAN SO‘NG BIRINCHI G‘OLIBLAR ANIQLANADI! ASLZAR’dan uy va avtomobil yutib olish imkoniyati tobora yaqinlashmoqda! #реклама','ASLZAR',()),
            ('Chimyonda tayyor apartamentlar sotiladi. Dam olish uchun ham apartament xarid qiling. Batafsil maʼlumot uchun MKBANKga murojaat qiling! Reklama','MKBANK',()),
            ('Бизнесингиз учун Apple Pay’ни Click орқали уланг! Click — бизнес учун Apple Pay тўлов ечимини улаш бўйича Apple ҳамкори. Реклама','Click',()),
            ('В Ташкенте подвели итоги исследования CX Banking Index 2026. Награждение прошло на площадке Uztelecom, который выступил соорганизатором (реклама).','Uztelecom',('Uztelecom',)),
            ('Платёжная система Visa отметила Octobank наградой Digital Wallets Launch Excellence за успешное внедрение цифровых платёжных решений (реклама).','Octobank',()),
            ('Квартал Mirabad Square получил системы кондиционирования: стратегическое партнёрство Apex Development и Daikin предусматривает оснащение всех квартир (реклама).','Apex Development',()),
            ('Iskander Buildings Mezbon — новый жилой проект с удобными квартирами, инфраструктурой и современными решениями для семей (реклама).','Iskander Buildings',())]
        for text,company,known in examples:
            source=dict(self.source,text=text,title=text[:180])
            found=RuleResearcher(known).classify([source],NOW)
            self.assertEqual(len(found),1,text)
            lead=validate(found[0],{source['id']:source},NOW)
            self.assertIsNotNone(lead,text)
            self.assertEqual(lead['company'],company)

    def test_repeat_brand_updates_latest_fact_and_keeps_every_post(self):
        lead=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.store.add_lead(lead);ident=lead['id']
        later=dict(lead,published=(NOW+timedelta(days=1)).isoformat(),source_url='https://t.me/spotuz/124',evidence_quote=TEXT+' Новая публикация.')
        self.assertFalse(self.store.add_lead(later))
        self.assertEqual(self.store.leads()[0]['id'],ident)
        self.assertEqual(self.store.leads()[0]['source_url'],later['source_url'])
        self.assertEqual(len(self.store.publications()),2)
        self.store.add_lead(lead)
        self.assertEqual(len(self.store.publications()),2)
        self.assertEqual(self.store.leads()[0]['source_url'],later['source_url'])

    def test_latest_signal_preserves_team_columns_and_custom_script(self):
        old=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.store.add_lead(old)
        new=dict(old,published=(NOW+timedelta(days=1)).isoformat(),source_url='https://t.me/spotuz/124',evidence_quote=TEXT+' Новое событие.',scripts=dict(old['scripts'],first='New generated script'))
        self.store.add_lead(new)
        row=lead_row(old);row[11]='Встреча';row[14]='Заметка команды'
        class Fake(Sheets):
            def __init__(self):self.writes=[];self.timezone='Asia/Tashkent'
            def verify(self):return {}
            def read(self,name,bounds):
                return [LEAD_HEADERS,row] if name=='Лиды' else [SCRIPT_HEADERS,[old['id'],old['company'],'Custom team script',old['scripts']['followup'],old['scripts']['call'],old['scripts']['objections']]]
            def api(self,method,path='',**kwargs):self.writes.append(kwargs['json']);return {}
        sheet=Fake();sheet.sync(self.store)
        writes=sheet.writes[-1]['data']
        self.assertTrue(any(w['range']=="'Лиды'!I2" and w['values']==[[new['source_url']]] for w in writes))
        self.assertFalse(any("K2" in w['range'] or "O2" in w['range'] or "'Скрипты'!C2"==w['range'] for w in writes))

    def test_fresh_leads_sort_after_writes_with_all_team_columns(self):
        old=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.store.add_lead(old)
        second=dict(old,company='SecondBrand',published=(NOW+timedelta(hours=1)).isoformat(),source_url='https://t.me/spotuz/124')
        self.store.add_lead(second)
        class Fake(Sheets):
            def __init__(self):self.calls=[];self.timezone='Asia/Tashkent'
            def verify(self):return {'sheets':[{'properties':{'sheetId':42,'title':'Лиды','gridProperties':{'columnCount':26}}}]}
            def publication_sheet(self,meta):return {'properties':{'sheetId':43}}
            def read(self,name,bounds):
                if name=='Лиды':return [LEAD_HEADERS,lead_row(old)]
                if name=='Скрипты':return [SCRIPT_HEADERS]
                from sales_agent.sheets import PUBLICATION_HEADERS
                return [PUBLICATION_HEADERS]
            def api(self,method,path='',**kwargs):self.calls.append((path,kwargs['json']));return {}
        sheet=Fake();sheet.sync(self.store)
        sort_call=sheet.calls[-1]
        sort=next(r['sortRange'] for r in sort_call[1]['requests'] if r['sortRange']['range']['sheetId']==42)
        self.assertEqual(sort['range'],dict(sheetId=42,startRowIndex=1,endRowIndex=3,startColumnIndex=0,endColumnIndex=26))
        self.assertEqual(sort['sortSpecs'],[dict(dimensionIndex=5,sortOrder='DESCENDING')])
        self.assertEqual(sheet.calls[-2][0],'/values:batchUpdate')
        self.assertEqual(self.store.state('lead_recent_first_v1'),'1')
        # A team sort remains until a new company or fresher publication arrives.
        def read(name,bounds):
            if name=='Лиды':return [LEAD_HEADERS,*[lead_row(l) for l in self.store.leads()]]
            if name=='Скрипты':return [SCRIPT_HEADERS]
            from sales_agent.sheets import PUBLICATION_HEADERS
            return [PUBLICATION_HEADERS]
        sheet.read=read;sheet.calls=[];sheet.sync(self.store)
        sorts=[r for path,data in sheet.calls if path==':batchUpdate' for r in data['requests'] if 'sortRange' in r]
        self.assertFalse(any(r['sortRange']['range']['sheetId']==42 for r in sorts))

    def test_script_order_matches_actual_leads_and_preserves_custom_content(self):
        class Fake(Sheets):
            def __init__(self):
                self.leads=[['new'],['same-date-b'],['same-date-a'],['old']]
                self.rows=[['old','Old','Edited by team',{'note':'Keep this note'}],
                           ['same-date-a','A','Custom A'],['new','New','Custom newest'],
                           ['unknown','Manual extra','Keep extra row'],['same-date-b','B','Custom B']]
                self.before={r[0]:r[:] for r in self.rows};self.calls=[]
            def read(self,name,bounds):
                self.calls.append(('read',name,bounds))
                return self.leads if name=='Лиды' else [[r[0]] for r in self.rows]
            def api(self,method,path='',**kwargs):
                self.calls.append(('write',method,path))
                for req in kwargs['json']['requests']:
                    self.assert_move(req)
                    move=req['moveDimension'];source=move['source']['startIndex']-1
                    self.rows.insert(move['destinationIndex']-1,self.rows.pop(source))
            def assert_move(self,req):
                assert set(req)=={'moveDimension'} and req['moveDimension']['source']['dimension']=='ROWS'
        sheet=Fake();meta={'sheets':[{'properties':{'title':n,'sheetId':i}} for i,n in enumerate(('Лиды','Скрипты'))]}
        sheet.align_script_order(meta)
        self.assertEqual([r[0] for r in sheet.rows],['new','same-date-b','same-date-a','old','unknown'])
        self.assertEqual({r[0]:r for r in sheet.rows},sheet.before)
        sheet.calls=[];sheet.align_script_order(meta)
        self.assertFalse(any(c[0]=='write' for c in sheet.calls))
        # A manual sort on Leads is reflected on the next sync, including tied dates.
        sheet.leads=[['old'],['same-date-a'],['same-date-b'],['new']]
        sheet.align_script_order(meta)
        self.assertEqual([r[0] for r in sheet.rows],['old','same-date-a','same-date-b','new','unknown'])
        self.assertEqual({r[0]:r for r in sheet.rows},sheet.before)

    def test_script_order_keeps_blanks_and_rejects_ambiguous_duplicate_ids(self):
        rows=[[],['b'],['a'],['unknown']]
        for req in script_row_moves(17,[['a'],['b']],rows):
            move=req['moveDimension'];rows.insert(move['destinationIndex']-1,rows.pop(move['source']['startIndex']-1))
        self.assertEqual(rows,[['a'],['b'],[],['unknown']])
        for leads,scripts in [([['a'],['a']],[['a']]),([['a']],[['a'],['a']])]:
            with self.assertRaisesRegex(ValueError,'Duplicate company IDs'):script_row_moves(17,leads,scripts)

    def test_script_alignment_runs_after_all_sync_writes_and_lead_sort(self):
        lead=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        self.store.add_lead(lead)
        self.store.add_lead(dict(lead,company='Other',source_url='https://t.me/spotuz/124'))
        class Fake(Sheets):
            def __init__(self):self.calls=[];self.timezone='Asia/Tashkent'
            def verify(self):return {'sheets':[{'properties':{'title':'Лиды','sheetId':42}}]}
            def publication_sheet(self,meta):return {'properties':{'sheetId':43}}
            def read(self,name,bounds):
                from sales_agent.sheets import PUBLICATION_HEADERS
                return [LEAD_HEADERS] if name=='Лиды' else [SCRIPT_HEADERS] if name=='Скрипты' else [PUBLICATION_HEADERS]
            def api(self,method,path='',**kwargs):self.calls.append((path,kwargs['json']));return {}
            def align_script_order(self,meta):
                assert self.calls[-1][0]==':batchUpdate'
                assert any(r.get('sortRange',{}).get('range',{}).get('sheetId')==42 for r in self.calls[-1][1]['requests'])
                self.calls.append(('aligned',{}))
        sheet=Fake();sheet.sync(self.store)
        self.assertEqual(sheet.calls[-1][0],'aligned')

    def test_ad_subject_is_not_the_rating_agency_or_ranking(self):
        examples=[
            ('Fitch Ratings «Oʻzbektelekom» АКнинг мустақил кредит профилини «bb» даражасига кўтарди. Компаниянинг янги натижалари ҳақида тўлиқ маълумот. Реклама','Oʻzbektelekom'),
            ('Toşkent amaliy fanlar universiteti THE xalqaro reytingiga kirdi. Times Higher Education (THE) World University Rankings 2027 xalqaro reytingida oliygoh Reporter maqomini oldi. Reklama','Toşkent amaliy fanlar universiteti'),
            ('Ташкентский университет прикладных наук получил статус Reporter в рейтинге THE World University Rankings 2027. ТАФУ вошёл в число негосударственных вузов (реклама).','Ташкентский университет прикладных наук')]
        for text,name in examples:
            row=dict(self.source,text=text,title=text[:180])
            found=RuleResearcher(['World University','THE World University','Fitch Ratings']).classify([row],NOW)
            self.assertEqual(len(found),1,text);self.assertEqual(found[0]['company'],name)
            self.assertIsNotNone(validate(found[0],{row['id']:row},NOW))

    def test_known_language_aliases_share_one_company_and_keep_publications(self):
        lead=validate(candidate(self.source['id']),{self.source['id']:self.source},NOW)
        for names in [('Uztelecom','Oʻzbektelekom'),('Ташкентский университет прикладных наук','Toşkent amaliy fanlar universiteti','TAFU')]:
            for index,name in enumerate(names):
                added=self.store.add_lead(dict(lead,company=name,source_url=f'https://t.me/test/{names[0]}/{index}'))
                self.assertEqual(added,index==0)
        self.assertEqual(len(self.store.leads()),2)
        self.assertEqual(len(self.store.publications()),5)

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

    def test_short_edited_publication_becomes_a_lead_and_keeps_source_contact(self):
        from sales_agent.sources import collect
        self.cfg.feeds=[]; self.cfg.channels=['example']
        original='Информация для покупателей.'
        revised='Haval открыл новый салон. #реклама'
        def page(body):
            return (f'<div class="tgme_widget_message" data-post="example/7"><time datetime="{NOW.isoformat()}"></time>'
                    f'<div class="tgme_widget_message_text">{body}<a href="https://haval.uz"></a></div></div>')
        with patch('sales_agent.sources.fetch',return_value=page(original)):
            collect(self.cfg,self.store,NOW)
        cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        with patch('sales_agent.sources.fetch',return_value=page(revised)):
            self.assertEqual(cycle(self.cfg,self.store,now=NOW)['status'],'ok')
            self.assertEqual(cycle(self.cfg,self.store,now=NOW)['new_publications'],0)
        lead=next(l for l in self.store.leads() if l['company']=='Haval')
        self.assertEqual(lead['evidence_quote'],revised)
        self.assertIn('https://haval.uz',lead['contact'])

    def test_history_restarts_on_saved_page_and_keeps_new_tip(self):
        import json
        from sales_agent.sources import collect
        self.cfg.feeds=[];self.cfg.channels=['example']
        url='https://t.me/s/example'
        def page(lo,hi,older=None,media=False):
            rows=''.join(f'<div class="tgme_widget_message" data-post="example/{i}"><time datetime="{NOW.isoformat()}"></time>'
                         + ('' if media else '<div class="tgme_widget_message_text">Текст</div>')+'</div>' for i in range(lo,hi+1))
            return rows+(f'<a href="/s/example?before={older}"></a>' if older else '')
        pages={url:page(9,10,9),url+'?before=9':page(7,8,7,True),url+'?before=7':page(5,6,5),
               url+'?before=5':page(3,4,3),url+'?before=3':page(1,2)}
        with patch('sales_agent.sources.fetch',side_effect=lambda address:pages[address]):
            collect(self.cfg,self.store,NOW)
            self.assertEqual(json.loads(self.store.state('telegram_history:example'))['before'],5)
            self.store.close();self.store=Store(self.root/'sales.db')
            with patch('sales_agent.sources.fetch',side_effect=[page(11,12,11),OSError('lost response')]):
                collect(self.cfg,self.store,NOW)
            self.assertEqual(json.loads(self.store.state('telegram_history:example'))['before'],5)
            pages[url]=page(11,12,11)
            collect(self.cfg,self.store,NOW)
            self.assertEqual(json.loads(self.store.state('telegram_history:example')),{'head':10})
            pages[url+'?before=11']=page(9,10,9)
            collect(self.cfg,self.store,NOW)
        self.assertEqual(json.loads(self.store.state('telegram_history:example')),{'head':12})
        rows=self.store.db.execute("SELECT url FROM sources WHERE origin='Telegram @example'").fetchall()
        self.assertEqual({int(r[0].rsplit('/',1)[1]) for r in rows},set(range(1,13))-{7,8})

    def test_all_sources_failed_still_drains_saved_queue_and_flags_current_cycle(self):
        self.store.source_health('removed_source','ok','')
        self.cfg.feeds=['https://example.invalid/rss'];self.cfg.channels=[]
        with patch('sales_agent.sources.fetch',side_effect=OSError()):
            result=cycle(self.cfg,self.store,now=NOW)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['new_leads'],1)

    def test_once_honors_pause_and_does_not_collect(self):
        from sales_agent.runner import run
        with patch('sales_agent.runner.Sheets') as sheets,patch('sales_agent.runner.cycle') as scan:
            sheets.return_value.read.return_value=[[True]]
            run(self.cfg,once=True)
            scan.assert_not_called()
            sheets.return_value.sync.assert_called_once()

    def test_restart_closes_orphan_run_and_concurrent_once_cannot_touch_it(self):
        from sales_agent.runner import run
        path=self.cfg.data_dir/'sales.sqlite3'
        db=Store(path)
        with db.db:
            db.db.execute("INSERT INTO runs(started,status) VALUES(?,'running')",(NOW.isoformat(),))
        db.close()
        with patch('sales_agent.runner.Sheets') as sheets:
            sheets.return_value.read.return_value=[[True]]
            def another_run(_):
                with self.assertRaisesRegex(RuntimeError,'уже выполняется'):
                    run(self.cfg,once=True)
            sheets.return_value.sync.side_effect=another_run
            run(self.cfg,once=True)
        db=Store(path)
        try:
            self.assertEqual(tuple(db.db.execute('SELECT status,finished FROM runs').fetchone()),('interrupted',None))
        finally:
            db.close()

    def test_malformed_contact_link_does_not_block_other_sources(self):
        import json
        from sales_agent.research import enrich_contact
        row=dict(self.source,links=json.dumps(['https://[','https://other-testbrand.uz','https://testbrand.uz']))
        lead=validate(candidate(row['id']),{row['id']:row},NOW)
        self.assertTrue(enrich_contact(lead,row)['contact'].startswith('https://testbrand.uz '))

    def test_atom_uses_article_url_when_self_link_is_last(self):
        rows=parse_feed(f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Компания</title>'
            f'<updated>{NOW.isoformat()}</updated><link rel="alternate" href="https://example.uz/article"/>'
            '<link rel="self" href="https://example.uz/feed"/></entry></feed>','test')
        self.assertEqual(rows[0]['url'],'https://example.uz/article')

    def test_newer_page_of_news_does_not_hide_the_older_lead(self):
        for i in range(305):
            self.store.add_source(dict(url=f'https://example.uz/{i}',title='Погода',text='Погода',
                published=(NOW+timedelta(seconds=1)).isoformat(),origin='news'))
        result=cycle(self.cfg,self.store,now=NOW+timedelta(minutes=1),collect_sources=False)
        self.assertEqual(result['new_leads'],1)
        self.assertFalse(self.store.pending('2026'))

    def test_invalid_rss_article_url_does_not_block_the_valid_item(self):
        body=''.join(f'<item><link>{url}</link><title>Новость</title><pubDate>{NOW.isoformat()}</pubDate></item>'
            for url in ['https://[','https://example.uz/article'])
        self.assertEqual([r['url'] for r in parse_feed('<rss>'+body+'</rss>','news')],['https://example.uz/article'])

    def test_deferred_articles_keep_the_issuer_and_host_institution(self):
        examples=[
            ('Турецкая OYAK рассматривает участие в проекте. Глава компании Мурат Ялчинташ обозначил направления для расширения сотрудничества.', 'OYAK'),
            ('Samarqanddagi “Ipak yo‘li” turizm va madaniy meros xalqaro universiteti Xitoyning Guilin Tourism University bilan hamkorlikda qabul e’lon qildi. Reklama','“Ipak yo‘li” turizm va madaniy meros xalqaro universiteti')]
        for text,company in examples:
            row=dict(self.source,title=text,text=text)
            self.assertEqual([c['company'] for c in RuleResearcher().classify([row],NOW)],[company])

    def test_anonymous_sponsored_teaser_uses_verified_publisher_copy_and_keeps_post_url(self):
        self.store.set_state('research_engine','rules-2')
        self.store.processed([self.source['id']])
        preview='Как наладить IT-мониторинг 24/7 и выбрать модель SOC для компании (на правах рекламы)'
        article='https://kapital.uz/example/'
        self.store.add_source(dict(url='https://t.me/KPTLUZ/1',origin='Telegram @KPTLUZ',published=NOW.isoformat(),
            title=preview,text=preview,links=[article]))
        page='<div class="article-content"><p><a href="https://onesec.uz/">ONESEC</a> предлагает компаниям три формата организации центра мониторинга.</p></div>'
        with patch('sales_agent.research.fetch',side_effect=OSError('temporary')):
            cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        self.assertEqual(len(self.store.pending('2026')),1)
        with patch('sales_agent.research.fetch',return_value=page):
            result=cycle(self.cfg,self.store,now=NOW,collect_sources=False)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(len(self.store.publications()),1)
        lead=self.store.leads()[0]
        self.assertEqual((lead['company'],lead['source_url'],lead['evidence_url']),('ONESEC','https://t.me/KPTLUZ/1',article))
        self.assertIn(article,lead['contact'])

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
