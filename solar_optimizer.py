#!/usr/bin/env python3
"""Thin wrapper so `python3 solar_optimizer.py` still works on HA."""
from solar_optimizer.__main__ import main

if __name__ == "__main__":
    main()
