#!/usr/bin/env python3
"""
xml_to_eaf.py — Convert Pangloss XML files to ELAN .eaf format.

Usage
-----
Inspect what's in a Pangloss XML file:
    python xml_to_eaf.py input.xml --inspect

Interactive conversion (prompts for tier names):
    python xml_to_eaf.py input.xml output.eaf

Load a previously saved config:
    python xml_to_eaf.py input.xml output.eaf --config my.json

Tier structure produced
-----------------------
  phono tier       : ALIGNABLE_ANNOTATION  (time-aligned to the audio)
  translation(s)   : REF_ANNOTATION        Symbolic_Association  under phono
  ortho            : REF_ANNOTATION        Symbolic_Association  under phono
  notes            : REF_ANNOTATION        Symbolic_Subdivision  under phono
  word             : REF_ANNOTATION        Symbolic_Subdivision  under phono
  word gloss       : REF_ANNOTATION        Symbolic_Association  under word
  morpheme         : REF_ANNOTATION        Symbolic_Subdivision  under word
  morpheme gloss   : REF_ANNOTATION        Symbolic_Association  under morpheme

Config file format (JSON)
--------------------------
{
  "phono_tier"    : "tx",
  "transl_tiers"  : {"fr": "ft"},
  "ortho_tier"    : "ortho",
  "notes_tier"    : "notes",
  "word_tier"     : "word",
  "word_gls_tier" : "ge_w",
  "morph_tier"    : "mb",
  "morph_gls_tier": "ge_m"
}
"""

import sys
import json
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone

XML_NS = "http://www.w3.org/XML/1998/namespace"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def sec_to_ms(s):
    """'1.234' → 1234"""
    try:
        return round(float(s) * 1000)
    except (TypeError, ValueError):
        return 0

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _esc_attr(text):
    return _esc(text).replace('"', "&quot;")


# ─── Parse Pangloss XML ───────────────────────────────────────────────────────

def parse_xml(path):
    """
    Returns (text_id, object_lang, sentences).

    Each sentence dict:
        ts1, ts2 : int  milliseconds
        phono    : str
        ortho    : str
        transl   : list of (lang, text) tuples  — preserves order
        notes    : list of str
        words    : list of {form, gls, morphs: [{form, gloss}]}
        who      : str
    """
    root = ET.parse(path).getroot()

    text_id     = root.get("id", "")
    object_lang = root.get(f"{{{XML_NS}}}lang", "")

    sentences = []
    for s_elem in root.findall("S"):
        audio = s_elem.find("AUDIO")
        ts1 = sec_to_ms(audio.get("start", "0")) if audio is not None else 0
        ts2 = sec_to_ms(audio.get("end",   "0")) if audio is not None else 0

        phono = ""
        ortho = ""
        for form in s_elem.findall("FORM"):
            kind = form.get("kindOf", "")
            if kind == "phono":
                phono = (form.text or "").strip()
            elif kind == "ortho":
                ortho = (form.text or "").strip()

        transl = [
            (t.get(f"{{{XML_NS}}}lang", ""), (t.text or "").strip())
            for t in s_elem.findall("TRANSL")
        ]

        notes = [
            n.get("message", "").strip()
            for n in s_elem.findall("NOTE")
            if n.get("message", "").strip()
        ]

        words = []
        for w_elem in s_elem.findall("W"):
            w_form    = (w_elem.findtext("FORM") or "").strip()
            w_gls_el  = w_elem.find("TRANSL")
            w_gls     = (w_gls_el.text or "").strip() if w_gls_el is not None else ""
            morphs = []
            for m_elem in w_elem.findall("M"):
                m_form   = (m_elem.findtext("FORM") or "").strip()
                m_gls_el = m_elem.find("TRANSL")
                m_gls    = (m_gls_el.text or "").strip() if m_gls_el is not None else ""
                morphs.append({"form": m_form, "gloss": m_gls})
            words.append({"form": w_form, "gls": w_gls, "morphs": morphs})

        sentences.append({
            "ts1":   ts1,
            "ts2":   ts2,
            "phono": phono,
            "ortho": ortho,
            "transl": transl,
            "notes": notes,
            "words": words,
            "who":   s_elem.get("who", ""),
        })

    return text_id, object_lang, sentences


# ─── Inspect ──────────────────────────────────────────────────────────────────

def inspect_xml(text_id, object_lang, sentences):
    print()
    print(f"Text ID   : {text_id or '(none)'}")
    print(f"Language  : {object_lang or '(none)'}")
    print(f"Sentences : {len(sentences)}")
    print()

    transl_langs = list(dict.fromkeys(
        lang for s in sentences for lang, _ in s["transl"]
    ))
    has_ortho  = any(s["ortho"]  for s in sentences)
    has_notes  = any(s["notes"]  for s in sentences)
    has_words  = any(s["words"]  for s in sentences)
    has_w_gls  = any(w["gls"]    for s in sentences for w in s["words"])
    has_morphs = any(w["morphs"] for s in sentences for w in s["words"])
    has_m_gls  = any(m["gloss"]  for s in sentences
                     for w in s["words"] for m in w["morphs"])

    def yn(b): return "yes" if b else "no"
    print("Content:")
    print(f"  Transcription (phono)  : yes")
    print(f"  Orthographic form      : {yn(has_ortho)}")
    tl = ", ".join(f"{l!r}" for l in transl_langs) if transl_langs else "no"
    print(f"  Translations           : {tl}")
    print(f"  Notes                  : {yn(has_notes)}")
    print(f"  Words                  : {yn(has_words)}")
    print(f"  Word glosses           : {yn(has_w_gls)}")
    print(f"  Morphemes              : {yn(has_morphs)}")
    print(f"  Morpheme glosses       : {yn(has_m_gls)}")
    print()


# ─── Interactive config ───────────────────────────────────────────────────────

def _ask(prompt, default=""):
    suffix = f"\n  (press Enter to use \"{default}\")" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _show_summary(cfg):
    print()
    print("=" * 60)
    print("Summary of your choices")
    print("=" * 60)
    print(f"  Transcription tier    : {cfg['phono_tier']}")
    for lang, tname in (cfg.get("transl_tiers") or {}).items():
        label = lang if lang else "(no lang code)"
        print(f"  Translation ({label:8s}): {tname}")
    def opt(k): return cfg.get(k) or "(none)"
    print(f"  Orthography tier      : {opt('ortho_tier')}")
    print(f"  Notes tier            : {opt('notes_tier')}")
    print(f"  Word tier             : {opt('word_tier')}")
    print(f"  Word gloss tier       : {opt('word_gls_tier')}")
    print(f"  Morpheme tier         : {opt('morph_tier')}")
    print(f"  Morpheme gloss tier   : {opt('morph_gls_tier')}")
    print()


def _predefined_cfg(transl_langs, has_ortho, has_notes,
                    has_words, has_w_gls, has_morphs, has_m_gls):
    """Return a config using the standard tier names.

    'ge' follows the SIL convention: it names the gloss at the deepest level
    present.  When both word and morpheme glosses exist simultaneously we
    disambiguate with ge_w / ge_m.
    """
    if len(transl_langs) == 1:
        transl_map = {transl_langs[0]: "ft"}
    else:
        transl_map = {
            lang: (f"ft_{lang}" if lang else "ft")
            for lang in transl_langs
        }
    both_gls = has_w_gls and has_m_gls and has_morphs and has_words
    return {
        "phono_tier":     "tx",
        "transl_tiers":   transl_map,
        "ortho_tier":     "ortho" if has_ortho                        else None,
        "notes_tier":     "notes" if has_notes                        else None,
        "word_tier":      "word"  if has_words                        else None,
        "word_gls_tier":  ("ge_w" if both_gls else "ge")
                          if has_w_gls and has_words                  else None,
        "morph_tier":     "mb"    if has_morphs and has_words         else None,
        "morph_gls_tier": ("ge_m" if both_gls else "ge")
                          if has_m_gls and has_morphs and has_words   else None,
    }


def _show_predefined(transl_langs, has_ortho, has_notes,
                     has_words, has_w_gls, has_morphs, has_m_gls):
    """Print the standard tier name table."""
    rows = [
        ("tx",  "Main transcription (phonetic)",    "<FORM kindOf='phono'>"),
    ]
    if len(transl_langs) == 1:
        rows.append(("ft", "Free translation", "<TRANSL>"))
    else:
        for lang in transl_langs:
            name = f"ft_{lang}" if lang else "ft"
            rows.append((name, f"Free translation ({lang or '?'})", "<TRANSL>"))
    if has_ortho:
        rows.append(("ortho", "Orthographic form", "<FORM kindOf='ortho'>"))
    if has_notes:
        rows.append(("notes", "Notes / comments",  "<NOTE>"))
    both_gls = has_w_gls and has_m_gls and has_morphs and has_words
    if has_words:
        rows.append(("word", "Word segmentation", "<W>"))
    if has_w_gls and has_words:
        name = "ge_w" if both_gls else "ge"
        rows.append((name, "Word gloss", "<W><TRANSL>"))
    if has_morphs and has_words:
        rows.append(("mb",  "Morpheme break",    "<M><FORM>"))
    if has_m_gls and has_morphs and has_words:
        name = "ge_m" if both_gls else "ge"
        rows.append((name, "Morpheme gloss",    "<M><TRANSL>"))

    print()
    print("Standard tier names:")
    print()
    for name, role, xml in rows:
        print(f"  {name:8s}  {role:35s}  [XML: {xml}]")
    print()


def _save_cfg(cfg):
    save_path = input(
        "Save these choices to a file so you can reuse them next time?\n"
        "(Enter a file name ending in .json, or press Enter to skip): "
    ).strip()
    if save_path:
        with open(save_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        print(f"Saved to {save_path}")


def interactive_config(text_id, object_lang, sentences):
    transl_langs = list(dict.fromkeys(
        lang for s in sentences for lang, _ in s["transl"]
    ))
    has_ortho  = any(s["ortho"]  for s in sentences)
    has_notes  = any(s["notes"]  for s in sentences)
    has_words  = any(s["words"]  for s in sentences)
    has_w_gls  = any(w["gls"]    for s in sentences for w in s["words"])
    has_morphs = any(w["morphs"] for s in sentences for w in s["words"])
    has_m_gls  = any(m["gloss"]  for s in sentences
                     for w in s["words"] for m in w["morphs"])

    # ── Offer predefined tier names ───────────────────────────────────────────
    _show_predefined(transl_langs, has_ortho, has_notes,
                     has_words, has_w_gls, has_morphs, has_m_gls)
    choice = input(
        "Use these standard names, or choose custom names? [standard / custom]: "
    ).strip().lower()

    if choice in ("s", "standard", ""):
        cfg = _predefined_cfg(transl_langs, has_ortho, has_notes,
                              has_words, has_w_gls, has_morphs, has_m_gls)
        _show_summary(cfg)
        answer = input(
            "Does this look correct? [yes / no, choose custom names instead]: "
        ).strip().lower()
        if answer in ("y", "yes", ""):
            print()
            _save_cfg(cfg)
            return cfg
        print("\nSwitching to custom names.\n")

    # ── Custom tier names ─────────────────────────────────────────────────────
    print("=" * 60)
    print("Tier naming")
    print("=" * 60)
    print("Choose a name for each tier that will be created in the EAF.")
    print()

    while True:
        cfg = {}

        cfg["phono_tier"] = _ask(
            "Main transcription tier name",
            "tx"
        )

        cfg["transl_tiers"] = {}
        for lang in transl_langs:
            default_name = f"ft_{lang}" if lang else "ft"
            cfg["transl_tiers"][lang] = _ask(
                f"Translation tier name (language: {lang!r})" if lang
                else "Translation tier name (no language code in XML)",
                default_name
            )

        cfg["ortho_tier"] = (
            _ask("Orthography tier name", "ortho") if has_ortho else None
        )

        cfg["notes_tier"] = (
            _ask("Notes tier name", "notes") if has_notes else None
        )

        cfg["word_tier"] = (
            _ask("Word tier name", "word") if has_words else None
        )

        both_gls = has_w_gls and has_m_gls and has_morphs and cfg["word_tier"]
        cfg["word_gls_tier"] = (
            _ask("Word gloss tier name", "ge_w" if both_gls else "ge")
            if has_w_gls and cfg["word_tier"] else None
        )

        cfg["morph_tier"] = (
            _ask("Morpheme tier name", "mb")
            if has_morphs and cfg["word_tier"] else None
        )

        cfg["morph_gls_tier"] = (
            _ask("Morpheme gloss tier name", "ge_m" if both_gls else "ge")
            if has_m_gls and cfg["morph_tier"] else None
        )

        _show_summary(cfg)
        answer = input("Does this look correct? [yes / no, start over]: ").strip().lower()
        if answer in ("y", "yes", ""):
            break
        print("\nStarting over — please re-enter your choices.\n")

    print()
    _save_cfg(cfg)
    return cfg


# ─── Build EAF ────────────────────────────────────────────────────────────────

def build_eaf(text_id, object_lang, sentences, cfg):
    """Generate an EAF XML string from sentences and config."""

    # ── ID counters ───────────────────────────────────────────────────────────
    _ann  = [0]
    _ts   = [0]
    ts_slots = []   # list of (ts_id, ms_value) in creation order

    def new_id():
        _ann[0] += 1
        return f"a{_ann[0]}"

    def ts_id(ms):
        _ts[0] += 1
        tsid = f"ts{_ts[0]}"
        ts_slots.append((tsid, ms))
        return tsid

    # ── Collect annotations per tier ─────────────────────────────────────────
    # All entries are 4-tuples: (ann_id, ref_or_ts1, prev_or_ts2, value)
    # For ALIGNABLE: (ann_id, ts1_id, ts2_id, value)
    # For REF:       (ann_id, parent_ann_id, prev_ann_id_or_None, value)

    phono_anns    = []
    transl_anns   = defaultdict(list)   # lang → list
    ortho_anns    = []
    notes_anns    = []
    word_anns     = []
    word_gls_anns = []
    morph_anns    = []
    morph_gls_anns = []

    for s in sentences:
        s_id = new_id()
        phono_anns.append((s_id, ts_id(s["ts1"]), ts_id(s["ts2"]), s["phono"]))

        # Translations — one per (sentence, language)
        for lang, text in s["transl"]:
            transl_anns[lang].append((new_id(), s_id, None, text))

        # Ortho — one per sentence
        if cfg.get("ortho_tier") and s["ortho"]:
            ortho_anns.append((new_id(), s_id, None, s["ortho"]))

        # Notes — may be multiple per sentence, chained via PREVIOUS_ANNOTATION
        if cfg.get("notes_tier"):
            prev = None
            for note in s["notes"]:
                aid = new_id()
                notes_anns.append((aid, s_id, prev, note))
                prev = aid

        # Words
        if cfg.get("word_tier"):
            prev_w = None
            for w in s["words"]:
                w_id = new_id()
                word_anns.append((w_id, s_id, prev_w, w["form"]))
                prev_w = w_id

                if cfg.get("word_gls_tier") and w["gls"]:
                    word_gls_anns.append((new_id(), w_id, None, w["gls"]))

                if cfg.get("morph_tier"):
                    prev_m = None
                    for m in w["morphs"]:
                        m_id = new_id()
                        morph_anns.append((m_id, w_id, prev_m, m["form"]))
                        prev_m = m_id

                        if cfg.get("morph_gls_tier") and m["gloss"]:
                            morph_gls_anns.append((new_id(), m_id, None, m["gloss"]))

    # ── XML lines ─────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ANNOTATION_DOCUMENT AUTHOR="" DATE="{now}" FORMAT="3.0" VERSION="3.0"',
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '    xsi:noNamespaceSchemaLocation="http://www.mpi.nl/tools/elan/EAFv3.0.xsd">',
        '    <HEADER MEDIA_FILE="" TIME_UNITS="milliseconds">',
        f'        <PROPERTY NAME="lastUsedAnnotationId">{_ann[0]}</PROPERTY>',
        '    </HEADER>',
        '    <TIME_ORDER>',
    ]
    for tsid, ms in ts_slots:
        lines.append(f'        <TIME_SLOT TIME_SLOT_ID="{tsid}" TIME_VALUE="{ms}"/>')
    lines.append('    </TIME_ORDER>')

    # ── Tier writers ──────────────────────────────────────────────────────────
    def write_alignable_tier(tier_id, ltype, anns, lang_ref=None):
        lang_attr = f' LANG_REF="{_esc_attr(lang_ref)}"' if lang_ref else ""
        lines.append(
            f'    <TIER{lang_attr} LINGUISTIC_TYPE_REF="{ltype}"'
            f' TIER_ID="{_esc_attr(tier_id)}">'
        )
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

    def write_ref_tier(tier_id, ltype, parent_tier_id, anns, lang_ref=None):
        lang_attr = f' LANG_REF="{_esc_attr(lang_ref)}"' if lang_ref else ""
        lines.append(
            f'    <TIER{lang_attr} LINGUISTIC_TYPE_REF="{ltype}"'
            f' PARENT_REF="{_esc_attr(parent_tier_id)}"'
            f' TIER_ID="{_esc_attr(tier_id)}">'
        )
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

    # ── Write tiers in order ──────────────────────────────────────────────────
    phono_name = cfg["phono_tier"]
    write_alignable_tier(phono_name, "default-lt", phono_anns,
                         lang_ref=object_lang or None)

    for lang, tname in (cfg.get("transl_tiers") or {}).items():
        if transl_anns[lang]:
            write_ref_tier(tname, "symassoc", phono_name,
                           transl_anns[lang], lang_ref=lang or None)

    if cfg.get("ortho_tier") and ortho_anns:
        write_ref_tier(cfg["ortho_tier"], "symassoc", phono_name, ortho_anns)

    if cfg.get("notes_tier") and notes_anns:
        write_ref_tier(cfg["notes_tier"], "symsub", phono_name, notes_anns)

    word_name = cfg.get("word_tier")
    if word_name and word_anns:
        write_ref_tier(word_name, "symsub", phono_name, word_anns)

        if cfg.get("word_gls_tier") and word_gls_anns:
            write_ref_tier(cfg["word_gls_tier"], "symassoc", word_name,
                           word_gls_anns)

        morph_name = cfg.get("morph_tier")
        if morph_name and morph_anns:
            write_ref_tier(morph_name, "symsub", word_name, morph_anns)

            if cfg.get("morph_gls_tier") and morph_gls_anns:
                write_ref_tier(cfg["morph_gls_tier"], "symassoc", morph_name,
                               morph_gls_anns)

    # ── Linguistic types ──────────────────────────────────────────────────────
    lines.extend([
        '    <LINGUISTIC_TYPE GRAPHIC_REFERENCES="false"'
        ' LINGUISTIC_TYPE_ID="default-lt" TIME_ALIGNABLE="true"/>',
        '    <LINGUISTIC_TYPE CONSTRAINTS="Symbolic_Association"'
        ' GRAPHIC_REFERENCES="false" LINGUISTIC_TYPE_ID="symassoc"'
        ' TIME_ALIGNABLE="false"/>',
        '    <LINGUISTIC_TYPE CONSTRAINTS="Symbolic_Subdivision"'
        ' GRAPHIC_REFERENCES="false" LINGUISTIC_TYPE_ID="symsub"'
        ' TIME_ALIGNABLE="false"/>',
    ])

    # ── Language elements (required for LANG_REF to be valid) ─────────────────
    # Collect every lang code referenced by any tier in the document.
    lang_codes = []
    if object_lang:
        lang_codes.append(object_lang)
    for lang in (cfg.get("transl_tiers") or {}):
        if lang and lang not in lang_codes:
            lang_codes.append(lang)
    for lang_code in lang_codes:
        lid = _esc_attr(lang_code)
        lines.append(
            f'    <LANGUAGE LANG_DEF="{lid}" LANG_ID="{lid}" LANG_LABEL="{lid}"/>'
        )

    # ── Constraint definitions ────────────────────────────────────────────────
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


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert Pangloss XML to ELAN .eaf format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("xml",    help="Input Pangloss XML file")
    parser.add_argument("output", nargs="?", help="Output .eaf file")
    parser.add_argument("--inspect", action="store_true",
                        help="Show XML contents and exit")
    parser.add_argument("--config", metavar="FILE",
                        help="Load tier names from a JSON config file")
    args = parser.parse_args()

    text_id, object_lang, sentences = parse_xml(args.xml)
    inspect_xml(text_id, object_lang, sentences)

    if args.inspect or not args.output:
        if not args.inspect:
            parser.error("output file is required unless --inspect is given")
        return

    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        cfg = interactive_config(text_id, object_lang, sentences)

    eaf_content = build_eaf(text_id, object_lang, sentences, cfg)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(eaf_content)
    print(f"Written {len(sentences)} sentence(s) to {args.output}")


if __name__ == "__main__":
    main()
