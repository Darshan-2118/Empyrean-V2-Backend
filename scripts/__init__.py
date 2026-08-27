"""Operational scripts package (M69).

Making ``scripts/`` a proper package lets the modules be imported/run as
``python -m scripts.<name>`` from the project root without relying solely on
the per-file ``sys.path`` bootstrap (which direct ``python scripts/x.py``
invocation still uses).
"""
