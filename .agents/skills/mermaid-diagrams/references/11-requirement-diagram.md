# Requirement Diagram

Official syntax: https://mermaid.js.org/syntax/requirementDiagram.html

## Starter template

```mermaid
requirementDiagram
  requirement auth_req {
    id: REQ-1
    text: User must authenticate with MFA
    risk: high
    verifymethod: test
  }

  element login_service {
    type: software
  }

  login_service - satisfies -> auth_req
```

## Core syntax

- Use blocks: `requirement`, `functionalRequirement`, `performanceRequirement`, etc.
- Add required fields in requirement blocks (`id`, `text`, `risk`, `verifymethod`).
- Define `element` blocks for systems/components.
- Link items with requirement relations such as `satisfies`, `verifies`, `refines`, `traces`.

## Useful additions

- Use stable requirement IDs aligned with team standards.
- Keep relationship verbs explicit and auditable.

## Common mistakes

- Omitting required requirement fields.
- Using free-form relationship phrases not supported by grammar.
- Modeling implementation sequence instead of traceability.
