"""Full sync scenarios: insert, refresh, retained team edits and interrupted writes."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import re
import tempfile
import unittest

from sales_agent.research import scripts
from sales_agent.sheets import Sheets,LEAD_HEADERS,SCRIPT_HEADERS,PUBLICATION_HEADERS
from sales_agent.store import Store
from test_sales import NOW,candidate


def position(address):
    match=re.fullmatch(r'([A-Z]+)(\d+)',address)
    column=0
    for letter in match[1]:column=column*26+ord(letter)-64
    return int(match[2])-1,column-1


class MemorySheets(Sheets):
    def __init__(self):
        self.timezone='Asia/Tashkent'
        self.rows={name:[deepcopy(headers)] for name,headers in (
            ('Лиды',LEAD_HEADERS),('Скрипты',SCRIPT_HEADERS),('Публикации',PUBLICATION_HEADERS),
            ('Источники',[]),('Работа агента',[]))}
        self.ids={name:index for index,name in enumerate(self.rows)}
        self.fail_sort=False;self.lose_values_reply=False;self.lose_moves_reply=False
        self.writes=[]

    def verify(self):
        return {'sheets':[{'properties':{'title':name,'sheetId':sid,'gridProperties':{'columnCount':26}}}
                          for name,sid in self.ids.items()]}

    def read(self,name,bounds):
        start,end=bounds.split(':');r0,c0=position(start);r1,c1=position(end)
        result=[deepcopy(row[c0:c1+1]) for row in self.rows[name][r0:r1+1]]
        while result and not any(v not in ('',None) for v in result[-1]):result.pop()
        return result

    def api(self,method,path='',**kwargs):
        payload=kwargs['json']
        if path=='/values:batchUpdate':
            for write in payload['data']:
                self.writes.append(deepcopy(write))
                sheet,bounds=write['range'].split('!');sheet=sheet.strip("'")
                r0,c0=position(bounds.split(':')[0]);rows=self.rows[sheet]
                while len(rows)<r0+len(write['values']):rows.append([])
                for index,values in enumerate(write['values'],r0):
                    while len(rows[index])<c0+len(values):rows[index].append('')
                    rows[index][c0:c0+len(values)]=deepcopy(values)
            if self.lose_values_reply:
                self.lose_values_reply=False;raise RuntimeError('Values committed but reply lost')
            return {}
        requests=payload['requests']
        if self.fail_sort and any('sortRange' in req for req in requests):
            self.fail_sort=False;raise RuntimeError('Sort temporarily unavailable')
        moved=False
        for req in requests:
            if 'sortRange' in req:
                spec=req['sortRange'];area=spec['range'];rows=self.rows[next(n for n,i in self.ids.items() if i==area['sheetId'])]
                start,end=area['startRowIndex'],area['endRowIndex'];column=spec['sortSpecs'][0]['dimensionIndex']
                rows[start:end]=sorted(rows[start:end],key=lambda row:row[column],reverse=spec['sortSpecs'][0]['sortOrder']=='DESCENDING')
            elif 'moveDimension' in req:
                move=req['moveDimension'];source=move['source'];rows=self.rows[next(n for n,i in self.ids.items() if i==source['sheetId'])]
                rows.insert(move['destinationIndex'],rows.pop(source['startIndex']));moved=True
        if moved and self.lose_moves_reply:
            self.lose_moves_reply=False;raise RuntimeError('Rows moved but reply lost')
        return {}


class SyncLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3'
        self.store=Store(self.path);self.sheet=MemorySheets()

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def add(self,name,minutes=0):
        lead=candidate('source');lead.update(company=name,published=(NOW+timedelta(minutes=minutes)).isoformat(),
            checked=NOW.isoformat(),source_url=f'https://example.invalid/{name}/{minutes}',contact='Auto contact')
        lead['scripts']=scripts(lead);self.store.add_lead(lead)
        return next(v for v in self.store.leads() if v['company']==name)

    def row(self,name,ident):
        return next(row for row in self.sheet.rows[name][1:] if row and row[0]==ident)

    def assert_order(self):
        leads=self.sheet.rows['Лиды'][1:];scripts=self.sheet.rows['Скрипты'][1:]
        self.assertEqual([r[0] for r in leads],[r[0] for r in scripts])
        self.assertEqual(len(leads),len({r[0] for r in leads}))
        self.assertEqual([r[5] for r in leads],sorted((r[5] for r in leads),reverse=True))

    def test_additions_ties_and_refresh_keep_team_edits_attached_to_id(self):
        old=self.add('Old',0);self.add('Recent',10);self.sheet.sync(self.store)
        row=self.row('Лиды',old['id']);row.extend(['']*(26-len(row)))
        for column,value in {4:'Team fact',6:'Team service',7:'Team hypothesis',9:'Team contact',
                             10:'Проверено',11:'Встреча',12:'Позвонить',13:47000,14:'Team note',17:'Extra team column',25:'Last column'}.items():
            row[column]=value
        script=self.row('Скрипты',old['id']);script.extend(['']*(26-len(script)))
        script[2:6]=['Custom first','','Custom call','Custom objections'];script[6]='Extra script';script[25]='Last script column'
        expected_lead=deepcopy(row);expected_script=deepcopy(script)
        self.add('New A',20);self.add('New B',20);self.sheet.sync(self.store);self.assert_order()
        self.sheet.writes=[];self.add('Old',30);self.sheet.sync(self.store);self.assert_order()
        row=self.row('Лиды',old['id'])
        for column in (4,6,7,9,10,11,12,13,14,17,25):self.assertEqual(row[column],expected_lead[column])
        self.assertEqual(self.row('Скрипты',old['id']),expected_script)
        # Protected team cells are excluded from the write payload, rather than
        # being read and sent back in a larger stale block.
        protected={4,6,7,9,10,11,12,13,14,17,25}
        for write in self.sheet.writes:
            name,bounds=write['range'].split('!')
            if name!="'Лиды'":continue
            _,start=position(bounds.split(':')[0]);width=len(write['values'][0])
            self.assertFalse(protected.intersection(range(start,start+width)))
        self.assertEqual(self.sheet.rows['Лиды'][1][0],old['id'])
        # Repeat after process restart: no duplicate companies or lost custom text.
        self.store.close();self.store=Store(self.path);before=deepcopy(self.sheet.rows)
        self.sheet.sync(self.store);self.assert_order()
        self.assertEqual(self.sheet.rows['Лиды'],before['Лиды']);self.assertEqual(self.sheet.rows['Скрипты'],before['Скрипты'])

    def test_failed_sort_after_new_rows_are_written_recovers_after_restart(self):
        self.add('Old');self.sheet.sync(self.store);fresh=self.add('New',10)
        self.sheet.fail_sort=True
        with self.assertRaisesRegex(RuntimeError,'Sort temporarily'):self.sheet.sync(self.store)
        self.store.close();self.store=Store(self.path)
        self.sheet.sync(self.store);self.assert_order()
        self.assertEqual(self.sheet.rows['Лиды'][1][0],fresh['id'])

    def test_lost_values_reply_recovers_without_duplicate_rows(self):
        self.add('Old');self.sheet.sync(self.store);fresh=self.add('New',10)
        self.sheet.lose_values_reply=True
        with self.assertRaisesRegex(RuntimeError,'reply lost'):self.sheet.sync(self.store)
        self.store.close();self.store=Store(self.path)
        self.sheet.sync(self.store);self.assert_order()
        self.assertEqual(self.sheet.rows['Лиды'][1][0],fresh['id'])

    def test_failed_sort_after_refresh_recovers_and_keeps_custom_script(self):
        old=self.add('Old',0);self.add('Recent',10);self.sheet.sync(self.store)
        self.row('Скрипты',old['id'])[2]='Manual script'
        self.add('Old',20);self.sheet.fail_sort=True
        with self.assertRaises(RuntimeError):self.sheet.sync(self.store)
        self.sheet.sync(self.store);self.assert_order()
        self.assertEqual(self.sheet.rows['Лиды'][1][0],old['id'])
        self.assertEqual(self.row('Скрипты',old['id'])[2],'Manual script')

    def test_lost_row_move_reply_is_idempotent(self):
        self.add('Old');self.sheet.sync(self.store);self.add('New',10)
        self.sheet.lose_moves_reply=True
        with self.assertRaisesRegex(RuntimeError,'Rows moved'):self.sheet.sync(self.store)
        self.sheet.sync(self.store);self.assert_order()
