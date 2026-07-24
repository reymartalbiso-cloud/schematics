"""Claude integration: natural language -> structured JSON spec via forced tool use.

Claude never generates DXF or draws anything - it only emits the small JSON spec
defined by the tool schemas below. cad_engine.py / container_engine.py turn that
JSON into real geometry deterministically.
"""
import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
MAX_TOKENS = 4000

_POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

# Open-ended escape hatch: anything the user asks for that has no dedicated
# first-class field goes here and is drawn as a dashed labeled placeholder
# zone - visible but clearly lower fidelity - rather than being dropped.
_ADDITIONAL_ELEMENTS_SCHEMA = {
    "type": "array",
    "description": (
        "Catch-all for elements the user asked for that have NO dedicated field "
        "above (e.g. a skylight, a loft bed, a built-in wardrobe, a wood stove, a "
        "carport). Never silently ignore a requested feature - if nothing else "
        "fits, put it here so it is drawn as a labeled zone. Do NOT use this for "
        "things that DO have a proper field (walls, doors, windows, kitchen, "
        "bathroom, rooms, deck, balcony) - use those. Positions/sizes are in mm "
        "in the same coordinate space as the rest of the drawing; approx_position "
        "is the CENTRE of the zone."
    ),
    "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Short human label drawn on the zone."},
            "approx_position": {**_POINT, "description": "Centre [x, y] of the zone in mm."},
            "approx_size_mm": {**_POINT, "description": "[width, height] of the zone in mm."},
            "notes": {"type": "string", "description": "Optional: what was left unspecified."},
        },
        "required": ["label", "approx_position", "approx_size_mm"],
    },
}

FLOORPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "meta": {
            "type": "object",
            "properties": {"units": {"type": "string"}},
        },
        "additional_elements": _ADDITIONAL_ELEMENTS_SCHEMA,
        "walls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "start": _POINT,
                    "end": _POINT,
                    "thickness": {"type": "number"},
                },
                "required": ["id", "start", "end", "thickness"],
            },
        },
        "openings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["door", "window"]},
                    "wall_id": {"type": "string"},
                    "position_along_wall": {"type": "number"},
                    "width": {"type": "number"},
                    "swing": {"type": "string", "enum": ["left", "right"]},
                },
                "required": ["type", "wall_id", "position_along_wall", "width"],
            },
        },
        "rooms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "area_sqm": {"type": "number"},
                    "label_position": _POINT,
                },
                "required": ["name", "label_position"],
            },
        },
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": _POINT,
                    "end": _POINT,
                    "offset": {"type": "number"},
                },
                "required": ["start", "end", "offset"],
            },
        },
    },
    "required": ["walls"],
}

_ROOM_SCHEMA = {
    "type": "object",
    "description": (
        "One interior room in a left-to-right series that fills the container. "
        "Rooms are separated by partition walls with doors and drawn with "
        "furniture symbols for their type."
    ),
    "properties": {
        "name": {"type": "string", "description": "Room label, e.g. 'Master Bedroom', 'Living Room'."},
        "type": {
            "type": "string",
            "enum": ["bedroom", "bathroom", "kitchen", "living", "office", "dining", "storage", "stair", "empty"],
        },
        "width_mm": {"type": "number", "description": "Room width along the container length."},
        "bed": {"type": "string", "enum": ["single", "double"], "description": "For bedrooms."},
        "fixtures": {
            "type": "array",
            "description": "For bathrooms: any of toilet, shower, basin.",
            "items": {"type": "string", "enum": ["toilet", "shower", "basin"]},
        },
    },
    "required": ["type", "width_mm"],
}

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "rooms": {
            "type": "array",
            "description": (
                "GENERAL multi-room interior: a left-to-right series of typed rooms "
                "(bedroom, bathroom, kitchen, living, office, dining, storage, stair) "
                "that partition the container. Use this for anything with bedrooms or "
                "multiple rooms - it is the correct way to model a real container home "
                "(e.g. bathroom | bedroom | kitchen | bedroom | bathroom). Widths should "
                "roughly sum to the container's interior length."
            ),
            "items": _ROOM_SCHEMA,
        },
        "balcony": {
            "type": "object",
            "description": "A railed balcony/terrace in front of the container (alternative to a fold-out deck).",
            "properties": {"depth_mm": {"type": "number"}, "label": {"type": "string"}},
            "required": ["depth_mm"],
        },
        "kitchen_run": {
            "type": "object",
            "properties": {
                "depth_mm": {"type": "number"},
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "width_mm": {"type": "number"},
                        },
                        "required": ["label", "width_mm"],
                    },
                },
            },
            "required": ["depth_mm", "segments"],
        },
        "sliding_door": {
            "type": "object",
            "description": "A single sliding door on the front wall. For more than one door, use sliding_doors instead.",
            "properties": {
                "width_mm": {"type": "number"},
                "position_from_left_mm": {"type": "number"},
                "height_mm": {"type": "number"},
            },
            "required": ["width_mm", "position_from_left_mm"],
        },
        "sliding_doors": {
            "type": "array",
            "description": (
                "Multiple sliding doors on the front wall. Use this (not "
                "sliding_door) whenever the user asks for two or more doors, "
                "giving each its own position so they don't overlap."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "width_mm": {"type": "number"},
                    "position_from_left_mm": {"type": "number"},
                    "height_mm": {"type": "number"},
                },
                "required": ["width_mm", "position_from_left_mm"],
            },
        },
        "windows": {
            "type": "array",
            "description": (
                "Windows in the back wall (the wall the kitchen run sits "
                "against), each drawn with a W:width*height callout."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "width_mm": {"type": "number"},
                    "height_mm": {"type": "number"},
                    "position_from_left_mm": {"type": "number"},
                },
                "required": ["width_mm", "height_mm", "position_from_left_mm"],
            },
        },
        "deck": {
            "type": "object",
            "description": (
                "Fold-out deck/terrace in front of the sliding-door wall, "
                "drawn in plan with projection lines and its own dimensions."
            ),
            "properties": {"depth_mm": {"type": "number"}},
            "required": ["depth_mm"],
        },
        "bathroom": {
            "type": "object",
            "description": (
                "Single enclosed bathroom at one end (simple layouts only). For "
                "anything with bedrooms or several rooms, use `rooms` instead and "
                "include a bathroom room there. Fixtures belong HERE - never as "
                "kitchen_run segments."
            ),
            "properties": {
                "width_mm": {
                    "type": "number",
                    "description": "Interior width of the bathroom along the container length.",
                },
                "position": {"type": "string", "enum": ["left", "right"]},
                "fixtures": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["toilet", "shower", "basin"]},
                },
            },
            "required": ["width_mm", "position"],
        },
    },
}

CONTAINER_SCHEMA = {
    "type": "object",
    "properties": {
        "container": {
            "type": "object",
            "properties": {
                "length_mm": {"type": "number"},
                "width_mm": {"type": "number"},
                "height_mm": {"type": "number"},
                "model": {"type": "string"},
            },
            "required": ["length_mm", "width_mm", "height_mm"],
        },
        "additional_elements": _ADDITIONAL_ELEMENTS_SCHEMA,
        "levels": {
            "type": "array",
            "description": (
                "For STACKED / multi-storey modular homes (two or more containers "
                "stacked). Each level is one storey with its own plan; include a room "
                "of type 'stair' on levels that connect to another storey. When levels "
                "is used, put the interior in each level's plan (not the top-level plan)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "e.g. 'Ground Floor', 'Upper Floor'."},
                    "plan": _PLAN_SCHEMA,
                },
                "required": ["plan"],
            },
        },
        "plan": _PLAN_SCHEMA,
        "front_elevation": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "height_mm": {"type": "number"},
                "glazing_panels": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "width_mm": {"type": "number"},
                            "type": {
                                "type": "string",
                                "enum": ["frame", "fixed_glass", "sliding_glass"],
                            },
                        },
                        "required": ["width_mm", "type"],
                    },
                },
                "frame_callouts": {"type": "array", "items": {"type": "string"}},
                "cable_bracing": {"type": "boolean"},
            },
        },
        "side_elevation": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "fold_out_platform": {
                    "type": "object",
                    "properties": {
                        "width_mm": {"type": "number"},
                        "swing_radius_mm": {"type": "number"},
                    },
                    "required": ["width_mm", "swing_radius_mm"],
                },
                "floor_extension_label": {"type": "string"},
            },
        },
        "back_elevation": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "vent_window": {
                    "type": "object",
                    "properties": {
                        "width_mm": {"type": "number"},
                        "height_mm": {"type": "number"},
                        "position_from_left_mm": {"type": "number"},
                        "center_height_mm": {"type": "number"},
                    },
                    "required": [
                        "width_mm",
                        "height_mm",
                        "position_from_left_mm",
                        "center_height_mm",
                    ],
                },
            },
        },
    },
    "required": ["container"],
}

FLOORPLAN_TOOL = {
    "name": "emit_floorplan_spec",
    "description": "Emit a complete floor plan specification as structured JSON.",
    "input_schema": FLOORPLAN_SCHEMA,
}

CONTAINER_TOOL = {
    "name": "emit_container_home_spec",
    "description": (
        "Emit a complete container-home shop-drawing specification as structured JSON."
    ),
    "input_schema": CONTAINER_SCHEMA,
}

_SHARED_RULES = """
- All text output must be in English only.
- All dimensions are in millimeters.
- Build exactly what the user describes. Never default toward a specific layout, fixture set, or template unless implied by their request. Every optional section of the schema should be included only when relevant to what the user actually asked for - omit sections entirely when not implied.
- When the user is editing an existing design (a current spec is provided), return the complete updated spec with the requested change applied. Do not drop unrelated elements that were not mentioned in the edit instruction.
- Keep geometry realistic: segment/panel widths and positions should sum sensibly within the container's or wall's actual dimensions.
"""

FLOORPLAN_SYSTEM_PROMPT = (
    "You translate plain-English floor plan descriptions into a structured JSON spec "
    "via the emit_floorplan_spec tool. You never draw anything yourself - a separate "
    "deterministic renderer turns your JSON into DXF geometry.\n" + _SHARED_RULES
)

CONTAINER_SYSTEM_PROMPT = (
    "You translate plain-English container-home design requests into a structured JSON "
    "spec via the emit_container_home_spec tool. You never draw anything yourself - a "
    "separate deterministic renderer turns your JSON into a multi-view shop-drawing "
    "sheet. The schema is a vocabulary, not a template: only include a section when the "
    "user's request implies it.\n"
    "- Choosing how to describe the interior:\n"
    "  * Anything with BEDROOMS or MULTIPLE ROOMS (a home, an office with rooms, a "
    "studio) -> use plan.rooms: a left-to-right series of typed rooms (bedroom, "
    "bathroom, kitchen, living, office, dining, storage, stair) whose widths roughly "
    "sum to the container's interior length. This is how the real reference homes are "
    "built (e.g. bathroom | bedroom | kitchen | bedroom | bathroom).\n"
    "  * A simple single-purpose unit (just a kitchenette + one bathroom, or an open "
    "cafe counter) may instead use the flat kitchen_run + bathroom fields.\n"
    "  * Never invent an empty container: if the user names rooms, model them in "
    "plan.rooms. Never force a non-bathroom room into the bathroom field.\n"
    "- STACKED / multi-storey / modular (two containers stacked, a two-storey home): use "
    "the top-level levels array - one entry per storey, each with its own plan. Put a "
    "room of type 'stair' on storeys that connect vertically. Do not also fill the "
    "top-level plan when using levels.\n"
    "- kitchen_run/kitchen-room segments are strictly kitchen elements (hob/stove, "
    "counter, cabinet, sink, fridge). Bathroom fixtures (toilet, shower, basin) belong "
    "in a bathroom (a bathroom room, or plan.bathroom) - never as kitchen segments.\n"
    "- balcony vs deck: a fold-out ground-level terrace -> plan.deck; a railed raised "
    "balcony (common on an upper storey) -> plan.balcony.\n"
    "- When the user resizes the container (e.g. 20ft -> 40ft), update container "
    "dimensions and re-check that positioned elements still sit sensibly, and that "
    "room widths still sum to the new interior length.\n" + _SHARED_RULES
)


class ClarificationNeeded(Exception):
    """Raised when the request is too underspecified to draw and Claude has
    asked a plain-text clarifying question instead of emitting a spec. The
    question is carried through to the user."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


# Appended to the system prompt so Claude knows it MAY ask instead of guess.
_CLARIFY_RULE = (
    "\nAsking vs. guessing: normally emit the spec via the tool. But if the "
    "request is genuinely underspecified in a way that would MATERIALLY change "
    "the drawing and you cannot reasonably default it - e.g. which wall an "
    "element goes on, or how many of something - reply with ONE short plain-text "
    "clarifying question INSTEAD of calling the tool, and do not guess. Only ask "
    "when it matters: default minor things silently (exact millimetre placement, "
    "a sensible standard size, the most obvious open wall). Never ask more than "
    "one question, and never ask when you already have enough to draw something "
    "reasonable."
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _build_content(current_spec, instruction_label, user_text, context_block, problems=None):
    parts = []
    if context_block:
        parts.append(context_block)
    if current_spec is not None:
        parts.append(f"Current spec: {json.dumps(current_spec)}")
    parts.append(f"{instruction_label}: {user_text}")
    if problems:
        parts.append(
            "Your previous spec had these problems - return a corrected spec that "
            "fixes them (do NOT ask a question this time, just fix and emit). Keep "
            "the container's stated length/width/height UNCHANGED - resolve the "
            "conflict by adjusting the interior elements (fixture/panel/room widths "
            "or count), never by enlarging the container:\n"
            + "\n".join(f"- {p}" for p in problems)
        )
    return "\n\n".join(parts)


def _call(system_prompt, tool, current_spec, user_text, context_block, problems=None):
    """Call Claude once. When `problems` is None the tool is optional, so
    Claude may return a plain-text clarifying question (-> ClarificationNeeded).
    When `problems` is given (a self-correction pass) the tool is forced."""
    label = "Instruction" if current_spec is not None else "Design request"
    content = _build_content(current_spec, label, user_text, context_block, problems)
    forcing = problems is not None
    system = system_prompt if forcing else system_prompt + _CLARIFY_RULE
    response = _client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[tool],
        # Force the tool on a correction pass; otherwise let Claude choose
        # between emitting the spec and asking a clarifying question.
        tool_choice={"type": "tool", "name": tool["name"]} if forcing else {"type": "auto"},
        messages=[{"role": "user", "content": content}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    # No tool call -> treat any returned text as a clarifying question.
    question = " ".join(
        b.text.strip() for b in response.content if getattr(b, "type", None) == "text" and b.text.strip()
    ).strip()
    if question:
        raise ClarificationNeeded(question)
    raise RuntimeError("Claude returned neither a spec nor a question.")


def generate_floorplan_spec(text: str, current_spec: dict | None = None, context_block: str = "",
                            correction_problems: list | None = None) -> dict:
    return _call(FLOORPLAN_SYSTEM_PROMPT, FLOORPLAN_TOOL, current_spec, text, context_block, correction_problems)


def generate_container_spec(text: str, current_spec: dict | None = None, context_block: str = "",
                            correction_problems: list | None = None) -> dict:
    return _call(CONTAINER_SYSTEM_PROMPT, CONTAINER_TOOL, current_spec, text, context_block, correction_problems)
