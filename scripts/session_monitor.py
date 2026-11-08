#!/usr/bin/env python3
"""Session Guardian telemetry analyzer for Antigravity transcripts.

Reads conversation `transcript.jsonl` files to determine:
1. How context was loaded (Warm KV cache hit vs. Cold full re-read).
2. Idle time elapsed since the last model response.
3. Compaction history (how many times `/compact` has compressed the session).
4. Actionable alerts when the user should run `/compact` or offload to a new session.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional


# Threshold constants kept explicit for readability and easy tuning.
COMPACT_TOKEN_THRESHOLD = int(os.environ.get("SG_COMPACT_TOKENS", "80000"))
OFFLOAD_TOKEN_THRESHOLD = int(os.environ.get("SG_OFFLOAD_TOKENS", "140000"))
COMPACT_STEP_THRESHOLD = int(os.environ.get("SG_COMPACT_STEPS", "45"))
OFFLOAD_COMPACTION_COUNT = int(os.environ.get("SG_OFFLOAD_COMPACTIONS", "2"))

WARM_IDLE_MINUTES = 15.0
COLD_IDLE_MINUTES = 60.0
COMPACTION_DROP_RATIO = 0.70
STATE_CACHE_FILE = Path("/tmp/session_guardian_state.json")


class StructuredLogger:
  """Low-cardinality structured logger writing to stderr to preserve stdout for hook JSON."""

  def info(self, context: Dict[str, Any], message: str) -> None:
    if os.environ.get("SG_DEBUG") == "1":
      payload = {"level": "INFO", "message": message, "context": context}
      sys.stderr.write(json.dumps(payload) + "\n")

  def error(self, context: Dict[str, Any], message: str) -> None:
    payload = {"level": "ERROR", "message": message, "context": context}
    sys.stderr.write(json.dumps(payload) + "\n")


logger = StructuredLogger()


@dataclass
class SessionMetrics:
  """Aggregated session health metrics derived from transcript.jsonl."""

  total_steps: int
  user_turns: int
  planner_turns: int
  latest_input_tokens: int
  latest_cache_read_tokens: int
  latest_output_tokens: int
  active_context_tokens: int
  cache_hit_ratio: float
  compaction_count: int
  steps_since_last_compaction: int
  idle_minutes: float
  cache_state: str
  recommendation: str
  alert_level: str


def persist_last_transcript_path(transcript_path: Path) -> None:
  """Records the active transcript path in /tmp so the Sidecar UI can poll it directly."""
  try:
    STATE_CACHE_FILE.write_text(
      json.dumps({"transcript_path": str(transcript_path)}),
      encoding="utf-8",
    )
  except OSError as error:
    logger.info({"error": str(error)}, "Could not write state cache file")


def parse_iso_timestamp(iso_str: str) -> Optional[datetime]:
  """Parses an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
  if not iso_str:
    return None
  try:
    normalized = iso_str.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)
  except ValueError as error:
    logger.error({"iso_str": iso_str, "error": str(error)}, "Failed to parse timestamp")
    return None


def resolve_transcript_path(raw_path: str, artifact_dir: str) -> Optional[Path]:
  """Resolves the actual transcript.jsonl path across Antigravity surfaces."""
  candidates: List[Path] = []
  if raw_path:
    candidates.append(Path(raw_path))
  if artifact_dir:
    candidates.append(Path(artifact_dir) / ".system_generated" / "logs" / "transcript.jsonl")
    candidates.append(Path(artifact_dir) / "transcript.jsonl")

  for candidate in candidates:
    if candidate.is_file():
      persist_last_transcript_path(candidate)
      return candidate
  return None


def load_transcript_steps(transcript_path: Path) -> List[Dict[str, Any]]:
  """Reads valid JSON lines from transcript.jsonl without failing on partial writes."""
  steps: List[Dict[str, Any]] = []
  try:
    with transcript_path.open("r", encoding="utf-8", errors="replace") as handle:
      for line_number, raw_line in enumerate(handle, start=1):
        stripped = raw_line.strip()
        if not stripped:
          continue
        try:
          steps.append(json.loads(stripped))
        except json.JSONDecodeError as error:
          logger.error(
            {"line_number": line_number, "error": str(error)},
            "Skipped malformed JSONL line",
          )
  except OSError as error:
    logger.error(
      {"path": str(transcript_path), "error": str(error)},
      "Unable to read transcript file",
    )
  return steps


def analyze_steps(steps: List[Dict[str, Any]], now_utc: datetime) -> SessionMetrics:
  """Computes cache warmth, context load, and compaction count from raw steps."""
  total_steps = len(steps)
  user_turns = 0
  planner_turns = 0
  latest_input_tokens = 0
  latest_cache_read_tokens = 0
  latest_output_tokens = 0

  previous_active_tokens = 0
  compaction_count = 0
  last_compaction_step_idx = 0
  latest_model_timestamp: Optional[datetime] = None

  for idx, step in enumerate(steps):
    step_type = step.get("type", "")
    if step_type == "USER_INPUT":
      user_turns += 1

    if step_type == "PLANNER_RESPONSE":
      planner_turns += 1
      input_tokens = int(step.get("input_tokens") or 0)
      cache_read_tokens = int(step.get("cache_read_tokens") or 0)
      output_tokens = int(step.get("output_tokens") or 0)
      active_tokens = input_tokens + cache_read_tokens

      # A sharp drop (>30%) in total context tokens between planner responses
      # indicates that `/compact` summarized the prior conversation history.
      if (
        previous_active_tokens > 10000
        and active_tokens > 0
        and active_tokens < (previous_active_tokens * COMPACTION_DROP_RATIO)
      ):
        compaction_count += 1
        last_compaction_step_idx = idx

      if active_tokens > 0:
        latest_input_tokens = input_tokens
        latest_cache_read_tokens = cache_read_tokens
        latest_output_tokens = output_tokens
        previous_active_tokens = active_tokens

      parsed_time = parse_iso_timestamp(step.get("created_at", ""))
      if parsed_time is not None:
        latest_model_timestamp = parsed_time

  active_context_tokens = latest_input_tokens + latest_cache_read_tokens
  cache_hit_ratio = (
    (latest_cache_read_tokens / active_context_tokens)
    if active_context_tokens > 0
    else 0.0
  )

  idle_minutes = 0.0
  if latest_model_timestamp is not None:
    delta_seconds = max(0.0, (now_utc - latest_model_timestamp).total_seconds())
    idle_minutes = delta_seconds / 60.0

  steps_since_last_compaction = total_steps - last_compaction_step_idx
  cache_state = determine_cache_state(
    planner_turns=planner_turns,
    cache_hit_ratio=cache_hit_ratio,
    idle_minutes=idle_minutes,
  )
  alert_level, recommendation = evaluate_alerts(
    active_context_tokens=active_context_tokens,
    compaction_count=compaction_count,
    steps_since_last_compaction=steps_since_last_compaction,
    idle_minutes=idle_minutes,
  )

  return SessionMetrics(
    total_steps=total_steps,
    user_turns=user_turns,
    planner_turns=planner_turns,
    latest_input_tokens=latest_input_tokens,
    latest_cache_read_tokens=latest_cache_read_tokens,
    latest_output_tokens=latest_output_tokens,
    active_context_tokens=active_context_tokens,
    cache_hit_ratio=cache_hit_ratio,
    compaction_count=compaction_count,
    steps_since_last_compaction=steps_since_last_compaction,
    idle_minutes=idle_minutes,
    cache_state=cache_state,
    recommendation=recommendation,
    alert_level=alert_level,
  )


def determine_cache_state(
  planner_turns: int,
  cache_hit_ratio: float,
  idle_minutes: float,
) -> str:
  """Classifies whether the server-side Gemini implicit KV cache is warm, cooling, or cold."""
  if planner_turns == 0:
    return "INITIALIZING (First turn in session)"
  if idle_minutes >= COLD_IDLE_MINUTES:
    return f"COLD_IDLE ({idle_minutes:.0f}m idle — full context re-read expected)"
  if idle_minutes >= WARM_IDLE_MINUTES:
    return f"COOLING ({idle_minutes:.0f}m idle — implicit KV cache at risk of eviction)"
  if planner_turns > 1 and cache_hit_ratio == 0.0:
    return "COLD_RELOAD (0% cache hit on last turn — full history was re-read)"
  return f"WARM ({cache_hit_ratio * 100:.1f}% served from Gemini KV cache)"


def evaluate_alerts(
  active_context_tokens: int,
  compaction_count: int,
  steps_since_last_compaction: int,
  idle_minutes: float,
) -> tuple[str, str]:
  """Determines whether the user should continue, run /compact, or offload to a fresh session."""
  if (
    compaction_count >= OFFLOAD_COMPACTION_COUNT
    or active_context_tokens >= OFFLOAD_TOKEN_THRESHOLD
  ):
    return (
      "OFFLOAD",
      "Offload Recommended: Ask the agent to write a `SESSION_HANDOFF.md` file and start a New Conversation to prevent multi-compaction drift.",
    )

  if (
    active_context_tokens >= COMPACT_TOKEN_THRESHOLD
    or steps_since_last_compaction >= COMPACT_STEP_THRESHOLD
  ):
    return (
      "COMPACT",
      "Ready to Compact: Context window is growing large. Run `/compact` before your next break to shrink re-upload overhead.",
    )

  if idle_minutes >= COLD_IDLE_MINUTES and active_context_tokens >= 35000:
    return (
      "COLD_WARNING",
      "Returning from a long break with a sizable context window. Consider running `/compact` once this turn completes.",
    )

  return (
    "HEALTHY",
    "Session context is lean and healthy.",
  )


def format_hook_banner(metrics: SessionMetrics) -> str:
  """Formats a concise inline Markdown badge for the chat UI."""
  tokens_k = metrics.active_context_tokens / 1000.0
  cached_k = metrics.latest_cache_read_tokens / 1000.0
  new_k = metrics.latest_input_tokens / 1000.0

  badge_icon = "🟢"
  if metrics.alert_level == "OFFLOAD":
    badge_icon = "🔴"
  elif metrics.alert_level in ("COMPACT", "COLD_WARNING"):
    badge_icon = "🟠"
  elif "COLD" in metrics.cache_state or "COOLING" in metrics.cache_state:
    badge_icon = "🔵"

  status_line = (
    f"{badge_icon} **Session Guardian** · "
    f"**Context:** `{tokens_k:.1f}k` tokens (`{cached_k:.1f}k` cached / `{new_k:.1f}k` new) · "
    f"**Cache:** {metrics.cache_state} · "
    f"**Compactions:** `{metrics.compaction_count}`"
  )

  if metrics.alert_level == "HEALTHY" and "WARM" in metrics.cache_state:
    return status_line

  return f"{status_line}\n> **{metrics.recommendation}**"


def format_markdown_report(metrics: SessionMetrics, transcript_path: Optional[Path]) -> str:
  """Builds a comprehensive Markdown report for on-demand session health checks."""
  cache_pct = f"{metrics.cache_hit_ratio * 100:.1f}%"
  path_str = str(transcript_path) if transcript_path else "Not found"
  return "\n".join([
    "### 🛡️ Session Guardian — Telemetry Report",
    "",
    "| Metric | Value |",
    "| :--- | :--- |",
    f"| **Alert Status** | **{metrics.alert_level}** |",
    f"| **Cache Load State** | {metrics.cache_state} |",
    f"| **Active Context Window** | `{metrics.active_context_tokens:,}` tokens |",
    f"| **Cached Tokens (`cache_read_tokens`)** | `{metrics.latest_cache_read_tokens:,}` tokens ({cache_pct}) |",
    f"| **Uncached Input (`input_tokens`)** | `{metrics.latest_input_tokens:,}` tokens |",
    f"| **Last Model Output (`output_tokens`)** | `{metrics.latest_output_tokens:,}` tokens |",
    f"| **Compaction Count (`/compact`)** | `{metrics.compaction_count}` (Threshold: `{OFFLOAD_COMPACTION_COUNT}`) |",
    f"| **Steps (Total / Since Compact)** | `{metrics.total_steps}` total / `{metrics.steps_since_last_compaction}` since compact |",
    f"| **Idle Time Since Last Turn** | `{metrics.idle_minutes:.1f}` minutes |",
    f"| **Transcript Source** | `{path_str}` |",
    "",
    f"**Recommendation:** {metrics.recommendation}",
  ])


def run_hook_mode() -> int:
  """Executes the PreInvocation hook contract: reads stdin JSON and writes stdout JSON."""
  raw_stdin = sys.stdin.read().strip()
  payload: Dict[str, Any] = {}
  if raw_stdin:
    try:
      payload = json.loads(raw_stdin)
    except json.JSONDecodeError as error:
      logger.error({"error": str(error)}, "Invalid JSON received on hook stdin")
      sys.stdout.write("{}\n")
      return 0

  transcript_path = resolve_transcript_path(
    raw_path=str(payload.get("transcriptPath") or ""),
    artifact_dir=str(payload.get("artifactDirectoryPath") or ""),
  )

  if transcript_path is None:
    logger.info({"payload_keys": list(payload.keys())}, "No transcript file found yet for session")
    sys.stdout.write("{}\n")
    return 0

  steps = load_transcript_steps(transcript_path)
  now_utc = datetime.now(timezone.utc)
  metrics = analyze_steps(steps, now_utc)

  banner_markdown = format_hook_banner(metrics)
  agent_note = (
    f"[Session Guardian Telemetry] active_tokens={metrics.active_context_tokens}, "
    f"cache_hit_ratio={metrics.cache_hit_ratio:.2f}, "
    f"compaction_count={metrics.compaction_count}, "
    f"idle_minutes={metrics.idle_minutes:.1f}, "
    f"alert_level={metrics.alert_level}. "
    f"If alert_level is COMPACT or OFFLOAD, briefly mention the recommendation to the user."
  )

  response = {
    "injectSteps": [
      {
        "systemMessage": {
          "systemMessage": "",
          "metadata": {
            "user_markdown": banner_markdown,
          },
        },
      },
      {
        "ephemeralMessage": agent_note,
      },
    ],
  }
  sys.stdout.write(json.dumps(response) + "\n")
  return 0


def run_report_mode(transcript_arg: str, artifact_dir_arg: str) -> int:
  """Prints a human-readable Markdown report for CLI or skill invocation."""
  transcript_path = resolve_transcript_path(transcript_arg, artifact_dir_arg)
  if transcript_path is None:
    sys.stdout.write("No `transcript.jsonl` found at the specified path.\n")
    return 1

  steps = load_transcript_steps(transcript_path)
  metrics = analyze_steps(steps, datetime.now(timezone.utc))
  sys.stdout.write(format_markdown_report(metrics, transcript_path) + "\n")
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description="Session Guardian transcript analyzer.")
  parser.add_argument(
    "--mode",
    choices=["hook", "report"],
    default="report",
    help="Execution mode: 'hook' for PreInvocation JSON, 'report' for Markdown table.",
  )
  parser.add_argument(
    "--transcript",
    default="",
    help="Explicit path to transcript.jsonl.",
  )
  parser.add_argument(
    "--artifact-dir",
    default="",
    help="Artifact directory containing .system_generated/logs/transcript.jsonl.",
  )
  args = parser.parse_args()

  if args.mode == "hook":
    return run_hook_mode()
  return run_report_mode(args.transcript, args.artifact_dir)


if __name__ == "__main__":
  sys.exit(main())
