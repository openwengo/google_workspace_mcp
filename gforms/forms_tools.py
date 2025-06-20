"""
Google Forms MCP Tools

This module provides MCP tools for interacting with Google Forms API.
"""

import logging
import asyncio
from typing import List, Optional, Dict, Any, Callable, Union, Tuple
from dataclasses import dataclass
from collections import defaultdict

from mcp import types
from googleapiclient.errors import HttpError

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS AND CONFIGURATION
# ============================================================================

# Question types that support grading
GRADABLE_QUESTION_TYPES = {
    "TEXT_QUESTION",
    "MULTIPLE_CHOICE_QUESTION",
    "SCALE_QUESTION",
    "CHECKBOX_QUESTION",
    "DATE_QUESTION",
    "TIME_QUESTION",
    "RATING_QUESTION",
}

# Valid update fields for validation
VALID_UPDATE_FIELDS = {
    "title",
    "description",
    "question",
    "questionGroupItem",
    "imageItem",
    "videoItem",
    "pageBreakItem",
    "textItem",
}

# Item type mappings for detection
ITEM_TYPE_MAPPINGS = {
    "questionItem": "questionItem",
    "videoItem": "videoItem",
    "imageItem": "imageItem",
    "pageBreakItem": "pageBreakItem",
    "textItem": "textItem",
    "questionGroupItem": "questionGroupItem",
}

# Question type detection mappings
QUESTION_TYPE_DETECTORS = {
    "choiceQuestion": "MULTIPLE_CHOICE",
    "textQuestion": "TEXT",
    "scaleQuestion": "SCALE",
    "dateQuestion": "DATE",
    "timeQuestion": "TIME",
    "ratingQuestion": "RATING",
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def _build_feedback_object(
    feedback_data: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Helper function to build a Feedback object for grading.
    """
    if not feedback_data:
        return None

    feedback_object = {}
    if "text" in feedback_data:
        feedback_object["text"] = feedback_data["text"]
    if "link" in feedback_data:
        feedback_object["link"] = {"uri": feedback_data["link"]}
    return feedback_object


def _build_choice_options(options_data: List[Union[str, Dict]]) -> List[Dict]:
    """Build choice options with consistent handling."""
    built_options = []
    for opt in options_data:
        option_obj = {"value": opt.get("value", opt) if isinstance(opt, dict) else opt}
        if isinstance(opt, dict):
            if "go_to_action" in opt:
                option_obj["goToAction"] = opt["go_to_action"]
            if "go_to_section_id" in opt:
                option_obj["goToSectionId"] = opt["go_to_section_id"]
        built_options.append(option_obj)
    return built_options


def _snake_to_camel(snake_str: str) -> str:
    """Convert snake_case to camelCase."""
    components = snake_str.split("_")
    return components[0] + "".join(word.capitalize() for word in components[1:])


def _build_properties_if_content(
    properties_data: Dict[str, Any], base_mask: str
) -> Tuple[Optional[Dict], List[str]]:
    """Build properties object only if it has actual content."""
    if not properties_data:
        return None, []

    properties = {}
    masks = []

    for field in ["alignment", "width"]:
        if field in properties_data:
            properties[field] = properties_data[field]
            masks.append(f"{base_mask}.{field}")

    return properties if properties else None, masks


def _process_feedback_fields(
    grading_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process all feedback fields using a mapping approach."""
    feedback_fields = {
        "whenRight": "question.grading.whenRight",
        "whenWrong": "question.grading.whenWrong",
        "generalFeedback": "question.grading.generalFeedback",
    }

    for field, mask in feedback_fields.items():
        if field in grading_data:
            feedback = _build_feedback_object(grading_data[field])
            if feedback is not None:
                updated_content[field] = feedback
                masks.append(mask)


def _detect_item_type(item: Dict[str, Any]) -> str:
    """Detect item type using a priority-based mapping."""
    type_detectors = {
        "videoItem": "VIDEO",
        "imageItem": "IMAGE",
        "questionItem": lambda item: _detect_question_subtype(item["questionItem"]),
        "pageBreakItem": "PAGE_BREAK",
        "textItem": "TEXT_ITEM",
        "questionGroupItem": "QUESTION_GROUP",
    }

    for key, type_or_func in type_detectors.items():
        if key in item:
            return type_or_func(item) if callable(type_or_func) else type_or_func
    return "UNKNOWN"


def _detect_question_subtype(question_item: Dict[str, Any]) -> str:
    """Detect question subtype from questionItem."""
    question = question_item.get("question", {})
    for question_key, question_type in QUESTION_TYPE_DETECTORS.items():
        if question_key in question:
            return question_type
    return "QUESTION"


# ============================================================================
# QUESTION BUILDER FUNCTIONS
# ============================================================================


def _build_text_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a text question item."""
    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "textQuestion": {"paragraph": q.get("paragraph", False)},
            }
        },
    }


def _build_choice_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a multiple choice question item."""
    options_data = q.get("options", [])
    choice_options = _build_choice_options(options_data)

    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "choiceQuestion": {
                    "type": q.get("choice_type", "RADIO"),
                    "options": choice_options,
                    "shuffle": q.get("shuffle", False),
                },
            }
        },
    }


def _build_checkbox_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a checkbox question item."""
    options_data = q.get("options", [])
    choice_options = _build_choice_options(options_data)

    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "choiceQuestion": {
                    "type": "CHECKBOX",
                    "options": choice_options,
                    "shuffle": q.get("shuffle", False),
                },
            }
        },
    }


def _build_scale_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a scale question item."""
    scale_labels = q.get("scale_labels", {})
    scale_min = q.get("scale_min")
    scale_max = q.get("scale_max")

    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "scaleQuestion": {
                    "low": scale_min,
                    "high": scale_max,
                    "lowLabel": scale_labels.get(str(scale_min)),
                    "highLabel": scale_labels.get(str(scale_max)),
                },
            }
        },
    }


def _build_date_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a date question item."""
    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "dateQuestion": {
                    "includeTime": q.get("include_time", False),
                    "includeYear": q.get("include_year", True),
                },
            }
        },
    }


def _build_time_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a time question item."""
    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "timeQuestion": {"duration": q.get("duration", False)},
            }
        },
    }


def _build_rating_question(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a rating question item."""
    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionItem": {
            "question": {
                "required": q.get("required", False),
                "ratingQuestion": {
                    "ratingScaleLevel": q.get("rating_scale_level"),
                    "iconType": q.get("icon_type"),
                },
            }
        },
    }


def _build_image_item(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build an image item."""
    image_data = q.get("image", {})
    properties, _ = _build_properties_if_content(
        {"alignment": image_data.get("alignment"), "width": image_data.get("width")},
        "imageItem.image.properties",
    )

    return {
        "imageItem": {
            "image": {
                "sourceUri": image_data.get("source_uri"),
                "altText": image_data.get("alt_text"),
                "properties": properties or {},
            }
        }
    }


def _build_video_item(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a video item."""
    video_data = q.get("video", {})
    properties, _ = _build_properties_if_content(
        {"alignment": video_data.get("alignment"), "width": video_data.get("width")},
        "videoItem.video.properties",
    )

    return {
        "videoItem": {
            "video": {
                "youtubeUri": video_data.get("youtube_uri"),
                "properties": properties or {},
            },
            "caption": q.get("caption"),
        }
    }


def _build_page_break_item(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a page break item."""
    return {"pageBreakItem": {}}


def _build_text_item(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a text item."""
    return {"textItem": {}}


def _build_question_group_item(q: Dict[str, Any]) -> Dict[str, Any]:
    """Build a question group item."""
    group_questions = q.get("questions", [])
    group_item_content = {
        "questions": [
            {"rowQuestion": {"title": g_q.get("title")}}
            for g_q in group_questions
            if g_q.get("type") == "ROW_QUESTION"
        ]
    }

    if "grid" in q:
        grid_data = q.get("grid", {})
        columns_data = grid_data.get("columns", {})
        group_item_content["grid"] = {
            "columns": {
                "type": columns_data.get("choice_type", "RADIO"),
                "options": [{"value": opt} for opt in columns_data.get("options", [])],
            },
            "shuffleQuestions": grid_data.get("shuffle_questions", False),
        }

    if "image" in q:
        image_data = q.get("image", {})
        properties, _ = _build_properties_if_content(
            {
                "alignment": image_data.get("alignment"),
                "width": image_data.get("width"),
            },
            "questionGroupItem.image.properties",
        )
        group_item_content["image"] = {
            "sourceUri": image_data.get("source_uri"),
            "altText": image_data.get("alt_text"),
            "properties": properties or {},
        }

    return {
        "title": q.get("title"),
        "description": q.get("description"),
        "questionGroupItem": group_item_content,
    }


# Question type handlers dictionary - the core optimization
QUESTION_TYPE_HANDLERS = {
    "TEXT_QUESTION": _build_text_question,
    "MULTIPLE_CHOICE_QUESTION": _build_choice_question,
    "SCALE_QUESTION": _build_scale_question,
    "CHECKBOX_QUESTION": _build_checkbox_question,
    "DATE_QUESTION": _build_date_question,
    "TIME_QUESTION": _build_time_question,
    "RATING_QUESTION": _build_rating_question,
    "IMAGE_ITEM": _build_image_item,
    "VIDEO_ITEM": _build_video_item,
    "PAGE_BREAK_ITEM": _build_page_break_item,
    "TEXT_ITEM": _build_text_item,
    "QUESTION_GROUP_ITEM": _build_question_group_item,
}


def _build_question_requests(
    questions: List[Dict[str, Any]], start_index: int = 0
) -> List[Dict[str, Any]]:
    """
    Helper function to convert simplified question schema to Google Forms API Request objects.

    Args:
        questions: List of question dictionaries
        start_index: Starting index for question insertion
    """
    requests = []
    for i, q in enumerate(questions):
        question_type = q.get("type")

        # Use dictionary dispatch instead of massive if-elif chain
        handler = QUESTION_TYPE_HANDLERS.get(question_type)
        if not handler:
            if question_type == "FILE_UPLOAD_QUESTION":
                logger.warning(
                    "File upload questions are not supported for creation via the API. Skipping."
                )
            else:
                logger.warning(
                    f"Unsupported question type: {question_type}. Skipping question: {q.get('title')}"
                )
            continue

        try:
            item = handler(q)
        except Exception as e:
            logger.warning(f"Failed to build question {question_type}: {e}. Skipping.")
            continue

        # Add grading if provided and it's a gradable question type
        if question_type in GRADABLE_QUESTION_TYPES and "grading" in q:
            grading_data = q.get("grading", {})
            correct_answers_data = grading_data.get("correct_answers", [])

            correct_answers_list = [
                {"value": ans.get("value")} for ans in correct_answers_data
            ]

            grading_content = {
                "pointValue": grading_data.get("point_value"),
                "correctAnswers": {"answers": correct_answers_list},
                "whenRight": _build_feedback_object(grading_data.get("when_right")),
                "whenWrong": _build_feedback_object(grading_data.get("when_wrong")),
                "generalFeedback": _build_feedback_object(
                    grading_data.get("general_feedback")
                ),
            }

            # Ensure the item has a questionItem structure for grading
            if "questionItem" in item:
                item["questionItem"]["question"]["grading"] = grading_content

        requests.append(
            {"createItem": {"item": item, "location": {"index": start_index + i}}}
        )
    return requests


# ============================================================================
# UPDATE FIELD PROCESSORS
# ============================================================================


def _process_question_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process question-specific fields for updates."""
    if "question" not in q_update or original_item_type != "questionItem":
        return

    question_data = q_update["question"]
    updated_question_content = {}

    if "required" in question_data:
        updated_question_content["required"] = question_data["required"]
        masks.append("question.required")

    # Question type processors
    question_processors = {
        "textQuestion": _process_text_question_fields,
        "choiceQuestion": _process_choice_question_fields,
        "scaleQuestion": _process_scale_question_fields,
        "dateQuestion": _process_date_question_fields,
        "timeQuestion": _process_time_question_fields,
        "ratingQuestion": _process_rating_question_fields,
        "grading": _process_grading_fields,
    }

    for field_type, processor in question_processors.items():
        if field_type in question_data:
            processor(question_data[field_type], updated_question_content, masks)

    updated_content["questionItem"] = {"question": updated_question_content}


def _process_text_question_fields(
    text_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process text question specific fields."""
    updated_content["textQuestion"] = {"paragraph": text_data.get("paragraph", False)}
    masks.append("question.textQuestion.paragraph")


def _process_choice_question_fields(
    choice_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process choice question specific fields."""
    updated_choice_content = {}

    field_mappings = {
        "type": "question.choiceQuestion.type",
        "shuffle": "question.choiceQuestion.shuffle",
    }

    for field, mask in field_mappings.items():
        if field in choice_data:
            updated_choice_content[field] = choice_data[field]
            masks.append(mask)

    if "options" in choice_data:
        updated_choice_content["options"] = _build_choice_options(
            choice_data["options"]
        )
        masks.append("question.choiceQuestion.options")

    updated_content["choiceQuestion"] = updated_choice_content


def _process_scale_question_fields(
    scale_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process scale question specific fields."""
    updated_scale_content = {}

    field_mappings = {
        "low": "question.scaleQuestion.low",
        "high": "question.scaleQuestion.high",
        "lowLabel": "question.scaleQuestion.lowLabel",
        "highLabel": "question.scaleQuestion.highLabel",
    }

    for field, mask in field_mappings.items():
        if field in scale_data:
            updated_scale_content[field] = scale_data[field]
            masks.append(mask)

    updated_content["scaleQuestion"] = updated_scale_content


def _process_date_question_fields(
    date_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process date question specific fields."""
    updated_date_content = {}

    field_mappings = {
        "includeTime": "question.dateQuestion.includeTime",
        "includeYear": "question.dateQuestion.includeYear",
    }

    for field, mask in field_mappings.items():
        if field in date_data:
            updated_date_content[field] = date_data[field]
            masks.append(mask)

    updated_content["dateQuestion"] = updated_date_content


def _process_time_question_fields(
    time_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process time question specific fields."""
    updated_time_content = {}

    if "duration" in time_data:
        updated_time_content["duration"] = time_data["duration"]
        masks.append("question.timeQuestion.duration")

    updated_content["timeQuestion"] = updated_time_content


def _process_rating_question_fields(
    rating_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process rating question specific fields."""
    updated_rating_content = {}

    field_mappings = {
        "ratingScaleLevel": "question.ratingQuestion.ratingScaleLevel",
        "iconType": "question.ratingQuestion.iconType",
    }

    for field, mask in field_mappings.items():
        if field in rating_data:
            updated_rating_content[field] = rating_data[field]
            masks.append(mask)

    updated_content["ratingQuestion"] = updated_rating_content


def _process_grading_fields(
    grading_data: Dict, updated_content: Dict, masks: List[str]
) -> None:
    """Process grading specific fields."""
    updated_grading_content = {}

    if "pointValue" in grading_data:
        updated_grading_content["pointValue"] = grading_data["pointValue"]
        masks.append("question.grading.pointValue")

    if "correctAnswers" in grading_data:
        correct_answers_list = [
            {"value": ans.get("value")}
            for ans in grading_data["correctAnswers"].get("answers", [])
        ]
        updated_grading_content["correctAnswers"] = {"answers": correct_answers_list}
        masks.append("question.grading.correctAnswers")

    # Process feedback fields using the optimized helper
    _process_feedback_fields(grading_data, updated_grading_content, masks)

    updated_content["grading"] = updated_grading_content


def _process_group_item_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process question group item specific fields."""
    if "questionGroupItem" not in q_update or original_item_type != "questionGroupItem":
        return

    group_data = q_update["questionGroupItem"]
    updated_group_content = {}

    if "questions" in group_data:
        group_questions_list = [
            {"rowQuestion": {"title": g_q.get("title")}}
            for g_q in group_data["questions"]
            if g_q.get("type") == "ROW_QUESTION"
        ]
        updated_group_content["questions"] = group_questions_list
        masks.append("questionGroupItem.questions")

    if "grid" in group_data:
        grid_data = group_data["grid"]
        updated_grid_content = {}

        if "columns" in grid_data:
            columns_data = grid_data["columns"]
            updated_grid_content["columns"] = {
                "type": columns_data.get("choice_type", "RADIO"),
                "options": [{"value": opt} for opt in columns_data.get("options", [])],
            }
            masks.append("questionGroupItem.grid.columns")

        if "shuffleQuestions" in grid_data:
            updated_grid_content["shuffleQuestions"] = grid_data["shuffleQuestions"]
            masks.append("questionGroupItem.grid.shuffleQuestions")

        updated_group_content["grid"] = updated_grid_content

    if "image" in group_data:
        image_data = group_data["image"]
        properties, property_masks = _build_properties_if_content(
            {
                "alignment": image_data.get("alignment"),
                "width": image_data.get("width"),
            },
            "questionGroupItem.image.properties",
        )

        updated_group_content["image"] = {
            "sourceUri": image_data.get("source_uri"),
            "altText": image_data.get("alt_text"),
            "properties": properties or {},
        }
        masks.append("questionGroupItem.image")
        masks.extend(property_masks)

    updated_content["questionGroupItem"] = updated_group_content


def _process_image_item_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process image item specific fields."""
    if "imageItem" not in q_update or original_item_type != "imageItem":
        return

    image_item_data = q_update["imageItem"]
    updated_image_item_content = {}

    if "image" in image_item_data:
        image_data = image_item_data["image"]
        updated_image_content = {}

        field_mappings = {
            "sourceUri": "imageItem.image.sourceUri",
            "altText": "imageItem.image.altText",
        }

        for field, mask in field_mappings.items():
            if field in image_data:
                updated_image_content[field] = image_data[field]
                masks.append(mask)

        if "properties" in image_data:
            properties, property_masks = _build_properties_if_content(
                image_data["properties"], "imageItem.image.properties"
            )
            if properties:
                updated_image_content["properties"] = properties
                masks.extend(property_masks)

        updated_image_item_content["image"] = updated_image_content

    updated_content["imageItem"] = updated_image_item_content


def _process_video_item_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process video item specific fields."""
    if "videoItem" not in q_update or original_item_type != "videoItem":
        return

    video_item_data = q_update["videoItem"]
    updated_video_item_content = {}

    if "video" in video_item_data:
        video_data = video_item_data["video"]
        updated_video_content = {}

        if "youtubeUri" in video_data:
            updated_video_content["youtubeUri"] = video_data["youtubeUri"]
            masks.append("videoItem.video.youtubeUri")

        if "properties" in video_data:
            properties, property_masks = _build_properties_if_content(
                video_data["properties"], "videoItem.video.properties"
            )
            if properties:
                updated_video_content["properties"] = properties
                masks.extend(property_masks)

        updated_video_item_content["video"] = updated_video_content

    if "caption" in video_item_data:
        updated_video_item_content["caption"] = video_item_data["caption"]
        masks.append("videoItem.caption")

    updated_content["videoItem"] = updated_video_item_content


def _process_page_break_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process page break item fields."""
    if "pageBreakItem" in q_update and original_item_type == "pageBreakItem":
        updated_content["pageBreakItem"] = {}


def _process_text_item_fields(
    q_update: Dict, original_item_type: str, updated_content: Dict, masks: List[str]
) -> None:
    """Process text item fields."""
    if "textItem" in q_update and original_item_type == "textItem":
        updated_content["textItem"] = {}


# Field processors for different item types - another core optimization
FIELD_PROCESSORS = {
    "question": _process_question_fields,
    "questionGroupItem": _process_group_item_fields,
    "imageItem": _process_image_item_fields,
    "videoItem": _process_video_item_fields,
    "pageBreakItem": _process_page_break_fields,
    "textItem": _process_text_item_fields,
}

# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================


def _validate_item_update_data(
    item_type: str, update_data: Dict[str, Any]
) -> List[str]:
    """
    Validates update data for specific item types and returns list of validation errors.

    Args:
        item_type: The type of item being updated (e.g., 'videoItem', 'imageItem')
        update_data: The update data for the item

    Returns:
        List of validation error messages
    """
    validation_rules = {
        "videoItem": _validate_video_item,
        "imageItem": _validate_image_item,
    }

    validator = validation_rules.get(item_type)
    return validator(update_data) if validator else []


def _validate_video_item(update_data: Dict[str, Any]) -> List[str]:
    """Validate video item data."""
    errors = []
    if "videoItem" in update_data:
        video_item_data = update_data["videoItem"]
        if "video" in video_item_data:
            video_data = video_item_data["video"]
            if "youtubeUri" in video_data and not video_data["youtubeUri"]:
                errors.append("Video items require a valid youtubeUri")
    return errors


def _validate_image_item(update_data: Dict[str, Any]) -> List[str]:
    """Validate image item data."""
    errors = []
    if "imageItem" in update_data:
        image_item_data = update_data["imageItem"]
        if "image" in image_item_data:
            image_data = image_item_data["image"]
            if "sourceUri" in image_data and not image_data["sourceUri"]:
                errors.append("Image items require a valid sourceUri")
    return errors


# ============================================================================
# MAIN TOOL FUNCTIONS
# ============================================================================


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("create_form")
async def create_form(
    service,
    user_google_email: str,
    title: str,
    description: Optional[str] = None,
    document_title: Optional[str] = None,
) -> str:
    """
    Create a new Google Form - UPDATED WITH API BEST PRACTICES.

    This function creates a basic Google Form with title, description, and document title.
    To add questions to the form, use the add_questions_to_form tool after creation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        title (str): The title of the form.
        description (Optional[str]): The description of the form.
        document_title (Optional[str]): The document title (shown in browser tab).

    Returns:
        str: Confirmation message with form ID, edit URL, and responder URL.
    """
    logger.info(f"[create_form] Invoked. Email: '{user_google_email}', Title: {title}")

    # Step 1: Create form with title and documentTitle (Google API constraint - documentTitle is create-only)
    form_body: Dict[str, Any] = {"info": {"title": title}}

    # documentTitle can ONLY be set during creation, not via batchUpdate
    if document_title:
        form_body["info"]["documentTitle"] = document_title

    created_form = await asyncio.to_thread(
        service.forms().create(body=form_body).execute
    )

    form_id = created_form.get("formId")
    edit_url = f"https://docs.google.com/forms/d/{form_id}/edit"
    responder_url = created_form.get(
        "responderUri", f"https://docs.google.com/forms/d/{form_id}/viewform"
    )

    # Step 2: Use batchUpdate to add description if provided (documentTitle is create-only)
    if description:

        update_requests = []
        info_updates = {"description": description}
        update_mask_fields = ["description"]

        if info_updates:
            update_requests.append(
                {
                    "updateFormInfo": {
                        "info": info_updates,
                        "updateMask": ",".join(update_mask_fields),
                    }
                }
            )

            batch_update_body = {"requests": update_requests}

            try:
                await asyncio.to_thread(
                    service.forms()
                    .batchUpdate(formId=form_id, body=batch_update_body)
                    .execute
                )
            except HttpError as e:
                logger.error(
                    f"[create_form] Failed to update form metadata for {form_id}: {e}"
                )
                # Continue with form creation confirmation even if metadata update failed
                confirmation_message = f"Successfully created form '{title}' for {user_google_email}. Form ID: {form_id}. Edit URL: {edit_url}. Responder URL: {responder_url}. WARNING: Failed to set description/documentTitle: {e}"
                return confirmation_message

    confirmation_message = f"Successfully created form '{title}' for {user_google_email}. Form ID: {form_id}. Edit URL: {edit_url}. Responder URL: {responder_url}"
    logger.info(f"Form created successfully for {user_google_email}. ID: {form_id}")
    return confirmation_message


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("add_questions_to_form")
async def add_questions_to_form(
    service,
    user_google_email: str,
    form_id: str,
    questions: List[Dict[str, Any]],
    insert_at_index: Optional[int] = None,
) -> str:
    """
    Add questions to an existing Google Form using batchUpdate for atomic operations.

       Args:
           user_google_email (str): The user's Google email address. Required.
           form_id (str): The ID of the form to add questions to. Required.
           questions (List[Dict[str, Any]]): A list of question dictionaries.
           insert_at_index (Optional[int]): Index position to insert questions. If None, adds at the end.

       Standard Fields (available for all question types):
           - title (str): Question title
           - type (str): Question type (see supported types below)
           - description (str, optional): Question description
           - required (bool, optional): Whether question is required (default: false)

       Supported Question Types:

       TEXT_QUESTION:
           - paragraph (bool, optional): Multi-line text input (default: false)

       MULTIPLE_CHOICE_QUESTION / CHECKBOX_QUESTION:
           - options (list): List of choice options
           - choice_type (str, optional): 'RADIO', 'CHECKBOX', 'DROP_DOWN' (default: 'RADIO')
           - shuffle (bool, optional): Randomize option order (default: false)

       SCALE_QUESTION:
           - scale_min (int): Minimum scale value
           - scale_max (int): Maximum scale value
           - scale_labels (dict, optional): Labels for min/max values

       DATE_QUESTION:
           - include_time (bool, optional): Include time (default: false)
           - include_year (bool, optional): Include year (default: true)

       TIME_QUESTION:
           - duration (bool, optional): Duration input (default: false)

       RATING_QUESTION:
           - rating_scale_level (int): Scale level
           - icon_type (str): Icon type (e.g., 'STAR', 'HEART')

       IMAGE_ITEM:
           - image (dict): Image properties including source_uri, alt_text, alignment, width

       VIDEO_ITEM:
           - video (dict): Video properties including youtube_uri, alignment, width
           - caption (str, optional): Video caption

       PAGE_BREAK_ITEM:
           - No unique parameters

       TEXT_ITEM:
           - No unique parameters

       QUESTION_GROUP_ITEM:
           - questions (list): List of row questions
           - grid (dict): Grid configuration with choice_type and options
           - shuffle_questions (bool, optional): Shuffle questions (default: false)
           - image (dict, optional): Image properties

       GRADING:
           - point_value (int): Points for the question
           - correct_answers (list): List of correct answers
           - when_right (dict, optional): Feedback when correct
           - when_wrong (dict, optional): Feedback when wrong
           - general_feedback (dict, optional): General feedback

       Note:
           File upload questions are not supported for creation via the API. This schema is for reference.

       Example:
           questions = [
               {
                   "title": "What is your favorite color?",
                   "type": "MULTIPLE_CHOICE_QUESTION",
                   "options": [
                       {"value": "Red"},
                       {"value": "Blue"},
                       {"value": "Green"}
                   ],
                   "required": True,
                   "choice_type": "RADIO",
                   "shuffle": False
               }
           ]

       Returns:
           str: Confirmation message with details about the questions added.
    """
    logger.info(
        f"[add_questions_to_form] Invoked. Email: '{user_google_email}', Form ID: {form_id}, Questions: {len(questions)}"
    )

    if not questions:
        return (
            f"No questions provided to add to form {form_id} for {user_google_email}."
        )

    # Get current form state to validate insertion context (following API best practices)
    form = await asyncio.to_thread(service.forms().get(formId=form_id).execute)
    current_items = form.get("items", [])
    current_item_count = len(current_items)

    # Validate and determine insertion index
    if insert_at_index is None:
        insert_at_index = current_item_count  # Append at end
    elif insert_at_index < 0 or insert_at_index > current_item_count:
        return f"Invalid insertion index {insert_at_index}. Form {form_id} currently has {current_item_count} items. Valid range: 0-{current_item_count}."

    # Build requests with proper sequential indexing for batchUpdate
    question_requests = _build_question_requests(questions, insert_at_index)

    if not question_requests:
        return f"No valid questions to add to form {form_id} for {user_google_email}. Check question types and required fields."

    batch_update_body = {"requests": question_requests}

    # Execute batchUpdate - all operations succeed or all fail atomically (API best practice)
    try:
        batch_response = await asyncio.to_thread(
            service.forms().batchUpdate(formId=form_id, body=batch_update_body).execute
        )

        confirmation_message = f"Successfully added {len(question_requests)} questions to form {form_id} for {user_google_email}. Questions were inserted starting at index {insert_at_index}."
        logger.info(
            f"Questions added successfully to form {form_id} for {user_google_email}."
        )
        return confirmation_message

    except HttpError as e:
        error_message = f"Failed to add questions to form {form_id}: {e}"
        logger.error(error_message)
        return error_message


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("get_form")
async def get_form(service, user_google_email: str, form_id: str) -> str:
    """
    Get a form.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form to retrieve.

    Returns:
        str: Form details including title, description, questions, and URLs.
    """
    logger.info(f"[get_form] Invoked. Email: '{user_google_email}', Form ID: {form_id}")

    form = await asyncio.to_thread(service.forms().get(formId=form_id).execute)

    form_info = form.get("info", {})
    title = form_info.get("title", "No Title")
    description = form_info.get("description", "No Description")
    document_title = form_info.get("documentTitle", title)

    edit_url = f"https://docs.google.com/forms/d/{form_id}/edit"
    responder_url = form.get(
        "responderUri", f"https://docs.google.com/forms/d/{form_id}/viewform"
    )

    items = form.get("items", [])
    questions_details = []
    for i, item in enumerate(items, 1):
        item_id = item.get("itemId", "N/A")
        item_title = item.get("title", f"Question {i}")
        # Use optimized item type detection
        item_type = _detect_item_type(item)
        questions_details.append(f"  {i}. [ID: {item_id}] [{item_type}] {item_title}")

    questions_text = (
        "\n".join(questions_details) if questions_details else "  No questions found"
    )

    result = f"""Form Details for {user_google_email}:
- Title: "{title}"
- Description: "{description}"
- Document Title: "{document_title}"
- Form ID: {form_id}
- Edit URL: {edit_url}
- Responder URL: {responder_url}
- Questions ({len(items)} total):
{questions_text}"""

    logger.info(f"Successfully retrieved form for {user_google_email}. ID: {form_id}")
    return result


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("set_form_publish_state")
async def set_form_publish_state(
    service,
    user_google_email: str,
    form_id: str,
    is_published: bool = False,
    is_accepting_responses: bool = True,
) -> str:
    """
    Updates the internal publish state of a Google Form.
    This controls whether the form is visible and accepts responses, but not public sharing.
    Public sharing is handled via the Drive API's permissions.create method.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form to update.
        is_published (bool): Whether the form is published and visible. Defaults to False.
        is_accepting_responses (bool): Whether the form accepts responses. Defaults to True.
                                       If is_published is False, this field is forced to False by API.

    Returns:
        str: Confirmation message of the successful publish state update.
    """
    logger.info(
        f"[set_form_publish_state] Invoked. Email: '{user_google_email}', Form ID: {form_id}"
    )

    # Construct the request body based on the Forms API's PublishSettings object
    # The setPublishSettings method expects a body with "publishSettings" and "updateMask"
    settings_body = {
        "publishSettings": {
            "publishState": {
                "isPublished": is_published,
                "isAcceptingResponses": is_accepting_responses,
            }
        },
        "updateMask": "publishState.isPublished,publishState.isAcceptingResponses",
    }

    await asyncio.to_thread(
        service.forms().setPublishSettings(formId=form_id, body=settings_body).execute
    )

    confirmation_message = f"Successfully updated publish state for form {form_id} for {user_google_email}. Is Published: {is_published}, Is Accepting Responses: {is_accepting_responses}"
    logger.info(
        f"Publish state updated successfully for {user_google_email}. Form ID: {form_id}"
    )
    return confirmation_message


@server.tool()
@require_google_service("drive", "drive_file")  # Requires Drive API access
@handle_http_errors("publish_form_publicly")
async def publish_form_publicly(
    service,  # This will be the Drive service due to @require_google_service("drive", "drive_file")
    user_google_email: str,
    form_id: str,
    public: bool = True,
) -> str:
    """
    Publishes a Google Form to be publicly accessible (anyone with the link can view).
    This uses the Google Drive API to manage file permissions.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form (which is also a Drive file ID).
        public (bool): If True, makes the form publicly viewable. If False, removes public access.

    Returns:
        str: Confirmation message of the public sharing update.
    """
    logger.info(
        f"[publish_form_publicly] Invoked. Email: '{user_google_email}', Form ID: {form_id}, Public: {public}"
    )

    if public:
        permission_body = {"role": "reader", "type": "anyone"}
        await asyncio.to_thread(
            service.permissions()
            .create(
                fileId=form_id,
                body=permission_body,
                supportsAllDrives=True,  # Required for shared drive items
            )
            .execute
        )
        confirmation_message = f"Successfully made form {form_id} publicly viewable for {user_google_email}."
    else:
        # To remove public access, we need to find the 'anyone' permission and delete it
        permissions_list = await asyncio.to_thread(
            service.permissions().list(fileId=form_id, supportsAllDrives=True).execute
        )

        anyone_permission_id = None
        for perm in permissions_list.get("permissions", []):
            if perm.get("type") == "anyone":
                anyone_permission_id = perm["id"]
                break

        if anyone_permission_id:
            await asyncio.to_thread(
                service.permissions()
                .delete(
                    fileId=form_id,
                    permissionId=anyone_permission_id,
                    supportsAllDrives=True,
                )
                .execute
            )
            confirmation_message = f"Successfully removed public access for form {form_id} for {user_google_email}."
        else:
            confirmation_message = f"Form {form_id} was not publicly accessible for {user_google_email}. No changes made."

    logger.info(
        f"Public sharing updated successfully for {user_google_email}. Form ID: {form_id}"
    )
    return confirmation_message


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("get_form_response")
async def get_form_response(
    service, user_google_email: str, form_id: str, response_id: str
) -> str:
    """
    Get one response from the form.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form.
        response_id (str): The ID of the response to retrieve.

    Returns:
        str: Response details including answers and metadata.
    """
    logger.info(
        f"[get_form_response] Invoked. Email: '{user_google_email}', Form ID: {form_id}, Response ID: {response_id}"
    )

    response = await asyncio.to_thread(
        service.forms().responses().get(formId=form_id, responseId=response_id).execute
    )

    response_id = response.get("responseId", "Unknown")
    create_time = response.get("createTime", "Unknown")
    last_submitted_time = response.get("lastSubmittedTime", "Unknown")

    answers = response.get("answers", {})
    answer_details = []
    for question_id, answer_data in answers.items():
        question_response = answer_data.get("textAnswers", {}).get("answers", [])
        if question_response:
            answer_text = ", ".join([ans.get("value", "") for ans in question_response])
            answer_details.append(f"  Question ID {question_id}: {answer_text}")
        else:
            answer_details.append(f"  Question ID {question_id}: No answer provided")

    answers_text = "\n".join(answer_details) if answer_details else "  No answers found"

    result = f"""Form Response Details for {user_google_email}:
- Form ID: {form_id}
- Response ID: {response_id}
- Created: {create_time}
- Last Submitted: {last_submitted_time}
- Answers:
{answers_text}"""

    logger.info(
        f"Successfully retrieved response for {user_google_email}. Response ID: {response_id}"
    )
    return result


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("list_form_responses")
async def list_form_responses(
    service,
    user_google_email: str,
    form_id: str,
    page_size: int = 10,
    page_token: Optional[str] = None,
) -> str:
    """
    List a form's responses.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form.
        page_size (int): Maximum number of responses to return. Defaults to 10.
        page_token (Optional[str]): Token for retrieving next page of results.

    Returns:
        str: List of responses with basic details and pagination info.
    """
    logger.info(
        f"[list_form_responses] Invoked. Email: '{user_google_email}', Form ID: {form_id}"
    )

    params = {"formId": form_id, "pageSize": page_size}
    if page_token:
        params["pageToken"] = page_token

    responses_result = await asyncio.to_thread(
        service.forms().responses().list(**params).execute
    )

    responses = responses_result.get("responses", [])
    next_page_token = responses_result.get("nextPageToken")

    if not responses:
        return f"No responses found for form {form_id} for {user_google_email}."

    response_details = []
    for i, response in enumerate(responses, 1):
        response_id = response.get("responseId", "Unknown")
        create_time = response.get("createTime", "Unknown")
        last_submitted_time = response.get("lastSubmittedTime", "Unknown")

        answers_count = len(response.get("answers", {}))
        response_details.append(
            f"  {i}. Response ID: {response_id} | Created: {create_time} | Last Submitted: {last_submitted_time} | Answers: {answers_count}"
        )

    responses_text = "\n".join(response_details)
    result = f"""Form Responses for {user_google_email}:
- Form ID: {form_id}
- Total Responses: {len(responses)}
- Next Page Token: {next_page_token if next_page_token else 'N/A'}
- Responses:
{responses_text}"""

    logger.info(
        f"Successfully listed responses for {user_google_email}. Form ID: {form_id}"
    )
    return result


@server.tool()
@require_google_service("forms", "forms")
@handle_http_errors("update_form_questions")
async def update_form_questions(
    service,
    user_google_email: str,
    form_id: str,
    questions_to_update: List[Dict[str, Any]],
) -> str:
    """
    Updates existing questions in a Google Form using batchUpdate.

    Each question dictionary must include an 'item_id' to identify the question to update,
    and can include any of the updatable fields for that question type.

    Args:
        user_google_email (str): The user's Google email address. Required.
        form_id (str): The ID of the form to update questions in. Required.
        questions_to_update (List[Dict[str, Any]]): A list of dictionaries defining questions to update.
                                                    Each dict must contain 'item_id' and fields to update.

    Returns:
        str: Confirmation message with details about the questions updated.
    """
    logger.info(
        f"[update_form_questions] Invoked. Email: '{user_google_email}', Form ID: {form_id}, Questions to update: {len(questions_to_update)}"
    )

    if not questions_to_update:
        return f"No questions provided to update for form {form_id} for {user_google_email}."

    # CRITICAL FIX: Get current form state to determine item types for preservation
    # This prevents "cannot be changed into non question Item type" errors
    form = await asyncio.to_thread(service.forms().get(formId=form_id).execute)
    current_items = form.get("items", [])
    item_type_map = {}
    for item in current_items:
        item_id = item.get("itemId")
        if item_id:
            # Use optimized item type detection
            for item_type_key in ITEM_TYPE_MAPPINGS:
                if item_type_key in item:
                    item_type_map[item_id] = item_type_key
                    break
            else:
                # CRITICAL: If we can't determine the type, assume it's a questionItem
                item_type_map[item_id] = "questionItem"

            # DEBUG: Log the detected item type and actual structure
            logger.debug(
                f"Item {item_id}: detected type = {item_type_map[item_id]}, keys = {list(item.keys())}"
            )

    requests = []
    for q_update in questions_to_update:
        item_id = q_update.get("item_id") or q_update.get("itemId")
        if not item_id:
            logger.warning(
                f"Skipping question update due to missing 'item_id' or 'itemId': {q_update}"
            )
            continue

        # Validate item type compatibility with provided data
        validation_errors = []
        for item_type in ["videoItem", "imageItem"]:
            if item_type in q_update:
                errors = _validate_item_update_data(item_type, q_update)
                validation_errors.extend(errors)

        if validation_errors:
            logger.warning(
                f"Validation errors for item_id {item_id}: {validation_errors}"
            )
            continue

        # CRITICAL FIX: Determine the original item type to preserve structure
        original_item_type = item_type_map.get(item_id)
        if not original_item_type:
            logger.warning(
                f"Could not determine original item type for item_id {item_id}. Skipping update."
            )
            continue

        # Validate that we have at least one valid update field
        has_valid_update = any(field in q_update for field in VALID_UPDATE_FIELDS)

        if not has_valid_update:
            logger.warning(
                f"No valid update fields found for item_id {item_id}. Available fields: {list(q_update.keys())}"
            )
            continue

        update_mask_fields = []
        updated_item_content = {}

        # CRITICAL FIX: Always preserve the original item type structure
        # Initialize the correct item type structure
        item_type_initializers = {
            "videoItem": lambda: {"videoItem": {}},
            "imageItem": lambda: {"imageItem": {}},
            "questionItem": lambda: {"questionItem": {"question": {}}},
            "questionGroupItem": lambda: {"questionGroupItem": {}},
            "pageBreakItem": lambda: {"pageBreakItem": {}},
            "textItem": lambda: {"textItem": {}},
        }

        initializer = item_type_initializers.get(original_item_type)
        if initializer:
            updated_item_content.update(initializer())

        # Update common item fields
        if "title" in q_update:
            updated_item_content["title"] = q_update["title"]
            update_mask_fields.append("title")
        if "description" in q_update:
            updated_item_content["description"] = q_update["description"]
            update_mask_fields.append("description")

        # Use optimized field processors
        for field_type, processor in FIELD_PROCESSORS.items():
            processor(
                q_update, original_item_type, updated_item_content, update_mask_fields
            )

        if not update_mask_fields:
            logger.warning(
                f"No updatable fields found for item_id {item_id}. Skipping update."
            )
            continue

        # Add the item ID to the item content itself
        updated_item_content["itemId"] = item_id

        # Construct UpdateItemRequest according to Google Forms API spec
        update_item_request = {
            "item": updated_item_content,
            "location": {
                "index": 0  # We'll need to get the actual index, but for now use 0
            },
            "updateMask": ",".join(update_mask_fields),
        }

        # Create request with exact key name needed by Google API
        requests.append({"updateItem": update_item_request})

    if not requests:
        return f"No valid update requests generated for form {form_id} for {user_google_email}."

    import json

    # Construct the batch update body with exact key names for Google API
    batch_update_body = {"requests": requests}

    # CRITICAL DEBUG LOGGING: Log the outgoing payload for verification
    try:
        # Use the Google API client's batchUpdate method directly
        batch_update_request = service.forms().batchUpdate(
            formId=form_id, body=batch_update_body
        )
        batch_response = await asyncio.to_thread(batch_update_request.execute)
        confirmation_message = f"Successfully updated {len(requests)} questions in form {form_id} for {user_google_email}."
        logger.info(
            f"Questions updated successfully in form {form_id} for {user_google_email}."
        )
        return confirmation_message
    except HttpError as e:
        error_message = f"Failed to update questions in form {form_id}: {e}"
        logger.error(error_message)
        return error_message
