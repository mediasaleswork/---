"""Durable review register and append-only history for uncertain publications."""
from datetime import datetime,timedelta,timezone
import json

OPEN_STATES=frozenset({'local_checked','offline_review','review','unavailable','archived_no_match','error'})
FINAL_STATES=frozenset({'match','no_match','obsolete'})


def next_review_day(now=None):
    now=now or datetime.now(timezone.utc)
    return (now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()


def observe(store,job_id,status,reason,value=None):
    from .storage import utcnow
    stamp=utcnow();decision=json.dumps(value,ensure_ascii=False,sort_keys=True) if value else ''
    exists=store.rows('SELECT job_id FROM campaign_reviews WHERE job_id=?',(job_id,))
    if not exists:
        if status not in OPEN_STATES:return
        job=store.rows('SELECT candidate_snapshot FROM campaign_checks WHERE id=?',(job_id,))
        if not job:return
        store.execute('INSERT OR IGNORE INTO campaign_reviews(job_id,opened_at,opened_reason,original_snapshot) VALUES (?,?,?,?)',
                      (job_id,stamp,reason[:1500],job[0]['candidate_snapshot']))
    previous=store.rows('SELECT status,reason,decision FROM campaign_review_events WHERE job_id=? ORDER BY id DESC LIMIT 1',(job_id,))
    if previous and (previous[0]['status'],previous[0]['reason'],previous[0]['decision'])==(status,reason[:1500],decision):return
    store.execute('INSERT INTO campaign_review_events(job_id,recorded_at,status,reason,decision) VALUES (?,?,?,?,?)',
                  (job_id,stamp,status,reason[:1500],decision))


def reconcile(store):
    # Direct status updates (transport retry, individual second pass) must also
    # remain visible after the original uncertain result changes.
    for job in store.rows("""SELECT j.id,j.status,j.reason,j.decision FROM campaign_checks j
      LEFT JOIN campaign_reviews r ON r.job_id=j.id
      WHERE j.status IN ('local_checked','offline_review','review','unavailable','archived_no_match','error') OR r.job_id IS NOT NULL"""):
        try:value=json.loads(job['decision']) if job['decision'] else None
        except (ValueError,TypeError):value=None
        observe(store,job['id'],job['status'],job['reason'],value)


def rows_for_partner(store,partner_id):
    rows=store.rows("""SELECT j.id,j.message_ids,j.status,j.reason,j.checked_at,j.next_try_at,j.candidate_snapshot,j.decision,
       r.opened_at,r.opened_reason,r.original_snapshot FROM campaign_reviews r JOIN campaign_checks j ON j.id=r.job_id
       WHERE j.partner_id=? ORDER BY CASE WHEN j.status IN ('match','no_match','obsolete') THEN 1 ELSE 0 END,r.opened_at DESC""",(partner_id,))
    for row in rows:
        row['events']=store.rows('SELECT recorded_at,status,reason FROM campaign_review_events WHERE job_id=? ORDER BY id DESC LIMIT 8',(row['id'],))
    return rows


def export_sheet(sheet,partners,timezone_name):
    from zoneinfo import ZoneInfo
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment,Font,PatternFill
    from openpyxl.worksheet.table import Table,TableStyleInfo
    from .report_view import local
    from .reports import _write_table
    from datetime import datetime
    tz=ZoneInfo(timezone_name)
    def explanation(reason):
        return {'http_503':'Gemini временно недоступен; запланирован повтор',
                'http_429':'Достигнут лимит Gemini; ожидается восстановление доступа',
                'request_timeout':'Gemini не ответил вовремя; запланирован повтор',
                'network_unavailable':'Временная ошибка связи; запланирован повтор',
                'media_unavailable':'Не удалось получить изображение; нужна повторная сверка',
                'missing_api_key':'Ключ ИИ недоступен; работает локальная проверка',
                'telegram_retry':'Telegram временно не ответил; запланирован повтор',
                'individual_review':'Выполняется дополнительная проверка неоднозначного результата'}.get(reason,reason)
    labels={'local_checked':'Нужна ручная проверка','offline_review':'Нужна ручная проверка','review':'Нужна ручная проверка',
            'pending':'Ожидает автопроверки','error':'Повтор после ошибки','unavailable':'Пост недоступен',
            'archived_no_match':'Архивные данные неполны','match':'Реклама подтверждена','no_match':'Не относится к рекламе','obsolete':'Версия заменена'}
    headers=['Состояние','Площадки','Ссылка на пост','Сохранённый текст','Причина сомнения','Опубликован','Добавлено на проверку',
             'Последняя проверка','Последний результат','Следующая попытка','Последние проверки','ID проверки']
    values=[headers];metadata=[]
    for partner in partners:
        for review in partner.get('manual_reviews',[]):
            try:snapshot=json.loads(review['candidate_snapshot'] or review['original_snapshot'] or '{}')
            except (ValueError,TypeError):snapshot={}
            ids=json.loads(review['message_ids']);mid=min(ids)
            url=f"https://t.me/{partner['username']}/{mid}" if partner.get('username') else f"https://t.me/c/{partner.get('private_channel_id') or partner.get('channel_id')}/{mid}"
            status=review['status']
            next_try='' if status in FINAL_STATES else (
                'После восстановления Gemini' if status in {'local_checked','offline_review'} else local(review['next_try_at'],tz))
            events='\n'.join(local(e['recorded_at'],tz)[:19].replace('T',' ')+' — '+labels.get(e['status'],e['status'])+': '+explanation(e['reason'])[:160] for e in reversed(review['events']))
            values.append([labels.get(status,status),partner.get('title') or partner.get('username') or 'Закрытый канал',url,
                           snapshot.get('text','')[:3000],explanation(review['opened_reason']),local(snapshot.get('published_at'),tz),local(review['opened_at'],tz),
                           local(review['checked_at'],tz),explanation(review['reason']),next_try,events,review['id']])
            metadata.append(snapshot)
    _write_table(sheet,values)
    for row in sheet.iter_rows():
        for cell in row:
            cell.font=Font(name='Arial',size=10,color='26272B',bold=cell.row==1)
            cell.alignment=Alignment(vertical='top',wrap_text=True)
        sheet.row_dimensions[row[0].row].height=38 if row[0].row==1 else 86
    for cell in sheet[1]:cell.fill=PatternFill('solid',fgColor='F1F2F4')
    for row,snapshot in enumerate(metadata,2):
        for col in (6,7,8,10):
            value=sheet.cell(row,col).value
            if value and str(value)[:4].isdigit():
                sheet.cell(row,col).value=datetime.fromisoformat(value)
                sheet.cell(row,col).number_format='yyyy-mm-dd hh:mm'
        if snapshot.get('text'):sheet.cell(row,4).comment=Comment(snapshot['text'][:16000],'Partner Monitor')
        if sheet.cell(row,1).value=='Нужна ручная проверка':sheet.cell(row,1).fill=PatternFill('solid',fgColor='FFF1CC')
    for col,width in {'A':25,'B':24,'C':28,'D':64,'E':44,'F':21,'G':21,'H':21,'I':46,'J':28,'K':58,'L':12}.items():sheet.column_dimensions[col].width=width
    sheet.column_dimensions['L'].hidden=True
    sheet.freeze_panes='A2';sheet.print_title_rows='1:1'
    if len(values)>1:
        table=Table(displayName='ManualReview',ref=sheet.dimensions)
        table.tableStyleInfo=TableStyleInfo(name='TableStyleLight1',showRowStripes=True);sheet.add_table(table)
