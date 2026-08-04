---
name: write-programming-agent
description: Guidelines for writing AGENT.md files for programming tools (scripts, servers, CLI utilities). Use when creating agents that orchestrate code generation, debugging, or tool development workflows. Covers execution patterns, error handling patterns, and tool coordination.
---

# Writing Programming AGENT.md Files

Agent files for programming tools orchestrate code creation, testing, and debugging workflows.

## Core Principle

> **Programming agents decide WHAT to build and HOW to sequence operations. Skills handle the actual coding patterns.**

---

## YAML Frontmatter (Required)

Every AGENT.md file **MUST** include a YAML frontmatter header. This enables future on-demand loading — where agents are discovered and loaded by metadata rather than hardcoded paths.

```yaml
---
name: {agent-name}
description: {When to use this agent. What it orchestrates. What domain it covers.}
type: agent
domain: {programming | documentation | investigation | operations}
scope:
  - {capability 1}
  - {capability 2}
skills:
  - {skill-name-1}
  - {skill-name-2}
---
```

### Field Definitions

| Field | Required | Description |
|-------|----------|-------------|
| `name` | ✅ Yes | Unique identifier, kebab-case (e.g., `incident-investigation`, `tech-doc-writing`) |
| `description` | ✅ Yes | One-line summary starting with "Use when..." — used for agent routing/discovery |
| `type` | ✅ Yes | Always `agent` (distinguishes from skills, prompts) |
| `domain` | ✅ Yes | Primary domain: `programming`, `documentation`, `investigation`, `operations` |
| `scope` | Recommended | List of capabilities this agent handles |
| `skills` | Recommended | Skills this agent depends on (loaded on demand) |

### Example Frontmatter

```yaml
---
name: incident-investigation
description: Use when investigating production incidents. Orchestrates data-gathering tools and scripts for diagnosis.
type: agent
domain: investigation
scope:
  - Incident triage
  - Entity lookups for impacted items
  - Stuck-workflow diagnosis
skills:
  - incident-tools
  - workflow
---
```

```yaml
---
name: tech-doc-writing
description: Use when writing technical documents (TSGs, How To guides, runbooks). Guides interaction mode selection and human-in-the-loop review process.
type: agent
domain: documentation
scope:
  - TSG authoring
  - How To guide authoring
  - Runbook authoring
  - Knowledge extraction from domain experts
skills:
  - write-tsg
  - write-howto
---
```

> **Future Vision:** A root orchestrator agent will read these headers to dynamically route user requests to the right sub-agent — without needing hardcoded paths.

---

## Programming Agent Structure

````markdown
# [Tool Name] Agent

---
name: {agent-name}
description: {Use when...}
type: agent
domain: programming
scope:
  - {capability 1}
skills:
  - {skill-1}
---

## Role
What this agent creates/maintains (scripts, servers, utilities)

## Scope
- ✅ In scope: tool creation, testing, debugging
- ❌ Out of scope: what belongs to other agents

---

## Environment Setup

### Prerequisites
- Runtime requirements (interpreter/compiler version, package manager)
- Required libraries and installation commands
- Authentication/credential setup

### Execution Context
How to run scripts in this environment

---

## Workflow

### Creating New [Tool Type]
1. Step 1
2. Step 2
3. Step 3

### Debugging [Tool Type]
1. Step 1
2. Step 2

---

## Skills Reference
| Need | Skill |
|------|-------|
| {language/framework syntax} | `{skill-name}` |
| Batch processing | `batch-processing` |

---

## Constraints

### ALWAYS
- Test before declaring complete
- Log errors with context

### NEVER
- Hardcode credentials
- Skip validation

---

## Programming-Specific Sections

### Environment Setup
Critical for programming agents—specify exact runtime requirements.

````markdown
## Environment Setup

### Prerequisites
- **Runtime**: exact interpreter/compiler + version (e.g., "runtime X 3.11+")
- **Package manager**: version and installation command
- **Shell**: which shell scripts assume, if any

### Library Installation
```
package-manager add some-http-library
package-manager add some-json-library
```

### Credential Setup
Load secrets from your project's secret store rather than hardcoding them:
```
token = get_secret_from_vault("service-name")
```
```

### Script Execution Patterns
Define how scripts run in your environment.

```markdown
## Script Execution

### Running Scripts
```
run-script path/to/script
```

### Running Servers
```
cd tools/some-server
start-server
```

### Interactive vs Batch Mode
- **Interactive**: Prompts user for input
- **Batch**: Reads from file, writes output incrementally
```

### Error Handling Workflow
Programming agents must handle failures gracefully.

```markdown
## Error Handling Workflow

1. **Capture error** with full stack trace
2. **Log to file** with timestamp and context
3. **Attempt recovery** if transient (retry 3x)
4. **Report partial results** if batch operation
5. **Preserve state** for debugging

### Common Error Patterns
| Error | Cause | Resolution |
|-------|-------|------------|
| HTTP 401 | Token expired | Refresh credentials |
| HTTP 429 | Rate limited | Add delay, reduce batch size |
| File locked | Concurrent access | Wait and retry |
```

---

## Tool Development Workflow

### New Script
```markdown
## Workflow: New Script

1. **Identify requirements** from PROMPT.md or user request
2. **Check existing shared code** for reusable clients/helpers
3. **Create script** using this project's client/helper patterns
4. **Add logging** for debugging
5. **Test with small input** before full batch
6. **Document usage** in script header
```

### New Server/Tool
```markdown
## Workflow: New Server/Tool

1. **Identify capabilities** to expose (list from existing clients/helpers)
2. **Create folder** under `tools/{name}/`
3. **Generate server entry point** with capability definitions
4. **Register** in the project's tool config
5. **Test via direct invocation** or an inspector tool
6. **Update the relevant AGENT.md** with the new tool
```

---

## Skills Coordination

Programming agents delegate to domain skills:

```markdown
## Skills Reference

| Task | Skill | When to Use |
|------|-------|-------------|
| Syntax conversion | `{lang}-syntax` | Converting between two languages/frameworks |
| Service client patterns | `write-serviceclient` | Creating API clients |
| Batch processing | `batch-processing` | Processing 100+ items |
| API verification | `{sdk}-reference` | Checking SDK usage against docs |
```

---

## Testing Constraints

```markdown
## Constraints

### ALWAYS
- **Always** test scripts with small input before batch
- **Always** verify output format matches specification
- **Always** check for compile/syntax errors before runtime testing
- **Always** log execution time for performance baselines

### NEVER
- **Never** commit scripts with hardcoded credentials
- **Never** skip error handling for HTTP calls
- **Never** assume network calls succeed
- **Never** test against production without confirmation
```

---

## Agent Hierarchy for Tools

```markdown
## Agent Hierarchy

```
AGENT.md (root)
├── tools/
│   ├── libs/AGENT.md          → Shared client/helper development
│   ├── servers/AGENT.md       → Server development
│   ├── incident/AGENT.md      → Incident investigation tools
│   └── batch/AGENT.md         → Batch-processing tools
```

Route to sub-agent based on tool type being created.
```

---

## Anti-Patterns

### Missing environment setup
**Problem:** Agent assumes runtime is configured.
**Solution:** Document exact installation commands.

### No testing workflow
**Problem:** Scripts deployed without verification.
**Solution:** Include explicit test steps with expected output.

### Hardcoded paths
**Problem:** Scripts break on different machines.
**Solution:** Use relative paths, environment variables, or config files.

### No error recovery
**Problem:** Batch fails completely on first error.
**Solution:** Log errors, continue processing, report at end.
