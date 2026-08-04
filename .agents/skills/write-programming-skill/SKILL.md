---
name: write-programming-skill
description: Guidelines for writing SKILL.md files for programming patterns (syntax conventions, API clients, data processing). Use when extracting reusable coding patterns, templates, or domain logic into skills. Covers code examples, pattern libraries, and anti-pattern documentation.
---

# Writing Programming SKILL.md Files

Programming skills encapsulate reusable code patterns, templates, and domain expertise.

## Core Principle

> **Programming skills are pattern libraries. They provide HOW to implement, not WHAT to build. Include code examples, not project specifications.**

---

## Programming Skill Structure

```markdown
---
name: '{pattern-name}'
description: '{What patterns this skill provides}. Use when {specific triggers}.'
---

# {Skill Name}

Brief overview of the patterns provided.

---

## Core Patterns

### Pattern 1: {Name}
When to use, code example

### Pattern 2: {Name}
When to use, code example

---

## Code Templates

### Template: {Name}
```
// Template code with placeholders, in this project's actual language
```

---

## Common Mistakes
What to avoid and why

---

## Quick Reference
Cheat sheet for common operations
```

> **IMPORTANT:** Do NOT wrap the generated SKILL.md file in code fences (e.g., ` ```skill ` or ` ```markdown `). The file must start directly with the `---` YAML frontmatter delimiter. Wrapping in code fences breaks editor YAML metadata discovery.

---

## Programming-Specific Sections

### Code Pattern Examples
Show the pattern with minimal context, in whatever language the target project actually uses.

```markdown
## Core Patterns

### Pattern: Async Call with Retry

Use when making network calls that may fail transiently.

```
async function withRetry(action, maxRetries = 3) {
    for (let i = 0; i < maxRetries; i++) {
        try {
            return await action();
        } catch (err) {
            if (i === maxRetries - 1) throw err;
            await delay(2 ** i * 1000);
        }
    }
}

// Usage
const result = await withRetry(() => client.get(url));
```
```

### Syntax Transformations
For skills that document migrating from one language/tool to another. Name the actual source and target — don't invent a fictitious pair.

```markdown
## Syntax Transformations

### Console Output
| {Source} | {Target} |
|----------|----------|
| `old.print(expr)` | `print(expr)` |
| `old.print(expr, "Label")` | `print(f"Label: {expr}")` |

### HTTP Clients
| {Source} | {Target} |
|----------|----------|
| `OldHttpClient()` | `new_http_client()` |
| `client.download_string(url)` | `await client.get_text(url)` |

### Interactive Input
| {Source} | {Target} |
|----------|----------|
| `old.read_line("Prompt")` | `input("Prompt: ")` |
| `old.read_password("Prompt")` | Use a secure input method for this platform |
```

### Template Skeletons
Provide copy-paste starting points, in the target project's actual language.

```markdown
## Code Templates

### Template: Script with Logging

```
# Configuration
log_file = f"{now():%Y%m%d_%H%M%S}.log"

def log(level, message):
    entry = f"[{now():%Y-%m-%d %H:%M:%S}] [{level}] {message}"
    print(entry)
    append_to_file(log_file, entry)

# Main execution
try:
    log("INFO", "Script started")

    # TODO: your code here

    log("INFO", "Script completed successfully")
except Exception as ex:
    log("ERROR", f"Script failed: {ex}")
    raise
```

### Template: Service Client Class

```
class ServiceClient:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}

    async def get(self, endpoint):
        response = await http_get(f"{self.base_url}/{endpoint}", headers=self.headers)
        response.raise_for_status()
        return response.json()
```
```

### Error Pattern Catalog
Document common errors and fixes, in the target project's actual language.

```markdown
## Common Mistakes

### Mistake: Blocking call in async context

**Wrong:**
```
result = client.get(url).result()  # Blocks the event loop
```

**Right:**
```
result = await client.get(url)
```

### Mistake: Bare exception catch

**Wrong:**
```
try:
    ...
except Exception:
    print("Error")  # Swallows details
```

**Right:**
```
try:
    ...
except HttpError as ex:
    log("ERROR", f"HTTP failed: {ex.status_code} - {ex.message}")
```

### Mistake: String concatenation in loops

**Wrong:**
```
result = ""
for item in items:
    result += str(item) + ","  # O(n^2) allocation
```

**Right:**
```
parts = [str(item) for item in items]
result = ",".join(parts)
```
```

---

## Quick Reference Format

Provide scannable cheat sheets, using this project's actual language and package manager.

```markdown
## Quick Reference

### Package Installation
```
package-manager add package-name==version
```

### Common Imports
```
import http_client
import json
import asyncio
```

### File Operations
| Operation | Code |
|-----------|------|
| Read all text | `read_file(path)` |
| Read lines | `read_lines(path)` |
| Write text | `write_file(path, content)` |
| Append line | `append_file(path, line + "\n")` |
| Check exists | `path_exists(path)` |

### JSON Operations
| Operation | Code |
|-----------|------|
| Serialize | `json.dumps(obj)` |
| Deserialize | `json.loads(text)` |
| Pretty print | `json.dumps(obj, indent=2)` |
```

---

## Skill Organization

### Single-Domain Skill
```
some-syntax/
├── SKILL.md          # All patterns in one file
```

### Multi-Domain Skill (with references)
```
api-patterns/
├── SKILL.md          # Overview + navigation
└── references/
    ├── rest-api.md   # REST patterns
    ├── graphql.md    # GraphQL patterns
    └── grpc.md       # gRPC patterns
```

---

## What Makes a Good Programming Skill

### High Value
- Patterns you copy-paste frequently
- Tricky syntax conversions with edge cases
- Error handling patterns for specific APIs
- Authentication flows for internal services

### Low Value (Don't Create)
- Basic language features (Codex already knows mainstream languages)
- Standard library documentation
- Single-use project-specific code

---

## Anti-Patterns

### Project context in skill
**Problem:** Skill describes specific project requirements.
**Solution:** Keep skills generic. Project context belongs in PROMPT.md.

### No code examples
**Problem:** Text descriptions without runnable code.
**Solution:** Every pattern needs a concrete example.

### Outdated syntax
**Problem:** Examples use deprecated APIs.
**Solution:** Use current best practices, note version requirements.

### Missing error cases
**Problem:** Only shows happy path.
**Solution:** Include error handling in examples.
