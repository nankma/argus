"""
A minimal agent built on the DeepSeek API (OpenAI-compatible Chat Completions).

Demonstrates the client-tool pattern: the model requests a tool call, YOU
execute it locally, and send the result back. This is the pattern you'll
reuse for anything custom: databases, file systems, internal APIs, etc.

Run:
    export DEEPSEEK_API_KEY=sk-e76b727c81364b97a5aa8d4052cda0c2
    python agent.py
"""

import os
import json
from datetime import datetime
import requests
from openai import OpenAI

MODEL = "deepseek-chat"  # use "deepseek-reasoner" for harder reasoning tasks
NOTES_FILE = "notes.jsonl"
NEWS_API_BASE = "https://ok.surf/api/v1"  # OK Surf News API — no key required

client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)

SYSTEM_PROMPT = (
    "You are a news trend analyst. Use the search_news tool to gather recent "
    "headlines (optionally scoped to sections like Technology or Business), "
    "spot recurring themes across them, and write a short trend-report "
    "article that cites the source outlets. If the user asks you to "
    "remember something, use the save_note tool."
)

# --- Tool declarations -------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Save a short note to persistent local storage for later recall.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note text to save."}
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "Fetch recent Google News headlines to spot trends. Optionally "
                "scope to one or more sections: US, World, Business, Technology, "
                "Entertainment, Sports, Science, Health."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Section names to filter by. Omit or leave empty for all sections.",
                    }
                },
                "required": [],
            },
        },
    },
]


# --- Client-side tool execution -----------------------------------------

def execute_tool(name: str, tool_input: dict) -> str:
    """Dispatch a tool call to real Python code and return a string result."""
    if name == "save_note":
        entry = {"note": tool_input["note"], "ts": datetime.now().isoformat()}
        with open(NOTES_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return f"Saved note: {tool_input['note']}"
    if name == "search_news":
        sections = tool_input.get("sections") or []
        if sections:
            resp = requests.post(f"{NEWS_API_BASE}/news-section", json={"sections": sections}, timeout=10)
        else:
            resp = requests.get(f"{NEWS_API_BASE}/news-feed", timeout=10)
        resp.raise_for_status()
        by_section = resp.json()  # {"Technology": [article, ...], "Business": [...], ...}
        total = sum(len(articles) for articles in by_section.values())
        lines = [
            f"- [{section}] {a.get('title')} ({a.get('source')})"
            for section, articles in by_section.items()
            for a in articles[:15]
        ]
        return f"{total} articles found across {len(by_section)} section(s), showing up to 15 per section:\n" + "\n".join(lines)
    return f"Unknown tool: {name}"


# --- The agent loop -------------------------------------------------------

def run_agent(messages: list) -> list:
    """Send messages to DeepSeek, resolve any tool calls, and loop until it
    produces a final text answer. Returns the updated message list."""
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            tools=TOOLS,
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            print(f"\nDeepSeek: {message.content}\n")
            return messages

        for call in message.tool_calls:
            result_text = execute_tool(call.function.name, json.loads(call.function.arguments))
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result_text,
            })
        # loop continues, sending the tool results back to DeepSeek


# --- CLI chat interface ----------------------------------------------------

def main():
    print("Agent ready. Type 'exit' to quit.\n")
    messages = []
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("exit", "quit"):
            break
        messages.append({"role": "user", "content": user_input})
        messages = run_agent(messages)


if __name__ == "__main__":
    main()
