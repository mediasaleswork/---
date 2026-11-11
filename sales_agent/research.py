from __future__ import annotations

import json
import re
from .sources import fetch, link_host
from bs4 import BeautifulSoup

SERVICES = {"PR", "Telegram и СМИ", "Digital", "SMM и контент", "Мероприятия", "Комплексное продвижение"}
KINDS = {"запуск", "открытие", "расширение", "мероприятие", "партнёрство", "инвестиции", "ребрендинг", "лицензирование", "рекламное размещение"}
KNOWLEDGE = """Mediasales / Media Sales Limited: PR- и digital-коммуникации в Узбекистане.
Подтверждённые направления: PR-стратегии, размещения в Telegram и СМИ, работа с блогерами,
SMM и контент, digital-реклама, спецпроекты, мероприятия, медиабаинг и аналитика.
Кейсы из предоставленного пользователем контекста: UZCARD, UzAuto Motors, Kapitalbank, AKFA.
Не обещать конкретные охваты, цены, ROI, наличие наружного инвентаря или гарантированные продажи.
Менеджер выясняет задачу, аудиторию, географию, сроки и бюджет, затем передаёт сделку аккаунт-менеджеру.
Стиль: разговор на равных, конкретный повод, вопросы вместо давления. Не писать всем подряд.
Клиентской CRM пока нет: все компании требуют сверки до обращения. Не выдавать контакт за проверенный.
"""


class Deferred(Exception):
    pass


def sponsored_article(source):
    from .rules import is_advertisement
    import requests
    if source['origin'].casefold()!='telegram @kptluz' or not is_advertisement(source['text']):
        return None
    url=next((u for u in json.loads(source['links']) if link_host(u) in {'kapital.uz','www.kapital.uz'}),None)
    if not url:return None
    try:
        page=BeautifulSoup(fetch(url),'html.parser')
    except (requests.RequestException,OSError,ValueError) as exc:
        raise Deferred("Статья издателя временно недоступна") from exc
    for paragraph in page.select('.article-content p')[:3]:
        body=paragraph.get_text(' ',strip=True)
        for anchor in paragraph.select('a[href]'):
            company=anchor.get_text(' ',strip=True)
            host=link_host(anchor['href']).removeprefix('www.')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 -]{2,60}',company):continue
            if re.sub('[ -]','',company.casefold())!=host.split('.')[0].replace('-',''):continue
            if not re.match(re.escape(company)+r'\s+(?:предлагает|запускает|открывает|представляет)\b',body,re.I):continue
            return dict(source,text=body,article_url=url,article_company=company,
                        links=json.dumps([anchor['href'],url]))
    return None


def validate(candidate, sources, now):
    if not isinstance(candidate, dict):
        return None
    sid = candidate.get("source_id")
    source = sources.get(sid)
    required = ("company", "evidence_quote", "sector", "event_kind", "service", "hypothesis", "priority", "rationale", "confidence")
    if not source or not link_host(source['url']) or any(not isinstance(candidate.get(k), str) or not candidate[k].strip() for k in required):
        return None
    company, quote = candidate["company"].strip(), candidate["evidence_quote"].strip()
    norm = lambda s: re.sub(r"\s+", " ", s).strip().casefold()
    if not 2 <= len(company) <= 100 or len(quote)>280 or (len(quote)<50 and norm(quote)!=norm(source['text'])):
        return None
    if re.search(r"^(?:IT Park|Центр технологий будущего|Национальное агентство|Министерств|Правительств)", company, re.I):
        return None
    if norm(company) not in norm(quote) or norm(quote) not in norm(source["text"][:5000]):
        return None
    if candidate["service"] not in SERVICES or candidate["event_kind"] not in KINDS:
        return None
    if candidate["priority"] not in {"A", "B", "C"} or candidate["confidence"] not in {"высокая", "средняя"}:
        return None
    # Extracted facts remain separate from rule-based hypotheses and contact routes.
    lead = {k: candidate[k].strip()[:1200] for k in required}
    lead['hypothesis'] = 'Гипотеза: ' + lead['hypothesis'].removeprefix('Гипотеза: ')
    if re.search(r'лицензи',quote,re.I) and not re.search(r'откр|запуск|запуст',quote,re.I):
        lead['priority']='C'
    lead.update(company=company, evidence_quote=quote, source_url=source["url"],
        published=source["published"], checked=now.isoformat(),
        contact="Найти официальный сайт «" + company + "» → контакты отдела маркетинга; контакт ещё не проверен")
    # An actual brand-domain link found in the source is a route to check, not a verified employee.
    name_key = re.sub(r"[^a-z0-9]", "", company.casefold())
    for url in json.loads(source.get("links") or "[]"):
        host = link_host(url)
        if len(name_key) >= 4 and name_key in {label.replace('-','') for label in host.split('.')}:
            lead["contact"] = url + " — ссылка из источника; проверить раздел контактов и получателя"
            break
    lead["scripts"] = scripts(lead)
    return lead


def enrich_contact(lead, source):
    """Follow a cited business article, never a model-invented URL or private source."""
    name = re.sub(r'[^a-z0-9]', '', lead['company'].casefold())
    if len(name) < 4:
        return lead
    urls = [source['url'], *json.loads(source.get('links') or '[]')]
    for url in urls[:4]:
        host=link_host(url).removeprefix('www.')
        if host not in {'spot.uz','gazeta.uz','uzdaily.uz','kun.uz'}:
            continue
        try:
            page=BeautifulSoup(fetch(url),'html.parser')
            for anchor in page.select('a[href]'):
                target=anchor.get('href','')
                domain=link_host(target)
                words={re.sub(r'[^a-z0-9]','',w) for w in lead['company'].casefold().split() if len(w)>=4}
                labels={p.replace('-','') for p in domain.split('.')}
                if target.startswith('https://') and (name in labels or words.intersection(labels) or name==domain.replace('.','').replace('-','')):
                    lead['contact']=target+' — сайт по ссылке в публикации; уточнить контакт маркетинга'
                    return lead
        except Exception:
            pass  # Contact absence never changes the independently sourced event fact.
    return lead


def scripts(lead):
    name, service, quote = lead["company"], lead["service"], lead["evidence_quote"]
    questions={
        'рекламное размещение':'Планируете дополнительные рекламные размещения для этой аудитории?',
        'запуск':'Планируете отдельно знакомить аудиторию с новым предложением?',
        'открытие':'Есть ли сейчас задача привлечь внимание аудитории к новой точке?',
        'расширение':'Планируете коммуникационную поддержку на новом рынке?',
        'мероприятие':'Кого хотите привлечь на мероприятие и нужна ли дополнительная информационная поддержка?',
        'партнёрство':'Планируете дополнительно рассказывать аудитории о партнёрстве?',
        'инвестиции':'Есть ли задача рассказать рынку о следующем этапе развития компании?',
        'ребрендинг':'Нужна ли поддержка знакомства аудитории с обновлённым брендом?',
    }
    phrases={'PR':'PR-поддержке','Telegram и СМИ':'размещениям в Telegram и СМИ','Digital':'digital-продвижению',
        'SMM и контент':'SMM и контенту','Мероприятия':'продвижению мероприятий','Комплексное продвижение':'комплексному продвижению'}
    first = (f"Здравствуйте! Представляю Mediasales. Увидел новость о {name}: «{quote}». "
        f"{questions.get(lead['event_kind'],'Есть ли сейчас задача дополнительного продвижения?')} "
        f"Если актуально, могу предложить короткую идею по {phrases[service]} в Узбекистане. С кем удобно обсудить?")
    return {
        "first":first,
        "followup":"Здравствуйте! Возвращаюсь к сообщению о продвижении. Это сейчас актуальная задача или лучше вернуться позднее? Если актуально, могу подготовить короткую идею под вашу аудиторию.",
        "call":f"1. Удобно две минуты? Представляю Mediasales. Повод — новость о {name}. "
            "2. Какую задачу хотите решить: узнаваемость, запуск или обращения? "
            "3. Кто аудитория и какая география? 4. Какие сроки и ориентир по бюджету? "
            f"5. Если есть совпадение, предложить обсудить {service.lower()}. "
            "6. Согласовать короткий бриф/встречу и следующий шаг; не обещать результат без расчёта.",
        "objections":"Уже есть агентство → Понимаю. Есть ли отдельные задачи или площадки, по которым полезен второй вариант? "
            "Нет бюджета → Когда планируется следующий цикл и стоит ли вернуться к этой дате? "
            "Пришлите КП → Уточню аудиторию, географию, сроки и задачу, чтобы не прислать общий прайс. "
            "Дорого → С каким вариантом сравниваете и какие критерии важнее? Проверим подходящий объём, без обещания скидки.",
    }
