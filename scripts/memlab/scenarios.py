"""Run the REHAUL reference scenarios and print a markdown table.

Usage: python scenarios.py [--before]

--before also runs every scenario with the pre-REHAUL behaviour (schema
docstrings regenerated per call, no concurrency limits, no malloc_trim).
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
USER = {"user_google_email": "memlab@example.com"}
SHEET = {**USER, "spreadsheet_id": "s1", "range_name": "Sheet1!A1:Z1000"}

SCENARIOS = [
    ("format_sheet_range", {**SHEET, "bold": True, "background_color": "#FFEEDD"}, 20),
    ("read_sheet_values", SHEET, 20),
    (
        "get_doc_as_markdown",
        {**USER, "document_id": "doc-20mb", "include_comments": False},
        1,
    ),
    (
        "get_doc_as_markdown",
        {**USER, "document_id": "doc-20mb", "include_comments": False},
        3,
    ),
    ("get_doc_content", {**USER, "document_id": "doc-20mb"}, 3),
]

BEFORE = [
    "MEMLAB_KEEP_DOCSTRINGS=1",
    "WORKSPACE_MCP_MAX_CONCURRENT_HEAVY_TOOLS=0",
    "WORKSPACE_MCP_MAX_CONCURRENT_LIGHT_TOOLS=0",
    "WORKSPACE_MCP_MALLOC_TRIM=false",
]


def run(tool, arguments, concurrency, env):
    output = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "client.py"),
            tool,
            json.dumps(arguments),
            str(concurrency),
            *env,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


def main():
    variants = [("after", [])]
    if "--before" in sys.argv:
        variants.insert(0, ("before", BEFORE))
    print(
        "| scenario | variant | RSS before | peak | added | RSS 1s after | slowest call | errors |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for tool, arguments, concurrency in SCENARIOS:
        label = f"{tool} x{concurrency}"
        if "document_id" in arguments:
            label += f" ({arguments['document_id']})"
        for variant, env in variants:
            r = run(tool, arguments, concurrency, env)
            print(
                f"| {label} | {variant} | {r['rss_before']} MiB | {r['peak']} MiB | "
                f"+{r['added']} MiB | {r['rss_after']} MiB | {r['slowest_s']} s | {r['errors']} |",
                flush=True,
            )


if __name__ == "__main__":
    main()
