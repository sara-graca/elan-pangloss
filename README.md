# Elan EAF ⇄ Pangloss XML converters

Two command-line tools to convert interlinear glossed text between **ELAN**
(`.eaf`) and the **Pangloss / Cocoon** XML format, both ways:

| Script | Direction |
|---|---|
| `eaf_to_xml.py` | ELAN `.eaf` → Pangloss/Cocoon XML |
| `xml_to_eaf.py` | Pangloss/Cocoon XML → ELAN `.eaf` |

## Requirements

Python 3.8+, standard library only (no dependencies).

## Usage

Inspect a file's structure:

```bash
python eaf_to_xml.py input.eaf --inspect
python xml_to_eaf.py input.xml --inspect
```

Convert a file (you'll answer a few questions the first time, then can save the
answers as a reusable config):

```bash
python eaf_to_xml.py input.eaf output.xml
python xml_to_eaf.py input.xml output.eaf --config my.json
```

Convert a folder — grouped and configured interactively, or with a saved config
file (one mapping for all matching files) or a folder of configs (each file
matched to the config that fits). Files that match no config are reported, and
you choose to configure them by hand or skip them:

```bash
python eaf_to_xml.py input_dir/ output_dir/ --config configs_folder/
```

During the interview: type `<` to go back, press Enter to accept a suggested
tier or skip an optional one. The final summary lists any tiers that won't
be exported and lets you go back to map them.

## What's mapped

The interlinear content: sentence/reference units, transcription lines,
translations, notes, words, morphemes, and glosses.

A few things to know:

- `xml_to_eaf` rebuilds a reference-rooted EAF (time-aligned reference tier,
  transcriptions beneath it, words then morphemes under that); multi-speaker
  XML is split onto `@SP1`/`@SP2` tiers.
- Morpheme boundary markers (`-`, `=`) are stripped from morpheme forms/glosses.
- Layers with no XML counterpart aren't stored in the XML; the
  "tiers not exported" summary shows what those are.

## Config files

Plain JSON, editable by hand. `eaf_to_xml` configs describe one or more speakers
and their tier mapping; `xml_to_eaf` configs name the ELAN tiers to create
(`@SP1`/`@SP2` suffixes are added automatically).

## Command line

```
eaf_to_xml.py INPUT [OUTPUT] [--inspect] [--config PATH] [--lang CODE] [--text-id ID]
xml_to_eaf.py INPUT [OUTPUT] [--inspect] [--config PATH]
```

`INPUT`/`OUTPUT` are files or directories; `--config` is a JSON file or a folder
of them; `--inspect` prints the structure and exits.
