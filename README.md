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
python3 screenplay2epub.py /path/to/script.pdf --cover /path/to/cover.jpg
```

**Note:** If any part of your file path contains spaces—including parent folders or the file name itself—enclose the entire path in quotes. For example: `python3 screenplay2epub.py "/Users/username/Scripts/My Script Folder/My Script.pdf"`

Metadata and cover overrides:

```bash
python3 screenplay2epub.py /path/to/script.pdf \
  --title "My Script" \
  --author "Writer Name" \
  --cover /path/to/cover.png
```

Supported cover formats: JPG, PNG, GIF, WEBP, and SVG.

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
- uses the PDF's first page as a dedicated centered title page when it contains lines before the first scene heading
- removes page numbers and common continuation markers
- preserves scene headings
- builds the EPUB table of contents from detected scene headings such as `INT.`, `EXT.`, `INT./EXT.`, and `I/E.`
- renders action as prose paragraphs
- renders character and dialogue as readable dialogue blocks

## Limitations

- text-based PDFs only
- no OCR
- currently tuned for PDFs that use straightforward text operators similar to the sample
- does not yet handle every PDF encoding variant or exotic screenplay layout
