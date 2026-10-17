# memlab — tool memory measurements

Runs the real server (`server.http_app(stateless_http=True)` under uvicorn, as
in production) with the real Docs, Sheets and Drive tools. Google is faked at
the HTTP layer only: tools receive a genuine `googleapiclient` service whose
transport serves synthetic payloads (`synth.py`, shaped like Docs/Sheets API
responses). A client fires concurrent MCP `tools/call` requests over HTTP and
reads the server's `VmRSS`/`VmHWM` from `/proc`. Linux only.

See [REHAUL.md](../../REHAUL.md) for the findings these scenarios reproduce.

## Usage

```bash
# Reference table; --before adds the pre-REHAUL behaviour for comparison
uv run python scripts/memlab/scenarios.py --before

# One scenario: TOOL ARGS_JSON CONCURRENCY [server ENV=VALUE ...]
uv run python scripts/memlab/client.py get_doc_as_markdown \
  '{"user_google_email":"m@example.com","document_id":"doc-50mb","include_comments":false}' 3 \
  WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS=1
```

- Docs IDs choose the payload: `doc-<N>mb` serves about N MB of Docs API JSON (3 tabs).
- Sheets reads return a 1000×26 grid; Sheets writes return an empty `batchUpdate` reply.
- `MEMLAB_KEEP_DOCSTRINGS=1` restores googleapiclient's per-call schema docstrings.
- `MEMLAB_SERVER_LOG=path` keeps the server's log, including its `tool_call` metrics lines.

Numbers are MiB added above the RSS measured after one warm-up call. Payloads
are synthetic and the machine is not CPU-limited like the pods: compare runs
with each other, not with production absolutes.

## Reference results (2026-10-17, Phase 1)

| scenario | variant | RSS before | peak | added | RSS 1s after | slowest call |
|---|---|---|---|---|---|---|
| format_sheet_range x20 | before | 176 MiB | 868 MiB | +692 MiB | 850 MiB | 7.24 s |
| format_sheet_range x20 | after | 112 MiB | 120 MiB | +8 MiB | 120 MiB | 2.18 s |
| read_sheet_values x20 | before | 173 MiB | 634 MiB | +461 MiB | 629 MiB | 7.08 s |
| read_sheet_values x20 | after | 115 MiB | 169 MiB | +54 MiB | 144 MiB | 3.21 s |
| get_doc_as_markdown x1 (doc-20mb) | before | 221 MiB | 330 MiB | +109 MiB | 230 MiB | 1.8 s |
| get_doc_as_markdown x1 (doc-20mb) | after | 127 MiB | 270 MiB | +143 MiB | 123 MiB | 1.68 s |
| get_doc_as_markdown x3 (doc-20mb) | before | 221 MiB | 576 MiB | +355 MiB | 233 MiB | 6.0 s |
| get_doc_as_markdown x3 (doc-20mb) | after | 151 MiB | 446 MiB | +295 MiB | 154 MiB | 4.95 s |
| get_doc_content x3 (doc-20mb) | before | 201 MiB | 474 MiB | +273 MiB | 261 MiB | 6.32 s |
| get_doc_content x3 (doc-20mb) | after | 124 MiB | 417 MiB | +293 MiB | 149 MiB | 5.82 s |
