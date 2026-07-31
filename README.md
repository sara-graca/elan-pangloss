# Elan EAF ⇄ Pangloss XML converters
Two command-line tools to convert interlinear glossed text between **ELAN**
(`.eaf`) and the **Pangloss / Cocoon** XML format, both ways:
| Script | Direction |
|---|---|
| `eaf_to_xml.py` | ELAN `.eaf` → Pangloss/Cocoon XML |
| `xml_to_eaf.py` | Pangloss/Cocoon XML → ELAN `.eaf` |

A third helper script, `eaf_uniformize.py`, cleans up inconsistent tier names
and types across a set of `.eaf` files (see [Uniformizing tiers](#uniformizing-tiers)
below).

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
<<<<<<< HEAD
`--config` also accepts a folder in single-file mode: the script looks for
`<input name>.json`, and failing that offers the config whose structure fits
the file.
=======

`--config` also accepts a folder in single-file mode: the script looks for
`<input name>.json`, and failing that offers the config whose structure fits
the file.

>>>>>>> 42c605177ceff9fd1968f516cfe3b8a9b5d51eef
During the interview: type `<` to go back, `y` to accept a suggestion, and
Enter to skip an optional tier. Where several tiers can be chosen at once
(transcriptions, translations, notes), give their numbers separated by commas.
The final summary lists any tiers that won't be exported and lets you go back
to map them.
<<<<<<< HEAD
=======

>>>>>>> 42c605177ceff9fd1968f516cfe3b8a9b5d51eef
## What's mapped
The interlinear content: sentence/reference units, transcription lines,
translations, notes, words, morphemes, and glosses.
A few things to know:
- `xml_to_eaf` rebuilds a reference-rooted EAF (time-aligned reference tier,
  transcriptions beneath it, words then morphemes under that); multi-speaker
  XML is split onto `@SP1`/`@SP2` tiers.
- Pangloss has no element for a part of speech, so `eaf_to_xml` appends a PoS
  tier to the morpheme gloss with a chosen separator (`deceive:v`);
  `xml_to_eaf` can split it back out into its own tier.
- Morpheme boundary markers (`-`, `=`) are stripped from morpheme forms/glosses.
- Layers with no XML counterpart aren't stored in the XML; the
  "tiers not exported" summary shows what those are.
## Config files
Plain JSON, editable by hand. `eaf_to_xml` configs describe one or more speakers
and their tier mapping; `xml_to_eaf` configs name the ELAN tiers to create
(`@SP1`/`@SP2` suffixes are added automatically).
<<<<<<< HEAD

## Uniformizing tiers
> **Note:** this script is newer and less thoroughly tested than the
> converters. It never touches your originals — it writes tidied copies to a
> separate output folder — but check the results before relying on them.

In a corpus built over years, the "same" tier may end up with many names and many tier types; sometimes the
reverse happens and different tiers share one type when you'd rather split them.
`eaf_uniformize.py` lets you group the variants, give each group one name/type,
and write clean `.eaf` files. Annotations, timing, and media links are left
untouched.

Inspect what's in your files:
```bash
python eaf_uniformize.py input_folder/ --inspect
python eaf_uniformize.py file.eaf --inspect
```
Uniformize a folder (interactive), or a single file:
```bash
python eaf_uniformize.py input_folder/ output_folder/
python eaf_uniformize.py file.eaf output_folder/
```
Reuse choices saved from a previous run:
```bash
python eaf_uniformize.py input_folder/ output_folder/ --config my_choices.json
```
Running with no arguments starts a guided, question-and-answer setup.

You control two things: **tier types** (pick existing types to merge them, or
pick individual tier names to split a type — mixing incompatible ELAN
stereotypes triggers a warning) and **tier names** (group base names and give
each group one name). Speaker suffixes like `mot@SP1` are understood — you group
the base names and the speaker part is reattached automatically
(`mot@SP1` → `word@SP1`).
=======
>>>>>>> 42c605177ceff9fd1968f516cfe3b8a9b5d51eef
