# startup-cache-proxy: development protocol

The proxy (installed and run as `startup-cache-proxy`) is a local mitmproxy addon that sits between Claude Code and the Anthropic API.
If you are a Claude Code session working on this repo, **your own API traffic may go
through the live proxy**. A broken or stopped proxy cuts your connection mid-task, and
you cannot recover by yourself. Follow these rules.

## Two copies, never mix them

| | Path | Port | State |
|---|---|---|---|
| Dev (this repo) | the repo checkout | 8081 | `./state` (gitignored) |
| Live (installed) | `~/.local/share/startup-cache-proxy` (read-only) | 8080 | `~/.local/state/startup-cache-proxy` |

- Edit only the repo. Never edit, touch or `chmod` the installed copy; `deploy` replaces it.
- mitmproxy hot-reloads the addon on save. That is why the live service runs from a
  separate installed copy: saving in the repo must never reach live sessions.

## Develop and test

1. Start the dev proxy as a background process: `./dev-proxy` (port 8081, `LOG=1`,
   own state dir). It is not a service; stop it when done.
2. Test with throwaway sessions pointed at 8081, never your own session:
   `./run-reorder-test.sh <name> <wait-seconds> "<first message>"` (tmux + Haiku), or
   `claude --settings '{"env":{"ANTHROPIC_BASE_URL":"http://localhost:8081"}}'`.
3. Check the result in `./state/logs/` (requests as sent, responses with cache usage).

## Deploy

1. Commit. `deploy` refuses a dirty tree and installs exactly `HEAD`.
2. Run `./deploy`. It copies `HEAD` to the install path, then:
   - addon-only change: hot reload, no downtime;
   - changed `startup-cache-proxy` wrapper: `launchctl kickstart -k` (about 1 s down);
   - changed plist template: waits for bootout to finish, then bootstrap.
   It exits non-zero if nothing listens on 8080 afterwards.
3. Run it as **one** command. Never split a stop and a start into separate tool calls:
   after the stop, your next call may not reach the API.

## The plugin (`mod/stable-mcp-listing`)

The proxy only reorders; it adds and removes nothing. Keeping the MCP tool listing
stable across sessions (early starts, late "now available" notes) is the plugin's job,
inside Claude Code. Live sessions may load the installed copy through
`CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json`, so `deploy` updates it too
(new sessions pick it up). Test it with `MOD=1 ./run-reorder-test.sh ...`. Claude Code
writes the plugin's type definitions to `.claude-plugin/types/` (gitignored) when it
loads the plugin; after that, type-check with `npx tsc -p mod/stable-mcp-listing`.
Validate with `claude plugin validate mod/stable-mcp-listing`.

## Never

- `launchctl bootout` / `unload` / `kill` of the live service outside `deploy`.
- `launchctl bootstrap` right after `bootout` without waiting: it fails with error 5
  and leaves the proxy down.
- Edit the live service's plist or settings by hand to "try something".

## Emergency bypass

If the live proxy is down, sessions using it fail. The user removes or renames
`ANTHROPIC_BASE_URL` in `~/.claude/settings.json` (env), and starts a new session that
goes direct to the API. Then fix the proxy and re-enable the setting.
