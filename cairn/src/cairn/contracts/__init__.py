"""Data contracts shared by the server and the dispatcher.

This package depends on nothing from ``cairn.server`` or ``cairn.dispatcher``, so both
sides can import the wire models without dragging in each other's runtime.
"""

# Frozen at 1.0 for the dispatcher/server wire: additive fields may land in minor
# revisions, but existing field names and their meanings never change within 1.x.
# `tests/test_contract_freeze.py` pins the field sets so a rename cannot slip through.
CONTRACT_VERSION = "1.0"
