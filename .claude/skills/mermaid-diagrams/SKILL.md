---
name: mermaid-diagrams
description: Create, edit, and troubleshoot Mermaid diagram syntax. Use when a user asks to generate Mermaid code, convert notes/specs into Mermaid diagrams, pick a Mermaid diagram type, style Mermaid output, or fix Mermaid parse/render errors across flowchart, sequence, class, state, ER, journey, gantt, pie, quadrant, requirement, gitGraph, C4, mindmap, timeline, ZenUML, sankey, xychart, block, packet, kanban, architecture, radar, and treemap diagrams.
---

# Mermaid Diagrams

Use this skill to produce valid Mermaid syntax with minimal retries.

## Workflow

1. Identify the diagram goal and audience.
2. Read [references/00-diagram-selection.md](references/00-diagram-selection.md) to pick the correct diagram type.
3. Always read [references/01-common-syntax-and-config.md](references/01-common-syntax-and-config.md).
4. Load only the specific diagram reference file you need.
5. Draft the smallest valid diagram first, then add labels, styling, and config.
6. Run the parse checklist from the common syntax guide before returning output.

## Diagram Reference Map

- Flowchart: [references/02-flowchart.md](references/02-flowchart.md)
- Sequence Diagram: [references/03-sequence-diagram.md](references/03-sequence-diagram.md)
- Class Diagram: [references/04-class-diagram.md](references/04-class-diagram.md)
- State Diagram: [references/05-state-diagram.md](references/05-state-diagram.md)
- Entity Relationship Diagram: [references/06-er-diagram.md](references/06-er-diagram.md)
- User Journey: [references/07-user-journey.md](references/07-user-journey.md)
- Gantt: [references/08-gantt.md](references/08-gantt.md)
- Pie Chart: [references/09-pie-chart.md](references/09-pie-chart.md)
- Quadrant Chart: [references/10-quadrant-chart.md](references/10-quadrant-chart.md)
- Requirement Diagram: [references/11-requirement-diagram.md](references/11-requirement-diagram.md)
- GitGraph: [references/12-gitgraph.md](references/12-gitgraph.md)
- C4: [references/13-c4.md](references/13-c4.md)
- Mindmap: [references/14-mindmap.md](references/14-mindmap.md)
- Timeline: [references/15-timeline.md](references/15-timeline.md)
- ZenUML: [references/16-zenuml.md](references/16-zenuml.md)
- Sankey: [references/17-sankey.md](references/17-sankey.md)
- XY Chart: [references/18-xy-chart.md](references/18-xy-chart.md)
- Block Diagram: [references/19-block-diagram.md](references/19-block-diagram.md)
- Packet: [references/20-packet.md](references/20-packet.md)
- Kanban: [references/21-kanban.md](references/21-kanban.md)
- Architecture: [references/22-architecture.md](references/22-architecture.md)
- Radar: [references/23-radar.md](references/23-radar.md)
- Treemap: [references/24-treemap.md](references/24-treemap.md)

## Output Rules

- Return Mermaid code in a fenced `mermaid` block.
- Include one short note about assumptions when requirements are ambiguous.
- Keep labels concise and deterministic.
- Prefer diagram readability over decorative styling.
- If renderer support may be version-sensitive, call it out.

## Source of Truth

Use Mermaid official syntax docs as canonical reference:
- https://mermaid.js.org/intro/syntax-reference.html
