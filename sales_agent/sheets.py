from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

from google.auth import load_credentials_from_file
from google.auth.transport.requests import AuthorizedSession

LEAD_HEADERS = ["ID", "Компания", "Ниша", "Приоритет", "Подтверждённый факт", "Дата сигнала", "Услуга", "Гипотеза потребности", "Источник", "Контактный маршрут", "CRM-проверка", "Статус", "Следующий шаг", "Дата следующего контакта", "Заметки команды", "Проверено UTC", "Уверенность"]
SCRIPT_HEADERS = ["ID", "Компания", "Первое сообщение", "Повторное касание", "Сценарий звонка", "Ответы на возражения"]
HEALTH_HEADERS = ["Источник", "Проверено UTC", "Статус", "Результат"]
RUN_HEADERS = ["Начало UTC", "Завершение UTC", "Статус", "Публикаций в периоде", "Новых лидов", "Результат"]


def datevalue(value):
    if not value:
        return ""
    return (datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None) - datetime(1899,12,30)).total_seconds()/86400


def lead_row(lead):
    return [lead["id"],lead["company"],lead["sector"],lead["priority"],lead["evidence_quote"],
        datevalue(lead["published"]),lead["service"],lead["hypothesis"],lead["source_url"],lead["contact"],
        "Не сверено", "Новый", "Сверить с клиентской базой и найти контакт маркетинга", "", "", datevalue(lead["checked"]), lead["confidence"]]


class Sheets:
    def __init__(self, cfg):
        if not cfg.spreadsheet_id:
            raise ValueError("Set spreadsheet_id")
        creds, _ = load_credentials_from_file(str(cfg.credentials_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.session = AuthorizedSession(creds)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + cfg.spreadsheet_id

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

    def sync(self, store):
        meta = self.verify() or {}
        writes = []
        leads = self.read("Лиды", "A1:Q5000")
        scripts = self.read("Скрипты", "A1:F5000")
        # Key by stable ID, never by row order. Existing rows and manual edits stay intact.
        existing = {str(r[0]) for r in leads[1:] if r}
        existing_scripts = {str(r[0]) for r in scripts[1:] if r}
        new_leads = [lead_row(l) for l in store.leads() if l["id"] not in existing]
        new_scripts = [[l["id"],l["company"],*(l["scripts"][k] for k in ("first","followup","call","objections"))]
            for l in store.leads() if l["id"] not in existing_scripts]
        if len(leads) + len(new_leads) >= 5000 or len(scripts) + len(new_scripts) >= 5000:
            raise ValueError("Workbook capacity reached; extend bounded sync ranges")
        if new_leads:
            writes.append({"range":f"'Лиды'!A{len(leads)+1}:Q{len(leads)+len(new_leads)}", "values":new_leads})
        if new_scripts:
            writes.append({"range":f"'Скрипты'!A{len(scripts)+1}:F{len(scripts)+len(new_scripts)}", "values":new_scripts})
        structure = []
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
                for column,pattern in [(5,'yyyy-mm-dd'),(15,'yyyy-mm-dd hh:mm')]:
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
        store.set_state("last_synced", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        return len(new_leads)
