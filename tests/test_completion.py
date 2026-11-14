"""Completion-time detection: English cue reachability and negated completions.

Each test pins a defect reproduced against the implementation that shipped
before this refactor.
"""
import pytest

from game_forensic.matcher import COMPLETION_CUE, estimate_completion_hours
from game_forensic.models import Game, Review


def _game(*texts: str, minutes: int = 3000) -> Game:
    """A game whose reviews all mention completion at the same playtime."""
    return Game(
        appid=1,
        name="Sample",
        reviews=[
            Review(id=str(index), text=text, positive=True, language="english",
                   minutes_at_review=minutes)
            for index, text in enumerate(texts)
        ],
    )


@pytest.mark.parametrize("text", [
    "100% completed",
    "I got 100% achievements",
    "100% achievements unlocked",
    "reached 100 % completion",
    "finished the game after 50 hours",
    "beat the game in 30 hours",
    "all achievements unlocked",
    "на 100% прошёл за 40 часов",
    "прошёл игру за 20 часов",
    "все достижения",
])
def test_completion_cues_are_reachable(text):
    """Every documented cue must actually match; `100%` previously never did."""
    assert COMPLETION_CUE.search(text), text


@pytest.mark.parametrize("text", [
    "the game is unfinished and buggy",
    "I have 200% fun with this",
])
def test_non_completion_text_is_not_a_cue(text):
    assert not COMPLETION_CUE.search(text), text


def test_english_percentage_cue_produces_an_estimate():
    """The reported bug: English `100%` reviews yielded no estimate at all."""
    assert estimate_completion_hours(_game("I got 100% achievements")) == 50.0


@pytest.mark.parametrize("text", [
    "never completed it, 300 hours in",
    "haven't finished it yet, 300 hours in",
    "didn't beat the game, gave up after 200 hours",
    "не прошёл, 300 часов наиграл",
])
def test_negated_completion_is_not_counted(text):
    """`never completed` must not be read as evidence of finishing."""
    assert estimate_completion_hours(_game(text)) is None


def test_negated_and_confirmed_reviews_use_only_the_confirmed_one():
    game = _game("never completed it", "finished the game", "haven't beaten it yet")
    assert estimate_completion_hours(game) == 50.0


def test_estimate_is_median_of_confirming_reviews():
    game = _game("finished it", "finished it", "finished it", minutes=600)
    game.reviews[0].minutes_at_review = 600
    game.reviews[1].minutes_at_review = 1200
    game.reviews[2].minutes_at_review = 1800
    assert estimate_completion_hours(game) == 20.0


def test_zero_playtime_is_still_ignored():
    game = _game("finished the game", minutes=0)
    assert estimate_completion_hours(game) is None
