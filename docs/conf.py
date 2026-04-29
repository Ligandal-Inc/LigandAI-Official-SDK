# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Sphinx configuration for the LIGANDAI Python SDK docs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ligandai._version import __version__  # noqa: E402

# -- Project information --

project = "LIGANDAI Python SDK"
author = "Andre Watson"
copyright = "2025, Ligandal, Inc."
version = __version__
release = __version__

# -- General configuration --

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "autoapi.extension",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- AutoAPI --

autoapi_dirs = ["../ligandai"]
autoapi_root = "api"
autoapi_python_class_content = "both"
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    "imported-members",
]
autoapi_keep_files = False

# -- Napoleon --

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False

# -- Intersphinx --

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "httpx": ("https://www.python-httpx.org/", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}

# -- HTML output --

html_theme = "sphinx_rtd_theme"
html_title = f"LIGANDAI Python SDK {version}"
html_static_path = ["_static"]
html_show_sourcelink = False
