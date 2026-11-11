"""Conservative extraction from published text. No model, key or generation API."""
from __future__ import annotations

import re

EVENTS = [
    ('открытие', re.compile(r'\b(?:откры(?:л[аи]?|ли|лась|лись|вает|вают|вается|тие)|откро(?:ет|ют|ется)|ochild\w*|ochil\w*|opened|opening)\b', re.I)),
    ('запуск', re.compile(r'\b(?:запуст\w*|запуска\w*|запуск|обновил[аи]?|обновили|обновление|ishga\s+tush\w*|launch\w*|taqdim\w*)\b', re.I)),
    ('расширение', re.compile(r'расшир\w*|выход\w*\s+на\s+рынок|нов\w*\s+филиал|kengay\w*|expan\w*', re.I)),
    ('ребрендинг', re.compile(r'ребренд\w*|rebrand\w*', re.I)),
    ('партнёрство', re.compile(r'партн[её]рств\w*|(?:генеральн\w*|стратегическ\w*)\s+партн[её]р\w*|hamkorlik\w*', re.I)),
    ('инвестиции', re.compile(r'привл[её]к\w*[^.!?]{0,70}(?:инвест|финанс)|инвестиров\w*|investment\w*|sarmoya\w*', re.I)),
    ('мероприятие', re.compile(r'(?:провод\w*|организ\w*|пройд\w*|состо\w*)[^.!?]{0,70}(?:выставк\w*|фестивал\w*|конференц\w*|форум\w*)|(?:выставк\w*|конференц\w*)[^.!?]{0,70}(?:пройд\w*|состо\w*)', re.I)),
    ('лицензирование', re.compile(r'выдал\w*[^.!?]{0,100}лицензи\w*|получил\w*[^.!?]{0,60}лицензи\w*', re.I)),
]
ORG = r'(?:компани[яию]|сервис|бренд|сеть|банк|оператор|ритейлер|девелопер|стартап|застройщик|маркетплейс|ресторан|кофейня|отель|завод[ае]?|холдинг|ООО|АО|MCHJ|AJ)'
NAME = r"[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9&'’ʼʻ\-]*(?:[ \t]+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9&'’ʼʻ\-]*){0,3}"
LATIN = r"[A-Z][A-Za-z0-9&'’ʼʻ\-]*(?:[ \t]+[A-Z][A-Za-z0-9&'’ʼʻ\-]*){0,3}"
PREFIX = re.compile(r'(?i:\b'+ORG+r')\s+(?:[«“\"](?P<quoted>[^»”\"\n]{2,80})[»”\"]|(?P<name>'+NAME+r'))')
UZBEK = re.compile(r'(?P<name>'+LATIN+r")\s+(?:kompaniyasi|banki|brendi|operatori|tarmog[‘’'ʻʼ]i)\b")
HEADING = re.compile(r'^(?:(?i:турецкая|российская|китайская|узбекская|казахстанская|корейская)\s+)?(?P<name>'+LATIN+r')\s+(?i:запуст|запуска|откры|откро|обнов|расшир|выход|представ|стал|заключ|рассматрива|launch|open|yangi|ishga)')
EXCLUDE = re.compile(r'^(?:IT Park|Центр технологий будущего|Национальн|Министерств|Правительств|Президент|Узбекистан|Ташкент|СМИ|ОАВ|OAV|Новый|Новая|Бизнес|Клиент|Реклама|Новости|Подробнее|Telegram|Instagram|Facebook|YouTube|Davlat|O‘zbekiston|Toshkent)\b', re.I)
COMPETITOR = re.compile(r'(?:рекламн\w*|маркетингов\w*|PR)[\s-]*(?:агентств\w*)', re.I)
TRAGEDY = re.compile(r'погиб|убийств|скончал|теракт|уголовн|похорон', re.I)
HYPOTHESES = {
    'рекламное размещение':'В источнике опубликован материал с рекламной пометкой. Возможно, компании нужны дополнительные площадки; бюджет, прямого заказчика и потребность необходимо уточнить.',
    'открытие':'Может быть полезно познакомить местную аудиторию с новой точкой или направлением.',
    'запуск':'Может быть актуально рассказать целевой аудитории о новом или обновлённом предложении.',
    'расширение':'Может быть полезна коммуникационная поддержка на новой территории или для новой аудитории.',
    'партнёрство':'Возможно, компании интересно дополнительно объяснить аудитории ценность партнёрства.',
    'инвестиции':'Возможно, следующему этапу развития понадобится PR-поддержка.',
    'ребрендинг':'Может быть актуально познакомить аудиторию с обновлённым брендом.',
    'мероприятие':'Возможно, организатору нужны охват и привлечение целевой аудитории.',
    'лицензирование':'Возможно, после получения лицензии компания готовит выход к клиентам; запуск и потребность ещё нужно уточнить.',
}

AD_MARKER=re.compile(r'\(\s*(?:реклама|на\s+правах\s+рекламы|reklama|advertisement)\s*\)|(?<!\w)#(?:реклама|reklama|ad)\b|(?<!\w)(?:реклама|на\s+правах\s+рекламы|reklama|advertisement)\s*[.!]?\s*$',re.I)
AD_NAME=re.compile(r"(?<!\w)[A-Z][A-Za-z0-9&'’ʼʻ\-]*(?:[ \t]+[A-Z][A-Za-z0-9&'’ʼʻ\-]*){0,5}")
AD_STOP={'IT','HR','EIOS','III','II','I','IV','V','VI','VII','VIII','IX','X','Batafsil','Reklama','Telegram','Instagram','Facebook','Sayt','The','Today',
    'KUNDAN','SO‘NG','BIRINCHI','ANIQLANADI','JAMI','SOVG‘A','Chimyonda','Dam',
    'BIR','KUN','KATTA','IMKONIYATLAR','FOYDALI','XARIDLAR','Kutgan','Markaziy','Global'}


def is_advertisement(text):
    return bool(AD_MARKER.search(re.sub(r'https?://\S+','',text)[-500:]))


def advertising_brand(text,known):
    # Explicit corporate suffixes and full university names identify the subject
    # before a rating agency or a World University Rankings title in the same ad.
    subject_patterns=[
        r'(?P<name>[“«"][^”»"\n]{2,60}[”»"]\s+(?:[a-zà-öø-ÿʻ’‘ʼ\x27]+\s+){0,7}universiteti)\b',
        r'[«“\"](?P<name>[^»”\"\n]{2,80})[»”\"]\s+(?:АК|АЖ|АО|AJ|AK|JSC|MCHJ|ООО)(?=\b|нинг\b|ning\b)',
        r'(?P<name>[A-ZÀ-ÖØ-Þ][\w’ʻʼ\x27-]*(?:\s+[a-zà-öø-ÿ][\w’ʻʼ\x27-]*){0,4}\s+universiteti)\b',
        r'(?P<name>[А-ЯЁ][а-яё]+(?:\s+[а-яё]+){0,2}\s+университет(?:\s+[а-яё]+){0,3})(?=\s+(?:получ|вош|откр|представ)|[.!?])',
    ]
    for pattern in subject_patterns:
        match=re.search(pattern,text[:1000])
        if match:return match.group('name'),match.start('name'),match.end('name')
    # An explicitly named organiser is preferable to the title of the advertised exhibition.
    organiser=re.search(r'(?i:организован\w*|организатор\w*)\s+(?P<name>'+LATIN+r')',text)
    if organiser:
        return organiser.group('name'),organiser.start('name'),organiser.end('name')
    aliases=['Yandex','Wuling','ASUS','HUMO','NBU','Click','MKBANK','ASLZAR','Kia','Uztelecom','MEDIAPARK','payme','Mastercard']
    for pattern in aliases:
        match=re.search(r'(?<!\w)'+pattern+r'(?=$|\W|(?:ga|dan|ning|ni|da)\b)',text[:1500],re.I)
        if match:return text[match.start():match.end()],match.start(),match.end()
    # In awards stories the recipient, rather than the payment network, is the prospect.
    recipient=re.search(r'(?i:отметил\w*)\s+(?P<name>'+LATIN+r')\s+(?i:наград\w*)',text)
    if recipient:return recipient.group('name'),recipient.start('name'),recipient.end('name')
    # A corporate name is stronger evidence than a named residential project or research index.
    corporate=re.search(r'(?P<name>[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,2}\s+(?:Development|Buildings|Insurance|University))\b',text[:1000])
    if corporate and not re.search(r'\b(?:World University|Rankings)\b',corporate.group('name')):
        return corporate.group('name'),corporate.start('name'),corporate.end('name')
    for name,start,end in brands(text,known):
        if name.endswith('-') or name in {'IT','SOC','SIEM'}:continue
        if name.casefold() in {v.casefold() for v in known} and name not in AD_STOP and not re.search(r'\b(?:Index|Apple Pay|World University|Rankings)\b',name):
            return name,start,end
    # Inspect the headline before names of interviewees, products and footer links.
    candidates=[]
    for match in AD_NAME.finditer(text[:240]):
        name=match.group().strip()
        if name.endswith('-') or name in {'SOC','SIEM'} or name in AD_STOP or EXCLUDE.search(name):continue
        name=re.sub(r"['’ʼʻ](?:dan|ning|ni|ga|da)\b.*$",'',name).rstrip("'’ʼʻ")
        if re.search(r'\b(?:Expo|Marathon|Forum|Olympiad|Index|Apple Pay|Medicine of the Future|World University|Rankings)\b',name,re.I):continue
        first=name.split()[0]
        if first in AD_STOP:continue
        if first.isupper() and len(first)>=3:
            name=first
        candidates.append((name,match.start(),match.start()+len(name)))
    if candidates:return candidates[0]
    return next((brand for brand in brands(text,known) if not brand[0].endswith('-') and brand[0] not in {'IT','SOC','SIEM'}),None)


def brands(text, known):
    found=[]
    for name in known:
        match=re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)', text, re.I)
        if match:
            found.append((text[match.start():match.end()],match.start(),match.end()))
    for pattern in (PREFIX,UZBEK,HEADING):
        for match in pattern.finditer(text):
            group='quoted' if 'quoted' in match.groupdict() and match.group('quoted') else 'name'
            name=match.group(group).strip()
            start,end=match.span(group)
            if pattern is PREFIX and re.search(r'(?:глава|главы|директор|руководитель|президент)\s+$',text[:match.start()],re.I):
                continue
            if not EXCLUDE.search(name) and 2 <= len(name) <= 80:
                found.append((name,start,end))
    unique={}
    for name,start,end in found:
        unique.setdefault(name.casefold(),(name,start,end))
    return list(unique.values())


def evidence(text, start, end, event):
    """Keep the brand and event together inside one literal source excerpt."""
    left=min(start,event.start()); right=max(end,event.end())
    if right-left>240:
        return None
    # Prefer a full sentence; a bounded literal excerpt is used for long Telegram paragraphs.
    before=max(text.rfind('. ',0,left),text.rfind('! ',0,left),text.rfind('? ',0,left))
    begin=before+2 if before>=0 else 0
    after=re.search(r'[.!?](?:\s|$)',text[right:])
    finish=right+after.end()-1 if after else len(text)
    if finish-begin>280:
        begin=max(0,left-30)
        if begin>0:
            space=text.find(' ',begin,left)
            if space>=0: begin=space+1
        finish=min(len(text),max(right+30,begin+220),begin+280)
        space=text.rfind(' ',right,finish)
        if finish<len(text) and space>right: finish=space
    quote=text[begin:finish].strip()
    if len(quote)<50:
        finish=min(len(text),begin+280)
        quote=text[begin:finish].strip()
    if len(text.strip()) < 50:
        quote = text.strip()
    return quote if quote and len(quote)<=280 and text[start:end] in quote else None


def sector(text):
    groups=[('Телекоммуникации',r'мобильн\w*\s+оператор|телеком|mobil\w*\s+operator'),
        ('Финансы',r'банк|кредит|крипто|плат[её]ж|вклад|финанс|bank|to[‘’\x27]lov'),
        ('Недвижимость',r'квартир|жил\w*\s+комплекс|девелоп|застрой|недвижим'),
        ('Ритейл',r'магазин|ритейл|маркетплейс|retail|do[‘’\x27]kon'),
        ('Еда и гостеприимство',r'ресторан|кофейн|отель|гостиниц|hotel|coffee'),
        ('Образование',r'университет|образован|учебн|школ|ta[‘’\x27]lim'),
        ('Производство',r'завод|производств|фабрик|электроник'),
        ('Технологии',r'приложени|стартап|программ|software|IT\b')]
    return next((name for name,pattern in groups if re.search(pattern,text,re.I)),'Нишу уточнить')


class RuleResearcher:
    def __init__(self, known=()):
        self.known=tuple(known)

    def classify(self,batch,now):
        result=[]
        for source in batch:
            text=source['text'][:5000]
            if TRAGEDY.search(source['title']) or COMPETITOR.search(text[:500]):
                continue
            article_company=source.get('article_company')
            if is_advertisement(text) or article_company:
                brand=(article_company,0,len(article_company)) if article_company and text.startswith(article_company) else advertising_brand(text,self.known)
                if brand:
                    company,start,end=brand
                    anchor=re.search(re.escape(text[start:end]),text)
                    quote=evidence(text,start,end,anchor)
                    if quote:
                        result.append(dict(source_id=source['id'],company=company,evidence_quote=quote,
                            sector=sector(text),event_kind='рекламное размещение',service='Telegram и СМИ',
                            hypothesis=HYPOTHESES['рекламное размещение'],priority='B',
                            rationale='В публикации есть рекламная пометка и конкретный бренд. Контакт и актуальность предложения проверить.',confidence='средняя'))
                        continue
            events=[(kind,m) for kind,pattern in EVENTS for m in pattern.finditer(text)]
            for company,start,end in brands(text,self.known):
                if EXCLUDE.search(company): continue
                choices=sorted(events,key=lambda pair:min(abs(pair[1].start()-end),abs(start-pair[1].end())))
                for kind,event in choices:
                    quote=evidence(text,start,end,event)
                    if not quote: continue
                    # A different named company between the brand and event is ambiguous.
                    between=text[min(end,event.end()):max(start,event.start())]
                    if any(other[0].casefold()!=company.casefold() for other in brands(between,())):
                        continue
                    service='Мероприятия' if kind=='мероприятие' else 'Telegram и СМИ' if kind in {'запуск','открытие'} else 'PR'
                    result.append(dict(source_id=source['id'],company=company,evidence_quote=quote,
                        sector=sector(quote),event_kind=kind,service=service,hypothesis=HYPOTHESES[kind]+' Предположение нужно проверить в разговоре.',
                        priority='C' if kind in {'лицензирование','партнёрство'} else 'B',
                        rationale='Правило: коммерческое название и сигнал «'+kind+'» найдены в одном фрагменте публикации; ручная проверка обязательна.',
                        confidence='средняя'))
                    break
        return result
