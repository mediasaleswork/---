from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote,urlsplit
from .sources import link_host

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


def sheet_rows(sheet):
    return sheet.get('properties',{}).get('gridProperties',{}).get('rowCount',5000)


def extend_grid(requests,sheet,end):
    extra=end-sheet_rows(sheet)
    if extra>0:
        requests.append({'appendDimension':{'sheetId':sheet['properties']['sheetId'],'dimension':'ROWS','length':extra}})


def script_row_moves(sheet_id,lead_rows,script_rows):
    lead_ids=[str(r[0]) for r in lead_rows if r and r[0]]
    current=[str(r[0]) if r and r[0] else None for r in script_rows]
    script_ids=[ident for ident in current if ident is not None]
    if len(set(lead_ids))!=len(lead_ids) or len(set(script_ids))!=len(script_ids):
        raise ValueError('Duplicate company IDs; script rows were not moved')
    present=set(script_ids)
    wanted=[ident for ident in lead_ids if ident in present]
    requests=[]
    for target,ident in enumerate(wanted):
        source=current.index(ident,target)
        if source==target:continue
        # Moves always go upward. Index 0 is the header, and all row content,
        # including custom scripts, notes and extra team columns, moves together.
        requests.append({'moveDimension':{'source':{'sheetId':sheet_id,'dimension':'ROWS',
            'startIndex':source+1,'endIndex':source+2},'destinationIndex':target+1}})
        current.insert(target,current.pop(source))
    return requests


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
        path="/values/" + quote("'" + name + "'!" + bounds, safe="")
        identity={'Лиды':(0,8),'Скрипты':(0,),'Публикации':(4,5)}.get(name,())
        if not identity or not bounds.startswith('A1:'):
            return self.api('GET',path,params={'valueRenderOption':'UNFORMATTED_VALUE'}).get('values',[])
        # A team's formula may display the previous generated text. Compare its
        # expression so the next refresh retains it. Resolve formula-based keys separately.
        rows=self.api('GET',path,params={'valueRenderOption':'FORMULA'}).get('values',[])
        keys=[(n,c) for n,row in enumerate(rows) for c in identity
              if c<len(row) and isinstance(row[c],str) and row[c].startswith('=')]
        if keys:
            effective=self.api('GET',path,params={'valueRenderOption':'UNFORMATTED_VALUE'}).get('values',[])
            for n,c in keys:
                rows[n][c]=effective[n][c] if n<len(effective) and c<len(effective[n]) else ''
        return rows

    def align_script_order(self,meta):
        names={s['properties']['title']:s for s in meta.get('sheets',[])}
        if not {'Лиды','Скрипты'}<=names.keys():return
        # Read the actual order after the lead sort, including ties/manual sorts.
        # Never regenerate or overwrite script values to rearrange their rows.
        leads=self.read('Лиды',f"A2:A{sheet_rows(names['Лиды'])}")
        scripts=self.read('Скрипты',f"A2:A{sheet_rows(names['Скрипты'])}")
        requests=script_row_moves(names['Скрипты']['properties']['sheetId'],leads,scripts)
        if requests:self.api('POST',':batchUpdate',json={'requests':requests})

    def verify(self):
        meta = self.api('GET',params={"fields":"spreadsheetId,spreadsheetUrl,properties(title),sheets(properties,tables,basicFilter)"})
        names = {s["properties"]["title"] for s in meta["sheets"]}
        if not {"Лиды", "Скрипты", "Источники", "Работа агента"} <= names:
            raise ValueError("Wrong workbook: required Mediasales sheets missing")
        if self.read("Лиды", "A1:Q1") != [LEAD_HEADERS] or self.read("Скрипты", "A1:F1") != [SCRIPT_HEADERS]:
            raise ValueError("Workbook headers changed; no cells were written")
        return meta

    def publication_sheet(self,meta):
        sh=next((s for s in meta.get('sheets',[]) if s['properties']['title']=='Публикации'),None)
        if not sh:
            used={s['properties']['sheetId'] for s in meta.get('sheets',[])}
            sid=next(i for i in range(len(used)+1) if i not in used)
            sh={'properties':{'sheetId':sid,'title':'Публикации','gridProperties':{'rowCount':5000,'columnCount':6,'frozenRowCount':1}}}
            requests=[{'addSheet':sh},{'updateCells':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':6},'rows':[{'values':[{'userEnteredValue':{'stringValue':v}} for v in PUBLICATION_HEADERS]}],'fields':'userEnteredValue'}},
                {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':0,'endIndex':1},'properties':{'pixelSize':44},'fields':'pixelSize'}},
                {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':6},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.93,'green':.93,'blue':.93},'textFormat':{'bold':True,'fontSize':11,'fontFamily':'Arial'},'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat'}}]
            for i,width in enumerate([190,205,180,500,320,155]):
                requests.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':i,'endIndex':i+1},'properties':{'pixelSize':width},'fields':'pixelSize'}})
            self.api('POST',':batchUpdate',json={'requests':requests})
        return sh

    def sync(self, store):
        meta = self.verify() or {}
        writes = []
        tabs={s['properties']['title']:s for s in meta.get('sheets',[])}
        leads = self.read("Лиды", f"A1:Q{sheet_rows(tabs.get('Лиды',{}))}")
        scripts = self.read("Скрипты", f"A1:F{sheet_rows(tabs.get('Скрипты',{}))}")
        versions=store.publications()
        revisions=store.publication_revisions()
        by_source={}
        script_versions={}
        for v in [*versions,*revisions]:
            by_source.setdefault((v['id'],v['source_url']),[]).append(v)
            for key,value in v.get('scripts',{}).items():
                script_versions.setdefault((v['id'],key),set()).add(value)
        latest={l['id']:l for l in store.leads()}
        refreshed=False
        # A newer source refreshes the publication fields, while team-owned K:O stays intact.
        for index,row in enumerate(leads[1:],start=2):
            if not row or row[0] not in latest or len(row)<9:continue
            lead=latest[row[0]]
            history=by_source.get((row[0],row[8]),[])
            if history and (row[8]!=lead['source_url'] or len(history)>1):
                current=(row+['']*17)[:17];target=lead_row(lead,self.timezone)
                previous=[lead_row(v,self.timezone) for v in history]
                row_changed=False
                for column in (2,3,4,5,6,7,8,9,16):
                    if (column in (5,8) or any(current[column]==p[column] for p in previous)) and current[column]!=target[column]:
                        writes.append({'range':f"'Лиды'!{chr(65+column)}{index}",'values':[[target[column]]]})
                        row_changed=True
                        if column==5:refreshed=True
                if row_changed:
                    writes.append({'range':f"'Лиды'!P{index}",'values':[[target[15]]]})
        for index,row in enumerate(scripts[1:],start=2):
            if not row or row[0] not in latest:continue
            for column,key in enumerate(['first','followup','call','objections'],start=2):
                value=row[column] if len(row)>column else ''
                target=latest[row[0]]['scripts'][key]
                if value!=target and value in script_versions.get((row[0],key),()):
                    letter=chr(65+column)
                    writes.append({'range':f"'Скрипты'!{letter}{index}",'values':[[target]]})
        # Key by stable ID, never by row order. Existing rows and manual edits stay intact.
        existing = {str(r[0]) for r in leads[1:] if r}
        existing_scripts = {str(r[0]) for r in scripts[1:] if r}
        new_leads = [lead_row(l,self.timezone) for l in store.leads() if l["id"] not in existing]
        new_scripts = [[l["id"],l["company"],*(l["scripts"][k] for k in ("first","followup","call","objections"))]
            for l in store.leads() if l["id"] not in existing_scripts]
        if new_leads or refreshed:
            # Persist before any Google mutation. If values commit but the reply
            # or later sort fails, the next sync/restart must still sort them.
            store.set_state('lead_recent_first_v1','')
        if new_leads:
            writes.append({"range":f"'Лиды'!A{len(leads)+1}:Q{len(leads)+len(new_leads)}", "values":new_leads})
        if new_scripts:
            writes.append({"range":f"'Скрипты'!A{len(scripts)+1}:F{len(scripts)+len(new_scripts)}", "values":new_scripts})
        structure = []
        publication_sort=None
        if meta.get('sheets'):
            publication_sheet=self.publication_sheet(meta)
            sid=publication_sheet['properties']['sheetId']
            posted=self.read('Публикации',f'A1:F{sheet_rows(publication_sheet)}')
            if not posted or posted[0]!=PUBLICATION_HEADERS:raise ValueError('Publication headers changed')
            published_keys={(r[5],r[4]) for r in posted[1:] if len(r)>=6}
            changed_dates=False
            for index,row in enumerate(posted[1:],start=2):
                if len(row)<6:continue
                history=by_source.get((row[5],row[4]),[])
                if len(history)<2:continue
                for column,key in [(1,'published'),(3,'evidence_quote')]:
                    values=[datevalue(v[key],self.timezone) if column==1 else v[key] for v in history]
                    if row[column]!=values[0] and row[column] in values[1:]:
                        writes.append({'range':f"'Публикации'!{chr(65+column)}{index}",'values':[[values[0]]]})
                        if column==1:changed_dates=True
            additions=[]
            for v in versions:
                if (v['id'],v['source_url']) in published_keys:continue
                host=link_host(v['source_url'])
                platform='@'+urlsplit(v['source_url']).path.strip('/').split('/')[0] if host=='t.me' else host
                additions.append([v['company'],datevalue(v['published'],self.timezone),platform,v['evidence_quote'],v['source_url'],v['id']])
            end=len(posted)+len(additions)
            extend_grid(structure,publication_sheet,end)
            if additions or changed_dates:
                store.set_state('publication_recent_first_v1','pending')
            if end>2 and store.state('publication_recent_first_v1')!='done':
                width=publication_sheet['properties'].get('gridProperties',{}).get('columnCount',6)
                publication_sort={'sortRange':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':end,'startColumnIndex':0,'endColumnIndex':width},'sortSpecs':[{'dimensionIndex':1,'sortOrder':'DESCENDING'}]}}
            if additions:
                writes.append({'range':f"'Публикации'!A{len(posted)+1}:F{end}",'values':additions})
                structure.extend([
                    {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':len(posted),'endRowIndex':end,'startColumnIndex':0,'endColumnIndex':6},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP','textFormat':{'fontFamily':'Arial','fontSize':11}}},'fields':'userEnteredFormat'}},
                    {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':len(posted),'endRowIndex':end,'startColumnIndex':1,'endColumnIndex':2},'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE_TIME','pattern':'dd.mm.yyyy hh:mm'}}},'fields':'userEnteredFormat.numberFormat'}},
                    {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':len(posted),'endIndex':end},'properties':{'pixelSize':100},'fields':'pixelSize'}},
                    ])
                active=publication_sheet.get('basicFilter',{})
                area=active.get('range',{'sheetId':sid,'startRowIndex':0,'startColumnIndex':0,'endColumnIndex':6,'endRowIndex':0})
                if area.get('endRowIndex',end)<end:
                    structure.append({'setBasicFilter':{'filter':{**active,'range':{**area,'endRowIndex':end}}}})
        for sh in meta.get('sheets', []):
            name=sh['properties']['title']
            additions=new_leads if name=='Лиды' else new_scripts if name=='Скрипты' else []
            previous=len(leads) if name=='Лиды' else len(scripts)
            if not additions:
                continue
            sid=sh['properties']['sheetId']
            extend_grid(structure,sh,previous+len(additions))
            structure.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':previous,'endRowIndex':previous+len(additions),'startColumnIndex':0,'endColumnIndex':17 if name=='Лиды' else 6},
                'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP','textFormat':{'fontFamily':'Arial','fontSize':11}}},'fields':'userEnteredFormat'}})
            structure.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':previous,'endIndex':previous+len(additions)},'properties':{'pixelSize':116 if name=='Лиды' else 190},'fields':'pixelSize'}})
            for table in sh.get('tables',[]):
                if table.get('name') in {'MediasalesLeads','MediasalesScripts'} and table['range'].get('endRowIndex',0)<previous+len(additions):
                    structure.append({'updateTable':{'table':{'tableId':table['tableId'],'range':dict(table['range'],endRowIndex=previous+len(additions))},'fields':'range'}})
            if name=='Лиды':
                for column,pattern in [(5,'dd.mm.yyyy hh:mm'),(15,'yyyy-mm-dd hh:mm')]:
                    structure.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':previous,'endRowIndex':previous+len(additions),'startColumnIndex':column,'endColumnIndex':column+1},
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
        sorts=[publication_sort] if publication_sort else []
        lead_sheet=next((s for s in meta.get('sheets',[]) if s['properties']['title']=='Лиды'),None)
        if lead_sheet and len(leads)+len(new_leads)>2 and (new_leads or refreshed or not store.state('lead_recent_first_v1')):
            properties=lead_sheet['properties']
            # Sort complete rows only after all index-based writes, including CRM and extra team columns.
            sorts.append({'sortRange':{'range':{'sheetId':properties['sheetId'],'startRowIndex':1,
                'endRowIndex':len(leads)+len(new_leads),'startColumnIndex':0,
                'endColumnIndex':properties.get('gridProperties',{}).get('columnCount',17)},
                'sortSpecs':[{'dimensionIndex':5,'sortOrder':'DESCENDING'}]}})
        if sorts:
            self.api('POST',':batchUpdate',json={'requests':sorts})
        if publication_sort:store.set_state('publication_recent_first_v1','done')
        if lead_sheet:store.set_state('lead_recent_first_v1','1')
        # Google has applied any row growth by this point.
        for name,end in [('Лиды',len(leads)+len(new_leads)),('Скрипты',len(scripts)+len(new_scripts))]:
            sh=tabs.get(name)
            if sh and end>sheet_rows(sh):
                sh['properties'].setdefault('gridProperties',{})['rowCount']=end
        self.align_script_order(meta)
        store.set_state("last_synced", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return len(new_leads)
