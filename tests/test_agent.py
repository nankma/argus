import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import agent
import guardrails
import news_sources
import users_db
from tests.fakes import FakeToolCallingModel, RecordingCallbackHandler


def _fake_request(context):
    """_compose_prompt only reads request.runtime.context -- a real
    LangChain ModelRequest is unnecessary machinery for testing the
    prompt-composition logic in isolation."""
    return SimpleNamespace(runtime=SimpleNamespace(context=context))


def _record_init_chat_model(monkeypatch):
    """build_model calls the module-level init_chat_model name, so
    monkeypatching it on the agent module (not the real langchain function)
    lets these tests assert what string was requested without constructing
    a real chat model or making any network call."""
    calls = []
    monkeypatch.setattr(agent, "init_chat_model", lambda s: calls.append(s) or "fake-model")
    return calls


def test_build_model_reads_env_var(monkeypatch):
    calls = _record_init_chat_model(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_TEST", "deepseek:deepseek-chat")
    result = agent.build_model("LLM_MODEL_TEST")
    assert calls == ["deepseek:deepseek-chat"]
    assert result == "fake-model"


def test_build_model_falls_back_to_default_when_env_unset(monkeypatch):
    calls = _record_init_chat_model(monkeypatch)
    monkeypatch.delenv("LLM_MODEL_TEST_UNSET", raising=False)
    agent.build_model("LLM_MODEL_TEST_UNSET")
    assert calls == [agent.DEFAULT_MODEL]


def test_build_model_honors_a_custom_default(monkeypatch):
    calls = _record_init_chat_model(monkeypatch)
    monkeypatch.delenv("LLM_MODEL_TEST_CUSTOM_DEFAULT", raising=False)
    agent.build_model("LLM_MODEL_TEST_CUSTOM_DEFAULT", default="openai:gpt-4o-mini")
    assert calls == ["openai:gpt-4o-mini"]


def test_compose_prompt_defaults_to_news_query_when_no_category():
    prompt = agent._compose_prompt(_fake_request({}))
    assert agent._NEWS_QUERY_INSTRUCTIONS in prompt
    assert agent.LAYER1_IDENTITY in prompt


def test_compose_prompt_defaults_to_news_query_when_context_is_none():
    prompt = agent._compose_prompt(_fake_request(None))
    assert agent._NEWS_QUERY_INSTRUCTIONS in prompt


def test_compose_prompt_always_uses_news_query_instructions():
    # Route B (set_interest/remove_interest/start_push/stop_push/
    # set_language) is dispatched directly by agent.dispatch_settings now
    # -- the agent loop, and therefore this prompt, only ever runs for
    # news_query. The `category` context key no longer selects anything
    # here; this just confirms that stays true regardless of what's passed.
    for category in (None, "news_query", "set_interest", "start_push"):
        prompt = agent._compose_prompt(_fake_request({"category": category}))
        assert agent._NEWS_QUERY_INSTRUCTIONS in prompt


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
    # confirmations -- see docs/plans/bot-features-plan.md item 2.
    users_db.set_language(105, "French")
    for category in ("news_query", "set_interest", "start_push", "set_language"):
        prompt = agent._compose_prompt(_fake_request({"chat_id": 105, "category": category}))
        assert "French" in prompt


def _classification(category, **kwargs):
    return guardrails.MessageClassification(on_topic=True, categories=[category], **kwargs)


def test_dispatch_settings_set_interest_adds_new_topic(isolated_subscribers_db):
    result = agent.dispatch_settings("set_interest", 201, _classification("set_interest", topic="robotics"))
    assert "Added robotics" in result
    assert users_db.get_interests(201) == ["robotics"]


def test_dispatch_settings_set_interest_already_covered(isolated_subscribers_db):
    users_db.set_interests(202, ["robotics"])
    result = agent.dispatch_settings("set_interest", 202, _classification("set_interest", topic="robotics"))
    assert "already have robotics" in result
    assert users_db.get_interests(202) == ["robotics"]


def test_dispatch_settings_remove_interest_removes_existing(isolated_subscribers_db):
    users_db.set_interests(203, ["robotics", "AI"])
    result = agent.dispatch_settings("remove_interest", 203, _classification("remove_interest", topic="robotics"))
    assert "Removed robotics" in result
    assert users_db.get_interests(203) == ["AI"]


def test_dispatch_settings_remove_interest_not_present(isolated_subscribers_db):
    result = agent.dispatch_settings("remove_interest", 204, _classification("remove_interest", topic="robotics"))
    assert "wasn't in your interests" in result


def test_dispatch_settings_start_push_enables_and_sets_interval(isolated_subscribers_db):
    result = agent.dispatch_settings("start_push", 205, _classification("start_push", push_interval_hours=6))
    assert "every 6 hour(s)" in result
    assert users_db.get_push_enabled(205) is True
    assert users_db.get_push_interval_hours(205) == 6


def test_dispatch_settings_start_push_no_interval_leaves_existing(isolated_subscribers_db):
    users_db.set_push_interval_hours(206, 12)
    result = agent.dispatch_settings("start_push", 206, _classification("start_push"))
    assert "every 12 hour(s)" in result
    assert users_db.get_push_interval_hours(206) == 12


def test_dispatch_settings_start_push_invalid_interval_reports_error(isolated_subscribers_db):
    result = agent.dispatch_settings("start_push", 207, _classification("start_push", push_interval_hours=0))
    assert "couldn't set that interval" in result
    assert users_db.get_push_enabled(207) is True  # the enable itself still succeeded


def test_dispatch_settings_stop_push_disables(isolated_subscribers_db):
    users_db.set_push_enabled(208, True)
    result = agent.dispatch_settings("stop_push", 208, _classification("stop_push"))
    assert "Turned off" in result
    assert users_db.get_push_enabled(208) is False


def test_dispatch_settings_set_language_sets_new_language(isolated_subscribers_db):
    result = agent.dispatch_settings("set_language", 209, _classification("set_language", language="Spanish"))
    assert "Spanish" in result
    assert users_db.get_language(209) == "Spanish"


def test_dispatch_settings_set_language_reports_current_when_none_named(isolated_subscribers_db):
    users_db.set_language(210, "French")
    result = agent.dispatch_settings("set_language", 210, _classification("set_language"))
    assert "currently set to French" in result
    assert users_db.get_language(210) == "French"  # unchanged


def test_dispatch_settings_set_language_reports_unset_when_none_named_and_unset(isolated_subscribers_db):
    result = agent.dispatch_settings("set_language", 211, _classification("set_language"))
    assert "No reply language is set" in result


def test_dispatch_settings_rejects_non_route_b_category():
    try:
        agent.dispatch_settings("news_query", 1, _classification("news_query"))
        assert False, "expected ValueError"
    except ValueError:
        pass


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
