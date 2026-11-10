"""Public stub for the proprietary assessment client.

The real module lives in unchained-core-private and is overlaid at deploy time;
it is never importable from the public boundary.
"""

raise RuntimeError(
    "jev_client.py is proprietary and lives in unchained-core-private. "
    "For full functionality, mount private core or run private-integrated CI."
)
