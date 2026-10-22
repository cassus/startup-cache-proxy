# Stop paying for Claude Code's startup context twice

**The first session pays for the startup context: CLAUDE.md, memory, skills, agents, MCP tool names and server instructions. Every new session after it reuses all of that from the cache.**

Why it's needed: the startup context is often tens of thousands of tokens. Most of it is identical between sessions, so the API's prompt cache could serve it cheaply. But the cache only matches an identical prefix, and Claude Code puts your first prompt inside that context. A different first prompt therefore breaks the match: only the system prompt and built-in tool definitions in front of it are reused, and everything else (CLAUDE.md, memory, the skill and agent lists, MCP tool names and server instructions) is written to the cache again. See [anthropics/claude-code#98513](https://github.com/anthropics/claude-code/issues/98513).

This repo has two independent parts:

- **The proxy.** It runs locally between Claude Code and the API, moves the parts that are the same in every session to the front and your prompt to the end, so the front part is served from the cache. It only rearranges: no content added, none removed. If it doesn't recognise the layout, it passes the request on unchanged. Your API traffic, including your login, flows through it, so it is built to be easy to trust: the proxy itself is [mitmproxy](https://github.com/mitmproxy/mitmproxy), a well-known open-source HTTPS proxy, and the reordering is one small extension, `reorder.py`, that you or your AI agent can read and check in a few minutes. In detail, it is a local reverse proxy that reorders the first message of a session: the session-invariant startup sections first, then the tool listing, then what changes per session (hook output, date), then your prompt, with cache breakpoints between them.
- **The mod** (`mod/stable-mcp-listing`, optional). A [Claude Code mod](https://code.claude.com/docs/en/plugins/mods/overview) (a plugin with a code module) that keeps the MCP tool list stable. If you send your first prompt before every MCP server has connected, the tool list is shorter than usual, misses the cache, and thousands of tokens are written again. The mod then sends, in place of that shorter list, the full list saved from the last start where everything connected. It does this only when the missing servers are still connecting; any other difference is sent as Claude Code made it. It uses Claude Code's mod hook API (`prompt.attachment`, tested with Claude Code 2.1.303) and works without the proxy.

Result in my tests: a second session with a different first prompt now reuses about 48k tokens from the cache and pays to write almost none. With the mod, this also holds when you start typing before all MCP servers have connected.

## How it works

A cache hit needs the whole request, from the start up to a cache breakpoint (▲), to be byte-identical to an earlier one. The first difference makes that breakpoint and every later one miss. This is the first request of a new session, as Claude Code 2.1.308 sends it, and where the proxy moves each part:

| Without proxy | With proxy | Block         | Contents                                                                                               | Changes when                                    |
| ------------- | ---------- | ------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| 1             | 1          | `tools`       | Built-in tool definitions (Agent, Bash, Read, …)                                                       | Claude Code version, tool settings              |
| 2             | 2          | `system[0]`   | Billing header (`cc_version=…`)                                                                        | Claude Code version                             |
| 3             | 3          | `system[1]`   | "You are Claude Code, …"                                                                               | never                                           |
| 4 ▲1          | 4 ▲1       |               | *Cache breakpoint 1*                                                                                   |                                                 |
| 5             | 5          | `system[2]`   | Main system prompt, including the project's memory path (`~/.claude/projects/-Users-me-myapp/memory/`) | **repo**, Claude Code version                   |
| 6 ▲2          | 6 ▲2       |               | *Cache breakpoint 2*                                                                                   |                                                 |
| 7             | 7          | `messages[0]` | System reminder: global and project CLAUDE.md (with its path), memory index                            | repo, CLAUDE.md or memory edits                 |
| 8             | 8          | `messages[0]` | System reminder: user email; git status if `includeGitInstructions` is on                              | **every commit or file change** (git status on) |
| 9             | 18         | `messages[0]` | **Your first prompt**                                                                                  | **every session**                               |
| 10            | 16         | `messages[1]` | SessionStart hook output                                                                               | whatever your hooks print                       |
| 11            | 9          | `messages[1]` | `# Environment` (working directory, platform, model)                                                   | repo                                            |
| 12            | 13         | `messages[1]` | MCP tool names, plus the "MCP servers still connecting" note if you type before they connect           | MCP server changes                              |
| 13            | 10         | `messages[1]` | Agent types                                                                                            | agent changes                                   |
| 14            | 14         | `messages[1]` | MCP server instructions                                                                                | MCP server changes                              |
| 15            | 11         | `messages[1]` | Skills list                                                                                            | skill changes                                   |
|               | 12 ▲3      |               | *Cache breakpoint 3 (with proxy)*                                                                      |                                                 |
|               | 15 ▲4      |               | *Cache breakpoint 4 (with proxy)*                                                                      |                                                 |
| 16            | 17         | `messages[1]` | Today's date                                                                                           | every day                                       |
| 17 ▲3         |            |               | *Cache breakpoint 3 (without proxy)*                                                                   |                                                 |

Each numbered column is the order of the request, breakpoints included: without the proxy, read the first column top to bottom; with it, sort by the second. `messages[1]` is the startup context, one block that Claude Code sends after your prompt. Without the proxy, ▲3 misses in every new session, because your first prompt comes before the startup context. The proxy moves everything except MCP tools added later into the first message: the stable sections (9–11) and ▲3, then the MCP parts (13–14, separate because they change when your MCP servers change) and ▲4, then hook output and date (16–17), then your prompt (18).

Measured in small test repos: without the proxy, a new session in the same repo reads ~15k (▲2) and writes ~10k; with it, it reads ~25k and writes nothing, even when the hook output changes every session. Across repos the proxy can't help: the repo path is in the main system prompt (▲2), so each repo has its own cache entry, and only ▲1 (~10k) is shared. Your own numbers depend on your CLAUDE.md, memory, skills and MCP servers: on my real setup a new session reuses about 48k.

## Use

Get the code and mitmproxy, then start the proxy:

```sh
git clone https://github.com/cassus/startup-cache-proxy && cd startup-cache-proxy
brew install mitmproxy
REORDER=1 mitmdump -q --mode reverse:https://api.anthropic.com --listen-host 127.0.0.1 -p 8080 -s reorder.py
```

Then tell Claude Code to send its requests through it, in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8080",
    "ENABLE_TOOL_SEARCH": "true"
  }
}
```

While this setting is on, Claude Code only works if the proxy is running. Remove `ANTHROPIC_BASE_URL` to go direct again. `ENABLE_TOOL_SEARCH=true` makes Claude Code list MCP tools by name and load their details only when needed; the proxy expects that layout.

Also turn off Claude Code's git status snapshot: add `includeGitInstructions` as a top-level key in the same file, next to `env`:

```json
{
  "env": { … },
  "includeGitInstructions": false
}
```

The snapshot (branch, `git status`, recent commits) sits ahead of the stable startup context, so every commit or file change makes the next new session write the startup context again (about 10k tokens in my tests). The setting also removes Claude Code's built-in git instructions (how to commit, use `gh`, and so on). If you rely on them, copy the parts you want into your `~/.claude/CLAUDE.md`, which stays the same between sessions and is served from the cache. For example:

```markdown
## Git

- Interactive flags (`-i`, e.g. `git rebase -i`, `git add -i`) are not supported.
- Use the `gh` CLI for GitHub operations (PRs, issues, API).
- Commit or push only when the user asks. If on the default branch, branch first.
- No startup git status in context: run `git status` / `git log` yourself when it matters.
```

Your login or API key passes through unchanged, and the proxy doesn't save your requests. It prints a line when it can't reorder a request, and for an error from the API on a reordered request, the first 300 characters of the error text. Run as a background service (below), these lines go to `~/Library/Logs/startup-cache-proxy.log`. For debugging, `LOG=1` writes every request and response in full to `$STARTUP_CACHE_PROXY_STATE/logs` (default `~/.local/state/startup-cache-proxy`), readable only by you. These logs grow by megabytes per session and contain your conversations, so turn it off when done and delete them.

To add the mod: `claude --plugin-dir mod/stable-mcp-listing`, or put the mod folder's full path in `CLAUDE_CODE_PLUGIN_DIRS` in the same `env` block (after `./deploy`: `~/.local/share/startup-cache-proxy/mod/stable-mcp-listing`). The mod saves the tool list of the last complete start in `~/.claude/plugins/store/stable-mcp-listing_*.json`; delete that file to reset it.

### Run in the background (macOS)

`./deploy` installs the committed version of your clone to `~/.local/share/startup-cache-proxy` and starts it as a launch agent (`startup-cache-proxy.plist`) at every login. It needs mitmproxy from Homebrew, plus `rsync` and `lsof`, which come with macOS. Run it again after a `git pull` to update.

If the proxy stops and Claude Code can't connect: remove `ANTHROPIC_BASE_URL` from `~/.claude/settings.json`, start a new session (it goes direct to the API), fix the proxy, then add the setting back.

To uninstall: remove `ANTHROPIC_BASE_URL` (and the mod folder in `CLAUDE_CODE_PLUGIN_DIRS`) from `~/.claude/settings.json`, then:

```sh
launchctl bootout gui/$UID/com.github.cassus.startup-cache-proxy
rm ~/Library/LaunchAgents/com.github.cassus.startup-cache-proxy.plist
chmod -R u+w ~/.local/share/startup-cache-proxy
rm -rf ~/.local/share/startup-cache-proxy ~/.local/state/startup-cache-proxy ~/Library/Logs/startup-cache-proxy.log
```

## Limits

- This is a workaround. The real fix belongs in Claude Code (see the issue [anthropics/claude-code#98513](https://github.com/anthropics/claude-code/issues/98513)).
- It depends on the exact wording of Claude Code's startup sections, which a new Claude Code version can change. Then the proxy stops reordering, sends requests unchanged, and shows a macOS notification.
- Tested only on macOS, in interactive sessions, with `ENABLE_TOOL_SEARCH=true`, on Haiku and Opus. Subagent requests are passed on unchanged. Not tested with the Agent SDK.
- A claude.ai connector that you remove, or that fails to connect, stays in the mod's saved list until the next start where every server connects. Other MCP servers don't have this problem: the mod only uses its saved list when they are listed as still connecting.
- Terms of use: the mod uses only Claude Code's mod API. The proxy changes the requests Claude Code sends; with an API key that is ordinary API use, but whether it is allowed with a Claude Pro or Max subscription login is unclear. Check the terms for your account.

## Development

See `CLAUDE.md`: a dev proxy on port 8081 (`./dev-proxy`), test sessions through it (`./run-reorder-test.sh`, needs tmux), and how to deploy without cutting off your own sessions.

Not affiliated with Anthropic.
