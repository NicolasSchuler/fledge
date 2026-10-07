#!/bin/bash
# Install from this checkout or an extracted source archive. Requires macOS.
# Package references: https://formulae.brew.sh/cask/mactex-no-gui
# https://formulae.brew.sh/formula/{python@3.13,poppler,tex-fmt,qpdf,mupdf-tools}

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash install.sh [--dry-run] [--yes] [--with-optional]
                       [--prefix DIR] [--bin-dir DIR]
       bash install.sh --uninstall [--dry-run] [--yes]
                       [--prefix DIR] [--bin-dir DIR]

Install Fledge from this local source directory on macOS.
Reuse existing Python 3.11+, TeX and Poppler tools. Install missing tools using
an existing Homebrew installation; see https://brew.sh if Homebrew is missing.

  --dry-run        Print the plan without writing files or installing anything.
  --yes            Accept the displayed plan without an interactive question.
  --with-optional  Also install missing tex-fmt, qpdf and MuPDF (mutool).
  --prefix DIR     New private installation directory.
                   Default: ~/.local/share/fledge
  --bin-dir DIR    Launcher directory. Default: ~/.local/bin
  --uninstall      Remove the installation directory and launcher that this
                   script created, using the same --prefix and --bin-dir. Only
                   a launcher that runs that directory's environment qualifies.
                   Homebrew and TeX packages are left installed.
  --help           Show this help.

To upgrade, run --uninstall, then run the installer again from the newer source.

The default plan may install the full MacTeX distribution: a multi-GB download
and system installation requiring administrator approval. Homebrew and MacTeX
manage their own system files; this script never edits shell startup files.
Existing installation directories or launchers are never overwritten.
The launcher saves only the directories that supplied the selected TeX, Poppler
and optional tools, in the order they were searched, followed by the fallback
directories and the system directories. The rest of this terminal's PATH,
such as a virtual environment or project directory, is not saved.
Linux and Windows are not supported by this installer. It does not validate
a document build.
EOF
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

report_failure() {
    printf 'Installation failed while %s (exit %s). Any completed dependency installs or partial environment are retained; inspect them before retrying.\n' "$2" "$1" >&2
    exit "$1"
}

# Keep detection separate from installation so it can be exercised with mocks.
find_tool() {
    local name="$1" candidate directory
    candidate=$(type -P "$name" || true)
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        [[ "$candidate" = /* ]] || candidate="$PWD/$candidate"
        printf '%s\n' "$candidate"
        return 0
    fi
    for directory in "${tool_dirs[@]}"; do
        if [[ -x "$directory/$name" ]]; then
            printf '%s\n' "$directory/$name"
            return 0
        fi
    done
    return 1
}

find_python() {
    local name candidate
    for name in python3 python3.14 python3.13 python3.12 python3.11; do
        candidate=$(find_tool "$name" || true)
        if [[ -n "$candidate" ]] && "$candidate" -c \
            'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 11))' \
            >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

confirm_plan() {
    local answer
    [[ -t 0 ]] || fail "Confirmation requires a terminal. Review --dry-run, then use --yes to accept the plan."
    read -r -p 'Proceed with this plan? [y/N] ' answer
    case "$answer" in
        y|Y|yes|YES) return 0 ;;
        *) printf 'Cancelled. No changes made.\n'; return 1 ;;
    esac
}

# Print the directories find_tool searches, in its order: every PATH entry made
# absolute exactly as find_tool does, then the fallback directories.
search_dirs() {
    local remaining_path="${PATH-}" entry directory
    while :; do
        entry="${remaining_path%%:*}"
        [[ "$entry" = /* ]] || entry="$PWD/${entry:-.}"
        printf '%s\n' "$entry"
        [[ "$remaining_path" = *:* ]] || break
        remaining_path="${remaining_path#*:}"
    done
    for directory in "${tool_dirs[@]}"; do
        printf '%s\n' "$directory"
    done
}

# Print the PATH to save in the launcher. Arguments are the selected tool paths.
# Keep only the directories that supplied them, in find_tool's search order
# (PATH first, then the fallbacks), so every tool still resolves to the copy
# that was selected. Order by directory, not by tool: the directory supplying
# latexmk can also contain an unselected pdflatex that must stay behind the
# directory supplying the selected one. The fallback and system directories
# follow; duplicates, including aliases of one directory, keep their first place.
launcher_path_for() {
    local selected=() ordered=() kept=() joined='' found directory entry duplicate
    for found in "$@"; do
        directory="${found%/*}"
        selected+=("${directory:-/}")
    done
    while IFS= read -r entry; do
        for directory in "${selected[@]}"; do
            if [[ "$entry" -ef "$directory" ]]; then ordered+=("$entry"); break; fi
        done
    done < <(search_dirs)
    # A selected directory outside the search list cannot happen with find_tool.
    ordered+=("${selected[@]}" "${tool_dirs[@]}" /usr/bin /bin /usr/sbin /sbin)
    for directory in "${ordered[@]}"; do
        duplicate=false
        for entry in ${kept[@]+"${kept[@]}"}; do
            if [[ "$directory" = "$entry" || "$directory" -ef "$entry" ]]; then
                duplicate=true
                break
            fi
        done
        if ! "$duplicate"; then
            kept+=("$directory")
            joined="${joined:+$joined:}$directory"
        fi
    done
    printf '%s\n' "$joined"
}

# The exec line of the launcher for a prefix. Writing and recognizing it share
# this function, so uninstalling accepts exactly what installing creates.
launcher_exec_line() {
    # shellcheck disable=SC2016 # The launcher expands "$@" itself.
    printf 'exec %q "$@"\n' "$1/venv/bin/fledge"
}

launcher_belongs_to() {
    [[ -f "$1" && ! -L "$1" ]] && grep -qxF -- "$(launcher_exec_line "$2")" "$1"
}

uninstall_fledge() {
    local prefix="$1" launcher="$2" dry_run="$3" accept="$4"
    case "$HOME/" in
        "$prefix"/*) fail "Refusing to remove $prefix: it is or contains your home directory." ;;
    esac
    [[ ! -L "$prefix" ]] ||
        fail "Installation directory is a symbolic link: $prefix. Nothing was removed; delete the link yourself if it is unwanted."
    [[ ! -e "$prefix" || -d "$prefix" ]] ||
        fail "Installation path is not a directory: $prefix. Nothing was removed."
    launcher_belongs_to "$launcher" "$prefix" ||
        fail "Launcher $launcher is missing or does not run $prefix/venv/bin/fledge. Nothing was removed; pass the --prefix and --bin-dir used for installation."
    printf 'Uninstall plan for Fledge:\n  Remove launcher: %s\n' "$launcher"
    if [[ -d "$prefix" ]]; then
        printf '  Remove installation directory and everything inside it: %s\n' "$prefix"
    else
        printf '  Installation directory is already absent: %s\n' "$prefix"
    fi
    printf '  Homebrew, TeX and Poppler packages, shell startup files and projects are not touched.\n'
    if "$dry_run"; then printf '\nDry run complete. Nothing removed.\n'; return; fi
    if ! "$accept"; then confirm_plan || return 0; fi
    # Recheck after confirmation. Remove the environment first: while it remains,
    # the launcher still proves ownership, so a partial removal can be repeated.
    launcher_belongs_to "$launcher" "$prefix" ||
        fail "The launcher changed after the plan was shown. Nothing was removed."
    rm -rf -- "$prefix"
    rm -f -- "$launcher"
    printf '\nFledge removed. To install it again, rerun this script without --uninstall.\n'
}

check_destinations() {
    [[ ! -e "$prefix" && ! -L "$prefix" ]] ||
        fail "Installation directory already exists: $prefix. Choose a new --prefix; nothing was overwritten."
    [[ ! -e "$launcher" && ! -L "$launcher" ]] ||
        fail "Launcher already exists: $launcher. Choose another --bin-dir; nothing was overwritten."
    [[ ! -e "$bin_dir" || -d "$bin_dir" ]] ||
        fail "Launcher directory is not a directory: $bin_dir"
}

main() {
    local dry_run=false accept=false optional=false uninstall=false
    local prefix="${HOME:?HOME is not set}/.local/share/fledge"
    local bin_dir="$HOME/.local/bin" argument
    while [[ $# -gt 0 ]]; do
        argument="$1"
        case "$argument" in
            --dry-run) dry_run=true; shift ;;
            --yes) accept=true; shift ;;
            --with-optional) optional=true; shift ;;
            --uninstall) uninstall=true; shift ;;
            --prefix|--bin-dir)
                [[ $# -ge 2 && -n "$2" && "$2" != --* ]] ||
                    fail "$argument requires a directory."
                if [[ "$argument" = --prefix ]]; then prefix="$2"; else bin_dir="$2"; fi
                shift 2 ;;
            --help|-h) usage; return ;;
            *) fail "Unknown option: $argument. Use --help." ;;
        esac
    done
    if "$uninstall" && "$optional"; then fail "--with-optional only applies to installation."; fi
    [[ "$(uname -s)" = Darwin ]] ||
        fail "This installer supports macOS only. Linux and Windows require manual setup; see docs/installation.md."
    [[ "$(id -u)" != 0 ]] || fail "Run this script as your normal user, without sudo."

    [[ "$prefix" = /* ]] || prefix="$PWD/$prefix"
    [[ "$bin_dir" = /* ]] || bin_dir="$PWD/$bin_dir"
    prefix="${prefix%/}"
    bin_dir="${bin_dir%/}"
    [[ -n "$prefix" && -n "$bin_dir" ]] || fail "The filesystem root is not an installation directory."
    local launcher="$bin_dir/fledge" venv="$prefix/venv"
    if "$uninstall"; then
        uninstall_fledge "$prefix" "$launcher" "$dry_run" "$accept"
        return
    fi
    local source_dir
    source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    [[ -f "$source_dir/pyproject.toml" && -f "$source_dir/src/latexprep/cli.py" ]] ||
        fail "Keep install.sh inside the complete checkout or extracted source archive."
    check_destinations

    local tool_dirs=(/Library/TeX/texbin /opt/homebrew/bin /usr/local/bin)
    local brew python sandbox tool found
    local formulas=() tex_missing=() pdf_missing=()
    local tex_tools=(latexmk pdflatex bibtex)
    local pdf_tools=(pdfinfo pdftotext pdftoppm pdffonts pdfdetach pdfimages pdftohtml)
    local optional_tools=(tex-fmt qpdf mutool)
    local optional_formulas=(tex-fmt qpdf mupdf-tools)
    local install_tex=false partial_tex=false index
    brew=$(find_tool brew || true)
    if [[ -n "$brew" ]]; then tool_dirs+=("${brew%/*}"); fi
    sandbox=$(find_tool sandbox-exec || true)
    [[ -n "$sandbox" ]] || fail "sandbox-exec is missing; this macOS build-isolation backend is required."
    python=$(find_python || true)
    [[ -n "$python" ]] || formulas+=(python@3.13)
    printf 'Dependency check:\n'
    if [[ -n "$python" ]]; then printf '  Reuse Python: %s\n' "$python"; fi
    for tool in "${tex_tools[@]}"; do
        found=$(find_tool "$tool" || true)
        if [[ -n "$found" ]]; then
            printf '  Reuse %s: %s\n' "$tool" "$found"
            partial_tex=true
        else tex_missing+=("$tool"); fi
    done
    if [[ ${#tex_missing[@]} -gt 0 ]]; then
        for tool in tex latex kpsewhich tlmgr; do
            if find_tool "$tool" >/dev/null; then partial_tex=true; fi
        done
        if "$partial_tex"; then
            fail "Existing TeX installation is incomplete (missing: ${tex_missing[*]}). Add those tools with that distribution's package manager and rerun; this installer will not replace it."
        fi
        install_tex=true
    fi
    for tool in "${pdf_tools[@]}"; do
        found=$(find_tool "$tool" || true)
        if [[ -n "$found" ]]; then
            printf '  Reuse %s: %s\n' "$tool" "$found"
        else pdf_missing+=("$tool"); fi
    done
    [[ ${#pdf_missing[@]} -eq 0 ]] || formulas+=(poppler)
    if "$optional"; then
        for index in 0 1 2; do
            tool="${optional_tools[$index]}"
            found=$(find_tool "$tool" || true)
            if [[ -n "$found" ]]; then
                printf '  Reuse %s: %s\n' "$tool" "$found"
            else formulas+=("${optional_formulas[$index]}"); fi
        done
    fi

    printf '\nInstallation plan for Fledge:\n  Source: %s\n  Private environment: %s\n  Launcher: %s\n' \
        "$source_dir" "$venv" "$launcher"
    if "$install_tex"; then
        printf '  MacTeX: multi-GB download and substantial disk space; administrator password required.\n'
        printf '  Its package installer writes system TeX files and command-path registration.\n'
        print_command "${brew:-brew}" install --cask mactex-no-gui
    fi
    if [[ ${#formulas[@]} -gt 0 ]]; then
        printf '  Homebrew downloads missing packages and their dependencies:\n'
        print_command "${brew:-brew}" install "${formulas[@]}"
    fi
    printf '  Install this local Python package and its dependencies (downloads may be needed):\n'
    print_command "${python:-python3.13}" -m venv "$venv"
    print_command "$venv/bin/python" -m pip install --disable-pip-version-check "$source_dir"
    printf '  Create a launcher saving only the directories of the selected tools in discovery order,\n'
    printf '  then fallback and system directories. The rest of this terminal\047s PATH is not saved.\n'
    printf '  Saved directories are absolute; no shell startup files are edited.\n'
    if "$optional"; then
        printf '  Optional tools requested: tex-fmt, qpdf and mutool.\n'
    else
        printf '  Missing optional tools left out; use --with-optional for tex-fmt, qpdf and mutool.\n'
        printf '  Optional tools that are already installed are still added to the launcher PATH.\n'
    fi
    printf '  Builds still require working isolation and project-specific TeX packages.\n'
    printf '  This installer does not validate document builds.\n'
    if [[ -z "$brew" ]] && { "$install_tex" || [[ ${#formulas[@]} -gt 0 ]]; }; then
        fail "Homebrew is required for missing dependencies. Install it using https://brew.sh, then rerun this command."
    fi
    if "$dry_run"; then printf '\nDry run complete. No files written or packages installed.\n'; return; fi
    if ! "$accept"; then confirm_plan || return 0; fi
    # Recheck immediately before the first writes, including after confirmation.
    check_destinations
    local current_step='installing external dependencies'
    trap 'report_failure "$?" "$current_step"' ERR
    if "$install_tex"; then "$brew" install --cask mactex-no-gui; fi
    if [[ ${#formulas[@]} -gt 0 ]]; then "$brew" install "${formulas[@]}"; fi
    hash -r
    python=$(find_python || true)
    [[ -n "$python" ]] || fail "Python 3.11+ with venv and ensurepip is still unavailable after installation."
    local required_tools=("${tex_tools[@]}" "${pdf_tools[@]}")
    local selected_tools=()
    if "$optional"; then required_tools+=("${optional_tools[@]}"); fi
    for tool in "${required_tools[@]}"; do
        found=$(find_tool "$tool" || true)
        [[ -n "$found" ]] || fail "$tool is still unavailable after installation. Inspect Homebrew's output and rerun once it is on PATH."
        selected_tools+=("$found")
    done
    if ! "$optional"; then
        # Not required, but keep an existing tex-fmt, qpdf or mutool reachable.
        for tool in "${optional_tools[@]}"; do
            found=$(find_tool "$tool" || true)
            if [[ -n "$found" ]]; then selected_tools+=("$found"); fi
        done
    fi
    current_step='creating the private Python environment'
    mkdir -p -- "${prefix%/*}"
    mkdir -- "$prefix"
    "$python" -m venv "$venv"
    current_step='installing the local Python package'
    "$venv/bin/python" -m pip install --disable-pip-version-check "$source_dir"
    "$venv/bin/fledge" --help >/dev/null
    current_step='creating the launcher'
    local launcher_path
    launcher_path=$(launcher_path_for "${selected_tools[@]}")
    mkdir -p -- "$bin_dir"
    # noclobber also refuses a file that appeared since the preflight check.
    (
        set -o noclobber
        {
            printf '#!/bin/bash\n'
            printf 'export PATH=%q\n' "$launcher_path"
            launcher_exec_line "$prefix"
        } > "$launcher"
    )
    chmod +x "$launcher"
    current_step='verifying the launcher'
    "$launcher" --help >/dev/null
    trap - ERR
    printf '\nFledge installed. Run from any directory:\n'
    print_command "$launcher" --help
    printf 'For the short command in this terminal only, run:\n'
    # shellcheck disable=SC2016 # Print a command for the user's shell.
    printf '  export PATH=%q:"$PATH"\n' "$bin_dir"
    printf '  fledge --help\n'
}

if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then main "$@"; fi
