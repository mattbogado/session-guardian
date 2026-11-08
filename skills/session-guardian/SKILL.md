---
name: session-guardian
description: Inspect live conversation token usage, Gemini KV cache hit rates (warm vs. cold context loading), compaction count, and idle gaps, or generate a SESSION_HANDOFF.md file to offload a worn session into a fresh conversation. Use whenever the user asks about session health, context size, cache warmth, compaction readiness, or offloading a session.
---

# Session Guardian Skill

Use this skill to inspect exact session telemetry from the conversation's `transcript.jsonl` and to offload long or multi-compacted sessions cleanly.

## 1. Checking Session Health & Cache Telemetry

Run the bundled analyzer script in `report` mode, passing the current conversation's artifact directory (`<appDataDir>/brain/<conversation-id>`):

```bash
python3 "$HOME/.gemini/config/plugins/session-guardian/scripts/session_monitor.py" \
  --mode report \
  --artifact-dir "<appDataDir>/brain/<conversation-id>"
```

Present the resulting Markdown table directly to the user and highlight:
- **How Context Was Loaded:** What percentage of the prompt came from Gemini's implicit KV cache (`cache_read_tokens`) versus freshly processed tokens (`input_tokens`).
- **Compaction Status:** How many times `/compact` has been run (`compaction_count`) and whether another `/compact` or a full session offload is recommended.

## 2. Offloading a Session (`SESSION_HANDOFF.md`)

When `compaction_count >= 2`, `active_context_tokens >= 140,000`, or when the user asks to offload the session:

1. Create a structured handoff artifact at `<appDataDir>/brain/<conversation-id>/SESSION_HANDOFF.md` (or in the active workspace root if requested) containing:
   - **Goal & Current State:** One-paragraph summary of what has been built or decided.
   - **Key Files & Symbols Modified:** Clickable `file://` links to every important file created or edited.
   - **Architecture & Decisions:** Bullet list of constraints, patterns, and resolved trade-offs so the next session does not re-litigate them.
   - **Next Steps:** Ordered checklist for the fresh conversation.
2. Give the user the clickable link to `SESSION_HANDOFF.md` and instruct them to open a **New Conversation** and `@`-mention `SESSION_HANDOFF.md` to resume with a zero-drift context window.
