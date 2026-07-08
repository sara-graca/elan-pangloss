#!/usr/bin/env python3
"""
xml_to_eaf.py — Convert Pangloss/Cocoon XML files to the ELAN .eaf format.

Usage
-----
Inspect what's in a Pangloss XML file:
    python xml_to_eaf.py input.xml --inspect

Convert a single file (interactive):
    python xml_to_eaf.py input.xml output.eaf
    python xml_to_eaf.py input.xml output_dir/

Reuse a saved configuration:
    python xml_to_eaf.py input.xml output.eaf --config my.json
    python xml_to_eaf.py input.xml output_dir/ --config my.json

Convert a whole directory (interactive):
    python xml_to_eaf.py input_dir/ output_dir/

Convert a whole directory with saved configs:
    python xml_to_eaf.py input_dir/ output_dir/ --config my.json
    python xml_to_eaf.py input_dir/ output_dir/ --config configs_folder/

Single-file output
------------------
The second argument may be an .eaf FILE name, or a FOLDER.  
When it is a folder, the EAF is written into it under the XML's own
name with a .eaf extension (input.xml -> output_dir/input.eaf).

Config reuse for directories
----------------------------
--config can be a single JSON file (used for every file) or a FOLDER of configs.
With a folder, each XML is matched to the config that can represent its content. 
You confirm the proposed file->config mapping before converting. 
Files no config fits are set up interactively; unused configs are ignored.

Tier structure produced
-----------------------
The reference tier is time-aligned and holds the <S>/<W> id. Each transcription
(<FORM>) becomes its own tier underneath it. If a unit has several FORM lines
(e.g. phono, ortho, phone), you name a separate tier for each. Words and
morphemes attach under the primary transcription tier.

Both TEXT and WORDLIST documents work. If a document has multiple speakers
(units with who="..."), every tier is duplicated per speaker, e.g. "ref@SP1"
and "ref@SP2" instead of just "ref".

Tier types
----------------
By default each tier gets its own LINGUISTIC_TYPE named after the tier (its base
name, without the @SPx suffix): the reference tier uses a time-alignable type,
transcriptions/translations/glosses use Symbolic_Association, words/morphemes use 
Symbolic_Subdivision.  After choosing tier names you may optionally rename these types.
"""

import sys
import json
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

XML_NS = "http://www.w3.org/XML/1998/namespace"
_AUDIO_MIME = {"wav": "audio/x-wav", "mp3": "audio/mpeg", "flac": "audio/x-flac",
               "ogg": "audio/ogg", "aif": "audio/x-aiff", "aiff": "audio/x-aiff",
               "m4a": "audio/mp4"}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def sec_to_ms(s):
    try:
        return round(float(s) * 1000)
    except (TypeError, ValueError):
        return 0

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _esc_attr(text):
    return _esc(text).replace('"', "&quot;")


@lru_cache(maxsize=32)
def _parse_xml_root(path):
    """Parse an XML file once per run; grouping, matching and conversion share it."""
    return ET.parse(path).getroot()


# ─── Parse Pangloss XML ──────────────────────────────────────────────────────

def parse_xml(path):
    root = _parse_xml_root(str(path))
    is_wordlist = (root.tag == "WORDLIST")
    unit_tag = "W" if is_wordlist else "S"

    text_id     = root.get("id", "")
    object_lang = root.get(f"{{{XML_NS}}}lang", "")

    header = root.find("HEADER")
    sf_el = header.find("SOUNDFILE") if header is not None else None
    soundfile = sf_el.get("href", "").strip() if sf_el is not None else ""

    def forms_of(elem):
        return [(form.get("kindOf", ""), (form.text or "").strip())
                for form in elem.findall("FORM")]

    def transl_of(elem):
        return [(t.get(f"{{{XML_NS}}}lang", ""), (t.text or "").strip())
                for t in elem.findall("TRANSL")]

    def notes_of(elem):
        return [n.get("message", "").strip()
                for n in elem.findall("NOTE") if n.get("message", "").strip()]

    def morphs_of(elem):
        morphs = []
        for m_elem in elem.findall("M"):
            m_form   = (m_elem.findtext("FORM") or "").strip()
            m_gls_el = m_elem.find("TRANSL")
            m_gls    = (m_gls_el.text or "").strip() if m_gls_el is not None else ""
            morphs.append({"form": m_form, "gloss": m_gls})
        return morphs

    units = []
    for u_elem in root.findall(unit_tag):
        audio = u_elem.find("AUDIO")
        ts1 = sec_to_ms(audio.get("start", "0")) if audio is not None else 0
        ts2 = sec_to_ms(audio.get("end",   "0")) if audio is not None else 0
        unit = {
            "ts1": ts1, "ts2": ts2, "id": u_elem.get("id", ""),
            "forms": forms_of(u_elem),
            "transl": transl_of(u_elem), "notes": notes_of(u_elem),
            "who": u_elem.get("who", ""), "words": [], "morphs": [],
        }
        if is_wordlist:
            unit["morphs"] = morphs_of(u_elem)
        else:
            words = []
            for w_elem in u_elem.findall("W"):
                w_form   = (w_elem.findtext("FORM") or "").strip()
                w_gls_el = w_elem.find("TRANSL")
                w_gls    = (w_gls_el.text or "").strip() if w_gls_el is not None else ""
                words.append({"form": w_form, "gls": w_gls,
                              "morphs": morphs_of(w_elem)})
            unit["words"] = words
        units.append(unit)

    # never drop content silently: count what this importer does not carry
    ignored = defaultdict(int)
    for u_elem in root.findall(unit_tag):
        for w_elem in u_elem.findall("W") if not is_wordlist else []:
            ignored["word-level NOTE"] += len(w_elem.findall("NOTE"))
            ignored["extra word FORM"] += max(0, len(w_elem.findall("FORM")) - 1)
            ignored["extra word TRANSL"] += max(0, len(w_elem.findall("TRANSL")) - 1)
            for m_elem in w_elem.findall("M"):
                ignored["morpheme-level NOTE"] += len(m_elem.findall("NOTE"))
                ignored["extra morpheme FORM"] += max(0, len(m_elem.findall("FORM")) - 1)
                ignored["extra morpheme TRANSL"] += max(0, len(m_elem.findall("TRANSL")) - 1)
        for m_elem in u_elem.findall("M") if is_wordlist else []:
            ignored["morpheme-level NOTE"] += len(m_elem.findall("NOTE"))
            ignored["extra morpheme FORM"] += max(0, len(m_elem.findall("FORM")) - 1)
            ignored["extra morpheme TRANSL"] += max(0, len(m_elem.findall("TRANSL")) - 1)
    ignored = {k: n for k, n in ignored.items() if n}
    if ignored:
        detail = ", ".join(f"{n} {k}(s)" for k, n in sorted(ignored.items()))
        print(f"  WARNING {Path(path).name}: the following XML content is not "
              f"imported and will be absent from the EAF: {detail}.",
              file=sys.stderr)

    return text_id, object_lang, is_wordlist, soundfile, units


def _form_text(unit_forms, kind):
    for k, t in unit_forms:
        if k == kind:
            return t
    if kind == "" and unit_forms:
        return unit_forms[0][1]
    return ""


# ─── Content flags ───────────────────────────────────────────────────────────

def _content_flags(units, is_wordlist):
    form_kinds = []
    for u in units:
        for k, _ in u["forms"]:
            if k not in form_kinds:
                form_kinds.append(k)
    if not form_kinds:
        form_kinds = [""]

    transl_langs = list(dict.fromkeys(lang for u in units for lang, _ in u["transl"]))
    has_notes = any(u["notes"] for u in units)
    if is_wordlist:
        has_words = has_w_gls = False
        has_morphs = any(u["morphs"] for u in units)
        has_m_gls  = any(m["gloss"] for u in units for m in u["morphs"])
    else:
        has_words  = any(u["words"] for u in units)
        has_w_gls  = any(w["gls"] for u in units for w in u["words"])
        has_morphs = any(w["morphs"] for u in units for w in u["words"])
        has_m_gls  = any(m["gloss"] for u in units
                         for w in u["words"] for m in w["morphs"])
    speakers = [w for w in dict.fromkeys(u["who"] for u in units) if w]
    return dict(form_kinds=form_kinds, transl_langs=transl_langs, has_notes=has_notes,
                has_words=has_words, has_w_gls=has_w_gls, has_morphs=has_morphs,
                has_m_gls=has_m_gls, speakers=speakers)


def _structure_signature(is_wordlist, flags):
    return (is_wordlist, tuple(flags["form_kinds"]), tuple(flags["transl_langs"]),
            flags["has_notes"], flags["has_words"], flags["has_w_gls"],
            flags["has_morphs"], flags["has_m_gls"], tuple(sorted(flags["speakers"])))


# ─── Tier roles / linguistic types ───────────────────────────────────────────

# The ELAN constraint each role's LINGUISTIC_TYPE must carry:
#   None                     -> time-alignable root (the reference tier)
#   "Symbolic_Association"   -> 1-1 child (transcriptions, translations, glosses)
#   "Symbolic_Subdivision"   -> ordered children (words, morphemes)

def _config_tiers_with_constraints(cfg, is_wordlist):
    """
    Yield (tier_name, constraint) for every tier this config will create, in the
    order they appear in the EAF.  Speaker @SPx suffixes are NOT included — types
    are shared across speakers, exactly like the tier base names.
    """
    forms = cfg.get("forms") or [{"kind": "", "tier": "tx"}]
    primary = forms[0]
    ref_name = cfg.get("ref_tier") or "ref"
    ref_is_primary = (ref_name == primary["tier"])

    out = [(ref_name, None)]
    for i, fm in enumerate(forms):
        if ref_is_primary and i == 0:
            continue
        out.append((fm["tier"], "Symbolic_Association"))
    for tname in (cfg.get("transl_tiers") or {}).values():
        out.append((tname, "Symbolic_Association"))
    if cfg.get("notes_tier"):
        out.append((cfg["notes_tier"], "Symbolic_Subdivision"))
    if is_wordlist:
        if cfg.get("morph_tier"):
            out.append((cfg["morph_tier"], "Symbolic_Subdivision"))
            if cfg.get("morph_gls_tier"):
                out.append((cfg["morph_gls_tier"], "Symbolic_Association"))
    else:
        if cfg.get("word_tier"):
            out.append((cfg["word_tier"], "Symbolic_Subdivision"))
            if cfg.get("word_gls_tier"):
                out.append((cfg["word_gls_tier"], "Symbolic_Association"))
            if cfg.get("morph_tier"):
                out.append((cfg["morph_tier"], "Symbolic_Subdivision"))
                if cfg.get("morph_gls_tier"):
                    out.append((cfg["morph_gls_tier"], "Symbolic_Association"))
    # de-duplicate while keeping order (a tier name maps to one type)
    seen, uniq = set(), []
    for name, constraint in out:
        if name in seen:
            continue
        seen.add(name)
        uniq.append((name, constraint))
    return uniq


def _default_types(cfg, is_wordlist):
    """Standard mapping: each tier's type == the tier name."""
    return {name: name for name, _ in _config_tiers_with_constraints(cfg, is_wordlist)}


def _type_of(cfg, tier_name, is_wordlist):
    """Type to use for a given tier (base name)."""
    return (cfg.get("types") or {}).get(tier_name, tier_name)


# ─── Inspect ─────────────────────────────────────────────────────────────────

def inspect_xml(text_id, object_lang, is_wordlist, soundfile, units):
    f = _content_flags(units, is_wordlist)
    print()
    print(f"Document  : {'WORDLIST' if is_wordlist else 'TEXT'}")
    print(f"Text ID   : {text_id or '(none)'}")
    print(f"Language  : {object_lang or '(none)'}")
    print(f"Sound file: {soundfile or '(none)'}")
    print(f"Units     : {len(units)}  ({'words' if is_wordlist else 'sentences'})")
    if f["speakers"]:
        print(f"Speakers  : {', '.join(f['speakers'])}")
    print()

    def yn(b): return "yes" if b else "no"
    kinds = ", ".join((k or "(no kindOf)") for k in f["form_kinds"])
    tl = ", ".join(repr(l) for l in f["transl_langs"]) if f["transl_langs"] else "no"
    print("Content:")
    print(f"  Transcription forms    : {kinds}")
    print(f"  Translations           : {tl}")
    print(f"  Notes                  : {yn(f['has_notes'])}")
    if not is_wordlist:
        print(f"  Words                  : {yn(f['has_words'])}")
        print(f"  Word glosses           : {yn(f['has_w_gls'])}")
    print(f"  Morphemes              : {yn(f['has_morphs'])}")
    print(f"  Morpheme glosses       : {yn(f['has_m_gls'])}")
    print()


# ─── Interactive config ──────────────────────────────────────────────────────

def _ask(prompt, default=""):
    suffix = f"\n  (press Enter to use \"{default}\")" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _yesno(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    return default if not raw else raw in ("y", "yes")


def _default_form_name(kind, used):
    base = {"phono": "tx", "": "tx", "ortho": "ortho"}.get(kind, kind or "tx")
    name, i = base, 2
    while name in used:
        name = f"{base}_{i}"; i += 1
    used.add(name)
    return name


def _form_label(kind, idx):
    role = "Transcription (primary)" if idx == 0 else "Transcription"
    return f"{role} ({kind or 'no kindOf'})"


def _show_summary(cfg, is_wordlist=False):
    print()
    print("=" * 60)
    print("Summary of your choices")
    print("=" * 60)
    print(f"  Segment/reference tier         : {cfg.get('ref_tier') or 'ref'}"
          f"  (time-aligned)")
    for idx, fm in enumerate(cfg.get("forms") or []):
        print(f"  {_form_label(fm.get('kind',''), idx):34s}: {fm['tier']}")
    for lang, tname in (cfg.get("transl_tiers") or {}).items():
        label = lang if lang else "(no lang code)"
        print(f"  Translation ({label})                  : {tname}")
    def opt(k): return cfg.get(k) or "(none)"
    print(f"  Notes tier                      : {opt('notes_tier')}")
    print(f"  Word tier                       : {opt('word_tier')}")
    print(f"  Word gloss tier                 : {opt('word_gls_tier')}")
    print(f"  Morpheme tier                   : {opt('morph_tier')}")
    print(f"  Morpheme gloss tier             : {opt('morph_gls_tier')}")
    if cfg.get("types"):
        pairs = _config_tiers_with_constraints(cfg, is_wordlist)
        renamed = ", ".join(f"{name} -> {_type_of(cfg, name, is_wordlist)}"
                            for name, _ in pairs
                            if _type_of(cfg, name, is_wordlist) != name)
        print(f"  Linguistic types                : "
              f"{renamed if renamed else 'same as tier names'}")
    print()


def _predefined_cfg(is_wordlist, f):
    used = set()
    forms = [{"kind": k, "tier": _default_form_name(k, used)} for k in f["form_kinds"]]
    langs = f["transl_langs"]
    if len(langs) == 1:
        transl_map = {langs[0]: "ft"}
    else:
        transl_map = {lang: (f"ft_{lang}" if lang else "ft") for lang in langs}
    has_words = f["has_words"] and not is_wordlist
    both_gls = f["has_w_gls"] and f["has_m_gls"] and f["has_morphs"] and has_words
    morph_ok = f["has_morphs"] and (has_words or is_wordlist)
    return {
        "ref_tier":       "ref",
        "forms":          forms,
        "transl_tiers":   transl_map,
        "notes_tier":     "notes" if f["has_notes"] else None,
        "word_tier":      "word"  if has_words else None,
        "word_gls_tier":  ("ge_w" if both_gls else "ge")
                          if f["has_w_gls"] and has_words else None,
        "morph_tier":     "mb"    if morph_ok else None,
        "morph_gls_tier": ("ge_m" if both_gls else "ge")
                          if f["has_m_gls"] and morph_ok else None,
    }


def _speaker_suffix_note(f):
    """
    When the document has 2+ speakers, print (once, during tier naming) why each
    tier name will be duplicated per speaker and suffixed "@<speaker>".  The
    suffixes are the real who= codes from the XML, whatever they are.
    """
    spk = f.get("speakers") or []
    if len(spk) < 2:
        return
    suffixes = ", ".join(f"@{w}" for w in spk)
    print(f"NOTE: This document has {len(spk)} speakers ({', '.join(spk)}).")
    print(f"      Every tier below is created once per speaker:")
    print(f"      the names become {suffixes} versions "
          f"(tx -> {', '.join('tx'+s for s in ('@'+w for w in spk))}).")
    print()


def _show_predefined(is_wordlist, f):
    cfg = _predefined_cfg(is_wordlist, f)
    print()
    print("Standard tier names:")
    print()
    _speaker_suffix_note(f)
    print(f"  {cfg['ref_tier']:8s}  {'Utterance id':32s}  "
          f"[XML: <S id> / <W id>]  (time-aligned)")
    for idx, fm in enumerate(cfg["forms"]):
        xml = "<FORM>" if idx == 0 and not fm["kind"] else f"<FORM kindOf='{fm['kind']}'>"
        print(f"  {fm['tier']:8s}  {_form_label(fm['kind'], idx):32s}  [XML: {xml}]")
    for lang, tname in (cfg["transl_tiers"] or {}).items():
        print(f"  {tname:8s}  {('Free translation ('+(lang or '?')+')'):32s}  [XML: <TRANSL>]")
    for key, role, xml in (("notes_tier", "Notes / comments", "<NOTE>"),
                           ("word_tier", "Word segmentation", "<W>"),
                           ("word_gls_tier", "Word gloss", "<W><TRANSL>"),
                           ("morph_tier", "Morpheme break", "<M><FORM>"),
                           ("morph_gls_tier", "Morpheme gloss", "<M><TRANSL>")):
        if cfg.get(key):
            print(f"  {cfg[key]:8s}  {role:32s}  [XML: {xml}]")
    print()


def _custom_cfg(is_wordlist, f):
    has_words = f["has_words"] and not is_wordlist
    while True:
        cfg = {"ref_tier": _ask("Segment/reference tier name (time-aligned)", "ref")}
        used = set()
        forms = []
        for idx, kind in enumerate(f["form_kinds"]):
            default = _default_form_name(kind, used)
            name = _ask(_form_label(kind, idx) + " tier name", default)
            forms.append({"kind": kind, "tier": name})
        cfg["forms"] = forms
        cfg["transl_tiers"] = {}
        for lang in f["transl_langs"]:
            default_name = f"ft_{lang}" if lang else "ft"
            cfg["transl_tiers"][lang] = _ask(
                f"Translation tier name (language: {lang!r})" if lang
                else "Translation tier name (no language code in XML)", default_name)
        cfg["notes_tier"] = _ask("Notes tier name", "notes") if f["has_notes"] else None
        cfg["word_tier"]  = _ask("Word tier name", "word") if has_words else None
        both_gls = f["has_w_gls"] and f["has_m_gls"] and f["has_morphs"] and cfg["word_tier"]
        cfg["word_gls_tier"] = (
            _ask("Word gloss tier name", "ge_w" if both_gls else "ge")
            if f["has_w_gls"] and cfg["word_tier"] else None)
        morph_ok = f["has_morphs"] and (cfg["word_tier"] or is_wordlist)
        cfg["morph_tier"] = _ask("Morpheme tier name", "mb") if morph_ok else None
        cfg["morph_gls_tier"] = (
            _ask("Morpheme gloss tier name", "ge_m" if both_gls else "ge")
            if f["has_m_gls"] and cfg["morph_tier"] else None)

        _show_summary(cfg, is_wordlist)
        if _yesno("Does this look correct?", True):
            return cfg
        print("\nStarting over — please re-enter your choices.\n")


def _show_predefined_types(cfg, is_wordlist):
    print()
    print("Standard linguistic types (one per tier, named after the tier):")
    print()
    label = {None: "time-aligned", "Symbolic_Association": "Symbolic_Association",
             "Symbolic_Subdivision": "Symbolic_Subdivision"}
    for name, constraint in _config_tiers_with_constraints(cfg, is_wordlist):
        print(f"  tier {name:12s}  ->  type {name:12s}  ({label[constraint]})")
    print()


def _custom_types(cfg, is_wordlist):
    """Let the user rename each linguistic type; constraints stay fixed by role."""
    pairs = _config_tiers_with_constraints(cfg, is_wordlist)
    while True:
        types = {}
        for name, _constraint in pairs:
            types[name] = _ask(f"Type for tier '{name}'", name)
        cfg["types"] = types
        _show_summary(cfg, is_wordlist)
        if _yesno("Does this look correct?", True):
            return types
        print("\nStarting over — please re-enter the types.\n")


def _choose_types(cfg, is_wordlist):
    """Optional step: choose the LINGUISTIC_TYPE for the chosen tiers."""
    if not _yesno("\nDo you also want to choose the tier TYPES "
                  "(LINGUISTIC_TYPE)?", False):
        return  # leave cfg["types"] unset -> tier type == tier name
    _show_predefined_types(cfg, is_wordlist)
    choice = input(
        "Use these standard types, or choose custom ones? "
        "[standard / custom]: ").strip().lower()
    if choice in ("s", "standard", ""):
        cfg["types"] = _default_types(cfg, is_wordlist)
        _show_summary(cfg, is_wordlist)
        if _yesno("Does this look correct?", True):
            return
        print("\nSwitching to custom types.\n")
    _custom_types(cfg, is_wordlist)


def interactive_config(is_wordlist, units, save=True, stem=None):
    f = _content_flags(units, is_wordlist)
    _show_predefined(is_wordlist, f)
    choice = input(
        "Use these standard names, or choose custom names? [standard / custom]: "
    ).strip().lower()

    cfg = None
    if choice in ("s", "standard", ""):
        cfg = _predefined_cfg(is_wordlist, f)
        _show_summary(cfg, is_wordlist)
        if not _yesno("Does this look correct?", True):
            print("\nSwitching to custom names.\n")
            cfg = None
    if cfg is None:
        print("=" * 60)
        print("Tier naming")
        print("=" * 60)
        print("Choose a name for each tier that will be created in the EAF.\n")
        cfg = _custom_cfg(is_wordlist, f)

    _choose_types(cfg, is_wordlist)

    if save:
        _save_config_interactive(cfg, stem)
    return cfg


# ─── Config saving (crash-safe) ──────────────────────────────────────────────

def _write_config(cfg, path):
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
        print("  Your selections are NOT lost — type a different path (or Enter to skip).")
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
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  Could not create folder '{folder}': {e}")
        return 0
    n = 0
    for cfg, paths in configs:
        for path in paths:
            if _write_config(cfg, str(folder / (path.stem + ".json"))):
                n += 1
    print(f"Saved {n} config(s) to {folder}/")
    return n


def _save_configs_per_file_interactive(configs):
    while True:
        folder = input(
            "\nSave configurations to reuse next time?\n"
            "Enter a FOLDER name (created if needed) — one '<filename>.json' is\n"
            "saved per XML.  Press Enter to skip: "
        ).strip()
        if not folder:
            return
        if _save_configs_per_file(configs, folder):
            return


# ─── Build EAF ───────────────────────────────────────────────────────────────

def build_eaf(text_id, object_lang, is_wordlist, soundfile, units, cfg):
    _ann = [0]
    _ts  = [0]
    ts_slots = []

    def new_id():
        _ann[0] += 1
        return f"a{_ann[0]}"

    def ts_id(ms):
        # one TIME_SLOT per boundary (no dedup): even when two annotations meet
        # at the same millisecond they get separate slots, so a linguist can
        # later move one boundary in ELAN without dragging the other with it.
        _ts[0] += 1
        tsid = f"ts{_ts[0]}"
        ts_slots.append((tsid, ms))
        return tsid

    # A Pangloss text without <AUDIO> timing would put every sentence on a
    # zero-length interval at 0:00 — unusable in ELAN.  Synthesize consecutive
    # 1-second placeholder spans instead (and say so).
    if units and all(not u["ts1"] and not u["ts2"] for u in units):
        for i, u in enumerate(units):
            u["ts1"], u["ts2"] = i * 1000, (i + 1) * 1000
        print("  WARNING: no <AUDIO> timing in the XML — using placeholder "
              "times (1 s per unit); re-align in ELAN if needed.",
              file=sys.stderr)

    forms = cfg.get("forms") or [{"kind": "", "tier": "tx"}]
    primary = forms[0]
    ref_name = cfg.get("ref_tier") or "ref"
    # When the reference tier and the primary transcription are the same tier,
    # the time-aligned tier *holds* the transcription (e.g. some source EAFs have
    # no separate ref tier); we then carry the form text on the ref tier itself.
    ref_is_primary = (ref_name == primary["tier"])

    def collect(units_sub):
        b = {"ref": [], "form": defaultdict(list), "transl": defaultdict(list),
             "notes": [], "word": [], "word_gls": [], "morph": [], "morph_gls": []}
        for u in units_sub:
            ref_id = new_id()
            ref_val = (_form_text(u["forms"], primary.get("kind", ""))
                       if ref_is_primary else u.get("id", ""))
            b["ref"].append((ref_id, ts_id(u["ts1"]), ts_id(u["ts2"]), ref_val))

            has_breakdown = bool(u["morphs"] if is_wordlist else u["words"])
            primary_id = ref_id if ref_is_primary else None
            for i, fm in enumerate(forms):
                if ref_is_primary and i == 0:
                    continue  # the ref tier already carries the primary form
                txt = _form_text(u["forms"], fm.get("kind", ""))
                # always emit the primary form when it must anchor a word/morph
                # breakdown; emit any form when it has text.
                if txt or (i == 0 and has_breakdown):
                    aid = new_id()
                    b["form"][fm["tier"]].append((aid, ref_id, None, txt))
                    if i == 0:
                        primary_id = aid

            for lang, text in u["transl"]:
                if text:
                    b["transl"][lang].append((new_id(), ref_id, None, text))

            if cfg.get("notes_tier"):
                prev = None
                for note in u["notes"]:
                    aid = new_id(); b["notes"].append((aid, ref_id, prev, note)); prev = aid

            # words/morphemes hang under the primary transcription (as in the
            # source EAFs); fall back to ref only if there is no primary form.
            anchor = primary_id if primary_id is not None else ref_id

            def add_morphs(parent_id, morphs):
                if not cfg.get("morph_tier"):
                    return
                prev_m = None
                for m in morphs:
                    mid = new_id()
                    b["morph"].append((mid, parent_id, prev_m, m["form"])); prev_m = mid
                    if cfg.get("morph_gls_tier") and m["gloss"]:
                        b["morph_gls"].append((new_id(), mid, None, m["gloss"]))

            if is_wordlist:
                add_morphs(anchor, u["morphs"])
            elif cfg.get("word_tier"):
                prev_w = None
                for w in u["words"]:
                    w_id = new_id()
                    b["word"].append((w_id, anchor, prev_w, w["form"])); prev_w = w_id
                    if cfg.get("word_gls_tier") and w["gls"]:
                        b["word_gls"].append((new_id(), w_id, None, w["gls"]))
                    add_morphs(w_id, w["morphs"])
        return b

    distinct = [w for w in dict.fromkeys(u["who"] for u in units) if w]
    if len(distinct) <= 1:
        groups = [("", distinct[0] if distinct else "", units)]
    else:

        groups = [(f"@{w}", w, [u for u in units if u["who"] == w]) for w in distinct]
        empties = [u for u in units if not u["who"]]
        if empties:
            print(f"  Note: {len(empties)} unit(s) have no who= and go on "
                  f"un-suffixed tiers alongside the per-speaker ones.",
                  file=sys.stderr)
            groups.append(("", "", empties))

    speaker_blocks = [(suffix, who, collect(sub)) for suffix, who, sub in groups]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ANNOTATION_DOCUMENT AUTHOR="" DATE="{now}" FORMAT="3.0" VERSION="3.0"',
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '    xsi:noNamespaceSchemaLocation="http://www.mpi.nl/tools/elan/EAFv3.0.xsd">',
        '    <HEADER MEDIA_FILE="" TIME_UNITS="milliseconds">',
    ]
    if soundfile:
        ext = soundfile.lower().rsplit(".", 1)[-1]
        mime = _AUDIO_MIME.get(ext, "audio/x-wav")
        lines.append(
            f'        <MEDIA_DESCRIPTOR MEDIA_URL="{_esc_attr(soundfile)}"'
            f' MIME_TYPE="{mime}" RELATIVE_MEDIA_URL="./{_esc_attr(soundfile)}"/>'
        )
    lines.append(f'        <PROPERTY NAME="lastUsedAnnotationId">{_ann[0]}</PROPERTY>')
    lines.append('    </HEADER>')
    lines.append('    <TIME_ORDER>')
    for tsid, ms in ts_slots:
        lines.append(f'        <TIME_SLOT TIME_SLOT_ID="{tsid}" TIME_VALUE="{ms}"/>')
    lines.append('    </TIME_ORDER>')

    def lt_of(tier_base):
        return _type_of(cfg, tier_base, is_wordlist)

    def tier_header(tier_id, ltype, parent=None, lang_ref=None, participant=None):
        attrs = ""
        if lang_ref:
            attrs += f' LANG_REF="{_esc_attr(lang_ref)}"'
        attrs += f' LINGUISTIC_TYPE_REF="{_esc_attr(ltype)}"'
        if parent:
            attrs += f' PARENT_REF="{_esc_attr(parent)}"'
        if participant:
            attrs += f' PARTICIPANT="{_esc_attr(participant)}"'
        attrs += f' TIER_ID="{_esc_attr(tier_id)}"'
        return f'    <TIER{attrs}>'

    def write_alignable(tier_id, ltype, anns, lang_ref=None, participant=None):
        lines.append(tier_header(tier_id, ltype, lang_ref=lang_ref, participant=participant))
        for aid, ts1, ts2, value in anns:
            lines.extend([
                '        <ANNOTATION>',
                f'            <ALIGNABLE_ANNOTATION ANNOTATION_ID="{aid}"'
                f' TIME_SLOT_REF1="{ts1}" TIME_SLOT_REF2="{ts2}">',
                f'                <ANNOTATION_VALUE>{_esc(value)}</ANNOTATION_VALUE>',
                '            </ALIGNABLE_ANNOTATION>',
                '        </ANNOTATION>',
            ])
        lines.append('    </TIER>')

    def write_ref(tier_id, ltype, parent, anns, lang_ref=None, participant=None):
        lines.append(tier_header(tier_id, ltype, parent=parent,
                                 lang_ref=lang_ref, participant=participant))
        for aid, ref_id, prev_id, value in anns:
            prev_attr = f' PREVIOUS_ANNOTATION="{prev_id}"' if prev_id else ""
            lines.extend([
                '        <ANNOTATION>',
                f'            <REF_ANNOTATION ANNOTATION_ID="{aid}"'
                f' ANNOTATION_REF="{ref_id}"{prev_attr}>',
                f'                <ANNOTATION_VALUE>{_esc(value)}</ANNOTATION_VALUE>',
                '            </REF_ANNOTATION>',
                '        </ANNOTATION>',
            ])
        lines.append('    </TIER>')

    for suffix, who, b in speaker_blocks:
        part = who or None
        def nm(base):
            return base + suffix
        ref_tier = nm(ref_name)
        primary_tier = nm(primary["tier"])
        # time-aligned reference tier; if it itself carries the transcription,
        # the object language goes here.
        write_alignable(ref_tier, lt_of(ref_name), b["ref"],
                        lang_ref=(object_lang or None) if ref_is_primary else None,
                        participant=part)
        # transcription forms — reference children of ref; object language on the
        # primary transcription (unless the ref tier already is it).
        for i, fm in enumerate(forms):
            if ref_is_primary and i == 0:
                continue  # already written as the ref tier
            anns = b["form"].get(fm["tier"])
            if anns:
                write_ref(nm(fm["tier"]), lt_of(fm["tier"]), ref_tier, anns,
                          lang_ref=(object_lang or None) if i == 0 else None,
                          participant=part)
        for lang, tname in (cfg.get("transl_tiers") or {}).items():
            if b["transl"][lang]:
                write_ref(nm(tname), lt_of(tname), ref_tier, b["transl"][lang],
                          lang_ref=lang or None, participant=part)
        if cfg.get("notes_tier") and b["notes"]:
            write_ref(nm(cfg["notes_tier"]), lt_of(cfg["notes_tier"]), ref_tier,
                      b["notes"], participant=part)
        # words/morphemes hang under the primary transcription
        if is_wordlist:
            if cfg.get("morph_tier") and b["morph"]:
                write_ref(nm(cfg["morph_tier"]), lt_of(cfg["morph_tier"]), primary_tier,
                          b["morph"], participant=part)
                if cfg.get("morph_gls_tier") and b["morph_gls"]:
                    write_ref(nm(cfg["morph_gls_tier"]), lt_of(cfg["morph_gls_tier"]),
                              nm(cfg["morph_tier"]), b["morph_gls"], participant=part)
        elif cfg.get("word_tier") and b["word"]:
            word_name = nm(cfg["word_tier"])
            write_ref(word_name, lt_of(cfg["word_tier"]), primary_tier, b["word"],
                      participant=part)
            if cfg.get("word_gls_tier") and b["word_gls"]:
                write_ref(nm(cfg["word_gls_tier"]), lt_of(cfg["word_gls_tier"]),
                          word_name, b["word_gls"], participant=part)
            if cfg.get("morph_tier") and b["morph"]:
                write_ref(nm(cfg["morph_tier"]), lt_of(cfg["morph_tier"]), word_name,
                          b["morph"], participant=part)
                if cfg.get("morph_gls_tier") and b["morph_gls"]:
                    write_ref(nm(cfg["morph_gls_tier"]), lt_of(cfg["morph_gls_tier"]),
                              nm(cfg["morph_tier"]), b["morph_gls"], participant=part)

    # ── LINGUISTIC_TYPE block: one per distinct type actually used ────────────
    emitted = []
    seen_types = set()
    for tier_base, constraint in _config_tiers_with_constraints(cfg, is_wordlist):
        tname = lt_of(tier_base)
        if tname in seen_types:
            continue
        seen_types.add(tname)
        emitted.append((tname, constraint))
    for tname, constraint in emitted:
        if constraint is None:
            lines.append(
                f'    <LINGUISTIC_TYPE GRAPHIC_REFERENCES="false"'
                f' LINGUISTIC_TYPE_ID="{_esc_attr(tname)}" TIME_ALIGNABLE="true"/>')
        else:
            lines.append(
                f'    <LINGUISTIC_TYPE CONSTRAINTS="{constraint}"'
                f' GRAPHIC_REFERENCES="false" LINGUISTIC_TYPE_ID="{_esc_attr(tname)}"'
                f' TIME_ALIGNABLE="false"/>')

    lang_codes = []
    if object_lang:
        lang_codes.append(object_lang)
    for lang in (cfg.get("transl_tiers") or {}):
        if lang and lang not in lang_codes:
            lang_codes.append(lang)
    for code in lang_codes:
        lid = _esc_attr(code)
        lines.append(f'    <LANGUAGE LANG_DEF="{lid}" LANG_ID="{lid}" LANG_LABEL="{lid}"/>')

    lines.extend([
        '    <CONSTRAINT DESCRIPTION="Time subdivision of parent annotation\'s'
        ' time interval, no time gaps allowed within this interval"'
        ' STEREOTYPE="Time_Subdivision"/>',
        '    <CONSTRAINT DESCRIPTION="Symbolic subdivision of a parent annotation.'
        ' Annotations refering to the same parent are ordered"'
        ' STEREOTYPE="Symbolic_Subdivision"/>',
        '    <CONSTRAINT DESCRIPTION="1-1 association with a parent annotation"'
        ' STEREOTYPE="Symbolic_Association"/>',
        '    <CONSTRAINT DESCRIPTION="Time alignable annotations within the parent'
        ' annotation\'s time interval, gaps are allowed"'
        ' STEREOTYPE="Included_In"/>',
        '</ANNOTATION_DOCUMENT>',
    ])
    return "\n".join(lines) + "\n"


# ─── Directory mode ──────────────────────────────────────────────────────────

def _convert_one(path, cfg, output_dir):
    text_id, object_lang, is_wordlist, soundfile, units = parse_xml(str(path))
    eaf = build_eaf(text_id, object_lang, is_wordlist, soundfile, units, cfg)
    out_path = Path(output_dir) / (path.stem + ".eaf")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(eaf)
    kind = "words" if is_wordlist else "sentences"
    print(f"  {path.name} -> {out_path.name}  ({len(units)} {kind})")


def _group_xmls(xml_paths):
    groups = {}
    for path in xml_paths:
        try:
            _, _, is_wordlist, _, units = parse_xml(str(path))
        except Exception as e:
            print(f"Warning: could not parse {path.name}: {e}", file=sys.stderr)
            continue
        sig = _structure_signature(is_wordlist, _content_flags(units, is_wordlist))
        if sig not in groups:
            groups[sig] = (is_wordlist, units, [])
        groups[sig][2].append(path)
    return sorted(groups.values(), key=lambda g: len(g[2]), reverse=True)


def _interactive_configs(xml_paths):
    groups = _group_xmls(xml_paths)
    configs = []
    multi = len(groups) > 1
    if multi:
        print(f"\nFound {len(groups)} different content shape(s) across "
              f"{len(xml_paths)} file(s).")
    for i, (is_wordlist, units, paths) in enumerate(groups, 1):
        if multi:
            names = ", ".join(p.name for p in paths[:4])
            if len(paths) > 4:
                names += f" … (+{len(paths)-4} more)"
            print(f"\n{'='*64}")
            print(f"Shape {i} — {len(paths)} file(s): {names}")
            print(f"{'='*64}")
        else:
            print(f"All {len(paths)} file(s) share the same content shape.\n")
        cfg = interactive_config(is_wordlist, units, save=False)
        configs.append((cfg, paths))
    return configs


def _load_folder_configs(config_dir):
    out = []
    for p in sorted(config_dir.glob("*.json")):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  Skipping config {p.name}: {e}", file=sys.stderr)
            continue
        out.append((p.stem, cfg))
    return out


def _config_form_kinds(cfg):
    return {fm.get("kind", "") for fm in (cfg.get("forms") or [])}


def _config_covers(cfg, flags, is_wordlist):
    if not cfg.get("forms"):
        return False
    if not set(flags["form_kinds"]) <= _config_form_kinds(cfg):
        return False
    if not set(flags["transl_langs"]) <= set((cfg.get("transl_tiers") or {}).keys()):
        return False
    if flags["has_notes"] and not cfg.get("notes_tier"):
        return False
    if not is_wordlist and flags["has_words"] and not cfg.get("word_tier"):
        return False
    if flags["has_w_gls"] and not cfg.get("word_gls_tier"):
        return False
    if flags["has_morphs"]:
        if not cfg.get("morph_tier"):
            return False
        if not is_wordlist and not cfg.get("word_tier"):
            return False
    if flags["has_m_gls"] and not cfg.get("morph_gls_tier"):
        return False
    return True


def _warn_uncovered(cfg, flags, is_wordlist, name):
    """
    Print exactly what content of one file the config cannot represent (and
    would therefore be dropped).  Returns True if anything is uncovered.
    """
    problems = []
    missing_kinds = set(flags["form_kinds"]) - _config_form_kinds(cfg)
    if missing_kinds:
        problems.append("FORM kind(s) " + ", ".join(repr(k or "(no label)")
                                                    for k in sorted(missing_kinds)))
    missing_langs = set(flags["transl_langs"]) - set((cfg.get("transl_tiers") or {}).keys())
    if missing_langs:
        problems.append("translation language(s) "
                        + ", ".join(repr(l or "(no lang)") for l in sorted(missing_langs)))
    if flags["has_notes"] and not cfg.get("notes_tier"):
        problems.append("NOTEs (no notes tier configured)")
    if not is_wordlist and flags["has_words"] and not cfg.get("word_tier"):
        problems.append("words (no word tier configured)")
    if flags["has_w_gls"] and not cfg.get("word_gls_tier"):
        problems.append("word glosses (no word-gloss tier configured)")
    if flags["has_morphs"] and not cfg.get("morph_tier"):
        problems.append("morphemes (no morpheme tier configured)")
    if flags["has_m_gls"] and not cfg.get("morph_gls_tier"):
        problems.append("morpheme glosses (no morpheme-gloss tier configured)")
    if problems:
        print(f"  WARNING {name}: this config drops content present in the "
              f"file: " + "; ".join(problems) + ".", file=sys.stderr)
    return bool(problems)


def _config_specificity(cfg, flags, is_wordlist):
    optional = [
        ("notes_tier", flags["has_notes"]),
        ("word_tier", flags["has_words"] and not is_wordlist),
        ("word_gls_tier", flags["has_w_gls"]),
        ("morph_tier", flags["has_morphs"]),
        ("morph_gls_tier", flags["has_m_gls"]),
    ]
    used = sum(1 for k, present in optional if cfg.get(k) and present)
    unused = sum(1 for k, present in optional if cfg.get(k) and not present)
    exact_forms = _config_form_kinds(cfg) == set(flags["form_kinds"])
    exact_lang = set((cfg.get("transl_tiers") or {}).keys()) == set(flags["transl_langs"])
    return (exact_forms, exact_lang, used, -unused)


def _propose_matches(xml_paths, folder_configs):
    mapping, unmatched = {}, []
    for path in xml_paths:
        try:
            _, _, is_wordlist, _, units = parse_xml(str(path))
        except Exception as e:
            print(f"  Could not read {path.name}: {e}", file=sys.stderr)
            unmatched.append(path)
            continue
        flags = _content_flags(units, is_wordlist)
        compatible = [(n, c) for (n, c) in folder_configs
                      if _config_covers(c, flags, is_wordlist)]
        if not compatible:
            unmatched.append(path)
            continue
        # A config named after the file wins outright (same rule as eaf_to_xml);
        # otherwise the most specific compatible config wins.
        compatible.sort(
            key=lambda t: (path.stem == t[0],
                           _config_specificity(t[1], flags, is_wordlist)),
            reverse=True)
        mapping[path] = compatible[0]
    return mapping, unmatched


def _confirm_and_adjust_mapping(xml_paths, mapping, unmatched, folder_configs):
    print("\nProposed config for each file (matched by content shape):")
    for path in xml_paths:
        if path in mapping:
            print(f"  {path.name:48s} ->  {mapping[path][0]}.json")
        else:
            print(f"  {path.name:48s} ->  (no match — configure interactively)")
    if _yesno("\nIs this correct?", True):
        return mapping, unmatched

    names = [n for n, _ in folder_configs]
    cfg_by_name = {n: c for n, c in folder_configs}
    print("\nFor each file: type a config number, Enter to keep the proposal, "
          "'i' to configure it interactively, or 's' to skip it.")
    new_map, new_unmatched = {}, []
    for path in xml_paths:
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
            pass
        elif raw.isdigit() and 1 <= int(raw) <= len(names):
            n = names[int(raw) - 1]
            new_map[path] = (n, cfg_by_name[n])
        else:
            print("  (unrecognized — keeping the proposal)")
            (new_map.__setitem__(path, cur) if cur else new_unmatched.append(path))
    return new_map, new_unmatched


def _convert_with_config_folder(xml_paths, output_dir, config_dir):
    folder_configs = _load_folder_configs(config_dir)
    mapping, unmatched = _propose_matches(xml_paths, folder_configs)
    if folder_configs:
        mapping, unmatched = _confirm_and_adjust_mapping(
            xml_paths, mapping, unmatched, folder_configs)

    if mapping:
        print(f"\nConverting {len(mapping)} matched file(s)...")
        for path in xml_paths:
            if path in mapping:
                _convert_one(path, mapping[path][1], output_dir)

    if unmatched:
        print(f"\n{len(unmatched)} file(s) have no matching config in the folder:")
        for path in unmatched:
            print(f"  {path.name}")
        if _yesno("Configure these by hand? (n = skip them)", False):
            new_configs = _interactive_configs(unmatched)
            _save_configs_per_file(new_configs, config_dir)
            print(f"\nConverting {len(unmatched)} newly-configured file(s)...")
            for cfg, paths in new_configs:
                for path in paths:
                    _convert_one(path, cfg, output_dir)
        else:
            print(f"Skipping {len(unmatched)} unmatched file(s).")


def process_directory(xml_dir, output_dir, config=None):
    xml_paths = sorted(Path(xml_dir).glob("*.xml"))
    if not xml_paths:
        print(f"No .xml files found in {xml_dir}", file=sys.stderr)
        sys.exit(1)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    cfg_path = Path(config) if config else None
    if cfg_path and not cfg_path.exists():
        print(f"Config path not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    if cfg_path and cfg_path.is_dir():
        _convert_with_config_folder(xml_paths, output_dir, cfg_path)
        return

    if cfg_path and cfg_path.is_file():
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        skipped, to_process = [], []
        for path in xml_paths:
            try:
                _, _, is_wordlist, _, units = parse_xml(str(path))
            except Exception as e:
                print(f"  Could not read {path.name}: {e}", file=sys.stderr)
                skipped.append(path)
                continue
            if _config_covers(cfg, _content_flags(units, is_wordlist), is_wordlist):
                to_process.append(path)
            else:
                _warn_uncovered(cfg, _content_flags(units, is_wordlist),
                                is_wordlist, path.name)
                skipped.append(path)
        if skipped:
            print(f"\n{len(skipped)} file(s) don't match the config "
                  f"'{cfg_path.name}':")
            for path in skipped:
                print(f"  {path.name}")
            hand = _yesno("Configure these by hand? (n = skip them)", False)
        else:
            hand = False

        print(f"\nConverting {len(to_process)} file(s) with '{cfg_path.name}'...")
        for path in to_process:
            _convert_one(path, cfg, output_dir)

        if hand:
            print(f"\nConfiguring {len(skipped)} remaining file(s) by hand "
                  f"(grouped by content shape)...")
            extra = _interactive_configs(skipped)
            _save_configs_per_file_interactive(extra)
            for extra_cfg, paths in extra:
                for path in paths:
                    _convert_one(path, extra_cfg, output_dir)
        return

    print(f"Scanning {len(xml_paths)} XML file(s)...")
    configs = _interactive_configs(xml_paths)
    _save_configs_per_file_interactive(configs)
    print(f"\nConverting {sum(len(p) for _, p in configs)} file(s)...")
    for cfg, paths in configs:
        for path in paths:
            _convert_one(path, cfg, output_dir)


# ─── Single-file output resolution ───────────────────────────────────────────

def _resolve_single_output(output_arg, input_path):
    """
    Resolve the output argument for single-file mode.

    The argument may be an .eaf FILE name or a FOLDER:
      - An existing directory, or a path ending in a path separator, is treated
        as a folder; the EAF is written into it as "<xml stem>.eaf".
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
        return out / (input_path.stem + ".eaf")

    return out


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert Pangloss/Cocoon XML to ELAN .eaf format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input",  help="Input XML file or directory of XML files")
    parser.add_argument("output", nargs="?",
                        help="Output .eaf file or a folder (single), or output "
                             "directory (batch)")
    parser.add_argument("--inspect", action="store_true",
                        help="Show contents and exit (single file only)")
    parser.add_argument("--config", metavar="PATH",
                        help="A JSON config file, or (for a directory) a FOLDER of "
                             "configs matched to each XML by content shape")
    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.is_dir():
        if not args.output:
            parser.error("output directory is required when input is a directory")
        process_directory(str(input_path), args.output, args.config)
        return

    text_id, object_lang, is_wordlist, soundfile, units = parse_xml(str(input_path))

    if args.inspect:
        inspect_xml(text_id, object_lang, is_wordlist, soundfile, units)
        return

    if not args.output:
        parser.error("output file or folder is required unless --inspect is given")

    # Resolve the output path up front — BEFORE the interview — so a folder
    # target becomes "<folder>/<xml stem>.eaf" now, never an IsADirectoryError
    # after the user has answered every prompt.
    out_path = _resolve_single_output(args.output, input_path)
    if out_path.suffix.lower() != ".eaf":
        print(f"  Note: output '{out_path}' does not end in .eaf (possible typo?) "
              f"— writing there anyway.", file=sys.stderr)

    if args.config:
        cfg_path = Path(args.config)
        if cfg_path.is_dir():
            match = cfg_path / (input_path.stem + ".json")
            if not match.exists():
                print(f"No config '{match.name}' found in {cfg_path}", file=sys.stderr)
                sys.exit(1)
            cfg_path = match
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        _warn_uncovered(cfg, _content_flags(units, is_wordlist), is_wordlist,
                        input_path.name)
    else:
        cfg = interactive_config(is_wordlist, units, stem=input_path.stem)

    eaf_content = build_eaf(text_id, object_lang, is_wordlist, soundfile, units, cfg)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(eaf_content)
    kind = "word" if is_wordlist else "sentence"
    print(f"Written {len(units)} {kind}(s) to {out_path}")


if __name__ == "__main__":
    main()