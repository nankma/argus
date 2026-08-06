import pytest
import agent


@pytest.fixture
def isolated_notes_file(monkeypatch, tmp_path):
    """Point agent.NOTES_FILE at a temp file for the duration of a test, so
    save_note tests never touch the real notes.jsonl. Confirmed necessary by
    an earlier ad-hoc test that wrote a real entry into notes.jsonl before
    this fixture existed."""
    path = tmp_path / "notes.jsonl"
    monkeypatch.setattr(agent, "NOTES_FILE", str(path))
    return path
