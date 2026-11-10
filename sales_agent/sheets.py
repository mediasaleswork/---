from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote,urlsplit

from google.auth import load_credentials_from_file
from google.auth.transport.requests import AuthorizedSession

LEAD_HEADERS = ["ID", "Компания", "Ниша", "Приоритет", "Подтверждённый факт", "Дата публикации (Ташкент)", "Услуга", "Гипотеза потребности", "Источник", "Контактный маршрут", "CRM-проверка", "Статус", "Следующий шаг", "Дата следующего контакта", "Заметки команды", "Проверено UTC", "Уверенность"]
SCRIPT_HEADERS = ["ID", "Компания", "Первое сообщение", "Повторное касание", "Сценарий звонка", "Ответы на возражения"]
HEALTH_HEADERS = ["Источник", "Проверено UTC", "Статус", "Результат"]
RUN_HEADERS = ["Начало UTC", "Завершение UTC", "Статус", "Публикаций в периоде", "Новых лидов", "Результат"]
PUBLICATION_HEADERS=['Компания','Дата публикации (Ташкент)','Площадка','Факт / цитата','Ссылка на публикацию','ID компании']


def datevalue(value,tz_name='UTC'):
    if not value:
        return ""
    return (datetime.fromisoformat(value).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None) - datetime(1899,12,30)).total_seconds()/86400


def lead_row(lead,tz_name='Asia/Tashkent'):
    return [lead["id"],lead["company"],lead["sector"],lead["priority"],lead["evidence_quote"],
        datevalue(lead["published"],tz_name),lead["service"],lead["hypothesis"],lead["source_url"],lead["contact"],
        "Не сверено", "Новый", "Сверить с клиентской базой и найти контакт маркетинга", "", "", datevalue(lead["checked"]), lead["confidence"]]


class Sheets:
    def __init__(self, cfg):
        if not cfg.spreadsheet_id:
            raise ValueError("Set spreadsheet_id")
        creds, _ = load_credentials_from_file(str(cfg.credentials_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.session = AuthorizedSession(creds)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + cfg.spreadsheet_id
        self.timezone=cfg.timezone

    def api(self, method, path="", **kwargs):
        response = self.session.request(method, self.base + path, timeout=(10, 45), **kwargs)
        if not response.ok:
            # Never propagate request URL/body; Google errors can contain credential details.
            raise RuntimeError(f"Google Sheets HTTP {response.status_code}")
        return response.json()

    def read(self, name, bounds):
        return self.api("GET", "/values/" + quote("'" + name + "'!" + bounds, safe=""),
            params={"valueRenderOption":"FORMATTED_VALUE"}).get("values", [])

    def verify(self):
        meta = self.api('GET',params={"fields":"spreadsheetId,spreadsheetUrl,properties(title),sheets(properties,tables)"})
        names = {s["properties"]["title"] for s in meta["sheets"]}
        if not {"Лиды", "Скрипты", "Источники", "Работа агента"} <= names:
            raise ValueError("Wrong workbook: required Mediasales sheets missing")
        if self.read("Лиды", "A1:Q1") != [LEAD_HEADERS] or self.read("Скрипты", "A1:F1") != [SCRIPT_HEADERS]:
            raise ValueError("Workbook headers changed; no cells were written")
        return meta

    def publication_sheet(self,meta):
        sh=next((s for s in meta.get('sheets',[]) if s['properties']['title']=='Публикации'),None)
        if not sh:
            response=self.api('POST',':batchUpdate',json={'requests':[{'addSheet':{'properties':{'title':'Публикации','gridProperties':{'rowCount':5000,'columnCount':6,'frozenRowCount':1}}}}]})
            sh={'properties':response['replies'][0]['addSheet']['properties']}
            sid=sh['properties']['sheetId']
            requests=[{'updateCells':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':6},'rows':[{'values':[{'userEnteredValue':{'stringValue':v}} for v in PUBLICATION_HEADERS]}],'fields':'userEnteredValue'}},
                {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':0,'endIndex':1},'properties':{'pixelSize':44},'fields':'pixelSize'}},
                {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':6},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.93,'green':.93,'blue':.93},'textFormat':{'bold':True,'fontSize':11,'fontFamily':'Arial'},'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat'}}]
            for i,width in enumerate([190,205,180,500,320,155]):
                requests.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':i,'endIndex':i+1},'properties':{'pixelSize':width},'fields':'pixelSize'}})
            self.api('POST',':batchUpdate',json={'requests':requests})
        return sh

    def sync(self, store):
        meta = self.verify() or {}
        writes = []
        leads = self.read("Лиды", "A1:Q5000")
        scripts = self.read("Скрипты", "A1:F5000")
        versions=store.publications()
        latest={l['id']:l for l in store.leads()}
        # A newer source refreshes the publication fields, while team-owned K:O stays intact.
        for index,row in enumerate(leads[1:],start=2):
            if not row or row[0] not in latest or len(row)<9:continue
            lead=latest[row[0]]
            old=next((v for v in versions if v['id']==row[0] and v['source_url']==row[8]),None)
            if old and row[8]!=lead['source_url']:
                current=(row+['']*17)[:17];target=lead_row(lead,self.timezone)
                previous=lead_row(old,self.timezone)
                block=current[4:10]
                for column in range(4,10):
                    if column in (5,8) or current[column]==previous[column]:block[column-4]=target[column]
                writes.append({'range':f"'Лиды'!E{index}:J{index}",'values':[block]})
                writes.append({'range':f"'Лиды'!P{index}:Q{index}",'values':[[target[15],target[16]]]})
        for index,row in enumerate(scripts[1:],start=2):
            if not row or row[0] not in latest:continue
            for column,key in enumerate(['first','followup','call','objections'],start=2):
                value=row[column] if len(row)>column else ''
                target=latest[row[0]]['scripts'][key]
                if value!=target and any(value==v.get('scripts',{}).get(key) for v in versions if v['id']==row[0]):
                    letter=chr(65+column)
                    writes.append({'range':f"'Скрипты'!{letter}{index}",'values':[[target]]})
        # Key by stable ID, never by row order. Existing rows and manual edits stay intact.
        existing = {str(r[0]) for r in leads[1:] if r}
        existing_scripts = {str(r[0]) for r in scripts[1:] if r}
        new_leads = [lead_row(l,self.timezone) for l in store.leads() if l["id"] not in existing]
        new_scripts = [[l["id"],l["company"],*(l["scripts"][k] for k in ("first","followup","call","objections"))]
            for l in store.leads() if l["id"] not in existing_scripts]
        if len(leads) + len(new_leads) >= 5000 or len(scripts) + len(new_scripts) >= 5000:
            raise ValueError("Workbook capacity reached; extend bounded sync ranges")
        if new_leads:
            writes.append({"range":f"'Лиды'!A{len(leads)+1}:Q{len(leads)+len(new_leads)}", "values":new_leads})
        if new_scripts:
            writes.append({"range":f"'Скрипты'!A{len(scripts)+1}:F{len(scripts)+len(new_scripts)}", "values":new_scripts})
        structure = []
        publication_sort=None
        if meta.get('sheets'):
            publication_sheet=self.publication_sheet(meta)
            sid=publication_sheet['properties']['sheetId']
            posted=self.read('Публикации','A1:F5000')
            if not posted or posted[0]!=PUBLICATION_HEADERS:raise ValueError('Publication headers changed')
            published_keys={(r[5],r[4]) for r in posted[1:] if len(r)>=6}
            additions=[]
            for v in versions:
                if (v['id'],v['source_url']) in published_keys:continue
                url=urlsplit(v['source_url']);platform='@'+url.path.strip('/').split('/')[0] if url.hostname=='t.me' else url.hostname
                additions.append([v['company'],datevalue(v['published'],self.timezone),platform,v['evidence_quote'],v['source_url'],v['id']])
            if len(posted)+len(additions)>=5000:raise ValueError('Publication capacity reached')
            if additions:
                end=len(posted)+len(additions)
                publication_sort={'sortRange':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':end,'startColumnIndex':0,'endColumnIndex':6},'sortSpecs':[{'dimensionIndex':1,'sortOrder':'DESCENDING'}]}}
                writes.append({'range':f"'Публикации'!A{len(posted)+1}:F{end}",'values':additions})
                structure.extend([
                    {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':len(posted),'endRowIndex':end,'startColumnIndex':0,'endColumnIndex':6},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP','textFormat':{'fontFamily':'Arial','fontSize':11}}},'fields':'userEnteredFormat'}},
                    {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':end,'startColumnIndex':1,'endColumnIndex':2},'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE_TIME','pattern':'dd.mm.yyyy hh:mm'}}},'fields':'userEnteredFormat.numberFormat'}},
                    {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':len(posted),'endIndex':end},'properties':{'pixelSize':100},'fields':'pixelSize'}},
                    {'setBasicFilter':{'filter':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':end,'startColumnIndex':0,'endColumnIndex':6}}}}])
        for sh in meta.get('sheets', []):
            name=sh['properties']['title']
            additions=new_leads if name=='Лиды' else new_scripts if name=='Скрипты' else []
            previous=len(leads) if name=='Лиды' else len(scripts)
            if not additions:
                continue
            sid=sh['properties']['sheetId']
            structure.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':previous,'endRowIndex':previous+len(additions),'startColumnIndex':0,'endColumnIndex':17 if name=='Лиды' else 6},
                'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP','textFormat':{'fontFamily':'Arial','fontSize':11}}},'fields':'userEnteredFormat'}})
            structure.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':previous,'endIndex':previous+len(additions)},'properties':{'pixelSize':116 if name=='Лиды' else 190},'fields':'pixelSize'}})
            for table in sh.get('tables',[]):
                if table.get('name') in {'MediasalesLeads','MediasalesScripts'} and table['range'].get('endRowIndex',0)<previous+len(additions):
                    structure.append({'updateTable':{'table':{'tableId':table['tableId'],'range':dict(table['range'],endRowIndex=previous+len(additions))},'fields':'range'}})
            if name=='Лиды':
                for column,pattern in [(5,'dd.mm.yyyy hh:mm'),(15,'yyyy-mm-dd hh:mm')]:
                    structure.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1 if column==5 else previous,'endRowIndex':previous+len(additions),'startColumnIndex':column,'endColumnIndex':column+1},
                        'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE_TIME','pattern':pattern}}},'fields':'userEnteredFormat.numberFormat'}})
        if structure:
            self.api('POST',':batchUpdate',json={'requests':structure})
        health = [HEALTH_HEADERS] + [[r[0],datevalue(r[1]),*r[2:]] for r in store.db.execute("SELECT url,checked,status,detail FROM source_health ORDER BY url")]
        runs = [RUN_HEADERS] + [[datevalue(r[0]),datevalue(r[1]),*r[2:]] for r in store.db.execute("SELECT started,finished,status,sources,leads,detail FROM runs ORDER BY id DESC LIMIT 100")]
        writes.extend([{"range":f"'Источники'!A1:D{len(health)}", "values":health},
            {"range":f"'Работа агента'!A9:F{8+len(runs)}", "values":runs},
            {"range":"'Работа агента'!B8:C8", "values":[['Обновлено UTC',datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')]]}])
        # RAW means source text beginning with '=' is never executed as a formula.
        self.api("POST", "/values:batchUpdate", json={"valueInputOption":"RAW", "data":writes})
        if publication_sort:self.api('POST',':batchUpdate',json={'requests':[publication_sort]})
        store.set_state("last_synced", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return len(new_leads)
