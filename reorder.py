"""mitmproxy addon: reorder the first message of a Claude Code session so its
startup context is cached across sessions (REORDER=1), and optionally log every
messages request and its response (LOG=1).

Run: [REORDER=1] [LOG=1] mitmdump -q --mode reverse:https://api.anthropic.com --listen-host 127.0.0.1 -p 8080 -s reorder.py
Then start claude with ENABLE_TOOL_SEARCH=true ANTHROPIC_BASE_URL=http://localhost:8080.
Runtime data (logs) lives in $STARTUP_CACHE_PROXY_STATE, default ~/.local/state/startup-cache-proxy.
With LOG=1 each call is written to $STARTUP_CACHE_PROXY_STATE/logs/<time>-<n>.req.json (as sent) and .resp.txt.
"""
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("STARTUP_CACHE_PROXY_STATE", "~/.local/state/startup-cache-proxy")).expanduser()
LOGS = STATE / "logs"
if os.environ.get("LOG"):
    # The logs hold whole conversations: readable by the owner only.
    os.umask(0o077)
    LOGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOGS.chmod(0o700)
counter = itertools.count(1)


def log(text):
    """To stderr: survives hot reloads and -q, unlike print."""
    print(time.strftime("%H:%M:%S"), text, file=sys.stderr, flush=True)

# Sections of Claude Code's startup system message, by the text they start
# with, grouped by how often they change. Text before the first section is the
# SessionStart hook output (volatile).
STABLE = ("# Environment", "Available agent types for the Agent tool:", "The following skills are available for use with the Skill tool:")
INLINE = "The following tools just became available"
LISTING = "The following deferred tools are now available"
INSTRUCTIONS = "# MCP Server Instructions"
DATE = "Today's date is"


def is_messages(flow):
    return "/v1/messages" in flow.request.path and "count_tokens" not in flow.request.path


def split_startup(text):
    """Return {stable, inline, listing, instructions, volatile} text, or None if a section is not recognised."""
    heads = STABLE + (INLINE, LISTING, INSTRUCTIONS, DATE)
    # Without SessionStart hook output the text starts with a section heading:
    # a leading newline lets it match like the others. It is cut off again below.
    text = "\n" + text
    starts = sorted({m for h in heads for m in [text.find("\n" + h)] if m >= 0})
    if not starts or not all(("\n" + h) in text for h in STABLE):
        return None
    out = {"stable": "", "inline": "", "listing": "", "instructions": "", "volatile": text[1 : starts[0] + 1]}
    for a, b in zip(starts, starts[1:] + [len(text) - 1]):
        part = text[a + 1 : b + 1]
        if part.startswith(STABLE):
            out["stable"] += part
        elif part.startswith(INLINE):
            out["inline"] += part
        elif part.startswith(LISTING):
            out["listing"] += part
        elif part.startswith(INSTRUCTIONS):
            out["instructions"] += part
        elif part.startswith(DATE):
            out["volatile"] += part
        else:
            return None
    return out


def reorder(body):
    # REORDER=1: send the first user message as
    #   CLAUDE.md + memory, stable startup sections ▲, tool names + MCP
    #   instructions ▲, hook output + date, first prompt
    # (▲ = cache breakpoint), then the startup system message with only its
    # added tools (a system message must come right before an assistant
    # message). No content is added or removed: sections are moved, wrapped
    # in a system reminder, and the cache breakpoints moved. Unrecognised
    # layout, or no breakpoint from Claude Code → request sent unchanged.
    msgs = body.get("messages", [])
    if len(msgs) < 2 or msgs[0].get("role") != "user" or msgs[1].get("role") != "system":
        return False
    first, startup = msgs[0]["content"], msgs[1]["content"]
    if isinstance(startup, str):
        # Later requests send a text-only startup message as a plain string.
        startup = [{"type": "text", "text": startup}]
    if not isinstance(first, list) or not isinstance(startup, list) or startup[0].get("type") != "text":
        return False
    parts = split_startup(startup[0]["text"])
    if parts is None:
        return False
    context = [b for b in first if b.get("type") == "text" and b["text"].startswith("<system-reminder>")]
    prompt = first[len(context):]
    if not context or not prompt or first[: len(context)] != context:
        return False
    # Our breakpoints copy Claude Code's own (its last system block), so the
    # TTL matches what this account may use and the TTL order stays valid.
    # Without one, Claude Code isn't caching: leave the request alone.
    system = body.get("system", [])
    cache = system[-1].get("cache_control") if isinstance(system, list) and system else None
    if not cache:
        return False
    for b in context + startup + prompt:
        b.pop("cache_control", None)
    for s in body.get("system", [])[:-1]:
        s.pop("cache_control", None)
    wrap = lambda t: {"type": "text", "text": f"<system-reminder>\n{t}</system-reminder>"}
    blocks = [{**wrap(parts["stable"]), "cache_control": cache}, {**wrap(parts["inline"] + parts["listing"] + parts["instructions"]), "cache_control": cache}, wrap(parts["volatile"])]
    tools = startup[1:]
    rest = msgs[2:]
    body["messages"] = [{"role": "user", "content": context + blocks + prompt}] + ([{"role": "system", "content": tools}] if tools else []) + rest
    # The last message with content gets the breakpoint: after a mid-session
    # /effort change Claude Code ends the request with an empty system message.
    last = next(m for m in reversed(body["messages"]) if m["content"])
    # At most 4 breakpoints: last system block + our 2 + the last message.
    for m in rest:
        if m is not last and isinstance(m["content"], list):
            for b in m["content"]:
                b.pop("cache_control", None)
    if isinstance(last["content"], str):
        # Typed follow-ups come as a plain string; a breakpoint needs a block.
        last["content"] = [{"type": "text", "text": last["content"]}]
    if not any("cache_control" in b for b in last["content"]):
        last["content"][-1]["cache_control"] = cache
    return True


last_warning = 0.0


def warn(text):
    """macOS notification, at most one per hour so a changed layout doesn't
    notify on every request."""
    global last_warning
    if time.time() - last_warning < 3600:
        return
    last_warning = time.time()
    if sys.platform != "darwin":
        return
    # The text goes in as an argument, so nothing in it is read as AppleScript.
    subprocess.Popen(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "startup-cache-proxy"', "-e", "end run", text])


def request(flow):
    if not is_messages(flow) or not os.environ.get("REORDER"):
        return
    try:
        body = json.loads(flow.request.content)
        done = reorder(body)
    except Exception as e:
        log(f"REORDER: failed ({e!r}), sent unchanged")
        warn("The cache proxy hit an error and sent a request unchanged. Sessions work, but new sessions may not reuse the cache.")
        return
    if done:
        flow.metadata["cc_reordered"] = True
        flow.request.content = json.dumps(body).encode()
    # Warn only when every stable section is there: a subagent's start has
    # no agent list, so it is passed on quietly.
    elif len(body.get("messages", [])) >= 2 and body["messages"][1].get("role") == "system" and all(h in json.dumps(body["messages"][1]) for h in STABLE):
        log("REORDER: layout not recognised, sent unchanged")
        if os.environ.get("LOG"):
            (STATE / "unrecognised.json").write_bytes(flow.request.content)
        warn("Claude Code's startup layout changed, so the cache proxy sent it unchanged. Sessions work, but new sessions don't reuse the cache.")


def log_stem():
    return LOGS / f"{time.strftime('%H%M%S')}-{next(counter):03d}"


def tee(path):
    """Stream callable: pass chunks through unchanged, copying them to path."""
    f = open(path, "wb")

    def chunk(data):
        if data:
            f.write(data)
            f.flush()
        else:
            f.close()  # an empty chunk ends the stream
        return data

    return chunk


def responseheaders(flow):
    # Successful messages responses are streamed through, so Claude Code sees
    # tokens as they come instead of after the whole answer. Errors are
    # buffered so response() can show their text.
    if not is_messages(flow) or flow.response.status_code != 200:
        return
    if os.environ.get("LOG"):
        stem = log_stem()
        stem.with_suffix(".req.json").write_bytes(flow.request.content or b"")
        # Streamed chunks are still content-encoded.
        enc = flow.response.headers.get("content-encoding", "")
        flow.response.stream = tee(stem.with_suffix(".resp.txt" + (".gz" if enc == "gzip" else "." + enc if enc else "")))
    else:
        flow.response.stream = True


def response(flow):
    if not is_messages(flow) or flow.response.stream:
        return
    if "cc_reordered" in flow.metadata and flow.response.status_code >= 400:
        text = (flow.response.text or "")[:300]
        log(f"REORDER: rewritten request got {flow.response.status_code}: {text}")
        if flow.response.status_code < 500 and flow.response.status_code != 429:
            warn(f"The API rejected a request rewritten by the cache proxy ({flow.response.status_code}): {text}")
    if os.environ.get("LOG"):
        stem = log_stem()
        stem.with_suffix(".req.json").write_bytes(flow.request.content or b"")
        stem.with_suffix(".resp.txt").write_bytes(flow.response.content or b"")
