"""
Base client class for shop clients.
All client implementations must inherit from this class and implement the required methods.
"""
from abc import ABC, abstractmethod
from flask import Request, Response
from typing import Tuple, Optional
from db import get_filtered_files
import logging

logger = logging.getLogger('main')


class BaseClient(ABC):
    """Base class for shop clients implementing common interface for authentication, shop serving, and file delivery."""
    
    # Class variables - should be overridden by subclasses
    CLIENT_NAME = "BaseClient"
    
    # ==================== Initialization ====================
    
    def __init__(self, app_settings: dict, db):
        """Initialize the client with application settings and database."""
        self.app_settings = app_settings
        self.db = db
        logger.debug(f"Initialized {self.CLIENT_NAME} client")
    
    # ==================== Abstract Methods (Required) ====================
    
    @classmethod
    @abstractmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify if the request is from this client type."""
        pass
    
    @abstractmethod
    def error_response(self, error_message: str) -> Response:
        """Generate an error response in the format expected by the client."""
        pass
    
    @abstractmethod
    def info_response(self, info_message: str) -> Response:
        """Generate an info response in the format expected by the client."""
        pass
    
    @abstractmethod
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        pass
    
    # ==================== Public Methods ====================
    
    def handle_request(self, request: Request) -> Response:
        """Handle an incoming HTTP request and route to appropriate handler."""
        method = request.method
        path = request.path
        headers = request.headers
        
        # Route request based on method and path
        if method == "OPTIONS":
            return self._handle_options(path, headers)
        elif method == "GET":
            return self._handle_get(request)
    
    def get_filtered_files(self, content_filter: Optional[str] = None) -> list:
        """Get filtered files from the database based on content type."""
        return get_filtered_files(content_filter)
    
    def log_info(self, message: str):
        """Log an info message with client context."""
        logger.info(f"[{self.CLIENT_NAME}] {message}")
    
    def log_warning(self, message: str):
        """Log a warning message with client context."""
        logger.warning(f"[{self.CLIENT_NAME}] {message}")
    
    def log_error(self, message: str):
        """Log an error message with client context."""
        logger.error(f"[{self.CLIENT_NAME}] {message}")
    
    # ==================== Private/Helper Methods ====================
    
    def _handle_options(self, path: str, headers: dict) -> Response:
        """Handle OPTIONS requests for CORS preflight."""
        pass
