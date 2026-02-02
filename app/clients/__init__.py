"""
Client module for handling different shop clients
"""
from .client import BaseClient
from .tinfoil import TinfoilClient

__all__ = ['BaseClient', 'TinfoilClient']
