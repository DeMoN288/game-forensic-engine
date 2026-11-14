import json

import pytest

from game_forensic import cli
from game_forensic.analyzer import Analyzer
from game_forensic.matcher import Matcher
from game_forensic.models import RecommendationReport


def test_argument_parsing():
    args = cli.build_parser().parse_args(["recommend", "1091500", "--max-candidates", "8", "--concurrency", "2"])
    assert args.command == "recommend" and args.appid == 1091500
    assert args.max_candidates == 8 and args.concurrency == 2
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["recommend", "0"])


def test_recommend_json_file_and_exit_code(monkeypatch, tmp_path):
    report = RecommendationReport(reference=1, raw_candidates=[], ranked=[], excluded={})

    async def run(args):
        assert args.appid == 1
        return report

    monkeypatch.setattr(cli, "run_recommendation", run)
    output = tmp_path / "report.json"
    assert cli.main(["recommend", "1", "--format", "json", "-o", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["reference"] == 1


def test_legacy_analyze_command(monkeypatch, capsys, game):
    async def run(args):
        assert args.command == "analyze"
        return Analyzer().assess(game(args.appid))

    monkeypatch.setattr(cli, "run_analysis", run)
    assert cli.main(["1"]) == 0
    assert "GTI:" in capsys.readouterr().out
    assert cli.main(["analyze", "1"]) == 0


async def test_table_has_required_columns_and_reasons(game, FakePipeline):
    report = await Matcher(FakePipeline({1: game(1, candidates=[2]), 2: game(2)})).recommend(1)
    text = cli.render_recommendations(report)
    for column in ["Название", "Сходство %", "GTI балл", "Почему подходит", "Предупреждения"]:
        assert column in text
    assert "100.0%" in text and "0.00/100" in text
    assert "Game 2" in text and "совпадение" in text


def test_config_error_is_safe(monkeypatch, capsys):
    async def run(args):
        raise ValueError("secret proxy credentials")

    monkeypatch.setattr(cli, "run_recommendation", run)
    assert cli.main(["recommend", "1"]) == 1
    assert "secret" not in capsys.readouterr().err
