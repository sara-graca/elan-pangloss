#!/usr/bin/env python3
"""
eaf_to_xml.py — Convert ELAN .eaf files to the Pangloss/Cocoon XML format.

Usage
-----
Inspect tier structure:
    python eaf_to_xml.py input.eaf --inspect

Convert a single file (interactive):
    python eaf_to_xml.py input.eaf output.xml
    python eaf_to_xml.py input.eaf output_dir/                      # -> output_dir/input.xml

Convert a single file with a saved configuration:
    python eaf_to_xml.py input.eaf output.xml --config my.json
    python eaf_to_xml.py input.eaf output_dir/ --config my.json     # -> output_dir/input.xml

Convert a whole directory (interactive):
    python eaf_to_xml.py input_dir/ output_dir/

Convert a whole directory with a saved configuration:
    python eaf_to_xml.py input_dir/ output_dir/ --config my.json
    python eaf_to_xml.py input_dir/ output_dir/ --config configs_folder/


Config reuse for directories
----------------------------
--config may point to a single JSON file OR to a FOLDER of configs.  
When it is a folder, each EAF is matched to a config by TIER STRUCTURE — 
the converter picks the config whose referenced tiers all exist in that file 
(the most specific one when several fit), shows the proposed file->config 
mapping for you to confirm or adjust, and converts.  So one config can serve 
every file built from the same template.  Files with no compatible config 
are configured interactively; configs that match nothing are ignored.

Interactive navigation
-----------------------
At most prompts you can type  <  (or "back") to return to the previous
question.  Press Enter to *skip* an optional field, "y" to accept a suggestion.
The final summary lists any tiers that will NOT be exported, and when there 
are such tiers, you are offered to go back and adjust the mapping before saving.
"""

import sys
import json
import argparse
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _plural(n, singular, plural=None):
    """
    "1 file" / "3 files" — pick the right form for a count.  Irregulars pass the
    plural explicitly (_plural(n, "entry", "entries")).
    """
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def ms_to_sec(ms):
    return f"{(ms or 0) / 1000:.3f}"

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _esc_attr(text):
    return _esc(text).replace('"', "&quot;")


@lru_cache(maxsize=32)
def _parse_root(path):
    """Parse an EAF once per run; parse_eaf and media_basenames share the tree."""
    return ET.parse(path).getroot()


# ─── Parse EAF ────────────────────────────────────────────────────────────────

def parse_eaf(path):
    """
    Returns (time_slots, annotations, tier_map, linguistic_types).

    annotations : dict  ann_id → {id, value, ref, previous, ts1, ts2, tier_id}
    tier_map    : dict  tier_id → tier Element
    linguistic_types : dict  lt_id → {CONSTRAINTS}
    """
    root = _parse_root(str(path))

    time_slots = {}
    for ts in root.findall(".//TIME_SLOT"):
        time_slots[ts.get("TIME_SLOT_ID")] = int(ts.get("TIME_VALUE", 0))

    annotations = {}
    tier_map = {}

    for tier in root.findall("TIER"):
        tid = tier.get("TIER_ID")
        tier_map[tid] = tier
        for ann in tier.findall("ANNOTATION"):
            aa = ann.find("ALIGNABLE_ANNOTATION")
            ra = ann.find("REF_ANNOTATION")
            if aa is not None:
                aid = aa.get("ANNOTATION_ID")
                annotations[aid] = {
                    "id":       aid,
                    "value":    (aa.findtext("ANNOTATION_VALUE") or "").strip(),
                    "ref":      None,
                    "previous": None,
                    "ts1":      time_slots.get(aa.get("TIME_SLOT_REF1"), 0),
                    "ts2":      time_slots.get(aa.get("TIME_SLOT_REF2"), 0),
                    "tier_id":  tid,
                }
            elif ra is not None:
                aid = ra.get("ANNOTATION_ID")
                annotations[aid] = {
                    "id":       aid,
                    "value":    (ra.findtext("ANNOTATION_VALUE") or "").strip(),
                    "ref":      ra.get("ANNOTATION_REF"),
                    "previous": ra.get("PREVIOUS_ANNOTATION"),
                    "ts1":      None,
                    "ts2":      None,
                    "tier_id":  tid,
                }

    linguistic_types = {}
    for lt in root.findall("LINGUISTIC_TYPE"):
        ltid = lt.get("LINGUISTIC_TYPE_ID")
        linguistic_types[ltid] = {"CONSTRAINTS": lt.get("CONSTRAINTS")}

    return time_slots, annotations, tier_map, linguistic_types


# ─── Children index ───────────────────────────────────────────────────────────

def build_children(annotations, tier_map, linguistic_types):
    """
    Build a children index:  parent_ann_id → [child_ann_ids, ordered].
    Handles Symbolic (ref-based) children and both time-based stereotypes
    (Time_Subdivision and Included_In), which are matched by containment.
    """
    children = {}

    by_ref = defaultdict(list)
    for aid, ann in annotations.items():
        if ann["ref"]:
            by_ref[ann["ref"]].append(aid)
    for parent_id, child_ids in by_ref.items():
        children[parent_id] = _sort_by_previous(child_ids, annotations)

    time_sub_tiers = {}
    for tid, tier in tier_map.items():
        parent_tier = tier.get("PARENT_REF")
        if not parent_tier:
            continue
        ltype = tier.get("LINGUISTIC_TYPE_REF", "")
        if linguistic_types.get(ltype, {}).get("CONSTRAINTS") in (
                "Time_Subdivision", "Included_In"):
            time_sub_tiers[tid] = parent_tier

    if time_sub_tiers:
        time_anns_by_tier = defaultdict(list)
        for aid, ann in annotations.items():
            if ann["ts1"] is not None:
                time_anns_by_tier[ann["tier_id"]].append(aid)
        for tid_key in time_anns_by_tier:
            time_anns_by_tier[tid_key].sort(key=lambda a: annotations[a]["ts1"])

        # Only the parents that actually receive time-subdivision children need
        # their child list re-sorted; everything else keeps its original order.
        touched_parents = set()
        for child_tier, parent_tier in time_sub_tiers.items():
            parent_list = time_anns_by_tier.get(parent_tier, [])
            child_list  = time_anns_by_tier.get(child_tier, [])
            for caid in child_list:
                c1 = annotations[caid]["ts1"]
                c2 = annotations[caid]["ts2"]
                for paid in parent_list:
                    p1 = annotations[paid]["ts1"]
                    p2 = annotations[paid]["ts2"]
                    if p1 <= c1 and c2 <= p2:
                        children.setdefault(paid, []).append(caid)
                        touched_parents.add(paid)
                        break

        for parent_id in touched_parents:
            child_ids = children[parent_id]
            timed = sorted(
                [cid for cid in child_ids if annotations[cid]["ts1"] is not None],
                key=lambda cid: annotations[cid]["ts1"]
            )
            refbased = [cid for cid in child_ids if annotations[cid]["ts1"] is None]
            children[parent_id] = timed + refbased

    return children


def _sort_by_previous(ids, annotations):
    if not ids:
        return ids
    id_set = set(ids)
    first = next(
        (i for i in ids
         if not annotations[i].get("previous")
         or annotations[i]["previous"] not in id_set),
        ids[0]
    )
    prev_map = {
        annotations[i]["previous"]: i
        for i in ids if annotations[i].get("previous")
    }
    ordered = [first]
    current = first
    while current in prev_map:
        current = prev_map[current]
        ordered.append(current)
    seen = set(ordered)
    ordered += [i for i in ids if i not in seen]
    return ordered


# ─── Tier relationships ───────────────────────────────────────────────────────

def tier_parent(tid, tier_map):
    t = tier_map.get(tid)
    return t.get("PARENT_REF") if t is not None else None


# ─── Descendant collection ────────────────────────────────────────────────────

def collect_descendants(ann_id, target_tier_id, children, annotations):
    """
    BFS from ann_id; collect descendants whose tier_id == target_tier_id.
    """
    result = []
    queue = deque(children.get(ann_id, []))
    while queue:
        cid = queue.popleft()
        if annotations[cid]["tier_id"] == target_tier_id:
            result.append(cid)
        else:
            queue.extend(children.get(cid, []))
    if result and all(annotations[aid]["ts1"] is not None for aid in result):
        result.sort(key=lambda aid: annotations[aid]["ts1"])
    return result


# ─── Speaker identification ───────────────────────────────────────────────────

_SPEAKER_SUFFIX_RE = re.compile(r"@\s*(\S.*?)?\s*$")


def _speaker_key(tid, tier_map):
    """
    Explicit speaker marker for a tier, or '' if none:
      1. PARTICIPANT attribute
      2. everything after @   (e.g. ref@SP2 -> "SP2", tx@marie -> "marie")
    """
    t = tier_map.get(tid)
    if t is None:
        return ""
    part = t.get("PARTICIPANT")
    if part:
        return part.strip()
    m = _SPEAKER_SUFFIX_RE.search(tid or "")
    # A bare trailing '@' (e.g. 'id@') carries no speaker: the group is empty.
    return (m.group(1) or "") if m else ""


def _base_tier(tid):
    """
    The tier name with any trailing '@<speaker>' discriminator removed, e.g.
    'tx@SP1' -> 'tx', 'ref@A' -> 'ref', 'tx@SP1-cp' -> 'tx', 'word' -> 'word'.
    """
    return _SPEAKER_SUFFIX_RE.sub("", tid or "")


def _file_speaker_codes(tier_map):
    """
    Distinct trailing '@<code>' speaker discriminators actually used on tier
    NAMES in this file (not PARTICIPANT), in first-seen order.  A file with no
    '@' suffix on any tier returns [] (single, unmarked speaker).
    """
    seen = []
    for tid in tier_map:
        m = _SPEAKER_SUFFIX_RE.search(tid or "")
        # A bare trailing '@' yields no code; it must not become a "speaker".
        if m and m.group(1) and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


# ─── Media descriptors ────────────────────────────────────────────────────────

def media_basenames(path):
    """
    Return the bare filenames of the AUDIO media linked in an EAF, in document
    order (path and file:// prefix stripped).  Used to fill <SOUNDFILE href=...>.
    Prefers RELATIVE_MEDIA_URL, falls back to MEDIA_URL.
    """
    try:
        root = _parse_root(str(path))
    except ET.ParseError:
        return []
    audio_ext = ("wav", "mp3", "flac", "ogg", "aif", "aiff", "m4a")
    names = []
    for md in root.findall(".//MEDIA_DESCRIPTOR"):
        url = md.get("RELATIVE_MEDIA_URL") or md.get("MEDIA_URL") or ""
        if not url:
            continue
        mime = (md.get("MIME_TYPE") or "").lower()
        ext = url.lower().rsplit(".", 1)[-1]
        if not (mime.startswith("audio") or ext in audio_ext):
            continue  # skip video / other media
        name = unquote(url).replace("\\", "/").rstrip("/").split("/")[-1]
        if name:
            names.append(name)
    return names


# ─── Tier inspection ──────────────────────────────────────────────────────────

# How many speaker codes to list in the tier tree's 'who' column before
# summarising the rest as '+N more'.
_WHO_MAX = 4

# Same idea for the 'tiers:' line listing the real tier names behind a merged
# node: a corpus-wide tier can stand for dozens of them.
_TIERS_MAX = 6


def _norm_tier_name(name):
    """
    A tier name with incidental whitespace around its '@' removed, so that
    'tx@ mohi' and 'tx@mohi' compare equal.  Used only for display decisions —
    the real names are never rewritten.
    """
    m = _SPEAKER_SUFFIX_RE.search(name or "")
    if not m:
        return name or ""
    # 'id@' normalises to 'id'
    return f"{_base_tier(name)}@{m.group(1)}" if m.group(1) else _base_tier(name)


def print_tier_tree(tier_map, annotations):
    ann_count = defaultdict(int)
    for ann in annotations.values():
        ann_count[ann["tier_id"]] += 1

    child_tiers = defaultdict(list)
    roots = []
    for tid, tier in tier_map.items():
        parent = tier.get("PARENT_REF")
        if parent:
            child_tiers[parent].append(tid)
        else:
            roots.append(tid)

    def cells(tid, prefix):
        """The rendered pieces of one node's line, plus any 'tiers:' extra."""
        tier  = tier_map[tid]
        ltype = tier.get("LINGUISTIC_TYPE_REF", "")
        lang  = tier.get("LANG_REF") or tier.get("DEFAULT_LOCALE") or ""
        who   = _speaker_key(tid, tier_map)
        count = ann_count.get(tid, 0)

        # In a MERGED (folder) view a node stands for several real tiers and the
        # files may disagree about its type or language; show every distinct
        # value rather than one file's as if it were everyone's.
        types    = tier.get("_types")
        langs    = tier.get("_langs")
        spk      = tier.get("_speakers")
        variants = tier.get("_variants")
        label    = tier.get("_display") or tid
        if types is not None and len(types) > 1:
            ltype = "|".join(types)
        if langs is not None and len(langs) > 1:
            lang = "|".join(langs)
        if spk:
            if len(spk) > _WHO_MAX:
                shown = ",".join(spk[:_WHO_MAX])
                who = f"{shown},+{len(spk) - _WHO_MAX} more"
            else:
                who = ",".join(spk)

        # The real tier names are usually just '<base>@<who>' — that is already
        # readable from the name and the who column, so listing them would be
        # noise.  Print them when they DON'T follow from it 
        extra = None
        if variants:
            base     = tier.get("_base") or tid
            norm      = {_norm_tier_name(v) for v in variants}
            expected  = {f"{base}@{w}" if w else base for w in (spk or [])}
            expected.add(base)
            if not norm <= expected:
                # Deviating names come FIRST
                odd     = [v for v in variants if _norm_tier_name(v) not in expected]
                regular = [v for v in variants if _norm_tier_name(v) in expected]
                ordered = odd + regular
                if len(ordered) > _TIERS_MAX:
                    body = (", ".join(ordered[:_TIERS_MAX])
                            + f", +{len(ordered) - _TIERS_MAX} more")
                else:
                    body = ", ".join(ordered)
                extra = f"{prefix}     tiers: " + body
        return {
            "name":  f"{prefix}+- {label!r}",
            "type":  f"type={ltype!r}",
            "lang":  f"lang={lang!r}",
            "who":   f"who={who!r}",
            "count": f"({count} ann)",
            "extra": extra,
        }

    rows = []

    def collect(tid, prefix=""):
        rows.append(cells(tid, prefix))
        for kid in child_tiers.get(tid, []):
            collect(kid, prefix + "   ")

    for root_tid in roots:
        collect(root_tid)

    if not rows:
        print()
        print("Tier tree")
        print("-" * 80)
        print()
        return

    w_name = max(len(r["name"]) for r in rows)
    w_type = max(len(r["type"]) for r in rows)
    w_lang = max(len(r["lang"]) for r in rows)
    w_who  = max(len(r["who"])  for r in rows)

    print()
    print("Tier tree")
    print("-" * 80)
    for r in rows:
        print(f"{r['name']:<{w_name}}  {r['type']:<{w_type}}  "
              f"{r['lang']:<{w_lang}}  {r['who']:<{w_who}}  {r['count']}")
        if r["extra"]:
            print(r["extra"])
    print()


# ─── Segment-tier auto-detection ──────────────────────────────────────────────

def detect_segment_tiers(tier_map, annotations):
    """
    Auto-detect the segment (time-aligned, parentless) tier(s).

    Strategy:
      1. Candidates: time-aligned root tiers with at least one annotation AND
         at least one child tier.
      2. Group by speaker (PARTICIPANT, then @SPx suffix).
      3. Within each speaker group, keep only the candidate with the most
         child tiers — that is almost always the main segment tier.

    Assumption: at most ONE segment tier per speaker (the richest is kept).
    """
    child_tier_count = defaultdict(int)
    for tier in tier_map.values():
        p = tier.get("PARENT_REF")
        if p:
            child_tier_count[p] += 1

    ann_count  = defaultdict(int)
    has_time   = defaultdict(bool)
    for ann in annotations.values():
        ann_count[ann["tier_id"]] += 1
        if ann["ts1"] is not None:
            has_time[ann["tier_id"]] = True

    candidates = [
        tid for tid, tier in tier_map.items()
        if not tier.get("PARENT_REF")
        and has_time.get(tid)
        and ann_count.get(tid, 0) > 0
        and child_tier_count.get(tid, 0) > 0
    ]

    by_spk = defaultdict(list)
    for tid in candidates:
        by_spk[_speaker_key(tid, tier_map)].append(tid)

    result = []
    for tids in by_spk.values():
        best = max(tids, key=lambda t: child_tier_count[t])
        result.append(best)

    return result


# ─── Interactive helpers ──────────────────────────────────────────────────────

class _GoBack(Exception):
    """Raised by input helpers (when allow_back=True) to step back one question."""

_BACK_TOKENS = {"<", "back", "b"}
_NOASK = object()   # returned by a step that chose not to ask (condition unmet)


def _ask(prompt, default="", allow_back=False):
    suffix = f"\n  (press Enter to use \"{default}\")" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    if allow_back and val in _BACK_TOKENS:
        raise _GoBack
    return val if val else default


def _yesno(prompt, default=True, allow_back=False):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    if allow_back and raw in _BACK_TOKENS:
        raise _GoBack
    if not raw:
        return default
    return raw in ("y", "yes")


def _tier_label(tid):
    """
    What to SHOW for a tier key.  Merged folder views may need two nodes for one
    base name (a real 'tx' under 'ref' and a stray root-level copy); the extra
    one is keyed 'tx (root)' / 'tx (under X)' so it stays distinguishable in the
    tree, in menus and in the summary.  Names are already display-ready.
    """
    return tid or ""


def _pick_one(prompt, tier_ids, required=False, default=None, allow_back=False):
    while True:
        print(f"\n{prompt}")
        for i, tid in enumerate(tier_ids, 1):
            mark = "  <- suggested" if tid == default else ""
            print(f"  {i:3d}. {_tier_label(tid)}{mark}")

        parts = []
        if required and default:
            parts.append(f'Type \'y\' for "{_tier_label(default)}"')
            parts.append("Select a number/name")
        elif required:
            parts.append("Select a number/name")
        elif default:
            # Optional field with a suggestion
            parts.append(f"Type 'y' for \"{_tier_label(default)}\"")
            parts.append("Select a number/name")
            parts.append("Press Enter to skip")
        else:
            parts.append("Select a number/name")
            parts.append("Press Enter to skip")
        hint = "  (" + " | ".join(parts) + ")"

        raw = input(f"Your choice{hint}: ").strip()

        if allow_back and raw in _BACK_TOKENS:
            raise _GoBack

        if not raw:
            if required:
                print("  This field is required — type 'y' to accept the "
                      "suggestion, or pick a number/name.")
                continue
            return None  # optional → Enter skips, even when a suggestion exists

        low = raw.lower()
        if default and low in ("y", "yes"):
            return default

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(tier_ids):
                return tier_ids[idx]
            print(f"  Number {raw} is out of range (1–{len(tier_ids)}).")
            continue
        if raw in tier_ids:
            return raw
        print(f"  '{raw}' does not match any tier name — try again "
              f"(or Enter to skip).")


def _pick_many(prompt, tier_ids, allow_back=False, defaults=(), required=False):
    print(f"\n{prompt}")
    defaults = list(defaults)
    for i, tid in enumerate(tier_ids, 1):
        mark = "  <- suggested" if tid in defaults else ""
        print(f"  {i:3d}. {_tier_label(tid)}{mark}")
    if defaults:
        shown = ", ".join(f'"{_tier_label(d)}"' for d in defaults)
        tail = "" if required else " | Enter to skip"
        hint = (f"Your choices (Type 'y' for {shown} | "
                f"comma-separated numbers/names{tail}): ")
    elif required:
        hint = "Your choices (comma-separated numbers/names): "
    else:
        hint = ("Your choices (comma-separated numbers/names "
                "or Enter to skip): ")
    while True:
        raw = input(hint).strip()
        if allow_back and raw in _BACK_TOKENS:
            raise _GoBack
        if not raw:
            if required:
                print("  This field is required — type 'y' to accept the "
                      "suggestion, or pick number(s)/name(s).")
                continue
            return []
        if defaults and raw.lower() in ("y", "yes"):
            return list(defaults)
        result = []
        for token in raw.split(","):
            token = token.strip()
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(tier_ids):
                    result.append(tier_ids[idx])
                else:
                    print(f"  Number {token} is out of range, skipping.")
            elif token in tier_ids:
                result.append(token)
            else:
                print(f"  '{token}' does not match any tier name, skipping.")
        if result:
            return result
        if required:
            print("  Nothing valid selected — please pick at least one.")
            continue
        return result

def _ask_lang_required(ttid, auto):
    """Translation language is mandatory; Enter accepts the auto-detected code."""
    hint = f"(Enter = \"{auto}\")" if auto else "(e.g. en, fr)"
    while True:
        raw = input(f"  Language code for the translation '{ttid}' {hint}: ").strip()
        if raw:
            return raw
        if auto:
            return auto
        print("  A language code is required for translations — please type one.")


def _tier_lang(tid, tier_map):
    t = tier_map.get(tid)
    if t is None:
        return ""
    return t.get("LANG_REF") or t.get("DEFAULT_LOCALE") or ""

def _guess_lang(tid, tier_map):
    """
    Tier LANG_REF/DEFAULT_LOCALE first, else a code that appears as a separate
    token in the tier name (e.g. 'A_phrase-gls-en' -> 'en').  Token-boundary
    matching, so 'sentence' does not guess 'en' nor 'friction' 'fr'.
    """
    lang = _tier_lang(tid, tier_map)
    if lang:
        return lang
    m = re.search(r"(?:^|[-_@.])(en|fr|es|de|ru|zh)(?:$|[-_@.])", (tid or "").lower())
    return m.group(1) if m else ""

def _run_flow(build_steps, state, history=None, resume=False, escape_back=False):
    """
    Drive a dynamically-built list of (label, fn) steps with back-navigation
    that spans the whole flow.

    `build_steps(state)` returns the current step list; it is re-evaluated each
    iteration so that steps which only become known mid-flow (e.g. one block per
    speaker, once the segment tiers are chosen) appear automatically.  Earlier
    steps keep stable positions, so the recorded history stays valid.

    Each fn(state) mutates `state` and returns:
      - _NOASK  → the step chose not to ask (condition unmet); not recorded, so
                  "go back" skips over it.
      - anything else → the step asked the user; it is recorded.
    A step may raise _GoBack to jump to the previous *recorded* step.  Steps read
    state["_at_start"] to know whether a "go back" option is meaningful.

    Passing the same `history` list back with resume=True re-enters the flow at
    the last asked question (with all answers kept), so the whole interview can
    be revisited after the summary.

    With escape_back=True, a "go back" at the very first question is re-raised
    instead of refused, so a caller that has somewhere earlier to go — the folder
    interview, where the previous structure is the natural target — can act on
    it.  Without it, the flow says there is nothing to go back to.
    """
    if history is None:
        history = []
    i = history.pop() if (resume and history) else 0
    while True:
        steps = build_steps(state)
        if i >= len(steps):
            break
        # At the very first question there is normally nothing to go back to —
        # unless the caller can act on it (escape_back), in which case '<' is a
        # meaningful option there and the steps must offer it.
        state["_at_start"] = (len(history) == 0) and not escape_back
        _label, fn = steps[i]
        try:
            result = fn(state)
        except _GoBack:
            if history:
                i = history.pop()
                print("\n" + "↩ " * 20)
                print("  Going back to the previous question — ignore the "
                      "options listed above.")
                print("↩ " * 20)
            elif escape_back:
                state.pop("_at_start", None)
                raise
            else:
                print("  (already at the first question — nothing to go back to)")
            continue
        if result is not _NOASK:
            history.append(i)
        i += 1
    state.pop("_at_start", None)
    return state


# ─── Role detection: LINGUISTIC_TYPE + name "contains" + structure ─────────────
#
# Tier *names* vary between annotators. So each role is matched as a substring against
# "<tier name> <linguistic type>" AND constrained structurally (descendant of a
# given tier, or direct child of it).  These are only *suggestions*; the user
# always confirms or overrides.

_POS = {
    "tx":   ("tx", "txt", "transcr", "phono", "syllab", "ortho"),
    "word": ("word", "mot", "wrd"),
    "mb":   ("morph", "mb", "mor"),
    "gls":  ("gls", "gloss", "glose", "ge", "meaning"),
    "pos":  ("pos", "msa", "gram", "cat", "tag", "rx", "ps"),
}
_NEG = {
    "tx":   ("word", "mot", "wrd", "morph", "mb", "gls", "gloss", "glose",
             "pos", "msa", "trad", "ft", "lit", "note", "not", "par", "segnum"),
    "word": ("morph", "mb", "gls", "gloss", "glose", "pos", "ps", "rx", "msa",
             "par", "cf", "hn", "trad", "wps"),
    "mb":   ("gls", "gloss", "glose", "pos", "rx", "msa", "par", "cf", "hn",
             "type", "variant", "wps", "cat", "append", "num", "segnum"),
    "gls":  ("pos", "rx", "msa", "cat", "gram", "par", "cf", "hn", "type",
             "append", "variant", "wps"),
    "pos":  ("gls", "gloss", "glose", "meaning", "cf", "hn", "append",
             "morph-txt", "word", "mot"),
}


def _haystack(tid, tier_map):
    t = tier_map.get(tid)
    typ = (t.get("LINGUISTIC_TYPE_REF") or "") if t is not None else ""
    return (str(tid) + " " + typ).lower()


def _depth_under(tid, under, tier_map):
    """Steps from `tid` up to `under` along PARENT_REF, or None if unrelated."""
    cur, d, seen = tid, 0, set()
    while cur and cur != under and cur not in seen:
        seen.add(cur)
        cur = tier_parent(cur, tier_map)
        d += 1
    return d if cur == under else None


def _best_tier(tier_ids, tier_map, ann_count, role,
               under=None, include_under=False, child_of=None):
    """
    Best-matching tier for `role` (a key of _POS), or None.

    A candidate must contain a positive keyword and no negative keyword in its
    "name + type" haystack, and satisfy the structural constraint:
      - child_of=T      → direct child of T
      - under=T         → descendant of T (and T itself if include_under)
    Ties are broken by shallower depth, then by more annotations.
    """
    pos = _POS[role]
    neg = _NEG.get(role, ())
    best, best_key = None, None
    for tid in tier_ids:
        hay = _haystack(tid, tier_map)
        if not any(k in hay for k in pos):
            continue
        if any(k in hay for k in neg):
            continue
        if child_of is not None:
            if tier_parent(tid, tier_map) != child_of:
                continue
            depth = 1
        elif under is not None:
            if tid == under:
                if not include_under:
                    continue
                depth = 0
            else:
                d = _depth_under(tid, under, tier_map)
                if d is None:
                    continue
                depth = d
        else:
            depth = 0
        key = (depth, -ann_count.get(tid, 0))
        if best_key is None or key < best_key:
            best, best_key = tid, key
    return best


def _make_speaker_steps(idx, tier_ids, tier_map, tier_set, multi, ann_count, is_wordlist=False):
    """
    Return a list of (label, fn) steps that configure speaker number `idx`.

    Every fn receives the *global* flow state and edits state["speakers"][idx].
    Suggestions are computed structurally (LINGUISTIC_TYPE + name + parent/child
    position) via _best_tier, so they survive inconsistent naming.  For idx > 0
    a "mirror" step can copy the previous speaker's mapping when the segment
    tiers differ only by speaker code.  Every prompt accepts '<' to go back.
    """

    def _spk(state):
        return state["speakers"][idx]

    def _skip_if_prefilled(fn):
        """Skip a question when this speaker's mapping is already filled in
        (accepted mirror or accepted fast-path proposal)."""
        def wrapper(state):
            if _spk(state).get("_prefilled"):
                return _NOASK
            return fn(state)
        return wrapper

    def step_header(state):
        # Pure display; never recorded so it never traps "go back".
        if multi:
            s = _spk(state)
            print(f"\n{'─'*60}")
            print(f"  Speaker: {s['who']}   (segment tier: {s['segment_tier']})")
            print(f"{'─'*60}")
        return _NOASK

    def step_mirror(state):
        if idx == 0:
            return _NOASK
        s    = _spk(state)
        prev = state["speakers"][idx - 1]
        transform = _derive_transform(prev["segment_tier"], s["segment_tier"])
        result    = _mirror_speaker(prev, s["segment_tier"], s["who"],
                                    tier_set, transform)
        if not result:
            return _NOASK
        mirrored, dropped = result
        print(f"\n  '{s['segment_tier']}' looks like '{prev['segment_tier']}' with "
              f"only the speaker code changed.")
        print(f"  Proposed mapping for speaker {s['who']} (mirrors {prev['who']}):\n")
        _print_speaker_mapping(mirrored, indent="      ")
        if dropped:
            print(f"\n  WARNING: {_plural(len(dropped), 'tier')} from {prev['who']} "
                  f"{'has' if len(dropped) == 1 else 'have'} no "
                  f"equivalent for {s['who']} and were left out:")
            for label, missing in dropped:
                print(f"      - {label}: '{missing}' (not in this file)")
            print("  Everything else is mirrored; you can add the missing pieces "
                  "by hand if you decline below.")
        if _yesno("  Re-use this mapping?", True, allow_back=True):
            mirrored["_prefilled"] = True
            state["speakers"][idx] = mirrored
        else:
            state["speakers"][idx] = {"who": s["who"],
                                      "segment_tier": s["segment_tier"]}

    @_skip_if_prefilled
    def step_forms(state):
        s = _spk(state)
        seg = s["segment_tier"]
        tx_default = _best_tier(tier_ids, tier_map, ann_count, "tx",
                                under=seg, include_under=True)
        # Select all transcription tiers at once (as with translations); the
        # first one chosen is the primary form.  At least one is required.
        chosen = _pick_many(
            "Transcription tier(s)  [XML: <FORM>]",
            tier_ids, allow_back=True,
            defaults=[tx_default] if tx_default else (),
            required=True,
        )
        # Each form may carry a kindOf label.  A label names the tier on the way
        # back (xml_to_eaf), and two unlabelled forms would be indistinguishable
        # there, so at most one form may be left unlabelled — the primary.
        while True:
            forms = []
            for ttid in chosen:
                kind = _ask(f"  Transcription type for '{_tier_label(ttid)}' "
                            "(e.g. phono, ortho, or Enter for none)  "
                            "[XML: <FORM kindOf='...'>]", "", allow_back=True)
                forms.append({"tier": ttid, "kind": kind or None})
            unlabelled = [f for f in forms if not f["kind"]]
            if len(unlabelled) <= 1:
                break
            print(f"  Only one transcription may be left unlabelled "
                  f"(you left {len(unlabelled)}).  A plain <FORM> cannot be "
                  f"told from another on the way back to ELAN — please give a "
                  f"label to all but one.")
        s["forms"] = forms

    @_skip_if_prefilled
    def step_transl(state):
        s = _spk(state)
        seg = s["segment_tier"]
        # Auto-detect sentence-translation tiers: names/types that look like a
        # free translation (ft, trad, translation, phrase-gls) sitting under this
        # speaker's segment tier — excluding word/morpheme glosses and notes.
        _T_POS = ("ft", "trad", "translation", "transl", "phrase-gls",
                  "phrase-gloss")
        _T_NEG = ("morph", "word-gls", "word-gloss", "mb", "-lit", "note",
                  "segnum")
        suggested = [
            tid for tid in tier_ids
            if any(k in _haystack(tid, tier_map) for k in _T_POS)
            and not any(k in _haystack(tid, tier_map) for k in _T_NEG)
            and _depth_under(tid, seg, tier_map) is not None
        ]
        s["transl"] = _pick_many(
            "OPTIONAL — Translation tier(s)  [XML: <TRANSL xml:lang='...'>]",
            tier_ids, allow_back=True, defaults=suggested
        )
        langs = {}
        for ttid in s["transl"]:
            langs[ttid] = _ask_lang_required(ttid, _guess_lang(ttid, tier_map))
        s["transl_langs"] = langs

    @_skip_if_prefilled
    def step_notes(state):
        s = _spk(state)
        seg = s["segment_tier"]
        _N_POS = ("note", "comm", "remark", "observ")
        _N_NEG = ("notation", "ft", "trad", "transl", "morph", "mb", "gls",
                  "gloss", "segnum")
        suggested = [
            tid for tid in tier_ids
            if any(k in _haystack(tid, tier_map) for k in _N_POS)
            and not any(k in _haystack(tid, tier_map) for k in _N_NEG)
            and _depth_under(tid, seg, tier_map) is not None
        ]
        s["notes"] = _pick_many(
            "OPTIONAL — Notes/comments tier(s)  [XML: <NOTE message='...'>]",
            tier_ids, allow_back=True, defaults=suggested
        )

    @_skip_if_prefilled
    def step_word(state):
        s = _spk(state)
        s["word_form"] = _pick_one(
            "OPTIONAL — Word tier  [XML: <W><FORM>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "word",
                               under=s["segment_tier"]),
            allow_back=True
        )
        if not s["word_form"]:
            s["word_gls"] = None

    @_skip_if_prefilled
    def step_word_gls(state):
        s = _spk(state)
        if not s.get("word_form"):
            s.setdefault("word_gls", None)
            return _NOASK
        s["word_gls"] = _pick_one(
            "OPTIONAL — Word-level gloss tier  [XML: <W><TRANSL>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "gls",
                               child_of=s["word_form"]),
            allow_back=True
        )

    @_skip_if_prefilled
    def step_morph(state):
        s = _spk(state)
        # morphemes sit under the word tier when there is one, else under the
        # segment tier.
        under = s.get("word_form") or s["segment_tier"]
        s["morph_form"] = _pick_one(
            "OPTIONAL — Morpheme tier  [XML: <M><FORM>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "mb", under=under),
            allow_back=True
        )
        if not s["morph_form"]:
            s["morph_gls"] = None
            s["morph_gls_lang"] = ""
            s["morph_pos"] = None
            s["morph_pos_sep"] = ""

    @_skip_if_prefilled
    def step_morph_gls(state):
        s = _spk(state)
        if not s.get("morph_form"):
            return _NOASK
        s["morph_gls"] = _pick_one(
            "OPTIONAL — Morpheme gloss tier  [XML: <M><TRANSL>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "gls",
                               under=s["morph_form"]),
            allow_back=True
        )

    @_skip_if_prefilled
    def step_morph_gls_lang(state):
        s = _spk(state)
        if not s.get("morph_form") or not s.get("morph_gls"):
            s.setdefault("morph_gls_lang", "")
            return _NOASK
        s["morph_gls_lang"] = _ask(
            "  Language code for the morpheme gloss (Enter for none)", "",
            allow_back=True
        )

    @_skip_if_prefilled
    def step_morph_pos(state):
        s = _spk(state)
        if not s.get("morph_form"):
            return _NOASK
        s["morph_pos"] = _pick_one(
            "OPTIONAL — Part-of-speech tier (will be appended to each morpheme gloss)",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "pos",
                               under=s["morph_form"]),
            allow_back=True
        )
        if not s["morph_pos"]:
            s["morph_pos_sep"] = ""

    @_skip_if_prefilled
    def step_morph_pos_sep(state):
        s = _spk(state)
        if not s.get("morph_form") or not s.get("morph_pos"):
            s.setdefault("morph_pos_sep", "")
            return _NOASK
        s["morph_pos_sep"] = _ask(
            "  Separator between gloss and PoS (e.g.  :  or  _)", ":",
            allow_back=True
        )

    p = f"s{idx}_"
    steps = [
        (p + "header", step_header), (p + "mirror", step_mirror),
        (p + "forms", step_forms), (p + "transl", step_transl),
        (p + "notes", step_notes),
    ]
    if not is_wordlist:
        # A wordlist entry IS the word — no separate word tier, no nested <W>.
        steps += [(p + "word_form", step_word), (p + "word_gls", step_word_gls)]
    steps += [
        (p + "morph_form", step_morph), (p + "morph_gls", step_morph_gls),
        (p + "morph_gls_lang", step_morph_gls_lang),
        (p + "morph_pos", step_morph_pos), (p + "morph_pos_sep", step_morph_pos_sep),
    ]
    return steps

# ─── Mirror a speaker (auto-fill SP2 from SP1) ─────────────────────────────────

def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _derive_transform(a, b):
    """
    Given two segment-tier names that differ only by a speaker discriminator
    (e.g. 'A_..' vs 'B_..', or '..@SP1' vs '..@SP2'), return a function that
    rewrites a speaker-A tier name into the speaker-B equivalent.  Returns None
    when the difference is too ambiguous to mirror safely.
    """
    if not a or not b or a == b:
        return None

    ma, mb = _SPEAKER_SUFFIX_RE.search(a), _SPEAKER_SUFFIX_RE.search(b)
    code_a = (ma.group(1) or "") if ma else ""
    code_b = (mb.group(1) or "") if mb else ""
    if code_a and code_b and _base_tier(a) == _base_tier(b):
        # Same base tier, different speaker code: swap the code, leaving every
        # other part of the name untouched.
        def f_code(name):
            m = _SPEAKER_SUFFIX_RE.search(name or "")
            if m and (m.group(1) or "") == code_a:
                return f"{_base_tier(name)}@{code_b}"
            return name   # shared tiers (no code, or another speaker's) as-is
        return f_code

    p = _common_prefix_len(a, b)
    s = _common_prefix_len(a[::-1], b[::-1])
    s = min(s, len(a) - p, len(b) - p)
    mid_a = a[p:len(a) - s]
    mid_b = b[p:len(b) - s]
    if not mid_a or not mid_b:
        return None  # pure insertion/deletion — don't guess

    def f(name):
        if name and mid_a in name:
            return name.replace(mid_a, mid_b, 1)
        return name  # shared tiers (no discriminator) map to themselves

    return f


def _mirror_speaker(prev, seg2, who2, tier_set, transform):
    """
    Build a speaker config for `seg2` by applying `transform` to every tier
    reference in `prev` — best effort.  Tiers whose mirrored name is absent from
    the file are DROPPED (not fatal) and collected so the caller can warn.

    Returns (config, dropped) where `dropped` is a list of (label, missing_tier),
    or None only when the mirror is impossible in principle: the transform can't
    reproduce the segment tier, or no transcription line survives (the mirror
    would carry no content at all).
    """
    if transform is None or transform(prev["segment_tier"]) != seg2:
        return None

    dropped = []

    def keep(label, t):
        """Mirror one tier ref; None (and record) if the target is missing."""
        if not t:
            return t
        mt = transform(t)
        if mt in tier_set:
            return mt
        dropped.append((label, mt))
        return None

    forms = []
    for fm in (prev.get("forms") or []):
        t = fm.get("tier")
        mt = transform(t) if t else t
        if mt and mt not in tier_set:
            dropped.append(("Transcription", mt))
            continue
        forms.append({"tier": mt, "kind": fm.get("kind")})

    transl, transl_langs = [], {}
    for t in (prev.get("transl") or []):
        mt = transform(t)
        if mt in tier_set:
            transl.append(mt)
            transl_langs[mt] = (prev.get("transl_langs") or {}).get(t, "")
        else:
            dropped.append(("Translation", mt))

    notes = []
    for t in (prev.get("notes") or []):
        mt = transform(t)
        if mt in tier_set:
            notes.append(mt)
        else:
            dropped.append(("Notes", mt))

    word_form = keep("Word", prev.get("word_form"))
    # Word gloss depends on the word tier; if the word tier is gone it goes too.
    word_gls = keep("Word gloss", prev.get("word_gls")) if word_form else None

    morph_form = keep("Morpheme", prev.get("morph_form"))
    if morph_form:
        morph_gls      = keep("Morph gloss", prev.get("morph_gls"))
        morph_gls_lang = prev.get("morph_gls_lang", "")
        morph_pos      = keep("Morph PoS", prev.get("morph_pos"))
        morph_pos_sep  = prev.get("morph_pos_sep", "") if morph_pos else ""
    else:
        morph_gls, morph_gls_lang = None, ""
        morph_pos, morph_pos_sep  = None, ""

    if not forms:
        # Nothing to transcribe survived — the mirror would be empty.
        return None

    new = {
        "who":            who2,
        "segment_tier":   seg2,
        "forms":          forms,
        "transl":         transl,
        "transl_langs":   transl_langs,
        "notes":          notes,
        "word_form":      word_form,
        "word_gls":       word_gls,
        "morph_form":     morph_form,
        "morph_gls":      morph_gls,
        "morph_gls_lang": morph_gls_lang,
        "morph_pos":      morph_pos,
        "morph_pos_sep":  morph_pos_sep,
    }
    return new, dropped


# ─── Interactive config builder ───────────────────────────────────────────────

def interactive_config(tier_map, annotations, stem, directory_mode=False,
                       escape_back=False):
    tier_ids = list(tier_map.keys())
    tier_set = set(tier_ids)
    ann_count = defaultdict(int)
    for ann in annotations.values():
        ann_count[ann["tier_id"]] += 1
    print_tier_tree(tier_map, annotations)

    print("=" * 64)
    print("Conversion setup")
    print("=" * 64)
    print("Answer each question by typing the number or the tier name shown.")
    print("Press Enter to skip optional questions.")
    print("Type '<' to go back to the previous question at any point.\n")

    auto_seg_tiers = detect_segment_tiers(tier_map, annotations) or []

    # ── prefix steps ──────────────────────────────────────────────────────────
    def step_text_id(state):
        tag = "WORDLIST" if state.get("doctype") == "wordlist" else "TEXT"
        state["text_id"] = _ask(f"Document identifier  [{tag} id='...']", stem)

    def step_object_lang(state):
        while True:
            val = _ask(
                "ISO 639-3 code of the object language  [XML: xml:lang='...']", "",
                allow_back=not state.get("_at_start")
            )
            if val:
                state["object_lang"] = val
                return
            print("  A language code is required — please type one (e.g. tvk, fr, bod).")

    def _set_seg_tiers(state, seg):
        multi = len(seg) > 1
        if state.get("seg_tiers") == seg and state.get("speakers"):
            state["multi"] = multi
            return  # unchanged — keep already-entered speaker answers
        state["seg_tiers"] = list(seg)
        state["multi"] = multi
        speakers = []
        for i, stid in enumerate(seg):
            who = _speaker_key(stid, tier_map) or (f"SP{i+1}" if multi else "")
            speakers.append({"who": who, "segment_tier": stid})
        state["speakers"] = speakers

    def step_segment(state):
        ab = not state.get("_at_start")
        if auto_seg_tiers:
            unit = "W" if state.get("doctype") == "wordlist" else "S"
            seg = _pick_many(
                f"Segment tier(s) (these set timing; "
                f"one per speaker)  [XML: <{unit}>]",
                tier_ids, allow_back=ab, defaults=auto_seg_tiers, required=True
            )
            auto_spk   = {_speaker_key(t, tier_map) for t in auto_seg_tiers}
            chosen_spk = {_speaker_key(t, tier_map) for t in seg}
            missing = auto_spk - chosen_spk
            if missing:
                print(f"\n  WARNING: detected speaker(s) not in your selection: "
                      f"{', '.join(sorted(missing))}")
                print(  "  Their sentences will be absent from the output.")
        else:
            print("\nNo segment tiers were auto-detected.")
            seg = _pick_many(
                "REQUIRED — Segment tier(s) (one per speaker):",
                tier_ids, allow_back=ab, required=True
            )
        _set_seg_tiers(state, seg)

    # ── suffix steps ──────────────────────────────────────────────────────────
    def step_doctype(state):
        raw = _ask("\nOutput type — text or wordlist? [text]", "text",
                   allow_back=not state.get("_at_start")).lower()
        state["doctype"] = raw if raw in ("text", "wordlist") else "text"

    def build_steps(state):
        steps = [("doctype", step_doctype)]
        if not directory_mode:
            steps.append(("text_id", step_text_id))
        steps.append(("object_lang", step_object_lang))
        steps.append(("segment", step_segment))
        is_wl = state.get("doctype") == "wordlist"
        for idx in range(len(state.get("seg_tiers", []))):
            steps.extend(_make_speaker_steps(idx, tier_ids, tier_map, tier_set,
                                             state.get("multi", False), ann_count,
                                             is_wordlist=is_wl))
        return steps

    history = []
    state = _run_flow(build_steps, {}, history, escape_back=escape_back)

    # ── assemble, summarize, and offer to go back and adjust ─────────────────
    # one authoritative statement of what a speaker config contains
    _SPK_DEFAULTS = {
        "forms": [], "transl": [], "transl_langs": {}, "notes": [],
        "word_form": None, "word_gls": None, "morph_form": None,
        "morph_gls": None, "morph_gls_lang": "", "morph_pos": None,
        "morph_pos_sep": "",
    }

    def _assemble():
        # build the config from copies: `state` stays intact for re-entry
        cfg = {
            "text_id":     state.get("text_id") if not directory_mode else None,
            "doctype":     state.get("doctype", "text"),
            "object_lang": state.get("object_lang", ""),
            "speakers":    [],
        }
        for sp in state.get("speakers", []):
            sp = dict(sp)
            sp.pop("_prefilled", None)
            for k, v in _SPK_DEFAULTS.items():
                sp.setdefault(k, type(v)() if isinstance(v, (list, dict)) else v)
            cfg["speakers"].append(sp)
        return cfg

    while True:
        cfg = _assemble()
        unmapped = _show_config_summary(cfg, tier_map)
        if unmapped:
            try:
                back = _yesno(
                    "Adjust the mapping? (y re-opens the last question, from "
                    "which '<' steps back one at a time)", False, allow_back=True)
            except _GoBack:
                back = True   # '<' here means the same as 'y': go back
            if back:
                state = _run_flow(build_steps, state, history, resume=True)
                continue
        break

    if not directory_mode:
        _save_config_interactive(cfg, stem)

    return cfg


def _print_speaker_mapping(spk, indent="    "):
    """Print one speaker's tier mapping (shared by the mirror preview and the
    final summary)."""
    forms = ", ".join(f"{f['tier']}({f.get('kind') or 'no label'})"
                      for f in (spk.get("forms") or [])) or "(none)"
    print(f"{indent}Transcription : {forms}")
    tl = spk.get("transl_langs") or {}
    transl = ", ".join(f"{t}({tl.get(t) or 'no lang'})"
                       for t in (spk.get("transl") or [])) or "(none)"
    print(f"{indent}Translation   : {transl}")
    for label, key in (("Notes", "notes"), ("Word", "word_form"),
                       ("Word gloss", "word_gls"), ("Morpheme", "morph_form"),
                       ("Morph gloss", "morph_gls")):
        v = spk.get(key)
        if isinstance(v, list):
            v = ", ".join(v) if v else "(none)"
        print(f"{indent}{label:<13}: {v if v else '(none)'}")
    if spk.get("morph_pos"):
        print(f"{indent}{'Morph PoS':<13}: {spk['morph_pos']} "
              f"(separator: {spk.get('morph_pos_sep', '')!r})")
    else:
        print(f"{indent}{'Morph PoS':<13}: (none)")


def _show_config_summary(cfg, tier_map):
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Output type    : {cfg.get('doctype')}")
    if cfg.get("text_id"):
        print(f"  Identifier     : {cfg.get('text_id')}")
    print(f"  Object lang    : {cfg.get('object_lang')}")
    for spk in cfg.get("speakers") or []:
        label = f"Speaker {spk['who']}" if spk.get("who") else "Speaker"
        print(f"\n  {label}  (segment tier: {spk['segment_tier']})")
        _print_speaker_mapping(spk, indent="    ")
    unmapped = sorted(set(tier_map) - _config_tier_names(cfg))
    if unmapped:
        print(f"\n  Tiers NOT exported ({len(unmapped)}): " + ", ".join(unmapped))
        print("  (their content will be absent from the XML)")
    print()
    return unmapped


# ─── Config saving (crash-safe) ────────────────────────────────────────────────

def _write_config(cfg, path):
    """Write one config to `path`, creating parent dirs. Returns True on success."""
    try:
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        print(f"Saved to {p}")
        return True
    except OSError as e:
        print(f"  Could not save to '{path}': {e}")
        print("  Your selections are NOT lost — type a different path "
              "(or Enter to skip).")
        return False


def _save_config_interactive(cfg, stem=None):
    """
    Prompt for a save path and retry on failure so selections survive.

    When `stem` is given (the input file's name), typing 'y' saves to
    "<stem>.json" without having to type the name out.
    """
    auto = f"{stem}.json" if stem else None
    while True:
        if auto:
            prompt = (
                "\nSave these choices to reuse next time?\n"
                f"(type 'y' for \"{auto}\", another file name ending in .json, "
                "or Enter to skip): "
            )
        else:
            prompt = (
                "\nSave these choices to reuse next time?\n"
                "(file name ending in .json, or Enter to skip): "
            )
        save_path = input(prompt).strip()
        if not save_path:
            return
        if auto and save_path.lower() in ("y", "yes"):
            save_path = auto
        if _write_config(cfg, save_path):
            return


def _save_configs_per_file(configs, folder):
    """
    Save one config per EAF into `folder` (created if needed), each named after
    the file it applies to: "<eaf stem>.json".  `configs` is a list of
    (cfg, [paths]); the same structure-config is written once per file in it.
    Returns the number of configs written.
    """
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  Could not create folder '{folder}': {e}")
        return 0
    n = 0
    for cfg, paths in configs:
        for path in paths:
            # Expand a base-name template to this file's real tiers so the saved
            # config is directly reusable (and matches by tier structure later);
            # non-template configs pass through unchanged.
            try:
                _, _, tm, _ = parse_eaf(str(path))
                file_cfg = dict(_expand_config_for_file(cfg, tm))
            except Exception:
                file_cfg = {k: v for k, v in cfg.items() if k != "_base_names"}
            file_cfg["text_id"] = path.stem
            if _write_config(file_cfg, str(folder / (path.stem + ".json"))):
                n += 1
    print(f"Saved {_plural(n, 'config')} to {folder}/")
    return n


def _save_configs_per_file_interactive(configs):
    """Ask for a folder, then save one <eaf stem>.json per file into it."""
    while True:
        folder = input(
            "\nSave configurations to reuse next time?\n"
            "Enter a FOLDER name (created if needed) — one '<filename>.json' is\n"
            "saved per EAF.  Press Enter to skip: "
        ).strip()
        if not folder:
            return
        if _save_configs_per_file(configs, folder):
            return
        # _save_configs_per_file already explained the failure; loop to retry.


def _config_tier_names(cfg):
    """Return the set of all tier names referenced in a v2 config."""
    tiers = set()
    for spk in cfg.get("speakers") or []:
        tiers.add(spk.get("segment_tier"))
        for f in spk.get("forms") or []:
            if f.get("tier"):
                tiers.add(f["tier"])
        for key in ("transl", "notes"):
            for t in spk.get(key) or []:
                tiers.add(t)
        for key in ("word_form", "word_gls", "morph_form", "morph_gls", "morph_pos"):
            if spk.get(key):
                tiers.add(spk[key])
    tiers.discard(None)
    tiers.discard("")
    return tiers


# ─── Build segments ───────────────────────────────────────────────────────────

def build_segments(annotations, children, tier_map, cfg):
    """
    Build the list of units (sentences or words) in time order, merging all
    speakers together.  Each speaker has its own tier mapping in cfg['speakers'].
    """
    speakers = cfg.get("speakers") or []

    def form_value(seg_id, form_tid):
        if annotations[seg_id]["tier_id"] == form_tid:
            return annotations[seg_id]["value"].strip()
        desc = collect_descendants(seg_id, form_tid, children, annotations)
        return " ".join(
            annotations[a]["value"] for a in desc if annotations[a]["value"]
        ).strip()

    def _collect_morphs(parent_id, spk):
        morphs  = []
        mtid    = spk.get("morph_form")
        if not mtid:
            return morphs
        gtid     = spk.get("morph_gls")
        gls_lang = spk.get("morph_gls_lang", "")
        pos_tid  = spk.get("morph_pos")
        pos_sep  = spk.get("morph_pos_sep", "")

        for mid in collect_descendants(parent_id, mtid, children, annotations):
            m_val = annotations[mid]["value"].strip().strip("-=")

            gloss = ""
            if gtid:
                for gid in collect_descendants(mid, gtid, children, annotations):
                    if annotations[gid]["value"]:
                        gloss = annotations[gid]["value"].strip().strip("-=")
                        break

            # Pangloss has no element for a part of speech, so it rides on the
            # morpheme GLOSS after a chosen separator ('go:vi').  Where the
            # morpheme has no gloss there is nothing to separate it from, so the
            # PoS stands alone rather than emitting a leading separator (':vi').
            if pos_tid:
                for pid in collect_descendants(mid, pos_tid, children, annotations):
                    pv = annotations[pid]["value"].strip()
                    if pv:
                        gloss = (gloss + pos_sep + pv) if gloss else pv
                        break

            morphs.append({"form": m_val, "gloss": gloss, "gloss_lang": gls_lang})
        return morphs

    def _collect_words(parent_id, spk):
        words = []
        wtid  = spk.get("word_form")
        if not wtid:
            if spk.get("morph_form"):
                morphs = _collect_morphs(parent_id, spk)
                if morphs:
                    words.append({"form": "", "gls": "", "morphs": morphs})
            return words
        wgtid = spk.get("word_gls")
        for wid in collect_descendants(parent_id, wtid, children, annotations):
            w_gls = ""
            if wgtid:
                parts = [
                    annotations[g]["value"]
                    for g in collect_descendants(wid, wgtid, children, annotations)
                    if annotations[g]["value"]
                ]
                w_gls = "".join(parts)
            words.append({
                "form":   annotations[wid]["value"],
                "gls":    w_gls,
                "morphs": _collect_morphs(wid, spk),
            })
        return words

    # Gather all segment annotations across all speakers
    seg_anns = []
    for spk in speakers:
        stid = spk["segment_tier"]
        who  = spk["who"]
        for ann in annotations.values():
            if ann["tier_id"] == stid:
                seg_anns.append((ann, who, spk))

    if not seg_anns:
        print("Warning: no segment annotations found.", file=sys.stderr)
        return []

    seg_anns.sort(key=lambda t: (t[0]["ts1"] or 0))

    segments = []
    for s_ann, who, spk in seg_anns:
        sid = s_ann["id"]

        sentence_tid = spk["forms"][0]["tier"] if spk.get("forms") else spk.get("sentence", "")
        raw_id = ""
        if sentence_tid and annotations[sid]["tier_id"] != sentence_tid:
            raw_id = annotations[sid]["value"]

        form_lines = []
        for f in (spk.get("forms") or []):
            if not f.get("tier"):
                continue
            txt = form_value(sid, f["tier"])
            if txt:
                form_lines.append({"kind": f.get("kind"), "text": txt})

        transl       = []
        transl_langs = spk.get("transl_langs") or {}
        for ttid in (spk.get("transl") or []):
            lang = transl_langs.get(ttid, _tier_lang(ttid, tier_map))
            desc = collect_descendants(sid, ttid, children, annotations)
            vals = [annotations[a]["value"] for a in desc if annotations[a]["value"]]
            if vals:
                transl.append((lang, " ".join(vals)))

        notes = []
        for ntid in (spk.get("notes") or []):
            for a in collect_descendants(sid, ntid, children, annotations):
                if annotations[a]["value"]:
                    notes.append(annotations[a]["value"])

        words = _collect_words(sid, spk)

        segments.append({
            "ts1":    s_ann["ts1"] or 0,
            "ts2":    s_ann["ts2"] or 0,
            "id":     raw_id,
            "who":    who,
            "forms":  form_lines,
            "transl": transl,
            "notes":  notes,
            "words":  words,
        })

    # Warn if any speaker's segment tier produced nothing
    count_by_who = defaultdict(int)
    for seg in segments:
        count_by_who[seg["who"]] += 1
    for spk in speakers:
        if count_by_who.get(spk["who"], 0) == 0:
            print(
                f"WARNING: speaker '{spk['who']}' (segment tier '{spk['segment_tier']}') "
                f"produced 0 segments.",
                file=sys.stderr,
            )

    return segments


# ─── Write XML ────────────────────────────────────────────────────────────────

def write_xml(segments, cfg, out_path):
    lang    = cfg.get("object_lang") or ""
    text_id = cfg.get("text_id") or "text"
    doctype = cfg.get("doctype") or "text"

    is_wordlist = (doctype == "wordlist")
    root_tag = "WORDLIST" if is_wordlist else "TEXT"
    unit_tag = "W"        if is_wordlist else "S"

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<!DOCTYPE {root_tag} SYSTEM "https://cocoon.huma-num.fr/schemas/Archive.dtd">',
    ]
    lang_attr = f' xml:lang="{_esc_attr(lang)}"' if lang else ""
    lines.append(f'<{root_tag} id="{_esc_attr(text_id)}"{lang_attr}>')
    soundfile = cfg.get("_soundfile")
    if soundfile:
        lines.append("    <HEADER>")
        lines.append(f'        <SOUNDFILE href="{_esc_attr(soundfile)}"/>')
        lines.append("    </HEADER>")
    else:
        lines.append("    <HEADER/>")

    used_ids = set()
    seq = 0
    for s in segments:
        raw = (s["id"] or "").strip()
        # The segment tier's own value is the document's identifier for that
        # unit, so it is carried across.  A bare number is the one
        # value that cannot stand alone, so it takes the unit tag. 
        if raw.isdigit():
            unit_id = f"{unit_tag}{raw}"
        else:
            unit_id = raw
        # Uniqueness is required per document: two speakers whose numbering both
        # restart at 1, or a source with repeated identifiers, drop to the next
        # free sequential id rather than emitting a duplicate.
        if not unit_id or unit_id in used_ids:
            seq += 1
            unit_id = f"{unit_tag}{seq}"
            while unit_id in used_ids:
                seq += 1
                unit_id = f"{unit_tag}{seq}"
        used_ids.add(unit_id)
        who_attr = f' who="{_esc_attr(s["who"])}"' if s["who"] else ""
        lines.append(f'    <{unit_tag} id="{_esc_attr(unit_id)}"{who_attr}>')
        lines.append(
            f'        <AUDIO start="{ms_to_sec(s["ts1"])}" end="{ms_to_sec(s["ts2"])}"/>'
        )

        for f in s["forms"]:
            kattr = f' kindOf="{_esc_attr(f["kind"])}"' if f.get("kind") else ""
            lines.append(f'        <FORM{kattr}>{_esc(f["text"])}</FORM>')

        for lang_key, text in s["transl"]:
            la = f' xml:lang="{_esc_attr(lang_key)}"' if lang_key else ""
            lines.append(f'        <TRANSL{la}>{_esc(text)}</TRANSL>')

        for note in s["notes"]:
            lines.append(f'        <NOTE message="{_esc_attr(note)}"/>')

        if is_wordlist:
            for w in s["words"]:
                for m in w["morphs"]:
                    lines.append("        <M>")
                    if m["form"]:
                        lines.append(f'            <FORM>{_esc(m["form"])}</FORM>')
                    if m["gloss"]:
                        gl = f' xml:lang="{_esc_attr(m["gloss_lang"])}"' if m.get("gloss_lang") else ""
                        lines.append(f'            <TRANSL{gl}>{_esc(m["gloss"])}</TRANSL>')
                    lines.append("        </M>")
        else:
            for w in s["words"]:
                has_form = bool(w["form"])
                # Keep a word if it shows a form/gloss OR groups at least one
                # morpheme; only words with nothing at all are dropped.
                if not (has_form or w["gls"] or w["morphs"]):
                    continue
                lines.append("        <W>")
                if w["form"]:
                    lines.append(f'            <FORM>{_esc(w["form"])}</FORM>')
                if w["gls"]:
                    lines.append(f'            <TRANSL>{_esc(w["gls"])}</TRANSL>')
                for m in w["morphs"]:
                    lines.append("            <M>")
                    if m["form"]:
                        lines.append(f'                <FORM>{_esc(m["form"])}</FORM>')
                    if m["gloss"]:
                        gl = f' xml:lang="{_esc_attr(m["gloss_lang"])}"' if m.get("gloss_lang") else ""
                        lines.append(f'                <TRANSL{gl}>{_esc(m["gloss"])}</TRANSL>')
                    lines.append("            </M>")
                lines.append("        </W>")

        lines.append(f"    </{unit_tag}>")

    lines.append(f"</{root_tag}>")

    out_path = Path(out_path)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ─── Directory mode ────────────────────────────────────────────────────────────

def _tier_structure_signature(tier_map):
    """Hashable fingerprint of a tier map for grouping EAFs by structure."""
    return frozenset(
        (tid, tier.get("PARENT_REF") or "", tier.get("LINGUISTIC_TYPE_REF") or "")
        for tid, tier in tier_map.items()
    )


class _MergedTier:
    """
    A minimal stand-in for an ELAN <TIER> element used only during the merged
    interactive interview.  It answers .get(attr) from a plain dict of the
    attributes the rest of the code reads (PARENT_REF, LINGUISTIC_TYPE_REF,
    PARTICIPANT, LANG_REF, DEFAULT_LOCALE, TIER_ID), all rebased to base names.
    """
    __slots__ = ("_a",)

    def __init__(self, attrs):
        self._a = attrs

    def get(self, key, default=None):
        return self._a.get(key, default)


def _base_path(tm, tid, memo=None):
    """
    A tier's full ancestor identity in BASE names: ('mb', ('mot', ('tx', ('ref',
    None)))).  Two tiers across files are the same node exactly when their whole
    base path matches — comparing only the immediate parent's base name is not
    enough, because a child of a root-level copy 'tx@X-cp' and a child of the
    real 'tx' under 'ref' would then look identical and get re-parented onto
    whichever 'tx' node happened to win the plain name.
    """
    if memo is None:
        memo = {}
    if tid in memo:
        return memo[tid]
    tier = tm.get(tid)
    p = tier.get("PARENT_REF") if tier is not None else None
    if not p:
        parent_path = None
    elif p in tm:
        parent_path = _base_path(tm, p, memo)
    else:
        parent_path = (_base_tier(p), None)   # dangling ref: best-effort root
    path = (_base_tier(tid), parent_path)
    memo[tid] = path
    return path


def _representative_tier_map(tier_maps):
    """
    Build ONE base-name tier_map covering every tier seen in any file of the
    group (union), for the merged folder interview.
    """
    # Collect per base path so tiers at different places never merge.
    candidates = defaultdict(list)         # path -> [(tid, tier), ...]
    all_types  = defaultdict(list)         # path -> [type, ...]  (ordered)
    all_langs  = defaultdict(list)
    all_spk    = defaultdict(list)
    all_names  = defaultdict(list)         # path -> real tier names
    for tm in tier_maps:
        memo = {}
        for tid, tier in tm.items():
            path = _base_path(tm, tid, memo)
            candidates[path].append((tid, tier))
            for store, val in (
                (all_types, tier.get("LINGUISTIC_TYPE_REF") or ""),
                (all_langs, tier.get("LANG_REF") or tier.get("DEFAULT_LOCALE") or ""),
                (all_spk,   _speaker_key(tid, tm)),
                (all_names, tid),
            ):
                if val and val not in store[path]:
                    store[path].append(val)

    paths_of = defaultdict(list)           # base -> [(path, occurrences)]
    for path, occ in candidates.items():
        paths_of[path[0]].append((path, len(occ)))

    primary_path = {}
    for base, plist in paths_of.items():
        primary_path[base] = max(
            plist, key=lambda pc: (pc[1], pc[0][1] is not None)
        )[0]

    key_of = {}                            # path -> unique node key
    all_bases = set(paths_of)

    def node_key(path):
        if path in key_of:
            return key_of[path]
        base, parent_path = path
        if len(paths_of[base]) == 1 or path == primary_path[base]:
            key = base
        else:
            key = (f"{base} (root)" if parent_path is None
                   else f"{base} (under {parent_path[0]})")
            taken = all_bases | set(key_of.values())
            while key in taken:
                key += "'"
        key_of[path] = key
        return key

    merged = {}
    for path, occ in candidates.items():
        base, parent_path = path
        name = node_key(path)
        pick = occ[0][1]
        parent_name = (node_key(parent_path)
                       if parent_path in candidates else None)
        attrs = {
            "TIER_ID":            name,
            "PARENT_REF":         parent_name,
            "LINGUISTIC_TYPE_REF": pick.get("LINGUISTIC_TYPE_REF"),
            "LANG_REF":           pick.get("LANG_REF"),
            "DEFAULT_LOCALE":     pick.get("DEFAULT_LOCALE"),
            "PARTICIPANT":        None,   # base view is speaker-agnostic
            "_base":              base,   # real base name to answer the interview with
            "_path":              path,   # full identity, matched by annotations
            "_display":           name,   # same as the key: menus must match the tree
            "_types":             all_types[path],
            "_langs":             all_langs[path],
            "_speakers":          all_spk[path],
            "_variants":          all_names[path],
        }
        if attrs["PARENT_REF"] is None:
            attrs.pop("PARENT_REF")
        merged[name] = _MergedTier(attrs)
    return merged


def _representative_annotations(annotation_samples, tier_maps, rep_map):
    """
    Merge per-file annotation samples into one sample keyed to the MERGED node
    names, so annotation counts and segment auto-detection see every node that
    exists in at least one file.
    """
    name_of_path = {rep.get("_path"): name for name, rep in rep_map.items()}

    # Map every real tier name, per file, to the merged node it belongs to.
    node_of = []                    # parallel to annotation_samples
    for tm in tier_maps:
        memo = {}
        node_of.append({tid: name_of_path.get(_base_path(tm, tid, memo))
                        for tid in tm})

    best_for_node = {}     # node name -> (count, [anns])
    for anns, m in zip(annotation_samples, node_of):
        per_node = defaultdict(list)
        for a in anns.values():
            node = m.get(a["tier_id"])
            if node:
                per_node[node].append(a)
        for node, lst in per_node.items():
            if node not in best_for_node or len(lst) > best_for_node[node][0]:
                best_for_node[node] = (len(lst), lst)

    merged = {}
    for i, (node, (_, lst)) in enumerate(best_for_node.items()):
        for j, a in enumerate(lst):
            a2 = dict(a)
            a2["tier_id"] = node            # rebase so counts land on the node
            merged[f"__rep{i}_{j}"] = a2    # unique key: real IDs collide across files
    return merged


def _base_spine(tier_map):
    """
    The structural backbone of a file: every tier that some other tier hangs off
    (i.e. appears as a PARENT_REF), identified by its full BASE PATH rather than
    its bare name.  Leaf tiers — glosses, notes, alternative translations — are
    the OPTIONAL extras that legitimately vary between files of the same corpus,
    so they are excluded here.
    """
    spine = set()
    memo = {}
    by_base = {}
    for tid in tier_map:
        by_base.setdefault(_base_tier(tid), tid)
    for tier in tier_map.values():
        p = tier.get("PARENT_REF")
        if not p:
            continue
        ref = p if p in tier_map else by_base.get(_base_tier(p))
        spine.add(_base_path(tier_map, ref, memo) if ref
                  else (_base_tier(p), None))
    return spine


def _same_corpus(a_tiers, a_spine, b_tiers, b_spine):
    """
    Whether two files belong in one interactive group: the smaller file's
    structural spine must be fully contained in the other file's spine (in
    either direction).  Flat files carry no structural signal, so they 
    only group with files having the same base tier set.
    """
    if not a_spine or not b_spine:
        return a_tiers == b_tiers
    return a_spine <= b_spine or b_spine <= a_spine


def _group_eafs(eaf_paths):
    """
    Parse all EAFs and group them for the interactive interview.
    """
    parsed = []   # (path, tier_map, annotations, base_tiers, spine)
    for path in eaf_paths:
        try:
            _, annotations, tier_map, _ = parse_eaf(str(path))
        except Exception as e:
            print(f"Warning: could not parse {path.name}: {e}", file=sys.stderr)
            continue
        base_tiers = frozenset(_base_tier(t) for t in tier_map)
        parsed.append((path, tier_map, annotations, base_tiers,
                       _base_spine(tier_map)))

    groups = []   # each: {"members": [idx,...], "tiers": set, "spine": set}
    for i, (_, _, _, tiers, spine) in enumerate(parsed):
        hits = [g for g in groups
                if any(_same_corpus(tiers, spine,
                                    parsed[j][3], parsed[j][4])
                       for j in g["members"])]
        if not hits:
            groups.append({"members": [i], "tiers": set(tiers),
                           "spine": set(spine)})
        else:
            first = hits[0]
            first["members"].append(i)
            first["tiers"] |= tiers
            first["spine"] |= spine
            for extra in hits[1:]:
                first["members"].extend(extra["members"])
                first["tiers"] |= extra["tiers"]
                first["spine"] |= extra["spine"]
                groups.remove(extra)

    result = []
    for g in groups:
        members = g["members"]
        tms   = [parsed[j][1] for j in members]
        anns  = [parsed[j][2] for j in members]
        paths = [parsed[j][0] for j in members]
        rep_map  = _representative_tier_map(tms)
        rep_anns = _representative_annotations(anns, tms, rep_map)
        result.append((rep_map, rep_anns, paths))
    return sorted(result, key=lambda t: len(t[2]), reverse=True)


def _soundfile_for(path):
    """
    Pick the <SOUNDFILE> value for one EAF: the single linked audio file's
    basename.  With zero or several audio files, return None (empty HEADER) —
    and on several, warn.
    """
    media = media_basenames(str(path))
    if len(media) == 1:
        return media[0]
    if len(media) >= 2:
        print(f"  WARNING {Path(path).name}: {len(media)} audio files linked "
              f"({', '.join(media)}); SOUNDFILE left empty — add the right one "
              f"by hand.", file=sys.stderr)
    return None


_SPK_TIER_LIST_KEYS = ("transl", "notes")
_SPK_TIER_SCALAR_KEYS = ("segment_tier", "word_form", "word_gls",
                         "morph_form", "morph_gls", "morph_pos")


def _rename_config_tiers(cfg, mapping):
    """
    Rewrite every tier name in a config through `mapping` (missing keys are left
    as-is).  Used to turn the merged view's display node names back into the real
    base tier names before the config is expanded per file.
    """
    def m(t):
        return mapping.get(t, t) if t else t

    out = dict(cfg)
    new_speakers = []
    for spk in cfg.get("speakers") or []:
        s = dict(spk)
        for key in _SPK_TIER_SCALAR_KEYS:
            if s.get(key):
                s[key] = m(s[key])
        for key in _SPK_TIER_LIST_KEYS:
            renamed = [m(t) for t in s.get(key) or []]
            # Two menu entries can rename to one base tier (picking both 'tx'
            # and 'tx (root)' both yield 'tx'); keep the first occurrence.
            s[key] = list(dict.fromkeys(renamed))
        s["forms"] = [dict(f, tier=m(f.get("tier"))) for f in s.get("forms") or []]
        langs = s.get("transl_langs") or {}
        new_langs = {}
        for t, v in langs.items():
            new_langs.setdefault(m(t), v)   # first occurrence wins, like the lists
        s["transl_langs"] = new_langs
        new_speakers.append(s)
    out["speakers"] = new_speakers
    return out


def _resolve_base_tier(base, suffix, present):
    """
    Resolve a base tier name to the concrete tier that exists in a file.

    `suffix` is the speaker code being expanded ('' for a single, unmarked
    speaker).  Prefer the speaker-specific 'base@suffix'; fall back to the bare
    'base' (a tier shared across speakers, e.g. a common note tier); return None
    when neither exists — the caller drops that role for this file.
    """
    if suffix:
        cand = f"{base}@{suffix}"
        if cand in present:
            return cand
    return base if base in present else None


def _expand_speaker(base_spk, suffix, present):
    """
    Stamp one base-name speaker mapping onto a concrete speaker.  Every tier
    reference is resolved against `present` (the file's real tier names); roles
    whose tier is absent are dropped silently.  Returns a speaker dict, or None
    if even the segment tier can't be resolved (nothing to build).
    """
    seg = _resolve_base_tier(base_spk.get("segment_tier"), suffix, present)
    if not seg:
        return None
    out = dict(base_spk)
    out["who"] = suffix or base_spk.get("who", "")
    out["segment_tier"] = seg

    for key in _SPK_TIER_SCALAR_KEYS:
        if key == "segment_tier":
            continue
        if base_spk.get(key):
            out[key] = _resolve_base_tier(base_spk[key], suffix, present)

    for key in _SPK_TIER_LIST_KEYS:
        resolved = [_resolve_base_tier(t, suffix, present)
                    for t in base_spk.get(key) or []]
        out[key] = [t for t in resolved if t]

    # forms is a list of {"tier": name, "kind": ...}; resolve each tier and drop
    # any whose tier is absent from this file (keeping at least nothing rather
    # than a dangling reference).
    new_forms = []
    for f in base_spk.get("forms") or []:
        r = _resolve_base_tier(f.get("tier"), suffix, present)
        if r:
            nf = dict(f)
            nf["tier"] = r
            new_forms.append(nf)
    out["forms"] = new_forms

    langs = base_spk.get("transl_langs") or {}
    out["transl_langs"] = {
        r: langs[t]
        for t in (base_spk.get("transl") or [])
        if (r := _resolve_base_tier(t, suffix, present)) and t in langs
    }
    return out


def _expand_config_for_file(cfg, tier_map):
    """
    Turn a base-name template config (produced by a merged directory interview)
    into a concrete per-file config.

    A file with no '@speaker' codes keeps the single base mapping, with absent
    optional tiers dropped.  A multispeaker file gets the base mapping stamped
    once per speaker code found on its tier names.  Non-template configs (from a
    single file or an exact-structure group) are returned unchanged.
    """
    if not cfg.get("_base_names"):
        return cfg
    present = set(tier_map.keys())
    codes = _file_speaker_codes(tier_map)
    base_speakers = cfg.get("speakers") or []

    new_speakers = []
    if not codes:
        for base_spk in base_speakers:
            spk = _expand_speaker(base_spk, "", present)
            if spk:
                spk["who"] = (spk.get("who")
                              or _speaker_key(spk["segment_tier"], tier_map))
                new_speakers.append(spk)
    else:
        for suffix in codes:
            for base_spk in base_speakers:
                spk = _expand_speaker(base_spk, suffix, present)
                if spk:
                    new_speakers.append(spk)

    out = dict(cfg)
    out.pop("_base_names", None)
    out["speakers"] = new_speakers
    return out


def _convert_one(path, cfg, output_dir):
    """Parse, build, filter, and write one EAF file."""
    _, annotations, tier_map, linguistic_types = parse_eaf(str(path))
    was_template = bool(cfg.get("_base_names"))
    cfg = _expand_config_for_file(cfg, tier_map)
    if was_template and not cfg.get("speakers"):
        print(f"  {path.name}: SKIPPED — its tiers do not include the chosen "
              f"segment tier (file left unconverted)", file=sys.stderr)
        return
    file_cfg = dict(cfg)
    file_cfg["text_id"] = path.stem
    file_cfg["_soundfile"] = _soundfile_for(path)
    children = build_children(annotations, tier_map, linguistic_types)
    segments = build_segments(annotations, children, tier_map, file_cfg)
    nonempty = [s for s in segments if s["forms"]]
    skipped  = len(segments) - len(nonempty)
    out_path = Path(output_dir) / (path.stem + ".xml")
    write_xml(nonempty, file_cfg, str(out_path))
    note = f" ({skipped} empty skipped)" if skipped else ""
    print(f"  {path.name} -> {out_path.name}  ({len(nonempty)} units{note})")


class _Interrupted(Exception):
    """
    Raised when the user presses Ctrl-C during a folder interview, carrying the
    structures answered so far so the caller can offer to keep that work rather
    than lose it.
    """
    def __init__(self, configs):
        super().__init__("interrupted")
        self.configs = configs


def _interactive_configs(eaf_paths, dir_stem):
    """
    Group EAFs by BASE tier structure (speaker '@' codes stripped, optional-tier
    variants unioned) and run the interview once per group, against a merged
    representative view.  The resulting config is a base-name template that is
    expanded per file at convert time.  Returns a list of (cfg, [paths]).
    """
    groups = _group_eafs(eaf_paths)
    if not groups:
        print("None of the EAF files could be parsed — nothing to configure.",
              file=sys.stderr)
        return []
    multi = len(groups) > 1
    if multi:
        print(f"\nFound {_plural(len(groups), 'tier structure')} across "
              f"{_plural(len(eaf_paths), 'file')} (speaker codes and optional "
              f"tiers merged).")
        print("Press Ctrl-C at any point to stop; you will be asked whether to "
              "keep the structures you have already answered.")

    done = [None] * len(groups)     # per group: cfg once answered

    def answered():
        return [(cfg, groups[j][2]) for j, cfg in enumerate(done)
                if cfg is not None]

    i = 0
    while i < len(groups):
        tier_map, annotations, paths = groups[i]
        if multi:
            names = ", ".join(p.name for p in paths[:4])
            if len(paths) > 4:
                names += f" … (+{len(paths)-4} more)"
            print(f"\n{'='*64}")
            print(f"Structure {i+1} of {len(groups)} — "
                  f"{_plural(len(paths), 'file')}: {names}")
            if done[i] is not None:
                print("(already configured — your answers are replaced by what "
                      "you choose now)")
            print(f"{'='*64}")
        else:
            print(f"All {_plural(len(paths), 'file')} share one base tier "
                  f"structure.\n")
        print("The tree below is the union of every file in this group; pick each "
              "tier role once.\nSpeaker '@' codes are filled in automatically per "
              "file, and tiers a file\nlacks are skipped for that file.")
        if multi and i > 0:
            print(f"Type '<' at the first question below to go back to "
                  f"structure {i}.")
        print()
        try:
            cfg = interactive_config(tier_map, annotations, dir_stem,
                                     directory_mode=True,
                                     escape_back=(multi and i > 0))
        except _GoBack:
            # '<' at the first question: step back a structure, exactly like '<'
            # steps back a question inside one.
            print("\n" + "↩ " * 20)
            print(f"  Going back to structure {i} — ignore the options listed "
                  f"above.")
            print("↩ " * 20)
            i -= 1
            continue
        except KeyboardInterrupt:
            raise _Interrupted(answered()) from None
        cfg["_base_names"] = True   # expanded per file in _expand_config_for_file
        node_to_base = {name: rep.get("_base") or name
                        for name, rep in tier_map.items()}
        done[i] = _rename_config_tiers(cfg, node_to_base)
        i += 1

    return answered()


def _finish_configs(configs, output_dir, config_dir=None):
    """
    Convert every file of every answered structure, then offer to save configs.
    Shared by the normal end of a folder run and the Ctrl-C path, so an
    interrupted run saves exactly what a completed one would.
    """
    n_files = sum(len(paths) for _, paths in configs)
    print(f"\nConverting {_plural(n_files, 'file')}...")
    for cfg, paths in configs:
        for path in paths:
            _convert_one(path, cfg, output_dir)
    if config_dir is not None:
        _save_configs_per_file(configs, config_dir)
    else:
        _save_configs_per_file_interactive(configs)


def _offer_partial_save(exc, output_dir, config_dir=None):
    """
    Handle a Ctrl-C out of the folder interview: offer to convert the structures
    already answered (and save their configs), or discard them.  The structure in
    progress when Ctrl-C arrived is not among them — it was never finished.

    A second Ctrl-C at this prompt quits immediately, so the interrupt always
    has a way out.
    """
    configs = exc.configs
    print()   # the ^C lands mid-line
    if not configs:
        print("Interrupted — no structure was fully answered, nothing to save.",
              file=sys.stderr)
        return
    n_files = sum(len(paths) for _, paths in configs)
    print(f"Interrupted after answering {_plural(len(configs), 'structure')} "
          f"({_plural(n_files, 'file')}).")
    try:
        if not _yesno("Convert and save those files before quitting?", True):
            print("Nothing saved.")
            return
    except (KeyboardInterrupt, EOFError):
        print("\nNothing saved.")
        return
    try:
        _finish_configs(configs, output_dir, config_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; some files may not have been written.",
              file=sys.stderr)


def _load_folder_configs(config_dir):
    """Load every <name>.json in the folder as (name, cfg, required_tiers)."""
    out = []
    for p in sorted(config_dir.glob("*.json")):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  Skipping config {p.name}: {e}", file=sys.stderr)
            continue
        out.append((p.stem, cfg, _config_tier_names(cfg)))
    return out


def _propose_matches(eaf_paths, folder_configs):
    """
    For each EAF, choose the most specific structurally-compatible config — i.e.
    one whose referenced tiers all exist in the file.  When several fit (e.g. a
    single-speaker config whose tiers are a subset of a 2-speaker file), the one
    requiring the MOST tiers wins; a config named after the file breaks ties.
    Returns (mapping {path: (name, cfg)}, unmatched [paths]).
    """
    mapping, unmatched = {}, []
    for path in eaf_paths:
        try:
            _, _, tier_map, _ = parse_eaf(str(path))
        except Exception as e:
            print(f"  Could not read {path.name}: {e}", file=sys.stderr)
            unmatched.append(path)
            continue
        tiers = set(tier_map.keys())
        compatible = [(n, c, req) for (n, c, req) in folder_configs if req and req <= tiers]
        if not compatible:
            unmatched.append(path)
            continue
        compatible.sort(key=lambda t: (path.stem != t[0], -len(t[2])))
        n, c, _ = compatible[0]
        mapping[path] = (n, c)
    return mapping, unmatched


def _confirm_and_adjust_mapping(eaf_paths, mapping, unmatched, folder_configs):
    """Show the proposed file→config mapping and let the user confirm or edit it."""
    def show():
        print("\nProposed config for each file (matched by tier structure):")
        for path in eaf_paths:
            if path in mapping:
                print(f"  {path.name:48s} ->  {mapping[path][0]}.json")
            else:
                print(f"  {path.name:48s} ->  (no match — configure interactively)")
    show()
    if _yesno("\nIs this correct?", True):
        return mapping, unmatched

    names = [n for n, _, _ in folder_configs]
    cfg_by_name = {n: c for n, c, _ in folder_configs}
    print("\nFor each file: type a config number, Enter to keep the proposal, "
          "'i' to configure it interactively, or 's' to skip it.")
    new_map, new_unmatched = {}, []
    for path in eaf_paths:
        cur = mapping.get(path)
        print(f"\n{path.name}   (proposed: {cur[0]+'.json' if cur else 'none'})")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}.json{'   <- proposed' if cur and cur[0] == n else ''}")
        raw = input("  Choice [Enter = keep]: ").strip().lower()
        if not raw:
            (new_map.__setitem__(path, cur) if cur else new_unmatched.append(path))
        elif raw == "i":
            new_unmatched.append(path)
        elif raw == "s":
            pass  # skip this file entirely
        elif raw.isdigit() and 1 <= int(raw) <= len(names):
            n = names[int(raw) - 1]
            new_map[path] = (n, cfg_by_name[n])
        else:
            print("  (unrecognized — keeping the proposal)")
            (new_map.__setitem__(path, cur) if cur else new_unmatched.append(path))
    return new_map, new_unmatched


def _convert_with_config_folder(eaf_paths, output_dir, config_dir, dir_stem):
    """
    Match each EAF to a config in `config_dir` by TIER STRUCTURE (not filename),
    confirm the mapping with the user, then convert.  Files with no compatible
    config are configured interactively and saved into the same folder; configs
    that match nothing are ignored.
    """
    folder_configs = _load_folder_configs(config_dir)
    mapping, unmatched = _propose_matches(eaf_paths, folder_configs)
    if folder_configs:
        mapping, unmatched = _confirm_and_adjust_mapping(
            eaf_paths, mapping, unmatched, folder_configs)

    if mapping:
        print(f"\nConverting {_plural(len(mapping), 'matched file')}...")
        for path in eaf_paths:
            if path in mapping:
                _convert_one(path, mapping[path][1], output_dir)

    # Configs that matched no file are simply never used → ignored.

    if unmatched:
        print(f"\n{_plural(len(unmatched), 'file')} "
              f"{'has' if len(unmatched) == 1 else 'have'} no matching config "
              f"in the folder:")
        for path in unmatched:
            print(f"  {path.name}")
        if _yesno("Configure these by hand? (n = skip them)", False):
            try:
                new_configs = _interactive_configs(unmatched, dir_stem)
            except _Interrupted as e:
                # save any new configs into the same folder, as a full run would
                _offer_partial_save(e, output_dir, config_dir)
                return
            _finish_configs(new_configs, output_dir, config_dir)
        else:
            print(f"Skipping {_plural(len(unmatched), 'unmatched file')}.")


def process_directory(eaf_dir, output_dir, config=None):
    eaf_paths = sorted(Path(eaf_dir).glob("*.eaf"))
    if not eaf_paths:
        print(f"No .eaf files found in {eaf_dir}", file=sys.stderr)
        sys.exit(1)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    dir_stem = Path(eaf_dir).stem
    cfg_path = Path(config) if config else None
    if cfg_path and not cfg_path.exists():
        print(f"Config path not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    # ── A config FOLDER: match each EAF to <eaf stem>.json ─────────────────────
    if cfg_path and cfg_path.is_dir():
        _convert_with_config_folder(eaf_paths, output_dir, cfg_path, dir_stem)
        return

    # ── A single config FILE: one mapping applied to every matching file ───────
    if cfg_path and cfg_path.is_file():
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        required = _config_tier_names(cfg)

        skipped    = []
        to_process = []
        for path in eaf_paths:
            _, _, tier_map, _ = parse_eaf(str(path))
            missing = required - set(tier_map.keys())
            if missing:
                skipped.append((path, missing))
            else:
                to_process.append(path)

        if skipped:
            print(f"\n{_plural(len(skipped), 'file')} "
                  f"{'does not' if len(skipped) == 1 else 'do not'} match the config "
                  f"'{cfg_path.name}':")
            for path, missing in skipped:
                print(f"  {path.name}: missing "
                      f"{_plural(len(missing), 'tier')}: "
                      f"{', '.join(sorted(missing))}")
            hand = _yesno("Configure these by hand? (n = skip them)", False)
        else:
            hand = False

        print(f"\nConverting {_plural(len(to_process), 'file')} "
              f"with '{cfg_path.name}'...")
        for path in to_process:
            _convert_one(path, cfg, output_dir)

        if hand:
            mismatched = [path for path, _ in skipped]
            print(f"\nConfiguring {_plural(len(mismatched), 'remaining file')} by hand "
                  f"(grouped by tier structure)...")
            try:
                extra = _interactive_configs(mismatched, dir_stem)
            except _Interrupted as e:
                _offer_partial_save(e, output_dir)
                return
            _finish_configs(extra, output_dir)
        return

    # ── No config: interview (grouped by structure), save per file, convert ────
    print(f"Scanning {_plural(len(eaf_paths), 'EAF file')}...")
    try:
        configs = _interactive_configs(eaf_paths, dir_stem)
    except _Interrupted as e:
        _offer_partial_save(e, output_dir)
        return
    _finish_configs(configs, output_dir)


# ─── Single-file output resolution ─────────────────────────────────────────────

def _resolve_single_output(output_arg, input_path):
    """
    Resolve the output argument for single-file mode.

    The argument may be an .xml FILE name or a FOLDER:
      - An existing directory, or a path ending in a path separator, is treated
        as a folder; the XML is written into it as "<eaf stem>.xml".
      - A path with no extension that is not an existing file is also treated as
        a folder (created on demand), for convenience.
      - Otherwise it is treated as a file path and used as-is.

    Returns the resolved output file Path.  Folders are created on demand, so
    the caller can always open the returned path for writing.
    """
    out = Path(output_arg)
    ends_with_sep = output_arg.endswith(("/", "\\"))

    is_folder = (
        out.is_dir()
        or ends_with_sep
        or (out.suffix == "" and not out.is_file())
    )

    if is_folder:
        out.mkdir(parents=True, exist_ok=True)
        return out / (input_path.stem + ".xml")

    return out


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert ELAN .eaf to Pangloss/Cocoon XML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input",  help="Input .eaf file or directory of .eaf files")
    parser.add_argument("output", nargs="?",
                        help="Output .xml file or a folder (single), or output "
                             "directory (batch)")
    parser.add_argument("--inspect", action="store_true",
                        help="Print tier tree and exit (single file only)")
    parser.add_argument("--config", metavar="PATH",
                        help="A JSON config file, or (for a directory) a FOLDER of "
                             "configs matched to each EAF by tier structure")
    args = parser.parse_args()

    input_path = Path(args.input)

    # ── Directory mode ─────────────────────────────────────────────────────────
    if input_path.is_dir():
        if not args.output:
            parser.error("output directory is required when input is a directory")
        process_directory(str(input_path), args.output, args.config)
        return

    # ── Single-file mode ───────────────────────────────────────────────────────
    _ts, annotations, tier_map, linguistic_types = parse_eaf(str(input_path))

    if args.inspect:
        print_tier_tree(tier_map, annotations)
        return

    if not args.output:
        parser.error("output file or folder is required unless --inspect is given")

    out_path = _resolve_single_output(args.output, input_path)
    if out_path.suffix.lower() != ".xml":
        print(f"  Note: output '{out_path}' does not end in .xml (possible typo?) "
              f"— writing there anyway.", file=sys.stderr)

    stem = input_path.stem

    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.is_dir():
            match = cfg_path / (stem + ".json")
            if match.exists():
                cfg_path = match
            else:
                folder_configs = _load_folder_configs(cfg_path)
                tiers = set(tier_map.keys())
                compatible = [(n, c, req) for (n, c, req) in folder_configs
                              if req and req <= tiers]
                compatible.sort(key=lambda t: -len(t[2]))
                if not compatible:
                    print(f"No config '{match.name}' in {cfg_path}, and none of "
                          f"its configs fit this file's tiers.", file=sys.stderr)
                    sys.exit(1)
                best_name, best_cfg, _ = compatible[0]
                print(f"No config '{match.name}' in {cfg_path}.")
                if not _yesno(f"Use '{best_name}.json', which fits this file's "
                              f"structure?", True):
                    print("Nothing converted.", file=sys.stderr)
                    sys.exit(1)
                cfg_path = cfg_path / (best_name + ".json")
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)

        # Two-way tier report: config tiers missing from the file, and file
        # tiers the config doesn't map (whose content won't be exported).
        cfg_tiers  = _config_tier_names(cfg)
        file_tiers = set(tier_map.keys())
        missing   = sorted(cfg_tiers - file_tiers)
        unmapped  = sorted(file_tiers - cfg_tiers)
        if missing:
            print(f"\n  WARNING: {_plural(len(missing), 'tier')} referenced by the config are "
                  f"absent from this file and will be empty: "
                  f"{', '.join(missing)}", file=sys.stderr)
        if unmapped:
            print(f"  WARNING: {_plural(len(unmapped), 'tier')} in this file are not mapped "
                  f"by the config and will not be exported: "
                  f"{', '.join(unmapped)}\n", file=sys.stderr)

        cfg["text_id"] = None
        if not cfg.get("object_lang"):
            print(f"Error: config '{cfg_path}' has no 'object_lang'.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        cfg = interactive_config(tier_map, annotations, stem)

    if not cfg.get("text_id"):
        cfg["text_id"] = stem
    cfg["_soundfile"] = _soundfile_for(input_path)

    if not cfg.get("speakers"):
        print("Error: no speaker/tier configuration.", file=sys.stderr)
        sys.exit(1)

    children = build_children(annotations, tier_map, linguistic_types)
    segments = build_segments(annotations, children, tier_map, cfg)
    nonempty = [s for s in segments if s["forms"]]
    if len(nonempty) < len(segments):
        print(f"  Note: {_plural(len(segments) - len(nonempty), 'segment')} with no "
              f"transcription text skipped.")
    segments = nonempty
    print(f"{_plural(len(segments), 'unit')} found. Writing {out_path} ...")
    write_xml(segments, cfg, str(out_path))
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)