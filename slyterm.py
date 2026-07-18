#!/usr/bin/env python3
"""slyterm — tiny free Claude-Code-style terminal agent.

Engine: GitHub Models free API (models.github.ai), auth = your existing
`gh auth token`. No sign-in, no API key, no card. The whole agent is kept
small on purpose: the free tier caps every request at ~8K input tokens,
so big agents (opencode, gemini-cli) don't fit — this one does.

Usage:
  slyterm                 interactive REPL
  slyterm -p "prompt"     one-shot, prints answer and exits
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse

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
MAX_CONTEXT_CHARS = 22000       # ~5.5K tokens, leaves room under the 8K cap
MAX_STEPS = 20                  # tool-loop safety stop

BOLD, DIM, CYAN, YELLOW, RED, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"

SYSTEM = (
    "You are slyterm, a terminal coding agent on the user's Mac (macOS, zsh). "
    "Work autonomously: use tools to inspect, edit, run and verify, then give a short final answer. "
    "Prefer bash for anything it can do. Keep outputs small: use head/tail/grep instead of dumping big files. "
    "Never invent file contents — read before editing. "
    "You have NO access to the user's email or cloud accounts — if asked, "
    "say so plainly instead of guessing or grepping ~/Library. "
    "Local MCP servers are available: use mcp_tools(server) to discover a server's tools, "
    "then mcp_call. Servers: {mcp}. Current directory: {cwd}"
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
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[...truncated {len(text) - limit} chars...]"


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


def run_tool(name, args):
    if name == "bash":
        return run_bash(args.get("command", ""))
    if name == "read_file":
        return read_file(args.get("path", ""), args.get("start_line"), args.get("num_lines"))
    if name == "edit_file":
        return edit_file(args.get("path", ""), args.get("old_string", ""),
                         args.get("new_string", ""))
    if name == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
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


def context_chars(messages):
    return sum(len(json.dumps(m)) for m in messages)


def trim(messages):
    """Drop oldest turns (keep system prompt) until we fit under the free-tier cap."""
    while context_chars(messages) > MAX_CONTEXT_CHARS and len(messages) > 3:
        drop = messages.pop(1)
        # Tool results must not be orphaned from their assistant tool_calls message.
        while messages[1:2] and messages[1].get("role") == "tool" and drop.get("tool_calls"):
            drop = messages.pop(1)


def call_api(token, messages, model):
    body = json.dumps({"model": model, "messages": messages,
                       "tools": TOOLS, "max_completion_tokens": 3000}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def chat_once(token, messages):
    """One API call. On 429 hop to the next model immediately; only sleep
    when every model is rate-limited in the same sweep."""
    for sweep in range(4):
        all_limited = True
        for model in MODELS:
            try:
                data = call_api(token, messages, model)
                return data["choices"][0]["message"]
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


def agent(token, messages, user_input):
    messages.append({"role": "user", "content": user_input})
    for _ in range(MAX_STEPS):
        trim(messages)
        msg = chat_once(token, messages)
        if msg is None:
            print(f"{RED}All models failed — likely out of free daily quota (resets midnight UTC).{RESET}")
            return
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        messages.append(entry)
        if msg.get("content"):
            print(f"{CYAN}{msg['content']}{RESET}")
        if not msg.get("tool_calls"):
            return
        for tc in msg["tool_calls"]:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(fn["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": truncate(str(result))})
    print(f"{RED}Stopped after {MAX_STEPS} steps.{RESET}")


def main():
    token = gh_token()
    messages = [{"role": "system", "content": SYSTEM.format(
        cwd=os.getcwd(), mcp=", ".join(MCP_SERVERS) or "(none configured)")}]
    if len(sys.argv) >= 3 and sys.argv[1] == "-p":
        agent(token, messages, " ".join(sys.argv[2:]))
        return
    print(f"{BOLD}slyterm{RESET} — free agent on GitHub Models (gpt-5 rotation). "
          f"{DIM}Ctrl-C or 'exit' to quit.{RESET}")
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
        agent(token, messages, user)


if __name__ == "__main__":
    main()
