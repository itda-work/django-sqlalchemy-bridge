"""Benchmarks behind the README's "성능과 메모리" section.

    uv run --group bench python -m bench [scenario ...] [--db sqlite|postgres]

Each run writes ``bench/results/<date>-<db>-<scenario>.json`` with every raw
timing next to the versions, database server and machine it ran on. The README
tables are rendered from those files by :mod:`bench.readme`.
"""
