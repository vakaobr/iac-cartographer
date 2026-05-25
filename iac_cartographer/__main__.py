"""`python -m iac_cartographer` entrypoint — defers to the console-script `main()`."""

from __future__ import annotations

from iac_cartographer.cli import main

raise SystemExit(main())
