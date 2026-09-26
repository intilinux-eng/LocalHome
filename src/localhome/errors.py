"""Refusals the pages can put in the user's language.

An InputError is a ValueError - every `except ValueError` still catches
it - that also carries a code and its parameters. The API sends them
along with the English message (kept for logs and API callers), and the
pages look the code up under `errors.<code>` in the locale files, filling
in the parameters: "Zona «Stanze»: inserisci l'ID del sensore" instead
of "zone 0 ('Stanze'): sensor_device_id is required when sensor_mode is
'shelly'".
"""
from __future__ import annotations


class InputError(ValueError):
    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.code = code
        self.params = params
