"""Hourly watch on a list of stocks, for deciding whether to manage a position.

Not a trading scanner. It polls roughly once an hour, asks "did anything
notable happen in a name I hold?", and tells you what it saw and what it could
not tell.
"""

__version__ = "2.0.0"
