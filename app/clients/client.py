"""
Base client class for shop clients.
All client implementations must inherit from this class and implement the required methods.
"""
from abc import ABC, abstractmethod
from flask import Request, Response
from typing import Tuple, Optional, Any
import logging

logger = logging.getLogger('main')


class BaseClient(ABC):
    """Base class for shop clients implementing common interface for authentication, shop serving, and file delivery."""
    
    # Client identifier - should be overridden by subclasses
    CLIENT_NAME = "BaseClient"
    
    def __init__(self, app_settings: dict, db):
        """Initialize the client with application settings and database."""
        self.app_settings = app_settings
        self.db = db
        logger.debug(f"Initialized {self.CLIENT_NAME} client")
    
    @classmethod
    @abstractmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify if the request is from this client type."""
        pass
    
    @abstractmethod
    def authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Authenticate the request and return (success, error_message, verified_host)."""
        pass
    
    @abstractmethod
    def serve_shop(self, request: Request, verified_host: Optional[str] = None) -> Response:
        """Generate and serve the shop listing response."""
        pass
    
    @abstractmethod
    def error_response(self, error_message: str) -> Response:
        """Generate an error response in the format expected by the client."""
        pass
    
    @abstractmethod
    def info_response(self, info_message: str) -> Response:
        """Generate an info response in the format expected by the client."""
        pass
    
    def log_info(self, message: str):
        """Log an info message with client context."""
        logger.info(f"[{self.CLIENT_NAME}] {message}")
    
    def log_warning(self, message: str):
        """Log a warning message with client context."""
        logger.warning(f"[{self.CLIENT_NAME}] {message}")
    
    def log_error(self, message: str):
        """Log an error message with client context."""
        logger.error(f"[{self.CLIENT_NAME}] {message}")
