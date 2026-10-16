# Configuration file for the Sphinx documentation builder.


import os
import sys

sys.path.insert(0, os.path.abspath('../../'))

# Django setup
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")  # replace if needed
import django
django.setup()

# -- Project information -----------------------------------------------------

project = 'Autofx'
copyright = '2025, Richard Mlambuzi'
author = 'Richard Mlambuzi'
release = '30 may 2025'

# -- General configuration ---------------------------------------------------

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx_autodoc_typehints',
]

templates_path = ['_templates']
exclude_patterns = []


# -- Options for HTML output -------------------------------------------------

html_theme = 'alabaster'
html_static_path = ['_static']

# -- Options for LaTeX output ------------------------------------------------

latex_elements = {
    'maketitle': r'''
        \pagenumbering{roman}
        \begin{titlepage}
        \centering
        \vspace*{40mm}
        {\Huge \bfseries \sffamily Autofx Documentation \par}
        \vspace{10mm}
        {\LARGE \sffamily Release \today \par}
        \vfill
        {\large Authors: Richard Mlambuzi}
        \end{titlepage}
        \pagenumbering{arabic}
    ''',
    'classoptions': ',openany,oneside',
}

