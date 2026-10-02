import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


_IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _IMPORT_DATA.name

import bot
from aiogram.fsm.storage.base import StorageKey
import phrasebank
from phrase_categories import ALL, ABSURD, GENERAL, HARD, INTELLECTUAL, classify_phrase
import render_store
import stats
import stickers
import submission_queue
from sqlite_storage import SQLiteStorage


class RenderHistoryTests(unittest.TestCase):
    def test_random_entry_always_keeps_phrase_for_repic(self):
        entry = bot.random_render_entry("source", "rendered", "верх|низ")
        self.assertEqual(entry["source_file_id"], "source")
        self.assertEqual(
            entry["spec"],
            {"kind": "random", "phrase": "верх|низ", "category": ALL},
        )

    def test_new_render_keyboard_contains_all_phrase_modes(self):
        callbacks = {
            button.callback_data
            for row in bot.try_again_kb("render42").inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn(f"reroll:{ALL}:render42", callbacks)
        self.assertIn(f"reroll:{ABSURD}:render42", callbacks)
        self.assertIn(f"reroll:{HARD}:render42", callbacks)
        self.assertIn(f"reroll:{INTELLECTUAL}:render42", callbacks)

    def test_text_limits_are_reported_before_queueing(self):
        self.assertIsNone(bot.validate_text("нормально", 20))
        self.assertIn("максимум 5", bot.validate_text("слишком длинно", 5))

    def test_render_buttons_do_not_depend_on_fsm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "renders.sqlite3"
            entry = bot.random_render_entry("source", "rendered", "фраза")
            with patch.object(render_store, "DB_FILE", path):
                render_store.save("abc", chat_id=10, user_id=20, entry=entry)
                self.assertEqual(render_store.get("abc", 10, 20), entry)
                self.assertIsNone(render_store.get("abc", 10, 99))


class StickerTests(unittest.TestCase):
    def test_pack_short_name_never_exceeds_telegram_limit(self):
        name = stickers.short_name("Очень длинное название стикерпака " * 4, "b" * 32)
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.endswith("_by_" + "b" * 32))
        self.assertNotIn("__", name)

    def test_encoded_sticker_respects_size_limit(self):
        source = BytesIO()
        pixels = os.urandom(512 * 512 * 4)
        Image.frombytes("RGBA", (512, 512), pixels).save(source, "PNG")

        class FakeBot:
            async def download(self, file_id, destination):
                destination.write(source.getvalue())

        data = asyncio.run(stickers.to_sticker_bytes(FakeBot(), "file"))
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), stickers.STICKER_MAX_BYTES)


class SubmissionTests(unittest.TestCase):
    def test_only_first_approval_claims_pending_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "submissions.json"
            queue.write_text(
                json.dumps({"1": {"status": "pending", "text": "", "photo_file_id": "x"}}),
                encoding="utf-8",
            )
            with patch.object(submission_queue, "QUEUE_FILE", queue):
                self.assertIsNotNone(submission_queue.claim_pending("1"))
                self.assertIsNone(submission_queue.claim_pending("1"))


class StatsTests(unittest.TestCase):
    def test_stats_use_user_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            with patch.object(stats, "STATS_FILE", path):
                stats.track(42)
                stats.track(42)
                self.assertEqual(stats.monthly_active_count(), 1)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(all(key.startswith("users:") for key in data))


class SQLiteStorageTests(unittest.TestCase):
    def test_state_and_data_survive_reopen(self):
        async def scenario(path: Path):
            key = StorageKey(bot_id=1, chat_id=2, user_id=3)
            first = SQLiteStorage(path)
            await first.set_state(key, "Flow:step")
            await first.set_data(key, {"renders": {"abc": {"spec": {"phrase": "мем"}}}})
            await first.close()

            second = SQLiteStorage(path)
            self.assertEqual(await second.get_state(key), "Flow:step")
            self.assertEqual(
                (await second.get_data(key))["renders"]["abc"]["spec"]["phrase"], "мем"
            )
            await second.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp) / "fsm.sqlite3"))


class PhraseCategoryTests(unittest.TestCase):
    def test_representative_phrases_are_classified(self):
        self.assertEqual(classify_phrase("обычный день и обычный стул"), GENERAL)
        self.assertEqual(classify_phrase("гусь назначил совещание"), ABSURD)
        self.assertEqual(classify_phrase("какая-то блядская бухгалтерия"), HARD)
        self.assertEqual(classify_phrase("онтология домашнего тапка"), INTELLECTUAL)

    def test_every_phrase_belongs_to_exactly_one_category(self):
        pools = phrasebank.load_categorized_phrases()
        flattened = [phrase for phrases in pools.values() for phrase in phrases]
        self.assertEqual(len(flattened), phrasebank.phrase_count())
        self.assertTrue(all(pools.values()))

    def test_all_pool_contains_every_category(self):
        pools = phrasebank.load_categorized_phrases()
        self.assertCountEqual(
            phrasebank.load_phrases(ALL),
            [phrase for phrases in pools.values() for phrase in phrases],
        )

    def test_category_cache_notices_an_added_user_phrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "phrases.txt"
            user = Path(tmp) / "phrases_user.txt"
            uncensored = Path(tmp) / "phrases_uncensored.txt"
            main.write_text("обычная фраза\n", encoding="utf-8")
            uncensored.write_text("", encoding="utf-8")
            with patch.object(phrasebank, "PHRASES_FILE", main), \
                    patch.object(phrasebank, "USER_PHRASES_FILE", user), \
                    patch.object(phrasebank, "UNCENSORED_PHRASES_FILE", uncensored), \
                    patch.object(phrasebank, "_categorized_cache_key", None), \
                    patch.object(phrasebank, "_categorized_cache", None):
                self.assertEqual(phrasebank.load_phrases(GENERAL), ["обычная фраза"])
                user.write_text("гусь назначил совещание\n", encoding="utf-8")
                self.assertEqual(
                    phrasebank.load_phrases(ABSURD), ["гусь назначил совещание"])

    def test_each_category_has_its_own_non_repeating_deck(self):
        pools = {
            GENERAL: ["обычная 1", "обычная 2", "обычная 3"],
            ABSURD: ["гусь 1", "гусь 2"],
            HARD: ["жёсткая 1"],
            INTELLECTUAL: ["умная 1"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(phrasebank, "STATE_FILE", state_file), \
                    patch.object(phrasebank, "load_phrases",
                                 side_effect=lambda category=None: pools[category or GENERAL]):
                general = {phrasebank.get_random_phrase(1, GENERAL) for _ in range(3)}
                absurd = {phrasebank.get_random_phrase(1, ABSURD) for _ in range(2)}
        self.assertEqual(general, set(pools[GENERAL]))
        self.assertEqual(absurd, set(pools[ABSURD]))


if __name__ == "__main__":
    unittest.main()
