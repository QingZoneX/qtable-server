"""
OAuth module - Modular interface
"""
from .endpoints import router

# Backward compatibility: expose router at package level
__all__ = ["router"]
