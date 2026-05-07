#!/usr/bin/env python3
"""Claude Code -> Langfuse hook"""
import json, os, sys, time, hashlib, subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from langfuse import Langfuse, propagate_attributes
    from opentelemetry.trace import use_span as _otel_use_span
except Exception:
    sys.exit(0)

STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"
DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))

def _log(level, message):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
    except Exception: pass

def debug(m):
    if DEBUG: _log("DEBUG", m)
def info(m): _log("INFO", m)

# ---- Git context (branch / repo / head_sha) ----
_GIT_CACHE: Dict[str, Dict[str, Any]] = {}
_GIT_CACHE_TTL_S = 5.0

def _git(cwd: Optional[str], *args: str) -> Optional[str]:
    if not cwd: return None
    try:
        r = subprocess.run(["git", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=2.0)
    except Exception:
        return None
    if r.returncode != 0: return None
    return (r.stdout or "").strip() or None

def get_git_context(cwd: Optional[str], force_refresh: bool = False) -> Dict[str, Optional[str]]:
    empty = {"branch": None, "repo": None, "repo_root": None, "head_sha": None}
    if not cwd: return empty
    now = time.time()
    if not force_refresh:
        cached = _GIT_CACHE.get(cwd)
        if cached and cached["expires_at"] > now:
            return cached["data"]
    repo_root = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo_root:
        _GIT_CACHE[cwd] = {"data": empty, "expires_at": now + _GIT_CACHE_TTL_S}
        return empty
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    head_sha = _git(cwd, "rev-parse", "HEAD")
    data = {"branch": branch, "repo": Path(repo_root).name,
            "repo_root": repo_root, "head_sha": head_sha}
    _GIT_CACHE[cwd] = {"data": data, "expires_at": now + _GIT_CACHE_TTL_S}
    return data

def git_tags(ctx: Dict[str, Optional[str]]) -> List[str]:
    out = []
    b = ctx.get("branch")
    if b and b != "HEAD": out.append(f"branch:{b}")
    r = ctx.get("repo")
    if r: out.append(f"repo:{r}")
    return out

def write_active_session(repo_root: Optional[str], data: Dict[str, Any]) -> None:
    """Persist {trace_id, session_id, branch, repo, head_sha} into <repo>/.claude/active-session.json
    so the commit-msg git hook can read it and inject the link into commit messages."""
    if not repo_root: return
    try:
        d = Path(repo_root) / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "active-session.json"
        existing = {}
        if p.exists():
            try: existing = json.loads(p.read_text(encoding="utf-8"))
            except Exception: existing = {}
        existing.update({k: v for k, v in data.items() if v is not None})
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        debug(f"write_active_session failed: {e}")

def get_otel_trace_id_hex(span) -> Optional[str]:
    try:
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return f"{ctx.trace_id:032x}"
    except Exception: pass
    return None

class FileLock:
    def __init__(self, path, timeout_s=2.0):
        self.path = path; self.timeout_s = timeout_s; self._fh = None
    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl
            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.time() > deadline: break
                    time.sleep(0.05)
        except Exception: pass
        return self
    def __exit__(self, *a):
        try:
            import fcntl; fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception: pass
        try: self._fh.close()
        except Exception: pass

def load_state():
    try:
        if not STATE_FILE.exists(): return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception: return {}

def save_state(state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e: debug(f"save_state failed: {e}")

def state_key(sid, tp):
    return hashlib.sha256(f"{sid}::{tp}".encode("utf-8")).hexdigest()

def read_hook_payload():
    try:
        d = sys.stdin.read()
        if not d.strip(): return {}
        return json.loads(d)
    except Exception: return {}

def extract_session_and_transcript(payload):
    sid = (payload.get("sessionId") or payload.get("session_id")
           or payload.get("session", {}).get("id"))
    tr = (payload.get("transcriptPath") or payload.get("transcript_path")
          or payload.get("transcript", {}).get("path"))
    tp = None
    if tr:
        try: tp = Path(tr).expanduser().resolve()
        except Exception: tp = None
    return sid, tp

def get_content(msg):
    if not isinstance(msg, dict): return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg):
    t = msg.get("type")
    if t in ("user", "assistant"): return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"): return r
    return None

def is_tool_result(msg):
    if get_role(msg) != "user": return False
    c = get_content(msg)
    if isinstance(c, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)
    return False

def iter_tool_results(c):
    return [x for x in c if isinstance(x, dict) and x.get("type") == "tool_result"] if isinstance(c, list) else []

def iter_tool_uses(c):
    return [x for x in c if isinstance(x, dict) and x.get("type") == "tool_use"] if isinstance(c, list) else []

def extract_text(c):
    if isinstance(c, str): return c
    if isinstance(c, list):
        parts = []
        for x in c:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str): parts.append(x)
        return "\n".join(p for p in parts if p)
    return ""

def truncate_text(s, max_chars=MAX_CHARS):
    if s is None: return "", {"truncated": False, "orig_len": 0}
    n = len(s)
    if n <= max_chars: return s, {"truncated": False, "orig_len": n}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": n, "kept_len": len(head),
                  "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg):
    m = msg.get("message")
    return (m.get("model") if isinstance(m, dict) else None) or "claude"

def get_message_id(msg):
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid: return mid
    return None

def get_timestamp(msg: Dict[str, Any]) -> Optional[datetime]:
    """从 transcript 消息的 timestamp 字段提取真实时间，用于计算正确的 latency。"""
    ts = msg.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass
    return None

def extract_usage(msg: Dict[str, Any]) -> Dict[str, int]:
    u = (msg.get("message") or {}).get("usage") or {}
    return {
        "input": int(u.get("input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cache_read_input": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation_input": int(u.get("cache_creation_input_tokens") or 0),
    }

def aggregate_usage(msgs: List[Dict[str, Any]]) -> Dict[str, int]:
    total = {"input": 0, "output": 0, "cache_read_input": 0, "cache_creation_input": 0}
    for m in msgs:
        for k, v in extract_usage(m).items():
            total[k] += v
    return {k: v for k, v in total.items() if v > 0}

_EXT_LANG = {
    "py": "python", "ipynb": "python", "js": "javascript", "mjs": "javascript",
    "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
    "go": "go", "rs": "rust", "java": "java", "kt": "kotlin",
    "rb": "ruby", "php": "php", "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp",
    "cs": "csharp", "swift": "swift", "scala": "scala",
    "sh": "shell", "bash": "shell", "zsh": "shell", "fish": "shell",
    "sql": "sql", "html": "html", "css": "css", "scss": "css",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml",
    "md": "markdown", "rst": "rst",
}
def _lang_from_path(fp: str) -> str:
    if not isinstance(fp, str) or "." not in fp: return "unknown"
    ext = fp.rsplit(".", 1)[-1].lower()
    return _EXT_LANG.get(ext, ext if ext.isalpha() and len(ext) <= 6 else "unknown")

def _count_lines(s: Any) -> int:
    if not s: return 0
    if not isinstance(s, str): s = str(s)
    return len(s.splitlines())

def loc_for_call(tc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = tc.get("name")
    inp = tc.get("input") or {}
    if not isinstance(inp, dict): return None
    if name == "Edit":
        added = _count_lines(inp.get("new_string"))
        removed = _count_lines(inp.get("old_string"))
        fp = inp.get("file_path") or ""
    elif name == "MultiEdit":
        edits = inp.get("edits") or []
        added = sum(_count_lines(e.get("new_string")) for e in edits if isinstance(e, dict))
        removed = sum(_count_lines(e.get("old_string")) for e in edits if isinstance(e, dict))
        fp = inp.get("file_path") or ""
    elif name == "Write":
        added = _count_lines(inp.get("content"))
        removed = 0
        fp = inp.get("file_path") or ""
    elif name == "NotebookEdit":
        added = _count_lines(inp.get("new_source"))
        removed = _count_lines(inp.get("old_source")) if inp.get("edit_mode") != "insert" else 0
        fp = inp.get("notebook_path") or ""
    else:
        return None
    return {"added": int(added), "removed": int(removed),
            "language": _lang_from_path(fp), "file_path": fp}

import re as _re
_COMMIT_RE = _re.compile(r"\bgit\s+(?:-[^\s]+\s+)*commit\b")
_PR_CREATE_RE = _re.compile(r"\bgh\s+(?:-[^\s]+\s+)*pr\s+create\b")
_DRY_HELP_RE = _re.compile(r"--(?:help|dry-run)\b")
def commit_pr_counts(tc: Dict[str, Any]) -> Tuple[int, int]:
    if tc.get("name") != "Bash": return (0, 0)
    inp = tc.get("input") or {}
    cmd = inp.get("command") if isinstance(inp, dict) else None
    if not isinstance(cmd, str): return (0, 0)
    if _DRY_HELP_RE.search(cmd): return (0, 0)
    return (len(_COMMIT_RE.findall(cmd)), len(_PR_CREATE_RE.findall(cmd)))

_MODEL_PRICES = [
    ("claude-opus-4",   {"input": 15.0, "output": 75.0, "cache_read_input": 1.50, "cache_creation_input": 18.75}),
    ("claude-sonnet-4", {"input":  3.0, "output": 15.0, "cache_read_input": 0.30, "cache_creation_input":  3.75}),
    ("claude-haiku-4",  {"input":  1.0, "output":  5.0, "cache_read_input": 0.10, "cache_creation_input":  1.25}),
    ("claude-3-5-sonnet", {"input": 3.0, "output": 15.0, "cache_read_input": 0.30, "cache_creation_input": 3.75}),
    ("claude-3-5-haiku",  {"input": 0.8, "output": 4.0,  "cache_read_input": 0.08, "cache_creation_input": 1.0}),
    ("claude-3-opus",   {"input": 15.0, "output": 75.0, "cache_read_input": 1.50, "cache_creation_input": 18.75}),
]
def compute_cost(model: str, usage: Dict[str, int]) -> Optional[Dict[str, float]]:
    if not model or not usage: return None
    rates = None
    for prefix, p in _MODEL_PRICES:
        if model.startswith(prefix):
            rates = p; break
    if rates is None: return None
    cd = {}
    for k, tok in usage.items():
        rate = rates.get(k)
        if rate is None or not tok: continue
        cd[k] = round(tok * rate / 1_000_000.0, 6)
    if cd:
        cd["total"] = round(sum(cd.values()), 6)
    return cd or None

REJECT_MARKERS = (
    "The user doesn't want to proceed with this tool use",
    "The tool use was rejected",
)
def classify_decision(output_text: Any, is_error: bool) -> str:
    if is_error and isinstance(output_text, str):
        for marker in REJECT_MARKERS:
            if marker in output_text:
                return "reject"
    return "accept"

def is_compact_event(msg: Dict[str, Any]) -> bool:
    if not isinstance(msg, dict): return False
    if msg.get("isCompactSummary") is True: return True
    if msg.get("type") == "system":
        st = msg.get("subtype", "")
        if isinstance(st, str) and "compact" in st.lower(): return True
    return False

@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(gs, key):
    s = gs.get(key, {})
    return SessionState(int(s.get("offset", 0)), str(s.get("buffer", "")), int(s.get("turn_count", 0)))

def write_session_state(gs, key, ss):
    gs[key] = {"offset": ss.offset, "buffer": ss.buffer, "turn_count": ss.turn_count,
               "updated": datetime.now(timezone.utc).isoformat()}

def read_new_jsonl(tp, ss):
    if not tp.exists(): return [], ss
    try:
        with open(tp, "rb") as f:
            f.seek(ss.offset); chunk = f.read(); new_offset = f.tell()
    except Exception as e:
        debug(f"read failed: {e}"); return [], ss
    if not chunk: return [], ss
    text = chunk.decode("utf-8", errors="replace")
    combined = ss.buffer + text
    lines = combined.split("\n")
    ss.buffer = lines[-1]; ss.offset = new_offset
    msgs = []
    for line in lines[:-1]:
        line = line.strip()
        if not line: continue
        try: msgs.append(json.loads(line))
        except Exception: continue
    return msgs, ss

@dataclass
class ToolResultEntry:
    content: Any
    is_error: bool
    timestamp: Optional[datetime]

@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, ToolResultEntry]

def build_turns(messages):
    turns = []
    cu = None; ao = []; al = {}; tr = {}
    def flush():
        nonlocal cu, ao, al, tr, turns
        if cu is None or not al: return
        ams = [al[mid] for mid in ao if mid in al]
        turns.append(Turn(cu, ams, dict(tr)))
    for msg in messages:
        role = get_role(msg)
        if is_tool_result(msg):
            ts = get_timestamp(msg)
            for x in iter_tool_results(get_content(msg)):
                tid = x.get("tool_use_id")
                if tid:
                    tr[str(tid)] = ToolResultEntry(
                        content=x.get("content"),
                        is_error=bool(x.get("is_error")),
                        timestamp=ts,
                    )
            continue
        if role == "user":
            flush(); cu = msg; ao = []; al = {}; tr = {}; continue
        if role == "assistant":
            if cu is None: continue
            mid = get_message_id(msg) or f"noid:{len(ao)}"
            if mid not in al: ao.append(mid)
            al[mid] = msg
    flush()
    return turns

def extract_events(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for m in messages:
        if is_compact_event(m):
            out.append({"kind": "compaction", "timestamp": get_timestamp(m), "raw": m})
    return out

def _tool_calls(ams):
    out = []
    for am in ams:
        am_ts = get_timestamp(am)
        for tu in iter_tool_uses(get_content(am)):
            tid = tu.get("id") or ""
            inp = tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {}
            out.append({"id": str(tid), "name": tu.get("name") or "unknown",
                        "input": inp, "start_ts": am_ts})
    return out

def emit_turn(lf, sid, n, turn, tp, user_id=None, git_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    git_ctx = git_ctx or {}
    git_meta = git_ctx if git_ctx.get("repo_root") else None
    ut, ut_meta = truncate_text(extract_text(get_content(turn.user_msg)))
    last = turn.assistant_msgs[-1]
    at, at_meta = truncate_text(extract_text(get_content(last)))
    model = get_model(turn.assistant_msgs[0])
    tcs = _tool_calls(turn.assistant_msgs)
    is_sidechain = bool(turn.user_msg.get("isSidechain"))
    query_source = "subagent" if is_sidechain else "main"

    for c in tcs:
        entry = turn.tool_results_by_id.get(c["id"]) if c["id"] else None
        if entry is not None:
            raw = entry.content
            s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            t, m = truncate_text(s)
            c["output"] = t
            c["output_meta"] = m
            c["is_error"] = entry.is_error
            c["end_ts"] = entry.timestamp
            c["decision"] = classify_decision(t, entry.is_error)
        else:
            c["output"] = None
            c["is_error"] = False
            c["end_ts"] = None
            c["decision"] = "pending"

    decision_counts = {"accept": 0, "reject": 0, "pending": 0}
    for c in tcs:
        decision_counts[c.get("decision", "accept")] = decision_counts.get(c.get("decision", "accept"), 0) + 1
    tool_error_count = sum(1 for c in tcs if c.get("is_error"))

    loc_added = loc_removed = 0
    loc_by_language: Dict[str, Dict[str, int]] = {}
    loc_files: List[str] = []
    for c in tcs:
        if c.get("decision") != "accept": continue
        info_ = loc_for_call(c)
        if info_ is None: continue
        loc_added += info_["added"]; loc_removed += info_["removed"]
        lang = info_["language"]
        bucket = loc_by_language.setdefault(lang, {"added": 0, "removed": 0})
        bucket["added"] += info_["added"]; bucket["removed"] += info_["removed"]
        if info_["file_path"]: loc_files.append(info_["file_path"])
        c["loc"] = info_

    commit_count = pr_count = 0
    for c in tcs:
        if c.get("decision") != "accept": continue
        ci, pi = commit_pr_counts(c)
        commit_count += ci; pr_count += pi

    usage_total = aggregate_usage(turn.assistant_msgs) or {}
    cost_details = compute_cost(model, usage_total)

    now = datetime.now(timezone.utc)
    turn_start = get_timestamp(turn.user_msg) or now
    turn_end   = get_timestamp(last) or now
    gen_start  = get_timestamp(turn.assistant_msgs[0]) or turn_start
    gen_end    = turn_end

    def _ns(dt): return int(dt.timestamp() * 1_000_000_000)
    turn_start_ns = _ns(turn_start)
    turn_end_ns   = max(_ns(turn_end), turn_start_ns + 1_000_000)
    gen_start_ns  = _ns(gen_start)
    gen_end_ns    = max(_ns(gen_end),  gen_start_ns  + 1_000_000)

    tracer = lf._otel_tracer
    pa_kwargs = {"session_id": sid, "trace_name": f"Claude Code - Turn {n}",
                 "tags": ["claude-code", query_source] + git_tags(git_ctx)}
    if user_id: pa_kwargs["user_id"] = user_id

    trace_id_hex: Optional[str] = None
    with propagate_attributes(**pa_kwargs):
        turn_span = tracer.start_span(name=f"Claude Code - Turn {n}", start_time=turn_start_ns)
        trace_id_hex = get_otel_trace_id_hex(turn_span)
        try:
            with _otel_use_span(turn_span, end_on_exit=False):
                lf._create_observation_from_otel_span(
                    otel_span=turn_span, as_type="span",
                    input={"role": "user", "content": ut},
                    output={"role": "assistant", "content": at},
                    metadata={"source": "claude-code", "session_id": sid, "turn_number": n,
                              "transcript_path": str(tp), "user_text": ut_meta,
                              "query_source": query_source,
                              "tool_decisions": decision_counts,
                              "tool_error_count": tool_error_count,
                              "tool_count": len(tcs),
                              "lines_of_code": {"added": loc_added, "removed": loc_removed,
                                                 "by_language": loc_by_language,
                                                 "files": loc_files} if (loc_added or loc_removed) else None,
                              "commit_count": commit_count,
                              "pr_count": pr_count,
                              "cost_usd": sum(cost_details.values()) if cost_details else None,
                              "git": git_meta})

                gen_span = tracer.start_span(name="Claude Response", start_time=gen_start_ns)
                try:
                    with _otel_use_span(gen_span, end_on_exit=False):
                        gen_output = {"role": "assistant", "content": at}
                        if tcs:
                            gen_output["tool_calls"] = [
                                {"id": tc["id"], "type": "function",
                                 "function": {"name": tc["name"],
                                              "arguments": json.dumps(tc["input"], ensure_ascii=False)
                                              if not isinstance(tc["input"], str) else tc["input"]}}
                                for tc in tcs
                            ]
                        gen_kwargs = dict(
                            otel_span=gen_span, as_type="generation", model=model,
                            input={"role": "user", "content": ut},
                            output=gen_output,
                            usage_details=usage_total or None,
                            metadata={"assistant_text": at_meta, "tool_count": len(tcs),
                                      "query_source": query_source, "git": git_meta},
                        )
                        if cost_details:
                            gen_kwargs["cost_details"] = cost_details
                        lf._create_observation_from_otel_span(**gen_kwargs)
                finally:
                    gen_span.end(end_time=gen_end_ns)

                for tc in tcs:
                    io = tc["input"]; im = None
                    if isinstance(io, str): io, im = truncate_text(io)
                    tool_start = tc.get("start_ts") or gen_end
                    tool_end   = tc.get("end_ts")   or turn_end
                    tool_start_ns = _ns(tool_start)
                    tool_end_ns   = max(_ns(tool_end), tool_start_ns + 1_000_000)
                    tool_meta = {
                        "tool_name": tc["name"], "tool_id": tc["id"],
                        "input_meta": im, "output_meta": tc.get("output_meta"),
                        "is_error": tc.get("is_error", False),
                        "decision": tc.get("decision", "accept"),
                        "loc": tc.get("loc"),
                        "query_source": query_source,
                        "git": git_meta,
                    }
                    tool_span = tracer.start_span(name=f"Tool: {tc['name']}", start_time=tool_start_ns)
                    try:
                        with _otel_use_span(tool_span, end_on_exit=False):
                            lf._create_observation_from_otel_span(
                                otel_span=tool_span, as_type="tool",
                                input=io, output=tc.get("output"),
                                metadata=tool_meta)
                    finally:
                        tool_span.end(end_time=tool_end_ns)
        finally:
            turn_span.end(end_time=turn_end_ns)
    return trace_id_hex

def emit_session_marker(lf, sid: str, event_name: str, payload: Dict[str, Any],
                         user_id: Optional[str] = None,
                         git_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    git_ctx = git_ctx or {}
    git_meta = git_ctx if git_ctx.get("repo_root") else None
    now = datetime.now(timezone.utc)
    start_ns = int(now.timestamp() * 1_000_000_000)
    end_ns = start_ns + 1_000_000
    tag = "session-start" if event_name == "SessionStart" else "session-end"
    pa = {"session_id": sid, "trace_name": f"Claude Code - {event_name}",
          "tags": ["claude-code", tag] + git_tags(git_ctx)}
    if user_id: pa["user_id"] = user_id
    tracer = lf._otel_tracer
    trace_id_hex = None
    with propagate_attributes(**pa):
        sp = tracer.start_span(name=event_name, start_time=start_ns)
        trace_id_hex = get_otel_trace_id_hex(sp)
        try:
            with _otel_use_span(sp, end_on_exit=False):
                lf._create_observation_from_otel_span(
                    otel_span=sp, as_type="event",
                    input=None, output=None,
                    metadata={"event": event_name, "session_id": sid,
                              "source": payload.get("source"),
                              "reason": payload.get("reason"),
                              "cwd": payload.get("cwd"),
                              "git": git_meta})
        finally:
            sp.end(end_time=end_ns)
    return trace_id_hex

def emit_user_prompt_marker(lf, sid: str, payload: Dict[str, Any],
                             user_id: Optional[str] = None,
                             git_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    git_ctx = git_ctx or {}
    git_meta = git_ctx if git_ctx.get("repo_root") else None
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    pt, pmeta = truncate_text(prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False))
    now = datetime.now(timezone.utc)
    start_ns = int(now.timestamp() * 1_000_000_000)
    end_ns = start_ns + 1_000_000
    pa = {"session_id": sid, "trace_name": "Claude Code - User Prompt",
          "tags": ["claude-code", "user-prompt"] + git_tags(git_ctx)}
    if user_id: pa["user_id"] = user_id
    tracer = lf._otel_tracer
    trace_id_hex = None
    with propagate_attributes(**pa):
        sp = tracer.start_span(name="UserPromptSubmit", start_time=start_ns)
        trace_id_hex = get_otel_trace_id_hex(sp)
        try:
            with _otel_use_span(sp, end_on_exit=False):
                lf._create_observation_from_otel_span(
                    otel_span=sp, as_type="event",
                    input={"role": "user", "content": pt},
                    output=None,
                    metadata={"event": "UserPromptSubmit", "session_id": sid,
                              "prompt_meta": pmeta, "cwd": payload.get("cwd"),
                              "git": git_meta})
        finally:
            sp.end(end_time=end_ns)
    return trace_id_hex

def emit_event(lf, sid, ev: Dict[str, Any], user_id: Optional[str] = None,
                git_ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    git_ctx = git_ctx or {}
    git_meta = git_ctx if git_ctx.get("repo_root") else None
    ts = ev.get("timestamp") or datetime.now(timezone.utc)
    start_ns = int(ts.timestamp() * 1_000_000_000)
    end_ns = start_ns + 1_000_000
    kind = ev.get("kind", "event")
    tracer = lf._otel_tracer
    pa = {"session_id": sid,
          "trace_name": f"Claude Code - {kind.capitalize()}",
          "tags": ["claude-code", kind] + git_tags(git_ctx)}
    if user_id: pa["user_id"] = user_id
    trace_id_hex = None
    with propagate_attributes(**pa):
        sp = tracer.start_span(name=kind.capitalize(), start_time=start_ns)
        trace_id_hex = get_otel_trace_id_hex(sp)
        try:
            with _otel_use_span(sp, end_on_exit=False):
                raw = ev.get("raw") or {}
                lf._create_observation_from_otel_span(
                    otel_span=sp, as_type="event",
                    input=None, output=None,
                    metadata={"kind": kind, "session_id": sid,
                              "subtype": raw.get("subtype"),
                              "uuid": raw.get("uuid"),
                              "git": git_meta})
        finally:
            sp.end(end_time=end_ns)
    return trace_id_hex

def emit_commit_event(lf, sid: str, payload: Dict[str, Any], git_ctx: Dict[str, Any],
                      user_id: Optional[str] = None) -> Optional[str]:
    """PostToolUse(Bash, git commit) → emit a Commit observation in Langfuse."""
    cwd = payload.get("cwd") or git_ctx.get("repo_root")
    head_sha = git_ctx.get("head_sha")
    if not head_sha:
        return None
    msg = _git(cwd, "log", "-1", "--pretty=%B", head_sha) or ""
    files_str = _git(cwd, "show", "--name-only", "--pretty=", head_sha) or ""
    files = [f for f in files_str.split("\n") if f.strip()]
    short_stat = _git(cwd, "show", "--shortstat", "--pretty=", head_sha) or ""

    now = datetime.now(timezone.utc)
    start_ns = int(now.timestamp() * 1_000_000_000)
    end_ns = start_ns + 1_000_000

    pa = {"session_id": sid, "trace_name": "Claude Code - Commit",
          "tags": ["claude-code", "commit"] + git_tags(git_ctx)}
    if user_id: pa["user_id"] = user_id
    tracer = lf._otel_tracer
    trace_id_hex = None
    with propagate_attributes(**pa):
        sp = tracer.start_span(name="GitCommit", start_time=start_ns)
        trace_id_hex = get_otel_trace_id_hex(sp)
        try:
            with _otel_use_span(sp, end_on_exit=False):
                lf._create_observation_from_otel_span(
                    otel_span=sp, as_type="event",
                    input=None,
                    output={"sha": head_sha, "branch": git_ctx.get("branch"),
                            "message": msg, "files": files,
                            "shortstat": short_stat.strip()},
                    metadata={"event": "GitCommit", "session_id": sid,
                              "git": git_ctx, "files_changed": len(files)})
        finally:
            sp.end(end_time=end_ns)
    return trace_id_hex

def resolve_user_id():
    uid = os.environ.get("CC_LANGFUSE_USER_ID")
    if uid: return uid
    for k in ("USER", "LOGNAME", "USERNAME"):
        v = os.environ.get(k)
        if v: return v
    try:
        import getpass; return getpass.getuser()
    except Exception:
        return None

def _is_git_commit_command(cmd: Any) -> bool:
    if not isinstance(cmd, str): return False
    if _DRY_HELP_RE.search(cmd): return False
    return bool(_COMMIT_RE.search(cmd))

def _post_tool_succeeded(payload: Dict[str, Any]) -> bool:
    """PostToolUse payloads: tool_response can be dict or str. Treat missing fields as success."""
    resp = payload.get("tool_response") or payload.get("toolResponse")
    if isinstance(resp, dict):
        if resp.get("isError") or resp.get("is_error"):
            return False
        if resp.get("interrupted"):
            return False
    return True

def main():
    start = time.time()
    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true": return 0
    pk = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = (os.environ.get("CC_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL")
            or os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com")
    if not pk or not sk: return 0
    user_id = resolve_user_id()
    payload = read_hook_payload()
    sid, tp = extract_session_and_transcript(payload)
    if not sid: return 0
    event_name = (payload.get("hook_event_name") or payload.get("hookEventName") or "Stop")

    cwd = payload.get("cwd") or os.getcwd()
    git_ctx = get_git_context(cwd)

    try: lf = Langfuse(public_key=pk, secret_key=sk, host=host)
    except Exception: return 0
    try:
        # SessionStart / SessionEnd — emit a marker, no transcript tail
        if event_name in ("SessionStart", "SessionEnd"):
            try:
                tid = emit_session_marker(lf, sid, event_name, payload,
                                           user_id=user_id, git_ctx=git_ctx)
            except Exception as e:
                tid = None; debug(f"emit_session_marker failed: {e}")
            try: lf.flush()
            except Exception: pass
            write_active_session(git_ctx.get("repo_root"), {
                "session_id": sid, "trace_id": tid,
                "branch": git_ctx.get("branch"), "repo": git_ctx.get("repo"),
                "head_sha": git_ctx.get("head_sha"),
                "last_event": event_name,
            })
            info(f"Emitted {event_name} marker (session={sid}, branch={git_ctx.get('branch')})")
            return 0

        # UserPromptSubmit
        if event_name == "UserPromptSubmit":
            try:
                tid = emit_user_prompt_marker(lf, sid, payload,
                                               user_id=user_id, git_ctx=git_ctx)
            except Exception as e:
                tid = None; debug(f"emit_user_prompt_marker failed: {e}")
            try: lf.flush()
            except Exception: pass
            write_active_session(git_ctx.get("repo_root"), {
                "session_id": sid, "trace_id": tid,
                "branch": git_ctx.get("branch"), "repo": git_ctx.get("repo"),
                "head_sha": git_ctx.get("head_sha"),
                "last_event": "UserPromptSubmit",
            })
            info(f"Emitted UserPromptSubmit marker (session={sid})")
            return 0

        # PostToolUse — only act on git commit Bash calls
        if event_name == "PostToolUse":
            tool_name = payload.get("tool_name") or payload.get("toolName")
            if tool_name != "Bash":
                return 0
            tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
            cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
            if not _is_git_commit_command(cmd):
                return 0
            if not _post_tool_succeeded(payload):
                debug("PostToolUse git commit reported error/interrupt — skipping emit")
                return 0
            # HEAD has just moved — bust the cache
            git_ctx = get_git_context(cwd, force_refresh=True)
            try:
                tid = emit_commit_event(lf, sid, payload, git_ctx, user_id=user_id)
            except Exception as e:
                tid = None; debug(f"emit_commit_event failed: {e}")
            try: lf.flush()
            except Exception: pass
            write_active_session(git_ctx.get("repo_root"), {
                "session_id": sid, "trace_id": tid,
                "branch": git_ctx.get("branch"), "repo": git_ctx.get("repo"),
                "head_sha": git_ctx.get("head_sha"),
                "last_event": "Commit",
            })
            info(f"Emitted Commit event (sha={git_ctx.get('head_sha')}, branch={git_ctx.get('branch')})")
            return 0

        # Default (Stop / etc): tail the transcript
        if not tp or not tp.exists(): return 0
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(sid, str(tp))
            ss = load_session_state(state, key)
            msgs, ss = read_new_jsonl(tp, ss)
            if not msgs:
                write_session_state(state, key, ss); save_state(state); return 0
            turns = build_turns(msgs)
            events = extract_events(msgs)
            if not turns and not events:
                write_session_state(state, key, ss); save_state(state); return 0
            emitted = 0
            last_trace_id = None
            for t in turns:
                emitted += 1
                try:
                    tid = emit_turn(lf, sid, ss.turn_count + emitted, t, tp,
                                    user_id=user_id, git_ctx=git_ctx)
                    if tid: last_trace_id = tid
                except Exception as e: debug(f"emit turn failed: {e}")
            for ev in events:
                try:
                    tid = emit_event(lf, sid, ev, user_id=user_id, git_ctx=git_ctx)
                    if tid: last_trace_id = tid
                except Exception as e: debug(f"emit event failed: {e}")
            ss.turn_count += emitted
            write_session_state(state, key, ss); save_state(state)
        try: lf.flush()
        except Exception: pass
        write_active_session(git_ctx.get("repo_root"), {
            "session_id": sid, "trace_id": last_trace_id,
            "branch": git_ctx.get("branch"), "repo": git_ctx.get("repo"),
            "head_sha": git_ctx.get("head_sha"),
            "last_event": event_name,
        })
        info(f"Processed {emitted} turns + {len(events)} events in {time.time()-start:.2f}s "
             f"(session={sid}, branch={git_ctx.get('branch')})")
        return 0
    except Exception as e:
        debug(f"failure: {e}"); return 0
    finally:
        try: lf.shutdown()
        except Exception: pass

if __name__ == "__main__":
    sys.exit(main())
