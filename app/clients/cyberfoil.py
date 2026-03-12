"""
CyberFoil client implementation.
"""
from flask import Request, Response, jsonify
from typing import Tuple, Optional, Dict, Any
import json

from .client import BaseClient
from settings import set_shop_settings
from constants import APP_TYPE_FILTERS

CYBERFOIL_HEADERS = [
    'Theme',
    'Uid',
    'Version',
    'Revision',
    'Language',
    'Hauth',
    'Uauth'
]

class CyberFoilClient(BaseClient):
    """CyberFoil client with header-based identification, Hauth verification."""

    # Class variables
    CLIENT_NAME = "CyberFoil"

    # ==================== Abstract Method Implementations (Required) ====================

    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify CyberFoil client by checking for required headers."""
        return all(header in request.headers for header in CYBERFOIL_HEADERS) and request.headers.get('User-Agent') == 'cyberfoil'

    def error_response(self, error_message: str) -> Response:
        """Generate CyberFoil error response in JSON format."""
        return jsonify({'error': error_message})

    def info_response(self, info_message: str) -> Response:
        """Generate CyberFoil info response in JSON format."""
        return jsonify({'success': info_message})

    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        # Access auth flags from request object (set by @authenticate decorator)
        if not request.client_auth_success:
            return self.error_response(request.client_auth_error)

        # Get client-specific settings
        client_settings = self.app_settings['shop']['clients']['cyberfoil']

        paths = request.path.strip('/').split('/')
        content_filter = paths[0] if paths and paths[0] in APP_TYPE_FILTERS else None
        # Build shop content
        shop = {"success": self.app_settings['shop']['motd']}
        shop["files"] = self._generate_shop_files(content_filter)

        # Get verified_host from auth_data
        verified_host = request.auth_data.get('verified_host')
        if verified_host:
            # Enforce client side host verification
            shop["referrer"] = f"https://{verified_host}"

        # Serve the shop
        return jsonify(shop)

    # ==================== Private/Helper Methods ====================

    def _client_authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Cyberfoil-specific authentication: Host verification for HTTPS requests."""
        success = True
        error = None
        verified_host = None

        # Perform host verification only for HTTPS requests
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            success, error, verified_host = self._verify_host(request)

        # Return auth data with verified_host
        auth_data = {'verified_host': verified_host}
        return success, error, auth_data

    def _verify_host(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Verify Hauth to prevent hotlinking."""
        request_host = request.host
        request_hauth = request.headers.get('Hauth')
        shop_host = self.app_settings["shop"].get("host")
        client_settings = self.app_settings["shop"]["clients"]["cyberfoil"]
        hauth_dict = client_settings.get("hauth", {})

        # Get hauth for this specific host
        shop_hauth = hauth_dict.get(request_host)

        self.log_info(f"Secure request from remote host {request_host}, proceeding with host verification.")

        if not shop_host:
            self.log_error("Missing shop host configuration, Host verification is disabled.")
            return True, None, None

        if not shop_hauth:
            return self._handle_missing_hauth(request, request_host, request_hauth)

        if request_hauth != shop_hauth:
            self.log_warning(f"Incorrect Hauth detected for host: {request_host}.")
            return False, f"Incorrect Hauth for URL `{request_host}`.", None

        return True, None, shop_host

    def _handle_missing_hauth(self, request: Request, request_host: str, request_hauth: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Handle case when Hauth is not configured."""
        basic_auth_success = request.basic_auth_success
        user_is_admin = request.user.has_admin_access() if request.user else False

        if basic_auth_success and user_is_admin:
            # Save hauth to client-specific settings as a dict with host as key
            shop_settings = self.app_settings['shop']
            hauth_dict = shop_settings['clients']['cyberfoil'].get('hauth', {})
            
            # Set hauth for this specific host
            hauth_dict[request_host] = request_hauth
            shop_settings['clients']['cyberfoil']['hauth'] = hauth_dict
            set_shop_settings(shop_settings)
            self.log_info(f"Successfully set Hauth value for host {request_host}.")
            return True, None, request_host

        self.log_warning(
            f"Hauth value not set for host {request_host}, Host verification is disabled. "
            f"Connect to the shop from Cyberfoil with an admin account to set it."
        )
        return True, None, None

    def _generate_shop_files(self, content_filter: Optional[str] = None) -> list:
        """Generate the files list for the shop with optional content type filtering."""
        files = self.get_filtered_files(content_filter)
        return [{'url': f'/api/get_game/{f.id}#{f.filename}', 'size': f.size} for f in files]
