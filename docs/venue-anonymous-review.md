# Prepare a double-blind submission

For anonymous review, the PDF (and, at some venues, the source) must not reveal
who wrote the paper. Fledge cannot know who you are: its identity checks search
only for the names, institutions and links you list, plus generic hints such as
an acknowledgements section. The `anonymous-review` preset switches these checks
on. It is a starting point, not a venue policy, and passing it does not certify
that the submission is anonymous. Read your venue's current instructions, and
read the final PDF yourself.

## Run it

```sh
fledge init paper --preset anonymous-review   # writes paper/fledge.toml to edit
```

Then open `paper/fledge.toml` and list every term that would identify the
authors. Nothing is searched until you do:

```toml
[submission_checks]
identity_terms = ["Jane Doe", "Example University", "example-lab.org", "github.com/janedoe"]

[metadata_privacy]
image_identity_terms = ["Jane Doe", "Example University"]
```

Put these settings into the tables that the generated file already has, under
the commented placeholders, rather than adding a second `[submission_checks]`
table. Include grant numbers, project names and repository URLs that point to
you. Then build, check and prepare:

```sh
fledge check paper --main main.tex
fledge prepare paper --main main.tex -o review-submission
```

Upload `review-submission/manuscript.pdf`, the PDF rebuilt from the prepared
sources, or `submission.zip` when the venue asks for sources.

## What the preset checks

| Concern | Checks |
| --- | --- |
| Your terms in the sources and the PDF | `PRV001` searches the sources, including comments, URLs and file names, and `PRV002` the PDF text and metadata for `identity_terms`; whole words, ignoring case, by default (`identity_term_matching = "substring"` matches inside words) |
| Phrases that identify | `PRV003` reports acknowledgements and self-citation phrasing such as "in our earlier work" next to a citation (advisory) |
| Figure metadata | `PRV103` searches the metadata of the figures the document includes for `image_identity_terms`; `PRV104` reports home-folder paths such as `/Users/<name>/` there without any setting |
| Included PDF figures | `PRV105` reports identity terms and home-folder paths that included PDFs leave in the built PDF; the preset's `suppress_included_pdf_information` (`TEX204`) keeps their information dictionaries and file names out of the PDF that `prepare` builds |
| Comments and files | Private notes in comments (`PRV005`), credential-shaped text (`PRV004`) and unused files (`PKG106`), which matter when reviewers receive the source |
| PDF | Embedded fonts (`PDF104`) and no attachments (`PDF109`) |
| Layout | Layout manipulation such as negative spacing or shrunken margins (`MAN002`, advisory) and floats: a caption on every figure and table (`MAN011`) and labels after their captions (`MAN013`) block; a missing label (`MAN012`) and a float the text never refers to (`MAN014`) are reported as warnings, through `[checks] advisory`; the order of first references (`MAN015`) is a commented-out line of the preset |

The `TEX204` setting cannot be written into a main file that is not UTF-8, such as
Latin-1 (`TEX328`), because Fledge never rewrites such a file; `prepare` then stops
with `TEX204`. [Convert the file](troubleshooting.md#tex328-a-source-file-that-is-not-utf-8),
or set `suppress_included_pdf_information = false` in `[source_transforms]` and check
`PRV105` yourself.

## Fill in the placeholders

Copy these from the venue's current call for papers:

- `max_pages` (`PDF001`): the page limit, counted as the venue counts it;
- `require_line_numbers` under `[manuscript_checks]` (`MAN017`): when reviewers
  are asked to refer to line numbers;
- `max_archive_bytes` (`PKG105`): an upload size limit;
- `comment_policy = "private"` under `[source_transforms]`: when reviewers also
  receive the source, this removes comments with private markers from the copy.

## What Fledge cannot see

- Identity that is not in your list: a co-author's name you forgot, a
  distinctive dataset name or a self-citation written in the third person.
- Text inside images, such as a logo or a screenshot with a user name.
- The XMP and application data (PieceInfo) of included PDF pages, which pdfLaTeX
  and LuaLaTeX still copy into the PDF; the figure files themselves are never
  rewritten. Re-export a figure whose metadata names you.
- Whether the paper is anonymous as a whole. That remains a human judgment; see
  [limitations](limitations.md#questions-that-need-human-judgment).

After acceptance, the [camera-ready guide](venue-camera-ready.md) covers the
final version.
