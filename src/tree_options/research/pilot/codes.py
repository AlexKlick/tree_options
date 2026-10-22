"""Reason codes of the research pilot (Wave 4 lane I).

The C3.5 helpers in ``trex.contracts.wave3`` already spell the run blockers
(``registration_not_sealed``, ``registration_sealer_not_owner``,
``budget_usage_unknown``, ``budget_usage_mismatch``,
``budget_requests_exhausted``, ``budget_compute_exhausted``,
``budget_storage_exhausted``) and the seal refusals
(``registration_already_sealed``, ``sealer_not_owner``). The codes below are
this lane's own; every one matches the C3.4 ``ReasonCode`` pattern
(``snake_case`` with an optional ``:detail``).
"""

from __future__ import annotations

from typing import Final

# -- the frozen contract's own spellings (re-used, never redefined) -------------------
REGISTRATION_NOT_SEALED: Final = "registration_not_sealed"
REGISTRATION_ALREADY_SEALED: Final = "registration_already_sealed"
SEALER_NOT_OWNER: Final = "sealer_not_owner"
BUDGET_USAGE_UNKNOWN: Final = "budget_usage_unknown"

# -- lane I ---------------------------------------------------------------------------
#: no draft and no sealed file for the program id
REGISTRATION_MISSING: Final = "registration_missing"
#: a registration file exists but does not read as that program's registration
REGISTRATION_UNREADABLE: Final = "registration_unreadable"
#: ``write_draft`` was handed a sealed document
REGISTRATION_NOT_DRAFT: Final = "registration_not_draft"
#: the draft's body hash is not the one the owner reviewed
REGISTRATION_SHA256_MISMATCH: Final = "registration_sha256_mismatch"
#: the contract refused the sealed document (a seal instant before ``created_at``)
REGISTRATION_SEAL_INVALID: Final = "registration_seal_invalid"
#: ``control_not_sealed:<program_id>``: a named control is not sealed yet
CONTROL_NOT_SEALED: Final = "control_not_sealed"
#: ``TREX_OWNER_EMAILS`` does not parse
OWNERS_UNCONFIGURED: Final = "owners_unconfigured"
#: a run's ``pilot_program_id`` is not a path-safe id
PILOT_PROGRAM_INVALID: Final = "pilot_program_invalid"
#: the admission itself failed (pilot dir unreadable, ...): the run never starts
PILOT_ADMISSION_UNAVAILABLE: Final = "pilot_admission_unavailable"
#: the worker's ``error_code`` on a run refused at admission
PILOT_ADMISSION_REFUSED: Final = "pilot_admission_refused"
#: the view could not list the registrations
PILOT_DIR_UNREADABLE: Final = "pilot_dir_unreadable"
#: ``pilot_cli_failed:<error class>``: a CLI command hit an I/O or parse error
PILOT_CLI_FAILED: Final = "pilot_cli_failed"
#: ``release`` named a run with no open job marker
JOB_NOT_IN_FLIGHT: Final = "job_not_in_flight"

#: details of ``budget_usage_unknown:<detail>`` (the view and the CLI show them)
USAGE_UNREADABLE: Final = "unreadable"
USAGE_INVALID: Final = "invalid"
USAGE_MISMATCH: Final = "mismatch"
USAGE_JOB_IN_FLIGHT: Final = "job_in_flight"
