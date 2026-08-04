# Kanban

Official syntax: https://mermaid.js.org/syntax/kanban.html

## Starter template

```mermaid
kanban
  todo[Todo]
    task1[Define API contract]
  doing[Doing]
    task2[Implement endpoint]
  done[Done]
    task3[Ship docs]
```

## Core syntax

- Start with `kanban`.
- Define columns and tasks by indentation.
- Keep task IDs stable if you need cross-references.
- Use config options for ticket link templates when needed.

## Useful additions

- Keep columns aligned to your team's actual workflow.
- Use short task titles and move detail to external tracker links.

## Common mistakes

- Treating kanban as gantt with dates/dependencies.
- Creating too many workflow columns.
- Omitting stable IDs when task linking is needed.
