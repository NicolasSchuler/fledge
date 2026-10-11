<img src="https://raw.githubusercontent.com/NicolasSchuler/fledge/main/docs/_static/logo.png" width="128" height="128" alt="Fledge logo: an ink-blue origami swallow with an orange fold">

# Fledge

Fledge turns a LaTeX project into a verified submission ZIP. It copies only the
files your paper needs, rebuilds the PDF from that exact ZIP in a sandbox, checks
that it matches your original, and reports problems such as missing files,
undefined citations and leftover TODOs. Your project is never edited.

## Install

On macOS, run the installer from this checkout or an extracted source
distribution. It asks before installing anything and sets up TeX and Poppler too:

```sh
bash install.sh
```

Elsewhere, or if TeX and Poppler are already installed:

```sh
pipx install fledge-latex   # or: uv tool install fledge-latex
```

Install `fledge-latex`, not `fledge`: the PyPI package named `fledge` is an
unrelated project. The command is still `fledge`. Builds need TeX and Poppler;
see the [installation guide](https://nicolasschuler.github.io/fledge/installation.html)
for Linux, Windows (WSL 2) and manual setup.

## Prepare your paper

```sh
fledge prepare /path/to/paper --main main.tex --output /path/to/submission
```

Read the [documentation](https://nicolasschuler.github.io/fledge/), starting
with the [quick start](https://nicolasschuler.github.io/fledge/quickstart.html).

## License

Fledge is released under the [MIT License](https://github.com/NicolasSchuler/fledge/blob/main/LICENSE).
