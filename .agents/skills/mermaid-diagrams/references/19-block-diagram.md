# Block Diagram

Official syntax: https://mermaid.js.org/syntax/block.html

## Starter template

```mermaid
block
  columns 1
    db((\"DB\"))
    spacer<[" "]>(down)
    block:core
      A
      B["A wide block"]
      C
```

## Core syntax

- Start with `block`.
- Use `columns` and block containers for layout.
- Declare blocks and optional IDs/labels.
- Use arrows for relationships between blocks.
- Apply `style` or `classDef` when emphasis is needed.

## Useful additions

- Keep layout simple before introducing nested blocks.
- Use descriptive labels for non-technical audiences.

## Common mistakes

- Treating block grammar exactly like flowchart grammar.
- Over-nesting blocks, which reduces readability.
- Depending on unsupported renderer versions.
