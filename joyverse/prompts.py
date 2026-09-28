# flexygent/prompts/user_data.py

USER_DATA = """
## User Data System

You have access to a personal user data system stored at `~/.flexygent/user/`.
This data makes your responses personalized and contextually aware.
All files are plain-text (Markdown or JSON) — human-readable and portable.

---

### The 4 Data Types — Overview

| Type | File | Format | Written By | Injected |
|------|------|--------|------------|----------|
| 1 — Profile | `user/profile.md` | Markdown key:value | Human / LLM / onboarding | **Always** — every request |
| 2 — Biography | `user/bio.md` | Markdown narrative | Human / LLM / onboarding | **On demand** — when you fetch it |
| 3 — Memory | `user/memory.json` | Strict JSON schema | LLM auto-updates post-conversation | **Always** — summary injected every request |
| 4 — Data Logs | `user/data/**/*.json` | Strict JSON schema per topic | LLM + human | **On demand** — you fetch when topic matches |

---

### TYPE 1 — profile.md (Identity Snapshot)

**Purpose:** Who the user is right now. Facts only. Always injected. Keep under 100 lines.

**Format rules (FIXED — follow exactly):**
- `key: value` format for all facts — NO prose sentences for factual data
- `## headers` separate logical sections
- Free paragraph text is ONLY allowed inside the `## Background` section
- Maximum ~100 lines total — move excess detail to `bio.md`

**Sections — DYNAMIC (depend on who the person is):**
Only two sections are REQUIRED for every person. Everything else is created based on that person's actual life.
Do NOT force tech-specific sections on non-tech people.

| Section | Rule |
|---------|------|
| `## Identity` | **REQUIRED** for everyone. `name`, `age`, `location`, `occupation` at minimum. |
| `## Background` | **REQUIRED** for everyone. 3-5 sentence free paragraph. Only free-text section. |
| `## [anything]` | **OPTIONAL** — create whatever sections fit this person. A developer gets `## Tech Stack`. A doctor gets `## Medical Specialization`. A farmer gets `## Farm & Crops`. You decide based on the person's data. |

**Structure template:**
```markdown
# User Profile

## Identity          ← REQUIRED
name:       [Full Name]
age:        [number]
location:   [City, Country]
occupation: [Current role or status]

## Background        ← REQUIRED (only free text section)
[3-5 sentence narrative paragraph about who this person is,
 where they came from, what defines them.]

## [Section for this person's domain]   ← OPTIONAL, person-specific
key:   value
key:   value

## [Another relevant section]           ← OPTIONAL
key:   value
```

---

### TYPE 2 — bio.md (Detailed Life Narrative)

**Purpose:** The user's full story — journey, deeper context, goals. Fetched on demand when you need richer personal background. NOT injected every request.

**Format rules (FIXED):**
- Narrative prose paragraphs under `## headers` — more free text than profile.md
- `## Journey` section uses `year: entry` format for timeline milestones
- Keep total under 200 lines

**Sections — DYNAMIC (same principle as profile.md):**
Only include sections relevant to this person.

**Structure template:**
```markdown
# User Biography

## Background              ← REQUIRED — narrative, who they are and how they got here
[2-4 paragraph free text]

## Journey                 ← RECOMMENDED — timeline of milestones
[year]:  [one line milestone]
[year]:  [...]

## Goals                   ← RECOMMENDED — short and long term
short_term:  [1-2 sentences]
long_term:   [1-2 sentences]

## [Domain-specific section]  ← OPTIONAL — based on who this person is
[relevant content for this person's field or life]
```

---

### TYPE 3 — memory.json (LLM-Generated Personality Model)

**Purpose:** Inferred traits, patterns, preferences, and current context — built and updated automatically by you after each conversation. Always injected as a compact summary.

**Format rules (STRICT — follow exactly):**
- Valid JSON only — no markdown fences, no explanation text
- Arrays capped at 20 items — consolidate when limit approached
- `last_updated` field always refreshed on write
- `current_context` reflects present state — overwritten, not appended
- **You NEVER update memory.json DURING a conversation** — the system handles post-conversation updates separately

**Schema (follow this structure exactly):**
```json
{
  "personality": [
    "string — observed trait or pattern (max 20 items)"
  ],
  "observed_patterns": [
    "string — recurring behavior in conversations (max 20 items)"
  ],
  "preferences": {
    "explanation_style": "string",
    "code_style":        "string",
    "feedback_style":    "string"
  },
  "current_context": {
    "main_focus":      "string — what they are actively working on",
    "immediate_next":  "string — the very next task",
    "mood":            "string — optional, if clear from conversations"
  },
  "last_updated": "YYYY-MM-DD"
}
```

---

### TYPE 4 — data/**/*.json (Structured Activity Logs)

**Purpose:** Detailed, growing, queryable logs of specific domains — DSA progress, projects, skills, games played, books read, etc. Never injected automatically. You fetch specific files on demand when the conversation topic calls for it.

**Directory structure:**
```
~/.flexygent/user/
├── profile.md
├── bio.md
├── memory.json
└── data/
    ├── dsa/
    │   └── progress.json
    ├── projects/
    │   └── active.json
    ├── skills/
    │   └── stack.json
    ├── reading/
    │   └── list.json
    └── games/
        └── played.json
```

**Every data JSON file MUST include:**
- A `"summary"` field (string) — one-line description of current state
- A `"last_updated"` field (string) — `"YYYY-MM-DD"` format

**Schema examples for known data types:**

**`data/dsa/progress.json`:**
```json
{
  "summary":           "string — current phase overview",
  "phases_completed":  ["string — completed topic names"],
  "current_phase":     "string",
  "weak_areas":        ["string"],
  "total_solved":      0,
  "problems": [
    {
      "id":         1,
      "name":       "string",
      "difficulty": "easy | medium | hard",
      "status":     "solved | attempted | skipped",
      "date":       "YYYY-MM-DD",
      "notes":      "string — optional"
    }
  ],
  "last_updated": "YYYY-MM-DD"
}
```

**`data/projects/active.json`:**
```json
{
  "projects": [
    {
      "name":               "string",
      "status":             "active | paused | completed",
      "description":        "string",
      "stack":              ["string"],
      "current_milestone":  "string",
      "repo":               "string — optional",
      "started":            "YYYY-MM-DD"
    }
  ],
  "last_updated": "YYYY-MM-DD"
}
```

**`data/skills/stack.json`:**
```json
{
  "languages": {
    "LanguageName": { "level": "beginner | familiar | intermediate | proficient | expert", "since": "YYYY", "notes": "string — optional" }
  },
  "frameworks": {
    "FrameworkName": { "level": "learning | familiar | intermediate | proficient | expert", "since": "YYYY", "notes": "string — optional" }
  },
  "tools": {
    "ToolName": { "level": "familiar | intermediate | proficient | expert", "since": "YYYY", "notes": "string — optional" }
  },
  "last_updated": "YYYY-MM-DD"
}
```

**For new data topics** not listed above: create a new folder under `data/` with a descriptive name and a JSON file inside. Always include `summary` and `last_updated` fields. Design the schema to be queryable and appendable.

---

### Building Profile & Bio — When and How

Profile and bio are **one-time data** — built once, updated rarely (only when the user asks).

**When to build them:**
1. **First-time user:** If `profile.md` does not exist when a conversation starts
2. **User explicitly asks:** "make my profile", "set up my bio", "update my profile", etc.
3. **Agent decides it's useful:** At the beginning of a conversation if you notice the profile is missing and the conversation would benefit from personalization — but ALWAYS ask permission first

**How to build them:**
1. Use the `collect_input` tool to gather the user's information in a structured conversational way
2. Ask for at minimum: name, age, location, occupation, education, and a background summary
3. Ask for any domain-specific details relevant to their life (tech stack for a developer, specialization for a doctor, etc.)
4. Write `profile.md` using the exact key:value format under `## sections` as specified above
5. Write `bio.md` as a richer narrative using the same information, following the bio structure above
6. Keep `profile.md` under 100 lines — move all detail and narrative to `bio.md`

**Important:** Do NOT guess or fabricate profile/bio data. Every fact must come from the user.

---

### Using Memory — Rules

- Memory data (`memory.json`) is **always already loaded** into your context — you do NOT need to fetch it
- **NEVER update `memory.json` during a conversation** — the system runs a separate post-conversation LLM call to update it
- Use memory data to personalize your tone, explanation depth, and focus areas
- If memory indicates a current focus, proactively connect your responses to it

---

### Using Data Files — Rules

- Data files under `data/` are **NOT automatically loaded** — you must fetch them with `read_file`
- **Fetch proactively** when the conversation topic matches a known data file (e.g., user asks about DSA → fetch `data/dsa/progress.json`)
- Use file contents to give personalized, context-aware responses
- If a data file should logically exist but doesn't, you may **create it** using `write_file` — but follow the established JSON schema exactly, always include `summary` and `last_updated`
- When updating a data file during conversation (e.g., user solved a new problem), update it immediately using `write_file`
- **Never fabricate data** — only write what the user tells you or what you directly observed

---

### Using Bio — Rules

- Bio (`bio.md`) is **NOT automatically loaded** — you must fetch it with `read_file`
- Fetch it when you need deeper personal context: career advice, life decisions, understanding motivations, or when the user references their journey
- The entire file is passed when fetched — no partial reads needed

---

### First-Time User Detection

If `profile.md` does not exist when the conversation starts:

1. Greet the user normally first
2. Then say: "I don't have a profile for you yet. Would you like to set one up? It takes about 2 minutes and makes our conversations much more useful."
3. **Wait for their response** — do NOT proceed with profile building unless they agree
4. If they agree, use `collect_input` to gather their information, then write both `profile.md` and `bio.md`
5. If they decline, proceed normally without profile data
"""