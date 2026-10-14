"""Encrypt the SMTP password at rest with Windows DPAPI (tied to the current Windows user)."""

import base64
import ctypes
import sys

PREFIX = "dpapi:"
_ENTROPY = b"mailer-smtp-password-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def available():
    return sys.platform == "win32"


def is_encrypted(value):
    return (value or "").startswith(PREFIX)


if available():
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _sig = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.argtypes = _sig
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = _sig
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _run(fn, data):
        buf = ctypes.create_string_buffer(data, len(data))
        ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        entropy = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not fn(ctypes.byref(inp), None, ctypes.byref(entropy), None, None,
                  _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def encrypt(plain):
    if not plain or not available():
        return plain
    return PREFIX + base64.b64encode(_run(_crypt32.CryptProtectData, plain.encode("utf-8"))).decode()


def decrypt(value):
    if not is_encrypted(value):
        return value
    if not available():
        raise ValueError("This password was encrypted on Windows and can't be read here.")
    try:
        raw = base64.b64decode(value[len(PREFIX):])
        return _run(_crypt32.CryptUnprotectData, raw).decode("utf-8")
    except Exception as e:
        raise ValueError("The saved password could not be decrypted (it was encrypted by a "
                         "different Windows user or machine). Enter it again in Settings.") from e
