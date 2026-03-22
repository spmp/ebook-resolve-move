import unittest
from pathlib import Path

import ebook_resolve_move as erm


class FunctionTests(unittest.TestCase):
    def test_norm_text_removes_accents_and_punctuation(self):
        self.assertEqual(erm.norm_text("Jalapeño: A-Test!"), "jalapeno a test")

    def test_parse_filename_author_dash_title(self):
        meta = erm.parse_filename_metadata(Path("Adyashanti - Emptiness Dancing.epub"))
        self.assertEqual(meta.author, "Adyashanti")
        self.assertEqual(meta.title, "Emptiness Dancing")

    def test_parse_filename_title_dash_author_current_behavior(self):
        meta = erm.parse_filename_metadata(Path("Emptiness Dancing - Adyashanti.epub"))
        self.assertEqual(meta.author, "Emptiness Dancing")
        self.assertEqual(meta.title, "Adyashanti")

    def test_parse_filename_underscore_only(self):
        meta = erm.parse_filename_metadata(Path("Adyashanti_Emptiness_Dancing.epub"))
        self.assertIsNone(meta.author)
        self.assertEqual(meta.title, "Adyashanti Emptiness Dancing")

    def test_build_query_prefers_embedded_then_filename(self):
        embedded = erm.EmbeddedMetadata(title="Book Title", author="Jane Doe", source="epub")
        filename = erm.FilenameMetadata(title="Fallback Title", author="Fallback Author")
        self.assertEqual(erm.build_query(embedded, filename, "ignored.epub"), "Jane Doe Book Title")

    def test_build_query_uses_filename_when_embedded_missing(self):
        embedded = erm.EmbeddedMetadata(title=None, author=None, source="epub")
        filename = erm.FilenameMetadata(title="Book Title", author="Jane Doe")
        self.assertEqual(erm.build_query(embedded, filename, "ignored.epub"), "Jane Doe Book Title")

    def test_title_and_author_match_flags(self):
        embedded = erm.EmbeddedMetadata(
            title="Emptiness Dancing - PDFDrive.com",
            author="Adyashanti",
            source="epub",
        )
        filename = erm.FilenameMetadata(title="Emptiness Dancing", author="Adyashanti")
        work = erm.WorkMetadata(
            work_id=7012,
            title="Emptiness Dancing: Selected Dharma Talks of Adyashanti",
            short_title="Emptiness Dancing",
            full_title="Emptiness Dancing: Selected Dharma Talks of Adyashanti",
            author="Adyashanti",
        )
        candidate = erm.score_work(embedded, filename, work)
        self.assertTrue(candidate.title_match)
        self.assertTrue(candidate.author_match)


if __name__ == "__main__":
    unittest.main(verbosity=2)
