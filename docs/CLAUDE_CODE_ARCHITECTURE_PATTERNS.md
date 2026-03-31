# Claude Code Architecture: Patterns for Verity Agent

Analysis of Claude Code source code (Anthropic's CLI tool) — identifying reusable architectural patterns for the Verity Telegram agent.

**Date:** 2026-03-31
**Source:** Decompiled Claude Code `src/` directory

---

## What Makes Claude Code Unique

### 1. Streaming Agent Loop (async generators)

The core loop in `query.ts` uses TypeScript async generators instead of traditional request-response:

```typescript
async function* query(params): AsyncGenerator<StreamEvent>
```

**Why it matters:**
- Backpressure-aware streaming — results yielded as available, not buffered
- Cancellation safety — `.return()` propagates through all nested generators
- Tools start executing *before* the model finishes responding

### 2. Smart Tool Parallelization

`StreamingToolExecutor` classifies tools at runtime:
- **Read-only** (Read, Grep, Glob) — run in parallel (up to 10 concurrent)
- **Write** (Edit, Bash) — run exclusively, serially
- Context modifiers are queued and applied in order

When the model calls 5 Reads + 1 Edit, reads fly in parallel, Edit waits for them.

### 3. Three-Layer Context Compaction

~4000 lines across 11 files in `services/compact/`:
- **Auto-compact** — triggers when context exceeds threshold (~130K tokens)
- **Micro-compact** — lightweight API-level tool-use grouping
- **Reactive compact** — forks a separate process for compaction inference

### 4. Custom React Terminal Renderer

`ink/` directory: a forked React Reconciler with Yoga (Facebook's layout engine). Full React with components, hooks, virtual scroll — rendering to terminal, not browser.

### 5. Permission Spectrum

From `default` (asks everything) to `bypassPermissions` (allows everything). Each decision is `allow | deny | ask` with optional AI classifiers running in background.

### 6. 18+ Lifecycle Hooks

Pre-tool, post-tool, session-start, compact, etc. Can block execution or run in background. User-configurable via `settings.json`.

---

## Public Reception (as of March 2026)

### Adoption
- 350K+ daily users within 7 weeks of launch
- 1M+ accepted pull requests
- 46% "most loved" (Cursor 19%, Copilot 9%)
- 80.9% on SWE-bench — highest of any coding agent
- 90% of Claude Code's own codebase written by Claude

### Key Criticism
- **Usage limits are #1 complaint** — Anthropic publicly acknowledged the issue
- **4.2x more tokens** than Aider for equivalent work
- Quality degrades as context fills (at 1M tokens, 1 in 4 retrievals fail)
- Reddit consensus: "Higher quality but unusable due to limits. Codex is lower quality but actually usable."

### Competitor Landscape
| Tool | Strength |
|------|----------|
| Cursor | Best IDE integration, fast autocomplete |
| Aider | 4.2x cheaper on tokens, any LLM provider |
| Codex CLI | Open-source, 67K GitHub stars, more generous limits |
| Copilot | Works everywhere, free tier |

Average developer uses 2.3 tools simultaneously.

---

## Reusable Patterns for Verity Agent

### Pattern 1: Tool Registry for Commands

From `Tool.ts` — each command is an object with `call()`, `validateInput()`, `checkPermissions()`:

```python
# Verity adaptation
class BotCommand:
    name: str
    aliases: list[str]

    async def validate_input(self, input: dict, context: ConversationContext) -> ValidationResult:
        """Validate before execution"""
        ...

    async def check_permissions(self, user_id: str, context: ConversationContext) -> PermissionResult:
        """Check user permissions"""
        ...

    async def call(self, input: dict, context: ConversationContext) -> CommandResult:
        """Execute the command"""
        ...

    def is_read_only(self) -> bool:
        """Can this run concurrently?"""
        ...

# Registry
COMMANDS: dict[str, BotCommand] = {
    '/add_task': AddTaskCommand(),
    '/list': ListTasksCommand(),
    '/categorize': CategorizeCommand(),
    '/done': MarkDoneCommand(),
}
```

**Why for Verity:** Decouples command definition from routing. Easy to add new commands, test in isolation, enforce permissions uniformly.

### Pattern 2: Immutable State Store per Conversation

From `state/AppStateStore.ts` — state updates via `setState(prev => ({...prev, updates}))`:

```python
# Verity adaptation
@dataclass(frozen=True)
class ConversationState:
    user_id: str
    chat_id: str
    current_mode: str  # 'menu' | 'adding_task' | 'categorizing' | 'viewing'
    current_task_id: str | None
    recent_task_ids: tuple[str, ...]
    last_action: str
    last_action_time: float

class StateStore:
    def __init__(self):
        self._states: dict[str, ConversationState] = {}

    def get(self, user_id: str, chat_id: str) -> ConversationState | None:
        return self._states.get(f"{user_id}:{chat_id}")

    def update(self, user_id: str, chat_id: str, **kwargs) -> ConversationState:
        key = f"{user_id}:{chat_id}"
        current = self._states.get(key)
        new_state = replace(current, **kwargs)  # dataclass replace
        self._states[key] = new_state
        return new_state
```

**Why for Verity:** Prevents mutation bugs in async handlers. Multiple Telegram updates can arrive simultaneously — immutable state prevents race conditions.

### Pattern 3: Memoized Context with Invalidation

From `context.ts` — expensive lookups cached, invalidated on change:

```python
# Verity adaptation
class ContextCache:
    def __init__(self, db: Database, ttl_seconds: int = 300):
        self._cache: dict[str, tuple[float, UserContext]] = {}
        self._db = db
        self._ttl = ttl_seconds

    async def get_user_context(self, user_id: str) -> UserContext:
        key = user_id
        if key in self._cache:
            timestamp, ctx = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return ctx

        # Expensive: fetch from PostgreSQL
        ctx = UserContext(
            user_id=user_id,
            tasks=await self._db.get_tasks(user_id),
            preferences=await self._db.get_preferences(user_id),
            permissions=await self._db.get_permissions(user_id),
        )
        self._cache[key] = (time.time(), ctx)
        return ctx

    def invalidate(self, user_id: str):
        self._cache.pop(user_id, None)
```

**Why for Verity:** Avoids hitting PostgreSQL on every message. Cache invalidates when tasks change (add/delete/categorize), so data stays fresh.

### Pattern 4: Background Task Tracking for n8n Workflows

From `tasks/types.ts` — tasks with status, retry count, execution tracking:

```python
# Verity adaptation
@dataclass
class WorkflowTask:
    id: str
    user_id: str
    task_type: str  # 'categorize' | 'morning_checkin' | 'evening_summary'
    status: str  # 'pending' | 'running' | 'completed' | 'failed'

    n8n_workflow_id: str | None = None
    n8n_execution_id: str | None = None

    input_data: dict = field(default_factory=dict)
    output_data: dict | None = None
    error: str | None = None

    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    retry_count: int = 0
    max_retries: int = 3

class WorkflowTracker:
    def __init__(self):
        self._tasks: dict[str, WorkflowTask] = {}

    async def execute_workflow(self, task: WorkflowTask, n8n_base_url: str):
        self._tasks[task.id] = task
        task.status = 'running'
        task.started_at = time.time()

        try:
            response = await httpx.post(
                f"{n8n_base_url}/webhook/{task.n8n_workflow_id}",
                json=task.input_data,
                timeout=30.0,
            )
            result = response.json()
            task.status = 'completed'
            task.output_data = result
            task.n8n_execution_id = result.get('executionId')
        except Exception as e:
            task.status = 'failed'
            task.error = str(e)
            task.retry_count += 1

            if task.retry_count < task.max_retries:
                await asyncio.sleep(2 ** task.retry_count)  # exponential backoff
                await self.execute_workflow(task, n8n_base_url)
        finally:
            task.completed_at = time.time()
```

**Why for Verity:** n8n webhooks can fail or timeout. Tracking state + retries prevents lost operations. Useful for morning/evening check-ins that MUST go through.

### Pattern 5: Cost Tracker for API Calls

From `cost-tracker.ts` — accumulates metrics, periodically persists:

```python
# Verity adaptation
@dataclass
class CostTracker:
    session_id: str
    groq_calls: int = 0       # voice transcription
    groq_cost: float = 0.0
    claude_calls: int = 0     # MECE formatting
    claude_cost: float = 0.0
    n8n_calls: int = 0        # workflow executions
    db_queries: int = 0
    telegram_messages_sent: int = 0
    start_time: float = field(default_factory=time.time)

    def track_api_call(self, provider: str, cost: float):
        if provider == 'groq':
            self.groq_calls += 1
            self.groq_cost += cost
        elif provider == 'claude':
            self.claude_calls += 1
            self.claude_cost += cost

    @property
    def total_cost(self) -> float:
        return self.groq_cost + self.claude_cost

    async def persist(self, db: Database):
        await db.execute(
            "INSERT INTO cost_tracking (session_id, groq_calls, groq_cost, claude_calls, claude_cost, n8n_calls) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            [self.session_id, self.groq_calls, self.groq_cost,
             self.claude_calls, self.claude_cost, self.n8n_calls]
        )
```

**Why for Verity:** Groq + Claude API calls cost money. Tracking per-user costs enables usage limits, cost alerts, and budget planning.

### Pattern 6: Event Hook System

From `utils/hooks.ts` — 18+ lifecycle hooks with priority and blocking:

```python
# Verity adaptation
class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, list[tuple[int, Callable]]] = defaultdict(list)

    def register(self, event: str, handler: Callable, priority: int = 50):
        self._hooks[event].append((priority, handler))
        self._hooks[event].sort(key=lambda x: -x[0])  # higher priority first

    async def emit(self, event: str, context: dict) -> dict:
        for priority, handler in self._hooks.get(event, []):
            result = await handler(context)
            if result and result.get('stop'):
                return result
            if result and result.get('context'):
                context = {**context, **result['context']}
        return context

# Usage
hooks = HookRegistry()
hooks.register('message_received', rate_limiter, priority=100)
hooks.register('message_received', spam_filter, priority=90)
hooks.register('task_added', invalidate_cache, priority=80)
hooks.register('task_added', send_confirmation, priority=50)
hooks.register('command_executed', log_command, priority=10)
```

**Why for Verity:** Cleanly separates cross-cutting concerns (rate limiting, logging, cache invalidation) from command logic. Easy to add/remove behavior without touching core code.

---

## Architecture Mapping

| Claude Code Component | Verity Equivalent |
|---|---|
| `Tool.ts` + `tools/` | `BotCommand` registry with validate/check/call |
| `state/AppStateStore.ts` | Frozen `ConversationState` dataclass per user |
| `context.ts` memoize | `ContextCache` with TTL + invalidation |
| `tasks/types.ts` | `WorkflowTracker` for n8n executions |
| `cost-tracker.ts` | `CostTracker` for Groq/Claude/n8n costs |
| `utils/hooks.ts` | `HookRegistry` with priority-based event system |
| `services/compact/` | Not needed (Telegram messages are short) |
| `ink/` (React terminal) | Not applicable (Telegram UI is message-based) |
| `query.ts` async generators | Could use for streaming long responses |

---

## Implementation Priority

1. **Tool Registry** — foundation for all commands, do first
2. **State Store** — needed for multi-step conversations (adding tasks, categorizing)
3. **Context Cache** — performance win, add after basic flow works
4. **Hook System** — add when cross-cutting concerns emerge (rate limiting, logging)
5. **Workflow Tracker** — add when n8n reliability becomes an issue
6. **Cost Tracker** — add when approaching production usage
