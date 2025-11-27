"""
Google Sheets MCP Tools

This module provides MCP tools for interacting with Google Sheets API.
"""

import logging
import asyncio
import json
import re
from typing import List, Optional, Union


from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors
from core.comments import create_comment_tools

# Configure module logger
logger = logging.getLogger(__name__)

A1_PART_REGEX = re.compile(r"^([A-Za-z]*)(\d*)$")


def _column_to_index(column: str) -> Optional[int]:
    """Convert column letters (A, B, AA) to zero-based index."""
    if not column:
        return None
    result = 0
    for char in column.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def _parse_a1_range(range_name: str, sheets: List[dict]) -> dict:
    """
    Convert an A1-style range (with optional sheet name) into a GridRange.

    Falls back to the first sheet if none is provided.
    """
    if "!" in range_name:
        sheet_name, a1_range = range_name.split("!", 1)
    else:
        sheet_name, a1_range = None, range_name

    if not sheets:
        raise Exception("Spreadsheet has no sheets to format.")

    target_sheet = None
    if sheet_name:
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == sheet_name:
                target_sheet = sheet
                break
        if target_sheet is None:
            raise Exception(f"Sheet '{sheet_name}' not found in spreadsheet.")
    else:
        target_sheet = sheets[0]

    props = target_sheet.get("properties", {})
    sheet_id = props.get("sheetId")

    if not a1_range:
        raise Exception("Range must not be empty.")

    if ":" in a1_range:
        start, end = a1_range.split(":", 1)
    else:
        start = end = a1_range

    def parse_part(part: str) -> tuple[Optional[int], Optional[int]]:
        match = A1_PART_REGEX.match(part)
        if not match:
            raise Exception(f"Invalid A1 range part: '{part}'.")
        col_letters, row_digits = match.groups()
        col_idx = _column_to_index(col_letters) if col_letters else None
        row_idx = int(row_digits) - 1 if row_digits else None
        return col_idx, row_idx

    start_col, start_row = parse_part(start)
    end_col, end_row = parse_part(end)

    grid_range = {"sheetId": sheet_id}
    if start_row is not None:
        grid_range["startRowIndex"] = start_row
    if start_col is not None:
        grid_range["startColumnIndex"] = start_col
    if end_row is not None:
        grid_range["endRowIndex"] = end_row + 1
    if end_col is not None:
        grid_range["endColumnIndex"] = end_col + 1

    return grid_range


def _parse_hex_color(color: Optional[str]) -> Optional[dict]:
    """
    Convert a hex color like '#RRGGBB' to Sheets API color (0-1 floats).
    """
    if not color:
        return None

    trimmed = color.strip()
    if trimmed.startswith("#"):
        trimmed = trimmed[1:]

    if len(trimmed) not in (6, 8):
        raise Exception(f"Color '{color}' must be in format #RRGGBB or RRGGBB.")

    try:
        red = int(trimmed[0:2], 16) / 255
        green = int(trimmed[2:4], 16) / 255
        blue = int(trimmed[4:6], 16) / 255
    except ValueError as exc:
        raise Exception(f"Color '{color}' is not valid hex.") from exc

    return {"red": red, "green": green, "blue": blue}


@server.tool()
@handle_http_errors("list_spreadsheets", is_read_only=True, service_type="sheets")
@require_google_service("drive", "drive_read")
async def list_spreadsheets(
    service,
    user_google_email: str,
    max_results: int = 25,
) -> str:
    """
    Lists spreadsheets from Google Drive that the user has access to.

    Args:
        user_google_email (str): The user's Google email address. Required.
        max_results (int): Maximum number of spreadsheets to return. Defaults to 25.

    Returns:
        str: A formatted list of spreadsheet files (name, ID, modified time).
    """
    logger.info(f"[list_spreadsheets] Invoked. Email: '{user_google_email}'")

    files_response = await asyncio.to_thread(
        service.files()
        .list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            pageSize=max_results,
            fields="files(id,name,modifiedTime,webViewLink)",
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute
    )

    files = files_response.get("files", [])
    if not files:
        return f"No spreadsheets found for {user_google_email}."

    spreadsheets_list = [
        f"- \"{file['name']}\" (ID: {file['id']}) | Modified: {file.get('modifiedTime', 'Unknown')} | Link: {file.get('webViewLink', 'No link')}"
        for file in files
    ]

    text_output = (
        f"Successfully listed {len(files)} spreadsheets for {user_google_email}:\n"
        + "\n".join(spreadsheets_list)
    )

    logger.info(f"Successfully listed {len(files)} spreadsheets for {user_google_email}.")
    return text_output


@server.tool()
@handle_http_errors("get_spreadsheet_info", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def get_spreadsheet_info(
    service,
    user_google_email: str,
    spreadsheet_id: str,
) -> str:
    """
    Gets information about a specific spreadsheet including its sheets.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet to get info for. Required.

    Returns:
        str: Formatted spreadsheet information including title, locale, and sheets list.
    """
    logger.info(f"[get_spreadsheet_info] Invoked. Email: '{user_google_email}', Spreadsheet ID: {spreadsheet_id}")

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="spreadsheetId,properties(title,locale),sheets(properties(title,sheetId,gridProperties(rowCount,columnCount)))",
        )
        .execute
    )

    properties = spreadsheet.get("properties", {})
    title = properties.get("title", "Unknown")
    locale = properties.get("locale", "Unknown")
    sheets = spreadsheet.get("sheets", [])

    sheets_info = []
    for sheet in sheets:
        sheet_props = sheet.get("properties", {})
        sheet_name = sheet_props.get("title", "Unknown")
        sheet_id = sheet_props.get("sheetId", "Unknown")
        grid_props = sheet_props.get("gridProperties", {})
        rows = grid_props.get("rowCount", "Unknown")
        cols = grid_props.get("columnCount", "Unknown")

        sheets_info.append(
            f"  - \"{sheet_name}\" (ID: {sheet_id}) | Size: {rows}x{cols}"
        )

    sheets_section = "\n".join(sheets_info) if sheets_info else "  No sheets found"
    text_output = (
        f"Spreadsheet: \"{title}\" (ID: {spreadsheet_id}) | Locale: {locale}\n"
        f"Sheets ({len(sheets)}):\n"
        f"{sheets_section}"
    )

    logger.info(f"Successfully retrieved info for spreadsheet {spreadsheet_id} for {user_google_email}.")
    return text_output


@server.tool()
@handle_http_errors("read_sheet_values", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def read_sheet_values(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str = "A1:Z1000",
) -> str:
    """
    Reads values from a specific range in a Google Sheet.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): The range to read (e.g., "Sheet1!A1:D10", "A1:D10"). Defaults to "A1:Z1000".

    Returns:
        str: The formatted values from the specified range.
    """
    logger.info(f"[read_sheet_values] Invoked. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}")

    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute
    )

    values = result.get("values", [])
    if not values:
        return f"No data found in range '{range_name}' for {user_google_email}."

    # Format the output as a readable table
    formatted_rows = []
    for i, row in enumerate(values, 1):
        # Pad row with empty strings to show structure
        padded_row = row + [""] * max(0, len(values[0]) - len(row)) if values else row
        formatted_rows.append(f"Row {i:2d}: {padded_row}")

    text_output = (
        f"Successfully read {len(values)} rows from range '{range_name}' in spreadsheet {spreadsheet_id} for {user_google_email}:\n"
        + "\n".join(formatted_rows[:50])  # Limit to first 50 rows for readability
        + (f"\n... and {len(values) - 50} more rows" if len(values) > 50 else "")
    )

    logger.info(f"Successfully read {len(values)} rows for {user_google_email}.")
    return text_output


@server.tool()
@handle_http_errors("modify_sheet_values", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def modify_sheet_values(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    values: Optional[Union[str, List[List[str]]]] = None,
    value_input_option: str = "USER_ENTERED",
    clear_values: bool = False,
) -> str:
    """
    Modifies values in a specific range of a Google Sheet - can write, update, or clear values.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): The range to modify (e.g., "Sheet1!A1:D10", "A1:D10"). Required.
        values (Optional[Union[str, List[List[str]]]]): 2D array of values to write/update. Can be a JSON string or Python list. Required unless clear_values=True.
        value_input_option (str): How to interpret input values ("RAW" or "USER_ENTERED"). Defaults to "USER_ENTERED".
        clear_values (bool): If True, clears the range instead of writing values. Defaults to False.

    Returns:
        str: Confirmation message of the successful modification operation.
    """
    operation = "clear" if clear_values else "write"
    logger.info(f"[modify_sheet_values] Invoked. Operation: {operation}, Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}")

    # Parse values if it's a JSON string (MCP passes parameters as JSON strings)
    if values is not None and isinstance(values, str):
        try:
            parsed_values = json.loads(values)
            if not isinstance(parsed_values, list):
                raise ValueError(f"Values must be a list, got {type(parsed_values).__name__}")
            # Validate it's a list of lists
            for i, row in enumerate(parsed_values):
                if not isinstance(row, list):
                    raise ValueError(f"Row {i} must be a list, got {type(row).__name__}")
            values = parsed_values
            logger.info(f"[modify_sheet_values] Parsed JSON string to Python list with {len(values)} rows")
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid JSON format for values: {e}")
        except ValueError as e:
            raise Exception(f"Invalid values structure: {e}")

    if not clear_values and not values:
        raise Exception("Either 'values' must be provided or 'clear_values' must be True.")

    if clear_values:
        result = await asyncio.to_thread(
            service.spreadsheets()
            .values()
            .clear(spreadsheetId=spreadsheet_id, range=range_name)
            .execute
        )

        cleared_range = result.get("clearedRange", range_name)
        text_output = f"Successfully cleared range '{cleared_range}' in spreadsheet {spreadsheet_id} for {user_google_email}."
        logger.info(f"Successfully cleared range '{cleared_range}' for {user_google_email}.")
    else:
        body = {"values": values}

        result = await asyncio.to_thread(
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption=value_input_option,
                body=body,
            )
            .execute
        )

        updated_cells = result.get("updatedCells", 0)
        updated_rows = result.get("updatedRows", 0)
        updated_columns = result.get("updatedColumns", 0)

        text_output = (
            f"Successfully updated range '{range_name}' in spreadsheet {spreadsheet_id} for {user_google_email}. "
            f"Updated: {updated_cells} cells, {updated_rows} rows, {updated_columns} columns."
        )
        logger.info(f"Successfully updated {updated_cells} cells for {user_google_email}.")

    return text_output


CONDITION_TYPES = {
    "NUMBER_GREATER",
    "NUMBER_GREATER_THAN_EQ",
    "NUMBER_LESS",
    "NUMBER_LESS_THAN_EQ",
    "NUMBER_EQ",
    "NUMBER_NOT_EQ",
    "TEXT_CONTAINS",
    "TEXT_NOT_CONTAINS",
    "TEXT_STARTS_WITH",
    "TEXT_ENDS_WITH",
    "TEXT_EQ",
    "DATE_BEFORE",
    "DATE_ON_OR_BEFORE",
    "DATE_AFTER",
    "DATE_ON_OR_AFTER",
    "DATE_EQ",
    "DATE_NOT_EQ",
    "DATE_BETWEEN",
    "DATE_NOT_BETWEEN",
    "NOT_BLANK",
    "BLANK",
    "CUSTOM_FORMULA",
    "ONE_OF_RANGE",
}


@server.tool()
@handle_http_errors("format_sheet_range", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def format_sheet_range(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None,
) -> str:
    """
    Applies formatting to a range: background/text color and number/date formats.

    Colors accept hex strings (#RRGGBB). Number formats follow Sheets types
    (e.g., NUMBER, NUMBER_WITH_GROUPING, CURRENCY, DATE, TIME, DATE_TIME,
    PERCENT, TEXT, SCIENTIFIC). If no sheet name is provided, the first sheet
    is used.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): A1-style range (optionally with sheet name). Required.
        background_color (Optional[str]): Hex background color (e.g., "#FFEECC").
        text_color (Optional[str]): Hex text color (e.g., "#000000").
        number_format_type (Optional[str]): Sheets number format type (e.g., "DATE").
        number_format_pattern (Optional[str]): Optional custom pattern for the number format.

    Returns:
        str: Confirmation of the applied formatting.
    """
    logger.info(
        "[format_sheet_range] Invoked. Email: '%s', Spreadsheet: %s, Range: %s",
        user_google_email,
        spreadsheet_id,
        range_name,
    )

    if not any([background_color, text_color, number_format_type]):
        raise Exception(
            "Provide at least one of background_color, text_color, or number_format_type."
        )

    bg_color_parsed = _parse_hex_color(background_color)
    text_color_parsed = _parse_hex_color(text_color)

    number_format = None
    if number_format_type:
        allowed_number_formats = {
            "NUMBER",
            "NUMBER_WITH_GROUPING",
            "CURRENCY",
            "PERCENT",
            "SCIENTIFIC",
            "DATE",
            "TIME",
            "DATE_TIME",
            "TEXT",
        }
        normalized_type = number_format_type.upper()
        if normalized_type not in allowed_number_formats:
            raise Exception(
                f"number_format_type must be one of {sorted(allowed_number_formats)}."
            )
        number_format = {"type": normalized_type}
        if number_format_pattern:
            number_format["pattern"] = number_format_pattern

    metadata = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute
    )
    sheets = metadata.get("sheets", [])
    grid_range = _parse_a1_range(range_name, sheets)

    user_entered_format = {}
    fields = []
    if bg_color_parsed:
        user_entered_format["backgroundColor"] = bg_color_parsed
        fields.append("userEnteredFormat.backgroundColor")
    if text_color_parsed:
        user_entered_format["textFormat"] = {
            "foregroundColor": text_color_parsed
        }
        fields.append("userEnteredFormat.textFormat.foregroundColor")
    if number_format:
        user_entered_format["numberFormat"] = number_format
        fields.append("userEnteredFormat.numberFormat")

    if not user_entered_format:
        raise Exception(
            "No formatting applied. Verify provided colors or number format."
        )

    request_body = {
        "requests": [
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"userEnteredFormat": user_entered_format},
                    "fields": ",".join(fields),
                }
            }
        ]
    }

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    applied_parts = []
    if bg_color_parsed:
        applied_parts.append(f"background {background_color}")
    if text_color_parsed:
        applied_parts.append(f"text {text_color}")
    if number_format:
        nf_desc = number_format["type"]
        if number_format_pattern:
            nf_desc += f" (pattern: {number_format_pattern})"
        applied_parts.append(f"format {nf_desc}")

    summary = ", ".join(applied_parts)
    return (
        f"Applied formatting to range '{range_name}' in spreadsheet {spreadsheet_id} "
        f"for {user_google_email}: {summary}."
    )


@server.tool()
@handle_http_errors("add_conditional_formatting", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def add_conditional_formatting(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    condition_type: str,
    condition_values: Optional[List[Union[str, int, float]]] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    rule_index: Optional[int] = None,
) -> str:
    """
    Adds a conditional formatting rule to a range.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): A1-style range (optionally with sheet name). Required.
        condition_type (str): Sheets condition type (e.g., NUMBER_GREATER, TEXT_CONTAINS, DATE_BEFORE, CUSTOM_FORMULA).
        condition_values (Optional[List[Union[str, int, float]]]): Values for the condition; depends on condition_type.
        background_color (Optional[str]): Hex background color to apply when condition matches.
        text_color (Optional[str]): Hex text color to apply when condition matches.
        rule_index (Optional[int]): Optional position to insert the rule (0-based) within the sheet's rules.

    Returns:
        str: Confirmation of the added rule.
    """
    logger.info(
        "[add_conditional_formatting] Invoked. Email: '%s', Spreadsheet: %s, Range: %s, Type: %s",
        user_google_email,
        spreadsheet_id,
        range_name,
        condition_type,
    )

    if not background_color and not text_color:
        raise Exception("Provide at least one of background_color or text_color for the rule format.")

    cond_type_normalized = condition_type.upper()
    if cond_type_normalized not in CONDITION_TYPES:
        raise Exception(f"condition_type must be one of {sorted(CONDITION_TYPES)}.")

    metadata = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute
    )
    sheets = metadata.get("sheets", [])
    grid_range = _parse_a1_range(range_name, sheets)

    condition = {"type": cond_type_normalized}
    if condition_values:
        condition["values"] = [
            {"userEnteredValue": str(value)} for value in condition_values
        ]

    bg_color_parsed = _parse_hex_color(background_color)
    text_color_parsed = _parse_hex_color(text_color)

    format_obj = {}
    if bg_color_parsed:
        format_obj["backgroundColor"] = bg_color_parsed
    if text_color_parsed:
        format_obj["textFormat"] = {"foregroundColor": text_color_parsed}

    rule = {
        "ranges": [grid_range],
        "booleanRule": {
            "condition": condition,
            "format": format_obj,
        },
    }

    add_rule_request = {"rule": rule}
    if rule_index is not None:
        add_rule_request["index"] = rule_index

    request_body = {"requests": [{"addConditionalFormatRule": add_rule_request}]}

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    values_desc = ""
    if condition_values:
        values_desc = f" with values {condition_values}"

    applied_parts = []
    if background_color:
        applied_parts.append(f"background {background_color}")
    if text_color:
        applied_parts.append(f"text {text_color}")
    format_desc = ", ".join(applied_parts)

    return (
        f"Added conditional format on '{range_name}' in spreadsheet {spreadsheet_id} "
        f"for {user_google_email}: {cond_type_normalized}{values_desc}; format: {format_desc or 'none'}."
    )


@server.tool()
@handle_http_errors("create_spreadsheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def create_spreadsheet(
    service,
    user_google_email: str,
    title: str,
    sheet_names: Optional[List[str]] = None,
) -> str:
    """
    Creates a new Google Spreadsheet.

    Args:
        user_google_email (str): The user's Google email address. Required.
        title (str): The title of the new spreadsheet. Required.
        sheet_names (Optional[List[str]]): List of sheet names to create. If not provided, creates one sheet with default name.

    Returns:
        str: Information about the newly created spreadsheet including ID, URL, and locale.
    """
    logger.info(f"[create_spreadsheet] Invoked. Email: '{user_google_email}', Title: {title}")

    spreadsheet_body = {
        "properties": {
            "title": title
        }
    }

    if sheet_names:
        spreadsheet_body["sheets"] = [
            {"properties": {"title": sheet_name}} for sheet_name in sheet_names
        ]

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .create(
            body=spreadsheet_body,
            fields="spreadsheetId,spreadsheetUrl,properties(title,locale)",
        )
        .execute
    )

    properties = spreadsheet.get("properties", {})
    spreadsheet_id = spreadsheet.get("spreadsheetId")
    spreadsheet_url = spreadsheet.get("spreadsheetUrl")
    locale = properties.get("locale", "Unknown")

    text_output = (
        f"Successfully created spreadsheet '{title}' for {user_google_email}. "
        f"ID: {spreadsheet_id} | URL: {spreadsheet_url} | Locale: {locale}"
    )

    logger.info(f"Successfully created spreadsheet for {user_google_email}. ID: {spreadsheet_id}")
    return text_output


@server.tool()
@handle_http_errors("create_sheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def create_sheet(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    sheet_name: str,
) -> str:
    """
    Creates a new sheet within an existing spreadsheet.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        sheet_name (str): The name of the new sheet. Required.

    Returns:
        str: Confirmation message of the successful sheet creation.
    """
    logger.info(f"[create_sheet] Invoked. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Sheet: {sheet_name}")

    request_body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": sheet_name
                    }
                }
            }
        ]
    }

    response = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]

    text_output = (
        f"Successfully created sheet '{sheet_name}' (ID: {sheet_id}) in spreadsheet {spreadsheet_id} for {user_google_email}."
    )

    logger.info(f"Successfully created sheet for {user_google_email}. Sheet ID: {sheet_id}")
    return text_output


# Create comment management tools for sheets
_comment_tools = create_comment_tools("spreadsheet", "spreadsheet_id")

# Extract and register the functions
read_sheet_comments = _comment_tools['read_comments']
create_sheet_comment = _comment_tools['create_comment']
reply_to_sheet_comment = _comment_tools['reply_to_comment']
resolve_sheet_comment = _comment_tools['resolve_comment']
