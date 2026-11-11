"""Share Telegram's FloodWait deadline across polling and live listeners."""
from __future__ import annotations

import asyncio
from telethon import errors


class TelegramAccess:
    def __init__(self, client):
        self.raw = client
        self.blocked_until = 0.0
        self.request_timeout = 120

    async def wait(self):
        while (delay := self.blocked_until - asyncio.get_running_loop().time()) > 0:
            await asyncio.sleep(delay)

    async def request(self, operation, *, timeout=None):
        await self.wait()
        try:
            return await asyncio.wait_for(operation(), timeout=timeout or self.request_timeout)
        except errors.FloodWaitError as exc:
            self.blocked_until = max(self.blocked_until, asyncio.get_running_loop().time() + exc.seconds + 1)
            raise

    async def __call__(self, request):
        return await self.request(lambda: self.raw(request))

    async def get_entity(self, *args, **kwargs):
        return await self.request(lambda: self.raw.get_entity(*args, **kwargs))

    async def get_messages(self, *args, **kwargs):
        return await self.request(lambda: self.raw.get_messages(*args, **kwargs))

    async def download_media(self, *args, **kwargs):
        return await self.request(lambda: self.raw.download_media(*args, **kwargs))

    async def download_original(self, media, target, size):
        # Keep the normal request timeout for small files; large original videos need longer.
        return await self.request(lambda: self.raw.download_media(media,file=target),
                                  timeout=max(self.request_timeout,min(14400,120+size/131072)))

    async def iter_messages(self, *args, **kwargs):
        iterator = self.raw.iter_messages(*args, **kwargs).__aiter__()
        while True:
            try:
                message = await self.request(lambda: anext(iterator))
            except StopAsyncIteration:
                return
            yield message

    async def iter_dialogs(self, *args, **kwargs):
        iterator = self.raw.iter_dialogs(*args, **kwargs).__aiter__()
        while True:
            try:
                dialog = await self.request(lambda: anext(iterator))
            except StopAsyncIteration:
                return
            yield dialog
