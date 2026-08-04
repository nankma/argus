# Diagram Selection Guide

Official syntax index: https://mermaid.js.org/intro/syntax-reference.html

## Pick by intent

- Show process or decisions: `flowchart`
- Show interactions over time: `sequenceDiagram`
- Show object model and inheritance: `classDiagram`
- Show lifecycle or finite states: `stateDiagram-v2`
- Show data entities and cardinality: `erDiagram`
- Show qualitative user sentiment over steps: `journey`
- Show schedule and duration: `gantt`
- Show category proportions: `pie`
- Show two-axis strategic placement: `quadrantChart`
- Show requirements and traceability: `requirementDiagram`
- Show git history story: `gitGraph`
- Show C4 architecture model: `C4Context`, `C4Container`, `C4Component`, `C4Dynamic`, `C4Deployment`
- Show hierarchy or brainstorming tree: `mindmap`
- Show chronological events: `timeline`
- Show message flow with ZenUML style: `zenuml`
- Show weighted flow between nodes: `sankey`
- Show numeric comparison trends: `xychart`
- Show structured blocks/layout: `block`
- Show protocol/header field layout: `packet`
- Show work tracking board: `kanban`
- Show infrastructure groups and services: `architecture-beta`
- Show multi-axis profile comparison: `radar-beta`
- Show hierarchical value distribution: `treemap-beta`

## Fast fallback rules

- Default to `flowchart` when unsure and no strict data model is required.
- Prefer `sequenceDiagram` over `flowchart` when actors and message order matter.
- Prefer `erDiagram` over `classDiagram` for database-centric conversations.
- Prefer `timeline` over `gantt` when exact duration/task dependencies are unnecessary.
- Avoid mixing two diagram grammars in one Mermaid block.
