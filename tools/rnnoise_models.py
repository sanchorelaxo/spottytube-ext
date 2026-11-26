#!/usr/bin/env python3
"""The five rnnoise voices SpottyTube ships inside the tool.

``arnndn`` is only useful with a ``.rnnn`` model file, and asking a user to
find and download one before they can try noise reduction is a poor first
run. So five small public-domain voices travel with the checkout, next to this
module, and the picker offers them by name. The models carry no licence beyond
public domain (the upstream ``GregorR/rnnoise-models`` set), so there is
nothing to fetch at install time and nothing to account for.

The upstream file names (marathon-prescription, leavened-quisling, ...) say
nothing about what a voice is good for, so each entry carries a plain-language
``label`` describing the training signal and noise instead. This is the order
the picker shows.

The data directory shares this module's name. Python resolves ``import
rnnoise_models`` to this file (a source file wins over an implicit namespace
package), so the two can sit side by side; there is deliberately no
``__init__.py`` in the directory.
"""

from pathlib import Path

# The directory holding the bundled ``.rnnn`` files, alongside this module.
DIR = Path(__file__).resolve().parent / "rnnoise_models"

# The bundled voices, in the order the picker shows them. ``name`` is the
# settings value; ``file`` is the file under :data:`DIR`; ``signal`` and
# ``noise`` record the upstream training split ("general" or "recording" for
# the signal, "general", "voice" or "speech" for the noise) that ``label``
# spells out in plain language.
MODELS = (
    {
        "name": "mp",
        "label": "General noise in general audio",
        "signal": "general",
        "noise": "general",
        "file": "mp.rnnn",
    },
    {
        "name": "lq",
        "label": "Voice in general audio",
        "signal": "general",
        "noise": "voice",
        "file": "lq.rnnn",
    },
    {
        "name": "cb",
        "label": "General noise in a clean recording",
        "signal": "recording",
        "noise": "general",
        "file": "cb.rnnn",
    },
    {
        "name": "bd",
        "label": "Voice in a clean recording",
        "signal": "recording",
        "noise": "voice",
        "file": "bd.rnnn",
    },
    {
        "name": "sh",
        "label": "Speech in a clean recording",
        "signal": "recording",
        "noise": "speech",
        "file": "sh.rnnn",
    },
)


def choices() -> list[dict]:
    """The picker's options: the empty built-in default first, then each voice.

    The empty value means "let the method pick its own default", so it leads
    the list and every bundled name follows in :data:`MODELS` order. Task 3
    feeds this straight into the ``nr_model`` schema field.
    """
    options = [{"value": "", "label": "built-in default"}]
    options.extend({"value": model["name"], "label": model["label"]}
                   for model in MODELS)
    return options


def model_path(value) -> str:
    """Resolve a settings value to a ``.rnnn`` path, or pass it through.

    A blank value (empty or whitespace-only) resolves to ``""``, which the
    chain builder reads as "no model chosen". A value that names a bundled
    voice resolves to that file's absolute path under :data:`DIR`. Anything
    else is returned unchanged, so a user's own downloaded model path is never
    rewritten. The path is never opened or stat'ed here or by the builder.
    """
    text = (value or "").strip()
    if not text:
        return ""
    for model in MODELS:
        if model["name"] == text:
            return str(DIR / model["file"])
    return text
