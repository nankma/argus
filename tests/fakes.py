"""
Test doubles for the LLM and telemetry/logging layers.

Neither LangChain's built-in fakes (GenericFakeChatModel,
FakeMessagesListChatModel) implement bind_tools() — langchain.agents.
create_agent calls model.bind_tools(tools, tool_choice=...) unconditionally
when tools are present, so those fakes raise NotImplementedError. Confirmed
live before writing this. FakeToolCallingModel below overrides bind_tools()
to return self, ignoring the schema, since responses are pre-scripted.
"""

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeToolCallingModel(BaseChatModel):
    """Cycles through a scripted list of AIMessage responses (stays on the
    last one once exhausted). Pass tool_calls on an AIMessage to script a
    tool call; create_agent executes the real tool and feeds the result
    back for the next response in the list."""

    responses: list[AIMessage]
    i: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[self.i]
        self.i = min(self.i + 1, len(self.responses) - 1)
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling-model"


class RecordingCallbackHandler(BaseCallbackHandler):
    """In-memory stand-in for a real telemetry backend. Records LLM and tool
    start/end events as plain dicts in self.events, in call order, for tests
    to assert against instead of hitting Phoenix/LangSmith/etc."""

    def __init__(self):
        self.events: list[dict] = []

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.events.append({"type": "llm_start"})

    def on_llm_end(self, response, **kwargs):
        self.events.append({"type": "llm_end"})

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.events.append({"type": "tool_start", "tool": serialized.get("name"), "input": input_str})

    def on_tool_end(self, output, **kwargs):
        self.events.append({"type": "tool_end", "output": str(output)})
