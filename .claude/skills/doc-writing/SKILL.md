---
name: doc-writing
description: Skill for writing project wiki/documentation pages. Enforces YAML frontmatter on every page, provides format templates for every document type (concept, how-to, TSG, reference, runbook, guideline, architecture, release-notes, entity, source-summary, comparison). Use this skill whenever composing or reviewing wiki/documentation pages.
---

# Document Writing Skill

Every page in this project's documentation library MUST follow these standards.

---

## MANDATORY: YAML Frontmatter

**Every single wiki page** must begin with this YAML frontmatter block. No exceptions.

```yaml
---
title: "Page Title"
description: "1-2 sentence semantic summary explaining what this doc covers and why someone would read it."
type: concept | how-to | tsg | reference | runbook | guideline | architecture | release-notes | entity | source-summary | comparison
service: ServiceName
tags: [Keyword1, Keyword2, Keyword3, Keyword4, Keyword5]
sources:
  - raw/papers/filename.md
  - repo/path/to/source.md
related:
  - "[[related-concept]]"
  - "[[another-page]]"
created: YYYY-MM-DD
updated: YYYY-MM-DD
confidence: high | medium | low
---
```

### Field Definitions

| Field | Required | Description |
|-------|----------|-------------|
| `title` | ✅ YES | Human-readable page title. Use quotes if it contains colons or special chars. |
| `description` | ✅ YES | 1-2 sentence semantic summary (100-250 chars). Explains what the doc covers AND why/when someone would read it. Must help a reader decide "is this the doc I need?" without opening it. |
| `type` | ✅ YES | Document type (determines which template to follow — see below) |
| `service` | ✅ YES | Primary component/service/module this doc belongs to (e.g., Auth, Billing, Notifications) |
| `tags` | ✅ YES | 5–10 domain-specific keyword tags for search/filtering. Array format on one line. |
| `sources` | ✅ YES | List of source files this content was migrated/derived from, as relative repo paths. If original content, use `original` |
| `related` | ⚠️ Recommended | Wiki-link references to related pages. Helps build knowledge graph. |
| `created` | ✅ YES | Date first created (ISO format) |
| `updated` | ✅ YES | Date last modified (ISO format) |
| `confidence` | ✅ YES | How confident we are in this content: `high` = verified from multiple sources or code; `medium` = from a single source or an unverified assistant answer; `low` = inferred or placeholder |

### Confidence Levels

| Level | Meaning | When to Use |
|-------|---------|-------------|
| `high` | Verified, accurate, from authoritative source | Content migrated from official docs, verified against code, or confirmed with citations |
| `medium` | Likely accurate but not fully verified | Single-source content, uncited assistant/search responses, reasonable inference |
| `low` | Placeholder or uncertain | Skeleton content, AI-generated without verification, topic needs expert review |

### Tags Rules

Tags are the **primary search/filter mechanism** for the knowledge query skill. They must be:

| Rule | Example ✅ | Anti-Example ❌ |
|------|-----------|----------------|
| Domain-specific nouns/concepts | `Rate Limiting`, `Dead Letter`, `OAuth` | `troubleshooting`, `steps`, `guide` |
| Searchable terms from CONTENT | `Webhook`, `Retry Policy`, `Postgres` | `pricing overview`, `about pricing` |
| Mix of specific + categorical | `Workflow`, `Stuck`, `InProgress` | `issue`, `problem`, `fix` |
| 5–10 tags per document | 7 tags | 2 tags or 20 tags |
| Array format, one line | `tags: [A, B, C]` | `tags:\n  - A\n  - B` |

---

## Document Types & Templates

### 1. `concept` — What-Is Documents

**Purpose**: Explain what something IS. Introduce a concept, service, or system to someone unfamiliar.

```markdown
---
title: "What is [Concept]"
description: "Explains what [Concept] is, why it exists, and how it fits into the system."
type: concept
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/source.md
related:
  - "[[architecture-page]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# What is [Concept]

## Overview
[2-3 sentences: what is it, why does it exist, who uses it]

## Key Concepts
[Explain the fundamental ideas. Use bullet points or sub-headings.]

### [Sub-concept 1]
[Explanation]

### [Sub-concept 2]
[Explanation]

## How It Fits In
[Where does this sit in the overall system? What depends on it? What does it depend on?]

## Key Terms
| Term | Meaning |
|------|---------|
| [term] | [definition in this context] |

## Further Reading
- [Link to architecture doc]
- [Link to how-to]
```

---

### 2. `how-to` — Step-by-Step Procedures

**Purpose**: Guide someone through a PLANNED action. "I want to do X — how?"

```markdown
---
title: "How To: [Action Description]"
description: "Step-by-step procedure to [action], including prerequisites, commands, and verification steps."
type: how-to
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/source.md
related:
  - "[[related-concept]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# How To: [Action Description]

## Overview
[1-2 sentences: what this procedure accomplishes and when to use it]

## Prerequisites
- [Access/permissions needed]
- [Tools required]
- [Prior knowledge assumed]

## Steps

### Step 1: [Action]
[Detailed instruction with commands/screenshots if applicable]

```
# Example command (use the shell/language this project actually uses)
example-command --parameter value
```

### Step 2: [Action]
[Next step]

### Step 3: [Action]
[Next step]

## Verification
[How to confirm the procedure succeeded]

## Troubleshooting
[Common issues and how to resolve them during this procedure]

## Related
- [Link to concept doc]
- [Link to TSG if this goes wrong]
```

---

### 3. `tsg` — Troubleshooting Guides

**Purpose**: Diagnose and resolve an INCIDENT. Written from the perspective of the engineer RECEIVING the alert/ticket for a service your team owns.

> ⚠️ **Ownership Perspective Rule**: TSGs describe what WE do when we receive this incident for a service we own. "Escalate to another team" IS a valid resolution step. "File the incident back to us" is NEVER correct — we're the ones acting on it.

```markdown
---
title: "TSG: [Specific Scenario Description]"
description: "Diagnoses and resolves [specific scenario] by identifying root cause and providing mitigation steps."
type: tsg
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - incidents/incident-number
  - repo/path/tsg-source.md
related:
  - "[[related-tsg]]"
  - "[[architecture-doc]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# TSG: [Specific Scenario Description]

## Symptoms
[What the on-call engineer observes: alerts, error messages, customer reports]

## Impact
[Who is affected, severity, blast radius]

## Root Cause
[Why this happens — the technical explanation]

## Diagnosis Steps

1. **Check [first thing]**
   ```
   # Command to investigate
   ```
   Expected: [what you should see]

2. **Verify [second thing]**
   [How to confirm root cause]

3. **Confirm scope**
   [Is this one product or many? One market or all?]

## Mitigation / Resolution

1. **[First action]**
   ```
   # Fix command
   ```

2. **[Second action]**
   [Next fix step]

3. **Verify fix**
   [How to confirm the issue is resolved]

## Prevention
[How to prevent recurrence — config change, alert, code fix]

## Escalation
[If mitigation fails: who to contact, what incident/ticket to escalate outward and to whom]

## Related Documents
- [Link to architecture doc]
- [Link to runbook if recurring]
```

**TSG Title Rules**:
- ✅ Scenario-specific: "TSG: Order Stuck in Pending Due to Payment Timeout"
- ✅ Symptom-based: "TSG: Auth Service Returns 401 on Valid Tokens"
- ❌ Generic: "TSG: API Errors"
- ❌ Service-name-only: "TSG: Billing Issues"

---

### 4. `reference` — Lookup Tables

**Purpose**: Data reference — no procedures, just facts to look up.

```markdown
---
title: "[Subject] Reference"
description: "Lookup reference for [subject] including endpoints, error codes, configuration values, and data schemas."
type: reference
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/swagger.json
  - repo/path/enums.cs
related:
  - "[[concept-doc]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# [Subject] Reference

## Overview
[1 sentence: what this reference covers]

## [Main Content]

### Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/api/v3/products` | List products | Bearer |
| POST | `/api/v3/products` | Create product | Bearer |

### Error Codes

| Code | Message | Resolution |
|------|---------|------------|
| 400 | InvalidPayload | Check request body against schema |
| 403 | Forbidden | Verify caller has the required role/scope |

### Configuration Values

| Key | Default | Description |
|-----|---------|-------------|
| `MaxRetries` | 3 | Maximum retry attempts |

## Related
- [Link to how-to for using these endpoints]
```

---

### 5. `runbook` — Recurring Operational Procedures

**Purpose**: Scheduled/recurring maintenance tasks. Different from TSG (planned, not reactive).

```markdown
---
title: "Runbook: [Action Description]"
description: "Recurring operational procedure for [action] with schedule, steps, verification, and rollback instructions."
type: runbook
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/script.ps1
related:
  - "[[tsg-if-fails]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# Runbook: [Action Description]

## Schedule
[When this runs: weekly, monthly, quarterly, on-demand]

## When to Use
[Trigger conditions — what tells you it's time to run this]

## Prerequisites
- [Access needed]
- [Tools/scripts needed]
- [Approvals required]

## Procedure

### Step 1: [Preparation]
```
# Command
```

### Step 2: [Execution]
```
# Command
```

### Step 3: [Verification]
[How to confirm success]

## Rollback
[If something goes wrong, how to undo]

## Post-Procedure
[Notifications, documentation updates, ticket closure]

## Related
- [TSG if this procedure causes issues]
- [Architecture context]
```

---

### 6. `guideline` — Best Practices & Standards

**Purpose**: Prescriptive rules — how things SHOULD be done.

```markdown
---
title: "[Practice/Pattern Name]"
description: "Prescriptive guideline establishing [practice] as the standard approach, with rationale and examples."
type: guideline
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - team-decision/meeting-date
related:
  - "[[concept-doc]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# [Practice/Pattern Name]

## Summary
[1-2 sentences: what rule this establishes and why]

## The Rule
[Clear, prescriptive statement of what to do]

> ✅ **DO**: [correct approach]
> ❌ **DON'T**: [incorrect approach]

## Rationale
[Why this rule exists — what goes wrong without it]

## Examples

### Good ✅
```
// Correct implementation (use this project's actual language)
```

### Bad ❌
```
// Incorrect implementation — explain why
```

## Exceptions
[When it's acceptable to deviate, if ever]

## Related
- [Link to architecture doc explaining the system]
```

---

### 7. `architecture` — System Design Documents

**Purpose**: Explain HOW a system is built — components, data flow, design decisions.

```markdown
---
title: "[System/Component] Architecture"
description: "Technical architecture of [System/Component] covering components, data flow, dependencies, and failure modes."
type: architecture
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/design-doc.md
  - adr/0001-decision.md
related:
  - "[[concept-doc]]"
  - "[[related-service-architecture]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# [System/Component] Architecture

## Overview
[2-3 sentences: what this system does and its role in the larger platform]

## Components

### [Component 1]
[What it does, what it owns]

### [Component 2]
[What it does, what it owns]

## Data Flow
[How data moves through the system — describe the happy path]

```
[Request] → [Component A] → [Component B] → [Store]
                  ↓
          [Async Event] → [Component C]
```

## Dependencies
| Dependency | Type | Purpose |
|-----------|------|---------|
| Product Service | Sync HTTP | Fetch product metadata |
| Service Bus | Async | Event notifications |

## Design Decisions
[Key architectural choices and why — link to ADRs if they exist]

## Failure Modes
[What breaks, what happens when it does, how the system recovers]

## Related
- [Link to TSGs for when this breaks]
- [Link to runbooks for maintenance]
```

---

### 8. `release-notes` — What Changed

**Purpose**: Version history, changelog, breaking changes.

```markdown
---
title: "[Service] Release Notes — [Date/Version]"
description: "Changes in [version/date] including new features, bug fixes, breaking changes, and migration steps."
type: release-notes
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - original
related:
  - "[[previous-release]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# [Service] Release Notes — [Date/Version]

## Summary
[1-2 sentences: what's in this release]

## New Features
- **[Feature name]** — [brief description]

## Bug Fixes
- **[Fix description]** — [what was broken, now resolved]

## Breaking Changes
> [!WARNING]
> [Describe what breaks and what consumers must do]

## Migration Guide
[If breaking changes, how to migrate]

## Known Issues
- [Any known issues in this release]
```

---

### 9. `entity` — Domain Object Documentation

**Purpose**: Document a specific domain entity (API resource, database model, feature).

```markdown
---
title: "[Entity Name]"
description: "Domain entity documentation for [Entity] covering properties, lifecycle states, relationships, and constraints."
type: entity
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - repo/path/model.cs
related:
  - "[[parent-entity]]"
  - "[[consuming-service]]"
created: 2026-05-04
updated: 2026-05-04
confidence: high
---

# [Entity Name]

## Definition
[What this entity represents in the domain]

## Properties
| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique identifier |
| `name` | string | Yes | Display name |

## Lifecycle
[States this entity goes through: created → submitted → certified → published]

## Relationships
- Belongs to: [Parent entity]
- Contains: [Child entities]
- Referenced by: [Services that use this]

## Constraints
[Business rules and validation requirements]
```

---

### 10. `source-summary` — Digested External Source

**Purpose**: Summarize an external document/spec/meeting for library consumption.

```markdown
---
title: "Summary: [Original Document Title]"
description: "Digest of [Original Document] highlighting key points relevant to this project."
type: source-summary
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - original-url-or-path
related:
  - "[[concept-it-informs]]"
created: 2026-05-04
updated: 2026-05-04
confidence: medium
---

# Summary: [Original Document Title]

## Source
- **Original**: [URL or file path]
- **Author**: [Who wrote it]
- **Date**: [When written]

## Key Points
1. [Main takeaway]
2. [Second point]
3. [Third point]

## Relevance
[Why this matters for this project]

## Action Items
- [What we should do based on this]
```

---

### 11. `comparison` — Side-by-Side Analysis

**Purpose**: Compare approaches, versions, services, or options.

```markdown
---
title: "[X] vs [Y]"
description: "Side-by-side comparison of [X] and [Y] with criteria analysis and recommendation for when to use each."
type: comparison
service: ServiceName
tags: [Term1, Term2, Term3, Term4, Term5]
sources:
  - analysis/source
related:
  - "[[concept-x]]"
  - "[[concept-y]]"
created: 2026-05-04
updated: 2026-05-04
confidence: medium
---

# [X] vs [Y]

## Summary
[1 sentence: what's being compared and why]

## Comparison

| Aspect | [X] | [Y] |
|--------|-----|-----|
| [Criterion 1] | [X's approach] | [Y's approach] |
| [Criterion 2] | [X's approach] | [Y's approach] |

## When to Use [X]
[Scenarios where X is the right choice]

## When to Use [Y]
[Scenarios where Y is the right choice]

## Recommendation
[Our team's default choice and why]
```

---

## Quality Rules

1. **Every page starts with frontmatter** — no exceptions
2. **Title in frontmatter must match the H1 heading**
3. **`sources` must be traceable** — never write content without citing where it came from
4. **`confidence` must be honest** — don't mark `high` if you're guessing
5. **`related` should link to at least 1 other page** — orphan knowledge is lost knowledge
6. **Minimum 40 lines** per content doc (frontmatter doesn't count)
7. **Use plain, renderer-agnostic markdown** — no raw HTML unless necessary
8. **Images go in `_images/[service]/`** with descriptive filenames

## Markdown Renderer Notes

- YAML frontmatter is passed through as metadata by most static-site generators (DocFX, MkDocs, Docusaurus, etc.) — verify field names match what your toolchain expects
- `title` field typically sets the page title in the browser tab
- Use `> [!NOTE]`, `> [!WARNING]`, `> [!IMPORTANT]` for callouts — supported by GitHub-flavored markdown and most doc generators
- Internal links use relative paths: `[text](../path/file.md)`
- Anchor links: `[text](#heading-as-lowercase-hyphens)`
