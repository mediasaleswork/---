"""Labelled evidence cards from authenticated channel data, not Telegram UI shots.

No AI, new login or external image URLs. Media is downloaded by the monitor's
existing MTProto client. Cards must remain visibly distinct from public widgets.
"""
import asyncio
import base64
from datetime import datetime
import hashlib
import html
import io
import os
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from .capture import CaptureResult
from .message_content import message_content,visual_sources


class BoundedMedia(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > 10 * 1024 * 1024:
            raise ValueError("private_media_too_large")
        return super().write(value)


def inline_picture(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("private_media_too_large")
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1200, 1600))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def card_html(title, messages, pictures, snapshot, url, timezone):
    def local(value):
        date = datetime.fromisoformat(value) if isinstance(value, str) else value
        return html.escape(date.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M:%S"))
    blocks = []
    for message in messages:
        picture = pictures.get(message.id)
        if picture:
            for value in ([picture] if isinstance(picture,str) else picture):
                blocks.append('<img alt="Медиа публикации" src="' + value + '">')
        text=message_content(message).text
        if text:
            blocks.append('<div class="text">' + html.escape(text) + '</div>')
        if getattr(message, "document", None):
            blocks.append('<div class="muted">Вложение Telegram; показана миниатюра, если доступна</div>')
        blocks.append('<div class="mid">Сообщение №' + str(message.id) + '</div>')
    views = snapshot.get("views")
    return '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<style>*{box-sizing:border-box}body{margin:0;padding:24px;background:#edf1f5;font-family:Arial,sans-serif;color:#182b3a}
article{width:712px;background:white;border:1px solid #d8e0e6;border-radius:12px;overflow:hidden}
header{padding:22px 24px;background:#eaf3fa;border-bottom:1px solid #d8e0e6}h1{font-size:23px;margin:9px 0 0;overflow-wrap:anywhere}
.label{font-size:13px;color:#355e7a;font-weight:bold}.content{padding:24px}.text{font-size:19px;line-height:1.48;white-space:pre-wrap;overflow-wrap:anywhere;margin:16px 0}
img{display:block;max-width:100%;max-height:1600px;height:auto;margin:auto}.mid,.muted{font-size:12px;color:#5e6e7b;margin:9px 0}
footer{padding:18px 24px;background:#f6f8fa;border-top:1px solid #d8e0e6;font-size:14px;line-height:1.65;overflow-wrap:anywhere}
</style><article><header><div class="label">КАРТОЧКА ЗАКРЫТОГО ПОСТА · ДАННЫЕ TELEGRAM</div><h1>''' + html.escape(title) + '''</h1></header><div class="content">''' + "".join(blocks) + '''</div><footer>Опубликован: ''' + local(messages[0].date) + '''<br>Просмотры в замере: ''' + (str(views) if views is not None else "не предоставлены") + '''<br>Данные зафиксированы: ''' + local(snapshot["checked_at"]) + " (" + html.escape(timezone) + ")<br>" + html.escape(url) + '''<div class="muted">Карточка собрана из данных аккаунта Telegram. Это не снимок интерфейса Telegram.</div></footer></article></html>'''


async def capture_private_card(capture, client, partner, messages, snapshot, url, key, timezone):
    if not getattr(capture, "_context", None):
        return CaptureResult(None, "failed", "Рендер карточки не запущен")
    page = None
    temporary = None
    slots=getattr(capture,'_slots',None)
    if slots:await slots.acquire()
    try:
        async with asyncio.timeout(90):
            pictures = {}
            for message in messages:
                for _,target in visual_sources(message):
                    if target is None:raise ValueError('private_photo_unavailable')
                    with BoundedMedia() as buffer:
                        await client.download_media(target, file=buffer, thumb=-1)
                        if buffer.getvalue():
                            pictures.setdefault(message.id,[]).append(await asyncio.to_thread(inline_picture, buffer.getvalue()))
                        else:
                            raise ValueError("private_photo_unavailable")
            markup = card_html(partner["title"], messages, pictures, snapshot, url, timezone)
            page = await capture._context.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(markup, wait_until="load")
            article = page.locator("article")
            if not await article.locator("img").evaluate_all("images=>images.every(i=>i.complete&&i.naturalWidth>0)"):
                raise ValueError("private_photo_unavailable")
            box = await article.bounding_box()
            if not box or box["height"] > 30000:
                raise ValueError("private_card_too_tall")
            digest = hashlib.sha256(str(key).encode()).hexdigest()[:24]
            final = capture.output_dir / f"private-card-{digest}.png"
            temporary = final.with_suffix(".tmp.png")
            await article.screenshot(path=str(temporary), animations="disabled")
            os.replace(temporary, final)
            return CaptureResult(str(final.resolve()), "ok", "Карточка закрытого поста из данных Telegram; не снимок интерфейса")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return CaptureResult(None, "failed", f"Карточка закрытого поста не получена ({type(exc).__name__})")
    finally:
        try:
            if page:
                try:await page.close()
                except Exception:pass
            if temporary:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
        finally:
            if slots:slots.release()
