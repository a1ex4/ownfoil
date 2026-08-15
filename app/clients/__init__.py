"""
Client module for handling different shop clients
"""
from .client import BaseClient
from .tinfoil import TinfoilClient
from .sphaira import SphairaClient
from .cyberfoil import CyberFoilClient

__all__ = ['BaseClient', 'CyberFoilClient', 'TinfoilClient', 'SphairaClient']
