"""
ZenfusionSEO — Open Source SEO/AEO/CRO Toolkit
Main package init.
"""

__version__ = "1.0.0"
__author__ = "Zenfusion"
__license__ = "MIT"

# Import key modules for convenience
try:
    from .cli import main
except ImportError:
    pass