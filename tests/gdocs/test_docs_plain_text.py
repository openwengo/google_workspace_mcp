"""Tests for context-preserving Google Docs plain-text rendering."""

import logging

import pytest

from gdocs.docs_links import LinkTarget, resolve_link_target
from gdocs.docs_plain_text import render_doc_to_plain_text


def _paragraph(*elements, positioned_object_ids=None):
    paragraph = {"elements": list(elements)}
    if positioned_object_ids is not None:
        paragraph["positionedObjectIds"] = positioned_object_ids
    return {"paragraph": paragraph}


def _text(content, *, link=None):
    style = {"link": link} if link is not None else {}
    return {"textRun": {"content": content, "textStyle": style}}


def _tab(title, tab_id, content, **document_tab_fields):
    return {
        "tabProperties": {"title": title, "tabId": tab_id},
        "documentTab": {
            "body": {"content": content},
            **document_tab_fields,
        },
    }


class TestCompatibility:
    def test_ordinary_paragraphs_are_byte_for_byte_unchanged(self):
        doc = {
            "body": {
                "content": [
                    _paragraph(_text("First paragraph\n")),
                    _paragraph(_text("\n")),
                    _paragraph(_text(" \t\n")),
                    _paragraph(_text("Second "), _text("paragraph\n")),
                    _paragraph(_text("\n")),
                ]
            }
        }

        assert render_doc_to_plain_text(doc) == (
            "First paragraph\n\n \t\nSecond paragraph\n\n"
        )

    @pytest.mark.parametrize("content", ["", "\n", " \t\n"])
    def test_empty_and_whitespace_only_contexts_are_preserved(self, content):
        doc = {"body": {"content": [_paragraph(_text(content))]}}

        assert render_doc_to_plain_text(doc) == content

    def test_single_and_nested_tabs_keep_existing_marker_format(self):
        parent = _tab("Main", "tab-1", [_paragraph(_text("Parent\n"))])
        parent["childTabs"] = [_tab("Details", "tab-2", [_paragraph(_text("Child\n"))])]

        assert render_doc_to_plain_text({"tabs": [parent]}) == (
            "\n--- TAB: Main (ID: tab-1) ---\n"
            "Parent\n"
            "\n--- TAB:     Details (ID: tab-2) ---\n"
            "Child\n"
        )


class TestLinkTargets:
    def test_resolver_uses_documented_priority(self):
        assert resolve_link_target(
            {
                "url": "https://example.com",
                "heading": {"id": "heading-1", "tabId": "tab-1"},
                "bookmark": {"id": "bookmark-1", "tabId": "tab-1"},
                "tabId": "tab-2",
            }
        ) == LinkTarget("url", "https://example.com")

    def test_resolver_supports_new_and_legacy_internal_shapes(self):
        assert resolve_link_target(
            {"heading": {"id": "heading-1", "tabId": "tab-2"}}
        ) == LinkTarget("heading", "heading-1", "tab-2")
        assert resolve_link_target(
            {"headingId": "legacy-heading", "tabId": "tab-2"}
        ) == LinkTarget("heading", "legacy-heading", "tab-2")
        assert resolve_link_target(
            {"bookmark": {"id": "bookmark-1", "tabId": "tab-3"}}
        ) == LinkTarget("bookmark", "bookmark-1", "tab-3")
        assert resolve_link_target(
            {"bookmarkId": "legacy-bookmark", "tabId": "tab-3"}
        ) == LinkTarget("bookmark", "legacy-bookmark", "tab-3")
        assert resolve_link_target({"tabId": "tab-4"}) == LinkTarget("tab", "tab-4")
        assert resolve_link_target({"unsupported": "target"}) == LinkTarget("unknown")

    def test_plain_text_renders_external_and_internal_targets(self):
        doc = {
            "tabs": [
                _tab(
                    "Main",
                    "tab-current",
                    [
                        _paragraph(
                            _text(
                                "External",
                                link={"url": "https://example.com"},
                            ),
                            _text(" "),
                            _text(
                                "Heading",
                                link={
                                    "heading": {
                                        "id": "heading-1",
                                        "tabId": "tab-target",
                                    }
                                },
                            ),
                            _text(" "),
                            _text(
                                "Bookmark",
                                link={"bookmarkId": "bookmark-1"},
                            ),
                            _text(" "),
                            _text("Tab\n", link={"tabId": "tab-other"}),
                        )
                    ],
                )
            ]
        }

        assert render_doc_to_plain_text(doc) == (
            "\n--- TAB: Main (ID: tab-current) ---\n"
            "External (https://example.com) "
            "Heading [heading: heading-1, tab: tab-target] "
            "Bookmark [bookmark: bookmark-1, tab: tab-current] "
            "Tab [tab: tab-other]\n"
        )


class TestContextualElements:
    def test_chips_use_readable_values_and_fallbacks(self):
        doc = {
            "body": {
                "content": [
                    _paragraph(
                        {
                            "person": {
                                "personProperties": {
                                    "name": "Ada Lovelace",
                                    "email": "ada@example.com",
                                }
                            }
                        },
                        _text(" | "),
                        {
                            "person": {
                                "personProperties": {"email": "grace@example.com"}
                            }
                        },
                        _text(" | "),
                        {"person": {"personProperties": {}}},
                        _text(" | "),
                        {
                            "richLink": {
                                "richLinkProperties": {
                                    "title": "Project",
                                    "uri": "https://example.com/project",
                                }
                            }
                        },
                        _text(" | "),
                        {"richLink": {"richLinkProperties": {}}},
                        _text(" | "),
                        {
                            "dateElement": {
                                "dateElementProperties": {
                                    "displayText": "July 14, 2026"
                                }
                            }
                        },
                        _text(" | "),
                        {
                            "dateElement": {
                                "dateElementProperties": {
                                    "timestamp": "2026-07-14T00:00:00Z"
                                }
                            }
                        },
                        _text(" | "),
                        {"dateElement": {"dateElementProperties": {}}},
                        _text("\n"),
                    )
                ]
            }
        }

        assert render_doc_to_plain_text(doc) == (
            "Ada Lovelace <ada@example.com> | <grace@example.com> | "
            "[Person chip: details unavailable] | "
            "Project (https://example.com/project) | "
            "[Rich link: details unavailable] | July 14, 2026 | "
            "2026-07-14T00:00:00Z | [Date: details unavailable]\n"
        )

    def test_inline_positioned_and_unresolved_objects(self):
        doc = {
            "inlineObjects": {
                "inline-1": {
                    "inlineObjectProperties": {
                        "embeddedObject": {
                            "title": "Logo",
                            "imageProperties": {
                                "contentUri": "https://example.com/logo.png"
                            },
                        }
                    }
                }
            },
            "positionedObjects": {
                "positioned-1": {
                    "positionedObjectProperties": {
                        "embeddedObject": {"description": "Diagram"}
                    }
                },
                "positioned-2": {
                    "positionedObjectProperties": {
                        "embeddedObject": {
                            "imageProperties": {
                                "sourceUri": "https://example.com/chart.png"
                            }
                        }
                    }
                },
            },
            "body": {
                "content": [
                    _paragraph(
                        {"inlineObjectElement": {"inlineObjectId": "inline-1"}},
                        _text("\n"),
                    ),
                    _paragraph(
                        _text("See diagram\n"),
                        positioned_object_ids=["positioned-1"],
                    ),
                    _paragraph(
                        {"inlineObjectElement": {"inlineObjectId": "missing-inline"}},
                        _text("\n"),
                    ),
                ]
            },
        }

        assert render_doc_to_plain_text(doc) == (
            "[Image: Logo; URI: https://example.com/logo.png]\n"
            "See diagram [Image: Diagram]\n"
            "[Inline object missing-inline: details unavailable]\n"
            "\n--- UNRESOLVED POSITIONED OBJECTS ---\n"
            "positioned-2: [Image; URI: https://example.com/chart.png]\n"
        )

    def test_dangling_positioned_object_reference_is_explicit(self):
        doc = {
            "body": {
                "content": [
                    _paragraph(
                        _text("Diagram\n"), positioned_object_ids=["missing-object"]
                    )
                ]
            }
        }

        assert render_doc_to_plain_text(doc) == (
            "Diagram [Positioned object missing-object: details unavailable]\n"
        )

    def test_headers_footers_and_footnotes_are_labeled(self):
        tab = _tab(
            "Main",
            "tab-1",
            [
                _paragraph(
                    _text("Claim"),
                    {"footnoteReference": {"footnoteId": "fn-1"}},
                    _text(".\n"),
                )
            ],
            headers={"header-1": {"content": [_paragraph(_text("Header\n"))]}},
            footers={"footer-1": {"content": [_paragraph(_text("Footer\n"))]}},
            footnotes={
                "fn-1": {"content": [_paragraph(_text("Supporting detail.\n"))]}
            },
        )

        assert render_doc_to_plain_text({"tabs": [tab]}) == (
            "\n--- TAB: Main (ID: tab-1) ---\n"
            "Claim[Footnote: fn-1].\n"
            "Footnote fn-1: Supporting detail.\n"
            "\n--- HEADER: header-1 ---\n"
            "Header\n"
            "\n--- FOOTER: footer-1 ---\n"
            "Footer\n"
        )

    def test_table_of_contents_and_nested_table_cells_are_recursive(self):
        nested_table = {
            "table": {
                "tableRows": [
                    {
                        "tableCells": [
                            {"content": [_paragraph(_text("Nested A\n"))]},
                            {"content": [_paragraph(_text("Nested B\n"))]},
                        ]
                    }
                ]
            }
        }
        doc = {
            "body": {
                "content": [
                    {
                        "tableOfContents": {
                            "content": [_paragraph(_text("Heading 1\n"))]
                        }
                    },
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                _paragraph(_text("A1\n")),
                                                _paragraph(_text("A2\n")),
                                            ]
                                        },
                                        {"content": [_paragraph(_text("B\n"))]},
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {"content": [nested_table]},
                                        {"content": [_paragraph(_text("C\n"))]},
                                    ]
                                },
                            ]
                        }
                    },
                ]
            }
        }

        assert render_doc_to_plain_text(doc) == (
            "Heading 1\nA1 A2\tB\nNested A\tNested B\tC\n"
        )

    def test_breaks_auto_text_equations_and_private_use_chip(self):
        doc = {
            "body": {
                "content": [
                    {"sectionBreak": {}},
                    _paragraph(
                        _text("Before"),
                        {"pageBreak": {}},
                        {"columnBreak": {}},
                        {"sectionBreak": {}},
                        {"autoText": {"type": "UNKNOWN_TYPE"}},
                        {"equation": {}},
                        _text("\ue907After\n"),
                    ),
                ]
            }
        }

        assert render_doc_to_plain_text(doc) == (
            "[Section Break]\n"
            "Before[Page Break][Column Break][Section Break]"
            "[Auto text: UNKNOWN_TYPE][Equation]"
            "[Smart Chip: details unavailable from Docs API]After\n"
        )

    def test_unknown_variants_emit_markers_and_warnings(self, caplog):
        doc = {
            "body": {
                "content": [
                    _paragraph({"futureInlineElement": {}}),
                    {"futureStructuralElement": {}},
                ]
            }
        }

        with caplog.at_level(logging.WARNING, logger="gdocs.docs_plain_text"):
            rendered = render_doc_to_plain_text(doc)

        assert rendered == (
            "[Unsupported Google Docs element: futureInlineElement]"
            "[Unsupported Google Docs element: futureStructuralElement]"
        )
        assert "futureInlineElement" in caplog.text
        assert "futureStructuralElement" in caplog.text
