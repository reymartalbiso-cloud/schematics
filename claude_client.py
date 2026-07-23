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

FLOORPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "meta": {
            "type": "object",
            "properties": {"units": {"type": "string"}},
        },
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
        "plan": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
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
                    "properties": {
                        "width_mm": {"type": "number"},
                        "position_from_left_mm": {"type": "number"},
                    },
                    "required": ["width_mm", "position_from_left_mm"],
                },
            },
        },
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
    "sheet. The schema is a vocabulary, not a template: only include a section (kitchen "
    "run, sliding door, glazing pattern, fold-out platform, vent window, etc.) when the "
    "user's request implies it.\n" + _SHARED_RULES
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _build_content(current_spec, instruction_label, user_text, context_block):
    parts = []
    if context_block:
        parts.append(context_block)
    if current_spec is not None:
        parts.append(f"Current spec: {json.dumps(current_spec)}")
    parts.append(f"{instruction_label}: {user_text}")
    return "\n\n".join(parts)


def _call(system_prompt, tool, current_spec, user_text, context_block):
    label = "Instruction" if current_spec is not None else "Design request"
    content = _build_content(current_spec, label, user_text, context_block)
    response = _client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Claude did not return a tool_use block")


def generate_floorplan_spec(text: str, current_spec: dict | None = None, context_block: str = "") -> dict:
    return _call(FLOORPLAN_SYSTEM_PROMPT, FLOORPLAN_TOOL, current_spec, text, context_block)


def generate_container_spec(text: str, current_spec: dict | None = None, context_block: str = "") -> dict:
    return _call(CONTAINER_SYSTEM_PROMPT, CONTAINER_TOOL, current_spec, text, context_block)
