import asyncio
from datetime import datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _DATA.name
import bot
import memegen
import usage_stats
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def photo(size=(600, 800)):
    out = BytesIO()
    Image.new("RGB", size, (90, 90, 90)).save(out, "PNG")
    return out.getvalue()


class CaptionInputTests(unittest.TestCase):
    def test_enter_and_legacy_separator(self):
        for text, expected in [
            ("верх\nниз", ("верх", "низ")),
            ("верх\r\nниз\nещё низ", ("верх", "низ ещё низ")),
            ("одна строка", ("", "одна строка")),
            ("верх|низ", ("верх", "низ")),
        ]:
            self.assertEqual(bot.parse_user_caption(text), expected)

    def test_one_font_and_explicit_positions_are_deterministic(self):
        first = memegen.make_classic_meme(photo(), "только сверху", "")
        second = memegen.make_classic_meme(photo(), "только сверху", "", font_choice=memegen.FONT_CHOICES_BY_ID["rubik"])
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertEqual(first.layout["top"], "только сверху")
        self.assertEqual(first.layout["bottom"], "")
        self.assertEqual(memegen.CUSTOM_FONT_IDS, ("oswald",))

    def test_both_kinds_offer_editor_and_reroll(self):
        for keyboard in (bot.try_again_kb("abc"), bot.submit_this_kb("abc")):
            buttons = {b.callback_data for row in keyboard.inline_keyboard for b in row}
            self.assertIn("mtext:abc", buttons)
            self.assertIn("reroll:all:abc", buttons)


class SafeCaptionTests(unittest.TestCase):
    def test_busy_photo_never_adds_bands(self):
        image = Image.new("RGB", (600, 800), "gray")
        ImageDraw.Draw(image).rectangle((40, 0, 550, 280), fill="red")
        source = BytesIO()
        image.save(source, "PNG")
        result = memegen.make_classic_meme(source.getvalue(), "лицо здесь", "снизу свободно")
        self.assertEqual(result.safety["bands"], ())
        self.assertEqual(Image.open(result).size, image.size)
        self.assertEqual(result.safety["photo_box"], (0, 0, 600, 800))
        self.assertEqual(result.layout["top"], "лицо здесь")

    def test_tiny_and_narrow_photos_are_not_padded(self):
        for size in [(1, 2000), (2000, 1), (64, 64), (180, 500)]:
            expected = Image.open(BytesIO(photo(size)))
            expected.thumbnail((memegen.MAX_SIDE, memegen.MAX_SIDE))
            result = memegen.make_classic_meme(photo(size), "Щ" * 500, "низ")
            self.assertEqual(Image.open(result).size, expected.size)
            self.assertEqual(result.safety["bands"], ())

    def test_random_and_saved_layout_render_without_bands(self):
        with patch.object(memegen.random, "random", return_value=.1):
            result = memegen.make_classic_meme(photo(), "", "обычная подпись", random_style=True)
        self.assertEqual(result.layout["top"], "обычная подпись")
        repeated = bot.render_by_spec(photo(), {"kind": "random", "layout": result.layout})
        self.assertEqual(Image.open(repeated).size, (600, 800))
        self.assertEqual(result.getvalue(), repeated.getvalue())

    def test_full_caption_stays_inside_canvas(self):
        for size in [(900, 1200), (1200, 400), (320, 160), (64, 64)]:
            for caption in ["ДОКАЗАТЕЛЬСТВО ТОГО,", "Щ" * 500, "слово " * 80]:
                result = memegen.make_classic_meme(photo(size), caption, "низ")
                image = Image.open(result).convert("RGB")
                self.assertEqual(image.size, size)
                self.assertEqual(result.safety["bands"], ())
                # JPEG-tolerant outer edge check: no white text touches an edge.
                for edge in [image.crop((0, 0, 1, image.height)),
                             image.crop((image.width-1, 0, image.width, image.height)),
                             image.crop((0, 0, image.width, 1)),
                             image.crop((0, image.height-1, image.width, image.height))]:
                    self.assertLess(max(max(pixel) for pixel in edge.getdata()), 160)
                self.assertEqual(result.layout["top"], caption)

    def test_demotivator_long_word_is_not_clipped(self):
        result = memegen.make_demotivator(photo((320, 200)), "Щ" * 500, "подпись")
        image = Image.open(result).convert("RGB")
        self.assertLess(max(max(p) for p in image.crop((0, 0, 10, image.height)).getdata()), 60)
        self.assertLess(max(max(p) for p in image.crop((image.width-10, 0, image.width, image.height)).getdata()), 60)

    def test_exif_orientation_is_applied(self):
        source = Image.new("RGB", (600, 400), "gray")
        exif = source.getexif()
        exif[274] = 6
        out = BytesIO()
        source.save(out, "JPEG", exif=exif)
        self.assertEqual(memegen._prepare_photo(out.getvalue(), 1280).size, (400, 600))


class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=2, user_id=3))
        self.sent = NS(photo=[NS(file_id="new-render")], edit_reply_markup=AsyncMock())
        self.message = NS(text="новый низ", chat=NS(id=2, type="private"), from_user=NS(id=3),
                          answer=AsyncMock(), answer_photo=AsyncMock(return_value=self.sent))
        self.telegram = NS(get_file=AsyncMock(return_value=NS(file_path="original-path")),
                           download_file=AsyncMock(return_value=BytesIO(photo())))
        self.entry = {"source_file_id": "original", "rendered_file_id": "old-render",
                      "spec": {"kind": "random", "phrase": "верх|низ",
                               "layout": {"fmt": "meme", "top": "верх", "bottom": "низ"}}}

    async def asyncTearDown(self):
        await self.storage.close()

    async def test_custom_format_skips_font_menu(self):
        callback = NS(data="custom_format:meme", answer=AsyncMock(),
                      message=NS(edit_reply_markup=AsyncMock(), answer=AsyncMock()))
        await bot.custom_format_chosen(callback, self.state)
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_custom_text.state)
        self.assertIn("Enter", callback.message.answer.call_args.args[0])

    async def test_edit_bottom_preserves_top_and_original(self):
        await bot.show_text_editor(self.message, self.state, self.entry)
        token = (await self.state.get_data())["edit_token"]
        callback = NS(data=f"mline:{token}:bottom", answer=AsyncMock(), message=self.message)
        await bot.edit_text_part(callback, self.state)
        with patch.object(bot, "persist_render", new_callable=AsyncMock) as persist, \
                patch.object(bot.ui, "bot_caption", new_callable=AsyncMock, return_value="бот"):
            await bot.edit_text_finish(self.message, self.state, self.telegram)
        entry = persist.call_args.args[2]
        self.assertEqual(entry["source_file_id"], "original")
        self.assertEqual(entry["spec"]["layout"], {"fmt": "meme", "top": "верх", "bottom": "новый низ"})
        self.assertEqual(persist.call_args.args[-1], "edit_text")
        self.telegram.get_file.assert_awaited_once_with("original")
        self.assertEqual(self.entry["spec"]["layout"]["bottom"], "низ")
        self.assertIsNone(await self.state.get_state())

    async def test_old_editor_buttons_cannot_change_current_edit(self):
        await bot.show_text_editor(self.message, self.state, self.entry)
        token = (await self.state.get_data())["edit_token"]
        await bot.show_text_editor(self.message, self.state, self.entry)
        callback = NS(data=f"mline:{token}:bottom", answer=AsyncMock(), message=self.message)
        await bot.edit_text_part(callback, self.state)
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_edit_choice.state)
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])

    async def test_old_custom_meme_requests_original_not_rendered_photo(self):
        old = {"rendered_file_id": "with-text", "spec": {"kind": "custom", "text": "old", "fmt": "meme"}}
        callback = NS(data="mtext:old", answer=AsyncMock(), message=self.message)
        with patch.object(bot, "find_render", new_callable=AsyncMock, return_value=old):
            await bot.edit_text_start(callback, self.state)
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_edit_photo.state)
        self.assertIn("без надписи", self.message.answer.call_args.args[0])

    async def test_invalid_edit_never_renders_or_counts(self):
        await self.state.set_data({"edit_entry": self.entry, "edit_layout": self.entry["spec"]["layout"], "edit_part": "bottom"})
        await self.state.set_state(bot.MemeStates.waiting_edit_text)
        self.message.text = "я" * 501
        with patch.object(bot.usage_stats, "record") as record:
            await bot.edit_text_finish(self.message, self.state, self.telegram)
        self.message.answer_photo.assert_not_awaited()
        record.assert_not_called()
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_edit_text.state)

    async def test_reuploaded_original_repairs_old_render_button(self):
        await self.state.set_data({"edit_entry": self.entry, "edit_render_id": "old-id"})
        self.message.photo = [NS(file_id="reuploaded-original")]
        with patch.object(bot.render_store, "save") as save:
            await bot.edit_text_source(self.message, self.state)
        self.assertEqual(save.call_args.args[:3], ("old-id", 2, 3))
        self.assertEqual(save.call_args.args[3]["source_file_id"], "reuploaded-original")
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_edit_choice.state)

    async def test_failed_send_keeps_editor_and_does_not_count(self):
        await self.state.set_data({"edit_entry": self.entry, "edit_layout": self.entry["spec"]["layout"], "edit_part": "bottom"})
        await self.state.set_state(bot.MemeStates.waiting_edit_text)
        self.message.answer_photo.side_effect = RuntimeError("Telegram unavailable")
        with patch.object(bot.ui, "bot_caption", new_callable=AsyncMock, return_value="бот"), \
                patch.object(usage_stats, "record") as record, self.assertLogs("bot", level="ERROR"):
            await bot.edit_text_finish(self.message, self.state, self.telegram)
        self.assertEqual(await self.state.get_state(), bot.MemeStates.waiting_edit_text.state)
        record.assert_not_called()

    async def test_custom_meme_keeps_source_and_enter_layout(self):
        await self.state.set_data({"custom_photo_file_id": "original", "custom_format": "meme"})
        self.message.text = "верх\nниз"
        with patch.object(bot, "persist_render", new_callable=AsyncMock) as persist, \
                patch.object(bot, "offer_custom_text_as_phrase", new_callable=AsyncMock) as offer, \
                patch.object(bot.ui, "bot_caption", new_callable=AsyncMock, return_value="бот"):
            await bot.custom_meme_got_text(self.message, self.state, self.telegram)
        entry = persist.call_args.args[2]
        self.assertEqual(entry["source_file_id"], "original")
        self.assertEqual(entry["spec"]["layout"], {"fmt": "meme", "top": "верх", "bottom": "низ"})
        self.assertEqual(offer.call_args.args[1], "верх|низ")

    async def test_stats_are_private_and_admin_only(self):
        with patch.object(bot, "ADMIN_ID", 99), patch.object(usage_stats, "report") as report:
            self.message.text = "/stats"
            await bot.cmd_stats(self.message)
            self.message.from_user.id = 99
            self.message.chat.type = "group"
            await bot.cmd_stats(self.message)
            report.assert_not_called()


class UsageTests(unittest.TestCase):
    def test_unique_people_not_click_counts_and_excludes_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(usage_stats, "DB_FILE", Path(tmp)/"usage.sqlite3"), patch.dict(os.environ, {"ADMIN_ID": "99"}):
            for _ in range(100):
                usage_stats.record(1, "random")
            usage_stats.record(2, "custom")
            usage_stats.record(3, "custom")
            usage_stats.record(4, "active")
            usage_stats.record(99, "random")
            report = usage_stats.report(7)
            self.assertIn("Активных пользователей: 4", report)
            self.assertIn("Рандомный мем: 1 · 25% · 100", report)
            self.assertIn("Свой мем: 2 · 50% · 2", report)
            self.assertLess(report.index("Свой мем:"), report.index("Рандомный мем:"))
            with sqlite3.connect(usage_stats.DB_FILE) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM usage WHERE user_id=99").fetchone()[0], 0)

    def test_period_filter_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(usage_stats, "DB_FILE", Path(tmp)/"usage.sqlite3"), patch.dict(os.environ, {"ADMIN_ID": "99"}):
            usage_stats.record(1, "chat")
            old = (datetime.now(usage_stats.TZ).date() - timedelta(days=10)).isoformat()
            with sqlite3.connect(usage_stats.DB_FILE) as db:
                db.execute("INSERT INTO usage VALUES (?,2,'custom',5)", (old,))
            self.assertNotIn("Свой мем:", usage_stats.report(7))
            self.assertIn("Свой мем: 1", usage_stats.report(30))
            self.assertEqual(usage_stats.report(30), usage_stats.report(30))

    def test_broken_stats_do_not_break_user_actions(self):
        with patch.object(usage_stats, "_connect", side_effect=OSError("unavailable")), \
                patch.dict(os.environ, {"ADMIN_ID": "99"}), self.assertLogs("usage_stats", level="ERROR"):
            usage_stats.record(1, "random")
