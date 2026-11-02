import os
import collections
import contextlib
import cv2
import io
import numpy as np
import threading
import time
import sqlite3
import json
import shutil
import sys
import multiprocessing
import calendar
import math
from datetime import date, datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk, ImageSequence

import core
from core import *  # 화면 코드가 쓰는 설정·함수를 그대로 쓴다 (밑줄로 시작하는 이름은 빠진다)

# 메인 화면 출근/퇴근 카드 크기: 키 → (관리자 화면에 보이는 이름, 기본 디자인 470x340 대비 배율)
# '크게'는 1600x900 화면에 두 카드를 넣을 수 있는 최대에 가깝다 (더 키우면 양옆 여백이 거의 없다)
MAIN_BUTTON_SIZES = {"normal": ("보통", 1.4), "large": ("크게", 1.5)}
DEFAULT_MAIN_BUTTON_SIZE = "normal"

RIGHT_PANEL_WIDTH = 620  # 우측 UI 패널 폭 — 1600x900 기준 값이고, 실제로는 px()로 화면 배율을 곱해서 쓴다

# 기준 창 크기(창공시스템과 동일). 모니터가 이보다 작으면 UI_SCALE만큼 전체를 줄여서 띄운다.
BASE_WIN_W, BASE_WIN_H = 1600, 900
UI_SCALE = 1.0

# 화면 글꼴 — 맑은 고딕은 윈도우에만 있어서 macOS에서는 기본 한글 글꼴(애플 SD 산돌고딕 Neo)을 쓴다
if IS_MAC:
    UI_FONT = "Apple SD Gothic Neo"
elif IS_WINDOWS:
    UI_FONT = "맑은 고딕"
else:
    UI_FONT = "NanumGothic"  # 라즈베리파이 등 리눅스 — raspberrypi/install.sh가 fonts-nanum을 설치한다


# macOS의 Tk는 사진을 화면에 올리는 속도(PhotoImage.paste)가 윈도우보다 3~4배 느리다
# (944x708 기준 윈도우 9ms / 맥 30ms 이상). 카메라 원본이 640x480이라 크게 늘려도 화질은 그대로이므로,
# 맥에서는 표시 배율에 상한을 둬서 UI 스레드가 그리기에 묶이지 않게 한다. (736x552 ≈ 14ms)
# 카메라 영상을 화면에 그릴 때 최대 확대 배율 — 라즈베리파이는 확대하지 않는다(그리는 픽셀 수가 곧 부하)
MAX_DISPLAY_SCALE = 1.15 if IS_MAC else (1.0 if LOW_POWER else None)

class FlatButton(tk.Label):
    """
    macOS용 버튼. 맥의 tk.Button은 시스템 버튼 모양으로 그려져서 bg(배경색)가 무시되고,
    흰 글씨(fg="white") 버튼은 흰 바탕에 글자가 안 보인다. Label로 같은 모양을 흉내 낸다.
    tk.Button과 같은 옵션(command, bg, activebackground, state ...)을 그대로 받는다.
    """

    def __init__(self, master=None, command=None, activebackground=None, activeforeground=None,
                 overrelief=None, **kw):
        super().__init__(master, **kw)
        self._command = command
        self._active_bg = activebackground
        self._active_fg = activeforeground
        self._normal_bg = self.cget("bg")
        self._normal_fg = self.cget("fg")
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_click)

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        if "command" in kw:
            self._command = kw.pop("command")
        if "activebackground" in kw:
            self._active_bg = kw.pop("activebackground")
        if "activeforeground" in kw:
            self._active_fg = kw.pop("activeforeground")
        kw.pop("overrelief", None)
        for key, attr in (("bg", "_normal_bg"), ("background", "_normal_bg"),
                          ("fg", "_normal_fg"), ("foreground", "_normal_fg")):
            if key in kw:
                setattr(self, attr, kw[key])
        if not kw:
            return super().configure()
        result = super().configure(**kw)
        if self._hover and ("bg" in kw or "background" in kw):
            self._on_enter()
        return result

    config = configure

    def _enabled(self):
        return str(self.cget("state")) != tk.DISABLED

    def _on_enter(self, _event=None):
        self._hover = True
        if self._enabled():
            if self._active_bg:
                super().configure(bg=self._active_bg)
            if self._active_fg:
                super().configure(fg=self._active_fg)

    def _on_leave(self, _event=None):
        self._hover = False
        super().configure(bg=self._normal_bg, fg=self._normal_fg)

    def _on_click(self, event):
        # 누른 채로 버튼 밖으로 나가서 뗐으면 취소 (일반 버튼과 동일)
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if inside:
            self.invoke()

    def invoke(self):
        if self._enabled() and self._command:
            return self._command()


# 맥에서는 배경색이 적용되는 FlatButton, 윈도우에서는 원래 tk.Button을 그대로 쓴다
Button = FlatButton if IS_MAC else tk.Button


def px(value):
    """1600x900 기준으로 잡은 픽셀 값을 현재 화면 배율에 맞춰 바꾼다."""
    return max(1, int(round(value * UI_SCALE)))


def _get_work_area(window):
    """작업 표시줄을 뺀 실제 사용 가능한 화면 크기(가로, 세로)를 구한다."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    if IS_MAC:
        # 메뉴 막대와 Dock을 뺀 영역 (pyobjc가 있을 때만)
        try:
            from AppKit import NSScreen
            frame = NSScreen.mainScreen().visibleFrame()
            return int(frame.size.width), int(frame.size.height)
        except Exception:
            pass
    # 윈도우가 아니거나 조회에 실패하면 작업 표시줄 높이를 대략 빼서 쓴다
    return window.winfo_screenwidth(), window.winfo_screenheight() - 48

# ==========================================
# 2. UI 상태 프레임 정의
# ==========================================

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def format_date_ko(d):
    return f"{d:%Y-%m-%d} ({WEEKDAY_KO[d.weekday()]})"


class DatePicker(Button):
    """누르면 달력이 펼쳐지고, 날짜를 고르면 값이 바뀌는 버튼. (tkcalendar 같은 추가 설치 없이 동작)"""

    def __init__(self, master, initial, on_change=None, flat=False, font_size=10):
        # flat=True — 테두리·달력 기호 없이 글자만 (테두리 있는 상자 안에 넣어 쓸 때)
        super().__init__(
            master, bg="white", fg="#0F172A", activebackground="#F1F5F9",
            relief=tk.FLAT if flat else tk.SOLID, bd=0 if flat else 1,
            font=(UI_FONT, font_size), padx=px(8), pady=px(2), cursor="hand2", command=self.toggle_calendar
        )
        self.on_change = on_change
        self.popup = None
        self._prefix = "" if flat else "📅 "
        self.set_date(initial)

    def get_date(self):
        return self._date

    def set_date(self, value):
        self._date = value
        self.config(text=f"{self._prefix}{format_date_ko(value)}")

    def toggle_calendar(self):
        if self.popup is not None:
            self.close_calendar()
            return

        self.popup = tk.Toplevel(self)
        self.popup.overrideredirect(True)
        self.popup.configure(bg="#CBD5E1")  # 바깥 1px 테두리
        self.body = tk.Frame(self.popup, bg="white")
        self.body.pack(padx=1, pady=1)

        self._view_year, self._view_month = self._date.year, self._date.month
        self._render_month()
        self._place_popup()

        # 달력 밖을 누르면 닫는다 — grab 중에는 창 밖 클릭도 이 팝업으로 전달된다
        self.popup.grab_set()
        self.popup.bind("<ButtonPress-1>", self._on_click)
        self.popup.bind("<Escape>", lambda e: self.close_calendar())
        self.popup.focus_set()

    def close_calendar(self):
        if self.popup is not None:
            self.popup.grab_release()
            self.popup.destroy()
            self.popup = None

    def _place_popup(self):
        self.popup.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        # 화면 아래/오른쪽으로 넘치면 안쪽으로 당긴다
        if y + self.popup.winfo_reqheight() > self.winfo_screenheight():
            y = self.winfo_rooty() - self.popup.winfo_reqheight() - 2
        x = min(x, self.winfo_screenwidth() - self.popup.winfo_reqwidth())
        self.popup.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _on_click(self, event):
        p = self.popup
        inside = (p.winfo_rootx() <= event.x_root < p.winfo_rootx() + p.winfo_width()
                  and p.winfo_rooty() <= event.y_root < p.winfo_rooty() + p.winfo_height())
        if not inside:
            self.close_calendar()

    def _shift_month(self, delta):
        index = self._view_month - 1 + delta
        self._view_year += index // 12
        self._view_month = index % 12 + 1
        self._render_month()

    def _pick(self, value):
        self.close_calendar()
        self.set_date(value)
        if self.on_change:
            self.on_change()

    def _render_month(self):
        for widget in self.body.winfo_children():
            widget.destroy()

        header = tk.Frame(self.body, bg="white")
        header.grid(row=0, column=0, columnspan=7, sticky=tk.EW, pady=(px(6), px(4)))
        nav_style = dict(bg="white", fg="#334155", activebackground="#E2E8F0", bd=0,
                         font=(UI_FONT, 11, "bold"), cursor="hand2", padx=px(8))
        Button(header, text="◀", command=lambda: self._shift_month(-1), **nav_style).pack(side=tk.LEFT, padx=px(4))
        Button(header, text="▶", command=lambda: self._shift_month(1), **nav_style).pack(side=tk.RIGHT, padx=px(4))
        tk.Label(header, text=f"{self._view_year}년 {self._view_month}월", bg="white", fg="#0F172A",
                 font=(UI_FONT, 11, "bold")).pack(side=tk.LEFT, expand=True)

        for col, name in enumerate(WEEKDAY_KO):
            color = "#EF4444" if col == 6 else "#3B82F6" if col == 5 else "#64748B"
            tk.Label(self.body, text=name, bg="white", fg=color, width=4,
                     font=(UI_FONT, 9, "bold")).grid(row=1, column=col, pady=(0, px(2)))

        today = date.today()
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self._view_year, self._view_month)
        for row, week in enumerate(weeks, start=2):
            for col, day in enumerate(week):
                selected = day == self._date
                is_today = day == today
                if selected:
                    bg, fg = "#3B82F6", "white"
                elif day.month != self._view_month:
                    bg, fg = "white", "#CBD5E1"
                else:
                    bg = "#DBEAFE" if is_today else "white"
                    fg = "#EF4444" if col == 6 else "#3B82F6" if col == 5 else "#0F172A"
                Button(
                    self.body, text=str(day.day), width=4, bg=bg, fg=fg, bd=0, relief=tk.FLAT,
                    activebackground="#BFDBFE", cursor="hand2",
                    font=(UI_FONT, 10, "bold" if (selected or is_today) else "normal"),
                    command=lambda d=day: self._pick(d)
                ).grid(row=row, column=col, padx=1, pady=1)

        Button(
            self.body, text=f"오늘 ({format_date_ko(today)})", bg="#F1F5F9", fg="#334155", bd=0,
            activebackground="#E2E8F0", font=(UI_FONT, 9, "bold"), cursor="hand2",
            command=lambda: self._pick(today)
        ).grid(row=len(weeks) + 2, column=0, columnspan=7, sticky=tk.EW, padx=px(6), pady=px(6))


# 전체 출결로그 조회 조건(LOG_FILTER_MODES)은 웹 관리자 화면과 같이 쓰도록 core.py에 있다
ACTION_LABELS = {"CHECK_IN": "출근", "CHECK_OUT": "퇴근"}
ACTION_COLORS = {"CHECK_IN": ("#10B981", "#059669"), "CHECK_OUT": ("#3B82F6", "#2563EB")}


def center_on(window, popup, rel_y=0.4):
    """popup을 window 가운데(세로는 rel_y 위치)에 띄운다."""
    popup.update_idletasks()
    x = window.winfo_rootx() + (window.winfo_width() - popup.winfo_reqwidth()) // 2
    y = window.winfo_rooty() + int((window.winfo_height() - popup.winfo_reqheight()) * rel_y)
    popup.geometry(f"+{max(0, x)}+{max(0, y)}")


# ==========================================
# 화면 키보드 (터치스크린용 — 라즈베리파이 등)
# ==========================================
HANGUL_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
HANGUL_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
HANGUL_JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ",
               "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]
COMPOUND_JUNG = {("ㅗ", "ㅏ"): "ㅘ", ("ㅗ", "ㅐ"): "ㅙ", ("ㅗ", "ㅣ"): "ㅚ", ("ㅜ", "ㅓ"): "ㅝ",
                 ("ㅜ", "ㅔ"): "ㅞ", ("ㅜ", "ㅣ"): "ㅟ", ("ㅡ", "ㅣ"): "ㅢ"}
COMPOUND_JONG = {("ㄱ", "ㅅ"): "ㄳ", ("ㄴ", "ㅈ"): "ㄵ", ("ㄴ", "ㅎ"): "ㄶ", ("ㄹ", "ㄱ"): "ㄺ", ("ㄹ", "ㅁ"): "ㄻ",
                 ("ㄹ", "ㅂ"): "ㄼ", ("ㄹ", "ㅅ"): "ㄽ", ("ㄹ", "ㅌ"): "ㄾ", ("ㄹ", "ㅍ"): "ㄿ", ("ㄹ", "ㅎ"): "ㅀ",
                 ("ㅂ", "ㅅ"): "ㅄ"}
SPLIT_JONG = {compound: pair for pair, compound in COMPOUND_JONG.items()}


def compose_hangul(jamos):
    """두벌식으로 누른 자모 목록을 한글 글자로 조합한다. 예: ㅎㅏㄴㄱㅡㄹ → 한글, ㄱㅏㄱㅏ → 가가"""
    out = []
    cho = jung = jong = None

    def flush():
        nonlocal cho, jung, jong
        if cho and jung:
            out.append(chr(0xAC00 + (HANGUL_CHO.index(cho) * 21 + HANGUL_JUNG.index(jung)) * 28
                           + HANGUL_JONG.index(jong or "")))
        elif cho or jung:
            out.append(cho or jung)  # 자음만 / 모음만 있으면 낱자 그대로
        cho = jung = jong = None

    for j in jamos:
        if j in HANGUL_JUNG:
            if cho and not jung:
                jung = j
            elif jung and not jong and (jung, j) in COMPOUND_JUNG:
                jung = COMPOUND_JUNG[(jung, j)]
            elif jong:
                # 받침 뒤에 모음이 오면 받침을 다음 글자 첫소리로 넘긴다 (각+ㅏ → 가가, 앉+ㅏ → 안자)
                if jong in SPLIT_JONG:
                    jong, moved = SPLIT_JONG[jong]
                else:
                    jong, moved = None, jong
                flush()
                cho, jung = moved, j
            else:
                flush()
                jung = j
        else:
            if not cho and not jung:
                cho = j
            elif jung and not jong and cho and j in HANGUL_JONG:
                jong = j
            elif jong and (jong, j) in COMPOUND_JONG:
                jong = COMPOUND_JONG[(jong, j)]
            else:
                flush()
                cho = j
    flush()
    return "".join(out)


class OnScreenKeyboard:
    """
    입력칸을 누르면 화면 아래(입력칸이 아래쪽에 있으면 위)에 뜨는 터치용 키보드.
    - 한글은 두벌식으로 직접 조합한다 — 라즈베리파이에서는 Tk 프로그램에 한글 입력기가 연결되지 않는다.
    - 관리자 로그인 창처럼 다른 화면 클릭을 막는(grab) 창에서도 눌리도록, 키보드 창을 입력칸이 있는 창의
      자식으로 만든다 (grab은 그 창과 자식 창의 클릭만 받는다).
    - 키보드가 입력칸을 가려도 무엇을 쳤는지 보이도록 키보드 위쪽에 입력 내용을 같이 보여준다.
    """
    LAYOUTS = {
        "ko": [list("ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔ"), list("ㅁㄴㅇㄹㅎㅗㅓㅏㅣ"), list("ㅋㅌㅊㅍㅠㅜㅡ")],
        "en": [list("qwertyuiop"), list("asdfghjkl"), list("zxcvbnm")],
    }
    SHIFT_KO = {"ㅂ": "ㅃ", "ㅈ": "ㅉ", "ㄷ": "ㄸ", "ㄱ": "ㄲ", "ㅅ": "ㅆ", "ㅐ": "ㅒ", "ㅔ": "ㅖ"}
    HEIGHT_RATIO = 0.42  # 메인 창 높이 대비 키보드 높이

    def __init__(self, root, enabled):
        self.root = root
        self.enabled = enabled      # 지금 켜져 있는지 묻는 함수 (관리자 화면 설정)
        self.target = None          # 지금 입력 중인 칸
        self.panel = None
        self.mode = "ko"
        self.shift = False
        self.jamo = []              # 조합 중인 한글 자모
        self.comp_start = 0
        self.comp_len = 0
        self.dismissed = None       # [완료]로 닫은 입력칸 — 다시 누르기 전까지는 키보드를 띄우지 않는다
        root.bind_class("TEntry", "<FocusIn>", self._on_focus, add="+")
        root.bind_class("TEntry", "<Button-1>", self._on_entry_click, add="+")

    # ---------- 보이기 / 숨기기 ----------
    def _on_focus(self, event):
        widget = event.widget
        if not self.enabled() or str(widget.cget("state")) in ("readonly", "disabled"):
            return
        # 키보드를 닫으면 커서가 입력칸으로 돌아오며 이 신호가 다시 온다 — 닫은 칸은 다시 눌러야 뜬다
        if widget is self.dismissed:
            return
        if widget is self.target and self.panel is not None and self.panel.winfo_exists() \
                and self.panel.winfo_ismapped():
            return
        self.commit()
        self.target = widget
        if not getattr(widget, "_osk_bound", False):
            widget.bind("<Destroy>", lambda e, w=widget: self.hide() if w is self.target else None, add="+")
            widget._osk_bound = True
        self.show()

    def _on_entry_click(self, event):
        if event.widget is self.target:
            self.commit()  # 입력칸을 눌러 커서를 옮기면 조합 중이던 글자는 확정한다
        elif event.widget is self.dismissed:
            # [완료]로 닫은 칸을 다시 누르면 키보드를 다시 띄운다
            self.dismissed = None
            self._on_focus(event)

    def show(self):
        owner = self.target.winfo_toplevel()
        if self.panel is None or not self.panel.winfo_exists() or self.panel.master is not owner:
            if self.panel is not None and self.panel.winfo_exists():
                self.panel.destroy()
            self.panel = tk.Toplevel(owner)
            self.panel.overrideredirect(True)
            self.panel.configure(bg="#CBD5E1")
            self._build()
        self._place()
        self.panel.deiconify()
        self._keep_on_top()
        # 입력칸을 누르면 그 창이 활성화되며 한 박자 늦게 앞으로 올라올 수 있어서, 잠시 뒤 한 번 더 올린다
        self.panel.after(150, self._keep_on_top)
        self._refresh_preview()

    def done(self):
        """[완료] — 지금 칸에서는 키보드를 닫는다 (그 칸을 다시 누르거나 다른 칸을 누르면 다시 뜬다)."""
        self.dismissed = self.target
        self.hide()

    def hide(self):
        self.commit()
        self.target = None
        if self.panel is not None and self.panel.winfo_exists():
            self.panel.withdraw()

    def _place(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = int(self.root.winfo_height() * self.HEIGHT_RATIO)
        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty() + self.root.winfo_height() - h
        # 입력칸이 키보드에 가려지는 위치면 키보드를 화면 위쪽에 띄운다
        target_bottom = self.target.winfo_rooty() + self.target.winfo_height()
        if target_bottom > y - px(10):
            y = self.root.winfo_rooty()
        self.panel.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- 자판 그리기 ----------
    def _build(self):
        for widget in self.panel.winfo_children():
            widget.destroy()
        body = tk.Frame(self.panel, bg="#E2E8F0")
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.preview = tk.Label(body, text="", anchor=tk.W, bg="white", fg="#0F172A",
                                font=(UI_FONT, 15, "bold"), padx=px(14), pady=px(6))
        self.preview.pack(fill=tk.X, padx=px(8), pady=(px(8), px(4)))

        keys = tk.Frame(body, bg="#E2E8F0")
        keys.pack(fill=tk.BOTH, expand=True, padx=px(6), pady=(0, px(6)))
        layout = self.LAYOUTS[self.mode]
        rows = [list("1234567890")]
        for i, row in enumerate(layout):
            if self.shift:
                row = [self.SHIFT_KO.get(k, k) if self.mode == "ko" else k.upper() for k in row]
            if i == 2:
                row = ["⇧"] + row + ["⌫"]
            rows.append(row)
        rows.append(["한/영", ".", "@", "␣", "-", "_", "완료"])

        for r, row in enumerate(rows):
            line = tk.Frame(keys, bg="#E2E8F0")
            line.pack(fill=tk.BOTH, expand=True)
            for label in row:
                self._key(line, label).pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=px(3), pady=px(3))

    def _key(self, parent, label):
        special = {"⇧": self._toggle_shift, "⌫": self.backspace, "한/영": self._toggle_mode,
                   "완료": self.done, "␣": lambda: self.press(" ")}
        command = special.get(label, lambda ch=label: self.press(ch))
        if label == "완료":
            colors = dict(bg="#3B82F6", fg="white", activebackground="#2563EB", activeforeground="white")
        elif label in special:
            colors = dict(bg="#94A3B8" if not (label == "⇧" and self.shift) else "#475569", fg="white",
                          activebackground="#64748B", activeforeground="white")
        else:
            colors = dict(bg="white", fg="#0F172A", activebackground="#DBEAFE", activeforeground="#0F172A")
        text = {"␣": "띄어쓰기", "한/영": "한/영" if self.mode == "ko" else "영/한"}.get(label, label)
        return Button(parent, text=text, font=(UI_FONT, 16, "bold"), bd=0, cursor="hand2",
                      takefocus=0, command=command, **colors)

    def _toggle_shift(self):
        self.shift = not self.shift
        self._build()
        self._refocus()

    def _toggle_mode(self):
        self.commit()
        self.mode = "en" if self.mode == "ko" else "ko"
        self.shift = False
        self._build()
        self._refocus()

    # ---------- 입력 ----------
    def press(self, ch):
        t = self.target
        if t is None or not t.winfo_exists():
            self.hide()
            return
        if self.mode == "ko" and (ch in HANGUL_CHO or ch in HANGUL_JUNG):
            self._compose(ch)
        else:
            self.commit()
            self._delete_selection()
            t.insert(tk.INSERT, ch)
        if self.shift:  # 한 글자만 쌍자음/대문자로 치고 돌아온다
            self.shift = False
            self._build()
        self._refocus()

    def _compose(self, jamo):
        t = self.target
        if not self.jamo:
            self._delete_selection()
            self.comp_start = t.index(tk.INSERT)
            self.comp_len = 0
        self.jamo.append(jamo)
        self._replace_composition(compose_hangul(self.jamo))

    def _replace_composition(self, text):
        t = self.target
        t.delete(self.comp_start, self.comp_start + self.comp_len)
        t.insert(self.comp_start, text)
        self.comp_len = len(text)
        t.icursor(self.comp_start + self.comp_len)

    def backspace(self):
        t = self.target
        if t is None or not t.winfo_exists():
            return
        if self.jamo:  # 조합 중이면 마지막 자모만 지운다 (한 → 하)
            self.jamo.pop()
            self._replace_composition(compose_hangul(self.jamo))
            if not self.jamo:
                self.commit()
        elif t.selection_present():
            self._delete_selection()
        else:
            i = t.index(tk.INSERT)
            if i > 0:
                t.delete(i - 1)
        self._refocus()

    def commit(self):
        self.jamo = []
        self.comp_len = 0

    def _delete_selection(self):
        t = self.target
        if t is not None and t.selection_present():
            t.delete(tk.SEL_FIRST, tk.SEL_LAST)

    def _refocus(self):
        t = self.target
        # 커서가 이미 입력칸에 있으면 건드리지 않는다 — 윈도우에서는 focus_set이 창을 활성화하면서
        # 키보드가 다른 창 뒤로 숨어, 첫 글자만 입력되고 다음 키가 안 눌리는 일이 있었다
        if t is not None and t.winfo_exists() and self.root.focus_get() is not t:
            t.focus_set()
        self._keep_on_top()
        self._refresh_preview()

    def _keep_on_top(self):
        if self.panel is not None and self.panel.winfo_exists() and self.panel.winfo_ismapped():
            self.panel.attributes("-topmost", True)
            self.panel.lift()

    def _refresh_preview(self):
        if self.panel is None or not self.panel.winfo_exists() or self.target is None \
                or not self.target.winfo_exists():
            return
        text = self.target.get()
        if str(self.target.cget("show")):
            text = "•" * len(text)  # 비밀번호는 가려서 보여준다
        mode = "한글" if self.mode == "ko" else "영어"
        self.preview.config(text=f"[{mode}]  {text}▏")


class AdminLoginDialog(tk.Toplevel):
    """상단 [관리자] 버튼을 누르면 뜨는 로그인 창."""

    def __init__(self, app):
        super().__init__(app.window)
        self.app = app
        self.title("관리자 로그인")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app.window)

        body = tk.Frame(self, bg=PANEL)
        body.pack(padx=px(36), pady=(px(32), px(28)))

        tk.Label(body, text="관리자 로그인", font=(UI_FONT, 18, "bold"), bg=PANEL, fg=TEXT) \
            .pack(anchor=tk.W, pady=(0, px(20)))

        def rounded_entry(label, show=""):
            tk.Label(body, text=label, font=(UI_FONT, 10, "bold"), bg=PANEL, fg=MUTED) \
                .pack(anchor=tk.W, padx=px(4))
            box = RoundedBox(body, fill="#FFFFFF", bg=PANEL, radius=px(10), pad=px(10), outline="#A39E93",
                             outline_width=2)
            box.pack(fill=tk.X, pady=(px(6), px(14)))
            entry = tk.Entry(box.body, font=(UI_FONT, 12), bd=0, relief=tk.FLAT, bg="white", fg=TEXT,
                             highlightthickness=0, width=10, show=show)
            entry.pack(fill=tk.X, padx=px(4))
            return entry

        self.ent_id = rounded_entry("아이디")
        self.ent_pwd = rounded_entry("비밀번호", show="*")

        # 버튼 두 개의 폭이 입력칸 폭(대화상자 폭)을 정한다
        btn_row = tk.Frame(body, bg=PANEL)
        btn_row.pack(fill=tk.X, pady=(px(8), 0))
        btn = dict(width=px(196), height=px(70), radius=px(12), shadow=px(8), bg=PANEL)
        RoundButton(btn_row, text="취소", command=self.destroy, fill=BLUE_SOFT, hover=BLUE_SOFT_HOVER, fg=BLUE,
                    **btn).pack(side=tk.LEFT, padx=(0, px(10)))
        RoundButton(btn_row, text="로그인", command=self.try_login, **btn).pack(side=tk.RIGHT)

        self.bind("<Return>", lambda e: self.try_login())
        self.bind("<Escape>", lambda e: self.destroy())

        center_on(app.window, self, rel_y=0.33)
        self.ent_id.focus_set()
        # 창이 화면에 뜬 다음에 잡아야 한다 (뜨기 전에 grab_set하면 'window not viewable' 오류가 날 수 있다)
        self.after(50, self._grab)

    def _grab(self):
        try:
            if self.winfo_exists():
                self.grab_set()
        except tk.TclError:
            pass

    def try_login(self):
        user_id = self.ent_id.get().strip()
        pwd = self.ent_pwd.get().strip()

        if not (user_id and pwd):
            messagebox.showwarning("주의", "아이디와 비밀번호를 입력해주세요.", parent=self)
            return

        result = check_admin_login(user_id, pwd)
        if result == "wrong":
            messagebox.showerror("오류", "로그인 정보가 틀렸습니다.", parent=self)
            return
        if result == "not_admin":
            messagebox.showerror("오류", "관리자 권한이 없습니다.", parent=self)
            return

        # 창을 먼저 닫고 나서 화면을 바꾼다 (닫힌 입력칸을 건드리지 않도록)
        self.destroy()
        self.app.set_admin_logged_in(True, user_id)


# ==========================================
# 2-1. 디자인 — 탁한 흰색 + 파란색 두 가지 색만 쓴다
# ==========================================
BG = "#EFEEEA"              # 앱 바탕 (탁한 흰색)
PANEL = "#FAF9F6"           # 카드
PANEL_ALT = "#F2F1ED"       # 카드 안의 보조 영역
LINE = "#DEDCD5"            # 테두리
TEXT = "#1A2233"
TEXT_SUB = "#4A5262"
MUTED = "#8A8F99"
BLUE = "#1F4FBF"
BLUE_DARK = "#173C96"
BLUE_SOFT = "#E3E9F6"
BLUE_SOFT_HOVER = "#D3DDF2"
BLUE_LINE = "#C3D0EC"
CAM_BG = "#111B30"          # 카메라 영상 뒤 배경
# 상태 표시에만 쓰는 색 (카메라 상자 색과 맞춘다)
OK = "#16A34A"
WARN = "#B26A00"
ERR = "#C2362B"
# 관리자 화면도 메인 화면과 같은 탁한 흰색 + 파란색으로 맞춘다
ADMIN_BG = BG               # 관리자 화면 바탕
ADMIN_CARD = PANEL          # 관리자 화면 카드 (메인 화면 카드와 같은 색 · 테두리 · 그림자)
ADMIN_CELL = BLUE_SOFT      # 입력칸 제목 상자 (안내 단계 상자와 같은 연한 파랑)
ADMIN_BORDER = LINE         # 입력칸 테두리
ADMIN_SHADOW = 10           # 카드 그림자 (1600x900 기준 px)

VERIFY_CARD_SIZE = (1330, 730)   # 출근/퇴근/출석 조회 확인 카드 (1600x900 기준)
RESULT_CARD_SIZE = (1060, 690)
STATS_CARD_SIZE = (1160, 740)
TEST_PANEL_WIDTH = 510           # 카메라 테스트 오른쪽 안내 패널 — 카메라가 화면의 약 2/3를 쓴다


def _hex_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


_render_cache = {}


def _cached(key, make):
    """같은 모양을 다시 그리지 않도록 PIL 이미지를 기억해 둔다 (창 크기 변경 때만 새로 그린다)."""
    img = _render_cache.get(key)
    if img is None:
        if len(_render_cache) > 256:
            _render_cache.clear()
        img = _render_cache[key] = make()
    return img


def render_round_rect(w, h, radius, fill, bg=None, outline=None, outline_width=1, shadow=0):
    """
    둥근 사각형 그림. Tk 캔버스는 곡선을 부드럽게(안티에일리어싱) 그리지 못해서
    PIL로 크게 그린 뒤 줄여서 가장자리를 매끄럽게 만든다.
    bg=None이면 모서리 바깥이 투명하다. shadow>0이면 그만큼 바깥 여백에 은은한 그림자를 깐다.
    """
    w, h = max(2, int(w)), max(2, int(h))
    radius = max(0, min(int(radius), (w - 2 * shadow) // 2, (h - 2 * shadow) // 2))
    key = ("rr", w, h, radius, fill, bg, outline, outline_width, shadow)

    def make():
        base = Image.new("RGBA", (w, h), (_hex_rgb(bg) + (255,)) if bg else (0, 0, 0, 0))
        if shadow:
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (shadow, shadow + shadow // 3, w - shadow, h - shadow + shadow // 3), radius, fill=34)
            mask = mask.filter(ImageFilter.GaussianBlur(shadow / 2))
            tint = Image.new("RGBA", (w, h), (23, 37, 84, 255))
            tint.putalpha(mask)
            base = Image.alpha_composite(base, tint)
        s = 3 if w * h < 400_000 else 2
        layer = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
        box = (shadow * s, shadow * s, (w - shadow) * s - 1, (h - shadow) * s - 1)
        ImageDraw.Draw(layer).rounded_rectangle(
            box, radius * s, fill=_hex_rgb(fill) + (255,),
            outline=(_hex_rgb(outline) + (255,)) if outline else None,
            width=outline_width * s if outline else 0)
        layer = layer.resize((w, h), Image.LANCZOS)
        return Image.alpha_composite(base, layer)

    return _cached(key, make)


def render_circle(d, fill):
    def make():
        s = 4
        img = Image.new("RGBA", (d * s, d * s), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, d * s - 1, d * s - 1), fill=_hex_rgb(fill) + (255,))
        return img.resize((d, d), Image.LANCZOS)
    return _cached(("circle", d, fill), make)


def render_icon(kind, size, color):
    """선 아이콘 (출근·퇴근·얼굴·안내·확인·시계·카메라 꺼짐·주의). 글꼴에 없는 기호를 쓰지 않으려고 직접 그린다."""
    def make():
        s = 4
        S = size * s
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rgb = _hex_rgb(color) + (255,)
        lw = max(2, int(S * 0.09))

        def P(x, y):
            return (x * S, y * S)

        def dot(x, y, r):
            d.ellipse((x * S - r * S, y * S - r * S, x * S + r * S, y * S + r * S), fill=rgb)

        def line(*pts):
            pts = [P(*p) for p in pts]
            d.line(pts, fill=rgb, width=lw, joint="curve")
            r = lw / 2
            for x, y in (pts[0], pts[-1]):
                d.ellipse((x - r, y - r, x + r, y + r), fill=rgb)

        def ring():
            m = lw / 2 + S * 0.06
            d.ellipse((m, m, S - m, S - m), outline=rgb, width=lw)

        if kind == "in":
            line((0.55, 0.14), (0.84, 0.14), (0.84, 0.86), (0.55, 0.86))
            line((0.12, 0.5), (0.62, 0.5))
            line((0.44, 0.32), (0.62, 0.5), (0.44, 0.68))
        elif kind == "out":
            line((0.45, 0.14), (0.16, 0.14), (0.16, 0.86), (0.45, 0.86))
            line((0.38, 0.5), (0.88, 0.5))
            line((0.70, 0.32), (0.88, 0.5), (0.70, 0.68))
        elif kind == "face":
            line((0.08, 0.30), (0.08, 0.08), (0.30, 0.08))
            line((0.70, 0.08), (0.92, 0.08), (0.92, 0.30))
            line((0.92, 0.70), (0.92, 0.92), (0.70, 0.92))
            line((0.30, 0.92), (0.08, 0.92), (0.08, 0.70))
            dot(0.36, 0.42, 0.055)
            dot(0.64, 0.42, 0.055)
            d.arc((*P(0.32, 0.40), *P(0.68, 0.72)), 25, 155, fill=rgb, width=lw)
        elif kind == "info":
            ring()
            dot(0.5, 0.31, 0.06)
            line((0.5, 0.48), (0.5, 0.72))
        elif kind == "alert":
            ring()
            line((0.5, 0.28), (0.5, 0.55))
            dot(0.5, 0.71, 0.06)
        elif kind == "check":
            line((0.18, 0.52), (0.42, 0.75), (0.84, 0.28))
        elif kind == "clock":
            ring()
            line((0.5, 0.28), (0.5, 0.5), (0.68, 0.62))
        elif kind == "camera_off":
            line((0.10, 0.30), (0.66, 0.30), (0.66, 0.76), (0.10, 0.76), (0.10, 0.30))
            line((0.66, 0.46), (0.92, 0.32), (0.92, 0.74), (0.66, 0.60))
            line((0.10, 0.10), (0.90, 0.92))
        # 아래 아이콘은 굵은 면 아이콘 (나가기 · 저장 · 새로고침 · 삭제)
        elif kind == "exit":
            lw = int(S * 0.12)
            d.line([P(0.56, 0.10), P(0.08, 0.10), P(0.08, 0.90), P(0.56, 0.90)], fill=rgb, width=lw, joint="curve")
            d.rectangle((*P(0.30, 0.42), *P(0.66, 0.58)), fill=rgb)
            d.polygon([P(0.62, 0.22), P(0.96, 0.50), P(0.62, 0.78)], fill=rgb)
        elif kind == "save":
            d.rounded_rectangle((*P(0.08, 0.08), *P(0.92, 0.92)), S * 0.10, fill=rgb)
            d.polygon([P(0.70, 0.04), P(0.96, 0.30), P(0.96, 0.04)], fill=(0, 0, 0, 0))
            d.rectangle((*P(0.24, 0.18), *P(0.66, 0.38)), fill=(0, 0, 0, 0))
            d.ellipse((*P(0.36, 0.52), *P(0.64, 0.80)), fill=(0, 0, 0, 0))
        elif kind == "refresh":
            lw = int(S * 0.13)
            m = S * 0.16
            box = (m, m, S - m, S - m)
            d.arc(box, 200, 330, fill=rgb, width=lw)
            d.arc(box, 20, 150, fill=rgb, width=lw)
            d.polygon([P(0.97, 0.10), P(0.97, 0.46), P(0.61, 0.46)], fill=rgb)
            d.polygon([P(0.03, 0.90), P(0.03, 0.54), P(0.39, 0.54)], fill=rgb)
        elif kind == "trash":
            d.rectangle((*P(0.10, 0.14), *P(0.90, 0.26)), fill=rgb)
            d.rectangle((*P(0.36, 0.04), *P(0.64, 0.16)), fill=rgb)
            d.polygon([P(0.16, 0.32), P(0.84, 0.32), P(0.78, 0.96), P(0.22, 0.96)], fill=rgb)
            for x in (0.36, 0.5, 0.64):
                d.rectangle((*P(x - 0.035, 0.44), *P(x + 0.035, 0.84)), fill=(0, 0, 0, 0))
        elif kind == "camera":
            d.rounded_rectangle((*P(0.04, 0.24), *P(0.96, 0.88)), S * 0.14, fill=rgb)
            d.rounded_rectangle((*P(0.30, 0.12), *P(0.70, 0.30)), S * 0.06, fill=rgb)
            d.ellipse((*P(0.29, 0.34), *P(0.71, 0.76)), fill=(0, 0, 0, 0))
            d.ellipse((*P(0.37, 0.42), *P(0.63, 0.68)), fill=rgb)
            d.ellipse((*P(0.80, 0.32), *P(0.88, 0.40)), fill=(0, 0, 0, 0))
        elif kind in ("box", "box_checked"):
            d.rounded_rectangle((*P(0.06, 0.06), *P(0.94, 0.94)), S * 0.12, fill=(255, 255, 255, 255),
                                outline=rgb, width=max(2, int(S * 0.08)))
            if kind == "box_checked":
                line((0.24, 0.52), (0.42, 0.70), (0.76, 0.30))
        return img.resize((size, size), Image.LANCZOS)

    return _cached(("icon", kind, size, color), make)


def render_ring(size, width, track, color=None, start=0, extent=360, bg=PANEL):
    """상태 표시용 원형 링. extent<360이면 끝이 둥근 호(돌아가는 로딩 표시)를 그린다."""
    def make():
        s = 3
        S = size * s
        img = Image.new("RGB", (S, S), _hex_rgb(bg))
        d = ImageDraw.Draw(img)
        m = s * 2
        box = (m, m, S - m, S - m)
        d.ellipse(box, outline=_hex_rgb(track), width=width * s)
        if color:
            rgb = _hex_rgb(color)
            if extent >= 360:
                d.ellipse(box, outline=rgb, width=width * s)
            else:
                d.arc(box, start, start + extent, fill=rgb, width=width * s)
                c = S / 2
                r_mid = (S - 2 * m) / 2 - width * s / 2
                cap = width * s / 2
                for a in (start, start + extent):
                    x = c + r_mid * math.cos(math.radians(a))
                    y = c + r_mid * math.sin(math.radians(a))
                    d.ellipse((x - cap, y - cap, x + cap, y + cap), fill=rgb)
        return img.resize((size, size), Image.LANCZOS)

    return _cached(("ring", size, width, track, color, start, extent, bg), make)


def compose_center(base, icon):
    """base 그림 가운데에 icon을 얹는다 (로고 타일 등)."""
    out = base.copy()
    out.alpha_composite(icon, ((base.width - icon.width) // 2, (base.height - icon.height) // 2))
    return out


class RoundedPanel(tk.Canvas):
    """모서리가 둥근 카드 (배치된 크기를 그대로 채운다). 안쪽 내용은 .body(Frame)에 넣는다."""

    def __init__(self, master, fill=PANEL, bg=BG, radius=None, outline=LINE, shadow=0, pad=None,
                 width=None, height=None):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, width=width or 1, height=height or 1)
        self.fill, self._bg, self.outline, self.shadow = fill, bg, outline, shadow
        self.radius = px(16) if radius is None else radius
        self.inset = shadow + (px(20) if pad is None else pad)
        self._photo = None
        self._img_item = self.create_image(0, 0, anchor=tk.NW)
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(self.inset, self.inset, window=self.body, anchor=tk.NW)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        img = render_round_rect(event.width, event.height, self.radius, self.fill, self._bg,
                                self.outline, shadow=self.shadow)
        self._photo = ImageTk.PhotoImage(img)
        self.itemconfig(self._img_item, image=self._photo)
        self.itemconfig(self._win, width=max(1, event.width - 2 * self.inset),
                        height=max(1, event.height - 2 * self.inset))


class RoundedBox(tk.Frame):
    """
    높이가 내용에 맞춰지는 둥근 상자. 일반 pack 배치로 크기가 정해지고,
    둥근 배경 그림은 뒤에 깔린 라벨이 크기에 맞춰 다시 그린다.
    """

    def __init__(self, master, fill=PANEL_ALT, bg=PANEL, radius=None, pad=None, outline=None, outline_width=1):
        super().__init__(master, bg=bg)
        self.fill, self._bg, self.outline, self.outline_width = fill, bg, outline, outline_width
        self.radius = px(12) if radius is None else radius
        self._photo = None
        self._bg_label = tk.Label(self, bg=bg, bd=0)
        self._bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=fill)
        pad = px(16) if pad is None else pad
        self.body.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None):
        w, h = (event.width, event.height) if event else (self.winfo_width(), self.winfo_height())
        if w < 2 or h < 2:
            return
        self._photo = ImageTk.PhotoImage(
            render_round_rect(w, h, self.radius, self.fill, self._bg, self.outline, self.outline_width))
        self._bg_label.config(image=self._photo)

    def set_outline(self, color):
        """테두리 색만 바꿔 다시 그린다 (입력칸을 누르면 파랗게)."""
        if color != self.outline:
            self.outline = color
            self._redraw()


class RoundButton(tk.Canvas):
    """둥근 버튼. command가 없으면 글자 표시용 알약(pill)으로도 쓴다. shadow>0이면 그만큼 안쪽에 그림자와 함께 그린다."""

    def __init__(self, master, text="", command=None, width=None, height=None, font=None, radius=None,
                 fill=BLUE, hover=BLUE_DARK, fg="white", hover_fg=None, outline=None, bg=PANEL,
                 padx=None, icon=None, shadow=0):
        self._shadow = shadow
        self._font = font or (UI_FONT, 12, "bold")
        self._padx = px(22) if padx is None else padx
        self._icon = ImageTk.PhotoImage(icon) if icon is not None else None
        self._fixed_width = width
        height = height or px(48)
        self._bw, self._bh = (width or self._measure(text)), height
        super().__init__(master, width=self._bw, height=height, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2" if command else "")
        self._bg = bg
        self.radius = px(10) if radius is None else radius
        self.command = command
        self._hovering = False
        self._style = None
        self._img_item = self.create_image(0, 0, anchor=tk.NW)
        self._icon_item = self.create_image(0, 0, anchor=tk.W)
        self._text_item = self.create_text(0, 0, text=text, font=self._font, anchor=tk.W)
        self._text = text
        self.set_style(fill, hover, fg, hover_fg, outline)
        if command:
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<ButtonRelease-1>", self._on_click)

    def _measure(self, text):
        width = tkfont.Font(font=self._font).measure(text) + 2 * self._padx
        if self._icon is not None:
            width += self._icon.width() + px(10)
        return width

    def _layout(self):
        text_w = tkfont.Font(font=self._font).measure(self._text)
        icon_w = self._icon.width() + px(10) if self._icon is not None else 0
        x = (self._bw - text_w - icon_w) // 2
        if self._icon is not None:
            self.itemconfig(self._icon_item, image=self._icon)
            self.coords(self._icon_item, x, self._bh // 2)
        self.coords(self._text_item, x + icon_w, self._bh // 2)

    def set_style(self, fill, hover=None, fg="white", hover_fg=None, outline=None):
        style = (fill, hover, fg, hover_fg, outline, self._bw)
        if style == self._style:
            return
        self._style = style
        self._fg, self._hover_fg = fg, hover_fg or fg
        self._img_normal = ImageTk.PhotoImage(
            render_round_rect(self._bw, self._bh, self.radius, fill, self._bg, outline, shadow=self._shadow))
        self._img_hover = ImageTk.PhotoImage(
            render_round_rect(self._bw, self._bh, self.radius, hover or fill, self._bg, outline,
                              shadow=self._shadow))
        self._apply()
        self._layout()

    def _apply(self):
        self.itemconfig(self._img_item, image=self._img_hover if self._hovering else self._img_normal)
        self.itemconfig(self._text_item, fill=self._hover_fg if self._hovering else self._fg)

    def set_text(self, text):
        if text == self._text:
            return
        self._text = text
        self.itemconfig(self._text_item, text=text)
        if self._fixed_width is None:
            new_w = self._measure(text)
            if new_w != self._bw:
                self._bw = new_w
                self.configure(width=new_w)
                fill, hover, fg, hover_fg, outline, _ = self._style
                self._style = None
                self.set_style(fill, hover, fg, hover_fg, outline)
                return
        self._layout()

    def _on_enter(self, _event=None):
        self._hovering = True
        self._apply()

    def _on_leave(self, _event=None):
        self._hovering = False
        self._apply()

    def _on_click(self, event):
        if 0 <= event.x < self._bw and 0 <= event.y < self._bh and self.command:
            self.command()


class ProgressBar(tk.Canvas):
    """얇은 둥근 진행 막대. marker를 주면 그 위치(0~1)에 기준선을 긋는다."""

    def __init__(self, master, width, height, bg=PANEL, track=BLUE_SOFT, color=BLUE):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self._bw, self._bh, self._bg, self._track, self._color = width, height, bg, track, color
        self._key = None
        self._item = self.create_image(0, 0, anchor=tk.NW)

    def set(self, fraction, color=None, marker=None):
        fraction = min(1.0, max(0.0, fraction))
        color = color or self._color
        key = (round(fraction * self._bw), color, marker)
        if key == self._key:
            return
        self._key = key
        w, h, s = self._bw, self._bh, 3
        img = Image.new("RGB", (w * s, h * s), _hex_rgb(self._bg))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w * s - 1, h * s - 1), h * s // 2, fill=_hex_rgb(self._track))
        if fraction > 0:
            d.rounded_rectangle((0, 0, max(h * s, int(w * s * fraction)) - 1, h * s - 1), h * s // 2,
                                fill=_hex_rgb(color))
        if marker is not None:
            mx = int(w * s * marker)
            d.rectangle((mx - s, 0, mx + s, h * s), fill=_hex_rgb(TEXT))
        self._photo = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        self.itemconfig(self._item, image=self._photo)


def make_pill(master, text, bg=PANEL, fill=BLUE_SOFT, fg=BLUE, font_size=10):
    """상태를 알려주는 작은 알약 모양 표시."""
    h = px(34)
    return RoundButton(master, text=text, height=h, radius=h // 2, fill=fill, fg=fg, bg=bg,
                       font=(UI_FONT, font_size, "bold"), padx=px(16))


def icon_label(master, kind, size, color, bg):
    photo = ImageTk.PhotoImage(render_icon(kind, size, color))
    lbl = tk.Label(master, image=photo, bg=bg, bd=0)
    lbl.image = photo  # 참조를 들고 있어야 그림이 사라지지 않는다
    return lbl


def icon_button(master, kind, size, command, bg, color=None, tile=None, tile_fill=BLUE_SOFT):
    """누를 수 있는 아이콘. tile을 주면 그 크기의 둥근 연한 파랑 타일 위에 파란 아이콘으로 얹는다 (관리자 도구 버튼)."""
    image = render_icon(kind, size, color or (BLUE if tile else TEXT))
    if tile:
        image = compose_center(render_round_rect(tile, tile, px(12), tile_fill, bg), image)
    photo = ImageTk.PhotoImage(image)
    lbl = tk.Label(master, image=photo, bg=bg, bd=0, cursor="hand2")
    lbl.image = photo
    lbl.bind("<ButtonRelease-1>", lambda e: command()
             if 0 <= e.x < lbl.winfo_width() and 0 <= e.y < lbl.winfo_height() else None)
    return lbl


def exit_button(master, command, size=None, bg=PANEL):
    """카드 오른쪽 위 [나가기] 아이콘 — 누르면 메인 화면으로."""
    return icon_button(master, "exit", size or px(52), command, bg)


def back_link(master, command, bg=PANEL, font_size=16):
    """카드 아래쪽 '메인으로 (N초뒤)' 글자. 눌러도 바로 메인 화면으로 간다."""
    lbl = tk.Label(master, text="", font=(UI_FONT, font_size), bg=bg, fg=BLUE, cursor="hand2")
    lbl.bind("<ButtonRelease-1>", lambda e: command())
    return lbl


_blink_hint_frames_cache = {}


def _get_blink_hint_frames(size):
    """'눈을 깜빡여 주세요' 안내에 같이 보여줄 힌트 이미지를 size(px) 정사각형으로 불러온다.
    움직이는 GIF/WEBP면 프레임 전체를 (PhotoImage, 이 프레임을 보여줄 시간ms) 목록으로 돌려준다.
    정지 이미지면 프레임이 하나뿐인 목록. 파일이 없거나 못 읽으면 빈 목록(이미지 없이 문구만 보인다)."""
    frames = _blink_hint_frames_cache.get(size)
    if frames is not None:
        return frames
    frames = []
    if os.path.exists(BLINK_HINT_IMAGE_PATH):
        try:
            # 네모난 이미지의 모서리가 바깥 링 위로 튀어나오지 않게 동그랗게 잘라낸다 (가장자리는 부드럽게)
            s = 4
            mask = Image.new("L", (size * s, size * s), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size * s - 1, size * s - 1), fill=255)
            mask = mask.resize((size, size), Image.LANCZOS)
            src = Image.open(BLINK_HINT_IMAGE_PATH)
            for frame in ImageSequence.Iterator(src):
                duration = frame.info.get("duration", 100) or 100
                resized = frame.convert("RGBA").resize((size, size), Image.LANCZOS)
                alpha = Image.composite(resized.getchannel("A"), Image.new("L", (size, size), 0), mask)
                resized.putalpha(alpha)
                frames.append((ImageTk.PhotoImage(resized), duration))
        except Exception as e:
            print(f"[출석] 눈 깜빡임 안내 이미지를 불러오지 못했습니다 ({e})")
            frames = []
    _blink_hint_frames_cache[size] = frames
    return frames


class StatusVisual(tk.Canvas):
    """
    얼굴 확인 상태를 보여주는 원형 표시.
      search : 링이 돌아간다 (얼굴을 찾는 중)
      face   : 학생을 찾았을 때 — 눈을 깜빡이는 얼굴 GIF
      done / warn / error : 완료 체크 · 주의 표시
    """
    SPIN_FRAMES = 30

    def __init__(self, master, size, bg=PANEL):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size = size
        ring_w = max(4, size // 30)
        c = size // 2
        self._spin = [ImageTk.PhotoImage(render_ring(size, ring_w, BLUE_SOFT, BLUE,
                                                     start=i * 360 // self.SPIN_FRAMES - 90, extent=100, bg=bg))
                      for i in range(self.SPIN_FRAMES)]
        self._rings = {
            "track": ImageTk.PhotoImage(render_ring(size, ring_w, BLUE_SOFT, bg=bg)),
            "blue": ImageTk.PhotoImage(render_ring(size, ring_w, BLUE_SOFT, BLUE, bg=bg)),
            "warn": ImageTk.PhotoImage(render_ring(size, ring_w, BLUE_SOFT, WARN, bg=bg)),
            "error": ImageTk.PhotoImage(render_ring(size, ring_w, BLUE_SOFT, ERR, bg=bg)),
        }
        icon_size = int(size * 0.4)
        self._icons = {
            "search": ImageTk.PhotoImage(render_icon("face", icon_size, BLUE_LINE)),
            "done": ImageTk.PhotoImage(render_icon("check", icon_size, BLUE)),
            "warn": ImageTk.PhotoImage(render_icon("alert", icon_size, WARN)),
            "error": ImageTk.PhotoImage(render_icon("alert", icon_size, ERR)),
        }
        self._gif = _get_blink_hint_frames(int(size * 0.68))
        self._ring_item = self.create_image(c, c)
        self._center_item = self.create_image(c, c)
        self._mode = None
        self._job = None
        self._idx = 0
        self.bind("<Destroy>", lambda e: self._cancel() if e.widget is self else None)

    def _cancel(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        self._cancel()
        self._idx = 0
        if mode == "search":
            self.itemconfig(self._center_item, image=self._icons["search"])
            self._spin_step()
        elif mode == "face":
            self.itemconfig(self._ring_item, image=self._rings["blue"])
            if self._gif:
                self._gif_step()
            else:
                self.itemconfig(self._center_item, image=self._icons["search"])
        else:
            ring = {"done": "blue", "warn": "warn", "error": "error"}.get(mode, "track")
            self.itemconfig(self._ring_item, image=self._rings[ring])
            self.itemconfig(self._center_item, image=self._icons.get(mode, ""))

    def _spin_step(self):
        self.itemconfig(self._ring_item, image=self._spin[self._idx])
        self._idx = (self._idx + 1) % self.SPIN_FRAMES
        self._job = self.after(33, self._spin_step)

    def _gif_step(self):
        photo, duration = self._gif[self._idx]
        self.itemconfig(self._center_item, image=photo)
        self._idx = (self._idx + 1) % len(self._gif)
        if len(self._gif) > 1:
            self._job = self.after(duration, self._gif_step)


class StepBox(tk.Canvas):
    """안내 단계 상자 (번호 · 안내 멘트). 상태: idle / active(지금 할 일) / done(완료)."""

    def __init__(self, master, number, title, width, height, bg=PANEL):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self._state = None
        r = px(14)
        self._boxes = {
            "idle": ImageTk.PhotoImage(render_round_rect(width, height, r, PANEL_ALT, bg)),
            "active": ImageTk.PhotoImage(render_round_rect(width, height, r, BLUE_SOFT, bg, BLUE_LINE)),
            "done": ImageTk.PhotoImage(render_round_rect(width, height, r, PANEL_ALT, bg)),
        }
        d = px(44)
        self._circles = {
            "idle": ImageTk.PhotoImage(render_circle(d, BLUE_SOFT)),
            "active": ImageTk.PhotoImage(render_circle(d, BLUE)),
            "done": ImageTk.PhotoImage(compose_center(render_circle(d, BLUE),
                                                      render_icon("check", int(d * 0.55), "#FFFFFF"))),
        }
        left = px(18)
        cx, cy = left + d // 2, height // 2
        text_x = left + d + px(14)
        self._box_item = self.create_image(0, 0, anchor=tk.NW)
        self._circle_item = self.create_image(cx, cy)
        self._num_item = self.create_text(cx, cy, text=str(number), font=(UI_FONT, 13, "bold"))
        self._title_item = self.create_text(text_x, cy, text=title, anchor=tk.W,
                                            font=(UI_FONT, 13, "bold"), width=width - text_x - px(12))
        self.set_state("idle")

    def set_state(self, state):
        if state == self._state:
            return
        self._state = state
        self.itemconfig(self._box_item, image=self._boxes[state])
        self.itemconfig(self._circle_item, image=self._circles[state])
        self.itemconfig(self._num_item, state=tk.HIDDEN if state == "done" else tk.NORMAL,
                        fill=BLUE if state == "idle" else "white")
        self.itemconfig(self._title_item, fill={"idle": TEXT_SUB, "active": BLUE_DARK, "done": TEXT}[state])


class ActionCard(tk.Canvas):
    """메인 화면의 큰 출근/퇴근 버튼 카드."""

    def __init__(self, master, log_type, command, width, height, bg=BG, scale=1.0):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        primary = log_type == "CHECK_IN"
        # 카드 안 아이콘·글자·여백은 470x340 카드 기준 값이다. 카드를 키우면 scale만큼 같이 키운다
        def s(value):
            return px(value * scale)

        def pt(size):
            return max(1, int(round(size * scale)))

        sh = s(12)
        r = s(22)
        if primary:
            fill, hover, outline, hover_outline = BLUE, BLUE_DARK, None, None
            title_fg, accent, tile_fill, icon_color, sep = "white", "#BFD0F5", "#3A64C9", "#FFFFFF", "#3D66CC"
        else:
            fill, hover, outline, hover_outline = PANEL, "#F3F5FA", BLUE_LINE, BLUE
            title_fg, accent, tile_fill, icon_color, sep = TEXT, BLUE, BLUE_SOFT, BLUE, LINE
        self._normal = ImageTk.PhotoImage(render_round_rect(width, height, r, fill, bg, outline, shadow=sh))
        self._hover = ImageTk.PhotoImage(render_round_rect(width, height, r, hover, bg, hover_outline,
                                                           outline_width=2, shadow=sh))
        tile = s(64)
        self._tile = ImageTk.PhotoImage(compose_center(
            render_round_rect(tile, tile, s(16), tile_fill),
            render_icon("in" if primary else "out", s(32), icon_color)))

        x0 = sh + s(32)
        y0 = sh + s(30)
        action = ACTION_LABELS[log_type]
        self._bg_item = self.create_image(0, 0, image=self._normal, anchor=tk.NW)
        self.create_image(x0, y0, image=self._tile, anchor=tk.NW)
        self.create_text(width - x0, y0 + s(4), text="CHECK-IN" if primary else "CHECK-OUT",
                         font=(UI_FONT, pt(10), "bold"), fill=accent, anchor=tk.NE)
        self.create_text(x0, y0 + s(96), text=action, font=(UI_FONT, pt(40), "bold"), fill=title_fg, anchor=tk.NW)
        line_y = height - sh - s(62)
        self.create_line(x0, line_y, width - x0, line_y, fill=sep)
        self.create_text(x0, height - sh - s(31), text=f"{action} 시작",
                         font=(UI_FONT, pt(12), "bold"), fill=accent if not primary else "white", anchor=tk.W)
        self.create_text(width - x0, height - sh - s(33), text="→", font=(UI_FONT, pt(18), "bold"),
                         fill=accent if not primary else "white", anchor=tk.E)

        self.bind("<Enter>", lambda e: self.itemconfig(self._bg_item, image=self._hover))
        self.bind("<Leave>", lambda e: self.itemconfig(self._bg_item, image=self._normal))
        self.bind("<ButtonRelease-1>", self._on_click)

    def _on_click(self, event):
        if 0 <= event.x < int(self.cget("width")) and 0 <= event.y < int(self.cget("height")):
            self.command()


class VerifyCard(RoundedPanel):
    """
    출근 / 퇴근 / 출석 조회 버튼을 누른 뒤 얼굴이 확인될 때까지 보여주는 화면.
    카메라 영상은 띄우지 않는다 — 카메라는 뒤에서 얼굴 확인에만 쓴다.
    """
    STEPS = {
        "loading": ("active", "idle", "idle"),
        "search": ("active", "idle", "idle"),
        "warn": ("active", "idle", "idle"),
        "error": ("active", "idle", "idle"),
        "blink": ("done", "active", "idle"),
        "done": ("done", "done", "done"),
    }
    VISUAL = {"loading": "search", "search": "search", "blink": "face",
              "warn": "warn", "error": "error", "done": "done"}
    TITLE_COLOR = {"loading": TEXT_SUB, "search": TEXT, "blink": BLUE,
                   "warn": WARN, "error": ERR, "done": BLUE}

    def __init__(self, app, kind, master, width, height):
        super().__init__(master, fill=PANEL, bg=BG, radius=px(24), outline=LINE, shadow=px(16), pad=px(40))
        self._limit = timeouts["verify"]
        self._last = None
        b = self.body
        inner_w = width - 2 * self.inset

        if kind == "LOOKUP":
            title = "출석 조회"
            step3 = "확인 되면 출석 현황이 표시 됩니다."
        else:
            action = ACTION_LABELS[kind]
            title = action
            step3 = f"확인 되면 자동으로 {action} 처리 됩니다."

        # 1) 제목 · 오른쪽 위 나가기
        tk.Label(b, text=title, font=(UI_FONT, 32, "bold"), bg=PANEL, fg=TEXT).pack(anchor=tk.W, pady=(0, px(22)))
        exit_button(b, app.show_idle).place(relx=1.0, y=0, anchor=tk.NE)

        # 2) 안내 멘트 — 지금 해야 할 단계를 파랗게 강조한다
        steps_row = tk.Frame(b, bg=PANEL)
        steps_row.pack(fill=tk.X)
        gap = px(16)
        box_w = (inner_w - 2 * gap) // 3
        self.steps = []
        for i, main in enumerate(("카메라를 정면으로 바라봐 주세요", "눈을 천천히 감았다 떠 주세요", step3)):
            box = StepBox(steps_row, i + 1, main, box_w, px(88))
            box.pack(side=tk.LEFT, padx=(0 if i == 0 else gap, 0))
            self.steps.append(box)

        # 4) 하단 — 남은 시간 (내용보다 먼저 배치해야 공간이 모자라도 잘리지 않는다)
        timer = tk.Frame(b, bg=PANEL)
        timer.pack(side=tk.BOTTOM, anchor=tk.W)
        self.lbl_remaining = tk.Label(timer, text="", font=(UI_FONT, 10), bg=PANEL, fg=MUTED)
        self.lbl_remaining.pack(anchor=tk.W, pady=(0, px(6)))
        self.timer_bar = ProgressBar(timer, px(440), px(6))
        self.timer_bar.pack(anchor=tk.W)

        # 3) 얼굴 확인 상태 — "얼굴을 찾는 중.." / "OOO 학생 얼굴 확인 중" + 눈 깜빡이는 얼굴
        status = tk.Frame(b, bg=PANEL)
        status.pack(expand=True)
        self.visual = StatusVisual(status, px(250))
        self.visual.pack()
        self.lbl_title = tk.Label(status, text="", font=(UI_FONT, 26, "bold"), bg=PANEL, fg=TEXT)
        self.lbl_title.pack(pady=(px(16), px(2)))
        self.lbl_detail = tk.Label(status, text="", font=(UI_FONT, 12), bg=PANEL, fg=TEXT_SUB,
                                   justify=tk.CENTER)
        self.lbl_detail.pack()

        self.set_state("search", "얼굴을 찾는 중..")

    def set_state(self, state, title, detail="", sim=None):
        # 화면이 매 프레임 부르므로 내용이 바뀔 때만 다시 그린다
        if (state, title, detail) == self._last:
            return
        self._last = (state, title, detail)
        self.visual.set_mode(self.VISUAL[state])
        self.lbl_title.config(text=title, fg=self.TITLE_COLOR[state])
        self.lbl_detail.config(text=detail)
        for box, step_state in zip(self.steps, self.STEPS[state]):
            box.set_state(step_state)

    def set_remaining(self, seconds):
        text = f"{seconds}초"
        if self.lbl_remaining.cget("text") != text:
            self.lbl_remaining.config(text=text)
        self.timer_bar.set(seconds / self._limit)


class ResultCard(RoundedPanel):
    """출근/퇴근 처리 결과 — timeouts["result"]초 뒤 저절로 메인 화면으로 돌아간다."""

    def __init__(self, app, student, log_type, success, detail, master):
        super().__init__(master, fill=PANEL, bg=BG, radius=px(24), outline=LINE, shadow=px(16), pad=px(40))
        self.app = app
        b = self.body
        action = ACTION_LABELS[log_type]
        name = student["name"]

        exit_button(b, app.show_idle).place(relx=1.0, y=0, anchor=tk.NE)
        self.lbl_back = back_link(b, app.show_idle)
        self.lbl_back.pack(side=tk.BOTTOM, pady=(0, px(6)))

        content = tk.Frame(b, bg=PANEL)
        content.pack(expand=True)
        visual = StatusVisual(content, px(210))
        visual.pack(pady=(0, px(26)))
        visual.set_mode("done" if success else "warn")
        headline = f"{name} 학생 {action} 되었습니다." if success else f"{name} 학생 {action} 처리가 되지 않았습니다."
        tk.Label(content, text=headline, font=(UI_FONT, 28, "bold"), bg=PANEL, fg=TEXT).pack()
        tk.Label(content, text=detail, font=(UI_FONT, 15), bg=PANEL, fg=TEXT_SUB, justify=tk.CENTER) \
            .pack(pady=(px(10), 0))

        self._remaining = timeouts["result"]
        self._tick()

    def _tick(self):
        if not self.winfo_exists():
            return
        if self._remaining <= 0:
            self.app.show_idle()
            return
        self.lbl_back.config(text=f"메인으로 ({self._remaining}초뒤)")
        self._remaining -= 1
        self.after(1000, self._tick)


class StatsCard(RoundedPanel):
    """본인 확인 후 보여주는 이번 주 출석 현황. timeouts["stats"]초 뒤 저절로 메인 화면으로 돌아간다."""
    MAX_ROWS = 6

    def __init__(self, app, student_info, master, width):
        super().__init__(master, fill=PANEL, bg=BG, radius=px(24), outline=LINE, shadow=px(16), pad=px(36))
        self.app = app
        self.student_info = student_info
        b = self.body
        inner_w = width - 2 * self.inset
        weekly = get_weekly_attendance_stats(student_info["user_id"])

        # 상단 — 이름 · 학번 · 자동 닫힘
        top = tk.Frame(b, bg=PANEL)
        top.pack(fill=tk.X)
        name_box = tk.Frame(top, bg=PANEL)
        name_box.pack(side=tk.LEFT)
        tk.Label(name_box, text=f"{student_info['name']} 학생의 이번 주 출석 현황", font=(UI_FONT, 22, "bold"),
                 bg=PANEL, fg=TEXT).pack(anchor=tk.W)
        tk.Label(name_box, text=f"학번 {student_info['user_id']}",
                 font=(UI_FONT, 10), bg=PANEL, fg=MUTED).pack(anchor=tk.W, pady=(px(4), 0))
        exit_button(top, app.show_idle).pack(side=tk.RIGHT, anchor=tk.N)
        # 맨 아래 '메인으로 (N초뒤)' — 표보다 먼저 배치해야 공간이 모자라도 잘리지 않는다
        self.lbl_back = back_link(b, app.show_idle, font_size=13)
        self.lbl_back.pack(side=tk.BOTTOM, pady=(px(8), 0))

        # 지표 4개
        tiles = tk.Frame(b, bg=PANEL)
        tiles.pack(fill=tk.X, pady=(px(22), px(18)))
        gap = px(14)
        tile_w = (inner_w - 3 * gap) // 4

        def tile(index, label, value, note, note_color):
            panel = RoundedPanel(tiles, fill=PANEL_ALT, bg=PANEL, radius=px(16), outline=None, pad=px(18),
                                 width=tile_w, height=px(122))
            panel.pack(side=tk.LEFT, padx=(0 if index == 0 else gap, 0))
            tk.Label(panel.body, text=label, font=(UI_FONT, 10, "bold"), bg=PANEL_ALT, fg=MUTED).pack(anchor=tk.W)
            lbl_value = tk.Label(panel.body, text=value, font=(UI_FONT, 17, "bold"), bg=PANEL_ALT, fg=TEXT)
            lbl_value.pack(anchor=tk.W, pady=(px(6), px(2)))
            lbl_note = tk.Label(panel.body, text=note, font=(UI_FONT, 10, "bold"), bg=PANEL_ALT, fg=note_color)
            lbl_note.pack(anchor=tk.W)
            return lbl_value, lbl_note

        def missing(text):
            return (f"부족 {text}", ERR) if text else ("기준 충족", BLUE)

        tile(0, f"이번 주 출근 일수 (기준 {WEEKLY_REQUIRED_DAYS}일)", f"{weekly['days']}일",
             *missing(f"{weekly['missing_days']}일" if weekly["missing_days"] else ""))
        tile(1, f"이번 주 출근 시간 (기준 {WEEKLY_REQUIRED_SECONDS // 3600}시간)", format_duration(weekly["seconds"]),
             *missing(format_duration(weekly["missing_seconds"]) if weekly["missing_seconds"] else ""))
        self.lbl_today_value, self.lbl_today_note = tile(2, "오늘 출근", "", "", MUTED)
        penalty = student_info.get("penalty") or 0
        tile(3, "패널티", f"{penalty}개",
             "양호" if penalty == 0 else "주의" if penalty == 1 else "경고 (3진 아웃제)",
             BLUE if penalty == 0 else WARN if penalty == 1 else ERR)

        # 이번 주 출퇴근 기록 표
        table = RoundedPanel(b, fill=PANEL, bg=PANEL, radius=px(16), outline=LINE, pad=px(22))
        table.pack(fill=tk.BOTH, expand=True)
        t = table.body
        tk.Label(t, text="이번 주 출퇴근 기록", font=(UI_FONT, 12, "bold"), bg=PANEL, fg=TEXT) \
            .grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, px(10)))
        for col in range(4):
            t.grid_columnconfigure(col, weight=1, uniform="cols")
        for col, heading in enumerate(("날짜", "출근", "퇴근", "인정 시간")):
            tk.Label(t, text=heading, font=(UI_FONT, 9, "bold"), bg=PANEL_ALT, fg=MUTED, anchor=tk.W,
                     padx=px(12), pady=px(7)).grid(row=1, column=col, sticky=tk.EW)
        sessions = weekly["sessions"]
        if not sessions:
            tk.Label(t, text="이번 주 기록이 없습니다", font=(UI_FONT, 11), bg=PANEL, fg=MUTED) \
                .grid(row=2, column=0, columnspan=4, sticky=tk.W, padx=px(12), pady=px(14))
        shown = sessions[-self.MAX_ROWS:]
        for row, s in enumerate(shown):
            try:
                day_text = format_date_ko(datetime.strptime(s["date"], "%Y-%m-%d").date())[5:]
            except (TypeError, ValueError):
                day_text = s["date"]
            seconds = session_seconds(s)
            cells = (day_text, s["check_in"] or "-", s["check_out"] or "-",
                     format_duration(seconds) if seconds else "-")
            for col, value in enumerate(cells):
                tk.Label(t, text=value, font=(UI_FONT, 11, "bold" if col == 3 else "normal"), bg=PANEL,
                         fg=BLUE if (col == 3 and seconds) else TEXT, anchor=tk.W, padx=px(12), pady=px(6)) \
                    .grid(row=2 + row * 2, column=col, sticky=tk.EW)
            tk.Frame(t, bg=PANEL_ALT, height=1).grid(row=3 + row * 2, column=0, columnspan=4, sticky=tk.EW)
        if len(sessions) > len(shown):
            tk.Label(t, text=f"이전 기록 {len(sessions) - len(shown)}건 더 있음", font=(UI_FONT, 9), bg=PANEL,
                     fg=MUTED).grid(row=3 + len(shown) * 2, column=0, columnspan=4, sticky=tk.W,
                                    padx=px(12), pady=(px(6), 0))

        self._refresh_today_status()
        self._remaining = timeouts["stats"]
        self._tick()

    def _tick(self):
        if not self.winfo_exists():
            return
        if self._remaining <= 0:
            self.app.show_idle()
            return
        self.lbl_back.config(text=f"메인으로 ({self._remaining}초뒤)")
        self._remaining -= 1
        self.after(1000, self._tick)

    def _refresh_today_status(self):
        """오늘 출근 시간을 다시 계산해서 표시한다. 출근 중이면 1초 뒤에 다시 갱신한다."""
        if not self.winfo_exists():
            return
        status = get_today_attendance_status(self.student_info["user_id"])
        total_seconds = status["finished_seconds"] + status["working_seconds"]
        self.lbl_today_value.config(text=format_duration(total_seconds))
        if status["working_since"]:
            self.lbl_today_note.config(text=f"출근 중 · {status['working_since']} 출근", fg=BLUE)
            self.after(1000, self._refresh_today_status)
        elif total_seconds > 0:
            self.lbl_today_note.config(text="퇴근 상태", fg=MUTED)
        else:
            self.lbl_today_note.config(text="오늘 출근 기록 없음", fg=MUTED)


class CameraTestCard(RoundedPanel):
    """카메라 테스트 오른쪽 안내 패널 — 출퇴근 기록 없이 카메라와 얼굴 인식만 확인한다."""
    TITLE_COLOR = {"loading": TEXT_SUB, "search": TEXT, "ok": OK, "unknown": ERR, "error": ERR}

    def __init__(self, app, master, width):
        super().__init__(master, fill=PANEL, bg=BG, radius=px(20), outline=LINE, shadow=px(10), pad=px(28))
        self._last = None
        b = self.body
        inner_w = width - 2 * self.inset

        tk.Label(b, text="카메라 테스트", font=(UI_FONT, 22, "bold"), bg=PANEL, fg=TEXT) \
            .pack(anchor=tk.W, pady=(0, px(18)))
        exit_button(b, app.show_idle, px(40)).place(relx=1.0, y=0, anchor=tk.NE)

        notice = RoundedBox(b, fill=BLUE_SOFT, bg=PANEL, pad=px(14))
        notice.pack(fill=tk.X)
        icon_label(notice.body, "info", px(20), BLUE, BLUE_SOFT).pack(side=tk.LEFT, anchor=tk.N, pady=px(1))
        tk.Label(notice.body, text="카메라 테스트에서는 출퇴근이 기록되지 않습니다.", font=(UI_FONT, 11, "bold"),
                 bg=BLUE_SOFT, fg=BLUE, justify=tk.LEFT, wraplength=inner_w - px(70)) \
            .pack(side=tk.LEFT, padx=(px(10), 0))

        # 하단 — 남은 시간 (먼저 배치해야 공간이 모자라도 잘리지 않는다)
        timer = RoundedBox(b, fill=PANEL_ALT, bg=PANEL, pad=px(16))
        timer.pack(side=tk.BOTTOM, fill=tk.X)
        row = tk.Frame(timer.body, bg=PANEL_ALT)
        row.pack(fill=tk.X)
        icon_label(row, "clock", px(18), TEXT_SUB, PANEL_ALT).pack(side=tk.LEFT, anchor=tk.N, pady=px(1))
        tk.Label(row, text=f"{TEST_TIMEOUT}초 동안 켜지며, 자동으로 꺼집니다.",
                 font=(UI_FONT, 10), bg=PANEL_ALT, fg=TEXT_SUB, justify=tk.LEFT,
                 wraplength=inner_w - px(70)).pack(side=tk.LEFT, padx=(px(8), 0))
        bar_row = tk.Frame(timer.body, bg=PANEL_ALT)
        bar_row.pack(fill=tk.X, pady=(px(10), 0))
        self.lbl_remaining = tk.Label(bar_row, text="", font=(UI_FONT, 10, "bold"), bg=PANEL_ALT, fg=BLUE)
        self.lbl_remaining.pack(side=tk.RIGHT)
        self.timer_bar = ProgressBar(bar_row, inner_w - 2 * px(16) - px(100), px(6), bg=PANEL_ALT,
                                     track=BLUE_SOFT_HOVER)
        self.timer_bar.pack(side=tk.LEFT)

        # 인식 결과
        tk.Label(b, text="인식 결과", font=(UI_FONT, 10, "bold"), bg=PANEL, fg=MUTED) \
            .pack(anchor=tk.W, pady=(px(20), px(8)))
        result = RoundedBox(b, fill=PANEL_ALT, bg=PANEL, radius=px(14), pad=px(18))
        result.pack(fill=tk.X)
        self.lbl_title = tk.Label(result.body, text="", font=(UI_FONT, 18, "bold"), bg=PANEL_ALT, fg=TEXT,
                                  anchor=tk.W, justify=tk.LEFT, wraplength=inner_w - px(40))
        self.lbl_title.pack(fill=tk.X)
        self.lbl_detail = tk.Label(result.body, text="", font=(UI_FONT, 10), bg=PANEL_ALT, fg=TEXT_SUB,
                                   anchor=tk.W, justify=tk.LEFT, wraplength=inner_w - px(40))
        self.lbl_detail.pack(fill=tk.X, pady=(px(4), px(12)))
        self.sim_bar = ProgressBar(result.body, inner_w - 2 * px(18), px(8), bg=PANEL_ALT, track=BLUE_SOFT_HOVER)
        self.sim_bar.pack(anchor=tk.W)

        self.set_state("loading", "카메라를 켜는 중..")

    def set_state(self, state, title, detail="", sim=None):
        key = (state, title, detail)
        if key != self._last:
            self._last = key
            self.lbl_title.config(text=title, fg=self.TITLE_COLOR.get(state, TEXT))
            self.lbl_detail.config(text=detail)
        if sim is None:
            self.sim_bar.set(0)
        else:
            self.sim_bar.set(sim, OK if state == "ok" else ERR, marker=THRESHOLD)

    def set_remaining(self, seconds):
        text = f"남은 시간 {seconds}초"
        if self.lbl_remaining.cget("text") != text:
            self.lbl_remaining.config(text=text)
        self.timer_bar.set(seconds / TEST_TIMEOUT)


def admin_card(master, width=None, pad=26):
    """관리자 화면 카드 — 메인 화면 카드와 같은 모양 (탁한 흰색 · 얇은 테두리 · 은은한 그림자)."""
    return RoundedPanel(master, fill=ADMIN_CARD, bg=ADMIN_BG, radius=px(20), outline=LINE,
                        shadow=px(ADMIN_SHADOW), pad=px(pad), width=width)


def admin_field_label(master, text):
    """입력칸 위에 붙는 작은 제목."""
    return tk.Label(master, text=text, bg=ADMIN_CARD, fg=TEXT_SUB, font=(UI_FONT, 10, "bold"), anchor=tk.W)


def admin_card_title(master, title, subtitle=""):
    """카드 제목 + 그 아래 흐린 설명 한 줄. 설명 라벨을 돌려준다 (나중에 글자를 바꿀 수 있게)."""
    box = tk.Frame(master, bg=ADMIN_CARD)
    tk.Label(box, text=title, font=(UI_FONT, 16, "bold"), bg=ADMIN_CARD, fg=TEXT).pack(anchor=tk.W)
    sub = tk.Label(box, text=subtitle, font=(UI_FONT, 10), bg=ADMIN_CARD, fg=MUTED, anchor=tk.W)
    sub.pack(anchor=tk.W, pady=(px(2), 0))
    return box, sub


class AdminField(RoundedBox):
    """관리자 화면 둥근 입력칸. 누르면 테두리가 파랗게 바뀐다. 글자는 .entry로 다룬다."""

    def __init__(self, master, font_size=12, readonly=False, bg=ADMIN_CARD):
        fill = PANEL_ALT if readonly else "#FFFFFF"
        super().__init__(master, fill=fill, bg=bg, radius=px(10), pad=px(8), outline=ADMIN_BORDER)
        self.entry = tk.Entry(self.body, bg=fill, readonlybackground=fill, fg=TEXT, relief=tk.FLAT, bd=0,
                              highlightthickness=0, width=6, font=(UI_FONT, font_size), insertbackground=TEXT)
        self.entry.pack(fill=tk.X, padx=px(6))
        if readonly:
            self.entry.config(state="readonly")
        else:
            self.entry.bind("<FocusIn>", lambda e: self.set_outline(BLUE), add="+")
            self.entry.bind("<FocusOut>", lambda e: self.set_outline(ADMIN_BORDER), add="+")


def chip_style(btn, on):
    """켜고 끄는 알약 버튼 — 켜지면 연한 파랑 + 파란 테두리, 꺼지면 카드색 + 회색 테두리."""
    if on:
        btn.set_style(BLUE_SOFT, BLUE_SOFT_HOVER, BLUE_DARK, outline=BLUE_LINE)
    else:
        btn.set_style(ADMIN_CARD, PANEL_ALT, TEXT_SUB, hover_fg=TEXT, outline=LINE)


class AdminTabs(tk.Frame):
    """
    관리자 화면 탭 — 상단 메뉴 버튼과 같은 둥근 버튼으로 탭을 고르고, 아래에 고른 탭 내용만 보여준다.
    ttk.Notebook 대신 쓰지만 add / select 사용법은 같다.
    """

    def __init__(self, master, on_change=None):
        super().__init__(master, bg=ADMIN_BG)
        self.on_change = on_change
        top = tk.Frame(self, bg=ADMIN_BG)
        top.pack(fill=tk.X, padx=px(16), pady=(px(2), px(4)))
        track = RoundedBox(top, fill="#E6E4DE", bg=ADMIN_BG, radius=px(16), pad=px(5))
        track.pack(side=tk.LEFT)
        self.bar = track.body
        self.content = tk.Frame(self, bg=ADMIN_BG)
        self.content.pack(fill=tk.BOTH, expand=True)
        self._tabs = []          # (frame, button)
        self._current = None

    def _style(self, btn, active):
        if active:
            btn.set_style(PANEL, PANEL, BLUE_DARK, outline=LINE)
        else:
            btn.set_style("#E6E4DE", "#DCDAD3", TEXT_SUB, hover_fg=TEXT)

    def add(self, frame, text):
        btn = RoundButton(self.bar, text=text.strip(), command=lambda: self.select(frame), height=px(42),
                          radius=px(12), font=(UI_FONT, 12, "bold"), bg="#E6E4DE", padx=px(26))
        btn.pack(side=tk.LEFT, padx=(0 if not self._tabs else px(4), 0))
        self._tabs.append((frame, btn))
        if self._current is None:
            self.select(frame)
        else:
            self._style(btn, False)

    def select(self, frame=None):
        """frame을 주면 그 탭으로 바꾸고, 안 주면 지금 탭의 경로(str)를 돌려준다 (ttk.Notebook과 같게)."""
        if frame is None:
            return str(self._current) if self._current is not None else ""
        if frame is self._current:
            return None
        if self._current is not None:
            self._current.pack_forget()
        self._current = frame
        frame.pack(in_=self.content, fill=tk.BOTH, expand=True)
        for f, btn in self._tabs:
            self._style(btn, f is frame)
        if self.on_change:
            self.on_change()
        return None


class CameraView:
    """카메라 패널 (상단 상태바 + 영상 영역). 앱은 attach_camera_view로 지금 그릴 패널을 고른다."""

    def __init__(self, master, bg=BG):
        self.panel = RoundedPanel(master, fill=CAM_BG, bg=bg, radius=px(20), outline=None, pad=px(14))
        cam = self.panel.body

        cam_bar = tk.Frame(cam, bg=CAM_BG, height=px(40))
        cam_bar.pack(fill=tk.X, side=tk.TOP)
        cam_bar.pack_propagate(False)

        self.lbl_cam_status = tk.Label(cam_bar, text="● OFF", bg=CAM_BG, fg="#F87171",
                                       font=(UI_FONT, 11, "bold"))
        self.lbl_cam_status.pack(side=tk.LEFT, padx=(px(8), px(14)))

        # 깜빡임(사람 확인) 상태 — 사진을 들이대면 여기가 회색으로 남는다
        self.lbl_liveness = tk.Label(cam_bar, text="", bg=CAM_BG, fg="#94A3B8", font=(UI_FONT, 10, "bold"))
        self.lbl_liveness.pack(side=tk.LEFT)

        # 영상 영역 — 이미지 크기 계산은 상태바를 뺀 이 영역 기준으로 한다
        self.video_area = tk.Frame(cam, bg=CAM_BG)
        self.video_area.pack(fill=tk.BOTH, expand=True)

        self.video_label = tk.Label(self.video_area, bg=CAM_BG)
        self.video_label.pack(padx=px(10), pady=px(10), fill=tk.BOTH, expand=True)


class PhotoViewer(tk.Toplevel):
    """관리자 출결로그에서 출퇴근 사진을 크게 보여주는 창."""

    def __init__(self, master, image, caption):
        super().__init__(master)
        self.title("출퇴근 사진")
        self.configure(bg="#0F172A")
        self.resizable(False, False)

        # 저장본은 320x240으로 작아서 보기 편하게 두 배로 키운다
        shown = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(shown)  # 참조를 들고 있어야 그림이 사라지지 않는다
        tk.Label(self, image=self._photo, bg="#0F172A").pack(padx=px(12), pady=(px(12), px(6)))
        tk.Label(self, text=caption, font=(UI_FONT, 11, "bold"), bg="#0F172A", fg="white") \
            .pack(pady=(0, px(12)))
        self.bind("<Escape>", lambda e: self.destroy())


class AdminDashboardFrame(tk.Frame):
    """
    관리자 로그인 후 화면. 탭이 화면 전체를 쓰고, 카메라는 [학생 등록] 탭에서만 왼쪽에 나온다.
    나가기(로그아웃)는 좌측 상단 로고를 누른다.
    """
    def __init__(self, parent, admin_email):
        super().__init__(parent.right_container, bg=BG)
        self.parent = parent
        self.admin_email = admin_email

        # 콤보박스 펼침 목록 글자도 화면 글자 크기에 맞춘다
        self.option_add("*TCombobox*Listbox.font", (UI_FONT, 12))
        style = ttk.Style()
        style.configure("Vertical.TScrollbar", background=PANEL_ALT, troughcolor=ADMIN_CARD, bordercolor=ADMIN_CARD,
                        lightcolor=PANEL_ALT, darkcolor=PANEL_ALT, arrowcolor=TEXT_SUB, relief=tk.FLAT)

        # 탭 — 상단 메뉴 버튼과 같은 둥근 버튼 (탭 내용은 아래 build_*_tab에서 채운다)
        self.notebook = AdminTabs(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        content = self.notebook.content
        self.tab_students = tk.Frame(content, bg=ADMIN_BG)
        self.tab_add_student = tk.Frame(content, bg=ADMIN_BG)
        self.tab_logs = tk.Frame(content, bg=ADMIN_BG)
        self.tab_settings = tk.Frame(content, bg=ADMIN_BG)

        self.notebook.add(self.tab_students, text="학생 현황/수정")
        self.notebook.add(self.tab_add_student, text="학생 등록")
        self.notebook.add(self.tab_logs, text="전체 출결로그")
        self.notebook.add(self.tab_settings, text="설정")

        self.build_students_tab()
        self.build_add_student_tab()
        self.build_logs_tab()
        self.build_settings_tab()
        self.notebook.on_change = self.on_tab_changed  # 다 만든 뒤부터 탭 바꿈을 받는다

    def on_tab_changed(self, _event=None):
        """[학생 등록] 탭에서만 그 탭 안의 카메라를 켠다 (얼굴을 찍어야 하므로)."""
        registering = self.notebook.select() == str(self.tab_add_student)
        self.parent.show_admin_camera(self.register_camera if registering else None)
        self.after(1500, self.show_camera_info)
        self._schedule_roster_refresh(now=registering)

    def _schedule_roster_refresh(self, now=False):
        """[학생 등록] 탭이 열려 있는 동안만 명단을 주기적으로 다시 읽는다 (구글폼에 새 응답이 들어오면 바로 보이게)."""
        if self._roster_job is not None:
            self.after_cancel(self._roster_job)
            self._roster_job = None
        if not self.winfo_exists() or self.notebook.select() != str(self.tab_add_student):
            return
        if now:
            self._fetch_roster()
        self._roster_job = self.after(ROSTER_REFRESH_SECONDS * 1000, self._auto_refresh_roster)

    def _auto_refresh_roster(self):
        self._roster_job = None
        self._schedule_roster_refresh(now=True)

    def choose_osk(self, enabled):
        self.parent.set_osk_enabled(enabled)
        self._sync_osk_buttons()

    @staticmethod
    def _sync_choice_buttons(buttons, current):
        """고른 값의 버튼은 파란색, 나머지는 회색으로 보여준다."""
        for value, btn in buttons.items():
            chip_style(btn, value == current)

    def _sync_osk_buttons(self):
        self._sync_choice_buttons(self._osk_buttons, self.parent.osk_enabled())

    def choose_button_size(self, key):
        self.parent.set_main_button_size(key)
        self._sync_size_buttons()

    def _sync_size_buttons(self):
        self._sync_choice_buttons(self._size_buttons, self.parent.main_button_size())

    def on_camera_selected(self):
        """고른 번호를 camera.txt에 저장하고, 캡처 스레드가 그 카메라로 다시 연결하게 한다."""
        index = self.cmb_camera.current()
        save_preferred_camera_index(index)
        core.ACTIVE_CAMERA_INDEX = None  # 새로 연결될 때까지 이전 번호를 보여주지 않는다
        self.parent.camera_reopen = True
        if self.parent.camera_on:
            self.lbl_camera_info.config(text="연결 중...", fg=TEXT_SUB)
            self.after(1500, self.show_camera_info)
        else:
            # 관리자 화면 카메라는 [학생 등록] 탭에서만 켜진다 — 거기서 바뀐 카메라로 열린다
            self.lbl_camera_info.config(text="[학생 등록] 탭에서 카메라를 켤 때 적용됩니다", fg=MUTED)

    def show_camera_info(self):
        """실제로 연결된 번호를 보여준다. 고른 번호가 안 열려서 0번으로 넘어갔다면 그것도 알려준다."""
        if not self.winfo_exists():
            return
        wanted = self.cmb_camera.current()
        if not self.parent.camera_on:
            return  # 카메라가 꺼져 있으면(학생 등록 탭이 아니면) 마지막 안내를 그대로 둔다
        if self.parent.camera_error:
            text, color = "● 연결 안 됨", "#EF4444"
        elif core.ACTIVE_CAMERA_INDEX is None:
            text, color = "연결 중...", "#64748B"
            self.after(1000, self.show_camera_info)
        elif core.ACTIVE_CAMERA_INDEX == wanted:
            text, color = f"● {core.ACTIVE_CAMERA_INDEX}번 연결됨", "#10B981"
        else:
            text, color = f"● {wanted}번이 열리지 않아 {core.ACTIVE_CAMERA_INDEX}번 사용 중", "#F59E0B"
        self.lbl_camera_info.config(text=text, fg=color)

    def export_attendance_excel(self):
        """
        현재까지 기록된 출석(attendance_logs) 전체를 학번+날짜별로 묶어서 .xlsx로 저장한다.
        DB 데이터는 읽기만 하고 건드리지 않는다.
        """
        try:
            wb, count = attendance_workbook()
        except ImportError:
            messagebox.showerror(
                "추출 실패",
                "엑셀 내보내기에 필요한 openpyxl 패키지가 설치되어 있지 않습니다.\n\n"
                "터미널에서 아래 명령으로 설치한 뒤 다시 시도해주세요:\n  pip install openpyxl",
            )
            return
        except Exception as e:
            messagebox.showerror("추출 실패", f"출석 데이터를 불러오는 중 오류가 발생했습니다.\n\n{e}")
            return

        if wb is None:
            messagebox.showinfo("추출할 데이터 없음", "저장된 출석 기록이 없습니다.")
            return

        default_name = f"출석기록_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        path = filedialog.asksaveasfilename(
            title="출석 데이터 추출",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel 파일", "*.xlsx")],
        )
        if not path:
            return  # 사용자가 취소함

        try:
            wb.save(path)
        except Exception as e:
            messagebox.showerror("추출 실패", f"엑셀 파일을 저장하는 중 오류가 발생했습니다.\n\n{e}")
            return

        messagebox.showinfo("추출 완료", f"출석 데이터 {count}건을 엑셀 파일로 저장했습니다.\n\n{path}")

    # 학생 목록 컬럼 — (키, 제목, 폭)
    STUDENT_COLUMNS = (("id", "학번", 150), ("name", "이름", 110), ("major", "전공", 170),
                       ("penalty", "패널티", 80), ("phone", "전화 번호", 160), ("grade", "학년", 70))
    # 편집 칸 — (키, 제목). 학번은 출결 기록과 이어져 있어서 바꾸지 못하게 보여주기만 한다
    EDIT_FIELDS = (("id", "학번"), ("name", "이름"), ("major", "전공"), ("phone", "전화번호"),
                   ("grade", "학년"), ("penalty", "패널티"))

    def build_students_tab(self):
        tab = self.tab_students
        gap = px(14)

        # 오른쪽: 학생 데이터 편집 (먼저 배치해야 폭이 줄어도 잘리지 않는다)
        edit_card = admin_card(tab, width=px(480))
        edit_card.pack(side=tk.RIGHT, fill=tk.Y, padx=(gap, gap), pady=gap)
        e = edit_card.body
        head = tk.Frame(e, bg=ADMIN_CARD)
        head.pack(fill=tk.X)
        title, _ = admin_card_title(head, "학생 데이터 편집", "목록에서 학생을 고르면 여기서 고칠 수 있습니다")
        title.pack(side=tk.LEFT)
        icon_button(head, "trash", px(30), self.delete_checked, ADMIN_CARD, color=ERR, tile=px(46),
                    tile_fill="#F8E5E3").pack(side=tk.RIGHT, anchor=tk.N)

        RoundButton(e, text="수정 완료", command=self.update_student, width=px(408), height=px(52), radius=px(12),
                    bg=ADMIN_CARD).pack(side=tk.BOTTOM, pady=(px(10), 0))

        form = tk.Frame(e, bg=ADMIN_CARD)
        form.pack(fill=tk.X, pady=(px(18), 0))
        form.columnconfigure((0, 1), weight=1, uniform="half")
        self.edit_entries = {}
        # 두 칸씩: 학번 · 이름 / 전공(넓게) / 전화번호 · 학년 / 패널티
        layout = {"id": (0, 0, 1), "name": (0, 1, 1), "major": (2, 0, 2), "phone": (4, 0, 1),
                  "grade": (4, 1, 1), "penalty": (6, 0, 1)}
        for key, title in self.EDIT_FIELDS:
            row, col, span = layout[key]
            padx = (0, px(6)) if col == 0 and span == 1 else (px(6), 0) if col == 1 else 0
            admin_field_label(form, title).grid(row=row, column=col, columnspan=span, sticky=tk.W, padx=padx,
                                                pady=(px(10), px(4)))
            field = AdminField(form, readonly=(key == "id"))
            field.grid(row=row + 1, column=col, columnspan=span, sticky=tk.EW, padx=padx)
            self.edit_entries[key] = field.entry

        # 왼쪽: 학생 리스트 (체크 · 저장(엑셀 추출) · 명단 새로고침)
        list_card = admin_card(tab, pad=8)
        list_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(gap, 0), pady=gap)
        body = list_card.body
        head = tk.Frame(body, bg=ADMIN_CARD)
        head.pack(fill=tk.X, padx=(px(26), px(18)), pady=(px(18), px(14)))
        title, self.lbl_student_count = admin_card_title(head, "학생 리스트")
        title.pack(side=tk.LEFT)
        icon_button(head, "refresh", px(30), self.refresh_roster_data, ADMIN_CARD, tile=px(50)) \
            .pack(side=tk.RIGHT)
        icon_button(head, "save", px(28), self.export_attendance_excel, ADMIN_CARD, tile=px(50)) \
            .pack(side=tk.RIGHT, padx=(0, px(12)))

        style = ttk.Style()
        style.configure("Admin.Treeview", background=ADMIN_CARD, fieldbackground=ADMIN_CARD, foreground=TEXT,
                        rowheight=px(48), font=(UI_FONT, 12), borderwidth=0)
        style.configure("Admin.Treeview.Heading", background=PANEL_ALT, foreground=TEXT_SUB,
                        font=(UI_FONT, 11, "bold"), relief=tk.FLAT, borderwidth=0, padding=(0, px(10)))
        # 고른 행은 진한 파랑 대신 연한 파랑으로 — 글자가 그대로 읽힌다
        style.map("Admin.Treeview", background=[("selected", BLUE_SOFT)], foreground=[("selected", BLUE_DARK)])
        style.map("Admin.Treeview.Heading", background=[("active", "#E8E6E0")])
        style.layout("Admin.Treeview", [("Admin.Treeview.treearea", {"sticky": "nswe"})])
        # 체크 칸(#0)에 펼침 표시(▶)가 자리를 차지하지 않게 뺀다
        style.layout("Admin.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        style.configure("Admin.Treeview.Item", padding=(px(14), 0))

        table = tk.Frame(body, bg=ADMIN_CARD)
        table.pack(fill=tk.BOTH, expand=True, padx=(0, px(12)), pady=(0, px(14)))
        scroll = ttk.Scrollbar(table)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(px(4), 0))

        box = px(24)
        self._img_box = ImageTk.PhotoImage(render_icon("box", box, MUTED))
        self._img_box_checked = ImageTk.PhotoImage(render_icon("box_checked", box, BLUE))
        self._checked = set()

        self.tree_students = ttk.Treeview(
            table, columns=[key for key, _, _ in self.STUDENT_COLUMNS], show="tree headings",
            style="Admin.Treeview", yscrollcommand=scroll.set, selectmode="browse"
        )
        self.tree_students.heading("#0", image=self._img_box, command=self.toggle_all_checked)
        self.tree_students.column("#0", width=px(58), minwidth=px(58), stretch=False, anchor=tk.CENTER)
        for key, title, width in self.STUDENT_COLUMNS:
            self.tree_students.heading(key, text=title)
            self.tree_students.column(key, width=px(width), minwidth=px(50), anchor=tk.CENTER)
        self.tree_students.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.tree_students.yview)

        self.tree_students.bind("<<TreeviewSelect>>", self.on_student_select)
        self.tree_students.bind("<Button-1>", self.on_student_click)

        self.load_students()

    def build_settings_tab(self):
        """
        왼쪽: 메인 화면 복귀 시간 (5초 단위, 5~60초) / 오른쪽: 화면 · 카메라 · 구글폼 명단 주소.
        모두 누르는 즉시 settings.json(카메라는 camera.txt)에 저장된다.
        """
        tab = self.tab_settings
        gap = px(14)

        def card(side, width=None, **pack):
            panel = admin_card(tab, width=width)
            panel.pack(side=side, fill=tk.BOTH, pady=gap, **pack)
            return panel.body

        def section(master, title, desc):
            tk.Label(master, text=title, font=(UI_FONT, 16, "bold"), bg=ADMIN_CARD, fg=TEXT).pack(anchor=tk.W)
            tk.Label(master, text=desc, font=(UI_FONT, 10), bg=ADMIN_CARD, fg=MUTED, justify=tk.LEFT,
                     wraplength=px(640)).pack(anchor=tk.W, pady=(px(4), px(14)))

        def setting_row(master, title, desc):
            row = RoundedBox(master, fill=PANEL_ALT, bg=ADMIN_CARD, radius=px(14), pad=px(14))
            row.pack(fill=tk.X, pady=(0, px(10)))
            r = row.body
            text = tk.Frame(r, bg=PANEL_ALT)
            text.pack(side=tk.LEFT)
            tk.Label(text, text=title, font=(UI_FONT, 13, "bold"), bg=PANEL_ALT, fg=TEXT).pack(anchor=tk.W)
            tk.Label(text, text=desc, font=(UI_FONT, 10), bg=PANEL_ALT, fg=TEXT_SUB).pack(anchor=tk.W)
            return r

        right = card(tk.RIGHT, width=px(700), padx=gap)
        left = card(tk.LEFT, expand=True, padx=(gap, 0))

        # 왼쪽 — 메인 화면 복귀 시간
        section(left, "메인 화면 복귀 시간",
                f"아무 조작이 없으면 이 시간 뒤에 메인 화면으로 돌아갑니다 "
                f"({TIMEOUT_STEP}초 단위, {TIMEOUT_MIN}~{TIMEOUT_MAX}초).")
        self._timeout_labels = {}
        self._timeout_buttons = {}
        for name, title, desc in (
            ("verify", "얼굴 확인 화면", "출근 · 퇴근 · 출석 조회를 누른 뒤 얼굴을 기다리는 시간"),
            ("result", "출퇴근 결과 화면", "\"OOO 학생 출근 되었습니다\" 화면이 떠 있는 시간"),
            ("stats", "출석 현황 화면", "이번 주 출석 현황이 떠 있는 시간"),
        ):
            r = setting_row(left, title, desc)
            # − 30초 + 를 한 덩어리로 묶은 조절기
            stepper = RoundedBox(r, fill=PANEL, bg=PANEL_ALT, radius=px(12), pad=px(4), outline=LINE)
            stepper.pack(side=tk.RIGHT)
            btn = dict(width=px(44), height=px(40), radius=px(9), font=(UI_FONT, 16, "bold"), bg=PANEL)
            minus = RoundButton(stepper.body, text="−", command=lambda n=name: self.change_timeout(n, -TIMEOUT_STEP),
                                **btn)
            minus.pack(side=tk.LEFT)
            lbl = tk.Label(stepper.body, text="", width=5, font=(UI_FONT, 14, "bold"), bg=PANEL, fg=TEXT)
            lbl.pack(side=tk.LEFT)
            plus = RoundButton(stepper.body, text="+", command=lambda n=name: self.change_timeout(n, TIMEOUT_STEP),
                               **btn)
            plus.pack(side=tk.LEFT)
            self._timeout_labels[name] = lbl
            self._timeout_buttons[name] = (minus, plus)
            self._show_timeout(name)

        # 오른쪽 — 화면 · 카메라 · 학생 명단
        section(right, "화면 · 장치", "고르면 바로 저장되고 반영됩니다.")
        choice = dict(height=px(44), font=(UI_FONT, 12, "bold"), bg=PANEL_ALT, padx=px(22))

        def choice_buttons(r, options, command):
            buttons = {}
            for value, label in reversed(options):  # 오른쪽부터 쌓이므로 거꾸로
                btn = RoundButton(r, text=label, command=lambda v=value: command(v), **choice)
                btn.pack(side=tk.RIGHT, padx=(px(8), 0))
                buttons[value] = btn
            return buttons

        self._size_buttons = choice_buttons(
            setting_row(right, "메인 화면 버튼 크기", "출근 · 퇴근 카드의 크기"),
            [(key, label) for key, (label, _scale) in MAIN_BUTTON_SIZES.items()], self.choose_button_size)
        self._sync_size_buttons()
        self._osk_buttons = choice_buttons(
            setting_row(right, "화면 키보드", "입력칸을 누르면 화면에 키보드를 띄운다"),
            [(True, "켬"), (False, "끔")], self.choose_osk)
        self._sync_osk_buttons()

        # 카메라 장치 — 외장 USB 카메라를 쓸 때 번호를 바꾼다 (노트북 내장 0번, 외장은 보통 1번)
        r = setting_row(right, "카메라 장치", "외장 USB 카메라는 보통 1번")
        self.camera_choices = [f"{i}번" for i in range(5)]
        self.cmb_camera = ttk.Combobox(r, state="readonly", width=5, font=(UI_FONT, 11),
                                       values=self.camera_choices, style="Admin.TCombobox")
        saved = preferred_camera_index()
        self.cmb_camera.current(saved if saved is not None and saved < len(self.camera_choices) else 0)
        self.cmb_camera.pack(side=tk.RIGHT)
        self.cmb_camera.bind("<<ComboboxSelected>>", lambda e: self.on_camera_selected())
        self.lbl_camera_info = tk.Label(right, text="", font=(UI_FONT, 10), bg=ADMIN_CARD, fg=MUTED)
        self.lbl_camera_info.pack(anchor=tk.E, pady=(0, px(6)))

        # 학생 명단 — 구글폼 응답 시트를 CSV로 웹에 게시한 주소 ([학생 등록]의 구글폼 학생 명단)
        # 서비스 계정 키 — 이 계정에 응답 시트를 '뷰어'로 공유하면 시트를 공개하지 않고 읽는다
        r = setting_row(right, "서비스 계정 키", "")
        self.lbl_key_email = r.winfo_children()[0].winfo_children()[1]
        RoundButton(r, text="키 파일 선택", command=self.choose_google_key, **choice).pack(side=tk.RIGHT)
        self._show_key_email()

        r = setting_row(right, "구글폼 학생 명단 주소",
                        "응답 시트 주소(실시간) 또는 CSV로 게시한 주소")
        RoundButton(r, text="저장", command=self.save_google_form_url, **choice).pack(side=tk.RIGHT)
        url_box = RoundedBox(right, fill="#FFFFFF", bg=ADMIN_CARD, radius=px(10), pad=px(8), outline=ADMIN_BORDER)
        url_box.pack(fill=tk.X)
        self.ent_form_url = tk.Entry(url_box.body, font=(UI_FONT, 10), bd=0, relief=tk.FLAT, bg="white",
                                     fg=TEXT, highlightthickness=0, width=10)
        self.ent_form_url.pack(fill=tk.X)
        self.ent_form_url.insert(0, str(load_settings().get("google_form_csv_url", "")))
        tk.Label(right, text="시트 주소를 넣으면 Sheets API로 실시간으로 읽고, 비워 두면 창공시스템 명단(roster.json)을 씁니다.",
                 wraplength=px(620), justify=tk.LEFT, font=(UI_FONT, 10),
                 bg=ADMIN_CARD, fg=MUTED).pack(anchor=tk.W, pady=(px(6), 0))

    def _show_key_email(self):
        email = google_service_account_email()
        self.lbl_key_email.config(text=email or "Google Cloud에서 받은 JSON 키 파일을 고르세요",
                                  fg=TEXT_SUB if email else ERR)

    def choose_google_key(self):
        """서비스 계정 JSON 키를 골라 프로그램 폴더에 복사하고, 시트를 공유할 이메일을 알려준다."""
        path = filedialog.askopenfilename(title="서비스 계정 키(JSON) 선택", filetypes=[("JSON 키", "*.json")])
        if not path:
            return
        email = google_service_account_email(path)
        if not email:
            messagebox.showerror("키 파일 오류", "서비스 계정 JSON 키가 아닙니다.\n"
                                 "Google Cloud → IAM 및 관리자 → 서비스 계정 → 키 → [JSON]으로 받은 파일을 골라주세요.")
            return
        shutil.copyfile(path, GOOGLE_KEY_PATH)
        self._show_key_email()
        messagebox.showinfo("키 등록 완료", "서비스 계정 키를 등록했습니다.\n\n"
                            "응답 시트의 [공유]에서 아래 이메일을 '뷰어'로 추가해야 명단을 읽을 수 있습니다.\n\n"
                            f"{email}")

    def save_google_form_url(self):
        url = self.ent_form_url.get().strip()
        if url and not url.startswith(("http://", "https://")):
            messagebox.showerror("주소 오류", "구글 응답 시트 주소(https://docs.google.com/spreadsheets/d/...) 또는 "
                                 "CSV로 게시한 https:// 주소를 넣어주세요.")
            return
        save_setting("google_form_csv_url", url)
        self._fetch_roster(show_message=True)  # 바로 읽어 봐서 주소가 맞는지 알려준다

    def _show_timeout(self, name):
        value = timeouts[name]
        self._timeout_labels[name].config(text=f"{value}초")
        # 끝에 닿은 쪽 버튼은 회색으로 — 눌러도 바뀌지 않는다 (change_timeout에서 막는다)
        minus, plus = self._timeout_buttons[name]
        for btn, enabled in ((minus, value > TIMEOUT_MIN), (plus, value < TIMEOUT_MAX)):
            if enabled:
                btn.set_style(PANEL, BLUE_SOFT, BLUE)
            else:
                btn.set_style(PANEL, PANEL, LINE)

    def change_timeout(self, name, delta):
        value = timeouts[name] + delta
        if not TIMEOUT_MIN <= value <= TIMEOUT_MAX:
            return
        save_timeout(name, value)
        self._show_timeout(name)

    # 학생 등록 입력칸 — (키, 제목)
    ADD_FIELDS = (("id", "학번"), ("name", "이름"), ("major", "전공"), ("phone", "전화번호"), ("grade", "학년"))

    def build_add_student_tab(self):
        tab = self.tab_add_student
        gap = px(14)

        # 오른쪽: 제목 + 입력 카드 (먼저 배치해야 카메라가 남은 폭을 다 쓴다)
        right = tk.Frame(tab, bg=ADMIN_BG, width=px(640))
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=gap, pady=gap)
        right.pack_propagate(False)
        tk.Label(right, text="학생 얼굴 및 상세 정보 등록", font=(UI_FONT, 24, "bold"), bg=ADMIN_BG, fg=TEXT) \
            .pack(anchor=tk.W, padx=px(4), pady=(px(4), px(2)))
        tk.Label(right, text="명단에서 학생을 고르고, 카메라를 바라본 채 눈을 한 번 깜빡인 뒤 등록하세요.",
                 font=(UI_FONT, 11), bg=ADMIN_BG, fg=TEXT_SUB).pack(anchor=tk.W, padx=px(4), pady=(0, px(12)))
        card_panel = admin_card(right, pad=30)
        card_panel.pack(fill=tk.BOTH, expand=True)
        card = card_panel.body

        RoundButton(card, text="얼굴 등록", command=self.register_student, height=px(68), width=px(530),
                    radius=px(16), font=(UI_FONT, 18, "bold"), bg=ADMIN_CARD,
                    icon=render_icon("camera", px(34), "#FFFFFF")).pack(side=tk.BOTTOM)

        form = tk.Frame(card, bg=ADMIN_CARD)
        form.pack(fill=tk.X)
        form.columnconfigure((0, 1), weight=1, uniform="half")

        # 구글폼 응답(설정 탭에서 주소 입력)에서 골라서 자동 입력 — 수기로 다시 안 쳐도 된다
        admin_field_label(form, "구글폼 학생 명단").grid(row=0, column=0, columnspan=2, sticky=tk.W,
                                                     pady=(0, px(4)))
        pick = tk.Frame(form, bg=ADMIN_CARD)
        pick.grid(row=1, column=0, columnspan=2, sticky=tk.EW)
        style = ttk.Style()
        style.configure("Admin.TCombobox", fieldbackground="white", background="white", foreground=TEXT,
                        arrowsize=px(18), padding=(px(10), px(8)), bordercolor=ADMIN_BORDER,
                        lightcolor="white", darkcolor="white", arrowcolor=TEXT_SUB)
        # 읽기 전용 콤보박스도 회색이 아니라 흰 바탕으로 보이게 한다
        style.map("Admin.TCombobox", fieldbackground=[("readonly", "white")],
                  selectbackground=[("readonly", "white")], selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", BLUE)])
        icon_button(pick, "refresh", px(26), self.refresh_roster_data, ADMIN_CARD, tile=px(46)) \
            .pack(side=tk.RIGHT, padx=(px(10), 0))
        self.roster_combo = ttk.Combobox(pick, font=(UI_FONT, 12), width=10, state="readonly",
                                         style="Admin.TCombobox")
        self.roster_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.roster_combo.bind("<<ComboboxSelected>>", self.on_roster_selected)
        # 명단을 언제 읽었는지 — 이 탭이 열려 있으면 ROSTER_REFRESH_SECONDS마다 저절로 다시 읽는다
        self.lbl_roster_status = tk.Label(form, text="명단을 불러오는 중...", font=(UI_FONT, 10), bg=ADMIN_CARD,
                                          fg=MUTED, anchor=tk.W)
        self.lbl_roster_status.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(px(6), 0))

        tk.Frame(form, bg=LINE, height=1).grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=px(18))

        # 학번 · 이름 / 전공 · 전화번호
        self.add_entries = {}
        for i, (key, title) in enumerate(self.ADD_FIELDS):
            row, col = 4 + (i // 2) * 2, i % 2
            padx = (0, px(6)) if col == 0 else (px(6), 0)
            admin_field_label(form, title).grid(row=row, column=col, sticky=tk.W, padx=padx, pady=(px(6), px(4)))
            field = AdminField(form, font_size=13)
            field.grid(row=row + 1, column=col, sticky=tk.EW, padx=padx)
            self.add_entries[key] = field.entry
        self.clear_add_form()

        # 왼쪽: 카메라 — 이 탭을 열 때만 켜진다 (on_tab_changed)
        self.register_camera = CameraView(tab, bg=ADMIN_BG)
        self.register_camera.panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(gap, 0), pady=gap)

        self._roster = []
        self._roster_map = {}
        self._roster_job = None     # 다음 자동 새로고침 예약
        self._roster_fetching = False
        self._fetch_roster()

    def clear_add_form(self):
        for entry in self.add_entries.values():
            entry.delete(0, tk.END)
        self.add_entries["major"].insert(0, DEPARTMENT_NAME)  # 대부분 소프트웨어전공이라 미리 채워 둔다

    def refresh_roster_data(self):
        """[학생 현황/수정]·[학생 등록]의 새로고침 아이콘 — 명단을 다시 읽고 결과를 알려준다."""
        self._fetch_roster(show_message=True)

    def _fetch_roster(self, show_message=False):
        """
        명단(구글폼 또는 창공시스템)을 다시 읽어 [학생 등록] 목록과 얼굴 인식 이름 표시에 반영한다.
        구글폼은 인터넷에서 받아 오므로 뒤에서 읽고, 다 읽으면 화면에 반영한다 (그동안 화면이 멈추지 않게).
        """
        if self._roster_fetching:  # 이미 읽는 중이면 겹쳐서 부르지 않는다
            return
        self._roster_fetching = True
        result = {}

        def work():
            try:
                result["roster"], result["source"] = load_student_roster()
            except Exception as e:
                result["error"] = e

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def finish():
            if not self.winfo_exists():
                return
            if worker.is_alive():
                self.after(100, finish)
                return
            self._roster_fetching = False
            now = datetime.now().strftime("%H:%M:%S")
            if "error" in result:
                self.lbl_roster_status.config(text=f"● 명단을 불러오지 못했습니다 ({now}) — 새로고침을 눌러 원인 확인",
                                              fg=ERR)
                if show_message:
                    messagebox.showerror("새로고침 실패", self._roster_error_message(result["error"]))
                return
            self._roster = result["roster"]
            self.parent.apply_roster(self._roster)
            self.reload_roster_combo()
            if self._fill_blanks_from_roster():
                self.load_students()
            live = f" · {ROSTER_REFRESH_SECONDS}초마다 자동 갱신" if result["source"] == "구글 시트" else ""
            self.lbl_roster_status.config(
                text=f"● {result['source']} 명단 {len(self._roster)}명 · 미등록 {len(self._roster_map)}명 "
                     f"· {now} 업데이트{live}", fg=OK)
            if show_message:
                messagebox.showinfo(
                    "새로고침 완료",
                    f"{result['source']} 명단 {len(self._roster)}건을 다시 불러왔습니다.\n"
                    "얼굴인식 결과에 표시되는 이름에도 바로 반영됩니다.",
                )

        finish()

    @staticmethod
    def _roster_error_message(error):
        return roster_error_message(error)

    def _fill_blanks_from_roster(self):
        """이미 등록된 학생의 빈 전화번호·학년을 명단 값으로 채운다. 바뀐 학생 수를 돌려준다."""
        return fill_blank_contacts_from_roster(self._roster)

    def reload_roster_combo(self):
        """받아 둔 명단에서 아직 등록 안 된 학생만 목록에 채운다."""
        self._roster_map = {}
        values = []
        for student in unregistered_roster(self._roster):
            sid = str(student.get("StudentId", "")).strip()
            label = f"{sid} - {student.get('Name', '')}"
            self._roster_map[label] = student
            values.append(label)

        # 자동 새로고침 중에도 고르던 학생은 그대로 둔다 (방금 등록해서 목록에서 빠졌으면 비운다)
        current = self.roster_combo.get()
        self.roster_combo["values"] = values
        self.roster_combo.set(current if current in self._roster_map else "")

    def on_roster_selected(self, event):
        student = self._roster_map.get(self.roster_combo.get())
        if not student:
            return
        self.clear_add_form()
        for key, field in (("id", "StudentId"), ("name", "Name"), ("major", "Department"), ("phone", "Phone"),
                           ("grade", "Grade")):
            value = str(student.get(field) or "").strip()
            if key == "grade":
                value = normalize_grade(value)
            if value:
                self.add_entries[key].delete(0, tk.END)
                self.add_entries[key].insert(0, value)

    def build_logs_tab(self):
        tab = self.tab_logs
        gap = px(14)

        # 오른쪽: 조회 조건 (먼저 배치해야 목록이 남은 폭을 다 쓴다)
        side = admin_card(tab, width=px(560))
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=gap, pady=gap)
        f = side.body
        title, _ = admin_card_title(f, "조회 조건", "기간과 조건을 고르고 [조회]를 누르세요")
        title.pack(fill=tk.X, pady=(0, px(6)))

        # 기간 — 조건에 따라 시작~종료 / 기준일 하나 / 고정(최근 7일)로 바뀐다
        self.lbl_log_date = admin_field_label(f, "기간")
        self.lbl_log_date.pack(fill=tk.X, pady=(px(12), px(4)))
        date_box = RoundedBox(f, fill="#FFFFFF", bg=ADMIN_CARD, radius=px(10), pad=px(4), outline=ADMIN_BORDER)
        date_box.pack(fill=tk.X)
        today = date.today()
        self.date_start = DatePicker(date_box.body, today - timedelta(days=6), on_change=self.load_logs_filtered,
                                     flat=True, font_size=12)
        self.date_start.pack(side=tk.LEFT, padx=(px(4), 0))
        self.lbl_date_tilde = tk.Label(date_box.body, text="~", bg="white", fg=MUTED, font=(UI_FONT, 12))
        self.lbl_date_tilde.pack(side=tk.LEFT)
        self.date_end = DatePicker(date_box.body, today, on_change=self.load_logs_filtered, flat=True,
                                   font_size=12)
        self.date_end.pack(side=tk.LEFT)
        self.lbl_log_fixed = tk.Label(date_box.body, text="오늘까지 최근 7일", bg="white", fg=TEXT_SUB,
                                      font=(UI_FONT, 12), padx=px(12), pady=px(4))

        admin_field_label(f, "이름 / 학번").pack(fill=tk.X, pady=(px(14), px(4)))
        search = AdminField(f)
        search.pack(fill=tk.X)
        self.ent_search_name = search.entry
        self.ent_search_name.bind("<Return>", lambda e: self.load_logs_filtered())

        # 조건 — 하나만 고를 수 있고, 다시 누르면 풀려서 기간 내역으로 돌아간다
        admin_field_label(f, "조건 — 주 단위는 월~일 기준, 학생별로 합칩니다 (다시 누르면 해제)")             .pack(fill=tk.X, pady=(px(18), px(6)))
        self._log_mode = "custom"
        self._mode_checks = {}
        chips = tk.Frame(f, bg=ADMIN_CARD)
        chips.pack(fill=tk.X)
        chip_w = (px(560) - 2 * (px(26) + px(ADMIN_SHADOW)) - px(10)) // 2
        for i, (key, label) in enumerate(LOG_FILTER_MODES):
            chip = RoundButton(chips, text=label, command=lambda k=key: self.toggle_log_mode(k), width=chip_w,
                               height=px(44), radius=px(12), font=(UI_FONT, 11, "bold"), bg=ADMIN_CARD)
            chip.grid(row=i // 2, column=i % 2, padx=(0 if i % 2 == 0 else px(10), 0), pady=(0, px(10)))
            chip_style(chip, False)
            self._mode_checks[key] = chip

        # 맨 아래: 조회 · 결과 요약 · 사진 안내 (먼저 배치해야 잘리지 않는다)
        tk.Label(f, text=f"행을 더블클릭하면 출퇴근 사진을 봅니다 · 사진은 {PHOTO_RETENTION_DAYS}일 뒤 자동 삭제",
                 font=(UI_FONT, 9), bg=ADMIN_CARD, fg=MUTED, anchor=tk.W).pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_log_result = tk.Label(f, text="", anchor=tk.W, justify=tk.LEFT, bg=ADMIN_CARD, fg=TEXT,
                                       font=(UI_FONT, 11, "bold"), wraplength=px(480))
        self.lbl_log_result.pack(side=tk.BOTTOM, fill=tk.X, pady=(px(14), px(4)))
        RoundButton(f, text="조회", command=self.load_logs_filtered, width=chip_w * 2 + px(10), height=px(54),
                    radius=px(12), font=(UI_FONT, 14, "bold"), bg=ADMIN_CARD).pack(side=tk.BOTTOM)

        # 왼쪽: 출결 목록 (조회 조건에 따라 내역/학생별 합계 컬럼으로 바뀐다)
        list_card = admin_card(tab, pad=8)
        list_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(gap, 0), pady=gap)
        list_container = tk.Frame(list_card.body, bg=ADMIN_CARD)
        head = tk.Frame(list_card.body, bg=ADMIN_CARD)
        head.pack(fill=tk.X, padx=(px(26), px(18)), pady=(px(18), px(14)))
        title, _ = admin_card_title(head, "출결 기록", "출근 · 퇴근이 찍힌 순서대로, 최근 기록이 위에 나옵니다")
        title.pack(side=tk.LEFT)
        list_container.pack(fill=tk.BOTH, expand=True, padx=(0, px(12)), pady=(0, px(14)))

        scroll = ttk.Scrollbar(list_container, orient=tk.VERTICAL)
        self.tree_logs = ttk.Treeview(list_container, show="headings", style="Admin.Treeview",
                                      yscrollcommand=scroll.set)
        self.tree_logs.tag_configure("invalid", foreground="#94A3B8")
        scroll.config(command=self.tree_logs.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(px(4), 0))
        self.tree_logs.pack(fill=tk.BOTH, expand=True)
        self.tree_logs.bind("<Double-1>", lambda e: self.show_log_photo())
        self._log_photos = {}  # 트리뷰 행 id → (사진 상대 경로, 설명)

        self.on_log_mode_changed()

    def current_log_mode(self):
        return self._log_mode

    def toggle_log_mode(self, key):
        self._log_mode = "custom" if self._log_mode == key else key
        for k, chip in self._mode_checks.items():
            chip_style(chip, k == self._log_mode)
        self.on_log_mode_changed()

    def on_log_mode_changed(self):
        mode = self.current_log_mode()

        # 최근 7일 합계는 날짜를 고를 필요가 없고, 월/주 단위는 기준일 1개, 기간 내역은 시작~종료 2개
        for widget in (self.date_start, self.lbl_date_tilde, self.date_end, self.lbl_log_fixed):
            widget.pack_forget()
        if mode == "recent7_sum":
            self.lbl_log_date.config(text="기간")
            self.lbl_log_fixed.pack(side=tk.LEFT)
        elif mode == "custom":
            self.lbl_log_date.config(text="기간")
            self.date_start.pack(side=tk.LEFT, padx=(px(4), 0))
            self.lbl_date_tilde.pack(side=tk.LEFT)
            self.date_end.pack(side=tk.LEFT)
        else:
            self.lbl_log_date.config(text="기준일")
            self.date_start.pack(side=tk.LEFT, padx=(px(4), 0))

        self.load_logs_filtered()

    def get_log_period(self):
        """현재 조회 조건의 실제 조회 기간 (시작일, 종료일)."""
        mode = self.current_log_mode()
        start, end = self.date_start.get_date(), self.date_end.get_date()
        if mode == "custom" and start > end:  # 거꾸로 골랐으면 순서를 바로잡아 보여준다
            self.date_start.set_date(end)
            self.date_end.set_date(start)
        return log_period(mode, start, end)

    # DB 및 로드 제어 기능들
    def load_students(self):
        self.tree_students.delete(*self.tree_students.get_children())
        self._checked.clear()
        self.tree_students.heading("#0", image=self._img_box)
        for r in list_students():
            # iid를 학번으로 두면 체크한 학생을 학번으로 바로 찾을 수 있다
            self.tree_students.insert("", tk.END, iid=str(r[0]), image=self._img_box, values=r)
        count = len(self.tree_students.get_children())
        self.lbl_student_count.config(text=f"등록된 학생 {count}명 · 체크한 학생은 휴지통으로 지웁니다")
        self.clear_edit_form()

    def clear_edit_form(self):
        for key, entry in self.edit_entries.items():
            entry.config(state=tk.NORMAL)
            entry.delete(0, tk.END)
            if key == "id":
                entry.config(state="readonly")

    def set_checked(self, item, checked):
        if checked:
            self._checked.add(item)
        else:
            self._checked.discard(item)
        self.tree_students.item(item, image=self._img_box_checked if checked else self._img_box)

    def on_student_click(self, event):
        """맨 앞 체크 칸을 누르면 체크를 켜고 끈다 (행 선택은 그대로 된다)."""
        if self.tree_students.identify_column(event.x) != "#0":
            return
        if self.tree_students.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree_students.identify_row(event.y)
        if item:
            self.set_checked(item, item not in self._checked)
            all_checked = self._checked and len(self._checked) == len(self.tree_students.get_children())
            self.tree_students.heading("#0", image=self._img_box_checked if all_checked else self._img_box)

    def toggle_all_checked(self):
        items = self.tree_students.get_children()
        check = len(self._checked) < len(items)
        for item in items:
            self.set_checked(item, check)
        self.tree_students.heading("#0", image=self._img_box_checked if check and items else self._img_box)

    def load_logs(self):
        # 기본값 로드 (필터 없이 전체 출력)
        self.load_logs_filtered()

    def load_logs_filtered(self):
        mode = self.current_log_mode()
        start, end = self.get_log_period()
        keyword = self.ent_search_name.get().strip()

        if mode in LOG_SUMMARY_MODES:
            self._show_log_summary(mode, start, end, keyword)
        else:
            self._show_log_rows(start, end, keyword)

        # 기준일로 정해지는 기간(주/월/최근 7일)은 실제 조회 기간을 결과 위에 같이 보여준다
        if mode != "custom":
            period = (f"{start.year}년 {start.month}월 전체" if mode == "month"
                      else f"{format_date_ko(start)} ~ {format_date_ko(end)}")
            self.lbl_log_result.config(text=f"{period}\n{self.lbl_log_result.cget('text')}")

    def _set_log_columns(self, columns):
        """columns: (키, 제목, 폭, 정렬) 목록으로 트리뷰 컬럼을 바꾸고 기존 행을 비운다."""
        self.tree_logs.delete(*self.tree_logs.get_children())
        self._log_photos = {}
        self.tree_logs["columns"] = [key for key, _, _, _ in columns]
        self.tree_logs["displaycolumns"] = "#all"
        for key, heading, width, anchor in columns:
            self.tree_logs.heading(key, text=heading)
            self.tree_logs.column(key, width=px(width), minwidth=px(50), anchor=anchor, stretch=True)

    def _show_log_rows(self, start, end, keyword):
        self._set_log_columns([
            ("user_id", "학번", 120, tk.CENTER),
            ("name", "이름", 90, tk.CENTER),
            ("log_type", "구분", 110, tk.CENTER),
            ("log_date", "날짜", 150, tk.CENTER),
            ("log_time", "시간", 100, tk.CENTER),
            ("penalty", "패널티", 70, tk.CENTER),
        ])

        rows = query_log_rows(start, end, keyword)
        for r in rows:
            type_text = "출근" if r["log_type"] == "CHECK_IN" else "퇴근"
            if not r["valid"]:
                type_text += " (미인정)"  # 00:00~06:00 기록은 일수/시간 계산에서 빠진다
            try:
                date_text = format_date_ko(datetime.strptime(r["log_date"], "%Y-%m-%d").date())
            except (TypeError, ValueError):
                date_text = r["log_date"]
            item = self.tree_logs.insert(
                "", tk.END,
                values=(r["user_id"], r["name"], type_text, date_text, r["log_time"],
                        "" if r["penalty"] is None else r["penalty"]),
                tags=() if r["valid"] else ("invalid",)
            )
            if r["photo_path"]:
                self._log_photos[item] = (r["photo_path"],
                                          f"{r['name']} ({r['user_id']}) · {date_text} {r['log_time']} {type_text}")

        check_in = sum(1 for r in rows if r["log_type"] == "CHECK_IN")
        self.lbl_log_result.config(
            text=f"총 {len(rows)}건 (출근 {check_in} · 퇴근 {len(rows) - check_in}) "
                 f"· 학생 {len({r['user_id'] for r in rows})}명"
        )

    def show_log_photo(self):
        """출결로그에서 고른 행의 출퇴근 사진을 연다. (관리자 화면에서만 볼 수 있다)"""
        selected = self.tree_logs.selection()
        if not selected:
            messagebox.showinfo("사진 보기", "사진을 볼 출결 기록을 목록에서 선택하세요.")
            return
        entry = self._log_photos.get(selected[0])
        if not entry:
            messagebox.showinfo(
                "사진 없음",
                "이 기록에는 사진이 없습니다.\n"
                f"(사진 기능 도입 전 기록이거나, {PHOTO_RETENTION_DAYS}일 보관 기간이 지나 삭제되었습니다)",
            )
            return
        rel_path, caption = entry
        try:
            with Image.open(photo_abs_path(rel_path)) as img:
                image = img.convert("RGB")
        except (OSError, ValueError):
            messagebox.showinfo("사진 없음", "사진 파일을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.")
            return
        PhotoViewer(self, image, caption)

    def _show_log_summary(self, mode, start, end, keyword):
        self._set_log_columns([
            ("user_id", "학번", 110, tk.CENTER),
            ("name", "이름", 90, tk.CENTER),
            ("days", "출근 일수", 90, tk.CENTER),
            ("seconds", "출석 시간 합계", 170, tk.CENTER),
        ])

        summary = query_log_summary(mode, start, end, keyword)
        for s in summary:
            self.tree_logs.insert("", tk.END, values=(s["user_id"], s["name"], f"{s['days']}일",
                                                      format_duration(s["seconds"])))

        if mode == "recent7_sum":
            total = sum(s["seconds"] for s in summary)
            attended = sum(1 for s in summary if s["days"] > 0)
            text = f"학생 {len(summary)}명 중 출석 {attended}명 · 전체 출석 시간 {format_duration(total)}"
        else:
            text = f"조건을 충족한 학생 {len(summary)}명"
        self.lbl_log_result.config(text=text)

    def on_student_select(self, event):
        selected = self.tree_students.selection()
        if not selected:
            return
        values = dict(zip([key for key, _, _ in self.STUDENT_COLUMNS],
                          self.tree_students.item(selected[0], "values")))
        self.clear_edit_form()
        for key, entry in self.edit_entries.items():
            entry.config(state=tk.NORMAL)
            entry.insert(0, values.get(key, ""))
            if key == "id":
                entry.config(state="readonly")

    def update_student(self):
        selected = self.tree_students.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "수정할 대상을 리스트에서 선택하세요.")
            return

        student_id = self.tree_students.item(selected[0], "values")[0]
        values = {key: entry.get().strip() for key, entry in self.edit_entries.items()}
        try:
            core.update_student(student_id, values["name"], values["major"], values["penalty"],
                                values["phone"], values["grade"])
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e))
            return

        messagebox.showinfo("수정 성공", "학생 정보 변경이 완료되었습니다.")
        self.load_students()
        self.parent.reload_users()

    def delete_checked(self):
        """체크한 학생들(체크가 없으면 지금 선택한 학생)과 그 학생들의 출결 기록·사진을 지운다."""
        items = [item for item in self.tree_students.get_children() if item in self._checked]
        if not items:
            items = list(self.tree_students.selection())
        if not items:
            messagebox.showwarning("선택 없음", "삭제할 학생을 체크하거나 리스트에서 선택하세요.")
            return

        students = [self.tree_students.item(item, "values")[:2] for item in items]
        if len(students) == 1:
            question = f"[{students[0][1]}] 학생 정보를 완전히 삭제하시겠습니까?"
        else:
            question = f"체크한 학생 {len(students)}명의 정보를 완전히 삭제하시겠습니까?"
        if not messagebox.askyesno("삭제 확인", question + "\n출결 기록과 출퇴근 사진도 함께 삭제됩니다."):
            return

        delete_students([student_id for student_id, _name in students])  # 출결 기록·출퇴근 사진도 같이

        messagebox.showinfo("성공", f"학생 {len(students)}명의 정보가 삭제되었습니다.")
        self.load_students()
        self.load_logs()
        self.parent.reload_users()

    def register_student(self):
        values = {key: entry.get().strip() for key, entry in self.add_entries.items()}
        s_id, name, phone = values["id"], values["name"], values["phone"]
        grade = normalize_grade(values["grade"])
        major = values["major"] or DEPARTMENT_NAME

        if not (s_id and name):
            messagebox.showwarning("입력 미달", "학번과 이름을 입력하세요.")
            return

        if not self.parent.cached_faces:
            messagebox.showerror("얼굴 인식 실패", "카메라 영역에 등록할 학생의 얼굴이 감지되지 않았습니다.")
            return

        # 사진으로 학생이 등록되면 이후 출석도 사진으로 뚫리므로 등록할 때도 사람인지 확인한다
        if not self.parent.passes_antispoof():
            messagebox.showerror("얼굴 인식 실패", self.parent.liveness_reject_message())
            return

        if not self.parent.has_recent_blink():
            messagebox.showerror(
                "얼굴 인식 실패",
                "사람 얼굴이 아닌 것으로 보입니다.\n"
                "등록할 학생이 카메라를 바라보고 눈을 천천히 감았다 뜬 뒤 다시 눌러주세요.",
            )
            return

        new_embedding = self.parent.cached_faces[0].embedding.astype(np.float32)
        dup_user = find_duplicate_face(new_embedding, self.parent.enrolled_users, self.parent.embed_matrix)
        if dup_user:
            messagebox.showerror(
                "중복 얼굴 감지",
                f"이미 등록된 얼굴과 유사도가 높습니다.\n"
                f"기존 등록자: {dup_user['name']} ({dup_user['user_id']})\n"
                "동일 인물로 판단되어 등록을 진행하지 않습니다."
            )
            return

        try:
            insert_student(s_id, name, major, phone, grade, new_embedding)
        except ValueError as e:
            messagebox.showerror("오류", str(e))
            return

        messagebox.showinfo("성공", f"[{name}] 학생이 성공적으로 등록되었습니다.")
        self.clear_add_form()

        self.load_students()
        self.parent.reload_users()
        self.reload_roster_combo()  # 방금 등록한 학생은 명단 목록에서 빠지도록


# ==========================================
# 3. 메인 어플리케이션
# ==========================================
class AttendanceApp:
    def __init__(self, window, kiosk=False):
        self.window = window
        # 무인 운영(--kiosk): 00:00~06:00(출퇴근 미인정 시간)이 되면 스스로 종료한다.
        # 06:00 재시작은 윈도우 작업 스케줄러가 맡는다 (windows/install.bat 참고).
        self.kiosk = kiosk
        self.window.title("창의공간 얼굴인식 출석체크 시스템")
        self.window.configure(bg=BG)  # 탁한 흰색 배경

        # 창공시스템(SeatManagerApp, MainWindow.xaml)과 동일한 1600x900 레이아웃을 기준으로 한다.
        # 리사이즈를 허용하면 내부 UI 폭/높이가 바뀌면서 학생 등록 탭 같은 화면이 잘려 보일 수 있어서
        # 크기는 고정하되, 모니터가 1600x900보다 작으면 창/글자/여백을 같은 비율로 줄여서 전체가 보이게 한다.
        global UI_SCALE
        work_w, work_h = _get_work_area(self.window)
        # 창 테두리와 제목 표시줄이 차지하는 만큼 여유를 둔다
        UI_SCALE = min(1.0, (work_w - 16) / BASE_WIN_W, (work_h - 40) / BASE_WIN_H)
        UI_SCALE = max(UI_SCALE, 0.5)  # 너무 작아져서 글자를 못 읽는 것만 방지

        # 포인트(pt) 단위로 지정한 모든 글꼴이 같은 비율로 줄어들게 한다
        base_tk_scaling = float(self.window.tk.call("tk", "scaling"))
        if IS_MAC:
            # 맥 Tk는 72dpi(1.0) 기준이라 같은 pt 글자가 윈도우(96dpi, 1.333)보다 25% 작게 나온다.
            # 레이아웃이 윈도우 기준 픽셀로 짜여 있으므로 윈도우와 같은 배율로 맞춘다.
            base_tk_scaling = 96 / 72
        self.window.tk.call("tk", "scaling", base_tk_scaling * UI_SCALE)
        # 글꼴을 따로 지정하지 않은 라벨/입력칸이 쓰는 기본 글꼴은 픽셀 단위라 위 설정으로 안 줄어서 직접 줄인다
        for font_name in tkfont.names(self.window):
            named_font = tkfont.nametofont(font_name, root=self.window)
            size = int(named_font.cget("size"))
            if size < 0:
                named_font.configure(size=-max(1, round(-size * UI_SCALE)))

        win_w, win_h = px(BASE_WIN_W), px(BASE_WIN_H)
        pos_x = max(0, (work_w - win_w) // 2)
        pos_y = max(0, (work_h - 40 - win_h) // 2)

        self.window.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        # 최대화(전체 화면) 버튼을 쓸 수 있게 크기 조절은 허용하되, 기본 크기보다 작게 줄이면
        # 오른쪽 패널 내용이 잘리므로 최소 크기를 기본 크기로 막는다. 늘어난 공간은 카메라 화면이 쓴다.
        self.window.resizable(True, True)
        self.window.minsize(win_w, win_h)
        self.window_w, self.window_h = win_w, win_h

        # UI 스타일 테마 통합 구성
        style = ttk.Style()
        style.theme_use('clam')
        
        # Notebook (Tabs) 스타일링
        style.configure('TNotebook', background='#F1F5F9', borderwidth=0)
        style.configure('TNotebook.Tab', background='#E2E8F0', foreground='#475569', padding=[px(15), px(6)], font=(UI_FONT, 10, 'bold'))
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')], foreground=[('selected', '#1E293B')])
        
        # Treeview 스타일링
        style.configure('Treeview', background='#ffffff', fieldbackground='#ffffff', rowheight=px(28), font=(UI_FONT, 9))
        style.configure('Treeview.Heading', background='#E2E8F0', foreground='#1E293B', font=(UI_FONT, 10, 'bold'))
        style.map('Treeview', background=[('selected', '#3B82F6')], foreground=[('selected', '#ffffff')])
        
        # LabelFrame 스타일링
        style.configure('TLabelframe', background='#ffffff', bordercolor='#CBD5E1', borderwidth=1)
        style.configure('TLabelframe.Label', background='#ffffff', foreground='#1E293B', font=(UI_FONT, 10, 'bold'))

        # Ttk Button 스타일링 크기 및 패딩 조절로 해상도 대폭 업그레이드
        style.configure('TButton', font=(UI_FONT, 11, 'bold'), padding=px(8))

        # DB 초기화
        init_db()
        load_timeouts()

        # 창공시스템이 항상 최신 출결 데이터를 읽어갈 수 있도록 시작할 때도 한 번 내보낸다
        export_attendance_json()

        # InsightFace는 별도 프로세스에서 로드/실행한다 (face_inference_process 참고)
        self.face_conn, child_conn = multiprocessing.Pipe()
        self.face_proc = multiprocessing.Process(
            target=face_inference_process, args=(child_conn, INFERENCE_THREADS), daemon=True
        )
        self.face_proc.start()
        self.enrolled_users = load_users()
        self.embed_matrix = None
        self._rebuild_embedding_matrix()

        # 창공시스템 명단(roster.json) — 얼굴 인식 결과에 표시할 이름을 여기서 우선 찾는다
        self.roster_lookup = {}
        self.apply_roster(load_roster())

        # 화면 상태
        #   idle       : 메인 화면. 카메라/얼굴 인식 꺼짐, 출근·퇴근 버튼만 보인다
        #   attendance : 출근/퇴근을 누른 뒤. 카메라가 켜지고 얼굴이 확인되면 자동으로 기록한다
        #   lookup     : 상단 [출석 조회]. 얼굴로 본인 확인만 하고 기록은 하지 않는다
        #   result     : 출근/퇴근 처리 결과 (카메라 꺼짐, 잠시 뒤 메인 화면으로)
        #   stats      : 본인 출석 현황 (카메라 꺼짐, 잠시 뒤 메인 화면으로)
        #   test       : 상단 [카메라 테스트]. 카메라와 인식 결과만 보여주고 기록은 하지 않는다
        #   admin      : 관리자 로그인 후. 학생 등록에 쓰도록 카메라가 켜져 있다
        self.mode = "idle"
        self.admin_logged_in = False
        self.admin_email = ""
        self.pending_action = None      # attendance 모드에서 누른 버튼 ("CHECK_IN" / "CHECK_OUT")
        self.session_started = 0.0      # attendance/test 모드에 들어간 시각 (시간 초과 계산용)
        self._session_card = None       # 오른쪽 안내 카드 (AttendanceWaitingFrame / CameraTestFrame)
        self._candidate_id = None       # 지금 인식되고 있는 학생 — RECOGNIZE_HOLD_SECONDS 동안 유지돼야 기록
        self._candidate_since = 0.0
        self.ai_ready = False           # 얼굴 인식 모델 로딩이 끝났는지
        self.ai_failed = False          # 모델을 받지 못했거나 인식 프로세스가 죽었는지
        self.camera_on = False          # 캡처 스레드가 이 값에 맞춰 카메라 장치를 열고 닫는다
        self.camera_error = False
        self.camera_reopen = False      # True면 캡처 스레드가 카메라를 닫고 camera.txt의 번호로 다시 연다
        self.camera_generation = 0     # 카메라를 켜고 끌 때마다 1씩 증가 — 이전 세션의 늦은 인식 결과를 버리는 데 쓴다

        # 사진/영상 판별용 깜빡임 상태 (인식 프로세스가 깜빡임 값을 보내주면 켜진다)
        self.liveness_enabled = False
        self.last_blink_time = 0.0
        self._eye_closed = False

        # InsightFace 위조판별 애드온 상태 (애드온이 결과를 보내주면 켜진다)
        self.antispoof_enabled = False
        self.last_live_time = 0.0
        self.last_fake_time = 0.0
        self.last_liveness_status = None

        # UI 레이아웃 구성
        self.create_widgets()
        # 입력칸을 누르면 뜨는 화면 키보드 — 라즈베리파이(터치스크린)는 기본으로 켜고, 관리자 화면에서 끄고 켤 수 있다
        self.osk = OnScreenKeyboard(self.window, self.osk_enabled)

        # macOS 카메라 권한은 앱을 켤 때 미리 받아둔다 (처음 한 번만 허용 창이 뜬다).
        # 카메라 장치 자체는 출근/퇴근 버튼을 눌렀을 때 캡처 스레드가 연다.
        ensure_camera_permission()

        # 3개월 지난 출퇴근 사진 정리 — 시작할 때 한 번, 이후 날짜가 바뀔 때마다 (_update_clock)
        self._photo_cleanup_date = date.today()
        threading.Thread(target=cleanup_old_photos, daemon=True).start()

        self.latest_frame = None
        self.frame_seq = 0          # 캡처 스레드가 새 프레임을 받을 때마다 1씩 증가
        self.cached_faces = []
        self.is_running = True

        # 화면 그리기 상태 (PhotoImage를 매 프레임 새로 만들지 않고 재사용한다)
        self._photo = None
        self._rendered_seq = -1
        self._last_render_time = 0.0
        self._fps_count = 0
        self._fps_since = time.perf_counter()

        # 카메라 읽기(cap.read는 다음 프레임이 올 때까지 최대 33ms 블로킹)와 인식 결과 주고받기는
        # 각자 백그라운드 스레드에서 돌리고, UI 스레드는 그리기만 한다
        self.capture_thread = threading.Thread(target=self.capture_worker, daemon=True)
        self.capture_thread.start()
        self.ai_thread = threading.Thread(target=self.ai_worker, daemon=True)
        self.ai_thread.start()

        self.show_idle()
        self.update_video()
        self._update_clock()
        if self.kiosk:
            # 창이 다 뜬 뒤부터 확인한다 (만드는 도중에 닫으면 오류가 난다)
            self.window.after(1000, self._check_operating_hours)

    def create_widgets(self):
        # 상단 헤더 — 좌측: 로고 · 제목 / 우측: 날짜·시간, [카메라 테스트] [출석 조회] [관리자]
        header = tk.Frame(self.window, bg=PANEL, height=px(84))
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)
        tk.Frame(self.window, bg=LINE, height=1).pack(fill=tk.X, side=tk.TOP)

        header_inner = tk.Frame(header, bg=PANEL)
        header_inner.pack(fill=tk.BOTH, expand=True, padx=px(28))

        logo = ImageTk.PhotoImage(compose_center(
            render_round_rect(px(50), px(50), px(14), BLUE_SOFT, PANEL), render_icon("face", px(28), BLUE)))
        lbl_logo = tk.Label(header_inner, image=logo, bg=PANEL, bd=0, cursor="hand2")
        lbl_logo.image = logo
        lbl_logo.pack(side=tk.LEFT)

        title_box = tk.Frame(header_inner, bg=PANEL)
        title_box.pack(side=tk.LEFT, padx=(px(14), 0))
        lbl_title = tk.Label(title_box, text="창의공간 얼굴 인식 출석체크 시스템", bg=PANEL, fg=TEXT,
                             font=(UI_FONT, 17, "bold"), cursor="hand2")
        lbl_title.pack(anchor=tk.W)
        # 로고·제목을 누르면 어느 화면에서든 메인 화면으로 (관리자 화면이면 로그아웃)
        for widget in (lbl_logo, lbl_title):
            widget.bind("<ButtonRelease-1>", lambda e: self.go_home())

        nav = dict(height=px(44), bg=PANEL, font=(UI_FONT, 11, "bold"), padx=px(18))
        # 로그인 후에는 [관리자 로그아웃]으로 바뀐다
        self.btn_admin_header = RoundButton(header_inner, text="관리자", command=self.on_admin_header_button, **nav)
        self.btn_admin_header.pack(side=tk.RIGHT, padx=(px(6), 0))
        # 관리자 화면에서는 숨기는 버튼 묶음
        self.header_tools = tk.Frame(header_inner, bg=PANEL)
        self.header_tools.pack(side=tk.RIGHT, padx=(px(16), 0))
        # 얼굴 인식이 안 될 때 카메라가 제대로 나오는지 확인하는 용도 (기록은 하지 않는다)
        self.btn_camera_test = RoundButton(self.header_tools, text="카메라 테스트", command=self.start_camera_test,
                                           **nav)
        self.btn_camera_test.pack(side=tk.LEFT)
        # 얼굴로 본인 확인 후 이번주 출근 시간을 본다 (출퇴근 기록 없이)
        self.btn_lookup = RoundButton(self.header_tools, text="출석 조회", command=self.start_lookup, **nav)
        self.btn_lookup.pack(side=tk.LEFT, padx=(px(6), 0))
        self._update_nav()

        clock_font = (UI_FONT, 13, "bold")
        self.lbl_clock = RoundButton(
            header_inner, text="0000-00-00  00:00:00", height=px(44), font=clock_font, bg=PANEL,
            fill=PANEL_ALT, fg=TEXT, padx=px(16), icon=render_icon("clock", px(18), TEXT_SUB),
            width=tkfont.Font(font=clock_font).measure("0000-00-00  00:00:00") + px(18 + 10 + 34))
        self.lbl_clock.pack(side=tk.RIGHT)

        # 메인 콘텐츠 컨테이너 — 메인 / 확인 중(카드) / 카메라 화면을 번갈아 보여준다
        content_frame = tk.Frame(self.window, bg=BG)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=px(24), pady=px(22))
        self._build_idle_view(content_frame)

        # 출근/퇴근·출석 조회 확인 중, 결과, 출석 현황 — 카메라 영상 없이 카드만 가운데에 보여준다
        self.checking_view = tk.Frame(content_frame, bg=BG)

        # 카메라 화면 — 카메라 테스트 / 관리자 화면에서만 영상이 보인다
        self.session_view = tk.Frame(content_frame, bg=BG)

        # 좌측: 카메라 패널 (상단 상태바 + 영상 영역)
        self.main_camera_view = CameraView(self.session_view)
        self.camera_panel = self.main_camera_view.panel
        self.camera_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.attach_camera_view(self.main_camera_view)

        # 우측: 가변 상태 패널 컨테이너 (카메라 테스트 안내 / 관리자 화면)
        self.right_container = tk.Frame(self.session_view, width=px(RIGHT_PANEL_WIDTH), bg=BG)
        self.right_container.pack(side=tk.RIGHT, fill=tk.Y, expand=False, padx=(px(20), 0))
        self.right_container.pack_propagate(False)

    def _build_idle_view(self, parent):
        """메인 화면 — 카메라는 꺼져 있고 출근/퇴근 버튼만 가운데에 보여준다."""
        self.idle_view = tk.Frame(parent, bg=BG)

        center = tk.Frame(self.idle_view, bg=BG)
        center.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # 출근/퇴근 카드 — 관리자 화면에서 크기를 바꾸면 이 안만 다시 만든다
        self._action_cards = tk.Frame(center, bg=BG)
        self._action_cards.pack()
        self._build_action_cards()

        # 얼굴 인식 준비 상태 — 처음 실행하면 모델(약 300MB)을 받느라 몇 분 걸린다 (_update_clock에서 갱신)
        self.lbl_ai_status = tk.Label(center, text="", font=(UI_FONT, 11, "bold"), bg=BG, fg=WARN,
                                      justify=tk.CENTER)
        self.lbl_ai_status.pack(pady=(px(14), 0))

    def main_button_size(self):
        """메인 화면 버튼 크기 설정 (MAIN_BUTTON_SIZES의 키). 저장된 값이 이상하면 기본값."""
        key = load_settings().get("main_button_size", DEFAULT_MAIN_BUTTON_SIZE)
        return key if key in MAIN_BUTTON_SIZES else DEFAULT_MAIN_BUTTON_SIZE

    def osk_enabled(self):
        """화면 키보드를 쓸지. 저장된 값이 없으면 라즈베리파이(터치스크린 키오스크)에서만 켠다."""
        return bool(load_settings().get("on_screen_keyboard", IS_RASPBERRY_PI))

    def set_osk_enabled(self, enabled):
        save_setting("on_screen_keyboard", bool(enabled))
        if not enabled:
            self.osk.hide()

    def set_main_button_size(self, key):
        """관리자 화면에서 고른 크기를 저장하고 메인 화면 카드를 바로 다시 만든다."""
        save_setting("main_button_size", key)
        self._build_action_cards()

    def _build_action_cards(self):
        for widget in self._action_cards.winfo_children():
            widget.destroy()
        # 기본 디자인(470x340)에 배율을 곱한다 — 카드 안 글자·아이콘도 같은 비율로 커진다
        card_scale = MAIN_BUTTON_SIZES[self.main_button_size()][1]
        card_w, card_h, gap = px(470 * card_scale), px(340 * card_scale), px(24 * card_scale)
        for i, log_type in enumerate(("CHECK_IN", "CHECK_OUT")):
            ActionCard(self._action_cards, log_type, lambda t=log_type: self.start_attendance(t),
                       card_w, card_h, scale=card_scale) \
                .pack(side=tk.LEFT, padx=(0 if i == 0 else gap, 0))

    def capture_worker(self):
        """
        camera_on에 맞춰 카메라를 열고 닫으며, 켜져 있는 동안 최신 프레임 1장만 보관한다.
        카메라는 이 스레드에서만 다룬다 (읽는 도중에 다른 스레드가 release하면 드라이버가 멈출 수 있다).
        꺼져 있을 때는 장치를 완전히 닫아서 카메라 불도 꺼진다.
        """
        cap = None
        last_frame_time = 0.0
        while self.is_running:
            try:
                if self.camera_reopen:  # 관리자 화면에서 다른 카메라를 골랐다 — 닫고 새 번호로 다시 연다
                    self.camera_reopen = False
                    if cap is not None:
                        cap.release()
                        cap = None

                if not self.camera_on:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(0.05)
                    continue

                if cap is None:
                    # 실제로 영상이 나오는 장치를 찾는다 (USB가 다시 붙으며 번호가 바뀐 경우까지)
                    cap = open_working_camera(640, 480, CAMERA_FPS)
                    if cap is None:
                        self.camera_error = True
                        time.sleep(1.0)  # 다른 프로그램이 놓아줄 수도 있으니 켜져 있는 동안 1초마다 다시 시도
                        continue
                    self.camera_error = False
                    last_frame_time = time.time()

                ret, frame = cap.read()
                if not ret:
                    # 과부하·전원 부족으로 USB 카메라가 잠깐 끊기면 읽기가 계속 실패한 채로 멈춘다.
                    # 예전에는 여기서 영영 재시도만 해서 프로그램을 껐다 켜야 했다 — 닫고 다시 연결한다.
                    if time.time() - last_frame_time > CAMERA_STALL_SECONDS:
                        print(f"[출석] 카메라에서 {CAMERA_STALL_SECONDS:.0f}초간 영상이 오지 않아 다시 연결합니다.")
                        cap.release()
                        cap = None
                        self.camera_error = True
                    else:
                        time.sleep(0.02)
                    continue
                last_frame_time = time.time()
                if self.camera_on:  # 읽는 사이에 꺼졌다면 버린다
                    self.latest_frame = cv2.flip(frame, 1)
                    self.frame_seq += 1
            except Exception as e:
                # 여기서 스레드가 죽으면 카메라를 영영 못 쓴다 — 장치를 닫고 다음 바퀴에 새로 연다
                print(f"[출석] 카메라 처리 중 오류가 있었지만 계속 진행합니다 ({type(e).__name__}: {e})")
                if cap is not None:
                    cap.release()
                    cap = None
                self.camera_error = True
                time.sleep(1.0)

        if cap is not None:
            cap.release()

    def ai_worker(self):
        """최신 프레임을 인식 프로세스로 보내고 결과를 받아온다 (한 번에 1장씩만 보내서 밀리지 않게)."""
        try:
            self.face_conn.recv()  # 인식 프로세스의 모델 로딩 완료 신호
            self.ai_ready = True
            last_seq = -1
            while self.is_running:
                frame = self.latest_frame
                seq = self.frame_seq
                generation = self.camera_generation
                # 같은 프레임을 두 번 분석하지 않는다 (관리자 탭에서도 학생 등록용 스캔은 계속 유지)
                if self.camera_on and frame is not None and seq != last_seq:
                    last_seq = seq
                    started = time.time()
                    self.face_conn.send(frame)
                    # 마지막 값(고개 방향)은 웹 버전의 고개 돌리기 확인용 — 이 화면은 눈 깜빡임을 쓴다
                    results, blink_score, liveness, _pose = self.face_conn.recv()
                    # 느린 기기(라즈베리파이)에서는 분석 사이에 잠깐 쉬어서 카메라·화면이 CPU를 쓸 틈을 준다
                    rest = AI_MIN_INTERVAL - (time.time() - started)
                    if rest > 0:
                        time.sleep(rest)
                    # 분석하는 사이에 카메라를 껐다 켰다면(다음 사람) 예전 얼굴 결과는 버린다
                    if self.camera_on and generation == self.camera_generation:
                        self.cached_faces = [DetectedFace(bbox, emb) for bbox, emb in results]
                        self._update_blink_state(blink_score)
                        self._update_liveness_state(liveness)
                else:
                    time.sleep(0.005)
        except (EOFError, OSError):
            # 프로그램 종료로 닫힌 경우가 아니면, 모델을 받지 못했거나 인식 프로세스가 죽은 것이다
            if self.is_running:
                self.ai_failed = True

    def _update_blink_state(self, blink_score):
        """
        눈을 감았다가(BLINK_CLOSED_SCORE 이상) 다시 떴을 때(BLINK_OPEN_SCORE 이하)
        한 번 깜빡인 것으로 보고 시각을 기록한다.
        사진은 눈을 감지 못하므로 이 시각이 갱신되지 않는다.
        """
        if blink_score is None:
            return  # 얼굴을 못 찾았거나 깜빡임 검사가 꺼져 있음

        self.liveness_enabled = True
        if blink_score >= BLINK_CLOSED_SCORE:
            self._eye_closed = True
        elif self._eye_closed and blink_score <= BLINK_OPEN_SCORE:
            self._eye_closed = False
            self.last_blink_time = time.time()

    def has_recent_blink(self):
        """최근 BLINK_VALID_SECONDS 안에 눈을 깜빡였는지. 검사가 꺼져 있으면 항상 통과."""
        if not self.liveness_enabled:
            return True
        return (time.time() - self.last_blink_time) <= BLINK_VALID_SECONDS

    def _update_liveness_state(self, liveness):
        """
        InsightFace 위조판별 애드온 결과를 기록한다.
        liveness = (is_live, status) 또는 None(애드온 없음/얼굴 없음)
        """
        if liveness is None:
            return

        is_live, status = liveness
        self.antispoof_enabled = True
        self.last_liveness_status = status
        if status == "ok":
            if is_live:
                self.last_live_time = time.time()
            else:
                self.last_fake_time = time.time()

    def passes_antispoof(self):
        """
        최근 LIVENESS_VALID_SECONDS 안에 '실제 사람' 판정이 있었는지.
        애드온이 없으면 항상 통과(이때는 깜빡임 검사가 대신 막는다).
        """
        if not self.antispoof_enabled:
            return True
        return (time.time() - self.last_live_time) <= LIVENESS_VALID_SECONDS

    def liveness_reject_message(self):
        """위조판별에서 막혔을 때 사용자에게 보여줄 안내 문구."""
        if (time.time() - self.last_fake_time) <= LIVENESS_VALID_SECONDS:
            return ("사진이나 화면으로 판단됩니다.\n"
                    "본인이 직접 카메라 앞에 서서 다시 시도해주세요.")
        if self.last_liveness_status == "input_rejected":
            return ("얼굴이 너무 크게 잡혀 확인할 수 없습니다.\n"
                    "카메라에서 조금 물러난 뒤 다시 시도해주세요.")
        return ("본인 확인을 하지 못했습니다.\n"
                "카메라를 정면으로 바라본 뒤 다시 시도해주세요.")

    def update_video(self):
        frame = self.latest_frame
        seq = self.frame_seq

        # 새 프레임이 들어왔을 때만 그린다 — 같은 프레임을 다시 그리는 CPU 낭비 방지.
        # 느린 기기는 초당 DISPLAY_FPS번까지만 그린다 (화면 그리기가 라즈베리파이 과부하의 큰 원인이었다)
        now = time.time()
        render_due = DISPLAY_FPS is None or now - self._last_render_time >= 1.0 / DISPLAY_FPS
        if self.camera_on and frame is not None and seq != self._rendered_seq and render_due:
            self._rendered_seq = seq
            self._last_render_time = now
            self._render_frame(frame)
        elif self.camera_on and self.camera_error and self._photo is not None:
            # 영상이 나오다 끊긴 경우 — 다시 연결하는 동안 마지막 화면은 두고 상태만 알린다
            if self.lbl_cam_status.cget("text") != "● 카메라 다시 연결 중...":
                self.lbl_cam_status.config(text="● 카메라 다시 연결 중...", fg="#FBBF24")
        elif self.camera_on and self.camera_error and self._photo is None:
            msg = "📷\n\n카메라를 열 수 없습니다\n다른 프로그램이 카메라를 쓰고 있는지 확인하세요"
            if IS_MAC:
                msg += "\n\n시스템 설정 > 개인정보 보호 및 보안 > 카메라에서\n실행한 앱(터미널 등)을 허용한 뒤 다시 실행하세요"
            if self.video_label.cget("text") != msg:
                self.video_label.config(image="", text=msg, fg="#F87171", font=(UI_FONT, 13, "bold"))
                self.lbl_cam_status.config(text="● 카메라 오류", fg="#F87171")
            if self._session_card is not None:
                self._session_card.set_state("error", "카메라를 열 수 없습니다",
                                             "관리자에게 알려주세요")

        # 출근/퇴근·카메라 테스트 중 시간이 지나면 메인 화면으로 (카메라도 꺼진다)
        if self.mode in ("attendance", "lookup", "test") and self._session_card is not None:
            limit = TEST_TIMEOUT if self.mode == "test" else timeouts["verify"]
            remaining = limit - (time.time() - self.session_started)
            if remaining <= 0:
                self.show_idle()
            else:
                self._session_card.set_remaining(int(remaining) + 1)

        if self.is_running:
            self.window.after(5, self.update_video)

    def _render_frame(self, frame):
        # 표시 크기로 먼저 줄이고 그 위에 박스/글자를 그린다 (PIL LANCZOS 11ms → cv2 1ms)
        panel_w = self.video_area.winfo_width() - 20
        panel_h = self.video_area.winfo_height() - 20
        src_h, src_w = frame.shape[:2]
        scale = min(panel_w / src_w, panel_h / src_h) if panel_w > 0 and panel_h > 0 else 1.0
        if MAX_DISPLAY_SCALE is not None:
            scale = min(scale, MAX_DISPLAY_SCALE)
        scale = max(scale, 0.1)
        out_w, out_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
        display = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

        best_user = None
        best_sim = -1.0
        # 사각형은 cv2로 그대로 그리고, 글자(한글)는 아래에서 PIL로 한 번에 그린다.
        # cv2.putText는 Hershey 폰트만 지원해서 한글을 못 그려 '?'로 깨져 보이는 문제가 있었다.
        labels_to_draw = []  # (x, y, text, RGB색상)

        # 얼굴 바운딩 박스 렌더링 (등록된 전원과의 유사도를 행렬곱으로 한번에 계산)
        faces = self.cached_faces
        if faces:
            face_embs = np.asarray([f.embedding for f in faces], dtype=np.float32)
            face_norms = np.linalg.norm(face_embs, axis=1, keepdims=True)
            face_norms[face_norms == 0] = 1.0
            face_embs_normed = face_embs / face_norms

            has_users = self.embed_matrix is not None and len(self.embed_matrix) > 0
            if has_users:
                sims = face_embs_normed @ self.embed_matrix.T  # (num_faces, num_users)
                match_idx = np.argmax(sims, axis=1)
                match_sims = sims[np.arange(len(faces)), match_idx]

            for i, face in enumerate(faces):
                x1, y1, x2, y2 = (face.bbox * scale).astype(int)  # 원본 좌표 → 표시 크기 좌표
                sim = float(match_sims[i]) if has_users else -1.0
                user = self.enrolled_users[match_idx[i]] if has_users else None

                if sim >= THRESHOLD and user:
                    # 이름은 창공시스템 명단(roster.json)을 우선 쓴다 — 새로고침하면 바로 반영된다.
                    display_name = self._resolve_display_name(user)
                    label = f"{display_name}  유사도 {sim:.2f}"
                    color_bgr = (74, 163, 22)  # 초록 (#16A34A) — cv2는 BGR 순서
                else:
                    label = f"미등록  유사도 {sim:.2f}" if sim >= 0 else "미등록"
                    color_bgr = (43, 54, 194)  # 빨강 (#C2362B) — cv2는 BGR 순서

                cv2.rectangle(display, (x1, y1), (x2, y2), color_bgr, 3)
                color_rgb = (color_bgr[2], color_bgr[1], color_bgr[0])
                labels_to_draw.append((x1, y1, label, color_rgb))

                if sim > best_sim:
                    best_sim = sim
                    best_user = user

        matched_user = best_user if (best_user and best_sim >= THRESHOLD) else None
        if self.mode in ("attendance", "lookup") and self._session_card is not None:
            self._process_attendance(frame, faces, matched_user)
        elif self.mode == "test" and self._session_card is not None:
            self._process_camera_test(faces, matched_user, best_sim)

        if self.mode in ("attendance", "lookup"):
            return  # 출근/퇴근·출석 조회 중에는 카메라 영상을 화면에 그리지 않는다

        img = Image.fromarray(cv2.cvtColor(display, cv2.COLOR_BGR2RGB))

        # 얼굴마다 이름 · 유사도를 상자 색 띠 위에 흰 글씨로, 한글이 제대로 보이게 PIL로 그린다
        if labels_to_draw:
            draw = ImageDraw.Draw(img)
            font = get_korean_font(px(18))
            pad_x, pad_y = px(8), px(5)
            for box_x, box_y, text, color_rgb in labels_to_draw:
                left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
                tag_w, tag_h = right - left + 2 * pad_x, bottom - top + 2 * pad_y
                tag_x = max(0, min(box_x - 1, img.width - tag_w))
                tag_y = box_y - tag_h if box_y - tag_h >= 0 else box_y  # 위가 잘리면 상자 안쪽에
                draw.rectangle((tag_x, tag_y, tag_x + tag_w, tag_y + tag_h), fill=color_rgb)
                draw.text((tag_x + pad_x - left, tag_y + pad_y - top), text, font=font, fill=(255, 255, 255))

        if self._photo is None:  # 카메라를 켠 뒤 첫 화면 — fps가 계산되기 전에도 LIVE로 바꿔 둔다
            self.lbl_cam_status.config(text="● LIVE", fg="#8FB0FF")

        # PhotoImage는 크기가 같으면 새로 만들지 않고 픽셀만 덮어쓴다 (20ms → 9ms)
        if self._photo is None or (self._photo.width(), self._photo.height()) != img.size:
            self._photo = ImageTk.PhotoImage(image=img)
            self.video_label.configure(image=self._photo)
        else:
            self._photo.paste(img)

        self._update_fps_label()

    def _update_fps_label(self):
        self._fps_count += 1
        now = time.perf_counter()
        elapsed = now - self._fps_since
        if elapsed >= 1.0:
            self.lbl_cam_status.config(text=f"● LIVE  {self._fps_count / elapsed:.0f} fps", fg="#8FB0FF")
            self._fps_count = 0
            self._fps_since = now
            self._update_liveness_label()

    def _update_liveness_label(self):
        """실제 사람으로 확인된 상태인지 카메라 상태바에 표시한다."""
        if self.antispoof_enabled and (time.time() - self.last_fake_time) <= LIVENESS_VALID_SECONDS:
            self.lbl_liveness.config(text="·  사진/화면으로 판단됨", fg="#F87171")
        elif not (self.antispoof_enabled or self.liveness_enabled):
            self.lbl_liveness.config(text="")
        elif self.passes_antispoof() and self.has_recent_blink():
            self.lbl_liveness.config(text="·  사람 확인됨 (눈 깜빡임 확인)", fg="#86EFAC")
        elif not self.has_recent_blink():
            self.lbl_liveness.config(text="·  눈을 천천히 감았다 떠 주세요", fg="#94A3B8")
        else:
            self.lbl_liveness.config(text="·  카메라를 바라봐 주세요", fg="#94A3B8")

    def _process_attendance(self, frame, faces, matched_user):
        """
        출근/퇴근을 누른 뒤 매 프레임 불린다. 같은 학생이 RECOGNIZE_HOLD_SECONDS 동안 계속 인식되고
        사람 확인(위조판별 + 눈 깜빡임)까지 통과하면 그 순간 화면을 사진으로 남기고 자동으로 기록한다.
        """
        card = self._session_card
        if not self.ai_ready:
            card.set_state("loading", "얼굴 인식 준비 중..")
            return
        if matched_user is None:
            self._candidate_id = None
            if faces:
                card.set_state("warn", "등록되지 않은 얼굴입니다")
            else:
                card.set_state("search", "얼굴을 찾는 중..")
            return

        # 학생을 찾으면 "OOO 학생 얼굴 확인 중" + 눈 깜빡이는 얼굴을 보여준다.
        # 깜빡임은 최근 BLINK_VALID_SECONDS 안의 것을 인정하므로 확인 중에 미리 깜빡여도 된다.
        name = self._resolve_display_name(matched_user)
        checking = f"{name} 학생 얼굴 확인 중"
        now = time.time()
        if self._candidate_id != matched_user["user_id"]:
            self._candidate_id = matched_user["user_id"]
            self._candidate_since = now
        if now - self._candidate_since < RECOGNIZE_HOLD_SECONDS:
            card.set_state("blink", checking, "눈을 천천히 감았다 떠 주세요")
            return
        if not self.passes_antispoof():
            if (now - self.last_fake_time) <= LIVENESS_VALID_SECONDS:
                card.set_state("error", "사진이나 화면으로 판단됩니다", "본인이 직접 카메라 앞에 서 주세요")
            elif self.last_liveness_status == "input_rejected":
                card.set_state("warn", "얼굴이 너무 가깝습니다", "카메라에서 조금 물러나 주세요")
            else:
                card.set_state("warn", checking, "카메라를 정면으로 바라봐 주세요")
            return
        if not self.has_recent_blink():
            card.set_state("blink", checking, "눈을 천천히 감았다 떠 주세요")
            return

        # 확인 완료 — 출근/퇴근이면 기록한다 (출석 조회는 조회만). 사진은 남기지 않는다.
        # 카드를 먼저 비워서 두 번 처리되지 않게 한다.
        self._session_card = None
        student = dict(matched_user)
        student["name"] = name  # 최신 창공시스템 명단 이름 우선
        self.window.after_idle(lambda: self.complete_attendance(student))

    def complete_attendance(self, student):
        if self.mode == "lookup":
            self.show_stats(student)  # 기록·사진 없이 본인 출석 현황만
            return
        log_type = self.pending_action
        success, detail = log_attendance(student["user_id"], student["name"], log_type)
        self.show_result(student, log_type, success, detail)  # 카메라를 끄고 결과를 3초 동안 보여준다

    def _process_camera_test(self, faces, matched_user, best_sim):
        card = self._session_card
        if not self.ai_ready:
            card.set_state("loading", "얼굴 인식 준비 중..")
        elif matched_user is not None:
            card.set_state("ok", f"{self._resolve_display_name(matched_user)} 학생 인식",
                           f"유사도 {best_sim:.2f}",
                           round(best_sim, 2))
        elif faces:
            card.set_state("unknown", "등록되지 않은 얼굴입니다",
                           f"유사도 {max(best_sim, 0):.2f}", round(max(best_sim, 0), 2))
        else:
            card.set_state("search", "얼굴을 찾는 중..")

    def set_camera(self, on):
        """카메라와 얼굴 인식을 켜고 끈다. 실제 장치 열기/닫기는 캡처 스레드가 한다."""
        if on == self.camera_on:
            return
        self.camera_generation += 1
        self.camera_on = on
        self.latest_frame = None
        self.cached_faces = []  # 꺼지거나 새로 켜면 예전 얼굴로 출결/등록이 되지 않게 비운다
        self._photo = None
        self._fps_count = 0
        self._fps_since = time.perf_counter()
        self.lbl_liveness.config(text="")
        if on:
            self.camera_error = False
            self.lbl_cam_status.config(text="● 카메라 켜는 중", fg="#FBBF24")
            self.video_label.config(image="", text="카메라를 켜는 중입니다...",
                                    fg="#8A94A8", font=(UI_FONT, 14, "bold"))
        else:
            self.lbl_cam_status.config(text="● OFF", fg="#F87171")
            self.video_label.config(image="", text="")

    def _reset_liveness(self):
        """다음 사람이 앞 사람의 깜빡임/실제 사람 판정을 이어받지 않도록 비운다."""
        self.last_blink_time = 0.0
        self._eye_closed = False
        self.last_live_time = 0.0
        self.last_fake_time = 0.0
        self.last_liveness_status = None

    def _update_clock(self):
        if not self.is_running:
            return
        now = datetime.now()
        self.lbl_clock.set_text(now.strftime("%Y-%m-%d  %H:%M:%S"))
        if self.ai_failed:
            ai_text, ai_color = ("얼굴 인식을 시작하지 못했습니다.\n"
                                 "인터넷 연결을 확인하고 프로그램을 다시 실행하세요. (logs\\app.log 참고)"), ERR
        elif not self.ai_ready:
            ai_text, ai_color = ("얼굴 인식을 준비하고 있습니다...\n"
                                 "처음 실행하면 모델(약 300MB)을 받느라 몇 분 걸립니다."), WARN
        else:
            ai_text, ai_color = "", WARN
        if self.lbl_ai_status.cget("text") != ai_text:
            self.lbl_ai_status.config(text=ai_text, fg=ai_color)
        # 프로그램을 며칠씩 켜두어도 3개월 지난 사진이 지워지도록 날짜가 바뀌면 한 번 정리한다
        if now.date() != self._photo_cleanup_date:
            self._photo_cleanup_date = now.date()
            threading.Thread(target=cleanup_old_photos, daemon=True).start()
        self.window.after(1000, self._update_clock)

    def _check_operating_hours(self):
        """
        무인 운영(--kiosk)이면 운영 시간(06:00~24:00)이 끝나는 자정에 종료한다.
        관리자가 작업 중이면 로그아웃할 때까지 기다리고, 새벽에 직접 켠 경우(점검용)는 끄지 않는다.
        """
        if not self.is_running:
            return
        now = datetime.now()
        in_hours = now.strftime("%H:%M:%S") >= VALID_ATTENDANCE_START
        if in_hours:
            self._seen_operating_hours = True
        elif getattr(self, "_seen_operating_hours", False) and self.mode != "admin":
            print(f"[출석] {now:%Y-%m-%d %H:%M:%S} 운영 시간이 끝나 종료합니다.")
            self.on_close()
            return
        self.window.after(1000, self._check_operating_hours)

    def _rebuild_embedding_matrix(self):
        # 정규화된 임베딩을 미리 쌓아두면 프레임마다 norm을 다시 계산하지 않고
        # 행렬곱 한 번으로 전체 등록자와의 유사도를 구할 수 있다.
        if not self.enrolled_users:
            self.embed_matrix = np.empty((0, 512), dtype=np.float32)
            return
        mat = np.stack([u["embedding"] for u in self.enrolled_users]).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.embed_matrix = mat / norms

    def reload_users(self):
        self.enrolled_users = load_users()
        self._rebuild_embedding_matrix()

    def apply_roster(self, roster):
        """창공시스템 명단(roster.json)을 얼굴 인식 이름 표시용 조회표로 반영한다."""
        self.roster_lookup = {
            str(s.get("StudentId", "")).strip(): s
            for s in roster
            if str(s.get("StudentId", "")).strip()
        }

    def _resolve_display_name(self, user):
        """
        얼굴 인식된 사용자의 표시 이름.
        창공시스템 명단(roster.json, 최신)을 우선 쓰고, 거기 없으면 등록 당시 이름,
        그마저 없으면 '?' 로 표시해서 최소한 빈 문자열로 비어 보이지 않게 한다.
        """
        roster_entry = self.roster_lookup.get(str(user.get("user_id", "")).strip())
        if roster_entry and roster_entry.get("Name"):
            return roster_entry["Name"]
        if user.get("name"):
            return user["name"]
        return "?"

    # ---------- 화면 전환 ----------
    def _show_view(self, view):
        """메인 / 카드(확인 중·결과·출석 현황) / 카메라 화면 중 하나만 보이게 한다."""
        for other in (self.idle_view, self.checking_view, self.session_view):
            if other is not view:
                other.pack_forget()
        view.pack(fill=tk.BOTH, expand=True)

    def _show_session_layout(self):
        self._show_view(self.session_view)

    def _clear_session_cards(self):
        self.clear_right_container()
        for widget in self.checking_view.winfo_children():
            widget.destroy()

    def _show_card(self, card, size):
        """카드 하나를 콘텐츠 영역 가운데에 띄운다 (카메라 영상 없이)."""
        width, height = (px(v) for v in size)
        card.place(relx=0.5, rely=0.5, anchor=tk.CENTER, width=width, height=height)
        self._show_view(self.checking_view)

    def _update_nav(self):
        """상단 버튼 중 지금 화면에 해당하는 버튼을 파랗게 칠한다."""
        active = {"test": self.btn_camera_test, "lookup": self.btn_lookup, "stats": self.btn_lookup,
                  "admin": self.btn_admin_header}.get(self.mode)
        for btn in (self.btn_camera_test, self.btn_lookup, self.btn_admin_header):
            if btn is active:
                btn.set_style(BLUE, BLUE_DARK, "white")
            else:
                btn.set_style(PANEL, BLUE_SOFT, TEXT_SUB, hover_fg=BLUE)

    def _start_session(self, mode, card_factory):
        self.mode = mode
        self.session_started = time.time()
        self._candidate_id = None
        self._reset_liveness()
        self._clear_session_cards()
        self._session_card = card_factory()
        if mode in ("attendance", "lookup"):
            # 카메라는 뒤에서만 돌고, 화면에는 안내 카드만 보인다
            self._show_card(self._session_card, VERIFY_CARD_SIZE)
        else:
            # 카메라 테스트 — 카메라가 왼쪽 약 2/3, 안내 패널이 오른쪽 1/3
            self._set_camera_panel(True)
            self.right_container.config(width=px(TEST_PANEL_WIDTH))
            self._session_card.pack(fill=tk.BOTH, expand=True)
            self._show_session_layout()
        self._update_nav()
        self.set_camera(True)

    def start_attendance(self, log_type):
        """메인 화면에서 출근/퇴근을 눌렀을 때 — 카메라를 (화면에 띄우지 않고) 켜고 얼굴을 기다린다."""
        self.pending_action = log_type
        self._start_session("attendance", lambda: VerifyCard(self, log_type, self.checking_view,
                                                             *(px(v) for v in VERIFY_CARD_SIZE)))

    def start_lookup(self):
        """상단 [출석 조회] — 얼굴로 본인 확인 후 그 학생의 출석 시간만 보여준다 (기록하지 않는다)."""
        self.pending_action = None
        self._start_session("lookup", lambda: VerifyCard(self, "LOOKUP", self.checking_view,
                                                         *(px(v) for v in VERIFY_CARD_SIZE)))

    def start_camera_test(self):
        """상단 [카메라 테스트] — 기록 없이 카메라 화면과 인식 결과만 보여준다."""
        self.pending_action = None
        self._start_session("test", lambda: CameraTestCard(self, self.right_container, px(TEST_PANEL_WIDTH)))

    def _end_session(self, mode):
        """확인이 끝나면 카메라를 끄고 다음 화면(결과 / 출석 현황)으로 넘어갈 준비를 한다."""
        self.mode = mode
        self.pending_action = None
        self._candidate_id = None
        self._session_card = None
        self.set_camera(False)
        self._clear_session_cards()
        self._update_nav()

    def show_result(self, student, log_type, success, detail):
        """출근/퇴근 처리 결과 — timeouts["result"]초 뒤 저절로 메인 화면으로 돌아간다."""
        self._end_session("result")
        self._show_card(ResultCard(self, student, log_type, success, detail, self.checking_view),
                        RESULT_CARD_SIZE)

    def show_stats(self, student):
        """본인 확인된 학생의 이번주 출석 현황 — timeouts["stats"]초 뒤 저절로 메인 화면으로 돌아간다."""
        self._end_session("stats")
        self._show_card(StatsCard(self, student, self.checking_view, px(STATS_CARD_SIZE[0])), STATS_CARD_SIZE)

    def show_idle(self):
        """메인 화면 — 카메라와 얼굴 인식을 끄고 출근/퇴근 버튼만 보여준다."""
        # 관리자 [학생 등록] 탭의 카메라 패널은 아래에서 관리자 화면과 함께 사라지므로 먼저 기본 패널로 되돌린다
        self.attach_camera_view(self.main_camera_view)
        self.mode = "idle"
        self.pending_action = None
        self._candidate_id = None
        self._session_card = None
        self._clear_session_cards()
        self.set_camera(False)
        self._show_view(self.idle_view)
        self._set_camera_panel(True)
        # 관리자 화면에서 숨겼던 상단 버튼을 원래 순서대로 다시 붙인다 (오른쪽부터: 관리자 · 도구 · 시계)
        if not self.btn_admin_header.winfo_manager():
            self.btn_admin_header.pack(side=tk.RIGHT, padx=(px(6), 0), before=self.lbl_clock)
        if not self.header_tools.winfo_manager():
            self.header_tools.pack(side=tk.RIGHT, padx=(px(16), 0), before=self.lbl_clock)
        self._update_nav()

    def go_home(self):
        """좌측 상단 로고 — 어느 화면에서든 메인 화면으로. 관리자 화면이면 로그아웃한다."""
        if self.admin_logged_in:
            self.set_admin_logged_in(False)
        else:
            self.show_idle()

    def attach_camera_view(self, view):
        """카메라 영상·상태를 그릴 화면을 바꾼다 (기본 카메라 패널 ↔ 관리자 [학생 등록] 탭 안의 패널)."""
        if getattr(self, "_camera_view", None) is view:
            return
        self._camera_view = view
        self.lbl_cam_status = view.lbl_cam_status
        self.lbl_liveness = view.lbl_liveness
        self.video_area = view.video_area
        self.video_label = view.video_label
        self._photo = None          # 새 화면 크기에 맞춰 다음 프레임부터 다시 그린다
        self._rendered_seq = -1

    def _set_camera_panel(self, visible):
        """카메라 패널을 왼쪽에 보이거나(카메라 테스트·학생 등록) 숨기고 오른쪽 패널이 전체 폭을 쓰게 한다."""
        if visible:
            if not self.camera_panel.winfo_manager():
                self.camera_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, before=self.right_container)
            self.right_container.pack_configure(fill=tk.Y, expand=False, padx=(px(20), 0))
        else:
            self.camera_panel.pack_forget()
            self.right_container.pack_configure(fill=tk.BOTH, expand=True, padx=0)

    def show_admin_camera(self, view):
        """
        관리자 화면은 탭이 화면 전체를 쓴다. [학생 등록] 탭이면 그 탭 안의 카메라 패널(view)에 영상을 그리고,
        다른 탭이면(view=None) 카메라를 끈다.
        """
        if self.mode != "admin":
            return
        self._set_camera_panel(False)
        if view is not None:
            self.set_camera(False)  # 그리던 화면을 바꾸기 전에 끄고, 새 패널에서 다시 켠다
            self.attach_camera_view(view)
            self.set_camera(True)
        else:
            self.set_camera(False)
            self.attach_camera_view(self.main_camera_view)

    def on_admin_header_button(self):
        if self.admin_logged_in:
            self.set_admin_logged_in(False)
            return
        AdminLoginDialog(self)

    def set_admin_logged_in(self, logged_in, admin_email=""):
        self.admin_logged_in = logged_in
        self.admin_email = admin_email
        if not logged_in:
            self.show_idle()
            return

        self.mode = "admin"
        self.pending_action = None
        self._session_card = None
        self._clear_session_cards()
        self.right_container.config(width=px(RIGHT_PANEL_WIDTH))
        frame = AdminDashboardFrame(self, admin_email)
        frame.pack(fill=tk.BOTH, expand=True)
        self._show_session_layout()
        self.show_admin_camera(None)  # 첫 탭(학생 현황/수정)은 카메라 없이 전체 폭을 쓴다
        # 관리자 화면 상단에는 시계만 둔다 — 나갈 때는 좌측 상단 로고를 누른다
        self.btn_admin_header.pack_forget()
        self.header_tools.pack_forget()
        self._update_nav()

    def clear_right_container(self):
        for widget in self.right_container.winfo_children():
            widget.destroy()

    def on_close(self):
        self.is_running = False
        self.capture_thread.join(timeout=1.0)  # 카메라는 캡처 스레드가 닫고 끝난다
        self.face_proc.terminate()
        self.window.destroy()


# ==========================================
# 4. 앱 실행
# ==========================================
def run_selftest(app, seconds=10):
    """
    --selftest : 설치 후 자가진단. 카메라 테스트 화면을 seconds초 동안 켜서
    카메라·얼굴 인식·사진 차단 검사가 도는지 logs/selftest.txt 에 적고 종료한다.
    """
    stats = {"frames_start": None, "faces": 0, "blink": False, "liveness": False}
    original = app._update_blink_state

    def spy(score):  # 인식 결과가 올 때마다 불린다
        if app.cached_faces:
            stats["faces"] += 1
        original(score)
    app._update_blink_state = spy

    def start():
        if not app.ai_ready and not app.ai_failed and time.time() - started < 600:
            app.window.after(500, start)  # 처음이면 모델을 받는 중이다 (최대 10분 대기)
            return
        app.start_camera_test()
        stats["frames_start"] = app.frame_seq
        app.window.after(seconds * 1000, finish)

    def finish():
        frames = app.frame_seq - (stats["frames_start"] or 0)
        lines = [
            f"자가진단 {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"얼굴 인식 모델 : {'정상' if app.ai_ready else '실패'}",
            f"카메라         : {'정상' if frames > 0 and not app.camera_error else '실패'} ({frames / seconds:.0f} fps)",
            f"얼굴 감지      : {stats['faces']}회 (카메라 앞에 사람이 있어야 잡힙니다)",
            f"눈 깜빡임 검사 : {'사용' if app.liveness_enabled else '확인 안 됨 (얼굴이 없었거나 모델 없음)'}",
            f"위조판별       : {'사용' if app.antispoof_enabled else '확인 안 됨 (얼굴이 없었거나 애드온 없음)'}",
        ]
        ok = app.ai_ready and frames > 0 and not app.camera_error
        lines.append("결과           : " + ("정상" if ok else "문제 있음"))
        os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
        with open(os.path.join(APP_DIR, "logs", "selftest.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        app.on_close()

    started = time.time()
    app.window.after(500, start)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # exe로 패키징해도 인식 프로세스가 앱을 다시 띄우지 않게

    # --prepare : 얼굴 인식·위조판별·깜빡임 모델을 미리 내려받고 끝낸다 (설치할 때 한 번)
    if "--prepare" in sys.argv:
        create_face_app()
        if not blink_mode_forced_to_landmark() and create_blink_detector() is not None:
            blink_status = "사용 (mediapipe)"
        elif create_landmark_blink_meter() is not None:
            blink_status = "사용 (얼굴 윤곽 모델)"
        else:
            blink_status = "사용 안 함"
        print(f"[출석] 모델 준비 완료 (깜빡임 검사: {blink_status})")
        sys.exit(0)

    # --list-cameras : 연결된 카메라 번호를 확인한다. exe는 콘솔이 없어서 창으로도 보여준다.
    if "--list-cameras" in sys.argv:
        result = list_cameras()
        message = ("카메라 번호별 확인\n\n" + "\n".join(result) +
                   "\n\n쓸 카메라 번호를 프로그램 폴더의 camera.txt 에 숫자만 적어 저장하세요. (예: 1)")
        print(message)
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showinfo("창의공간 출석체크 - 카메라 확인", message)
        sys.exit(0)

    # --scheduled : 작업 스케줄러/시작프로그램이 띄운 경우. 00:00~06:00이면 켜지 않고, 자정에 스스로 종료한다.
    scheduled = "--scheduled" in sys.argv
    if scheduled and datetime.now().strftime("%H:%M:%S") < VALID_ATTENDANCE_START:
        sys.exit(0)

    # 카메라는 한 프로그램만 쓸 수 있으므로 이미 켜져 있으면 하나 더 띄우지 않는다
    if not acquire_single_instance():
        if not scheduled:
            _root = tk.Tk()
            _root.withdraw()
            messagebox.showinfo("창의공간 출석체크", "출석체크 프로그램이 이미 실행 중입니다.")
        sys.exit(0)

    root = tk.Tk()
    app = AttendanceApp(root, kiosk=scheduled or "--kiosk" in sys.argv)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    if "--selftest" in sys.argv:
        run_selftest(app)
    root.mainloop()