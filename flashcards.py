import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from sm20 import (
    RELEARN_DELAY_MINUTES,
    backfill_sm20_state,
    format_interval_label,
    normalize_sm20_state,
    preview_sm20_review,
    score_sm20_review,
    serialize_sm20_state,
)


STATE_FILENAME = ".flashcards.study_state.json"
DEFAULT_SESSION_SIZE = 20
BATCH_SIZE = 80
DIFFICULTIES = ("Easy", "Medium", "Hard")
DIFFICULTY_ORDER = {name: index for index, name in enumerate(DIFFICULTIES)}
ALL_SUBJECTS = "All subjects"
ALL_DIFFICULTIES = "All difficulties"


class FlashcardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Spaced Repetition Study Library")
        self.root.geometry("1240x820")
        self.root.minsize(1080, 720)

        self.deck_dir = Path.cwd()
        self.state_path = self.deck_dir / STATE_FILENAME
        self.palette = {
            "bg": "#f4efe7",
            "panel": "#fbf6ef",
            "card": "#fffaf4",
            "border": "#d6c9b8",
            "text": "#17323c",
            "muted": "#5f6f75",
            "accent": "#0d6b6e",
            "accent_soft": "#d8ece7",
            "again": "#b24c3d",
            "hard": "#c8892f",
            "good": "#2f7f76",
            "easy": "#527d47",
        }

        self.cards = []
        self.cards_by_id = {}
        self.subjects = []
        self.deck_paths = []
        self.session_queue = []
        self.active_batch_ids = []
        self.active_batch_index = 0
        self.active_batch_total = 0
        self.index = 0
        self.show_answer = False
        self.difficulty_thresholds = (0.0, 0.0)
        self.speech_backend = self.detect_speech_backend()
        self.speech_process = None

        self.subject_var = tk.StringVar(value=ALL_SUBJECTS)
        self.difficulty_var = tk.StringVar(value=ALL_DIFFICULTIES)
        self.session_size_var = tk.StringVar(value=str(DEFAULT_SESSION_SIZE))
        self.status_var = tk.StringVar(value="Scanning JSON decks...")

        self.ui_font = self.pick_font(
            "Avenir Next",
            "Trebuchet MS",
            "Gill Sans",
            "Verdana",
            "Segoe UI",
        )
        self.display_font = self.pick_font(
            "Baskerville",
            "Georgia",
            "Palatino Linotype",
            "Book Antiqua",
            "Times New Roman",
        )

        self.configure_styles()
        self.build_ui()
        self.load_library(refresh_if_empty=False)
        self.restore_state()
        self.update_interface()
        self.bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self.handle_close)

    def pick_font(self, *candidates):
        available = set(tkfont.families(self.root))
        for name in candidates:
            if name in available:
                return name
        return "TkDefaultFont"

    def configure_styles(self):
        self.root.configure(bg=self.palette["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "TCombobox",
            padding=6,
            fieldbackground=self.palette["card"],
            background=self.palette["card"],
        )
        style.configure("TSpinbox", padding=6)
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=self.palette["accent_soft"],
            background=self.palette["accent"],
            bordercolor=self.palette["accent_soft"],
            lightcolor=self.palette["accent"],
            darkcolor=self.palette["accent"],
        )

    def build_ui(self):
        self.shell = tk.Frame(self.root, bg=self.palette["bg"])
        self.shell.pack(fill="both", expand=True, padx=22, pady=18)

        self.build_header()
        self.build_body()
        self.root.bind("<Configure>", self.handle_resize)

    def build_header(self):
        header = tk.Frame(self.shell, bg=self.palette["bg"])
        header.pack(fill="x")

        tk.Label(
            header,
            text="Study Library",
            bg=self.palette["bg"],
            fg=self.palette["text"],
            font=(self.display_font, 26, "bold"),
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Every JSON deck in this folder is loaded, tagged by subject, sorted by difficulty, and scheduled with spaced repetition.",
            bg=self.palette["bg"],
            fg=self.palette["muted"],
            font=(self.ui_font, 11),
        ).pack(anchor="w", pady=(4, 14))

        stats_row = tk.Frame(header, bg=self.palette["bg"])
        stats_row.pack(fill="x")

        self.library_stat = self.build_stat_tile(stats_row, "Library")
        self.filtered_stat = self.build_stat_tile(stats_row, "Filtered")
        self.due_stat = self.build_stat_tile(stats_row, "Due Now")
        self.retention_stat = self.build_stat_tile(stats_row, "Retention")
        self.lapse_stat = self.build_stat_tile(stats_row, "Lapse Rate")

    def build_stat_tile(self, parent, title):
        tile = tk.Frame(
            parent,
            bg=self.palette["panel"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=10,
        )
        tile.pack(side="left", fill="x", expand=True, padx=(0, 10))

        tk.Label(
            tile,
            text=title,
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
        ).pack(anchor="w")

        value = tk.Label(
            tile,
            text="—",
            bg=self.palette["panel"],
            fg=self.palette["text"],
            font=(self.display_font, 16, "bold"),
        )
        value.pack(anchor="w", pady=(4, 0))
        return value

    def build_body(self):
        body = tk.Frame(self.shell, bg=self.palette["bg"])
        body.pack(fill="both", expand=True, pady=(18, 0))

        self.sidebar = tk.Frame(
            body,
            bg=self.palette["panel"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
            padx=18,
            pady=18,
        )
        self.sidebar.pack(side="left", fill="y", padx=(0, 18))
        self.sidebar.configure(width=300)
        self.sidebar.pack_propagate(False)

        self.study_panel = tk.Frame(
            body,
            bg=self.palette["panel"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
            padx=24,
            pady=20,
        )
        self.study_panel.pack(side="left", fill="both", expand=True)

        self.build_sidebar()
        self.build_study_panel()

    def build_sidebar(self):
        tk.Label(
            self.sidebar,
            text="Session Filters",
            bg=self.palette["panel"],
            fg=self.palette["text"],
            font=(self.display_font, 18, "bold"),
        ).pack(anchor="w")

        self.build_sidebar_field("Subject", self.subject_var)
        self.subject_combo = ttk.Combobox(
            self.sidebar,
            textvariable=self.subject_var,
            state="readonly",
            font=(self.ui_font, 11),
        )
        self.subject_combo.pack(fill="x", pady=(0, 14))
        self.subject_combo.bind("<<ComboboxSelected>>", self.refresh_session)

        self.build_sidebar_field("Difficulty", self.difficulty_var)
        self.difficulty_combo = ttk.Combobox(
            self.sidebar,
            textvariable=self.difficulty_var,
            state="readonly",
            values=[ALL_DIFFICULTIES, *DIFFICULTIES],
            font=(self.ui_font, 11),
        )
        self.difficulty_combo.pack(fill="x", pady=(0, 14))
        self.difficulty_combo.bind("<<ComboboxSelected>>", self.refresh_session)

        self.build_sidebar_field("Session Size", self.session_size_var)
        self.session_size_spinbox = ttk.Spinbox(
            self.sidebar,
            from_=5,
            to=BATCH_SIZE,
            increment=5,
            textvariable=self.session_size_var,
            font=(self.ui_font, 11),
        )
        self.session_size_spinbox.pack(fill="x", pady=(0, 18))
        self.session_size_spinbox.bind("<Return>", self.refresh_session)

        self.make_button(
            self.sidebar,
            "Refresh Queue",
            self.refresh_session,
            color=self.palette["accent"],
            pady=(0, 10),
        )
        self.make_button(
            self.sidebar,
            "Rescan JSON Decks",
            self.rescan_decks,
            color="#375b65",
            pady=(0, 10),
        )
        self.make_button(
            self.sidebar,
            "Reset Progress",
            self.reset_progress,
            color=self.palette["again"],
            pady=(0, 18),
        )

        tk.Label(
            self.sidebar,
            text="Library Notes",
            bg=self.palette["panel"],
            fg=self.palette["text"],
            font=(self.display_font, 15, "bold"),
        ).pack(anchor="w")

        self.library_summary_label = tk.Label(
            self.sidebar,
            text="",
            justify="left",
            anchor="nw",
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
            wraplength=250,
        )
        self.library_summary_label.pack(fill="x", pady=(8, 14))

        self.shortcut_label = tk.Label(
            self.sidebar,
            text="Shortcuts\nSpace: flip card\nS: speak current face\nLeft: previous\nRight: skip\n1: Wrong\n2: Right",
            justify="left",
            anchor="nw",
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
            wraplength=250,
        )
        self.shortcut_label.pack(fill="x")

    def build_sidebar_field(self, title, variable):
        tk.Label(
            self.sidebar,
            text=title,
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
        ).pack(anchor="w", pady=(0, 6))

    def build_study_panel(self):
        self.meta_row = tk.Frame(self.study_panel, bg=self.palette["panel"])
        self.meta_row.pack(fill="x")

        self.subject_chip = self.build_chip(self.meta_row)
        self.deck_chip = self.build_chip(self.meta_row)
        self.difficulty_chip = self.build_chip(self.meta_row)
        self.schedule_chip = self.build_chip(self.meta_row)

        self.face_label = tk.Label(
            self.study_panel,
            text="Question",
            bg=self.palette["panel"],
            fg=self.palette["accent"],
            font=(self.ui_font, 12, "bold"),
        )
        self.face_label.pack(anchor="w", pady=(18, 10))

        self.card_frame = tk.Frame(
            self.study_panel,
            bg=self.palette["card"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
            padx=24,
            pady=24,
        )
        self.card_frame.pack(fill="both", expand=True)

        self.card_text = tk.Label(
            self.card_frame,
            text="",
            justify="left",
            anchor="nw",
            bg=self.palette["card"],
            fg=self.palette["text"],
            font=(self.display_font, 22),
            wraplength=760,
        )
        self.card_text.pack(fill="both", expand=True)

        self.card_metrics_label = tk.Label(
            self.study_panel,
            text="",
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
        )
        self.card_metrics_label.pack(anchor="w", pady=(12, 12))

        progress_row = tk.Frame(self.study_panel, bg=self.palette["panel"])
        progress_row.pack(fill="x", pady=(0, 16))

        self.progress_label = tk.Label(
            progress_row,
            text="Session 0 of 0",
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
        )
        self.progress_label.pack(anchor="w")

        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(8, 0))

        navigation_row = tk.Frame(self.study_panel, bg=self.palette["panel"])
        navigation_row.pack(fill="x", pady=(0, 14))

        self.back_button = self.make_button(
            navigation_row,
            "Previous",
            self.previous_card,
            color="#6b7a7f",
            side="left",
            padx=(0, 10),
            width=12,
        )
        self.flip_button = self.make_button(
            navigation_row,
            "Show Answer",
            self.flip_card,
            color=self.palette["accent"],
            side="left",
            padx=(0, 10),
            width=14,
        )
        self.speak_button = self.make_button(
            navigation_row,
            "Speak Face",
            self.speak_current_face,
            color="#7b5a36",
            side="left",
            padx=(0, 10),
            width=12,
        )
        self.skip_button = self.make_button(
            navigation_row,
            "Skip",
            self.skip_card,
            color="#6b7a7f",
            side="left",
            width=12,
        )

        ratings_row = tk.Frame(self.study_panel, bg=self.palette["panel"])
        ratings_row.pack(fill="x")

        self.rate_buttons = {
            "wrong": self.make_button(
                ratings_row,
                f"Wrong\n{RELEARN_DELAY_MINUTES}m",
                lambda: self.rate_card("wrong"),
                color=self.palette["again"],
                side="left",
                padx=(0, 10),
                width=18,
                height=2,
            ),
            "right": self.make_button(
                ratings_row,
                "Right",
                lambda: self.rate_card("right"),
                color=self.palette["good"],
                side="left",
                width=18,
                height=2,
            ),
        }

        self.status_label = tk.Label(
            self.study_panel,
            textvariable=self.status_var,
            bg=self.palette["panel"],
            fg=self.palette["muted"],
            font=(self.ui_font, 10),
            anchor="w",
            justify="left",
        )
        self.status_label.pack(fill="x", pady=(16, 0))

    def build_chip(self, parent):
        chip = tk.Label(
            parent,
            text="",
            bg=self.palette["accent_soft"],
            fg=self.palette["accent"],
            font=(self.ui_font, 10, "bold"),
            padx=10,
            pady=5,
        )
        chip.pack(side="left", padx=(0, 8))
        return chip

    def make_button(
        self,
        parent,
        text,
        command,
        color,
        side=None,
        padx=(0, 0),
        pady=(0, 0),
        width=None,
        height=1,
    ):
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="white",
            activebackground=color,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=(self.ui_font, 11, "bold"),
            cursor="hand2",
            padx=14,
            pady=10,
            width=width,
            height=height,
        )
        if side:
            button.pack(side=side, padx=padx, pady=pady)
        else:
            button.pack(fill="x", pady=pady)
        return button

    def handle_resize(self, event):
        if event.widget is self.root:
            new_width = max(420, self.card_frame.winfo_width() - 36)
            self.card_text.configure(wraplength=new_width)
            self.library_summary_label.configure(wraplength=max(220, self.sidebar.winfo_width() - 40))

    def bind_shortcuts(self):
        self.root.bind("<space>", lambda _event: self.flip_card())
        self.root.bind("s", lambda _event: self.speak_current_face())
        self.root.bind("S", lambda _event: self.speak_current_face())
        self.root.bind("<Left>", lambda _event: self.previous_card())
        self.root.bind("<Right>", lambda _event: self.skip_card())
        self.root.bind("1", lambda _event: self.rate_card("wrong"))
        self.root.bind("2", lambda _event: self.rate_card("right"))

    def detect_speech_backend(self):
        command_map = (
            ("say", ["say"]),
            ("espeak-ng", ["espeak-ng"]),
            ("spd-say", ["spd-say"]),
            ("espeak", ["espeak"]),
        )

        for name, command in command_map:
            if shutil.which(command[0]):
                return {"name": name, "command": command}

        if sys.platform.startswith("win") and shutil.which("powershell"):
            return {"name": "powershell", "command": ["powershell", "-Command"]}

        return None

    def speech_backend_name(self):
        if not self.speech_backend:
            return "Unavailable"
        return self.speech_backend["name"]

    def stop_speech(self):
        if self.speech_process and self.speech_process.poll() is None:
            self.speech_process.terminate()
            try:
                self.speech_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.speech_process.kill()
        self.speech_process = None

    def build_speech_command(self, text):
        if not self.speech_backend:
            return None

        if self.speech_backend["name"] == "powershell":
            escaped = text.replace("'", "''")
            return [
                *self.speech_backend["command"],
                f"Add-Type -AssemblyName System.Speech; "
                f"$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$speaker.Speak('{escaped}')",
            ]

        return [*self.speech_backend["command"], text]

    def speak_current_face(self):
        card = self.current_card()
        if card is None:
            return

        if not self.speech_backend:
            messagebox.showinfo(
                "Speech Unavailable",
                "No supported text-to-speech backend was found on this machine.",
            )
            return

        text = (card["answer"] if self.show_answer else card["question"]).strip()
        if not text:
            self.status_var.set("There is no text on the current card face to read aloud.")
            return

        self.stop_speech()
        try:
            self.speech_process = subprocess.Popen(
                self.build_speech_command(text),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.speech_process = None
            messagebox.showerror("Speech Error", f"Unable to start speech playback.\n\n{exc}")
            return

        face_name = "answer" if self.show_answer else "question"
        self.status_var.set(
            f"Reading the current {face_name} aloud with {self.speech_backend_name()}."
        )

    def parse_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def parse_float(self, value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def parse_timestamp(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def now(self):
        return datetime.now()

    def elapsed_days_for_card(self, card, now=None):
        last_reviewed = self.parse_timestamp(card.get("last_reviewed"))
        if last_reviewed is None:
            return 0.0
        now = now or self.now()
        return max(0.0, (now - last_reviewed).total_seconds() / 86400.0)

    def prettify_deck_name(self, stem):
        name = stem.replace("_", " ").strip()
        replacements = {
            "notes fixed": "Notes Fixed",
            "chemistry: the central science list out the chapters": "Chemistry: The Central Science",
        }
        lowered = name.lower()
        if lowered in replacements:
            return replacements[lowered]
        return re.sub(r"\s+", " ", name).strip().title() if name.islower() else name

    def infer_subject(self, stem):
        name = stem.lower().replace("_", " ")
        if "organic" in name:
            return "Organic Chemistry"
        if "protein" in name and "structure" in name:
            return "Structural Biology"
        if "biochemistry" in name:
            return "Biochemistry"
        if "driving forces" in name:
            return "Physical Chemistry"
        if "modelling" in name or "modeling" in name:
            return "Molecular Modeling"
        if "central science" in name or name.startswith("chemistry"):
            return "General Chemistry"
        if "notes" in name:
            return "Cell and Molecular Biology"
        return self.prettify_deck_name(stem)

    def make_card_id(self, source_path, question):
        digest = hashlib.sha1(f"{source_path.name}\n{question}".encode("utf-8")).hexdigest()
        return digest

    def find_deck_paths(self):
        return sorted(
            path
            for path in self.deck_dir.glob("*.json")
            if not path.name.startswith(".") and not path.name.endswith(".state.json")
        )

    def write_text_atomic(self, path, text):
        path = Path(path)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def load_library(self, refresh_if_empty=True):
        previous_subject = self.subject_var.get()
        previous_difficulty = self.difficulty_var.get()
        previous_session_size = self.session_size_var.get()
        errors = []
        loaded_cards = []

        self.deck_paths = self.find_deck_paths()
        for path in self.deck_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue

            if not isinstance(data, dict):
                errors.append(f"{path.name}: top-level JSON must be an object")
                continue

            for question, value in data.items():
                loaded_cards.append(self.normalize_card(path, question, value))

        self.cards = loaded_cards
        self.cards_by_id = {card["id"]: card for card in self.cards}
        self.recalculate_difficulties()
        self.refresh_subject_choices(previous_subject)

        if previous_difficulty in [ALL_DIFFICULTIES, *DIFFICULTIES]:
            self.difficulty_var.set(previous_difficulty)
        else:
            self.difficulty_var.set(ALL_DIFFICULTIES)

        self.session_size_var.set(previous_session_size or str(DEFAULT_SESSION_SIZE))
        self.save_all_decks()

        if errors:
            messagebox.showwarning("Deck Load Warning", "\n".join(errors))

        if refresh_if_empty and not self.session_queue:
            self.refresh_session()

    def normalize_card(self, source_path, question, payload):
        data = payload if isinstance(payload, dict) else {"answer": payload}
        correct_count = max(0, self.parse_int(data.get("correct_count", 0)))
        missed_count = max(0, self.parse_int(data.get("missed_count", 0)))
        review_count = max(
            correct_count + missed_count,
            self.parse_int(data.get("review_count", correct_count + missed_count)),
        )
        interval_days = max(
            0.0,
            self.parse_float(
                data.get(
                    "interval_days",
                    data.get("sm20", {}).get("next_interval_days", 0.0)
                    if isinstance(data.get("sm20"), dict)
                    else 0.0,
                ),
                0.0,
            ),
        )
        sm20_state = normalize_sm20_state(
            data.get("sm20"),
            difficulty_score=self.parse_float(data.get("difficulty_score", 0.0), 0.0),
            difficulty_label=str(data.get("difficulty") or "Easy"),
            review_count=review_count,
            missed_count=missed_count,
            repetitions=max(0, self.parse_int(data.get("repetitions", 0))),
            interval_days=interval_days,
            correct_count=correct_count,
        )
        last_reviewed = data.get("last_reviewed")
        due_at = data.get("due_at")
        if not due_at and last_reviewed and review_count > 0:
            last_reviewed_dt = self.parse_timestamp(last_reviewed)
            if last_reviewed_dt is not None:
                due_at = (
                    last_reviewed_dt + timedelta(days=max(interval_days, sm20_state["next_interval_days"]))
                ).isoformat(timespec="seconds")
        sm20_state = backfill_sm20_state(
            sm20_state,
            correct_count=correct_count,
            missed_count=missed_count,
            review_count=review_count,
            interval_days=interval_days,
            last_reviewed=last_reviewed,
            due_at=due_at,
            now=self.now(),
        )

        card = {
            "id": self.make_card_id(source_path, question),
            "source_path": str(source_path),
            "question": str(question).strip(),
            "answer": str(data.get("answer", "")).strip(),
            "deck_name": str(data.get("deck_name") or self.prettify_deck_name(source_path.stem)),
            "subject": str(data.get("subject") or self.infer_subject(source_path.stem)),
            "difficulty": str(data.get("difficulty") or "Easy"),
            "difficulty_score": self.parse_float(data.get("difficulty_score", 0.0)),
            "correct_count": correct_count,
            "missed_count": missed_count,
            "review_count": review_count,
            "ease_factor": round(sm20_state["a_factor"], 3),
            "interval_days": max(interval_days, sm20_state["next_interval_days"] if review_count else interval_days),
            "repetitions": max(0, self.parse_int(data.get("repetitions", sm20_state["repetition"]))),
            "lapse_count": max(
                missed_count,
                self.parse_int(data.get("lapse_count", missed_count)),
            ),
            "due_at": due_at,
            "last_reviewed": last_reviewed,
            "manual_review": bool(data.get("manual_review", data.get("save_for_later", False))),
            "sm20": sm20_state,
        }
        return card

    def calculate_base_difficulty_score(self, card):
        question = card["question"]
        answer = card["answer"]
        combined = f"{question} {answer}"
        lowered = combined.lower()
        question_words = len(re.findall(r"\w+", question))
        answer_words = len(re.findall(r"\w+", answer))

        score = question_words * 0.7 + answer_words * 0.55
        if question.lower().startswith(("why", "how", "explain", "describe", "compare")):
            score += 5
        if any(
            token in lowered
            for token in (
                "difference",
                "compare",
                "mechanism",
                "pathway",
                "equation",
                "derive",
                "relationship",
                "energy",
                "thermodynamic",
            )
        ):
            score += 5
        if any(symbol in combined for symbol in ("=", "Δ", "→", "⇌", "±", "°", "(", ")")):
            score += 4
        if "," in answer or ";" in answer:
            score += 2
        if answer_words >= 18:
            score += 4
        if answer_words <= 8 and question.lower().startswith("what is"):
            score -= 4
        return max(0.0, score)

    def calculate_difficulty_score(self, card):
        sm20_state = card["sm20"]
        score = self.calculate_base_difficulty_score(card)
        score += card["missed_count"] * 2.5
        score += card["lapse_count"] * 2.0
        score += sm20_state["difficulty"] * 18
        score += max(0.0, 6.5 - math.log2(max(1.0, sm20_state["stability"]))) * 0.9
        score -= min(card["correct_count"], 8) * 0.35
        if card["manual_review"]:
            score += 2
        if sm20_state["last_result"] == "wrong":
            score += 1.5
        return round(max(0.0, score), 2)

    def percentile(self, values, ratio):
        if not values:
            return 0.0
        index = int((len(values) - 1) * ratio)
        return values[index]

    def label_for_difficulty(self, score):
        easy_cutoff, medium_cutoff = self.difficulty_thresholds
        if score <= easy_cutoff:
            return "Easy"
        if score <= medium_cutoff:
            return "Medium"
        return "Hard"

    def recalculate_difficulties(self):
        if not self.cards:
            self.difficulty_thresholds = (0.0, 0.0)
            return

        scores = sorted(self.calculate_difficulty_score(card) for card in self.cards)
        self.difficulty_thresholds = (
            self.percentile(scores, 0.50),
            self.percentile(scores, 0.80),
        )

        for card in self.cards:
            card["difficulty_score"] = self.calculate_difficulty_score(card)
            card["difficulty"] = self.label_for_difficulty(card["difficulty_score"])

    def refresh_subject_choices(self, preferred_subject=None):
        self.subjects = sorted({card["subject"] for card in self.cards})
        values = [ALL_SUBJECTS, *self.subjects]
        self.subject_combo["values"] = values
        if preferred_subject in values:
            self.subject_var.set(preferred_subject)
        elif self.subject_var.get() in values:
            return
        else:
            self.subject_var.set(ALL_SUBJECTS)

    def serialize_card(self, card):
        return {
            "answer": card["answer"],
            "subject": card["subject"],
            "deck_name": card["deck_name"],
            "difficulty": card["difficulty"],
            "difficulty_score": round(card["difficulty_score"], 2),
            "correct_count": card["correct_count"],
            "missed_count": card["missed_count"],
            "review_count": card["review_count"],
            "ease_factor": round(card["sm20"]["a_factor"], 3),
            "interval_days": round(card["interval_days"], 4),
            "repetitions": card["sm20"]["repetition"],
            "lapse_count": card["lapse_count"],
            "retention_rate": round(self.retention_rate(card) or 0.0, 3),
            "lapse_rate": round(self.lapse_rate(card) or 0.0, 3),
            "due_at": card["due_at"],
            "last_reviewed": card["last_reviewed"],
            "manual_review": card["manual_review"],
            "save_for_later": card["manual_review"],
            "known_pile": False,
            "sm20": serialize_sm20_state(card["sm20"]),
        }

    def save_all_decks(self, target_paths=None):
        grouped = defaultdict(list)
        for card in self.cards:
            if target_paths and card["source_path"] not in target_paths:
                continue
            grouped[card["source_path"]].append(card)

        for source_path, cards in grouped.items():
            ordered_cards = sorted(cards, key=self.save_sort_key)
            data = {
                card["question"]: self.serialize_card(card)
                for card in ordered_cards
            }
            self.write_text_atomic(
                source_path,
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            )

    def save_sort_key(self, card):
        return (
            card["subject"].lower(),
            DIFFICULTY_ORDER.get(card["difficulty"], 99),
            card["deck_name"].lower(),
            card["question"].lower(),
        )

    def batch_sort_key(self, card):
        return self.save_sort_key(card)

    def session_sort_key(self, card):
        due_at = self.parse_timestamp(card["due_at"]) or datetime.min
        due_bucket = 0 if self.is_due(card) else 1
        manual_bucket = 0 if card["manual_review"] else 1
        return (
            manual_bucket,
            DIFFICULTY_ORDER.get(card["difficulty"], 99),
            due_bucket,
            due_at,
            card["subject"].lower(),
            card["question"].lower(),
        )

    def parse_session_size(self):
        size = self.parse_int(self.session_size_var.get(), DEFAULT_SESSION_SIZE)
        size = max(5, min(size, BATCH_SIZE))
        self.session_size_var.set(str(size))
        return size

    def retention_rate(self, card):
        if card["review_count"] <= 0:
            return None
        return card["correct_count"] / card["review_count"]

    def lapse_rate(self, card):
        if card["review_count"] <= 0:
            return None
        return card["lapse_count"] / card["review_count"]

    def aggregate_rate(self, cards, numerator_key):
        total_reviews = sum(card["review_count"] for card in cards)
        if total_reviews <= 0:
            return None
        total_hits = sum(card[numerator_key] for card in cards)
        return total_hits / total_reviews

    def format_percent(self, value):
        if value is None:
            return "—"
        return f"{value * 100:.0f}%"

    def format_due_window(self, due_at):
        if due_at is None:
            return "Due now"
        delta = due_at - self.now()
        if delta.total_seconds() <= 0:
            return "Due now"
        minutes = max(1, int(delta.total_seconds() // 60))
        if minutes < 60:
            return f"in {minutes}m"
        hours = int(delta.total_seconds() // 3600)
        if hours < 24:
            return f"in {hours}h"
        days = max(1, int(round(delta.total_seconds() / 86400)))
        return f"in {days}d"

    def is_due(self, card):
        if card["manual_review"]:
            return True
        due_at = self.parse_timestamp(card["due_at"])
        return due_at is None or due_at <= self.now()

    def filtered_cards(self):
        subject = self.subject_var.get()
        difficulty = self.difficulty_var.get()
        cards = self.cards
        if subject != ALL_SUBJECTS:
            cards = [card for card in cards if card["subject"] == subject]
        if difficulty != ALL_DIFFICULTIES:
            cards = [card for card in cards if card["difficulty"] == difficulty]
        return cards

    def next_due_in_filter(self, cards):
        future_due = sorted(
            due_at
            for due_at in (self.parse_timestamp(card["due_at"]) for card in cards)
            if due_at is not None and due_at > self.now()
        )
        return future_due[0] if future_due else None

    def build_batch_groups(self, cards=None):
        ordered_cards = sorted(cards if cards is not None else self.filtered_cards(), key=self.batch_sort_key)
        card_ids = [card["id"] for card in ordered_cards]
        return [card_ids[index:index + BATCH_SIZE] for index in range(0, len(card_ids), BATCH_SIZE)]

    def cards_from_ids(self, card_ids):
        return [self.cards_by_id[card_id] for card_id in card_ids if card_id in self.cards_by_id]

    def active_batch_cards(self):
        return self.cards_from_ids(self.active_batch_ids)

    def resolve_active_batch(self, allow_advance=True, preferred_card_id=None):
        batches = self.build_batch_groups()
        self.active_batch_total = len(batches)

        if not batches:
            self.active_batch_ids = []
            self.active_batch_index = 0
            return []

        batch_index = None
        active_batch_set = set(self.active_batch_ids)
        if active_batch_set:
            for index, batch_ids in enumerate(batches):
                if active_batch_set == set(batch_ids):
                    batch_index = index
                    break

        if batch_index is None and preferred_card_id:
            for index, batch_ids in enumerate(batches):
                if preferred_card_id in batch_ids:
                    batch_index = index
                    break

        if batch_index is None:
            batch_index = 0

        if allow_advance:
            current_batch_cards = self.cards_from_ids(batches[batch_index])
            if not any(self.is_due(card) for card in current_batch_cards):
                for next_index in range(batch_index + 1, len(batches)):
                    next_batch_cards = self.cards_from_ids(batches[next_index])
                    if any(self.is_due(card) for card in next_batch_cards):
                        batch_index = next_index
                        break

        self.active_batch_ids = list(batches[batch_index])
        self.active_batch_index = batch_index + 1
        return self.cards_from_ids(self.active_batch_ids)

    def build_session_queue(self):
        due_cards = [card for card in self.resolve_active_batch() if self.is_due(card)]
        due_cards.sort(key=self.session_sort_key)
        session_size = self.parse_session_size()
        return [card["id"] for card in due_cards[:session_size]]

    def refresh_session(self, _event=None):
        self.session_queue = self.build_session_queue()
        self.index = 0
        self.show_answer = False
        count = len(self.session_queue)

        if count:
            self.status_var.set(
                f"Loaded {count} due card{'s' if count != 1 else ''} from batch "
                f"{self.active_batch_index}/{self.active_batch_total}."
            )
        else:
            matching_cards = self.filtered_cards()
            active_batch_cards = self.resolve_active_batch(allow_advance=False)
            next_due = self.next_due_in_filter(active_batch_cards or matching_cards)
            if active_batch_cards and next_due:
                self.status_var.set(
                    f"Batch {self.active_batch_index}/{self.active_batch_total} is clear for now. "
                    f"Next card opens {self.format_due_window(next_due)}."
                )
            elif matching_cards and next_due:
                self.status_var.set(f"No cards are due right now. Next card opens {self.format_due_window(next_due)}.")
            elif matching_cards:
                self.status_var.set("No due cards remain in the active batch or filter right now.")
            else:
                self.status_var.set("No cards match the selected subject and difficulty.")

        self.update_interface()
        self.save_state()

    def rescan_decks(self):
        self.session_queue = []
        self.active_batch_ids = []
        self.load_library()
        self.refresh_session()

    def restore_state(self):
        if not self.state_path.exists():
            self.refresh_session()
            return

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.refresh_session()
            return

        session_size = self.parse_int(state.get("session_size"), DEFAULT_SESSION_SIZE)
        self.session_size_var.set(str(session_size))

        subject = state.get("subject", ALL_SUBJECTS)
        if subject in self.subject_combo["values"]:
            self.subject_var.set(subject)

        difficulty = state.get("difficulty", ALL_DIFFICULTIES)
        if difficulty in [ALL_DIFFICULTIES, *DIFFICULTIES]:
            self.difficulty_var.set(difficulty)

        saved_batch = [
            card_id for card_id in state.get("active_batch", [])
            if card_id in self.cards_by_id
        ]
        if saved_batch:
            self.active_batch_ids = saved_batch

        queue = [
            card_id for card_id in state.get("queue", [])
            if card_id in self.cards_by_id
        ]
        if queue:
            self.resolve_active_batch(
                allow_advance=False,
                preferred_card_id=queue[0],
            )
            self.session_queue = queue
            self.index = min(self.parse_int(state.get("index"), 0), len(queue) - 1)
            self.show_answer = bool(state.get("show_answer", False))
            self.status_var.set("Restored your previous study session.")
        else:
            self.refresh_session()

    def save_state(self):
        state = {
            "subject": self.subject_var.get(),
            "difficulty": self.difficulty_var.get(),
            "session_size": self.parse_session_size(),
            "active_batch": self.active_batch_ids,
            "queue": self.session_queue,
            "index": self.index,
            "show_answer": self.show_answer,
        }
        self.write_text_atomic(
            self.state_path,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        )

    def current_card(self):
        if not self.session_queue:
            return None
        self.index = max(0, min(self.index, len(self.session_queue) - 1))
        return self.cards_by_id.get(self.session_queue[self.index])

    def update_interface(self):
        self.update_stats()
        self.update_card_view()
        self.update_navigation_buttons()
        self.update_rating_buttons()
        self.save_state()

    def update_stats(self):
        matching_cards = self.filtered_cards()
        due_cards = [card for card in matching_cards if self.is_due(card)]
        visible_subjects = len({card["subject"] for card in matching_cards})

        self.library_stat.config(text=f"{len(self.cards)} cards\n{len(self.deck_paths)} decks")
        self.filtered_stat.config(text=f"{len(matching_cards)} cards\n{visible_subjects} subjects")
        self.due_stat.config(text=str(len(due_cards)))
        self.retention_stat.config(text=self.format_percent(self.aggregate_rate(matching_cards, "correct_count")))
        self.lapse_stat.config(text=self.format_percent(self.aggregate_rate(matching_cards, "lapse_count")))

        difficulty_summary = defaultdict(int)
        for card in matching_cards:
            difficulty_summary[card["difficulty"]] += 1

        self.library_summary_label.config(
            text=(
                f"Folder: {self.deck_dir}\n\n"
                f"Batch size: {BATCH_SIZE}\n"
                f"Active batch: {self.active_batch_index or 0}/{self.active_batch_total or 0}  "
                f"({len(self.active_batch_cards())} cards)\n"
                f"Session size: {self.parse_session_size()} "
                f"(must be <= {BATCH_SIZE})\n\n"
                f"Difficulty split in current filter\n"
                f"Easy: {difficulty_summary['Easy']}\n"
                f"Medium: {difficulty_summary['Medium']}\n"
                f"Hard: {difficulty_summary['Hard']}\n\n"
                f"Each card keeps its SM20 scheduling state inside the deck JSON files."
            )
        )

    def update_card_view(self):
        card = self.current_card()
        if not card:
            self.face_label.config(text="Queue Clear")
            self.subject_chip.config(text=self.subject_var.get())
            self.deck_chip.config(text=self.difficulty_var.get())
            self.difficulty_chip.config(text="No active card")
            self.schedule_chip.config(text="Ready when due")

            matching_cards = self.filtered_cards()
            next_due = self.next_due_in_filter(matching_cards)
            if not self.cards:
                message = "No JSON flashcards were found in this folder."
            elif not matching_cards:
                message = "No cards match the current subject and difficulty filters."
            elif next_due:
                message = f"No cards are due right now.\nNext review window opens {self.format_due_window(next_due)}."
            else:
                message = "No cards are due right now."

            self.card_text.config(text=message)
            self.card_metrics_label.config(text="Adjust the filters, rescan the folder, or come back when more cards are due.")
            self.progress_label.config(text="Session 0 of 0")
            self.progress_bar.config(maximum=1, value=0)
            self.flip_button.config(text="Show Answer")
            return

        due_at = self.parse_timestamp(card["due_at"])
        due_label = "New card" if card["review_count"] == 0 else self.format_due_window(due_at)
        face = "Answer" if self.show_answer else "Question"

        self.face_label.config(text=face)
        self.subject_chip.config(text=card["subject"])
        self.deck_chip.config(text=card["deck_name"])
        self.difficulty_chip.config(
            text=f"{card['difficulty']} • S {card['sm20']['stability']:.1f} • D {card['sm20']['difficulty']:.2f}"
        )
        self.schedule_chip.config(text=due_label)
        self.card_text.config(text=card["answer"] if self.show_answer else card["question"])

        retention = self.format_percent(self.retention_rate(card))
        lapse = self.format_percent(self.lapse_rate(card))
        retrievability = self.format_percent(card["sm20"]["retrievability"])
        self.card_metrics_label.config(
            text=(
                f"Reviews: {card['review_count']}  |  Retention: {retention}  |  "
                f"Lapse rate: {lapse}  |  SM20 rep: {card['sm20']['repetition']}  |  "
                f"R: {retrievability}  |  Next: {format_interval_label(card['interval_days'])}"
            )
        )

        self.progress_label.config(text=f"Session {self.index + 1} of {len(self.session_queue)}")
        self.progress_bar.config(maximum=max(1, len(self.session_queue)), value=self.index + 1)
        self.flip_button.config(text="Back to Question" if self.show_answer else "Show Answer")

    def update_navigation_buttons(self):
        has_card = self.current_card() is not None
        self.back_button.config(state=tk.NORMAL if has_card and self.index > 0 else tk.DISABLED)
        self.flip_button.config(state=tk.NORMAL if has_card else tk.DISABLED)
        self.speak_button.config(
            state=tk.NORMAL if has_card and self.speech_backend is not None else tk.DISABLED
        )
        self.skip_button.config(state=tk.NORMAL if has_card and len(self.session_queue) > 1 else tk.DISABLED)

    def update_rating_buttons(self):
        card = self.current_card()
        enabled = card is not None and self.show_answer
        previews = self.preview_intervals(card) if enabled else None

        labels = {
            "wrong": "Wrong",
            "right": "Right",
        }

        for rating, button in self.rate_buttons.items():
            if previews:
                button.config(text=f"{labels[rating]}\n{previews[rating]}", state=tk.NORMAL)
            else:
                button.config(text=labels[rating], state=tk.DISABLED)

    def flip_card(self):
        if self.current_card() is None:
            return
        self.show_answer = not self.show_answer
        self.update_interface()

    def previous_card(self):
        if self.index <= 0:
            return
        self.index -= 1
        self.show_answer = False
        self.update_interface()

    def skip_card(self):
        if len(self.session_queue) <= 1:
            return
        card_id = self.session_queue.pop(self.index)
        self.session_queue.append(card_id)
        if self.index >= len(self.session_queue):
            self.index = 0
        self.show_answer = False
        self.status_var.set("Moved the current card to the end of the queue.")
        self.update_interface()

    def preview_intervals(self, card):
        now = self.now()
        elapsed_days = self.elapsed_days_for_card(card, now)
        return {
            "wrong": preview_sm20_review(
                card["sm20"],
                False,
                now=now,
                elapsed_days=elapsed_days,
            )["interval_label"],
            "right": preview_sm20_review(
                card["sm20"],
                True,
                now=now,
                elapsed_days=elapsed_days,
            )["interval_label"],
        }

    def calculate_review_outcome(self, card, rating):
        now = self.now()
        was_correct = rating == "right"
        sm20_outcome = score_sm20_review(
            card["sm20"],
            was_correct,
            now=now,
            elapsed_days=self.elapsed_days_for_card(card, now),
        )
        return {
            "review_count": card["review_count"] + 1,
            "correct_count": card["correct_count"] + (1 if was_correct else 0),
            "missed_count": card["missed_count"] + (0 if was_correct else 1),
            "lapse_count": card["lapse_count"] + (0 if was_correct else 1),
            "repetitions": sm20_outcome["state"]["repetition"],
            "interval_days": sm20_outcome["interval_days"],
            "ease_factor": sm20_outcome["state"]["a_factor"],
            "due_at": sm20_outcome["due_at"],
            "last_reviewed": sm20_outcome["last_reviewed"],
            "manual_review": False,
            "sm20": sm20_outcome["state"],
            "interval_label": sm20_outcome["interval_label"],
            "result": sm20_outcome["result"],
        }

    def rate_card(self, rating):
        card = self.current_card()
        if card is None:
            return
        if not self.show_answer:
            messagebox.showinfo("Reveal The Answer", "Flip the card before grading it.")
            return

        outcome = self.calculate_review_outcome(card, rating)
        for key, value in outcome.items():
            if key not in {"interval_label", "result"}:
                card[key] = value

        card["difficulty_score"] = self.calculate_difficulty_score(card)
        card["difficulty"] = self.label_for_difficulty(card["difficulty_score"])

        self.session_queue.pop(self.index)

        if self.index >= len(self.session_queue) and self.session_queue:
            self.index = len(self.session_queue) - 1
        elif not self.session_queue:
            self.index = 0

        self.show_answer = False
        self.save_all_decks({card["source_path"]})

        if not self.session_queue:
            self.session_queue = self.build_session_queue()

        self.status_var.set(
            f"{outcome['result'].title()} recorded with SM20. Next interval: {outcome['interval_label']}."
        )
        self.update_interface()

    def handle_close(self):
        try:
            self.save_state()
        finally:
            self.stop_speech()
            self.root.destroy()

    def reset_progress(self):
        if not self.cards:
            messagebox.showinfo("Reset Progress", "No deck data is loaded.")
            return

        confirmed = messagebox.askyesno(
            "Reset Progress",
            "Reset counts, SM20 scheduling state, and manual review flags for every card in every JSON deck?",
        )
        if not confirmed:
            return

        for card in self.cards:
            card["correct_count"] = 0
            card["missed_count"] = 0
            card["review_count"] = 0
            card["sm20"] = normalize_sm20_state(
                None,
                difficulty_score=card["difficulty_score"],
                difficulty_label=card["difficulty"],
                review_count=0,
                missed_count=0,
                repetitions=0,
                interval_days=0.0,
                correct_count=0,
            )
            card["ease_factor"] = card["sm20"]["a_factor"]
            card["interval_days"] = 0.0
            card["repetitions"] = 0
            card["lapse_count"] = 0
            card["due_at"] = None
            card["last_reviewed"] = None
            card["manual_review"] = False

        self.recalculate_difficulties()
        self.save_all_decks()
        self.active_batch_ids = []
        self.session_queue = []
        self.refresh_session()
        self.status_var.set("Progress reset across all JSON decks.")


if __name__ == "__main__":
    root = tk.Tk()
    app = FlashcardApp(root)
    root.mainloop()
