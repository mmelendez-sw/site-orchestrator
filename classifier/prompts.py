"""Vision prompts and structured-output schemas for the site classifier.

Claude (tool ``input_schema``) and Gemini (``response_schema``) take the same
fields in two JSON-schema dialects. Both are generated from one field spec so
they cannot drift.
"""

from __future__ import annotations

from typing import Any

from salesforce.site_type_mapping import TOWER_SUBTYPE_VALUES

SITE_TYPE_VALUES: tuple[str, ...] = ("tower", "rooftop", "other", "unclear")
TOWER_ONLY_SITE_TYPE_VALUES: tuple[str, ...] = ("tower", "other", "unclear")
CELL_GEAR_KIND_VALUES: tuple[str, ...] = (
    "sector_panel",
    "facade_mount",
    "microwave",
    "rru",
    "parapet_mast",
    "none",
    "unclear",
)
TOWER_SUBTYPE_SCHEMA_VALUES = list(TOWER_SUBTYPE_VALUES)

# --------------------------------- schemas ----------------------------------

# (name, type, enum, gemini_nullable). Types use the Claude/JSON-schema names.
_CLASSIFY_FIELDS: tuple[tuple[str, str, tuple[str, ...] | None, bool], ...] = (
    ("site_type", "string", SITE_TYPE_VALUES, False),
    ("tower_subtype", "string", tuple(TOWER_SUBTYPE_VALUES), True),
    ("site_confidence", "number", None, False),
    ("site_evidence", "string", None, False),
    ("asset_box_2d", "integer[]", None, True),
    ("asset_view", "string", None, True),
    ("cell_equipment", "boolean", None, True),
    ("cell_equipment_confidence", "number", None, False),
    ("cell_equipment_evidence", "string", None, False),
    ("cell_gear_kind", "string", CELL_GEAR_KIND_VALUES, True),
)
_CLASSIFY_REQUIRED = ["site_type", "site_confidence", "site_evidence"]


def _type_node(kind: str, *, gemini: bool) -> dict[str, Any]:
    def name(text: str) -> str:
        return text.upper() if gemini else text

    if kind.endswith("[]"):
        return {"type": name("array"), "items": {"type": name(kind[:-2])}}
    return {"type": name(kind)}


def _field_schema(
    kind: str,
    enum: tuple[str, ...] | None,
    nullable: bool,
    *,
    gemini: bool,
) -> dict[str, Any]:
    node = _type_node(kind, gemini=gemini)
    if enum is not None:
        node["enum"] = list(enum)
    if gemini and nullable:
        node["nullable"] = True
    return node


def classify_schema(
    *, gemini: bool, site_types: tuple[str, ...] = SITE_TYPE_VALUES
) -> dict[str, Any]:
    """Classification reply schema in the Claude or Gemini dialect."""
    properties = {
        name: _field_schema(
            kind,
            site_types if name == "site_type" else enum,
            nullable,
            gemini=gemini,
        )
        for name, kind, enum, nullable in _CLASSIFY_FIELDS
    }
    return {
        "type": "OBJECT" if gemini else "object",
        "properties": properties,
        "required": list(_CLASSIFY_REQUIRED),
    }


def scan_schema(*, gemini: bool) -> dict[str, Any]:
    """Zoom-scout reply: up to four candidate boxes with a reason each."""
    obj = "OBJECT" if gemini else "object"
    candidate = {
        "type": obj,
        "properties": {
            "box_2d": _type_node("integer[]", gemini=gemini),
            "reason": _type_node("string", gemini=gemini),
        },
        "required": ["box_2d", "reason"],
    }
    return {
        "type": obj,
        "properties": {
            "candidates": {
                "type": "ARRAY" if gemini else "array",
                "items": candidate,
            },
        },
        "required": ["candidates"],
    }


RESPONSE_SCHEMA = classify_schema(gemini=False)
GEMINI_RESPONSE_SCHEMA = classify_schema(gemini=True)
SCAN_SCHEMA = scan_schema(gemini=False)
GEMINI_SCAN_SCHEMA = scan_schema(gemini=True)

# --------------------------------- prompts ----------------------------------

CLASSIFICATION_PROMPT = """\
You are analyzing aerial imagery of one location where a cellular-infrastructure \
asset is expected. One or more views are provided, each preceded by a text label:
- "NAIP top-down": wide straight-down chip (~250 m across, ~1 m resolution). \
The recorded coordinates can be off by tens of meters, so the asset may appear \
ANYWHERE in this chip, not just at the center.
- "Nearmap top-down": recent high-resolution (~7 cm) straight-down view of the \
same location, usually covering a smaller area than the NAIP chip.
- "Nearmap oblique (North/East/South/West)": 45-degree angled views of the same \
location. These reveal the vertical sides of structures - towers, masts, and \
rooftop antennas that are nearly invisible from straight above stand out \
clearly here. Weight them heavily in every task.

Definitions:
- TOWER SITE: a ground-based, purpose-built vertical structure carrying \
antennas - monopole, lattice/self-support tower, guyed mast, or a disguised \
mast (monopalm / monopine / canister shroud). Top-down cues: tiny footprint, \
long thin linear shadow, lattice cross-pattern, guy wires, small \
cleared/fenced compound with equipment cabinets, or a palm/pine that is \
far taller and straighter than its neighbors. Oblique cues: a tall thin \
structure rising far above its surroundings — including a faux palm/pine.
- ROOFTOP SITE: a building whose roof hosts cellular telecom equipment. True \
cues: panel antennas / sector frames at roof corners or edges (often 3 sectors), \
triangular or steel antenna mounts, microwave backhaul dishes, RRUs, telecom \
cabinets with cable trays, short masts or poles carrying panel antennas on the \
parapet. Ordinary building mechanicals alone are NOT a rooftop cell site.
- STEALTH TREE / DISGUISED MAST: a purpose-built telecom mast camouflaged as \
a palm (monopalm) or pine (monopine), or a slim canister/flagpole shroud. \
Cues: unnaturally straight/thick trunk, crown taller and more regular than \
nearby real trees, antenna cylinder or panel bulge in the fronds, equipment \
cabinets or a fenced pad at the base. Often sits at a lot edge, behind a \
billboard, or in a corner of the chip — search edges. Natural palms/pines \
have tapered trunks, irregular fronds, and no cabinets; those are not towers.
- STEALTH / BUILDING-TOWER SITE: a structure that looks like a building but \
has a tall narrow tower section - church steeple, clock tower, faux-building \
monopole, or a tower segment rising from one corner of a larger footprint. \
From above: a compact building with an unusually tall shadow from one corner \
or a square tower block on the roofline; antennas may sit on the tower cap.

Perform three tasks:

TASK 1 - site_type. Search the ENTIRE extent of EVERY view - edges and corners \
included, never just the center - and classify the site:
- The recorded pin is often tens of meters off. If the center is a parking lot, \
empty pavement, driveway, or landscaping next to a larger commercial / mall \
building, search adjacent rooftops and tower compounds in the chip for cellular \
gear — do not stop at "other" just because the pin itself is pavement.
- "tower": a PURPOSE-BUILT cellular/telecom tower is visible. Must show cell \
platforms, sector racks, microwave dishes, a fenced telecom compound, or a \
disguised mast (monopalm/monopine/canister) — NOT a wood utility pole, street \
light, traffic signal, power transmission lattice, or a natural tree. A \
too-tall straight palm/pine with a regular crown and cabinets or an antenna \
bulge is stealth, not "other". Power/transmission lattices and bare utility \
poles are "other".
- "rooftop": no tower present, and a building roof hosts (or most plausibly \
hosts) the equipment.
- "other": neither applies (water tank, silo, bare field, power lattice, \
utility pole without cell racks, etc.) - describe it.
- "unclear": image quality or ambiguity prevents a confident call.
When site_type is "tower", also set tower_subtype to the best match:
- "monopole": single thin pole, minimal footprint, no lattice faces
- "guyed": guy wires or anchor pads visible from above or oblique
- "self_support": lattice or solid self-supporting tower legs
- "stealth": ONLY a purpose-built disguised telecom mast — e.g. monopalm \
(faux palm), monopine (faux pine), slim flagpole/canister shroud, or a \
freestanding faux-steeple mast with visible antenna bays/slots. A palm- or \
pine-disguised mast is stealth, never flagpole. Do NOT use stealth for \
ordinary church steeples, cupolas, elevator penthouses, decorative \
parking-lot towers, building corners, real trees, or architecture that \
merely "could" hide antennas. If unsure, use other_tower or unclear — never \
guess stealth.
- "water_tower": elevated tank on legs
- "silo": agricultural/industrial silo hosting antennas
- "flagpole": slim mast with a flag or ball finial and no foliage disguise \
and no RF canister/shroud. A cylindrical telecom shroud is stealth; faux \
fronds/needles are stealth.
- "smokestack": industrial stack
- "other_tower": tower present but subtype unclear
- "unclear": tower confirmed but subtype not discernible
When site_type is not "tower", set tower_subtype to null.
Never set cell_equipment=true on a stealth call from "likely conceals" or \
"typical for stealth" alone. For monopalm/monopine, true when you see \
equipment cabinets or a fenced pad at the base, a cylindrical antenna bay \
or panel bulge in the crown, or panels/dishes through the fronds — do not \
require exposed sector racks like a bare monopole. Real trees with no \
cabinets or bulge stay cell_equipment=false.
Set site_confidence to at most 0.6 unless two or more independent cues or \
views corroborate the call.

TASK 2 - locate the asset / cellular gear. Report:
- asset_box_2d: [ymin, xmin, ymax, xmax], integers in 0-1000 normalized image \
coordinates for ONE view only.
- asset_view: the exact label of that same view (e.g. "Nearmap oblique (South)").
Boxing rules (critical):
- Draw the box TIGHTLY around the cellular hardware you are claiming \
(sector panels, dishes, RRUs, parapet masts, facade mounts) — not the whole \
building, whole roof, parking deck, or HVAC plant.
- Prefer the Nearmap oblique where those antennas are clearest. Use Nearmap \
top-down only when gear is unmistakable from above. Prefer NAIP only when no \
Nearmap view shows the gear.
- The box must match the view named in asset_view; never reuse one box across \
views. Leave both fields null only if no asset/gear can be located.
- Keep the box compact: typically 3–25% of the image on each side \
unless the tower compound truly fills more. Never return inverted \
coordinates (ymin must be < ymax, xmin < xmax).

TASK 3 - cell_equipment: is cellular telecom equipment visible on the located asset?
- true: clear visible evidence of cellular gear; false: none visible; null: cannot \
assess (resolution or viewing angle insufficient). Prefer null over true when unsure.
TRUE only for cellular/telecom hardware, for example:
- Panel / sector antennas (flat vertical rectangles on frames, often ~3 sectors) \
on the roof, parapet, or building facade / wall (side-mounted)
- Microwave / backhaul dishes, RRUs, cable trays feeding antenna mounts
- Short masts or poles on the parapet carrying antennas
- Rooftop or wall-mounted radio panels / sector frames (not HVAC condensers)
- Telecom equipment cabinets clearly paired with antenna mounts (not alone)
- Monopalm/monopine/canister mast with cabinets or a fenced pad at the base, \
or an antenna cylinder/panel bulge in the faux crown
Default when unsure: cell_equipment=null (not true). Do not mark cell_equipment \
true from Nearmap Vert / top-down alone unless sector panels or dishes are \
unmistakable; prefer an oblique confirmation. Never set cell_equipment=true \
and never draw asset_box_2d around HVAC condensers, chillers, vents, or \
generic mechanical clusters.
FALSE / NOT cellular when they are the ONLY roof objects (HVAC and cell often \
coexist — do not call false just because condensers/vents are also present):
- HVAC condensers, chillers, air handlers, cooling towers alone
- Roof vents, pipes, stacks, drains, skylights, access hatches alone
- Solar panels, satellite TV dishes for building use, water tanks alone
- Random dark rectangles in neat rows across a roof (typical multi-zone HVAC) \
with no parapet sector frames, dishes, or antenna masts elsewhere on the roof
On rooftops, gear is often missed when it sits in **building shadow** or along \
shaded parapets, or when it sits next to HVAC. In oblique views, inspect sunlit \
AND shaded roof edges, corners, and mechanical zones before calling false. \
Top-down-only HVAC clusters must never be called cellular by themselves — but \
HVAC next to panel antennas is still cell_equipment=true.

Also set cell_gear_kind to exactly one of:
sector_panel | facade_mount | microwave | rru | parapet_mast | none | unclear
Use none when cell_equipment is false; unclear only when you cannot tell.

Field meanings: site_evidence and cell_equipment_evidence are one short \
sentence each, citing the specific views and cues used. For cell_equipment true, \
evidence must name the antenna/dish/mount cue, or for stealth the cabinets/\
shroud/crown bulge — not "equipment on roof". \
site_evidence describes the host structure only; it must NOT claim cellular \
gear unless cell_equipment is also true. If cell_equipment is false/null, \
site_evidence must not say "cellular site" / "sector panels" / "antenna mounts".
"""

TOWER_ONLY_CLASSIFICATION_PROMPT = """\
You are analyzing NAIP top-down aerial imagery of one location where a \
ground-based cellular TOWER may exist. Detection mode is TOWER ONLY.

The recorded coordinates can be off by tens of meters, so a tower may appear \
ANYWHERE in the chip, not just at the center.

Definitions:
- TOWER: a ground-based, purpose-built vertical structure carrying antennas - \
monopole, lattice/self-support tower, guyed mast, stealth monopalm/monopine/\
canister, stealth steeple/clock tower, water tower on legs, silo, flagpole, \
or smokestack with telecom gear. Top-down cues: tiny footprint, long thin \
shadow, lattice cross-pattern, guy wires, small fenced compound with \
equipment cabinets, or a palm/pine far taller and straighter than neighbors.
- NOT A TOWER: rooftop cellular hosts, bare fields, unrelated buildings, parking \
lots, or structures with no vertical tower mast. Classify those as "other".

Perform three tasks:

TASK 1 - site_type. Search the ENTIRE chip - edges and corners included:
- "tower": a tower (as defined above) is visible anywhere in the imagery
- "other": no tower visible (including rooftop sites and non-telecom structures)
- "unclear": image quality or ambiguity prevents a confident call
When site_type is "tower", also set tower_subtype to the best match:
monopole | guyed | self_support | stealth | steeple | water_tower | silo | \
flagpole | smokestack | other_tower | unclear
When site_type is not "tower", set tower_subtype to null.
Set site_confidence to at most 0.6 unless two or more independent cues \
corroborate a tower.

TASK 2 - locate the tower. If site_type is tower, report asset_box_2d \
[ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates on the clearest view, \
plus asset_view. Otherwise set both to null.

TASK 3 - cell_equipment: is cellular equipment visible on the located tower?
true | false | null

Field meanings: site_evidence and cell_equipment_evidence are one short \
sentence each, citing the specific cues used.
"""

TOWER_ONLY_SCAN_PROMPT = """\
You are reviewing a NAIP top-down image where a ground-based cellular tower \
may exist, but a first pass could not identify it. Search the ENTIRE image for \
tower cues: tiny footprints with long shadows, lattice cross-patterns, guy \
wires, fenced compounds, monopoles, or stealth tower blocks on a building corner.

Return ONLY JSON with a "candidates" array (up to four entries). Each entry needs:
- box_2d: [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates
- reason: one short phrase citing the visual cue

Ignore rooftop-only hosts. If nothing looks like a tower, return an empty array.
"""

TOWER_ONLY_ZOOM_CLASSIFICATION_PROMPT = """\
You are performing a SECOND-PASS tower review on magnified NAIP zoom crops from \
a site that was not identified in wide imagery. A ground-based cellular tower \
may still be present.

Use the zoom crops as primary evidence. Look for lattice masts, monopoles, guyed \
structures, fenced compounds, stealth tower sections, or long thin tower shadows.

Detection mode is TOWER ONLY:
1. site_type: tower | other | unclear (rooftop hosts are "other")
2. When site_type is tower, tower_subtype: monopole | guyed | self_support | \
stealth | steeple | water_tower | silo | flagpole | smokestack | other_tower | unclear
3. asset_box_2d + asset_view on the clearest view when tower is found
4. cell_equipment: true | false | null

Set site_confidence to at most 0.7 unless zoom crops show an unambiguous tower.
"""

INPUT_CONFIDENCE_PROMPTS = {
    "high": (
        "\n\nSOURCE TRUST: HIGH. This is an outreach-verified / carrier-claimed "
        "site — an active cellular asset is likely present somewhere in the "
        "chip, often tens of meters off the pin and sometimes in a corner or "
        "lot edge. Search EVERY view including edges. Expect rooftop gear, a "
        "ground mast, or stealth (monopalm / monopine / canister) mixed with "
        "real trees. Do not call other just because the pin is pavement or "
        "the center building looks empty. Prefer null over false when "
        "imagery is ambiguous — but never mark HVAC or ordinary roof "
        "mechanicals as cell_equipment true."
    ),
    "medium": (
        "\n\nSOURCE TRUST: MEDIUM. The coordinate likely points to a cellular "
        "site but may be approximate. Weight oblique views when assessing "
        "rooftop equipment in shadow. Do not confuse HVAC with cellular gear."
    ),
    "low": (
        "\n\nSOURCE TRUST: LOW. The coordinate is exploratory; apply normal "
        "evidence standards. Prefer null/false over true for cell_equipment "
        "when cues are ambiguous."
    ),
}

EQUIPMENT_RECHECK_PROMPT = """\
You are re-checking ONLY for visible cellular equipment at a trusted \
outreach-verified site. The first pass called cell_equipment false, but an \
asset is expected here.

Re-examine every view — especially Nearmap obliques, chip edges/corners, \
vacant-lot margins, and tree clusters:
- Thin rectangular panel antennas on parapets, short masts, or building walls
- Sector frames at roof corners (often three sectors) or facade mounts
- Microwave dishes, RRUs, cable trays, telecom cabinets paired with mounts
- A monopalm/monopine (too-tall straight palm/pine with a regular crown, \
cabinets or antenna bulge) or a canister/flagpole shroud — not a natural tree

Do NOT flip to true for HVAC condensers, vents, pipes, skylights, drains, \
rows of identical mechanical units, or ordinary street palms. Prefer null \
over true when ambiguous.

Return the same JSON schema. If any clear cellular equipment or disguised \
mast is visible, set cell_equipment true and explain which view shows it.
"""


ROOFTOP_CELL_CROP_CONFIRM_PROMPT = """\
You are checking a magnified crop PLUS the source view of a rooftop/facade \
region another model boxed as cellular gear.

TRUE when the crop OR the source view shows sector panels, facade mounts, \
microwave dishes, RRUs, or parapet masts. FALSE only when that boxed region is \
clearly HVAC/vents/pipes/solar/skylights with no telecom gear in either view.

If the crop is too tight, blurry, or foliage-obscured, use the source view. \
If still unsure, set cell_equipment null (not false) and cell_gear_kind=unclear. \
Leave asset_box_2d null — the prior box is fixed.
Return the same JSON schema.
"""

ROOFTOP_CELL_LOCALIZE_CONFIRM_PROMPT = """\
Independent localization pass: does this rooftop/facade show CLEAR cellular \
equipment anywhere in the views (not only a prior crop)?

If cell_equipment=true you MUST draw your own tight asset_box_2d on the Nearmap \
oblique where the gear is clearest, and set asset_view to that exact view label. \
Never box HVAC alone. Do not vote false merely because a prior crop was too \
tight — search the full obliques. Prefer null over false when unsure.
Return the same JSON schema.
"""

TOWER_CELL_CROP_CONFIRM_PROMPT = """\
You are checking a magnified crop PLUS the source view of a suspected telecom \
TOWER (not a rooftop HVAC cluster).

Vote cell_equipment true if the crop OR the source view shows a purpose-built \
telecom tower with cellular/microwave gear: sector panels, RRUs, microwave \
dishes, or an antenna platform/array. Lattice/self-support, monopole, guyed, \
stealth (monopine/palm/canister), water tower, silo, flagpole, and smokestack \
hosts WITH antennas are towers — never treat those as HVAC.

If the crop is foliage, a tank rim, the mast base, or otherwise inconclusive \
but the source view shows the tower and its antenna array, vote true from the \
source view. A monopalm/monopine crop that shows the faux crown, cabinets, or \
antenna bulge is still a tower — do not vote false as "just a tree". \
Satellite-only earth-station dishes without cellular arrays → false. \
Leave asset_box_2d null — the prior box is fixed.
Return the same JSON schema.
"""

TOWER_CELL_LOCALIZE_CONFIRM_PROMPT = """\
Independent confirmation of a candidate GROUND-BASED telecom tower (not a \
rooftop HVAC check). Search EVERY view, including edges and corners — \
monopalm/monopine masts are often at a lot edge or in a corner cluster of \
real trees.

A tower may be a monopole, lattice/self-support, guyed mast, stealth \
monopine/palm/canister, water tower, silo, flagpole, or smokestack with \
telecom gear.

1. site_type: "tower" if a purpose-built tower/mast is visible; "rooftop" only \
if there is no tower and a building hosts gear; "other" if neither. A \
too-tall straight palm/pine with a regular synthetic crown is a stealth \
tower, not a natural tree.
2. cell_equipment: true when cellular or microwave gear is visible on that \
tower (sector panels, RRUs, dishes, antenna platforms/arrays, side-arm \
mounts, a canister/antenna bulge in faux fronds, or equipment cabinets / \
fenced pad at a disguised mast). Satellite-only earth-station dishes without \
cellular arrays → false. HVAC, natural trees, bare tanks, and vehicles are \
not cell gear — but do not dismiss a monopalm/monopine as a tree.
3. If cell_equipment=true, draw a tight asset_box_2d on the clearest view \
(prefer the antenna array; the full mast is OK) and set asset_view to that \
view's exact label.

Do NOT classify a visible lattice/monopole/guyed/water/stealth tower as \
rooftop or other. Do NOT vote false merely because individual panels are \
small, hidden in fronds, or the top is slightly soft — platforms, arrays, \
shrouds, and cabinets count. Prefer false only when no tower is present, or \
the structure has no telecom gear.
Return the same JSON schema.
"""


CLAIMED_SITE_TOWER_CONFIRM_NOTE = """

CLAIMED SITE: an outreach-verified record expects an asset here. Search \
edges and corners for a monopalm/monopine/canister mixed with real trees. \
Do not vote cell_equipment false with "no tower exists" unless you have \
checked lot edges and tree clusters. Prefer null over false when a tall \
straight palm/pine or canister mast is visible but panels are hidden in \
the crown.
"""


# Appended when SUPPLEMENTAL_CAN_CONFIRM=1 and street-level photos are in the views.
STREET_BOX_NOTE = """
Street-level photos (labels starting "Street-level photo") were taken from a \
nearby street with the camera facing the site; the label gives the camera \
position. When cellular gear on the site is clearer in a street-level photo \
than in any aerial view, draw asset_box_2d on that photo and set asset_view to \
its exact label. Box only gear on the building or structure the camera faces at \
the site, never gear on other buildings in the background or on utility poles.
"""


def cell_confirm_prompt(site_type: str, *, used_crop: bool) -> str:
    """Prompt for dual-model cell confirm: tower vs rooftop, crop vs localize."""
    site = str(site_type or "").strip().lower()
    if site == "tower":
        return (
            TOWER_CELL_CROP_CONFIRM_PROMPT
            if used_crop
            else TOWER_CELL_LOCALIZE_CONFIRM_PROMPT
        )
    return (
        ROOFTOP_CELL_CROP_CONFIRM_PROMPT
        if used_crop
        else ROOFTOP_CELL_LOCALIZE_CONFIRM_PROMPT
    )

ROOFTOP_BOX_REPAIR_PROMPT = """\
Prior classification suggested a rooftop cellular site but did not return a \
usable asset_box_2d (missing, inverted, too small, or too large).

Your ONLY job: locate cellular gear and draw ONE valid box.

Rules:
- Prefer a Nearmap oblique (North/East/South/West) where sector panels, dishes, \
RRUs, or parapet masts are clearest. Do not box HVAC alone.
- asset_box_2d = [ymin, xmin, ymax, xmax] integers in 0-1000 with ymin < ymax \
and xmin < xmax. Draw TIGHTLY around the cellular hardware (typically 3–25% of \
the image on each side — never the whole roof or whole building).
- asset_view must be the exact label of that same view.
- If cellular gear is clearly visible: site_type=rooftop, cell_equipment=true, \
cell_gear_kind set, and both box fields filled.
- If no cellular gear is visible: cell_equipment=false, cell_gear_kind=none, \
leave asset_box_2d and asset_view null.
Return the same JSON schema.
"""


SCAN_PROMPT = """\
You are reviewing a single top-down aerial image where a cellular tower or \
rooftop site is expected, but a first-pass classifier could not identify it. \
The asset may be anywhere in the frame and is often subtle: a small lattice \
mast, monopole shadow, fenced compound, rooftop antenna cluster, a stealth \
tower disguised as a building with a tall tower section (steeple, clock tower, \
faux-building cell site), or a monopalm/monopine (too-tall straight palm/pine \
in a lot corner or tree cluster). Check the area just below image center \
especially when coordinates are approximate.

Search the ENTIRE image - especially edges and corners - and return up to four \
candidate regions that could plausibly be a tower site or rooftop cellular host. \
Prioritize: tiny footprints with long shadows, lattice cross-patterns, fenced \
pads with equipment cabinets, a palm/pine that is taller and straighter than \
its neighbors, or building roofs with sector-frame mounts.

Return ONLY JSON with a "candidates" array. Each entry needs:
- box_2d: [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates
- reason: one short phrase citing the visual cue

If nothing looks plausible, return an empty candidates array.
"""


ZOOM_CLASSIFICATION_PROMPT = """\
You are performing a SECOND-PASS review on magnified zoom crops from a site \
that was not identified in wide imagery. A cellular tower or rooftop site is \
still expected at this location.

One or more "Zoom crop" views are provided - each is a magnified section of a \
top-down image. Also included may be the original wide "NAIP top-down" or \
"Nearmap top-down" view for context.

Use the zoom crops as primary evidence. A tower site often appears as a \
lattice mast, monopole, guyed structure, small fenced compound with \
equipment, or a monopalm/monopine (straight trunk, regular crown, cabinets \
at the base). A stealth site may also be a building with a tall tower block \
or steeple on one corner and a long shadow from that tower section. A \
rooftop site shows panel antennas, sector frames, or dishes on a building roof.

Perform the same three tasks as the primary classifier:
1. site_type: tower | rooftop | other | unclear
2. When site_type is tower, tower_subtype: monopole | guyed | self_support | \
stealth | steeple | water_tower | silo | flagpole | smokestack | other_tower | unclear
3. asset_box_2d + asset_view on the view where the asset is clearest
4. cell_equipment: true | false | null

Set site_confidence to at most 0.7 unless zoom crops show unambiguous equipment.
"""
