#!/usr/bin/env python3
"""
eaf_uniformize.py — Uniformize tier TYPES and tier NAMES across ELAN .eaf files.

The problem this solves
-----------------------
In a corpus built over the years, the "same" tier ends up with many names
(mot, word, wrd, A_word-txt-qaa ...) and many tier types (mots, word,
words ...).  Sometimes the opposite is true too: several DIFFERENT tiers
(e.g. word@sabi and morph@sabi) share ONE type and you want to give them
separate types.  This script lets you group variants and give each group one
single name/type of your choice, then writes clean .eaf files.  Everything
else in the files (annotations, timing, media links) is left untouched.

Usage
-----
Inspect what is in your files:
    python eaf_uniformize.py input_folder/ --inspect
    python eaf_uniformize.py file.eaf --inspect

Uniformize a folder of EAF files (interactive):
    python eaf_uniformize.py input_folder/ output_folder/

Uniformize a single file:
    python eaf_uniformize.py file.eaf output_folder/
    python eaf_uniformize.py file.eaf output_file.eaf

Reuse choices saved from a previous run:
    python eaf_uniformize.py input_folder/ output_folder/ --config my_choices.json

Two things you control
----------------------
1. Tier TYPE of each tier.  You build target types by picking, in any mix,
   existing TYPES (affects every tier that uses them) and/or individual tier
   NAMES (affects just those tiers, overriding their old type).  Picking types
   only = merging.  Picking tier names = you can SPLIT one type into several.
2. Tier NAMES.  You group base names and give each group one name.

Speaker suffixes
----------------
Names like  mot@SP1 / word@Maria  are understood: you group the BASE names and
the speaker part is kept automatically (mot@SP1 -> word@SP1).  The same holds
for type assignment (picking base 'word' covers word@SP1, word@SP2, ...).

Config format
-------------
    {
      "type_renames":     {"mots": "word", "morph": "mb"},
      "tier_type_assign": {"word": "word", "morph": "mb"},
      "tier_renames":     {"mot": "word", "wrd": "word"}
    }
`type_renames` maps an existing type -> target type (merge/rename).
`tier_type_assign` maps a tier BASE name (or an exact name containing '@')
-> target type; it wins over type_renames for those tiers (used to split).
`tier_renames` maps a base name (or exact '@' name) -> new base name.
"""

import sys
import copy
import fnmatch
import json
import argparse
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# Keep the conventional "xsi:" prefix on the schema attribute when rewriting.
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

_BACK_TOKENS = {"<", "back", "b"}
_NO_STEREO = "(none: time-aligned)"


# ─── Parsing / inventory ──────────────────────────────────────────────────────

def parse_tree(path):
    tree = ET.parse(str(path))
    return tree, tree.getroot()


def split_speaker(tier_id):
    """Separate a tier's base name from its speaker part, normalising the
    speaker to an '@speaker' SUFFIX so the rest of the tool can re-attach it.

        'mot@SP1'           -> ('mot', '@SP1')
        'SP1 transcription' -> ('transcription', '@SP1')
        'word'              -> ('word', '')

    Two speaker conventions are understood:
      * trailing  '@speaker'   (e.g. mot@SP1, word@Maria)
      * leading   'SPEAKER '   where the speaker is a short label made of
        letters/digits followed by a separator, then the base name
        (e.g. 'SP1 transcription', 'SPK2_word').  The base is whatever comes
        after the speaker label; the speaker is re-expressed as '@SP1' so it
        survives renaming just like the '@' convention.
    """
    tier_id = tier_id or ""
    # 1) trailing @speaker
    m = re.match(r"^(.*)(@[^@]+)$", tier_id)
    if m and m.group(1):
        return m.group(1), m.group(2)
    # 2) leading speaker label:  SP1 transcription / SPK2_word / S3-mot ...
    m = re.match(r"^(SP[A-Za-z]*\d+|S\d+)[ _-]+(.+)$", tier_id)
    if m:
        return m.group(2), "@" + m.group(1)
    return tier_id, ""


def scan_files(paths):
    """
    Cross-file inventory.

    types : {type_id: {"constraints": set, "tiers": set, "files": set}}
    bases : {base_name: {"full_names": set, "types": set, "files": set,
                         "ann_count": int}}
    trees : {path: (tree, root)}
    """
    types = defaultdict(lambda: {"constraints": set(), "tiers": set(),
                                 "files": set()})
    bases = defaultdict(lambda: {"full_names": set(), "types": set(),
                                 "files": set(), "ann_count": 0})
    trees = {}

    for path in paths:
        try:
            tree, root = parse_tree(path)
        except ET.ParseError as e:
            print(f"WARNING: could not parse {path.name}: {e} — file skipped.",
                  file=sys.stderr)
            continue
        trees[path] = (tree, root)

        for lt in root.findall("LINGUISTIC_TYPE"):
            ltid = lt.get("LINGUISTIC_TYPE_ID")
            if not ltid:
                continue
            types[ltid]["constraints"].add(lt.get("CONSTRAINTS") or _NO_STEREO)
            types[ltid]["files"].add(path.name)

        for tier in root.findall("TIER"):
            tid = tier.get("TIER_ID")
            if not tid:
                continue
            ltref = tier.get("LINGUISTIC_TYPE_REF") or ""
            base, _spk = split_speaker(tid)
            bases[base]["full_names"].add(tid)
            bases[base]["types"].add(ltref)
            bases[base]["files"].add(path.name)
            bases[base]["ann_count"] += len(tier.findall("ANNOTATION"))
            if ltref:
                types[ltref]["tiers"].add(tid)
                types[ltref]["files"].add(path.name)

    return types, bases, trees


def base_stereotypes(base, types, bases):
    """Stereotypes seen on the tiers grouped under one base name."""
    out = set()
    for ty in bases[base]["types"]:
        if ty:
            out |= types.get(ty, {}).get("constraints", set())
        else:
            out.add(_NO_STEREO)
    return out


# ─── Inspection display ───────────────────────────────────────────────────────

def _fmt_files(files, total):
    n = len(files)
    return "all files" if n == total else f"{n} file(s)"


def print_inventory(types, bases, n_files):
    print()
    print("=" * 72)
    print(f"TIER TYPES found across {n_files} file(s)")
    print("=" * 72)
    for ltid in sorted(types, key=str.lower):
        info = types[ltid]
        cons = ", ".join(sorted(info["constraints"]))
        tiers = ", ".join(sorted(info["tiers"])[:6])
        if len(info["tiers"]) > 6:
            tiers += f" … (+{len(info['tiers'])-6})"
        print(f"  {ltid!r:24s} stereotype: {cons}")
        used = f"used by tiers: {tiers}" if info["tiers"] \
            else "used by tiers: (none — unused type, ignored during grouping)"
        print(f"  {'':24s} {used}  "
              f"[{_fmt_files(info['files'], n_files)}]")
    print()
    print("=" * 72)
    print("TIER NAMES (grouped by base name — the part before '@speaker')")
    print("=" * 72)
    for base in sorted(bases, key=str.lower):
        info = bases[base]
        full = ", ".join(sorted(info["full_names"]))
        typ = ", ".join(sorted(t for t in info["types"] if t))
        print(f"  {base!r:24s} tiers: {full}")
        print(f"  {'':24s} type(s): {typ or '(none)'}   "
              f"({info['ann_count']} annotations, {_fmt_files(info['files'], n_files)})")
    print()


def print_tier_tree(root, name):
    tiers = {t.get("TIER_ID"): t for t in root.findall("TIER")}
    kids = defaultdict(list)
    roots = []
    for tid, t in tiers.items():
        p = t.get("PARENT_REF")
        if p:
            kids[p].append(tid)
        else:
            roots.append(tid)

    def show(tid, prefix=""):
        t = tiers[tid]
        n_ann = len(t.findall("ANNOTATION"))
        print(f"{prefix}+- {tid!r:32s} type={t.get('LINGUISTIC_TYPE_REF')!r:18s} "
              f"({n_ann} ann)")
        for k in kids.get(tid, []):
            show(k, prefix + "   ")

    print(f"\n{name}")
    print("-" * 72)
    for r in roots:
        show(r)


# ─── Naming a group ───────────────────────────────────────────────────────────

def _ask_new_name(kind, labels, default, forbid_at=False):
    """`kind` is a full label such as 'tier type' or 'tier name'."""
    while True:
        raw = input(
            f"  New {kind} for [{', '.join(labels)}]  (Enter = \"{default}\"): "
        ).strip()
        value = raw or default
        if not value:
            print("  A value is required.")
            continue
        if forbid_at and "@" in value:
            print("  Give the BASE name only (no '@speaker') — the speaker "
                  "part is kept automatically.")
            continue
        return value


def _print_assigned_summary(mapping, label):
    """One compact line per target: 'word  <-  mot, wrd'."""
    if not mapping:
        return
    by_new = defaultdict(list)
    for old, new in mapping.items():
        by_new[new].append(old)
    print(f"\n  {label} so far  ('<' undoes the last group; "
          f"regroup an item to move it):")
    for new in sorted(by_new, key=str.lower):
        print(f"    {new}  <-  {', '.join(sorted(by_new[new], key=str.lower))}")


def _expand_pattern(pattern, candidates):
    """Case-insensitive wildcard match of `pattern` (*, ?) over candidates."""
    pat = pattern.lower()
    return [c for c in candidates if fnmatch.fnmatchcase(c.lower(), pat)]


def _resolve_tokens(raw, display, all_items=None):
    """Numbers index into `display`; names match `all_items`; tokens with
    * or ? are wildcards matched (case-insensitively) against `all_items`."""
    all_items = all_items if all_items is not None else display
    chosen = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(display):
                chosen.append(display[idx])
            else:
                print(f"  Number {token} out of range — skipped.")
        elif "*" in token or "?" in token:
            hits = _expand_pattern(token, all_items)
            if hits:
                print(f"  {token} matched {len(hits)}: "
                      f"{', '.join(sorted(hits, key=str.lower)[:8])}"
                      f"{' …' if len(hits) > 8 else ''}")
                chosen.extend(hits)
            else:
                print(f"  {token} matched nothing — skipped.")
        elif token in all_items:
            chosen.append(token)
        else:
            print(f"  '{token}' matches nothing — skipped.")
    seen = set()
    return [c for c in chosen if not (c in seen or seen.add(c))]


# ─── Tier-NAME grouping (rename) ──────────────────────────────────────────────

def build_name_groups(items, described, initial=None):
    """Group base names and give each group one base name. Returns {old:new}.
    `initial` pre-loads an existing mapping (used when fixing collisions)."""
    mapping = dict(initial or {})
    # Seed the undo history with the pre-loaded groups (grouped by target name)
    # so that '<' can undo choices carried in from a previous step — otherwise
    # undo would say "Nothing to undo." for anything the user can actually see.
    history = []
    if initial:
        by_new = defaultdict(list)
        for old, new in initial.items():
            by_new[new].append(old)
        for new in sorted(by_new, key=str.lower):
            history.append(by_new[new])
    first = True
    while True:
        unassigned = [it for it in items if it not in mapping]
        print("\n── Group tier NAMES that mean the same thing (one group at a time) ──")
        if first:
            print("  Tip: wildcards work — e.g.  *word*  selects every name "
                  "containing 'word'.")
            first = False
        if not unassigned:
            print("  Every tier name has been grouped — press Enter to finish.")
        for i, it in enumerate(unassigned, 1):
            print(f"  {i:3d}. {described(it)}")
        _print_assigned_summary(mapping, "Grouped")
        raw = input("Numbers, names or *patterns* to group, comma-separated "
                    "(Enter = finished, '<' = undo last): ").strip()
        if raw in _BACK_TOKENS:
            if history:
                undone = history.pop()
                for it in undone:
                    mapping.pop(it, None)
                print(f"  Undid the group [{', '.join(sorted(undone, key=str.lower))}]"
                      f" — those names are ungrouped again.")
            else:
                print("  Nothing to undo.")
            continue
        if not raw:
            break
        group = _resolve_tokens(raw, unassigned, items)
        if not group:
            continue
        default = split_speaker(group[0])[0]
        new_name = _ask_new_name("tier name", group, default, forbid_at=True)
        for it in group:
            mapping[it] = new_name
        history.append(group)
        renamed = [it for it in group if it != new_name]
        if renamed:
            print(f"  ✔ {', '.join(renamed)}  ->  {new_name}")
        else:
            print("  ✔ (kept as is)")
    return {k: v for k, v in mapping.items() if k != v}


# ─── Tier-TYPE grouping (merge AND split) ─────────────────────────────────────

def build_type_groups(types, bases, n_files):
    """
    Build target types out of a mix of existing TYPES and tier base NAMES.
    Returns (type_renames, tier_type_assign).

    Menu items are tuples: ("type", type_id) or ("tier", base_name).  Picking a
    type routes into type_renames; picking a tier base routes into
    tier_type_assign, which overrides the type for exactly those tiers — so a
    single shared type can be split into several.
    """
    # Only offer types that a tier actually uses; an unused LINGUISTIC_TYPE
    # (e.g. left over from a template) would just be empty noise in the menu.
    items = [("type", t) for t in sorted(types, key=str.lower)
             if types[t]["tiers"]] + \
            [("tier", b) for b in sorted(bases, key=str.lower)]

    def described(item):
        kind, val = item
        if kind == "type":
            cons = ", ".join(sorted(types[val]["constraints"]))
            tl = ", ".join(sorted(types[val]["tiers"])[:4])
            more = " …" if len(types[val]["tiers"]) > 4 else ""
            return f"[type] {val!r:20s} {cons}   tiers: {tl}{more}"
        fn = ", ".join(sorted(bases[val]["full_names"]))
        cons = ", ".join(sorted(base_stereotypes(val, types, bases)))
        return f"[tier] {val!r:20s} {cons}   = {fn}"

    def group_stereos(group, target=None):
        stereos = set()
        for kind, val in group:
            if kind == "type":
                stereos |= types[val]["constraints"]
            elif kind == "tier":
                stereos |= base_stereotypes(val, types, bases)
            # tierfull: stereotype checked loosely; skip (rare exact-name case)
        if target and target in types:
            stereos |= types[target]["constraints"]
        return stereos

    def resolve(raw, display_types):
        """Numbers -> types in the displayed list. Names/wildcards -> types
        first, then tier base names, then exact full tier names."""
        chosen = []
        all_full = {fn for b in bases.values() for fn in b["full_names"]}
        type_ids = {t for k, t in items if k == "type"}
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(display_types):
                    chosen.append(display_types[idx])
                else:
                    print(f"  Number {token} out of range — skipped.")
            elif "*" in token or "?" in token:
                t_hits = _expand_pattern(token, type_ids)
                b_hits = _expand_pattern(token, set(bases))
                # A base that duplicates a matched type adds nothing; keep the
                # rest so the pattern can reach names for splitting.
                hits = [("type", t) for t in sorted(t_hits, key=str.lower)] + \
                       [("tier", b) for b in sorted(b_hits, key=str.lower)]
                if hits:
                    shown = [v for _k, v in hits]
                    print(f"  {token} matched {len(hits)}: "
                          f"{', '.join(shown[:8])}{' …' if len(hits) > 8 else ''}")
                    chosen.extend(hits)
                else:
                    print(f"  {token} matched nothing — skipped.")
            elif ("type", token) in items:
                chosen.append(("type", token))
            elif token in bases:
                chosen.append(("tier", token))
            elif token in all_full:
                chosen.append(("tierfull", token))
            else:
                print(f"  '{token}' matches no tier type or tier name — skipped.")
        seen = set()
        return [c for c in chosen if not (c in seen or seen.add(c))]

    type_renames, tier_type_assign = {}, {}
    history = []

    def is_assigned(item):
        kind, val = item
        return (kind == "type" and val in type_renames) or \
               (kind == "tier" and val in tier_type_assign)

    print("\n── STEP 1 of 2: choose the tier TYPE for your tiers ──")
    print("(In ELAN this is the tier's \"Linguistic Type\".)")
    print("Group the types that mean the same thing, one group at a time, and")
    print("give each group ONE name.  Press Enter when you are done (or right")
    print("away if the types are already fine).")
    print("\nThe word beside each type is ELAN's \"stereotype\":")
    print("  Symbolic_Subdivision = several ordered children per parent (words, morphemes)")
    print("  Symbolic_Association = exactly one child per parent (a gloss, a translation)")
    print("  Time_Subdivision     = children aligned to time inside the parent")
    print("  (none: time-aligned)  = a top-level tier with its own timing, no parent")
    print("Grouping different stereotypes is usually a mistake; you'll be warned.")
    print("\nAdvanced — SPLITTING one type into several: instead of a number, type")
    print("tier names (wildcards work:  *morph-txt*  ) to give just those tiers")
    print("a type of their own.  Type  tiers  to list all tier names.")

    while True:
        display_types = [it for it in items
                         if it[0] == "type" and not is_assigned(it)]
        print()
        if not display_types:
            print("  Every tier type has been dealt with — press Enter to finish.")
        for i, item in enumerate(display_types, 1):
            print(f"  {i:3d}. {described(item)}")
        combined = {**{k: v for k, v in type_renames.items()},
                    **{f"tiers '{k}'": v for k, v in tier_type_assign.items()}}
        _print_assigned_summary(combined, "Assigned")
        raw = input("Numbers, names or *patterns* to group "
                    "(Enter = finished, '<' = undo last, 'tiers' = list tier names): ").strip()

        if raw in _BACK_TOKENS:
            if history:
                _g, _t, keys = history.pop()
                for space, k in keys:
                    (type_renames if space == "type" else tier_type_assign).pop(k, None)
                print("  Undid the previous group.")
            else:
                print("  Nothing to undo.")
            continue
        if raw.lower() == "tiers":
            print("\n  Tier names in these files (usable in your selection):")
            for b in sorted(bases, key=str.lower):
                print(f"    {described(('tier', b))}")
            continue
        if not raw:
            break

        group = resolve(raw, display_types)
        if not group:
            continue

        stereos = group_stereos(group)
        if len(stereos) > 1:
            print(f"\n  ⚠ WARNING: this group mixes DIFFERENT stereotypes "
                  f"({' / '.join(sorted(stereos))}).")
            print("  Giving them one tier type can break the structure in ELAN.")
            if input("  Continue anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("  Group cancelled.")
                continue

        default = next((val for kind, val in group if kind == "type"), None)
        if default is None:
            default = split_speaker(group[0][1])[0]
        target = _ask_new_name("tier type", [v for _k, v in group], default)

        stereos2 = group_stereos(group, target=target)
        if len(stereos2) > 1 and stereos2 != stereos:
            print(f"\n  ⚠ WARNING: tier type '{target}' already exists with a "
                  f"different stereotype; result would mix "
                  f"{' / '.join(sorted(stereos2))}.")
            if input("  Continue anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("  Group cancelled.")
                continue

        keys = []
        for kind, val in group:
            if kind == "type":
                type_renames[val] = target
                keys.append(("type", val))
            else:  # tier or tierfull
                tier_type_assign[val] = target
                keys.append(("tier", val))
        history.append((group, target, keys))
        print(f"  ✔ tier type '{target}':  " + ", ".join(v for _k, v in group))

    type_renames = {k: v for k, v in type_renames.items() if k != v}
    return type_renames, tier_type_assign


# ─── Resolution shared by plan and apply ──────────────────────────────────────

def resolve_final_type(tid, orig_type, type_renames, tier_type_assign):
    """The LINGUISTIC_TYPE a tier should end up with (assignment wins)."""
    base, _spk = split_speaker(tid)
    if tid in tier_type_assign:
        return tier_type_assign[tid]
    if base in tier_type_assign:
        return tier_type_assign[base]
    if orig_type in type_renames:
        return type_renames[orig_type]
    return orig_type


def rename_tier_id(tid, tier_renames):
    """New TIER_ID (exact '@' rule wins over base-name rule)."""
    if tid in tier_renames:
        return tier_renames[tid]
    base, spk = split_speaker(tid)
    if base in tier_renames:
        return tier_renames[base] + spk
    return tid


# ─── Applying the changes to one file ─────────────────────────────────────────

def apply_to_file(root, type_renames, tier_type_assign, tier_renames):
    """
    Mutate one parsed EAF in place.  Returns (n_type_changes, n_name_changes,
    problems).  If problems is non-empty, nothing was changed.
    """
    tiers = root.findall("TIER")
    lt_elems = root.findall("LINGUISTIC_TYPE")
    orig_lt_by_id = {lt.get("LINGUISTIC_TYPE_ID"): lt for lt in lt_elems}

    # ── name collision check (before touching anything) ─────────────────────
    old_ids = [t.get("TIER_ID") for t in tiers]
    new_ids = [rename_tier_id(t, tier_renames) for t in old_ids]
    dup = defaultdict(list)
    for o, n in zip(old_ids, new_ids):
        dup[n].append(o)
    problems = [
        f"tiers {', '.join(olds)} would all be renamed to '{new}' — "
        f"tier names must stay unique in a file"
        for new, olds in dup.items() if len(olds) > 1
    ]
    if problems:
        return 0, 0, problems

    # ── compute each tier's final type ──────────────────────────────────────
    orig_type = {t.get("TIER_ID"): (t.get("LINGUISTIC_TYPE_REF") or "")
                 for t in tiers}
    final_type = {
        tid: resolve_final_type(tid, orig_type[tid], type_renames, tier_type_assign)
        for tid in orig_type
    }
    n_type_changes = sum(1 for tid in orig_type
                         if final_type[tid] != orig_type[tid])

    # ── decide the final set of LINGUISTIC_TYPE elements ────────────────────
    referenced = {t for t in final_type.values() if t}   # ignore empty refs
    rep_orig = {}
    for tid, ft in final_type.items():
        if ft:
            rep_orig.setdefault(ft, orig_type[tid])

    def template_for(t):
        if t in orig_lt_by_id:
            return orig_lt_by_id[t]
        src = rep_orig.get(t)
        if src and src in orig_lt_by_id:
            return orig_lt_by_id[src]
        el = ET.Element("LINGUISTIC_TYPE")
        el.set("LINGUISTIC_TYPE_ID", t)
        el.set("TIME_ALIGNABLE", "false")
        el.set("GRAPHIC_REFERENCES", "false")
        return el

    ordered, seen = [], set()
    for lt in lt_elems:
        tid = lt.get("LINGUISTIC_TYPE_ID")
        if tid in referenced and tid not in seen:
            ordered.append(tid)
            seen.add(tid)
    for tid in old_ids:
        ft = final_type[tid]
        if ft in referenced and ft not in seen:
            ordered.append(ft)
            seen.add(ft)

    new_lts = []
    for t in ordered:
        el = copy.deepcopy(template_for(t))
        el.set("LINGUISTIC_TYPE_ID", t)
        new_lts.append(el)

    children = list(root)
    first_idx = next((i for i, c in enumerate(children)
                      if c.tag == "LINGUISTIC_TYPE"), len(children))
    for lt in lt_elems:
        root.remove(lt)
    for offset, el in enumerate(new_lts):
        root.insert(first_idx + offset, el)

    # ── rewrite tiers: LINGUISTIC_TYPE_REF, TIER_ID, PARENT_REF ─────────────
    id_map = {o: n for o, n in zip(old_ids, new_ids) if o != n}
    for tier in tiers:
        tid = tier.get("TIER_ID")
        if final_type[tid]:
            tier.set("LINGUISTIC_TYPE_REF", final_type[tid])
        if tid in id_map:
            tier.set("TIER_ID", id_map[tid])
        pref = tier.get("PARENT_REF")
        if pref in id_map:
            tier.set("PARENT_REF", id_map[pref])

    return n_type_changes, len(id_map), []


def write_eaf(tree, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(out_path), encoding="UTF-8", xml_declaration=True)


# ─── Preview / summary ────────────────────────────────────────────────────────

def show_plan(types, bases, type_renames, tier_type_assign, tier_renames, n_files):
    print()
    print("=" * 72)
    print("Summary of the changes that will be made")
    print("=" * 72)

    if type_renames:
        print("\nTier TYPES — merged/renamed by type:")
        by_new = defaultdict(list)
        for o, n in type_renames.items():
            by_new[n].append(o)
        for new in sorted(by_new):
            print(f"  {', '.join(sorted(by_new[new]))}  ->  {new}")

    if tier_type_assign:
        print("\nTier TYPES — assigned by tier name (this is how a type is split):")
        by_new = defaultdict(list)
        for name, ttype in tier_type_assign.items():
            by_new[ttype].append(name)
        for new in sorted(by_new):
            covered = []
            for nm in sorted(by_new[new]):
                if "@" in nm:
                    covered.append(nm)
                else:
                    covered.extend(sorted(bases.get(nm, {}).get("full_names", {nm})))
            print(f"  tiers [{', '.join(covered)}]  ->  type '{new}'")

    if not type_renames and not tier_type_assign:
        print("\nTier TYPES: no changes.")

    if tier_renames:
        print("\nTier NAMES:")
        shown = set()
        for base in sorted(tier_renames):
            for full in sorted(bases.get(base, {}).get("full_names", {base})):
                new_full = rename_tier_id(full, tier_renames)
                if full != new_full and full not in shown:
                    print(f"  {full}  ->  {new_full}")
                    shown.add(full)
    else:
        print("\nTier NAMES: no changes.")
    print()


# ─── Config load/save ─────────────────────────────────────────────────────────

def save_config_interactive(type_renames, tier_type_assign, tier_renames):
    while True:
        raw = input("\nSave these choices to reuse on other folders?\n"
                    "(file name ending in .json, or Enter to skip): ").strip()
        if not raw:
            return
        try:
            p = Path(raw)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"type_renames": type_renames,
                           "tier_type_assign": tier_type_assign,
                           "tier_renames": tier_renames},
                          fh, indent=2, ensure_ascii=False)
            print(f"Saved to {p}")
            return
        except OSError as e:
            print(f"  Could not save to '{raw}': {e} — try another path "
                  f"(or Enter to skip).")


def load_config(path):
    with open(path, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    return (dict(cfg.get("type_renames") or {}),
            dict(cfg.get("tier_type_assign") or {}),
            dict(cfg.get("tier_renames") or {}))


# ─── Main flow ────────────────────────────────────────────────────────────────

def _precheck_collisions(trees, tier_renames):
    """{file_name: [messages]} for files where renaming would duplicate a
    TIER_ID.  Mirrors the check in apply_to_file, but before anything runs."""
    out = {}
    for path, (_tree, root) in trees.items():
        old_ids = [t.get("TIER_ID") for t in root.findall("TIER")]
        dup = defaultdict(list)
        for o in old_ids:
            dup[rename_tier_id(o, tier_renames)].append(o)
        msgs = [f"{' and '.join(olds)} would both become '{new}'"
                for new, olds in dup.items() if len(olds) > 1]
        if msgs:
            out[path.name] = msgs
    return out


def run(in_paths, out_dir, out_single_file, config, inspect_only, yes=False):
    types, bases, trees = scan_files(in_paths)
    n_files = len(trees)
    unreadable = [p for p in in_paths if p not in trees]
    if unreadable:
        print(f"\nNOTE: {len(unreadable)} file(s) could not be read and will be "
              f"left out:")
        for p in unreadable:
            print(f"  - {p.name}")
    if n_files == 0:
        print("No readable EAF file — nothing to do.", file=sys.stderr)
        sys.exit(1)

    if inspect_only:
        print_inventory(types, bases, n_files)
        for path, (_t, root) in trees.items():
            print_tier_tree(root, path.name)
        return

    if config:
        type_renames, tier_type_assign, tier_renames = load_config(config)
        full_names = {fn for b in bases.values() for fn in b["full_names"]}
        unused = [k for k in type_renames if k not in types]
        unused += [k for k in tier_type_assign
                   if k not in bases and k not in full_names]
        unused += [k for k in tier_renames
                   if k not in bases and k not in full_names]
        if unused:
            print(f"Note: config rules matching nothing here: {', '.join(unused)}")
    else:
        print_inventory(types, bases, n_files)
        type_renames, tier_type_assign = build_type_groups(types, bases, n_files)
        print("\n── STEP 2 of 2: the tier NAMES ──")
        print("You group BASE names; the '@speaker' part is kept automatically")
        print("(mot@SP1 -> word@SP1).  Press Enter now if you don't need to rename any.")
        name_items = sorted(bases, key=str.lower)
        name_described = lambda b: (
            f"{b!r:24s} = {', '.join(sorted(bases[b]['full_names']))}"
            f"   [{_fmt_files(bases[b]['files'], n_files)}]")
        tier_renames = build_name_groups(name_items, name_described)

        # Catch name collisions NOW, while the user can still fix them,
        # instead of silently skipping files at write time.
        while tier_renames:
            collisions = _precheck_collisions(trees, tier_renames)
            if not collisions:
                break
            print("\n  ⚠ PROBLEM: with these choices, some files would end up "
                  "with two tiers sharing one name:")
            for fname, msgs in collisions.items():
                for m in msgs:
                    print(f"    {fname}: {m}")
            ans = input("\n  Fix the groups now? (Yes = go back to the tier-name "
                        "step; No = those files will be skipped) [Y/n]: ").strip().lower()
            if ans in ("n", "no"):
                break
            tier_renames = build_name_groups(name_items, name_described,
                                             initial=tier_renames)

    if not (type_renames or tier_type_assign or tier_renames):
        print("\nNo changes requested — nothing will be written.")
        return

    show_plan(types, bases, type_renames, tier_type_assign, tier_renames, n_files)

    if not yes:
        if input("Write the modified file(s) now? [Y/n]: ").strip().lower() in ("n", "no"):
            print("Nothing written.")
            if not config:
                save_config_interactive(type_renames, tier_type_assign, tier_renames)
            return

    ok, failed = 0, 0
    for path, (tree, root) in trees.items():
        n_t, n_n, problems = apply_to_file(root, type_renames,
                                           tier_type_assign, tier_renames)
        if problems:
            failed += 1
            print(f"  ✗ {path.name}: NOT written:")
            for p in problems:
                print(f"      - {p}")
            continue
        out_path = Path(out_single_file) if out_single_file \
            else Path(out_dir) / path.name
        write_eaf(tree, out_path)
        ok += 1
        print(f"  ✔ {path.name} -> {out_path}   "
              f"({n_t} type change(s), {n_n} name change(s))")

    print(f"\nDone: {ok} file(s) written"
          + (f", {failed} skipped for name collisions." if failed else "."))

    if not config:
        save_config_interactive(type_renames, tier_type_assign, tier_renames)


def _clean_path(raw):
    """Tidy a path a user typed into the terminal."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    raw = raw.replace("\\ ", " ").strip()
    return raw


def _list_eaf(folder):
    """All .eaf files in a folder, case-insensitively (.eaf, .EAF, .Eaf ...)."""
    return sorted(p for p in Path(folder).iterdir()
                  if p.is_file() and p.suffix.lower() == ".eaf")


def _prompt_existing_path(prompt):
    while True:
        raw = _clean_path(input(prompt))
        if not raw:
            print("  Please type a path a file/folder in "
                  "(or press Ctrl+C to quit).")
            continue
        p = Path(raw).expanduser()
        if p.exists():
            return p
        print(f"  I couldn't find “{raw}”. Please check it and try again.")


def guided_setup():
    """Friendly question-and-answer setup for users not comfortable with the
    command line.  Returns (in_path, out_dir)."""
    print("=" * 72)
    print("  EAF tier uniformizer")
    print("=" * 72)
    print("This tool makes tidy copies of your ELAN files with consistent tier")
    print("names and types.  Your ORIGINAL files are never changed.\n")

    in_path = _prompt_existing_path(
        "1) Which EAF file or folder do you want to tidy up?\n   > ")

    if in_path.is_dir():
        default_out = in_path.parent / (in_path.name + "_uniformized")
    else:
        default_out = in_path.parent / "uniformized"

    print(f"\n2) Where should the tidied copies go?")
    raw = _clean_path(input(f"   Press Enter to use “{default_out}”, "
                            f"or type another folder:\n   > "))
    out_dir = Path(raw).expanduser() if raw else default_out
    print()
    return in_path, out_dir


def _main():
    parser = argparse.ArgumentParser(
        description="Uniformize tier names and tier types across ELAN .eaf files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="?",
                        help="An .eaf file or a folder of .eaf files")
    parser.add_argument("output", nargs="?",
                        help="Output folder (or output .eaf for a single input file)")
    parser.add_argument("--inspect", action="store_true",
                        help="Only show the tier types / names found, then exit")
    parser.add_argument("--config", metavar="PATH",
                        help="Reuse a saved JSON of choices (no questions asked)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the final confirmation before writing")
    args = parser.parse_args()

    # No input given: guide the user step by step (only if someone is there to
    # answer).  Non-interactive with no input keeps the normal error.
    if args.input is None:
        if sys.stdin.isatty():
            in_path, out_dir_path = guided_setup()
            in_paths, out_single, out_dir = _prepare_paths(in_path, str(out_dir_path))
            run(in_paths, out_dir, out_single, None, False)
            return
        parser.error("an input .eaf file or folder is required")

    in_path = Path(args.input).expanduser()
    if not in_path.exists():
        print(f"I couldn't find “{args.input}”. Please check the path.",
              file=sys.stderr)
        sys.exit(1)

    if args.config:
        cfg = Path(args.config)
        if not cfg.exists():
            print(f"Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        if cfg.is_dir():
            print(f"--config should be a single .json file, not a folder: "
                  f"{args.config}", file=sys.stderr)
            sys.exit(1)

    if not args.inspect and not args.output:
        # An input but no output, and someone is watching: just ask for it.
        if sys.stdin.isatty():
            if in_path.is_dir():
                default_out = in_path.parent / (in_path.name + "_uniformized")
            else:
                default_out = in_path.parent / "uniformized"
            raw = _clean_path(input(
                f"Where should the tidied copies go?\n"
                f"Press Enter for “{default_out}”, or type a folder: "))
            args.output = raw if raw else str(default_out)
        else:
            parser.error("an output folder (or output .eaf) is required unless "
                         "--inspect is given")

    in_paths, out_single, out_dir = _prepare_paths(in_path, args.output)

    if out_dir:
        in_abs = (in_path if in_path.is_dir() else in_path.parent).resolve()
        if Path(out_dir).resolve() == in_abs:
            print("The output folder is the same as the input folder — your "
                  "originals would be overwritten.\nPlease choose a different "
                  "output folder.", file=sys.stderr)
            sys.exit(1)

    run(in_paths, out_dir, out_single, args.config, args.inspect, yes=args.yes)


def _prepare_paths(in_path, output):
    """Resolve input into a list of EAF paths and decide output shape."""
    if in_path.is_dir():
        in_paths = _list_eaf(in_path)
        if not in_paths:
            print(f"No .eaf files found in {in_path}", file=sys.stderr)
            sys.exit(1)
        return in_paths, None, output
    # single file
    if output and output.lower().endswith(".eaf"):
        return [in_path], output, None
    return [in_path], None, output


def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("\n\nCancelled — nothing was written. Your originals are unchanged.")
    except SystemExit:
        raise
    except Exception as e:            # never show a raw traceback to a linguist
        print("\nSomething went wrong and the tool had to stop.")
        print(f"  Details (for a technician): {type(e).__name__}: {e}")
        print("  Your original files were NOT changed.")
    finally:
        # Keep the window open when launched by double-click, so the user can
        # read the result instead of it vanishing instantly.
        try:
            if sys.stdin.isatty() and sys.stdout.isatty():
                input("\nPress Enter to close this window.")
        except EOFError:
            pass


if __name__ == "__main__":
    main()