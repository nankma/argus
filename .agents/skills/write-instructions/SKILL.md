---
name: write-instructions
description: Guidelines for writing .github/instructions/*.instructions.md files. Use when creating new instruction files that provide contextual rules for specific file patterns. Instructions are auto-applied by Copilot when editing matching files.
---

# Writing GitHub Copilot Instructions

Instructions are contextual rules that automatically apply when working on files matching a glob pattern. They live in `.github/instructions/` and guide AI assistants (and humans) toward correct patterns for specific file types.

---

## File Location

```
.github/instructions/<name>.instructions.md
```

---

## Required Format

Every instruction file MUST follow this exact structure:

```markdown
---
applyTo: "<glob-pattern>"
---

- Rule 1
- Rule 2
- Rule 3
```

### Frontmatter

| Field | Required | Description |
|-------|----------|-------------|
| `applyTo` | ✅ | Glob pattern for files this instruction applies to |

**No other frontmatter fields.** Instructions use only `applyTo`.

### Body

- **Bullet list only** — each rule is a `- ` list item
- Rules are imperative and actionable ("Do X", "Never Y", "Always Z")
- Keep each rule to 1–3 sentences max
- Include short code examples inline where helpful (use fenced code blocks within the bullet)
- Order rules from most critical to least critical
- Target 5–15 rules per file (enough to be useful, short enough to be read)

---

## `applyTo` Pattern Examples

| Pattern | Matches |
|---------|---------|
| `"**/toc.yml"` | All toc.yml files anywhere |
| `"wiki/**/*.md"` | All markdown in wiki folder |
| `"**/*.csproj"` | All C# project files |
| `"**/Dockerfile"` | All Dockerfiles |
| `"src/**/*.ts"` | TypeScript in src folder |
| `"**/*.test.ts"` | All test files |
| `"**/docfx.json"` | DocFX config files |
| `"**/*.yml,**/*.yaml"` | All YAML files |

---

## Naming Convention

```
<topic>.instructions.md
```

Examples:
- `tocyml.instructions.md`
- `csharp-tests.instructions.md`
- `wiki-frontmatter.instructions.md`
- `dockerfile.instructions.md`
- `api-controllers.instructions.md`

---

## Writing Good Rules

### ✅ DO

- Be specific and actionable
- Include the "why" briefly if not obvious
- Show short code snippets for complex patterns
- Focus on mistakes that actually happen (learned from bugs/reviews)
- Cover the one thing people always get wrong

### ❌ DON'T

- Write generic advice ("write clean code")
- Repeat what a linter already catches
- Include long prose explanations (this isn't documentation)
- Add rules that contradict each other
- Include more than 15 rules (split into multiple files if needed)

---

## Example: Complete Instruction File

```markdown
---
applyTo: "**/toc.yml"
---

- The root `wiki/toc.yml` is the top navbar only. Never use `href` to reference child toc.yml files.
- Root toc entries use `topicHref` without `href`:
  \```yaml
  - name: Section
    topicHref: section/index.md
  \```
- Each section folder has its own standalone `toc.yml` for the left sidebar.
- Every `.md` file must have a corresponding toc.yml entry. No orphans.
- Values with colons must be quoted: `name: "TSG: Something"`
```

---

## When to Create an Instruction File

Create a new instruction file when:
1. A pattern mistake has been made and you want to prevent it recurring
2. A file type has non-obvious conventions specific to this project
3. Multiple team members (human or AI) will edit files of this type
4. The rules are specific enough that a linter can't enforce them

---

## Verification

After creating an instruction file:
1. Confirm the `applyTo` glob matches the intended files
2. Verify rules don't conflict with existing instructions (check other files in `.github/instructions/`)
3. Test by opening a matching file — the instructions should contextually apply
