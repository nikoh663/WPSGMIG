"""wpmigrate: move a WordPress site onto the server this tool runs on.

The package is split into an ``engine`` (named, resumable stages that emit
structured events and ask questions through a small interface) and front
ends that render those events. ``wpmigrate.cli`` is the terminal front end.
"""

__version__ = "1.3.0"
