"""Private evidence is labelled, escapes source text, and never embeds web URLs."""
import unittest
from partner_monitor.private_card import BoundedMedia, card_html
from test_engine import NOW, message


class PrivateCardTests(unittest.TestCase):
    def test_untrusted_text_is_plain_text_and_measurement_is_explicit(self):
        markup = card_html('Channel <script>leak()</script>', [message(7, '<img src="https://external.invalid/pixel">')], {},
                           {"views": 321, "checked_at": NOW.isoformat()}, "https://t.me/c/200/7", "Asia/Tashkent")
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img src="https:', markup)
        self.assertIn('&lt;img', markup)
        self.assertIn('Просмотры в замере: 321', markup)
        self.assertIn('КАРТОЧКА ЗАКРЫТОГО ПОСТА', markup)
        self.assertIn('Это не снимок интерфейса Telegram', markup)

    def test_oversized_media_is_rejected_without_unbounded_allocation(self):
        with BoundedMedia() as buffer:
            with self.assertRaises(ValueError):
                buffer.write(b'x' * (10 * 1024 * 1024 + 1))
