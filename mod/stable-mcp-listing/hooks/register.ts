import type { Register } from 'claude-code'

// The startup tool listing and MCP instructions vary with how far the MCP
// connections got: servers "still connecting" instead of their tools, or a
// server's tools listed but its instructions not yet in. Either makes the
// session's startup context differ from every other one, so it misses the
// prompt cache.
//
// This mod keeps the listing and instructions of the last start where every
// server had connected. A new session gets that saved text only when the
// difference is servers not in yet: every tool it lists is in the saved
// listing, and every saved tool it lacks belongs to a server still connecting
// or a claude.ai connector. A claude.ai connector that is removed or fails
// stays in the saved text until the next complete start replaces it.
// Otherwise it gets its own text, unchanged. In a session that got the saved
// text, a later "now available" note is dropped only when it repeats what the
// saved text already said; a note with anything new is kept.
const LISTING = 'deferred_tools_delta'
const INSTRUCTIONS = 'mcp_instructions_delta'
const CONNECTING = 'The following MCP servers are still connecting'
const FULL_LISTING = 'The following deferred tools are now available'

const serverKey = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_')
const serverOf = (tool: string) => (tool.startsWith('mcp__') ? (tool.split('__')[1] ?? '') : '')

/** Sorted names of the MCP servers with tools in a set of tool names. */
const serversOf = (tools: Iterable<string>) => [...new Set([...tools].map(serverOf).filter(Boolean))].sort()

/**
 * A listing attachment, split into the tools loaded right now ("just became
 * available", with their schemas sent alongside) and the deferred listing.
 * Only the deferred listing is ever swapped: the loaded part stays the session's own.
 */
const split = (text: string) => {
  const i = text.indexOf(FULL_LISTING)
  return i < 0 ? { loaded: text, deferred: '' } : { loaded: text.slice(0, i), deferred: text.slice(i) }
}

/** Tool names of a deferred listing (one per line, no spaces), before any connecting notice. */
const toolNames = (deferred: string) => {
  const i = deferred.indexOf(CONNECTING)
  return new Set(
    (i >= 0 ? deferred.slice(0, i) : deferred).split('\n').slice(1).filter((l) => l && !l.includes(' ')),
  )
}

/** Keys of the servers a listing says are still connecting. */
const connecting = (text: string) => {
  const i = text.indexOf(CONNECTING)
  if (i < 0) return new Set<string>()
  const lines = text.slice(i).split('\n').slice(1)
  const end = lines.indexOf('')
  return new Set((end >= 0 ? lines.slice(0, end) : lines).map(serverKey))
}

/** '## <server>' sections of the MCP instructions, by heading. */
const sections = (text: string) =>
  new Map(
    text
      .split(/^(?=## )/m)
      .filter((p) => p.startsWith('## '))
      .map((p) => [p.split('\n', 1)[0], p.trimEnd()] as const),
  )

/**
 * The saved listing may stand in for the real one: the only difference is
 * servers not in yet. Such a server is listed as still connecting, or is a
 * claude.ai connector (those are not mentioned at all until they connect).
 * Any other missing server, or one listed with fewer tools, is a real difference.
 */
const onlyNotYetConnected = (real: string, saved: string) => {
  const have = toolNames(real)
  const had = toolNames(saved)
  const waiting = connecting(real)
  const notYet = (server: string) => waiting.has(server) || server.startsWith('claude_ai_')
  return [...have].every((t) => had.has(t)) && [...had].every((t) => have.has(t) || notYet(serverOf(t)))
}

/** Every section of the real instructions is in the saved ones, word for word. */
const coveredBy = (real: string, saved: string) => {
  const s = sections(saved)
  return [...sections(real)].every(([h, t]) => s.get(h) === t)
}

/** No answer yet: the attachments belong to a new session's first prompt, not a resumed or reloaded one. */
const isStart = async ($: { session: { messages: () => Promise<readonly { role: string }[]> } }) =>
  !(await $.session.messages()).some((m) => m.role === 'assistant')

export const register: Register = (on) => {
  // Per session: whether its startup texts have been answered, and whether
  // with the saved ones. Module variables reset on a reload; the next
  // attachments then pass unchanged, since the session already has answers.
  let listingSeen = false
  let instructionsSeen = false
  let substituted = false
  let savedTools = new Set<string>()
  let savedInstructions = ''
  let completeListing: string | undefined

  on('session.end', (_$, e, next) => {
    listingSeen = instructionsSeen = substituted = false
    completeListing = undefined
    return next(e)
  })

  // After a compaction the context is rebuilt from the session's own texts.
  on('session.compact', (_$, e, next) => {
    if (!e.agentId) substituted = false
    return next(e)
  })

  on('prompt.attachment', async ($, e, next) => {
    if ((e as { agentId?: string }).agentId || (e.type !== LISTING && e.type !== INSTRUCTIONS)) return next(e)

    if (e.type === LISTING && !listingSeen) {
      listingSeen = true
      if (!(await isStart($))) return next(e)
      const saved = (await $.store.get(LISTING)) as string | undefined
      const real = split(e.text)
      if (!real.deferred.includes(CONNECTING) && real.deferred.includes('mcp__')) completeListing = real.deferred
      if (!saved || saved === real.deferred) return next(e)
      if (!onlyNotYetConnected(real.deferred, saved)) {
        $.ui.log('stable-mcp-listing: tool listing differs from the snapshot in more than missing servers, sent as is')
        return next(e)
      }
      substituted = true
      savedTools = toolNames(saved)
      const have = new Set(serversOf(toolNames(real.deferred)))
      const filled = serversOf(savedTools).filter((s) => !have.has(s))
      $.ui.log(`stable-mcp-listing: filled in ${filled.length} missing MCP server(s) from the snapshot: ${filled.join(', ')}`)
      return next({ ...e, text: real.loaded + saved })
    }

    if (e.type === INSTRUCTIONS && !instructionsSeen) {
      instructionsSeen = true
      const saved = ((await $.store.get(INSTRUCTIONS)) as string | undefined) ?? ''
      // A complete start refreshes the saved texts, unless its instructions
      // lack a saved section of a server it lists (that server's instructions
      // not in yet). Sections of removed servers don't block the refresh.
      // A session that got the saved listing never refreshes: its listing
      // lacked servers.
      const listed = new Set([...toolNames(completeListing ?? '')].map(serverOf))
      const real = sections(e.text)
      if (
        completeListing &&
        !substituted &&
        [...sections(saved).keys()].every((h) => real.has(h) || !listed.has(serverKey((h ?? '').slice(3))))
      ) {
        await $.store.set(LISTING, completeListing)
        await $.store.set(INSTRUCTIONS, e.text)
        const servers = serversOf(toolNames(completeListing))
        $.ui.log(`stable-mcp-listing: saved full snapshot of ${servers.length} MCP servers: ${servers.join(', ')}`)
      }
      if (substituted && saved && coveredBy(e.text, saved)) {
        savedInstructions = saved
        $.ui.log('stable-mcp-listing: MCP instructions taken from the snapshot')
        return next({ ...e, text: saved })
      }
      return next(e)
    }

    // A later note in a session that got the saved texts: drop it when it
    // only repeats them.
    if (!substituted) return next(e)
    if (e.type === INSTRUCTIONS) {
      return savedInstructions !== '' && coveredBy(e.text, savedInstructions) ? { text: null } : next(e)
    }
    const { loaded, deferred } = split(e.text)
    const repeats =
      deferred !== '' &&
      !deferred.includes(CONNECTING) &&
      !e.text.includes('no longer available') &&
      [...toolNames(deferred)].every((t) => savedTools.has(t))
    if (!repeats) return next(e)
    // Keep the part that announces loaded tools: their schemas come with it.
    return loaded.trim() ? next({ ...e, text: loaded.trimEnd() }) : { text: null }
  })
}
