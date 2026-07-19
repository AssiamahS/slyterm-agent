#!/usr/bin/env python3
"""slyterm — tiny free Claude-Code-style terminal agent.

Engine: GitHub Models free API (models.github.ai), auth = your existing
`gh auth token`. No sign-in, no API key, no card. The whole agent is kept
small on purpose: the free tier caps every request at ~8K input tokens,
so big agents (opencode, gemini-cli) don't fit — this one does.

What makes it feel like Claude Code despite the cap:
  - streaming answers (text prints as it generates)
  - context COMPACTION: old turns get summarized, not thrown away
  - sub-agents: fresh-context workers for big-repo searches
  - a persistent plan the model keeps updated across the whole task
  - grep/glob/read/edit tools instead of hoping the model writes find(1)

Usage:
  slyterm                 interactive REPL (readline history, /commands)
  slyterm -p "prompt"     one-shot, prints answer and exits
  slyterm -c              continue the previous session (also: -c -p "...")

It also reads ./CLAUDE.md or ./AGENTS.md into its rules, like Claude Code.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse

try:
    import readline
except ImportError:
    readline = None

API_URL = "https://models.github.ai/inference/chat/completions"
# Best-first; every model is a separate free-tier quota bucket, so the 429
# sweep in chat_once effectively multiplies the daily allowance.
MODELS = [
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
    "openai/gpt-4o-mini",
    "meta/llama-4-maverick-17b-128e-instruct-fp8",
]
MAX_TOOL_OUTPUT = 3500          # chars per tool result kept in context
MAX_CONTEXT_CHARS = 20000       # ~5K tokens of history, leaves room under 8K
MAX_STEPS = 25                  # tool-loop safety stop
SUB_STEPS = 12                  # sub-agent loop cap
HISTORY_FILE = os.path.expanduser("~/.slyterm_history")
SESSION_FILE = os.path.expanduser("~/.slyterm/session.json")
RULES_BUDGET = 1500             # chars of CLAUDE.md/AGENTS.md folded into system

BOLD, DIM, CYAN, YELLOW, RED, GREEN, RESET = ("\033[1m", "\033[2m", "\033[36m",
                                              "\033[33m", "\033[31m", "\033[32m",
                                              "\033[0m")

SYSTEM = (
    "You are slyterm, a terminal coding agent on the user's Mac (macOS, zsh). "
    "Work autonomously: inspect, edit, run and verify with tools, then give a short final answer. "
    "For any multi-step task, call plan first and keep it updated — it survives context trimming. "
    "Use grep/glob to find code, read_file before edit_file, bash for everything else. "
    "For broad searches across a big repo, delegate to the agent tool: it gets a FRESH context "
    "and returns only its conclusion, so your own context stays small. "
    "Keep outputs small: head/tail/grep, never dump big files. Never invent file contents. "
    "You have NO access to the user's email or cloud accounts — if asked, "
    "say so plainly instead of guessing or grepping ~/Library. "
    "Local MCP servers are available: mcp_tools(server) to discover, then mcp_call. "
    "Servers: {mcp}. Current directory: {cwd}"
)

TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a zsh command (60s timeout). Returns stdout+stderr, truncated.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file. Optional start_line/num_lines for big files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"}, "num_lines": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace an exact string in a file once. old_string must match exactly and be unique.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write full content to a file (overwrites, makes parent dirs).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents for a regex. Returns file:line:text matches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "dir or file, default ."},
            "glob": {"type": "string", "description": "e.g. *.py"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "Find files by name pattern, newest first. e.g. **/*.swift",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "root dir, default ."}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "plan",
        "description": "Set/update your task plan (markdown checklist). Always visible to you; check items off as you go.",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "agent",
        "description": "Run a sub-agent with a FRESH empty context on a self-contained task (big-repo search, multi-file summary). Returns only its final answer.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "full instructions incl. paths; it can't see this conversation"}},
            "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (DuckDuckGo). Returns titles and URLs.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch a URL and return its visible text, truncated.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "mcp_tools",
        "description": "List the tools an MCP server offers. Server names are in the system prompt.",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"}}, "required": ["server"]}}},
    {"type": "function", "function": {
        "name": "mcp_call",
        "description": "Call a tool on an MCP server. arguments_json is a JSON object string.",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"}, "tool": {"type": "string"},
            "arguments_json": {"type": "string"}},
            "required": ["server", "tool", "arguments_json"]}}},
]


# ---------------------------------------------------------------- MCP client

def load_mcp_config():
    """stdio MCP servers from every place Claude Code keeps them — same
    local servers and creds, no new sign-ins. Priority: cwd .mcp.json >
    ~/.mcp.json > per-project > global ~/.claude.json."""
    servers = {}

    def take(block):
        for name, s in (block or {}).items():
            if name not in servers and s.get("type", "stdio") == "stdio" and s.get("command"):
                servers[name] = s

    def read(path):
        try:
            return json.load(open(os.path.expanduser(path)))
        except Exception:
            return {}

    take(read(os.path.join(os.getcwd(), ".mcp.json")).get("mcpServers"))
    take(read("~/.mcp.json").get("mcpServers"))
    claude_cfg = read("~/.claude.json")
    take(claude_cfg.get("projects", {}).get(os.getcwd(), {}).get("mcpServers"))
    take(claude_cfg.get("mcpServers"))
    return servers


MCP_SERVERS = load_mcp_config()
_mcp_procs = {}


class McpSession:
    def __init__(self, name, spec):
        env = {**os.environ, **spec.get("env", {})}
        self.proc = subprocess.Popen(
            [spec["command"], *spec.get("args", [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env)
        self.next_id = 0
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "slyterm", "version": "1.0"}})
        self.notify("notifications/initialized")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method, params, timeout=60):
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line on stdout
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result", {})
        raise RuntimeError(f"timeout waiting for {method}")


def mcp_session(server):
    if server not in MCP_SERVERS:
        raise RuntimeError(f"unknown server '{server}'. Available: {', '.join(MCP_SERVERS)}")
    sess = _mcp_procs.get(server)
    if sess is None or sess.proc.poll() is not None:
        print(f"{DIM}mcp: starting {server}…{RESET}")
        sess = _mcp_procs[server] = McpSession(server, MCP_SERVERS[server])
    return sess


def mcp_tools(server):
    try:
        tools = mcp_session(server).request("tools/list", {}).get("tools", [])
        return "\n".join(f"- {t['name']}: {t.get('description', '')[:110]}"
                         for t in tools) or "No tools."
    except Exception as e:
        return f"ERROR: {e}"


def mcp_call(server, tool, arguments_json):
    print(f"{DIM}mcp: {server}.{tool}{RESET}")
    try:
        args = json.loads(arguments_json or "{}")
        result = mcp_session(server).request("tools/call",
                                             {"name": tool, "arguments": args})
        parts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        return "\n".join(parts) or json.dumps(result)[:MAX_TOOL_OUTPUT]
    except Exception as e:
        return f"ERROR: {e}"


def gh_token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        tok = out.stdout.strip()
    except Exception:
        tok = ""
    if not tok:
        sys.exit("No GitHub token. Run: gh auth login")
    return tok


def truncate(text, limit=MAX_TOOL_OUTPUT):
    """Keep head AND tail — build/test failures live at the end of logs."""
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 4, limit // 4
    return (text[:head] + f"\n[...truncated {len(text) - limit} chars...]\n"
            + text[-tail:])


def run_bash(command):
    print(f"{DIM}$ {command}{RESET}")
    try:
        p = subprocess.run(["/bin/zsh", "-c", command], capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip() or f"(no output, exit {p.returncode})"
        if p.returncode != 0:
            out += f"\n(exit code {p.returncode})"
        return out
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 60s"
    except Exception as e:
        return f"ERROR: {e}"


def read_file(path, start_line=None, num_lines=None):
    path = os.path.expanduser(path)
    print(f"{DIM}read {path}{RESET}")
    try:
        with open(path) as f:
            lines = f.readlines()
        start = max((start_line or 1) - 1, 0)
        chunk = lines[start:start + (num_lines or len(lines))]
        out = "".join(f"{start + i + 1}\t{l}" for i, l in enumerate(chunk))
        return truncate(out) or "(empty file)"
    except Exception as e:
        return f"ERROR: {e}"


def edit_file(path, old_string, new_string):
    path = os.path.expanduser(path)
    print(f"{DIM}edit {path}{RESET}")
    try:
        text = open(path).read()
        n = text.count(old_string)
        if n == 0:
            return "ERROR: old_string not found — read_file and match exactly."
        if n > 1:
            return f"ERROR: old_string appears {n} times — add context to make it unique."
        with open(path, "w") as f:
            f.write(text.replace(old_string, new_string, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"ERROR: {e}"


def write_file(path, content):
    path = os.path.expanduser(path)
    print(f"{DIM}write {path} ({len(content)} chars){RESET}")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def grep_files(pattern, path=".", glob_pat=None):
    path = os.path.expanduser(path or ".")
    print(f"{DIM}grep {pattern} {path}{RESET}")
    cmd = ["grep", "-rnIE", "--exclude-dir=.git", "--exclude-dir=node_modules",
           "--exclude-dir=__pycache__", "--exclude-dir=.build", "-m", "4"]
    if glob_pat:
        cmd += [f"--include={glob_pat}"]
    try:
        p = subprocess.run(cmd + [pattern, path], capture_output=True, text=True, timeout=30)
        lines = p.stdout.strip().splitlines()
        if not lines:
            return "No matches."
        out = "\n".join(l[:200] for l in lines[:40])
        if len(lines) > 40:
            out += f"\n[+{len(lines) - 40} more matches]"
        return out
    except subprocess.TimeoutExpired:
        return "ERROR: grep timed out — narrow the path."
    except Exception as e:
        return f"ERROR: {e}"


def glob_files(pattern, path="."):
    path = os.path.expanduser(path or ".")
    print(f"{DIM}glob {pattern} {path}{RESET}")
    import glob as _glob
    try:
        hits = _glob.glob(os.path.join(path, pattern), recursive=True)
        hits = [h for h in hits if "/.git/" not in h and "/node_modules/" not in h]
        hits.sort(key=lambda h: os.path.getmtime(h) if os.path.exists(h) else 0,
                  reverse=True)
        return "\n".join(hits[:50]) or "No files match."
    except Exception as e:
        return f"ERROR: {e}"


def http_get(url, headers=None):
    # curl instead of urllib: DDG serves urllib a bot-challenge page but answers curl.
    cmd = ["curl", "-sSL", "-m", "30",
           "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    p = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=40)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"curl exit {p.returncode}")
    return p.stdout


def web_search(query):
    print(f"{DIM}search: {query}{RESET}")
    try:
        html = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
        results = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html)
        lines = []
        for href, title in results[:8]:
            m = re.search(r"uddg=([^&]+)", href)
            url = urllib.parse.unquote(m.group(1)) if m else href
            lines.append(f"- {re.sub('<[^>]+>', '', title)} | {url}")
        return "\n".join(lines) or "No results."
    except Exception as e:
        return f"ERROR: {e}"


def fetch_url(url):
    print(f"{DIM}fetch: {url}{RESET}")
    try:
        html = http_get(url)
        html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return truncate(text)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- API + context

def call_api(token, messages, model, stream=False, tools=TOOLS, max_tokens=3000):
    body = {"model": model, "messages": messages, "max_completion_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                 method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=180)
    if not stream:
        with resp:
            return json.loads(resp.read())["choices"][0]["message"]
    return read_stream(resp)


def read_stream(resp):
    """Consume an SSE stream, printing text as it arrives (the Claude Code feel).
    Tool-call fragments are stitched back together by index."""
    content, tool_calls, printing = [], {}, False
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                choices = json.loads(data).get("choices") or []
            except json.JSONDecodeError:
                continue
            delta = choices[0].get("delta", {}) if choices else {}
            piece = delta.get("content")
            if piece:
                if not printing:
                    sys.stdout.write(CYAN)
                    printing = True
                sys.stdout.write(piece)
                sys.stdout.flush()
                content.append(piece)
            for tc in delta.get("tool_calls") or []:
                slot = tool_calls.setdefault(tc.get("index", 0), {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    if printing:
        print(RESET)
    msg = {"role": "assistant", "content": "".join(content)}
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return msg


def chat_once(token, messages, stream=True):
    """One API call. On 429 hop to the next model immediately; only sleep
    when every model is rate-limited in the same sweep."""
    for sweep in range(4):
        all_limited = True
        for model in MODELS:
            try:
                return call_api(token, messages, model, stream=stream)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code != 429:
                    all_limited = False
                    print(f"{YELLOW}{model}: HTTP {e.code} {detail}{RESET}")
            except Exception as e:
                all_limited = False
                print(f"{YELLOW}{model}: {e}{RESET}")
        if all_limited and sweep < 3:
            wait = 10 * (sweep + 1)
            print(f"{YELLOW}all models rate-limited, retrying in {wait}s…{RESET}")
            time.sleep(wait)
    return None


def context_chars(messages):
    return sum(len(json.dumps(m)) for m in messages)


def summarize(token, old_summary, dropped):
    """Compress trimmed-out turns into a running summary so long tasks stay
    coherent — Claude Code compacts, it doesn't forget. Cheap models first."""
    events = truncate(json.dumps(dropped), 6000)
    prompt = ("Merge into ONE dense summary (max 120 words) of an ongoing coding "
              "session: decisions, files touched, findings, remaining steps. "
              f"Current summary: {old_summary or '(none)'}\nNew events: {events}")
    for model in reversed(MODELS):
        try:
            msg = call_api(token, [{"role": "user", "content": prompt}], model,
                           tools=None, max_tokens=400)
            text = (msg.get("content") or "").strip()
            if text:
                return text
        except Exception:
            continue
    # Every model failed — keep whatever summary we had rather than losing it.
    return old_summary


def project_rules():
    """Claude Code reads CLAUDE.md — so do we. cwd first, AGENTS.md as the
    open-standard fallback, hard-capped so it can't eat the 8K window."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(os.getcwd(), name)
        try:
            text = open(path).read().strip()
        except OSError:
            continue
        if text:
            if len(text) > RULES_BUDGET:
                text = text[:RULES_BUDGET] + "\n[...rules truncated...]"
            return f"\n\nPROJECT RULES ({name}):\n{text}"
    return ""


def build_messages(state, history):
    sysmsg = SYSTEM.format(cwd=os.getcwd(),
                           mcp=", ".join(MCP_SERVERS) or "(none configured)")
    sysmsg += project_rules()
    if state["summary"]:
        sysmsg += f"\n\nEarlier in this session (compacted): {state['summary']}"
    if state["plan"]:
        sysmsg += f"\n\nCURRENT PLAN:\n{state['plan']}"
    return [{"role": "system", "content": sysmsg}] + history


def trim(token, state, history):
    """Compact instead of forget: pop oldest turns, fold them into the summary."""
    dropped = []
    while context_chars(build_messages(state, history)) > MAX_CONTEXT_CHARS and len(history) > 2:
        drop = history.pop(0)
        dropped.append(drop)
        # Tool results must not be orphaned from their assistant tool_calls message.
        while drop.get("tool_calls") and history and history[0].get("role") == "tool":
            dropped.append(history.pop(0))
    if dropped:
        print(f"{DIM}compacting {len(dropped)} old messages…{RESET}")
        state["summary"] = summarize(token, state["summary"], dropped)


# ---------------------------------------------------------------- agent loops

def sub_agent(token, task):
    """Fresh-context worker: same tools minus agent/plan, its own 8K window.
    This is how slyterm handles repos its main context can't hold."""
    print(f"{GREEN}⏵ sub-agent: {task[:120]}{RESET}")
    state = {"summary": "", "plan": ""}
    history = [{"role": "user", "content": task
                + "\nWork with tools, then reply with ONLY your findings/answer."}]
    answer = "(sub-agent produced no answer)"
    for _ in range(SUB_STEPS):
        trim(token, state, history)
        msg = chat_once(token, build_messages(state, history), stream=False)
        if msg is None:
            return "ERROR: sub-agent got no model response (rate limits)."
        history.append({k: v for k, v in msg.items() if k in
                        ("role", "content", "tool_calls") and v})
        if msg.get("content"):
            answer = msg["content"]
        if not msg.get("tool_calls"):
            break
        run_tool_calls(token, state, history, msg["tool_calls"], depth=1)
    print(f"{GREEN}⏴ sub-agent done{RESET}")
    return truncate(answer)


def run_tool_calls(token, state, history, tool_calls, depth=0):
    for tc in tool_calls:
        fn = tc["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        result = run_tool(token, state, fn["name"], args, depth)
        history.append({"role": "tool", "tool_call_id": tc["id"],
                        "content": truncate(str(result))})


def run_tool(token, state, name, args, depth=0):
    if name == "bash":
        return run_bash(args.get("command", ""))
    if name == "read_file":
        return read_file(args.get("path", ""), args.get("start_line"), args.get("num_lines"))
    if name == "edit_file":
        return edit_file(args.get("path", ""), args.get("old_string", ""),
                         args.get("new_string", ""))
    if name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    if name == "grep":
        return grep_files(args.get("pattern", ""), args.get("path", "."), args.get("glob"))
    if name == "glob":
        return glob_files(args.get("pattern", ""), args.get("path", "."))
    if name == "plan":
        state["plan"] = args.get("content", "")
        print(f"{GREEN}plan:{RESET}\n{state['plan']}")
        return "Plan saved. It stays visible even after context compaction."
    if name == "agent":
        if depth > 0:
            return "ERROR: sub-agents can't spawn sub-agents — do it yourself."
        return sub_agent(token, args.get("task", ""))
    if name == "web_search":
        return web_search(args.get("query", ""))
    if name == "fetch_url":
        return fetch_url(args.get("url", ""))
    if name == "mcp_tools":
        return mcp_tools(args.get("server", ""))
    if name == "mcp_call":
        return mcp_call(args.get("server", ""), args.get("tool", ""),
                        args.get("arguments_json", "{}"))
    return f"ERROR: unknown tool {name}"


def agent(token, state, history, user_input):
    history.append({"role": "user", "content": user_input})
    for _ in range(MAX_STEPS):
        trim(token, state, history)
        msg = chat_once(token, build_messages(state, history))
        if msg is None:
            print(f"{RED}All models failed — likely out of free daily quota (resets midnight UTC).{RESET}")
            return
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        history.append(entry)
        if not msg.get("tool_calls"):
            return
        run_tool_calls(token, state, history, msg["tool_calls"])
    print(f"{RED}Stopped after {MAX_STEPS} steps.{RESET}")


# ---------------------------------------------------------------- sessions

def save_session(state, history):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump({"state": state, "history": history,
                       "cwd": os.getcwd(), "ts": time.time()}, f)
    except OSError:
        pass


def load_session(state, history):
    try:
        data = json.load(open(SESSION_FILE))
    except (OSError, json.JSONDecodeError):
        print(f"{YELLOW}no saved session to continue{RESET}")
        return
    state.update(data.get("state", {}))
    history.extend(data.get("history", []))
    age = int((time.time() - data.get("ts", 0)) / 60)
    print(f"{DIM}resumed session from {age}m ago "
          f"({len(history)} messages, cwd was {data.get('cwd', '?')}){RESET}")


# ---------------------------------------------------------------- REPL

HELP = f"""{BOLD}slash commands{RESET}
  /clear     wipe history + plan (fresh start)
  /compact   fold history into the summary now
  /plan      show the current plan
  /cd DIR    change working directory
  /mcp       list configured MCP servers
  /help      this text"""


def handle_slash(token, state, history, cmd):
    parts = cmd.split(None, 1)
    name = parts[0]
    if name == "/clear":
        history.clear()
        state["summary"] = state["plan"] = ""
        print(f"{DIM}context cleared{RESET}")
    elif name == "/compact":
        if history:
            state["summary"] = summarize(token, state["summary"], history)
            history.clear()
            print(f"{DIM}compacted → {state['summary'][:200]}{RESET}")
        else:
            print(f"{DIM}nothing to compact{RESET}")
    elif name == "/plan":
        print(state["plan"] or f"{DIM}(no plan){RESET}")
    elif name == "/cd":
        try:
            os.chdir(os.path.expanduser(parts[1]))
            print(f"{DIM}cwd → {os.getcwd()}{RESET}")
        except (IndexError, OSError) as e:
            print(f"{RED}{e}{RESET}")
    elif name == "/mcp":
        print("\n".join(sorted(MCP_SERVERS)) or "(none)")
    elif name == "/help":
        print(HELP)
    else:
        print(f"{RED}unknown command {name}{RESET}\n{HELP}")


def main():
    token = gh_token()
    state = {"summary": "", "plan": ""}
    history = []
    argv = sys.argv[1:]
    if argv[:1] == ["-c"]:
        load_session(state, history)
        argv = argv[1:]
    if argv[:1] == ["-p"] and len(argv) >= 2:
        agent(token, state, history, " ".join(argv[1:]))
        save_session(state, history)
        return
    if readline:
        try:
            readline.read_history_file(HISTORY_FILE)
        except OSError:
            pass
        readline.set_history_length(1000)
    print(f"{BOLD}slyterm{RESET} — free agent on GitHub Models (gpt-5 rotation, "
          f"streaming, compaction, sub-agents). {DIM}/help · Ctrl-C interrupts · 'exit' quits.{RESET}")
    while True:
        try:
            user = input(f"{BOLD}> {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit"):
            break
        if user.startswith("/"):
            handle_slash(token, state, history, user)
            continue
        try:
            agent(token, state, history, user)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}interrupted — context kept, prompt again to continue{RESET}")
    save_session(state, history)
    if readline:
        try:
            readline.write_history_file(HISTORY_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
