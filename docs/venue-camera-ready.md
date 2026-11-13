# Prepare the camera-ready version

The camera-ready version goes to production, where a publisher checks the PDF
and often the source against its own requirements. The `camera-ready` preset
switches on generic PDF production, structure and bibliography checks. It is a
starting point, not a publisher policy: Fledge maintains no publisher rules, and
passing the preset does not mean that the publisher will accept the files. Copy
every limit and requirement from the publisher's current instructions.

## Run it

```sh
fledge init paper --preset camera-ready    # writes paper/fledge.toml to edit
fledge check paper --main main.tex
fledge prepare paper --main main.tex -o camera-ready
```

`camera-ready/manuscript.pdf` is the PDF rebuilt from `camera-ready/submission.zip`;
submit whichever the publisher asks for. Add `--layout flat` if the publisher
wants every file in one folder.

## What the preset checks

| Concern | Checks |
| --- | --- |
| Fonts | Every font embedded (`PDF104`) and no Type 3 fonts (`PDF105`), which often come from bitmap fonts in figures |
| PDF objects | No encryption (`PDF106`), JavaScript (`PDF108`) or attachments (`PDF109`); the same page size on every page (`PDF102`) |
| PDF properties | A PDF Title (`PDF206`), which viewers and screen readers show instead of the file name |
| Front matter | Literal `\title` and `\author` (`MAN008`) and a valid checksum on any ORCID iD (`MAN009`) |
| Floats | Captions, labels and references of figures and tables, in order (`MAN011`–`MAN015`) |
| Leftovers | The `draft` class option (`TEX102`), `showframe` (`TEX103`), private notes in comments (`PRV005`), credential-shaped text (`PRV004`), shell escape (`PRV006`), unused files (`PKG106`), empty sections (`MAN006`), "Figure 2" typed instead of a reference (`MAN007`) and layout manipulation (`MAN002`) |
| Bibliography | DOIs for articles and proceedings papers (`BIB104`), capitals in titles protected from the style (`BIB107`) and one work cited twice, such as a preprint and its article (`BIB108`); all advisories |

The first run often stops at `PDF206`, because LaTeX writes no PDF Title by
default. Set one with hyperref in the preamble:

```latex
\usepackage{hyperref}
\hypersetup{pdftitle={The title of the paper}}
```

## Fill in the placeholders

The generated `fledge.toml` comments out every value that differs between
publishers. Fill in those the instructions state:

- `max_pages` (`PDF001`) and the page size in PDF points,
  `expected_page_width_pt` and `expected_page_height_pt` (`PDF101`), for example
  612 × 792 for US Letter or 595 × 842 for A4;
- `expected_document_class` and `required_class_options` (`TEX101`, `TEX102`);
- `min_image_dpi` (`PDF103`): the minimum raster image resolution;
- `required_declarations` under `[manuscript_checks]` (`MAN010`): statements such
  as Data Availability, by heading;
- `forbid_line_numbers` (`MAN017`): line numbers left over from review;
- `require_document_metadata` (`MAN019`): a `\DocumentMetadata` declaration with
  the document language and tagging, for publishers that require a tagged PDF;
- `required_metadata` under `[pdf_checks]`: further PDF properties, such as
  `"Author"`;
- `expected_texlive_year` (`BLD105`), `required_color_space` for figures
  (`PDF313`) and `max_archive_bytes` (`PKG105`).

## Polish with suggestions

Suggestions never change the outcome, so they are safe to try on a final
version. This adds the style, bibliography and chktex suggestions to the preset's
checks:

```sh
fledge check paper --main main.tex --select ALL --select STY --select BIB11 --select CHK001
```

They point at obsolete commands, missing ties before references, hyphens in
number ranges, unused labels, cited preprints that may have been published since
and venues spelled several ways. Act on those you agree with; a
`% fledge: ignore CODE` comment silences one where it occurs. See
[suggestions](reports.md#suggestions) and [lint with chktex](chktex.md).
