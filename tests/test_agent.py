import json

from langchain_core.messages import AIMessage, ToolMessage

import agent
import news_sources
from tests.fakes import FakeToolCallingModel, RecordingCallbackHandler


def test_save_note_writes_isolated_file(isolated_notes_file):
    fake_model = FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "save_note", "args": {"note": "test note"}, "id": "call_1"}],
            ),
            AIMessage(content="Saved it for you."),
        ]
    )
    built = agent.build_agent(fake_model)

    result = agent.run_agent(built, [{"role": "user", "content": "remember: test note"}])

    assert result[-1].content == "Saved it for you."
    lines = isolated_notes_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["note"] == "test note"
    assert "ts" in entry


def test_search_news_aggregates_and_isolates_errors(monkeypatch):
    def working_source(query, max_results):
        return [{"title": "Test Article", "link": "https://example.com", "source": "TestSource"}]

    def failing_source(query, max_results):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(
        news_sources, "enabled_sources", lambda: [("working", working_source), ("failing", failing_source)]
    )

    fake_model = FakeToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_news", "args": {"query": "test"}, "id": "call_1"}],
            ),
            AIMessage(content="Here's what I found."),
        ]
    )
    built = agent.build_agent(fake_model)

    result = agent.run_agent(built, [{"role": "user", "content": "what's trending?"}])

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    tool_output = tool_messages[0].content
    assert "Test Article" in tool_output
    assert "ERROR: simulated network failure" in tool_output
    # one failing source must not prevent the other source's results from
    # reaching the model, or the final answer from being produced
    assert result[-1].content == "Here's what I found."


def test_run_agent_no_tool_call_direct_answer():
    fake_model = FakeToolCallingModel(responses=[AIMessage(content="Hi there!")])
    built = agent.build_agent(fake_model)

    result = agent.run_agent(built, [{"role": "user", "content": "hello"}])

    assert result[-1].content == "Hi there!"
    assert not any(isinstance(m, ToolMessage) for m in result)


def test_run_agent_records_callback_events():
    fake_model = FakeToolCallingModel(responses=[AIMessage(content="Hi there!")])
    built = agent.build_agent(fake_model)
    recorder = RecordingCallbackHandler()

    agent.run_agent(built, [{"role": "user", "content": "hello"}], callbacks=[recorder])

    event_types = [e["type"] for e in recorder.events]
    assert "llm_start" in event_types
    assert "llm_end" in event_types
