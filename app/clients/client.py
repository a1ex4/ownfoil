"""
Base client class for shop clients.
All client implementations must inherit from this class and implement the required methods.
"""
from abc import ABC, abstractmethod
from flask import Request, Response
from typing import Tuple, Optional, Dict, Any
from functools import wraps
from db import get_filtered_files
from auth import basic_auth
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

    # ==================== Authentication Decorator ====================

    @staticmethod
    def authenticate(handler):
        """Decorator that handles authentication for handle_<method> functions."""
        @wraps(handler)
        def wrapper(self, request: Request) -> Response:
            # Initialize auth flags on request object
            request.basic_auth_success = False
            request.basic_auth_error = None
            request.client_auth_success = False
            request.client_auth_error = None
            request.user = None
            request.auth_data = {}

            # Perform host verification only for HTTPS requests
            if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
                shop_host = self.app_settings["shop"].get("host")
                if not shop_host:
                    self.log_error("Missing shop host configuration, Host verification is disabled.")
                elif request.host != shop_host:
                    return self.error_response(f"Incorrect URL referrer detected: {request.host}.")

            # Generic Basic Auth
            basic_auth_success, basic_auth_error, user = basic_auth(request)
            if not basic_auth_success:
                request.basic_auth_success = False
                request.basic_auth_error = basic_auth_error
                self.log_warning(f"Authentication failed: {basic_auth_error}")

            else:
                request.basic_auth_success = True
                self.log_info(f"successful authentication for user {user.user}")
            request.user = user

            # Client-specific authentication
            client_auth_success, client_auth_error, client_auth_data = self._client_authenticate(request)
            if not client_auth_success:
                request.client_auth_success = False
                request.client_auth_error = client_auth_error
                self.log_warning(f"Client-specific auth failed: {client_auth_error}")
            else:
                request.client_auth_success = True
                self.log_info("Client-specific authentication successful.")
                if client_auth_data:
                    request.auth_data.update(client_auth_data)

            # Call the actual handler
            return handler(self, request)

        return wrapper

    @staticmethod
    def verify_shop_access(handler):
        """Decorator that enforces authenticated access to the shop."""
        @wraps(handler)
        def wrapper(self, request: Request) -> Response:
            # Check if shop requires authentication
            if not self.app_settings['shop']['public']:
                if not request.basic_auth_success:
                    return self.error_response("Shop requires authentication.\n" + (request.basic_auth_error))
                # Check if user has shop access
                if request.user and not request.user.has_shop_access():
                    return self.error_response(f'User "{request.user.user}" does not have access to the shop.')

            # Call the actual handler
            return handler(self, request)

        return wrapper

    def _client_authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Client-specific authentication logic. Override in subclasses for custom behavior.

        Returns:
            Tuple of (success: bool, error_message: Optional[str], auth_data: Optional[Dict])
            - success: True if authentication passed
            - error_message: Error message if authentication failed
            - auth_data: Additional authentication data to be stored in request.auth_data
        """
        # Default implementation: no additional authentication required
        return True, None, {}

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
