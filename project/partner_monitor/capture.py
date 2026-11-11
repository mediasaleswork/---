"""Capture real Telegram public widgets, never a generated imitation of a post.

An album is saved as a vertical strip of the actual widgets for its message IDs.
Telegram sometimes repeats the whole album in each widget. No authenticated
browser is needed; posts without a working public widget are reported as failed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright


@dataclass(frozen=True)
class CaptureResult:
    path: str | None
    status: str
    error: str | None = None


class UnavailableWidget(Exception):
    """The exact public message cannot be safely identified on the page."""


class UnsupportedContent(UnavailableWidget):
    """The public widget has a valid permalink but does not show the publication."""


def _validate(username: str, message_ids: list[int]) -> list[int]:
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValueError("Invalid public Telegram username")
    if not message_ids or len(message_ids) > 20:
        raise ValueError("Expected between 1 and 20 message IDs")
    if any(type(i) is not int or i <= 0 or i > 2**31 - 1 for i in message_ids):
        raise ValueError("Message IDs must be positive Telegram message numbers")
    return list(dict.fromkeys(message_ids))


def _proxy_config() -> dict[str, str] | None:
    value = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    server = f"{parsed.scheme}://{host}"
    if parsed.port:
        server += f":{parsed.port}"
    result = {"server": server}
    if parsed.username:
        from urllib.parse import unquote
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


class Screenshotter:
    def __init__(self, output_dir: Path, *, timeout_ms: int = 30000):
        self.output_dir = Path(output_dir)
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._slots = asyncio.Semaphore(2)

    async def __aenter__(self):
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = await async_playwright().start()
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        if not executable:
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
        options = {"headless": True, "args": ["--disable-dev-shm-usage"]}
        if executable:
            options["executable_path"] = executable
        proxy = _proxy_config()
        if proxy:
            options["proxy"] = proxy
        try:
            self._browser = await self._playwright.chromium.launch(**options)
            self._context = await self._browser.new_context(
                viewport={"width": 760, "height": 1000}, device_scale_factor=1,
                locale="ru-RU", timezone_id="Asia/Tashkent",
            )
            self._context.set_default_timeout(self.timeout_ms)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()
            self._context = self._browser = self._playwright = None

    async def _capture_widget(self, page, username: str, message_id: int, path: Path):
        url = f"https://t.me/{username}/{message_id}?embed=1&mode=tme"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if response is None or response.status != 200:
            raise UnavailableWidget("Public widget returned an unsuccessful response")
        # Never fall back to taking a screenshot of the page: a deleted post can
        # redirect to a channel landing page and would become misleading evidence.
        post = page.locator(f'[data-post="{username}/{message_id}" i]')
        if await post.count() != 1:
            raise UnavailableWidget("Exact message is absent from public widget")
        if await post.locator(".tgme_widget_message_error").count():
            raise UnavailableWidget("Telegram does not permit this public widget")
        for selector in (".tgme_widget_message_author", ".tgme_widget_message_footer", ".tgme_widget_message_date", ".tgme_widget_message_views"):
            item = post.locator(selector).first
            if not await item.count():
                raise UnavailableWidget("Message header, timestamp or views are missing")
            await item.wait_for(state="visible", timeout=self.timeout_ms)
        date_link = await post.locator("a.tgme_widget_message_date").first.get_attribute("href")
        actual = urlsplit(date_link or "")
        if actual.hostname != "t.me" or actual.path.rstrip("/").casefold() != f"/{username}/{message_id}".casefold():
            raise UnavailableWidget("Message timestamp links to a different post")
        if await post.locator(".message_media_not_supported:visible, .message_media_view_in_telegram:visible").count():
            raise UnsupportedContent("Telegram заменил содержимое поста кнопкой открытия в приложении")
        content = post.locator(".tgme_widget_message_text:visible, .tgme_widget_message_photo_wrap:visible, "
                               ".tgme_widget_message_video_player:visible, .tgme_widget_message_document:visible, "
                               ".tgme_widget_message_poll:visible, .tgme_widget_message_voice_player:visible, "
                               ".tgme_widget_message_sticker:visible, .tgme_widget_message_link_preview:visible")
        if not await content.count():
            raise UnsupportedContent("В веб-превью Telegram отсутствуют текст и вложения публикации")
        await post.scroll_into_view_if_needed()
        # Load actual images and CSS backgrounds before capturing; wait remains
        # bounded when Telegram serves slow media. Videos retain their poster.
        await post.evaluate("""async element => {
          const jobs = [...element.querySelectorAll('img')].map(img => {
            img.loading = 'eager';
            if (img.complete) return Promise.resolve();
            return new Promise(resolve => { img.addEventListener('load', resolve, {once:true}); img.addEventListener('error', resolve, {once:true}); });
          });
          for (const el of element.querySelectorAll('*')) {
            const bg = getComputedStyle(el).backgroundImage;
            for (const match of bg.matchAll(/url\\(["']?(.*?)["']?\\)/g)) {
              jobs.push(new Promise(resolve => {const img = new Image(); img.onload = img.onerror = resolve; img.src = match[1];}));
            }
          }
          await Promise.race([Promise.all(jobs), new Promise(r => setTimeout(r, 5000))]);
        }""")
        box = await post.bounding_box()
        if not box or box["height"] < 40 or box["height"] > 30000:
            raise UnavailableWidget("Public widget has an unsupported size")
        await post.screenshot(path=str(path), animations="disabled", timeout=self.timeout_ms)

    async def capture(self, username: str, message_ids: list[int], key: str) -> CaptureResult:
        try:
            ids = _validate(username, message_ids)
        except (ValueError, TypeError):
            return CaptureResult(None, "invalid", "Invalid public channel or message IDs")
        if self._context is None:
            return CaptureResult(None, "failed", "Screenshotter is not started")
        digest = hashlib.sha256(str(key).encode()).hexdigest()[:24]
        name = f"{username.lower()}_{ids[0]}_{digest}.png"
        final = self.output_dir / name
        page = None
        await self._slots.acquire()
        try:
            # A total deadline also bounds multi-widget albums and stalled media.
            async with asyncio.timeout((self.timeout_ms / 1000 + 7) * len(ids)):
                with tempfile.TemporaryDirectory(prefix="capture-", dir=self.output_dir) as scratch:
                    paths = []
                    page = await self._context.new_page()
                    for index, message_id in enumerate(ids):
                        part = Path(scratch) / f"{index}.png"
                        await self._capture_widget(page, username, message_id, part)
                        paths.append(part)
                    combined = Path(scratch) / "complete.png"
                    if len(paths) == 1:
                        paths[0].replace(combined)
                    else:
                        pictures = []
                        try:
                            pictures = [Image.open(p).convert("RGB") for p in paths]
                            height = sum(im.height for im in pictures)
                            if height > 60000:
                                raise UnavailableWidget("Combined album screenshot is too tall")
                            strip = Image.new("RGB", (max(im.width for im in pictures), height), "white")
                            offset = 0
                            for picture in pictures:
                                strip.paste(picture, (0, offset))
                                offset += picture.height
                            strip.save(combined)
                            strip.close()
                        finally:
                            for picture in pictures:
                                picture.close()
                    os.replace(combined, final)
            return CaptureResult(str(final.resolve()), "ok")
        except asyncio.CancelledError:
            raise
        except UnsupportedContent as exc:
            return CaptureResult(None, "unsupported_content", str(exc))
        except UnavailableWidget as exc:
            return CaptureResult(None, "unavailable", str(exc))
        except Exception as exc:
            # Browser error strings can contain proxy URLs or credentials. Only
            # expose the class, not raw exception text, in persisted reports.
            return CaptureResult(None, "failed", f"Capture failed ({type(exc).__name__})")
        finally:
            try:
                if page:
                    try:await page.close()
                    except Exception:pass  # A closed browser must not discard an already saved PNG.
            finally:
                self._slots.release()
