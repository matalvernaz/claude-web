"""Build identity stamped into frozen bundles by the release workflow.

A source checkout says ``dev``. ``.github/workflows/release.yml`` rewrites
this file right before PyInstaller runs, so the frozen binary knows which
release it is. ``self_update`` compares ``VERSION`` (a ``vX.Y.Z`` tag)
against the repo's GitHub releases; for a manual ``workflow_dispatch`` build
whose label is not a version it falls back to ``BUILT_AT`` and offers any
versioned release published after the build.
"""

VERSION = "dev"
# ISO-8601 UTC, e.g. "2026-10-07T08:05:47Z". Empty for source checkouts.
BUILT_AT = ""
COMMIT = ""
# windows-x64 | macos-arm64 | linux-x64. Empty for source checkouts.
TARGET = ""
