"""recon2b: match a purchase register against GSTR-2B and find the ITC at risk."""

from .loaders import load_books, load_gstr2b
from .matcher import MatchConfig, ReconResult, reconcile
from .pipeline import run
from .report import write_excel, write_flat

__version__ = "0.1.0"
__all__ = [
    "MatchConfig",
    "ReconResult",
    "load_books",
    "load_gstr2b",
    "reconcile",
    "run",
    "write_excel",
    "write_flat",
]
