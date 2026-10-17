# REHAUL — memory, concurrency and large-document handling

Status: Phase 1 implemented on branch `rehaul/phase1-memory` (2026-10-17, not yet deployed);
phases 2–4 proposed.

## TL;DR

- Production pods (`exploit/google-workspace-mcp`, limit 575Mi) were OOM-killed about 40 times
  in the 30 days to 2026-10-14. Two triggers account for the crashes we could attribute:
  bursts of parallel Sheets calls, and reads of large multi-tab Google Docs.
- **Root cause 1 — measured:** every tool call rebuilds a `googleapiclient` service, and the
  library pretty-prints the whole API schema into method docstrings each time. For Sheets this
  costs ~35–74 MiB *per in-flight call*, independent of the data. 20 parallel
  `format_sheet_range` calls: **+704 MiB**. With docstring generation stubbed: **+14 MiB**.
- **Root cause 2 — measured:** Docs reads materialise the whole document (all tabs, all styling)
  as a Python dict, ~5.3× the JSON size. Rendering markdown is cheap. Users usually want one tab.
- **Aggravating factors:** glibc keeps freed memory (baseline ratchets up after each burst;
  `malloc_trim` fixes it), no concurrency control, and a startup port check that turns each OOM
  into ~2 minutes of crash-looping.
- **Direction:** fix the per-call overhead, control concurrency by cost class, make documents
  tab-centric and navigable instead of dumped, move large artefacts to S3 behind presigned URLs,
  and consider a queue + worker pool for heavy tools once phase 1 data is in.

---

## 1. What production shows

Data from CloudWatch Logs Insights (`/eks/paris-prod/exploit`) and the cluster, 2026-09-14 → 2026-10-14.

| Observation | Value |
|---|---|
| Memory request / limit | 500Mi / 575Mi (set by a manual `kubectl edit` on 2026-10-13; was 500Mi/500Mi) |
| Idle RSS | 120–150 MiB; VPA (mode Off) target ≈ 283 MiB |
| Hard kills (in-pod restart with no graceful shutdown) | 48 restarts across 25 pods; 37 excluding the startup crash loops of 2026-09-18 |
| SIGTERM in-pod restarts (liveness) | 3 |
| `Startup aborted: Port 8000 is already in use` | 73 |
| Pod replacements | ~30/day, all graceful (`Shutting down` → `Finished server process`): node consolidation, not OOM. Pods live ~1.5 h |
| Image in production | `build-main-20260922-32` (≈ commit `51ab624`) |

### 1.1 Crash triggers

Determined by listing the tool calls logged in the 2 minutes before each hard kill.

1. **Sheets bursts.** The client fans out 9–20 calls in the same millisecond
   (`format_sheet_range`, `modify_sheet_values`, `read_sheet_values`, `create_table_with_data`);
   the pod dies seconds later. The same pod survived 26 `read_sheet_values` spread over a minute,
   so it is concurrency, not volume.
2. **Docs poison requests.** `get_doc_as_markdown` / `get_doc_content` / `inspect_doc_structure`
   on a large document kills one pod; the client retries and kills the other replica. On
   2026-10-06 09:48–09:51 the same document killed the two replicas four times. The
   2026-10-14 16:27 OOM followed an `inspect_doc_structure` call.

### 1.2 The port check prolongs every OOM

`main.py:958-960` binds a probe socket **without `SO_REUSEADDR`** before starting uvicorn. TIME_WAIT
sockets left by the killed process live in the pod's network namespace, which survives
container restarts, so the probe fails for ~60 s while uvicorn itself (which sets
`SO_REUSEADDR`) would bind fine. Kubernetes then backs off. Each OOM becomes ~2 minutes of
unavailability instead of ~15 s.

### 1.3 Configuration drift

- `LOG_LEVEL=DEBUG` was added by hand but nothing reads it (the app reads
  `WORKSPACE_MCP_LOG_LEVEL`, FastMCP reads `FASTMCP_LOG_LEVEL`). Logging is not a factor.
- The 2026-10-13 manual edits (memory, `LOG_LEVEL`, `TOOLS`) will be overwritten by the next
  Terraform/Helm apply.

---

## 2. Root causes, measured

### 2.1 Method

Harness (currently in `/tmp/memlab`, to be imported into the repo in phase 1):

- `server.py` runs the **real** FastMCP app (`server.http_app(stateless_http=True)` under
  uvicorn, like production) with the real Docs/Sheets/Drive tools. Only
  `auth.service_decorator._authenticate_service` is patched, to return a real
  `googleapiclient` service whose HTTP layer serves synthetic payloads (stored gzip-compressed,
  inflated per request as httplib2 does).
- `client.py` sends N concurrent MCP `tools/call` requests over HTTP and reads the server's
  `VmRSS`/`VmHWM` from `/proc` (peak reset via `clear_refs` after a warm-up call).
- `synth.py` generates Docs JSON shaped like the API's (styled text runs, paragraph styles, lists,
  tables, inline images, tabs) and Sheets payloads.

Caveats: synthetic payloads; local machine, not CPU-limited like the 500m pods; numbers are
"MiB added above the post-warm-up RSS".

### 2.2 `googleapiclient` regenerates schema docstrings on every call

`googleapiclient.discovery.createMethod` builds each method's docstring with
`Schemas.prettyPrintByName`, memoised per service instance. We `build()` a service per tool
call, so every call regenerates them; Sheets' `batchUpdate` schema yields multi-MB strings.
tracemalloc on one `format_sheet_range` call: **74 MiB peak**, top allocations all in
`googleapiclient/schema.py`. No leak: after GC nothing remains. But every in-flight call holds
its copy until its Google request returns, and glibc keeps the freed pages.

| Scenario | Before | Docstrings stubbed |
|---|---|---|
| `format_sheet_range` ×1 | +61 MiB, 0.41 s | +1 MiB, 0.11 s |
| `format_sheet_range` ×20 | **+704 MiB**, 10.9 s | **+14 MiB**, 2.6 s |
| `read_sheet_values` (1000×26) ×1 / ×10 / ×20 | +47 / +311 / +394 MiB | ×20: +80 MiB (payload only) |
| Idle RSS after warm-up | 176 MiB | 112 MiB |

It is also CPU on the event loop, which on a 500m pod plausibly explains the slow health checks
and the liveness restarts.

Fix: replace `Schemas.prettyPrintByName` / `Schemas.prettyPrintSchema` with stubs at startup
(docstrings of dynamically generated API methods are never used by the server). Guard on the
attributes existing and pin with a regression test.

Cost of `build()` plus accessing the main resource, per API (tracemalloc peak):

| API | Original | Stubbed |
|---|---|---|
| Sheets (`spreadsheets()`) | 55.6 MiB | 1.1 MiB |
| Docs (`documents()`) | 35.5 MiB | 0.7 MiB |
| Drive (`files()`) | 1.1 MiB | 0.7 MiB |
| Gmail (`users()`) | 0.5 MiB | 0.5 MiB |

So every Docs call paid ~35 MiB on top of its document as well.

### 2.3 Docs reads materialise the whole document

| Docs API JSON | Parallel calls | Peak added | Response |
|---|---|---|---|
| 5 MB | 1 | +28 MiB | 1.5 MB |
| 20 MB | 1 | +112 MiB | 5.8 MB |
| 50 MB | 1 | +367 MiB | 14.5 MB |
| 20 MB | 3 (`get_doc_as_markdown`) | +409 MiB → 629 MiB total | 5.8 MB |
| 20 MB | 3 (`get_doc_content`) | +416 MiB → 614 MiB total | 5.3 MB |

Stage breakdown, one 20 MB document:

| Stage | Peak | Retained |
|---|---|---|
| `json.loads` → dict | +121 MiB | +102 MiB (5.3× the JSON) |
| `convert_doc_to_markdown` | +9 MiB | +3 MiB |
| JSON-RPC serialisation of the 2.9M-char result | +11 MiB | +6 MiB |

Markdown rendering is not the problem. Two measured mitigations:

- **Field mask** limited to what the converter reads (run text; bold/italic/strikethrough/link/
  font family; `namedStyleType`; bullets/lists; tables; inline object `contentUri`; chips;
  footnotes): JSON 19.2 → 9.1 MiB, dict 102 → 55 MiB, **byte-identical markdown**. Real
  documents carry more styling/suggestion data than the synthetic one, so savings should be larger.
  `inspect_doc_structure` already uses a mask; `get_doc_as_markdown`, `get_doc_content` and
  ~25 other `documents.get` calls do not.
- **Keep only the requested tab while parsing** (`ijson`, C backend): 8-tab 20 MB document,
  peak 122 → **40 MiB**, identical markdown for the tab. The Docs API cannot fetch one tab
  (`documents.get` has no tab parameter; masks select fields, not array entries), so this has
  to happen client-side. See the
  [`documents.get` reference](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get);
  per-tab *export* exists only through an undocumented URL
  ([write-up](https://dev.to/googleworkspace/exporting-individual-tabs-from-google-docs-as-pdfs-2903)).

Also found: `get_doc_as_markdown` wraps the fetch in `wait_for(..., 30)`; on timeout the
thread keeps running, so a retry fetches the document a second time concurrently.

### 2.4 glibc retention

After a burst, Python frees everything but RSS stays near the peak, so each spike raises the
baseline for the next one.

| After burst | RSS 1 s later | With `malloc_trim(0)` after each request |
|---|---|---|
| 20 × `read_sheet_values` | 558 MiB | **176 MiB** |
| 3 × 20 MB Docs reads | 279 MiB | **169 MiB** |

`malloc_trim` restores the baseline but does not lower peaks. `MALLOC_ARENA_MAX=2` did not
help in our runs. jemalloc was not tested.

### 2.5 Other findings (code audit, not measured)

- FastMCP 3.4.7 gives `str` tools an output schema, so every text result is sent twice
  (`content` and `structuredContent.result`). Doubles response size; counts against
  Claude Code's 16 MiB response limit.
- Office text extraction (`core/utils.py`) has no uncompressed-size cap: a 2.2 MB XLSX expanded
  to a 219 MB element tree in the audit.
- Gmail `full=True` / raw reads load whole messages with attachments; attachment and forward
  paths hold several copies; Drive downloads are uncapped unless `WORKSPACE_MCP_MAX_FILE_BYTES`
  is set (it is not, in production).
- Per call: a new `httplib2.Http` + SSL context, a new `httpx.AsyncClient` for token
  verification, an unconditional `gc.collect()` (`_release_google_service_cycles`) — CPU, not memory.
- Default thread pool = `min(32, cpu+4)` = 8 on the 4-vCPU nodes; nothing bounds in-flight tool calls.
- The existing `/attachments/{file_id}` route serves files from pod-local disk with metadata in
  process memory: with 2 replicas, a download URL only works if the GET lands on the same pod.

---

## 3. Client constraints

| | Claude Code | claude.ai / Desktop connectors | MCP spec |
|---|---|---|---|
| Large text results | > 50,000 chars → saved to a file in `tool-results/`, model gets the path. Per-tool `_meta["anthropic/maxResultSizeChars"]` up to 500,000 | ~150,000 chars per result (Anthropic connector docs, not re-verified) | — |
| Hard response limit | 16 MiB per HTTP response / SSE event, request fails | — | — |
| Images | PNG/JPEG/GIF/WebP shown inline **and** original saved to a file (v2.1.283+) | image results supported | `image` content |
| Resources | `@server:uri` mentions; model can list/read resources itself | resources supported; `resource_link` behaviour unverified | `resource_link`, `resources/read`; no range/paging on read |
| Streaming a single result in chunks | — | — | **Not possible** (progress notifications only) |
| Can download a URL | Yes (Bash `curl`), then `Read` handles images and PDF pages | No shell | — |

Sources: <https://code.claude.com/docs/en/mcp>, <https://code.claude.com/docs/en/tools-reference>,
<https://claude.com/docs/connectors/building>,
<https://modelcontextprotocol.io/specification/2025-11-25/server/tools>,
<https://modelcontextprotocol.io/specification/2025-11-25/server/resources>.

Implication: there is no protocol-level way to stream one big result. Big content must be
either **made smaller and navigable** (works everywhere) or **delivered out of band** (URL;
useful for clients with a shell, harmless for others).

---

## 4. Architecture discussion

### 4.1 Concurrency: in-process gate, or queue + worker pool?

**Option A — keep the FastMCP process, add a gate.** Two semaphores per process:
*heavy* tools (full document reads, exports, downloads, Gmail raw/full, attachments) limited to
1–2, *light* tools limited to ~8. Excess calls wait (bounded by a timeout below the client's
own, e.g. 240 s on claude.ai) instead of failing. Optional admission control: read the cgroup's
`memory.max` / `memory.current` before starting a heavy call and keep it queued while headroom
is below its budget.

- Pros: no new infrastructure, no added latency, small change.
- Cons: a poison request still kills the pod and every call in flight on it; memory must be sized
  for `light_limit × light_cost + heavy_limit × worst_heavy_cost`, and "worst" is unbounded
  unless reads are budgeted (section 4.4).

**Option B — thin front + queue + single-job workers.** The MCP front authenticates, runs light
tools inline, and enqueues heavy tools; workers run one job at a time.

- Pros:
  - **Calibration:** a worker's request is `baseline (~110 MiB) + job budget`.
  - **Isolation:** a poison request kills one worker, never the front. Retry read-only jobs a
    bounded number of times, then dead-letter, which ends the ping-pong between replicas.
  - **Scaling and recycling:** KEDA can scale workers on queue lag, and recycling is trivial
    (exit after N jobs or above an RSS threshold; nothing is in flight).
- Cons and requirements:
  - **Reply path:** a tool call is synchronous request/reply, so the front must hold the MCP
    request open until the job's result comes back.
  - **Credentials:** the job carries the user's Google access token (short-lived; encrypt with
    the existing Fernet key or pass a reference to a TTL'd Valkey key).
  - **Writes:** run at most once and never auto-retry. Only `readOnlyHint` tools are retried.
  - **Cost:** a second Deployment, job serialisation and observability.

**Queue technology for Option B:**

| | Valkey Streams | SQS | Postgres (`SKIP LOCKED`) |
|---|---|---|---|
| New infra | none (ElastiCache Valkey already used for OAuth storage) | SQS + IAM | a database, if none exists |
| Added latency | ~ms (blocking read + reply key / `BLPOP`) | tens–hundreds of ms (long polling) + a separate reply channel | ~ms (`LISTEN/NOTIFY`) |
| Request/reply | natural (reply key per call) | not native | `NOTIFY` + result row |
| Retries / DLQ | consumer groups, `XAUTOCLAIM`, manual DLQ | native visibility timeout + DLQ | manual |
| Audit/history | stream retention | none | queryable |
| KEDA scaler | redis-streams | aws-sqs-queue | postgresql |
| Best for | synchronous tool calls of seconds | durable background jobs | jobs that need history or transactions |

For request/reply tool calls, **Valkey Streams** is the best fit: no new infrastructure and the
lowest latency. To verify: blocking commands on ElastiCache *Serverless*. SQS is the right tool if
we later add genuinely asynchronous jobs ("export this 300-page doc, notify me").

**Not recommended — child processes inside the same pod** (e.g. `ProcessPoolExecutor(max_tasks_per_child=…)`).
The cluster runs Kubernetes 1.36 on AL2023 (cgroup v2). Since 1.28 the kubelet kills the
**whole container** when the cgroup hits its limit, unless the nodes set
`singleProcessOOMKill: true` (kubelet option since 1.32). A child's OOM would still take the
pod down. It would give recycling, not isolation. Sources:
[kubernetes#117793](https://github.com/kubernetes/kubernetes/pull/117793),
[kubernetes#126096](https://github.com/kubernetes/kubernetes/pull/126096),
[Preferred Networks write-up](https://tech.preferred.jp/en/blog/kubernetes-single-process-oom-kill/).

**Seam in the code:** `require_google_service` / `require_multiple_services` already separate
"authenticate and build the service" from `await func(service, ...)`. Remote execution can hook
in there: the front resolves credentials, the worker builds the service and calls the same
function by module/qualname. Tool code is unchanged.

**Recommendation:** do Option A now (it is cheap and, with the docstring fix, light calls cost
~1 MiB so the gate mostly matters for heavy tools). Instrument. Then decide on Option B for the
heavy tools only, with phase 1 production numbers in hand. If isolation from poison requests
is a hard requirement, Option B is the only design that provides it.

### 4.2 Recycling memory in the current process

In order of cost:

1. **`malloc_trim(0)`** after heavy calls, when no other heavy call is running (measured, §2.4).
2. **RSS-triggered graceful drain:** when RSS exceeds a soft threshold (e.g. 70% of the cgroup
   limit), turn readiness to 503, finish in-flight calls, exit 0. Kubernetes restarts the
   container while the other replica serves. Requires the port-check fix.
3. **Two worker processes per pod** (gunicorn + uvicorn workers, `max_requests` + jitter): zero-
   downtime recycling inside the pod, at the cost of one extra baseline (~110 MiB after the
   docstring fix). Same cgroup caveat as above: no isolation, only recycling.
4. jemalloc (`LD_PRELOAD` + `MALLOC_CONF=background_thread:true,...`): returns memory over time
   without code changes. Not measured yet.

### 4.3 Large artefacts: presigned S3 URLs

Yes, and preferred over a streaming proxy on the MCP host.

Flow:

1. The tool streams from Google into an S3 multipart upload, holding only one or two parts in
   memory (≥ 5 MiB per part).
2. It generates a presigned GET with a short TTL (10–15 min) and a `Content-Disposition` filename.
3. It returns metadata plus the URL. Claude Code runs `curl -o file` and then reads or greps it.
   claude.ai users get a clickable link.

Why S3 rather than a proxy endpoint:

- It works across replicas.
- Transfers don't tie up MCP pods.
- No download-auth scheme to invent: the presigned URL is the credential.
- It replaces the pod-local `/attachments` store, which is broken with more than one replica.

Design points:

- **Access:** a private bucket in eu-west-3, Block Public Access, SSE-KMS, pod access via IRSA /
  EKS Pod Identity. Presigned URLs made with temporary credentials also expire when those
  credentials do.
- **Retention:** lifecycle expiration has **day** granularity. For shorter retention, add an
  explicit delete sweeper or accept 1 day.
- **Bearer URLs:** they end up in LLM transcripts. Keep TTLs short and keys unguessable (UUID),
  with no PII in key names.
- **Reuse:** key artefacts by `(user, file id, revision/modifiedTime, format)`. Repeated or
  retried requests reuse the object instead of re-fetching. Always scope by user.
- **Data at rest:** this stores copies of Google content in AWS. Needs a short policy decision
  (retention, who can read the bucket).
- **Synergy with Option B:** workers return large results through the same bucket.

Candidates:

- Drive downloads, which replace today's `/tmp` + `/attachments`.
- Docs PDF/DOCX exports. A single tab can be exported via the undocumented
  `docs.google.com/document/d/{id}/export?format=pdf&tab={tabId}`, so this needs a fallback.
- Gmail raw messages and attachments.
- Optionally, full-document markdown when it exceeds what should go inline.

### 4.4 Documents: tab-centric, navigable reads

What users do: they share a big multi-tab document (many tabs, images) and want to work on **one
small tab**, either by pasting a URL that points at it (`…/edit?tab=t.xxxxx`) or by naming it.

What the code does today:

- Only `get_doc_as_markdown` extracts the document ID from a URL (`docs_tools.py:2665`), and it
  drops `?tab=`. The other Docs tools need a bare ID.
- Tabs can be selected by ID only (`_find_tab`), never by title. Getting the ID first requires
  `inspect_doc_structure`, which itself fetches every tab.
- Whatever is requested, the whole document is fetched and parsed.

Proposal:

1. **One document-reference parser for all Docs tools:** accept a bare ID, a URL with `?tab=`
   (and ignore `#heading=`), plus an optional `tab` argument that accepts an ID **or a title**
   (case-insensitive, nested tabs; ambiguous titles return the candidates).
2. **Cheap tab tree:** a `fields=tabs(tabProperties,childTabs(tabProperties,childTabs(tabProperties)))`
   mask returns only titles and IDs (tabs nest up to 3 levels). Use it to resolve names and to
   build an outline.
3. **Masked, single-tab parse:** request only the fields the renderer needs, stream-parse the
   response and keep only the target tab (§2.3: 122 → 40 MiB, before masking). Optionally read
   the HTTP body as a stream (`AuthorizedSession`, `stream=True`) so the raw JSON isn't held
   either.
4. **Outline first for big documents:** when no tab is given and the document is large, return
   the tab outline (titles, IDs, approximate sizes, image counts) and ask for a tab, instead of
   rendering everything. A 20 MB-JSON document renders to ~2.9M characters (~700k tokens),
   which no context window holds anyway.
5. **Images:** the markdown already links images via Google's `contentUri`, so the server never
   downloads them. These are bearer URLs valid ~30 min for the requester. Add
   `get_doc_image(document, object_id)` returning an MCP image (bounded size, optionally
   downscaled) for clients that render images, or an S3 URL for full resolution.
6. **Stop sending text twice** (`output_schema=None` for `str` tools, or an equivalent server
   setting). Halves response size.

---

## 5. Suggested order

Each phase is shippable on its own, with a measurable acceptance check (harness and production
logs).

### Phase 1 — stop the crashes (small PRs)

1. Stub `googleapiclient` schema docstring generation, plus a regression test with a memory
   budget. *Check:* 20 × `format_sheet_range` under +30 MiB in the harness.
2. Fix the port pre-check (`SO_REUSEADDR`, or drop the check).
3. Per-call instrumentation: tool, duration, queue wait, RSS before/after, result bytes.
4. Concurrency gate with heavy/light classes, configurable by environment, waiting with a timeout.
5. `malloc_trim(0)` after heavy calls when idle.
6. Import the harness into the repo (e.g. `scripts/memlab/`) and keep its scenarios as benchmarks.
7. Ops: move the manual `kubectl edit` changes into Terraform/Helm; replace `LOG_LEVEL` with
   `WORKSPACE_MCP_LOG_LEVEL` if needed; set `WORKSPACE_MCP_MAX_FILE_BYTES`.

*Check:* no OOM kill over two weeks of normal usage; instrumentation attributes any remaining
spike to a tool.

**Status (2026-10-17): implemented, not deployed.**

| Item | Where |
|---|---|
| Docstring stub | `auth/google_api_docstrings.py`, applied on import of `auth.google_auth` and `auth.machine_credentials`; `tests/auth/test_google_api_docstrings.py` |
| Port check | `main.check_port_bindable`; `tests/test_main_port_check.py` reproduces a TIME_WAIT socket |
| Gate, `malloc_trim`, metrics | `core/tool_concurrency.py` (`ToolConcurrencyMiddleware`, innermost on `server`, copied into every profile app), `core/process_memory.py`; `tests/core/test_tool_concurrency.py` |
| Harness | `scripts/memlab/` (`scenarios.py --before` prints the comparison) |
| Ops | `../terraform-helm/modules/google-workspace-mcp/eks-paris-prod-values.tftpl` edited, not applied |

Configuration, all validated at startup:

| Variable | Default |
|---|---|
| `WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS` | 2 (0 = unlimited) |
| `WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS` | 8 (0 = unlimited) |
| `WORKSPACE_MCP_TOOL_QUEUE_TIMEOUT` | 120 s, then a "Server busy" tool error |
| `WORKSPACE_MCP_HEAVY_TOOLS` | built-in list (Docs reads and the Docs writes that read the whole document, Drive downloads/imports, Gmail message/attachment reads and sends, Slides presentations, Chat attachments); a comma-separated value replaces it |
| `WORKSPACE_MCP_MALLOC_TRIM` | true; runs in a worker thread (~250 ms on a fragmented 1 GiB heap) |

Metrics: one line per call, for example
`tool_call tool=read_sheet_values class=light status=ok wait_ms=871 duration_ms=674 result_chars=513133 rss_mb=123->165 peak_rss_mb=165 cgroup_mb=410/575 inflight_heavy=0 inflight_light=4`.
`status` is `ok`, `tool_error`, `error`, `busy` or `cancelled`; in-flight counts include the
call itself. Insights example:
`filter log like /tool_call/ | parse log /tool=(?<tool>\S+) .*duration_ms=(?<ms>\d+).*rss_mb=(?<before>\d+)->(?<after>\d+)/`.

Harness, before → after Phase 1 (`scripts/memlab/README.md` has the full table):

| Scenario | Peak | RSS 1 s after |
|---|---|---|
| 20 × `format_sheet_range` | 868 → **120 MiB** | 850 → 120 MiB |
| 20 × `read_sheet_values` | 634 → **169 MiB** | 629 → 144 MiB |
| 3 × `get_doc_as_markdown` (20 MB doc) | 576 → 446 MiB | 233 → 154 MiB |

Docs reads remain proportional to document size until phase 2.

Known limits: a call cancelled by the client releases its slot while its Google request may
still run in a thread; queue waits count against the client's own timeout.

Deployment notes: build an image from this branch and bump `image.tag` in the Terraform values.
The manually added `LOG_LEVEL` env var may survive a Helm upgrade (three-way merge keeps
live-only additions); it is not read, so remove it with `kubectl set env` at leisure.

### Phase 2 — tab-centric Docs

1. Shared document-reference parser (URL `?tab=`, tab by name) across Docs tools.
2. Cheap tab tree and a `get_doc_outline` tool.
3. Field masks on every Docs read path.
4. Single-tab streaming parse (`ijson`, a new dependency) with a byte budget that fails with an
   actionable message ("document too large: choose a tab").
5. Outline-first default for large documents without a tab.
6. Stop duplicating text results in `structuredContent`.

*Check:* reading one tab of a 20 MB, 8-tab document costs under +40 MiB; three parallel reads
stay under +150 MiB.

### Phase 3 — artefacts in S3

1. S3 backend for `AttachmentStorage` (streaming multipart upload, presigned GET), bucket with
   lifecycle and KMS, IRSA.
2. Move Drive downloads, Gmail attachments/raw and Docs/PDF exports to it; retire the pod-local
   `/attachments` store.
3. `get_doc_image`.
4. Caps on Office extraction (uncompressed size) and Gmail `full`/raw paths, with an S3 URL as
   the escape hatch rather than truncation.

### Phase 4 — decide on the worker pool

Using phase 1–3 production data: if heavy calls still dominate memory, or isolation from poison
requests is required, move heavy read tools to Valkey Streams + single-job workers (§4.1),
reusing the S3 artefact path for results.

---

## 6. Decisions and open questions

Decided 2026-10-17:

- **Clients.** Mostly Claude Code / Codex, and LibreChat in two modes:
  - *classic*: LibreChat is the MCP client for a model that usually has no shell. Outline,
    tab reads and image results matter most; S3 URLs are only links for the human.
  - *wengoagent*: the model is a Claude Code instance running in a container, seeing the
    LibreChat tools as stdio MCP servers. It has a shell, so S3 URLs are directly usable,
    provided that container can reach S3 (check its egress).

  Design for both: every large-content tool must stay useful without a shell (phase 2), and
  URLs are an addition (phase 3). Codex's handling of large MCP results is still to be checked.
- **S3.** A private bucket dedicated to this project, created in
  `../terraform-helm/modules/google-workspace-mcp`, encrypted with the AWS-managed KMS key.
- **Latency.** Under 1 s added is invisible to users. This fits Valkey Streams for a phase 4
  worker pool. Note that gate queue waits during client fan-out bursts can exceed 1 s; that is
  the price of not crashing.
- **Pod churn.** To be addressed after phase 1 is deployed.

Still open:

- Is node-level `singleProcessOOMKill` acceptable on the Karpenter node classes? It only
  matters if in-pod worker processes are ever considered.
- Retention period for S3 artefacts: one day via lifecycle rule, or shorter with a sweeper.
