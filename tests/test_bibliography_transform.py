"""Behavioral preservation and refusal cases for explicit bibliography proposals."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography import _Document, _Parser
from latexprep.bibliography_transform import BibliographyTransformOptions, plan_bibliography
from latexprep.bibliography_transform_rules import RULE_DEFINITIONS
from latexprep.models import Change, Finding, PreparationError, has_blockers


class BibliographyTransformTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.options = BibliographyTransformOptions()
        self.manuscript()

    def write(self, name: str, value: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
        return path

    def manuscript(self, body: str = "", resources: str = "references") -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n"
            + body
            + "\n\\bibliography{"
            + resources
            + "}\n\\end{document}\n",
        )

    def plan(self, **options: object) -> tuple[dict[str, bytes], list[Change], list[Finding]]:
        return plan_bibliography(self.root, "main.tex", replace(self.options, **options))

    def code(self, rows: list[Finding]) -> str:
        self.assertEqual(len(rows), 1)
        definition = next(item for item in RULE_DEFINITIONS if item["name"] == rows[0].rule)
        return str(definition["code"])

    def assert_blocked(
        self,
        result: tuple[dict[str, bytes], list[Change], list[Finding]],
        code: str,
        status: str = "inconclusive",
    ) -> None:
        proposed, changes, rows = result
        self.assertEqual((proposed, changes), ({}, []))
        self.assertEqual(self.code(rows), code)
        self.assertEqual(rows[0].status, status)
        self.assertTrue(has_blockers(rows))
        self.assertIn("no requested transformation", rows[0].next_step or "")

    def parsed(self, text: bytes) -> _Document:
        document = _Document("result.bib", text.decode("utf-8"))
        _Parser(document).parse()
        self.assertTrue(document.valid, document.findings)
        return document

    def test_disabled_options_are_empty_and_immutable(self) -> None:
        self.assertEqual(plan_bibliography(Path("/nonexistent")), ({}, [], []))
        with self.assertRaises(FrozenInstanceError):
            self.options.cited_only = True  # ty: ignore[invalid-assignment]

    def test_format_preserves_values_comments_macros_bom_and_crlf(self) -> None:
        original = (
            "\ufeff% library header\r\n@comment{Do not change {this}.}\r\n"
            '@string{venue="A {Journal}"}\r\n@preamble{"\\newcommand{\\x}{X}"}\r\n'
            "@Article(KEY,title={A {GPU} Study},\r\n"
            ' % retained field explanation\r\n journal=venue # " Supplement",'
            " customField={100\\% and \\{sets\\}})\r\n"
        )
        path = self.write("references.bib", original)
        proposed, changes, rows = self.plan(format_entries=True)
        output = proposed["references.bib"].decode("utf-8")
        self.assertEqual(self.code(rows), "BIB201")
        self.assertEqual(rows[0].status, "passed")
        self.assertIn("title = {A {GPU} Study}", output)
        self.assertIn('journal = venue # " Supplement"', output)
        self.assertIn("customField = {100\\% and \\{sets\\}},\r\n)", output)
        self.assertIn(" % retained field explanation\r\n", output)
        self.assertEqual(output.split("@Article")[0], original.split("@Article")[0])
        self.assertEqual(path.read_bytes(), original.encode("utf-8"))
        self.assertIn("-@Article", changes[0].diff or "")
        before, after = self.parsed(original.encode()), self.parsed(proposed["references.bib"])
        self.assertEqual(
            [
                [(f.name, [(a.kind, a.value) for a in f.atoms]) for f in e.fields]
                for e in before.entries
            ],
            [
                [(f.name, [(a.kind, a.value) for a in f.atoms]) for f in e.fields]
                for e in after.entries
            ],
        )
        path.write_bytes(proposed["references.bib"])
        repeated = self.plan(format_entries=True)
        self.assertEqual(repeated[:2], ({}, []))
        self.assertEqual(repeated[2][0].status, "passed")

    def test_format_refuses_partial_or_unreadable_bibliographies(self) -> None:
        for value in ("@misc{k,title={", b"@misc{k,title={\xff}}", "% note\r@misc{k}\r"):
            with self.subTest(value=value):
                self.write("references.bib", value)
                self.assert_blocked(self.plan(format_entries=True), "BIB201")

    def test_selected_field_removal_retains_other_fields_and_comments(self) -> None:
        original = (
            '@string{note = "Keep macro named like field"}\n'
            "@misc{k,note = % retain syntax comment\n{private}, title={Keep {GPU}},"
            " file={local path},file={second path},unknown={Untouched}}\n"
            '@misc{only,note="private"}\n'
        )
        path = self.write("references.bib", original)
        proposed, _, rows = self.plan(remove_fields=("note", "file"))
        output = proposed["references.bib"].decode()
        self.assertEqual(self.code(rows), "BIB202")
        self.assertNotIn("private", output)
        self.assertNotIn("local path", output)
        self.assertIn("% retain syntax comment\n", output)
        self.assertIn('@string{note = "Keep macro named like field"}', output)
        self.assertIn("title={Keep {GPU}}", output)
        self.assertIn("unknown={Untouched}", output)
        self.assertEqual(path.read_text(), original)
        self.parsed(proposed["references.bib"])

    def test_selected_field_removal_noop_and_damaged_file_refusal(self) -> None:
        self.write("references.bib", "@misc{k,title={Keep}}")
        proposed, changes, rows = self.plan(remove_fields=("note",))
        self.assertEqual((proposed, changes), ({}, []))
        self.assertEqual(self.code(rows), "BIB202")
        self.assertEqual(rows[0].status, "passed")
        self.write("references.bib", "@misc{k,note={private}}\n@misc{damaged,title={")
        self.assert_blocked(self.plan(remove_fields=("note",)), "BIB202")

    def test_cited_only_retains_nocite_relationship_closure_aliases_sets_and_macros(self) -> None:
        self.manuscript("\\cite{child,alias,member}\\nocite{explicit}")
        original = (
            '% header\n@string{venue="Venue"}\n@string{unusedmacro="Still retained"}\n'
            '@preamble{"Literal preamble retained"}\n'
            "@inproceedings{child,crossref={parent},xdata={data},journal=venue}\n"
            "@proceedings{parent,related={related}}\n@xdata{data}\n@misc{related}\n"
            "@misc{primary,ids={alias}}\n@misc{explicit}\n"
            "@set{set,entryset={member,sibling}}\n@misc{member}\n@misc{sibling}\n"
            "@misc{unused,% keep this comment\n title={Remove this entry}}\n"
            "@comment{Always retained}\n"
        )
        path = self.write("references.bib", original)
        self.write("unselected.bib", "@misc{unselected}")
        proposed, _, rows = self.plan(cited_only=True)
        self.assertEqual(self.code(rows), "BIB203")
        document = self.parsed(proposed["references.bib"])
        self.assertEqual(
            {entry.key for entry in document.entries if entry.key},
            {
                "child",
                "parent",
                "data",
                "related",
                "primary",
                "explicit",
                "set",
                "member",
                "sibling",
            },
        )
        self.assertIn('@string{unusedmacro="Still retained"}', document.text)
        self.assertIn("% keep this comment\n", document.text)
        self.assertIn("@comment{Always retained}", document.text)
        self.assertEqual(path.read_text(), original)
        self.assertNotIn("unselected.bib", proposed)

    def test_cited_only_nocite_star_and_fully_used_noop(self) -> None:
        self.write("references.bib", "@misc{a}\n@misc{b}")
        for body in ("\\nocite{*}", "\\cite{a}\\nocite{b}"):
            with self.subTest(body=body):
                self.manuscript(body)
                proposed, changes, rows = self.plan(cited_only=True)
                self.assertEqual((proposed, changes), ({}, []))
                self.assertEqual(self.code(rows), "BIB203")
                self.assertEqual(rows[0].status, "passed")

    def test_cited_only_refuses_uncertain_usage_and_relationships(self) -> None:
        cases = (
            ("\\newcommand{\\mycite}[1]{\\cite{#1}}\\mycite{keep}", "@misc{keep}@misc{unused}"),
            ("\\cite{keep}\\printbibliography[type=article]", "@misc{keep}@misc{unused}"),
            ("\\cite{missing}", "@misc{unused}"),
            ("\\cite{keep}", "@misc{keep,crossref=parent}@misc{unused}"),
            ("\\cite{keep}", "@misc{keep,crossref={missing}}@misc{unused}"),
            ("\\cite{keep}", "@misc{keep}@misc{keep}@misc{unused}"),
            ("\\MyCite{keep}", "@misc{keep}@misc{unused}"),
            ("\\bibentry{keep}", "@misc{keep}@misc{unused}"),
            (
                "\\cite{keep}",
                '@preamble{"\\newcommand{\\mycite}{\\cite{unused}}"}@misc{keep}@misc{unused}',
            ),
        )
        for body, bibliography in cases:
            with self.subTest(body=body, bibliography=bibliography):
                self.manuscript(body)
                self.write("references.bib", bibliography)
                self.assert_blocked(self.plan(cited_only=True), "BIB203")
        self.manuscript("\\cite{keep}")
        self.write("references.bib", "@misc{keep}@misc{unused}")
        self.write("other.tex", "\\documentclass{article}\\cite{unused}\\bibliography{references}")
        self.assert_blocked(self.plan(cited_only=True), "BIB203")

    def test_resolved_source_dependencies_do_not_block_pruning(self) -> None:
        self.manuscript("\\includegraphics{fig}\\cite{keep}")
        self.write("fig.pdf", b"%PDF-1.4")
        self.write("fig.png", b"\x89PNG\r\n\x1a\n")
        self.write("references.bib", "@misc{keep}@misc{unused}")
        proposed, _, rows = self.plan(cited_only=True)
        self.assertEqual(self.code(rows), "BIB203")
        self.assertEqual(rows[0].status, "passed")
        self.assertEqual(
            {entry.key for entry in self.parsed(proposed["references.bib"]).entries}, {"keep"}
        )

    def test_ordinary_macro_and_conditional_definitions_do_not_block_pruning(self) -> None:
        self.manuscript(
            "\\newcommand{\\R}{\\mathbb{R}}\\def\\shorthand{text}\\let\\old\\relax\n"
            "\\ifdefined\\flag\\relax\\fi\n\\cite{keep}"
        )
        self.write("references.bib", "@misc{keep}@misc{unused}")
        proposed, _, rows = self.plan(cited_only=True)
        self.assertEqual(self.code(rows), "BIB203")
        self.assertEqual(rows[0].status, "passed")
        self.assertEqual(
            {entry.key for entry in self.parsed(proposed["references.bib"]).entries}, {"keep"}
        )
        self.manuscript("\\ifdefined\\flag\\cite{keep}\\fi")
        self.assert_blocked(self.plan(cited_only=True), "BIB203")

    def test_key_rename_updates_only_literal_uses_and_relationships_atomically(self) -> None:
        self.manuscript("\\input{chapter}\\nocite{old}\n% \\cite{old}\n\\verb|\\cite{old}|")
        chapter = self.write("chapter.tex", "\ufeffText \\parencites[see]{old, stay}{child}.\r\n")
        original = (
            "@misc{old,title={old remains prose}}\r\n@misc{stay}\r\n@misc{child,crossref={old}}\r\n"
        )
        path = self.write("references.bib", original)
        before_main = (self.root / "main.tex").read_bytes()
        before_chapter = chapter.read_bytes()
        proposed, changes, rows = self.plan(key_renames=(("old", "NewKey"),))
        self.assertEqual(self.code(rows), "BIB204")
        self.assertEqual(set(proposed), {"main.tex", "chapter.tex", "references.bib"})
        self.assertIn(b"\\nocite{NewKey}", proposed["main.tex"])
        self.assertIn(b"% \\cite{old}", proposed["main.tex"])
        self.assertIn(b"\\verb|\\cite{old}|", proposed["main.tex"])
        self.assertEqual(proposed["chapter.tex"], before_chapter.replace(b"{old,", b"{NewKey,"))
        self.assertIn(b"crossref={NewKey}", proposed["references.bib"])
        self.assertIn(b"title={old remains prose}", proposed["references.bib"])
        self.assertEqual(path.read_bytes(), original.encode())
        self.assertEqual((self.root / "main.tex").read_bytes(), before_main)
        self.assertEqual(chapter.read_bytes(), before_chapter)
        self.assertTrue(all(change.diff for change in changes))

    def test_reviewed_merge_keeps_target_metadata_and_redirects_donor_aliases(self) -> None:
        self.manuscript("\\cite{donor,donor_alias,child}")
        self.write(
            "references.bib",
            "@misc{donor,ids={donor_alias},title={Discarded after explicit review}}\n"
            "@misc{target,title={Retained exact metadata}}\n"
            "@misc{child,related={donor}}\n@misc{unused}\n",
        )
        proposed, _, rows = self.plan(
            merge_keys=(("donor", "target"),),
            key_renames=(("target", "canonical"),),
            cited_only=True,
        )
        self.assertEqual([row.status for row in rows], ["passed", "passed"])
        output = proposed["references.bib"].decode()
        self.assertNotIn("Discarded", output)
        self.assertNotIn("@misc{unused", output)
        self.assertIn("@misc{canonical,title={Retained exact metadata}}", output)
        self.assertIn("related={canonical}", output)
        self.assertIn(b"\\cite{canonical,canonical,child}", proposed["main.tex"])

    def test_key_mapping_noop_and_invalid_reviewed_mappings(self) -> None:
        self.manuscript("\\cite{a}")
        self.write("references.bib", "@misc{a}@misc{b}")
        result = self.plan(key_renames=(("a", "a"),))
        self.assertEqual(result[:2], ({}, []))
        self.assertEqual(self.code(result[2]), "BIB204")
        for options in (
            {"key_renames": (("a", "b"),)},
            {"key_renames": (("missing", "new"),)},
            {"merge_keys": (("a", "a"),)},
            {"merge_keys": (("a", "b"), ("b", "a"))},
            {"merge_keys": (("a", "b"),), "key_renames": (("a", "new"),)},
        ):
            with self.subTest(options=options):
                self.assert_blocked(self.plan(**options), "BIB204", "failed")

    def test_key_mapping_refuses_dynamic_uses_alias_collision_and_mixed_scope(self) -> None:
        self.manuscript("\\cite{\\selected}")
        self.write("references.bib", "@misc{old}")
        self.assert_blocked(self.plan(key_renames=(("old", "new"),)), "BIB204")
        self.manuscript("\\cite{old}")
        self.write("references.bib", "@misc{old}@misc{other,ids={new}}")
        self.assert_blocked(self.plan(key_renames=(("old", "new"),)), "BIB204", "failed")
        self.write("references.bib", "@misc{old}")
        self.write("other.tex", "\\documentclass{article}\\cite{old}\\bibliography{references}")
        self.assert_blocked(self.plan(key_renames=(("old", "new"),)), "BIB204")

    def test_guarded_field_edits_preserve_protection_and_add_delete_literal_metadata(self) -> None:
        original = (
            "@misc{k,title={A GPU Study},year=2020,note={remove},unknown={keep}}\r\n"
            "@misc{empty}\r\n"
        )
        path = self.write("references.bib", original)
        proposed, _, rows = self.plan(
            field_edits=(
                ("k", "title", "A GPU Study", "A {GPU} Study"),
                ("k", "year", "2020", "2021"),
                ("k", "note", "remove", None),
                ("k", "doi", None, "10.1234/Verified"),
                ("empty", "title", None, "Explicit new metadata"),
                ("empty", "author", None, "Doe, Jane"),
            )
        )
        output = proposed["references.bib"].decode()
        self.assertEqual(self.code(rows), "BIB205")
        self.assertIn("title={A {GPU} Study}", output)
        self.assertIn("year=2021", output)
        self.assertIn("unknown={keep}", output)
        self.assertIn("doi = {10.1234/Verified}", output)
        self.assertNotIn("note=", output)
        self.assertIn("author = {Doe, Jane}", output)
        self.parsed(proposed["references.bib"])
        self.assertEqual(path.read_bytes(), original.encode())

    def test_guarded_field_edit_noop_and_stale_or_invalid_replacements(self) -> None:
        self.write("references.bib", "@misc{k,title={Keep {GPU}}}")
        result = self.plan(field_edits=(("k", "title", "Keep {GPU}", "Keep {GPU}"),))
        self.assertEqual(result[:2], ({}, []))
        self.assertEqual(self.code(result[2]), "BIB205")
        for edit in (
            ("k", "title", "Wrong", "Replacement"),
            ("missing", "title", None, "Replacement"),
            ("k", "year", "2020", "2021"),
            ("k", "title", "Keep {GPU}", "Unbalanced {brace"),
            ("k", "title", "Keep {GPU}", "value},evil={injection"),
        ):
            with self.subTest(edit=edit):
                self.assert_blocked(self.plan(field_edits=(edit,)), "BIB205", "failed")

    def test_guarded_field_edits_refuse_macros_concatenation_and_repeated_fields(self) -> None:
        for value in (
            '@string{t="Title"}@misc{k,title=t}',
            "@misc{k,title={A} # {B}}",
            "@misc{k,title={A},title={B}}",
        ):
            with self.subTest(value=value):
                self.write("references.bib", value)
                self.assert_blocked(self.plan(field_edits=(("k", "title", "A", "C"),)), "BIB205")

    def test_ordering_retains_prefix_macros_and_exact_entry_bytes(self) -> None:
        original = (
            '\ufeff% library header\r\n@string{venue="Venue"}\r\n'
            "@misc{z,title={Z},journal=venue}\r\n\r\n"
            '@misc{a, % internal comment\r\n title="A"}\r\n'
        )
        path = self.write("references.bib", original)
        proposed, _, rows = self.plan(order_entries=True)
        output = proposed["references.bib"].decode()
        self.assertEqual(self.code(rows), "BIB206")
        expected = (
            '\ufeff% library header\r\n@string{venue="Venue"}\r\n'
            '@misc{a, % internal comment\r\n title="A"}\r\n\r\n'
            "@misc{z,title={Z},journal=venue}\r\n"
        )
        self.assertEqual(output, expected)
        self.assertEqual(path.read_bytes(), original.encode())
        path.write_bytes(proposed["references.bib"])
        result = self.plan(order_entries=True)
        self.assertEqual(result[:2], ({}, []))
        self.assertEqual(result[2][0].status, "passed")

    def test_ordering_refuses_ambiguous_comment_macro_and_crossref_boundaries(self) -> None:
        for value in (
            "@misc{z}\n% about the next entry\n@misc{a}",
            '@misc{z}\n@string{venue="Changed"}\n@misc{a}',
            "@misc{zchild,crossref={aparent}}\n@proceedings{aparent}",
            "@misc{zchild,crossref=parent}\n@misc{a}",
        ):
            with self.subTest(value=value):
                self.write("references.bib", value)
                self.assert_blocked(self.plan(order_entries=True), "BIB206")

    def test_all_requested_operations_are_atomic_and_unselected_files_stay_untouched(self) -> None:
        self.manuscript("\\cite{old}")
        path = self.write("references.bib", "@misc{old,note={private}}\n@misc{collision}")
        outside = self.write("not_selected.bib", "@misc{outside,note={keep},title={")
        before, outside_before = path.read_bytes(), outside.read_bytes()
        result = self.plan(
            remove_fields=("note",), format_entries=True, key_renames=(("old", "collision"),)
        )
        self.assert_blocked(result, "BIB204", "failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(outside.read_bytes(), outside_before)
        result = self.plan(remove_fields=("note",), format_entries=True)
        self.assertEqual(set(result[0]), {"references.bib"})

    def test_resource_limits_and_links_refuse_proposals(self) -> None:
        self.write("references.bib", "@misc{k,title={A}}")
        with patch("latexprep.bibliography_transform._MAX_TOTAL_BYTES", 5):
            self.assert_blocked(self.plan(format_entries=True), "BIB201")
        link = self.root / "alias.bib"
        link.symlink_to(self.root / "references.bib")
        self.manuscript(resources="alias")
        self.assert_blocked(self.plan(format_entries=True), "BIB201")

    def test_imported_citations_are_retained_and_renamed_in_their_actual_sources(self) -> None:
        self.manuscript("\\import{chapters/}{body}")
        self.write("chapters/body.tex", "\\cite{imported}\\subimport{sub/}{details}")
        self.write("chapters/sub/details.tex", "\\nocite{nested}")
        self.write("references.bib", "@misc{imported}\n@misc{nested}\n@misc{unused}\n")
        proposed, _, rows = self.plan(cited_only=True, key_renames=(("imported", "new_imported"),))
        self.assertEqual([row.status for row in rows], ["passed", "passed"])
        self.assertIn(b"\\cite{new_imported}", proposed["chapters/body.tex"])
        parsed = self.parsed(proposed["references.bib"])
        self.assertEqual({entry.key for entry in parsed.entries}, {"new_imported", "nested"})

    def test_imported_resource_declarations_contribute_to_the_selected_graph(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\\begin{document}\\import{chapters/}{body}\\end{document}",
        )
        self.write("chapters/body.tex", "\\cite{kept}\\bibliography{references}")
        self.write("references.bib", "@misc{kept}\n@misc{unused}\n")
        proposed, _, rows = self.plan(cited_only=True)
        self.assertEqual(self.code(rows), "BIB203")
        self.assertEqual(rows[0].status, "passed")
        parsed = self.parsed(proposed["references.bib"])
        self.assertEqual({entry.key for entry in parsed.entries}, {"kept"})

    def test_subfile_citation_context_is_inconclusive_before_any_prune_or_rename(self) -> None:
        self.manuscript("\\subfile{part}")
        self.write(
            "part.tex",
            "\\documentclass[main.tex]{subfiles}\\begin{document}\\cite{kept}\\end{document}",
        )
        self.write("references.bib", "@misc{kept}\n@misc{unused}\n")
        for options, code in (
            ({"cited_only": True}, "BIB203"),
            ({"key_renames": (("kept", "renamed"),)}, "BIB204"),
        ):
            with self.subTest(options=options):
                result = self.plan(**options)
                self.assert_blocked(result, code)
                self.assertIn("subfile", result[2][0].message)

    def test_options_reject_ambiguous_or_structural_requests(self) -> None:
        for options in (
            {"format_entries": 1},
            {"cited_only": "true"},
            {"order_entries": None},
            {"remove_fields": ["note"]},
            {"remove_fields": ("crossref",)},
            {"remove_fields": ("note", "note")},
            {"remove_fields": ("Title",)},
            {"key_renames": (("a", "\\dynamic"),)},
            {"key_renames": (("a", "*"),)},
            {"key_renames": (("a", "b"), ("a", "c"))},
            {"field_edits": (("k", "ids", None, "alias"),)},
            {"field_edits": (("k", "title", None, 1),)},
            {"field_edits": (("k", "title", "a", "b"), ("k", "title", "b", "c"))},
            {"remove_fields": ("note",), "field_edits": (("k", "note", "a", "b"),)},
        ):
            with self.subTest(options=options), self.assertRaises(PreparationError):
                replace(self.options, **options)


if __name__ == "__main__":
    unittest.main()
