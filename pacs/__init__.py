"""Carino PACS — a small, cross-platform DICOM store-and-reconcile PACS.

Core (v1.0.0):
  * receive  — a Storage SCP that accepts C-STORE (and C-ECHO) and files
               incoming studies to disk.
  * send     — watches a folder and auto-forwards new .dcm files to one or
               more remote DICOM nodes via C-STORE.

Added since v1.0.0:
  * emergency RIS — HL7 ORM^O01 order intake over MLLP + accession-number
                    reconciliation of arriving studies.
  * modality worklist — serves open orders to modalities via C-FIND.
  * emergency failover — monitors a primary PACS and takes over the worklist
                         when it goes unreachable, holding/forwarding studies.
  * virtual print receiver — captures print-only modalities as PDF.
  * embedded DICOM editor — bundled dcmjs tag editor / de-identifier.
  * disk-space guard, PDF/ODF accession attachment.

Everything is driven from a single JSON config and can be run head-less from
the CLI or through the bundled local web dashboard.

Copyright (C) 2026 Miguel Carino.
Licensed under the GNU Affero General Public License v3.0 or later; see LICENSE.
"""

__version__ = "1.1.0"
APP_NAME = "Carino PACS"
