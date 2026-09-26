"""`# private` sections never reach the database, the hash, the chunks or search."""
import pytest

from secondbrain import db as dbmod
from secondbrain.ingest import run
from secondbrain.private import strip_private
from secondbrain.search import search
from secondbrain.sources.obsidian import Obsidian

from fakes import FakeEmbedder


def test_strips_until_next_h1():
    text = "# Day\nate lunch\n# private\nsecret one\n# Work\nshipped it\n"
    assert strip_private(text) == "# Day\nate lunch\n# Work\nshipped it\n"


def test_subheaders_stay_private():
    text = "# private\n## Therapy\nsecret\n### deeper\nsecret\n# Public\nok\n"
    assert strip_private(text) == "# Public\nok\n"


def test_private_to_end_of_file():
    assert strip_private("intro\n# private\nsecret\n## more\nsecret") == "intro\n"


@pytest.mark.parametrize("header", ["# private", "# Private", "# PRIVATE ", "  # private",
                                    "#\tprivate", "# private #"])
def test_header_variants_start_private(header):
    assert strip_private(f"ok\n{header}\nsecret\n") == "ok\n"


@pytest.mark.parametrize("line", ["#private", "## private", "# private notes",
                                  "    # private", "private"])
def test_non_matching_lines_do_not_start_private(line):
    text = f"{line}\nvisible\n"
    assert strip_private(text) == text


def test_code_comment_inside_private_does_not_end_it():
    text = ("# private\nsecret\n```bash\n# install deps\npip install x\n```\n"
            "still secret\n# Public\nok\n")
    assert strip_private(text) == "# Public\nok\n"


def test_private_header_inside_code_block_is_not_a_header():
    text = "# Notes\n```md\n# private\n```\nvisible\n"
    assert strip_private(text) == text


def test_tilde_fences_and_longer_closers():
    text = "# private\n~~~~\n# x\n~~~\n# still in fence\n~~~~~\nsecret\n# Public\nok\n"
    assert strip_private(text) == "# Public\nok\n"


def test_unclosed_fence_in_private_keeps_rest_private():
    assert strip_private("# private\n```\n# Public\nleak?\n") == ""


def test_windows_line_endings():
    assert strip_private("a\r\n# private\r\nsecret\r\n# B\r\nb\r\n") == "a\r\n# B\r\nb\r\n"


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Daily").mkdir(parents=True)
    (vault / "Daily" / "2026-09-01.md").write_text(
        "# Morning\nwalked the dog\n# private\n## feelings\nzebra confession\n# Evening\nread\n")
    (vault / "Projects.md").write_text("# Plans\nbuild the barn\n")
    return vault, dbmod.connect(tmp_path / "t.db"), Obsidian(vault), FakeEmbedder()


def test_private_text_never_stored_or_searchable(env):
    vault, db, src, emb = env
    run(db, src, emb)
    dump = "\n".join(str(tuple(r)) for t in ("notes", "chunks")
                     for r in db.execute(f"SELECT * FROM {t}"))
    assert "zebra" not in dump and "feelings" not in dump
    assert "walked the dog" in dump and "read" in dump
    assert all("zebra" not in h["text"] for h in search(db, emb, "zebra confession", limit=10))


def test_editing_only_private_text_creates_no_new_row(env):
    vault, db, src, emb = env
    run(db, src, emb)
    note = vault / "Daily" / "2026-09-01.md"
    note.write_text(note.read_text().replace("zebra confession", "other secret"))
    s = run(db, src, emb)
    assert (s["inserted"], s["marked_stale"]) == (0, 0)


def test_whole_vault_including_subfolders(env):
    vault, db, src, emb = env
    run(db, src, emb)
    ids = [r[0] for r in db.execute("SELECT external_id FROM notes ORDER BY 1")]
    assert ids == ["Daily/2026-09-01.md", "Projects.md"]


def test_icloud_placeholder_blocks_fetch(env):
    vault, db, src, emb = env
    run(db, src, emb)
    (vault / "Projects.md").rename(vault / ".Projects.md.icloud")
    with pytest.raises(RuntimeError, match="offloaded to iCloud"):
        run(db, src, emb)
    assert db.execute("SELECT count(*) FROM notes WHERE stale_at IS NULL").fetchone()[0] == 2
