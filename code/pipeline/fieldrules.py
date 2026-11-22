"""Field rules copied verbatim from the mapping builder's deid/mapping.py (sha 68cb7251 line range of
_KEEP_FIELD and TOOL): values of identity fields that stay unchanged (dates, hashes, GUIDs, versions, tools)."""
import re

_KEEP_FIELD = re.compile(r"""^\s*(?:
    \d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?)?
  | \d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?)?
  | \d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[AP]M)?
  | (?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+[a-z]{3,9}\.?\s+\d{1,2},?\s+(?:\d{1,2}:\d{2}(?::\d{2})?\s+)?\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?(?:\s*[+-]\d{4})?
  | \d{8}T?\d{0,6}Z?
  | \{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?
  | (?:0x)?[0-9a-f]{12,}
  | v?\d+(?:\.\d+){1,3}(?:[-_.+]?[a-z0-9]+)?
)\s*$""", re.I | re.X)
TOOL = re.compile(r"(?i)^\s*(?:pcbnew|eeschema|kicad|freecad|solidworks|solid ?edge|inventor|creo|pro/?engineer|catia|nx|unigraphics|"
                  r"onshape|fusion ?360|sketchup|rhino(?:ceros)?|blender|openscad|librecad|qcad|draftsight|autocad|cadquery|"
                  r"build123d|ezdxf|inkscape|illustrator|acrobat(?: distiller)?|ghostscript|open ?cascade(?: step \w+)?|"
                  r"st-developer|cad ?exchanger|microsoft(?: \w+)?|adobe(?: \w+)?|matlab|python|cura|prusaslicer|slic3r|meshlab)"
                  r"(?:[\s_-]*(?:\(?v?[\d.]+[-\w.]*\)?|\d{4}|x64|\(\S+\))){0,3}\s*$")

TOOL = re.compile(r"(?i)^\s*(?:pcbnew|eeschema|kicad|freecad|solidworks|solid ?edge|inventor|creo|pro/?engineer|catia|nx|unigraphics|"
                  r"onshape|fusion ?360|sketchup|rhino(?:ceros)?|blender|openscad|librecad|qcad|draftsight|autocad|cadquery|"
                  r"build123d|ezdxf|inkscape|illustrator|acrobat(?: distiller)?|ghostscript|open ?cascade(?: step \w+)?|"
                  r"st-developer|cad ?exchanger|microsoft(?: \w+)?|adobe(?: \w+)?|matlab|python|cura|prusaslicer|slic3r|meshlab)"
                  r"(?:[\s_-]*(?:\(?v?[\d.]+[-\w.]*\)?|\d{4}|x64|\(\S+\))){0,3}\s*$")
