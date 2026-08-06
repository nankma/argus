"""
A news-trend agent built on LangChain, with DeepSeek as the LLM.

Agent construction (build_agent) takes the model as a parameter, and
invocation (run_agent) takes an optional callbacks list — neither is
hardcoded at import time. This is what makes the agent testable: swap in a
fake chat model and an in-memory/local callback handler for CI, without
touching this file. See docs/telemetry-and-testing-plan.md for what's built
vs. still planned (test suite, CI, real telemetry backend).

Run:
    conda activate myfirstagent
    export DEEPSEEK_API_KEY=<your-deepseek-key>
    python agent.py
"""

import json
from datetime import datetime
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from langchain.agents import create_agent
import news_sources

MODEL = "deepseek-chat"
NOTES_FILE = "notes.jsonl"

SYSTEM_PROMPT = (
    "You are an AI industry analyst. Use the search_news tool to gather "
    "recent items on a topic (e.g. a company, model, or trend) across "
    "sources like Hacker News, arXiv, and major AI/tech outlets, spot "
    "recurring themes, and write a short trend-report article that cites "
    "the source outlets. If the user asks you to remember something, use "
    "the save_note tool."
)

# --- Tools -------------------------------------------------------------


@tool
def save_note(note: str) -> str:
    """Save a short note to persistent local storage for later recall."""
    entry = {"note": note, "ts": datetime.now().isoformat()}
    with open(NOTES_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return f"Saved note: {note}"


@tool
def search_news(query: str = "AI", max_results_per_source: int = 5) -> str:
    """Search multiple AI-industry news sources for a query (e.g. a company,
    model name, or topic like "AI regulation") and return recent items
    grouped by source. Sources are pluggable — see news_sources.py.
    """
    sources = news_sources.enabled_sources()
    lines = []
    total = 0
    for name, fetch in sources:
        try:
            articles = fetch(query, max_results_per_source)
        except Exception as exc:
            lines.append(f"- [{name}] ERROR: {exc}")
            continue
        total += len(articles)
        for a in articles:
            lines.append(f"- [{name}] {a['title']} ({a.get('source', name)})")
    return f"{total} articles found across {len(sources)} source(s):\n" + "\n".join(lines)


TOOLS = [save_note, search_news]


# --- Agent construction & invocation ------------------------------------

def build_agent(model):
    return create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)


def run_agent(agent, messages: list, callbacks: list | None = None) -> list:
    config = {"callbacks": callbacks} if callbacks else None
    result = agent.invoke({"messages": messages}, config=config)
    return result["messages"]


# --- CLI chat interface ----------------------------------------------------

def main():
    model = ChatDeepSeek(model=MODEL)
    agent = build_agent(model)

    print("Agent ready. Type 'exit' to quit.\n")
    messages = []
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("exit", "quit"):
            break
        messages.append({"role": "user", "content": user_input})
        messages = run_agent(agent, messages)
        print(f"\nDeepSeek: {messages[-1].content}\n")


if __name__ == "__main__":
    main()
