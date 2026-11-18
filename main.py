#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Download Folder Cleaner - タスクトレイ常駐＆自動掃除ユーティリティ

主な機能:
- タスクトレイ常駐＆定期自動クリーンアップ（起動時自動実行＋定期タイマー）
- 初回起動時は自動掃除を実行せず設定画面を表示
- 二重起動防止（既に起動中の場合は既存プロセスの設定画面を前面に表示）
- 設定画面の表示位置（ウィンドウ座標）を自動記憶・復元（940x825固定）
- 日時判定基準の選択（保存日時 ctime と更新日時 mtime の新しい方 / mtimeのみ / ctimeのみ）
- ピン留め（保護）機能（プレフィックス保護、個別ファイル保護、ワイルドカード除外、拡張子別保持時間）
- 最小・最大ファイルサイズフィルタ（MB単位）
- Windows トースト（バルーン）通知機能
- ログファイルの自動ローテーション（肥大化防止）
- Windows スタートアップ登録（ログオン時自動起動）切替
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image, ImageTk
from send2trash import send2trash

# -----------------------------
# アプリケーション定数
# -----------------------------
APP_NAME = "Download Folder Cleaner"
APP_VERSION = "1.0.0"
MUTEX_NAME = "Local\\DownloadFolderCleaner_SingleInstance_Mutex_v1"
SHOW_EVENT_NAME = "Local\\DownloadFolderCleaner_ShowSettings_Event_v1"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_VALUE = "DownloadFolderCleaner"

CONFIG_FILENAME = "settings.json"
ASSETS_DIRNAME = "assets"
ICON_ICO_FILENAME = "icon.ico"
ICON_PNG_FILENAME = "icon.png"

WINDOW_WIDTH = 940
WINDOW_HEIGHT = 825

DEFAULT_EXCLUDES: tuple[str, ...] = (
    "*.crdownload",  # Chrome / Edge ダウンロード中
    "*.part",        # Firefox 等
    "*.tmp",
    ".DS_Store",
    "desktop.ini",
)

DEFAULT_PIN_PREFIXES: tuple[str, ...] = (
    "_",
    "📌",
    "!",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "initialized": False,
    "downloads_path": "",                # 空文字 = Windows標準Downloads自動検出
    "threshold_hours": 24.0,             # 対象とする経過時間（時間）
    "time_basis": "max_mtime_ctime",     # "max_mtime_ctime" | "mtime" | "ctime"
    "run_on_startup_launch": True,       # プログラム起動時に掃除を実行する（初回起動を除く）
    "dry_run_mode": False,               # True = 実際には移動せずログ/通知のみ
    "auto_interval_enabled": False,      # タスクトレイ常駐中の定期掃除（デフォルト: オフ）
    "auto_interval_minutes": 60,         # 定期チェック間隔（分）
    "windows_startup": True,             # Windowsスタートアップ登録（デフォルト: オン）
    "notify_enabled": True,              # ゴミ箱移動時にWindowsトースト通知を表示
    "notify_on_empty": False,            # 対象0件のときも通知を表示
    "min_size_mb": 0.0,                  # 最小サイズ制限 (MB, 0=制限なし)
    "max_size_mb": 0.0,                  # 最大サイズ制限 (MB, 0=制限なし)
    "pin_prefixes": list(DEFAULT_PIN_PREFIXES),
    "exclude_patterns": list(DEFAULT_EXCLUDES),
    "pinned_items": [],                  # 個別ピン留め（保護）対象の名前リスト
    "extension_hours": {},               # 拡張子別保持時間 例: {".iso": 12.0, ".pdf": 168.0}
    "log_max_size_kb": 1024,             # ログローテーション上限サイズ (KB)
    "log_backup_count": 3,               # ログローテーション保持世代数
    "window_geometry": "",               # 設定画面の位置 (例: "940x825+320+140")
}

# --- ディープ・スレート (Deep Slate) カラーパレット ---
COLOR_BG_ROOT = "#0F172A"       # ベース背景 (Slate 900)
COLOR_BG_PANEL = "#1E293B"      # カード・パネル背景 (Slate 800)
COLOR_BG_INPUT = "#0B1120"      # 入力欄・リスト背景
COLOR_BG_HEADER = "#111827"     # ヘッダー・フッター背景
COLOR_BORDER = "#334155"        # ボーダー (Slate 700)
COLOR_TEXT_MAIN = "#F8FAFC"     # メインテキスト (Slate 50)
COLOR_TEXT_BODY = "#E2E8F0"     # 本文テキスト (Slate 200)
COLOR_TEXT_MUTED = "#94A3B8"    # 補助テキスト (Slate 400)
COLOR_ACCENT = "#0284C7"        # アクセントブルー (Sky 600)
COLOR_ACCENT_HOVER = "#0369A1"  # アクセントホバー (Sky 700)
COLOR_ACCENT_LIGHT = "#38BDF8"  # ハイライトシアン (Sky 400)
COLOR_SUCCESS = "#10B981"       # エメラルドグリーン
COLOR_SUCCESS_HOVER = "#059669"
COLOR_WARNING = "#F59E0B"       # アンバーゴールド
COLOR_DANGER = "#E11D48"        # ローズレッド
COLOR_DANGER_HOVER = "#BE123C"
COLOR_BTN_SUB = "#334155"       # サブボタン
COLOR_BTN_SUB_HOVER = "#475569"

FONT_FAMILY = "Yu Gothic UI"


# -----------------------------
# パス・設定ファイル管理
# -----------------------------
def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path:
    """assets/ 配下または直下のリソースパスを解決（PyInstaller --onefile 対応）"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        candidates.append(meipass / ASSETS_DIRNAME / filename)
        candidates.append(meipass / filename)

    app_dir = get_app_dir()
    candidates.append(app_dir / ASSETS_DIRNAME / filename)
    candidates.append(app_dir / filename)

    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def get_config_path() -> Path:
    return get_app_dir() / CONFIG_FILENAME


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "download_folder_cleaner.log"


def load_settings(config_path: Path | None = None) -> tuple[dict[str, Any], bool]:
    """
    設定ファイルを読み込む。
    戻り値: (settings_dict, is_first_launch)
    settings.json が存在しない、または initialized が False の場合は is_first_launch=True を返す。
    """
    path = config_path or get_config_path()
    settings = dict(DEFAULT_SETTINGS)
    settings["pin_prefixes"] = list(DEFAULT_PIN_PREFIXES)
    settings["exclude_patterns"] = list(DEFAULT_EXCLUDES)
    settings["pinned_items"] = []
    settings["extension_hours"] = {}

    if not path.is_file():
        return settings, True

    try:
        with path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for k, default_val in DEFAULT_SETTINGS.items():
                if k in loaded:
                    val = loaded[k]
                    if isinstance(default_val, bool):
                        settings[k] = bool(val)
                    elif isinstance(default_val, float):
                        try:
                            settings[k] = float(val)
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(default_val, int):
                        try:
                            settings[k] = int(val)
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(default_val, list) and isinstance(val, list):
                        settings[k] = [str(x) for x in val if str(x).strip()]
                    elif isinstance(default_val, dict) and isinstance(val, dict):
                        ext_map: dict[str, float] = {}
                        for ek, ev in val.items():
                            try:
                                k_norm = str(ek).strip().lower()
                                if k_norm and not k_norm.startswith("."):
                                    k_norm = "." + k_norm
                                if k_norm:
                                    ext_map[k_norm] = float(ev)
                            except (TypeError, ValueError):
                                continue
                        settings[k] = ext_map
                    elif isinstance(default_val, str) and isinstance(val, str):
                        settings[k] = val
    except Exception:
        return settings, True

    is_first_launch = not bool(settings.get("initialized", False))
    return settings, is_first_launch


def save_settings(settings: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# -----------------------------
# Windows スタートアップ登録管理
# -----------------------------
def is_startup_registered() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, STARTUP_REG_VALUE)
            return bool(val)
    except OSError:
        return False


def set_startup_registered(enable: bool) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Windows以外のOSではサポートされていません。"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    venv_pythonw = get_app_dir() / ".venv" / "Scripts" / "pythonw.exe"
                    py_exe = venv_pythonw if venv_pythonw.is_file() else Path(sys.executable)
                    if py_exe.name.lower() == "python.exe":
                        pw = py_exe.with_name("pythonw.exe")
                        if pw.is_file():
                            py_exe = pw
                    script_path = Path(__file__).resolve()
                    cmd = f'"{py_exe}" "{script_path}"'
                winreg.SetValueEx(key, STARTUP_REG_VALUE, 0, winreg.REG_SZ, cmd)
                return True, "Windows スタートアップに登録しました。"
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_REG_VALUE)
                except FileNotFoundError:
                    pass
                return True, "Windows スタートアップ登録を解除しました。"
    except Exception as e:
        return False, f"スタートアップ設定エラー: {e}"


# -----------------------------
# 二重起動防止 ＆ 既存プロセス通知 (Win32 Mutex & Event)
# -----------------------------
class SingleInstanceGuard:
    """
    名前付きMutexとEventを用いて二重起動を防止する。
    既に起動している場合、既存プロセスへ「設定画面を開く」イベントをシグナル送信する。
    """

    def __init__(self, mutex_name: str = MUTEX_NAME, event_name: str = SHOW_EVENT_NAME) -> None:
        self.mutex_name = mutex_name
        self.event_name = event_name
        self.mutex_handle: int | None = None
        self.event_handle: int | None = None
        self.is_primary = True

        if os.name == "nt":
            self._init_win32()

    def _init_win32(self) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE

        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL

        self.mutex_handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        last_err = kernel32.GetLastError()

        self.event_handle = kernel32.CreateEventW(None, False, False, self.event_name)

        if last_err == 183:  # ERROR_ALREADY_EXISTS
            self.is_primary = False
            if self.event_handle:
                kernel32.SetEvent(self.event_handle)

    def check_show_signaled(self) -> bool:
        """非ブロッキングでイベントがシグナル状態か確認する"""
        if os.name != "nt" or not self.event_handle:
            return False
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        WAIT_OBJECT_0 = 0x00000000
        res = kernel32.WaitForSingleObject(self.event_handle, 0)
        return res == WAIT_OBJECT_0

    def close(self) -> None:
        if os.name != "nt":
            return
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        if self.event_handle:
            kernel32.CloseHandle(self.event_handle)
            self.event_handle = None
        if self.mutex_handle:
            kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None


# -----------------------------
# Windows コンソールアイコン適用
# -----------------------------
def _apply_console_icon() -> None:
    if os.name != "nt":
        return

    icon_path = get_resource_path(ICON_ICO_FILENAME)
    if not icon_path.is_file():
        return

    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        kernel32.GetConsoleWindow.restype = wintypes.HWND
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE

        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM

        hicon = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x0010 | 0x0040)
        if hicon:
            user32.SendMessageW(hwnd, 0x0080, 0, hicon)
            user32.SendMessageW(hwnd, 0x0080, 1, hicon)
    except Exception:
        pass


# -----------------------------
# Downloads フォルダ検出
# -----------------------------
def _get_downloads_dir_windows() -> Path | None:
    if os.name != "nt":
        return None

    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        folder_id_downloads = GUID(
            0x374DE290,
            0x123F,
            0x4565,
            (wintypes.BYTE * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        sh_get_known_folder_path = shell32.SHGetKnownFolderPath
        sh_get_known_folder_path.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        sh_get_known_folder_path.restype = wintypes.HRESULT

        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None

        path_ptr = ctypes.c_wchar_p()
        try:
            hr = sh_get_known_folder_path(
                ctypes.byref(folder_id_downloads), 0, None, ctypes.byref(path_ptr)
            )
            if hr != 0 or not path_ptr.value:
                return None
            p = Path(path_ptr.value)
            return p if p.is_dir() else None
        finally:
            if path_ptr:
                ole32.CoTaskMemFree(path_ptr)
    except Exception:
        return None


def get_downloads_dir(explicit: str | None = None) -> Path:
    if explicit and explicit.strip():
        p = Path(explicit.strip()).expanduser()
        if not p.is_dir():
            raise FileNotFoundError(f"指定されたパスが存在しないかフォルダではありません: {p}")
        return p

    if os.name == "nt":
        p = _get_downloads_dir_windows()
        if p:
            return p

    home = Path.home()
    for candidate in (home / "Downloads", home / "ダウンロード"):
        if candidate.is_dir():
            return candidate

    raise FileNotFoundError("Downloads フォルダを自動検出できませんでした。設定または --path で明示指定してください。")


# -----------------------------
# 判定・サイズ計算・移動処理・ログローテーション
# -----------------------------
@dataclass(slots=True)
class Candidate:
    path: Path
    kind: str               # "file" | "dir"
    age_seconds: float      # newest_mtime (または判定基準時刻) からの経過秒数
    newest_mtime: float     # 判定基準タイムスタンプ
    size_bytes: int = 0     # ファイルサイズまたはフォルダ合計サイズ
    threshold_hours: float = 24.0


@dataclass(slots=True)
class ScanItemInfo:
    """GUI一覧表示用の全エントリ情報"""
    path: Path
    kind: str               # "file" | "dir"
    age_seconds: float
    timestamp: float
    size_bytes: int
    status: str             # "target" | "pinned" | "excluded" | "size_filtered" | "recent"
    reason: str


def human_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}日{h}時間"
    if h > 0:
        return f"{h}時間{m}分"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    kb = size_bytes / 1024.0
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"


def extract_timestamp(st: os.stat_result, time_basis: str = "max_mtime_ctime") -> float:
    """
    設定された日時判定基準に従ってタイムスタンプを返す。
    - max_mtime_ctime: 保存日時(ctime)と更新日時(mtime)の新しい方（DL直後の古いmtime誤削除防止）
    - mtime: 更新日時のみ
    - ctime: 作成(保存)日時のみ
    """
    if time_basis == "mtime":
        return st.st_mtime
    if time_basis == "ctime":
        return st.st_ctime
    return max(st.st_mtime, st.st_ctime)


def newest_mtime_in_dir(
    dir_path: Path,
    cutoff_mtime: float | None = None,
    time_basis: str = "mtime",
) -> float:
    """
    ディレクトリ自身＋配下の全ファイル/フォルダのタイムスタンプ最大値を返す（後方互換API）。
    """
    newest = extract_timestamp(dir_path.stat(), time_basis)
    if cutoff_mtime is not None and newest > cutoff_mtime:
        return newest

    stack: list[Path | str] = [dir_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        ts = extract_timestamp(st, time_basis)
                        if ts > newest:
                            newest = ts
                            if cutoff_mtime is not None and newest > cutoff_mtime:
                                return newest
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue

    return newest


def analyze_directory(
    dir_path: Path,
    cutoff_ts: float | None = None,
    time_basis: str = "max_mtime_ctime",
    compute_full_size: bool = False,
) -> tuple[float, int]:
    """
    ディレクトリの最新タイムスタンプと合計サイズ(bytes)を高速走査する。
    compute_full_size=False かつ cutoff_ts が指定されている場合、
    cutoff_ts より新しい要素が見つかった時点で早期終了する。
    """
    st_root = dir_path.stat()
    newest = extract_timestamp(st_root, time_basis)
    total_size = 0

    if not compute_full_size and cutoff_ts is not None and newest > cutoff_ts:
        return newest, 0

    stack: list[Path | str] = [dir_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        ts = extract_timestamp(st, time_basis)
                        if ts > newest:
                            newest = ts
                            if not compute_full_size and cutoff_ts is not None and newest > cutoff_ts:
                                return newest, total_size
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total_size += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue

    return newest, total_size


def should_exclude(name: str, patterns: list[str]) -> bool:
    name_lower = name.lower()
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name_lower, pat.lower()) for pat in patterns)


def is_pinned_name(name: str, pin_prefixes: list[str], pinned_items: list[str]) -> tuple[bool, str]:
    for pref in pin_prefixes:
        if pref and name.startswith(pref):
            return True, f"プレフィックス保護 ({pref})"
    pinned_lower = {x.lower() for x in pinned_items if x}
    if name.lower() in pinned_lower:
        return True, "個別ピン留め保護"
    return False, ""


def get_item_threshold_hours(
    entry_path: Path,
    is_dir: bool,
    base_hours: float,
    extension_hours: dict[str, float] | None = None,
) -> float:
    if not is_dir and extension_hours:
        ext = entry_path.suffix.lower()
        if ext in extension_hours:
            return max(0.0, float(extension_hours[ext]))
    return max(0.0, float(base_hours))


def scan_downloads_detailed(
    downloads: Path,
    threshold_hours: float = 24.0,
    exclude_patterns: list[str] | None = None,
    pin_prefixes: list[str] | None = None,
    pinned_items: list[str] | None = None,
    extension_hours: dict[str, float] | None = None,
    time_basis: str = "max_mtime_ctime",
    min_size_mb: float = 0.0,
    max_size_mb: float = 0.0,
    compute_all_dir_sizes: bool = False,
) -> tuple[list[Candidate], list[ScanItemInfo]]:
    """
    ダウンロードフォルダを走査し、(削除対象Candidateリスト, 全アイテム詳細リスト) を返す。
    """
    now = time.time()
    excludes = exclude_patterns if exclude_patterns is not None else list(DEFAULT_EXCLUDES)
    prefixes = pin_prefixes if pin_prefixes is not None else []
    pinned = pinned_items if pinned_items is not None else []
    ext_map = extension_hours or {}

    min_bytes = int(min_size_mb * 1024 * 1024) if min_size_mb > 0 else 0
    max_bytes = int(max_size_mb * 1024 * 1024) if max_size_mb > 0 else 0

    candidates: list[Candidate] = []
    all_items: list[ScanItemInfo] = []

    with os.scandir(downloads) as it:
        for entry in it:
            name = entry.name
            try:
                if entry.is_symlink():
                    continue

                entry_path = Path(entry.path)
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                if not is_file and not is_dir:
                    continue

                kind = "dir" if is_dir else "file"
                item_hours = get_item_threshold_hours(entry_path, is_dir, threshold_hours, ext_map)
                older_than_sec = int(item_hours * 3600)
                cutoff_ts = now - older_than_sec

                pinned_flag, pin_reason = is_pinned_name(name, prefixes, pinned)
                excluded_flag = should_exclude(name, excludes)

                if is_file:
                    st = entry.stat(follow_symlinks=False)
                    ts = extract_timestamp(st, time_basis)
                    size_bytes = st.st_size
                else:
                    need_full = compute_all_dir_sizes or (not pinned_flag and not excluded_flag)
                    ts, size_bytes = analyze_directory(
                        entry_path,
                        cutoff_ts=None if compute_all_dir_sizes else cutoff_ts,
                        time_basis=time_basis,
                        compute_full_size=compute_all_dir_sizes,
                    )
                    if not compute_all_dir_sizes and need_full and ts <= cutoff_ts:
                        _, size_bytes = analyze_directory(
                            entry_path,
                            cutoff_ts=None,
                            time_basis=time_basis,
                            compute_full_size=True,
                        )

                age = max(0.0, now - ts)

                if pinned_flag:
                    all_items.append(ScanItemInfo(entry_path, kind, age, ts, size_bytes, "pinned", pin_reason))
                    continue

                if excluded_flag:
                    all_items.append(ScanItemInfo(entry_path, kind, age, ts, size_bytes, "excluded", "除外パターン一致"))
                    continue

                if ts > cutoff_ts:
                    all_items.append(
                        ScanItemInfo(entry_path, kind, age, ts, size_bytes, "recent", f"{item_hours:g}h 未満")
                    )
                    continue

                if min_bytes > 0 and size_bytes < min_bytes:
                    all_items.append(
                        ScanItemInfo(
                            entry_path,
                            kind,
                            age,
                            ts,
                            size_bytes,
                            "size_filtered",
                            f"最小サイズ未満 (<{min_size_mb:g}MB)",
                        )
                    )
                    continue

                if max_bytes > 0 and size_bytes > max_bytes:
                    all_items.append(
                        ScanItemInfo(
                            entry_path,
                            kind,
                            age,
                            ts,
                            size_bytes,
                            "size_filtered",
                            f"最大サイズ超過 (>{max_size_mb:g}MB)",
                        )
                    )
                    continue

                cand = Candidate(entry_path, kind, age, ts, size_bytes, item_hours)
                candidates.append(cand)
                all_items.append(
                    ScanItemInfo(entry_path, kind, age, ts, size_bytes, "target", f"対象 ({item_hours:g}h 以上経過)")
                )
            except OSError:
                continue

    candidates.sort(key=lambda c: c.newest_mtime)
    all_items.sort(key=lambda x: x.timestamp)
    return candidates, all_items


def gather_candidates(
    downloads: Path,
    older_than_seconds: int,
    exclude_patterns: list[str],
    pin_prefixes: list[str] | None = None,
    pinned_items: list[str] | None = None,
    extension_hours: dict[str, float] | None = None,
    time_basis: str = "mtime",
    min_size_mb: float = 0.0,
    max_size_mb: float = 0.0,
) -> list[Candidate]:
    """後方互換およびCLI用候補収集関数"""
    threshold_hours = older_than_seconds / 3600.0
    candidates, _ = scan_downloads_detailed(
        downloads=downloads,
        threshold_hours=threshold_hours,
        exclude_patterns=exclude_patterns,
        pin_prefixes=pin_prefixes or [],
        pinned_items=pinned_items or [],
        extension_hours=extension_hours,
        time_basis=time_basis,
        min_size_mb=min_size_mb,
        max_size_mb=max_size_mb,
    )
    return candidates


def rotate_log_if_needed(log_file: Path, max_size_kb: int = 1024, backup_count: int = 3) -> None:
    """ログファイルが上限サイズを超えた場合に自動ローテーションする"""
    if max_size_kb <= 0 or not log_file.is_file():
        return
    try:
        max_bytes = max_size_kb * 1024
        if log_file.stat().st_size < max_bytes:
            return

        if backup_count > 0:
            for idx in range(backup_count, 0, -1):
                src = log_file.with_name(f"{log_file.name}.{idx}")
                if idx == backup_count:
                    if src.exists():
                        src.unlink()
                else:
                    dst = log_file.with_name(f"{log_file.name}.{idx + 1}")
                    if src.exists():
                        src.replace(dst)
            first_backup = log_file.with_name(f"{log_file.name}.1")
            log_file.replace(first_backup)
        else:
            content = log_file.read_text(encoding="utf-8", errors="ignore")
            half = content[len(content) // 2 :]
            newline_pos = half.find("\n")
            if newline_pos != -1:
                half = half[newline_pos + 1 :]
            log_file.write_text(half, encoding="utf-8")
    except OSError:
        pass


def append_log(
    log_file: Path,
    lines: list[str],
    max_size_kb: int = 1024,
    backup_count: int = 3,
) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_if_needed(log_file, max_size_kb=max_size_kb, backup_count=backup_count)
        with log_file.open("a", encoding="utf-8", errors="ignore") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def trash_path(p: Path) -> tuple[bool, str]:
    try:
        send2trash(str(p))
        return True, "trashed"
    except Exception as e:
        return False, f"error: {e}"


@dataclass(slots=True)
class CleanupResult:
    downloads_dir: Path
    candidates: list[Candidate]
    moved_count: int
    moved_bytes: int
    failed: list[str]
    dry_run: bool
    timestamp_str: str
    error_message: str = ""


def execute_cleanup_from_settings(
    settings: dict[str, Any],
    override_dry_run: bool | None = None,
    log_file: Path | None = None,
) -> CleanupResult:
    """設定辞書に基づいて掃除（またはDry-Run）を実行し、結果を返す"""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    dry_run = bool(settings.get("dry_run_mode", False)) if override_dry_run is None else override_dry_run
    target_log = log_file or default_log_path()
    max_kb = int(settings.get("log_max_size_kb", 1024))
    backups = int(settings.get("log_backup_count", 3))

    try:
        downloads = get_downloads_dir(settings.get("downloads_path", ""))
    except FileNotFoundError as e:
        return CleanupResult(
            downloads_dir=Path(),
            candidates=[],
            moved_count=0,
            moved_bytes=0,
            failed=[],
            dry_run=dry_run,
            timestamp_str=now_str,
            error_message=str(e),
        )

    threshold_hours = max(0.0, float(settings.get("threshold_hours", 24.0)))
    time_basis = str(settings.get("time_basis", "max_mtime_ctime"))
    min_size_mb = max(0.0, float(settings.get("min_size_mb", 0.0)))
    max_size_mb = max(0.0, float(settings.get("max_size_mb", 0.0)))
    pin_prefixes = list(settings.get("pin_prefixes", DEFAULT_PIN_PREFIXES))
    exclude_patterns = list(settings.get("exclude_patterns", DEFAULT_EXCLUDES))
    pinned_items = list(settings.get("pinned_items", []))
    extension_hours = dict(settings.get("extension_hours", {}))

    header = [
        f"[{now_str}] {APP_NAME} start",
        f"  downloads={downloads}",
        f"  threshold_hours={threshold_hours} (time_basis={time_basis})",
        f"  size_filter_mb=min:{min_size_mb}, max:{max_size_mb}",
        f"  pin_prefixes={pin_prefixes}, pinned_items={pinned_items}",
        f"  exclude={exclude_patterns}",
        f"  extension_hours={extension_hours}",
        f"  mode={'DRY-RUN' if dry_run else 'EXECUTE'}",
    ]
    append_log(target_log, header, max_size_kb=max_kb, backup_count=backups)

    candidates, _ = scan_downloads_detailed(
        downloads=downloads,
        threshold_hours=threshold_hours,
        exclude_patterns=exclude_patterns,
        pin_prefixes=pin_prefixes,
        pinned_items=pinned_items,
        extension_hours=extension_hours,
        time_basis=time_basis,
        min_size_mb=min_size_mb,
        max_size_mb=max_size_mb,
    )

    if not candidates:
        append_log(target_log, [f"[{now_str}] no candidates", "-" * 40], max_size_kb=max_kb, backup_count=backups)
        return CleanupResult(
            downloads_dir=downloads,
            candidates=[],
            moved_count=0,
            moved_bytes=0,
            failed=[],
            dry_run=dry_run,
            timestamp_str=now_str,
        )

    lines = [
        f"[{now_str}] candidates={len(candidates)}",
        *(
            f"  - [{c.kind}] {c.path.name} (age={human_age(c.age_seconds)}, size={human_size(c.size_bytes)})"
            for c in candidates
        ),
    ]
    append_log(target_log, lines, max_size_kb=max_kb, backup_count=backups)

    if dry_run:
        total_preview_bytes = sum(c.size_bytes for c in candidates)
        append_log(
            target_log,
            [f"dry-run: nothing moved (total={human_size(total_preview_bytes)})", "-" * 40],
            max_size_kb=max_kb,
            backup_count=backups,
        )
        return CleanupResult(
            downloads_dir=downloads,
            candidates=candidates,
            moved_count=0,
            moved_bytes=total_preview_bytes,
            failed=[],
            dry_run=True,
            timestamp_str=now_str,
        )

    moved = 0
    moved_bytes = 0
    failed: list[str] = []
    for c in candidates:
        ok, reason = trash_path(c.path)
        if ok:
            moved += 1
            moved_bytes += c.size_bytes
        else:
            failed.append(f"  - {c.path.name}: {reason}")

    tail = [f"moved={moved}/{len(candidates)} ({human_size(moved_bytes)})"]
    if failed:
        tail.append("failed:")
        tail.extend(failed)
    tail.append("-" * 40)
    append_log(target_log, tail, max_size_kb=max_kb, backup_count=backups)

    return CleanupResult(
        downloads_dir=downloads,
        candidates=candidates,
        moved_count=moved,
        moved_bytes=moved_bytes,
        failed=failed,
        dry_run=False,
        timestamp_str=now_str,
    )


# -----------------------------
# Win32 システムトレイアイコン ＆ トースト通知
# -----------------------------
class Win32TrayIcon:
    """
    Windows 標準 Shell_NotifyIconW を用いた軽量・高精細タスクトレイ常駐アイコン＆トースト通知クラス
    """

    WM_USER = 0x0400
    WM_TRAYICON = WM_USER + 20
    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_NULL = 0x0000
    WM_CLOSE = 0x0010

    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    NIN_BALLOONUSERCLICK = WM_USER + 5

    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002

    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010

    NIIF_INFO = 0x00000001
    NIIF_WARNING = 0x00000002
    NIIF_ERROR = 0x00000003
    NIIF_NOSOUND = 0x00000010

    ID_OPEN_SETTINGS = 1001
    ID_RUN_CLEANUP = 1002
    ID_RUN_DRYRUN = 1003
    ID_OPEN_DOWNLOADS = 1004
    ID_OPEN_LOG = 1005
    ID_TOGGLE_AUTO = 1006
    ID_EXIT = 1009

    def __init__(self, icon_path: Path, tooltip: str, action_queue: queue.Queue[str]) -> None:
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.action_queue = action_queue
        self.hwnd: int | None = None
        self.hicon: int | None = None
        self.auto_enabled_state = False
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._wndproc_ref: Any = None
        self._nid: Any = None

    def start(self) -> None:
        if os.name != "nt":
            return
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=3.0)

    def stop(self) -> None:
        if os.name != "nt" or not self.hwnd:
            return
        try:
            self._remove_tray_icon()
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
        except Exception:
            pass

    def update_tooltip(self, tooltip: str) -> None:
        self.tooltip = tooltip
        if os.name != "nt" or not self.hwnd or not self._nid:
            return
        try:
            self._nid.uFlags = self.NIF_TIP
            self._nid.szTip = tooltip[:127]
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def show_toast(self, title: str, message: str, warning: bool = False) -> None:
        if os.name != "nt" or not self.hwnd or not self._nid:
            return
        try:
            self._nid.uFlags = self.NIF_INFO
            self._nid.szInfoTitle = title[:63]
            self._nid.szInfo = message[:255]
            self._nid.dwInfoFlags = self.NIIF_WARNING if warning else self.NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            pass

    def _remove_tray_icon(self) -> None:
        if self.hwnd and self._nid:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass

    def _run_message_loop(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        def wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == self.WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK, self.NIN_BALLOONUSERCLICK):
                    self.action_queue.put("show_settings")
                elif event in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                    self._show_context_menu(hwnd)
                return 0
            if msg == self.WM_CLOSE:
                self._remove_tray_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "DownloadFolderCleanerTrayWndClass"

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "DownloadFolderCleanerTrayHost",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinst,
            None,
        )
        self.hwnd = hwnd

        hicon = None
        if self.icon_path.is_file():
            hicon = user32.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x0010 | 0x0040)
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))
        self.hicon = hicon

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self.tooltip[:127]
        self._nid = nid

        shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self._nid))
        self._ready_event.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_context_menu(self, hwnd: int) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        MF_STRING = 0x00000000
        MF_SEPARATOR = 0x00000800
        MF_CHECKED = 0x00000008
        TPM_LEFTALIGN = 0x0000
        TPM_BOTTOMALIGN = 0x0020
        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100
        TPM_NONOTIFY = 0x0080

        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return

        try:
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN_SETTINGS, "⚙️ 設定画面を開く")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_RUN_CLEANUP, "🧹 今すぐ掃除を実行")
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_RUN_DRYRUN, "🔍 今すぐプレビュー (Dry-Run)")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)

            auto_flags = MF_STRING | (MF_CHECKED if self.auto_enabled_state else 0)
            user32.AppendMenuW(hmenu, auto_flags, self.ID_TOGGLE_AUTO, "⏱️ 定期自動掃除を有効にする")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN_DOWNLOADS, "📂 ダウンロードフォルダを開く")
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN_LOG, "📋 ログファイルを開く")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_EXIT, "🚪 終了")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu,
                TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                hwnd,
                None,
            )
            user32.PostMessageW(hwnd, self.WM_NULL, 0, 0)

            cmd_map = {
                self.ID_OPEN_SETTINGS: "show_settings",
                self.ID_RUN_CLEANUP: "run_cleanup",
                self.ID_RUN_DRYRUN: "run_dryrun",
                self.ID_OPEN_DOWNLOADS: "open_downloads",
                self.ID_OPEN_LOG: "open_log",
                self.ID_TOGGLE_AUTO: "toggle_auto",
                self.ID_EXIT: "exit_app",
            }
            if cmd in cmd_map:
                self.action_queue.put(cmd_map[cmd])
        finally:
            user32.DestroyMenu(hmenu)


# -----------------------------
# メインアプリケーション（タスクトレイ常駐 ＆ 設定画面 GUI）
# -----------------------------
class DownloadsCleanerApp:
    def __init__(self, guard: SingleInstanceGuard) -> None:
        self.guard = guard
        self.settings, self.is_first_launch = load_settings()
        self.action_queue: queue.Queue[str] = queue.Queue()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=COLOR_BG_ROOT)
        # ウィンドウサイズを 940x825 固定にする
        self.root.resizable(False, False)

        # ウィンドウの位置記憶トラッキング
        self._last_valid_geometry: str = self.settings.get("window_geometry", "")
        self._is_window_visible: bool = False
        self._last_periodic_check: float = time.time()
        self._cleanup_lock = threading.Lock()
        self._header_icon_photo: ImageTk.PhotoImage | None = None

        self._apply_window_icon()
        self._apply_saved_geometry()

        # 閉じるボタン(×)はタスクトレイへの格納とする
        self.root.protocol("WM_DELETE_WINDOW", self.hide_settings_window)
        self.root.bind("<Configure>", self._on_window_configure)

        # UI構築
        self._setup_styles()
        self._build_ui()
        self._load_settings_into_ui()

        # タスクトレイアイコン起動
        icon_path = get_resource_path(ICON_ICO_FILENAME)
        self.tray = Win32TrayIcon(
            icon_path=icon_path,
            tooltip=self._build_tray_tooltip(),
            action_queue=self.action_queue,
        )
        self.tray.auto_enabled_state = bool(self.settings.get("auto_interval_enabled", False))
        self.tray.start()

        # 初回起動時 vs 2回目以降の起動時挙動
        if self.is_first_launch:
            # 初回起動時のみ掃除を実行せずに設定画面を開く
            self.show_settings_window(first_launch_banner=True)
        else:
            # 2回目以降はタスクトレイ常駐で起動し、設定に沿って自動実行
            self.root.withdraw()
            self._is_window_visible = False
            if self.settings.get("run_on_startup_launch", True):
                self.root.after(400, lambda: self.trigger_cleanup_async(source="起動時自動実行"))

        # イベントポーリング開始（トレイ操作・二重起動イベント・定期タイマー）
        self.root.after(150, self._poll_events)

    def _apply_window_icon(self) -> None:
        ico_path = get_resource_path(ICON_ICO_FILENAME)
        if ico_path.is_file():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

    def _parse_geometry(self, geom_str: str) -> tuple[int, int] | None:
        """保存されたジオメトリ文字列から表示座標 (x, y) を抽出する（サイズは常に 940x825 固定）"""
        if not geom_str:
            return None
        m = re.match(r"^(?:\d+x\d+)?\+(-?\d+)\+(-?\d+)$", geom_str.strip())
        if not m:
            return None
        x = int(m.group(1))
        y = int(m.group(2))
        return x, y

    def _clamp_geometry_to_screen(self, x: int, y: int) -> str:
        w = WINDOW_WIDTH
        h = WINDOW_HEIGHT
        if os.name == "nt":
            user32 = ctypes.windll.user32
            vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
            vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            if vw > 0 and vh > 0:
                x = max(vx, min(x, vx + vw - 120))
                y = max(vy, min(y, vy + vh - 120))
                return f"{w}x{h}+{x}+{y}"

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, min(x, sw - 120))
        y = max(0, min(y, sh - 120))
        return f"{w}x{h}+{x}+{y}"

    def _apply_saved_geometry(self) -> None:
        geom = self.settings.get("window_geometry", "") or self._last_valid_geometry
        parsed = self._parse_geometry(geom)
        if parsed:
            x, y = parsed
            clamped = self._clamp_geometry_to_screen(x, y)
            self.root.geometry(clamped)
            self._last_valid_geometry = clamped
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = max(0, (sw - WINDOW_WIDTH) // 2)
            y = max(0, (sh - WINDOW_HEIGHT) // 2)
            default_geom = f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}"
            self.root.geometry(default_geom)
            self._last_valid_geometry = default_geom

    def _on_window_configure(self, _event: tk.Event[Any] | None = None) -> None:
        if not self._is_window_visible:
            return
        try:
            if self.root.state() != "normal":
                return
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            if x > -10000 and y > -10000:
                self._last_valid_geometry = f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}"
        except Exception:
            pass

    def _persist_window_geometry(self) -> None:
        self._on_window_configure()
        if self._last_valid_geometry:
            self.settings["window_geometry"] = self._last_valid_geometry
            self.settings["initialized"] = True
            self.is_first_launch = False
            try:
                save_settings(self.settings)
            except Exception:
                pass

    def show_settings_window(self, first_launch_banner: bool = False) -> None:
        self._apply_saved_geometry()
        self.root.deiconify()
        self.root.state("normal")
        self._is_window_visible = True
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(120, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

        if os.name == "nt":
            try:
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

        if first_launch_banner:
            self.var_status.set("💡 初回起動のため自動掃除は実行していません。設定を確認して「設定を保存」してください。")
            self.lbl_banner.configure(
                text="💡 初回起動セットアップ：自動掃除はまだ実行されていません。各オプションを確認し「💾 設定を保存」を押してください。",
                bg="#1E3A8A",
                fg="#BAE6FD",
            )
            self.lbl_banner.pack(fill=tk.X, padx=16, pady=(8, 0), before=self.notebook)
        else:
            self.lbl_banner.pack_forget()

        self.refresh_downloads_list()
        self.refresh_log_view()

    def hide_settings_window(self) -> None:
        self._persist_window_geometry()
        self._is_window_visible = False
        self.root.withdraw()

    def _build_tray_tooltip(self) -> str:
        hrs = self.settings.get("threshold_hours", 24.0)
        auto = "ON" if self.settings.get("auto_interval_enabled", False) else "OFF"
        mode = "Dry-Run" if self.settings.get("dry_run_mode", False) else "自動移動"
        return f"{APP_NAME}\n閾値: {hrs:g}時間 ({mode}) | 定期監視: {auto}"

    # -----------------------------
    # UI構築
    # -----------------------------
    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(
            "TNotebook",
            background=COLOR_BG_ROOT,
            borderwidth=0,
            tabmargins=[12, 8, 12, 0],
        )
        # 選択中のタブと非選択中のタブのサイズ・パディング・高さを完全に同一にする
        style.configure(
            "TNotebook.Tab",
            background=COLOR_BG_PANEL,
            foreground=COLOR_TEXT_MUTED,
            padding=[18, 9],
            font=(FONT_FAMILY, 10, "bold"),
            borderwidth=1,
            lightcolor=COLOR_BORDER,
            bordercolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            focuscolor=COLOR_ACCENT,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLOR_ACCENT), ("active", COLOR_BTN_SUB_HOVER)],
            foreground=[("selected", COLOR_TEXT_MAIN), ("active", COLOR_TEXT_MAIN)],
            padding=[("selected", [18, 9]), ("!selected", [18, 9])],
            expand=[("selected", [0, 0, 0, 0]), ("!selected", [0, 0, 0, 0])],
            lightcolor=[("selected", COLOR_ACCENT), ("!selected", COLOR_BORDER)],
            bordercolor=[("selected", COLOR_ACCENT), ("!selected", COLOR_BORDER)],
        )

        style.configure(
            "Cleaner.Treeview",
            background=COLOR_BG_INPUT,
            foreground=COLOR_TEXT_BODY,
            fieldbackground=COLOR_BG_INPUT,
            rowheight=28,
            font=(FONT_FAMILY, 9),
            borderwidth=0,
        )
        style.configure(
            "Cleaner.Treeview.Heading",
            background=COLOR_BG_PANEL,
            foreground=COLOR_TEXT_MAIN,
            font=(FONT_FAMILY, 9, "bold"),
            borderwidth=1,
        )
        style.map(
            "Cleaner.Treeview",
            background=[("selected", COLOR_ACCENT)],
            foreground=[("selected", COLOR_TEXT_MAIN)],
        )

    def _make_btn(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        bg: str = COLOR_BTN_SUB,
        hover_bg: str = COLOR_BTN_SUB_HOVER,
        fg: str = COLOR_TEXT_MAIN,
        font_size: int = 9,
        bold: bool = True,
        padx: int = 12,
        pady: int = 6,
    ) -> tk.Button:
        font_spec = (FONT_FAMILY, font_size, "bold") if bold else (FONT_FAMILY, font_size)
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover_bg,
            activeforeground=fg,
            font=font_spec,
            relief=tk.FLAT,
            bd=0,
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover_bg))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg))
        return btn

    def _build_ui(self) -> None:
        # 1. ヘッダーバー
        header = tk.Frame(self.root, bg=COLOR_BG_HEADER, padx=18, pady=12)
        header.pack(fill=tk.X)

        left_hdr = tk.Frame(header, bg=COLOR_BG_HEADER)
        left_hdr.pack(side=tk.LEFT, fill=tk.Y)

        png_path = get_resource_path(ICON_PNG_FILENAME)
        if png_path.is_file():
            try:
                pil_img = Image.open(png_path).convert("RGBA").resize((44, 44), Image.Resampling.LANCZOS)
                self._header_icon_photo = ImageTk.PhotoImage(pil_img)
                lbl_ico = tk.Label(left_hdr, image=self._header_icon_photo, bg=COLOR_BG_HEADER)
                lbl_ico.pack(side=tk.LEFT, padx=(0, 12))
            except Exception:
                pass

        title_box = tk.Frame(left_hdr, bg=COLOR_BG_HEADER)
        title_box.pack(side=tk.LEFT)
        tk.Label(
            title_box,
            text=APP_NAME,
            font=(FONT_FAMILY, 15, "bold"),
            fg=COLOR_TEXT_MAIN,
            bg=COLOR_BG_HEADER,
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="保存から一定時間が経過したダウンロードファイルを自動検出しゴミ箱へ整理（タスクトレイ常駐）",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_BG_HEADER,
            anchor="w",
        ).pack(anchor="w")

        right_hdr = tk.Frame(header, bg=COLOR_BG_HEADER)
        right_hdr.pack(side=tk.RIGHT)

        self._make_btn(
            right_hdr,
            "📂 フォルダを開く",
            self.open_downloads_folder,
            bg=COLOR_BTN_SUB,
            hover_bg=COLOR_BTN_SUB_HOVER,
        ).pack(side=tk.LEFT, padx=4)

        self._make_btn(
            right_hdr,
            "🔍 Dry-Run 確認",
            lambda: self.trigger_cleanup_async(override_dry_run=True, source="手動プレビュー"),
            bg=COLOR_ACCENT,
            hover_bg=COLOR_ACCENT_HOVER,
        ).pack(side=tk.LEFT, padx=4)

        self._make_btn(
            right_hdr,
            "🧹 今すぐ掃除を実行",
            lambda: self.trigger_cleanup_async(override_dry_run=False, source="手動実行"),
            bg=COLOR_SUCCESS,
            hover_bg=COLOR_SUCCESS_HOVER,
        ).pack(side=tk.LEFT, padx=4)

        # 初回起動バナー（通常は非表示）
        self.lbl_banner = tk.Label(
            self.root,
            text="",
            font=(FONT_FAMILY, 9, "bold"),
            padx=12,
            pady=6,
            anchor="w",
        )

        # 2. フッターバー（先に side=tk.BOTTOM で配置し、常に高さを確保）
        footer = tk.Frame(self.root, bg=COLOR_BG_HEADER, padx=18, pady=10)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        self.var_status = tk.StringVar(value="タスクトレイ常駐中 - 設定は settings.json に保存されます。")
        tk.Label(
            footer,
            textvariable=self.var_status,
            font=(FONT_FAMILY, 9),
            fg=COLOR_ACCENT_LIGHT,
            bg=COLOR_BG_HEADER,
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._make_btn(
            footer,
            "🚪 アプリを完全終了",
            self.exit_application,
            bg=COLOR_DANGER,
            hover_bg=COLOR_DANGER_HOVER,
            padx=12,
            pady=6,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        self._make_btn(
            footer,
            "🔽 トレイに隠す",
            self.hide_settings_window,
            bg=COLOR_BTN_SUB,
            hover_bg=COLOR_BTN_SUB_HOVER,
            padx=12,
            pady=6,
        ).pack(side=tk.RIGHT, padx=6)

        self._make_btn(
            footer,
            "💾 設定を保存",
            self.save_settings_from_ui,
            bg=COLOR_ACCENT,
            hover_bg=COLOR_ACCENT_HOVER,
            font_size=10,
            padx=18,
            pady=6,
        ).pack(side=tk.RIGHT, padx=6)

        # 3. タブノートブック
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 6))

        self.tab_general = tk.Frame(self.notebook, bg=COLOR_BG_ROOT, padx=14, pady=8)
        self.tab_pin = tk.Frame(self.notebook, bg=COLOR_BG_ROOT, padx=14, pady=10)
        self.tab_log = tk.Frame(self.notebook, bg=COLOR_BG_ROOT, padx=14, pady=10)

        self.notebook.add(self.tab_general, text=" ⚙️ 基本・スケジュール設定 ")
        self.notebook.add(self.tab_pin, text=" 📌 ピン留め(保護)・除外管理 ")
        self.notebook.add(self.tab_log, text=" 📋 実行ログ・履歴 ")

        self._build_tab_general()
        self._build_tab_pin()
        self._build_tab_log()

    def _create_card(self, parent: tk.Widget, title: str) -> tk.Frame:
        card = tk.Frame(
            parent,
            bg=COLOR_BG_PANEL,
            padx=14,
            pady=9,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
        )
        card.pack(fill=tk.X, pady=4)
        tk.Label(
            card,
            text=title,
            font=(FONT_FAMILY, 10, "bold"),
            fg=COLOR_ACCENT_LIGHT,
            bg=COLOR_BG_PANEL,
            anchor="w",
        ).pack(anchor="w", pady=(0, 5))
        return card

    def _build_tab_general(self) -> None:
        # --- カード1: 対象フォルダ＆経過時間・日時判定基準 ---
        c1 = self._create_card(self.tab_general, "📂 対象フォルダ ＆ 経過時間・日時判定ルール")

        row_path = tk.Frame(c1, bg=COLOR_BG_PANEL)
        row_path.pack(fill=tk.X, pady=4)
        tk.Label(
            row_path,
            text="対象フォルダ (空欄=自動検出):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=25,
            anchor="w",
        ).pack(side=tk.LEFT)

        self.var_downloads_path = tk.StringVar()
        ent_path = tk.Entry(
            row_path,
            textvariable=self.var_downloads_path,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            font=(FONT_FAMILY, 9),
        )
        ent_path.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=4)

        self._make_btn(row_path, "参照...", self._browse_downloads_dir, pady=3, padx=10).pack(side=tk.LEFT, padx=2)
        self._make_btn(row_path, "自動検出", lambda: self.var_downloads_path.set(""), pady=3, padx=10).pack(
            side=tk.LEFT, padx=2
        )

        # 経過時間
        row_hrs = tk.Frame(c1, bg=COLOR_BG_PANEL)
        row_hrs.pack(fill=tk.X, pady=4)
        tk.Label(
            row_hrs,
            text="ゴミ箱へ移す経過時間 (時間):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=25,
            anchor="w",
        ).pack(side=tk.LEFT)

        self.var_threshold_hours = tk.StringVar(value="24.0")
        tk.Entry(
            row_hrs,
            textvariable=self.var_threshold_hours,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=10,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(side=tk.LEFT, padx=6, ipady=4)

        tk.Label(row_hrs, text="時間以上経過  ｜ プリセット:", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_MUTED, bg=COLOR_BG_PANEL).pack(
            side=tk.LEFT, padx=4
        )
        for label, val in (("12時間", "12.0"), ("24時間", "24.0"), ("48時間", "48.0"), ("7日間(168h)", "168.0")):
            self._make_btn(
                row_hrs,
                label,
                lambda v=val: self.var_threshold_hours.set(v),
                pady=2,
                padx=8,
                font_size=8,
            ).pack(side=tk.LEFT, padx=2)

        # 日時判定基準
        row_basis = tk.Frame(c1, bg=COLOR_BG_PANEL)
        row_basis.pack(fill=tk.X, pady=4)
        tk.Label(
            row_basis,
            text="日時判定基準:",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=25,
            anchor="w",
        ).pack(side=tk.LEFT)

        self.var_time_basis = tk.StringVar(value="max_mtime_ctime")
        basis_frame = tk.Frame(row_basis, bg=COLOR_BG_PANEL)
        basis_frame.pack(side=tk.LEFT, fill=tk.X)

        for text, mode in (
            ("保存日時(ctime)と更新日時(mtime)の新しい方 [推奨・DL直後の誤削除防止]", "max_mtime_ctime"),
            ("更新日時(mtime)のみ", "mtime"),
            ("保存/作成日時(ctime)のみ", "ctime"),
        ):
            tk.Radiobutton(
                basis_frame,
                text=text,
                variable=self.var_time_basis,
                value=mode,
                bg=COLOR_BG_PANEL,
                fg=COLOR_TEXT_BODY,
                selectcolor=COLOR_BG_INPUT,
                activebackground=COLOR_BG_PANEL,
                activeforeground=COLOR_TEXT_MAIN,
                font=(FONT_FAMILY, 9),
            ).pack(anchor="w")

        # サイズフィルタ
        row_size = tk.Frame(c1, bg=COLOR_BG_PANEL)
        row_size.pack(fill=tk.X, pady=4)
        tk.Label(
            row_size,
            text="対象ファイルサイズ制限 (MB):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=25,
            anchor="w",
        ).pack(side=tk.LEFT)

        tk.Label(row_size, text="最小:", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_BODY, bg=COLOR_BG_PANEL).pack(side=tk.LEFT)
        self.var_min_size_mb = tk.StringVar(value="0.0")
        tk.Entry(
            row_size,
            textvariable=self.var_min_size_mb,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=8,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=4, ipady=4)

        tk.Label(row_size, text="MB  〜  最大:", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_BODY, bg=COLOR_BG_PANEL).pack(
            side=tk.LEFT, padx=4
        )
        self.var_max_size_mb = tk.StringVar(value="0.0")
        tk.Entry(
            row_size,
            textvariable=self.var_max_size_mb,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=8,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=4, ipady=4)
        tk.Label(row_size, text="MB  (0 = 制限なし)", font=(FONT_FAMILY, 8), fg=COLOR_TEXT_MUTED, bg=COLOR_BG_PANEL).pack(
            side=tk.LEFT, padx=4
        )

        # --- カード2: 起動時実行・タスクトレイ常駐スケジュール ---
        c2 = self._create_card(self.tab_general, "⏱️ 起動時実行 ＆ タスクトレイ常駐スケジュール設定")

        self.var_run_on_startup_launch = tk.BooleanVar(value=True)
        tk.Checkbutton(
            c2,
            text="プログラム起動時に設定されたオプションに沿って自動掃除を実行する（※初回起動時は実行せず設定画面を開きます）",
            variable=self.var_run_on_startup_launch,
            bg=COLOR_BG_PANEL,
            fg=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_TEXT_MAIN,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w", pady=3)

        row_auto = tk.Frame(c2, bg=COLOR_BG_PANEL)
        row_auto.pack(fill=tk.X, pady=3)
        self.var_auto_interval_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row_auto,
            text="タスクトレイ常駐中に定期自動掃除を実行する   ｜  チェック間隔:",
            variable=self.var_auto_interval_enabled,
            bg=COLOR_BG_PANEL,
            fg=COLOR_TEXT_BODY,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_TEXT_MAIN,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)

        self.var_auto_interval_minutes = tk.StringVar(value="60")
        tk.Entry(
            row_auto,
            textvariable=self.var_auto_interval_minutes,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=6,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(side=tk.LEFT, padx=4, ipady=4)
        tk.Label(row_auto, text="分ごと", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_BODY, bg=COLOR_BG_PANEL).pack(side=tk.LEFT)

        self.var_dry_run_mode = tk.BooleanVar(value=False)
        tk.Checkbutton(
            c2,
            text="テストモード (Dry-Run: 実際にはゴミ箱へ移動せず、検出ログの記録と通知のみ行う)",
            variable=self.var_dry_run_mode,
            bg=COLOR_BG_PANEL,
            fg=COLOR_WARNING,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_WARNING,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w", pady=3)

        self.var_windows_startup = tk.BooleanVar(value=True)
        tk.Checkbutton(
            c2,
            text="Windows スタートアップに登録する（PCログオン時に自動でタスクトレイ常駐を開始）",
            variable=self.var_windows_startup,
            command=self._on_toggle_windows_startup,
            bg=COLOR_BG_PANEL,
            fg=COLOR_ACCENT_LIGHT,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_ACCENT_LIGHT,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w", pady=3)

        # --- カード3: Windows トースト通知 ＆ ログローテーション ---
        c3 = self._create_card(self.tab_general, "🔔 Windows トースト通知 ＆ ログローテーション設定")

        row_notif = tk.Frame(c3, bg=COLOR_BG_PANEL)
        row_notif.pack(fill=tk.X, pady=3)
        self.var_notify_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(
            row_notif,
            text="ファイルをゴミ箱へ移動した際に Windows トースト通知を表示する",
            variable=self.var_notify_enabled,
            bg=COLOR_BG_PANEL,
            fg=COLOR_TEXT_BODY,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_TEXT_MAIN,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT)

        self._make_btn(
            row_notif,
            "🔔 テスト通知を送信",
            lambda: self.tray.show_toast(APP_NAME, "トースト通知のテストです。タスクトレイ常駐が正常に機能しています。"),
            pady=3,
            padx=10,
            font_size=8,
        ).pack(side=tk.RIGHT)

        self.var_notify_on_empty = tk.BooleanVar(value=False)
        tk.Checkbutton(
            c3,
            text="対象ファイルが 0 件だったときも通知を表示する",
            variable=self.var_notify_on_empty,
            bg=COLOR_BG_PANEL,
            fg=COLOR_TEXT_MUTED,
            selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL,
            activeforeground=COLOR_TEXT_MAIN,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w", pady=3)

        row_log_rot = tk.Frame(c3, bg=COLOR_BG_PANEL)
        row_log_rot.pack(fill=tk.X, pady=4)
        tk.Label(
            row_log_rot,
            text="ログ自動ローテーション上限:",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=25,
            anchor="w",
        ).pack(side=tk.LEFT)

        self.var_log_max_size_kb = tk.StringVar(value="1024")
        tk.Entry(
            row_log_rot,
            textvariable=self.var_log_max_size_kb,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=8,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=4, ipady=4)
        tk.Label(row_log_rot, text="KB (1024KB=1MB)  ｜ 保持世代数:", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_BODY, bg=COLOR_BG_PANEL).pack(
            side=tk.LEFT, padx=4
        )
        self.var_log_backup_count = tk.StringVar(value="3")
        tk.Entry(
            row_log_rot,
            textvariable=self.var_log_backup_count,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            width=5,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=4, ipady=4)
        tk.Label(row_log_rot, text="世代", font=(FONT_FAMILY, 9), fg=COLOR_TEXT_BODY, bg=COLOR_BG_PANEL).pack(side=tk.LEFT)

    def _build_tab_pin(self) -> None:
        # --- カード1: プレフィックス・除外パターン・拡張子別保持時間 ---
        c_rules = self._create_card(self.tab_pin, "🛡️ 保護プレフィックス・除外パターン・拡張子別保持時間")

        r1 = tk.Frame(c_rules, bg=COLOR_BG_PANEL)
        r1.pack(fill=tk.X, pady=4)
        tk.Label(
            r1,
            text="保護プレフィックス (カンマ区切り):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=28,
            anchor="w",
        ).pack(side=tk.LEFT)
        self.var_pin_prefixes = tk.StringVar(value="_, 📌, !")
        tk.Entry(
            r1,
            textvariable=self.var_pin_prefixes,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=4)

        r2 = tk.Frame(c_rules, bg=COLOR_BG_PANEL)
        r2.pack(fill=tk.X, pady=4)
        tk.Label(
            r2,
            text="除外パターン (カンマ区切り):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=28,
            anchor="w",
        ).pack(side=tk.LEFT)
        self.var_exclude_patterns = tk.StringVar()
        tk.Entry(
            r2,
            textvariable=self.var_exclude_patterns,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=4)

        r3 = tk.Frame(c_rules, bg=COLOR_BG_PANEL)
        r3.pack(fill=tk.X, pady=4)
        tk.Label(
            r3,
            text="拡張子別保持時間 (例 .iso=12, .pdf=168):",
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_BODY,
            bg=COLOR_BG_PANEL,
            width=28,
            anchor="w",
        ).pack(side=tk.LEFT)
        self.var_extension_hours = tk.StringVar()
        tk.Entry(
            r3,
            textvariable=self.var_extension_hours,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN,
            insertbackground=COLOR_TEXT_MAIN,
            relief=tk.FLAT,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, ipady=4)

        # --- カード2: ダウンロードフォルダ内のファイル一覧＆ワンクリックピン留め ---
        c_list = tk.Frame(
            self.tab_pin,
            bg=COLOR_BG_PANEL,
            padx=16,
            pady=12,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
        )
        c_list.pack(fill=tk.BOTH, expand=True, pady=6)

        top_bar = tk.Frame(c_list, bg=COLOR_BG_PANEL)
        top_bar.pack(fill=tk.X, pady=(0, 8))
        tk.Label(
            top_bar,
            text="📌 ダウンロードフォルダ内のファイル一覧（選択して「ピン留め保護切替」またはダブルクリックで保護/解除）",
            font=(FONT_FAMILY, 10, "bold"),
            fg=COLOR_ACCENT_LIGHT,
            bg=COLOR_BG_PANEL,
        ).pack(side=tk.LEFT)

        self._make_btn(top_bar, "🔄 一覧を更新", self.refresh_downloads_list, pady=3, padx=10, font_size=8).pack(
            side=tk.RIGHT, padx=2
        )
        self._make_btn(
            top_bar,
            "📌 選択項目のピン留め保護を切替",
            self.toggle_selected_pin,
            bg=COLOR_ACCENT,
            hover_bg=COLOR_ACCENT_HOVER,
            pady=3,
            padx=12,
            font_size=8,
        ).pack(side=tk.RIGHT, padx=4)

        tree_frame = tk.Frame(c_list, bg=COLOR_BG_INPUT)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("status", "kind", "name", "age", "size", "reason")
        self.tree_items = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="headings",
            style="Cleaner.Treeview",
            selectmode="extended",
        )
        self.tree_items.heading("status", text="状態")
        self.tree_items.heading("kind", text="種別")
        self.tree_items.heading("name", text="ファイル / フォルダ名")
        self.tree_items.heading("age", text="経過時間")
        self.tree_items.heading("size", text="サイズ")
        self.tree_items.heading("reason", text="判定詳細")

        self.tree_items.column("status", width=115, anchor="center")
        self.tree_items.column("kind", width=65, anchor="center")
        self.tree_items.column("name", width=330, anchor="w")
        self.tree_items.column("age", width=100, anchor="e")
        self.tree_items.column("size", width=95, anchor="e")
        self.tree_items.column("reason", width=165, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree_items.yview)
        self.tree_items.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_items.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree_items.bind("<Double-1>", lambda _e: self.toggle_selected_pin())

    def _build_tab_log(self) -> None:
        c_log = tk.Frame(
            self.tab_log,
            bg=COLOR_BG_PANEL,
            padx=16,
            pady=12,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
        )
        c_log.pack(fill=tk.BOTH, expand=True, pady=6)

        bar = tk.Frame(c_log, bg=COLOR_BG_PANEL)
        bar.pack(fill=tk.X, pady=(0, 8))

        self.var_log_info = tk.StringVar(value=f"ログファイル: {default_log_path()}")
        tk.Label(
            bar,
            textvariable=self.var_log_info,
            font=(FONT_FAMILY, 9),
            fg=COLOR_TEXT_MUTED,
            bg=COLOR_BG_PANEL,
            anchor="w",
        ).pack(side=tk.LEFT)

        self._make_btn(
            bar,
            "🗑️ ログをクリア",
            self.clear_log_file,
            bg=COLOR_DANGER,
            hover_bg=COLOR_DANGER_HOVER,
            pady=3,
            padx=10,
            font_size=8,
        ).pack(side=tk.RIGHT, padx=2)
        self._make_btn(bar, "📄 メモ帳で開く", self.open_log_file_external, pady=3, padx=10, font_size=8).pack(
            side=tk.RIGHT, padx=2
        )
        self._make_btn(bar, "🔄 最新に更新", self.refresh_log_view, pady=3, padx=10, font_size=8).pack(
            side=tk.RIGHT, padx=2
        )

        txt_frame = tk.Frame(c_log, bg=COLOR_BG_INPUT)
        txt_frame.pack(fill=tk.BOTH, expand=True)

        self.txt_log = tk.Text(
            txt_frame,
            bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_BODY,
            insertbackground=COLOR_TEXT_MAIN,
            font=("Consolas", 9),
            relief=tk.FLAT,
            wrap=tk.NONE,
        )
        vsb = ttk.Scrollbar(txt_frame, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # -----------------------------
    # 設定のUI反映・収集
    # -----------------------------
    def _load_settings_into_ui(self) -> None:
        s = self.settings
        self.var_downloads_path.set(str(s.get("downloads_path", "")))
        self.var_threshold_hours.set(str(s.get("threshold_hours", 24.0)))
        self.var_time_basis.set(str(s.get("time_basis", "max_mtime_ctime")))
        self.var_min_size_mb.set(str(s.get("min_size_mb", 0.0)))
        self.var_max_size_mb.set(str(s.get("max_size_mb", 0.0)))

        self.var_run_on_startup_launch.set(bool(s.get("run_on_startup_launch", True)))
        self.var_auto_interval_enabled.set(bool(s.get("auto_interval_enabled", False)))
        self.var_auto_interval_minutes.set(str(s.get("auto_interval_minutes", 60)))
        self.var_dry_run_mode.set(bool(s.get("dry_run_mode", False)))

        # デフォルト設定では「Windowsスタートアップに登録する」はオン (True)
        if self.is_first_launch:
            self.var_windows_startup.set(bool(s.get("windows_startup", True)))
        else:
            self.var_windows_startup.set(bool(s.get("windows_startup", is_startup_registered())))

        self.var_notify_enabled.set(bool(s.get("notify_enabled", True)))
        self.var_notify_on_empty.set(bool(s.get("notify_on_empty", False)))
        self.var_log_max_size_kb.set(str(s.get("log_max_size_kb", 1024)))
        self.var_log_backup_count.set(str(s.get("log_backup_count", 3)))

        prefixes = s.get("pin_prefixes", list(DEFAULT_PIN_PREFIXES))
        self.var_pin_prefixes.set(", ".join(prefixes))

        excludes = s.get("exclude_patterns", list(DEFAULT_EXCLUDES))
        self.var_exclude_patterns.set(", ".join(excludes))

        ext_map = s.get("extension_hours", {})
        if isinstance(ext_map, dict) and ext_map:
            self.var_extension_hours.set(", ".join(f"{k}={v:g}" for k, v in ext_map.items()))
        else:
            self.var_extension_hours.set("")

    def _collect_ui_to_settings(self) -> bool:
        try:
            hrs = float(self.var_threshold_hours.get().strip())
            if hrs < 0:
                raise ValueError("経過時間は 0 以上の数値を入力してください。")
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"経過時間の設定が不正です:\n{e}", parent=self.root)
            return False

        try:
            min_mb = float(self.var_min_size_mb.get().strip() or "0")
            max_mb = float(self.var_max_size_mb.get().strip() or "0")
            if min_mb < 0 or max_mb < 0 or (max_mb > 0 and min_mb > max_mb):
                raise ValueError("最小サイズ・最大サイズの範囲を確認してください。")
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"サイズ制限の設定が不正です:\n{e}", parent=self.root)
            return False

        try:
            interval_min = max(1, int(float(self.var_auto_interval_minutes.get().strip() or "60")))
            log_kb = max(64, int(float(self.var_log_max_size_kb.get().strip() or "1024")))
            log_cnt = max(0, int(float(self.var_log_backup_count.get().strip() or "3")))
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"数値項目の入力が不正です:\n{e}", parent=self.root)
            return False

        dl_path_str = self.var_downloads_path.get().strip()
        if dl_path_str:
            p = Path(dl_path_str).expanduser()
            if not p.is_dir():
                messagebox.showerror(APP_NAME, f"指定された対象フォルダが存在しません:\n{p}", parent=self.root)
                return False

        ext_map: dict[str, float] = {}
        ext_raw = self.var_extension_hours.get().strip()
        if ext_raw:
            for token in ext_raw.split(","):
                token = token.strip()
                if not token:
                    continue
                if "=" not in token:
                    messagebox.showerror(
                        APP_NAME,
                        f"拡張子別保持時間は '.iso=12, .pdf=168' の形式で入力してください:\n不正な項目: {token}",
                        parent=self.root,
                    )
                    return False
                k, v = token.split("=", 1)
                k = k.strip().lower()
                if not k.startswith("."):
                    k = "." + k
                try:
                    ext_map[k] = max(0.0, float(v.strip()))
                except ValueError:
                    messagebox.showerror(APP_NAME, f"拡張子 '{k}' の時間指定が数値ではありません: {v}", parent=self.root)
                    return False

        prefixes = [x.strip() for x in self.var_pin_prefixes.get().split(",") if x.strip()]
        excludes = [x.strip() for x in self.var_exclude_patterns.get().split(",") if x.strip()]
        want_startup = bool(self.var_windows_startup.get())

        self._on_window_configure()
        self.settings.update(
            {
                "initialized": True,
                "downloads_path": dl_path_str,
                "threshold_hours": hrs,
                "time_basis": self.var_time_basis.get(),
                "min_size_mb": min_mb,
                "max_size_mb": max_mb,
                "run_on_startup_launch": bool(self.var_run_on_startup_launch.get()),
                "auto_interval_enabled": bool(self.var_auto_interval_enabled.get()),
                "auto_interval_minutes": interval_min,
                "windows_startup": want_startup,
                "dry_run_mode": bool(self.var_dry_run_mode.get()),
                "notify_enabled": bool(self.var_notify_enabled.get()),
                "notify_on_empty": bool(self.var_notify_on_empty.get()),
                "log_max_size_kb": log_kb,
                "log_backup_count": log_cnt,
                "pin_prefixes": prefixes,
                "exclude_patterns": excludes,
                "extension_hours": ext_map,
                "window_geometry": self._last_valid_geometry,
            }
        )
        set_startup_registered(want_startup)
        self.is_first_launch = False
        return True

    def save_settings_from_ui(self) -> None:
        if not self._collect_ui_to_settings():
            return
        try:
            save_settings(self.settings)
            self.tray.auto_enabled_state = bool(self.settings.get("auto_interval_enabled", False))
            self.tray.update_tooltip(self._build_tray_tooltip())
            self.lbl_banner.pack_forget()
            self.var_status.set(f"✅ 設定を保存しました ({time.strftime('%H:%M:%S')})")
            self.refresh_downloads_list()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"設定の保存に失敗しました:\n{e}", parent=self.root)

    def _browse_downloads_dir(self) -> None:
        initial = self.var_downloads_path.get().strip()
        if not initial:
            try:
                initial = str(get_downloads_dir())
            except Exception:
                initial = str(Path.home())
        chosen = filedialog.askdirectory(title="対象ダウンロードフォルダを選択", initialdir=initial, parent=self.root)
        if chosen:
            self.var_downloads_path.set(chosen)
            self.refresh_downloads_list()

    def _on_toggle_windows_startup(self) -> None:
        want = bool(self.var_windows_startup.get())
        ok, msg = set_startup_registered(want)
        if ok:
            self.settings["windows_startup"] = want
            self.var_status.set(f"✅ {msg}")
        else:
            self.var_windows_startup.set(is_startup_registered())
            messagebox.showerror(APP_NAME, msg, parent=self.root)

    # -----------------------------
    # ピン留め一覧・ログ操作
    # -----------------------------
    def refresh_downloads_list(self) -> None:
        for item in self.tree_items.get_children():
            self.tree_items.delete(item)

        try:
            dl = get_downloads_dir(self.var_downloads_path.get().strip())
        except Exception as e:
            self.var_status.set(f"⚠️ フォルダ取得エラー: {e}")
            return

        try:
            hrs = max(0.0, float(self.var_threshold_hours.get().strip() or "24.0"))
        except ValueError:
            hrs = 24.0

        prefixes = [x.strip() for x in self.var_pin_prefixes.get().split(",") if x.strip()]
        excludes = [x.strip() for x in self.var_exclude_patterns.get().split(",") if x.strip()]
        pinned = list(self.settings.get("pinned_items", []))

        try:
            min_mb = max(0.0, float(self.var_min_size_mb.get().strip() or "0"))
            max_mb = max(0.0, float(self.var_max_size_mb.get().strip() or "0"))
        except ValueError:
            min_mb, max_mb = 0.0, 0.0

        ext_map: dict[str, float] = {}
        for token in self.var_extension_hours.get().split(","):
            if "=" in token:
                k, v = token.split("=", 1)
                k = k.strip().lower()
                if k and not k.startswith("."):
                    k = "." + k
                try:
                    ext_map[k] = float(v.strip())
                except ValueError:
                    pass

        _, all_items = scan_downloads_detailed(
            downloads=dl,
            threshold_hours=hrs,
            exclude_patterns=excludes,
            pin_prefixes=prefixes,
            pinned_items=pinned,
            extension_hours=ext_map,
            time_basis=self.var_time_basis.get(),
            min_size_mb=min_mb,
            max_size_mb=max_mb,
            compute_all_dir_sizes=False,
        )

        status_labels = {
            "target": "🧹 削除対象",
            "pinned": "📌 ピン留め保護",
            "excluded": "🛡️ 除外対象",
            "size_filtered": "📏 サイズ除外",
            "recent": "⏳ 時間未経過",
        }

        for info in all_items:
            st_lbl = status_labels.get(info.status, info.status)
            kind_lbl = "フォルダ" if info.kind == "dir" else "ファイル"
            size_lbl = human_size(info.size_bytes) if (info.kind == "file" or info.size_bytes > 0) else "-"
            self.tree_items.insert(
                "",
                tk.END,
                values=(st_lbl, kind_lbl, info.path.name, human_age(info.age_seconds), size_lbl, info.reason),
            )

    def toggle_selected_pin(self) -> None:
        selected = self.tree_items.selection()
        if not selected:
            messagebox.showinfo(APP_NAME, "一覧からピン留め（保護）を切り替える項目を選択してください。", parent=self.root)
            return

        pinned: list[str] = list(self.settings.get("pinned_items", []))
        pinned_map = {x.lower(): x for x in pinned}

        toggled_names: list[str] = []
        for iid in selected:
            vals = self.tree_items.item(iid, "values")
            if len(vals) >= 3:
                name = str(vals[2])
                key = name.lower()
                if key in pinned_map:
                    pinned = [x for x in pinned if x.lower() != key]
                    del pinned_map[key]
                else:
                    pinned.append(name)
                    pinned_map[key] = name
                toggled_names.append(name)

        self.settings["pinned_items"] = pinned
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self.refresh_downloads_list()
        if toggled_names:
            self.var_status.set(f"📌 ピン留め保護を更新しました: {', '.join(toggled_names[:3])}")

    def refresh_log_view(self) -> None:
        log_path = default_log_path()
        size_str = human_size(log_path.stat().st_size) if log_path.is_file() else "0 B"
        self.var_log_info.set(f"ログファイル: {log_path} ({size_str})")

        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        if log_path.is_file():
            try:
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                lines = content.splitlines()[-400:]
                self.txt_log.insert(tk.END, "\n".join(lines) + "\n")
                self.txt_log.see(tk.END)
            except Exception as e:
                self.txt_log.insert(tk.END, f"ログ読み込みエラー: {e}")
        else:
            self.txt_log.insert(tk.END, "（ログファイルはまだありません）")
        self.txt_log.configure(state=tk.DISABLED)

    def clear_log_file(self) -> None:
        log_path = default_log_path()
        try:
            if log_path.is_file():
                log_path.write_text("", encoding="utf-8")
            self.refresh_log_view()
            self.var_status.set("🗑️ ログをクリアしました。")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"ログのクリアに失敗しました:\n{e}", parent=self.root)

    def open_downloads_folder(self) -> None:
        try:
            dl = get_downloads_dir(self.settings.get("downloads_path", ""))
            if os.name == "nt":
                os.startfile(str(dl))
            else:
                subprocess.Popen(["xdg-open", str(dl)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"フォルダを開けませんでした:\n{e}", parent=self.root)

    def open_log_file_external(self) -> None:
        log_path = default_log_path()
        if not log_path.is_file():
            append_log(log_path, [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Log initialized"])
        try:
            if os.name == "nt":
                os.startfile(str(log_path))
            else:
                subprocess.Popen(["xdg-open", str(log_path)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"ログファイルを開けませんでした:\n{e}", parent=self.root)

    # -----------------------------
    # 掃除実行・イベントループ
    # -----------------------------
    def trigger_cleanup_async(self, override_dry_run: bool | None = None, source: str = "自動実行") -> None:
        if not self._cleanup_lock.acquire(blocking=False):
            return

        self.var_status.set(f"⏳ {source}: ダウンロードフォルダをスキャン中...")

        def worker() -> None:
            try:
                res = execute_cleanup_from_settings(self.settings, override_dry_run=override_dry_run)
                self.root.after(0, lambda: self._on_cleanup_finished(res, source))
            finally:
                self._cleanup_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _on_cleanup_finished(self, res: CleanupResult, source: str) -> None:
        if res.error_message:
            self.var_status.set(f"❌ {source}エラー: {res.error_message}")
            if self.settings.get("notify_enabled", True):
                self.tray.show_toast(APP_NAME, res.error_message, warning=True)
            return

        total_candidates = len(res.candidates)
        if total_candidates == 0:
            msg = f"対象ファイルはありませんでした（{res.timestamp_str}）"
            self.var_status.set(f"✅ {source}: {msg}")
            if self.settings.get("notify_enabled", True) and (
                self.settings.get("notify_on_empty", False) or source.startswith("手動")
            ):
                self.tray.show_toast(APP_NAME, msg)
        elif res.dry_run:
            msg = f"[Dry-Run] {total_candidates} 件の対象候補を検出しました（計 {human_size(res.moved_bytes)}・移動なし）"
            self.var_status.set(f"🔍 {source}: {msg}")
            if self.settings.get("notify_enabled", True):
                self.tray.show_toast(APP_NAME, msg)
        else:
            msg = f"{res.moved_count}/{total_candidates} 件をゴミ箱へ移動しました（合計 {human_size(res.moved_bytes)}）"
            self.var_status.set(f"🧹 {source}: {msg}")
            if self.settings.get("notify_enabled", True):
                self.tray.show_toast(APP_NAME, msg, warning=bool(res.failed))

        if self._is_window_visible:
            self.refresh_downloads_list()
            self.refresh_log_view()

    def _poll_events(self) -> None:
        # 1. 二重起動時の「設定画面を開く」シグナルチェック
        if self.guard.check_show_signaled():
            self.show_settings_window(first_launch_banner=False)

        # 2. タスクトレイメニューからのアクション処理
        while True:
            try:
                action = self.action_queue.get_nowait()
            except queue.Empty:
                break

            if action == "show_settings":
                self.show_settings_window(first_launch_banner=False)
            elif action == "run_cleanup":
                self.trigger_cleanup_async(override_dry_run=False, source="トレイ手動実行")
            elif action == "run_dryrun":
                self.trigger_cleanup_async(override_dry_run=True, source="トレイDry-Run")
            elif action == "open_downloads":
                self.open_downloads_folder()
            elif action == "open_log":
                self.open_log_file_external()
            elif action == "toggle_auto":
                cur = bool(self.settings.get("auto_interval_enabled", False))
                new_val = not cur
                self.settings["auto_interval_enabled"] = new_val
                self.var_auto_interval_enabled.set(new_val)
                self.tray.auto_enabled_state = new_val
                self.tray.update_tooltip(self._build_tray_tooltip())
                save_settings(self.settings)
                state_txt = "有効" if new_val else "無効"
                self.var_status.set(f"⏱️ 定期自動掃除を{state_txt}にしました。")
            elif action == "exit_app":
                self.exit_application()
                return

        # 3. 定期自動掃除タイマーチェック
        if not self.is_first_launch and self.settings.get("auto_interval_enabled", False):
            interval_sec = max(60, int(self.settings.get("auto_interval_minutes", 60)) * 60)
            now = time.time()
            if now - self._last_periodic_check >= interval_sec:
                self._last_periodic_check = now
                self.trigger_cleanup_async(source="定期自動掃除")

        self.root.after(200, self._poll_events)

    def exit_application(self) -> None:
        self._persist_window_geometry()
        self.tray.stop()
        self.guard.close()
        self.root.quit()
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


# -----------------------------
# CLI パーサー ＆ エントリポイント
# -----------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Folder Cleaner（引数なし起動でタスクトレイ常駐モード、--cli 指定でワンショットCLI実行）"
    )
    parser.add_argument("--cli", action="store_true", help="タスクトレイ常駐せず、CLIとして1回だけ実行して終了する")
    parser.add_argument("--settings", action="store_true", help="起動時に設定画面を強制的に開く")
    parser.add_argument("--path", type=str, default=None, help="Downloads フォルダを明示指定")
    parser.add_argument("--hours", type=float, default=None, help="この時間(時間)より古いものを対象（デフォルト: 設定値または24）")
    parser.add_argument(
        "--time-basis",
        choices=["max_mtime_ctime", "mtime", "ctime"],
        default=None,
        help="日時判定基準（max_mtime_ctime / mtime / ctime）",
    )
    parser.add_argument("--min-size-mb", type=float, default=None, help="最小対象サイズ (MB)")
    parser.add_argument("--max-size-mb", type=float, default=None, help="最大対象サイズ (MB)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="追加の除外パターン（ワイルドカード可）複数指定可",
    )
    parser.add_argument("--execute", action="store_true", help="[CLIモード] 実際にゴミ箱へ移動する（付けない場合は dry-run）")
    parser.add_argument("--yes", action="store_true", help="[CLIモード] --execute 時の確認を省略")
    parser.add_argument("--log-file", type=str, default=None, help="[CLIモード] ログファイル出力先")
    parser.add_argument("--quiet", action="store_true", help="[CLIモード] 標準出力を抑制")
    return parser


def run_cli_mode(args: argparse.Namespace) -> int:
    settings, _ = load_settings()

    hours = args.hours if args.hours is not None else float(settings.get("threshold_hours", 24.0))
    if hours < 0:
        print("hours は 0 以上の値にしてください。", file=sys.stderr)
        return 2

    explicit_path = args.path if args.path is not None else settings.get("downloads_path", "")
    try:
        downloads = get_downloads_dir(explicit_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    time_basis = args.time_basis or str(settings.get("time_basis", "max_mtime_ctime"))
    min_mb = args.min_size_mb if args.min_size_mb is not None else float(settings.get("min_size_mb", 0.0))
    max_mb = args.max_size_mb if args.max_size_mb is not None else float(settings.get("max_size_mb", 0.0))
    exclude_patterns = [*settings.get("exclude_patterns", DEFAULT_EXCLUDES), *(args.exclude or [])]
    pin_prefixes = list(settings.get("pin_prefixes", DEFAULT_PIN_PREFIXES))
    pinned_items = list(settings.get("pinned_items", []))
    extension_hours = dict(settings.get("extension_hours", {}))

    log_file = Path(args.log_file).expanduser() if args.log_file else default_log_path()
    max_kb = int(settings.get("log_max_size_kb", 1024))
    backups = int(settings.get("log_backup_count", 3))
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    header = [
        f"[{now_str}] {APP_NAME} CLI start",
        f"  downloads={downloads}",
        f"  threshold_hours={hours} (time_basis={time_basis})",
        f"  exclude={exclude_patterns}",
        f"  mode={'EXECUTE' if args.execute else 'DRY-RUN'}",
    ]
    append_log(log_file, header, max_size_kb=max_kb, backup_count=backups)

    candidates, _ = scan_downloads_detailed(
        downloads=downloads,
        threshold_hours=hours,
        exclude_patterns=exclude_patterns,
        pin_prefixes=pin_prefixes,
        pinned_items=pinned_items,
        extension_hours=extension_hours,
        time_basis=time_basis,
        min_size_mb=min_mb,
        max_size_mb=max_mb,
    )

    if not candidates:
        append_log(log_file, [f"[{now_str}] no candidates", "-" * 40], max_size_kb=max_kb, backup_count=backups)
        if not args.quiet:
            print("対象なし")
        return 0

    lines = [
        f"[{now_str}] candidates={len(candidates)}",
        *(
            f"  - [{c.kind}] {c.path.name} (age={human_age(c.age_seconds)}, size={human_size(c.size_bytes)})"
            for c in candidates
        ),
    ]
    append_log(log_file, lines, max_size_kb=max_kb, backup_count=backups)

    if not args.execute:
        append_log(log_file, ["dry-run: nothing moved", "-" * 40], max_size_kb=max_kb, backup_count=backups)
        if not args.quiet:
            for c in candidates:
                print(f"  - [{c.kind}] {c.path.name} (経過: {human_age(c.age_seconds)}, サイズ: {human_size(c.size_bytes)})")
            print(f"計 {len(candidates)} 件（dry-run のため移動しません。--execute を付けるとゴミ箱へ移動します）")
        return 0

    if not args.yes:
        if not args.quiet:
            ans = input(f"{len(candidates)} 件をゴミ箱へ移動しますか？ (yes/no): ").strip().lower()
        else:
            ans = "no"
        if ans not in ("yes", "y"):
            append_log(log_file, ["cancelled by user", "-" * 40], max_size_kb=max_kb, backup_count=backups)
            if not args.quiet:
                print("キャンセルしました。")
            return 0

    moved = 0
    failed: list[str] = []
    for c in candidates:
        ok, reason = trash_path(c.path)
        if ok:
            moved += 1
        else:
            failed.append(f"  - {c.path.name}: {reason}")

    tail = [f"moved={moved}/{len(candidates)}"]
    if failed:
        tail.append("failed:")
        tail.extend(failed)
    tail.append("-" * 40)
    append_log(log_file, tail, max_size_kb=max_kb, backup_count=backups)

    if not args.quiet:
        print(f"ゴミ箱へ移動: {moved}/{len(candidates)}")
        if failed:
            print("失敗:")
            for f in failed:
                print(f)

    return 0


def main(argv: list[str] | None = None) -> int:
    _apply_console_icon()
    args = build_parser().parse_args(argv)

    is_cli_invocation = bool(
        args.cli
        or args.path is not None
        or args.hours is not None
        or args.time_basis is not None
        or args.min_size_mb is not None
        or args.max_size_mb is not None
        or args.exclude
        or args.execute
        or args.log_file is not None
    )
    if is_cli_invocation:
        return run_cli_mode(args)

    # タスクトレイ常駐GUIモード（二重起動チェック）
    guard = SingleInstanceGuard()
    if not guard.is_primary:
        guard.close()
        return 0

    try:
        app = DownloadsCleanerApp(guard)
        if args.settings:
            app.show_settings_window(first_launch_banner=False)
        return app.run()
    finally:
        guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
