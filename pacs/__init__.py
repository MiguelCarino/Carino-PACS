"""Carino PACS — a small cross-platform DICOM store-only PACS.

Two jobs:
  * receive  — a Storage SCP that accepts C-STORE (and C-ECHO) and files
               incoming studies to disk.
  * send     — watches a folder and auto-forwards new .dcm files to one or
               more remote DICOM nodes via C-STORE.

Everything is driven from a single JSON config and can be run head-less from
the CLI or through the bundled local web dashboard.
"""

__version__ = "1.0.0"
APP_NAME = "Carino PACS"
