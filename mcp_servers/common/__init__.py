"""Shared data layer and business rules for the ShopAssist MCP tools.

What this package does
    Holds the code that both tool servers share: `store.py` (reading and writing orders, returns and
    tickets) and `rules.py` (the return-eligibility policy).

Where it fits
    `scripts/build.py` copies this folder, as the package `common`, into the orders Lambda zip and the
    support MCP server zip, so both deployed services run exactly the same rules and data code.
"""
