"""Headless, robust Kick drops miner engine + runtime.

Independent of the legacy ``core``/``ui`` packages. See ARCHITECTURE.md for the
module contract. Submodules are imported explicitly by callers (no eager imports
here, so importing the package never requires a browser to be installed)."""
