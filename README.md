<img src="https://raw.githubusercontent.com/NicolasSchuler/fledge/main/docs/_static/logo.png" width="128" height="128" alt="Fledge logo: an ink-blue origami swallow with an orange fold">

# Fledge

Fledge turns a LaTeX project into a verified submission ZIP. It copies only the
files your paper needs, rebuilds the PDF from that exact ZIP in a sandbox, checks
that it matches your original, and reports problems such as missing files,
undefined citations and leftover TODOs. Your project is never edited.

## Install

On macOS, run the installer from a clone of a release tag. It asks before
installing anything and sets up TeX and Poppler too:

```sh
git clone --branch v0.3.1 https://github.com/NicolasSchuler/fledge.git
cd fledge
bash install.sh
```

Elsewhere, or if TeX and Poppler are already installed, install the same tag
with pipx (or `uv tool install` with the same argument):

```sh
pipx install "git+https://github.com/NicolasSchuler/fledge.git@v0.3.1"
```

Use the newest tag on the [tags page](https://github.com/NicolasSchuler/fledge/tags).
Fledge is not on PyPI yet; a release there as `fledge-latex` is planned. The
PyPI package named `fledge` is an unrelated project. Builds need TeX and Poppler;
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
