"""
Tinfoil client implementation.
"""
from flask import Request, Response, jsonify
from typing import Tuple, Optional
import json
import random
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP, AES
from Crypto.Hash import SHA256
import zstandard as zstd

from .client import BaseClient
from constants import TINFOIL_HEADERS
from auth import basic_auth
from settings import set_shop_settings

# https://github.com/blawar/tinfoil/blob/master/docs/files/public.key
TINFOIL_PUBLIC_KEY = '''-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvPdrJigQ0rZAy+jla7hS
jwen8gkF0gjtl+lZGY59KatNd9Kj2gfY7dTMM+5M2tU4Wr3nk8KWr5qKm3hzo/2C
Gbc55im3tlRl6yuFxWQ+c/I2SM5L3xp6eiLUcumMsEo0B7ELmtnHTGCCNAIzTFzV
4XcWGVbkZj83rTFxpLsa1oArTdcz5CG6qgyVe7KbPsft76DAEkV8KaWgnQiG0Dps
INFy4vISmf6L1TgAryJ8l2K4y8QbymyLeMsABdlEI3yRHAm78PSezU57XtQpHW5I
aupup8Es6bcDZQKkRsbOeR9T74tkj+k44QrjZo8xpX9tlJAKEEmwDlyAg0O5CLX3
CQIDAQAB
-----END PUBLIC KEY-----'''


class TinfoilClient(BaseClient):
    """Tinfoil client with header-based identification, Hauth verification, and encrypted shop responses."""
    
    # Class variables
    CLIENT_NAME = "Tinfoil"
    
    # ==================== Abstract Method Implementations (Required) ====================
    
    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify Tinfoil client by checking for required headers."""
        return all(header in request.headers for header in TINFOIL_HEADERS)
    
    def error_response(self, error_message: str) -> Response:
        """Generate Tinfoil error response in JSON format."""
        return jsonify({'error': error_message})
    
    def info_response(self, info_message: str) -> Response:
        """Generate Tinfoil info response in JSON format."""
        return jsonify({'success': info_message})
    
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        # Authenticate the request
        auth_success, error_message, verified_host = self._authenticate(request)
        if not auth_success:
            return self.error_response(error_message)
        
        # Parse path for content type filtering
        content_filter = request.path.strip('/') if request.path else None
        
        # Serve the shop
        return self._serve_shop(verified_host, content_filter)
    
    # ==================== Private/Helper Methods ====================
    
    def _authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Authenticate Tinfoil request with host verification and basic auth."""
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            success, error, verified_host = self._verify_host(request)
            if not success:
                return False, error, None
        else:
            verified_host = None
        
        if not self.app_settings['shop']['public']:
            success, error, _ = basic_auth(request)
            if not success:
                return False, error, verified_host
        
        return True, None, verified_host
    
    def _verify_host(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Verify host and Hauth to prevent hotlinking."""
        request_host = request.host
        request_hauth = request.headers.get('Hauth')
        shop_host = self.app_settings["shop"].get("host")
        shop_hauth = self.app_settings["shop"].get("hauth")
        
        self.log_info(f"Secure request from remote host {request_host}, proceeding with host verification.")
        
        if not shop_host:
            self.log_error("Missing shop host configuration, Host verification is disabled.")
            return True, None, None
        
        if request_host != shop_host:
            self.log_warning(f"Incorrect URL referrer detected: {request_host}.")
            return False, f"Incorrect URL `{request_host}`.", None
        
        if not shop_hauth:
            return self._handle_missing_hauth(request, request_host, request_hauth)
        
        if request_hauth != shop_hauth:
            self.log_warning(f"Incorrect Hauth detected for host: {request_host}.")
            return False, f"Incorrect Hauth for URL `{request_host}`.", None
        
        return True, None, shop_host
    
    def _handle_missing_hauth(self, request: Request, request_host: str, request_hauth: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Handle case when Hauth is not configured."""
        auth_success, auth_error, auth_is_admin = basic_auth(request)
        
        if auth_success and auth_is_admin:
            shop_settings = self.app_settings['shop']
            shop_settings['hauth'] = request_hauth
            set_shop_settings(shop_settings)
            self.log_info(f"Successfully set Hauth value for host {request_host}.")
            return True, None, request_host
        
        self.log_warning(
            f"Hauth value not set for host {request_host}, Host verification is disabled. "
            f"Connect to the shop from Tinfoil with an admin account to set it."
        )
        return True, None, None
    
    def _serve_shop(self, verified_host: Optional[str] = None, content_filter: Optional[str] = None) -> Response:
        """Generate and serve Tinfoil shop listing (encrypted or JSON)."""
        shop = {"success": self.app_settings['shop']['motd']}
        
        if verified_host:
            # Enforce client side host verification
            shop["referrer"] = f"https://{verified_host}"
        
        shop["files"] = self._generate_shop_files(content_filter)
        
        if self.app_settings['shop']['encrypt']:
            return Response(self._encrypt_shop(shop), mimetype='application/octet-stream')
        
        return jsonify(shop)
    
    def _generate_shop_files(self, content_filter: Optional[str] = None) -> list:
        """Generate the files list for the shop with optional content type filtering."""
        files = self.get_filtered_files(content_filter)
        return [{'url': f'/api/get_game/{f.id}#{f.filename}', 'size': f.size} for f in files]
    
    def _encrypt_shop(self, shop: dict) -> bytes:
        """Encrypt shop data for Tinfoil using RSA + AES encryption."""
        input_data = json.dumps(shop).encode('utf-8')
        
        # Random 128-bit AES key (16 bytes), used later for symmetric encryption (AES)
        aes_key = random.randint(0, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF).to_bytes(0x10, 'big')
        
        # Zstandard compression
        flag = 0xFD
        cctx = zstd.ZstdCompressor(level=22)
        buf = cctx.compress(input_data)
        sz = len(buf)
        
        # Encrypt the AES key with RSA, PKCS1_OAEP padding scheme
        pub_key = RSA.importKey(TINFOIL_PUBLIC_KEY)
        cipher = PKCS1_OAEP.new(pub_key, hashAlgo=SHA256, label=b'')
        # Now the AES key can only be decrypted with Tinfoil private key
        session_key = cipher.encrypt(aes_key)
        
        # Encrypting the Data with AES
        cipher = AES.new(aes_key, AES.MODE_ECB)
        buf = cipher.encrypt(buf + (b'\x00' * (0x10 - (sz % 0x10))))
        
        binary_data = (
            b'TINFOIL' + 
            flag.to_bytes(1, byteorder='little') + 
            session_key + 
            sz.to_bytes(8, 'little') + 
            buf
        )
        return binary_data
