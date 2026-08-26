"""Generated release version.

Source checkouts keep the last published version. The release workflow rewrites this
module from the pushed ``vX.Y.Z`` tag before PyInstaller freezes the application, so a
forgotten manual bump can never make an installed Link misreport its version.
"""

__version__ = "0.1.5"
