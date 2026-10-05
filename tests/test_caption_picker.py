import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import caption_picker
import photo_topics
import phrasebank
from phrase_categories import ALL, HARD


class CaptionPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.phrases = ["кот на смене", "кошки требуют отпуск", "котик управляет заводом",
                        "обычная фраза 1", "обычная фраза 2", "обычная фраза 3"]
        for target, name, value in (
            (caption_picker, "DB_FILE", folder / "photos.sqlite3"),
            (caption_picker, "_analysis_gate", asyncio.Semaphore(1)),
            (phrasebank, "STATE_FILE", folder / "state.json"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(phrasebank, "load_phrases", side_effect=lambda category: self.phrases.copy())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_first_three_are_relevant_then_random_without_repeats(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]) as analyze:
            chosen = [await caption_picker.choose_caption(b"photo", 1, 2) for _ in range(6)]
        self.assertEqual(set(chosen[:3]), set(self.phrases[:3]))
        self.assertEqual(set(chosen[3:]), set(self.phrases[3:]))
        self.assertEqual(len(set(chosen)), 6)
        analyze.assert_called_once()

    async def test_earlier_button_and_category_change_share_persistent_history(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]) as analyze:
            first = await caption_picker.choose_caption(b"photo", 1, 2)
            # Всегда жмём кнопку первого результата; каждый вызов заново читает SQLite.
            rest = [await caption_picker.choose_caption(
                b"photo", 1, 2, HARD if i % 2 else ALL, previous_phrase=first)
                for i in range(5)]
        self.assertEqual(len(set([first] + rest)), 6)
        analyze.assert_called_once()

    async def test_thematic_budget_ends_even_with_many_relevant_phrases(self):
        self.phrases = [f"кот номер {i}" for i in range(8)]
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]), \
                patch.object(caption_picker, "get_random_phrase",
                             wraps=phrasebank.get_random_phrase) as choose:
            for _ in range(5):
                await caption_picker.choose_caption(b"photo", 1, 2)
        self.assertEqual(
            [call.kwargs["preferred_topics"] for call in choose.call_args_list],
            [["cat"], ["cat"], ["cat"], (), ()],
        )

    async def test_unknown_image_immediately_uses_random_pool(self):
        with patch.object(photo_topics, "analyze_topics", return_value=[]), \
                patch.object(caption_picker, "get_random_phrase",
                             wraps=phrasebank.get_random_phrase) as choose:
            await caption_picker.choose_caption(b"photo", 1, 2)
        self.assertFalse(choose.call_args.kwargs["preferred_topics"])

    async def test_exhausted_theme_falls_back_early(self):
        self.phrases = ["кот на смене", "обычная фраза 1", "обычная фраза 2"]
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]):
            chosen = [await caption_picker.choose_caption(b"photo", 1, 2) for _ in range(3)]
        self.assertEqual(chosen[0], self.phrases[0])
        self.assertEqual(len(set(chosen)), 3)

    async def test_old_render_phrase_is_not_repeated(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]):
            phrase = await caption_picker.choose_caption(
                b"photo", 1, 2, previous_phrase="кот на смене")
        self.assertNotEqual(phrase, "кот на смене")

    async def test_empty_category_switches_to_all_without_repeating_photo_history(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]), \
                patch.object(phrasebank, "load_phrases", side_effect=lambda category:
                             ["кот на смене"] if category == HARD else self.phrases.copy()):
            first = await caption_picker.choose_caption(b"photo", 1, 2, HARD)
            second = await caption_picker.choose_caption(b"photo", 1, 2, HARD)
        self.assertEqual(first, "кот на смене")
        self.assertNotEqual(first, second)

    async def test_reset_clears_only_requested_chat(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]) as analyze:
            await caption_picker.choose_caption(b"photo", 1, 2)
            await caption_picker.choose_caption(b"photo", 9, 2)
            caption_picker.reset_chat(1)
            await caption_picker.choose_caption(b"photo", 9, 2)
            self.assertEqual(analyze.call_count, 2)
            await caption_picker.choose_caption(b"photo", 1, 2)
            self.assertEqual(analyze.call_count, 3)

    async def test_simultaneous_clicks_cannot_return_same_phrase(self):
        with patch.object(photo_topics, "analyze_topics", return_value=["cat"]) as analyze:
            chosen = await asyncio.gather(*[
                caption_picker.choose_caption(b"photo", 1, 2) for _ in range(6)])
        self.assertEqual(len(set(chosen)), 6)
        analyze.assert_called_once()


class TopicRecognitionTests(unittest.TestCase):
    def test_word_boundaries_do_not_turn_kotoryy_into_a_cat(self):
        self.assertNotIn("cat", photo_topics.phrase_topics("который год уже"))
        self.assertNotIn("food", photo_topics.phrase_topics("поездка в среду"))
        self.assertIn("cat", photo_topics.phrase_topics("котёнок уснул"))
        self.assertIn("dog", photo_topics.phrase_topics("пёс на созвоне"))

    def test_low_confidence_is_not_forced_into_a_topic(self):
        self.assertEqual(photo_topics.topics_from_scores(np.zeros(1000)), [])
        self.assertEqual(photo_topics.topics_from_scores([float("nan")] * 1000), [])

    def test_cat_breeds_are_combined_into_one_topic(self):
        scores = np.full(1000, -15.0)
        scores[281:286] = 0
        self.assertEqual(photo_topics.topics_from_scores(scores), ["cat"])

    def test_unavailable_model_falls_back_without_breaking_render(self):
        source = BytesIO()
        Image.new("RGB", (200, 200), "gray").save(source, "PNG")
        with patch.object(photo_topics, "_network", side_effect=RuntimeError("unavailable")), \
                self.assertLogs(photo_topics.logger, level="ERROR"):
            self.assertEqual(photo_topics.analyze_topics(source.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
