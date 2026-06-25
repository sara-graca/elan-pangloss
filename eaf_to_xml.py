#!/usr/bin/env python3
"""
eaf_to_xml.py — Convert ELAN .eaf files to the Pangloss XML format.

Usage
-----
Inspect tier structure:
    python eaf_to_xml.py input.eaf --inspect

Convert a single file (interactive):
    python eaf_to_xml.py input.eaf output.xml

Reuse a saved configuration:
    python eaf_to_xml.py input.eaf output.xml --config my.json

Convert a whole directory (interactive):
    python eaf_to_xml.py input_dir/ output_dir/

Convert a whole directory with a saved config:
    python eaf_to_xml.py input_dir/ output_dir/ --config my.json
    python eaf_to_xml.py input_dir/ output_dir/ --config configs_folder/

Config reuse for directories
----------------------------
--config can be a single JSON file (used for every file) or a FOLDER of configs.
With a folder, each EAF is matched to the config whose tiers it actually has
(the most specific one if several fit). You confirm the proposed file->config
mapping before converting. One config can cover every file from the same
template; add more for the multispeaker or wordlist variants. Files no config
fits are set up interactively and saved into the folder; unused configs are
ignored.

Interactive navigation
-----------------------
Type < (or "back") at a prompt to return to the previous question. 
Press Enter to accept a suggestion (required field) or skip (optional field).
"""

import sys
import json
import argparse
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote


# ─── Helpers ──────────────────────────────────────────────────────────────────

def ms_to_sec(ms):
    return f"{(ms or 0) / 1000:.3f}"

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _esc_attr(text):
    return _esc(text).replace('"', "&quot;")


# ─── Parse EAF ────────────────────────────────────────────────────────────────

def parse_eaf(path):
    """
    Returns (time_slots, annotations, tier_map, linguistic_types).

    annotations : dict  ann_id → {id, value, ref, previous, ts1, ts2, tier_id}
    tier_map    : dict  tier_id → tier Element
    linguistic_types : dict  lt_id → {CONSTRAINTS}
    """
    tree = ET.parse(path)
    root = tree.getroot()

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
    Handles both Symbolic (ref-based) and Time_Subdivision (time-based) children.
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
        if linguistic_types.get(ltype, {}).get("CONSTRAINTS") == "Time_Subdivision":
            time_sub_tiers[tid] = parent_tier

    # Note: only Symbolic (ref-based) and Time_Subdivision children are linked.
    # The rarer ELAN "Included_In" stereotype is not handled — such children
    # would be left unlinked.  No interlinear corpus in scope uses it.

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
    queue = list(children.get(ann_id, []))
    while queue:
        cid = queue.pop(0)
        if annotations[cid]["tier_id"] == target_tier_id:
            result.append(cid)
        else:
            queue.extend(children.get(cid, []))
    if result and all(annotations[aid]["ts1"] is not None for aid in result):
        result.sort(key=lambda aid: annotations[aid]["ts1"])
    return result


# ─── Speaker identification ───────────────────────────────────────────────────

def _speaker_key(tid, tier_map):
    """
    Explicit speaker marker for a tier, or '' if none:
      1. PARTICIPANT attribute
      2. trailing  @SPx  suffix   (e.g. ref@SP2 -> "SP2", tx@SP -> "SP")

    A leading name fragment like 'A_' is deliberately NOT treated as a speaker:
    in these corpora it's a FLEx text/export prefix, not a participant, so it
    must not invent a who="A".  Real multiple speakers always appear as distinct
    @SPx suffixes (or PARTICIPANT values).
    """
    t = tier_map.get(tid)
    if t is None:
        return ""
    part = t.get("PARTICIPANT")
    if part:
        return part.strip()
    m = re.search(r"@(\w+)$", tid or "")
    return m.group(1) if m else ""


# ─── Media descriptors ────────────────────────────────────────────────────────

def media_basenames(path):
    """
    Return the bare filenames of the AUDIO media linked in an EAF, in document
    order (path and file:// prefix stripped).  Used to fill <SOUNDFILE href=...>.
    Prefers RELATIVE_MEDIA_URL, falls back to MEDIA_URL.
    """
    try:
        root = ET.parse(path).getroot()
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

    def print_node(tid, prefix=""):
        tier  = tier_map[tid]
        ltype = tier.get("LINGUISTIC_TYPE_REF", "")
        lang  = tier.get("LANG_REF") or tier.get("DEFAULT_LOCALE") or ""
        who   = _speaker_key(tid, tier_map)
        count = ann_count.get(tid, 0)
        line = (f"{prefix}+- {tid!r:30s} type={ltype!r:16s} "
                f"lang={lang!r:7s} who={who!r:6s} ({count} ann)")
        print(line)
        for kid in child_tiers.get(tid, []):
            print_node(kid, prefix + "   ")

    print()
    print("Tier tree")
    print("-" * 80)
    for root_tid in roots:
        print_node(root_tid)
    print()


# ─── Segment-tier auto-detection ──────────────────────────────────────────────

def detect_segment_tiers(tier_map, annotations):
    """
    Auto-detect the segment (time-aligned, parentless) tier(s).

    Strategy:
      1. Candidates: time-aligned root tiers with at least one annotation AND
         at least one child tier (standalone annotation tracks have 0 children).
      2. Group by speaker (PARTICIPANT, then @SPx suffix).
      3. Within each speaker group, keep only the candidate with the most
         child tiers — that is almost always the main segment tier.

    Assumption: at most ONE segment tier per speaker (the richest is kept).  A
    file that legitimately has two parallel root tiers for the same speaker
    would need them picked manually.
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


def _pick_one(prompt, tier_ids, required=False, default=None, allow_back=False):
    while True:
        print(f"\n{prompt}")
        for i, tid in enumerate(tier_ids, 1):
            mark = "  <- suggested" if tid == default else ""
            print(f"  {i:3d}. {tid}{mark}")

        parts = []
        if required and default:
            parts.append(f'Enter for "{default}"')
            parts.append("Select a number/name")
        elif required:
            parts.append("Select a number/name")
        elif default:
            # Optional field with a suggestion
            parts.append(f"Type 'y' for \"{default}\"")
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
            if required and default:
                return default
            if required:
                print("  This field is required — please make a selection.")
                continue
            return None  # optional → Enter skips, even when a suggestion exists

        low = raw.lower()
        if (not required) and default and low in ("y", "yes"):
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


def _pick_many(prompt, tier_ids, allow_back=False):
    print(f"\n{prompt}")
    for i, tid in enumerate(tier_ids, 1):
        print(f"  {i:3d}. {tid}")
    raw = input("Your choices (comma-separated numbers/names "
                "or Enter to skip): ").strip()
    if allow_back and raw in _BACK_TOKENS:
        raise _GoBack
    if not raw:
        return []
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
    return result


def _ask_lang_required(ttid, auto):
    """Translation language is mandatory; Enter accepts the auto-detected code."""
    while True:
        if auto:
            raw = input(f"  Language code for the translation '{ttid}' "
                        f"(Enter = \"{auto}\"): ").strip()
            return raw if raw else auto
        raw = input(f"  Language code for the translation '{ttid}' "
                    f"(e.g. en, fr): ").strip()
        if raw:
            return raw
        print("  A language code is required for translations — please type one.")


def _tier_lang(tid, tier_map):
    t = tier_map.get(tid)
    if t is None:
        return ""
    return t.get("LANG_REF") or t.get("DEFAULT_LOCALE") or ""

def _guess_lang(tid, tier_map):
    """Tier LANG_REF/DEFAULT_LOCALE first, else 'en'/'fr' if the name contains it."""
    lang = _tier_lang(tid, tier_map)
    if lang:
        return lang
    low = (tid or "").lower()
    if "en" in low:
        return "en"
    if "fr" in low:
        return "fr"
    return ""

def _run_flow(build_steps, state):
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
    """
    i = 0
    history = []
    while True:
        steps = build_steps(state)
        if i >= len(steps):
            break
        state["_at_start"] = (len(history) == 0)
        _label, fn = steps[i]
        try:
            result = fn(state)
        except _GoBack:
            if history:
                i = history.pop()
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
# Tier *names* vary wildly between annotators (syllabique / mots / gloses, or
# A_morph-gls-en, or phono / mot / morph).  What is stable is the
# LINGUISTIC_TYPE (ref, tx, mot, mb, ge, ps/rx, ft, …) and the parent/child
# structure.  So each role is matched as a substring against
# "<tier name> <linguistic type>" AND constrained structurally (descendant of a
# given tier, or direct child of it).  These are only *suggestions*; the user
# always confirms or overrides.
#
# Short tokens like "ge", "rx", "ps", "mb", "tx" are not typos: they are real
# FLEx/CorpAfroAs LINGUISTIC_TYPE abbreviations (gloss, PoS, morpheme, …) and
# are matched against the type string, not just the visible tier name.

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
        mirrored  = _mirror_speaker(prev, s["segment_tier"], s["who"], tier_set, transform)
        if not mirrored:
            s["_mirrored"] = False
            return _NOASK
        print(f"\n  '{s['segment_tier']}' looks like '{prev['segment_tier']}' with "
              f"only the speaker code changed.")
        print(f"  Proposed mapping for speaker {s['who']} (mirrors {prev['who']}):")
        _print_speaker_mapping(mirrored, indent="      ")
        if _yesno("  Re-use this mapping?", True, allow_back=True):
            mirrored["_mirrored"] = True
            state["speakers"][idx] = mirrored
        else:
            s["_mirrored"] = False

    def step_forms(state):
        s = _spk(state)
        if s.get("_mirrored"):
            return _NOASK
        seg = s["segment_tier"]
        tx_default = _best_tier(tier_ids, tier_map, ann_count, "tx",
                                under=seg, include_under=True)
        s["sentence"] = _pick_one(
            "Transcription tier  [XML: <FORM>]",
            tier_ids, required=True, default=tx_default, allow_back=True
        )
        kind = _ask("  Transcription type (e.g. phono, ortho)  "
                    "[XML: <FORM kindOf='...'>]", "", allow_back=True)
        forms = [{"tier": s["sentence"], "kind": kind or None}]
        while _yesno("Add another transcription line?", False, allow_back=True):
            ft = _pick_one("  Transcription tier  [XML: <FORM>]", tier_ids, allow_back=True)
            if not ft:
                break
            fk = _ask("  Transcription type (e.g. phono, ortho)  "
                      "[XML: <FORM kindOf='...'>]", "", allow_back=True)
            forms.append({"tier": ft, "kind": fk or None})
        s["forms"] = forms

    def step_transl(state):
        s = _spk(state)
        if s.get("_mirrored"):
            return _NOASK
        s["transl"] = _pick_many(
            "OPTIONAL — Translation tier(s)  [XML: <TRANSL xml:lang='...'>]",
            tier_ids, allow_back=True
        )
        langs = {}
        for ttid in s["transl"]:
            langs[ttid] = _ask_lang_required(ttid, _guess_lang(ttid, tier_map))
        s["transl_langs"] = langs

    def step_notes(state):
        s = _spk(state)
        if s.get("_mirrored"):
            return _NOASK
        s["notes"] = _pick_many(
            "OPTIONAL — Notes/comments tier(s)  [XML: <NOTE message='...'>]",
            tier_ids, allow_back=True
        )

    def step_word(state):
        s = _spk(state)
        if s.get("_mirrored"):
            return _NOASK
        s["word_form"] = _pick_one(
            "OPTIONAL — Word tier  [XML: <W><FORM>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "word",
                               under=s["segment_tier"]),
            allow_back=True
        )
        if not s["word_form"]:
            s["word_gls"] = None

    def step_word_gls(state):
        s = _spk(state)
        if s.get("_mirrored") or not s.get("word_form"):
            s.setdefault("word_gls", None)
            return _NOASK
        s["word_gls"] = _pick_one(
            "OPTIONAL — Word-level gloss tier  [XML: <W><TRANSL>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "gls",
                               child_of=s["word_form"]),
            allow_back=True
        )

    def step_word_form_visible(state):
        # Global setting, asked once (idx 0) right after the word tier is chosen.
        s = _spk(state)
        if s.get("_mirrored") or not s.get("word_form"):
            return _NOASK
        state["word_form_visible"] = _yesno(
            "Show the word form in the output?  [XML: <W><FORM>]", True,
            allow_back=True
        )

    def step_morph(state):
        s = _spk(state)
        if s.get("_mirrored"):
            return _NOASK
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

    def step_morph_gls(state):
        s = _spk(state)
        if s.get("_mirrored") or not s.get("morph_form"):
            return _NOASK
        s["morph_gls"] = _pick_one(
            "OPTIONAL — Morpheme gloss tier  [XML: <M><TRANSL>]",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "gls",
                               under=s["morph_form"]),
            allow_back=True
        )

    def step_morph_gls_lang(state):
        s = _spk(state)
        if s.get("_mirrored") or not s.get("morph_form") or not s.get("morph_gls"):
            s.setdefault("morph_gls_lang", "")
            return _NOASK
        s["morph_gls_lang"] = _ask(
            "  Language code for the morpheme gloss (Enter for none)", "",
            allow_back=True
        )

    def step_morph_pos(state):
        s = _spk(state)
        if s.get("_mirrored") or not s.get("morph_form"):
            return _NOASK
        s["morph_pos"] = _pick_one(
            "OPTIONAL — Part-of-speech tier (will be appended to each morpheme form)",
            tier_ids,
            default=_best_tier(tier_ids, tier_map, ann_count, "pos",
                               under=s["morph_form"]),
            allow_back=True
        )
        if not s["morph_pos"]:
            s["morph_pos_sep"] = ""

    def step_morph_pos_sep(state):
        s = _spk(state)
        if s.get("_mirrored") or not s.get("morph_form") or not s.get("morph_pos"):
            s.setdefault("morph_pos_sep", "")
            return _NOASK
        s["morph_pos_sep"] = _ask(
            "  Separator between morpheme and PoS (e.g.  :  or  _)", ":",
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
        if idx == 0:
            steps += [(p + "word_form_visible", step_word_form_visible)]
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
    reference in `prev`.  Returns None if the transform doesn't reproduce the
    segment tier or any mapped tier is missing from the file.
    """
    if transform is None or transform(prev["segment_tier"]) != seg2:
        return None

    def m(t):
        return transform(t) if t else t

    new = {
        "who":            who2,
        "segment_tier":   seg2,
        "sentence":       m(prev.get("sentence")),
        "forms":          [{"tier": m(fm["tier"]), "kind": fm.get("kind")}
                           for fm in (prev.get("forms") or [])],
        "transl":         [m(t) for t in (prev.get("transl") or [])],
        "transl_langs":   {m(t): (prev.get("transl_langs") or {}).get(t, "")
                           for t in (prev.get("transl") or [])},
        "notes":          [m(t) for t in (prev.get("notes") or [])],
        "word_form":      m(prev.get("word_form")),
        "word_gls":       m(prev.get("word_gls")),
        "morph_form":     m(prev.get("morph_form")),
        "morph_gls":      m(prev.get("morph_gls")),
        "morph_gls_lang": prev.get("morph_gls_lang", ""),
        "morph_pos":      m(prev.get("morph_pos")),
        "morph_pos_sep":  prev.get("morph_pos_sep", ""),
    }

    refs = [new["sentence"], new["word_form"], new["word_gls"],
            new["morph_form"], new["morph_gls"], new["morph_pos"]]
    refs += [fm["tier"] for fm in new["forms"]]
    refs += new["transl"] + new["notes"]
    for r in refs:
        if r and r not in tier_set:
            return None
    return new


# ─── Interactive config builder ───────────────────────────────────────────────

def interactive_config(tier_map, annotations, stem, directory_mode=False):
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
        state["text_id"] = _ask("Document identifier", stem)

    def step_object_lang(state):
        while True:
            val = _ask(
                "ISO 639-3 code of the object language  [XML: xml:lang='...']", "",
                allow_back=not state.get("_at_start")
            )
            if val:
                state["object_lang"] = val
                return
            print("  A language code is required — please type one.")

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
            print("\nDetected segment tier(s) (these set timing, IDs and speakers):")
            for t in auto_seg_tiers:
                who = _speaker_key(t, tier_map) or "(single speaker)"
                print(f"   - {t}   speaker={who}")
            if _yesno("Use these?", True, allow_back=ab):
                seg = list(auto_seg_tiers)
            else:
                seg = _pick_many(
                    "Pick the segment tier(s) (one per speaker):", tier_ids
                ) or list(auto_seg_tiers)
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
                "Pick the segment tier(s) manually (one per speaker):",
                tier_ids, allow_back=ab
            )
            if not seg:
                seg = [_pick_one("REQUIRED — Segment tier", tier_ids, required=True)]
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

    state = _run_flow(build_steps, {})

    # ── assemble the config ───────────────────────────────────────────────────
    cfg = {
        "text_id":           state.get("text_id") if not directory_mode else None,
        "doctype":           state.get("doctype", "text"),
        "object_lang":       state.get("object_lang", ""),
        "word_form_visible": state.get(
            "word_form_visible",
            any(sp.get("word_form") for sp in state.get("speakers", []))
        ),
        "speakers":          [],
    }
    for sp in state.get("speakers", []):
        sp.pop("_mirrored", None)
        sp.setdefault("forms", [{"tier": sp.get("sentence"), "kind": None}])
        sp.setdefault("transl", [])
        sp.setdefault("transl_langs", {})
        sp.setdefault("notes", [])
        sp.setdefault("word_form", None)
        sp.setdefault("word_gls", None)
        sp.setdefault("morph_form", None)
        sp.setdefault("morph_gls", None)
        sp.setdefault("morph_gls_lang", "")
        sp.setdefault("morph_pos", None)
        sp.setdefault("morph_pos_sep", "")
        cfg["speakers"].append(sp)

    _show_config_summary(cfg)

    if not directory_mode:
        _save_config_interactive(cfg)

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


def _show_config_summary(cfg):
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Output type    : {cfg.get('doctype')}")
    if cfg.get("text_id"):
        print(f"  Identifier     : {cfg.get('text_id')}")
    print(f"  Object lang    : {cfg.get('object_lang')}")
    print(f"  Word form shown: {cfg.get('word_form_visible')}")
    for spk in cfg.get("speakers") or []:
        label = f"Speaker {spk['who']}" if spk.get("who") else "Speaker"
        print(f"\n  {label}  (segment tier: {spk['segment_tier']})")
        _print_speaker_mapping(spk, indent="    ")
    print()


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


def _save_config_interactive(cfg):
    """Prompt for a save path and retry on failure so selections survive."""
    while True:
        save_path = input(
            "\nSave these choices to reuse next time?\n"
            "(file name ending in .json, or Enter to skip): "
        ).strip()
        if not save_path:
            return
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
            file_cfg = dict(cfg)
            file_cfg["text_id"] = path.stem
            if _write_config(file_cfg, str(folder / (path.stem + ".json"))):
                n += 1
    print(f"Saved {n} config(s) to {folder}/")
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
            m_val = annotations[mid]["value"].strip("-")

            if pos_tid:
                for pid in collect_descendants(mid, pos_tid, children, annotations):
                    pv = annotations[pid]["value"].strip("-")
                    if pv:
                        m_val = m_val + pos_sep + pv
                        break

            gloss = ""
            if gtid:
                for gid in collect_descendants(mid, gtid, children, annotations):
                    if annotations[gid]["value"]:
                        gloss = annotations[gid]["value"].strip("-")
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
    lang    = cfg.get("object_lang", "")
    text_id = cfg.get("text_id", "text")
    doctype = cfg.get("doctype", "text")
    word_form_visible = cfg.get("word_form_visible", True)

    is_wordlist = (doctype == "wordlist")
    root_tag = "WORDLIST" if is_wordlist else "TEXT"
    unit_tag = "W"        if is_wordlist else "S"

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<!DOCTYPE {root_tag} SYSTEM "https://cocoon.huma-num.fr/schemas/Archive.dtd">',
    ]
    lang_attr = f' xml:lang="{lang}"' if lang else ""
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
    for i, s in enumerate(segments, 1):
        raw = (s["id"] or "").strip()
        # Prefer the segment's own integer value as the id (S5 / W5); fall back
        # to a sequential number.  Guarantee uniqueness: a duplicate integer id
        # (e.g. two speakers whose segnum both restart at 1) drops to the next
        # free sequential id, since Pangloss requires ids unique per document.
        unit_id = f"{unit_tag}{raw}" if raw.isdigit() else ""
        if not unit_id or unit_id in used_ids:
            seq += 1
            unit_id = f"{unit_tag}{seq}"
            while unit_id in used_ids:
                seq += 1
                unit_id = f"{unit_tag}{seq}"
        used_ids.add(unit_id)
        who_attr = f' who="{s["who"]}"' if s["who"] else ""
        lines.append(f'    <{unit_tag} id="{_esc_attr(unit_id)}"{who_attr}>')
        lines.append(
            f'        <AUDIO start="{ms_to_sec(s["ts1"])}" end="{ms_to_sec(s["ts2"])}"/>'
        )

        for f in s["forms"]:
            kattr = f' kindOf="{f["kind"]}"' if f.get("kind") else ""
            lines.append(f'        <FORM{kattr}>{_esc(f["text"])}</FORM>')

        for lang_key, text in s["transl"]:
            la = f' xml:lang="{lang_key}"' if lang_key else ""
            lines.append(f'        <TRANSL{la}>{_esc(text)}</TRANSL>')

        for note in s["notes"]:
            lines.append(f'        <NOTE message="{_esc_attr(note)}"/>')

        if is_wordlist:
            # A <W> entry may contain only <M> morphemes, never a nested <W>.
            # The entry's own transcription/gloss are the FORM/TRANSL above.
            for w in s["words"]:
                for m in w["morphs"]:
                    lines.append("        <M>")
                    if m["form"]:
                        lines.append(f'            <FORM>{_esc(m["form"])}</FORM>')
                    if m["gloss"]:
                        gl = f' xml:lang="{m["gloss_lang"]}"' if m.get("gloss_lang") else ""
                        lines.append(f'            <TRANSL{gl}>{_esc(m["gloss"])}</TRANSL>')
                    lines.append("        </M>")
        else:
            for w in s["words"]:
                has_form = word_form_visible and w["form"]
                # Keep a word if it shows a form/gloss OR groups at least one
                # morpheme; only words with nothing at all are dropped.
                if not (has_form or w["gls"] or w["morphs"]):
                    continue
                lines.append("        <W>")
                if word_form_visible and w["form"]:
                    lines.append(f'            <FORM>{_esc(w["form"])}</FORM>')
                if w["gls"]:
                    lines.append(f'            <TRANSL>{_esc(w["gls"])}</TRANSL>')
                for m in w["morphs"]:
                    lines.append("            <M>")
                    if m["form"]:
                        lines.append(f'                <FORM>{_esc(m["form"])}</FORM>')
                    if m["gloss"]:
                        gl = f' xml:lang="{m["gloss_lang"]}"' if m.get("gloss_lang") else ""
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


def _group_eafs(eaf_paths):
    """
    Parse all EAFs and group by tier structure.
    Returns list of (tier_map, annotations_sample, [paths]) sorted by group size desc.
    """
    groups = {}
    for path in eaf_paths:
        try:
            _, annotations, tier_map, _ = parse_eaf(str(path))
            sig = _tier_structure_signature(tier_map)
            if sig not in groups:
                groups[sig] = (tier_map, annotations, [])
            groups[sig][2].append(path)
        except Exception as e:
            print(f"Warning: could not parse {path.name}: {e}", file=sys.stderr)
    return sorted(groups.values(), key=lambda g: len(g[2]), reverse=True)


def _soundfile_for(path):
    """
    Pick the <SOUNDFILE> value for one EAF: the single linked audio file's
    basename.  With zero or several audio files, return None (empty HEADER) —
    and on several, warn, because there is no reliable way to know which one is
    the right recording (e.g. a stray second media left over from a template).
    """
    media = media_basenames(str(path))
    if len(media) == 1:
        return media[0]
    if len(media) >= 2:
        print(f"  WARNING {Path(path).name}: {len(media)} audio files linked "
              f"({', '.join(media)}); SOUNDFILE left empty — add the right one "
              f"by hand.", file=sys.stderr)
    return None


def _convert_one(path, cfg, output_dir):
    """Parse, build, filter, and write one EAF file."""
    _, annotations, tier_map, linguistic_types = parse_eaf(str(path))
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


def _interactive_configs(eaf_paths, dir_stem):
    """
    Group EAFs by tier structure and run the interview once per structure.
    Returns a list of (cfg, [paths]).
    """
    groups = _group_eafs(eaf_paths)
    configs = []
    multi = len(groups) > 1
    if multi:
        print(f"\nFound {len(groups)} different tier structure(s) across "
              f"{len(eaf_paths)} file(s).")
    for i, (tier_map, annotations, paths) in enumerate(groups, 1):
        if multi:
            names = ", ".join(p.name for p in paths[:4])
            if len(paths) > 4:
                names += f" … (+{len(paths)-4} more)"
            print(f"\n{'='*64}")
            print(f"Structure {i} — {len(paths)} file(s): {names}")
            print(f"{'='*64}")
        else:
            print(f"All {len(paths)} file(s) share the same tier structure.\n")
        cfg = interactive_config(tier_map, annotations, dir_stem, directory_mode=True)
        configs.append((cfg, paths))
    return configs


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
        print(f"\nConverting {len(mapping)} matched file(s)...")
        for path in eaf_paths:
            if path in mapping:
                _convert_one(path, mapping[path][1], output_dir)

    # Configs that matched no file are simply never used → ignored.

    if unmatched:
        print(f"\n{len(unmatched)} file(s) need a new config — let's set them up.")
        new_configs = _interactive_configs(unmatched, dir_stem)
        _save_configs_per_file(new_configs, config_dir)   # add to the same folder
        print(f"\nConverting {len(unmatched)} newly-configured file(s)...")
        for cfg, paths in new_configs:
            for path in paths:
                _convert_one(path, cfg, output_dir)


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
            print(f"\nSkipping {len(skipped)} file(s) — tier mismatch with config:")
            for path, missing in skipped:
                print(f"  {path.name}: missing tier(s): {', '.join(sorted(missing))}")

        print(f"\nConverting {len(to_process)} file(s)...")
        for path in to_process:
            _convert_one(path, cfg, output_dir)
        return

    # ── No config: interview (grouped by structure), save per file, convert ────
    print(f"Scanning {len(eaf_paths)} EAF file(s)...")
    configs = _interactive_configs(eaf_paths, dir_stem)
    _save_configs_per_file_interactive(configs)

    print(f"\nConverting {sum(len(p) for _, p in configs)} file(s)...")
    for cfg, paths in configs:
        for path in paths:
            _convert_one(path, cfg, output_dir)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert ELAN .eaf to Pangloss XML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input",  help="Input .eaf file or directory of .eaf files")
    parser.add_argument("output", nargs="?",
                        help="Output .xml file (single) or output directory (batch)")
    parser.add_argument("--inspect", action="store_true",
                        help="Print tier tree and exit (single file only)")
    parser.add_argument("--config", metavar="PATH",
                        help="A JSON config file, or (for a directory) a FOLDER of "
                             "configs matched to each EAF by tier structure")
    parser.add_argument("--lang",    metavar="CODE",
                        help="Object language ISO code (overrides config)")
    parser.add_argument("--text-id", metavar="ID",
                        help="Value for id='...' (overrides config, single file only)")
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
        parser.error("output file is required unless --inspect is given")

    stem = input_path.stem

    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.is_dir():
            # In single-file mode, accept a folder by matching '<stem>.json'.
            match = cfg_path / (stem + ".json")
            if not match.exists():
                print(f"No config '{match.name}' found in {cfg_path}", file=sys.stderr)
                sys.exit(1)
            cfg_path = match
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    else:
        cfg = interactive_config(tier_map, annotations, stem)

    if args.lang:
        cfg["object_lang"] = args.lang
    if args.text_id:
        cfg["text_id"] = args.text_id

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
        print(f"  Note: {len(segments) - len(nonempty)} segment(s) with no "
              f"transcription text skipped.")
    segments = nonempty
    print(f"{len(segments)} unit(s) found. Writing {args.output} ...")
    write_xml(segments, cfg, args.output)
    print("Done.")


if __name__ == "__main__":
    main()
