# utils.py
from pylatexenc.latex2text import LatexNodes2Text
import logging

def decode_author_name(name: str) -> str:
    """Converts LaTeX-style encoded strings to proper Unicode."""
    try:
        return LatexNodes2Text().latex_to_text(name)
    except Exception as e:
        logging.warning(f"Failed to decode LaTeX author name '{name}': {e}")
        return name # Return original name on failure