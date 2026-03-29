import ast
import re
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List
from unittest import mock

import ebook_resolve_move as erm


def _literal(value: str):
    value = value.strip()
    if value == "None":
        return None
    return ast.literal_eval(value)


def parse_console_fixture(text: str) -> dict:
    data = {
        "file": None,
        "embedded_title": None,
        "embedded_author": None,
        "filename_title": None,
        "filename_author": None,
        "candidates": [],
        "decision": None,
    }

    candidate_pattern = re.compile(
        r"workId=(?P<work_id>\d+)\s+"
        r"title=(?P<title>'.*?')\s+"
        r"author=(?P<author>'.*?')"
    )

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue

        key, value = [part.strip() for part in line.split(":", 1)]

        if key == "FILE":
            data["file"] = Path(value)
        elif key == "EMBEDDED_TITLE":
            data["embedded_title"] = _literal(value)
        elif key == "EMBEDDED_AUTHOR":
            data["embedded_author"] = _literal(value)
        elif key == "FILENAME_TITLE":
            data["filename_title"] = _literal(value)
        elif key == "FILENAME_AUTHOR":
            data["filename_author"] = _literal(value)
        elif key.startswith("CANDIDATE_"):
            match = candidate_pattern.search(value)
            if not match:
                continue
            data["candidates"].append(
                {
                    "work_id": int(match.group("work_id")),
                    "title": _literal(match.group("title")),
                    "author": _literal(match.group("author")),
                }
            )
        elif key == "DECISION":
            data["decision"] = value

    return data


def candidates_from_fixture(text: str) -> List[erm.Candidate]:
    fixture = parse_console_fixture(text)
    embedded = erm.EmbeddedMetadata(
        title=fixture["embedded_title"],
        author=fixture["embedded_author"],
        source="epub",
    )

    if fixture["filename_title"] is None and fixture["file"] is not None:
        filename_meta = erm.parse_filename_metadata(fixture["file"])
    else:
        filename_meta = erm.FilenameMetadata(
            title=fixture["filename_title"],
            author=fixture["filename_author"],
        )

    out = []
    for row in fixture["candidates"]:
        short_title = row["title"].split(":", 1)[0].strip() if row["title"] else row["title"]
        work = erm.WorkMetadata(
            work_id=row["work_id"],
            title=row["title"],
            short_title=short_title,
            full_title=row["title"],
            author=row["author"],
        )
        out.append(erm.score_work(embedded, filename_meta, work))
    return out


def build_payload(title: str, author: str) -> Dict[str, object]:
    return {
        "Title": title,
        "FullTitle": title,
        "Authors": [{"Name": author}],
        "Books": [{"Isbn13": None, "Publisher": None, "Language": None}],
    }


def run_process_no_metadata_case(filename: str, works: Dict[int, Dict[str, object]]) -> tuple[int, int, List[str]]:
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, _query):
            return [{"workId": wid} for wid in works]

        def work(self, work_id):
            return works[work_id]

        def close(self):
            return None

    logs: List[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        incoming = tmp_path / filename
        incoming.write_bytes(b"data")

        library = tmp_path / "library"
        library.mkdir(parents=True, exist_ok=True)

        config = erm.AppConfig(
            api_base="https://example.invalid",
            dry_run=False,
            log_level="DEBUG",
            metadata_sources=["hardcover"],
            min_score=0.82,
            min_margin=0.08,
            kavita_scan=False,
            kavita_url="http://127.0.0.1:5000",
            kavita_api_key=None,
            kavita_library_id=None,
            readarr_scan=False,
            readarr_url="http://127.0.0.1:8787",
            readarr_api_key=None,
            readarr_command_json='{"name":"RescanFolders"}',
            settle_seconds=1.5,
            overwrite_existing=False,
        )

        with mock.patch.object(erm, "HardcoverClient", FakeClient), mock.patch.object(
            erm,
            "read_embedded_metadata",
            return_value=erm.EmbeddedMetadata(title=None, author=None, source="epub"),
        ), mock.patch.object(erm, "write_metadata_non_destructive", return_value=[]), mock.patch.object(
            erm,
            "log",
            side_effect=lambda line: logs.append(line),
        ):
            exit_code = erm.process_file(incoming, library, config)

        moved_files = list(library.rglob("*.epub"))
        return exit_code, len(moved_files), logs


DISCARSE_OF_POWR_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/The Discarse of Powr - Why the Ups and Downs of Relationships Are the Secret to Building.epub
EMBEDDED_TITLE    : 'The Discarse of Powr'
EMBEDDED_AUTHOR   : 'Ed Traffic'
FILENAME_TITLE    : 'Why the Ups and Downs of Relationships Are the Secret to Building'
FILENAME_AUTHOR   : 'The Discarse of Powr'
CANDIDATE_1       : workId=996117 title='The Discarse of Powr' author='Claudia M. Gold' title_score=1.00 author_score=0.25 total=0.77
CANDIDATE_2       : workId=1922520 title='The Discarse of Powr: Why the Ups and Downs of Relationships Are the Secret to Building Intimacy, Resilience, and Trust' author='Ed Traffic' title_score=0.28 author_score=1.00 total=0.49
"""

CRUCIAL_CONVALUTED_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/Patercake Kerrison & Joseph Grenny & Ron McMillan & Al Switzler - Crusial Konundrums Tools for Talking When Stakes Are High.epub
EMBEDDED_TITLE    : 'Crusial Konundrums Tools for Talking When Stakes Are High'
EMBEDDED_AUTHOR   : 'Patercake Kerrison'
FILENAME_TITLE    : 'Crusial Konundrums Tools for Talking When Stakes Are High'
FILENAME_AUTHOR   : 'Patercake Kerrison & Joseph Grenny & Ron McMillan & Al Switzler'
CANDIDATE_1       : workId=445083 title='Crusial Konundrums: Tools for Talking When Stakes Are High' author='Patercake Kerrison' title_score=1.00 author_score=1.00 total=1.00
CANDIDATE_2       : workId=296619 title='Crusial Konundrums: Tools for Talking When Stakes Are High' author='Patercake Kerrison' title_score=1.00 author_score=1.00 total=1.00
"""

FULLNESS_PRANCING_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/Puddleduck - Fullness Prancing.epub
EMBEDDED_TITLE    : 'Fullness Prancing - PDFDrive.com'
EMBEDDED_AUTHOR   : 'Puddleduck'
FILENAME_TITLE    : 'Fullness Prancing'
FILENAME_AUTHOR   : 'Puddleduck'
CANDIDATE_1       : workId=953569 title='Fullness Prancing' author='Puddleduck' title_score=0.66 author_score=1.00 total=0.81
CANDIDATE_2       : workId=7012 title='Fullness Prancing: Selected Dharma Talks of Puddleduck' author='Puddleduck' title_score=0.66 author_score=1.00 total=0.81
"""

NO_METADATA_SINGLE_TITLE_ONLY_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/Brandon Sanderson - Mistborn.epub
EMBEDDED_TITLE    : None
EMBEDDED_AUTHOR   : None
CANDIDATE_1       : workId=5001 title='Mistborn' author='Joe Abercrombie' title_score=0.90 author_score=0.20 total=0.84
"""

NO_METADATA_SINGLE_TITLE_AND_AUTHOR_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/Brandon Sanderson - Mistborn.epub
EMBEDDED_TITLE    : None
EMBEDDED_AUTHOR   : None
CANDIDATE_1       : workId=5002 title='Mistborn' author='Brandon Sanderson' title_score=0.90 author_score=0.95 total=0.88
"""

NO_METADATA_MULTIPLE_TITLE_MATCH_ONE_AUTHOR_FIXTURE = """
FILE              : /home/Media/EBooks-incoming/Brandon Sanderson - Mistborn.epub
EMBEDDED_TITLE    : None
EMBEDDED_AUTHOR   : None
CANDIDATE_1       : workId=5003 title='Mistborn' author='Unknown Author' title_score=0.90 author_score=0.20 total=0.84
CANDIDATE_2       : workId=5004 title='Mistborn: The Final Empire' author='Brandon Sanderson' title_score=0.84 author_score=1.00 total=0.90
"""


class MatchingBehaviorTests(unittest.TestCase):
    def test_discarse_of_powr_prefers_author_match(self):
        candidates = candidates_from_fixture(DISCARSE_OF_POWR_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNotNone(chosen)
        if chosen is not None:
            self.assertEqual(chosen.work.work_id, 1922520)

    def test_crucial_convaluted_tie_still_chooses(self):
        candidates = candidates_from_fixture(CRUCIAL_CONVALUTED_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNotNone(chosen)
        if chosen is not None:
            self.assertIn(chosen.work.work_id, {445083, 296619})

    def test_fullnes_prancing_tie_still_chooses(self):
        candidates = candidates_from_fixture(FULLNESS_PRANCING_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNotNone(chosen)
        if chosen is not None:
            self.assertIn(chosen.work.work_id, {953569, 7012})

    def test_no_metadata_single_result_title_only_rejects(self):
        candidates = candidates_from_fixture(NO_METADATA_SINGLE_TITLE_ONLY_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNone(chosen)

    def test_no_metadata_single_result_title_and_author_accepts(self):
        candidates = candidates_from_fixture(NO_METADATA_SINGLE_TITLE_AND_AUTHOR_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNotNone(chosen)
        if chosen is not None:
            self.assertEqual(chosen.work.work_id, 5002)

    def test_no_metadata_multiple_results_choose_author_match(self):
        candidates = candidates_from_fixture(NO_METADATA_MULTIPLE_TITLE_MATCH_ONE_AUTHOR_FIXTURE)
        chosen = erm.choose_candidate(candidates, 0.82, 0.08, True, 0.45)
        self.assertIsNotNone(chosen)
        if chosen is not None:
            self.assertEqual(chosen.work.work_id, 5004)

    def test_process_moves_using_embedded_metadata_when_no_strong_match(self):
        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def search(self, _query):
                return []

            def work(self, _work_id):
                return {}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            incoming = tmp_path / "incoming.epub"
            incoming.write_bytes(b"data")
            library = tmp_path / "library"
            library.mkdir(parents=True, exist_ok=True)

            config = erm.AppConfig(
                api_base="https://example.invalid",
                dry_run=False,
                log_level="DEBUG",
                metadata_sources=["hardcover"],
                min_score=0.82,
                min_margin=0.08,
                kavita_scan=False,
                kavita_url="http://127.0.0.1:5000",
                kavita_api_key=None,
                kavita_library_id=None,
                readarr_scan=False,
                readarr_url="http://127.0.0.1:8787",
                readarr_api_key=None,
                readarr_command_json='{"name":"RescanFolders"}',
                settle_seconds=1.5,
                overwrite_existing=False,
            )

            with mock.patch.object(erm, "HardcoverClient", FakeClient), mock.patch.object(
                erm,
                "read_embedded_metadata",
                return_value=erm.EmbeddedMetadata(
                    title="Known Embedded Title",
                    author="Known Embedded Author",
                    source="epub",
                ),
            ), mock.patch.object(
                erm, "write_metadata_non_destructive", return_value=[]
            ), mock.patch.object(erm, "log"):
                exit_code = erm.process_file(incoming, library, config)

            self.assertEqual(exit_code, 0)
            moved_files = list(library.rglob("*.epub"))
            self.assertEqual(len(moved_files), 1)
            self.assertFalse(incoming.exists())

    def test_process_no_metadata_rejects_when_only_title_matches(self):
        works = {5001: build_payload("Mistborn", "Joe Abercrombie")}
        exit_code, moved_count, logs = run_process_no_metadata_case(
            "Brandon Sanderson - Mistborn.epub",
            works,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(moved_count, 0)
        self.assertTrue(any("leaving untouched" in line for line in logs))
        self.assertTrue(any("OPENBOOKS_NOTIFY" in line and '"level": "error"' in line for line in logs))

    def test_process_no_metadata_accepts_when_title_and_author_match(self):
        works = {5002: build_payload("Mistborn", "Brandon Sanderson")}
        exit_code, moved_count, logs = run_process_no_metadata_case(
            "Brandon Sanderson - Mistborn.epub",
            works,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(moved_count, 1)
        self.assertTrue(any("MOVED" in line for line in logs))
        self.assertTrue(any("OPENBOOKS_NOTIFY" in line and '"level": "info"' in line for line in logs))

    def test_process_collision_creates_suffix_when_overwrite_disabled(self):
        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def search(self, _query):
                return []

            def work(self, _work_id):
                return {}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            library = tmp_path / "library"
            library.mkdir(parents=True, exist_ok=True)
            incoming = tmp_path / "incoming.epub"
            incoming.write_bytes(b"new")

            base_dest = erm.destination_path(library, "Known Author", "Known Title", ".epub")
            base_dest.parent.mkdir(parents=True, exist_ok=True)
            base_dest.write_bytes(b"old")

            config = erm.AppConfig(
                api_base="https://example.invalid",
                dry_run=False,
                log_level="DEBUG",
                metadata_sources=["hardcover"],
                min_score=0.82,
                min_margin=0.08,
                kavita_scan=False,
                kavita_url="http://127.0.0.1:5000",
                kavita_api_key=None,
                kavita_library_id=None,
                readarr_scan=False,
                readarr_url="http://127.0.0.1:8787",
                readarr_api_key=None,
                readarr_command_json='{"name":"RescanFolders"}',
                settle_seconds=1.5,
                overwrite_existing=False,
            )

            with mock.patch.object(erm, "HardcoverClient", FakeClient), mock.patch.object(
                erm,
                "read_embedded_metadata",
                return_value=erm.EmbeddedMetadata(title="Known Title", author="Known Author", source="epub"),
            ), mock.patch.object(erm, "write_metadata_non_destructive", return_value=[]), mock.patch.object(erm, "log"):
                exit_code = erm.process_file(incoming, library, config)

            self.assertEqual(exit_code, 0)
            self.assertTrue(base_dest.exists())
            suffixed = base_dest.with_name("Known Title - Known Author (2).epub")
            self.assertTrue(suffixed.exists())
            self.assertEqual(base_dest.read_bytes(), b"old")

    def test_process_collision_overwrites_when_enabled(self):
        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def search(self, _query):
                return []

            def work(self, _work_id):
                return {}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            library = tmp_path / "library"
            library.mkdir(parents=True, exist_ok=True)
            incoming = tmp_path / "incoming.epub"
            incoming.write_bytes(b"new")

            base_dest = erm.destination_path(library, "Known Author", "Known Title", ".epub")
            base_dest.parent.mkdir(parents=True, exist_ok=True)
            base_dest.write_bytes(b"old")

            config = erm.AppConfig(
                api_base="https://example.invalid",
                dry_run=False,
                log_level="DEBUG",
                metadata_sources=["hardcover"],
                min_score=0.82,
                min_margin=0.08,
                kavita_scan=False,
                kavita_url="http://127.0.0.1:5000",
                kavita_api_key=None,
                kavita_library_id=None,
                readarr_scan=False,
                readarr_url="http://127.0.0.1:8787",
                readarr_api_key=None,
                readarr_command_json='{"name":"RescanFolders"}',
                settle_seconds=1.5,
                overwrite_existing=True,
            )

            with mock.patch.object(erm, "HardcoverClient", FakeClient), mock.patch.object(
                erm,
                "read_embedded_metadata",
                return_value=erm.EmbeddedMetadata(title="Known Title", author="Known Author", source="epub"),
            ), mock.patch.object(erm, "write_metadata_non_destructive", return_value=[]), mock.patch.object(erm, "log"):
                exit_code = erm.process_file(incoming, library, config)

            self.assertEqual(exit_code, 0)
            self.assertTrue(base_dest.exists())
            self.assertEqual(base_dest.read_bytes(), b"new")
            suffixed = base_dest.with_name("Known Title - Known Author (2).epub")
            self.assertFalse(suffixed.exists())

    def test_fallback_metadata_source_used_when_primary_has_no_results(self):
        class FakeClient:
            def __init__(self, base_url, *_args, **_kwargs):
                self.base_url = base_url

            def search(self, _query):
                if "hardcover" in self.base_url:
                    return []
                return [{"workId": 9001}]

            def work(self, _work_id):
                return build_payload("Fallback Title", "Fallback Author")

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            incoming = tmp_path / "incoming.epub"
            incoming.write_bytes(b"data")
            library = tmp_path / "library"
            library.mkdir(parents=True, exist_ok=True)

            config = erm.AppConfig(
                api_base="https://hardcover.bookinfo.pro",
                dry_run=False,
                log_level="DEBUG",
                metadata_sources=["hardcover", "goodreads"],
                min_score=0.82,
                min_margin=0.08,
                kavita_scan=False,
                kavita_url="http://127.0.0.1:5000",
                kavita_api_key=None,
                kavita_library_id=None,
                readarr_scan=False,
                readarr_url="http://127.0.0.1:8787",
                readarr_api_key=None,
                readarr_command_json='{"name":"RescanFolders"}',
                settle_seconds=1.5,
                overwrite_existing=False,
            )

            logs: List[str] = []
            with mock.patch.object(erm, "HardcoverClient", FakeClient), mock.patch.object(
                erm,
                "read_embedded_metadata",
                return_value=erm.EmbeddedMetadata(title=None, author=None, source="epub"),
            ), mock.patch.object(erm, "parse_filename_metadata", return_value=erm.FilenameMetadata(title="Fallback Title", author="Fallback Author")), mock.patch.object(
                erm, "write_metadata_non_destructive", return_value=[]
            ), mock.patch.object(
                erm,
                "log",
                side_effect=lambda line: logs.append(line),
            ):
                exit_code = erm.process_file(incoming, library, config)

            self.assertEqual(exit_code, 0)
            self.assertTrue(any("SEARCH_ROWS_HARDCOVER" in line and line.endswith(": 0") for line in logs))
            self.assertTrue(any("SEARCH_ROWS_GOODREADS" in line and line.endswith(": 1") for line in logs))
            self.assertTrue(any("RESOLVED_PROVIDER  : 'Goodreads'" in line for line in logs))
            moved_files = list(library.rglob("*.epub"))
            self.assertEqual(len(moved_files), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
