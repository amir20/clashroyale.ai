"""Rook: Clash Royale deck analytics and an AI coach.

Where things live, in the order data flows through them:

    collect/    game API client and the crawler
    storage/    SQLite database, Parquet snapshot, schema upgrades
    analysis/   deck statistics, per-player insights, data for visuals
    coach/      Rook's voice, the AI loop, his tools, and his prompts (.txt)
    web/        FastAPI routes, the player-loading stream, usage limits
    cli/        the `crai` commands
    mcp_server  the same data as tools for Claude
    config      settings from the environment and .env
"""
