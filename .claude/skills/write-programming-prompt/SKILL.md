---
name: write-programming-prompt
description: Guidelines for writing PROMPT.md files for programming projects (scripts, CLIs, tools). Use when defining specifications for code generation, tool development, or automation workflows. Covers input/output contracts, ID formats, execution modes, and acceptance criteria.
---

# Writing Programming PROMPT.md Files

Prompt files for programming projects define the contract for code/tool generation.

## Core Principle

> **Programming PROMPT.md specifies WHAT the code does, not HOW it's implemented. Include input formats, output contracts, and testable acceptance criteria.**

---

## Programming Prompt Structure

````markdown
---
name: '{Tool Name}'
author: '{Author}'
mode: 'agent'
tools: ['run_in_terminal', 'create_file', 'read_file']
description: '{What this tool does}'
version: '1.0'
---

# {Tool Name}

## Purpose
What this tool accomplishes

## Goals
- Functional goal 1
- Functional goal 2

## Non-Goals
- Explicitly excluded functionality

---

## Inputs

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|

### Input File Format
- Format specification
- Example content

---

## Outputs

### Output Files
| File | Format | Purpose |
|------|--------|---------|

### Output Format
- Format specification
- Example content

---

## Execution Modes

### Interactive Mode
How the tool runs interactively

### Batch Mode
How the tool runs in batch

---

## Error Handling
How errors are handled and reported

---

## Acceptance Criteria
- [ ] Testable criterion 1
- [ ] Testable criterion 2

---

## ALWAYS / NEVER

### ALWAYS
- Critical rule 1

### NEVER
- Critical constraint 1

---

## Examples
Concrete usage examples

---

## Programming-Specific Sections

### Input Parameters
Define all configurable options.

```markdown
## Inputs

### Parameters
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `inputFile` | string | Yes | - | Path to file with IDs |
| `isProd` | boolean | No | true | Use production environment |
| `outputFormat` | enum | Yes | - | A, B, or C format selection |
| `batchSize` | int | No | 50 | Items per batch request |

### Input File Format
Plain text file with one ID per line:
```
ID-0001
ID-0002
ID-0003
```
```

### ID Pattern Detection
Useful for tools that process entity identifiers — document the actual formats this project uses, not a fixed list.

```markdown
### Supported ID Patterns

| Type | Pattern | Regex | Example |
|------|---------|-------|---------|
| {ID type 1} | {shape} | `{regex}` | `{example}` |
| {ID type 2} | {shape} | `{regex}` | `{example}` |
| UUID | 36-char, hyphenated | `^[a-f0-9-]{36}$` | `550e8400-e29b-41d4-a716-446655440000` |
| Numeric ID | 1-8 digits | `^\d{1,8}$` | `12345678` |

### ID Detection Priority
1. Check exact length match
2. Validate character set
3. Fall back to heuristic lookup
```

### Output File Contracts
Specify exact output formats.

```markdown
## Outputs

### Output Files
| File | Format | When Created |
|------|--------|--------------|
| `{input}.output.csv` | CSV/TSV | Always |
| `{input}.output.log` | Text | Always |
| `{input}.errors.txt` | Text | On errors |

### Output Format A: {name this project's actual format}
```csv
{field1},{field2},{field3},{field4}
```

### Output Format B: {name this project's actual format}
```csv
{field1},{field2},{field3},{field4}
```

### Log File Format
```
[YYYY-MM-DD HH:mm:ss] [LEVEL] Message
[2024-01-15 14:30:00] [INFO] Processing ID-0001...
[2024-01-15 14:30:01] [ERROR] Item not found: ID-0099
```
```

### Execution Modes
Define how the tool runs in different contexts.

```markdown
## Execution Modes

### Interactive Mode
1. Prompt for input file path
2. Prompt for environment (Prod/Staging)
3. Prompt for output format (A/B/C)
4. Display progress during execution
5. Show summary on completion

### Batch Mode (headless)
```
run-tool --input ids.txt --prod --format A
```

### Scheduled Mode
For automated execution, ensure:
- No interactive prompts
- Log to file instead of console
- Exit with appropriate error codes
```

### Error Handling Contract
Define what happens when things go wrong.

```markdown
## Error Handling

### Transient Errors (retry)
| Error | Action | Max Retries |
|-------|--------|-------------|
| HTTP 429 | Exponential backoff | 3 |
| HTTP 503 | Wait 5s, retry | 3 |
| Network timeout | Retry immediately | 2 |

### Permanent Errors (log and continue)
| Error | Action |
|-------|--------|
| HTTP 404 | Log to errors file, skip item |
| HTTP 403 | Log authentication failure |
| Invalid ID | Log and skip |

### Fatal Errors (abort)
| Error | Action |
|-------|--------|
| Input file not found | Exit with error |
| Auth failure | Exit with error |
| Disk full | Save partial results, exit |


---

## Acceptance Criteria for Code

Frame as testable conditions.

```markdown
## Acceptance Criteria

### Functional
- [ ] Script builds/compiles without errors
- [ ] All input IDs are processed (count matches)
- [ ] Output file created in correct format
- [ ] Log file contains start/end timestamps
- [ ] Errors logged with specific item ID

### Performance
- [ ] Processes 100 items in < 5 minutes
- [ ] Memory usage < 500MB for 1000 items

### Reliability
- [ ] Partial results saved on interrupt (Ctrl+C)
- [ ] Resumable from last successful item
- [ ] No data loss on transient failures
````

---

## ALWAYS / NEVER for Programming

```markdown
## ALWAYS

- **Always** validate input file exists before processing
- **Always** write results incrementally (not at end)
- **Always** include timestamps in log entries
- **Always** close/release resources explicitly (files, connections, handles)
- **Always** catch specific exceptions, not bare `catch`/`except`

## NEVER

- **Never** hardcode credentials or secrets
- **Never** store secrets in output files
- **Never** skip error logging
- **Never** assume network calls succeed
- **Never** use synchronous I/O for large files
```

---

## Examples Section

Provide concrete test cases.

```markdown
## Examples

### Example 1: Small Batch Test

**Input:** `test-3-ids.txt`
```
ID-0001
ID-0002
INVALID123
```

**Expected Output:** `test-3-ids.output.csv`
```csv
Source1,ID-0001,RETAIL,TypeA
Source2,ID-0002,RETAIL,TypeB
```

**Expected Log:** `test-3-ids.output.log`
```
[INFO] Starting processing of 3 items
[INFO] Processing ID-0001... OK
[INFO] Processing ID-0002... OK
[WARN] Skipping invalid ID: INVALID123
[INFO] Completed: 2 success, 0 failed, 1 skipped
```

### Example 2: Error Recovery

**Scenario:** Network failure after 50 items

**Expected Behavior:**
1. Partial results saved to output file (50 items)
2. Last processed position saved to state file
3. Resume command available: `--resume test.txt`
```

---

## Anti-Patterns

### No input validation examples
**Problem:** Edge cases not covered.
**Solution:** Include malformed input examples and expected handling.

### Missing output format specification
**Problem:** Implementation guesses at format.
**Solution:** Provide exact field order, delimiters, encoding.

### Vague error handling
**Problem:** "Handle errors appropriately."
**Solution:** Define specific error→action mappings.

### No performance expectations
**Problem:** Script runs forever without timeout.
**Solution:** Set specific performance targets.
