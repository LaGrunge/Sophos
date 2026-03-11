"""Multiplayer philosophy dictionary game (Balderdash-style).

Game phases:
  LOBBY        – players join, waiting for /startgame
  SELECTING    – everyone picks unknown words from a random set
  WRITING      – each player writes fake definitions for the chosen words
  GUESSING     – each player guesses which definition is real
  FINISHED     – scores tallied, winner announced
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Set, Tuple

from config import MAX_PLAYERS, MIN_PLAYERS, REQUIRED_COMMON_WORDS, WORDS_PER_ROUND

logger = logging.getLogger(__name__)


class Phase(Enum):
    LOBBY = auto()
    SELECTING = auto()
    WRITING = auto()
    GUESSING = auto()
    FINISHED = auto()


@dataclass
class Player:
    user_id: int
    display_name: str
    score: int = 0
    # SELECTING phase: words this player marked as unknown
    selected_words: Set[str] = field(default_factory=set)
    selection_confirmed: bool = False
    # WRITING phase: {word: fake_definition}
    definitions: Dict[str, str] = field(default_factory=dict)
    # index of the next word to write a definition for
    writing_index: int = 0
    # GUESSING phase: {word: chosen_definition_key}
    guesses: Dict[str, str] = field(default_factory=dict)
    # index of the next word to guess
    guessing_index: int = 0


class Game:
    """State machine for a single game room (not bound to any Telegram chat)."""

    _next_id: int = 1

    def __init__(self, room_name: str, creator_id: int, dictionary: Dict[str, str]) -> None:
        self.room_id: int = Game._next_id
        Game._next_id += 1
        self.room_name = room_name
        self.creator_id = creator_id
        self.dictionary = dictionary
        self.all_titles: List[str] = list(dictionary.keys())
        self.phase: Phase = Phase.LOBBY
        self.players: Dict[int, Player] = {}

        # SELECTING phase
        self.current_word_set: List[str] = []
        self.used_words: Set[str] = set()
        # Accumulated common unknown words across selection rounds
        self.accumulated_common: List[str] = []

        # Words chosen for the game (intersection of unknown words)
        self.game_words: List[str] = []

        # WRITING / GUESSING helpers
        # {word: {def_key: definition_text}}
        # def_key is f"player_{user_id}" or "dictionary"
        self.word_definitions: Dict[str, Dict[str, str]] = {}

    # ── Lobby ─────────────────────────────────────────────────────

    def add_player(self, user_id: int, display_name: str) -> Tuple[bool, str]:
        if self.phase != Phase.LOBBY:
            return False, "Игра уже началась, присоединиться нельзя."
        if user_id in self.players:
            return False, "Ты уже в игре!"
        if len(self.players) >= MAX_PLAYERS:
            return False, f"Максимум игроков: {MAX_PLAYERS}."
        self.players[user_id] = Player(user_id=user_id, display_name=display_name)
        return True, f"{display_name} присоединился! Игроков: {len(self.players)}"

    def can_start(self) -> Tuple[bool, str]:
        if len(self.players) < MIN_PLAYERS:
            return False, f"Нужно минимум {MIN_PLAYERS} игрока. Сейчас: {len(self.players)}."
        return True, ""

    # ── Selecting ─────────────────────────────────────────────────

    def start_selection(self) -> List[str]:
        """Move to SELECTING phase and return a new random word set."""
        self.phase = Phase.SELECTING
        self.accumulated_common.clear()
        for p in self.players.values():
            p.selected_words.clear()
            p.selection_confirmed = False
        return self._generate_word_set()

    def new_word_set(self) -> List[str]:
        """Generate another word set, keeping accumulated common words."""
        for p in self.players.values():
            p.selected_words.clear()
            p.selection_confirmed = False
        return self._generate_word_set()

    def _generate_word_set(self) -> List[str]:
        accumulated_set = set(self.accumulated_common)
        available = [t for t in self.all_titles
                     if t not in self.used_words and t not in accumulated_set]
        if len(available) < WORDS_PER_ROUND:
            self.used_words.clear()
            available = [t for t in self.all_titles if t not in accumulated_set]
        self.current_word_set = random.sample(available, min(WORDS_PER_ROUND, len(available)))
        return self.current_word_set

    def toggle_word(self, user_id: int, word: str) -> bool:
        """Toggle a word in/out of the player's unknown set. Returns new state (True=selected)."""
        p = self.players[user_id]
        if word in p.selected_words:
            p.selected_words.discard(word)
            return False
        else:
            p.selected_words.add(word)
            return True

    def confirm_selection(self, user_id: int) -> None:
        self.players[user_id].selection_confirmed = True

    def all_selected(self) -> bool:
        return all(p.selection_confirmed for p in self.players.values())

    def compute_intersection(self) -> List[str]:
        """Compute intersection for this round, add to accumulated, return total."""
        sets = [p.selected_words for p in self.players.values()]
        if not sets:
            return list(self.accumulated_common)
        common = set.intersection(*sets)
        new_words = [w for w in self.current_word_set if w in common]
        # Mark current round words as used so they don't repeat
        self.used_words.update(self.current_word_set)
        # Accumulate
        self.accumulated_common.extend(new_words)
        return list(self.accumulated_common)

    # ── Writing ───────────────────────────────────────────────────

    def start_writing(self, words: List[str]) -> None:
        """Transition to the WRITING phase with the final word list."""
        self.game_words = words[:REQUIRED_COMMON_WORDS]
        self.used_words.update(self.game_words)
        self.phase = Phase.WRITING
        for p in self.players.values():
            p.writing_index = 0
            p.definitions.clear()

    def current_writing_word(self, user_id: int) -> str | None:
        p = self.players[user_id]
        if p.writing_index >= len(self.game_words):
            return None
        return self.game_words[p.writing_index]

    def submit_definition(self, user_id: int, definition: str) -> str | None:
        """Save definition, advance index. Returns next word or None if done."""
        p = self.players[user_id]
        word = self.game_words[p.writing_index]
        p.definitions[word] = definition
        p.writing_index += 1
        return self.current_writing_word(user_id)

    def all_finished_writing(self) -> bool:
        return all(
            p.writing_index >= len(self.game_words) for p in self.players.values()
        )

    # ── Guessing ──────────────────────────────────────────────────

    def start_guessing(self) -> None:
        """Build shuffled definition sets and transition to GUESSING."""
        self.phase = Phase.GUESSING
        self.word_definitions.clear()

        for word in self.game_words:
            defs: Dict[str, str] = {}
            defs["dictionary"] = self.dictionary[word]
            for p in self.players.values():
                if word in p.definitions:
                    defs[f"player_{p.user_id}"] = p.definitions[word]
            self.word_definitions[word] = defs

        for p in self.players.values():
            p.guessing_index = 0
            p.guesses.clear()

    def current_guessing_word(self, user_id: int) -> str | None:
        p = self.players[user_id]
        if p.guessing_index >= len(self.game_words):
            return None
        return self.game_words[p.guessing_index]

    def get_definitions_for_player(self, user_id: int, word: str) -> List[Tuple[str, str]]:
        """Return shuffled [(def_key, text)] excluding the player's own definition."""
        defs = self.word_definitions.get(word, {})
        items = [(k, v) for k, v in defs.items() if k != f"player_{user_id}"]
        random.shuffle(items)
        return items

    def submit_guess(self, user_id: int, def_key: str) -> str | None:
        """Record guess, advance index. Returns next word or None."""
        p = self.players[user_id]
        word = self.game_words[p.guessing_index]
        p.guesses[word] = def_key
        p.guessing_index += 1
        return self.current_guessing_word(user_id)

    def all_finished_guessing(self) -> bool:
        return all(
            p.guessing_index >= len(self.game_words) for p in self.players.values()
        )

    # ── Scoring ───────────────────────────────────────────────────

    def compute_scores(self) -> List[Tuple[int, int, str, int, List[str]]]:
        """Calculate final scores and return sorted results.

        Returns list of (place, user_id, display_name, total_score, [detail_strings])
        sorted desc by score.  Players with equal scores share the same place.
        """
        self.phase = Phase.FINISHED

        for word in self.game_words:
            for p in self.players.values():
                chosen = p.guesses.get(word)
                if chosen is None:
                    continue
                # +1 for guessing the dictionary definition
                if chosen == "dictionary":
                    p.score += 1
                # +2 to the author whose fake definition was picked
                if chosen and chosen.startswith("player_"):
                    author_id = int(chosen.split("_", 1)[1])
                    if author_id in self.players and author_id != p.user_id:
                        self.players[author_id].score += 2

        sorted_players = sorted(self.players.values(), key=lambda x: x.score, reverse=True)

        results: List[Tuple[int, int, str, int, List[str]]] = []
        prev_score: int | None = None
        place = 0
        for i, p in enumerate(sorted_players):
            if p.score != prev_score:
                place = i + 1
                prev_score = p.score
            details: List[str] = []
            for word in self.game_words:
                guess = p.guesses.get(word, "—")
                correct = "✅" if guess == "dictionary" else "❌"
                details.append(f"  {word}: {correct}")
            results.append((place, p.user_id, p.display_name, p.score, details))
        return results

    def get_word_results(self) -> List[Tuple[str, str, List[Tuple[str, str, List[str]]]]]:
        """Per-word breakdown: [(word, real_definition, [(def_key, text, [who_chose])])]."""
        breakdown = []
        for word in self.game_words:
            real_def = self.dictionary[word]
            defs_info = []
            for def_key, text in self.word_definitions[word].items():
                choosers = []
                for p in self.players.values():
                    if p.guesses.get(word) == def_key:
                        choosers.append(p.display_name)
                if def_key == "dictionary":
                    label = "📖 Словарное определение"
                else:
                    author_id = int(def_key.split("_", 1)[1])
                    author = self.players.get(author_id)
                    label = f"✍️ {author.display_name}" if author else "✍️ ???"
                defs_info.append((label, text, choosers))
            breakdown.append((word, real_def, defs_info))
        return breakdown
