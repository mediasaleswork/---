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
      LEFT JOIN campaign_review_events e ON e.id=(
        SELECT max(id) FROM campaign_review_events WHERE job_id=j.id)
      WHERE (j.status IN ('local_checked','offline_review','review','unavailable','archived_no_match','error') OR r.job_id IS NOT NULL)
        AND (e.id IS NULL OR e.status<>j.status OR e.reason<>substr(j.reason,1,1500)
             OR e.decision<>coalesce(j.decision,''))"""):
        try:value=json.loads(job['decision']) if job['decision'] else None
        except (ValueError,TypeError):value=None
        observe(store,job['id'],job['status'],job['reason'],value)


def rows_for_partner(store,partner_id):
    rows=store.rows("""SELECT j.id,j.message_ids,j.status,j.reason,j.checked_at,j.next_try_at,j.candidate_snapshot,j.decision,
       r.opened_at,r.opened_reason,r.original_snapshot FROM campaign_reviews r JOIN campaign_checks j ON j.id=r.job_id
       WHERE j.partner_id=? ORDER BY CASE WHEN j.status IN ('match','no_match','obsolete') THEN 1 ELSE 0 END,r.opened_at DESC""",(partner_id,))
    files={}
    for item in store.rows('''SELECT j.id,a.position,f.key,f.name,f.saved,f.drive_id,f.error FROM campaign_checks j
       JOIN candidate_parts a ON a.partner_id=j.partner_id AND a.signature=j.candidate_signature
       JOIN candidate_files f ON f.key=a.asset WHERE j.partner_id=? ORDER BY j.id,a.position''',(partner_id,)):
        files.setdefault(item['id'],[]).append(item)
    for row in rows:
        row['archive_files']=files.get(row['id'],[])
        row['events']=store.rows('SELECT recorded_at,status,reason FROM campaign_review_events WHERE job_id=? ORDER BY id DESC LIMIT 8',(row['id'],))
    return rows


def saved_snapshot(review):
    snapshot={}
    for raw in (review.get('original_snapshot'),review.get('candidate_snapshot')):
        try:value=json.loads(raw or '{}')
        except (ValueError,TypeError):continue
        if isinstance(value,dict):
            snapshot.update({key:item for key,item in value.items() if item is not None and item!=''})
    return snapshot


def original_missing(review):
    if review['status'] in {'unavailable','archived_no_match'}:return True
    try:value=json.loads(review.get('decision') or '{}')
    except (ValueError,TypeError):return False
    return isinstance(value,dict) and value.get('evidence_type') in {'archived_caption_without_visuals','observed_archive'}


def ordered_reviews(partners):
    rows=[(partner,review) for partner in partners for review in partner.get('manual_reviews',[])]
    rows.sort(key=lambda item:(item[1]['opened_at'],item[1]['id']),reverse=True)
    rows.sort(key=lambda item:2 if item[1]['status'] in FINAL_STATES else 1 if original_missing(item[1]) else 0)
    return rows


def export_sheet(sheet,partners,timezone_name):
    from zoneinfo import ZoneInfo
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment,Font,PatternFill
    from openpyxl.worksheet.table import Table,TableStyleInfo
    from openpyxl.worksheet.hyperlink import Hyperlink
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
            'archived_no_match':'Архив — оригинал не открывается','match':'Реклама подтверждена','no_match':'Не относится к рекламе','obsolete':'Версия заменена'}
    headers=['Состояние','Площадки','Открыть / архив','Сохранённый текст','Причина сомнения','Опубликован','Добавлено на проверку',
             'Последняя проверка','Последний результат','Следующая попытка','Последние проверки','ID проверки','Адрес недоступного оригинала']
    from openpyxl.utils import get_column_letter
    ordered=ordered_reviews(partners)
    max_files=max((len(r.get('archive_files',[]))-1 for _,r in ordered),default=0)
    headers+=['Сохранено при обнаружении','Полная копия текста']+[f'Вложение {i+1}' for i in range(max_files)]
    values=[headers];metadata=[]
    for partner,review in ordered:
        snapshot=saved_snapshot(review)
        ids=json.loads(review['message_ids']);mid=min(ids)
        url=f"https://t.me/{partner['username']}/{mid}" if partner.get('username') else f"https://t.me/c/{partner.get('private_channel_id') or partner.get('channel_id')}/{mid}"
        status=review['status']
        missing=original_missing(review)
        action=('Читать сохранённый текст' if snapshot.get('text') else 'Оригинал недоступен; текста нет') if missing else url
        next_try='' if status in FINAL_STATES else (
            'После восстановления Gemini' if status in {'local_checked','offline_review'} else local(review['next_try_at'],tz))
        events='\n'.join(local(e['recorded_at'],tz)[:19].replace('T',' ')+' — '+labels.get(e['status'],e['status'])+': '+explanation(e['reason'])[:160] for e in reversed(review['events']))
        values.append([labels.get(status,status),partner.get('title') or partner.get('username') or 'Закрытый канал',action,
                       snapshot.get('text','')[:16000],explanation(review['opened_reason']),local(snapshot.get('published_at'),tz),local(review['opened_at'],tz),
                       local(review['checked_at'],tz),explanation(review['reason']),next_try,events,review['id'],url if missing else ''])
        files=review.get('archive_files',[])
        binaries=[f for f in files if not f['key'].startswith('post:')]
        count=sum(bool(f['saved']) for f in binaries)
        state=(f'Вложений сохранено: {count} из {len(binaries)}' if binaries else 'Текст, без файловых вложений') if files else 'Старая запись: полной копии нет'
        values[-1].extend([state]+[('Открыть: '+f['name'] if f['drive_id'] else
            ('На сервере: ' if f['saved'] else 'Ещё не сохранено: ')+f['name']) for f in files])
        metadata.append((snapshot,url,missing,files))
    _write_table(sheet,values)
    for row in sheet.iter_rows():
        for cell in row:
            cell.font=Font(name='Arial',size=10,color='1567B0' if cell.hyperlink else '26272B',bold=cell.row==1,underline='single' if cell.hyperlink else None)
            cell.alignment=Alignment(vertical='top',wrap_text=True)
        sheet.row_dimensions[row[0].row].height=38 if row[0].row==1 else 86
    for cell in sheet[1]:cell.fill=PatternFill('solid',fgColor='F1F2F4')
    for row,(snapshot,url,missing,files) in enumerate(metadata,2):
        for col,f in enumerate(files,15):
            if f['drive_id']:
                cell=sheet.cell(row,col)
                cell.hyperlink='https://drive.google.com/open?id='+f['drive_id']
                cell.font=Font(name='Arial',size=10,color='1567B0',underline='single')
            elif f.get('error'):
                sheet.cell(row,col).comment=Comment('Повтор запланирован. '+f['error'],'Partner Monitor')
        for col in (6,7,8,10):
            value=sheet.cell(row,col).value
            if value and str(value)[:4].isdigit():
                sheet.cell(row,col).value=datetime.fromisoformat(value)
                sheet.cell(row,col).number_format='yyyy-mm-dd hh:mm'
        if snapshot.get('text'):sheet.cell(row,4).comment=Comment(snapshot['text'][:16000],'Partner Monitor')
        if missing:
            cell=sheet.cell(row,3)
            cell.comment=Comment('Telegram не возвращает исходный пост. Проверяйте сохранённый текст справа. Исходный адрес сохранён для повторных попыток: '+url,'Partner Monitor')
            if snapshot.get('text'):
                cell.hyperlink=Hyperlink(ref=cell.coordinate,location=f"'Ручная проверка'!D{row}",display=cell.value)
                cell.font=Font(name='Arial',size=10,color='1567B0',underline='single')
            else:
                first=next((f for f in files if f['drive_id'] and not f['key'].startswith('post:')),None)
                if first:
                    cell.value='Открыть сохранённое вложение'
                    cell.hyperlink='https://drive.google.com/open?id='+first['drive_id']
                    cell.comment=Comment('Оригинал недоступен. Здесь открывается сохранённый файл. Остальные вложения — справа.','Partner Monitor')
                    cell.font=Font(name='Arial',size=10,color='1567B0',underline='single')
        else:sheet.cell(row,3).comment=Comment('Ссылка на исходную публикацию. Если доступность изменилась, сохранённый текст остаётся справа.','Partner Monitor')
        if sheet.cell(row,1).value=='Нужна ручная проверка':sheet.cell(row,1).fill=PatternFill('solid',fgColor='FFF1CC')
    for col,width in {'A':25,'B':24,'C':28,'D':64,'E':44,'F':21,'G':21,'H':21,'I':46,'J':28,'K':58,'L':12,'M':40}.items():sheet.column_dimensions[col].width=width
    sheet.column_dimensions['L'].hidden=True
    for col in range(14,16+max(0,max_files)):sheet.column_dimensions[get_column_letter(col)].width=32
    sheet.freeze_panes='A2';sheet.print_title_rows='1:1'
    if len(values)>1:
        table=Table(displayName='ManualReview',ref=sheet.dimensions)
        table.tableStyleInfo=TableStyleInfo(name='TableStyleLight1',showRowStripes=True);sheet.add_table(table)
