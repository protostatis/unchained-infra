# DDM Label Resolution — 3-Phase Saliency Method

DDM extracts a human-readable label for every interactive element on the page. The label appears in `--llm-2pass` output (e.g., `Submit@400,300` or `>Email@200,150`).

The challenge: many elements have no accessible label, or their programmatic identifiers (HTML `name`, `id`) are code strings like `searchMethod` or `form_input_3` that carry no meaning to a human or LLM.

## The Problem

Given a form with 8 radio buttons all named `searchMethod`:

```
<input type="radio" name="searchMethod"> Corporation Name
<input type="radio" name="searchMethod"> LLC Name
<input type="radio" name="searchMethod"> Filing Number
...
```

Naive extraction produces:

```
>searchMethod@94,536|>searchMethod@269,536|>searchMethod@444,536|...
```

All 8 labels are identical. The LLM has no way to click the right one.

## The Solution: 3-Phase Resolution

### Phase 1 — Standard Label Sources

Extract from the element itself, in priority order:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | `aria-label` | `<input aria-label="Search by name">` |
| 2 | `aria-labelledby` | `<input aria-labelledby="lbl1 lbl2">` → concatenated text of referenced elements |
| 3 | `<label for="id">` | `<label for="email">Email</label><input id="email">` |
| 4 | Wrapping `<label>` | `<label><input type="radio"> Option Text</label>` |
| 5 | Selected option | `<select>` → text of the currently selected `<option>` |
| 6 | `placeholder` | `<input placeholder="Enter name">` |
| 7 | `textContent` | Button/link inner text (skipped for `<select>` to avoid options blob) |
| 8 | `title` | `<input title="Search field">` |

If Phase 1 produces a label, it goes to Phase 2. If no label is found, it goes directly to Phase 3.

### Phase 2 — Saliency Gate

Test whether the extracted label is actually human-readable:

```javascript
function _isSalient(s) {
    if (!s || s.length <= 1) return false;
    if (/^[a-z][a-zA-Z0-9]*[A-Z]/.test(s)) return false;  // camelCase
    if (/^[a-z]+[_-][a-z]/.test(s)) return false;          // snake_case, kebab-case
    if (/^\d+$/.test(s)) return false;                      // pure digits
    return true;
}
```

**Rejected labels** (fail saliency → trigger Phase 3):
- `searchMethod` — camelCase
- `form_input` — snake_case
- `btn-submit` — kebab-case
- `42` — pure digits
- `x` — single character

**Accepted labels** (pass saliency → used as-is):
- `Search` — normal word
- `Start a New Search` — phrase
- `Email Address` — multi-word
- `OK` — short but readable

### Phase 3 — Context Probe

When the label fails saliency (or is empty), look outward from the element into the surrounding DOM:

| Probe | What it checks | Example |
|-------|---------------|---------|
| **Adjacent sibling (next)** | Text node or element immediately after | `<input> Corporation Name` → `Corporation Name` |
| **Adjacent sibling (prev)** | Text node or element immediately before | `Email: <input>` → `Email:` |
| **Fieldset legend** | Nearest ancestor `<fieldset>` → `<legend>` | `<fieldset><legend>Search By</legend>...` → `Search By` |
| **Table column header** | If inside `<td>`, find matching `<th>` by column index | `<th>Name</th>...<td><input></td>` → `Name` |
| **Preceding element** | Previous sibling element's text content | `<span>Phone</span><input>` → `Phone` |

The first probe that returns a non-empty string wins. Context probing is general — it doesn't check what type of element it's labeling. A radio button, select, text input, or custom component all get the same treatment.

### Phase 4 — Typed Fallback

If all three phases produce nothing, synthesize a minimal label from the element's type and name:

| Element | Fallback label |
|---------|---------------|
| `<select name="searchMethod">` | `select:searchMethod` |
| `<input type="radio" name="searchMethod">` | `radio:searchMethod` |
| `<input name="query">` | `query` |

This is the last resort. The type prefix (`select:`, `radio:`) at least tells the LLM what kind of element it is.

## Result

The same 8 radio buttons now produce:

```
>Corporation Name@94,536|>LLC Name@269,536|>Filing Number@444,536|...
```

Each label is unique, human-readable, and directly actionable.

## Why This Is General

The saliency gate is the key design decision. Instead of adding element-specific heuristics ("if radio, check sibling; if select, use option text"), the method asks one question: **is this label human-readable?**

- If yes → use it, regardless of element type
- If no → probe context, regardless of element type

This means the same code handles:
- Radio buttons in a group (adjacent text)
- Selects with code-name attributes (fieldset legend)
- Form fields in a table (column headers)
- Any future element type we haven't seen yet
