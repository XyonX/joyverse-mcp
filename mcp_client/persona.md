# Virtual Persona: Aarav Mehta

**Username for R2 keys: `aarav-test`**

This is a fictional person used to test the joyverse-mcp server end-to-end.
No part of this is real. All R2 objects created for this persona live under the
prefix `users/aarav-test/` so they can be deleted in one go from the Cloudflare
dashboard.

---

## The person

Aarav Mehta, 24, a backend engineer two years into his career, based in
Bengaluru, India. He is at the stage where he has stopped learning things for
the first time and started caring about depth, direction, and whether his work
matters. He is not unhappy — he is restless in a quiet, productive way.

### Career

- **Current role:** Backend Engineer II at a mid-size fintech firm (Series B, ~400 people)
- **Started:** March 2024, after a 6-month internship that converted
- **Team:** 5 engineers, one staff engineer, one product manager
- **Stack ownership:** payment reconciliation service and the internal ledger API
- **Reports to:** Engineering Manager; no direct reports

### Goals

- **Short term (this quarter):** get promoted to Senior Engineer. Needs two shipped projects and visible ownership of something cross-team.
- **Medium term:** become the person on the team who understands payments properly — not just the code, but the domain.
- **Long term (3-5 years):** staff engineer or engineering manager. Genuinely undecided. Enjoys the craft; tolerates the meetings.

### Current work

Building a reconciliation service that matches internal ledger entries against
bank settlements daily. Currently a month behind schedule because the finance
team changed the settlement format twice. He is the only engineer who
understands the ledger schema well enough to fix it.

Second project: a CLI tool, personal, for tracking practice problems and spaced
repetition review. This is the thing he actually cares about.

### Technical skills

- **Languages:** Python (proficient — his strongest), Go (intermediate), JavaScript (familiar), SQL (proficient)
- **Frameworks:** FastAPI (intermediate), Django (familiar), React (familiar)
- **Tools:** PostgreSQL (proficient), Docker (intermediate), Redis (familiar), AWS (familiar), Git (proficient)
- **Learning:** distributed systems patterns, Go concurrency, Kubernetes internals

### How he thinks

- Prefers being told *why* something is designed a certain way, not just how.
- Dislikes being praised for effort. Responds better to blunt technical feedback.
- Tends to over-research before starting. Loses days to design docs for problems
  that needed a two-hour spike to resolve.
- Under-communicates when stuck. Will try something three times before asking.

---

## Data to seed (R2 keys this persona should produce)

| Key | Content |
|---|---|
| `users/aarav-test/profile.md` | Identity, Background, Current Work, Skills |
| `users/aarav-test/bio.md` | Background, Journey, Goals |
| `users/aarav-test/memory.json` | personality traits, preferences, current_context |
| `users/aarav-test/data/dsa/progress.json` | DSA practice, weak areas |
| `users/aarav-test/data/projects/active.json` | reconciliation + CLI tool |
| `users/aarav-test/data/skills/stack.json` | language/framework/tool levels |
| `users/aarav-test/data/reading/list.json` | books currently reading |

The topic→filename mapping is server-side (`joyverse/config.py`):
`dsa`→`progress.json`, `projects`→`active.json`, `skills`→`stack.json`,
`reading`→`list.json`, `games`→`played.json`. Anything else → `progress.json`.
