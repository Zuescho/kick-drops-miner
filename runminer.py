"""Thin entrypoint for the headless Kick drops miner. See miner/ARCHITECTURE.md."""
from miner.runner import main
import sys

sys.exit(main())
