import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import agent
import news_sources
import users_db
from tests.fakes import FakeToolCallingModel, RecordingCallbackHandler


def _fake_request(context):
    """_compose_prompt only reads request.runtime.context -- a real
    LangChain ModelRequest is unnecessary machinery for testing the
    prompt-composition logic in isolation."""
    return SimpleNamespace(runtime=SimpleNamespace(context=context))


def test_compose_prompt_defaults_to_news_query_when_no_category():
    prompt = agent._compose_prompt(_fake_request({}))
    assert agent._NEWS_QUERY_INSTRUCTIONS in prompt
    assert agent.LAYER1_IDENTITY in prompt


def test_compose_prompt_defaults_to_news_query_when_context_is_none():
    prompt = agent._compose_prompt(_fake_request(None))
    assert agent._NEWS_QUERY_INSTRUCTIONS in prompt


def test_compose_prompt_selects_instructions_per_category():
    by_category = {
        "news_query": agent._NEWS_QUERY_INSTRUCTIONS,
        "set_interest": agent._SET_INTEREST_INSTRUCTIONS,
        "remove_interest": agent._REMOVE_INTEREST_INSTRUCTIONS,
        "start_push": agent._START_PUSH_INSTRUCTIONS,
        "stop_push": agent._STOP_PUSH_INSTRUCTIONS,
        "set_language": agent._SET_LANGUAGE_INSTRUCTIONS,
    }
    for category, expected in by_category.items():
        prompt = agent._compose_prompt(_fake_request({"category": category}))
        assert expected in prompt


def test_compose_prompt_includes_interests_when_set(isolated_subscribers_db):
    users_db.set_interests(101, ["AI", "robotics"])
    prompt = agent._compose_prompt(_fake_request({"chat_id": 101, "category": "news_query"}))
    assert "AI, robotics" in prompt


def test_compose_prompt_omits_interests_when_unset(isolated_subscribers_db):
    prompt = agent._compose_prompt(_fake_request({"chat_id": 102, "category": "news_query"}))
    assert "stated interests" not in prompt


def test_compose_prompt_omits_interests_when_no_chat_id():
    prompt = agent._compose_prompt(_fake_request({"category": "news_query"}))
    assert "stated interests" not in prompt


def test_compose_prompt_includes_language_when_set(isolated_subscribers_db):
    users_db.set_language(103, "Spanish")
    prompt = agent._compose_prompt(_fake_request({"chat_id": 103, "category": "news_query"}))
    assert "Spanish" in prompt
    assert "preferred reply language" in prompt


def test_compose_prompt_omits_language_when_unset(isolated_subscribers_db):
    prompt = agent._compose_prompt(_fake_request({"chat_id": 104, "category": "news_query"}))
    assert "preferred reply language" not in prompt


def test_compose_prompt_language_applies_regardless_of_category(isolated_subscribers_db):
    # Real requirement: unlike interests (news_query-only), a language
    # preference must govern every reply, including subscription
    # confirmations -- see docs/bot-features-plan.md item 2.
    users_db.set_language(105, "French")
    for category in ("news_query", "set_interest", "start_push", "set_language"):
        prompt = agent._compose_prompt(_fake_request({"chat_id": 105, "category": category}))
        assert "French" in prompt


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


def test_search_news_aggregates_and_isolates_errors(monkeypatch, isolated_subscribers_db):
    def working_source(query, max_results):
        return [{"title": "Test Article", "link": "https://example.com", "source": "TestSource"}]

    def failing_source(query, max_results):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(
        news_sources,
        "enabled_sources",
        lambda include_restricted=True: [("working", working_source), ("failing", failing_source)],
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

    result = agent.run_agent(
        built, [{"role": "user", "content": "what's trending?"}], context={"chat_id": 1}
    )

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    tool_output = tool_messages[0].content
    assert "Test Article" in tool_output
    assert "https://example.com" in tool_output  # link must be surfaced so the model can cite it
    assert "ERROR: simulated network failure" in tool_output
    # one failing source must not prevent the other source's results from
    # reaching the model, or the final answer from being produced
    assert result[-1].content == "Here's what I found."


def test_search_news_records_api_call_for_restricted_sources_only(monkeypatch, isolated_subscribers_db):
    def perigon_source(query, max_results):
        return [{"title": "Perigon Article", "link": "https://example.com/p", "source": "Perigon"}]

    def free_source(query, max_results):
        return [{"title": "Free Article", "link": "https://example.com/f", "source": "Free"}]

    monkeypatch.setattr(
        news_sources,
        "enabled_sources",
        lambda include_restricted=True: [("perigon", perigon_source), ("hackernews", free_source)],
    )
    monkeypatch.setattr(users_db, "get_restricted_sources_enabled", lambda chat_id: True)
    record_api_call = SimpleNamespace(calls=[])
    monkeypatch.setattr(
        users_db, "record_api_call", lambda source, today: record_api_call.calls.append((source, today))
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

    agent.run_agent(built, [{"role": "user", "content": "what's trending?"}], context={"chat_id": 1})

    assert len(record_api_call.calls) == 1
    assert record_api_call.calls[0][0] == "perigon"


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
