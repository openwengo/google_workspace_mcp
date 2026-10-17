"""Synthetic Google API payloads shaped like real Docs/Sheets responses."""

import json
import random

_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua rapport client équipe projet"
).split()


def _style(rng):
    style = {
        "fontSize": {"magnitude": rng.choice([10, 11, 12]), "unit": "PT"},
        "weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
        "foregroundColor": {
            "color": {"rgbColor": {"red": 0.2, "green": 0.2, "blue": 0.2}}
        },
    }
    if rng.random() < 0.2:
        style["bold"] = True
    if rng.random() < 0.1:
        style["link"] = {"url": "https://example.com/" + str(rng.randrange(10**6))}
    return style


def _paragraph(rng, idx, list_id=None, inline_object=None):
    elements, start = [], idx
    for _ in range(rng.randint(2, 6)):
        text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 15))) + " "
        elements.append(
            {
                "startIndex": idx,
                "endIndex": idx + len(text),
                "textRun": {"content": text, "textStyle": _style(rng)},
            }
        )
        idx += len(text)
    if inline_object:
        elements.append(
            {
                "startIndex": idx,
                "endIndex": idx + 1,
                "inlineObjectElement": {
                    "inlineObjectId": inline_object,
                    "textStyle": {},
                },
            }
        )
        idx += 1
    elements.append(
        {
            "startIndex": idx,
            "endIndex": idx + 1,
            "textRun": {"content": "\n", "textStyle": {}},
        }
    )
    idx += 1
    para = {
        "elements": elements,
        "paragraphStyle": {
            "namedStyleType": rng.choice(
                ["NORMAL_TEXT"] * 8 + ["HEADING_2", "HEADING_3"]
            ),
            "direction": "LEFT_TO_RIGHT",
            "lineSpacing": 115,
            "spaceAbove": {"magnitude": 0, "unit": "PT"},
            "spaceBelow": {"magnitude": 0, "unit": "PT"},
        },
    }
    if list_id:
        para["bullet"] = {"listId": list_id, "textStyle": {}}
    return {"startIndex": start, "endIndex": idx, "paragraph": para}, idx


def _table(rng, idx, rows, cols):
    start, table_rows = idx, []
    idx += 1
    for _ in range(rows):
        cells = []
        for _ in range(cols):
            para, idx2 = _paragraph(rng, idx + 1)
            cells.append(
                {
                    "startIndex": idx,
                    "endIndex": idx2,
                    "content": [para],
                    "tableCellStyle": {"rowSpan": 1, "columnSpan": 1},
                }
            )
            idx = idx2
        table_rows.append(
            {"startIndex": cells[0]["startIndex"], "endIndex": idx, "tableCells": cells}
        )
    return {
        "startIndex": start,
        "endIndex": idx,
        "table": {"rows": rows, "columns": cols, "tableRows": table_rows},
    }, idx


def docs_document(target_mb: float, tabs: int = 3, seed: int = 7) -> dict:
    """A multi-tab document whose JSON serialisation is about target_mb megabytes."""
    rng = random.Random(seed)
    budget = int(target_mb * 1_000_000 / tabs)
    doc_tabs = []
    for t in range(tabs):
        content, idx, size, inline_objects, n = (
            [{"endIndex": 1, "sectionBreak": {}}],
            1,
            0,
            {},
            0,
        )
        while size < budget:
            n += 1
            if n % 40 == 0:
                elem, idx = _table(
                    rng, idx, rows=rng.randint(3, 8), cols=rng.randint(2, 5)
                )
            else:
                obj = None
                if n % 25 == 0:
                    obj = f"kix.img{t}_{n}"
                    inline_objects[obj] = {
                        "objectId": obj,
                        "inlineObjectProperties": {
                            "embeddedObject": {
                                "imageProperties": {
                                    "contentUri": "https://lh7-rt.googleusercontent.com/docsz/"
                                    + "A" * 180
                                },
                                "size": {
                                    "width": {"magnitude": 400, "unit": "PT"},
                                    "height": {"magnitude": 300, "unit": "PT"},
                                },
                            }
                        },
                    }
                elem, idx = _paragraph(
                    rng,
                    idx,
                    list_id="kix.list1" if n % 7 == 0 else None,
                    inline_object=obj,
                )
            content.append(elem)
            size += len(json.dumps(elem))
        doc_tabs.append(
            {
                "tabProperties": {"tabId": f"t.{t}", "title": f"Tab {t}", "index": t},
                "documentTab": {
                    "body": {"content": content},
                    "inlineObjects": inline_objects,
                    "lists": {
                        "kix.list1": {
                            "listProperties": {
                                "nestingLevels": [
                                    {
                                        "glyphSymbol": "●",
                                        "glyphType": "GLYPH_TYPE_UNSPECIFIED",
                                    }
                                ]
                            }
                        }
                    },
                    "documentStyle": {
                        "pageSize": {"width": {"magnitude": 612, "unit": "PT"}}
                    },
                },
            }
        )
    return {
        "documentId": "doc",
        "title": "Synthetic",
        "tabs": doc_tabs,
        "revisionId": "r1",
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
    }


def sheet_values(rows: int = 1000, cols: int = 26) -> dict:
    """A full A1:Z1000 read, the default range of read_sheet_values."""
    rng = random.Random(1)
    return {
        "range": "Sheet1!A1:Z1000",
        "majorDimension": "ROWS",
        "values": [
            [
                " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4)))
                for _ in range(cols)
            ]
            for _ in range(rows)
        ],
    }


def sheet_metadata() -> dict:
    return {
        "spreadsheetId": "s",
        "properties": {"title": "S", "locale": "en_US"},
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Sheet1",
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            }
        ],
    }
