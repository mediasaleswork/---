import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from partner_monitor.capture import Screenshotter, _validate


def widget(username="telegram", message_id=83, missing_footer=False):
    footer = "" if missing_footer else f'''<div class="tgme_widget_message_footer"><span class="tgme_widget_message_views">123</span>
      <a class="tgme_widget_message_date" href="https://t.me/{username}/{message_id}">12:30</a></div>'''
    return f'''<!doctype html><html><head><style>
    body {{ margin:0; }} [data-post] {{padding:18px;width:450px;background:#eef;}}
    .text {{height:1500px;}} .tgme_widget_message_footer {{height:30px;}}
    </style></head><body><div data-post="{username}/{message_id}">
    <div class="tgme_widget_message_author">Telegram</div>
    <div class="text">Real DOM fixture with a long publication</div>{footer}</div></body></html>'''


class ValidationTests(unittest.TestCase):
    def test_rejects_host_injection_and_invalid_ids(self):
        for user in ("https://evil.example", "test/../evil", "telegram\"", "ab"):
            with self.assertRaises(ValueError):
                _validate(user, [83])
        for ids in ([], [0], [-1], [True], ["83"], [2**31]):
            with self.assertRaises(ValueError):
                _validate("telegram", ids)

    def test_deduplicates_message_ids(self):
        self.assertEqual(_validate("telegram", [83, 84, 83]), [83, 84])


@unittest.skipUnless(shutil.which("chromium") or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), "Chromium unavailable")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Fixtures are fulfilled locally and never require outbound access.
        self.env = patch.dict(os.environ, {"HTTP_PROXY": "", "HTTPS_PROXY": ""})
        self.env.start()
        self.capture = Screenshotter(Path(self.tmp.name), timeout_ms=3000)
        await self.capture.__aenter__()

    async def asyncTearDown(self):
        await self.capture.__aexit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    async def route(self, html):
        await self.capture._context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))

    async def test_long_post_screenshot_contains_full_footer(self):
        from PIL import Image
        await self.route(widget())
        result = await self.capture.capture("telegram", [83], "../../outside")
        self.assertEqual(result.status, "ok", result.error)
        path = Path(result.path)
        self.assertEqual(path.parent, Path(self.tmp.name))
        with Image.open(path) as picture:
            self.assertGreater(picture.height, 1500)

    async def test_wrong_message_is_not_screenshotted(self):
        await self.route(widget(message_id=84))
        result = await self.capture.capture("telegram", [83], "wrong")
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(list(Path(self.tmp.name).glob("*.png")))

    async def test_missing_views_footer_fails_closed(self):
        await self.route(widget(missing_footer=True))
        result = await self.capture.capture("telegram", [83], "incomplete")
        self.assertEqual(result.status, "unavailable")

    async def test_album_combines_real_widget_screenshots(self):
        from PIL import Image
        async def respond(route):
            message_id = int(route.request.url.split("/")[-1].split("?")[0])
            await route.fulfill(status=200, content_type="text/html", body=widget(message_id=message_id))
        await self.capture._context.route("**/*", respond)
        result = await self.capture.capture("telegram", [83, 84], "album")
        self.assertEqual(result.status, "ok", result.error)
        with Image.open(result.path) as picture:
            self.assertGreater(picture.height, 3000)


if __name__ == "__main__":
    unittest.main()
