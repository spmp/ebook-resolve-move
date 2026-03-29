import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

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

    def test_parse_work_metadata_extracts_rich_fields(self):
        payload = {
            "FullTitle": "Example Book: A Story",
            "Title": "Example Book",
            "Authors": [{"Name": "Jane Author"}],
            "Description": "A detailed description.",
            "PublicationDate": "2020-01-02",
            "Tags": [{"Name": "Fiction"}, {"Name": "Adventure"}],
            "Books": [
                {
                    "Isbn13": "9781234567890",
                    "Publisher": "Example Publisher",
                    "Language": "en",
                }
            ],
        }

        work = erm.parse_work_metadata(42, payload)
        self.assertEqual(work.work_id, 42)
        self.assertEqual(work.title, "Example Book: A Story")
        self.assertEqual(work.short_title, "Example Book")
        self.assertEqual(work.author, "Jane Author")
        self.assertEqual(work.isbn13, "9781234567890")
        self.assertEqual(work.publisher, "Example Publisher")
        self.assertEqual(work.language, "en")
        self.assertEqual(work.description, "A detailed description.")
        self.assertEqual(work.published_date, "2020-01-02")
        self.assertEqual(work.subjects, ["Fiction", "Adventure"])
        self.assertEqual(work.metadata_provider, "Hardcover")
        self.assertEqual(work.metadata_provider_id, "42")

    def test_write_epub_metadata_dry_run_includes_rich_fields(self):
        existing = erm.EmbeddedMetadata(source="epub")
        resolved = erm.WorkMetadata(
            work_id=1,
            title="Book Title",
            author="Author Name",
            isbn13="9781234567890",
            publisher="Publisher",
            language="en",
            description="Description",
            published_date="2020-01-02",
            subjects=["Tag One", "Tag Two"],
            metadata_provider="Hardcover",
            metadata_provider_id="1",
            metadata_provider_endpoint="https://hardcover.bookinfo.pro",
        )

        writes = erm.write_epub_metadata_non_destructive(
            Path("dummy.epub"),
            existing,
            resolved,
            dry_run=True,
        )

        self.assertIn("WOULD_WRITE_EPUB title=Book Title", writes)
        self.assertIn("WOULD_WRITE_EPUB creator=Author Name", writes)
        self.assertIn("WOULD_WRITE_EPUB identifier=9781234567890", writes)
        self.assertIn("WOULD_WRITE_EPUB publisher=Publisher", writes)
        self.assertIn("WOULD_WRITE_EPUB language=en", writes)
        self.assertIn("WOULD_WRITE_EPUB description=Description", writes)
        self.assertIn("WOULD_WRITE_EPUB date=2020-01-02", writes)
        self.assertIn("WOULD_WRITE_EPUB subject=Tag One", writes)
        self.assertIn("WOULD_WRITE_EPUB subject=Tag Two", writes)
        self.assertIn("WOULD_WRITE_EPUB ebook_resolve_provider=Hardcover", writes)
        self.assertIn("WOULD_WRITE_EPUB ebook_resolve_work_id=1", writes)
        self.assertIn("WOULD_WRITE_EPUB ebook_resolve_provider_endpoint=https://hardcover.bookinfo.pro", writes)

    def test_write_pdf_metadata_dry_run_includes_rich_fields(self):
        existing = erm.EmbeddedMetadata(source="pdf")
        resolved = erm.WorkMetadata(
            work_id=2,
            title="Book Title",
            author="Author Name",
            description="Description",
            publisher="Publisher",
            language="en",
            published_date="2021-01-01",
            isbn13="9781234567890",
            subjects=["Mindfulness", "Meditation"],
            metadata_provider="Hardcover",
            metadata_provider_id="2",
            metadata_provider_endpoint="https://hardcover.bookinfo.pro",
        )

        writes = erm.write_pdf_metadata_non_destructive(
            Path("dummy.pdf"),
            existing,
            resolved,
            dry_run=True,
        )

        self.assertIn("WOULD_WRITE_PDF /Title=Book Title", writes)
        self.assertIn("WOULD_WRITE_PDF /Author=Author Name", writes)
        self.assertIn("WOULD_WRITE_PDF /Subject=Description", writes)
        self.assertIn("WOULD_WRITE_PDF /Keywords=Mindfulness, Meditation", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:title=Book Title", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:creator=['Author Name']", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:description=Description", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:subject=['Mindfulness', 'Meditation']", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:publisher=['Publisher']", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:date=['2021-01-01']", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:language=['en']", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP prism:isbn=9781234567890", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP pdfx:Isbn=9781234567890", writes)
        self.assertIn("WOULD_WRITE_PDF /EBX_MetadataProvider=Hardcover", writes)
        self.assertIn("WOULD_WRITE_PDF /EBX_WorkId=2", writes)
        self.assertIn("WOULD_WRITE_PDF /EBX_MetadataProviderEndpoint=https://hardcover.bookinfo.pro", writes)
        self.assertIn("WOULD_WRITE_PDF_XMP dc:source=Hardcover:2", writes)

    def test_merge_pdf_xmp_non_destructive_writes_expected_tags(self):
        xmp_bytes = erm.merge_pdf_xmp_non_destructive(
            None,
            {
                "dc_title": "Book Title",
                "dc_creator": ["A", "B"],
                "dc_description": "Desc",
                "dc_subject": ["S1", "S2"],
                "dc_publisher": ["Pub"],
                "dc_date": ["2022-01-01"],
                "dc_language": ["en"],
                "prism_isbn": "9781234567890",
                "pdfx_isbn": "9781234567890",
            },
        )
        xml = xmp_bytes.decode("utf-8")
        self.assertIn("Book Title", xml)
        self.assertIn("Desc", xml)
        self.assertIn("S1", xml)
        self.assertIn("S2", xml)
        self.assertIn("Pub", xml)
        self.assertIn("2022-01-01", xml)
        self.assertIn("9781234567890", xml)

    def test_write_docx_metadata_dry_run(self):
        existing = erm.EmbeddedMetadata(source="docx")
        resolved = erm.WorkMetadata(
            work_id=3,
            title="Doc Title",
            author="Doc Author",
            description="Doc Description",
            published_date="2021-06-01",
            subjects=["One", "Two"],
        )
        writes = erm.write_docx_metadata_non_destructive(Path("x.docx"), existing, resolved, dry_run=True)
        self.assertIn("WOULD_WRITE_DOCX title=Doc Title", writes)
        self.assertIn("WOULD_WRITE_DOCX creator=Doc Author", writes)
        self.assertIn("WOULD_WRITE_DOCX description=Doc Description", writes)
        self.assertIn("WOULD_WRITE_DOCX created=2021-06-01", writes)
        self.assertIn("WOULD_WRITE_DOCX keywords=One, Two", writes)

    def test_write_odt_metadata_dry_run(self):
        existing = erm.EmbeddedMetadata(source="odt")
        resolved = erm.WorkMetadata(
            work_id=4,
            title="ODT Title",
            author="ODT Author",
            language="en",
            description="ODT Description",
            published_date="2022-05-05",
            subjects=["Essay"],
        )
        writes = erm.write_odt_metadata_non_destructive(Path("x.odt"), existing, resolved, dry_run=True)
        self.assertIn("WOULD_WRITE_ODT title=ODT Title", writes)
        self.assertIn("WOULD_WRITE_ODT creator=ODT Author", writes)
        self.assertIn("WOULD_WRITE_ODT language=en", writes)
        self.assertIn("WOULD_WRITE_ODT description=ODT Description", writes)
        self.assertIn("WOULD_WRITE_ODT date=2022-05-05", writes)
        self.assertIn("WOULD_WRITE_ODT keyword=Essay", writes)

    def test_write_fb2_metadata_dry_run(self):
        existing = erm.EmbeddedMetadata(source="fb2")
        resolved = erm.WorkMetadata(
            work_id=5,
            title="FB2 Title",
            author="FB2 Author",
            publisher="FB2 Pub",
            language="en",
            description="FB2 Desc",
            published_date="2023",
            subjects=["A", "B"],
        )
        writes = erm.write_fb2_metadata_non_destructive(Path("x.fb2"), existing, resolved, dry_run=True)
        self.assertIn("WOULD_WRITE_FB2 title=FB2 Title", writes)
        self.assertIn("WOULD_WRITE_FB2 author=FB2 Author", writes)
        self.assertIn("WOULD_WRITE_FB2 publisher=FB2 Pub", writes)
        self.assertIn("WOULD_WRITE_FB2 language=en", writes)
        self.assertIn("WOULD_WRITE_FB2 description=FB2 Desc", writes)
        self.assertIn("WOULD_WRITE_FB2 date=2023", writes)

    def test_write_rtf_metadata_dry_run(self):
        existing = erm.EmbeddedMetadata(source="rtf")
        resolved = erm.WorkMetadata(
            work_id=6,
            title="RTF Title",
            author="RTF Author",
            description="RTF Subject",
            subjects=["x", "y"],
        )
        writes = erm.write_rtf_metadata_non_destructive(Path("x.rtf"), existing, resolved, dry_run=True)
        self.assertIn("WOULD_WRITE_RTF title=RTF Title", writes)
        self.assertIn("WOULD_WRITE_RTF author=RTF Author", writes)
        self.assertIn("WOULD_WRITE_RTF subject=RTF Subject", writes)
        self.assertIn("WOULD_WRITE_RTF keywords=x, y", writes)

    def test_write_mobi_non_destructive_reports_skip_in_non_dry(self):
        existing = erm.EmbeddedMetadata(source="mobi")
        resolved = erm.WorkMetadata(work_id=7, title="M", author="A", description="D")
        writes = erm.write_mobi_family_metadata_non_destructive(
            Path("x.mobi"), existing, resolved, dry_run=False, source="mobi"
        )
        self.assertIn("SKIP_WRITE_MOBI title=M", writes)
        self.assertIn("SKIP_WRITE_MOBI author=A", writes)

    def test_read_embedded_metadata_dispatches_extensions(self):
        with mock.patch.object(erm, "read_docx_metadata", return_value=erm.EmbeddedMetadata(source="docx")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.docx")).source, "docx")

        with mock.patch.object(erm, "read_odt_metadata", return_value=erm.EmbeddedMetadata(source="odt")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.odt")).source, "odt")

        with mock.patch.object(erm, "read_fb2_metadata", return_value=erm.EmbeddedMetadata(source="fb2")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.fb2")).source, "fb2")

        with mock.patch.object(erm, "read_fbz_metadata", return_value=erm.EmbeddedMetadata(source="fbz")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.fbz")).source, "fbz")

        with mock.patch.object(erm, "read_zip_opf_metadata", return_value=erm.EmbeddedMetadata(source="htmlz")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.htmlz")).source, "htmlz")

        with mock.patch.object(erm, "read_mobi_family_metadata", return_value=erm.EmbeddedMetadata(source="mobi")):
            self.assertEqual(erm.read_embedded_metadata(Path("a.mobi")).source, "mobi")

    def test_destination_path_includes_author_in_filename(self):
        result = erm.destination_path(
            Path("/library"),
            "Kerry Patterson & Joseph Grenny",
            "Crucial Conversations",
            ".epub",
        )
        self.assertEqual(
            str(result),
            "/library/Kerry Patterson & Joseph Grenny/Crucial Conversations/Crucial Conversations - Kerry Patterson & Joseph Grenny.epub",
        )

    def test_build_config_reads_overwrite_existing_from_env(self):
        args = Namespace(
            api_base=None,
            dry_run=None,
            log_level=None,
            metadata_source=None,
            min_score=None,
            min_margin=None,
            overwrite_existing=None,
            kavita_scan=None,
            kavita_url=None,
            kavita_api_key=None,
            kavita_library_id=None,
            readarr_scan=None,
            readarr_url=None,
            readarr_api_key=None,
            readarr_command_json=None,
            settle_seconds=None,
        )

        with mock.patch.dict("os.environ", {"EBOOK_OVERWRITE_EXISTING": "true"}, clear=False):
            config = erm.build_config(args)
        self.assertTrue(config.overwrite_existing)

    def test_parse_metadata_sources(self):
        self.assertEqual(erm.parse_metadata_sources("all"), ["hardcover", "goodreads"])
        self.assertEqual(erm.parse_metadata_sources("hardcover"), ["hardcover"])
        self.assertEqual(erm.parse_metadata_sources("goodreads,hardcover"), ["goodreads", "hardcover"])
        self.assertEqual(erm.parse_metadata_sources("harcover"), ["hardcover"])

    def test_parse_log_level(self):
        self.assertEqual(erm.parse_log_level("debug"), "DEBUG")
        self.assertEqual(erm.parse_log_level("warn"), "WARN")
        self.assertEqual(erm.parse_log_level("INFO"), "INFO")

    def test_notification_emission_by_log_level(self):
        erm.set_active_log_level("DEBUG")
        self.assertTrue(erm.should_emit_notification("info"))
        self.assertTrue(erm.should_emit_notification("warning"))
        self.assertTrue(erm.should_emit_notification("error"))

        erm.set_active_log_level("WARN")
        self.assertFalse(erm.should_emit_notification("info"))
        self.assertTrue(erm.should_emit_notification("warning"))
        self.assertTrue(erm.should_emit_notification("error"))

        erm.set_active_log_level("INFO")
        self.assertFalse(erm.should_emit_notification("info"))
        self.assertFalse(erm.should_emit_notification("warning"))
        self.assertTrue(erm.should_emit_notification("error"))

    def test_console_emission_by_log_level(self):
        erm.set_active_log_level("INFO")
        self.assertTrue(erm.should_emit_console_line("MOVED             : /a -> /b"))
        self.assertTrue(erm.should_emit_console_line("DECISION          : no non-ambiguous strong match; leaving untouched"))
        self.assertTrue(erm.should_emit_console_line("OPENBOOKS_NOTIFY {\"level\":\"error\"}"))
        self.assertFalse(erm.should_emit_console_line("SEARCH_QUERY      : 'abc'"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
