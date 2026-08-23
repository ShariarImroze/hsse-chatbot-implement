"""Local retrieval-augmented chatbot for the HSSE incident corpus."""

from .config import ChatbotConfig
from .service import ChatService

__all__ = ["ChatService", "ChatbotConfig"]
