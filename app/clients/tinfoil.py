"""
Tinfoil client implementation.
"""
from flask import Request, Response, jsonify, send_from_directory
from typing import Tuple, Optional
import os
import json
import random
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
import zstandard as zstd

from .client import BaseClient
from constants import TINFOIL_HEADERS
from auth import basic_auth
from settings import set_shop_settings
from db import get_shop_files, Files


# https://github.com/blawar/tinfoil/blob/master/docs/files/public.key 1160174fa2d7589831f74d149bc403711f3991e4
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
    
    CLIENT_NAME = "Tinfoil"
    
    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify Tinfoil client by checking for required headers."""
        return all(header in request.headers for header in TINFOIL_HEADERS)
    
    def authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Authenticate Tinfoil request with host verification and basic auth."""
        hauth_success = None
        auth_success = None
        verified_host = None
        
        # Host verification to prevent hotlinking
        host_verification = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
        
        if host_verification:
            request_host = request.host
            request_hauth = request.headers.get('Hauth')
            self.log_info(f"Secure request from remote host {request_host}, proceeding with host verification.")
            
            shop_host = self.app_settings["shop"].get("host")
            shop_hauth = self.app_settings["shop"].get("hauth")
            
            if not shop_host:
                self.log_error("Missing shop host configuration, Host verification is disabled.")
            
            elif request_host != shop_host:
                self.log_warning(f"Incorrect URL referrer detected: {request_host}.")
                error = f"Incorrect URL `{request_host}`."
                hauth_success = False
            
            elif not shop_hauth:
                # Try authentication, if an admin user is logging in then set the hauth
                auth_success, auth_error, auth_is_admin = basic_auth(request)
                if auth_success and auth_is_admin:
                    shop_settings = self.app_settings['shop']
                    shop_settings['hauth'] = request_hauth
                    set_shop_settings(shop_settings)
                    self.log_info(f"Successfully set Hauth value for host {request_host}.")
                    hauth_success = True
                else:
                    self.log_warning(
                        f"Hauth value not set for host {request_host}, Host verification is disabled. "
                        f"Connect to the shop from Tinfoil with an admin account to set it."
                    )
            
            elif request_hauth != shop_hauth:
                self.log_warning(f"Incorrect Hauth detected for host: {request_host}.")
                error = f"Incorrect Hauth for URL `{request_host}`."
                hauth_success = False
            
            else:
                hauth_success = True
                verified_host = shop_host
            
            if hauth_success is False:
                return False, error, None
        
        # Now checking auth if shop is private
        if not self.app_settings['shop']['public']:
            # Shop is private
            if auth_success is None:
                auth_success, auth_error, _ = basic_auth(request)
            if not auth_success:
                return False, auth_error, verified_host
        
        # Auth success
        return True, None, verified_host
    
    def serve_shop(self, request: Request, verified_host: Optional[str] = None) -> Response:
        """Generate and serve Tinfoil shop listing (encrypted or JSON)."""
        shop = {
            "success": self.app_settings['shop']['motd']
        }
        
        if verified_host is not None:
            # enforce client side host verification
            shop["referrer"] = f"https://{verified_host}"
        
        shop["files"] = self._generate_shop_files()
        
        if self.app_settings['shop']['encrypt']:
            return Response(self._encrypt_shop(shop), mimetype='application/octet-stream')
        
        return jsonify(shop)
    
    def error_response(self, error_message: str) -> Response:
        """Generate Tinfoil error response in JSON format."""
        return jsonify({
            'error': error_message
        })
    
    def info_response(self, info_message: str) -> Response:
        """Generate Tinfoil info response in JSON format."""
        return jsonify({
            'success': info_message
        })
    
    def _generate_shop_files(self) -> list:
        """Generate the files list for the shop."""
        shop_files = []
        files = get_shop_files()
        for file in files:
            shop_files.append({
                "url": f'/api/get_game/{file["id"]}#{file["filename"]}',
                'size': file["size"]
            })
        return shop_files
    
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
