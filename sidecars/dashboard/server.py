#!/usr/bin/env python3
"""Telemetry collector and Offload/Compact action runner for Session Guardian.

Reuses core transcript analysis from `scripts/session_monitor.py` (DRY) and provides:
1. `--json`: Detailed Claude-style context breakdown, active conversation auto-detection
   via `~/.gemini/antigravity/annotations/*.pbtxt` (`last_user_view_time`) + transcript mtime,
   and recent conversation list.
2. `--compact`: Sends `/compact` via `agentapi` and copies `/compact` to clipboard.
3. `--offload`: Summarizes the active conversation from `transcript.jsonl`, writes
   `SESSION_HANDOFF.md`, and starts a new Antigravity chat via `agentapi new-conversation`.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import session_monitor as sm  # noqa: E402


AGY_DIR = Path.home() / ".gemini" / "antigravity"
BRAIN_DIR = AGY_DIR / "brain"
ANNOTATIONS_DIR = AGY_DIR / "annotations"
AGENTAPI_BIN = AGY_DIR / "bin" / "agentapi"
MAX_CONTEXT_WINDOW = 1_000_000
AUTOCOMPACT_BUFFER_TOKENS = 33_000


def load_annotations_map() -> Dict[str, Dict[str, Any]]:
  """Parses `~/.gemini/antigravity/annotations/*.pbtxt` for titles and `last_user_view_time`."""
  result: Dict[str, Dict[str, Any]] = {}
  try:
    if not ANNOTATIONS_DIR.is_dir():
      return result
    for pbtxt_file in ANNOTATIONS_DIR.iterdir():
      if pbtxt_file.suffix != ".pbtxt":
        continue
      conv_id = pbtxt_file.stem
      try:
        raw = pbtxt_file.read_text(encoding="utf-8", errors="replace")
      except OSError:
        continue

      title_match = re.search(r'title:\s*"([^"]+)"', raw)
      view_match = re.search(
        r"last_user_view_time:\s*\{\s*seconds:\s*(\d+)(?:\s+nanos:\s*(\d+))?",
        raw,
      )
      archived = "archived:true" in raw.replace(" ", "")

      view_ts = 0.0
      if view_match:
        secs = float(view_match.group(1))
        nanos = float(view_match.group(2) or 0)
        view_ts = secs + (nanos / 1_000_000_000.0)

      result[conv_id] = {
        "title": title_match.group(1) if title_match else "",
        "last_view_ts": view_ts,
        "archived": archived,
      }
  except OSError as error:
    sm.logger.info({"error": str(error)}, "Annotations scan restricted in sandbox")
  return result


def discover_conversations(
  requested_id: Optional[str] = None,
) -> Tuple[Optional[Path], List[Dict[str, Any]]]:
  """Finds the active conversation transcript (reacting immediately to UI tab switches)
  and returns a list of recent conversations for the selector."""
  annotations = load_annotations_map()
  candidates: List[Dict[str, Any]] = []

  try:
    if BRAIN_DIR.is_dir():
      for child in BRAIN_DIR.iterdir():
        if not child.is_dir():
          continue
        conv_id = child.name
        transcript_candidate = child / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_candidate.is_file():
          continue

        try:
          t_mtime = transcript_candidate.stat().st_mtime
        except OSError:
          t_mtime = 0.0

        ann = annotations.get(conv_id, {})
        if ann.get("archived"):
          continue

        view_ts = float(ann.get("last_view_ts") or 0.0)
        # Activity score combines the latest UI view/switch timestamp and transcript write time.
        activity_ts = max(t_mtime, view_ts)
        candidates.append({
          "id": conv_id,
          "title": str(ann.get("title") or ""),
          "transcript_path": transcript_candidate,
          "activity_ts": activity_ts,
          "view_ts": view_ts,
          "t_mtime": t_mtime,
        })
  except OSError as error:
    sm.logger.info({"error": str(error)}, "Brain directory scan restricted in sandbox")

  # Sort newest-viewed or newest-updated conversation first.
  candidates.sort(key=lambda item: item["activity_ts"], reverse=True)

  recent_list: List[Dict[str, Any]] = []
  for item in candidates[:10]:
    cid = item["id"]
    title = item["title"] or f"Session {cid[:8]}"
    recent_list.append({
      "id": cid,
      "title": title,
    })

  # 1. If a specific conversation was requested by the UI, honor it first.
  if requested_id and requested_id != "auto":
    for item in candidates:
      if item["id"] == requested_id:
        return item["transcript_path"], recent_list
    direct_path = BRAIN_DIR / requested_id / ".system_generated" / "logs" / "transcript.jsonl"
    if direct_path.is_file():
      return direct_path, recent_list

  # 2. Otherwise use the highest activity_ts conversation (updates on every chat switch!).
  if candidates:
    return candidates[0]["transcript_path"], recent_list

  # 3. Fallback for sandboxed CLI tests where directory listing is blocked.
  if sm.STATE_CACHE_FILE.is_file():
    try:
      cached = json.loads(sm.STATE_CACHE_FILE.read_text(encoding="utf-8"))
      cached_path = Path(str(cached.get("transcript_path") or ""))
      if cached_path.is_file():
        cid = cached_path.parents[2].name
        return cached_path, [{"id": cid, "title": f"Session {cid[:8]}"}]
    except (OSError, json.JSONDecodeError) as error:
      sm.logger.info({"error": str(error)}, "State cache read skipped")

  return None, []


def extract_clean_user_text(raw_content: str) -> str:
  """Extracts human-readable user prompt text from raw USER_INPUT step content."""
  if not raw_content:
    return ""

  talker_matches = re.findall(r'user:\s*"([^"]+)"', raw_content)
  if talker_matches:
    return talker_matches[-1].strip()

  request_match = re.search(
    r"<USER_REQUEST>(.*?)</USER_REQUEST>",
    raw_content,
    flags=re.DOTALL,
  )
  body = request_match.group(1) if request_match else raw_content
  body = re.sub(r"<voice_instructions>.*?</voice_instructions>", "", body, flags=re.DOTALL)
  body = re.sub(r"<talker_agent_context>.*?</talker_agent_context>", "", body, flags=re.DOTALL)
  body = re.sub(r"<[^>]+>", "", body).strip()
  return " ".join(body.split())


def extract_conversation_digest(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
  """Extracts user requests, touched files, and the latest assistant summary from steps."""
  user_prompts: List[str] = []
  files_touched: List[str] = []
  last_assistant_reply = ""

  for step in steps:
    step_type = step.get("type", "")
    if step_type == "USER_INPUT":
      cleaned = extract_clean_user_text(str(step.get("content") or ""))
      if cleaned and cleaned not in user_prompts:
        user_prompts.append(cleaned)

    if step_type == "PLANNER_RESPONSE":
      content = str(step.get("content") or "").strip()
      if content:
        last_assistant_reply = content

      for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
          continue
        tool_name = str(call.get("name") or "")
        args = call.get("args") or {}
        if tool_name in ("write_file", "edit_file") and isinstance(args, dict):
          raw_path = str(args.get("Path") or "").strip('"')
          if raw_path and raw_path not in files_touched:
            files_touched.append(raw_path)

  return {
    "user_prompts": user_prompts,
    "files_touched": files_touched,
    "last_assistant_reply": last_assistant_reply,
  }


def compute_token_breakdown(active_tokens: int, cached_tokens: int, input_tokens: int) -> Dict[str, Any]:
  """Splits active context tokens into Claude-style categories out of 1M capacity."""
  system_prompt_tokens = min(12_000, int(active_tokens * 0.08)) if active_tokens > 0 else 12_000
  skills_tokens = min(9_900, int(active_tokens * 0.06)) if active_tokens > 0 else 9_900
  mcp_tokens = min(7_300, int(active_tokens * 0.05)) if active_tokens > 0 else 7_300
  rules_tokens = min(2_800, int(active_tokens * 0.02)) if active_tokens > 0 else 2_800

  overhead_tokens = system_prompt_tokens + skills_tokens + mcp_tokens + rules_tokens
  messages_tokens = max(0, active_tokens - overhead_tokens)

  used_percent = round((active_tokens / MAX_CONTEXT_WINDOW) * 100.0, 1)
  free_space_tokens = max(0, MAX_CONTEXT_WINDOW - active_tokens - AUTOCOMPACT_BUFFER_TOKENS)

  def pct(val: int) -> float:
    return round((val / MAX_CONTEXT_WINDOW) * 100.0, 1)

  return {
    "max_tokens": MAX_CONTEXT_WINDOW,
    "used_percent": used_percent,
    "messages_tokens": messages_tokens,
    "messages_pct": pct(messages_tokens),
    "cached_tokens": cached_tokens,
    "cached_pct": pct(cached_tokens),
    "uncached_tokens": input_tokens,
    "uncached_pct": pct(input_tokens),
    "system_prompt_tokens": system_prompt_tokens,
    "system_prompt_pct": pct(system_prompt_tokens),
    "skills_tokens": skills_tokens,
    "skills_pct": pct(skills_tokens),
    "mcp_tokens": mcp_tokens,
    "mcp_pct": pct(mcp_tokens),
    "rules_tokens": rules_tokens,
    "rules_pct": pct(rules_tokens),
    "autocompact_buffer_tokens": AUTOCOMPACT_BUFFER_TOKENS,
    "autocompact_buffer_pct": pct(AUTOCOMPACT_BUFFER_TOKENS),
    "free_space_tokens": free_space_tokens,
    "free_space_pct": pct(free_space_tokens),
  }


def build_offload_prompt(
  conversation_id: str,
  metrics: sm.SessionMetrics,
  digest: Dict[str, Any],
  handoff_file_path: Path,
) -> str:
  """Constructs the prompt summary used to seed the offloaded new conversation."""
  prompts = digest.get("user_prompts") or []
  origin_goal = prompts[0] if prompts else "Continue previous Antigravity session"
  recent_goals = prompts[-3:] if len(prompts) > 1 else prompts
  files_touched = digest.get("files_touched") or []

  recent_bullets = "\n".join(f"- {item}" for item in recent_goals)
  file_bullets = (
    "\n".join(f"- `{path}`" for path in files_touched[-8:])
    if files_touched
    else "- No files modified yet"
  )

  return (
    f"@[{handoff_file_path}]\n\n"
    f"### Context Offloaded from Session `{conversation_id[:8]}`\n"
    f"- **Original Goal:** {origin_goal}\n"
    f"- **Previous Session Stats:** `{metrics.active_context_tokens:,}` tokens "
    f"(`{metrics.cache_hit_ratio * 100:.1f}%` KV cache hit), "
    f"`{metrics.compaction_count}` compactions, `{metrics.total_steps}` steps.\n\n"
    f"#### Recent Requests in Original Chat\n"
    f"{recent_bullets}\n\n"
    f"#### Key Files Created / Modified\n"
    f"{file_bullets}\n\n"
    f"Please review `@[{handoff_file_path}]` and continue from where we left off."
  )


def write_handoff_markdown(
  conversation_id: str,
  metrics: sm.SessionMetrics,
  digest: Dict[str, Any],
  handoff_path: Path,
) -> None:
  """Writes a comprehensive SESSION_HANDOFF.md file to the conversation artifact directory."""
  prompts = digest.get("user_prompts") or []
  files_touched = digest.get("files_touched") or []
  last_reply = str(digest.get("last_assistant_reply") or "").strip()
  if len(last_reply) > 1200:
    last_reply = last_reply[:1200] + "..."

  prompt_lines = "\n".join(f"{idx}. {text}" for idx, text in enumerate(prompts, start=1))
  file_lines = "\n".join(f"- [{Path(p).name}](file://{p}) (`{p}`)" for p in files_touched)

  content = (
    f"# Session Handoff — `{conversation_id}`\n\n"
    f"## 1. Telemetry Snapshot at Offload\n"
    f"- **Active Context Window:** `{metrics.active_context_tokens:,}` tokens\n"
    f"- **Gemini KV Cache Hit Rate:** `{metrics.cache_hit_ratio * 100:.1f}%` ({metrics.cache_state})\n"
    f"- **Compactions Run:** `{metrics.compaction_count}`\n"
    f"- **Total Steps:** `{metrics.total_steps}`\n\n"
    f"## 2. User Request Trajectory\n"
    f"{prompt_lines or 'No user prompts recorded.'}\n\n"
    f"## 3. Files Created or Modified\n"
    f"{file_lines or 'No files recorded.'}\n\n"
    f"## 4. Latest Assistant State\n"
    f"{last_reply or 'No assistant response recorded.'}\n"
  )
  try:
    handoff_path.write_text(content, encoding="utf-8")
  except OSError as error:
    sm.logger.error({"path": str(handoff_path), "error": str(error)}, "Failed writing handoff file")


def collect_live_telemetry(requested_id: Optional[str] = None) -> Dict[str, Any]:
  """Loads the active or requested conversation transcript and returns JSON telemetry."""
  transcript_path, recent_conversations = discover_conversations(requested_id)
  if transcript_path is None:
    breakdown = compute_token_breakdown(0, 0, 0)
    return {
      "found": False,
      "conversation_id": "none",
      "conversation_title": "No active chat",
      "recent_conversations": [],
      "alert_level": "HEALTHY",
      "cache_state": "Warm (Waiting)",
      "active_context_tokens": 0,
      "latest_cache_read_tokens": 0,
      "latest_input_tokens": 0,
      "latest_output_tokens": 0,
      "cache_hit_ratio": 0.0,
      "compaction_count": 0,
      "total_steps": 0,
      "steps_since_last_compaction": 0,
      "idle_minutes": 0.0,
      "recommendation": "No active session transcript found.",
      "compact_threshold": sm.COMPACT_TOKEN_THRESHOLD,
      "offload_threshold": sm.OFFLOAD_TOKEN_THRESHOLD,
      "offload_compactions": sm.OFFLOAD_COMPACTION_COUNT,
      "compact_available": False,
      "breakdown": breakdown,
      "offload_preview": "No active session to summarize.",
      "files_count": 0,
      "prompts_count": 0,
    }

  conversation_id = transcript_path.parents[2].name
  artifact_dir = transcript_path.parents[2]
  steps = sm.load_transcript_steps(transcript_path)
  metrics = sm.analyze_steps(steps, datetime.now(timezone.utc))
  digest = extract_conversation_digest(steps)
  breakdown = compute_token_breakdown(
    active_tokens=metrics.active_context_tokens,
    cached_tokens=metrics.latest_cache_read_tokens,
    input_tokens=metrics.latest_input_tokens,
  )
  handoff_path = artifact_dir / "SESSION_HANDOFF.md"
  offload_preview = build_offload_prompt(conversation_id, metrics, digest, handoff_path)

  # Resolve human-readable title for the active conversation.
  conv_title = ""
  for item in recent_conversations:
    if item["id"] == conversation_id and not item["title"].startswith("Session "):
      conv_title = item["title"]
      break
  if not conv_title:
    prompts = digest.get("user_prompts") or []
    if prompts:
      first_p = prompts[0]
      conv_title = (first_p[:40] + "...") if len(first_p) > 40 else first_p
    else:
      conv_title = f"Session {conversation_id[:8]}"

  compact_available = (
    metrics.active_context_tokens >= sm.COMPACT_TOKEN_THRESHOLD
    or metrics.steps_since_last_compaction >= 20
  )

  return {
    "found": True,
    "conversation_id": conversation_id,
    "conversation_title": conv_title,
    "recent_conversations": recent_conversations,
    "alert_level": metrics.alert_level,
    "cache_state": metrics.cache_state,
    "active_context_tokens": metrics.active_context_tokens,
    "latest_cache_read_tokens": metrics.latest_cache_read_tokens,
    "latest_input_tokens": metrics.latest_input_tokens,
    "latest_output_tokens": metrics.latest_output_tokens,
    "cache_hit_ratio": round(metrics.cache_hit_ratio * 100.0, 1),
    "compaction_count": metrics.compaction_count,
    "total_steps": metrics.total_steps,
    "steps_since_last_compaction": metrics.steps_since_last_compaction,
    "idle_minutes": round(metrics.idle_minutes, 1),
    "recommendation": metrics.recommendation,
    "compact_threshold": sm.COMPACT_TOKEN_THRESHOLD,
    "offload_threshold": sm.OFFLOAD_TOKEN_THRESHOLD,
    "offload_compactions": sm.OFFLOAD_COMPACTION_COUNT,
    "compact_available": compact_available,
    "breakdown": breakdown,
    "offload_preview": offload_preview,
    "files_count": len(digest.get("files_touched") or []),
    "prompts_count": len(digest.get("user_prompts") or []),
  }


def copy_to_clipboard(text: str) -> bool:
  """Copies text to the macOS clipboard via pbcopy."""
  try:
    proc = subprocess.run(
      ["pbcopy"],
      input=text.encode("utf-8"),
      check=False,
      timeout=2,
    )
    return proc.returncode == 0
  except OSError:
    return False


def execute_compact_action(requested_id: Optional[str] = None) -> Dict[str, Any]:
  """Triggers compaction or prepares `/compact` for the active conversation."""
  transcript_path, _ = discover_conversations(requested_id)
  copied = copy_to_clipboard("/compact")
  if transcript_path is None:
    return {
      "ok": False,
      "message": "Copied `/compact` to clipboard (no active transcript path detected).",
    }

  conversation_id = transcript_path.parents[2].name
  agentapi_sent = False
  if AGENTAPI_BIN.is_file():
    try:
      result = subprocess.run(
        [str(AGENTAPI_BIN), "send-message", conversation_id, "/compact"],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
      )
      agentapi_sent = result.returncode == 0 and '"error"' not in result.stdout
    except OSError:
      agentapi_sent = False

  if agentapi_sent:
    return {
      "ok": True,
      "message": f"Sent `/compact` to session {conversation_id[:8]} and copied to clipboard.",
    }
  return {
    "ok": True,
    "copied": copied,
    "message": "Copied `/compact` to clipboard — press Cmd+L then Cmd+V in chat to compact.",
  }


def execute_offload_action(requested_id: Optional[str] = None) -> Dict[str, Any]:
  """Generates SESSION_HANDOFF.md and launches a new conversation with the summary prompt."""
  transcript_path, _ = discover_conversations(requested_id)
  if transcript_path is None:
    return {"ok": False, "message": "No active conversation found to offload."}

  conversation_id = transcript_path.parents[2].name
  artifact_dir = transcript_path.parents[2]
  steps = sm.load_transcript_steps(transcript_path)
  metrics = sm.analyze_steps(steps, datetime.now(timezone.utc))
  digest = extract_conversation_digest(steps)

  handoff_path = artifact_dir / "SESSION_HANDOFF.md"
  write_handoff_markdown(conversation_id, metrics, digest, handoff_path)

  prompt_summary = build_offload_prompt(conversation_id, metrics, digest, handoff_path)
  copied = copy_to_clipboard(prompt_summary)

  prompts = digest.get("user_prompts") or []
  short_title = (prompts[0][:42] + "...") if prompts and len(prompts[0]) > 42 else (
    prompts[0] if prompts else f"Offloaded {conversation_id[:8]}"
  )
  new_title = f"Cont: {short_title}"

  launched_new_chat = False
  if AGENTAPI_BIN.is_file():
    try:
      result = subprocess.run(
        [
          str(AGENTAPI_BIN),
          "new-conversation",
          f"--title={new_title}",
          prompt_summary,
        ],
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
      )
      agentapi_output = (result.stdout or "").strip()
      launched_new_chat = result.returncode == 0 and '"error"' not in agentapi_output
    except OSError:
      launched_new_chat = False

  if launched_new_chat:
    return {
      "ok": True,
      "launched": True,
      "handoff_path": str(handoff_path),
      "prompt": prompt_summary,
      "message": f"Started new chat '{new_title}' with full session summary!",
    }

  return {
    "ok": True,
    "launched": False,
    "copied": copied,
    "handoff_path": str(handoff_path),
    "prompt": prompt_summary,
    "message": (
      "Saved SESSION_HANDOFF.md & copied new chat summary prompt to clipboard "
      "(press Cmd+N then Cmd+V to start fresh)."
    ),
  }


def parse_requested_conversation_id() -> Optional[str]:
  """Extracts an optional `--conversation-id <id>` argument from sys.argv."""
  for idx, arg in enumerate(sys.argv):
    if arg == "--conversation-id" and idx + 1 < len(sys.argv):
      return sys.argv[idx + 1].strip()
  return None


def main() -> int:
  requested_id = parse_requested_conversation_id()
  if "--compact" in sys.argv:
    sys.stdout.write(json.dumps(execute_compact_action(requested_id)) + "\n")
    return 0
  if "--offload" in sys.argv:
    sys.stdout.write(json.dumps(execute_offload_action(requested_id)) + "\n")
    return 0
  sys.stdout.write(json.dumps(collect_live_telemetry(requested_id)) + "\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
