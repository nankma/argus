# Gantt

Official syntax: https://mermaid.js.org/syntax/gantt.html

## Starter template

```mermaid
gantt
  title Release Plan
  dateFormat  YYYY-MM-DD
  section Build
    Implement feature :a1, 2026-02-01, 7d
    QA pass           :after a1, 3d
  section Launch
    Rollout           :milestone, 2026-02-12, 0d
```

## Core syntax

- Define `title` and `dateFormat` early.
- Group tasks with `section`.
- Define tasks with id/date/duration syntax.
- Use task flags: `milestone`, `done`, `active`, `crit` as needed.
- Use dependency syntax (`after taskId`) for sequencing.

## Useful additions

- Use `axisFormat` or tick settings for readability on long schedules.
- Exclude non-working days when schedule semantics require it.

## Common mistakes

- Mixing absolute dates and dependencies inconsistently.
- Forgetting durations for non-milestone tasks.
- Overusing critical styling so everything appears critical.
