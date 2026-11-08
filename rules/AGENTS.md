# Session Guardian Rules

When the `session-guardian` plugin is enabled:

1. **Honor Hook Alerts:** If a `[Session Guardian Telemetry]` ephemeral message reports `alert_level=COMPACT`, remind the user at the end of your turn that they can run `/compact` to compress the context window. If it reports `alert_level=OFFLOAD`, recommend offloading the session into a `SESSION_HANDOFF.md` file and starting a New Conversation.
2. **On-Demand Telemetry:** Whenever the user asks how their context was loaded, whether the cache is warm or cold, or whether they should compact or offload, activate the `session-guardian` skill and run `session_monitor.py --mode report` against the current conversation's `transcript.jsonl`.
