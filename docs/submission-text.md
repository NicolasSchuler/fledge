# Copy text into a submission form

`fledge submission-text INPUT` previews the title, abstract and keywords from a
project folder or ZIP. It reads source without compiling TeX or accessing the
network, and leaves the original project unchanged. Select the main document
with `--main main.tex`, or use `main` in the discovered project settings.
`--config FILE` selects settings explicitly; `--isolated` ignores discovery.

```sh
fledge submission-text paper --field title
fledge submission-text paper --field abstract
fledge submission-text paper --field keywords
fledge submission-text paper --json
```

A selected ready field prints exactly one physical line to stdout. Missing,
empty or unresolved fields print diagnostics to stderr and exit with code 3,
without exporting text to stdout. JSON returns all fields, statuses, draft text,
source locations, candidate texts and interpretation issues. JSON and preview
exit with code 3 when any field is unresolved. Input/configuration failures use
code 4. Missing optional fields do not make a preview fail.

The converter retains the text inside common formatting commands, decodes
escaped characters and common accents, preserves Unicode, converts TeX dashes
and double quotes, and joins source line breaks. Comment continuation follows
TeX semantics: `foo% comment` followed immediately by `bar` becomes `foobar`.
Meaningful single hyphens remain intact. `\texttt{...}` retains literal dashes
and quotes, such as `--jobs`. URL commands retain their address, and `\href`
retains its visible link text. Keywords retain the source punctuation
and order; explicit `\sep` and `\and` become `; ` to preserve keyword phrases.
Terminal control and directional characters are displayed as visible Unicode
escapes and make the affected field unresolved.

Simple inline mathematics such as `$n=100$` and common lowercase Greek letters
can be converted literally. Scripts, fractions, complex mathematical commands,
citations, references and custom macros need manual conversion. Their original
fragments remain visible in the draft, and affected fields are unresolved.
No macro execution, citation numbering or inferred scientific wording is used.
Supported converter commands that are locally defined or redefined in the
selected literal source graph also need manual review. Their original fragments
remain in the draft, even when the replacement looks simple. Unrelated command
definitions do not block an otherwise ready field.
Locally assigned active characters, such as a redefined `~`, also require
review when they appear in the field; the draft retains the original LaTeX.

Multiple declarations are shown as candidates rather than choosing one.
Conditional, nested or unclosed declarations and incomplete source graphs need
source clarification. The supported declarations are `\title{...}`,
`\abstract{...}`, the `abstract` environment, `\keywords{...}`, and the
`keywords`, `keyword` and `IEEEkeywords` environments. Unsupported class-specific
declarations and legacy subfiles behavior are outside this bounded converter.
Review the preview before pasting, especially the notation and keyword separators.
