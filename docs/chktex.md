# Lint with chktex

[chktex](https://www.nongnu.org/chktex/) is the LaTeX lint tool that ships with TeX Live
and MacTeX. Fledge can run it on your sources and list its warnings as
[suggestions](reports.md#suggestions): optional advice in a section of its own, which never
changes the result or the exit code.

## Turn it on

The lint is off by default, and `ALL` does not switch it on. Select its check, `CHK001`,
beside the checks you run:

```sh
fledge inspect paper --select ALL --select CHK001
```

or in your settings file:

```toml
[checks]
select = ["ALL", "CHK001"]
```

`inspect`, `check` and `prepare` all run it; it needs no build. Each warning is listed at
its file and line with chktex's number and message:

```text
Suggestions (they never change the result or the exit code)
  CHK001  chktex warning  main.tex:4 [chktex lint]
    Non-breaking space (`~') should have been used. (chktex warning 2)
```

A report lists at most 200 warnings and counts the rest. In JSON, each warning's `details`
hold chktex's `chktex_number`, `chktex_kind`, `chktex_message` and `column`. When chktex is
not installed, a suggestion says that the lint was not checked and how to install it:
MacTeX and a full TeX Live include chktex; with BasicTeX run `sudo tlmgr install chktex`,
and on Debian or Ubuntu `sudo apt install chktex`.

## Configure it

chktex's own controls work as usual:

- `% chktex 2` at the end of a line keeps warning 2 on that line, and `% chktex-file 2`
  keeps it in the whole file. `% fledge: ignore CHK001` silences every chktex suggestion
  on its line, as for any other suggestion.
- A `.chktexrc` beside the main file, where you would run chktex yourself, is read after
  chktex's built-in settings. For example, to switch two warnings off for the project:

  ```text
  CmdLine { -n1 -n8 }
  ```

Your own `~/.chktexrc` is never read: chktex runs without your home folder, so every
computer reports the same warnings for the same project.

Fledge reads a project `.chktexrc` before chktex does, and refuses it, with a suggestion at
the line concerned, when it could make chktex read another file, write a file or stop
before it checks. `CmdLine` may hold only options that choose warnings or set the output
format, each written on its own: `-wN`, `-eN`, `-mN` and `-nN` (N a warning number or
`all`), `-L`, `-H`, `-x`, `-I`, `-q`, `-r`, `-b`, `-t`, `-v`, `-f` and `-s`. Fledge refuses
`-l` (another configuration file), `-S`, `-o`, `-p`, `-V`, `-d`, `-g`, `-h`, `-i` and `-W`;
folders outside the project in `TeXInputs`; unknown settings and syntax errors; and text
that chktex could read differently from Fledge, such as control characters, lines longer
than 1000 characters or a byte-order mark.

## How it runs, and its limits

- chktex runs in the same sandbox as the TeX tools. It may read read-only copies of the
  TeX files that the main file uses, its own configuration in the TeX distribution (or
  `/etc/chktexrc` for the Debian and Ubuntu package) and an accepted project `.chktexrc`,
  and it may write only into an empty working folder of its own.
- Fledge checks each `.tex`, `.ltx`, `.latex`, `.tikz` and `.pgf` file that the main file
  reaches through the file commands Fledge follows, each in a run of its own, with chktex's
  own `\input` following turned off. Bracket counts are therefore per file: an environment
  that one file opens and another closes is reported in both. Local class and package
  files are not checked, nor are more than 256 files.
- The lint reads the sources as imported. `prepare` does not lint the shipped copy again,
  so comment removal cannot bring back warnings that `% chktex` comments silenced.
- Each run has the time and output limits of every tool (`timeout_seconds`). A run that a
  limit stops ends the lint, and the report names the files left unchecked. A
  `UserWarnRegex` pattern that backtracks can make chktex slow; the time limit bounds it.
- Fledge supports the chktex of TeX Live and MacTeX and the Debian and Ubuntu package.
  Another build that keeps its configuration elsewhere cannot read it in the sandbox and is
  reported as not completing. The lint has been run live under macOS `sandbox-exec`; the
  Linux Bubblewrap grant has not been tested live yet.
- chktex's heuristics produce false positives. Keep the construct with a `% chktex`
  comment, or switch the warning off in `.chktexrc`.
