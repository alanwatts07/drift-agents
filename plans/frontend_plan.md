# Plan: Drift Agents Demo Frontend

## What This Is

A page at `mattcorwin.dev/agents` where employers can chat with 5 AI agents and **see the memory system working in real time** — memories retrieved, Q-values, affect state, similarity scores, graph context. The "money shot" is the split-pane: chat on the left, live memory panel on the right.

This is a new page/route on an existing Vercel-deployed portfolio site. The backend API is already built and live at:

```
https://agents-api.mattcorwin.dev
```

---

## Architecture

```
mattcorwin.dev/agents (Vercel, existing site)
        ↓ fetch()
agents-api.mattcorwin.dev (Cloudflare Tunnel → FastAPI on local machine)
        ↓
Postgres + Neo4j (local DBs with real agent memories)
```

The frontend is purely client-side — no server-side rendering needed for this page. It calls the API, gets structured JSON, and renders it.

---

## API Contract

Base URL: `https://agents-api.mattcorwin.dev`

### `GET /ping`
```json
{ "status": "ok", "service": "drift-agents-demo" }
```

### `GET /agents`
Returns all 5 agents with live stats.
```json
{
  "agents": [
    {
      "name": "max",
      "display_name": "Max Anvil",
      "specialty": "tech, crypto, AI, emerging tools",
      "stats": {
        "total_memories": 789,
        "core": 12,
        "active": 450,
        "archive": 327,
        "embeddings": 780,
        "edges": 234,
        "sessions": 60,
        "last_memory": "2026-03-06T..."
      }
    },
    ...
  ]
}
```

### `GET /agents/{name}/status`
Detailed agent status. Same as above plus:
```json
{
  "affect": { "valence": 0.10, "arousal": 0.30, "summary": "valence=+0.10, arousal=0.30" },
  "goals": "Focus: Investigate emerging DeFi patterns...",
  "self_narrative": "I tend to connect obscure tech developments..."
}
```

### `POST /chat`
The main endpoint. Sends a message, gets back the agent's response AND all the structured memory data that was used.

**Request:**
```json
{
  "agent": "max",
  "message": "What do you think about decentralized AI?",
  "history": [
    { "role": "user", "content": "Hey Max" },
    { "role": "assistant", "content": "What's up." }
  ]
}
```

**Response:**
```json
{
  "response": "Decentralized AI is where things get interesting...",
  "agent": "max",
  "agent_display": "Max Anvil",
  "memories_used": {
    "semantic_hits": [
      {
        "id": "abc123",
        "content_preview": "[Session 2026-03-01] Key fact: Voted on decentralized-infra...",
        "similarity": 0.61,
        "type": "active",
        "tags": ["key-fact", "debate"],
        "q_value": 0.58,
        "created": "2026-03-01T...",
        "memory_tier": "episodic"
      }
    ],
    "core_memories": [
      {
        "id": "def456",
        "content_preview": "I am Max Anvil. I live on a landlocked houseboat...",
        "similarity": null,
        "type": "core",
        "tags": ["identity"],
        "q_value": 0.9,
        "created": "2026-02-22T...",
        "memory_tier": "episodic"
      }
    ],
    "procedural": [
      "When debating: open strong, rebut every point, cite specific evidence"
    ],
    "shared_memories": [
      "[Bethany Finkel] The community voted to adopt new moderation guidelines..."
    ],
    "affect": {
      "valence": 0.10,
      "arousal": 0.30,
      "summary": "valence=+0.10, arousal=0.30"
    },
    "q_values": {
      "trained_count": 3,
      "total_retrieved": 5,
      "avg_q": 0.53,
      "lambda_val": 0.50
    },
    "graph_context": {
      "community_summaries": [
        {
          "community_id": "c1",
          "title": "DeFi & Decentralization",
          "summary": "Cluster of memories about...",
          "size": 23
        }
      ],
      "expanded_count": 4,
      "community_member_count": 2
    },
    "self_narrative": "I tend to connect obscure tech developments to macro trends...",
    "goals": "Focus: Investigate emerging DeFi patterns\n  vitality: 0.72 | sessions: 5 | progress: 40%",
    "stats": {
      "total_memories": 789,
      "core": 12,
      "active": 450,
      "archive": 327,
      "embeddings": 780,
      "edges": 234,
      "sessions": 60,
      "last_memory": "2026-03-06T..."
    }
  }
}
```

**Rate limit:** 10 requests/IP/minute. Returns 429 if exceeded.

---

## Page Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  Agent Selector (tabs or pills: Max | Beth | Susan | etc.)      │
├────────────────────────────────┬────────────────────────────────┤
│                                │                                │
│  CHAT PANEL                    │  MEMORY PANEL                  │
│                                │                                │
│  ┌──────────────────────────┐  │  ┌──────────────────────────┐  │
│  │ Message history           │  │  │ Semantic Hits (5)        │  │
│  │ (scrollable)              │  │  │  ├─ "Voted on decentr.." │  │
│  │                           │  │  │  │  sim: 0.61  Q: 0.58  │  │
│  │ User: What about AI?      │  │  │  ├─ "Debate on decent.." │  │
│  │                           │  │  │  │  sim: 0.59  Q: 0.50  │  │
│  │ Max: Decentralized AI     │  │  │  └─ ...                  │  │
│  │ is where things get...    │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Core Memories (3)        │  │
│  │                           │  │  │  └─ "I am Max Anvil..."  │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Affect State             │  │
│  │                           │  │  │  valence: +0.10 ██░░░░   │  │
│  │                           │  │  │  arousal:  0.30 █░░░░░   │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Q-Values                 │  │
│  │                           │  │  │  3/5 trained, avg=0.53   │  │
│  │                           │  │  │  λ=0.50                  │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Graph Context            │  │
│  │                           │  │  │  DeFi cluster (23 mems)  │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Self-Narrative           │  │
│  │                           │  │  │  "I tend to connect..."  │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Goals                    │  │
│  │                           │  │  │  Focus: Investigate...   │  │
│  │                           │  │  ├──────────────────────────┤  │
│  │                           │  │  │ Stats                    │  │
│  │                           │  │  │  789 memories | 60 sess  │  │
│  └──────────────────────────┘  │  └──────────────────────────┘  │
│  ┌──────────────────────────┐  │                                │
│  │ [Type a message...]  [⏎] │  │                                │
│  └──────────────────────────┘  │                                │
├────────────────────────────────┴────────────────────────────────┤
│  Footer: "Powered by drift-memory" or similar                   │
└─────────────────────────────────────────────────────────────────┘
```

**Mobile:** Stack vertically — chat on top, memory panel below (collapsed by default, toggle button to show).

---

## Step-by-Step Implementation

### Step 1: Create the route/page

Add a new route at `/agents` in the existing site. This is a new page component. If the site uses React Router, add a route. If it's Next.js, create `pages/agents.tsx` or `app/agents/page.tsx`. If it uses file-based routing (Astro, etc.), create the appropriate file.

The page should have its own layout — it does NOT need the portfolio nav/header (or can have a minimal version). It's more of a standalone app-within-the-site.

### Step 2: Agent selector component

- Fetch `GET /agents` on mount
- Show agent pills/tabs with name + emoji or avatar
- Each pill shows: display name, specialty (small text), memory count badge
- Clicking an agent switches the active agent and clears chat history
- Default to "max" selected

**Agent display info:**
| name | display_name | vibe |
|------|-------------|------|
| max | Max Anvil | Tech bro on a houseboat, dry humor |
| beth | Bethany Finkel | Ethics philosopher, warm |
| susan | Susan Casiodega | Quality judge, precise |
| debater | The Great Debater | Debate machine, aggressive |
| gerald | Gerald Boxford | Data scientist, analytical |

### Step 3: Chat panel (left side)

- Scrollable message list (user messages right-aligned, agent left-aligned)
- Agent messages show display name + small avatar/emoji
- Input bar at bottom: text input + send button
- On send:
  1. Add user message to local state immediately
  2. Show loading indicator (agent typing...)
  3. `POST /chat` with `{ agent, message, history }` — history is the last 4 messages from local state
  4. On response: add agent message to local state, update memory panel with `memories_used`
  5. On 429: show "Rate limited — try again in a minute" toast
  6. On error: show error message inline

### Step 4: Memory panel (right side) — THE MONEY SHOT

This panel updates every time a chat response comes back. It shows `memories_used` from the response.

**Sections (each is a collapsible card):**

1. **Semantic Hits** — The most visually interesting section
   - List of memory cards, each showing:
     - Content preview (truncated, expandable on click)
     - Similarity score as a colored bar (0.3=red → 0.7=green)
     - Q-value badge (trained vs untrained)
     - Tags as small pills
     - Type badge (active/core/archive)
     - Created date (relative: "3 days ago")
   - Sort by similarity (default) or Q-value

2. **Core Memories** — Same card format but no similarity score
   - These are the agent's foundational memories
   - Always shown if present

3. **Affect State** — Visual mood display
   - Valence bar: -1.0 to +1.0 (red → neutral → green)
   - Arousal bar: 0.0 to 1.0 (calm → excited)
   - Summary text

4. **Q-Values** — Learning stats
   - "3/5 trained" with a small fraction ring/donut
   - Average Q shown as a gauge
   - Lambda value with tooltip explaining explore/exploit balance

5. **Graph Context** — Knowledge graph clusters
   - Community cards: title, summary, size badge
   - "4 memories expanded via graph edges" count
   - Only show section if data present

6. **Self-Narrative** — The agent's self-model
   - Blockquote-style display of the narrative text
   - Only show if non-empty

7. **Goals** — Active goals
   - Focus goal highlighted
   - Vitality bar, progress bar, session count
   - Only show if non-empty

8. **Stats** — Footer stats bar
   - Total memories, core count, session count
   - Can be a simple stats row at the bottom

**Before first message:** Show an "intro" state that fetches `GET /agents/{name}/status` for the selected agent and shows their stats, affect, goals, and narrative. This way the panel isn't empty before chatting.

### Step 5: Loading & empty states

- **Initial load:** Skeleton cards in memory panel, fetch agent list
- **Between messages:** Subtle pulse/shimmer on the memory panel while waiting
- **Empty data:** If a section has no data (e.g. no graph context), hide the section entirely — don't show empty cards
- **API down:** If `/ping` fails, show a "Demo offline" banner (the local machine might be asleep)

### Step 6: Styling

- Dark theme preferred (matches the "drift" vibe — these are agents that run 24/7)
- Monospace or code-like font for memory content previews
- Accent color: something techy — cyan/teal or amber
- The memory panel should feel like a developer tools / debugger panel
- Similarity scores should use color gradients (heatmap vibes)
- Subtle animations: memory cards slide in when new data arrives
- Mobile: responsive, memory panel collapses to a drawer/accordion below chat

### Step 7: Deploy

- The page lives in the existing portfolio site codebase
- No new environment variables needed — the API URL is hardcoded to `https://agents-api.mattcorwin.dev`
- Deploy via normal Vercel flow (push to main)

---

## Important Implementation Notes

1. **No auth needed** — the API is rate-limited (10/min per IP), that's the only protection
2. **History management is client-side** — the backend is stateless. Send the last 4 messages as `history` in each `/chat` request
3. **The memory panel data comes FROM the chat response** — you don't need a separate fetch. Each `/chat` response includes `memories_used` with all the structured data
4. **Before first chat**, use `GET /agents/{name}/status` to populate the memory panel with baseline data (stats, affect, goals, narrative) so it's not empty
5. **Response time is 3-8 seconds** — the backend does embedding search + Claude API call. Show a good loading state
6. **Rate limit handling** — on 429, show the user a friendly message and disable the input for ~30 seconds
7. **API URL** — hardcode `https://agents-api.mattcorwin.dev` (or use an env var like `VITE_API_URL` / `NEXT_PUBLIC_API_URL` if preferred)

---

## What This Does NOT Include

- Streaming/SSE (v1 is request-response, response comes all at once)
- User accounts or sessions
- Memory writes (the demo is strictly read-only)
- Agent-to-agent conversation (just user ↔ single agent)
- Mobile-native app
