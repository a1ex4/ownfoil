"""
Client module for handling different shop clients
"""
from .client import BaseClient
from .tinfoil import TinfoilClient
from .sphaira import SphairaClient

__all__ = ['BaseClient', 'TinfoilClient', 'SphairaClient']
