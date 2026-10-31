"""One project's literal source analysis, built once and shared by the checks that read it.

A check of a document's sources needs the literal graph (``SourceGraph``) of one project
root and main file, and many also need the graph spliced into one document
(``ExpandedDocument``). Each built them on its own, so every check re-read and re-lexed
the same files. ``AnalysedSource`` names that pair for one root and main file and computes
each part on first use; ``AnalysedSourceCache`` hands the same value to every check of a
job that asks for the same root and main file.

Both parts are read-only once built: a check must not change the graph or the document.
Sharing is safe because neither depends on settings, only on the files under the root, so
a root whose files change (a staging copy that is edited between steps) needs a fresh
``AnalysedSource``; the transformations that plan each edit on such a copy build their own
graph. The graph is always the whole-project one (``GraphScope`` "project"): the
selected-scope graph that preparation uses to choose inputs reads fewer files and is
built separately. Checks are written to work stand-alone: they accept an optional
``AnalysedSource`` and build their own when none is passed (:func:`reuse_or_analyse`).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .expanded_document import ExpandedDocument, expand_document
from .source_graph import SourceGraph

logger = logging.getLogger(__name__)


class AnalysedSource:
    """The literal source graph of ``root`` and ``main``, and its expanded document.

    The graph is built on first use of :attr:`graph` and the expanded document on first
    use of :attr:`expanded`; threads that ask at the same time wait for the one build
    instead of repeating it. A build that fails (an unreadable root, a cancelled job) is
    not remembered, so the next caller tries again.

    Attributes:
        root: The project root whose files the analysis reads.
        main: The main file as the caller named it; ``None`` selects the project's single
            document root.
    """

    __slots__ = ("_expanded", "_expansion_lock", "_graph", "_graph_lock", "main", "root")

    def __init__(self, root: Path, main: str | None) -> None:
        self.root = root
        self.main = main
        self._graph: SourceGraph | None = None
        self._expanded: ExpandedDocument | None = None
        self._graph_lock = threading.Lock()
        self._expansion_lock = threading.Lock()

    @property
    def graph(self) -> SourceGraph:
        """The literal dependency graph; every source file under the root is read once."""
        graph = self._graph
        if graph is None:
            with self._graph_lock:
                graph = self._graph
                if graph is None:
                    started = time.monotonic()
                    graph = self._graph = SourceGraph.build(self.root, self.main)
                    logger.debug(
                        "source graph built: %d files in %.2f s",
                        len(graph.sources),
                        time.monotonic() - started,
                    )
        return graph

    @property
    def expanded(self) -> ExpandedDocument:
        """The graph's selected sources spliced in source order, built from :attr:`graph`."""
        document = self._expanded
        if document is None:
            graph = self.graph
            with self._expansion_lock:
                document = self._expanded
                if document is None:
                    started = time.monotonic()
                    document = self._expanded = expand_document(self.root, self.main, graph)
                    logger.debug(
                        "expanded document built: %d characters in %.2f s",
                        len(document.text),
                        time.monotonic() - started,
                    )
        return document

    def covers(self, root: Path, main: str | None) -> bool:
        """Return whether this analysis describes ``main`` of the project at ``root``.

        Args:
            root: The project root to compare with.
            main: The main file to compare with, as the caller names it.

        Returns:
            ``True`` when both the root and the main file are the ones analysed.
        """
        return self.root == root and self.main == main


def reuse_or_analyse(root: Path, main: str | None, shared: AnalysedSource | None) -> AnalysedSource:
    """Return ``shared`` when a caller passed one, otherwise a new analysis.

    A shared analysis of another root or main file would silently describe the wrong
    project, so it is refused instead of used.

    Args:
        root: The project root to analyse.
        main: The main file as the caller named it; ``None`` selects the single document
            root.
        shared: An analysis the caller already holds, or ``None``.

    Returns:
        ``shared``, or a new, not yet built analysis of ``main`` under ``root``.

    Raises:
        ValueError: ``shared`` describes another root or main file.
    """
    if shared is None:
        return AnalysedSource(root, main)
    if not shared.covers(root, main):
        raise ValueError(
            f"Shared source analysis is for {shared.main!r} under {shared.root}, "
            f"not for {main!r} under {root}"
        )
    return shared


class AnalysedSourceCache:
    """The analyses of one job, one per project root and main file, created on first request.

    The orchestration layer asks for the analysis of each root a task reads, so tasks that
    run concurrently on the same sources share one build. The cache holds the built graphs,
    which can be large; :meth:`clear` releases them once the checks that share them are done.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._analyses: dict[tuple[Path, str | None], AnalysedSource] = {}

    def get(self, root: Path, main: str | None) -> AnalysedSource:
        """Return the shared analysis of ``main`` under ``root``, creating it on first request.

        Nothing is read until the analysis is used.

        Args:
            root: The project root to analyse.
            main: The main file as the caller named it; ``None`` selects the single
                document root.

        Returns:
            The one analysis this cache holds for ``root`` and ``main``.
        """
        with self._lock:
            analysis = self._analyses.get((root, main))
            if analysis is None:
                analysis = self._analyses[root, main] = AnalysedSource(root, main)
                logger.debug(
                    "source analysis cache miss for main %s (%d analyses held)",
                    main or "(the only root)",
                    len(self._analyses),
                )
            else:
                logger.debug("source analysis cache hit for main %s", main or "(the only root)")
            return analysis

    def clear(self) -> None:
        """Forget every analysis; analyses already handed out stay valid."""
        with self._lock:
            self._analyses.clear()
