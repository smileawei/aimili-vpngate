#!/usr/bin/env python3
"""Run the dashboard and proxy with the SoftEther VPN backend."""
import os
import sys

os.environ['VPN_BACKEND'] = 'softether'
sys.path.insert(0, '/app')

import vpngate_manager

vpngate_manager.main()
