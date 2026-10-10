<img src="docs/_static/logo.png" width="128" height="128" alt="Fledge logo: an ink-blue origami swallow with an orange fold">

# Fledge

Fledge turns a LaTeX project into a verified submission ZIP. It copies only the
files your paper needs, rebuilds the PDF from that exact ZIP in a sandbox, checks
that it matches your original, and reports problems such as missing files,
undefined citations and leftover TODOs. Your project is never edited.

## Install on macOS

From this checkout or an extracted source distribution:

```sh
bash install.sh
```

The installer shows its plan and asks before installing anything.

## Prepare your paper

```sh
fledge prepare /path/to/paper --main main.tex --output /path/to/submission
```

Read the [documentation](https://nicolasschuler.github.io/fledge/), starting
with the [quick start](https://nicolasschuler.github.io/fledge/quickstart.html).
