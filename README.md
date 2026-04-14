# Screenplay PDF to EPUB

Dependency-free CLI for converting text-based screenplay PDFs into readable EPUB files.

It is tuned for screenplay layouts with common formatting patterns such as:
- scene headings on the left
- action on the left
- character cues centered
- dialogue indented
- parentheticals slightly less indented than cues
- transitions right-aligned

## Usage

```bash
python3 screenplay2epub.py /path/to/script.pdf
python3 screenplay2epub.py /path/to/script.pdf -o /path/to/script.epub
```

Useful tuning flags:

```bash
python3 screenplay2epub.py sample.pdf \
  --scene-max-x 130 \
  --dialogue-min-x 165 \
  --parenthetical-min-x 200 \
  --character-min-x 235 \
  --transition-min-x 430 \
  --debug-lines
```

## Current approach

The parser reads compressed PDF text streams directly and classifies lines by:
- x-position
- capitalization
- simple screenplay markers such as `INT.`, `EXT.`, parentheticals, and `CUT TO:`

The generated EPUB:
- removes page numbers and common continuation markers
- preserves scene headings
- renders action as prose paragraphs
- renders character and dialogue as readable dialogue blocks

## Limitations

- text-based PDFs only
- no OCR
- currently tuned for PDFs that use straightforward text operators similar to the sample
- does not yet handle every PDF encoding variant or exotic screenplay layout
